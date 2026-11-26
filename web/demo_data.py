#!/usr/bin/env python3
"""演示模式（DEMO_MODE）的模拟数据与分流辅助。

开启方式：环境变量 MBOT_DEMO_MODE=1（见 web/config.py 的 DEMO_MODE）。
演示模式下全部数据接口返回这里的模拟数据，不调真实 OCI；写操作
只返回假成功（见 fake_success），不改真实状态。

模拟数据全部是假的、明显可识别的：
  - 租户名：DEMO_SG / DEMO_SJ / DEMO_CHI（显示名“新加坡演示”等）；
  - 实例名：DEMO-ARM-01 / DEMO-AMD-01 等；
  - 公网 IP：TEST-NET-3 段 203.0.113.x（RFC 5737 文档地址）；
  - 内网 IP：10.0.0.x；IPv6：2001:db8::/32 文档段；
  - OCID：形如 ocid1.instance.oc1..demoinstance0001，含 demo 前缀；
  - 域名：只用 example.com。
绝不出现真实租户名、真实实例名、真实 IP、真实 OCID、真实凭据。

配额/健康检查/能否开机按模拟数据实时算出（与真实 oci/quota.py
同一套规则），所以各页面的数字互相一致（如 A1 已用核数 = 运行中
A1 实例核数之和，启动盘已用 = 未删除启动盘大小之和）。

本模块只提供数据与纯函数，不直接调 OCI、不读写文件；路由层在
demo 模式下调这里，非 demo 模式走原有逻辑（见各路由开头的
demo_data.is_demo() 分支）。
"""

import time

from web import config

# Demo login: demo/demo (works only in demo mode; the real admin login
# still works in demo mode too). 演示登录账号：demo/demo（仅演示模式
# 可用；演示模式下真实管理员登录仍可用）。
DEMO_USERNAME = "demo"
DEMO_PASSWORD = "demo"

# Default demo profile (used when the request does not name one). 默认演示租户（请求没指定时用）。
DEFAULT_DEMO_PROFILE = "DEMO_SG"

# Fake compartment OCIDs (obviously fake: "demo" + zeros; never a real tenancy). 假 compartment OCID（明显是假的：demo + 0，绝不是真实 tenancy）。
_DEMO_TENANCY = "ocid1.tenancy.oc1..demo0000000000000000000000000000000000000000000000000000000"

# Demo tenants: profile key -> home region / display name / compartment. 演示租户：profile 键 -> 主区 / 显示名 / compartment。
DEMO_PROFILES = {
    "DEMO_SG": {
        "region": "ap-singapore-1",
        "region_label": "新加坡",
        "display_name": "新加坡演示",
        "compartment_id": _DEMO_TENANCY,
    },
    "DEMO_SJ": {
        "region": "us-sanjose-1",
        "region_label": "圣何塞",
        "display_name": "圣何塞演示",
        "compartment_id": _DEMO_TENANCY,
    },
    "DEMO_CHI": {
        "region": "us-chicago-1",
        "region_label": "芝加哥",
        "display_name": "芝加哥演示",
        "compartment_id": _DEMO_TENANCY,
    },
}

# Free-tier limits (same as oci/quota.py). 免费额度上限（与 oci/quota.py 一致）。
A1_OCPU_LIMIT = 4
A1_MEMORY_LIMIT_GB = 24
E2_LIMIT = 2
BOOT_VOLUME_LIMIT_GB = 200
BOOT_VOLUME_DEFAULT_GB = 47
BACKUP_LIMIT = 5

