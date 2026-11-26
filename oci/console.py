#!/usr/bin/env python3
"""串行控制台连接管理（救机入口）。

对应 OCI CLI：
  oci compute instance-console-connection create --instance-id ...
      --ssh-public-key-file <公钥文件>
  oci compute instance-console-connection list --compartment-id ...
      [--instance-id ...]
  oci compute instance-console-connection get --instance-console-connection-id ...
  oci compute instance-console-connection delete
      --instance-console-connection-id ... --force

所有函数都接受一个 oci.client.OCIClient 对象作为第一个参数。
"""

import os

# ================= 配置区（改这里） =================
# Default compartment OCID (placeholder). The root compartment is the tenancy OCID. 默认 compartment OCID（占位符）。根 compartment 就是 tenancy OCID。
DEFAULT_COMPARTMENT_ID = "ocid1.tenancy.oc1..xxxxxxxx"
# =====================================================


def _unwrap(data):
    if isinstance(data, dict) and "data" in data:
        return data["data"]
    return data


def validate_rsa_public_key(key_text):
    """校验公钥文本：只接受 ssh-rsa，其他类型抛 ValueError。

    返回去掉首尾空白的公钥文本。
    """
    # OCI serial console rejects ed25519 keys with 400 Invalid ssh public
    # key type "ssh-ed25519" (verified on ARM test machine 2026-11-25), so
    # only RSA keys are accepted here.
    # OCI 串行控制台拒绝 ed25519 密钥，400 Invalid ssh public key type
    # "ssh-ed25519"（2026-11-25 ARM 真机实测），这里只接受 RSA 密钥。
    text = (key_text or "").strip()
    if not text:
        raise ValueError("SSH 公钥为空")
    key_type = text.split(None, 1)[0]
    if key_type != "ssh-rsa":
        raise ValueError(
            f"串行控制台只接受 RSA 密钥（ssh-rsa），当前密钥类型是 "
            f"{key_type}，OCI 会报 400 Invalid ssh public key type。"
            "请用 ssh-keygen -t rsa 生成一把 RSA 密钥再试。")
    return text


def resolve_console_key_file(ssh_public_key_file=None):
    """解析串行控制台用的 RSA 公钥文件路径并校验内容。

    优先级：显式传入 > 环境变量 MBOT_CONSOLE_SSH_KEY_FILE >
    ~/.ssh/id_rsa.pub。文件不存在或内容不是 ssh-rsa 时抛 ValueError。
    """
    path = (ssh_public_key_file
            or os.environ.get("MBOT_CONSOLE_SSH_KEY_FILE")
            or os.path.expanduser("~/.ssh/id_rsa.pub"))
    if not os.path.isfile(path):
        raise ValueError(
            f"串行控制台需要 RSA 公钥文件，未找到：{path}。"
            "请设环境变量 MBOT_CONSOLE_SSH_KEY_FILE 指向一把 RSA 公钥"
            "（ssh-keygen -t rsa 生成，ed25519 会被 OCI 拒绝）。")
    with open(path, encoding="utf-8") as f:
        validate_rsa_public_key(f.read())
    return path


def create_console_connection(cli, instance_id, ssh_public_key_file=None):
    """为实例创建一个串行控制台连接，等到 ACTIVE。

    返回精简字典 {id, connection-string, fingerprint,
    service-host-key-fingerprint, lifecycle-state}。
    connection-string 是完整的 ssh 连接命令，用实例的 SSH 密钥连接。
    """
    key_file = resolve_console_key_file(ssh_public_key_file)
    created = _unwrap(cli.run(
        ["compute", "instance-console-connection", "create",
         "--instance-id", instance_id,
         "--ssh-public-key-file", key_file,
         "--wait-for-state", "ACTIVE",
         "--max-wait-seconds", "120",
         "--wait-interval-seconds", "5"])) or {}
    connection_id = created.get("id")
    if connection_id:
        # 跟进查详情走 SDK 直调（读接口，不起子进程）。
        detail = cli.sdk_get_instance_console_connection(connection_id) or {}
        for key, value in detail.items():
            if value is not None:
                created[key] = value
    return {
        "id": created.get("id"),
        "connection-string": created.get("connection-string"),
        "fingerprint": created.get("fingerprint"),
        "service-host-key-fingerprint":
            created.get("service-host-key-fingerprint"),
        "lifecycle-state": created.get("lifecycle-state"),
    }


def list_console_connections(cli, compartment_id, instance_id=None):
    """列出 compartment 下的串行控制台连接，可按实例过滤（SDK 直调）。

    返回精简列表，每项含 id / instance-id / connection-string /
    lifecycle-state / time-created。
    """
    items = cli.sdk_list_instance_console_connections(
        compartment_id, instance_id=instance_id) or []
    return [{
        "id": item.get("id"),
        "instance-id": item.get("instance-id"),
        "connection-string": item.get("connection-string"),
        "lifecycle-state": item.get("lifecycle-state"),
        "time-created": item.get("time-created"),
    } for item in items]


def delete_console_connection(cli, connection_id):
    """删除一个串行控制台连接。"""
    return cli.run(["compute", "instance-console-connection", "delete",
                    "--instance-console-connection-id", connection_id,
                    "--force"])
