#!/usr/bin/env python3
"""启动卷备份与恢复：备份系统启动卷、从备份恢复出新实例。

M_Bot 独有功能：备份与恢复入口。

原创实现，参考 OCI CLI 公开命令（oci bv boot-volume-backup ... /
oci bv boot-volume create / oci compute instance launch）编写。
所有函数都接受一个 oci.client.OCIClient 对象作为第一个参数。

恢复流程（两步，OCI API 不支持直接从备份启动实例）：
  1. 用备份创建一个新的启动卷：
     oci bv boot-volume create --boot-volume-backup-id <备份 OCID>
       --availability-domain <AD> --compartment-id <OCID>
       --wait-for-state AVAILABLE
     （launch 的 --source-details 只有 image / bootVolume 两种，
     没有 bootVolumeBackup——已用本地 OCI SDK 与 CLI 的
     --generate-param-json-input 双重核实，2026-11-24。）
  2. 用新启动卷启动一台新实例（原机不动）：
     oci compute instance launch --source-boot-volume-id <新盘 OCID>
       --shape <形状> [--shape-config ...] --subnet-id <子网>
       --display-name <新实例名> --assign-public-ip true --no-retry
  第 2 步失败时尽力删除第 1 步建出来的启动卷，不留孤儿盘占额度。

安全说明（重要）：
  - 本模块不读取、不打印、不外传 API 私钥。认证全部由 OCI CLI 自己读
    配置文件（~/oci_config，旧部署回退 ~/.oci/config）与 key_file
    完成，私钥只在本地用于请求签名。
  - 配置区在文件顶端，真实 OCID / fingerprint 一律用占位符，绝不写进代码。
  - 备份与恢复都是写操作，只在用户明确要求时调用；删除备份前调用方
    应先做二次确认（备份删了不可恢复）。

用法示例（占位符）：
    from oci.client import OCIClient
    from oci.boot_backup import (
        check_backup_quota, create_boot_volume_backup,
        list_boot_volume_backups, restore_boot_volume_backup)
    cli = OCIClient(profile="DEFAULT", region="ap-singapore-1")
    count, limit, full, oldest = check_backup_quota(
        cli, "ocid1.tenancy.oc1..xxxxxxxx")
    backup = create_boot_volume_backup(
        cli, "ocid1.bootvolume.oc1..xxxxxxxx")
    result = restore_boot_volume_backup(
        cli, "ocid1.tenancy.oc1..xxxxxxxx", backup["id"],
        availability_domain="xxxx:AP-SINGAPORE-1-AD-1",
        subnet_id="ocid1.subnet.oc1..xxxxxxxx",
        display_name="restored-instance",
        ssh_keys_file="/path/to/keys.pub")
"""

import json
import time
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9 没有 zoneinfo
    ZoneInfo = None

# ================= 配置区（改这里） =================
# 默认 compartment OCID（占位符）。根 compartment 就是 tenancy OCID。
DEFAULT_COMPARTMENT_ID = "ocid1.tenancy.oc1..xxxxxxxx"
# 默认区域（如 ap-singapore-1 / us-sanjose-1 / us-chicago-1）。
DEFAULT_REGION = "ap-singapore-1"
# 免费额度：Always Free 账号每个租户最多 5 个卷备份（启动卷备份与
# 块存储卷备份共用这个额度；本模块只数启动卷备份）。
FREE_BACKUP_LIMIT = 5
# 备份类型：只做 FULL 全量（INCREMENTAL 增量依赖链式备份，恢复时
# 必须整条链都在，删最早备份会断链；全量备份各自独立，可单独删除）。
BACKUP_TYPE = "FULL"
# 恢复时形状查不到的默认值：A1.Flex 1 核 / 6GB（免费额度内最小档）。
DEFAULT_RESTORE_SHAPE = "VM.Standard.A1.Flex"
DEFAULT_RESTORE_OCPUS = 1
DEFAULT_RESTORE_MEMORY_GB = 6
# 备份默认名里的时间戳用的时区：北京时间（与面板显示的创建时间一致；
# 原来用 UTC，名字里的日期会比显示的创建时间早一天，2026-11-24 浏览器
# 实测发现）。zoneinfo 不可用时回退到 UTC。
BACKUP_NAME_TIMEZONE = "Asia/Shanghai"
# 删最早备份后立即创建时，OCI 侧配额计数可能还没同步（实测 2026-11-24
# 芝加哥：删完立即创建报 LimitExceeded），创建失败时按此重试：
# 最多尝试次数（首创 + 重试）与每次间隔（秒）。
CREATE_LIMIT_RETRY_ATTEMPTS = 5
CREATE_LIMIT_RETRY_INTERVAL_SECONDS = 15
# 恢复第 1 步（从备份建启动卷）等 AVAILABLE 的最长等待（秒）：
# 47GB 的盘实测约几分钟，1200 秒（20 分钟）留足余量。
RESTORE_WAIT_MAX_SECONDS = 1200
# 等 AVAILABLE 时的轮询间隔（秒）。
RESTORE_WAIT_INTERVAL_SECONDS = 15
# 备份创建后等 AVAILABLE 的最长等待（秒）：47GB 全量备份实测约几分钟，
# 1800 秒（30 分钟）留足余量；轮询间隔 30 秒（备份创建是慢操作，
# 不必像恢复建卷那样 15 秒一查）。
BACKUP_WATCH_MAX_SECONDS = 1800
BACKUP_WATCH_INTERVAL_SECONDS = 30
# =====================================================