# Demo instances, keyed by profile. Each entry carries everything the
# panel needs: OCI-shape fields (id/display-name/shape/lifecycle-state/
# shape-config/time-created/availability-domain) plus panel extras
# (public/private/IPv6, cpu model, boot volume). 演示实例（按租户分）：
# 每条含 OCI 形状字段与面板附加字段（公网/内网/IPv6、CPU 型号、启动盘）。
DEMO_INSTANCES = {
    "DEMO_SG": [
        {
            "id": "ocid1.instance.oc1..demoinstance0001",
            "display_name": "DEMO-ARM-01",
            "shape": "VM.Standard.A1.Flex",
            "state": "RUNNING",
            "ocpus": 1, "memory_gb": 6,
            "availability_domain": "DEMO:AP-SINGAPORE-1-AD-1",
            "time_created": "2026-11-20T08:00:00.000000+00:00",
            "public_ip": "203.0.113.11",
            "private_ip": "10.0.0.11",
            "ipv6": ["2001:db8::11"],
            "cpu_model": "Ampere Altra",
            "boot_volume": {"id": "ocid1.bootvolume.oc1..demovolume0001",
                            "size_gb": 47, "vpus": 10,
                            "state": "AVAILABLE"},
        },
        {
            "id": "ocid1.instance.oc1..demoinstance0002",
            "display_name": "DEMO-ARM-02",
            "shape": "VM.Standard.A1.Flex",
            "state": "RUNNING",
            "ocpus": 2, "memory_gb": 12,
            "availability_domain": "DEMO:AP-SINGAPORE-1-AD-1",
            "time_created": "2026-11-21T09:30:00.000000+00:00",
            "public_ip": "203.0.113.12",
            "private_ip": "10.0.0.12",
            "ipv6": [],
            "cpu_model": "Ampere Altra",
            "boot_volume": {"id": "ocid1.bootvolume.oc1..demovolume0002",
                            "size_gb": 47, "vpus": 10,
                            "state": "AVAILABLE"},
        },
        {
            "id": "ocid1.instance.oc1..demoinstance0003",
            "display_name": "DEMO-AMD-01",
            "shape": "VM.Standard.E2.1.Micro",
            "state": "RUNNING",
            "ocpus": 1, "memory_gb": 1,
            "availability_domain": "DEMO:AP-SINGAPORE-1-AD-1",
            "time_created": "2026-11-22T10:15:00.000000+00:00",
            "public_ip": "203.0.113.13",
            "private_ip": "10.0.0.13",
            "ipv6": ["2001:db8::13"],
            "cpu_model": "AMD EPYC 7J13",
            "boot_volume": {"id": "ocid1.bootvolume.oc1..demovolume0003",
                            "size_gb": 47, "vpus": 10,
                            "state": "AVAILABLE"},
        },
        {
            "id": "ocid1.instance.oc1..demoinstance0004",
            "display_name": "DEMO-AMD-02",
            "shape": "VM.Standard.E2.1.Micro",
            "state": "STOPPED",
            "ocpus": 1, "memory_gb": 1,
            "availability_domain": "DEMO:AP-SINGAPORE-1-AD-1",
            "time_created": "2026-11-23T11:00:00.000000+00:00",
            "public_ip": None,
            "private_ip": "10.0.0.14",
            "ipv6": [],
            "cpu_model": "AMD EPYC 7J13",
            "boot_volume": {"id": "ocid1.bootvolume.oc1..demovolume0004",
                            "size_gb": 47, "vpus": 10,
                            "state": "AVAILABLE"},
        },
    ],
    "DEMO_SJ": [
        {
            "id": "ocid1.instance.oc1..demoinstance0011",
            "display_name": "DEMO-SJ-ARM-01",
            "shape": "VM.Standard.A1.Flex",
            "state": "RUNNING",
            "ocpus": 4, "memory_gb": 24,
            "availability_domain": "DEMO:US-SANJOSE-1-AD-1",
            "time_created": "2026-11-19T07:45:00.000000+00:00",
            "public_ip": "203.0.113.21",
            "private_ip": "10.0.0.21",
            "ipv6": [],
            "cpu_model": "Ampere Altra",
            "boot_volume": {"id": "ocid1.bootvolume.oc1..demovolume0011",
                            "size_gb": 50, "vpus": 10,
                            "state": "AVAILABLE"},
        },
        {
            "id": "ocid1.instance.oc1..demoinstance0012",
            "display_name": "DEMO-SJ-AMD-01",
            "shape": "VM.Standard.E2.1.Micro",
            "state": "RUNNING",
            "ocpus": 1, "memory_gb": 1,
            "availability_domain": "DEMO:US-SANJOSE-1-AD-1",
            "time_created": "2026-11-20T12:20:00.000000+00:00",
            "public_ip": "203.0.113.22",
            "private_ip": "10.0.0.22",
            "ipv6": ["2001:db8::22"],
            "cpu_model": "AMD EPYC 7J13",
            "boot_volume": {"id": "ocid1.bootvolume.oc1..demovolume0012",
                            "size_gb": 47, "vpus": 10,
                            "state": "AVAILABLE"},
        },
    ],
    "DEMO_CHI": [
        {
            "id": "ocid1.instance.oc1..demoinstance0021",
            "display_name": "DEMO-CHI-ARM-01",
            "shape": "VM.Standard.A1.Flex",
            "state": "RUNNING",
            "ocpus": 2, "memory_gb": 10,
            "availability_domain": "DEMO:US-CHICAGO-1-AD-1",
            "time_created": "2026-11-18T06:10:00.000000+00:00",
            "public_ip": "203.0.113.31",
            "private_ip": "10.0.0.31",
            "ipv6": [],
            "cpu_model": "Ampere Altra",
            "boot_volume": {"id": "ocid1.bootvolume.oc1..demovolume0021",
                            "size_gb": 47, "vpus": 10,
                            "state": "AVAILABLE"},
        },
        {
            "id": "ocid1.instance.oc1..demoinstance0022",
            "display_name": "DEMO-CHI-ARM-02",
            "shape": "VM.Standard.A1.Flex",
            "state": "STOPPED",
            "ocpus": 1, "memory_gb": 6,
            "availability_domain": "DEMO:US-CHICAGO-1-AD-1",
            "time_created": "2026-11-19T14:40:00.000000+00:00",
            "public_ip": None,
            "private_ip": "10.0.0.32",
            "ipv6": [],
            "cpu_model": "Ampere Altra",
            "boot_volume": {"id": "ocid1.bootvolume.oc1..demovolume0022",
                            "size_gb": 47, "vpus": 10,
                            "state": "AVAILABLE"},
        },
    ],
}

