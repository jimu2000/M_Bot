#!/usr/bin/env python3
"""Azure（Microsoft Azure）stub：实现 CloudProvider，方法尚未实现。

凭据（环境变量注入，绝不硬编码）：
    AZURE_SUBSCRIPTION_ID   订阅 ID
    AZURE_TENANT_ID         租户 ID
    AZURE_CLIENT_ID         服务主体（应用）ID
    AZURE_CLIENT_SECRET     服务主体 Secret

SDK / CLI：
    azure-mgmt-compute + azure-identity
    （pip install azure-mgmt-compute azure-identity）。
    也可用 Azure CLI（az vm ...）。

官方文档：
    azure-mgmt-compute 参考：
    https://learn.microsoft.com/en-us/python/api/azure-mgmt-compute/
    Python SDK 认证（服务主体）：
    https://learn.microsoft.com/en-us/azure/developer/python/sdk/authentication/on-premises-apps
"""

import os

from .base import CloudProvider


class AzureProvider(CloudProvider):
    """Azure 厂商实现（stub：所有方法抛 NotImplementedError）。"""

    PROVIDER_NAME = "azure"
    display_name = "Azure"

    def __init__(self, subscription_id=None, tenant_id=None,
                 client_id=None, client_secret=None, **kwargs):
        """只保存配置，不做真实连接；缺凭据也不报错。"""
        super().__init__(
            subscription_id=subscription_id
            or os.environ.get("AZURE_SUBSCRIPTION_ID"),
            tenant_id=tenant_id or os.environ.get("AZURE_TENANT_ID"),
            client_id=client_id or os.environ.get("AZURE_CLIENT_ID"),
            client_secret=client_secret
            or os.environ.get("AZURE_CLIENT_SECRET"),
            **kwargs,
        )

    def list_instances(self):
        raise NotImplementedError("尚未实现：Azure list_instances")

    def get_instance(self, instance_id):
        raise NotImplementedError("尚未实现：Azure get_instance")

    def create_instance(self, name, shape, image, **kwargs):
        raise NotImplementedError("尚未实现：Azure create_instance")

    def start_instance(self, instance_id):
        raise NotImplementedError("尚未实现：Azure start_instance")

    def stop_instance(self, instance_id):
        raise NotImplementedError("尚未实现：Azure stop_instance")

    def restart_instance(self, instance_id):
        raise NotImplementedError("尚未实现：Azure restart_instance")

    def terminate_instance(self, instance_id):
        raise NotImplementedError("尚未实现：Azure terminate_instance")

    def list_public_ips(self):
        raise NotImplementedError("尚未实现：Azure list_public_ips")

    def change_public_ip(self, instance_id):
        raise NotImplementedError("尚未实现：Azure change_public_ip")

    def list_volumes(self):
        raise NotImplementedError("尚未实现：Azure list_volumes")

    def get_quota(self):
        raise NotImplementedError("尚未实现：Azure get_quota")
