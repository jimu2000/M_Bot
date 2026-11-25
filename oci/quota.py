#!/usr/bin/env python3
"""免费额度查询：查 Always Free 额度用了多少、还能不能再开实例。

只读模块：只调 instance list 与 boot-volume list，不做任何写操作。

来源说明：额度常量来自 Oracle Cloud Always Free 官方额度
（https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier.htm），
每个租户在每个区域的免费额度。注意这是"租户级"额度，不是单个
compartment 的额度——本模块按传入的 compartment_id 查询，传根
compartment（tenancy OCID）即查整个租户的用量。

安全说明：
  - 本模块不读取、不打印、不外传 API 私钥。认证全部由 OCI CLI 自己处理。
  - 配置区在文件顶端，真实 OCID 一律用占位符，绝不写进代码。

用法示例（占位符）：
    from oci.client import OCIClient
    from oci.quota import get_free_tier_summary, can_launch
    cli = OCIClient(profile="DEFAULT", region="ap-singapore-1")
    summary = get_free_tier_summary(cli, "ocid1.tenancy.oc1..xxxxxxxx")
    ok, reason = can_launch(cli, "ocid1.tenancy.oc1..xxxxxxxx",
                            "VM.Standard.E2.1.Micro")
"""

# ================= 配置区（改这里） =================
# Oracle Always Free official quotas (source: see the module docstring). Oracle Always Free 官方额度（来源见模块 docstring）。
A1_MAX_OCPU = 4  # Ampere A1.Flex: at most 4 OCPUs. Ampere A1.Flex：最多 4 个 OCPU。
A1_MAX_MEMORY_GB = 24  # Ampere A1.Flex: at most 24 GB of memory. Ampere A1.Flex：最多 24GB 内存。
E2_MAX_INSTANCES = 2  # VM.Standard.E2.1.Micro: at most 2 instances. VM.Standard.E2.1.Micro：最多 2 台。
BOOT_VOLUME_MAX_GB = 200  # Boot volume total cap: 200 GB. 启动盘总量上限：200GB。
# Default boot volume size (GB) for a new instance. Without --boot-volume-size-in-gbs, OCI defaults to 47 GB. 新建一台实例默认占用的启动盘大小（GB）。创建实例时若不指定 --boot-volume-size-in-gbs，OCI 默认 47GB。
BOOT_VOLUME_DEFAULT_GB = 47
SHAPE_A1_FLEX = "VM.Standard.A1.Flex"
SHAPE_E2_MICRO = "VM.Standard.E2.1.Micro"
# A1 config health check: a single instance above this shape gets a downsize suggestion (a reference value for common needs; e.g. a proxy / cloud drive light load usually runs fine on 2 OCPUs / 12 GB). A1 配置体检：单台实例超过该规格即建议降配（常用需求参考值，如跑代理/网盘等轻负载一般 2 核/12GB 已足够）。
A1_TYPICAL_OCPU = 2
A1_TYPICAL_MEMORY_GB = 12
# =====================================================


def _fmt(n):
    """数字转字符串：4.0 显示为 4，方便拼中文提示。"""
    if isinstance(n, float) and n.is_integer():
        return str(int(n))
    return str(n)


def _list_instances(cli, compartment_id):
    """列出 compartment 下所有实例，返回 list（空则 []）。

    instance list 返回 {"data": [...]}；空输出时 cli.run 返回 None。
    """
    result = cli.run(["compute", "instance", "list",
                      "--compartment-id", compartment_id])
    if not result:
        return []
    if isinstance(result, dict):
        return result.get("data") or []
    return result


def _list_boot_volumes(cli, compartment_id):
    """列出 compartment 下所有启动盘，返回 list（空则 []）。"""
    result = cli.run(["bv", "boot-volume", "list",
                      "--compartment-id", compartment_id])
    if not result:
        return []
    if isinstance(result, dict):
        return result.get("data") or []
    return result


