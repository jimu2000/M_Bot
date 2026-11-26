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
  - 本模块不读取、不打印、不外传 OCI API 私钥；get_profile_user_id
    只读配置文件里的 user 字段（OCID），不读 key_file。
"""

import configparser
import logging
import os

from oci.client import resolve_oci_config_path

logger = logging.getLogger(__name__)

# ================= 配置区（改这里） =================
# OCI CLI config file path: use oci/client.py's unified resolution (MBOT_OCI_CONFIG_FILE > OCI_CLI_CONFIG_FILE > ~/oci_config > ~/.oci/config). OCI CLI 配置文件路径：用 oci/client.py 的统一解析（MBOT_OCI_CONFIG_FILE > OCI_CLI_CONFIG_FILE > ~/oci_config > ~/.oci/config）。
# Max user description length (OCI caps at 255 chars). 用户描述最大长度（OCI 限制 255 字符）。
MAX_DESCRIPTION_LEN = 255
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
    if description:
        args += ["--description", description]
    return _extract_data(cli.run(args)) or {}


def get_group_id_by_name(cli, compartment_id, group_name):
    """按名字找 IAM 组，返回组 OCID（找不到返回 None，SDK 直调）。"""
    for group in cli.sdk_list_groups(compartment_id) or []:
        if group.get("name") == group_name:
            return group.get("id")
    return None


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
    调用方必须先二次确认，并禁止删除机器人当前正在用的 API 用户。
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
