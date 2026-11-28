#!/usr/bin/env python3
"""mbot_config.py 统一配置文件（纯文本 M_bot_config.conf）的测试。

The unified config is plain text in the original R-Bot
client_config.txt style: oci / cloudflare / bark first, then a
44-# separator, then the reserved sections (username/password,
local_address/model, azure, ssh, solusvm). Placeholders only —
no real credentials anywhere in this file.
"""

import json
import os
import stat
import tempfile
import unittest
from unittest.mock import patch

import mbot_config

FAKE_USER = "ocid1.user.oc1..xxxx"
FAKE_FINGERPRINT = "aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99"
FAKE_TENANCY = "ocid1.tenancy.oc1..xxxx"
FAKE_KEY_FILE = "/root/1.pem"
FAKE_REGION = "ap-singapore-1"

FAKE_PROFILE = {
    "user": FAKE_USER,
    "fingerprint": FAKE_FINGERPRINT,
    "tenancy": FAKE_TENANCY,
    "region": FAKE_REGION,
    "key_file": FAKE_KEY_FILE,
}

FAKE_BARK = {
    "url": "https://api.day.app/xxxx",
    "key": "barkkey123",
    "enabled": True,
    "chat_id": 1234567890,
}

SEPARATOR = "#" * 44

# Full sample in the template layout (profiles deliberately NOT in
# alphabetical order: parse must not depend on order, save sorts them).
# 完整样板（profile 故意不按字母序：解析不依赖顺序，保存时才排好序）。
SAMPLE_TXT = """# M_Bot 统一配置文件 / M_Bot unified config file
# 只放手动管的配置；MBOT_* 那些 bot 自动写的值还留在安装目录下的 env 文件里，不在这里。
# Only manually managed settings live here; the MBOT_* values the bot writes itself stay in the env file in the install directory, not here.

# 在 oci=begin 和 oci=end 之间放入你的 OCI API 配置信息，支持多个配置文件，机器人切换 profile 可更换操作配置
# Put your OCI API config info between oci=begin and oci=end. Multiple profiles supported; switch profiles in the bot to change the active config.
# 每个 profile 五项：user（用户 OCID）、fingerprint（API 密钥指纹）、key_file（API 私钥文件路径）、tenancy（租户 OCID）、region（区域，如 ap-singapore-1）
# Each profile has five fields: user (user OCID), fingerprint (API key fingerprint), key_file (API private key file path), tenancy (tenancy OCID), region (region, e.g. ap-singapore-1).
oci=begin

[DEFAULT]
user=ocid1.user.oc1..xxxx
fingerprint=aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99
tenancy=ocid1.tenancy.oc1..xxxx
region=ap-singapore-1
key_file=/root/.oci/oci_api_key.pem

[ACCOUNT2]
user=ocid1.user.oc1..xxxx
fingerprint=aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99
tenancy=ocid1.tenancy.oc1..xxxx
region=us-sanjose-1
key_file=/root/.oci/oci_api_key.pem

[ACCOUNT3]
user=ocid1.user.oc1..xxxx
fingerprint=aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99
tenancy=ocid1.tenancy.oc1..xxxx
region=us-chicago-1
key_file=/root/.oci/oci_api_key.pem

oci=end


# Cloudflare 功能参数，非必传 / Cloudflare options (optional)
# 非必传 Cloudflare 邮箱 / Optional Cloudflare email
cf_email=user@example.com
# 非必传 Cloudflare Key，在我的个人资料 -> API 令牌 -> API 密钥 -> Global API Key 处获取
# Optional Cloudflare Key: get it from My Profile -> API Tokens -> API Keys -> Global API Key
cf_account_key=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx


# Bark 推送配置 / Bark push config
# url 和 key 从机器人原来的 bark.json 搬过来；enabled 为 true 才推送；chat_id 是你的 Telegram 用户 ID
# url and key are copied from the bot's original bark.json; only pushes when enabled is true; chat_id is your Telegram user ID
bark=begin

url=https://api.day.app/xxxxxxxxxxxxxxxxxxxx
key=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
enabled=true
chat_id=1234567890

bark=end


############################################
# 以下为 M_Bot 暂不支持的配置，预留以后用，不用改
# The sections below are not yet supported by M_Bot; reserved for future use, no changes needed
############################################


# 用户信息 / User info
# M_Bot 不用这一节：M_Bot 用 Telegram Token + 用户 ID 鉴权（在 env 文件里），不是这里的 username/password。
# M_Bot does not use this section: M_Bot authenticates with a Telegram Token + user ID (in the env file), not username/password here.
# 必传 / Required (R-Bot only, M_Bot 不用 / not used by M_Bot)
#username=
#password=


# 非必填 本机 ip 和端口号（进阶玩家选项，可填写域名），不写将自动获取本机 ip 并使用默认端口号 9527（小白用户建议不填），如填写格式为：https://xxx.xx:9527
# Optional: local IP and port (advanced; a domain works too). Leave empty to auto-detect the local IP and use the default port 9527 (recommended for most users). Example: https://xxx.xx:9527
# M_Bot 的面板地址在 env 文件的 MBOT_PANEL_URL 里，这里不用填 / M_Bot panel URL lives in MBOT_PANEL_URL in the env file, not here
#local_address=
# 非必填 url 名称（默认为 address，可在 bot 上修改） / Optional URL name (default is address; changeable in the bot)
#local_url_name=


# 非必填 启动模式，填写 local 为启动本地无公网 IP 模式（只要能联网即可），不填或填其他则启动端口模式
# Optional startup mode: set to local for local mode without a public IP (internet access is enough); leave empty or anything else for port mode
# M_Bot 不用这一项 / M_Bot does not use this option
#model=


# 在 azure=begin 和 azure=end 之间放入你的 Azure API 配置信息，支持多个配置文件，机器人切换 profile 可更换操作配置
# Put your Azure API config info between azure=begin and azure=end. Multiple profiles supported; switch profiles in the bot to change the active config.
# M_Bot 暂不支持 Azure，这里预留 / Azure is not supported by M_Bot yet; reserved here
azure=begin

azure=end

# 在 ssh=begin 和 ssh=end 之间维护 SSH 连接信息，支持多个实例
# Maintain SSH connection info between ssh=begin and ssh=end. Multiple instances supported.
# M_Bot 的 SSH 主机存在面板数据里（web/data/hosts.json），这里预留 / M_Bot SSH hosts live in the panel data (web/data/hosts.json); reserved here
# 示例 / Examples:
#ssh_192.168.1.10=root:MyPassword123
#ssh_129.159.100.50=opc:/root/.ssh/oci_key
ssh=begin

ssh=end

# 在 solusvm=begin 和 solusvm=end 之间维护 SolusVM 客户端配置（示例为 RackNerd）
# Maintain SolusVM client configs between solusvm=begin and solusvm=end (example is RackNerd).
# 每个配置使用独立的 [] 块，必填项：key/hash，其余可选
# Each config uses its own [] block. Required: key/hash; the rest are optional.
# M_Bot 暂不支持 SolusVM，这里预留 / SolusVM is not supported by M_Bot yet; reserved here
solusvm=begin
#[racknerd-1]
#api=https://nerdvm.racknerd.com/api/client/command.php
#key=
#hash=

#[virmach-1]
#api=https://vps.virmach.com/api/client/command.php
#key=
#hash=
solusvm=end
"""