def get_a1_usage(cli, compartment_id):
    """查 A1.Flex 使用量。

    只统计 RUNNING 状态的 VM.Standard.A1.Flex 实例，把每台 shape-config
    里的 ocpus 与 memory-in-gbs 加总。

    返回：
        {"ocpu_used": X, "ocpu_limit": 4,
         "memory_used_gb": Y, "memory_limit_gb": 24,
         "instances": [{"id", "display_name", "ocpus", "memory_gb"}, ...]}
    """
    ocpu_used = 0
    memory_used_gb = 0
    instances = []
    for inst in _list_instances(cli, compartment_id):
        if inst.get("lifecycle-state") != "RUNNING":
            continue
        if inst.get("shape") != SHAPE_A1_FLEX:
            continue
        cfg = inst.get("shape-config") or {}
        ocpus = cfg.get("ocpus") or 0
        memory_gb = cfg.get("memory-in-gbs") or 0
        ocpu_used += ocpus
        memory_used_gb += memory_gb
        instances.append({
            "id": inst.get("id"),
            "display_name": inst.get("display-name"),
            "ocpus": ocpus,
            "memory_gb": memory_gb,
        })
    return {
        "ocpu_used": ocpu_used,
        "ocpu_limit": A1_MAX_OCPU,
        "memory_used_gb": memory_used_gb,
        "memory_limit_gb": A1_MAX_MEMORY_GB,
        "instances": instances,
    }


def get_e2_usage(cli, compartment_id):
    """查 E2.1.Micro 数量。

    只统计 RUNNING 状态的 VM.Standard.E2.1.Micro 实例。

    返回：
        {"count": N, "limit": 2,
         "instances": [{"id", "display_name"}, ...]}
    """
    instances = []
    for inst in _list_instances(cli, compartment_id):
        if inst.get("lifecycle-state") != "RUNNING":
            continue
        if inst.get("shape") != SHAPE_E2_MICRO:
            continue
        instances.append({
            "id": inst.get("id"),
            "display_name": inst.get("display-name"),
        })
    return {
        "count": len(instances),
        "limit": E2_MAX_INSTANCES,
        "instances": instances,
    }


def get_boot_volume_usage(cli, compartment_id):
    """查启动盘总量。

    统计除 TERMINATING / TERMINATED 之外的全部启动盘，把 size-in-gbs
    加总。注意启动盘没有 IN-USE 状态，挂在运行中实例上的盘同样是
    AVAILABLE（2026-11-23 实测），所以不能只按 AVAILABLE 过滤——
    要排除的是已删除（TERMINATED）与删除中（TERMINATING）的盘，
    它们不占免费额度。

    返回：
        {"used_gb": X, "limit_gb": 200,
         "volumes": [{"id", "display_name", "size_gb"}, ...]}
    """
    used_gb = 0
    volumes = []
    for vol in _list_boot_volumes(cli, compartment_id):
        if vol.get("lifecycle-state") in ("TERMINATING", "TERMINATED"):
            continue
        size_gb = vol.get("size-in-gbs") or 0
        used_gb += size_gb
        volumes.append({
            "id": vol.get("id"),
            "display_name": vol.get("display-name"),
            "size_gb": size_gb,
        })
    return {
        "used_gb": used_gb,
        "limit_gb": BOOT_VOLUME_MAX_GB,
        "volumes": volumes,
    }


