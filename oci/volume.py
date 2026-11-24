#!/usr/bin/env python3
"""磁盘模块：启动盘（Boot Volume）与块存储卷（Block Volume）管理。

原创实现，参考 OCI CLI 公开命令（oci bv ...）编写。
所有函数都接受一个 oci.client.OCIClient 对象作为第一个参数。

安全说明（重要）：
  - 本模块不读取、不打印、不外传 API 私钥。认证全部由 OCI CLI 自己读
    配置文件（~/oci_config，旧部署回退 ~/.oci/config）与 key_file
    完成，私钥只在本地用于请求签名。
  - 配置区在文件顶端，真实 OCID / fingerprint 一律用占位符，绝不写进代码。
  - 写操作（resize_boot_volume / delete_boot_volume）只在用户明确要求时调用；
    本模块不做任何自动删除或扩容。

用法示例（占位符）：
    from oci.client import OCIClient
    from oci.volume import list_boot_volumes, total_boot_volume_gb
    cli = OCIClient(profile="DEFAULT", region="ap-singapore-1")
    volumes = list_boot_volumes(cli, "ocid1.tenancy.oc1..xxxxxxxx")
    print(total_boot_volume_gb(cli, "ocid1.tenancy.oc1..xxxxxxxx"))
"""

# ================= 配置区（改这里） =================
# 默认 compartment OCID（占位符）。根 compartment 就是 tenancy OCID。
DEFAULT_COMPARTMENT_ID = "ocid1.tenancy.oc1..xxxxxxxx"
# 默认区域（如 ap-singapore-1 / us-sanjose-1 / us-chicago-1）。
DEFAULT_REGION = "ap-singapore-1"
# 免费额度：Always Free 账号的块存储卷 + 启动盘总容量上限（GB）。
# total_boot_volume_gb() 的返回值配合 quota 模块用，不要超过这个数。
FREE_BOOT_VOLUME_GB = 200
# 启动盘 VPU（每 GB 的性能单位）的有效值：10=默认（平衡）、
# 20=高性能、30=超高性能（OCI 只认这三档）。
VALID_VPUS_PER_GB = (10, 20, 30)
# =====================================================


def _extract_data(result):
    """从 cli.run() 的返回值里取出数据列表/字典。

    list 类命令返回 {"data": [...]}，get 类命令返回 {"data": {...}}；
    兼容直接返回 list / dict 的情况。
    """
    if isinstance(result, dict) and "data" in result:
        return result["data"]
    return result


def _simplify_boot_volume(item):
    """把启动盘原始 JSON 精简为常用字段。

    availability-domain 也保留：查挂载关系时
    `compute boot-volume-attachment list` 必须按 AD 查询。
    """
    simplified = {
        "id": item.get("id"),
        "display-name": item.get("display-name"),
        "size-in-gbs": item.get("size-in-gbs"),
        "lifecycle-state": item.get("lifecycle-state"),
        "availability-domain": item.get("availability-domain"),
        # 创建时间（Web 磁盘列表的 timeCreated 列用；API 未返回时为 None）。
        "time-created": item.get("time-created"),
    }
    # 启动盘资源本身不带 instance-id（实测：2026-11-23），但如果 API
    # 将来返回该字段，这里会一并保留，供孤儿盘判断做兜底。
    if item.get("instance-id"):
        simplified["instance-id"] = item["instance-id"]
    # VPU（每 GB 的性能单位）：API 返回时保留，供"改 VPU"显示当前值。
    if item.get("vpus-per-gb") is not None:
        simplified["vpus-per-gb"] = item["vpus-per-gb"]
    return simplified


def _simplify_volume(item):
    """把块存储卷原始 JSON 精简为常用字段。"""
    return {
        "id": item.get("id"),
        "display-name": item.get("display-name"),
        "size-in-gbs": item.get("size-in-gbs"),
        "lifecycle-state": item.get("lifecycle-state"),
    }