# Orphan boot volumes (AVAILABLE, unattached): one in the Chicago demo
# tenant. 孤儿启动盘（AVAILABLE、未挂载）：芝加哥演示租户里有一块。
DEMO_ORPHAN_VOLUMES = {
    "DEMO_CHI": [
        {
            "id": "ocid1.bootvolume.oc1..demovolume0023",
            "display_name": "DEMO-CHI-ORPHAN-01 (Boot Volume)",
            "size_gb": 47, "vpus": 10, "state": "AVAILABLE",
            "availability_domain": "DEMO:US-CHICAGO-1-AD-1",
            "time_created": "2026-11-19T14:40:00.000000+00:00",
        },
    ],
}

# Demo boot-volume backups, keyed by profile. 演示启动盘备份（按租户分）。
DEMO_BACKUPS = {
    "DEMO_SG": [
        {
            "id": "ocid1.bootvolumebackup.oc1..demobackup0001",
            "display_name": "DEMO-ARM-01-backup-20261124-0900",
            "boot_volume_id": "ocid1.bootvolume.oc1..demovolume0001",
            "size_gb": 47, "state": "AVAILABLE",
            "source_type": "MANUAL", "type": "FULL",
            "time_created": "2026-11-24T09:00:00.000000+00:00",
            "image_id": None,
        },
        {
            "id": "ocid1.bootvolumebackup.oc1..demobackup0002",
            "display_name": "DEMO-AMD-01-backup-20261125-1000",
            "boot_volume_id": "ocid1.bootvolume.oc1..demovolume0003",
            "size_gb": 47, "state": "CREATING",
            "source_type": "MANUAL", "type": "FULL",
            "time_created": "2026-11-25T10:00:00.000000+00:00",
            "image_id": None,
        },
    ],
    "DEMO_SJ": [
        {
            "id": "ocid1.bootvolumebackup.oc1..demobackup0011",
            "display_name": "DEMO-SJ-ARM-01-backup-20261123-0800",
            "boot_volume_id": "ocid1.bootvolume.oc1..demovolume0011",
            "size_gb": 50, "state": "AVAILABLE",
            "source_type": "MANUAL", "type": "FULL",
            "time_created": "2026-11-23T08:00:00.000000+00:00",
            "image_id": None,
        },
    ],
    "DEMO_CHI": [
        {
            "id": "ocid1.bootvolumebackup.oc1..demobackup0021",
            "display_name": "DEMO-CHI-ARM-01-backup-20261122-0700",
            "boot_volume_id": "ocid1.bootvolume.oc1..demovolume0021",
            "size_gb": 47, "state": "AVAILABLE",
            "source_type": "MANUAL", "type": "FULL",
            "time_created": "2026-11-22T07:00:00.000000+00:00",
            "image_id": None,
        },
    ],
}

# Demo tasks (same record shape as tgbot/task_store.py records:
# id/kind/status/detail/label/logs/result/created_at/origin/
# cancel_requested). 演示任务（与 tgbot/task_store.py 的记录同一形状）。
DEMO_TASKS = [
    {
        "id": "demo-grab-1",
        "kind": "grab",
        "status": "running",
        "detail": "第 3 次失败（无容量），29 秒后重试。",
        "label": "抢机：DEMO-AMD-03 (VM.Standard.E2.1.Micro)",
        "logs": ["第 1 次失败（无容量）", "第 2 次失败（无容量）",
                 "第 3 次失败（无容量），29 秒后重试。"],
        "result": None,
        "created_at_offset": 3600,
        "origin": "web",
        "cancel_requested": False,
    },
    {
        "id": "demo-grab-2",
        "kind": "grab",
        "status": "done",
        "detail": "已开出，CPU：Ampere Altra。",
        "label": "抢机：DEMO-ARM-02 (VM.Standard.A1.Flex)",
        "logs": ["第 1 次创建成功", "CPU 检测：Ampere Altra，符合要求",
                 "已完成。"],
        "result": {"instance_id": "ocid1.instance.oc1..demoinstance0002",
                   "public_ip": "203.0.113.12"},
        "created_at_offset": 86400,
        "origin": "web",
        "cancel_requested": False,
    },
    {
        "id": "demo-bootswap-1",
        "kind": "bootswap",
        "status": "failed",
        "detail": "失败：备份状态不是 AVAILABLE。",
        "label": "引导卷恢复：DEMO-AMD-01",
        "logs": ["开始恢复引导卷", "校验备份状态", "失败：备份状态不是 AVAILABLE。"],
        "result": None,
        "created_at_offset": 172800,
        "origin": "web",
        "cancel_requested": False,
    },
    {
        "id": "demo-create-1",
        "kind": "create",
        "status": "cancelled",
        "detail": "任务已取消。",
        "label": "创建实例：DEMO-TEST-01",
        "logs": ["任务已取消。"],
        "result": None,
        "created_at_offset": 259200,
        "origin": "web",
        "cancel_requested": False,
    },
]


