#!/usr/bin/env python3
"""引导卷恢复到实例：用启动卷备份替换某台实例的启动盘（原地恢复）。

流程（用户 2026-11-24 定案：停机换盘，原地恢复，不是恢复成新实例）：
  1. 从备份创建一个新的启动卷（与实例同一可用域，等到 AVAILABLE）；
  2. 调 OCI 原生换盘接口（update-instance-source-via-boot-volume-details）：
     OCI 自己停机、换盘、把实例恢复到换盘前状态，失败时自动回滚；
  3. 轮询确认新盘已挂上、实例回到换盘前状态；
  4. 删除换下来的旧启动盘（v1.35.0，用户定：不留旧盘占额度；
     删除失败只警告，不影响恢复结果）。

为什么不用手动 detach + attach：从备份恢复的启动盘调 attach 会报
Conflict（2026-11-24 首尔真机验证实测），OCI 只支持用原生换盘接口
替换（见 docs.oracle.com 的 Replacing a Boot Volume）。detach/attach
两个辅助函数保留（拆盘、挂回原盘仍可用），但换盘流程不走它们。

失败回滚：原生换盘失败时 OCI 自动回滚（恢复原状态、旧盘仍挂着）；
本模块在失败后实查挂载关系与实例状态确认回滚结论（rolled_back），
新盘未挂上时顺手删掉避免留孤儿盘占额度。

旧盘自动删除（v1.35.0，用户定）：换盘成功、实例回到换盘前状态后，
自动删除换下来的旧启动盘，不留旧盘占启动盘免费额度。删除失败不影响
恢复结果，只在返回里标记 old_volume_deleted=False，调用方在结果消息
里警告用户手动删除。新盘创建前做启动盘额度预检（FREE_BOOT_VOLUME_GB），
额度不够时直接拒绝，不动实例（新盘先建、旧盘后删，峰值仍需额度）。

原创实现，参考 OCI CLI 公开命令编写：
  oci bv boot-volume create --boot-volume-backup-id ...
  oci compute instance update-instance-update-instance-source-via-boot-volume-details
              --instance-id ... --source-details-boot-volume-id ...
  （原生换盘；attach 没有 --type 选项，挂载类型由 OCI 自动决定；
  CLI 3.96.3 实查。）
所有函数都接受一个 oci.client.OCIClient 对象作为第一个参数。

安全说明（重要）：
  - 本模块不读取、不打印、不外传 API 私钥。认证全部由 OCI CLI 自己读
    配置文件（~/oci_config，旧部署回退 ~/.oci/config）与 key_file
    完成，私钥只在本地用于请求签名。
  - 配置区在文件顶端，真实 OCID 一律用占位符，绝不写进代码。
  - 换盘是破坏性写操作（实例停机 + 系统盘替换），只在用户明确确认后调用。
"""

import time

from oci.boot_backup import (
    RESTORE_WAIT_INTERVAL_SECONDS,
    RESTORE_WAIT_MAX_SECONDS,
    get_boot_volume_backup,
)
from oci.instance import get_instance, get_instance_state
from oci.volume import (
    FREE_BOOT_VOLUME_GB,
    delete_boot_volume,
    list_boot_volume_attachments,
    total_boot_volume_gb,
)

# ================= 配置区（改这里） =================
# Default compartment OCID (placeholder). The root compartment is the tenancy OCID. 默认 compartment OCID（占位符）。根 compartment 就是 tenancy OCID。
DEFAULT_COMPARTMENT_ID = "ocid1.tenancy.oc1..xxxxxxxx"
# Per-step wait caps (seconds): building a volume from a backup takes a few minutes for a 47 GB disk, so 1200s leaves headroom; a native swap (stop + swap + start) usually takes 3-6 minutes, so 900s leaves headroom; waiting for the instance to return to its pre-swap state, 600s; detach / attach (helper functions) usually 1-3 minutes. 各步等待上限（秒）：47GB 的盘从备份建卷实测约几分钟，1200 秒留足余量；原生换盘（停机 + 换盘 + 开机）通常 3-6 分钟，900 秒留足余量；换盘后等实例回到原状态 600 秒；拆盘 / 挂盘（辅助函数）通常 1-3 分钟。
SWAP_DETACH_MAX_SECONDS = 300
SWAP_ATTACH_MAX_SECONDS = 300
SWAP_REPLACE_MAX_SECONDS = 900
SWAP_STATE_MAX_SECONDS = 600
# Poll interval (seconds) for instance state. 轮询实例状态的间隔（秒）。
SWAP_POLL_INTERVAL_SECONDS = 10
# New boot disk display-name suffix (instance name + suffix). 新启动盘显示名后缀（实例名 + 后缀）。
SWAP_VOLUME_NAME_SUFFIX = "-restored-boot-volume"
# =====================================================


