#!/usr/bin/env python3
"""对象存储模块：Bucket 与对象管理（Web 面板的“对象存储”功能）。

原创实现，参考 OCI CLI 公开命令（oci os ...）编写。
所有函数都接受一个 oci.client.OCIClient 对象作为第一个参数。

命令对照（OCI CLI 3.96.3 --help 实测核对）：
  - 命名空间：oci os ns get（每个租户唯一，Bucket/对象命令都必填
    --namespace-name；本模块的 namespace 参数不传时自动查一次）。
  - Bucket：oci os bucket list/create/delete（delete 带 --force，
    只能删空 Bucket；想连对象一起删传 empty=True，走 --empty）。
  - 对象：oci os object list/put/delete（put 带 --force 才覆盖同名对象，
    delete 带 --force）。

安全说明（重要）：
  - 本模块不读取、不打印、不外传 API 私钥。认证全部由 OCI CLI 自己读
    配置文件（~/oci_config，旧部署回退 ~/.oci/config）与 key_file
    完成，私钥只在本地用于请求签名。
  - 配置区在文件顶端，真实 OCID / 命名空间一律用占位符，绝不写进代码。
  - 写操作（create/delete/upload）只在用户明确要求时调用；
    删除 Bucket/对象不可恢复，调用方应在用户二次确认后才调用。

用法示例（占位符）：
    from oci.client import OCIClient
    from oci.objectstorage import list_buckets, list_objects
    cli = OCIClient(profile="DEFAULT", region="ap-singapore-1")
    buckets = list_buckets(cli, "ocid1.tenancy.oc1..xxxxxxxx")
    objects = list_objects(cli, "my-bucket")
"""

import os
import re

# ================= 配置区（改这里） =================
# 默认 compartment OCID（占位符）。根 compartment 就是 tenancy OCID。
DEFAULT_COMPARTMENT_ID = "ocid1.tenancy.oc1..xxxxxxxx"
# 默认区域（如 ap-singapore-1 / us-sanjose-1 / us-chicago-1）。
DEFAULT_REGION = "ap-singapore-1"
# Bucket 名规则（OCI 要求）：1-63 字符，只能含小写字母、数字、点、横线，
# 且必须以字母或数字开头。
BUCKET_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.\-]{0,62}$")
# =====================================================


def _extract_data(result):
    """从 cli.run() 的返回值里取出 data 字段。

    list/get 类命令返回 {"data": ...}；兼容直接返回 list / dict /
    字符串的情况（os ns get 的 data 就是命名空间字符串本身）。
    """
    if isinstance(result, dict) and "data" in result:
        return result["data"]
    return result


def _resolve_namespace(cli, namespace):
    """namespace 参数不传时自动查一次（oci os ns get）。"""
    if namespace:
        return namespace
    return get_namespace(cli)


def _validate_bucket_name(name):
    """校验 Bucket 名是否符合 OCI 规则；不合法抛 ValueError。"""
    if not isinstance(name, str) or not BUCKET_NAME_PATTERN.match(name):
        raise ValueError(
            f"Bucket 名不合法：{name!r}（1-63 字符，小写字母/数字/点/横线，"
            "必须以字母或数字开头）")


def get_namespace(cli):
    """查当前租户的对象存储命名空间。

    对应命令：oci os ns get

    返回命名空间字符串。拿不到时抛 ValueError。
    """
    data = _extract_data(cli.run(["os", "ns", "get"]))
    if not isinstance(data, str) or not data:
        raise ValueError("无法获取对象存储命名空间（oci os ns get 返回异常）")
    return data


def list_buckets(cli, compartment_id, namespace=None):
    """列出指定 compartment 下的所有 Bucket。

    对应命令：oci os bucket list --compartment-id <OCID>
              --namespace-name <命名空间>

    返回精简列表，每项含 name、time-created、public-access-type、
    storage-tier。
    """
    ns = _resolve_namespace(cli, namespace)
    result = cli.run(["os", "bucket", "list",
                      "--compartment-id", compartment_id,
                      "--namespace-name", ns])
    items = _extract_data(result) or []
    return [{
        "name": item.get("name"),
        "time-created": item.get("time-created"),
        "public-access-type": item.get("public-access-type"),
        "storage-tier": item.get("storage-tier"),
    } for item in items]


