#!/usr/bin/env python3
"""M_Bot 统一配置文件：纯文本格式（安装目录下的 M_bot_config.conf），照原版 R-Bot
client_config.txt 的样式与节顺序来组织（另加 M_Bot 自己的 bark 节）。

Plain-text unified config (default M_bot_config.conf in the install dir), following
the original R-Bot client_config.txt layout exactly (plus M_Bot's own
bark section and bilingual comments). The env file
(the .rbot-clone.env / .mbot.env in the install dir) stays exactly
as it is: all MBOT_* values live
there, are read by systemd via EnvironmentFile, and are auto-written by
the bot/panel. This file only holds the settings the user edits by hand.

文件排版 / layout（写回时固定按这个顺序；解析时按节名认，不依赖位置）：
    oci、cloudflare、bark（M_Bot 实际用的三节）
    44 个 # 的分隔线 + 两行说明 + 44 个 # 的分隔线
    username/password、local_address/local_url_name/model（注释掉，M_Bot 不用）
    azure、ssh、solusvm（空节，预留以后用）
节的写法照原版：oci/azure/ssh/solusvm/bark 用 =begin/=end 块；
username/password、cf_email/cf_account_key、local_address/
local_url_name/model 是顶层 key=value。# 开头的行是注释（中英双语），
空行忽略。

解析结果 / parsed dict：
    {
      "oci": {"profiles": {"DEFAULT": {"user": ..., "fingerprint": ...,
                                       "tenancy": ..., "region": ...,
                                       "key_file": ...}}},
      "cloudflare": {"email": "...", "key": "..."},
      "bark": {"url": "...", "key": "...", "enabled": True,
               "chat_id": 1234567890},
      "azure": {"profiles": {...}, "values": {...}},
      "ssh": {"profiles": {...}, "values": {...}},
      "solusvm": {"profiles": {...}, "values": {...}},
    }
- oci：3 个账号的 profile（和原版 oci=begin 段的 INI 格式对应；
  [DEFAULT] 用 configparser 读时要把 default_section 换名，否则读不到）。
- cloudflare：{"email", "key"}，对应原版的 cf_email / cf_account_key。
  迁移时从 env 文件的 CF_API_KEY / CF_EMAIL 挪过来（两个地方只留
  一份，放这里），env 里那两行删掉。
- bark：机器人自己的 bark.json 原样搬过来的（url / key / enabled /
  chat_id 四个键）；apply_to_environ 只把 url/key 写进环境变量
  （MBOT_BARK_URL / MBOT_BARK_KEY），enabled 与 chat_id 只放文件里。
  迁移后 bark.json 还可以继续存在（load_bark_config 优先读它），但
  M_bot_config.conf 里的 bark 节才是唯一真实来源；启动时
  apply_to_environ 把 bark 的 url/key 写进环境变量，
  load_bark_config 读不到文件时就用环境变量兜底。
- azure / ssh / solusvm：原版有、M_Bot 暂不用的节；解析时按
  =begin/=end 块 + 块内 key=value 或 [NAME] 小节来读（[NAME] 小节
  进 profiles，块内平铺的 key=value 进 values），代码现在不用它们，
  但解析不能报错；写回时原样保留（用户以后填了不能丢）。
- username/password、local_address、local_url_name、model：原版有、
  M_Bot 不用的顶层项；写回时按原样以注释形式保留（空的或注释掉），
  不删掉，保持文件结构和原版一致，以后支持了直接填。解析时不认
  它们（即使被取消注释也不会读进配置）。
- 保存时 oci profiles 按字母序写，key 按 user / fingerprint /
  tenancy / region / key_file 的顺序写（和原版一致）；bark /
  cloudflare 的键按固定顺序写。
- OCI 命令行工具只认 INI 格式：启动时从解析出的 oci profiles 生成
  INI（write_oci_config），OCI CLI 与 OCIClient 仍读 INI 文件，不用改。
- 旧部署自迁移：conf 不存在、旧文件还在时，bootstrap() 用
  migrate_from_legacy() 转成 conf 并保存（CF 两行从 env 里删掉），
  服务不用手动搬配置；旧名 M_bot_config.txt（项目根目录或家目录
  下）存在时也会先读它、另存为 M_bot_config.conf。

注意：CF_API_TOKEN 不进这个文件——txt 格式里没有放 Token 的位置。
env 文件里的 CF_API_TOKEN 行迁移时不动（也不删除），它继续经
systemd EnvironmentFile 生效（clouds/cloudflare.py 优先用 Token）；
删掉它会让只配了 Token 的环境丢掉 Cloudflare 凭据。

安全说明：本模块不读取、不打印、不外传任何私钥与 Token；真实值
只存在部署服务器的配置文件里，绝不写进代码、测试与文档。
"""

import configparser
import json
import logging
import os
import tempfile

