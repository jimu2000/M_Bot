#!/usr/bin/env python3
"""网络模块：VNIC、公网 IP、内网 IP 管理（“换 IP”等功能）。

全部函数接受一个 OCIClient 对象作为第一个参数。
读接口走 OCI Python SDK 直调（cli.sdk_*，进程内 HTTP，不起 CLI 子进程）；
写操作（create / assign / delete / change）仍走 client.run()（OCI CLI）。

安全说明（重要）：
  - 本模块不读取、不打印、不外传 API 私钥。认证全部由 OCI CLI 自己读
    配置文件（~/oci_config，旧部署回退 ~/.oci/config）与 key_file
    完成，私钥只在本地用于请求签名。
  - 配置区在文件顶端，真实 OCID / IP 一律用占位符，绝不写进代码。
  - 写操作（create / assign / delete / change）会改动真实资源：换 IP 会
    断掉正在跑的服务，删 IP 会让实例失联。不要对生产实例随手执行，
    本项目的单元测试一律用 mock，不碰真实 API。

用法示例（占位符）：
    from oci.client import OCIClient
    from oci.network import get_public_ip, list_vnics
    cli = OCIClient(profile="DEFAULT", region="ap-singapore-1")
    ip = get_public_ip(cli, "ocid1.instance.oc1..xxxxxxxx")
"""

from concurrent.futures import ThreadPoolExecutor
import time

# ================= 配置区（改这里） =================
# Default display name when change_public_ip() creates a reserved IP. change_public_ip() 创建保留 IP 时的默认显示名。
DEFAULT_CHANGED_IP_NAME = "changed-ip"
# lifetime when creating a reserved IP: RESERVED (a reserved IP survives instance termination); EPHEMERAL is an ephemeral IP that is released with the instance — do not use it for IP changes. 创建保留 IP 时的 lifetime：RESERVED（保留 IP，实例释放后仍保留）；EPHEMERAL 是临时 IP，随实例释放而释放，换 IP 场景不要用。
RESERVED_LIFETIME = "RESERVED"
# wait_for_public_ip() waits for a just-launched instance to get a public IP: the VNIC may have no IP yet right after creation, so check every 3s for up to 60s (waiting only 9s after a grab often missed the IP, and the notice would say "获取中" and never be re-sent). wait_for_public_ip() 等刚开出的实例分配公网 IP：VNIC 刚建好时可能还没 IP，3 秒查一次、最多等 60 秒（实测：抢机后只等 9 秒经常拿不到 IP，通知里写“获取中”后就再也不补发）。
BOOT_IP_WAIT_TIMEOUT_SECONDS = 60
BOOT_IP_WAIT_INTERVAL_SECONDS = 5
# =====================================================


def _data(result):
    """从 cli.run() 的返回值里取出 data 字段。

    OCI CLI 的 list/get 命令返回 {"data": ...}；少数命令直接返回列表或
    字典，做一层兼容。
    """
    if isinstance(result, dict) and "data" in result:
        return result["data"]
    return result


def list_vnics(cli, instance_id, compartment_id=None):
    """列实例的 VNIC（SDK 直调，复现 CLI list-vnics 的组合）。

    返回精简列表，每项含：vnic id、display-name、public-ip、private-ip、
    is-primary。compartment_id 传了可省一次查实例详情的调用。
    """
    vnics = cli.sdk_list_vnics(instance_id,
                               compartment_id=compartment_id) or []
    return [{
        "id": v.get("id"),
        "display-name": v.get("display-name"),
        "public-ip": v.get("public-ip"),
        "private-ip": v.get("private-ip"),
        "is-primary": v.get("is-primary"),
    } for v in vnics]


def get_public_ip(cli, instance_id, compartment_id=None):
    """返回实例主 VNIC 的公网 IPv4，没有则返回 None。"""
    for vnic in list_vnics(cli, instance_id,
                           compartment_id=compartment_id):
        if vnic.get("is-primary"):
            return vnic.get("public-ip")
    return None


