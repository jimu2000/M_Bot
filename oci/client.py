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
# Project root (this package lives one level under it). 项目根目录
# （本包在它下一层）。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# OCI config file path: M_Bot-managed OCI config lives in the project
# root (oci_config next to the code); /root only keeps the user's own
# .pem private keys (key_file can point anywhere, e.g. /root/1.pem).
# OCI 配置文件路径：M_Bot 管理的 OCI 配置放项目根目录（代码旁边的
# oci_config）；/root 只留用户自己放的 .pem 私钥（key_file 可以指
# 任意路径，如 /root/1.pem）。
# Resolution priority (resolve_oci_config_path): 解析优先级（resolve_oci_config_path）：
#   1. Env var MBOT_OCI_CONFIG_FILE (explicit, for deploys/tests); 1. 环境变量 MBOT_OCI_CONFIG_FILE（显式指定，部署/测试用）；
#   2. The OCI CLI's official env var OCI_CLI_CONFIG_FILE (respect existing user settings); 2. OCI CLI 官方环境变量 OCI_CLI_CONFIG_FILE（尊重用户已有设置）；
#   3. <project root>/oci_config (when it exists); 3. 项目根目录下的 oci_config（存在时用它）；
#   4. ~/oci_config (old deploy path, when it exists); 4. ~/oci_config（旧部署位置，存在时用它）；
#   5. Fallback ~/.oci/config (OCI SDK default, for old deploys). 5. 回退 ~/.oci/config（OCI SDK 默认路径，旧部署兼容）。
# On first config upload via the bot, an old file is auto-migrated to
# <project root>/oci_config (see tgbot/handlers_client.py
# append_oci_profile). 首次通过机器人上传配置时，旧文件会自动迁移到
# 项目根目录的 oci_config（见 tgbot/handlers_client.py 的
# append_oci_profile）。
OCI_CONFIG_ENV_VAR = "MBOT_OCI_CONFIG_FILE"
OCI_CONFIG_ROOT_PATH = os.path.join(_PROJECT_ROOT, "oci_config")
OCI_CONFIG_LEGACY_PATH = os.path.expanduser("~/oci_config")
OCI_CONFIG_SDK_PATH = os.path.expanduser("~/.oci/config")
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
    项目根目录 oci_config（存在时）> ~/oci_config（旧部署位置，存在时）
    > ~/.oci/config（OCI SDK 默认，存在时）> 项目根目录 oci_config
    （都不存在时返回新路径，写入方负责创建）。
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
    if os.path.exists(OCI_CONFIG_SDK_PATH):
        return OCI_CONFIG_SDK_PATH
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
    ("OutOfCapacity", OutOfCapacityError),
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


# ================= OCI Python SDK 直调层 =================
# The hot read paths (instance list, quota, boot volumes, object storage,
# backups, cloud report) call the OCI Python SDK directly instead of
# spawning an OCI CLI subprocess per call. A subprocess costs a fresh
# Python interpreter + CLI startup (~3-5 s per call); parallel menu clicks
# used to push the panel's CPU to 80-100%. SDK calls are in-process HTTP
# requests and stay near idle.
#
# Shadowing problem: this repo has a local top-level package named `oci`
# (this directory), which shadows the real OCI SDK's `oci` package on
# sys.path. _load_sdk() works around it by temporarily swapping
# sys.modules['oci'] out, importing the real SDK from site-packages, then
# restoring the local package. The returned SDK module object is cached
# and used directly (attribute access, no further imports), so the swap
# only happens once per process. Do NOT rename the local oci/ directory.
# =====================================================

import threading

_SDK = None
_SDK_LOCK = threading.Lock()
_SDK_LOAD_ERROR = None


