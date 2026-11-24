#!/usr/bin/env python3
"""SolusVM stub：实现 CloudProvider，方法尚未实现。

SolusVM API 可管理 racknerd 等 VPS 商的机器（开机/重启/重装等），本模块以后接这套 API。

凭据（环境变量注入，绝不硬编码）：
    SOLUSVM_API_URL    SolusVM 面板 API 地址（如 https://面板域名/api/client）
    SOLUSVM_API_KEY    API Key（在 SolusVM 面板 API Access 里生成）
    SOLUSVM_API_HASH   API Hash（与 Key 配对）

API：
    SolusVM API v1：POST 到 <面板地址>/api/client/command.php，
    参数含 key/hash/action 等；用 httpx 或 requests 直接调，无官方
    Python SDK。

官方文档：
    SolusVM API 文档：
    https://documentation.solusvm.com/display/DOCS/API
"""

import os

from .base import CloudProvider


class SolusVMProvider(CloudProvider):
    """SolusVM 厂商实现（stub：所有方法抛 NotImplementedError）。"""

    PROVIDER_NAME = "solusvm"
    display_name = "SolusVM"

    def __init__(self, api_url=None, api_key=None, api_hash=None,
                 **kwargs):
        """只保存配置，不做真实连接；缺凭据也不报错。"""
        super().__init__(
            api_url=api_url or os.environ.get("SOLUSVM_API_URL"),
            api_key=api_key or os.environ.get("SOLUSVM_API_KEY"),
            api_hash=api_hash or os.environ.get("SOLUSVM_API_HASH"),
            **kwargs,
        )

    def list_instances(self):
        raise NotImplementedError("尚未实现：SolusVM list_instances")

    def get_instance(self, instance_id):
        raise NotImplementedError("尚未实现：SolusVM get_instance")

    def create_instance(self, name, shape, image, **kwargs):
        raise NotImplementedError("尚未实现：SolusVM create_instance")

    def start_instance(self, instance_id):
        raise NotImplementedError("尚未实现：SolusVM start_instance")

    def stop_instance(self, instance_id):
        raise NotImplementedError("尚未实现：SolusVM stop_instance")

    def restart_instance(self, instance_id):
        raise NotImplementedError("尚未实现：SolusVM restart_instance")

    def terminate_instance(self, instance_id):
        raise NotImplementedError("尚未实现：SolusVM terminate_instance")

    def list_public_ips(self):
        raise NotImplementedError("尚未实现：SolusVM list_public_ips")

    def change_public_ip(self, instance_id):
        raise NotImplementedError("尚未实现：SolusVM change_public_ip")

    def list_volumes(self):
        raise NotImplementedError("尚未实现：SolusVM list_volumes")

    def get_quota(self):
        raise NotImplementedError("尚未实现：SolusVM get_quota")