logger = logging.getLogger(__name__)

# Project root (this module lives at the repo/install root, so the
# config, the OCI config and the env file all live here; /root only
# keeps the user's own .pem private keys). 项目根目录（本模块就在
# 安装根目录，配置 / OCI 配置 / env 文件都放这里；/root 只留用户
# 自己放的 .pem 私钥）。
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
# Config file path: env var MBOT_CONFIG_FILE wins (MBOT_WEB_ENV_FILE is
# kept as an alias for the old deploy variable name); otherwise the file
# is M_bot_config.conf in the project root (/opt/rbot-clone or /opt/mbot
# on deploy). 配置文件路径：环境变量 MBOT_CONFIG_FILE 优先
# （MBOT_WEB_ENV_FILE 是旧部署变量名的别名，仍认）；否则是项目根目录
# 下的 M_bot_config.conf（部署时是 /opt/rbot-clone 或 /opt/mbot 下
# 的 M_bot_config.conf）。
CONFIG_ENV_VAR = "MBOT_CONFIG_FILE"
CONFIG_ENV_ALIAS = "MBOT_WEB_ENV_FILE"
DEFAULT_CONFIG_NAME = "M_bot_config.conf"

# =begin/=end block names (parsed by section name, order does not matter).
# =begin/=end 块名（解析时按节名认，不依赖位置）。
BLOCK_NAMES = ("oci", "bark", "azure", "ssh", "solusvm")
# Blocks M_Bot does not use yet (parsed, preserved on save, unused by code).
# M_Bot 暂不用的块（解析、写回保留，代码现在不用）。
RESERVED_BLOCKS = ("azure", "ssh", "solusvm")

# Env files scanned by bootstrap() when the conf file does not exist
# yet (the first one found is migrated). The env file now lives in the
# project root (new location first); the old home-dir locations are kept
# for deploys that have not moved it yet. Paths are resolved at call time
# (not import time) so a patched HOME in tests works. env 文件（conf
# 不存在时 bootstrap() 按顺序找，找到的第一个会被迁移）。env 文件现在
# 在项目根目录（新位置在前），旧的家目录位置保留，给还没搬的旧部署用。
# 路径在调用时才解析（不在 import 时定死），测试里 patch HOME 才有效。
def _legacy_env_paths():
    home = os.path.expanduser("~")
    return (os.path.join(_PROJECT_ROOT, ".rbot-clone.env"),
            os.path.join(_PROJECT_ROOT, ".mbot.env"),
            os.path.join(home, ".rbot-clone.env"),
            os.path.join(home, ".mbot.env"))


# Old-name config file locations (before the rename it was
# M_bot_config.txt): one in the project root, one in the home dir.
# bootstrap() reads the first one found and re-saves it as .conf
# (same format, just renamed). 旧名配置文件的位置（改名前叫
# M_bot_config.txt）：项目根目录与家目录各一份。bootstrap() 在
# .conf 不存在时先找它们，找到就读出来另存为 .conf（格式相同，
# 只是改名）。
def _legacy_txt_paths():
    return (os.path.join(_PROJECT_ROOT, "M_bot_config.txt"),
            os.path.expanduser("~/M_bot_config.txt"))

# OCI profile keys in txt write order (same as the original file).
# txt 里写 profile 的键顺序（和原版一致）。
TXT_PROFILE_KEYS = ("user", "fingerprint", "tenancy", "region", "key_file")
# OCI profile keys in INI write order (write_oci_config).
# INI 里写 profile 的键顺序（write_oci_config 用）。
OCI_PROFILE_KEYS = ("user", "fingerprint", "key_file", "tenancy", "region")

# CF env var names in the old env file -> config cloudflare keys.
# NOTE: CF_API_TOKEN is deliberately NOT here — the txt format has no
# slot for a token, so a token line stays in the env file (see the module
# docstring). 旧 env 文件里的 CF 变量名 -> cloudflare 节的键。
# 注意：CF_API_TOKEN 故意不在这里——txt 格式没有放 Token 的位置，
# Token 行留在 env 文件里（见模块说明）。
CF_ENV_TO_JSON = (("CF_API_KEY", "key"),
                  ("CF_EMAIL", "email"))

# Separator between the sections M_Bot uses and the reserved ones
# (44 '#' characters, as in the template). 已用节与预留节之间的
# 分隔线（44 个 #，和样板一致）。
SEPARATOR = "#" * 44