def _extract_data(result):
    """从 cli.run() 的返回值里取出数据列表/字典。

    list 类命令返回 {"data": [...]}，get/create 类命令返回
    {"data": {...}}；兼容直接返回 list / dict 的情况。
    """
    if isinstance(result, dict) and "data" in result:
        return result["data"]
    return result


def _simplify_backup(item):
    """把备份原始 JSON 精简为常用字段。

    字段名按 OCI CLI 返回的 kebab-case（与 oci/volume.py 一致）：
    id / display-name / boot-volume-id / size-in-gbs /
    lifecycle-state / source-type（MANUAL 手动 / SCHEDULED 定时策略）/
    type（FULL 全量 / INCREMENTAL 增量）/ time-created / image-id
    （来源启动卷的系统镜像 OCID，恢复时参考用）。
    """
    return {
        "id": item.get("id"),
        "display-name": item.get("display-name"),
        "boot-volume-id": item.get("boot-volume-id"),
        "size-in-gbs": item.get("size-in-gbs"),
        "lifecycle-state": item.get("lifecycle-state"),
        "source-type": item.get("source-type"),
        "type": item.get("type"),
        "time-created": item.get("time-created"),
        "image-id": item.get("image-id"),
    }


def _is_counted(backup):
    """该备份是否计入免费额度：已删除 / 删除中的不计。"""
    return backup.get("lifecycle-state") not in ("TERMINATED",
                                                 "TERMINATING")


def _backup_name_now():
    """备份名时间戳的当前时间（BACKUP_NAME_TIMEZONE 时区）。

    单独抽成函数方便测试固定时间；zoneinfo 不可用时回退到 UTC。
    """
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(BACKUP_NAME_TIMEZONE))
        except Exception:  # noqa: BLE001 - 时区数据缺失时回退 UTC
            pass
    return datetime.now(timezone.utc)


def default_backup_name(base_name):
    """默认备份名：<实例名或卷名>-backup-YYYYMMDD-HHMM（北京时间）。

    base_name 为空时用 "boot-volume" 兜底。
    """
    base = (base_name or "").strip() or "boot-volume"
    stamp = _backup_name_now().strftime("%Y%m%d-%H%M")
    return f"{base}-backup-{stamp}"


def list_boot_volume_backups(cli, compartment_id, boot_volume_id=None,
                             include_terminated=False):
    """列出指定 compartment 下的启动卷备份，按创建时间升序（最早的在前）。

    对应命令：oci bv boot-volume-backup list --compartment-id <OCID>
              --all --sort-by TIMECREATED --sort-order ASC
    boot_volume_id 传了则只列该启动卷的备份。

    默认不含已删除 / 删除中（TERMINATED / TERMINATING）的备份：
    删了的备份在 OCI 侧仍会列出一段时间，显示出来会和配额计数
    （只数未删除的）对不上，浏览器实测 2026-11-24 发现；传
    include_terminated=True 可列出全部。

    返回精简列表（见 _simplify_backup）；服务端排序后本地再按
    time-created 排一次序（time-created 缺失的排最后），保证
    “最早备份”取到的永远是时间最早的那条。
    """
    args = ["bv", "boot-volume-backup", "list",
            "--compartment-id", compartment_id,
            "--all", "--sort-by", "TIMECREATED", "--sort-order", "ASC"]
    if boot_volume_id:
        args += ["--boot-volume-id", boot_volume_id]
    result = cli.run(args)
    items = _extract_data(result) or []
    backups = [_simplify_backup(item) for item in items]
    if not include_terminated:
        backups = [b for b in backups if _is_counted(b)]
    backups.sort(key=lambda b: b.get("time-created") or "9999")
    return backups


