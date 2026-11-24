#!/usr/bin/env python3
"""Email Delivery 模块：一键搭建发件人 + SMTP 凭据。

原创实现，参考 OCI CLI 公开命令编写。
所有函数都接受一个 oci.client.OCIClient 对象作为第一个参数。

命令对照（OCI CLI 3.96.3 --help 实测核对，注意第二条的命令组）：
  - 发件人：oci email sender list/create（create 后 OCI 会向该邮箱
    发验证邮件，需点击链接后发件人才可用；未验证前状态是待验证）。
  - SMTP 凭据：oci iam smtp-credential create --user-id <用户 OCID>
    --description <描述>（description 必填）。
    注意：任务初稿写的 `oci email smtp-credential create` 在 OCI CLI
    里不存在（实测报 No such command 'smtp-credential'），SMTP 凭据
    属于 IAM 命令组，这里用的是真实存在的命令。

安全说明（重要）：
  - 本模块不读取、不打印、不外传 API 私钥。认证全部由 OCI CLI 自己读
    配置文件（~/oci_config，旧部署回退 ~/.oci/config）与 key_file
    完成，私钥只在本地用于请求签名。
  - 配置区在文件顶端，真实 OCID 一律用占位符，绝不写进代码。
  - SMTP 凭据的密码只在创建时返回一次：调用方必须只显示一次，
    绝不写进日志、文件或测试快照，页面上也要明确提示用户立即保存。
  - 写操作（create_sender / create_smtp_credential）只在用户明确
    要求时调用。

用法示例（占位符）：
    from oci.client import OCIClient
    from oci.email import create_sender, create_smtp_credential
    cli = OCIClient(profile="DEFAULT", region="ap-singapore-1")
    sender = create_sender(cli, "ocid1.tenancy.oc1..xxxxxxxx",
                           "noreply@example.com")
    cred = create_smtp_credential(cli, "ocid1.user.oc1..xxxxxxxx")
    # cred["username"] / cred["password"] 只显示一次，立即保存！
"""

import re

# ================= 配置区（改这里） =================
# 默认 compartment OCID（占位符）。根 compartment 就是 tenancy OCID。
DEFAULT_COMPARTMENT_ID = "ocid1.tenancy.oc1..xxxxxxxx"
# 默认区域（如 ap-singapore-1 / us-sanjose-1 / us-chicago-1）。
DEFAULT_REGION = "ap-singapore-1"
# SMTP 凭据的默认描述（创建时 --description 必填）。
DEFAULT_SMTP_DESCRIPTION = "mbot"
# 邮箱格式校验（宽松版：只要形如 本地@域名.后缀 就放行，
# 真正的可用性由 OCI 验证邮件确认）。
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# =====================================================


def _extract_data(result):
    """从 cli.run() 的返回值里取出 data 字段（兼容直接返回的情况）。"""
    if isinstance(result, dict) and "data" in result:
        return result["data"]
    return result


def _extract_list(result):
    """取出列表型接口的 data 列表。

    空列表时 OCI CLI 只返回 {"opc-total-items": "0"}，没有 data
    字段——此时必须返回 []，不能把 dict 本身当列表迭代（迭代 dict
    取到的是 key 字符串，下游 .get() 会报 AttributeError，实测
    2026-11-24 触发过 /api/email/senders 500）。
    """
    if isinstance(result, dict):
        data = result.get("data")
        return data if isinstance(data, list) else []
    if isinstance(result, list):
        return result
    return []


def is_valid_email(email_address):
    """邮箱格式是否形如 本地@域名.后缀（宽松校验）。"""
    return bool(isinstance(email_address, str)
                and EMAIL_PATTERN.match(email_address.strip()))


def list_senders(cli, compartment_id):
    """列出指定 compartment 下的已批准发件人。

    对应命令：oci email sender list --compartment-id <OCID>

    返回精简列表，每项含 id、email-address、lifecycle-state、
    time-created。
    """
    result = cli.run(["email", "sender", "list",
                      "--compartment-id", compartment_id])
    items = _extract_list(result)
    return [{
        "id": item.get("id"),
        "email-address": item.get("email-address"),
        "lifecycle-state": item.get("lifecycle-state"),
        "time-created": item.get("time-created"),
    } for item in items]


def create_sender(cli, compartment_id, email_address):
    """创建一个发件人（Email Delivery 一键搭建第一步）。

    对应命令：oci email sender create --compartment-id <OCID>
              --email-address <邮箱>

    注意：
      - 邮箱格式不对时直接抛 ValueError，不调 OCI。
      - 创建后 OCI 会向该邮箱发验证邮件，必须点击链接验证后
        发件人才可用（lifecycle-state 变为 ACTIVE）。

    返回创建后的发件人详情字典。
    """
    if not is_valid_email(email_address):
        raise ValueError(f"邮箱格式不正确：{email_address!r}")
    result = cli.run(["email", "sender", "create",
                      "--compartment-id", compartment_id,
                      "--email-address", email_address.strip()])
    return _extract_data(result)


def delete_sender(cli, sender_id):
    """删除一个发件人。

    对应命令：oci email sender delete --sender-id <OCID> --force

    注意：--force 必须带，否则 CLI 交互确认会挂到超时。
    """
    cli.run(["email", "sender", "delete",
             "--sender-id", sender_id, "--force"])


def list_iam_users(cli, compartment_id):
    """列出租户下的 IAM 用户（给 SMTP 凭据选所属用户用）。

    对应命令：oci iam user list --compartment-id <OCID>

    返回精简列表，每项含 id、name、email、lifecycle-state。
    """
    result = cli.run(["iam", "user", "list",
                      "--compartment-id", compartment_id])
    items = _extract_list(result)
    return [{
        "id": item.get("id"),
        "name": item.get("name"),
        "email": item.get("email"),
        "lifecycle-state": item.get("lifecycle-state"),
    } for item in items]


def create_smtp_credential(cli, user_id, description=DEFAULT_SMTP_DESCRIPTION):
    """给指定 IAM 用户创建一个 SMTP 凭据（Email Delivery 一键搭建第二步）。

    对应命令：oci iam smtp-credential create --user-id <用户 OCID>
              --description <描述>

    注意：
      - --description 是必填参数（OCI CLI 3.96.3 --help 实测）。
      - 返回的字典含 username 与 password：密码只在创建时返回这一次，
        调用方必须只显示一次并提示用户立即保存，绝不写进日志或文件。

    返回 SMTP 凭据详情字典（含 id、username、password、description）。
    """
    if not user_id or not isinstance(user_id, str):
        raise ValueError("user_id 不能为空")
    if not description:
        raise ValueError("description 不能为空（OCI 要求必填）")
    result = cli.run(["iam", "smtp-credential", "create",
                      "--user-id", user_id,
                      "--description", description])
    return _extract_data(result)
