#!/usr/bin/env python3
"""救机功能核心逻辑（v2.01）：安全恢复 / 无盘救机 / 孤儿盘 /
Netboot 向导 / 前置检查 / 控制台连接管理。

本模块只做纯逻辑（返回结构化字典），不依赖 Telegram / FastAPI，
Web 与 Bot 两端共用。所有函数都接受一个 oci.client.OCIClient
对象作为第一个参数，真实 OCI 调用走 mock 友好的 cli.run()。

ARM 实测结论（2026-11-25，ARM 测试机），本模块按实测写：
  - 安全恢复（先建后删）通：备份 → 恢复盘 → 原生换盘 → 删旧盘。
  - 无盘实例原地救回没有路：直接启动 409、从镜像建盘无此 API、
    恢复盘/克隆盘 attach 一律 409、无盘时原生换盘 400。
    唯一出路是从备份新建实例（恢复成盘 → launch 新实例）。
    本模块不提供“挂盘救回原实例”的函数。
  - 孤儿盘判定：无任何 attachment 记录指向它 + source-details.type
    为 bootVolumeBackup。不能只看 AVAILABLE（在用盘也是 AVAILABLE）。
  - 控制台连接须用 RSA 密钥，ed25519 会被 OCI 拒绝（见 oci/console.py）。
"""

import os

from oci.boot_backup import (
    list_boot_volume_backups,
    restore_boot_volume_backup,
)
from oci.boot_swap import get_boot_volume_attachment, swap_boot_volume
from oci.console import (
    create_console_connection as _create_console_connection,
)
from oci.console import (
    delete_console_connection as _delete_console_connection,
)
from oci.console import (
    list_console_connections as _list_console_connections,
)
from oci.instance import get_instance
from oci.volume import (
    FREE_BOOT_VOLUME_GB,
    delete_boot_volume,
    get_boot_volume,
    list_boot_volume_attachments,
    list_boot_volumes,
    total_boot_volume_gb,
)

# ================= 配置区（改这里） =================
# Default compartment OCID (placeholder). The root compartment is the tenancy OCID. 默认 compartment OCID（占位符）。根 compartment 就是 tenancy OCID。
DEFAULT_COMPARTMENT_ID = "ocid1.tenancy.oc1..xxxxxxxx"
# =====================================================

PREFLIGHT_ACTIONS = ("start", "restore", "delete_volume", "terminate",
                     "rebuild_from_backup")
# Netboot guide TFTP nodes: pick the nearest. Your own node goes in the MBOT_TFTP_NODE environment variable (never hard-code a real IP here: this repo is public); without it the example placeholder below is shown. Netboot 向导的 TFTP 节点：就近选择；自己的节点填环境变量 MBOT_TFTP_NODE（不要把真实 IP 写进代码：本仓库是公开的），不填时显示下面的示例占位地址。
_TFTP_NODE = os.environ.get("MBOT_TFTP_NODE", "").strip()
NETBOOT_TFTP_NODES = [
    {"label": "新加坡（自备节点）", "host": _TFTP_NODE or "203.0.113.10"},
    {"label": "netboot.xyz 官方演示节点", "host": "boot.netboot.xyz"},
]
# Rescue commands for the EFI Shell (in execution order). EFI Shell 里救机用的命令（按执行顺序）。
NETBOOT_EFI_COMMANDS = [
    "FS0:",
    "ls",
    "ifconfig -s eth0 dhcp",
    "ping 8.8.8.8",
]
# netboot.xyz menu guide (the arrow-key steps to walk through after entering the EFI Shell). netboot.xyz 菜单指引（进 EFI Shell 后手动走的方向键步骤）。
NETBOOT_MENU_STEPS = [
    "在 EFI Shell 里用 tftp 从 TFTP 节点下载 netboot.xyz 启动文件并执行",
    "进入 netboot.xyz 菜单后选 Linux Network Installs",
    "选 Alpine Linux（轻量临时系统，启动快）",
    "进 Alpine 后确认网络通（ping 网关 / ping 8.8.8.8），再跑 DD 脚本重装",
]


def _unwrap(data):
    if isinstance(data, dict) and "data" in data:
        return data["data"]
    return data


# ================= 1. 安全恢复 =================

