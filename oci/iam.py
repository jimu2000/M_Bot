#!/usr/bin/env python3
"""IAM 用户与 API 密钥管理（oci iam user ...）。

配置区在文件顶端。调用方（Telegram 机器人）应在用户确认后才调
写操作；删除用户、删除 API 密钥均不可逆。

注意：
  - 用户名（name）不可改：OCI 的 UpdateUser API 只接受 description /
    email / tags 等字段，`oci iam user update` 没有 --name 选项
    （CLI 3.96.3 --help 实测）；改名只能删用户重建。
  - update / delete / api-key delete 都带 --force：不带时 OCI CLI
    会交互确认，subprocess 无 stdin 应答会挂到超时（2026-11-23
    真实验证：不加 --force 超时 120 秒）。
  - 本模块不读取已有的 OCI API 私钥；新 API 私钥只在权限 0700 的
    临时目录生成，权限 0600，读取后交给调用方一次性交付，绝不写日志。
  - get_profile_user_id / get_profile_api_fingerprint 只读配置文件中的
    user / fingerprint 字段，不读取 key_file 指向的私钥文件。
"""

import configparser
import logging
import os
import json
import subprocess
import tempfile

from oci.client import resolve_oci_config_path

logger = logging.getLogger(__name__)

# ================= 配置区（改这里） =================
# OCI CLI config file path: use oci/client.py's unified resolution (MBOT_OCI_CONFIG_FILE > OCI_CLI_CONFIG_FILE > ~/oci_config > ~/.oci/config). OCI CLI 配置文件路径：用 oci/client.py 的统一解析（MBOT_OCI_CONFIG_FILE > OCI_CLI_CONFIG_FILE > ~/oci_config > ~/.oci/config）。
# Max user description length (OCI caps at 255 chars). 用户描述最大长度（OCI 限制 255 字符）。
MAX_DESCRIPTION_LEN = 255
# Tenancy admin group name (OCI default; created with every tenancy). 租户管理员组名（OCI 默认组，每个租户创建时自带）。
ADMIN_GROUP_NAME = "Administrators"
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
    2026-01-01 触发过 /api/email/senders 500）。
    """
    if isinstance(result, dict):
        data = result.get("data")
        return data if isinstance(data, list) else []
    if isinstance(result, list):
        return result
    return []


def create_user(cli, compartment_id, name, email, description=None):
    """创建 IAM 用户。

    对应命令：oci iam user create --compartment-id <租户 OCID>
              --name <用户名> --email <邮箱> [--description <描述>]

    compartment_id 传 tenancy OCID（IAM 用户属于租户，不属于子
    compartment）。创建后用户没有控制台密码，需另行设置临时密码
    （`oci iam user create-or-reset-ui-password` 或控制台）。
    返回新建用户的字典（id / name / email / lifecycle-state 等）。
    """
    args = ["iam", "user", "create",
            "--compartment-id", compartment_id,
            "--name", name]
    if email:
        args += ["--email", email]
    # OCI CLI 当前版本将 description 列为必填；空描述也显式传入。
    args += ["--description", description or "",
             "--wait-for-state", "ACTIVE",
             "--wait-interval-seconds", "2",
             "--max-wait-seconds", "60"]
    return _extract_data(cli.run(args, timeout=90)) or {}


def get_group_id_by_name(cli, compartment_id, group_name):
    """按名字找 IAM 组，返回组 OCID（找不到返回 None，SDK 直调）。"""
    for group in cli.sdk_list_groups(compartment_id) or []:
        if group.get("name") == group_name:
            return group.get("id")
    return None


def is_last_admin(cli, tenancy_id, user_id):
    """目标用户是不是租户里最后一个管理员。

    管理员 = Administrators 组成员。OCI 本身不拦删最后一个管理员
    （删完整个租户就没人能管了，只能找 Oracle 支持恢复），所以
    M_Bot 在这里拦：目标用户不在 Administrators 组里、组不存在、
    或组里还有其他成员时返回 False；组里只剩目标用户一人时返回
    True，调用方应拒绝删除。
    """
    if not user_id or not isinstance(user_id, str):
        raise ValueError("user_id 不能为空")
    group_id = get_group_id_by_name(cli, tenancy_id, ADMIN_GROUP_NAME)
    if not group_id:
        return False
    members = cli.sdk_list_group_users(group_id) or []
    member_ids = [m.get("id") for m in members if isinstance(m, dict)]
    return user_id in member_ids and len(member_ids) == 1


def add_user_to_group(cli, user_id, group_id):
    """把 IAM 用户加进组。

    对应命令：oci iam group add-user --user-id <用户 OCID>
              --group-id <组 OCID>。
    """
    return cli.run(["iam", "group", "add-user",
                    "--user-id", user_id, "--group-id", group_id])


def update_user(cli, user_id, description=None, email=None):
    """更新 IAM 用户的描述 / 邮箱。

    对应命令：oci iam user update --user-id <OCID>
              [--description <描述>] [--email <邮箱>] --force

    注意：
      - description 与 email 至少传一个，否则直接抛 ValueError。
      - description 超过 255 字符时抛 ValueError，不调 OCI。
      - 邮箱格式由调用方校验（tgbot 用 oci.email.is_valid_email）。

    返回更新后的用户详情字典。
    """
    if not user_id or not isinstance(user_id, str):
        raise ValueError("user_id 不能为空")
    if description is None and email is None:
        raise ValueError("description 与 email 至少要传一个")
    if description is not None and len(description) > MAX_DESCRIPTION_LEN:
        raise ValueError(f"描述不能超过 {MAX_DESCRIPTION_LEN} 字符")
    args = ["iam", "user", "update", "--user-id", user_id]
    if description is not None:
        args += ["--description", description]
    if email is not None:
        args += ["--email", email]
    args += ["--force"]
    return _extract_data(cli.run(args))


def delete_user(cli, user_id):
    """删除 IAM 用户（oci iam user delete --user-id <OCID> --force）。

    不可逆：删除用户会同时删除它的全部 API 密钥与 MFA 设备。
    调用方必须先二次确认，并禁止删除机器人当前正在用的 API 用户，
    还要先调 is_last_admin() 确认目标不是租户最后一个管理员
    （OCI 本身不拦删最后一个管理员，删了租户就没人能管了）。
    成功时 OCI CLI 无输出，本函数返回 None。
    """
    if not user_id or not isinstance(user_id, str):
        raise ValueError("user_id 不能为空")
    return cli.run(["iam", "user", "delete",
                    "--user-id", user_id, "--force"])


def list_api_keys(cli, user_id):
    """列出某用户的 API 密钥（SDK 直调）。

    返回精简列表，每项含 fingerprint、lifecycle-state、time-created。
    公钥内容（key-value）不返回——展示不需要，少传敏感数据。
    """
    if not user_id or not isinstance(user_id, str):
        raise ValueError("user_id 不能为空")
    items = cli.sdk_list_api_keys(user_id) or []
    return [{
        "fingerprint": item.get("fingerprint"),
        "lifecycle-state": item.get("lifecycle-state"),
        "time-created": item.get("time-created"),
    } for item in items]


def delete_api_key(cli, user_id, fingerprint):
    """删除某用户的 API 密钥。

    对应命令：oci iam user api-key delete --user-id <OCID>
              --fingerprint <指纹> --force

    不可逆：删除后用该密钥的 API 调用立即失效。
    成功时 OCI CLI 无输出，本函数返回 None。
    """
    if not user_id or not isinstance(user_id, str):
        raise ValueError("user_id 不能为空")
    if not fingerprint or not isinstance(fingerprint, str):
        raise ValueError("fingerprint 不能为空")
    return cli.run(["iam", "user", "api-key", "delete",
                    "--user-id", user_id,
                    "--fingerprint", fingerprint, "--force"])


def get_profile_user_id(profile="DEFAULT", config_path=None):
    """读 OCI 配置文件里某 profile 的 user OCID（即该 profile 的
    API 用户）。

    用途：删除用户前的安全检查——禁止删除机器人当前 profile
    正在用的 API 用户（删了机器人立即失联且无法自愈）。

    配置文件缺失 / profile 不存在 / 没有 user 字段时返回 None。
    只读 user 字段，不读 key_file、不碰私钥。

    注意 configparser 的坑：OCI 配置文件有 [DEFAULT] 段，
    configparser 默认把 DEFAULT 当特殊段处理，必须用
    default_section="__oci_no_defaults__" 才能正常读。
    """
    path = config_path or resolve_oci_config_path()
    parser = configparser.ConfigParser(
        default_section="__oci_no_defaults__", interpolation=None)
    try:
        read = parser.read(path)
    except (OSError, configparser.Error) as exc:
        logger.warning("OCI 配置文件读取失败：%s", exc)
        return None
    if not read:
        return None
    try:
        user_id = parser.get(profile, "user")
    except (configparser.NoSectionError, configparser.NoOptionError):
        return None
    return user_id.strip() or None

def create_or_reset_ui_password(cli, user_id):
    """为 IAM 用户生成/重置 OCI 控制台临时密码。只返回密码，不记录日志。

    OCI 的 NotAuthorizedOrNotFound 是刻意模糊化的响应，常见原因包括：
      - 当前 API 签名用户没有 tenancy 级管理用户凭据的权限；
      - 目标用户不存在、已停用，或不属于经典 IAM 控制台密码流程；
      - 租户使用 Identity Domains，密码重置应由身份域邮件流程完成。
    不把原始 ServiceError JSON、请求签名细节或请求 ID暴露给面板/机器人。
    """
    if not user_id or not isinstance(user_id, str):
        raise ValueError("user_id 不能为空")
    # OCI CLI 的真实命令路径为 iam user ui-password create-or-reset。
    try:
        result = cli.run(["iam", "user", "ui-password", "create-or-reset",
                          "--user-id", user_id])
    except Exception as exc:  # noqa: BLE001 - 翻译已知 OCI 错误再抛出
        text = str(exc)
        if "NotAuthorizedOrNotFound" in text:
            raise RuntimeError(
                "Oracle 拒绝了密码重置请求（NotAuthorizedOrNotFound）。"
                "请确认当前 API 用户属于 Administrators 组，或其所在组在根租户"
                "策略中具有 `Allow group <组名> to manage users in tenancy` 权限；"
                "仅有 compartment 权限不够。目标用户必须是 ACTIVE 且支持经典 "
                "IAM 控制台密码。若租户启用了 Identity Domains，请改用身份域"
                "中的“重置密码”，系统会向用户邮箱发送重置链接。"
                "此错误不能通过面板绕过 OCI 权限校验。") from exc
        if "IdcsConversionError" in text or "用户已停用" in text:
            raise RuntimeError(
                "Oracle 无法为该用户创建控制台临时密码。请检查用户是否为 "
                "ACTIVE，以及该用户是否由 Identity Domains 管理；身份域用户"
                "应通过身份域邮件重置流程处理。") from exc
        if "PATCH request failed" in text or "request failed" in text.lower():
            raise RuntimeError(
                "Oracle 身份服务未能完成密码重置请求。请检查账号主区域、"
                "API 签名密钥和网络代理；若其他 IAM 操作正常，请在 OCI 控制台"
                "检查该用户的登录类型/身份域，并稍后重试。") from exc
        raise RuntimeError(
            "OCI 控制台密码重置失败。请检查该用户状态、当前 API 用户的 "
            "tenancy 级 IAM 权限和账号主区域；服务器日志中保留了诊断信息。"
        ) from exc
    data = _extract_data(result) or {}
    if isinstance(data, dict):
        password = (data.get("password") or data.get("temporary-password")
                    or data.get("temporary_password"))
    else:
        password = None
    # 使用 Identity Domains 的租户可能通过邮件发送重置链接，而不是返回密码。
    return str(password) if password else None


def build_oci_config(profile_name, user_id, fingerprint, tenancy_id,
                     region):
    """拼一段可直接用的 OCI config 文本。

    新建用户/新增 API Key 后随私钥一起返回，前端展示为可下载的
    oci_config.txt，用户可直接粘贴到云账户页“添加租户”。key_file
    用 /root/oci_api_key.pem，与该页占位示例一致（机器人端用的是
    ~/.oci/oci_api_key.pem，那是另一条路）。
    """
    if not profile_name or not user_id or not fingerprint or not tenancy_id:
        raise ValueError("生成 OCI 配置缺少必要字段")
    lines = [
        f"[{profile_name}]",
        f"user={user_id}",
        f"fingerprint={fingerprint}",
        "key_file=/root/oci_api_key.pem",
        f"tenancy={tenancy_id}",
        f"region={region or 'ap-singapore-1'}",
    ]
    return "\n".join(lines) + "\n"


def create_api_key(cli, user_id, bits=2048):
    """本机临时生成 RSA 私钥，上传公钥并返回 fingerprint + 私钥字节。

    私钥仅存在于权限 0700 的临时目录，读取后随 TemporaryDirectory 自动删除；
    不打印、不写入项目配置、不进入日志。调用方负责通过受控方式交付私钥。
    """
    if not user_id or not isinstance(user_id, str):
        raise ValueError("user_id 不能为空")
    existing_keys = cli.sdk_list_api_keys(user_id) or []
    if len(existing_keys) >= 3:
        raise ValueError("该 OCI 用户已达到最多 3 个 API signing key 的上限，请先删除旧 Key。")
    if bits not in (2048, 3072, 4096):
        raise ValueError("RSA bits 只允许 2048、3072 或 4096")
    with tempfile.TemporaryDirectory(prefix="mbot-oci-key-") as tmp:
        os.chmod(tmp, 0o700)
        private_path = os.path.join(tmp, "oci_api_key.pem")
        public_path = os.path.join(tmp, "oci_api_key_public.pem")
        gen = subprocess.run(
            ["openssl", "genrsa", "-out", private_path, str(bits)],
            capture_output=True, text=True, timeout=30)
        if gen.returncode != 0:
            raise RuntimeError("生成 API 私钥失败：请确认服务器已安装 openssl。")
        os.chmod(private_path, 0o600)
        pub = subprocess.run(
            ["openssl", "rsa", "-pubout", "-in", private_path,
             "-out", public_path], capture_output=True, text=True, timeout=30)
        if pub.returncode != 0:
            raise RuntimeError("从私钥导出公钥失败。")
        os.chmod(public_path, 0o600)
        result = cli.run(["iam", "user", "api-key", "upload",
                          "--user-id", user_id,
                          "--key-file", public_path])
        data = _extract_data(result) or {}
        fingerprint = data.get("fingerprint") if isinstance(data, dict) else None
        if not fingerprint:
            raise RuntimeError("OCI 未返回 API Key fingerprint，密钥上传状态需检查。")
        with open(private_path, "rb") as f:
            private_key_pem = f.read()
    lifecycle_state = (data.get("lifecycle-state") or "CREATING"
                       if isinstance(data, dict) else "CREATING")
    return {"fingerprint": fingerprint, "private_key_pem": private_key_pem,
            "lifecycle_state": lifecycle_state}


def create_bot_permission_user(cli, tenancy_id, group_name, user_name,
                               user_email, policy_name):
    """幂等创建专用 Bot IAM 用户、组和策略，并将用户加入该组。

    策略只授予实例族、卷族、虚拟网络族管理权限，与用户提供的脚本一致。
    已存在的策略只补齐缺失语句，不覆盖原有语句。
    """
    for value, label in ((tenancy_id, "tenancy_id"),
                         (group_name, "group_name"),
                         (user_name, "user_name"),
                         (user_email, "user_email"),
                         (policy_name, "policy_name")):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} 不能为空")

    group_id = get_group_id_by_name(cli, tenancy_id, group_name)
    group_created = False
    if not group_id:
        result = cli.run(["iam", "group", "create",
                          "--compartment-id", tenancy_id,
                          "--name", group_name,
                          "--description", "Group for API usage",
                          "--wait-for-state", "ACTIVE",
                          "--wait-interval-seconds", "2",
                          "--max-wait-seconds", "60"], timeout=90)
        data = _extract_data(result) or {}
        group_id = data.get("id") if isinstance(data, dict) else None
        if not group_id:
            raise RuntimeError("IAM 组创建成功响应中缺少组 OCID。")
        group_created = True

    users = cli.sdk_list_users(tenancy_id) or []
    user = next((item for item in users
                 if item.get("name") == user_name), None)
    user_created = False
    if user is None:
        user = create_user(cli, tenancy_id, user_name, user_email,
                           "API user for automation")
        user_created = True
    elif user.get("email") and user.get("email").lower() != user_email.lower():
        raise ValueError(
            f"同名 IAM 用户 {user_name} 已存在，但邮箱与指定邮箱不同；"
            "为避免误改现有用户，已停止创建。")
    user_id = user.get("id") if isinstance(user, dict) else None
    if not user_id:
        raise RuntimeError("IAM 用户创建/查询结果缺少用户 OCID。")

    statements = [
        f"Allow group {group_name} to manage instance-family in tenancy",
        f"Allow group {group_name} to manage volume-family in tenancy",
        f"Allow group {group_name} to manage virtual-network-family in tenancy",
        # Required by the panel/bot IAM user-management features, including
        # CreateOrResetUIPassword for other users. This is tenancy-wide IAM
        # user administration and must be clearly disclosed to the operator.
        f"Allow group {group_name} to manage users in tenancy",
    ]
    policies = _extract_list(cli.run([
        "iam", "policy", "list", "--compartment-id", tenancy_id, "--all"]))
    policy = next((item for item in policies
                   if item.get("name") == policy_name), None)
    policy_created = False
    if policy is None:
        result = cli.run(["iam", "policy", "create",
                          "--compartment-id", tenancy_id,
                          "--name", policy_name,
                          "--description", "Policy for API Group",
                          "--statements", json.dumps(statements),
                          "--wait-for-state", "ACTIVE",
                          "--wait-interval-seconds", "2",
                          "--max-wait-seconds", "60"], timeout=90)
        policy_created = True
    else:
        old_statements = policy.get("statements") or []
        merged = list(old_statements)
        for statement in statements:
            if statement not in merged:
                merged.append(statement)
        if merged != old_statements:
            policy_id = policy.get("id")
            if not policy_id:
                raise RuntimeError("同名 IAM Policy 已存在，但缺少 Policy OCID，无法补齐权限。")
            cli.run(["iam", "policy", "update", "--policy-id", policy_id,
                     "--statements", json.dumps(merged), "--force"])

    # 先查询用户当前组，避免重复 add-user 触发 409。
    memberships = _extract_list(cli.run([
        "iam", "user", "list-groups", "--user-id", user_id, "--all"]))
    already_member = any(item.get("id") == group_id for item in memberships)
    membership_added = False
    if not already_member:
        add_user_to_group(cli, user_id, group_id)
        membership_added = True

    return {
        "user": user, "user_id": user_id, "group_id": group_id,
        "group_created": group_created, "user_created": user_created,
        "policy_created": policy_created,
        "membership_added": membership_added,
        "policy_name": policy_name, "group_name": group_name,
    }


def get_profile_tenancy_id(profile="DEFAULT", config_path=None):
    """只读 OCI profile 的 tenancy OCID，不读取 key_file 或私钥。"""
    path = config_path or resolve_oci_config_path()
    parser = configparser.ConfigParser(
        default_section="__oci_no_defaults__", interpolation=None)
    try:
        if not parser.read(path):
            return None
        value = parser.get(profile, "tenancy")
    except (OSError, configparser.Error):
        return None
    return value.strip() or None

def get_profile_api_fingerprint(profile="DEFAULT", config_path=None):
    """只读 OCI profile 的 API key fingerprint，用于防止删掉机器人正在用的 Key。"""
    path = config_path or resolve_oci_config_path()
    parser = configparser.ConfigParser(
        default_section="__oci_no_defaults__", interpolation=None)
    try:
        if not parser.read(path):
            return None
        value = parser.get(profile, "fingerprint")
    except (OSError, configparser.Error):
        return None
    return value.strip().lower() or None

