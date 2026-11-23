#!/usr/bin/env python3
"""DigitalOcean stub：实现 CloudProvider，方法尚未实现。

凭据（环境变量注入，绝不硬编码）：
    DIGITALOCEAN_TOKEN   个人访问令牌（Personal Access Token，
                         在 DO 控制台 API 页面生成，只需读/写 Droplet
                         权限即可，不要给不必要的权限）

SDK / CLI：
    pydo（pip install pydo，DO 官方 Python SDK）。
    也可直接调 REST API（https://api.digitalocean.com/v2），
    或用 doctl CLI（doctl compute droplet ...）。

官方文档：
    pydo 参考：
    https://docs.digitalocean.com/reference/pydo/
    REST API 参考：
    https://docs.digitalocean.com/reference/api/api-reference/
"""

import os

from .base import CloudProvider


class DOProvider(CloudProvider):
    """DigitalOcean 厂商实现（stub：所有方法抛 NotImplementedError）。"""

    PROVIDER_NAME = "do"
    display_name = "DigitalOcean"

    def __init__(self, token=None, **kwargs):
        """只保存配置，不做真实连接；缺凭据也不报错。"""
        super().__init__(
            token=token or os.environ.get("DIGITALOCEAN_TOKEN"),
            **kwargs,
        )

    def list_instances(self):
        raise NotImplementedError("尚未实现：DigitalOcean list_instances")

    def get_instance(self, instance_id):
        raise NotImplementedError("尚未实现：DigitalOcean get_instance")

    def create_instance(self, name, shape, image, **kwargs):
        raise NotImplementedError("尚未实现：DigitalOcean create_instance")

    def start_instance(self, instance_id):
        raise NotImplementedError("尚未实现：DigitalOcean start_instance")

    def stop_instance(self, instance_id):
        raise NotImplementedError("尚未实现：DigitalOcean stop_instance")

    def restart_instance(self, instance_id):
        raise NotImplementedError("尚未实现：DigitalOcean restart_instance")

    def terminate_instance(self, instance_id):
        raise NotImplementedError(
            "尚未实现：DigitalOcean terminate_instance")

    def list_public_ips(self):
        raise NotImplementedError("尚未实现：DigitalOcean list_public_ips")

    def change_public_ip(self, instance_id):
        raise NotImplementedError(
            "尚未实现：DigitalOcean change_public_ip")

    def list_volumes(self):
        raise NotImplementedError("尚未实现：DigitalOcean list_volumes")

    def get_quota(self):
        raise NotImplementedError("尚未实现：DigitalOcean get_quota")
