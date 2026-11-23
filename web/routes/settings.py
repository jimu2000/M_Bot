#!/usr/bin/env python3
"""设置路由（阶段 5 工作包 E）：OCI 代理设置。

  - GET /api/settings/oci-proxy：查看当前生效的代理
    {proxy, source}，source 为 env（环境变量 RBOT_OCI_PROXY）/
    file（oci_proxy.json）/ none（未配置）。
  - POST /api/settings/oci-proxy：设置代理 {proxy}，proxy 为空字符串
    表示清除；写入 DATA_DIR/oci_proxy.json，立即生效（OCI CLI 调用
    时每次重新解析，不用重启面板）。

注意：环境变量 RBOT_OCI_PROXY 的优先级高于配置文件；环境变量
已设置时，改配置文件不会改变实际生效的代理（响应里明确提示）。
代理只影响 OCI CLI 子进程的环境变量，不改全局环境。
"""

import json
import os

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from oci.client import PROXY_SCHEMES, get_oci_proxy
from web import config
from web.deps import error_response, get_current_user

router = APIRouter()


class OciProxyBody(BaseModel):
    """设置 OCI 代理请求体：proxy 为空字符串表示清除代理。"""

    proxy: str = ""


def _proxy_file():
    """oci_proxy.json 路径（每次都从 config 读 DATA_DIR，方便测试替换）。"""
    return os.path.join(config.DATA_DIR, "oci_proxy.json")


def get_proxy_status():
    """当前代理状态：{proxy, source}。

    source：env（环境变量生效）/ file（配置文件生效）/ none（未配置）。
    环境变量优先于配置文件（见 oci.client.get_oci_proxy）。
    """
    env_proxy = (os.environ.get("RBOT_OCI_PROXY") or "").strip()
    if env_proxy and env_proxy.startswith(PROXY_SCHEMES):
        return {"proxy": env_proxy, "source": "env"}
    proxy = get_oci_proxy(proxy_file=_proxy_file())
    if proxy:
        return {"proxy": proxy, "source": "file"}
    return {"proxy": None, "source": "none"}


def set_proxy(proxy):
    """设置代理并写文件：proxy 为空字符串表示清除（写空值，不删文件）。

    地址必须以 http://、https:// 或 socks5:// 开头，否则抛 ValueError。
    """
    proxy = (proxy or "").strip()
    if proxy and not proxy.startswith(PROXY_SCHEMES):
        raise ValueError(
            "代理地址必须以 http://、https:// 或 socks5:// 开头")
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(_proxy_file(), "w", encoding="utf-8") as f:
        json.dump({"proxy": proxy}, f, ensure_ascii=False, indent=2)
    return proxy


@router.get("/api/settings/oci-proxy")
def get_oci_proxy_settings(username: str = Depends(get_current_user)):
    """查看当前 OCI 代理设置。"""
    return {"ok": True, **get_proxy_status()}


@router.post("/api/settings/oci-proxy")
def set_oci_proxy_settings(body: OciProxyBody,
                           username: str = Depends(get_current_user)):
    """设置 OCI 代理（立即生效，不用重启面板）。"""
    try:
        proxy = set_proxy(body.proxy)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    status = get_proxy_status()
    message = "代理已更新，立即生效"
    if status["source"] == "env":
        message += ("（注意：环境变量 RBOT_OCI_PROXY 已设置且优先级更高，"
                    "当前实际生效的仍是环境变量里的代理）")
    return {"ok": True, "proxy": proxy or None, "source": status["source"],
            "effectiveProxy": status["proxy"], "message": message}