# ---- Fixed comment blocks (exact wording from the template
# ~/workspace/M_bot_config.example.conf; do not reword). ----
# ---- 固定注释块（措辞照样板 ~/workspace/M_bot_config.example.conf，
# 不要改写） ----
HEADER_LINES = [
    "# M_Bot 统一配置文件 / M_Bot unified config file",
    "# 只放手动管的配置；MBOT_* 那些 bot 自动写的值还留在安装目录下的 env 文件里，不在这里。",
    "# Only manually managed settings live here; the MBOT_* values the bot writes itself stay in the env file in the install directory, not here.",
]
OCI_COMMENT_LINES = [
    "# 在 oci=begin 和 oci=end 之间放入你的 OCI API 配置信息，支持多个配置文件，机器人切换 profile 可更换操作配置",
    "# Put your OCI API config info between oci=begin and oci=end. Multiple profiles supported; switch profiles in the bot to change the active config.",
    "# 每个 profile 五项：user（用户 OCID）、fingerprint（API 密钥指纹）、key_file（API 私钥文件路径）、tenancy（租户 OCID）、region（区域，如 ap-singapore-1）",
    "# Each profile has five fields: user (user OCID), fingerprint (API key fingerprint), key_file (API private key file path), tenancy (tenancy OCID), region (region, e.g. ap-singapore-1).",
]
CF_COMMENT_LINES = [
    "# Cloudflare 功能参数，非必传 / Cloudflare options (optional)",
    "# 非必传 Cloudflare 邮箱 / Optional Cloudflare email",
]
CF_KEY_COMMENT_LINES = [
    "# 非必传 Cloudflare Key，在我的个人资料 -> API 令牌 -> API 密钥 -> Global API Key 处获取",
    "# Optional Cloudflare Key: get it from My Profile -> API Tokens -> API Keys -> Global API Key",
]
BARK_COMMENT_LINES = [
    "# Bark 推送配置 / Bark push config",
    "# url 和 key 从机器人原来的 bark.json 搬过来；enabled 为 true 才推送；chat_id 是你的 Telegram 用户 ID",
    "# url and key are copied from the bot's original bark.json; only pushes when enabled is true; chat_id is your Telegram user ID",
]
SEPARATOR_COMMENT_LINES = [
    "# 以下为 M_Bot 暂不支持的配置，预留以后用，不用改",
    "# The sections below are not yet supported by M_Bot; reserved for future use, no changes needed",
]
USERINFO_LINES = [
    "# 用户信息 / User info",
    "# M_Bot 不用这一节：M_Bot 用 Telegram Token + 用户 ID 鉴权（在 env 文件里），不是这里的 username/password。",
    "# M_Bot does not use this section: M_Bot authenticates with a Telegram Token + user ID (in the env file), not username/password here.",
    "# 必传 / Required (R-Bot only, M_Bot 不用 / not used by M_Bot)",
    "#username=",
    "#password=",
]
LOCAL_LINES = [
    "# 非必填 本机 ip 和端口号（进阶玩家选项，可填写域名），不写将自动获取本机 ip 并使用默认端口号 9527（小白用户建议不填），如填写格式为：https://xxx.xx:9527",
    "# Optional: local IP and port (advanced; a domain works too). Leave empty to auto-detect the local IP and use the default port 9527 (recommended for most users). Example: https://xxx.xx:9527",
    "# M_Bot 的面板地址在 env 文件的 MBOT_PANEL_URL 里，这里不用填 / M_Bot panel URL lives in MBOT_PANEL_URL in the env file, not here",
    "#local_address=",
    "# 非必填 url 名称（默认为 address，可在 bot 上修改） / Optional URL name (default is address; changeable in the bot)",
    "#local_url_name=",
]
MODEL_LINES = [
    "# 非必填 启动模式，填写 local 为启动本地无公网 IP 模式（只要能联网即可），不填或填其他则启动端口模式",
    "# Optional startup mode: set to local for local mode without a public IP (internet access is enough); leave empty or anything else for port mode",
    "# M_Bot 不用这一项 / M_Bot does not use this option",
    "#model=",
]
AZURE_COMMENT_LINES = [
    "# 在 azure=begin 和 azure=end 之间放入你的 Azure API 配置信息，支持多个配置文件，机器人切换 profile 可更换操作配置",
    "# Put your Azure API config info between azure=begin and azure=end. Multiple profiles supported; switch profiles in the bot to change the active config.",
    "# M_Bot 暂不支持 Azure，这里预留 / Azure is not supported by M_Bot yet; reserved here",
]
SSH_COMMENT_LINES = [
    "# 在 ssh=begin 和 ssh=end 之间维护 SSH 连接信息，支持多个实例",
    "# Maintain SSH connection info between ssh=begin and ssh=end. Multiple instances supported.",
    "# M_Bot 的 SSH 主机存在面板数据里（web/data/hosts.json），这里预留 / M_Bot SSH hosts live in the panel data (web/data/hosts.json); reserved here",
    "# 示例 / Examples:",
    "#ssh_192.168.1.10=root:MyPassword123",
    "#ssh_129.159.100.50=opc:/root/.ssh/oci_key",
]
SOLUSVM_COMMENT_LINES = [
    "# 在 solusvm=begin 和 solusvm=end 之间维护 SolusVM 客户端配置（示例为 RackNerd）",
    "# Maintain SolusVM client configs between solusvm=begin and solusvm=end (example is RackNerd).",
    "# 每个配置使用独立的 [] 块，必填项：key/hash，其余可选",
    "# Each config uses its own [] block. Required: key/hash; the rest are optional.",
    "# M_Bot 暂不支持 SolusVM，这里预留 / SolusVM is not supported by M_Bot yet; reserved here",
]
# Commented example blocks inside the solusvm block (kept verbatim).
# solusvm 块里的注释示例（原样保留）。
SOLUSVM_EXAMPLE_LINES = [
    "#[racknerd-1]",
    "#api=https://nerdvm.racknerd.com/api/client/command.php",
    "#key=",
    "#hash=",
    "",
    "#[virmach-1]",
    "#api=https://vps.virmach.com/api/client/command.php",
    "#key=",
    "#hash=",
]