def is_demo():
    """当前是否演示模式（每次调用都读 web.config.DEMO_MODE，测试
    patch 生效；不要在模块顶层缓存）。"""
    return bool(config.DEMO_MODE)


def fake_success(verb, **extra):
    """演示模式写操作的假成功响应：不调 OCI、不改真实状态。

    verb 是中文动作描述（如“启动实例”）；extra 是各接口自己的
    返回字段（如 instance_id / task_id / action）。前端凭 demo: true
    识别演示操作，可显示“演示模式”提示。

    注意：第一个参数叫 verb 而不是 action，因为调用方常把 action
    当 extra 字段传（如 action="terminate"），同名会报
    "got multiple values for argument 'action'"。
    """
    result = {"ok": True, "demo": True,
              "message": f"演示模式：{verb}未真实执行"}
    result.update(extra)
    return result


def resolve_demo_profile(profile=None, region=None):
    """解析演示租户：返回 (profile 名, 区域, compartment OCID)。

    profile 不传时用默认演示租户；region 不传时用该租户的主区；
    未知租户抛 ValueError（与真实 resolve_profile 的行为一致）。
    """
    name = profile or DEFAULT_DEMO_PROFILE
    cfg = DEMO_PROFILES.get(name)
    if cfg is None:
        raise ValueError(
            f"未知演示租户：{name}"
            f"（演示租户：{', '.join(DEMO_PROFILES)}）")
    return name, region or cfg["region"], cfg["compartment_id"]


def demo_profile_list():
    """/api/profiles 的演示租户列表（与真实接口同一形状）。"""
    profiles = []
    for name, cfg in DEMO_PROFILES.items():
        profiles.append({
            "name": name,
            "region": cfg["region"],
            "regionLabel": cfg["region_label"],
            "regionCount": 1,
            "regions": [{"code": cfg["region"],
                         "label": cfg["region_label"],
                         "isHome": True}],
            "displayName": cfg["display_name"],
            "key_file": "",
            "key_ready": False,
        })
    return {"ok": True, "default": DEFAULT_DEMO_PROFILE,
            "profiles": profiles}


def _raw_instance(inst):
    """演示实例转 OCI 形状 dict（kebab-case 字段，供详情页用）。"""
    return {
        "id": inst["id"],
        "display-name": inst["display_name"],
        "shape": inst["shape"],
        "lifecycle-state": inst["state"],
        "shape-config": {"ocpus": inst["ocpus"],
                         "memory-in-gbs": inst["memory_gb"]},
        "time-created": inst["time_created"],
        "availability-domain": inst["availability_domain"],
        "region": DEMO_PROFILES[_profile_of(inst)]["region"],
        "compartment-id": _DEMO_TENANCY,
    }


def _profile_of(inst):
    """实例所属的演示租户（按实例 id 查表）。"""
    for name, instances in DEMO_INSTANCES.items():
        if any(item["id"] == inst["id"] for item in instances):
            return name
    return DEFAULT_DEMO_PROFILE


def _panel_instance(inst, region):
    """演示实例转面板形状（与 web/routes/instances.py 的
    format_instance 同一字段集）。"""
    return {
        "id": inst["id"],
        "name": inst["display_name"],
        "publicIp": inst["public_ip"],
        "shape": inst["shape"],
        "state": inst["state"],
        "ocpus": inst["ocpus"],
        "memoryInGBs": inst["memory_gb"],
        "region": region,
        "timeCreated": inst["time_created"],
        "cpuModel": inst["cpu_model"],
        "display-name": inst["display_name"],
        "lifecycle-state": inst["state"],
        "time-created": inst["time_created"],
        "public-ip": inst["public_ip"],
    }