def check_volume_quota(cli, compartment_id, needed_gb):
    """启动盘额度检查：当前用量 + needed_gb 是否超 200GB。

    返回 {"ok", "used_gb", "remaining_gb", "needed_gb"}。
    """
    used = total_boot_volume_gb(cli, compartment_id)
    remaining = FREE_BOOT_VOLUME_GB - used
    return {"ok": remaining >= needed_gb,
            "used_gb": used,
            "remaining_gb": remaining,
            "needed_gb": needed_gb}


def find_backups_by_source_volume(cli, compartment_id, volume_id):
    """查以指定启动盘为源盘的全部备份（未删除的）。

    返回备份精简列表（见 oci.boot_backup._simplify_backup 的字段）。
    """
    backups = list_boot_volume_backups(cli, compartment_id)
    return [b for b in backups
            if b.get("boot-volume-id") == volume_id]


def check_delete_volume_safety(cli, compartment_id, volume_id):
    """删盘前检查：该盘是否为任意备份的源盘。

    返回 {"is_backup_source", "backups", "warning"}：
    是源盘时 warning 说明后果（删盘不可恢复；这些备份本身仍可用于
    安全恢复/从备份新建实例，但源盘数据永久丢失）。
    """
    backups = find_backups_by_source_volume(cli, compartment_id, volume_id)
    warning = ""
    if backups:
        names = "、".join(
            b.get("display-name") or b.get("id") for b in backups)
        warning = (f"该启动盘是 {len(backups)} 个备份的源盘（{names}）。"
                   "删除后源盘数据永久丢失，这些备份本身仍可用于"
                   "安全恢复或从备份新建实例，但无法再从源盘增量备份。")
    return {"is_backup_source": bool(backups),
            "backups": backups,
            "warning": warning}


def delete_boot_volume_guarded(cli, compartment_id, volume_id,
                               confirm=False, allow_backup_source=False):
    """带安全检查的删盘：未确认抛 ValueError；是备份源盘且未显式放行时拦截。

    返回 {"deleted": True, "was_backup_source": bool}。
    """
    if not confirm:
        raise ValueError("删除启动盘需要确认：请传 confirm=true")
    safety = check_delete_volume_safety(cli, compartment_id, volume_id)
    if safety["is_backup_source"] and not allow_backup_source:
        raise ValueError(
            "已拦截：" + safety["warning"]
            + "确认要删请显式传 allow_backup_source=true。")
    delete_boot_volume(cli, volume_id)
    return {"deleted": True,
            "was_backup_source": safety["is_backup_source"]}


def safe_restore(cli, compartment_id, instance_id, backup_id,
                 allow_delete_backup_source=False, on_step=None):
    """安全恢复：先建后删（见 oci.boot_swap.swap_boot_volume）。

    前置：额度预检（不够直接拒绝，不动实例）；旧盘是备份源盘时，
    换盘照常进行，但不自动删旧盘（留给用户手动处理），结果里带警告。
    成功返回 swap_boot_volume 的结果字典 + "warnings" 列表。
    """
    from oci.boot_backup import get_boot_volume_backup
    backup = get_boot_volume_backup(cli, backup_id)
    size_gb = backup.get("size-in-gbs") or 0
    quota = check_volume_quota(cli, compartment_id, size_gb)
    if not quota["ok"]:
        raise ValueError(
            f"启动盘额度不足：还剩 {quota['remaining_gb']}GB，"
            f"新盘需要 {size_gb}GB。请先删除不用的启动盘（孤儿盘清理）"
            "腾出额度再恢复。")
    attachment = get_boot_volume_attachment(cli, compartment_id, instance_id)
    old_volume_id = (attachment or {}).get("boot-volume-id")
    warnings = []
    delete_old = True
    if old_volume_id:
        safety = check_delete_volume_safety(cli, compartment_id,
                                            old_volume_id)
        if safety["is_backup_source"] and not allow_delete_backup_source:
            delete_old = False
            warnings.append(
                "旧启动盘是备份的源盘，本次换盘后不自动删除旧盘："
                + safety["warning"])
    result = swap_boot_volume(cli, compartment_id, instance_id, backup_id,
                              on_step=on_step, delete_old_volume=delete_old)
    result["warnings"] = warnings
    return result