def get_boot_volume_backup(cli, backup_id):
    """查单个启动卷备份的详情。

    对应命令：oci bv boot-volume-backup get
              --boot-volume-backup-id <OCID>

    返回精简字典（见 _simplify_backup）。OCID 不存在时抛 NotFoundError。
    """
    result = cli.run(["bv", "boot-volume-backup", "get",
                      "--boot-volume-backup-id", backup_id])
    return _simplify_backup(_extract_data(result) or {})


def wait_for_backup_available(cli, backup_id, timeout_seconds=None,
                              interval_seconds=None):
    """轮询备份状态，直到 AVAILABLE 或进入终态。

    AVAILABLE 返回 "AVAILABLE"；进入终态（FAULTY / TERMINATED /
    TERMINATING）返回该状态；超时抛 TimeoutError。备份刚创建时是
    REQUEST_RECEIVED / CREATING，需要几分钟才到 AVAILABLE。
    """
    timeout = (BACKUP_WATCH_MAX_SECONDS if timeout_seconds is None
               else timeout_seconds)
    interval = (BACKUP_WATCH_INTERVAL_SECONDS if interval_seconds is None
                else interval_seconds)
    deadline = time.time() + timeout
    terminal = {"FAULTY", "TERMINATED", "TERMINATING"}
    while True:
        backup = get_boot_volume_backup(cli, backup_id)
        state = (backup.get("lifecycle-state") or "").upper()
        if state == "AVAILABLE" or state in terminal:
            return state
        if time.time() >= deadline:
            raise TimeoutError(
                f"等待备份 …{backup_id[-4:]} 可用超时（{timeout} 秒），"
                f"当前状态：{state or '未知'}")
        time.sleep(interval)


def create_boot_volume_backup(cli, boot_volume_id, display_name=None):
    """给指定启动卷做一次 FULL 全量备份。

    对应命令：oci bv boot-volume-backup create --boot-volume-id <OCID>
              --type FULL --display-name <名字>

    display_name 不传时先查启动卷的显示名，再按
    default_backup_name() 生成“<卷名>-backup-YYYYMMDD-HHMM”；
    卷名查不到时用 "boot-volume" 兜底。

    注意：备份刚创建时状态是 REQUEST_RECEIVED / CREATING，需要几分钟
    才到 AVAILABLE；本函数不等待，调用方可用 get 查状态。
    返回新建备份的精简字典。
    """
    if not display_name:
        name = None
        try:
            from oci.volume import get_boot_volume
            volume = get_boot_volume(cli, boot_volume_id) or {}
            name = volume.get("display-name")
        except Exception:  # noqa: BLE001 - 卷名查不到就用默认名，不阻塞备份
            name = None
        display_name = default_backup_name(name)
    result = cli.run(["bv", "boot-volume-backup", "create",
                      "--boot-volume-id", boot_volume_id,
                      "--type", BACKUP_TYPE,
                      "--display-name", display_name])
    return _simplify_backup(_extract_data(result) or {})