def create_bucket(cli, compartment_id, name, namespace=None):
    """创建一个 Bucket（默认私有访问、标准存储层）。

    对应命令：oci os bucket create --compartment-id <OCID>
              --name <Bucket 名> --namespace-name <命名空间>

    返回创建后的 Bucket 详情字典。
    """
    _validate_bucket_name(name)
    ns = _resolve_namespace(cli, namespace)
    result = cli.run(["os", "bucket", "create",
                      "--compartment-id", compartment_id,
                      "--name", name,
                      "--namespace-name", ns])
    return _extract_data(result)


def delete_bucket(cli, bucket_name, namespace=None, empty=False):
    """删除一个 Bucket。

    对应命令：oci os bucket delete --bucket-name <名>
              --namespace-name <命名空间> --force [--empty]

    注意：
      - 只能删空 Bucket：Bucket 里还有对象时不带 --empty 会报错；
        empty=True 时先删光 Bucket 内全部对象再删 Bucket（不可恢复）。
      - 删了不可恢复：调用方应在用户二次确认后才调用。

    删除成功返回 None（OCI CLI 无输出）。
    """
    _validate_bucket_name(bucket_name)
    ns = _resolve_namespace(cli, namespace)
    args = ["os", "bucket", "delete",
            "--bucket-name", bucket_name,
            "--namespace-name", ns,
            "--force"]
    if empty:
        args.append("--empty")
    return cli.run(args)


def list_objects(cli, bucket_name, namespace=None):
    """列出某 Bucket 下的所有对象。

    对应命令：oci os object list --bucket-name <名>
              --namespace-name <命名空间>

    返回精简列表，每项含 name、size、time-created。
    """
    _validate_bucket_name(bucket_name)
    ns = _resolve_namespace(cli, namespace)
    result = cli.run(["os", "object", "list",
                      "--bucket-name", bucket_name,
                      "--namespace-name", ns])
    items = _extract_data(result) or []
    return [{
        "name": item.get("name"),
        "size": item.get("size"),
        "time-created": item.get("time-created"),
    } for item in items]


def upload_object(cli, bucket_name, object_name, file_path,
                  namespace=None, content_type=None):
    """上传一个文件为对象（同名对象会被覆盖）。

    对应命令：oci os object put --bucket-name <名> --name <对象名>
              --file <本地文件> --namespace-name <命名空间> --force
              [--content-type <类型>]

    注意：
      - --force 必加：不加时同名对象已存在会拒绝覆盖
        （OCI CLI 3.96.3 --help：--force 表示已存在则覆盖）。
      - file_path 必须存在且是文件，否则抛 ValueError（不等 OCI 报错）。

    返回上传后的对象信息字典。
    """
    _validate_bucket_name(bucket_name)
    if not object_name or not isinstance(object_name, str):
        raise ValueError("对象名不能为空")
    if not os.path.isfile(file_path):
        raise ValueError(f"上传文件不存在：{file_path}")
    ns = _resolve_namespace(cli, namespace)
    args = ["os", "object", "put",
            "--bucket-name", bucket_name,
            "--name", object_name,
            "--file", file_path,
            "--namespace-name", ns,
            "--force"]
    if content_type:
        args += ["--content-type", content_type]
    result = cli.run(args)
    return _extract_data(result)


def delete_object(cli, bucket_name, object_name, namespace=None):
    """删除 Bucket 里的一个对象。

    对应命令：oci os object delete --bucket-name <名> --name <对象名>
              --namespace-name <命名空间> --force

    注意：删了不可恢复，调用方应在用户二次确认后才调用。
    删除成功返回 None（OCI CLI 无输出）。
    """
    _validate_bucket_name(bucket_name)
    if not object_name or not isinstance(object_name, str):
        raise ValueError("对象名不能为空")
    ns = _resolve_namespace(cli, namespace)
    return cli.run(["os", "object", "delete",
                    "--bucket-name", bucket_name,
                    "--name", object_name,
                    "--namespace-name", ns,
                    "--force"])