def _find_instance(instance_id):
    """按 OCID 在全部演示租户里找实例；找不到返回 None。"""
    for instances in DEMO_INSTANCES.values():
        for inst in instances:
            if inst["id"] == instance_id:
                return inst
    return None


def demo_instances_payload(profile=None, region=None, show_all=False):
    """/api/instances 的演示响应：默认只含 RUNNING，state=all 时含
    全部非 TERMINATED 实例（与真实接口同一语义）。"""
    name, eff_region, _comp = resolve_demo_profile(profile, region)
    instances = []
    for inst in DEMO_INSTANCES[name]:
        if show_all:
            if inst["state"] == "TERMINATED":
                continue
        elif inst["state"] != "RUNNING":
            continue
        instances.append(_panel_instance(inst, eff_region))
    return {"ok": True, "profile": name, "region": eff_region,
            "instances": instances}


def demo_instance_detail(instance_id):
    """/api/instances/{id} 的演示响应；实例不存在返回 None。"""
    inst = _find_instance(instance_id)
    if inst is None:
        return None
    profile = _profile_of(inst)
    region = DEMO_PROFILES[profile]["region"]
    vol = inst["boot_volume"]
    return {
        "ok": True,
        "instance": _raw_instance(inst),
        "public_ip": inst["public_ip"],
        "cpu_model": inst["cpu_model"],
        "private_ip": inst["private_ip"],
        "ipv6_addresses": list(inst["ipv6"]),
        "boot_volume_size_gbs": vol["size_gb"],
        "boot_volume_id": vol["id"],
        "boot_volume_vpus_per_gb": vol["vpus"],
        "boot_volume_state": vol["state"],
        "boot_volume_attachment_state": "ATTACHED",
    }


def _panel_volume(volume, attachment=None, instance_name=None):
    """演示启动盘转面板形状（与 web/routes/volumes.py 的
    format_volume 同一字段集）。"""
    attached = bool(attachment)
    return {
        "volumeId": volume["id"],
        "attachmentId": (attachment or {}).get("id"),
        "displayName": volume["display_name"],
        "sizeInGBs": volume["size_gb"],
        "vpusPerGB": volume["vpus"],
        "lifecycleState": volume["state"],
        "instanceId": (attachment or {}).get("instance_id"),
        "instanceName": instance_name,
        "attached": attached,
        "attachmentState": (attachment or {}).get("state"),
        "availabilityDomain": volume["availability_domain"],
        "timeCreated": volume["time_created"],
        "id": volume["id"],
        "display-name": volume["display_name"],
        "size-in-gbs": volume["size_gb"],
        "vpus-per-gb": volume["vpus"],
        "lifecycle-state": volume["state"],
        "availability-domain": volume["availability_domain"],
        "time-created": volume["time_created"],
    }


def _all_volumes(profile):
    """某演示租户的全部启动盘（含挂载关系）：每台实例一块启动盘，
    外加该租户的孤儿盘。返回 [(volume dict, attachment|None,
    instance_name|None)]。"""
    out = []
    for inst in DEMO_INSTANCES.get(profile, []):
        vol = inst["boot_volume"]
        out.append(({
            "id": vol["id"],
            "display_name": f"{inst['display_name']} (Boot Volume)",
            "size_gb": vol["size_gb"],
            "vpus": vol["vpus"],
            "state": vol["state"],
            "availability_domain": inst["availability_domain"],
            "time_created": inst["time_created"],
        }, {"id": f"ocid1.bootvolumeattachment.oc1..demoatt{inst['id'][-4:]}",
            "instance_id": inst["id"], "state": "ATTACHED"},
            inst["display_name"]))
    for orphan in DEMO_ORPHAN_VOLUMES.get(profile, []):
        out.append((orphan, None, None))
    return out


def demo_volumes_payload(profile=None, region=None):
    """/api/volumes 的演示响应。"""
    name, eff_region, _comp = resolve_demo_profile(profile, region)
    volumes = [_panel_volume(vol, att, inst_name)
               for vol, att, inst_name in _all_volumes(name)]
    return {"ok": True, "profile": name, "region": eff_region,
            "volumes": volumes}


def demo_orphans_payload(profile=None, region=None):
    """/api/volumes/orphans 的演示响应（AVAILABLE 且未挂载的盘）。"""
    name, eff_region, _comp = resolve_demo_profile(profile, region)
    volumes = [_panel_volume(vol, att, inst_name)
               for vol, att, inst_name in _all_volumes(name)
               if vol["state"] == "AVAILABLE" and att is None]
    return {"ok": True, "profile": name, "region": eff_region,
            "volumes": volumes}