def wait_for_public_ip(cli, instance_id, timeout_seconds=None,
                       interval_seconds=None):
    """轮询等一台实例分配公网 IPv4（刚开出时 VNIC 可能还没 IP）。

    查到就返回 IP；超时返回 None。单次查询异常按无 IP 处理继续等，
    绝不抛异常（调用方是通知流程，不能让查 IP 拖垮开机通知）。
    timeout_seconds / interval_seconds 不传时用配置区默认值。
    """
    timeout = (BOOT_IP_WAIT_TIMEOUT_SECONDS if timeout_seconds is None
               else timeout_seconds)
    interval = (BOOT_IP_WAIT_INTERVAL_SECONDS if interval_seconds is None
                else interval_seconds)
    deadline = time.monotonic() + timeout
    while True:
        try:
            ip = get_public_ip(cli, instance_id)
        except Exception:  # noqa: BLE001 - 查询失败按无 IP 处理继续等
            ip = None
        if ip:
            return ip
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)


def attach_public_ips(cli, instances, max_workers=5):
    """并行给实例精简列表补 public-ip 字段（原地修改并返回同一列表）。

    每台查主 VNIC 的公网 IPv4（get_public_ip），写入
    inst["public-ip"]。TERMINATED / TERMINATING 的实例跳过不查
    （已无 VNIC，直接记 None）；单台查询失败（异常）或无公网 IP
    时该台记 None，不抛异常，不让一台实例拖垮整个列表。
    max_workers 控制并行度（默认 5；OCI CLI 调用是 subprocess，
    并行安全）。
    """
    todo = []
    for idx, inst in enumerate(instances):
        state = (inst.get("lifecycle-state") or "").upper()
        if state in ("TERMINATED", "TERMINATING") or not inst.get("id"):
            inst["public-ip"] = None
        else:
            todo.append(idx)
    if not todo:
        return instances

    def _fetch(idx):
        try:
            return get_public_ip(cli, instances[idx]["id"])
        except Exception:  # noqa: BLE001 - 单台失败记 None，不拖垮列表
            return None

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        ips = list(pool.map(_fetch, todo))
    for idx, ip in zip(todo, ips):
        instances[idx]["public-ip"] = ip
    return instances


def attach_public_ips_bulk(cli, instances, compartment_id, max_workers=5):
    """批量给实例精简列表补 public-ip（返回与输入等长的 IP 列表）。

    与 attach_public_ips 的逐台查法不同：先一次 compartment 级
    list-vnic-attachments（不按实例过滤）拿到全部挂载关系，再按
    实例分发、每台只查自己 VNIC 的详情（get-vnic）。实例列表页
    从 1 + 2N 次 SDK 调用降到 2 + N 次（N 为实例数），冷缓存时
    少一次 TLS 握手 + 签名 + 解析；实例越多省得越多。

    语义与 get_public_ip 一致：返回主 VNIC（is-primary）的公网
    IPv4，没有主 VNIC 时返回 None。TERMINATED / TERMINATING 的
    实例不查（直接记 None）；单台查询失败（异常、VNIC 已删 404
    等）该台记 None，不拖垮整个列表。批量挂载列表本身失败时回
    退到逐台 get_public_ip（与旧路径完全一致的结果），绝不因
    优化本身让列表页报错。
    max_workers 控制并行度（默认 5）。
    """
    todo = []
    results = [None] * len(instances)
    for idx, inst in enumerate(instances):
        state = (inst.get("lifecycle-state") or "").upper()
        if state in ("TERMINATED", "TERMINATING") or not inst.get("id"):
            results[idx] = None
        else:
            todo.append(idx)
    if not todo:
        return results

    def _fetch_one(idx):
        try:
            return get_public_ip(cli, instances[idx]["id"], compartment_id)
        except Exception:  # noqa: BLE001 - 单台失败记 None，不拖垮列表
            return None

    try:
        attachments = cli.sdk_list_vnic_attachments(compartment_id) or []
    except Exception:  # noqa: BLE001 - 批量列表失败，回退逐台查
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            ips = list(pool.map(_fetch_one, todo))
        for idx, ip in zip(todo, ips):
            results[idx] = ip
        return results

    by_instance = {}
    for att in attachments:
        iid = att.get("instance-id")
        vnic_id = att.get("vnic-id")
        if iid and vnic_id:
            by_instance.setdefault(iid, []).append(vnic_id)

    def _fetch_bulk(idx):
        try:
            vnic_ids = by_instance.get(instances[idx]["id"]) or []
            for vnic_id in vnic_ids:
                vnic = cli.sdk_get_vnic(vnic_id) or {}
                if vnic.get("is-primary"):
                    return vnic.get("public-ip")
            return None
        except Exception:  # noqa: BLE001 - 单台失败记 None，不拖垮列表
            return None

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        ips = list(pool.map(_fetch_bulk, todo))
    for idx, ip in zip(todo, ips):
        results[idx] = ip
    return results


