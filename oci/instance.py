#!/usr/bin/env python3
"""实例管理：列出、查询、创建、启停、重启、终止 OCI 计算实例。

全部函数接受一个 oci.client.OCIClient 对象作为第一个参数，命令通过它走
官方 OCI CLI 执行（认证由 OCI CLI 自己处理，本模块不读私钥）。

安全说明（重要）：
  - terminate_instance 默认永久删除引导卷（--preserve-boot-volume false），
    这是硬规则：终止实例就不要留着引导卷继续扣钱。
  - 配置区在文件顶端，真实 OCID / 镜像 ID 一律用占位符，绝不写进代码。

用法示例（占位符）：
    from oci.client import OCIClient
    from oci.instance import list_instances, get_instance_state
    cli = OCIClient(profile="DEFAULT", region="ap-singapore-1")
    instances = list_instances(cli, "ocid1.tenancy.oc1..xxxxxxxx")
    state = get_instance_state(cli, "ocid1.instance.oc1..xxxxxxxx")
"""

import json
import os

# ================= 配置区（改这里） =================
# Default compartment OCID (the root compartment is the tenancy OCID; placeholder). 默认 compartment OCID（根 compartment 即 tenancy OCID，占位符）。
DEFAULT_COMPARTMENT_ID = "ocid1.tenancy.oc1..xxxxxxxx"
# Default instance shape (commonly used on the free tier). 默认实例规格（免费层常用）。
DEFAULT_SHAPE = "VM.Standard.E2.1.Micro"
# Default image OCID (placeholder; find the real value with oci compute image list). 默认镜像 OCID（占位符，真实值用 oci compute image list 查）。
DEFAULT_IMAGE_ID = "ocid1.image.oc1..xxxxxxxx"
# Default subnet OCID (placeholder). 默认子网 OCID（占位符）。
DEFAULT_SUBNET_ID = "ocid1.subnet.oc1..xxxxxxxx"
# Default availability domain (placeholder, e.g. AD-1). 默认可用域（占位符，如 AD-1）。
DEFAULT_AVAILABILITY_DOMAIN = "AD-1"
# Assign a public IP by default. 默认分配公网 IP。
DEFAULT_ASSIGN_PUBLIC_IP = True
# Max instance display-name length (OCI caps at 255 chars; used by the rename check). 实例显示名的最大长度（OCI 限制 255 字符，改名时校验用）。
MAX_DISPLAY_NAME_LEN = 255
# =====================================================

# The slim fields list_instances returns. list_instances 返回的精简字段。
_LIST_FIELDS = ("id", "display-name", "shape", "lifecycle-state",
                "time-created")


def _unwrap(data):
    """OCI CLI 的 get/list 响应通常包一层 {"data": ...}，这里解包。"""
    if isinstance(data, dict) and "data" in data:
        return data["data"]
    return data


def list_instances(cli, compartment_id, include_shape_config=False):
    """列出 compartment 下的实例，返回精简列表。

    每项只保留 id / display-name / shape / lifecycle-state / time-created。
    include_shape_config=True 时每项额外保留 shape-config
    （{ocpus, memory-in-gbs}，Web 实例列表的 OCPU/内存列用；默认不带，
    保持原有精简形状不变）。
    """
    result = cli.run(["compute", "instance", "list",
                      "--compartment-id", compartment_id])
    items = _unwrap(result) or []
    fields = (_LIST_FIELDS + ("shape-config",) if include_shape_config
              else _LIST_FIELDS)
    return [{field: item.get(field) for field in fields}
            for item in items]


def get_instance(cli, instance_id):
    """查单个实例详情，返回 OCI CLI 返回的完整 JSON。"""
    return cli.run(["compute", "instance", "get",
                    "--instance-id", instance_id])


def get_instance_state(cli, instance_id):
    """只返回实例的 lifecycle-state 字符串（如 RUNNING / STOPPED）。

    实例不存在时抛 NotFoundError（OCI 返回 NotAuthorizedOrNotFound）。
    """
    result = _unwrap(get_instance(cli, instance_id)) or {}
    return result.get("lifecycle-state")


def image_arch_from_name(display_name):
    """从镜像显示名推断架构：含 aarch64/arm64 -> aarch64；含
    x86_64/amd64 -> x86_64；否则返回 None（推断不出）。

    OCI 的 image get 没有 architecture 字段，只能看名字
    （如 Canonical-Ubuntu-24.04-aarch64-...）。
    """
    if not isinstance(display_name, str):
        return None
    name = display_name.lower()
    if "aarch64" in name or "arm64" in name:
        return "aarch64"
    if "x86_64" in name or "amd64" in name:
        return "x86_64"
    return None