def demo_volume_detail(volume_id):
    """/api/volumes/{id} 的演示响应（volume 是 OCI 原始形状，
    与真实 get_boot_volume 的返回一致）；不存在返回 None。"""
    for profile in DEMO_PROFILES:
        for vol, _att, _name in _all_volumes(profile):
            if vol["id"] == volume_id:
                return {"ok": True, "volume": {
                    "id": vol["id"],
                    "display-name": vol["display_name"],
                    "size-in-gbs": vol["size_gb"],
                    "vpus-per-gb": vol["vpus"],
                    "lifecycle-state": vol["state"],
                    "availability-domain": vol["availability_domain"],
                    "time-created": vol["time_created"],
                }}
    return None


def _demo_usage(profile):
    """按模拟数据算 A1 / E2 / 启动盘用量（与真实 oci/quota.py
    同一统计口径：A1 与 E2 只计 RUNNING，启动盘排除已删除的盘）。"""
    instances = DEMO_INSTANCES.get(profile, [])
    a1_ocpu = 0
    a1_mem = 0
    a1_list = []
    e2_list = []
    for inst in instances:
        if inst["state"] != "RUNNING":
            continue
        if inst["shape"] == "VM.Standard.A1.Flex":
            a1_ocpu += inst["ocpus"]
            a1_mem += inst["memory_gb"]
            a1_list.append({"id": inst["id"],
                            "display_name": inst["display_name"],
                            "ocpus": inst["ocpus"],
                            "memory_gb": inst["memory_gb"]})
        elif inst["shape"] == "VM.Standard.E2.1.Micro":
            e2_list.append({"id": inst["id"],
                            "display_name": inst["display_name"]})
    boot_used = 0
    boot_list = []
    for vol, _att, _name in _all_volumes(profile):
        if vol["state"] in ("TERMINATING", "TERMINATED"):
            continue
        boot_used += vol["size_gb"]
        boot_list.append({"id": vol["id"],
                          "display_name": vol["display_name"],
                          "size_gb": vol["size_gb"]})
    return {
        "a1": {"ocpu_used": a1_ocpu, "ocpu_limit": A1_OCPU_LIMIT,
               "memory_used_gb": a1_mem,
               "memory_limit_gb": A1_MEMORY_LIMIT_GB,
               "instances": a1_list},
        "e2": {"count": len(e2_list), "limit": E2_LIMIT,
               "instances": e2_list},
        "boot_volume": {"used_gb": boot_used,
                        "limit_gb": BOOT_VOLUME_LIMIT_GB,
                        "volumes": boot_list},
    }