def _import_real_sdk():
    """Import the real OCI Python SDK from site-packages.

    Temporarily removes the local `oci` package (and its submodules)
    from sys.modules and the repo root from sys.path, imports the real
    SDK, then restores everything. Returns the real SDK's top-level
    module. Raises ImportError when the SDK is not installed.
    """
    import sys

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    saved_path = list(sys.path)
    saved_modules = {k: v for k, v in list(sys.modules.items())
                     if k == "oci" or k.startswith("oci.")}
    for key in saved_modules:
        sys.modules.pop(key, None)
    sys.path = [p for p in sys.path
                if os.path.abspath(p or ".") != os.path.abspath(repo_root)]
    try:
        import oci as real_sdk  # noqa: F811 - intentional re-import
        # Import every SDK submodule we use while sys.modules['oci'] is
        # still the real SDK, so their internal `from oci import ...`
        # resolves against the SDK, not the local package.
        import oci.config  # noqa: F401
        import oci.core  # noqa: F401
        import oci.email  # noqa: F401
        import oci.exceptions  # noqa: F401
        import oci.identity  # noqa: F401
        import oci.object_storage  # noqa: F401
        import oci.pagination  # noqa: F401
        import oci.util  # noqa: F401
        return real_sdk
    finally:
        sys.path = saved_path
        sys.modules.update(saved_modules)


def _load_sdk():
    """Return the real OCI Python SDK module (cached, thread-safe).

    Raises ImportError with a clear message when the SDK is not
    installed (pip install oci, or see requirements.txt).
    """
    global _SDK, _SDK_LOAD_ERROR
    if _SDK is not None:
        return _SDK
    if _SDK_LOAD_ERROR is not None:
        raise _SDK_LOAD_ERROR
    with _SDK_LOCK:
        if _SDK is not None:
            return _SDK
        if _SDK_LOAD_ERROR is not None:
            raise _SDK_LOAD_ERROR
        try:
            _SDK = _import_real_sdk()
        except ImportError as exc:
            _SDK_LOAD_ERROR = ImportError(
                "OCI Python SDK 未安装，读接口需要它（pip install oci）。"
                f"原始错误：{exc}")
            raise _SDK_LOAD_ERROR
        return _SDK


def _snake_to_kebab(obj):
    """Recursively convert dict keys from snake_case to kebab-case.

    The SDK's to_dict() returns snake_case keys; the OCI CLI JSON the
    rest of the code expects uses kebab-case (display-name,
    lifecycle-state, shape-config, memory-in-gbs, ...). Values
    (datetimes are already ISO strings from to_dict) pass through
    unchanged.

    Allocation reduction (round 7): a container whose subtree contains
    no snake_case key at all is returned as-is instead of rebuilt
    (e.g. freeform-tags with plain keys, or a dict that is already
    kebab-case). The result is identical to rebuilding; callers only
    ever pass fresh to_dict() output here, so sharing the unchanged
    subtree is safe. Tuples still become lists, as before.
    """
    if isinstance(obj, dict):
        new = {}
        changed = False
        for k, v in obj.items():
            nk = k.replace("_", "-") if "_" in k else k
            nv = _snake_to_kebab(v)
            if nk != k or nv is not v:
                changed = True
            new[nk] = nv
        return new if changed else obj
    if isinstance(obj, list):
        new = [_snake_to_kebab(v) for v in obj]
        if all(nv is v for nv, v in zip(new, obj)):
            return obj
        return new
    if isinstance(obj, tuple):
        return [_snake_to_kebab(v) for v in obj]
    return obj