def _sample_config():
    return {
        "oci": {"profiles": {
            "DEFAULT": dict(FAKE_PROFILE),
            "ACCOUNT2": dict(FAKE_PROFILE, region="us-sanjose-1"),
            "ACCOUNT3": dict(FAKE_PROFILE, region="us-chicago-1"),
        }},
        "cloudflare": {"email": "user@example.com",
                       "key": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"},
        "bark": {"url": "https://api.day.app/xxxxxxxxxxxxxxxxxxxx",
                 "key": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
                 "enabled": True, "chat_id": 1234567890},
        "azure": {"profiles": {}, "values": {}},
        "ssh": {"profiles": {}, "values": {}},
        "solusvm": {"profiles": {}, "values": {}},
    }


class MbotConfigTestCase(unittest.TestCase):
    """Base: temp HOME + temp config path, env vars cleaned."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # Real home dir, captured before HOME is patched (used by the
        # isolation regression test to prove the real home is untouched).
        self.real_home = os.path.expanduser("~")
        self.home = os.path.join(self._tmp.name, "home")
        os.makedirs(self.home)
        self.config_path = os.path.join(self._tmp.name, "M_bot_config.conf")
        self._patchers = [
            patch.dict(os.environ, {"HOME": self.home}),
            patch.dict(os.environ, {}, clear=False),
        ]
        for var in ("MBOT_CONFIG_FILE", "MBOT_WEB_ENV_FILE",
                    "CF_API_KEY", "CF_EMAIL", "CF_API_TOKEN",
                    "MBOT_BARK_URL", "MBOT_BARK_KEY"):
            os.environ.pop(var, None)
        for patcher in self._patchers:
            patcher.start()
        self.addCleanup(self._stop_patchers)

    def _stop_patchers(self):
        for patcher in reversed(self._patchers):
            patcher.stop()

    def _write_config(self, text):
        with open(self.config_path, "w", encoding="utf-8") as f:
            f.write(text)
        return self.config_path


class TestResolveConfigPath(MbotConfigTestCase):

    def test_default_is_home_m_bot_config_conf(self):
        """默认路径是家目录下的 M_bot_config.conf（不是项目根目录）。"""
        expected = os.path.join(self.home, "M_bot_config.conf")
        self.assertEqual(mbot_config.resolve_config_path(), expected)

    def test_install_dir_conf_used_when_home_missing(self):
        """旧部署兼容：家目录没有、项目根目录有 conf 时，读项目根目录那份。"""
        install_dir = os.path.join(self._tmp.name, "install")
        os.makedirs(install_dir)
        install_conf = os.path.join(install_dir, "M_bot_config.conf")
        with open(install_conf, "w", encoding="utf-8") as f:
            f.write("oci=begin\noci=end\n")
        with patch.object(mbot_config, "_PROJECT_ROOT", install_dir):
            self.assertEqual(mbot_config.resolve_config_path(),
                             install_conf)

    def test_home_conf_wins_over_install_dir(self):
        """两边都有时家目录优先。"""
        home_conf = os.path.join(self.home, "M_bot_config.conf")
        with open(home_conf, "w", encoding="utf-8") as f:
            f.write("oci=begin\noci=end\n")
        install_dir = os.path.join(self._tmp.name, "install")
        os.makedirs(install_dir)
        install_conf = os.path.join(install_dir, "M_bot_config.conf")
        with open(install_conf, "w", encoding="utf-8") as f:
            f.write("oci=begin\noci=end\n")
        with patch.object(mbot_config, "_PROJECT_ROOT", install_dir):
            self.assertEqual(mbot_config.resolve_config_path(), home_conf)

    def test_mbot_config_file_env_wins(self):
        custom = os.path.join(self._tmp.name, "custom.txt")
        with patch.dict(os.environ, {"MBOT_CONFIG_FILE": custom}):
            self.assertEqual(mbot_config.resolve_config_path(), custom)

    def test_web_env_file_alias(self):
        custom = os.path.join(self._tmp.name, "alias.txt")
        with patch.dict(os.environ, {"MBOT_WEB_ENV_FILE": custom}):
            self.assertEqual(mbot_config.resolve_config_path(), custom)


class TestOciIniPaths(MbotConfigTestCase):
    """OCI INI 路径解析：写目标固定是项目根目录，读时按当前环境找现有的那份。"""

    def test_write_path_is_project_root(self):
        with patch.dict(os.environ, {}, clear=False):
            for var in ("MBOT_OCI_CONFIG_FILE", "OCI_CLI_CONFIG_FILE"):
                os.environ.pop(var, None)
            expected = os.path.join(
                os.path.dirname(os.path.abspath(mbot_config.__file__)),
                "oci_config")
            self.assertEqual(mbot_config._oci_ini_write_path(), expected)

    def test_write_path_env_var_wins(self):
        custom = os.path.join(self._tmp.name, "custom_oci")
        with patch.dict(os.environ, {"MBOT_OCI_CONFIG_FILE": custom}):
            self.assertEqual(mbot_config._oci_ini_write_path(), custom)

    def test_resolve_respects_patched_home(self):
        """读解析按当前 HOME 找：临时家目录里的 oci_config 优先于真实家目录。"""
        fake_ini = os.path.join(self.home, "oci_config")
        with open(fake_ini, "w", encoding="utf-8") as f:
            f.write("[DEFAULT]\n")
        # Point _PROJECT_ROOT at an empty temp dir for this test:
        # a full-suite run can leave a real project-root oci_config
        # behind (module-level bootstrap() writes the generated INI
        # there), and _resolve_oci_ini_path() prefers it over the
        # patched-HOME file. Patching the root away keeps this test
        # independent of suite ordering WITHOUT deleting the file
        # that later tests (quota / read-cache) need to exist for the
        # OCI client to load.
        # 把 _PROJECT_ROOT 指到空临时目录：全量运行时项目根目录可能
        # 留有一个 oci_config（模块级 bootstrap() 生成的 INI），读解
        # 析会优先取它而盖掉打了补丁的 HOME；这样改既让本测试不依赖
        # 套件顺序，又不删掉后面 quota/缓存测试要用的那个文件。
        empty_root = os.path.join(self._tmp.name, "empty_root")
        os.makedirs(empty_root)
        with patch.object(mbot_config, "_PROJECT_ROOT", empty_root):
            self.assertEqual(mbot_config._resolve_oci_ini_path(), fake_ini)

    def test_key_file_arbitrary_path_round_trip(self):
        """key_file 写什么路径就读什么路径：/root/1.pem（用户自己放的
        私钥，不在项目根目录下）解析与生成 oci_config 都原样保留。"""
        text = ("oci=begin\n[DEFAULT]\nuser=ocid1.user.oc1..xxxx\n"
                "fingerprint=aa:bb\ntenancy=ocid1.tenancy.oc1..xxxx\n"
                "region=ap-singapore-1\nkey_file=/root/1.pem\noci=end\n")
        self._write_config(text)
        cfg = mbot_config.load_config(self.config_path)
        self.assertEqual(cfg["oci"]["profiles"]["DEFAULT"]["key_file"],
                         "/root/1.pem")
        ini = os.path.join(self._tmp.name, "oci_config")
        self.assertTrue(mbot_config.write_oci_config(cfg, ini))
        with open(ini, encoding="utf-8") as f:
            self.assertIn("key_file = /root/1.pem", f.read())


class TestLoad(MbotConfigTestCase):

    def test_missing_file_returns_empty_config(self):
        cfg = mbot_config.load_config(self.config_path)
        self.assertEqual(cfg, mbot_config.empty_config())
        self.assertEqual(sorted(cfg.keys()),
                         ["azure", "bark", "cloudflare", "oci", "solusvm",
                          "ssh"])

    def test_parse_full_template(self):
        """解析完整样板：oci 三 profile、cloudflare、bark、预留空节不报错、注释行忽略。"""
        self._write_config(SAMPLE_TXT)
        cfg = mbot_config.load_config(self.config_path)
        self.assertEqual(sorted(cfg["oci"]["profiles"].keys()),
                         ["ACCOUNT2", "ACCOUNT3", "DEFAULT"])
        default = cfg["oci"]["profiles"]["DEFAULT"]
        self.assertEqual(default["user"], FAKE_USER)
        self.assertEqual(default["fingerprint"], FAKE_FINGERPRINT)
        self.assertEqual(default["tenancy"], FAKE_TENANCY)
        self.assertEqual(default["region"], FAKE_REGION)
        self.assertEqual(default["key_file"], "/root/.oci/oci_api_key.pem")
        self.assertEqual(cfg["oci"]["profiles"]["ACCOUNT2"]["region"],
                         "us-sanjose-1")
        self.assertEqual(cfg["cloudflare"],
                         {"email": "user@example.com",
                          "key": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"})
        self.assertEqual(cfg["bark"]["url"],
                         "https://api.day.app/xxxxxxxxxxxxxxxxxxxx")
        self.assertEqual(cfg["bark"]["key"],
                         "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
        self.assertIs(cfg["bark"]["enabled"], True)
        self.assertEqual(cfg["bark"]["chat_id"], 1234567890)
        # Reserved sections parse to empty (no error).
        for name in ("azure", "ssh", "solusvm"):
            self.assertEqual(cfg[name], {"profiles": {}, "values": {}})

    def test_parse_is_order_independent(self):
        """解析时顺序不重要：把 bark 放到 oci 前面，解析结果一样。"""
        scrambled = (
            "bark=begin\nurl=https://api.day.app/xxxx\nkey=k\n"
            "enabled=false\nchat_id=42\nbark=end\n\n"
            "oci=begin\n[DEFAULT]\nuser=ocid1.user.oc1..xxxx\n"
            "fingerprint=aa:bb\ntenancy=ocid1.tenancy.oc1..xxxx\n"
            "region=ap-singapore-1\nkey_file=/root/1.pem\noci=end\n\n"
            "cf_email=a@b.c\ncf_account_key=ck\n")
        self._write_config(scrambled)
        cfg = mbot_config.load_config(self.config_path)
        self.assertEqual(cfg["oci"]["profiles"]["DEFAULT"]["user"],
                         FAKE_USER)
        self.assertEqual(cfg["cloudflare"],
                         {"email": "a@b.c", "key": "ck"})
        self.assertEqual(cfg["bark"]["enabled"], False)
        self.assertEqual(cfg["bark"]["chat_id"], 42)

    def test_enabled_variants(self):
        for text, expected in (("true", True), ("True", True),
                               ("TRUE", True), ("1", True),
                               ("false", False), ("False", False),
                               ("0", False), ("yes", True),
                               ("no", False)):
            self._write_config(
                f"bark=begin\nenabled={text}\nbark=end\n")
            cfg = mbot_config.load_config(self.config_path)
            self.assertIs(cfg["bark"]["enabled"], expected, text)

    def test_reserved_sections_with_content(self):
        """预留节填了内容也能解析：azure/solusvm 是 [小节]，ssh 是平铺 key=value。"""
        text = (
            "oci=begin\noci=end\n\n"
            "azure=begin\n[az-1]\nclient_id=abc\ntenant_id=def\n"
            "azure=end\n\n"
            "ssh=begin\nssh_10.0.0.1=root:secret\n"
            "ssh_10.0.0.2=opc:/root/.ssh/key\nssh=end\n\n"
            "solusvm=begin\n[racknerd-1]\n"
            "api=https://nerdvm.racknerd.com/api/client/command.php\n"
            "key=k1\nhash=h1\nsolusvm=end\n")
        self._write_config(text)
        cfg = mbot_config.load_config(self.config_path)
        self.assertEqual(cfg["azure"]["profiles"],
                         {"az-1": {"client_id": "abc",
                                   "tenant_id": "def"}})
        self.assertEqual(cfg["ssh"]["values"],
                         {"ssh_10.0.0.1": "root:secret",
                          "ssh_10.0.0.2": "opc:/root/.ssh/key"})
        self.assertEqual(cfg["solusvm"]["profiles"],
                         {"racknerd-1": {
                             "api": "https://nerdvm.racknerd.com/api/client/command.php",
                             "key": "k1", "hash": "h1"}})

    def test_commented_out_lines_ignored(self):
        """被注释掉的 #username=、#local_address= 这类行直接忽略，不当成配置。"""
        self._write_config(
            "#username=someone\n#password=secret\n"
            "#local_address=https://203.0.113.4:9527\n#model=local\n"
            "oci=begin\noci=end\n")
        cfg = mbot_config.load_config(self.config_path)
        self.assertNotIn("username", str(cfg))
        self.assertNotIn("local_address", str(cfg))
        self.assertNotIn("model", str(cfg))


class TestSave(MbotConfigTestCase):

    def test_round_trip_and_mode_0600(self):
        cfg = _sample_config()
        path = mbot_config.save_config(cfg, self.config_path)
        self.assertEqual(path, self.config_path)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(mbot_config.load_config(path), cfg)

    def test_round_trip_sample_template(self):
        """样板解析再写回，解析结果不变（写回只重排 profile 顺序）。"""
        self._write_config(SAMPLE_TXT)
        cfg = mbot_config.load_config(self.config_path)
        mbot_config.save_config(cfg, self.config_path)
        self.assertEqual(mbot_config.load_config(self.config_path), cfg)

    def test_layout_order_and_separator(self):
        """写回后顺序对：oci、cloudflare、bark；分隔线在；预留节在分隔线后。"""
        mbot_config.save_config(_sample_config(), self.config_path)
        with open(self.config_path, encoding="utf-8") as f:
            text = f.read()
        order = ["oci=begin", "cf_email=", "bark=begin", SEPARATOR,
                 "#username=", "#local_address=", "#model=",
                 "azure=begin", "ssh=begin", "solusvm=begin"]
        positions = [text.index(marker) for marker in order]
        self.assertEqual(positions, sorted(positions))
        # Exactly two separator lines, each 44 '#' characters.
        self.assertEqual(text.count(SEPARATOR), 2)
        self.assertIn("# 以下为 M_Bot 暂不支持的配置，预留以后用，不用改",
                      text)
        self.assertIn("# The sections below are not yet supported by "
                      "M_Bot; reserved for future use, no changes needed",
                      text)
        # Section positions: the separator sits between bark and username.
        self.assertLess(text.index("bark=end"),
                        text.index(SEPARATOR))
        self.assertLess(text.index(SEPARATOR),
                        text.index("#username="))

    def test_profiles_sorted_and_key_order(self):
        """oci profiles 按字母序；key 按 user/fingerprint/tenancy/region/key_file。"""
        mbot_config.save_config(_sample_config(), self.config_path)
        with open(self.config_path, encoding="utf-8") as f:
            text = f.read()
        self.assertLess(text.index("[ACCOUNT2]"),
                        text.index("[ACCOUNT3]"))
        self.assertLess(text.index("[ACCOUNT3]"),
                        text.index("[DEFAULT]"))
        default = text[text.index("[DEFAULT]"):]
        keys = [line.split("=")[0] for line in default.splitlines()
                if "=" in line and not line.startswith("#")]
        self.assertEqual(keys[:5], ["user", "fingerprint", "tenancy",
                                    "region", "key_file"])

    def test_bark_key_order_and_comments(self):
        """bark 键按 url/key/enabled/chat_id；双语注释还在。"""
        mbot_config.save_config(_sample_config(), self.config_path)
        with open(self.config_path, encoding="utf-8") as f:
            text = f.read()
        bark = text[text.index("bark=begin"):text.index("bark=end")]
        keys = [line.split("=")[0] for line in bark.splitlines()[1:]
                if "=" in line and not line.startswith("#")]
        self.assertEqual(keys, ["url", "key", "enabled", "chat_id"])
        self.assertIn("# M_Bot 统一配置文件 / M_Bot unified config file",
                      text)
        self.assertIn("#ssh_192.168.1.10=root:MyPassword123", text)
        self.assertIn("#[racknerd-1]", text)
        self.assertIn("#[virmach-1]", text)

    def test_reserved_content_preserved(self):
        """预留节填了内容，写回再解析不能丢。"""
        cfg = _sample_config()
        cfg["azure"] = {"profiles": {"az-1": {"client_id": "abc"}},
                        "values": {}}
        cfg["ssh"] = {"profiles": {}, "values": {
            "ssh_10.0.0.1": "root:secret"}}
        cfg["solusvm"] = {"profiles": {
            "racknerd-1": {"api": "https://x.example.com/api",
                           "key": "k1", "hash": "h1"}},
            "values": {}}
        mbot_config.save_config(cfg, self.config_path)
        loaded = mbot_config.load_config(self.config_path)
        self.assertEqual(loaded["azure"], cfg["azure"])
        self.assertEqual(loaded["ssh"], cfg["ssh"])
        self.assertEqual(loaded["solusvm"], cfg["solusvm"])


class TestApplyToEnviron(MbotConfigTestCase):

    def test_maps_cloudflare_and_bark(self):
        cfg = _sample_config()
        cfg["bark"] = dict(FAKE_BARK)
        set_keys = mbot_config.apply_to_environ(cfg)
        self.assertEqual(os.environ.get("CF_EMAIL"), "user@example.com")
        self.assertEqual(os.environ.get("CF_API_KEY"),
                         "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
        self.assertEqual(os.environ.get("MBOT_BARK_URL"),
                         "https://api.day.app/xxxx")
        self.assertEqual(os.environ.get("MBOT_BARK_KEY"), "barkkey123")
        # enabled / chat_id stay in the file only, never in env vars.
        self.assertNotIn("MBOT_BARK_ENABLED", os.environ)
        self.assertNotIn("MBOT_BARK_CHAT_ID", os.environ)
        self.assertIn("CF_EMAIL", set_keys)
        self.assertIn("MBOT_BARK_URL", set_keys)

    def test_existing_env_vars_win(self):
        cfg = _sample_config()
        cfg["bark"] = dict(FAKE_BARK)
        with patch.dict(os.environ, {"CF_API_KEY": "env-wins",
                                     "MBOT_BARK_URL": "env-url"}):
            mbot_config.apply_to_environ(cfg)
            self.assertEqual(os.environ["CF_API_KEY"], "env-wins")
            self.assertEqual(os.environ["MBOT_BARK_URL"], "env-url")
            # Unset ones are still filled from the config.
            self.assertEqual(os.environ.get("CF_EMAIL"),
                             "user@example.com")
            self.assertEqual(os.environ.get("MBOT_BARK_KEY"),
                             "barkkey123")

    def test_oci_profiles_not_in_environ(self):
        mbot_config.apply_to_environ(_sample_config())
        for key in os.environ:
            self.assertFalse(key.startswith("OCID"),
                             f"OCI profile leaked into environ: {key}")


class TestWriteOciConfig(MbotConfigTestCase):

    def test_output_format_and_mode_0600(self):
        cfg = {
            "oci": {"profiles": {
                "DEFAULT": dict(FAKE_PROFILE, extra="",
                                pass_phrase="secret"),
                "ACCOUNT2": {"user": "ocid1.user.oc1..yyyy",
                             "region": "us-sanjose-1"}}},
            "cloudflare": {}, "bark": {},
        }
        ini = os.path.join(self._tmp.name, "oci_config")
        self.assertTrue(mbot_config.write_oci_config(cfg, ini))
        self.assertEqual(stat.S_IMODE(os.stat(ini).st_mode), 0o600)
        with open(ini, encoding="utf-8") as f:
            text = f.read()
        # Profiles sorted, keys in user/fingerprint/key_file/tenancy/region order.
        self.assertLess(text.index("[ACCOUNT2]"), text.index("[DEFAULT]"))
        default = text[text.index("[DEFAULT]"):]
        keys = [line.split(" = ")[0] for line in default.splitlines()
                if " = " in line]
        self.assertEqual(keys[:5], ["user", "fingerprint", "key_file",
                                    "tenancy", "region"])
        # Extra non-empty keys come after, empty ones are skipped.
        self.assertIn("pass_phrase = secret", text)
        self.assertNotIn("extra", text)
        # ACCOUNT2 only has the non-empty keys it was given.
        account2 = text[text.index("[ACCOUNT2]"):text.index("[DEFAULT]")]
        self.assertIn("user = ocid1.user.oc1..yyyy", account2)
        self.assertIn("region = us-sanjose-1", account2)
        self.assertNotIn("fingerprint", account2)


class TestMigrateFromLegacy(MbotConfigTestCase):

    def _write_old_files(self, env_text=None, oci_text=None, bark=None):
        env_path = os.path.join(self._tmp.name, "old.env")
        oci_path = os.path.join(self._tmp.name, "old_oci")
        bark_path = os.path.join(self._tmp.name, "bark.json")
        if env_text is not None:
            with open(env_path, "w", encoding="utf-8") as f:
                f.write(env_text)
        if oci_text is not None:
            with open(oci_path, "w", encoding="utf-8") as f:
                f.write(oci_text)
        if bark is not None:
            with open(bark_path, "w", encoding="utf-8") as f:
                json.dump(bark, f)
        return env_path, oci_path, bark_path

    def test_top_level_sections(self):
        env_path, oci_path, bark_path = self._write_old_files(
            env_text=("MBOT_TELEGRAM_TOKEN=tok123\n"
                      "CF_API_KEY=ck123\nCF_EMAIL=x@y.z\n"),
            oci_text=("[DEFAULT]\nuser = ocid1.user.oc1..xxxx\n"
                      "fingerprint = aa:bb\nkey_file = /root/1.pem\n"
                      "tenancy = ocid1.tenancy.oc1..xxxx\n"
                      "region = ap-singapore-1\n"),
            bark=dict(FAKE_BARK))
        cfg = mbot_config.migrate_from_legacy(
            env_path, oci_path, bark_path=bark_path)
        self.assertEqual(sorted(cfg.keys()),
                         ["azure", "bark", "cloudflare", "oci", "solusvm",
                          "ssh"])
        self.assertNotIn("mbot", cfg)

    def test_mbot_lines_untouched_only_cf_lines_removed(self):
        """迁移后 env 文件的 MBOT_* 行原样保留，只删 CF_API_KEY/CF_EMAIL 两行。"""
        env_text = ("# comment\n"
                    "MBOT_TELEGRAM_TOKEN=tok123\n"
                    "MBOT_WEB_SECRET='a b c'\n"
                    "MBOT_IMAGE_AARCH64=img-a\n"
                    "CF_API_KEY=ck123\n"
                    "CF_EMAIL=x@y.z\n"
                    "CF_API_TOKEN=tok-keep-me\n"
                    "MBOT_WEB_PASSWORD_HASH='$hash$'\n")
        env_path, oci_path, bark_path = self._write_old_files(
            env_text=env_text, oci_text="", bark=None)
        cfg = mbot_config.migrate_from_legacy(
            env_path, oci_path, bark_path=bark_path)
        self.assertEqual(cfg["cloudflare"],
                         {"email": "x@y.z", "key": "ck123"})
        with open(env_path, encoding="utf-8") as f:
            remaining = f.read()
        self.assertIn("MBOT_TELEGRAM_TOKEN=tok123", remaining)
        self.assertIn("MBOT_WEB_SECRET='a b c'", remaining)
        self.assertIn("MBOT_IMAGE_AARCH64=img-a", remaining)
        self.assertIn("MBOT_WEB_PASSWORD_HASH='$hash$'", remaining)
        self.assertIn("# comment", remaining)
        self.assertNotIn("CF_API_KEY", remaining)
        self.assertNotIn("CF_EMAIL", remaining)
        # CF_API_TOKEN has no slot in the txt format: it stays in the
        # env file and keeps working via systemd (see module docstring).
        self.assertIn("CF_API_TOKEN=tok-keep-me", remaining)

    def test_oci_profiles_from_ini(self):
        env_path, oci_path, bark_path = self._write_old_files(
            env_text="",
            oci_text=("[DEFAULT]\nuser = ocid1.user.oc1..xxxx\n"
                      "fingerprint = aa:bb\nkey_file = /root/1.pem\n"
                      "tenancy = ocid1.tenancy.oc1..xxxx\n"
                      "region = ap-singapore-1\n"
                      "[ACCOUNT2]\nuser = ocid1.user.oc1..yyyy\n"
                      "region = us-sanjose-1\n"
                      "[cloudflare]\ncf_key = ocfkey\n"
                      "cf_email = ocf@example.com\n"),
            bark=None)
        cfg = mbot_config.migrate_from_legacy(
            env_path, oci_path, bark_path=bark_path)
        self.assertEqual(sorted(cfg["oci"]["profiles"].keys()),
                         ["ACCOUNT2", "DEFAULT"])
        self.assertEqual(cfg["oci"]["profiles"]["DEFAULT"]["user"],
                         FAKE_USER)
        # [cloudflare] is not an account profile.
        self.assertNotIn("cloudflare", cfg["oci"]["profiles"])
        self.assertNotIn("CLOUDFLARE", cfg["oci"]["profiles"])

    def test_bark_section_from_bark_json(self):
        """bark.json 的 url/key/enabled/chat_id 原样进 bark 节。"""
        env_path, oci_path, bark_path = self._write_old_files(
            env_text="", oci_text="", bark=dict(FAKE_BARK))
        cfg = mbot_config.migrate_from_legacy(
            env_path, oci_path, bark_path=bark_path)
        self.assertEqual(cfg["bark"], FAKE_BARK)

    def test_bark_section_empty_without_bark_json(self):
        env_path, oci_path, bark_path = self._write_old_files(
            env_text="", oci_text="", bark=None)
        cfg = mbot_config.migrate_from_legacy(
            env_path, oci_path, bark_path=bark_path)
        self.assertEqual(cfg["bark"], {})

    def test_missing_files_return_empty(self):
        cfg = mbot_config.migrate_from_legacy(
            os.path.join(self._tmp.name, "no-env"),
            os.path.join(self._tmp.name, "no-oci"),
            bark_path=os.path.join(self._tmp.name, "no-bark"))
        self.assertEqual(cfg, mbot_config.empty_config())


class TestBootstrap(MbotConfigTestCase):

    def test_migrates_legacy_on_first_start(self):
        env_path = os.path.join(self.home, ".rbot-clone.env")
        with open(env_path, "w", encoding="utf-8") as f:
            f.write("MBOT_TELEGRAM_TOKEN=tok123\n"
                    "CF_API_KEY=ck123\nCF_EMAIL=x@y.z\n")
        oci_path = os.path.join(self.home, "oci_config")
        with open(oci_path, "w", encoding="utf-8") as f:
            f.write("[DEFAULT]\nuser = ocid1.user.oc1..xxxx\n"
                    "fingerprint = aa:bb\nkey_file = /root/1.pem\n"
                    "tenancy = ocid1.tenancy.oc1..xxxx\n"
                    "region = ap-singapore-1\n")
        with patch.dict(os.environ, {"MBOT_CONFIG_FILE": self.config_path}):
            cfg = mbot_config.bootstrap(oci_path=oci_path)
            # CF values applied to environ (assert inside the patch:
            # patch.dict restores os.environ on exit).
            self.assertEqual(os.environ.get("CF_API_KEY"), "ck123")
        # Txt file saved with the six sections and the separator.
        self.assertTrue(os.path.exists(self.config_path))
        saved = mbot_config.load_config(self.config_path)
        self.assertEqual(sorted(saved.keys()),
                         ["azure", "bark", "cloudflare", "oci", "solusvm",
                          "ssh"])
        self.assertEqual(saved["cloudflare"],
                         {"email": "x@y.z", "key": "ck123"})
        with open(self.config_path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn(SEPARATOR, text)
        self.assertLess(text.index("oci=begin"),
                        text.index(SEPARATOR))
        # CF lines removed from the env file, MBOT_* kept.
        with open(env_path, encoding="utf-8") as f:
            remaining = f.read()
        self.assertIn("MBOT_TELEGRAM_TOKEN=tok123", remaining)
        self.assertNotIn("CF_API_KEY", remaining)
        # OCI INI regenerated.
        with open(oci_path, encoding="utf-8") as f:
            self.assertIn("[DEFAULT]", f.read())
        self.assertEqual(cfg, saved)

    def test_noop_when_txt_exists(self):
        mbot_config.save_config(_sample_config(), self.config_path)
        ini_path = os.path.join(self._tmp.name, "oci_config")
        with patch.dict(os.environ, {"MBOT_CONFIG_FILE": self.config_path}):
            cfg = mbot_config.bootstrap(oci_path=ini_path)
        self.assertEqual(cfg, _sample_config())
        # INI regenerated into the explicit path, never the real home config.
        self.assertTrue(os.path.exists(ini_path))
        with open(ini_path, encoding="utf-8") as f:
            self.assertIn("[DEFAULT]", f.read())


    def test_bootstrap_never_writes_real_home_ini(self):
        """回归（2026-01-03）：bootstrap 绝不把假数据写进真实家目录的
        ~/.oci/config；生成的 INI 只写到显式指定的路径。"""
        real_oci = os.path.join(self.real_home, ".oci", "config")
        before = None
        if os.path.exists(real_oci):
            with open(real_oci, encoding="utf-8") as f:
                before = f.read()
        env_path = os.path.join(self.home, ".rbot-clone.env")
        with open(env_path, "w", encoding="utf-8") as f:
            f.write("CF_API_KEY=ck123\nCF_EMAIL=x@y.z\n")
        tmp_ini = os.path.join(self._tmp.name, "generated_oci_config")
        with open(tmp_ini, "w", encoding="utf-8") as f:
            f.write("[DEFAULT]\nuser = ocid1.user.oc1..xxxx\n"
                    "fingerprint = aa:bb\nkey_file = /root/1.pem\n"
                    "tenancy = ocid1.tenancy.oc1..xxxx\n"
                    "region = ap-singapore-1\n")
        with patch.dict(os.environ, {"MBOT_CONFIG_FILE": self.config_path,
                                     "MBOT_OCI_CONFIG_FILE": tmp_ini}):
            cfg = mbot_config.bootstrap()
        self.assertEqual(cfg["cloudflare"],
                         {"email": "x@y.z", "key": "ck123"})
        with open(tmp_ini, encoding="utf-8") as f:
            self.assertIn("[DEFAULT]", f.read())
        if before is None:
            self.assertFalse(os.path.exists(real_oci))
        else:
            with open(real_oci, encoding="utf-8") as f:
                self.assertEqual(f.read(), before)
        # No config file created in the real home either.
        self.assertFalse(os.path.exists(
            os.path.join(self.real_home, "M_bot_config.conf")))
        self.assertFalse(os.path.exists(
            os.path.join(self.real_home, "M_bot_config.txt")))

    def test_bootstrap_migrates_old_txt_name(self):
        """旧名 M_bot_config.txt（家目录下）存在时，读它另存为 .conf。"""
        old_txt = os.path.join(self.home, "M_bot_config.txt")
        mbot_config.save_config(_sample_config(), old_txt)
        ini_path = os.path.join(self._tmp.name, "oci_config")
        with patch.dict(os.environ, {"MBOT_CONFIG_FILE": self.config_path}):
            cfg = mbot_config.bootstrap(oci_path=ini_path)
        self.assertEqual(cfg, _sample_config())
        self.assertTrue(os.path.exists(self.config_path))
        self.assertEqual(mbot_config.load_config(self.config_path),
                         _sample_config())

    def test_bootstrap_moves_install_dir_conf_home(self):
        """旧部署：conf 还在安装目录时，bootstrap 启动时把它搬到家目录。"""
        install_dir = os.path.join(self._tmp.name, "install")
        os.makedirs(install_dir)
        install_conf = os.path.join(install_dir, "M_bot_config.conf")
        mbot_config.save_config(_sample_config(), install_conf)
        ini_path = os.path.join(self._tmp.name, "oci_config")
        home_conf = os.path.join(self.home, "M_bot_config.conf")
        with patch.object(mbot_config, "_PROJECT_ROOT", install_dir):
            with patch.dict(os.environ, {}, clear=False):
                cfg = mbot_config.bootstrap(oci_path=ini_path)
        self.assertEqual(cfg, _sample_config())
        self.assertTrue(os.path.exists(home_conf))
        self.assertFalse(os.path.exists(install_conf))
        self.assertEqual(mbot_config.load_config(home_conf),
                         _sample_config())

    def test_bootstrap_does_not_move_explicit_config(self):
        """显式配了 MBOT_CONFIG_FILE 时，安装目录的 conf 原地不动。"""
        install_dir = os.path.join(self._tmp.name, "install")
        os.makedirs(install_dir)
        install_conf = os.path.join(install_dir, "M_bot_config.conf")
        mbot_config.save_config(_sample_config(), install_conf)
        ini_path = os.path.join(self._tmp.name, "oci_config")
        with patch.object(mbot_config, "_PROJECT_ROOT", install_dir):
            with patch.dict(os.environ,
                            {"MBOT_CONFIG_FILE": self.config_path}):
                mbot_config.bootstrap(oci_path=ini_path)
        self.assertTrue(os.path.exists(install_conf))
        self.assertFalse(os.path.exists(
            os.path.join(self.home, "M_bot_config.conf")))


if __name__ == "__main__":
    unittest.main()
