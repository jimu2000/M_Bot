#!/usr/bin/env python3
"""AWS（Amazon Web Services）stub：实现 CloudProvider，方法尚未实现。

凭据（环境变量注入，绝不硬编码）：
    AWS_ACCESS_KEY_ID       访问密钥 ID
    AWS_SECRET_ACCESS_KEY   访问密钥 Secret
    AWS_DEFAULT_REGION      默认区域（如 us-east-1）

SDK / CLI：
    boto3（pip install boto3），EC2 用 boto3.client("ec2")。
    也可用 AWS CLI（aws ec2 ...）。

官方文档：
    boto3 EC2 参考：
    https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/ec2.html
    环境变量配置：
    https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-envvars.html
"""

import os

from .base import CloudProvider


class AWSProvider(CloudProvider):
    """AWS 厂商实现（stub：所有方法抛 NotImplementedError）。"""

    PROVIDER_NAME = "aws"
    display_name = "AWS"

    def __init__(self, access_key_id=None, secret_access_key=None,
                 region=None, **kwargs):
        """只保存配置，不做真实连接；缺凭据也不报错。"""
        super().__init__(
            access_key_id=access_key_id
            or os.environ.get("AWS_ACCESS_KEY_ID"),
            secret_access_key=secret_access_key
            or os.environ.get("AWS_SECRET_ACCESS_KEY"),
            region=region or os.environ.get("AWS_DEFAULT_REGION"),
            **kwargs,
        )

    def list_instances(self):
        raise NotImplementedError("尚未实现：AWS list_instances")

    def get_instance(self, instance_id):
        raise NotImplementedError("尚未实现：AWS get_instance")

    def create_instance(self, name, shape, image, **kwargs):
        raise NotImplementedError("尚未实现：AWS create_instance")

    def start_instance(self, instance_id):
        raise NotImplementedError("尚未实现：AWS start_instance")

    def stop_instance(self, instance_id):
        raise NotImplementedError("尚未实现：AWS stop_instance")

    def restart_instance(self, instance_id):
        raise NotImplementedError("尚未实现：AWS restart_instance")

    def terminate_instance(self, instance_id):
        raise NotImplementedError("尚未实现：AWS terminate_instance")

    def list_public_ips(self):
        raise NotImplementedError("尚未实现：AWS list_public_ips")

    def change_public_ip(self, instance_id):
        raise NotImplementedError("尚未实现：AWS change_public_ip")

    def list_volumes(self):
        raise NotImplementedError("尚未实现：AWS list_volumes")

    def get_quota(self):
        raise NotImplementedError("尚未实现：AWS get_quota")
