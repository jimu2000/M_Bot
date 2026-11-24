#!/usr/bin/env python3
"""Cloudflare 凭据管理：手动添加 + 从服务器 1.sh 提取（v1.26.0）。

凭据来源（优先级从高到低）：
  1. 配置文件 tgbot/data/cloudflare.json（机器人/面板手动添加或提取后
     保存，0600 权限，只存 api_token / api_key / email 三个字段）
  2. 环境变量 CF_API_TOKEN（优先）或 CF_API_KEY + CF_EMAIL

安全说明：
  - 凭据值绝不写日志、绝不在机器人回复或网页响应里回显；
    展示时只显示来源（文件/环境变量）、认证方式与脱敏后的邮箱。
  - clouds/cloudflare.py 本身只接受环境变量或显式传入的参数，
    从不读文件；本模块是唯一读配置文件的入口，读出后显式传参
    构造客户端（不改 clouds 模块的边界）。
  - 提取只读目标服务器上的证书脚本（1.sh）里的 CF_Key/CF_Email
    两行，不读其他文件；提取到的凭据先调 Cloudflare API 校验，
    通过后才保存。

用法：
    from tgbot.cf_config import get_cf_client, validate_and_save
    client = get_cf_client()  # 文件优先，环境变量兜底
    ok, zones, err = validate_and_save(api_token="...", api_key="",
                                       email="")
"""

import json
import os
import re
import tempfile

from . import config as bot_config

try:
    from clouds.cloudflare import CloudflareClient, CloudflareError
except ImportError:  # pragma: no cover - clouds 模块缺失时降级
    CloudflareClient = None

    class CloudflareError(Exception):
        """clouds.cloudflare 未就绪时的替身（有 .kind 字段）。"""

        def __init__(self, message="", kind="api"):
            super().__init__(message)
            self.kind = kind


# ================= 配置区（改这里） =================
# 凭据配置文件：机器人/面板手动添加或提取后保存（0600 权限）。
CF_CONFIG_FILE = os.path.join(bot_config.DATA_DIR, "cloudflare.json")
# 提取时在目标服务器上找的证书脚本路径（按顺序，取第一个有凭据的）。
SCRIPT_PATHS = ("/usr/local/etc/xray/1.sh", "/root/1.sh")
# 环境变量名（与 clouds/cloudflare.py 一致）。
ENV_API_TOKEN = "CF_API_TOKEN"
ENV_API_KEY = "CF_API_KEY"
ENV_EMAIL = "CF_EMAIL"
# =====================================================

# 1.sh 里的凭据行：export CF_Key="..." / CF_Email='...' / 不带 export 也行。
_CRED_LINE_RE = re.compile(
    r"^\s*(?:export\s+)?(CF_Key|CF_Email)\s*=\s*[\"']?([^\"'\s]+)[\"']?\s*$",
    re.MULTILINE)


def _default_config():
    """空凭据配置（三个字段都为空字符串）。"""
    return {"api_token": "", "api_key": "", "email": ""}


def load_cf_config(path=None):
    """读凭据配置文件。文件缺失/损坏时返回空配置（不抛异常）。"""
    cfg = _default_config()
    path = path or CF_CONFIG_FILE
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return cfg
    if isinstance(data, dict):
        for key in cfg:
            if isinstance(data.get(key), str):
                cfg[key] = data[key]
    return cfg


def save_cf_config(cfg, path=None):
    """写凭据配置文件（原子写入，0600 权限，只存三个凭据字段）。"""
    path = path or CF_CONFIG_FILE
    data = {key: (cfg or {}).get(key, "") for key in _default_config()}
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=parent or ".", prefix=".cf-config-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return data


def clear_cf_config(path=None):
    """删凭据配置文件（删除后回落到环境变量；文件不存在时什么都不做）。"""
    path = path or CF_CONFIG_FILE
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def cf_configured(cfg=None):
    """这份配置能不能用：有 Token，或 Key + 邮箱齐全。"""
    cfg = cfg if cfg is not None else load_cf_config()
    if cfg.get("api_token"):
        return True
    return bool(cfg.get("api_key") and cfg.get("email"))


def cf_auth_mode(cfg=None):
    """认证方式：'token' / 'api_key' / None（未配置）。"""
    cfg = cfg if cfg is not None else load_cf_config()
    if cfg.get("api_token"):
        return "token"
    if cfg.get("api_key") and cfg.get("email"):
        return "api_key"
    return None