# ================= 2. 无盘救机（从备份新建实例） =================

def plan_diskless_rescue(cli, compartment_id, instance_id):
    """无盘救机引导：实例无启动盘时，给出“从备份新建实例”的方案。

    不提供挂盘救回原实例的路径（ARM 实测五条路全灭）。
    返回 {"instance_id", "instance_name", "instance_state",
          "has_boot_volume", "backups", "can_rebuild", "message",
          "cost_note"}。backups 是全部 AVAILABLE 的备份（原盘已无，
    无法按源盘筛选，由用户挑选）。
    """
    detail = _unwrap(get_instance(cli, instance_id)) or {}
    attachment = get_boot_volume_attachment(cli, compartment_id, instance_id)
    has_boot_volume = attachment is not None
    backups = [b for b in list_boot_volume_backups(cli, compartment_id)
               if b.get("lifecycle-state") == "AVAILABLE"]
    name = detail.get("display-name") or instance_id
    # No in-place rescue path exists for a diskless instance: attach of a
    # restored/cloned boot volume is always rejected with 409 and the native
    # swap API returns 400 without a mounted volume (ARM test 2026-11-25).
    # The only way out is to rebuild a new instance from a backup.
    # 无盘实例没有原地救回的路：恢复盘/克隆盘 attach 一律 409，原生换盘
    # 在无盘时 400（2026-11-25 ARM 实测），唯一出路是从备份新建实例。
    if has_boot_volume:
        message = (f"实例「{name}」当前挂着启动盘，不需要无盘救机。"
                   "系统砖了请走 Netboot 向导；要恢复系统请走安全恢复。")
        can_rebuild = False
    elif not backups:
        message = (f"实例「{name}」没有挂载启动盘，且当前没有任何可用备份，"
                   "无法凭空恢复。只能新建实例重装系统（数据无法找回）。"
                   "以后请先给启动盘建备份。")
        can_rebuild = False
    else:
        message = (f"实例「{name}」没有挂载启动盘，原实例无法原地救回。"
                   f"可从以下 {len(backups)} 个可用备份新建一台实例"
                   "（恢复成盘 → launch 新实例，复用原 SSH 密钥）。")
        can_rebuild = True
    return {
        "instance_id": instance_id,
        "instance_name": name,
        "instance_state": detail.get("lifecycle-state"),
        "has_boot_volume": has_boot_volume,
        "backups": backups,
        "can_rebuild": can_rebuild,
        "message": message,
        "cost_note": ("代价：新实例有新的 OCID 与新的公网 IP（旧 IP 不继承），"
                      "旧实例无法复活，终止旧实例前请确认数据已迁移。"),
    }


def rebuild_diskless_from_backup(cli, compartment_id, instance_id,
                                 backup_id, availability_domain, subnet_id,
                                 display_name, ssh_keys_file=None,
                                 shape=None, ocpus=None, memory_gb=None,
                                 assign_public_ip=True):
    """无盘救机执行：从备份新建实例（原实例不动，事后由用户终止旧实例）。

    先校验实例确实无启动盘（有盘时拒绝，应用安全恢复），再调
    restore_boot_volume_backup（备份 → 恢复盘 → launch 新实例）。
    """
    attachment = get_boot_volume_attachment(cli, compartment_id, instance_id)
    if attachment is not None:
        raise ValueError(
            "该实例当前挂着启动盘，不适用无盘救机。"
            "要恢复系统请走安全恢复（从备份换盘）。")
    return restore_boot_volume_backup(
        cli, compartment_id, backup_id,
        availability_domain=availability_domain,
        subnet_id=subnet_id,
        display_name=display_name,
        ssh_keys_file=ssh_keys_file,
        shape=shape, ocpus=ocpus, memory_gb=memory_gb,
        assign_public_ip=assign_public_ip)


# ================= 3. 孤儿盘检测清理 =================

def is_rescue_orphan(volume_detail, attached_volume_ids):
    """孤儿盘纯判定（实测规则）：无任何 attachment 记录指向它，且
    source-details.type 为 bootVolumeBackup。

    volume_detail 是 get_boot_volume 返回的完整详情；attached_volume_ids
    是全部挂载记录里的启动盘 OCID 集合（含 DETACHED 等所有状态——
    只要有过挂载记录就不是孤儿）。只看 AVAILABLE 会误判在用盘。
    """
    volume_id = (volume_detail or {}).get("id")
    if not volume_id or volume_id in (attached_volume_ids or set()):
        return False
    source = (volume_detail or {}).get("source-details") or {}
    return source.get("type") == "bootVolumeBackup"


