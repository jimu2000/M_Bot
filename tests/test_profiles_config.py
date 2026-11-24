#!/usr/bin/env python3
"""v1.28.0：动态账号表（_build_profiles）测试。

MBOT_COMPARTMENT_<PROFILE> 环境变量注册账号，region 从配置文件读。
bot 端（tgbot/config.py）与网页端（web/config.py）行为一致。
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tgbot import config as bot_config
from web import config as web_config


FAKE_FILE_PROFILES = {
    "DEFAULT": {"region": "ap-singapore-1"},
    "ACCOUNT2": {"region": "us-sanjose-1"},
    "SG1": {"region": "ap-singapore-1"},
    "SG2": {"region": "ap-singapore-1"},
}


class ProfilesTestMixin:
    """两个 config 模块共用的断言（build 函数与默认 compartment 不同）。"""

    module = None  # 子类指定：bot_config 或 web_config

    def _build(self, env, file_profiles=None):
        with patch.dict(os.environ, env, clear=True), \
                patch("oci.client.read_oci_config_profiles",
                      return_value=file_profiles or {}):
            return self.module._build_profiles()

    def test_default_always_present(self):
        profiles = self._build({}, FAKE_FILE_PROFILES)
        self.assertIn("DEFAULT", profiles)
        self.assertEqual(profiles["DEFAULT"]["region"], "ap-singapore-1")

    def test_legacy_accounts_from_env(self):
        profiles = self._build(
            {"MBOT_COMPARTMENT_ACCOUNT2": "ocid1.tenancy.oc1..fake2",
             "MBOT_COMPARTMENT_ACCOUNT3": "ocid1.tenancy.oc1..fake3"},
            FAKE_FILE_PROFILES)
        self.assertEqual(profiles["ACCOUNT2"]["region"], "us-sanjose-1")
        self.assertEqual(profiles["ACCOUNT2"]["compartment_id"],
                         "ocid1.tenancy.oc1..fake2")
        # ACCOUNT3 在文件里没有 region 记录，回退旧表（芝加哥）。
        self.assertEqual(profiles["ACCOUNT3"]["region"], "us-chicago-1")

    def test_new_profiles_from_env_with_file_region(self):
        profiles = self._build(
            {"MBOT_COMPARTMENT_SG1": "ocid1.tenancy.oc1..fake1",
             "MBOT_COMPARTMENT_SG2": "ocid1.tenancy.oc1..fake2"},
            FAKE_FILE_PROFILES)
        self.assertEqual(profiles["SG1"]["region"], "ap-singapore-1")
        self.assertEqual(profiles["SG1"]["compartment_id"],
                         "ocid1.tenancy.oc1..fake1")
        self.assertIn("SG2", profiles)

    def test_new_profile_without_file_region_falls_back(self):
        profiles = self._build(
            {"MBOT_COMPARTMENT_NEWX": "ocid1.tenancy.oc1..fakex"}, {})
        self.assertEqual(profiles["NEWX"]["region"], "ap-singapore-1")

    def test_empty_compartment_value_ignored(self):
        profiles = self._build({"MBOT_COMPARTMENT_SG1": ""},
                               FAKE_FILE_PROFILES)
        self.assertNotIn("SG1", profiles)

    def test_unregistered_profile_not_included(self):
        """配置文件里有但没配 MBOT_COMPARTMENT_* 的 profile 不进菜单
        （例如只给 R-Bot 用的受限 API profile）。"""
        profiles = self._build({}, FAKE_FILE_PROFILES)
        self.assertNotIn("SG1", profiles)
        self.assertNotIn("SG2", profiles)


class TestBotProfiles(ProfilesTestMixin, unittest.TestCase):
    module = bot_config


class TestWebProfiles(ProfilesTestMixin, unittest.TestCase):
    module = web_config


class DisplayNameTestMixin:
    """同主区多账号时显示名的区分后缀（两个 config 模块一致）。"""

    module = None

    def _profiles(self):
        return {
            "DEFAULT": {"region": "ap-singapore-1",
                        "compartment_id": "ocid1.tenancy.oc1..x"},
            "ACCOUNT2": {"region": "us-sanjose-1",
                         "compartment_id": "ocid1.tenancy.oc1..x"},
            "sg1": {"region": "ap-singapore-1",
                    "compartment_id": "ocid1.tenancy.oc1..x"},
            "sg2": {"region": "ap-singapore-1",
                    "compartment_id": "ocid1.tenancy.oc1..x"},
        }

    def test_first_in_region_keeps_clean_name(self):
        with patch.object(self.module, "PROFILES", self._profiles()):
            self.assertEqual(
                self.module.profile_display_name("DEFAULT"), "新加坡")
            self.assertEqual(
                self.module.profile_display_name("ACCOUNT2"), "圣何塞")

    def test_later_in_region_gets_suffix(self):
        with patch.object(self.module, "PROFILES", self._profiles()):
            self.assertEqual(
                self.module.profile_display_name("sg1"), "新加坡·sg1")
            self.assertEqual(
                self.module.profile_display_name("sg2"), "新加坡·sg2")

    def test_suffix_combines_with_region_count(self):
        with patch.object(self.module, "PROFILES", self._profiles()):
            self.assertEqual(
                self.module.profile_display_name("DEFAULT", 3),
                "新加坡（3 个区）")
            self.assertEqual(
                self.module.profile_display_name("sg1", 2),
                "新加坡·sg1（2 个区）")


class TestBotDisplayNames(DisplayNameTestMixin, unittest.TestCase):
    module = bot_config

    def test_list_variant_suffix(self):
        profiles = {
            "DEFAULT": {"region": "ap-singapore-1",
                        "compartment_id": "ocid1.tenancy.oc1..x"},
            "sg1": {"region": "ap-singapore-1",
                    "compartment_id": "ocid1.tenancy.oc1..x"},
        }
        with patch.object(bot_config, "PROFILES", profiles):
            self.assertEqual(
                bot_config.profile_display_name_list("DEFAULT", 3),
                "新加坡、3 个区")
            self.assertEqual(
                bot_config.profile_display_name_list("sg1"),
                "新加坡·sg1")


class TestWebDisplayNames(DisplayNameTestMixin, unittest.TestCase):
    module = web_config


if __name__ == "__main__":
    unittest.main()