def resolve_config_path():
    """返回当前生效的统一配置文件路径（只解析，不读盘）。

    优先级：MBOT_CONFIG_FILE > MBOT_WEB_ENV_FILE（别名）> 项目根目录下的
    M_bot_config.conf。
    """
    explicit = (os.environ.get(CONFIG_ENV_VAR) or "").strip()
    if explicit:
        return os.path.expanduser(explicit)
    alias = (os.environ.get(CONFIG_ENV_ALIAS) or "").strip()
    if alias:
        return os.path.expanduser(alias)
    return os.path.join(_PROJECT_ROOT, DEFAULT_CONFIG_NAME)


CONFIG_PATH = resolve_config_path()


def empty_config():
    """返回空配置骨架（缺段时补齐用）。"""
    return {
        "oci": {"profiles": {}},
        "cloudflare": {},
        "bark": {},
        "azure": {"profiles": {}, "values": {}},
        "ssh": {"profiles": {}, "values": {}},
        "solusvm": {"profiles": {}, "values": {}},
    }


def _parse_bool(value):
    """把 true/false（大小写不敏感，兼认 1/0、yes/no、on/off）转成 bool。

    认不出的值按 False 处理（不开推送，安全方向）。
    """
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "on"):
        return True
    return False


def _parse_chat_id(value):
    """chat_id 是整数就转 int，否则原样留字符串。"""
    text = str(value).strip()
    try:
        return int(text)
    except (TypeError, ValueError):
        return text


def _parse_block_lines(lines):
    """解析一个 =begin/=end 块里的行（通用解析器）。

    [NAME] 开小节，小节里的 key=value 进 profiles；小节外的
    key=value（平铺，如 ssh 的 ssh_<host>=...）进 values。
    返回 {"profiles": {...}, "values": {...}}。
    """
    profiles = {}
    values = {}
    current = None
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip()
            if name:
                current = name
                profiles.setdefault(current, {})
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if current is not None:
            profiles.setdefault(current, {})[key] = value
        else:
            values[key] = value
    return {"profiles": profiles, "values": values}


def _parse_ini_profiles(lines):
    """用 configparser 解析 INI 格式的 profile 块（oci 块用）。

    [DEFAULT] 段要用 default_section 换名才能读到（configparser 把
    [DEFAULT] 当默认段，不换名会丢）。解析失败时回落到通用解析器
    （重复键等情况下至少不丢全部内容）。返回 {段名: {键: 值}}。
    """
    parser = configparser.ConfigParser(
        default_section="__oci_no_defaults__", interpolation=None)
    try:
        parser.read_string("\n".join(lines))
    except configparser.Error:
        return _parse_block_lines(lines)["profiles"]
    profiles = {}
    for section in parser.sections():
        profiles[section] = {k: v.strip()
                             for k, v in parser.items(section)}
    return profiles