def list_rescue_orphans(cli, compartment_id):
    """列出孤儿盘（实测规则），每项含 id / display-name / size-in-gbs /
    lifecycle-state / availability-domain / source-backup-id。

    流程：列全部启动盘 → 按各 AD 查全部挂载记录（任何状态都算有记录）
    → 无记录的盘逐个查详情，按 source-details.type 筛选。
    """
    volumes = list_boot_volumes(cli, compartment_id)
    attached_ids = set()
    for ad in {v.get("availability-domain") for v in volumes
               if v.get("availability-domain")}:
        for att in list_boot_volume_attachments(cli, compartment_id, ad):
            if att.get("boot-volume-id"):
                attached_ids.add(att.get("boot-volume-id"))
    orphans = []
    for volume in volumes:
        if volume.get("id") in attached_ids:
            continue
        detail = get_boot_volume(cli, volume.get("id")) or {}
        if not is_rescue_orphan(detail, attached_ids):
            continue
        source = detail.get("source-details") or {}
        orphans.append({
            "id": volume.get("id"),
            "display-name": volume.get("display-name"),
            "size-in-gbs": volume.get("size-in-gbs"),
            "lifecycle-state": volume.get("lifecycle-state"),
            "availability-domain": volume.get("availability-domain"),
            "source-backup-id": source.get("bootVolumeBackupId"),
        })
    return orphans


def total_rescue_orphan_gb(orphans):
    """孤儿盘占用额度合计（GB）。"""
    return sum(o.get("size-in-gbs") or 0 for o in orphans or [])


# ================= 4. Netboot 救机向导 =================

def netboot_guide(instance_id, region=None, tftp_server=None):
    """Netboot 救机向导（针对“盘在、系统砖了”）。

    返回结构化步骤：先决条件 / 控制台连接说明 / EFI 命令 /
    TFTP 节点 / 菜单指引 / DD 步骤。抢 ESC、菜单方向键是手动步骤，
    标记 manual=true；创建控制台连接可自动化（走控制台连接管理）。
    """
    server = tftp_server or NETBOOT_TFTP_NODES[0]["host"]
    return {
        "instance_id": instance_id,
        "region": region,
        "applies_when": "实例有启动盘但系统砖了（无法启动/无法 SSH），"
                        "无盘实例不适用（先走无盘救机）。",
        "prerequisites": [
            "实例当前挂着启动盘（前置检查确认）",
            "一把 RSA 公钥（串行控制台只接受 RSA，ed25519 会被拒）",
            "一个就近的 TFTP 节点（默认新加坡节点）",
        ],
        "console_steps": [
            {"text": "创建串行控制台连接（面板/机器人一键创建，约 11 秒 ACTIVE）",
             "manual": False},
            {"text": "用返回的 ssh 连接命令连上串行控制台",
             "manual": False},
            {"text": "重启实例，在启动瞬间抢按 ESC 进 BIOS/启动菜单（窗口很短，"
                     "多试几次）", "manual": True},
            {"text": "在启动菜单里选进入 EFI Shell", "manual": True},
        ],
        "efi_commands": NETBOOT_EFI_COMMANDS,
        "tftp_server": server,
        "tftp_nodes": NETBOOT_TFTP_NODES,
        "menu_steps": [{"text": step, "manual": True}
                       for step in NETBOOT_MENU_STEPS],
        "dd_steps": [
            "进 Alpine 临时系统后，先确认网络通（ping 网关 / ping 8.8.8.8）",
            "跑 DD 脚本重装目标系统（InstallNET.sh，Debian 13，SSH 端口 14422）",
            "DD 完成后等机器重启，用新系统 SSH 登录验证",
        ],
    }


# ================= 5. 救机前置检查 =================