class BootSwapError(Exception):
    """换盘失败。rolled_back 表示是否已换回旧盘（并按需重启）。

    old_boot_volume_id / new_boot_volume_id 供调用方在报错消息里
    说明现场（只在对应步骤完成后才有值）。
    """

    def __init__(self, message, rolled_back=False,
                 old_boot_volume_id=None, new_boot_volume_id=None):
        super().__init__(message)
        self.rolled_back = rolled_back
        self.old_boot_volume_id = old_boot_volume_id
        self.new_boot_volume_id = new_boot_volume_id


def _unwrap(data):
    """OCI CLI 的 get 响应通常包一层 {"data": ...}，这里解包。"""
    if isinstance(data, dict) and "data" in data:
        return data["data"]
    return data


def get_boot_volume_attachment(cli, compartment_id, instance_id):
    """查某台实例当前挂着的启动盘挂载记录。

    先 get_instance 取可用域，再按 AD 列挂载关系，找 instance-id
    匹配且状态为 ATTACHED（优先）或 ATTACHING 的那条。
    返回挂载记录精简字典 {id, boot-volume-id, instance-id,
    lifecycle-state}；找不到返回 None。
    """
    detail = _unwrap(get_instance(cli, instance_id)) or {}
    ad = detail.get("availability-domain")
    if not ad:
        return None
    attachments = list_boot_volume_attachments(
        cli, compartment_id, ad, include_id=True)
    mine = [a for a in attachments
            if a.get("instance-id") == instance_id]
    for state in ("ATTACHED", "ATTACHING"):
        for att in mine:
            if att.get("lifecycle-state") == state:
                return att
    return None


def wait_for_instance_state(cli, instance_id, target_states,
                            timeout_seconds, interval_seconds=None):
    """轮询实例状态，直到进入 target_states 之一。

    target_states 可传单个字符串或字符串集合。
    到达目标状态返回最终状态字符串；超时抛 TimeoutError。
    """
    if isinstance(target_states, str):
        target_states = {target_states}
    interval = interval_seconds or SWAP_POLL_INTERVAL_SECONDS
    deadline = time.time() + timeout_seconds
    last_state = None
    while True:
        last_state = get_instance_state(cli, instance_id)
        if last_state in target_states:
            return last_state
        if time.time() >= deadline:
            raise TimeoutError(
                f"等待实例进入 {sorted(target_states)} 超时 "
                f"（{timeout_seconds} 秒），当前状态：{last_state}")
        time.sleep(interval)


def check_boot_volume_space(cli, compartment_id, needed_gb):
    """启动盘额度预检：新盘 needed_gb 能不能放下。

    返回 (ok, remaining_gb)：remaining_gb 是当前剩余免费额度（GB）。
    """
    used = total_boot_volume_gb(cli, compartment_id)
    remaining = FREE_BOOT_VOLUME_GB - used
    return remaining >= needed_gb, remaining


def detach_boot_volume(cli, attachment_id):
    """拆下启动盘（等到 DETACHED）。

    对应命令：oci compute boot-volume-attachment detach
              --boot-volume-attachment-id <OCID>
              --wait-for-state DETACHED --force
    注意：detach 会弹确认提示（Are you sure you want to delete this
    resource?），不加 --force 在非交互环境下直接 Abort（2026-11-24
    首尔真机验证实测，与 instance update 同类问题）。
    """
    cli.run(["compute", "boot-volume-attachment", "detach",
             "--boot-volume-attachment-id", attachment_id,
             "--wait-for-state", "DETACHED",
             "--max-wait-seconds", str(SWAP_DETACH_MAX_SECONDS),
             "--wait-interval-seconds",
             str(RESTORE_WAIT_INTERVAL_SECONDS),
             "--force"])