def load_config(path=None):
    """读统一配置文件（纯文本），返回 dict。

    文件不存在返回空配置；缺段时补齐；读失败记一条警告后返回空配置
    （启动不因此崩掉）。# 注释与空行一律忽略；节按名字认，文件里
    的顺序不重要；username/password、local_address、local_url_name、
    model 不读进配置（M_Bot 不用，写回时以注释形式保留）。
    """
    target = path or resolve_config_path()
    cfg = empty_config()
    try:
        with open(target, encoding="utf-8") as f:
            raw_lines = f.read().splitlines()
    except FileNotFoundError:
        return cfg
    except OSError as exc:
        logger.warning("配置文件 %s 读失败（%s），按空配置处理。",
                       target, exc)
        return cfg
    block = None
    block_lines = {name: [] for name in BLOCK_NAMES}
    top_lines = []
    for raw in raw_lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lowered = line.lower()
        matched = False
        for name in BLOCK_NAMES:
            if lowered == f"{name}=begin":
                block = name
                matched = True
                break
            if lowered == f"{name}=end":
                block = None
                matched = True
                break
        if matched:
            continue
        if block is not None:
            block_lines[block].append(line)
        else:
            top_lines.append(line)
    # oci 块：INI profiles（[DEFAULT] 也能读到）。
    if block_lines["oci"]:
        cfg["oci"]["profiles"] = _parse_ini_profiles(block_lines["oci"])
    # cloudflare：顶层的 cf_email / cf_account_key。
    for line in top_lines:
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key == "cf_email" and value:
            cfg["cloudflare"]["email"] = value
        elif key == "cf_account_key" and value:
            cfg["cloudflare"]["key"] = value
    # bark 块：url / key / enabled / chat_id（其余键原样留字符串）。
    for line in block_lines["bark"]:
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key or value == "":
            continue
        if key == "enabled":
            cfg["bark"]["enabled"] = _parse_bool(value)
        elif key == "chat_id":
            cfg["bark"]["chat_id"] = _parse_chat_id(value)
        else:
            cfg["bark"][key] = value
    # 预留块：azure / ssh / solusvm（解析出来，写回时原样保留）。
    for name in RESERVED_BLOCKS:
        if block_lines[name]:
            cfg[name] = _parse_block_lines(block_lines[name])
    return cfg


def _clean_value(value):
    """写回用的值：转字符串、去首尾空白、去掉换行（值里不能有换行）。"""
    return str(value).strip().replace("\r", "").replace("\n", "")


def _profile_block_lines(profiles, key_order=None):
    """把 {段名: {键: 值}} 写成 [NAME] 小节（每段后空一行）。

    段名按字母序；key_order 给定时先按它写（oci 用），其余键按字母序
    排在后面；空值不写。
    """
    lines = []
    for name in sorted(profiles):
        fields = profiles[name] or {}
        lines.append(f"[{name}]")
        written = set()
        for key in (key_order or ()):
            value = fields.get(key)
            if value is not None and str(value).strip() != "":
                lines.append(f"{key}={_clean_value(value)}")
                written.add(key)
        for key in sorted(fields):
            if key in written or key.startswith("_"):
                continue
            value = fields.get(key)
            if value is not None and str(value).strip() != "":
                lines.append(f"{key}={_clean_value(value)}")
        lines.append("")
    return lines


def _flat_block_lines(values):
    """把平铺的 {键: 值} 写成 key=value 行（键按字母序，空值不写）。"""
    lines = []
    for key in sorted(values):
        if key.startswith("_"):
            continue
        value = values.get(key)
        if value is not None and str(value).strip() != "":
            lines.append(f"{key}={_clean_value(value)}")
    return lines


def _bark_block_lines(bark):
    """写 bark 块：bark=begin、空行、url/key/enabled/chat_id（固定顺序，
    其余键按字母序排后面）、空行、bark=end。"""
    lines = ["bark=begin", ""]
    for key in ("url", "key", "enabled", "chat_id"):
        if key not in bark or bark[key] is None \
                or str(bark[key]).strip() == "":
            lines.append(f"{key}=")
        elif key == "enabled":
            lines.append(
                f"enabled={'true' if _parse_bool(bark[key]) else 'false'}")
        else:
            lines.append(f"{key}={_clean_value(bark[key])}")
    for key in sorted(bark):
        if key in ("url", "key", "enabled", "chat_id") \
                or key.startswith("_"):
            continue
        value = bark[key]
        if value is not None and str(value).strip() != "":
            lines.append(f"{key}={_clean_value(value)}")
    lines.append("")
    lines.append("bark=end")
    return lines


def _reserved_section(data):
    """取预留块的 profiles / values（缺键时补空 dict，防旧数据）。"""
    data = data or {}
    if not isinstance(data, dict):
        return {}, {}
    profiles = data.get("profiles") or {}
    values = data.get("values") or {}
    if not isinstance(profiles, dict):
        profiles = {}
    if not isinstance(values, dict):
        values = {}
    return profiles, values