def create_boot_volume_backup_with_limit_retry(cli, boot_volume_id,
                                               display_name=None):
    """创建备份；遇 LimitExceeded 时重试（删最早备份后专用）。

    删最早备份后立即创建，OCI 侧配额计数可能还没同步（实测
    2026-11-24 芝加哥：删完立即创建报 LimitExceeded，但删完的备份
    在列表里已是 TERMINATED、我方配额计数已降到 4/5——说明是我方
    计数与 OCI 侧限流计数同步有延迟，轮询我方配额没有用，必须
    重试创建本身）。

    只有错误里含 LimitExceeded 才重试（最多
    CREATE_LIMIT_RETRY_ATTEMPTS 次，间隔
    CREATE_LIMIT_RETRY_INTERVAL_SECONDS 秒）；其他错误立即抛出。
    全部重试都失败时抛出最后一次的异常（调用方应提示用户稍后
    再试创建——最早备份已删，配额迟早会释放）。
    """
    last_exc = None
    for attempt in range(CREATE_LIMIT_RETRY_ATTEMPTS):
        try:
            return create_boot_volume_backup(cli, boot_volume_id,
                                             display_name)
        except Exception as exc:  # noqa: BLE001 - 只重试限流，其他立即抛
            text = str(exc) + getattr(exc, "raw_output", "")
            if "LimitExceeded" not in text:
                raise
            last_exc = exc
            if attempt < CREATE_LIMIT_RETRY_ATTEMPTS - 1:
                time.sleep(CREATE_LIMIT_RETRY_INTERVAL_SECONDS)
    raise last_exc


def delete_boot_volume_backup(cli, backup_id):
    """删除启动卷备份。

    对应命令：oci bv boot-volume-backup delete
              --boot-volume-backup-id <OCID> --force

    --force 必加：不加时 OCI CLI 会交互确认，subprocess 无 stdin
    应答会一直挂到超时（同类教训见实例升降配，2026-11-23）。
    删了不可恢复，调用前请二次确认。
    删除成功返回 None（OCI CLI 无输出）。
    """
    return cli.run(["bv", "boot-volume-backup", "delete",
                    "--boot-volume-backup-id", backup_id,
                    "--force"])


def check_backup_quota(cli, compartment_id):
    """查免费备份额度用了多少。

    只数未删除的备份（TERMINATED / TERMINATING 不计，与
    total_boot_volume_gb 排除已删盘的逻辑一致）。

    返回 (当前数量, 上限, 是否已满, 最早备份) 四元组：
      - 当前数量：未删除的备份个数；
      - 上限：FREE_BACKUP_LIMIT（5）；
      - 是否已满：数量 >= 上限；
      - 最早备份：时间最早的那条备份的精简字典
        {id, display-name, time-created, ...}，没有备份时为 None。
    """
    backups = [b for b in list_boot_volume_backups(cli, compartment_id)
               if _is_counted(b)]
    count = len(backups)
    oldest = backups[0] if backups else None
    return count, FREE_BACKUP_LIMIT, count >= FREE_BACKUP_LIMIT, oldest


def get_source_instance_shape(cli, compartment_id, backup):
    """查备份来源实例的形状（恢复时默认用来源形状）。

    链路：备份的 boot-volume-id -> 查启动卷拿到 availability-domain ->
    查该 AD 的启动盘挂载关系找到 instance-id -> 查实例拿到 shape 与
    shape-config。任一步查不到（来源实例已终止且引导卷已删是最常见
    的情况）就返回 None，调用方用默认形状兜底；这里不抛异常。

    返回 (shape, ocpus, memory_gb) 三元组，或 None。
    E2.1.Micro 是固定规格，ocpus / memory_gb 返回 None。
    """
    boot_volume_id = (backup or {}).get("boot-volume-id")
    if not boot_volume_id:
        return None
    try:
        from oci.volume import (
            get_boot_volume,
            list_boot_volume_attachments,
        )
        from oci.instance import get_instance
        volume = get_boot_volume(cli, boot_volume_id) or {}
        ad = volume.get("availability-domain")
        if not ad:
            return None
        instance_id = None
        for att in list_boot_volume_attachments(cli, compartment_id, ad):
            if att.get("boot-volume-id") == boot_volume_id:
                instance_id = att.get("instance-id")
                break
        if not instance_id:
            return None
        detail = get_instance(cli, instance_id)
        if isinstance(detail, dict):
            detail = detail.get("data") or detail
        shape = (detail or {}).get("shape")
        if not shape:
            return None
        cfg = (detail or {}).get("shape-config") or {}
        if shape.endswith(".Flex"):
            return (shape, cfg.get("ocpus"), cfg.get("memory-in-gbs"))
        return (shape, None, None)
    except Exception:  # noqa: BLE001 - 来源查不到就兜底默认形状，不阻塞恢复
        return None


