#!/usr/bin/env python3
"""网络模块：VNIC、公网 IP、内网 IP 管理（R-Bot 的“换 IP”等功能）。

全部函数接受一个 OCIClient 对象作为第一个参数，命令通过 client.run()
发给官方 OCI CLI。args 里不带 --profile / --region（client 自动追加）。

安全说明（重要）：
  - 本模块不读取、不打印、不外传 API 私钥。认证全部由 OCI CLI 自己读
    ~/.oci/config 与 key_file 完成，私钥只在本地用于请求签名。
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

# ================= 配置区（改这里） =================
# change_public_ip() 创建保留 IP 时的默认显示名。
DEFAULT_CHANGED_IP_NAME = "changed-ip"
# 创建保留 IP 时的 lifetime：RESERVED（保留 IP，实例释放后仍保留）；
# EPHEMERAL 是临时 IP，随实例释放而释放，换 IP 场景不要用。
RESERVED_LIFETIME = "RESERVED"
# =====================================================


def _data(result):
    """从 cli.run() 的返回值里取出 data 字段。

    OCI CLI 的 list/get 命令返回 {"data": ...}；少数命令直接返回列表或
    字典，做一层兼容。
    """
    if isinstance(result, dict) and "data" in result:
        return result["data"]
    return result


def list_vnics(cli, instance_id):
    """列实例的 VNIC（oci compute instance list-vnics）。

    返回精简列表，每项含：vnic id、display-name、public-ip、private-ip、
    is-primary。
    """
    result = cli.run(["compute", "instance", "list-vnics",
                      "--instance-id", instance_id])
    vnics = _data(result) or []
    return [{
        "id": v.get("id"),
        "display-name": v.get("display-name"),
        "public-ip": v.get("public-ip"),
        "private-ip": v.get("private-ip"),
        "is-primary": v.get("is-primary"),
    } for v in vnics]


def get_public_ip(cli, instance_id):
    """返回实例主 VNIC 的公网 IPv4，没有则返回 None。"""
    for vnic in list_vnics(cli, instance_id):
        if vnic.get("is-primary"):
            return vnic.get("public-ip")
    return None


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


def get_vnic(cli, vnic_id):
    """查 VNIC 详情（oci network vnic get）。

    返回完整的 VNIC 详情字典。VNIC 不存在时抛 NotFoundError
    （OCI 返回 NotAuthorizedOrNotFound，client 已分类）。
    """
    result = cli.run(["network", "vnic", "get", "--vnic-id", vnic_id])
    return _data(result)


def list_public_ips(cli, compartment_id, availability_domain=None):
    """列公网 IP（oci network public-ip list）。

    返回精简列表，每项含：id、ip-address、lifetime、lifecycle-state、
    assigned-entity（绑定的 VNIC / 私有 IP 的 OCID，没有则为 None）。

    注意：list 命令必须带 --scope；不传 availability_domain 时用 REGION
    作用域，返回区域级公网 IP（保留 IP）。实例的临时公网 IP 是可用区
    级的，不会出现在 REGION 结果里；要列临时 IP 需传 availability_domain，
    此时用 --scope AVAILABILITY_DOMAIN。
    实测：不带 --scope 会报错 "Missing option(s) --scope"（2026-11-23）。
    """
    args = ["network", "public-ip", "list",
            "--compartment-id", compartment_id]
    if availability_domain:
        args += ["--scope", "AVAILABILITY_DOMAIN",
                 "--availability-domain", availability_domain]
    else:
        args += ["--scope", "REGION"]
    result = cli.run(args)
    ips = _data(result) or []
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


def get_primary_vnic(cli, instance_id):
    """返回实例的主 VNIC 字典，没有主 VNIC 时抛 ValueError。"""
    vnics = list_vnics(cli, instance_id)
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
    """查某 VNIC 上的 IPv6 列表（network ipv6 list --vnic-id）。"""
    result = cli.run(["network", "ipv6", "list", "--vnic-id", vnic_id])
    return _data(result) or []


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
    inst = _data(cli.run(["compute", "instance", "get",
                          "--instance-id", instance_id])) or {}
    state = inst.get("lifecycle-state") or ""
    if state == "TERMINATED":
        raise ValueError(f"实例 {instance_id} 已终止，无法修复")
    checks = [{"name": "实例状态", "status": "ok",
               "detail": state or "未知"}]
    vnic = get_primary_vnic(cli, instance_id)
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
    """列 VNIC 的内网 IP（oci network private-ip list --vnic-id）。

    返回精简列表，每项含：id、ip-address、is-primary、display-name。
    """
    result = cli.run(["network", "private-ip", "list",
                      "--vnic-id", vnic_id])
    ips = _data(result) or []
    return [{
        "id": ip.get("id"),
        "ip-address": ip.get("ip-address"),
        "is-primary": ip.get("is-primary"),
        "display-name": ip.get("display-name"),
    } for ip in ips]


def change_public_ip(cli, compartment_id, instance_id,
                     display_name=DEFAULT_CHANGED_IP_NAME):
    """换 IP 的完整流程（R-Bot 的“换 IP”功能）。

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
    vnics = list_vnics(cli, instance_id)
    primary_vnic = next((v for v in vnics if v.get("is-primary")), None)
    if primary_vnic is None:
        raise ValueError(f"实例 {instance_id} 没有主 VNIC，无法换 IP")

    private_ips = list_private_ips(cli, primary_vnic["id"])
    primary_private = next((p for p in private_ips if p.get("is-primary")),
                           None)
    if primary_private is None:
        raise ValueError(f"VNIC {primary_vnic['id']} 没有主内网 IP，无法换 IP")

    # 先删已绑的旧公网 IP（临时 IP 是可用区级的，需用 AD 作用域列出）。
    inst = _data(cli.run(["compute", "instance", "get",
                          "--instance-id", instance_id])) or {}
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