def list_boot_volumes(cli, compartment_id):
    """列出指定 compartment 下的所有启动盘。

    对应命令：oci bv boot-volume list --compartment-id <OCID>

    返回精简列表，每项含 id、display-name、size-in-gbs、lifecycle-state、
    availability-domain，以及 instance-id（仅当 API 返回该字段时才有）。
    """
    result = cli.run(["bv", "boot-volume", "list",
                      "--compartment-id", compartment_id])
    items = _extract_data(result) or []
    return [_simplify_boot_volume(item) for item in items]


def get_boot_volume(cli, boot_volume_id):
    """查单个启动盘的详情。

    对应命令：oci bv boot-volume get --boot-volume-id <OCID>

    返回启动盘的完整详情字典。OCID 不存在时抛 NotFoundError。
    """
    result = cli.run(["bv", "boot-volume", "get",
                      "--boot-volume-id", boot_volume_id])
    return _extract_data(result)


def list_volumes(cli, compartment_id):
    """列出指定 compartment 下的所有块存储卷。

    对应命令：oci bv volume list --compartment-id <OCID>

    返回精简列表，每项含 id、display-name、size-in-gbs、lifecycle-state。
    """
    result = cli.run(["bv", "volume", "list",
                      "--compartment-id", compartment_id])
    items = _extract_data(result) or []
    return [_simplify_volume(item) for item in items]


def resize_boot_volume(cli, boot_volume_id, size_in_gbs):
    """启动盘扩容。

    对应命令：oci bv boot-volume update --boot-volume-id <OCID>
              --size-in-gbs <GB>

    注意：
      - 只能扩不能缩：新容量必须大于当前容量，否则 OCI 会报错。
      - 扩完只改了云盘大小，系统里还要跑 resize2fs（ext4）或
        相应的文件系统扩容命令才生效，否则 df 看到的还是旧容量。
      - 扩容是写操作，调用前请确认目标盘与容量无误。

    返回更新后的启动盘详情字典。
    """
    size = int(size_in_gbs)
    if size <= 0:
        raise ValueError(f"size_in_gbs 必须为正整数，收到 {size_in_gbs!r}")
    result = cli.run(["bv", "boot-volume", "update",
                      "--boot-volume-id", boot_volume_id,
                      "--size-in-gbs", str(size),
                      # --force 必加：不加时 OCI CLI 会交互确认，
                      # subprocess 无 stdin 应答会一直挂到超时
                      # （同类问题见实例升降配，2026-11-23 真实验证）。
                      "--force"])
    return _extract_data(result)


def update_boot_volume_vpus(cli, boot_volume_id, vpus_per_gb):
    """修改启动盘 VPU（每 GB 的性能单位）。

    对应命令：oci bv boot-volume update --boot-volume-id <OCID>
              --vpus-per-gb <N> --force

    有效值只有 10 / 20 / 30（见 VALID_VPUS_PER_GB）：10 是默认
    （平衡性能），20 高性能，30 超高性能。其他值抛 ValueError。
    --force 必加：不加时 OCI CLI 会交互确认，subprocess 无 stdin
    应答会一直挂到超时（同 resize_boot_volume）。

    注意：改 VPU 不影响盘上数据，只改性能档位；高档位可能产生
    费用（超出免费层时），调用前请确认。
    返回更新后的启动盘详情字典。
    """
    try:
        vpus = int(vpus_per_gb)
    except (TypeError, ValueError):
        raise ValueError(f"vpus_per_gb 必须是整数，收到 {vpus_per_gb!r}")
    if vpus not in VALID_VPUS_PER_GB:
        raise ValueError(
            f"vpus_per_gb 必须是 {VALID_VPUS_PER_GB} 之一，收到 {vpus}")
    result = cli.run(["bv", "boot-volume", "update",
                      "--boot-volume-id", boot_volume_id,
                      "--vpus-per-gb", str(vpus),
                      "--force"])
    return _extract_data(result)


def delete_boot_volume(cli, boot_volume_id):
    """删除启动盘。

    对应命令：oci bv boot-volume delete --boot-volume-id <OCID> --force

    注意：
      - 只能删未附加的盘：挂在实例上的启动盘（IN-USE 状态）删不掉，
        必须先终止实例（或先分离）才能删除。
      - 删了不可恢复：启动盘删除后数据永久丢失，调用前请再三确认。
      - 本函数不做任何二次确认，调用方应在用户明确同意后才调用。

    删除成功返回 None（OCI CLI 无输出）。
    """
    return cli.run(["bv", "boot-volume", "delete",
                    "--boot-volume-id", boot_volume_id,
                    "--force"])