def get_free_tier_summary(cli, compartment_id):
    """汇总 A1 / E2 / 启动盘三项额度，返回 used/limit/remaining 与 overall_ok。

    remaining = limit - used，超限时为负数。
    overall_ok 为 True 当且仅当三项全部未超限。
    """
    a1 = get_a1_usage(cli, compartment_id)
    e2 = get_e2_usage(cli, compartment_id)
    boot = get_boot_volume_usage(cli, compartment_id)

    a1_ok = (a1["ocpu_used"] <= a1["ocpu_limit"]
             and a1["memory_used_gb"] <= a1["memory_limit_gb"])
    e2_ok = e2["count"] <= e2["limit"]
    boot_ok = boot["used_gb"] <= boot["limit_gb"]

    return {
        "a1": {
            "ocpu_used": a1["ocpu_used"],
            "ocpu_limit": a1["ocpu_limit"],
            "ocpu_remaining": a1["ocpu_limit"] - a1["ocpu_used"],
            "memory_used_gb": a1["memory_used_gb"],
            "memory_limit_gb": a1["memory_limit_gb"],
            "memory_remaining_gb": a1["memory_limit_gb"] - a1["memory_used_gb"],
            "instances": a1["instances"],
        },
        "e2": {
            "count": e2["count"],
            "limit": e2["limit"],
            "remaining": e2["limit"] - e2["count"],
            "instances": e2["instances"],
        },
        "boot_volume": {
            "used_gb": boot["used_gb"],
            "limit_gb": boot["limit_gb"],
            "remaining_gb": boot["limit_gb"] - boot["used_gb"],
            "volumes": boot["volumes"],
        },
        "overall_ok": a1_ok and e2_ok and boot_ok,
    }


def a1_health_check(cli, compartment_id):
    """A1 配置体检：查超限、降配空间与启动盘。

    返回 {"overall_ok": bool, "suggestions": [...]}，
    每条建议含 severity 与 message：
      - error：A1 OCPU / 内存或启动盘已超免费额度（会产生费用）；
      - warning：A1 OCPU / 内存已用满，无法再开 A1 实例；
      - info：某台 A1 实例规格高于常用需求（2 核/12GB），建议降配；
      - ok：全部正常时唯一的一条建议。
    overall_ok 为 True 当且仅当没有任何 error 级建议。
    """
    summary = get_free_tier_summary(cli, compartment_id)
    suggestions = []
    a1 = summary["a1"]
    boot = summary["boot_volume"]

    if a1["ocpu_used"] > a1["ocpu_limit"]:
        suggestions.append({
            "severity": "error",
            "message": (f"A1 OCPU 超免费额度：已用 {_fmt(a1['ocpu_used'])} 核，"
                        f"上限 {_fmt(a1['ocpu_limit'])} 核，"
                        "超出的部分会按量计费，请降配或释放实例。"),
        })
    elif a1["ocpu_used"] == a1["ocpu_limit"]:
        suggestions.append({
            "severity": "warning",
            "message": (f"A1 OCPU 已用满（{_fmt(a1['ocpu_used'])}/"
                        f"{_fmt(a1['ocpu_limit'])} 核），无法再开 A1 实例。"),
        })
    if a1["memory_used_gb"] > a1["memory_limit_gb"]:
        suggestions.append({
            "severity": "error",
            "message": (f"A1 内存超免费额度：已用 {_fmt(a1['memory_used_gb'])}GB，"
                        f"上限 {_fmt(a1['memory_limit_gb'])}GB，"
                        "超出的部分会按量计费，请降配或释放实例。"),
        })
    elif a1["memory_used_gb"] == a1["memory_limit_gb"]:
        suggestions.append({
            "severity": "warning",
            "message": (f"A1 内存已用满（{_fmt(a1['memory_used_gb'])}/"
                        f"{_fmt(a1['memory_limit_gb'])}GB），"
                        "无法再开 A1 实例。"),
        })
    if boot["used_gb"] > boot["limit_gb"]:
        suggestions.append({
            "severity": "error",
            "message": (f"启动盘超免费额度：已用 {_fmt(boot['used_gb'])}GB，"
                        f"上限 {_fmt(boot['limit_gb'])}GB，"
                        "超出的部分会按量计费，请清理孤儿盘或释放实例。"),
        })

    for inst in a1["instances"]:
        ocpus = inst.get("ocpus") or 0
        memory_gb = inst.get("memory_gb") or 0
        if ocpus > A1_TYPICAL_OCPU or memory_gb > A1_TYPICAL_MEMORY_GB:
            suggestions.append({
                "severity": "info",
                "message": (f"实例「{inst.get('display_name') or '?'}」"
                            f"当前 {_fmt(ocpus)} 核/{_fmt(memory_gb)}GB，"
                            f"高于常用需求（{A1_TYPICAL_OCPU} 核/"
                            f"{A1_TYPICAL_MEMORY_GB}GB），"
                            "如负载不高可考虑降配释放额度。"),
            })

    if not suggestions:
        suggestions.append({
            "severity": "ok",
            "message": "A1 配置健康：未超免费额度，暂无降配建议。",
        })
    overall_ok = all(s["severity"] != "error" for s in suggestions)
    return {"overall_ok": overall_ok, "suggestions": suggestions}


