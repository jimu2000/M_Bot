"""其他云厂商模块（阶段 4）。

- cloudflare.py：Cloudflare DNS 管理（CF API v4，httpx）。
- base.py：统一云厂商抽象接口。
- aws.py / gcp.py / azure.py / do.py / solusvm.py / virtfusion.py：
  各云 stub，方法体抛 NotImplementedError，待后续实现。
"""

from .aws import AWSProvider
from .azure import AzureProvider
from .base import CloudProvider
from .do import DOProvider
from .gcp import GCPProvider
from .solusvm import SolusVMProvider
from .virtfusion import VirtFusionProvider

__all__ = [
    "CloudProvider",
    "AWSProvider",
    "GCPProvider",
    "AzureProvider",
    "DOProvider",
    "SolusVMProvider",
    "VirtFusionProvider",
]