def restore_boot_volume_backup(cli, compartment_id, backup_id,
                               availability_domain, subnet_id, display_name,
                               ssh_keys_file, shape=None, ocpus=None,
                               memory_gb=None, assign_public_ip=True):
    """从备份恢复出一台新实例（原机不动）。

    两步（见模块 docstring）：
      1. 从备份建新启动卷（等到 AVAILABLE，最长
         RESTORE_WAIT_MAX_SECONDS 秒）；
      2. 用新启动卷 launch 新实例（--no-retry，单次尝试）。

    形状：shape 传了就用它（A1.Flex 时 ocpus / memory_gb 都传了才加
    --shape-config；固定规格形状永远不传）；不传则查备份来源实例的
    形状，查不到用默认 A1.Flex 1 核 / 6GB。
    ssh_keys_file 为空时不传 --ssh-authorized-keys-file（launch 本身
    不强制要求 SSH 公钥；恢复出来的系统保留原机的 authorized_keys，
    原密钥照样能登录）。

    第 2 步失败时尽力删除第 1 步建出来的启动卷（不留孤儿盘占额度），
    再把原始异常抛给调用方。
    成功返回 {"instance_id": 新实例 OCID,
              "boot_volume_id": 新启动卷 OCID,
              "shape": 实际用的形状, "ocpus": ..., "memory_gb": ...}。
    """
    backup = get_boot_volume_backup(cli, backup_id)
    if backup.get("lifecycle-state") != "AVAILABLE":
        state = backup.get("lifecycle-state") or "未知"
        raise ValueError(
            f"备份「{backup.get('display-name') or backup_id}」当前状态是 "
            f"{state}，只有 AVAILABLE 状态的备份才能恢复。")
    if shape is None:
        detected = get_source_instance_shape(cli, compartment_id, backup)
        if detected:
            shape, ocpus, memory_gb = detected
        else:
            shape, ocpus, memory_gb = (
                DEFAULT_RESTORE_SHAPE, DEFAULT_RESTORE_OCPUS,
                DEFAULT_RESTORE_MEMORY_GB)
    if shape.endswith(".Flex"):
        if ocpus is None:
            ocpus = DEFAULT_RESTORE_OCPUS
        if memory_gb is None:
            memory_gb = DEFAULT_RESTORE_MEMORY_GB

    # 第 1 步：从备份建新启动卷，等到 AVAILABLE。
    volume_name = f"{display_name}-boot-volume"
    created = _extract_data(cli.run(
        ["bv", "boot-volume", "create",
         "--availability-domain", availability_domain,
         "--compartment-id", compartment_id,
         "--boot-volume-backup-id", backup_id,
         "--display-name", volume_name,
         "--wait-for-state", "AVAILABLE",
         "--max-wait-seconds", str(RESTORE_WAIT_MAX_SECONDS),
         "--wait-interval-seconds", str(RESTORE_WAIT_INTERVAL_SECONDS)])) or {}
    new_volume_id = created.get("id")
    if not new_volume_id:
        raise ValueError("从备份创建启动卷后未返回 OCID，恢复中止。")

    # 第 2 步：用新启动卷 launch 新实例。
    args = ["compute", "instance", "launch",
            "--availability-domain", availability_domain,
            "--compartment-id", compartment_id,
            "--shape", shape,
            "--source-boot-volume-id", new_volume_id,
            "--subnet-id", subnet_id,
            "--display-name", display_name,
            "--assign-public-ip", "true" if assign_public_ip else "false"]
    if ssh_keys_file:
        args += ["--ssh-authorized-keys-file", ssh_keys_file]
    if shape.endswith(".Flex") and ocpus is not None and memory_gb is not None:
        args += ["--shape-config", json.dumps({
            "ocpus": ocpus, "memoryInGBs": memory_gb})]
    args += ["--no-retry"]
    try:
        launched = _extract_data(cli.run(args)) or {}
    except Exception:
        # launch 失败：尽力删掉第 1 步建出来的启动卷，不留孤儿盘。
        try:
            from oci.volume import delete_boot_volume
            delete_boot_volume(cli, new_volume_id)
        except Exception:  # noqa: BLE001 - 清理失败也不掩盖原始异常
            pass
        raise
    instance_id = launched.get("id")
    if not instance_id:
        raise ValueError("启动实例后未返回 OCID，恢复中止（新启动卷已创建，"
                         "请在启动盘页面查看）。")
    return {"instance_id": instance_id,
            "boot_volume_id": new_volume_id,
            "shape": shape,
            "ocpus": ocpus,
            "memory_gb": memory_gb}
