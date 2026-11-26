#!/usr/bin/env python3
"""DD 重装后的初始化执行器（新建实例两阶段流程的第二阶段）。

流程（见 tgbot/boot_form.py 的 DD 开关）：
  阶段一（创建时）：user-data 是 DD 命令脚本（InstallNET.sh），实例
    首启即重装为 Debian 13（SSH 端口 14422、root 密码为开机时生成的
    UUID）。cloud-init 只在原系统跑一次，DD 后的新系统不会再跑它，
    所以初始化脚本不能放 user-data，必须由本模块在 DD 完成后经 SSH
    传上去执行。
  阶段二（本模块）：等 DD 完成（14422 能用 DD 密码 SSH 登录且
    /etc/os-release 是 Debian 13），再把初始化脚本传到实例上后台
    执行，轮询完成标记，回调通知调用方。

本模块不依赖 tgbot / web：进度与结果经 on_event 回调交给调用方
（Telegram / Bark 通知由调用方发），模块本身只做 SSH 与等待。
SSH 用 Paramiko 密码认证（DD 阶段只有密码可用，初始化脚本跑完
才装密钥并关掉密码登录）。
"""

import logging
import threading
import time

logger = logging.getLogger(__name__)

# ==================== 配置区（放文件顶端，方便修改）====================
# DD 目标系统与参数（与用户给的 DD 命令一致；密码由调用方传生成好的 UUID，
# 绝不在这里写死）。DD target system and parameters.
DD_OS = "debian"
DD_VERSION = "13"
DD_SSH_PORT = 14422
DD_SWAP_MB = 1024
DD_INSTALLNET_URL = ("https://raw.githubusercontent.com/leitbogioro/"
                     "Tools/master/Linux_reinstall/InstallNET.sh")

# 实例上的初始化脚本落盘路径与日志/完成标记。Paths on the instance.
INIT_SCRIPT_PATH = "/root/mbot-init.sh"
INIT_LOG_PATH = "/root/mbot-init.log"
INIT_DONE_PATH = "/root/mbot-init.done"

# 等待参数。DD 约 6-10 分钟，初始化脚本装一堆包常要 15-30 分钟，
# 超时放得宽；轮询间隔 30 秒。Timeouts and poll intervals.
DD_WAIT_TIMEOUT_SECONDS = 30 * 60
DD_WAIT_POLL_SECONDS = 30
INIT_RUN_TIMEOUT_SECONDS = 60 * 60
INIT_RUN_POLL_SECONDS = 30
SSH_CONNECT_TIMEOUT = 15
# =====================================================================


def build_dd_script(root_password):
    """生成 DD 命令脚本（user-data 首启脚本）。

    与用户给的命令一致：下载 InstallNET.sh，重装 Debian 13，
    SSH 端口 14422，swap 1024MB，root 密码用调用方生成的 UUID。
    另外在最后无条件 sleep 5 再 reboot：InstallNET.sh 上游结尾
    只打印 "Input 'reboot' ..." 就 exit 1，不会自动重启（上游把
    sleep 5 && reboot 注释掉了），不加这句 DD 会一直等不到新系统
    而 30 分钟超时。reboot 必须另起一行，不能接在 && 链里
    （InstallNET.sh 正常走完也 exit 1，接链里永远跑不到）；
    装失败时 reboot 只是原地重启回原系统，不会丢机器。
    返回带 #!/bin/bash 头的脚本字符串。
    """
    return (
        "#!/bin/bash\n"
        "wget --no-check-certificate -qO InstallNET.sh "
        f"'{DD_INSTALLNET_URL}'"
        " && chmod a+x InstallNET.sh"
        f" && bash InstallNET.sh -{DD_OS} {DD_VERSION}"
        f' -port "{DD_SSH_PORT}" -pwd \'{root_password}\''
        f' -swap "{DD_SWAP_MB}"\n'
        "sleep 5\n"
        "reboot\n"
    )


def _load_paramiko():
    """延迟导入 Paramiko（模块被无 Paramiko 环境 import 时不炸）。"""
    import paramiko  # noqa: PLC0415 - 延迟导入，见上
    return paramiko


def _ssh_connect(public_ip, port, password, timeout=SSH_CONNECT_TIMEOUT):
    """用 root + 密码连 SSH，返回已连接的 client（调用方负责 close）。

    连不上抛异常（调用方按“还没好”处理并重试）。
    """
    paramiko = _load_paramiko()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(hostname=public_ip, port=port, username="root",
                   password=password, timeout=timeout,
                   banner_timeout=timeout, auth_timeout=timeout,
                   look_for_keys=False, allow_agent=False)
    return client


def _run_ssh_command(client, command, timeout=30):
    """在已连接的 client 上跑一条命令，返回 (stdout, stderr)。"""
    _, stdout, stderr = client.exec_command(command, timeout=timeout)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    return out, err


def _is_debian13(client):
    """检查实例系统是否为 Debian 13（读 /etc/os-release）。"""
    try:
        out, _ = _run_ssh_command(client, "cat /etc/os-release")
    except Exception:  # noqa: BLE001 - 读不到按没装好处理
        return False
    fields = {}
    for line in out.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            fields[key.strip()] = value.strip().strip('"')
    return fields.get("ID") == "debian" and fields.get("VERSION_ID") == "13"