def preflight_check(cli, compartment_id, instance_id, action,
                    needed_gb=None, volume_id=None):
    """开机/恢复/删盘/终止前自动检查，给出能不能做、缺什么、风险。

    action 见 PREFLIGHT_ACTIONS。needed_gb 是恢复/新建需要的新盘大小
    （不传时跳过额度检查并在 warnings 里说明）；volume_id 是删盘操作
    的目标盘（不传时用实例当前挂的启动盘）。
    返回 {"action", "can_do", "blockers", "warnings", "facts"}。
    """
    if action not in PREFLIGHT_ACTIONS:
        raise ValueError(f"未知的前置检查操作：{action}")
    blockers, warnings = [], []
    detail = _unwrap(get_instance(cli, instance_id)) or {}
    state = detail.get("lifecycle-state")
    attachment = get_boot_volume_attachment(cli, compartment_id, instance_id)
    boot_volume_id = (attachment or {}).get("boot-volume-id")
    facts = {
        "instance_id": instance_id,
        "instance_name": detail.get("display-name"),
        "instance_state": state,
        "has_boot_volume": attachment is not None,
        "boot_volume_id": boot_volume_id,
        "used_gb": None,
        "remaining_gb": None,
        "is_backup_source": False,
        "backup_count": 0,
    }
    if needed_gb is not None:
        quota = check_volume_quota(cli, compartment_id, needed_gb)
        facts["used_gb"] = quota["used_gb"]
        facts["remaining_gb"] = quota["remaining_gb"]
        if not quota["ok"]:
            blockers.append(
                f"启动盘额度不足：还剩 {quota['remaining_gb']}GB，"
                f"需要 {needed_gb}GB。请先清理孤儿盘腾额度。")
    if action == "start":
        if not attachment:
            blockers.append(
                "实例没有挂载启动盘，无法启动（OCI 409）。"
                "原实例无法原地救回，请走无盘救机从备份新建实例。")
        elif state not in ("STOPPED", "RUNNING"):
            blockers.append(f"实例当前状态是 {state or '未知'}，无法启动。")
    elif action == "restore":
        if state not in ("RUNNING", "STOPPED"):
            blockers.append(
                f"实例当前状态是 {state or '未知'}，只有运行中或已停止的"
                "实例才能安全恢复。")
        if not attachment:
            blockers.append(
                "实例没有挂载启动盘，原生换盘要求当前有盘挂载（无盘时 400）。"
                "请走无盘救机从备份新建实例。")
        if needed_gb is None:
            warnings.append("未传新盘大小，未做额度检查；恢复前请确认额度。")
    elif action == "rebuild_from_backup":
        if attachment:
            blockers.append(
                "实例当前挂着启动盘，不适用无盘救机。"
                "要恢复系统请走安全恢复（从备份换盘）。")
        else:
            warnings.append(
                "从备份新建实例会产生新 OCID 与新公网 IP，旧实例无法复活。")
    elif action in ("delete_volume", "terminate"):
        target = volume_id or boot_volume_id
        if action == "delete_volume" and not target:
            blockers.append("未指定要删除的启动盘。")
        if action == "delete_volume" and attachment \
                and boot_volume_id == target:
            blockers.append("该启动盘正挂在实例上，不能直接删除，"
                            "请先终止实例或换盘。")
        if target:
            safety = check_delete_volume_safety(cli, compartment_id, target)
            facts["is_backup_source"] = safety["is_backup_source"]
            facts["backup_count"] = len(safety["backups"])
            if safety["is_backup_source"]:
                if action == "delete_volume":
                    blockers.append("已拦截：" + safety["warning"])
                else:
                    warnings.append(
                        "终止实例会永久删除引导卷：" + safety["warning"])
    return {"action": action,
            "can_do": not blockers,
            "blockers": blockers,
            "warnings": warnings,
            "facts": facts}


# ================= 6. 控制台连接管理 =================

def create_console(cli, instance_id, ssh_public_key_file=None):
    """创建串行控制台连接（RSA 校验见 oci.console）。"""
    return _create_console_connection(cli, instance_id, ssh_public_key_file)


def list_consoles(cli, compartment_id, instance_id=None):
    """列出串行控制台连接。"""
    return _list_console_connections(cli, compartment_id, instance_id)


def delete_console(cli, connection_id):
    """删除串行控制台连接。"""
    return _delete_console_connection(cli, connection_id)