def attach_boot_volume(cli, boot_volume_id, instance_id):
    """把启动盘挂到实例上（等到 ATTACHED）。

    对应命令：oci compute boot-volume-attachment attach
              --boot-volume-id <OCID> --instance-id <OCID>
              --wait-for-state ATTACHED
    注意：attach 没有 --type 选项（CLI 3.96.3 实查），挂载类型由
    OCI 自动决定；从备份恢复的盘不能用这个接口挂（会报 Conflict），
    换盘必须走 replace_boot_volume（见模块 docstring）。
    """
    cli.run(["compute", "boot-volume-attachment", "attach",
             "--boot-volume-id", boot_volume_id,
             "--instance-id", instance_id,
             "--wait-for-state", "ATTACHED",
             "--max-wait-seconds", str(SWAP_ATTACH_MAX_SECONDS),
             "--wait-interval-seconds",
             str(RESTORE_WAIT_INTERVAL_SECONDS)])


def replace_boot_volume(cli, instance_id, new_boot_volume_id):
    """OCI 原生换盘：把实例的启动盘换成指定盘（不等待完成）。

    对应命令：oci compute instance
              update-instance-update-instance-source-via-boot-volume-details
              --instance-id <OCID>
              --source-details-boot-volume-id <OCID> --force
    OCI 自己停机、换盘、把实例恢复到换盘前状态，失败时自动回滚。
    注意：不要加 --wait-for-state——实例本来就在 RUNNING，wait 会
    立即返回；换盘完成靠 wait_for_boot_volume_attachment 轮询确认。
    update 命令会弹确认提示，必须加 --force。
    """
    cli.run(["compute", "instance",
             "update-instance-update-instance-source-via-boot-volume-details",
             "--instance-id", instance_id,
             "--source-details-boot-volume-id", new_boot_volume_id,
             "--force"])


def wait_for_boot_volume_attachment(cli, compartment_id, instance_id,
                                    boot_volume_id, timeout_seconds,
                                    interval_seconds=None):
    """轮询实例的挂载关系，直到挂着的是指定启动盘。

    到达目标返回挂载记录；超时抛 TimeoutError。
    """
    interval = interval_seconds or SWAP_POLL_INTERVAL_SECONDS
    deadline = time.time() + timeout_seconds
    while True:
        attachment = get_boot_volume_attachment(cli, compartment_id,
                                                instance_id)
        if (attachment is not None
                and attachment.get("boot-volume-id") == boot_volume_id
                and attachment.get("lifecycle-state") == "ATTACHED"):
            return attachment
        if time.time() >= deadline:
            raise TimeoutError(
                f"等待实例挂上启动盘 …{boot_volume_id[-4:]} 超时"
                f"（{timeout_seconds} 秒）")
        time.sleep(interval)


def _replace_failed_error(cli, compartment_id, instance_id,
                          old_volume_id, new_volume_id, was_running, exc):
    """替换失败后确认现场，返回带回滚结论的 BootSwapError。

    OCI 原生换盘失败时会自动回滚（恢复原状态、旧盘仍挂着）。这里
    实查挂载关系与实例状态确认回滚是否成功；新盘未挂上时顺手删掉
    避免留孤儿盘占额度；新盘已挂上时绝不删盘，请手动处理。
    """
    current = None
    try:
        current = get_boot_volume_attachment(cli, compartment_id,
                                             instance_id)
    except Exception:  # noqa: BLE001 - 查不到挂载状态时按未知处理
        current = None
    attached_id = (current or {}).get("boot-volume-id")
    if attached_id == new_volume_id:
        return BootSwapError(
            f"替换启动盘失败：{exc}。新盘已经挂上，实例状态未知，"
            "请在控制台检查后再操作。",
            rolled_back=False,
            old_boot_volume_id=old_volume_id,
            new_boot_volume_id=new_volume_id)
    cleanup_note = ""
    try:
        delete_boot_volume(cli, new_volume_id)
    except Exception:  # noqa: BLE001 - 清理失败只记在文案里
        cleanup_note = "新建的启动盘（未挂载）删除失败，请手动删除以释放额度。"
    state = None
    try:
        state = get_instance_state(cli, instance_id)
    except Exception:  # noqa: BLE001 - 查不到状态时按未知处理
        state = None
    prior_state = "RUNNING" if was_running else "STOPPED"
    if attached_id == old_volume_id and state == prior_state:
        return BootSwapError(
            f"替换启动盘失败：{exc}。OCI 已自动回滚：实例恢复"
            f"{prior_state}，旧盘仍在使用。{cleanup_note}",
            rolled_back=True,
            old_boot_volume_id=old_volume_id,
            new_boot_volume_id=new_volume_id)
    return BootSwapError(
        f"替换启动盘失败：{exc}。回滚状态不明（实例当前状态："
        f"{state or '未知'}），请在控制台手动处理。{cleanup_note}",
        rolled_back=False,
        old_boot_volume_id=old_volume_id,
        new_boot_volume_id=new_volume_id)


