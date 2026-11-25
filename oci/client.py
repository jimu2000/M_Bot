#!/usr/bin/env python3
"""OCI CLI 调用封装：用 subprocess 调官方 OCI CLI，多 profile / 多 region，错误分类。

安全说明（重要）：
  - 本模块不读取、不打印、不外传 API 私钥。认证全部由 OCI CLI 自己读
    配置文件（resolve_oci_config_path() 解析出的路径，见下方）与
    key_file 完成，私钥只在本地用于请求签名。
  - 配置区在文件顶端，真实 OCID / fingerprint 一律用占位符，绝不写进代码。

API 私钥（key_file）说明：
  - 私钥文件是 OCI 控制台生成的 .pem 文件，下载后放到服务器上，例如
    /root/1.pem（路径可自定），权限设 600（chmod 600 /root/1.pem）。
  - 获取方法：OCI 控制台 → 右上角用户头像 → My Profile → API Keys →
    Add API Key → Generate API Key Pair → 下载私钥（.pem）与配置文件片段，
    把片段里的 key_file 改成你存放私钥的实际路径。
  - 也可以用命令生成：openssl genrsa -out /root/1.pem 2048，再把公钥
    （openssl rsa -pubout -in /root/1.pem）贴到控制台 API Keys 里添加。

用法示例（占位符）：
    from oci.client import OCIClient
    cli = OCIClient(profile="DEFAULT", region="ap-singapore-1")
    instances = cli.run(["compute", "instance", "list",
                         "--compartment-id", "ocid1.tenancy.oc1..xxxxxxxx"])
"""

import json
import os
import shutil
import subprocess

# ================= 配置区（改这里） =================
# OCI CLI executable path, resolved in this order (no code change needed when deploying to a different machine): OCI CLI 可执行文件路径，按以下顺序解析（部署到不同机器时不用改代码）：
#   1. Env var MBOT_OCI_CLI (can be set explicitly at deploy time); 1. 环境变量 MBOT_OCI_CLI（部署时可显式指定）；
#   2. oci on PATH (pip install oci-cli puts it on PATH when installed into a venv); 2. PATH 里的 oci（pip install oci-cli 装进 venv 时就在 PATH 里）；
#   3. Fallback ~/oci-venv/bin/oci (old path of the local dev environment). 3. 回退 ~/oci-venv/bin/oci（本机开发环境的旧路径）。
OCI_CLI = (os.environ.get("MBOT_OCI_CLI")
           or shutil.which("oci")
           or os.path.expanduser("~/oci-venv/bin/oci"))
# OCI config file path: M_Bot-managed OCI config lives in root's home dir (~/oci_config), matching R-Bot's placement habit — config and private keys both under /root, no ~/.oci/ subdirectory. OCI 配置文件路径：M_Bot 管理的 OCI 配置放 root 家目录（~/oci_config），与 R-Bot 的放置习惯一致——配置与私钥都在 /root，不用 ~/.oci/ 子目录。
# Resolution priority (resolve_oci_config_path): 解析优先级（resolve_oci_config_path）：
#   1. Env var MBOT_OCI_CONFIG_FILE (explicit, for deploys/tests); 1. 环境变量 MBOT_OCI_CONFIG_FILE（显式指定，部署/测试用）；
#   2. The OCI CLI's official env var OCI_CLI_CONFIG_FILE (respect existing user settings); 2. OCI CLI 官方环境变量 OCI_CLI_CONFIG_FILE（尊重用户已有设置）；
#   3. ~/oci_config (when it exists); 3. ~/oci_config（存在时用它）；
#   4. Fallback ~/.oci/config (compatibility path for old deploys). 4. 回退 ~/.oci/config（旧部署的兼容路径）。
# On first config upload via the bot, the old file is auto-migrated to ~/oci_config (see tgbot/handlers_client.py append_oci_profile). 首次通过机器人上传配置时，旧文件会自动迁移到 ~/oci_config（见 tgbot/handlers_client.py 的 append_oci_profile）。
OCI_CONFIG_ENV_VAR = "MBOT_OCI_CONFIG_FILE"
OCI_CONFIG_ROOT_PATH = os.path.expanduser("~/oci_config")
OCI_CONFIG_LEGACY_PATH = os.path.expanduser("~/.oci/config")
# Profile name in the OCI config file (build several profiles for multi-account, e.g. DEFAULT / ACCOUNT2). OCI 配置文件里的 profile 名（多账号时建多个 profile，如 DEFAULT / ACCOUNT2）。
DEFAULT_PROFILE = "DEFAULT"
# Default region (e.g. ap-singapore-1 / us-sanjose-1 / us-chicago-1). 默认区域（如 ap-singapore-1 / us-sanjose-1 / us-chicago-1）。
DEFAULT_REGION = "ap-singapore-1"
# Per-command timeout (seconds). launch wants 120+, list/get are usually fine with 60. 单次命令超时（秒）。launch 建议 120 以上，list/get 一般 60 就够。
DEFAULT_TIMEOUT = 120
# OCI proxy: the proxy only affects the OCI CLI subprocess's env vars, never the global environment. Resolution priority: OCI 代理：代理只影响 OCI CLI 子进程的环境变量，不改全局环境。解析优先级：
# 不改全局环境。解析优先级：
#   1. Env var MBOT_OCI_PROXY (e.g. "http://127.0.0.1:7890"); 1. 环境变量 MBOT_OCI_PROXY（如 "http://127.0.0.1:7890"）；
#   2. JSON file (see PROXY_FILE; written by the web settings page /api/settings/oci-proxy), format {"proxy": "http://127.0.0.1:7890"}; an empty proxy string means no proxy. 2. JSON 文件（见 PROXY_FILE，Web 设置页 /api/settings/oci-proxy 写它），格式 {"proxy": "http://127.0.0.1:7890"}，proxy 为空字符串表示不代理。
#   3. No proxy when neither is set. 3. 都没有时不设代理。
# The file path can be overridden with env var MBOT_OCI_PROXY_FILE (for tests). 文件路径可用环境变量 MBOT_OCI_PROXY_FILE 覆盖（测试用）。
PROXY_FILE = os.environ.get(
    "MBOT_OCI_PROXY_FILE",
    os.path.expanduser("~/workspace/rbot-clone/web/data/oci_proxy.json"))
