"""Bark 加密推送（AES-256-CBC）与配置存取。

与 send_bark.py 同规格：payload 是 JSON
{"title", "body", "level": "active", "group"}，AES-256-CBC 加密
（key 是 32 字节原文，IV 每次随机 16 位 ASCII 字符），
POST ciphertext（base64）+ iv（原文）到 Bark 推送地址。
加密优先用 cryptography 库，未安装时用 openssl 命令行兜底
（与 web/routes/cert.py 的兜底模式一致）。

配置存 tgbot/data/bark.json（0600 权限）：
{"url": Bark 推送地址, "key": 32 位加密 Key, "enabled": 开关,
 "chat_id": 通知发到哪个 Telegram 聊天（定时任务用）}。
环境变量 MBOT_BARK_URL / MBOT_BARK_KEY 可作 url/key 的默认值
（文件里已配置的值优先；开关只认文件里的值，默认关闭）。
Key 与 URL 绝不记日志、不出现在报错里。
"""

import base64
import json
import os
import secrets
import string
import subprocess
import urllib.parse
import urllib.request

from . import config as bot_config

BARK_CONFIG_FILE = os.path.join(bot_config.DATA_DIR, "bark.json")
BARK_GROUP = "m-bot"
_IV_ALPHABET = string.ascii_letters + string.digits


def generate_bark_key():
    """生成一个 32 位随机加密 Key（16 字节随机数 hex 编码）。

    AES-256 需要 32 字节 Key；16 字节随机数 hex 编码后正好是
    32 位可打印字符，等价于 openssl rand -hex 16。注意 openssl
    rand -hex 32 会得到 64 位，太长了不能直接当 Key 用。
    """
    return secrets.token_hex(16)


def make_key_qr_png(key):
    """把 Bark 加密 Key 编成二维码 PNG（bytes），供 Bark App 扫码添加。

    用 segno 纯 Python 生成（自带 PNG 编码，不依赖 Pillow）。
    segno 未安装时返回 None，调用方退回只显示文字 Key。
    二维码内容就是 Key 原文。
    """
    try:
        import segno
    except ImportError:
        return None
    import io
    buf = io.BytesIO()
    segno.make(key, error="m").save(buf, kind="png", scale=6, border=2)
    return buf.getvalue()


def _encrypt_cryptography(payload_bytes, key_bytes, iv_bytes):
    """用 cryptography 库做 AES-256-CBC + PKCS7 加密。"""
    from cryptography.hazmat.primitives import padding as sym_padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    padder = sym_padding.PKCS7(128).padder()
    padded = padder.update(payload_bytes) + padder.finalize()
    cipher = Cipher(algorithms.AES(key_bytes), modes.CBC(iv_bytes))
    encryptor = cipher.encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def _encrypt_openssl(payload_bytes, key_bytes, iv_bytes):
    """openssl 命令行兜底（与 send_bark.py 完全一致的调用方式）。"""
    enc = subprocess.run(
        ["openssl", "enc", "-aes-256-cbc",
         "-K", key_bytes.hex(), "-iv", iv_bytes.hex()],
        input=payload_bytes, capture_output=True, timeout=15)
    if enc.returncode != 0:
        raise RuntimeError("openssl encrypt failed")
    return enc.stdout


def encrypt_payload(payload_bytes, key, iv=None):
    """加密一条 Bark payload，返回 (ciphertext_base64, iv_str)。

    key：32 字节原文（str 或 bytes）；iv 不传时随机生成 16 位 ASCII。
    优先用 cryptography，未安装时用 openssl 命令行兜底。
    """
    key_bytes = key.encode("utf-8") if isinstance(key, str) else key
    if len(key_bytes) != 32:
        raise ValueError("Bark 加密 Key 必须是 32 位")
    if iv is None:
        iv = "".join(secrets.choice(_IV_ALPHABET) for _ in range(16))
    iv_bytes = iv.encode("utf-8") if isinstance(iv, str) else iv
    if len(iv_bytes) != 16:
        raise ValueError("IV 必须是 16 位")
    try:
        ciphertext = _encrypt_cryptography(payload_bytes, key_bytes, iv_bytes)
    except ImportError:
        ciphertext = _encrypt_openssl(payload_bytes, key_bytes, iv_bytes)
    return base64.b64encode(ciphertext).decode("ascii"), iv