def get_vnic(cli, vnic_id):
    """查 VNIC 详情（SDK 直调）。

    返回完整的 VNIC 详情字典。VNIC 不存在时抛 NotFoundError
    （OCI 返回 NotAuthorizedOrNotFound，client 已分类）。
    """
    return cli.sdk_get_vnic(vnic_id)


def list_public_ips(cli, compartment_id, availability_domain=None):
    """列公网 IP（SDK 直调）。

    返回精简列表，每项含：id、ip-address、lifetime、lifecycle-state、
    assigned-entity（绑定的 VNIC / 私有 IP 的 OCID，没有则为 None）。

    注意：不传 availability_domain 时用 REGION 作用域，返回区域级公网 IP
    （保留 IP）。实例的临时公网 IP 是可用区级的，不会出现在 REGION 结果里；
    要列临时 IP 需传 availability_domain，此时用 AVAILABILITY_DOMAIN 作用域。
    """
    ips = cli.sdk_list_public_ips(
        compartment_id, availability_domain=availability_domain) or []
    return [{
        "id": ip.get("id"),
        "ip-address": ip.get("ip-address"),
        "lifetime": ip.get("lifetime"),
        "lifecycle-state": ip.get("lifecycle-state"),
        "assigned-entity": ip.get("assigned-entity-id"),
    } for ip in ips]


def create_reserved_ip(cli, compartment_id, display_name):
    """创建保留公网 IP（oci network public-ip create --lifetime RESERVED）。

    返回 (新 IP 的 OCID, IP 地址) 二元组。
    注意：保留 IP 是计费资源（免费额度外），不用了要及时删除。
    """
    result = cli.run(["network", "public-ip", "create",
                      "--compartment-id", compartment_id,
                      "--lifetime", RESERVED_LIFETIME,
                      "--display-name", display_name])
    data = _data(result) or {}
    return data.get("id"), data.get("ip-address")


def assign_public_ip(cli, public_ip_id, private_ip_id):
    """把保留 IP 绑到指定内网 IP 上（oci network public-ip update）。

    注意：该内网 IP 上不能已有公网 IP，否则 OCI 报 409 Conflict；
    换 IP 时必须先删旧的（见 change_public_ip）。
    返回更新后的公网 IP 详情字典。
    """
    result = cli.run(["network", "public-ip", "update",
                      "--public-ip-id", public_ip_id,
                      "--private-ip-id", private_ip_id])
    return _data(result)


def delete_public_ip(cli, public_ip_id):
    """删除公网 IP（oci network public-ip delete --force）。

    注意：删除已绑定的公网 IP 后实例立即失联，确认无误再执行。
    成功时 OCI CLI 无输出，本函数返回 None。
    """
    return cli.run(["network", "public-ip", "delete",
                    "--public-ip-id", public_ip_id, "--force"])