def get_image_arch(cli, image_id):
    """查镜像的架构（image get 的 display-name 推断）。

    查不到、镜像不存在或推断不出时返回 None——调用方跳过校验
    不阻塞创建（抢机失败时 OCI 自己报错）。
    """
    try:
        image = cli.run(["compute", "image", "get",
                         "--image-id", image_id])
        return image_arch_from_name((image or {}).get("display-name"))
    except Exception:  # noqa: BLE001 - 查不到就跳过校验
        return None


def launch_instance(cli, compartment_id, availability_domain, shape,
                    image_id, subnet_id, display_name, ssh_keys_file,
                    assign_public_ip=True, no_retry=True):
    """创建实例，返回新实例的 OCID。

    --no-retry 是必须的：OCI CLI 默认内部重试会把单次调用拖到约 110 秒，
    抢机场景下重试由编排器自己控制，不要让 CLI 替我们等。
    """
    args = ["compute", "instance", "launch",
            "--availability-domain", availability_domain,
            "--compartment-id", compartment_id,
            "--shape", shape,
            "--image-id", image_id,
            "--subnet-id", subnet_id,
            "--display-name", display_name,
            "--ssh-authorized-keys-file", ssh_keys_file,
            "--assign-public-ip", "true" if assign_public_ip else "false"]
    if no_retry:
        args.append("--no-retry")
    result = _unwrap(cli.run(args)) or {}
    return result.get("id")


def start_instance(cli, instance_id):
    """启动实例（oci compute instance action --action START）。"""
    cli.run(["compute", "instance", "action",
             "--instance-id", instance_id, "--action", "START"])


def stop_instance(cli, instance_id):
    """停止实例（--action STOP）。"""
    cli.run(["compute", "instance", "action",
             "--instance-id", instance_id, "--action", "STOP"])


def restart_instance(cli, instance_id):
    """重启实例（--action RESET）。"""
    cli.run(["compute", "instance", "action",
             "--instance-id", instance_id, "--action", "RESET"])


def terminate_instance(cli, instance_id, preserve_boot_volume=False):
    """终止实例。

    默认 preserve_boot_volume=False，即 --preserve-boot-volume false：
    终止时永久删除引导卷，不留着继续扣钱。这是硬规则，不要改默认值。
    只有明确要保留引导卷时才传 preserve_boot_volume=True。
    --force 用于跳过交互确认（subprocess 调用是非交互的）。
    """
    cli.run(["compute", "instance", "terminate",
             "--instance-id", instance_id,
             "--preserve-boot-volume",
             "true" if preserve_boot_volume else "false",
             "--force"])


def _run_batch(cli, instance_ids, func, **kwargs):
    """批量操作公共逻辑：逐台调 func，单台失败不影响其他台。

    返回 {"succeeded": [instance_id, ...],
          "failed": [{"id": instance_id, "error": str(exc)}, ...]}。
    空列表返回空汇总，不调任何命令。
    """
    succeeded = []
    failed = []
    for instance_id in instance_ids:
        try:
            func(cli, instance_id, **kwargs)
            succeeded.append(instance_id)
        except Exception as exc:  # noqa: BLE001 - 单台失败记下来继续下一台
            failed.append({"id": instance_id, "error": str(exc)})
    return {"succeeded": succeeded, "failed": failed}


def batch_start(cli, instance_ids):
    """批量启动实例（逐台调 start_instance，汇总每台结果）。"""
    return _run_batch(cli, instance_ids, start_instance)


def batch_stop(cli, instance_ids):
    """批量停止实例（逐台调 stop_instance，汇总每台结果）。"""
    return _run_batch(cli, instance_ids, stop_instance)


def batch_restart(cli, instance_ids):
    """批量重启实例（逐台调 restart_instance，汇总每台结果）。"""
    return _run_batch(cli, instance_ids, restart_instance)


def batch_terminate(cli, instance_ids):
    """批量终止实例，默认永久删除各实例的引导卷（硬规则）。

    逐台调 terminate_instance(preserve_boot_volume=False)。
    """
    return _run_batch(cli, instance_ids, terminate_instance,
                      preserve_boot_volume=False)


def rename_instance(cli, instance_id, new_name):
    """更改实例显示名（compute instance update --display-name）。

    --force 必加：不加时 OCI CLI 会交互确认，subprocess 无 stdin
    应答会一直挂到超时（2026-11-23 真实验证教训）。
    新名字为空或超过 255 字符时抛 ValueError。
    返回更新后的实例详情字典（已解包 data）。
    """
    name = (new_name or "").strip()
    if not name:
        raise ValueError("新实例名不能为空")
    if len(name) > MAX_DISPLAY_NAME_LEN:
        raise ValueError(
            f"新实例名不能超过 {MAX_DISPLAY_NAME_LEN} 字符")
    result = cli.run(["compute", "instance", "update",
                      "--instance-id", instance_id,
                      "--display-name", name,
                      "--force"])
    return _unwrap(result) or {}