def wait_for_dd(public_ip, password, port=DD_SSH_PORT,
                timeout=DD_WAIT_TIMEOUT_SECONDS,
                poll_interval=DD_WAIT_POLL_SECONDS,
                sleep=time.sleep, stop_event=None):
    """等 DD 完成：14422 能用 DD 密码 SSH 登录且系统是 Debian 13。

    返回 True（DD 完成）或 False（超时/被取消）。期间不抛异常，
    连接失败都按“还没好”处理继续轮询。
    """
    deadline = time.monotonic() + timeout
    while True:
        if stop_event is not None and stop_event.is_set():
            return False
        try:
            client = _ssh_connect(public_ip, port, password)
            try:
                if _is_debian13(client):
                    return True
            finally:
                client.close()
        except Exception as exc:  # noqa: BLE001 - 连不上就是还没好
            logger.debug("DD 等待中 SSH 未通 %s:%s：%s",
                         public_ip, port, exc)
        if time.monotonic() >= deadline:
            return False
        sleep(poll_interval)


def run_init_script(public_ip, password, script_content,
                    port=DD_SSH_PORT,
                    timeout=INIT_RUN_TIMEOUT_SECONDS,
                    poll_interval=INIT_RUN_POLL_SECONDS,
                    sleep=time.sleep, stop_event=None):
    """把初始化脚本传到实例上后台执行，等完成标记。

    返回 (ok, exit_code, log_tail)：ok 为 True 表示脚本跑完且退出码
    为 0；exit_code 为 None 表示超时/被取消。脚本以 nohup 后台跑，
    日志写 INIT_LOG_PATH，跑完写 INIT_DONE_PATH（内容为退出码）。
    """
    paramiko = _load_paramiko()
    client = _ssh_connect(public_ip, port, password)
    try:
        sftp = client.open_sftp()
        try:
            with sftp.file(INIT_SCRIPT_PATH, "w") as f:
                f.write(script_content)
            sftp.chmod(INIT_SCRIPT_PATH, 0o755)
        finally:
            sftp.close()
        # 后台跑：日志与退出码落盘，调用方轮询完成标记。
        _run_ssh_command(
            client,
            "nohup bash -c 'bash "
            f"{INIT_SCRIPT_PATH} > {INIT_LOG_PATH} 2>&1; "
            f"echo $? > {INIT_DONE_PATH}' >/dev/null 2>&1 &")
    finally:
        client.close()
    deadline = time.monotonic() + timeout
    while True:
        if stop_event is not None and stop_event.is_set():
            return False, None, ""
        try:
            client = _ssh_connect(public_ip, port, password)
            try:
                out, _ = _run_ssh_command(
                    client, f"test -f {INIT_DONE_PATH} && cat {INIT_DONE_PATH}")
                if out.strip() != "":
                    code = int(out.strip().split()[0])
                    log_out, _ = _run_ssh_command(
                        client, f"tail -n 50 {INIT_LOG_PATH}")
                    return code == 0, code, log_out
            finally:
                client.close()
        except Exception as exc:  # noqa: BLE001 - 轮询失败继续等
            logger.debug("初始化轮询失败 %s：%s", public_ip, exc)
        if time.monotonic() >= deadline:
            return False, None, ""
        sleep(poll_interval)


def run_dd_init(public_ip, password, init_script, instance_name="",
                port=DD_SSH_PORT, on_event=None, stop_event=None):
    """DD 等待 + 初始化执行的完整流程（同步，会阻塞到结束）。

    on_event(event_type, message)：进度回调，event_type 为
    "dd_done" / "init_started" / "init_done" / "init_failed" /
    "dd_timeout" / "init_timeout"。返回 True（初始化成功）或 False。
    """
    def _emit(event_type, message):
        if on_event is not None:
            try:
                on_event(event_type, message)
            except Exception as exc:  # noqa: BLE001 - 回调失败不拖垮流程
                logger.warning("DD 回调失败：%s", exc)

    _emit("dd_waiting", f"实例 {instance_name}（{public_ip}）DD 进行中，"
                        "等待 Debian 13 就绪……")
    if not wait_for_dd(public_ip, password, port=port,
                       stop_event=stop_event):
        _emit("dd_timeout", f"实例 {instance_name}（{public_ip}）DD 等待"
                            "超时（30 分钟），未检测到 Debian 13。")
        return False
    _emit("dd_done", f"实例 {instance_name}（{public_ip}）DD 完成，"
                     "Debian 13 已就绪，开始执行初始化脚本。")
    ok, code, log_tail = run_init_script(
        public_ip, password, init_script, port=port, stop_event=stop_event)
    if ok:
        _emit("init_done", f"实例 {instance_name}（{public_ip}）初始化"
                           "脚本执行完成。")
        return True
    if code is None:
        _emit("init_timeout", f"实例 {instance_name}（{public_ip}）初始化"
                              "脚本执行超时（60 分钟）。")
    else:
        _emit("init_failed", f"实例 {instance_name}（{public_ip}）初始化"
                             f"脚本退出码 {code}。\n{log_tail}")
    return False


def start_dd_init(public_ip, password, init_script, instance_name="",
                  port=DD_SSH_PORT, on_event=None, stop_event=None):
    """开 daemon 线程跑 run_dd_init，立即返回线程对象。"""
    thread = threading.Thread(
        target=run_dd_init,
        args=(public_ip, password, init_script, instance_name),
        kwargs={"port": port, "on_event": on_event,
                "stop_event": stop_event},
        daemon=True,
        name=f"dd-init-{instance_name or public_ip}")
    thread.start()
    return thread