def save_config(config, path=None):
    """把配置按固定排版写回纯文本文件（原子替换，权限 600），返回写入的路径。

    排版（照样板）：oci、cloudflare、bark；44 个 # 的分隔线（夹两行
    说明）；username/password、local_address/local_url_name/model
    （注释掉）；azure、ssh、solusvm（空节预留，解析到的内容原样保留）。
    oci profiles 按字母序，key 按 user / fingerprint / tenancy /
    region / key_file 的顺序写；bark / cloudflare 的键按固定顺序写。
    """
    target = path or resolve_config_path()
    cfg = config or {}
    oci_profiles = (cfg.get("oci") or {}).get("profiles") or {}
    cloudflare = cfg.get("cloudflare") or {}
    bark = cfg.get("bark") or {}
    azure_profiles, azure_values = _reserved_section(cfg.get("azure"))
    ssh_profiles, ssh_values = _reserved_section(cfg.get("ssh"))
    solusvm_profiles, solusvm_values = _reserved_section(cfg.get("solusvm"))

    lines = []
    lines += HEADER_LINES
    lines.append("")
    lines += OCI_COMMENT_LINES
    lines.append("oci=begin")
    lines.append("")
    lines += _profile_block_lines(oci_profiles, TXT_PROFILE_KEYS)
    lines.append("oci=end")
    lines.append("")
    lines.append("")
    lines += CF_COMMENT_LINES
    lines.append(f"cf_email={_clean_value(cloudflare.get('email') or '')}")
    lines += CF_KEY_COMMENT_LINES
    lines.append(
        f"cf_account_key={_clean_value(cloudflare.get('key') or '')}")
    lines.append("")
    lines.append("")
    lines += BARK_COMMENT_LINES
    lines += _bark_block_lines(bark)
    lines.append("")
    lines.append("")
    lines.append(SEPARATOR)
    lines += SEPARATOR_COMMENT_LINES
    lines.append(SEPARATOR)
    lines.append("")
    lines.append("")
    lines += USERINFO_LINES
    lines.append("")
    lines.append("")
    lines += LOCAL_LINES
    lines.append("")
    lines.append("")
    lines += MODEL_LINES
    lines.append("")
    lines.append("")
    lines += AZURE_COMMENT_LINES
    lines.append("azure=begin")
    lines.append("")
    lines += _profile_block_lines(azure_profiles)
    lines += _flat_block_lines(azure_values)
    if azure_values:
        lines.append("")
    lines.append("azure=end")
    lines.append("")
    lines += SSH_COMMENT_LINES
    lines.append("ssh=begin")
    lines.append("")
    lines += _profile_block_lines(ssh_profiles)
    ssh_flat = _flat_block_lines(ssh_values)
    lines += ssh_flat
    if ssh_flat:
        lines.append("")
    lines.append("ssh=end")
    lines.append("")
    lines += SOLUSVM_COMMENT_LINES
    lines.append("solusvm=begin")
    lines += SOLUSVM_EXAMPLE_LINES
    if solusvm_profiles or solusvm_values:
        lines.append("")
        lines += _profile_block_lines(solusvm_profiles)
        solusvm_flat = _flat_block_lines(solusvm_values)
        lines += solusvm_flat
        if solusvm_flat:
            lines.append("")
    lines.append("solusvm=end")

    text = "\n".join(lines) + "\n"
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=parent or ".",
                                    prefix=".mbot-config-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp_path, target)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    os.chmod(target, 0o600)
    return target


def apply_to_environ(config):
    """把配置里 M_Bot 用的三节写进 os.environ（环境变量优先：已设置的键不动）。

    cloudflare.email -> CF_EMAIL，cloudflare.key -> CF_API_KEY；
    bark.url -> MBOT_BARK_URL，bark.key -> MBOT_BARK_KEY（enabled
    与 chat_id 不进环境变量，只放文件里）。oci profiles 不进环境
    变量，只用来生成 INI 文件。返回实际写入的环境变量名列表。
    """
    set_keys = []
    cf_mapping = {"email": "CF_EMAIL", "key": "CF_API_KEY"}
    cloudflare = config.get("cloudflare") or {}
    for cf_key, env_key in cf_mapping.items():
        value = cloudflare.get(cf_key)
        if value and env_key not in os.environ:
            os.environ[env_key] = str(value)
            set_keys.append(env_key)
    bark_mapping = {"url": "MBOT_BARK_URL", "key": "MBOT_BARK_KEY"}
    bark = config.get("bark") or {}
    for bark_key, env_key in bark_mapping.items():
        value = bark.get(bark_key)
        if value and env_key not in os.environ:
            os.environ[env_key] = str(value)
            set_keys.append(env_key)
    return set_keys


def write_oci_config(config, path):
    """从配置的 oci.profiles 生成 OCI INI 配置文件。

    每个 profile 一个 [NAME] 段，键按 user / fingerprint / key_file /
    tenancy / region 的顺序写（只有非空值才写），其余键排在后面。
    原子替换，权限 600。成功返回 True。
    """
    profiles = (config.get("oci") or {}).get("profiles") or {}
    lines = [
        "# M_Bot OCI config (generated from M_bot_config.conf — "
        "edit the conf file, not this file).",
        "# M_Bot OCI 配置（由 M_bot_config.conf 生成，改配置请改 conf，"
        "不要直接改这里）。",
    ]
    for name in sorted(profiles):
        fields = profiles[name] or {}
        lines.append(f"[{name}]")
        written = set()
        for key in OCI_PROFILE_KEYS:
            value = fields.get(key)
            if value is not None and str(value) != "":
                lines.append(f"{key} = {value}")
                written.add(key)
        for key in sorted(fields):
            if key in written:
                continue
            value = fields.get(key)
            if value is not None and str(value) != "":
                lines.append(f"{key} = {value}")
        lines.append("")
    text = "\n".join(lines)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=parent or ".",
                                    prefix=".mbot-oci-config-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    os.chmod(path, 0o600)
    return True