def load_bark_config(path=None):
    """读 Bark 配置。文件缺失/损坏时返回默认值（url/key 取环境变量）。

    返回 {"url": str, "key": str, "enabled": bool}。
    """
    cfg = {
        "url": os.environ.get("MBOT_BARK_URL", ""),
        "key": os.environ.get("MBOT_BARK_KEY", ""),
        "enabled": False,
        "chat_id": None,
    }
    path = path or BARK_CONFIG_FILE
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return cfg
    if isinstance(data, dict):
        if data.get("url"):
            cfg["url"] = data["url"]
        if data.get("key"):
            cfg["key"] = data["key"]
        cfg["enabled"] = bool(data.get("enabled"))
        if data.get("chat_id") is not None:
            cfg["chat_id"] = data["chat_id"]
    return cfg


def save_bark_config(cfg, path=None):
    """写 Bark 配置（0600 权限，只存 url/key/enabled/chat_id 四个字段）。"""
    path = path or BARK_CONFIG_FILE
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    data = {
        "url": (cfg or {}).get("url", ""),
        "key": (cfg or {}).get("key", ""),
        "enabled": bool((cfg or {}).get("enabled")),
        "chat_id": (cfg or {}).get("chat_id"),
    }
    text = json.dumps(data, ensure_ascii=False, indent=2)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return data


def bark_configured(cfg=None):
    """Bark 是否配齐（地址 + 32 位 Key）。"""
    cfg = cfg if cfg is not None else load_bark_config()
    return bool(cfg.get("url")) and len(cfg.get("key") or "") == 32


def mask_bark_url(url):
    """脱敏显示 Bark 地址：只留域名 + 路径末 4 位。"""
    if not url:
        return "未设置"
    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc or parsed.path.split("/")[0]
        tail = parsed.path.rstrip("/").split("/")[-1][-4:]
        return f"{parsed.scheme}://{host}/…{tail}" if tail else (
            f"{parsed.scheme}://{host}/…")
    except Exception:  # noqa: BLE001
        return "…"


def send_bark(title, body, group=BARK_GROUP, config=None):
    """发一条加密 Bark 推送，返回 (ok, error)。

    config 不传时读存储的配置；未配置或开关关闭时直接返回失败，
    不发请求（调用方决定是否静默）。Key/URL 绝不出现在返回值里。
    """
    cfg = config if config is not None else load_bark_config()
    if not cfg.get("enabled"):
        return False, "Bark 推送未开启"
    if not bark_configured(cfg):
        return False, "Bark 地址或加密 Key 未配置"
    payload = json.dumps(
        {"title": title, "body": body, "level": "active", "group": group},
        ensure_ascii=False,
    ).encode("utf-8")
    try:
        ciphertext, iv = encrypt_payload(payload, cfg["key"])
        data = urllib.parse.urlencode(
            {"ciphertext": ciphertext, "iv": iv}).encode("ascii")
        req = urllib.request.Request(cfg["url"], data=data, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    if result.get("code") == 200:
        return True, ""
    return False, f"bark code={result.get('code')} message={result.get('message')}"


def send_bark_test(config=None):
    """测试推送（忽略开关，但要求地址 + Key 配齐）。"""
    cfg = config if config is not None else load_bark_config()
    if not bark_configured(cfg):
        return False, "Bark 地址或加密 Key 未配置"
    cfg = dict(cfg)
    cfg["enabled"] = True
    return send_bark("M-探长测试", "Bark 推送测试成功", config=cfg)