def _normalize_shape(shape):
    """规格名归一化：VM.Standard.E2.1.Micro 与 E2.1.Micro 都接受。"""
    if not shape:
        return ""
    return shape.replace("VM.Standard.", "")


def check_a1_single_instance_cap(ocpus, memory_gb):
    """校验单台 A1 实例规格是否在免费额度内。

    Always Free 额度内单台 A1.Flex 上限是 4 OCPU / 24GB（与租户级
    A1 总额度一致：一台占满即用完免费额度）。创建（web/routes/create.py）
    与升降配（web/routes/instances.py）共用此校验，超过即报错，
    防止误开付费规格。

    返回 None 表示通过，否则返回错误信息字符串。
    """
    if ocpus is not None and ocpus > A1_MAX_OCPU:
        return (f"A1 免费额度内单台上限为 {_fmt(A1_MAX_OCPU)} OCPU / "
                f"{_fmt(A1_MAX_MEMORY_GB)}GB，超过会产生费用")
    if memory_gb is not None and memory_gb > A1_MAX_MEMORY_GB:
        return (f"A1 免费额度内单台上限为 {_fmt(A1_MAX_OCPU)} OCPU / "
                f"{_fmt(A1_MAX_MEMORY_GB)}GB，超过会产生费用")
    return None


def can_launch(cli, compartment_id, shape, ocpus=0, memory_gb=0):
    """判断能不能再开一台指定规格的实例。

    Args:
        shape: 规格名，如 "VM.Standard.E2.1.Micro" 或 "VM.Standard.A1.Flex"
            （不带 VM.Standard. 前缀的简写也接受）。
        ocpus / memory_gb: A1.Flex 要开的 OCPU 数与内存（GB），E2 不用传
            （E2.1.Micro 是固定 1 核 1GB）。

    检查规则：
        E2.1.Micro：E2 数量 < 2，且启动盘余量 >= 47GB。
        A1.Flex：OCPU 余量 >= ocpus，内存余量 >= memory_gb，
            且启动盘余量 >= 47GB。

    返回 (bool, reason) 元组，reason 是中文说明。
    """
    summary = get_free_tier_summary(cli, compartment_id)
    boot_remaining = summary["boot_volume"]["remaining_gb"]
    short = _normalize_shape(shape)

    if short == "E2.1.Micro":
        e2 = summary["e2"]
        if e2["count"] >= e2["limit"]:
            return False, f"E2 已满（{_fmt(e2['count'])}/{_fmt(e2['limit'])}）"
        if boot_remaining < BOOT_VOLUME_DEFAULT_GB:
            return (False,
                    f"启动盘余量不足（剩 {_fmt(boot_remaining)}GB，"
                    f"需要 {BOOT_VOLUME_DEFAULT_GB}GB）")
        return True, "可以创建"

    if short == "A1.Flex":
        a1 = summary["a1"]
        if a1["ocpu_remaining"] < ocpus:
            return (False,
                    f"A1 OCPU 不足（已用 {_fmt(a1['ocpu_used'])}/"
                    f"{_fmt(a1['ocpu_limit'])} 核，需要 {_fmt(ocpus)} 核）")
        if a1["memory_remaining_gb"] < memory_gb:
            return (False,
                    f"A1 内存不足（已用 {_fmt(a1['memory_used_gb'])}/"
                    f"{_fmt(a1['memory_limit_gb'])}GB，需要 {_fmt(memory_gb)}GB）")
        if boot_remaining < BOOT_VOLUME_DEFAULT_GB:
            return (False,
                    f"启动盘余量不足（剩 {_fmt(boot_remaining)}GB，"
                    f"需要 {BOOT_VOLUME_DEFAULT_GB}GB）")
        return True, "可以创建"

    return False, f"不支持的规格：{shape}（仅支持 E2.1.Micro 与 A1.Flex）"