# Allowed protocol prefixes for the proxy address (same as the bot's PROXY_SCHEMES). 代理地址允许的协议前缀（与 tgbot 机器人代理 PROXY_SCHEMES 一致）。
PROXY_SCHEMES = ("http://", "https://", "socks5://")
# =====================================================


def resolve_oci_config_path():
    """返回当前生效的 OCI 配置文件路径（只读解析，绝不写盘）。

    优先级：MBOT_OCI_CONFIG_FILE > OCI_CLI_CONFIG_FILE >
    ~/oci_config（存在时）> ~/.oci/config（存在时）> ~/oci_config
    （两边都不存在时返回新路径，写入方负责创建）。
    """
    explicit = (os.environ.get(OCI_CONFIG_ENV_VAR) or "").strip()
    if explicit:
        return os.path.expanduser(explicit)
    official = (os.environ.get("OCI_CLI_CONFIG_FILE") or "").strip()
    if official:
        return os.path.expanduser(official)
    if os.path.exists(OCI_CONFIG_ROOT_PATH):
        return OCI_CONFIG_ROOT_PATH
    if os.path.exists(OCI_CONFIG_LEGACY_PATH):
        return OCI_CONFIG_LEGACY_PATH
    return OCI_CONFIG_ROOT_PATH


def read_oci_config_profiles(path=None):
    """读 OCI 配置文件，返回 {profile 名: {键: 值}}。

    文件不存在 / 解析失败时返回 {}。只读配置文本，不读 key_file
    指向的私钥文件。注意 configparser 的坑：OCI 配置文件有 [DEFAULT]
    段，必须用 default_section 换名才能正常读。
    """
    import configparser
    target = path or resolve_oci_config_path()
    parser = configparser.ConfigParser(
        default_section="__oci_no_defaults__", interpolation=None)
    try:
        read = parser.read(target, encoding="utf-8")
    except (OSError, configparser.Error):
        return {}
    if not read:
        return {}
    profiles = {}
    for section in parser.sections():
        try:
            profiles[section] = {key: value.strip()
                                 for key, value in parser.items(section)}
        except configparser.Error:
            continue
    return profiles


def get_oci_proxy(proxy_file=None):
    """解析当前生效的 OCI 代理地址；未配置返回 None。

    优先级：环境变量 MBOT_OCI_PROXY > JSON 配置文件。
    配置文件不存在 / 损坏 / proxy 为空时返回 None（不代理）。
    地址不带允许的协议前缀时返回 None（不把非法值传给子进程）。
    """
    proxy = (os.environ.get("MBOT_OCI_PROXY") or "").strip()
    if proxy:
        return proxy if proxy.startswith(PROXY_SCHEMES) else None
    path = proxy_file or PROXY_FILE
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    proxy = (data.get("proxy") or "").strip()
    if not proxy:
        return None
    return proxy if proxy.startswith(PROXY_SCHEMES) else None


def proxy_env(proxy=None, proxy_file=None):
    """返回 OCI CLI 子进程要追加的代理环境变量 dict（未配置时为空）。

    proxy 显式传入时用它；否则调用 get_oci_proxy() 解析。
    大小写两套变量都设（OCI CLI 底层 requests 两套都认）。
    只返回一个 dict，调用方把它合并进子进程 env，不改 os.environ。
    """
    if proxy is None:
        proxy = get_oci_proxy(proxy_file=proxy_file)
    if not proxy:
        return {}
    return {"HTTPS_PROXY": proxy, "HTTP_PROXY": proxy,
            "https_proxy": proxy, "http_proxy": proxy}


class OCIError(Exception):
    """OCI 调用失败（基类）。"""

    def __init__(self, message, raw_output=""):
        super().__init__(message)
        self.raw_output = raw_output


class OutOfCapacityError(OCIError):
    """无容量：Out of host capacity。抢机时遇到这个就继续重试。"""