def get_primary_vnic(cli, instance_id, compartment_id=None):
    """返回实例的主 VNIC 字典，没有主 VNIC 时抛 ValueError。"""
    vnics = list_vnics(cli, instance_id, compartment_id=compartment_id)
    primary = next((v for v in vnics if v.get("is-primary")), None)
    if primary is None:
        raise ValueError(f"实例 {instance_id} 没有主 VNIC")
    return primary


def get_primary_private_ip(cli, vnic_id):
    """返回 VNIC 的主内网 IP 字典，没有时抛 ValueError。"""
    private_ips = list_private_ips(cli, vnic_id)
    primary = next((p for p in private_ips if p.get("is-primary")), None)
    if primary is None:
        raise ValueError(f"VNIC {vnic_id} 没有主内网 IP")
    return primary


def list_ipv6_for_vnic(cli, vnic_id):
    """查某 VNIC 上的 IPv6 列表（SDK 直调）。"""
    return cli.sdk_list_ipv6s(vnic_id) or []


def create_ipv6(cli, vnic_id, ip_address=None):
    """给 VNIC 分配一个 IPv6（network ipv6 create）。

    写操作：调用方应在用户确认后才调用。ip_address 可选，不传由 OCI 分配。
    返回创建后的 IPv6 详情字典。
    """
    args = ["network", "ipv6", "create", "--vnic-id", vnic_id]
    if ip_address:
        args += ["--ip-address", ip_address]
    return _data(cli.run(args))


def delete_ipv6(cli, ipv6_id):
    """删除（解绑）一个 IPv6（network ipv6 delete --force）。

    写操作：调用方应在用户确认后才调用。成功时 OCI CLI 无输出，返回 None。
    """
    return cli.run(["network", "ipv6", "delete",
                    "--ipv6-id", ipv6_id, "--force"])


def create_ephemeral_public_ip(cli, compartment_id, private_ip_id):
    """给指定内网 IP 创建并绑定一个临时公网 IP。

    一步完成创建 + 绑定（create 时直接传 --private-ip-id）。
    注意：该内网 IP 上不能已有公网 IP，否则 OCI 报 409 Conflict；
    调用前应先确认未绑定（见 repair_instance_ip）。
    返回 (新 IP 的 OCID, IP 地址) 二元组。
    """
    result = cli.run(["network", "public-ip", "create",
                      "--compartment-id", compartment_id,
                      "--lifetime", "EPHEMERAL",
                      "--private-ip-id", private_ip_id])
    data = _data(result) or {}
    return data.get("id"), data.get("ip-address")


def repair_instance_ip(cli, compartment_id, instance_id):
    """尝试修复实例的公网 IP（Web 面板“尝试修复”）。

    保守策略，只做检查 + 补缺，不做破坏性操作：
      1. 查实例状态（已终止的实例无法修复，抛 ValueError）；
      2. 查主 VNIC（没有主 VNIC 时抛 ValueError）；
      3. 主 VNIC 已有公网 IP（无论临时还是保留）即正常，不动任何资源；
      4. 缺失时给主内网 IP 创建并绑定一个临时公网 IP。

    返回修复报告字典：
    {instance_id, state, public_ip, repaired, new_ip, checks}，
    checks 是 [{name, status, detail}]，status 为 ok / fixed。
    """
    inst = cli.sdk_get_instance(instance_id) or {}
    state = inst.get("lifecycle-state") or ""
    if state == "TERMINATED":
        raise ValueError(f"实例 {instance_id} 已终止，无法修复")
    checks = [{"name": "实例状态", "status": "ok",
               "detail": state or "未知"}]
    vnic = get_primary_vnic(cli, instance_id, compartment_id=compartment_id)
    checks.append({"name": "主 VNIC", "status": "ok",
                   "detail": vnic.get("display-name") or vnic["id"]})
    current_ip = vnic.get("public-ip")
    if current_ip:
        checks.append({"name": "公网 IP", "status": "ok",
                       "detail": f"已绑定 {current_ip}，无需修复"})
        return {"instance_id": instance_id, "state": state,
                "public_ip": current_ip, "repaired": False,
                "new_ip": None, "checks": checks}
    checks.append({"name": "公网 IP", "status": "fixed",
                   "detail": "缺失，正在重新附加临时公网 IP…"})
    private_ip = get_primary_private_ip(cli, vnic["id"])
    _new_id, new_ip = create_ephemeral_public_ip(
        cli, compartment_id, private_ip["id"])
    checks[-1]["detail"] = f"缺失，已重新附加临时公网 IP {new_ip}"
    return {"instance_id": instance_id, "state": state,
            "public_ip": new_ip, "repaired": True,
            "new_ip": new_ip, "checks": checks}