def demo_quota_summary(profile=None, region=None):
    """/api/quota 的演示响应（与真实 get_free_tier_summary 同一形状）。"""
    name, _region, _comp = resolve_demo_profile(profile, region)
    usage = _demo_usage(name)
    a1 = usage["a1"]
    e2 = usage["e2"]
    boot = usage["boot_volume"]
    return {
        "ok": True,
        "profile": name,
        "profileLabel": DEMO_PROFILES[name]["display_name"],
        "summary": {
            "a1": {
                "ocpu_used": a1["ocpu_used"],
                "ocpu_limit": a1["ocpu_limit"],
                "ocpu_remaining": a1["ocpu_limit"] - a1["ocpu_used"],
                "memory_used_gb": a1["memory_used_gb"],
                "memory_limit_gb": a1["memory_limit_gb"],
                "memory_remaining_gb":
                    a1["memory_limit_gb"] - a1["memory_used_gb"],
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
            "overall_ok": (
                a1["ocpu_used"] <= a1["ocpu_limit"]
                and a1["memory_used_gb"] <= a1["memory_limit_gb"]
                and e2["count"] <= e2["limit"]
                and boot["used_gb"] <= boot["limit_gb"]),
        },
    }


def demo_a1_quota(profile=None, region=None):
    """/api/quota/a1 的演示响应（与真实 format_a1_quota 同一形状）。"""
    name, _region, _comp = resolve_demo_profile(profile, region)
    a1 = _demo_usage(name)["a1"]
    over_ocpu = max(0, a1["ocpu_used"] - a1["ocpu_limit"])
    over_mem = max(0, a1["memory_used_gb"] - a1["memory_limit_gb"])
    return {
        "ok": True,
        "profile": name,
        "a1": {
            "capOcpu": a1["ocpu_limit"],
            "capMem": a1["memory_limit_gb"],
            "usedOcpu": a1["ocpu_used"],
            "usedMem": a1["memory_used_gb"],
            "over": over_ocpu > 0 or over_mem > 0,
            "overOcpu": over_ocpu,
            "overMem": over_mem,
            "instances": [{
                "id": inst["id"],
                "name": inst["display_name"],
                "ocpus": inst["ocpus"],
                "memoryInGBs": inst["memory_gb"],
                "state": "RUNNING",
            } for inst in a1["instances"]],
        },
    }


def demo_a1_health(profile=None, region=None):
    """/api/quota/a1-health 的演示响应（与真实 a1_health_check
    同一套建议规则：超限 error、用满 warning、规格偏高 info、
    全部正常时唯一一条 ok）。"""
    name, _region, _comp = resolve_demo_profile(profile, region)
    summary = demo_quota_summary(name)["summary"]
    suggestions = []
    a1 = summary["a1"]
    boot = summary["boot_volume"]
    if a1["ocpu_used"] > a1["ocpu_limit"]:
        suggestions.append({
            "severity": "error",
            "message": (f"A1 OCPU 超免费额度：已用 {a1['ocpu_used']} 核，"
                        f"上限 {a1['ocpu_limit']} 核，"
                        "超出的部分会按量计费，请降配或释放实例。"),
        })
    elif a1["ocpu_used"] == a1["ocpu_limit"]:
        suggestions.append({
            "severity": "warning",
            "message": (f"A1 OCPU 已用满（{a1['ocpu_used']}/"
                        f"{a1['ocpu_limit']} 核），无法再开 A1 实例。"),
        })
    if a1["memory_used_gb"] > a1["memory_limit_gb"]:
        suggestions.append({
            "severity": "error",
            "message": (f"A1 内存超免费额度：已用 {a1['memory_used_gb']}GB，"
                        f"上限 {a1['memory_limit_gb']}GB，"
                        "超出的部分会按量计费，请降配或释放实例。"),
        })
    elif a1["memory_used_gb"] == a1["memory_limit_gb"]:
        suggestions.append({
            "severity": "warning",
            "message": (f"A1 内存已用满（{a1['memory_used_gb']}/"
                        f"{a1['memory_limit_gb']}GB），无法再开 A1 实例。"),
        })
    if boot["used_gb"] > boot["limit_gb"]:
        suggestions.append({
            "severity": "error",
            "message": (f"启动盘超免费额度：已用 {boot['used_gb']}GB，"
                        f"上限 {boot['limit_gb']}GB，"
                        "超出的部分会按量计费，请删除不用的启动盘。"),
        })
    for inst in a1["instances"]:
        if inst["ocpus"] > 2 or inst["memory_gb"] > 12:
            suggestions.append({
                "severity": "info",
                "message": (f"实例 {inst['display_name']} 规格"
                            f"（{inst['ocpus']} 核 / {inst['memory_gb']}GB）"
                            "高于常用需求（2 核 / 12GB），可考虑降配。"),
            })
    if not suggestions:
        suggestions.append({"severity": "ok",
                            "message": "A1 配置健康，未发现问题。"})
    overall_ok = not any(s["severity"] == "error"
                         for s in suggestions)
    return {"ok": True, "profile": name,
            "health": {"overall_ok": overall_ok,
                       "suggestions": suggestions}}


def demo_can_launch(profile=None, region=None, shape="",
                    ocpus=0, memory_gb=0):
    """/api/quota/can-launch 的演示判断（与真实 oci/quota.py 的
    can_launch 同一规则）：E2 数量 < 2 且启动盘余量 >= 47GB；
    A1 余量够且启动盘余量 >= 47GB。返回 (bool, reason)。"""
    name, _region, _comp = resolve_demo_profile(profile, region)
    summary = demo_quota_summary(name)["summary"]
    boot_remaining = summary["boot_volume"]["remaining_gb"]
    short = (shape or "").replace("VM.Standard.", "")
    if short == "E2.1.Micro":
        e2 = summary["e2"]
        if e2["count"] >= e2["limit"]:
            return False, f"E2 已满（{e2['count']}/{e2['limit']}）"
        if boot_remaining < BOOT_VOLUME_DEFAULT_GB:
            return False, (f"启动盘余量不足：还剩 {boot_remaining}GB，"
                           f"需要 {BOOT_VOLUME_DEFAULT_GB}GB")
        return True, "可以开机"
    if short == "A1.Flex":
        a1 = summary["a1"]
        if a1["ocpu_remaining"] < ocpus:
            return False, (f"A1 OCPU 余量不足：还剩 {a1['ocpu_remaining']} 核，"
                           f"需要 {ocpus} 核")
        if a1["memory_remaining_gb"] < memory_gb:
            return False, (f"A1 内存余量不足：还剩 {a1['memory_remaining_gb']}GB，"
                           f"需要 {memory_gb}GB")
        if boot_remaining < BOOT_VOLUME_DEFAULT_GB:
            return False, (f"启动盘余量不足：还剩 {boot_remaining}GB，"
                           f"需要 {BOOT_VOLUME_DEFAULT_GB}GB")
        return True, "可以开机"
    return False, f"不支持的规格：{shape}"


def _panel_backup(backup):
    """演示备份转面板形状（与 web/routes/boot_backups.py 的
    format_backup 同一字段集）。"""
    return {
        "backupId": backup["id"],
        "displayName": backup["display_name"],
        "bootVolumeId": backup["boot_volume_id"],
        "sizeInGBs": backup["size_gb"],
        "lifecycleState": backup["state"],
        "sourceType": backup["source_type"],
        "type": backup["type"],
        "timeCreated": backup["time_created"],
        "imageId": backup["image_id"],
        "id": backup["id"],
        "display-name": backup["display_name"],
        "boot-volume-id": backup["boot_volume_id"],
        "size-in-gbs": backup["size_gb"],
        "lifecycle-state": backup["state"],
        "source-type": backup["source_type"],
        "time-created": backup["time_created"],
        "image-id": backup["image_id"],
    }


def demo_backups_payload(profile=None, region=None, boot_volume_id=None):
    """/api/boot-backups 的演示响应；传 boot_volume_id 时只返回该盘的备份。"""
    name, _region, _comp = resolve_demo_profile(profile, region)
    backups = DEMO_BACKUPS.get(name, [])
    if boot_volume_id:
        backups = [b for b in backups
                   if b["boot_volume_id"] == boot_volume_id]
    return {"ok": True, "profile": name,
            "backups": [_panel_backup(b) for b in backups]}


def demo_backup_quota(profile=None, region=None):
    """/api/boot-backups/quota 的演示响应（免费账号，上限 5 个）。"""
    name, _region, _comp = resolve_demo_profile(profile, region)
    backups = DEMO_BACKUPS.get(name, [])
    count = len(backups)
    full = count >= BACKUP_LIMIT
    oldest = None
    if backups:
        first = backups[0]
        oldest = {"id": first["id"], "name": first["display_name"],
                  "time_created": first["time_created"]}
    return {"ok": True, "profile": name, "paid": False,
            "count": count, "limit": BACKUP_LIMIT, "full": full,
            "oldest": oldest}


def demo_backup(backup_id):
    """按 id 找演示备份；找不到返回 None。"""
    for backups in DEMO_BACKUPS.values():
        for backup in backups:
            if backup["id"] == backup_id:
                return backup
    return None


def demo_tasks():
    """/api/tasks 的演示任务列表（公开视图，与真实任务记录同一形状）。"""
    now = time.time()
    tasks = []
    for task in DEMO_TASKS:
        view = dict(task)
        view["created_at"] = now - task["created_at_offset"]
        del view["created_at_offset"]
        view["logs"] = list(task["logs"])
        tasks.append(view)
    return tasks


def demo_task(task_id):
    """/api/tasks/{id} 的演示任务；不存在返回 None。"""
    for task in demo_tasks():
        if task["id"] == task_id:
            return task
    return None


def demo_cloud_report():
    """/api/cloud-report/summary 的演示响应：各演示租户的实例 /
    磁盘 / 配额汇总（与真实 summarize_profile 同一形状）。"""
    groups = []
    for name, cfg in DEMO_PROFILES.items():
        instances = DEMO_INSTANCES.get(name, [])
        by_state = {}
        for inst in instances:
            by_state[inst["state"]] = by_state.get(inst["state"], 0) + 1
        volumes = _all_volumes(name)
        live = [v for v, _att, _n in volumes
                if v["state"] not in ("TERMINATING", "TERMINATED")]
        total_gb = sum(v["size_gb"] for v in live)
        usage = _demo_usage(name)
        groups.append({
            "profile": name,
            "profileLabel": cfg["display_name"],
            "region": cfg["region"],
            "instances": {"total": len(instances), "byState": by_state},
            "volumes": {"count": len(live), "totalGB": total_gb},
            "quota": {
                "a1": {
                    "usedOcpu": usage["a1"]["ocpu_used"],
                    "capOcpu": usage["a1"]["ocpu_limit"],
                    "usedMem": usage["a1"]["memory_used_gb"],
                    "capMem": usage["a1"]["memory_limit_gb"],
                },
                "e2": {"count": usage["e2"]["count"],
                       "limit": usage["e2"]["limit"]},
            },
        })
    return {"groups": groups, "generatedAtEpochSec": int(time.time())}


def demo_create_defaults(profile=None, region=None):
    """/api/create/defaults 的演示响应：可用域 / 子网 / SSH 公钥文件
    均为演示占位（子网与密钥留空，前端让用户手填）。"""
    name, eff_region, _comp = resolve_demo_profile(profile, region)
    return {
        "ok": True,
        "profile": name,
        "region": eff_region,
        "availability_domain": f"DEMO:{eff_region.upper()}-AD-1",
        "subnet_id": "",
        "ssh_keys_file": "",
        "ssh_keys_file_exists": False,
    }