def _model_to_dict(model):
    """Convert one SDK model object to a CLI-compatible kebab-case dict."""
    sdk = _load_sdk()
    return _snake_to_kebab(sdk.util.to_dict(model))


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
        # SDK service clients cache, keyed by (service, region). SDK 服务
        # 客户端缓存，按（服务，区域）键保存。
        self._sdk_clients = {}
        # Cached Object Storage namespace (per client instance). 对象存储
        # 命名空间缓存（每个客户端实例一份）。
        self._sdk_namespace = None

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

    # ---- OCI Python SDK 直调（热路径读接口） ----
    # These read methods call the OCI Python SDK directly (in-process
    # HTTP) instead of spawning an OCI CLI subprocess. Return values are
    # CLI-compatible kebab-case dicts/lists (same shape as cli.run()
    # returns after JSON parsing, minus the {"data": ...} wrapper), so
    # callers only swap cli.run([...]) for cli.sdk_*() and keep the rest
    # of their logic. Write operations still go through run() (CLI).
    # 这些读方法直接调 OCI Python SDK（进程内 HTTP），不再为每次调用
    # 起 OCI CLI 子进程。返回值是与 CLI 兼容的 kebab-case dict/list
    # （与 cli.run() 解析 JSON 后的形状一致，只是不包 {"data": ...}
    # 那层），调用方只需把 cli.run([...]) 换成 cli.sdk_*()，其余逻辑
    # 不动。写操作仍走 run()（CLI）。

    def _sdk_config_dict(self, region=None):
        """Build an SDK config dict from the same config file + profile.

        Uses resolve_oci_config_path() (the same file the CLI subprocess
        reads) and overrides region with the caller's region, matching
        the --region the CLI path passes.
        """
        sdk = _load_sdk()
        cfg = sdk.config.from_file(
            file_location=resolve_oci_config_path(),
            profile_name=self.profile)
        cfg["region"] = region or self.region
        return cfg

    def _sdk_service_client(self, service, region=None):
        """Return a cached SDK service client.

        service: 'compute' / 'blockstorage' / 'object_storage' /
        'virtual_network' / 'identity' / 'email'.
        The client is built once per (service, region); the private key
        is read by the SDK signer at construction, so caching avoids
        re-reading it on every call. The proxy (explicit self.proxy, else
        MBOT_OCI_PROXY / oci_proxy.json) is re-applied on every use so a
        changed proxy takes effect without a restart; with no proxy
        configured the session's proxies are cleared, letting requests
        fall back to environment proxies (same as the CLI subprocess
        inheriting os.environ).
        """
        sdk = _load_sdk()
        key = (service, region or self.region)
        client = self._sdk_clients.get(key)
        if client is None:
            cfg = self._sdk_config_dict(region)
            if service == "compute":
                client = sdk.core.ComputeClient(cfg)
            elif service == "blockstorage":
                client = sdk.core.BlockstorageClient(cfg)
            elif service == "object_storage":
                client = sdk.object_storage.ObjectStorageClient(cfg)
            elif service == "virtual_network":
                client = sdk.core.VirtualNetworkClient(cfg)
            elif service == "identity":
                client = sdk.identity.IdentityClient(cfg)
            elif service == "email":
                client = sdk.email.EmailClient(cfg)
            else:
                raise ValueError(f"未知的 SDK 服务：{service}")
            self._sdk_clients[key] = client
        proxy = self.proxy if self.proxy is not None else get_oci_proxy()
        if proxy:
            client.base_client.session.proxies = {"http": proxy,
                                                  "https": proxy}
        else:
            client.base_client.session.proxies = {}
        return client

    def _sdk_wrap(self, operation, thunk):
        """Run an SDK call, mapping SDK exceptions to our classes.

        ServiceError -> classify_error on "code message status" text
        (same mapping as the CLI path, so callers see the same
        OutOfCapacityError / RateLimitError / QuotaExceededError /
        AuthError / NotFoundError); config errors -> AuthError;
        network errors -> OCIError.
        """
        sdk = _load_sdk()
        try:
            return thunk()
        except sdk.exceptions.ServiceError as exc:
            code = exc.code or ""
            msg = exc.message or ""
            status = str(exc.status) if exc.status is not None else ""
            text = " ".join(p for p in (code, msg, status) if p)
            exc_class = classify_error(text)
            raise exc_class(f"oci {operation} 失败：{text[:500]}",
                            raw_output=text)
        except (sdk.exceptions.InvalidConfig,
                sdk.exceptions.ProfileNotFound,
                sdk.exceptions.ConfigFileNotFound,
                sdk.exceptions.InvalidPrivateKey,
                sdk.exceptions.InvalidKeyFilePath) as exc:
            raise AuthError(f"oci {operation} 失败：OCI 配置错误：{exc}",
                            raw_output=str(exc))
        except (sdk.exceptions.RequestException,
                sdk.exceptions.ConnectTimeout) as exc:
            raise OCIError(f"oci {operation} 失败：网络错误：{exc}",
                           raw_output=str(exc))

    def _sdk_paginate(self, operation, list_fn, *args, **kwargs):
        """List-all-pages helper (matches CLI list-all behavior).

        Returns the aggregated list of SDK model objects.
        """
        sdk = _load_sdk()
        return self._sdk_wrap(
            operation,
            lambda: sdk.pagination.list_call_get_all_results(
                list_fn, *args, **kwargs).data)

    def sdk_list_instances(self, compartment_id, region=None,
                           display_name=None):
        """列出 compartment 下的实例（SDK 直调）。

        返回 CLI 兼容的 dict 列表（kebab-case，不包 {"data": ...}）。
        display_name 传了则按显示名服务端过滤（抢机防重复建用）。
        """
        client = self._sdk_service_client("compute", region)
        kwargs = {}
        if display_name:
            kwargs["display_name"] = display_name
        items = self._sdk_paginate("compute instance list",
                                   client.list_instances, compartment_id,
                                   **kwargs)
        return [_model_to_dict(item) for item in items]

    def sdk_get_instance(self, instance_id, region=None):
        """查单个实例详情（SDK 直调），返回 CLI 兼容的 dict。"""
        client = self._sdk_service_client("compute", region)
        resp = self._sdk_wrap("compute instance get",
                              lambda: client.get_instance(instance_id))
        return _model_to_dict(resp.data)

    def sdk_get_image(self, image_id, region=None):
        """查单个镜像详情（SDK 直调），返回 CLI 兼容的 dict。"""
        client = self._sdk_service_client("compute", region)
        resp = self._sdk_wrap("compute image get",
                              lambda: client.get_image(image_id))
        return _model_to_dict(resp.data)

    def sdk_list_boot_volumes(self, compartment_id, region=None):
        """列出 compartment 下的启动盘（SDK 直调）。"""
        client = self._sdk_service_client("blockstorage", region)
        # 注意：BlockstorageClient.list_boot_volumes 只有 **kwargs 签名，
        # compartment_id 必须用关键字传，位置传会报 takes 1 positional argument。
        items = self._sdk_paginate("bv boot-volume list",
                                   client.list_boot_volumes,
                                   compartment_id=compartment_id)
        return [_model_to_dict(item) for item in items]

    def sdk_get_boot_volume(self, boot_volume_id, region=None):
        """查单个启动盘详情（SDK 直调），返回 CLI 兼容的 dict。"""
        client = self._sdk_service_client("blockstorage", region)
        resp = self._sdk_wrap("bv boot-volume get",
                              lambda: client.get_boot_volume(
                                  boot_volume_id))
        return _model_to_dict(resp.data)

    def sdk_list_volumes(self, compartment_id, region=None):
        """列出 compartment 下的块存储卷（SDK 直调）。"""
        client = self._sdk_service_client("blockstorage", region)
        # 同 list_boot_volumes：BlockstorageClient.list_volumes 只有 **kwargs 签名。
        items = self._sdk_paginate("bv volume list",
                                   client.list_volumes,
                                   compartment_id=compartment_id)
        return [_model_to_dict(item) for item in items]

    def sdk_list_boot_volume_attachments(self, compartment_id,
                                         availability_domain, region=None):
        """列出启动盘挂载关系（SDK 直调）。

        注意：SDK 方法的真实签名是
        list_boot_volume_attachments(availability_domain,
        compartment_id)，可用域在前、compartment 在后，与本方法的
        参数顺序相反——这里用关键字传参，绝不能按本方法顺序位置传
        （第 2 轮曾按 (compartment_id, availability_domain) 位置传，
        导致可用域拿到租户 OCID、线上孤儿盘查询报 400，mock 测试拦不住）。
        """
        client = self._sdk_service_client("compute", region)
        items = self._sdk_paginate(
            "compute boot-volume-attachment list",
            client.list_boot_volume_attachments,
            availability_domain=availability_domain,
            compartment_id=compartment_id)
        return [_model_to_dict(item) for item in items]

    def sdk_list_boot_volume_backups(self, compartment_id,
                                     boot_volume_id=None, region=None):
        """列出启动卷备份（SDK 直调），按创建时间升序。"""
        client = self._sdk_service_client("blockstorage", region)
        kwargs = {"sort_by": "TIMECREATED", "sort_order": "ASC"}
        if boot_volume_id:
            kwargs["boot_volume_id"] = boot_volume_id
        items = self._sdk_paginate("bv boot-volume-backup list",
                                   client.list_boot_volume_backups,
                                   compartment_id, **kwargs)
        return [_model_to_dict(item) for item in items]

    def sdk_get_boot_volume_backup(self, backup_id, region=None):
        """查单个启动卷备份详情（SDK 直调），返回 CLI 兼容的 dict。"""
        client = self._sdk_service_client("blockstorage", region)
        resp = self._sdk_wrap(
            "bv boot-volume-backup get",
            lambda: client.get_boot_volume_backup(backup_id))
        return _model_to_dict(resp.data)

    def sdk_get_namespace(self, region=None):
        """查当前租户的对象存储命名空间（SDK 直调），返回字符串。"""
        if self._sdk_namespace is not None:
            return self._sdk_namespace
        client = self._sdk_service_client("object_storage", region)
        resp = self._sdk_wrap("os ns get",
                              lambda: client.get_namespace())
        self._sdk_namespace = resp.data
        return self._sdk_namespace

    def sdk_list_buckets(self, compartment_id, namespace, region=None):
        """列出 compartment 下的 Bucket（SDK 直调）。"""
        client = self._sdk_service_client("object_storage", region)
        items = self._sdk_paginate("os bucket list",
                                   client.list_buckets, namespace,
                                   compartment_id)
        return [_model_to_dict(item) for item in items]

    def sdk_list_objects(self, namespace, bucket_name, region=None):
        """列出某 Bucket 下的对象（SDK 直调）。

        注意：分页助手的 list_objects 返回是 ListObjects 模型，
        对象列表在 .data.objects 里。
        """
        client = self._sdk_service_client("object_storage", region)
        sdk = _load_sdk()
        resp = self._sdk_wrap(
            "os object list",
            lambda: sdk.pagination.list_call_get_all_results(
                client.list_objects, namespace, bucket_name))
        return [_model_to_dict(item) for item in resp.data.objects]

    # ---- 第 3 轮：网络 / 控制台 / IAM / 邮件读接口（SDK 直调） ----
    # The SDK has no list_vnics method (the CLI's `compute instance
    # list-vnics` is a composite: list vnic attachments, then get each
    # vnic). sdk_list_vnics reproduces that composite in-process.
    # SDK 里没有 list_vnics 方法（CLI 的 compute instance list-vnics
    # 是组合操作：先列 VNIC 挂载关系，再逐个查 VNIC 详情）。
    # sdk_list_vnics 在进程内复现这个组合。

    def sdk_list_vnic_attachments(self, compartment_id, instance_id=None,
                                  region=None):
        """列出 VNIC 挂载关系（SDK 直调），可按实例过滤。"""
        client = self._sdk_service_client("compute", region)
        kwargs = {}
        if instance_id:
            kwargs["instance_id"] = instance_id
        items = self._sdk_paginate("compute vnic-attachment list",
                                   client.list_vnic_attachments,
                                   compartment_id, **kwargs)
        return [_model_to_dict(item) for item in items]

    def sdk_get_vnic(self, vnic_id, region=None):
        """查单个 VNIC 详情（SDK 直调），返回 CLI 兼容的 dict。"""
        client = self._sdk_service_client("virtual_network", region)
        resp = self._sdk_wrap("network vnic get",
                              lambda: client.get_vnic(vnic_id))
        return _model_to_dict(resp.data)

    def sdk_list_vnics(self, instance_id, compartment_id=None,
                       region=None):
        """列出实例的 VNIC（SDK 直调，复现 CLI list-vnics 的组合）。

        返回 CLI 兼容的 VNIC dict 列表（kebab-case，不包 {"data": ...}）。
        compartment_id 不传时先查实例详情取它的 compartment-id
        （挂载关系列表必须带 compartment）。
        """
        if compartment_id is None:
            instance = self.sdk_get_instance(instance_id, region=region)
            compartment_id = instance.get("compartment-id")
        attachments = self.sdk_list_vnic_attachments(
            compartment_id, instance_id=instance_id, region=region)
        vnics = []
        for att in attachments:
            vnic_id = att.get("vnic-id")
            if vnic_id:
                vnics.append(self.sdk_get_vnic(vnic_id, region=region))
        return vnics

    def sdk_list_public_ips(self, compartment_id, availability_domain=None,
                            region=None):
        """列公网 IP（SDK 直调）。

        注意：SDK 方法的真实签名是
        list_public_ips(scope, compartment_id, ...)，scope 在前且是
        必填位置参数；可用域是关键字参数。可用域不传时用 REGION 作用域
        （只返回区域级保留 IP）；传了用 AVAILABILITY_DOMAIN 作用域
        （含该可用域的临时 IP），与 CLI --scope 的行为一致。
        """
        client = self._sdk_service_client("virtual_network", region)
        if availability_domain:
            scope = "AVAILABILITY_DOMAIN"
            kwargs = {"availability_domain": availability_domain}
        else:
            scope = "REGION"
            kwargs = {}
        items = self._sdk_paginate("network public-ip list",
                                   client.list_public_ips,
                                   scope, compartment_id, **kwargs)
        return [_model_to_dict(item) for item in items]

    def sdk_list_ipv6s(self, vnic_id, region=None):
        """列出某 VNIC 上的 IPv6（SDK 直调）。

        注意：VirtualNetworkClient.list_ipv6s 只有 **kwargs 签名，
        vnic_id 必须用关键字传。
        """
        client = self._sdk_service_client("virtual_network", region)
        items = self._sdk_paginate("network ipv6 list",
                                   client.list_ipv6s, vnic_id=vnic_id)
        return [_model_to_dict(item) for item in items]

    def sdk_list_private_ips(self, vnic_id, region=None):
        """列出某 VNIC 的内网 IP（SDK 直调）。

        注意：同 list_ipv6s，list_private_ips 只有 **kwargs 签名，
        vnic_id 必须用关键字传。
        """
        client = self._sdk_service_client("virtual_network", region)
        items = self._sdk_paginate("network private-ip list",
                                   client.list_private_ips, vnic_id=vnic_id)
        return [_model_to_dict(item) for item in items]

    def sdk_list_instance_console_connections(
            self, compartment_id, instance_id=None, region=None):
        """列出串行控制台连接（SDK 直调），可按实例过滤。"""
        client = self._sdk_service_client("compute", region)
        kwargs = {}
        if instance_id:
            kwargs["instance_id"] = instance_id
        items = self._sdk_paginate(
            "compute instance-console-connection list",
            client.list_instance_console_connections,
            compartment_id, **kwargs)
        return [_model_to_dict(item) for item in items]

    def sdk_get_instance_console_connection(self, connection_id,
                                            region=None):
        """查单个串行控制台连接详情（SDK 直调），返回 CLI 兼容的 dict。"""
        client = self._sdk_service_client("compute", region)
        resp = self._sdk_wrap(
            "compute instance-console-connection get",
            lambda: client.get_instance_console_connection(connection_id))
        return _model_to_dict(resp.data)

    def sdk_list_group_users(self, group_id, region=None):
        """列出某 IAM 组里的用户（SDK 直调）。"""
        client = self._sdk_service_client("identity", region)
        items = self._sdk_paginate("iam group user list",
                                   client.list_users_in_group, group_id)
        return [_model_to_dict(item) for item in items]

    def sdk_list_groups(self, compartment_id, region=None):
        """列出 compartment 下的 IAM 组（SDK 直调）。"""
        client = self._sdk_service_client("identity", region)
        items = self._sdk_paginate("iam group list",
                                   client.list_groups, compartment_id)
        return [_model_to_dict(item) for item in items]

    def sdk_list_api_keys(self, user_id, region=None):
        """列出某用户的 API 密钥（SDK 直调）。"""
        client = self._sdk_service_client("identity", region)
        items = self._sdk_paginate("iam user api-key list",
                                   client.list_api_keys, user_id)
        return [_model_to_dict(item) for item in items]

    def sdk_list_users(self, compartment_id, region=None):
        """列出租户下的 IAM 用户（SDK 直调）。"""
        client = self._sdk_service_client("identity", region)
        items = self._sdk_paginate("iam user list",
                                   client.list_users, compartment_id)
        return [_model_to_dict(item) for item in items]

    def sdk_list_senders(self, compartment_id, region=None):
        """列出 Email Delivery 已批准发件人（SDK 直调）。"""
        client = self._sdk_service_client("email", region)
        items = self._sdk_paginate("email sender list",
                                   client.list_senders, compartment_id)
        return [_model_to_dict(item) for item in items]