def resolve_cf_credentials(path=None):
    """解析最终生效的凭据：配置文件优先，环境变量兜底。

    返回 (api_token, api_key, email, source)；source 是 "file" /
    "env" / None（都没配）。调用方只拿返回值构造客户端，不要
    把返回值写日志。
    """
    cfg = load_cf_config(path)
    if cf_configured(cfg):
        return (cfg.get("api_token") or "", cfg.get("api_key") or "",
                cfg.get("email") or "", "file")
    token = os.environ.get(ENV_API_TOKEN) or ""
    key = os.environ.get(ENV_API_KEY) or ""
    email = os.environ.get(ENV_EMAIL) or ""
    if token or (key and email):
        return token, key, email, "env"
    return "", "", "", None


def get_cf_client(path=None):
    """取 Cloudflare 客户端（文件优先，环境变量兜底）。

    clouds/cloudflare.py 只接受显式参数，这里把解析出的凭据
    显式传进去；都没配时抛 CloudflareError(kind="config")。
    """
    if CloudflareClient is None:
        raise CloudflareError("Cloudflare 客户端未就绪", kind="config")
    api_token, api_key, email, _source = resolve_cf_credentials(path)
    if not (api_token or (api_key and email)):
        raise CloudflareError(
            f"未配置 Cloudflare 凭据：请设置环境变量 {ENV_API_TOKEN}，"
            f"或同时设置 {ENV_API_KEY} 与 {ENV_EMAIL}，"
            "或在机器人/面板里手动添加",
            kind="config",
        )
    return CloudflareClient(api_token=api_token or None,
                            api_key=api_key or None,
                            email=email or None)


def mask_cf_email(email):
    """脱敏邮箱：只留首字符与域名，如 a***@example.com。"""
    if not email or "@" not in email:
        return "***"
    local, domain = email.split("@", 1)
    return f"{local[:1]}***@{domain}"


def validate_cf_credentials(api_token, api_key, email):
    """校验一组凭据：调 Cloudflare API 列 Zone，通了才算有效。

    返回 (ok, zones_total, error_kind)；ok=False 时 error_kind 是
    CloudflareError 的 kind（config/auth/network/api 等）。
    凭据值绝不写日志。
    """
    if CloudflareClient is None:
        return False, 0, "config"
    if not (api_token or (api_key and email)):
        return False, 0, "config"
    try:
        client = CloudflareClient(api_token=api_token or None,
                                  api_key=api_key or None,
                                  email=email or None)
        zones = client.list_zones()
    except CloudflareError as exc:
        return False, 0, getattr(exc, "kind", "api")
    except Exception:  # noqa: BLE001 - 其他异常都按网络失败算
        return False, 0, "network"
    return True, len(zones or []), None


def validate_and_save(api_token, api_key, email, path=None):
    """校验凭据，通过后保存到配置文件；不通过不保存。

    返回 (ok, zones_total, error_kind)，与 validate_cf_credentials
    一致。保存的是调用方传进来的值（空字符串表示该字段不用）。
    """
    ok, zones_total, error_kind = validate_cf_credentials(
        api_token, api_key, email)
    if not ok:
        return False, 0, error_kind
    save_cf_config({"api_token": api_token or "",
                    "api_key": api_key or "",
                    "email": email or ""}, path=path)
    return True, zones_total, None


def extract_cf_from_script(text):
    """从 1.sh 文本里提取 CF_Key / CF_Email。

    返回 (api_key, email)，缺哪个返回 None；文本里没有凭据行时
    返回 (None, None)。只认 CF_Key / CF_Email 两行，不碰其他内容。
    """
    api_key = email = None
    for match in _CRED_LINE_RE.finditer(text or ""):
        if match.group(1) == "CF_Key":
            api_key = match.group(2)
        else:
            email = match.group(2)
    return api_key, email


def extract_cf_from_host(host, run_remote_command=None):
    """从一台服务器的 1.sh 提取 CF 凭据（SSH 只读）。

    host：run_remote_command 用的主机记录 dict。
    run_remote_command 不传时延迟 import（避免模块循环引用）；
    测试可传假函数。

    返回 (api_key, email, error)：成功时 error 是 None；
    失败时 error 是中文原因（ssh 失败 / 没有 1.sh / 里面没有凭据）。
    """
    if run_remote_command is None:
        from .handlers_client import run_remote_command as _runner
        run_remote_command = _runner
    paths = " ".join(SCRIPT_PATHS)
    command = (
        "grep -h -E '^(export )?(CF_Key|CF_Email)=' "
        f"{paths} 2>/dev/null"
    )
    try:
        _code, out, _err = run_remote_command(host, command)
    except Exception as exc:  # noqa: BLE001 - 连接/认证/超时都转中文
        return None, None, f"SSH 连接失败：{exc}"
    api_key, email = extract_cf_from_script(out or "")
    if not api_key and not email:
        return None, None, "该服务器上没有找到 1.sh，或里面没有 CF 凭据"
    if not api_key or not email:
        missing = "CF_Key" if not api_key else "CF_Email"
        return None, None, f"1.sh 里只有一半凭据，缺 {missing}"
    return api_key, email, None