def list_private_ips(cli, vnic_id):
    """列 VNIC 的内网 IP（SDK 直调）。

    返回精简列表，每项含：id、ip-address、is-primary、display-name。
    """
    ips = cli.sdk_list_private_ips(vnic_id) or []
    return [{
        "id": ip.get("id"),
        "ip-address": ip.get("ip-address"),
        "is-primary": ip.get("is-primary"),
        "display-name": ip.get("display-name"),
    } for ip in ips]


def change_public_ip(cli, compartment_id, instance_id,
                     display_name=DEFAULT_CHANGED_IP_NAME):
    """换 IP 的完整流程（“换 IP”功能）。

    步骤：
      1. 查实例主 VNIC 及其主内网 IP；
      2. 查该内网 IP 上已绑的临时公网 IP，有则先删除（实测 2026-11-23：
         OCI 拒绝给已绑公网 IP 的内网 IP 再绑一个新 IP，报 409 Conflict，
         必须先删旧的；旧版代码假设“绑定后旧 IP 自动释放”是错的）；
      3. 创建一个新的保留公网 IP；
      4. 把新 IP 绑到该内网 IP 上；
      5. 返回新 IP 地址。

    注意：
      - 换 IP 后旧 IP 立即释放，无法找回；指向旧 IP 的 DNS 记录
        （A 记录、反代、客户端配置等）必须同步更新，否则服务不可达。
      - 换 IP 瞬间会断开所有基于旧 IP 的连接（SSH、代理、网站访问）。
      - 保留 IP 本身是计费资源；若之后不再需要，应及时删除。
    """
    vnics = list_vnics(cli, instance_id, compartment_id=compartment_id)
    primary_vnic = next((v for v in vnics if v.get("is-primary")), None)
    if primary_vnic is None:
        raise ValueError(f"实例 {instance_id} 没有主 VNIC，无法换 IP")

    private_ips = list_private_ips(cli, primary_vnic["id"])
    primary_private = next((p for p in private_ips if p.get("is-primary")),
                           None)
    if primary_private is None:
        raise ValueError(f"VNIC {primary_vnic['id']} 没有主内网 IP，无法换 IP")

    # Delete the old public IP first (an ephemeral IP is availability-domain-scoped and must be listed with the AD scope). 先删已绑的旧公网 IP（临时 IP 是可用区级的，需用 AD 作用域列出）。
    inst = cli.sdk_get_instance(instance_id) or {}
    ad = inst.get("availability-domain")
    if ad:
        for pub in list_public_ips(cli, compartment_id,
                                   availability_domain=ad):
            if pub.get("assigned-entity") == primary_private["id"]:
                delete_public_ip(cli, pub["id"])

    new_ip_id, new_ip_address = create_reserved_ip(
        cli, compartment_id, display_name)
    assign_public_ip(cli, new_ip_id, primary_private["id"])
    return new_ip_address