def can_launch_batch(cli, compartment_id, shape, ocpus=0, memory_gb=0,
                     count=1, boot_volume_size_gbs=None):
    """判断能不能再开 count 台指定规格的实例（批量开机配额预检）。

    Args:
        shape: 规格名，如 "VM.Standard.E2.1.Micro" 或 "VM.Standard.A1.Flex"
            （不带 VM.Standard. 前缀的简写也接受）。
        ocpus / memory_gb: A1.Flex 每台的 OCPU 数与内存（GB），E2 不用传
            （E2.1.Micro 是固定 1 核 1GB）。
        count: 要创建的台数（< 1 时按 1 算）。
        boot_volume_size_gbs: 每台的启动盘大小（GB），None 时按镜像默认
            BOOT_VOLUME_DEFAULT_GB（47GB）算。

    检查规则（单台规则 × 数量）：
        E2.1.Micro：E2 余量 >= count，且启动盘余量 >= 每台盘大小 × count。
        A1.Flex：OCPU 余量 >= ocpus × count，内存余量 >= memory_gb × count，
            且启动盘余量 >= 每台盘大小 × count。

    返回 (bool, reason) 元组，reason 是中文说明。
    """
    count = max(1, int(count or 1))
    boot_each = boot_volume_size_gbs or BOOT_VOLUME_DEFAULT_GB
    boot_need = boot_each * count
    summary = get_free_tier_summary(cli, compartment_id)
    boot_remaining = summary["boot_volume"]["remaining_gb"]
    short = _normalize_shape(shape)

    if short == "E2.1.Micro":
        e2 = summary["e2"]
        if e2["remaining"] < count:
            return (False,
                    f"E2 余量不足（剩 {_fmt(e2['remaining'])} 台，"
                    f"需要 {count} 台）")
        if boot_remaining < boot_need:
            return (False,
                    f"启动盘余量不足（剩 {_fmt(boot_remaining)}GB，"
                    f"{count} 台需要 {boot_need}GB）")
        return True, "可以创建"

    if short == "A1.Flex":
        a1 = summary["a1"]
        if a1["ocpu_remaining"] < ocpus * count:
            return (False,
                    f"A1 OCPU 不足（已用 {_fmt(a1['ocpu_used'])}/"
                    f"{_fmt(a1['ocpu_limit'])} 核，{count} 台需要 "
                    f"{_fmt(ocpus * count)} 核）")
        if a1["memory_remaining_gb"] < memory_gb * count:
            return (False,
                    f"A1 内存不足（已用 {_fmt(a1['memory_used_gb'])}/"
                    f"{_fmt(a1['memory_limit_gb'])}GB，{count} 台需要 "
                    f"{_fmt(memory_gb * count)}GB）")
        if boot_remaining < boot_need:
            return (False,
                    f"启动盘余量不足（剩 {_fmt(boot_remaining)}GB，"
                    f"{count} 台需要 {boot_need}GB）")
        return True, "可以创建"

    return False, f"不支持的规格：{shape}（仅支持 E2.1.Micro 与 A1.Flex）"