def _unquote_env_value(value):
    """env 文件里的值去引号（单引号转义 '"'"' 还原）。"""
    if len(value) >= 2 and value[0] in ("'", '"') \
            and value[-1] == value[0]:
        quote = value[0]
        inner = value[1:-1]
        if quote == "'":
            inner = inner.replace("'\"'\"'", "'")
        return inner
    return value


def _default_bark_path():
    """默认 bark.json 路径（与 tgbot/bark.py 的 BARK_CONFIG_FILE 一致）。

    数据目录取环境变量 MBOT_TGBOT_DATA_DIR（systemd 已从 env 文件
    读进环境变量），没有时用开发默认值。
    """
    data_dir = (os.environ.get("MBOT_TGBOT_DATA_DIR")
                or "~/workspace/rbot-clone/tgbot/data")
    return os.path.join(os.path.expanduser(data_dir), "bark.json")


def migrate_from_legacy(env_path, oci_path, bark_path=None):
    """把旧配置（env 文件的 CF 两行 + OCI INI + bark.json）转成统一配置 dict。

    只迁移三样：
      - oci profiles：从 oci_config 的 INI 读；
      - cloudflare：从 env 文件的 CF_API_KEY / CF_EMAIL 读，读完从
        env 文件里删掉这两行；
      - bark：从 tgbot 数据目录的 bark.json 读 url / key / enabled /
        chat_id 四个键，原样搬进 bark 节。
    env 文件里的任何 MBOT_* 行都不动（原样保留）；CF_API_TOKEN 行
    也不动（txt 格式没有放 Token 的位置，它留在 env 里继续经
    systemd 生效，见模块说明）。
    其余节（azure / ssh / solusvm）迁移时就是空的。
    注意：本函数会改写 env 文件（删 CF 行），调用方必须把返回的
    dict 保存成 conf，否则 CF 凭据就丢了（env 里已删、conf 里未存）。
    """
    cfg = empty_config()
    if env_path and os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            lines = f.readlines()
        new_lines = []
        changed = False
        for raw in lines:
            stripped = raw.strip()
            key = None
            body = stripped
            if stripped and not stripped.startswith("#"):
                if stripped.startswith("export "):
                    body = stripped[len("export "):].strip()
                if "=" in body:
                    key = body.split("=", 1)[0].strip()
            cf_key = dict(CF_ENV_TO_JSON).get(key) if key else None
            if cf_key is not None:
                value = _unquote_env_value(body.split("=", 1)[1].strip())
                if cf_key not in cfg["cloudflare"]:
                    cfg["cloudflare"][cf_key] = value
                changed = True
                continue
            new_lines.append(raw)
        if changed:
            parent = os.path.dirname(env_path)
            fd, tmp_path = tempfile.mkstemp(
                dir=parent or ".", prefix=".mbot-env-migrate-")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.writelines(new_lines)
                os.replace(tmp_path, env_path)
            except BaseException:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise
            os.chmod(env_path, 0o600)
    if oci_path and os.path.exists(oci_path):
        try:
            with open(oci_path, encoding="utf-8") as f:
                ini_lines = f.read().splitlines()
        except OSError:
            ini_lines = []
        # [cloudflare] 段不是账号 profile：它的 cf_key/cf_email 只在
        # env 里没有 CF 凭据时才补进 cloudflare 节。
        profiles = _parse_ini_profiles(ini_lines)
        for section, items in profiles.items():
            if section.lower() == "cloudflare":
                if items.get("cf_key") \
                        and "key" not in cfg["cloudflare"]:
                    cfg["cloudflare"]["key"] = items["cf_key"]
                if items.get("cf_email") \
                        and "email" not in cfg["cloudflare"]:
                    cfg["cloudflare"]["email"] = items["cf_email"]
                continue
            cfg["oci"]["profiles"][section] = items
    # Bark 配置：机器人自己的 bark.json 是来源，url / key / enabled /
    # chat_id 四个键原样搬进 bark 节（.160 上 url/key 有值、enabled
    # 是 true、chat_id 是用户 Telegram ID）。bark.json 不存在时
    # bark 节留空，等用户以后填。
    resolved_bark_path = bark_path or _default_bark_path()
    if resolved_bark_path and os.path.exists(resolved_bark_path):
        try:
            with open(resolved_bark_path, encoding="utf-8") as f:
                bark_data = json.load(f)
        except (OSError, ValueError):
            bark_data = None
        if isinstance(bark_data, dict):
            for key in ("url", "key", "enabled", "chat_id"):
                if key in bark_data:
                    cfg["bark"][key] = bark_data[key]
    return cfg