def resize_instance(cli, instance_id, ocpus, memory_gb):
    """升/降配 A1.Flex 实例（compute instance update --shape-config）。

    与 Telegram 菜单 5 的流程一致（tgbot/handlers_extra.py 的
    handle_confirm/resize，2026-11-23 真实验证通过）：直接 update，
    不先关机——OCI 会在改配置过程中自动重启实例。--force 必加：
    不加时 OCI CLI 会交互确认，subprocess 无 stdin 应答会一直挂到
    超时（同类教训见 rename_instance）。

    调用方需先确认实例是 VM.Standard.A1.Flex（E2.1.Micro 是固定
    规格不支持升降配）且规格在免费额度内（见 oci/quota.py 的
    check_a1_single_instance_cap）；本函数只校验数值本身为正整数。
    返回更新后的实例详情字典（已解包 data）。
    """
    if not isinstance(ocpus, int) or isinstance(ocpus, bool) or ocpus < 1:
        raise ValueError("OCPU 必须是大于 0 的整数")
    if not isinstance(memory_gb, int) or isinstance(memory_gb, bool) \
            or memory_gb < 1:
        raise ValueError("内存必须是大于 0 的整数（GB）")
    shape_config = json.dumps({
        "ocpus": ocpus,
        "memoryInGBs": memory_gb,
    })
    result = cli.run(["compute", "instance", "update",
                      "--instance-id", instance_id,
                      "--shape-config", shape_config,
                      "--force"])
    return _unwrap(result) or {}


def reinstall_instance(cli, instance_id, image_id=None):
    """重装系统：用 compute instance update --source-details 重建引导卷。

    警告：重装会清空系统盘（引导卷）上的全部数据，不可恢复；
    实例 OCID 与 IP 地址（公网/内网）保留不变。
    image_id 不传时用实例当前镜像重装（先 get 查 source-details
    里的 image-id，查不到时抛 ValueError）；传了则换成指定镜像重装。
    --force 必加（同 rename_instance）。
    返回更新后的实例详情字典（已解包 data）。
    """
    if image_id is None:
        detail = _unwrap(get_instance(cli, instance_id)) or {}
        source = detail.get("source-details") or {}
        image_id = source.get("image-id")
        if not image_id:
            raise ValueError(
                "无法获取当前实例的镜像 ID，请显式传入 image_id")
    source_details = json.dumps({
        "sourceType": "image",
        "imageId": image_id,
    })
    result = cli.run(["compute", "instance", "update",
                      "--instance-id", instance_id,
                      "--source-details", source_details,
                      "--force"])
    return _unwrap(result) or {}


def get_serial_console(cli, instance_id, ssh_public_key_file=None):
    """获取串行控制台连接。

    两步：先 create instance-console-connection（返回连接串与指纹），
    再 get 确认连接状态（create 未返回 id 时跳过 get）。
    返回精简字典：
        {"id", "connection-string", "fingerprint",
         "service-host-key-fingerprint", "lifecycle-state"}。
    connection-string 是完整的 ssh 连接命令，用实例的 SSH 密钥连接。

    ssh_public_key_file：串行控制台登录用的 SSH 公钥文件（OCI CLI
    必填参数 --ssh-public-key-file，缺了报 Missing option）。
    不传时依次取环境变量 MBOT_CONSOLE_SSH_KEY_FILE、~/.ssh/id_ed25519.pub；
    都没有时抛 ValueError（调用方应提示用户先配置）。
    """
    key_file = (ssh_public_key_file
                or os.environ.get("MBOT_CONSOLE_SSH_KEY_FILE")
                or os.path.expanduser("~/.ssh/id_ed25519.pub"))
    if not os.path.isfile(key_file):
        raise ValueError(
            f"串行控制台需要 SSH 公钥文件，未找到：{key_file}。"
            "请设环境变量 MBOT_CONSOLE_SSH_KEY_FILE 指向一个公钥文件。")
    created = _unwrap(cli.run(
        ["compute", "instance-console-connection", "create",
         "--instance-id", instance_id,
         "--ssh-public-key-file", key_file])) or {}
    connection_id = created.get("id")
    if connection_id:
        detail = _unwrap(cli.run(
            ["compute", "instance-console-connection", "get",
             "--instance-console-connection-id", connection_id])) or {}
        for key, value in detail.items():
            if value is not None:
                created[key] = value
    return {
        "id": created.get("id"),
        "connection-string": created.get("connection-string"),
        "fingerprint": created.get("fingerprint"),
        "service-host-key-fingerprint":
            created.get("service-host-key-fingerprint"),
        "lifecycle-state": created.get("lifecycle-state"),
    }


def find_instance_by_name(cli, compartment_id, display_name):
    """按显示名找实例，返回精简字典；找不到返回 None。

    用途：创建成功但响应丢失时，用它确认实例是否已存在，避免重复建。
    """
    for instance in list_instances(cli, compartment_id):
        if instance.get("display-name") == display_name:
            return instance
    return None
