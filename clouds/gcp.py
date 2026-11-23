#!/usr/bin/env python3
"""GCP（Google Cloud Platform）stub：实现 CloudProvider，方法尚未实现。

凭据（环境变量注入，绝不硬编码）：
    GOOGLE_APPLICATION_CREDENTIALS   服务账号 JSON 文件路径
    （服务账号需有 Compute Engine 相关权限；JSON 文件自己放到服务器上，
    路径只写进环境变量，密钥内容不进代码、不进仓库。）

SDK / CLI：
    google-cloud-compute（pip install google-cloud-compute）。
    也可用 gcloud CLI（gcloud compute instances ...）。

官方文档：
    google-cloud-compute 参考：
    https://cloud.google.com/python/docs/reference/compute/latest
    应用默认凭据（ADC）：
    https://cloud.google.com/docs/authentication/application-default-credentials
"""

import os

from .base import CloudProvider


class GCPProvider(CloudProvider):
    """GCP 厂商实现（stub：所有方法抛 NotImplementedError）。"""

    PROVIDER_NAME = "gcp"
    display_name = "GCP"

    def __init__(self, credentials_path=None, project=None, zone=None,
                 **kwargs):
        """只保存配置，不做真实连接；缺凭据也不报错。"""
        super().__init__(
            credentials_path=credentials_path
            or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"),
            project=project,
            zone=zone,
            **kwargs,
        )

    def list_instances(self):
        raise NotImplementedError("尚未实现：GCP list_instances")

    def get_instance(self, instance_id):
        raise NotImplementedError("尚未实现：GCP get_instance")

    def create_instance(self, name, shape, image, **kwargs):
        raise NotImplementedError("尚未实现：GCP create_instance")

    def start_instance(self, instance_id):
        raise NotImplementedError("尚未实现：GCP start_instance")

    def stop_instance(self, instance_id):
        raise NotImplementedError("尚未实现：GCP stop_instance")

    def restart_instance(self, instance_id):
        raise NotImplementedError("尚未实现：GCP restart_instance")

    def terminate_instance(self, instance_id):
        raise NotImplementedError("尚未实现：GCP terminate_instance")

    def list_public_ips(self):
        raise NotImplementedError("尚未实现：GCP list_public_ips")

    def change_public_ip(self, instance_id):
        raise NotImplementedError("尚未实现：GCP change_public_ip")

    def list_volumes(self):
        raise NotImplementedError("尚未实现：GCP list_volumes")

    def get_quota(self):
        raise NotImplementedError("尚未实现：GCP get_quota")