def list_boot_volume_attachments(cli, compartment_id, availability_domain,
                                 include_id=False):
    """列出指定 compartment + 可用域下的启动盘挂载关系。

    对应命令：oci compute boot-volume-attachment list
              --compartment-id <OCID> --availability-domain <AD>

    注意：该命令在 compute 组下（bv 组没有这个子命令），且
    --availability-domain 是必填参数。

    返回精简列表，每项含 boot-volume-id、instance-id、lifecycle-state
    （ATTACHED / ATTACHING / DETACHING / DETACHED）。
    include_id=True 时每项额外含挂载记录自身的 id（attachmentId，
    Web 磁盘列表用；默认不带，保持原有精简形状不变）。
    """
    result = cli.run(["compute", "boot-volume-attachment", "list",
                      "--compartment-id", compartment_id,
                      "--availability-domain", availability_domain])
    items = _extract_data(result) or []
    simplified = []
    for item in items:
        entry = {
            "boot-volume-id": item.get("boot-volume-id"),
            "instance-id": item.get("instance-id"),
            "lifecycle-state": item.get("lifecycle-state"),
        }
        if include_id:
            entry["id"] = item.get("id")
        simplified.append(entry)
    return simplified


def list_orphan_boot_volumes(cli, compartment_id):
    """列出孤儿启动盘：AVAILABLE 状态且无对应实例的盘。

    释放实例时如果保留了引导卷（--preserve-boot-volume true，OCI 的默认
    行为），启动盘会留下来，既占免费额度又容易被遗忘。本函数找出这些盘，
    方便用户清理。

    判断标准（实测修正，2026-11-23）：
      - 不能只看 lifecycle-state：启动盘没有 IN-USE 状态，挂在运行中
        实例上的盘同样是 AVAILABLE（新加坡 3 台在跑实例的启动盘实测
        全是 AVAILABLE）。只按 AVAILABLE 判断会把正常在用的盘全误判
        成孤儿。
      - 正确做法是查挂载关系：孤儿盘 = AVAILABLE 状态，且在
        boot-volume-attachment 列表里没有任何 ATTACHED / ATTACHING
        记录，且没有 instance-id 字段（兜底）。
    """
    volumes = list_boot_volumes(cli, compartment_id)
    # 按启动盘实际所在的 AD 逐个查挂载关系（attachment list 必须带 AD）。
    attached_ids = set()
    for ad in {v.get("availability-domain") for v in volumes if v.get("availability-domain")}:
        for att in list_boot_volume_attachments(cli, compartment_id, ad):
            if att.get("lifecycle-state") in ("ATTACHED", "ATTACHING"):
                attached_ids.add(att.get("boot-volume-id"))
    return [v for v in volumes
            if v.get("lifecycle-state") == "AVAILABLE"
            and v.get("id") not in attached_ids
            and not v.get("instance-id")]


def total_boot_volume_gb(cli, compartment_id):
    """返回该 compartment 下所有启动盘的总 GB 数。

    统计除 TERMINATING / TERMINATED 之外的全部启动盘：免费额度的 200GB
    上限按所有块存储卷 + 启动盘的总容量计算，挂在实例上的盘同样
    占用额度（实测：挂在运行中实例上的启动盘也是 AVAILABLE 状态，
    启动盘没有 IN-USE 状态）。已删除（TERMINATED）与删除中
    （TERMINATING）的盘不占额度，要排除——否则圣何塞这种删过盘的
    账号会把已删除的盘也算进去（2026-11-23 协调员复核发现）。
    配合 quota 模块判断当前已用多少免费额度。
    如只想看可清理的孤儿盘容量，改用 list_orphan_boot_volumes()
    再对 size-in-gbs 求和。
    """
    volumes = list_boot_volumes(cli, compartment_id)
    return sum(v.get("size-in-gbs") or 0 for v in volumes
               if v.get("lifecycle-state") not in ("TERMINATING", "TERMINATED"))