def swap_boot_volume(cli, compartment_id, instance_id, backup_id,
                     on_step=None, delete_old_volume=True):
    """用备份替换实例的启动盘（原地恢复，见模块 docstring）。

    on_step：可选回调，每步开始时调 on_step(中文进度文本)。
    delete_old_volume=False 时换盘成功后不删旧盘（救机安全恢复用：
    旧盘是备份源盘时留给用户手动处理）。
    成功返回 {"old_boot_volume_id", "new_boot_volume_id",
              "rolled_back": False, "was_running": bool,
              "old_volume_deleted": bool}（旧盘删除成功为 True；
    删除失败时恢复仍算成功，old_volume_deleted 为 False，调用方
    应在消息里警告用户手动删除旧盘；delete_old_volume=False 时
    old_volume_deleted 恒为 False）。
    失败抛 BootSwapError（替换失败时 OCI 会自动回滚，见
    rolled_back）或 ValueError（前置校验不通过：备份不可用 /
    实例状态不对 / 找不到挂载关系 / 额度不够）。
    """
    def step(text):
        if on_step is not None:
            on_step(text)

    # Pre-check 1: the backup must be AVAILABLE. 前置校验 1：备份必须 AVAILABLE。
    backup = get_boot_volume_backup(cli, backup_id)
    if backup.get("lifecycle-state") != "AVAILABLE":
        state = backup.get("lifecycle-state") or "未知"
        raise ValueError(
            f"备份「{backup.get('display-name') or backup_id}」当前状态是 "
            f"{state}，只有 AVAILABLE 状态的备份才能恢复。")
    # Pre-check 2: the instance must be RUNNING or STOPPED. 前置校验 2：实例状态必须是 RUNNING 或 STOPPED。
    detail = _unwrap(get_instance(cli, instance_id)) or {}
    state = detail.get("lifecycle-state")
    if state not in ("RUNNING", "STOPPED"):
        raise ValueError(
            f"实例当前状态是 {state or '未知'}，只有运行中或已停止的"
            "实例才能恢复引导卷。")
    was_running = state == "RUNNING"
    ad = detail.get("availability-domain")
    name = detail.get("display-name") or instance_id[-4:]
    # Pre-check 3: find the currently attached boot volume. 前置校验 3：找到当前挂着的启动盘。
    attachment = get_boot_volume_attachment(cli, compartment_id, instance_id)
    if attachment is None:
        raise ValueError("找不到该实例当前挂载的启动盘，无法换盘。")
    old_volume_id = attachment.get("boot-volume-id")
    # Pre-check 4: enough boot-volume quota for the new disk (the new disk is built first and the old one deleted later, so the peak still needs quota). 前置校验 4：启动盘额度够放新盘（新盘先建、旧盘后删，峰值仍需额度）。
    size_gb = backup.get("size-in-gbs") or 0
    ok, remaining = check_boot_volume_space(cli, compartment_id, size_gb)
    if not ok:
        raise ValueError(
            f"启动盘额度不足：还剩 {remaining}GB，新盘需要 {size_gb}GB。"
            "请先删除不用的启动盘再恢复。")

    # Step 1: build a new boot volume from the backup, wait until AVAILABLE. 第 1 步：从备份建新启动卷，等到 AVAILABLE。
    step("① 从备份创建新启动盘…")
    volume_name = f"{name}{SWAP_VOLUME_NAME_SUFFIX}"
    created = cli.run(
        ["bv", "boot-volume", "create",
         "--availability-domain", ad,
         "--compartment-id", compartment_id,
         "--boot-volume-backup-id", backup_id,
         "--display-name", volume_name,
         "--wait-for-state", "AVAILABLE",
         "--max-wait-seconds", str(RESTORE_WAIT_MAX_SECONDS),
         "--wait-interval-seconds", str(RESTORE_WAIT_INTERVAL_SECONDS)])
    new_volume_id = (_unwrap(created) or {}).get("id")
    if not new_volume_id:
        raise ValueError("从备份创建启动卷后未返回 OCID，恢复中止。")

    # Step 2: native OCI volume swap (stop -> swap -> restore original state; rolls back automatically on failure). 第 2 步：OCI 原生换盘（停机→换盘→恢复原状态，失败自动回滚）。
    # Manual detach + attach does not work: attaching a volume restored from a backup reports Conflict; the native API must be used (see the module docstring). 手动 detach + attach 行不通：从备份恢复的盘 attach 会报 Conflict，必须走原生接口（见模块 docstring）。
    step("② 正在替换启动盘（OCI 原生换盘）…")
    try:
        replace_boot_volume(cli, instance_id, new_volume_id)
    except Exception as exc:  # noqa: BLE001 - 替换失败要确认回滚状态
        raise _replace_failed_error(cli, compartment_id, instance_id,
                                    old_volume_id, new_volume_id,
                                    was_running, exc) from exc
    # Wait for the new disk to attach (poll the attachment until the attached volume is the new one). 等新盘挂上（轮询挂载关系，直到挂着的是新盘）。
    try:
        wait_for_boot_volume_attachment(cli, compartment_id, instance_id,
                                        new_volume_id,
                                        SWAP_REPLACE_MAX_SECONDS)
    except TimeoutError as exc:
        raise _replace_failed_error(cli, compartment_id, instance_id,
                                    old_volume_id, new_volume_id,
                                    was_running, exc) from exc
    # Wait for the instance to return to its pre-swap state (a running one returns to RUNNING; a stopped one stays STOPPED). 等实例回到换盘前状态（在运行的回到 RUNNING，已停止的保持 STOPPED）。
    target_state = "RUNNING" if was_running else "STOPPED"
    try:
        wait_for_instance_state(cli, instance_id, target_state,
                                SWAP_STATE_MAX_SECONDS)
    except TimeoutError as exc:
        raise BootSwapError(
            f"新启动盘已挂上，但实例 {SWAP_STATE_MAX_SECONDS} 秒未回到"
            f"{target_state} 状态，请在控制台检查。",
            rolled_back=False,
            old_boot_volume_id=old_volume_id,
            new_boot_volume_id=new_volume_id) from exc

    # Step 3: after a successful swap, delete the old boot volume so it does not keep occupying quota. 第 3 步：换盘成功后删除旧启动盘，不留旧盘占额度。
    # Skipped when delete_old_volume=False (rescue safe-restore: the old disk is the backup's source disk, left for the user to handle manually); a delete failure does not affect the restore result, only marks it in the return value, and the caller warns the user in the message to delete it manually. delete_old_volume=False 时跳过（救机安全恢复：旧盘是备份源盘，留给用户手动处理）；删除失败不影响恢复结果，只在返回里标记，调用方在消息里警告用户手动删除。
    step("③ 正在删除旧启动盘…")
    old_volume_deleted = False
    if delete_old_volume:
        old_volume_deleted = True
        try:
            delete_boot_volume(cli, old_volume_id)
        except Exception:  # noqa: BLE001 - 删盘失败只警告，不影响恢复结果
            old_volume_deleted = False

    return {"old_boot_volume_id": old_volume_id,
            "new_boot_volume_id": new_volume_id,
            "rolled_back": False,
            "was_running": was_running,
            "old_volume_deleted": old_volume_deleted}