class RateLimitError(OCIError):
    """429 限流：TooManyRequests。遇到这个要指数退避，不要硬刷。"""


class QuotaExceededError(OCIError):
    """配额不足：LimitExceeded / QuotaExceeded。重试没用，要停下通知用户。"""


class AuthError(OCIError):
    """认证失败：NotAuthenticated / NotAuthorized。检查 OCI 配置与密钥。"""


class NotFoundError(OCIError):
    """资源不存在：404 / NotFound。"""


# Error text -> exception class mapping (checked in order; first match wins). 错误文本 -> 异常类的匹配表（按顺序检查，先匹配的优先）。
# Note: for a missing resource OCI returns "NotAuthorizedOrNotFound" (404), an anti-enumeration design (it does not reveal whether the resource exists). It must sort before "NotAuthorized", or it is misread as an auth failure. 注意：OCI 对不存在的资源返回 "NotAuthorizedOrNotFound"（404），这是防枚举的设计（不透露资源是否存在）。它必须排在 "NotAuthorized" 之前，否则会被误判为认证失败。
_ERROR_PATTERNS = [
    ("Out of host capacity", OutOfCapacityError),
    ("TooManyRequests", RateLimitError),
    ("429", RateLimitError),
    ("LimitExceeded", QuotaExceededError),
    ("QuotaExceeded", QuotaExceededError),
    ("service limits", QuotaExceededError),
    ("NotAuthorizedOrNotFound", NotFoundError),
    ("NotAuthenticated", AuthError),
    ("NotAuthorized", AuthError),
    ("NotFound", NotFoundError),
    ("404", NotFoundError),
]


def classify_error(text):
    """按错误文本返回对应的异常类；没匹配到返回 OCIError。"""
    for pattern, exc_class in _ERROR_PATTERNS:
        if pattern in text:
            return exc_class
    return OCIError


class OCIClient:
    """OCI CLI 的薄封装。每次调用都带 --profile 与 --region。"""

    def __init__(self, profile=DEFAULT_PROFILE, region=DEFAULT_REGION,
                 oci_cli=OCI_CLI, timeout=DEFAULT_TIMEOUT, proxy=None):
        self.profile = profile
        self.region = region
        self.oci_cli = oci_cli
        self.timeout = timeout
        # proxy: an explicitly passed proxy address; None means resolve on every call (env var MBOT_OCI_PROXY or oci_proxy.json) and take effect immediately after being set. proxy：显式传入的代理地址；None 表示每次调用时自动解析（环境变量 MBOT_OCI_PROXY 或 oci_proxy.json），设置后立即生效。
        self.proxy = proxy

    def run_raw(self, args, profile=None, region=None, timeout=None):
        """直接跑一条 OCI CLI 命令，返回 subprocess.CompletedProcess。

        args 里不要带 --profile / --region（本方法自动追加）。
        调用前设 SUPPRESS_LABEL_WARNING=True（压掉 OCI CLI 3.x 的警告）。
        配置文件路径经 OCI_CLI_CONFIG_FILE 环境变量传给子进程
        （resolve_oci_config_path() 解析，~/oci_config 优先，
        旧部署回退 ~/.oci/config）。
        代理（环境变量 / oci_proxy.json）只进子进程 env，不改全局环境。
        不解析输出、不抛异常，由调用方自己判断。
        """
        cmd = [self.oci_cli] + list(args) + [
            "--profile", profile or self.profile,
            "--region", region or self.region,
        ]
        env = dict(os.environ, SUPPRESS_LABEL_WARNING="True")
        env["OCI_CLI_CONFIG_FILE"] = resolve_oci_config_path()
        env.update(proxy_env(self.proxy))
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout or self.timeout, env=env)

    def run(self, args, profile=None, region=None, timeout=None):
        """跑一条 OCI CLI 命令，返回解析后的 JSON（dict 或 list）。

        失败时按错误文本抛分类异常（OutOfCapacityError / RateLimitError /
        QuotaExceededError / AuthError / NotFoundError / OCIError）。
        空输出返回 None；非 JSON 输出返回原始字符串。
        """
        r = self.run_raw(args, profile=profile, region=region, timeout=timeout)
        if r.returncode != 0:
            err_text = (r.stdout + r.stderr).strip()
            exc_class = classify_error(err_text)
            raise exc_class(f"oci {' '.join(args[:3])}... 失败：{err_text[:500]}",
                            raw_output=err_text)
        out = r.stdout.strip()
        if not out:
            return None
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return out

    def run_text(self, args, profile=None, region=None, timeout=None):
        """跑一条命令，返回原始 stdout 文本（配合 --raw-output 使用）。

        失败时同样抛分类异常。
        """
        r = self.run_raw(args, profile=profile, region=region, timeout=timeout)
        if r.returncode != 0:
            err_text = (r.stdout + r.stderr).strip()
            exc_class = classify_error(err_text)
            raise exc_class(f"oci {' '.join(args[:3])}... 失败：{err_text[:500]}",
                            raw_output=err_text)
        return r.stdout.strip()
