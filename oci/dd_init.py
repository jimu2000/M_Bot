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
    执行，轮询完成标记，回调通知调用方。脚本跑完后强制把密码登录
    开回来（公开版人人都要用 root 密码登录），再用密码 SSH 进去
    验证状态（密码登录可用、sshd 运行中），验证结果随 init_done
    回调交给调用方发双通道通知。

本模块不依赖 tgbot / web：进度与结果经 on_event 回调交给调用方
（Telegram / Bark 通知由调用方发），模块本身只做 SSH 与等待。
SSH 用 Paramiko 密码认证（DD 阶段只有密码可用）。
"""

import logging
import os
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
# root 登录的 sshd drop-in（与 build_user_data 用的同一路径）：
# 初始化脚本跑完后强制写回这里，把密码登录开回来。
ROOT_LOGIN_CONF = "/etc/ssh/sshd_config.d/60-mbot-root-login.conf"

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


def upload_files(public_ip, password, files, port=DD_SSH_PORT,
                 timeout=SSH_CONNECT_TIMEOUT):
    """DD 完成后用 root 密码 SSH（SFTP）把文件传到实例上。

    files 是列表，每项是 dict：{"local": 本地路径, "remote": 目标路径,
    "mode": 权限（可选，默认 0644）} 或 {"content": 文本内容,
    "remote": 目标路径, "mode": 权限（可选）}。DD 初始化脚本要用的
    文件（流量看门狗、安全守护脚本、自动升级脚本、Bark 配置）靠这个
    传上去，而不是把大段代码塞进初始化脚本字符串里。
    返回 (uploaded_count, error_or_None)。
    """
    if not files:
        return 0, None
    try:
        client = _ssh_connect(public_ip, port, password, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - 连不上就报给调用方
        return 0, f"SSH 连接失败：{exc}"
    try:
        sftp = client.open_sftp()
    except Exception as exc:  # noqa: BLE001 - SFTP 打不开就报给调用方
        try:
            client.close()
        except Exception:  # noqa: BLE001 - 关闭失败忽略
            pass
        return 0, f"SFTP 打开失败：{exc}"
    uploaded = 0
    try:
        for entry in files:
            remote = entry.get("remote")
            mode = entry.get("mode", 0o644)
            if not remote:
                continue
            try:
                if "content" in entry:
                    # Write content via a temp file (SFTP has no direct
                    # write-string). 内容经临时文件写（SFTP 没有直接写字符串）。
                    import tempfile
                    fd, tmp = tempfile.mkstemp()
                    try:
                        with os.fdopen(fd, "w", encoding="utf-8") as f:
                            f.write(entry["content"])
                        sftp.put(tmp, remote)
                    finally:
                        try:
                            os.unlink(tmp)
                        except OSError:
                            pass
                else:
                    sftp.put(entry["local"], remote)
                try:
                    sftp.chmod(remote, mode)
                except Exception:  # noqa: BLE001 - chmod 失败不阻断
                    pass
                uploaded += 1
            except Exception as exc:  # noqa: BLE001 - 单个文件失败就停
                return uploaded, f"传文件 {remote} 失败：{exc}"
        return uploaded, None
    finally:
        try:
            sftp.close()
        except Exception:  # noqa: BLE001 - 关闭失败忽略
            pass
        try:
            client.close()
        except Exception:  # noqa: BLE001 - 关闭失败忽略
            pass
    """DD 完成后用 root 密码 SSH 进去，把公钥追加到 authorized_keys。

    DD 会抹掉开机时 cloud-init 塞的 SSH 公钥（只剩 root 密码能登），
    不补回去看门狗 / CPU 检测 / 一键 SSH 全都连不上。幂等地追加
    （已在文件里的跳过）；返回 (added_count, error_or_None)。
    keys 是公钥字符串列表（用户 keys + guard 公钥）。
    """
    keys = [k.strip() for k in (keys or []) if k and k.strip()]
    if not keys:
        return 0, None
    try:
        client = _ssh_connect(public_ip, port, password, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - 连不上就报给调用方
        return 0, f"SSH 连接失败：{exc}"
    try:
        added = 0
        for key in keys:
            parts = key.split()
            marker = parts[1][:40] if len(parts) > 1 else key[:40]
            # Single-quoted marker is safe (base64 has no quote); the key
            # itself goes through repr() so any quote in it is escaped.
            # marker 是 base64 无引号；key 本身用 repr() 转义后写进命令。
            # The shell prints ADDED only when it actually appends.
            # 只有真正追加时才打印 ADDED，已存在时打印 EXISTS。
            cmd = (
                "mkdir -p /root/.ssh && chmod 700 /root/.ssh && "
                "touch /root/.ssh/authorized_keys && "
                "chmod 600 /root/.ssh/authorized_keys && "
                f"if grep -q '{marker}' /root/.ssh/authorized_keys; then "
                f"echo EXISTS; else echo {key!r} >> /root/.ssh/authorized_keys && echo ADDED; fi"
            )
            try:
                out, _err = _run_ssh_command(client, cmd, timeout=30)
            except Exception as exc:  # noqa: BLE001 - 单条失败不拖垮其余
                return added, f"写公钥失败：{exc}"
            if "ADDED" in out:
                added += 1
        return added, None
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001 - 关闭失败忽略
            pass


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
        # 用户的初始化脚本通常会在最后关掉密码登录（只留密钥），那样
        # 开机时给的 root 密码就没用了，而且轮询完成标记的 SSH 也会断
        # （误报超时）。所以脚本跑完后强制把密码登录开回来
        # （PermitRootLogin yes + PasswordAuthentication yes，reload
        # sshd），退出码仍取用户脚本的，失败上报不受影响。
        inner = (
            f"bash {INIT_SCRIPT_PATH} > {INIT_LOG_PATH} 2>&1; "
            "CODE=$?; "
            "printf \"PermitRootLogin yes\\nPasswordAuthentication yes\\n\" "
            f"> {ROOT_LOGIN_CONF}; "
            "systemctl reload ssh || systemctl reload sshd || true; "
            f"echo $CODE > {INIT_DONE_PATH}"
        )
        _run_ssh_command(
            client,
            f"nohup bash -c '{inner}' >/dev/null 2>&1 &")
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


def verify_password_login(public_ip, password, port=DD_SSH_PORT,
                          require_dropin=True):
    """初始化完成后验证：用 root 密码 SSH 登录，确认密码登录可用且系统就绪。

    登录本身即证明密码可用；再读状态（Debian 13、sshd 运行中、
    drop-in 含 PasswordAuthentication yes）。返回 (ok, detail)，
    detail 是给通知用的状态说明。登录失败或状态不符都返回 False
    （脚本本身已跑完，验证失败只影响通知里的提醒，不改 init 结果）。

    require_dropin=False 时不要求 drop-in 存在：没跑初始化脚本时
    DD 后的系统里没有这份 drop-in，但 InstallNET 装好的系统本身已
    开密码登录，SSH 登录成功即证明可用。
    """
    try:
        client = _ssh_connect(public_ip, port, password)
    except Exception as exc:  # noqa: BLE001 - 登录失败即验证不通过
        return False, f"密码 SSH 登录失败：{exc}"
    try:
        cmd = ("grep -E '^(ID|VERSION_ID)=' /etc/os-release; "
               "systemctl is-active ssh || systemctl is-active sshd")
        if require_dropin:
            cmd += f"; grep -h PasswordAuthentication {ROOT_LOGIN_CONF}"
        out, _ = _run_ssh_command(client, cmd)
    except Exception as exc:  # noqa: BLE001 - 读状态失败即验证不通过
        return False, f"读实例状态失败：{exc}"
    finally:
        client.close()
    ok = ("ID=debian" in out and "VERSION_ID=\"13\"" in out
          and "active" in out)
    if require_dropin:
        ok = ok and "PasswordAuthentication yes" in out
    if ok:
        return True, "root 密码登录已验证可用，sshd 运行中。"
    return False, f"状态不符：{out.strip()}"


def run_dd_init(public_ip, password, init_script, instance_name="",
                port=DD_SSH_PORT, on_event=None, stop_event=None,
                ssh_keys=None, files=None):
    """DD 等待 + 初始化执行的完整流程（同步，会阻塞到结束）。

    ssh_keys：DD 完成后要补回 authorized_keys 的公钥字符串列表
    （用户 keys + guard 公钥）。DD 会抹掉开机时 cloud-init 塞的
    公钥，不补看门狗 / CPU 检测全连不上；补钥匙失败不阻断初始化，
    只写进通知提醒用户手动补。
    files：DD 完成后要传到实例上的文件列表（见 upload_files：每项
    {"local"/"content", "remote", "mode"}，如流量看门狗、安全守护
    脚本、Bark 配置）。传文件失败不阻断初始化，只写进通知提醒。
    init_script 为空时只等 DD 完成并验证系统就绪（不跑脚本），同样
    发 DD 完成与初始化完成通知。on_event(event_type, message)：进度
    回调，event_type 为 "dd_done" / "init_started" / "init_done" /
    "init_failed" / "dd_timeout" / "init_timeout"。返回 True（初始化
    成功）或 False。
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
    # DD done: re-add SSH public keys (DD wiped the boot-time authorized_keys, leaving only password login). DD 完成：把公钥补回 authorized_keys（DD 抹掉了开机时塞的公钥，只剩密码能登）；补失败不阻断初始化，只提醒。
    keys_note = ""
    if ssh_keys:
        added, key_err = add_ssh_keys(public_ip, password, ssh_keys,
                                      port=port)
        if key_err:
            keys_note = f"公钥补回失败（{key_err}），看门狗与一键 SSH 可能连不上，请手动把公钥加进 /root/.ssh/authorized_keys。"
        elif added:
            keys_note = f"已把 {added} 条 SSH 公钥补回 authorized_keys。"
        else:
            keys_note = "SSH 公钥已在 authorized_keys 里，无需补回。"
    # Upload files (traffic watchdog, guard script, Bark config, ...).
    # 传文件（流量看门狗、安全守护脚本、Bark 配置等）；失败不阻断，只提醒。
    if files:
        uploaded, file_err = upload_files(public_ip, password, files,
                                          port=port)
        if file_err:
            keys_note += f"文件上传失败（{file_err}），部分安全功能可能没装上。"
        elif uploaded:
            keys_note += f"已传 {uploaded} 个安全文件到实例。"
    if not init_script:
        # 无初始化脚本：DD 已完成，只验证系统就绪（密码登录可用、sshd
        # 运行中），同样发完成通知。No init script: DD is done; verify
        # the system is ready and notify.
        _emit("dd_done", f"实例 {instance_name}（{public_ip}）DD 完成，"
                         "Debian 13 已就绪，无初始化脚本。")
        verified, detail = verify_password_login(
            public_ip, password, port=port, require_dropin=False)
        if verified:
            _emit("init_done", f"实例 {instance_name}（{public_ip}）DD 完成，"
                               f"无初始化脚本。{detail}{keys_note}")
        else:
            _emit("init_done", f"实例 {instance_name}（{public_ip}）DD 完成，"
                               f"无初始化脚本，但密码登录验证失败：{detail}{keys_note}")
        return True
    _emit("dd_done", f"实例 {instance_name}（{public_ip}）DD 完成，"
                     "Debian 13 已就绪，开始执行初始化脚本。")
    ok, code, log_tail = run_init_script(
        public_ip, password, init_script, port=port, stop_event=stop_event)
    if ok:
        # 脚本跑完了：M_Bot 再用 root 密码 SSH 进去验一下状态（密码
        # 登录可用、sshd 运行中），验证结果写进 init_done 通知里。
        # 脚本本身已成功，验证失败不改 init 结果，只提醒用户检查。
        verified, detail = verify_password_login(
            public_ip, password, port=port)
        if verified:
            _emit("init_done", f"实例 {instance_name}（{public_ip}）初始化"
                               f"脚本执行完成。{detail}{keys_note}")
        else:
            _emit("init_done", f"实例 {instance_name}（{public_ip}）初始化"
                               f"脚本执行完成，但密码登录验证失败：{detail}{keys_note}")
        return True
    if code is None:
        _emit("init_timeout", f"实例 {instance_name}（{public_ip}）初始化"
                              "脚本执行超时（60 分钟）。")
    else:
        _emit("init_failed", f"实例 {instance_name}（{public_ip}）初始化"
                             f"脚本退出码 {code}。\n{log_tail}")
    return False


def start_dd_init(public_ip, password, init_script, instance_name="",
                  port=DD_SSH_PORT, on_event=None, stop_event=None,
                  ssh_keys=None, files=None):
    """开 daemon 线程跑 run_dd_init，立即返回线程对象。"""
    thread = threading.Thread(
        target=run_dd_init,
        args=(public_ip, password, init_script, instance_name),
        kwargs={"port": port, "on_event": on_event,
                "stop_event": stop_event, "ssh_keys": ssh_keys,
                "files": files},
        daemon=True,
        name=f"dd-init-{instance_name or public_ip}")
    thread.start()
    return thread