def _oci_ini_write_path():
    """OCI INI 的写入目标（bootstrap 生成 INI 写这里，也是机器人同步
    txt 配置的判定标准）。

    优先级：MBOT_OCI_CONFIG_FILE > OCI_CLI_CONFIG_FILE > 项目根目录
    下的 oci_config。写盘目标不依赖“文件存不存在”，只看配置。
    """
    explicit = (os.environ.get("MBOT_OCI_CONFIG_FILE") or "").strip()
    if explicit:
        return os.path.expanduser(explicit)
    official = (os.environ.get("OCI_CLI_CONFIG_FILE") or "").strip()
    if official:
        return os.path.expanduser(official)
    return os.path.join(_PROJECT_ROOT, "oci_config")


def _resolve_oci_ini_path():
    """返回 OCI INI 配置文件的路径（读：找现有的那份；与
    oci.client.resolve_oci_config_path 同一套优先级，但每次调用都按
    当前环境解析）。

    oci.client 的版本用的是 import 时定死的路径，测试里 patch HOME
    后它仍指回真实家目录，bootstrap 写 INI 时会把假数据写进真实
    ~/.oci/config（2026-01-03 全量测试踩过这个坑）。这里改成每次
    都按当前环境解析，测试里 patch HOME 才有效；生产上 HOME 不变，
    结果和 oci.client 的版本一致。

    优先级：MBOT_OCI_CONFIG_FILE > OCI_CLI_CONFIG_FILE >
    项目根目录 oci_config（存在时）> ~/oci_config（旧部署位置，
    存在时）> ~/.oci/config（OCI SDK 默认，存在时）> 项目根目录
    oci_config（都不存在时返回新路径，写入方负责创建）。
    """
    explicit = (os.environ.get("MBOT_OCI_CONFIG_FILE") or "").strip()
    if explicit:
        return os.path.expanduser(explicit)
    official = (os.environ.get("OCI_CLI_CONFIG_FILE") or "").strip()
    if official:
        return os.path.expanduser(official)
    root = os.path.join(_PROJECT_ROOT, "oci_config")
    if os.path.exists(root):
        return root
    legacy = os.path.expanduser("~/oci_config")
    if os.path.exists(legacy):
        return legacy
    sdk = os.path.expanduser("~/.oci/config")
    if os.path.exists(sdk):
        return sdk
    return root


def bootstrap(oci_path=None):
    """启动时加载统一配置文件（幂等，多次调用无害）。

    步骤：读 conf → conf 不存在时先找旧名 M_bot_config.txt（项目
    根目录或家目录），找到就另存为 .conf；再没有就从旧文件迁移
    （env 的 CF 两行 + OCI INI + bark.json，MBOT_* 行原样保留）
    并保存成 conf → 把 cloudflare / bark 写进 os.environ（环境变
    量优先）→ 有 OCI profile 时从解析结果生成 OCI INI（写到项目
    根目录的 oci_config，OCI CLI 与 OCIClient 仍读 INI）。
    任何一步失败只记警告，不让启动崩掉。返回加载到的配置 dict。
    oci_path 显式给出时（测试用），迁移的读源与 INI 的写目标都
    用它；不给时读源按 _resolve_oci_ini_path() 找现有的那份，
    写目标固定是项目根目录的 oci_config。
    """
    target = resolve_config_path()
    cfg = load_config(target)
    if not os.path.exists(target):
        migrated_txt = False
        for old_txt in _legacy_txt_paths():
            if os.path.exists(old_txt):
                try:
                    cfg = load_config(old_txt)
                    save_config(cfg, target)
                    logger.info("旧配置文件 %s 已改名迁移到 %s。",
                                old_txt, target)
                except OSError as exc:
                    logger.warning("旧配置文件 %s 迁移失败（%s），"
                                   "继续用环境变量启动。",
                                   old_txt, exc)
                migrated_txt = True
                break
        if not migrated_txt:
            for legacy in _legacy_env_paths():
                if os.path.exists(legacy):
                    try:
                        migrated = migrate_from_legacy(
                            legacy, _resolve_oci_ini_path())
                        save_config(migrated, target)
                        cfg = migrated
                        logger.info("旧配置 %s 已迁移到 %s。",
                                    legacy, target)
                    except OSError as exc:
                        logger.warning("旧配置 %s 迁移失败（%s），"
                                       "继续用环境变量启动。",
                                       legacy, exc)
                    break
    apply_to_environ(cfg)
    profiles = (cfg.get("oci") or {}).get("profiles") or {}
    if profiles:
        ini_path = oci_path or _oci_ini_write_path()
        try:
            write_oci_config(cfg, ini_path)
        except OSError as exc:
            logger.warning("OCI 配置 %s 生成失败（%s），"
                           "OCI 功能可能读不到账号。",
                           ini_path, exc)
    return cfg
