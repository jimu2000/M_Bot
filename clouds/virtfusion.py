#!/usr/bin/env python3
"""VirtFusion stub：实现 CloudProvider，方法尚未实现。

凭据（环境变量注入，绝不硬编码）：
    VIRT_FUSION_API_URL     VirtFusion 面板 API 地址
                            （如 https://面板域名/api/v1）
    VIRT_FUSION_API_TOKEN   API Token（在 VirtFusion 面板里生成）

API：
    VirtFusion REST API（Token 认证）。
    实例按厂商（hypervisor 节点）分组，列表/开关机/重启/改名/
    重置密码等接口待实现。
"""

import os

from .base import CloudProvider


class VirtFusionProvider(CloudProvider):
    """VirtFusion 厂商实现（stub：所有方法抛 NotImplementedError）。"""

    PROVIDER_NAME = "vf"
    display_name = "VirtFusion"

    def __init__(self, api_url=None, **kwargs):
        """只保存配置，不做真实连接；缺凭据也不报错。

        凭据用 kwargs 传，或从环境变量读；缺凭据也不报错，
        调用方法时才由具体实现决定如何报错。
        """
        config = dict(kwargs)
        config.setdefault(
            "api_url", api_url or os.environ.get("VIRT_FUSION_API_URL"))
        config.setdefault(
            "api_token", os.environ.get("VIRT_FUSION_API_TOKEN"))
        super().__init__(**config)

    def list_instances(self):
        raise NotImplementedError("尚未实现：VirtFusion list_instances")

    def get_instance(self, instance_id):
        raise NotImplementedError("尚未实现：VirtFusion get_instance")

    def create_instance(self, name, shape, image, **kwargs):
        raise NotImplementedError("尚未实现：VirtFusion create_instance")

    def start_instance(self, instance_id):
        raise NotImplementedError("尚未实现：VirtFusion start_instance")

    def stop_instance(self, instance_id):
        raise NotImplementedError("尚未实现：VirtFusion stop_instance")

    def restart_instance(self, instance_id):
        raise NotImplementedError("尚未实现：VirtFusion restart_instance")

    def terminate_instance(self, instance_id):
        raise NotImplementedError("尚未实现：VirtFusion terminate_instance")

    def list_public_ips(self):
        raise NotImplementedError("尚未实现：VirtFusion list_public_ips")

    def change_public_ip(self, instance_id):
        raise NotImplementedError("尚未实现：VirtFusion change_public_ip")

    def list_volumes(self):
        raise NotImplementedError("尚未实现：VirtFusion list_volumes")

    def get_quota(self):
        raise NotImplementedError("尚未实现：VirtFusion get_quota")
