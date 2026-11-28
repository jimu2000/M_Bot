#!/usr/bin/env python3
"""tgbot/boot_form.py 的单元测试（纯逻辑，不碰 Telegram / OCI）。

覆盖：表单状态机（点选切换/取消选择）、自定义值校验、
形状/系统映射、延迟解析、user-data 生成、root 密码、
表单校验、预设生成、展示与键盘。
"""

import json
import os
import re
import string
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from tgbot import boot_form as bf
from tgbot import config as bot_config

FAKE_IMAGE_AARCH64 = "ocid1.image.oc1.ap-singapore-1.aaa111"
FAKE_IMAGE_X86_64 = "ocid1.image.oc1.ap-singapore-1.xxx222"
FAKE_OL_AARCH64 = "ocid1.image.oc1.ap-singapore-1.ol111"
FAKE_OL_X86_64 = "ocid1.image.oc1.ap-singapore-1.ol222"
FAKE_DEPLOY = {
    "availability_domain": "wgwP:AP-SINGAPORE-1-AD-1",
    "subnet_id": "ocid1.subnet.oc1.ap-singapore-1.ssssss",
    "ssh_keys_file": "/opt/rbot-clone/web/data/ssh_keys.pub",
    "image_aarch64": FAKE_IMAGE_AARCH64,
    "image_x86_64": FAKE_IMAGE_X86_64,
    "ol_aarch64": FAKE_OL_AARCH64,
    "ol_x86_64": FAKE_OL_X86_64,
}


def _filled_form(**overrides):
    """一个填完整的 A1 表单（默认可直接确认），overrides 可改个别字段。"""
    form = bf.new_form()
    form.update({
        "cpu": "arm",
        "os": "ubuntu",
        "ocpus": 1,
        "memory_gb": 6,
        "disk_gb": None,
        "count": 1,
        "delays": [5, 20],
        "ssh_key": "use",
        "root_login": False,
        "public_ip": True,
        "ipv6": False,
        "script": None,
    })
    form.update(overrides)
    return form


def _patched_images(test_case):
    """把 IMAGE_IDS / OL_IMAGE_IDS 换成测试用的假镜像。"""
    test_case.addCleanup(patch.stopall)
    patch("tgbot.boot_form.IMAGE_IDS", {
        "aarch64": FAKE_IMAGE_AARCH64,
        "x86_64": FAKE_IMAGE_X86_64}).start()
    patch("tgbot.boot_form.OL_IMAGE_IDS", {
        "aarch64": FAKE_OL_AARCH64,
        "x86_64": FAKE_OL_X86_64}).start()


class TestNewForm(unittest.TestCase):
    def test_defaults(self):
        form = bf.new_form()
        self.assertIsNone(form["cpu"])
        self.assertIsNone(form["os"])
        self.assertIsNone(form["ocpus"])
        self.assertIsNone(form["memory_gb"])
        self.assertIsNone(form["disk_gb"])
        self.assertEqual(form["count"], 1)
        self.assertEqual(form["delays"], [])
        self.assertEqual(form["ssh_key"], "use")
        self.assertTrue(form["root_login"])  # On by default: a built instance must always allow root login. 默认开：建完实例一定能 root 登录。
        self.assertTrue(form["public_ip"])
        self.assertFalse(form["ipv6"])
        self.assertIsNone(form["script"])

    def test_user_form_defaults(self):
        """用户偏好默认值（2026-01-05 定）：交互式开机表单与一键开机
        用 new_user_form()，延迟 20 秒、IPv6 开、DD 重装开；new_form()
        本身保持中性，不影响预设与抢机。"""
        form = bf.new_user_form()
        self.assertEqual(form["delays"], [20])
        self.assertTrue(form["ipv6"])
        self.assertTrue(form["dd"])
        self.assertTrue(form["root_login"])
        self.assertTrue(form["public_ip"])
        self.assertEqual(form["ssh_key"], "use")
        self.assertIsNone(form["script"])
        # Neutral base is unchanged: presets built from new_form() still default DD off. 中性底子不变：new_form() 建的预设 DD 仍默认关。
        self.assertFalse(bf.new_form()["dd"])
        self.assertFalse(bf.new_form()["ipv6"])
        self.assertEqual(bf.new_form()["delays"], [])


class TestToggleOption(unittest.TestCase):
    def test_single_select_and_deselect(self):
        form = bf.new_form()
        changed, notice = bf.toggle_option(form, "cpu", "arm")
        self.assertTrue(changed)
        self.assertIsNone(notice)
        self.assertEqual(form["cpu"], "arm")
        changed, _ = bf.toggle_option(form, "cpu", "arm")
        self.assertTrue(changed)
        self.assertIsNone(form["cpu"])

    def test_single_select_switch(self):
        form = bf.new_form()
        bf.toggle_option(form, "cpu", "arm")
        bf.toggle_option(form, "cpu", "amd")
        self.assertEqual(form["cpu"], "amd")

    def test_numeric_rows(self):
        form = bf.new_form()
        bf.toggle_option(form, "ocpus", "2")
        self.assertEqual(form["ocpus"], 2)
        bf.toggle_option(form, "mem", "12")
        self.assertEqual(form["memory_gb"], 12)
        bf.toggle_option(form, "count", "3")
        self.assertEqual(form["count"], 3)

    def test_disk_default_toggle(self):
        form = bf.new_form()
        changed, _ = bf.toggle_option(form, "disk", "default")
        self.assertFalse(changed)
        bf.toggle_option(form, "disk", "100")
        self.assertEqual(form["disk_gb"], 100)
        changed, _ = bf.toggle_option(form, "disk", "default")
        self.assertTrue(changed)
        self.assertIsNone(form["disk_gb"])

    def test_binary_rows(self):
        form = bf.new_form()
        changed, _ = bf.toggle_option(form, "key", "use")
        self.assertFalse(changed)
        changed, _ = bf.toggle_option(form, "key", "none")
        self.assertTrue(changed)
        self.assertEqual(form["ssh_key"], "none")
        changed, _ = bf.toggle_option(form, "root", "on")
        self.assertFalse(changed)
        changed, _ = bf.toggle_option(form, "root", "off")
        self.assertTrue(changed)
        self.assertFalse(form["root_login"])
        changed, _ = bf.toggle_option(form, "root", "on")
        self.assertTrue(changed)
        self.assertTrue(form["root_login"])
        changed, _ = bf.toggle_option(form, "pubip", "no")
        self.assertTrue(changed)
        self.assertFalse(form["public_ip"])
        changed, _ = bf.toggle_option(form, "ipv6", "yes")
        self.assertTrue(changed)
        self.assertTrue(form["ipv6"])

    def test_delay_multi_select_max_two(self):
        form = bf.new_form()
        bf.toggle_option(form, "delay", "5")
        bf.toggle_option(form, "delay", "20")
        self.assertEqual(form["delays"], [5, 20])
        changed, notice = bf.toggle_option(form, "delay", "60")
        self.assertFalse(changed)
        self.assertIn("最多选两个", notice)
        self.assertEqual(form["delays"], [5, 20])
        bf.toggle_option(form, "delay", "5")
        changed, _ = bf.toggle_option(form, "delay", "60")
        self.assertTrue(changed)
        self.assertEqual(form["delays"], [20, 60])

    def test_delay_out_of_range(self):
        form = bf.new_form()
        changed, notice = bf.toggle_option(form, "delay", "0")
        self.assertFalse(changed)
        self.assertIn("延迟", notice)
        changed, notice = bf.toggle_option(form, "delay", "99999")
        self.assertFalse(changed)

    def test_script_none_clears(self):
        form = _filled_form()
        bf.set_script(form, "echo hi", name="test.sh")
        changed, _ = bf.toggle_option(form, "script", "none")
        self.assertTrue(changed)
        self.assertIsNone(form["script"])
        changed, _ = bf.toggle_option(form, "script", "none")
        self.assertFalse(changed)

    def test_invalid_row_and_value(self):
        form = bf.new_form()
        changed, notice = bf.toggle_option(form, "nope", "x")
        self.assertFalse(changed)
        self.assertIn("未知", notice)
        changed, _ = bf.toggle_option(form, "cpu", "sparc")
        self.assertFalse(changed)
        self.assertIsNone(form["cpu"])
        changed, _ = bf.toggle_option(form, "ocpus", "abc")
        self.assertFalse(changed)


class TestSetCustom(unittest.TestCase):
    def test_cpu_shape(self):
        form = bf.new_form()
        ok, _ = bf.set_custom(form, "cpu", "VM.Standard.E3.Flex")
        self.assertTrue(ok)
        self.assertEqual(form["cpu"], "VM.Standard.E3.Flex")
        ok, error = bf.set_custom(form, "cpu", "not a shape")
        self.assertFalse(ok)
        self.assertIn("形状名", error)

    def test_os_image_ocid(self):
        form = bf.new_form()
        ocid = "ocid1.image.oc1.ap-singapore-1.abcdefghij1234567890"
        ok, _ = bf.set_custom(form, "os", ocid)
        self.assertTrue(ok)
        self.assertEqual(form["os"], ocid)
        ok, error = bf.set_custom(form, "os", "ocid1.instance.oc1..xxx")
        self.assertFalse(ok)
        self.assertIn("镜像 OCID", error)

    def test_numeric_ranges(self):
        form = bf.new_form()
        ok, _ = bf.set_custom(form, "ocpus", "8")
        self.assertTrue(ok)
        self.assertEqual(form["ocpus"], 8)
        ok, _ = bf.set_custom(form, "ocpus", "0")
        self.assertFalse(ok)
        ok, _ = bf.set_custom(form, "ocpus", "81")
        self.assertFalse(ok)
        ok, _ = bf.set_custom(form, "mem", "64")
        self.assertTrue(ok)
        ok, _ = bf.set_custom(form, "mem", "0")
        self.assertFalse(ok)
        ok, _ = bf.set_custom(form, "disk", "120")
        self.assertTrue(ok)
        ok, _ = bf.set_custom(form, "disk", "46")
        self.assertFalse(ok)
        ok, _ = bf.set_custom(form, "disk", "201")
        self.assertFalse(ok)
        ok, _ = bf.set_custom(form, "count", "5")
        self.assertTrue(ok)
        ok, _ = bf.set_custom(form, "count", "11")
        self.assertFalse(ok)
        ok, _ = bf.set_custom(form, "ocpus", "abc")
        self.assertFalse(ok)

    def test_delay_custom_max_two(self):
        form = bf.new_form()
        ok, _ = bf.set_custom(form, "delay", "7")
        self.assertTrue(ok)
        ok, _ = bf.set_custom(form, "delay", "30")
        self.assertTrue(ok)
        ok, error = bf.set_custom(form, "delay", "90")
        self.assertFalse(ok)
        self.assertIn("最多选两个", error)
        ok, _ = bf.set_custom(form, "delay", "7")
        self.assertTrue(ok)
        self.assertEqual(form["delays"], [7, 30])

    def test_unknown_row(self):
        form = bf.new_form()
        ok, error = bf.set_custom(form, "nope", "1")
        self.assertFalse(ok)
        self.assertIn("未知", error)


class TestResolve(unittest.TestCase):
    def setUp(self):
        _patched_images(self)

    def test_shape_arm_amd_intel_custom(self):
        shape, note = bf.resolve_shape(_filled_form(cpu="arm"))
        self.assertEqual(shape, "VM.Standard.A1.Flex")
        self.assertIsNone(note)
        shape, note = bf.resolve_shape(_filled_form(cpu="amd"))
        self.assertEqual(shape, "VM.Standard.E2.1.Micro")
        self.assertIsNone(note)
        shape, note = bf.resolve_shape(_filled_form(cpu="intel"))
        self.assertEqual(shape, "VM.Standard.E2.1.Micro")
        self.assertIn("Intel", note)
        shape, _ = bf.resolve_shape(_filled_form(cpu="VM.Standard.E3.Flex"))
        self.assertEqual(shape, "VM.Standard.E3.Flex")
        shape, _ = bf.resolve_shape(bf.new_form())
        self.assertIsNone(shape)

    def test_arch(self):
        self.assertEqual(bf.resolve_arch(_filled_form(cpu="arm")), "aarch64")
        self.assertEqual(bf.resolve_arch(_filled_form(cpu="amd")), "x86_64")
        self.assertEqual(bf.resolve_arch(_filled_form(cpu="intel")), "x86_64")
        self.assertEqual(
            bf.resolve_arch(_filled_form(cpu="VM.Standard.E3.Flex")),
            "x86_64")

    def test_image_ubuntu_by_arch(self):
        image, error = bf.resolve_image_id(_filled_form(cpu="arm",
                                                         os="ubuntu"))
        self.assertEqual(image, FAKE_IMAGE_AARCH64)
        self.assertIsNone(error)
        image, _ = bf.resolve_image_id(_filled_form(cpu="amd", os="ubuntu"))
        self.assertEqual(image, FAKE_IMAGE_X86_64)

    def test_image_centos_maps_to_oracle_linux(self):
        image, error = bf.resolve_image_id(_filled_form(cpu="arm",
                                                         os="centos"))
        self.assertEqual(image, FAKE_OL_AARCH64)
        self.assertIsNone(error)
        image, _ = bf.resolve_image_id(_filled_form(cpu="amd", os="centos"))
        self.assertEqual(image, FAKE_OL_X86_64)

    def test_image_placeholder_errors(self):
        with patch("tgbot.boot_form.IMAGE_IDS", {"aarch64": bf.IMAGE_PLACEHOLDER,
                                                 "x86_64": bf.IMAGE_PLACEHOLDER}):
            _, error = bf.resolve_image_id(_filled_form(cpu="arm",
                                                         os="ubuntu"))
            self.assertIn("MBOT_IMAGE_AARCH64", error)
        with patch("tgbot.boot_form.OL_IMAGE_IDS",
                   {"aarch64": bf.IMAGE_PLACEHOLDER,
                    "x86_64": bf.IMAGE_PLACEHOLDER}):
            _, error = bf.resolve_image_id(_filled_form(cpu="amd",
                                                         os="centos"))
            self.assertIn("MBOT_IMAGE_OL_X86_64", error)

    def test_image_custom_ocid(self):
        ocid = "ocid1.image.oc1.ap-singapore-1.custom1234567890"
        image, error = bf.resolve_image_id(_filled_form(os=ocid))
        self.assertEqual(image, ocid)
        self.assertIsNone(error)

    def test_delay_resolution(self):
        self.assertEqual(bf.resolve_delay(_filled_form(delays=[])),
                         (None, None))
        self.assertEqual(bf.resolve_delay(_filled_form(delays=[10])),
                         (10, 10))
        self.assertEqual(bf.resolve_delay(_filled_form(delays=[20, 5])),
                         (5, 20))

    def test_spec_e2_normalizes_to_1_1(self):
        ocpus, mem = bf.resolve_spec(_filled_form(cpu="amd", ocpus=4,
                                                  memory_gb=24))
        self.assertEqual((ocpus, mem), (1, 1))
        ocpus, mem = bf.resolve_spec(_filled_form(cpu="arm", ocpus=2,
                                                  memory_gb=12))
        self.assertEqual((ocpus, mem), (2, 12))


class TestValidateForm(unittest.TestCase):
    def setUp(self):
        _patched_images(self)

    def test_filled_form_passes(self):
        self.assertEqual(bf.validate_form(_filled_form(), FAKE_DEPLOY), [])

    def test_missing_fields(self):
        errors = bf.validate_form(bf.new_form(), FAKE_DEPLOY)
        self.assertIn("未选 CPU 类型", errors)
        self.assertIn("未选系统", errors)
        self.assertIn("未选核心数", errors)
        self.assertIn("未选内存", errors)

    def test_e2_skips_ocpus_memory(self):
        form = _filled_form(cpu="amd", ocpus=None, memory_gb=None)
        self.assertEqual(bf.validate_form(form, FAKE_DEPLOY), [])

    def test_a1_free_tier_cap(self):
        """A1 免费额度内单台上限 4 核 / 24GB，超过报错。"""
        form = _filled_form(cpu="arm", ocpus=8, memory_gb=12)
        errors = bf.validate_form(form, FAKE_DEPLOY)
        self.assertTrue(any("4 核" in e for e in errors))
        form = _filled_form(cpu="arm", ocpus=4, memory_gb=48)
        errors = bf.validate_form(form, FAKE_DEPLOY)
        self.assertTrue(any("24GB" in e for e in errors))
        form = _filled_form(cpu="arm", ocpus=4, memory_gb=24)
        self.assertEqual(bf.validate_form(form, FAKE_DEPLOY), [])

    def test_no_key_requires_root(self):
        form = _filled_form(ssh_key="none", root_login=False)
        errors = bf.validate_form(form, FAKE_DEPLOY)
        self.assertTrue(any("root" in e for e in errors))
        form["root_login"] = True
        self.assertEqual(bf.validate_form(form, FAKE_DEPLOY), [])

    def test_cloud_config_script_conflicts_with_root(self):
        form = _filled_form(root_login=True)
        bf.set_script(form, "#cloud-config\nruncmd:\n  - echo hi",
                      name="cc.yaml")
        errors = bf.validate_form(form, FAKE_DEPLOY)
        self.assertTrue(any("cloud-config" in e for e in errors))

    def test_deploy_placeholders(self):
        empty = {"availability_domain": bf.AD_PLACEHOLDER,
                 "subnet_id": bf.SUBNET_PLACEHOLDER,
                 "ssh_keys_file": bf.SSH_KEYS_PLACEHOLDER}
        errors = bf.validate_form(_filled_form(), empty)
        self.assertIn("未配置可用域（环境变量 MBOT_PLAN_AD）", errors)
        self.assertIn("未配置子网（环境变量 MBOT_PLAN_SUBNET_ID）", errors)
        self.assertIn("未配置 SSH 公钥文件（环境变量 MBOT_PLAN_SSH_KEYS_FILE）",
                      errors)
        form = _filled_form(ssh_key="none", root_login=True)
        errors = bf.validate_form(form, empty)
        self.assertFalse(any("公钥文件" in e for e in errors))

    def test_deploy_region_mismatch(self):
        """部署值与工作区域不一致时报错（跨区开机会报 CannotParseRequest）。"""
        errors = bf.validate_form(_filled_form(), FAKE_DEPLOY,
                                  region="us-sanjose-1",
                                  profile="ACCOUNT2")
        self.assertTrue(any("不一致" in e for e in errors))
        self.assertEqual(bf.validate_form(_filled_form(), FAKE_DEPLOY,
                                          region="ap-singapore-1",
                                          profile="DEFAULT"), [])

    def test_deploy_region_mismatch_skipped_without_region(self):
        """不传 region 时不做一致性校验（旧调用照常通过）。"""
        self.assertEqual(bf.validate_form(_filled_form(), FAKE_DEPLOY), [])


class TestRegionOfAvailabilityDomain(unittest.TestCase):
    def test_parses_region(self):
        cases = {
            "wgwP:AP-SINGAPORE-1-AD-1": "ap-singapore-1",
            "wgwP:AP-SEOUL-1-AD-1": "ap-seoul-1",
            "wgwP:US-SANJOSE-1-AD-1": "us-sanjose-1",
            "wgwP:US-CHICAGO-1-AD-1": "us-chicago-1",
            "xxxx:AP-SINGAPORE-1-AD-1": "ap-singapore-1",
        }
        for ad, region in cases.items():
            self.assertEqual(bf.region_of_availability_domain(ad), region)

    def test_bad_format_returns_none(self):
        for bad in (None, "", "AD-1", "wgwP:", "wgwP:AP-SINGAPORE-1"):
            self.assertIsNone(bf.region_of_availability_domain(bad))


class TestDeployRegionMismatch(unittest.TestCase):
    def test_matching_region_ok(self):
        self.assertIsNone(bf.deploy_region_mismatch(
            FAKE_DEPLOY, "ap-singapore-1", "DEFAULT"))

    def test_mismatch_message(self):
        msg = bf.deploy_region_mismatch(FAKE_DEPLOY, "us-sanjose-1",
                                        "ACCOUNT2")
        self.assertIn("ap-singapore-1", msg)
        self.assertIn("us-sanjose-1", msg)
        self.assertIn("ACCOUNT2/us-sanjose-1", msg)
        self.assertIn("CannotParseRequest", msg)

    def test_no_region_or_ad_skips(self):
        self.assertIsNone(bf.deploy_region_mismatch(FAKE_DEPLOY, None,
                                                    "DEFAULT"))
        self.assertIsNone(bf.deploy_region_mismatch(
            {}, "us-sanjose-1", "ACCOUNT2"))
        self.assertIsNone(bf.deploy_region_mismatch(
            {"availability_domain": "AD-1"}, "us-sanjose-1"))

    def test_count_none_is_error(self):
        form = _filled_form(count=None)
        errors = bf.validate_form(form, FAKE_DEPLOY)
        self.assertIn("未选数量", errors)


class TestBuildPreset(unittest.TestCase):
    def setUp(self):
        _patched_images(self)

    def test_a1_preset_fields(self):
        preset = bf.build_preset(_filled_form(), FAKE_DEPLOY)
        self.assertEqual(preset["shape"], "VM.Standard.A1.Flex")
        self.assertEqual(preset["ocpus"], 1)
        self.assertEqual(preset["memory_gb"], 6)
        self.assertIsNone(preset["target_cpu"])
        self.assertEqual(preset["image_id"], FAKE_IMAGE_AARCH64)
        self.assertEqual(preset["subnet_id"], FAKE_DEPLOY["subnet_id"])
        self.assertEqual(preset["availability_domain"],
                         FAKE_DEPLOY["availability_domain"])
        self.assertEqual(preset["ssh_keys_file"], FAKE_DEPLOY["ssh_keys_file"])
        self.assertEqual(preset["count"], 1)
        self.assertIsNone(preset["boot_volume_size_gbs"])
        self.assertTrue(preset["assign_public_ip"])
        self.assertFalse(preset["assign_ipv6"])
        self.assertIsNone(preset["startup_script"])
        self.assertFalse(preset["root_login"])
        self.assertEqual((preset["min_delay"], preset["max_delay"]), (5, 20))
        self.assertNotIn("user_data", preset)
        self.assertNotIn("root_password", preset)

    def test_e2_preset(self):
        preset = bf.build_preset(_filled_form(cpu="amd", ocpus=4,
                                              memory_gb=24), FAKE_DEPLOY)
        self.assertEqual(preset["shape"], "VM.Standard.E2.1.Micro")
        self.assertEqual((preset["ocpus"], preset["memory_gb"]), (1, 1))
        self.assertEqual(preset["target_cpu"], "7J13")
        self.assertEqual(preset["image_id"], FAKE_IMAGE_X86_64)

    def test_no_key_preset_has_no_ssh_file(self):
        preset = bf.build_preset(_filled_form(ssh_key="none",
                                              root_login=True), FAKE_DEPLOY)
        self.assertIsNone(preset["ssh_keys_file"])
        self.assertTrue(preset["root_login"])

    def test_invalid_raises_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            bf.build_preset(bf.new_form(), FAKE_DEPLOY)
        self.assertIn("未选 CPU 类型", str(ctx.exception))

    def test_display_name_format(self):
        # Free instance: type + Beijing-time date (17:00 UTC is already 01:00 next day). 免费实例：类型 + 北京时间日期（UTC 17:00 已是次日凌晨 1 点）。
        now = datetime(2026, 11, 24, 17, 0, tzinfo=timezone.utc)
        name = bf.generate_display_name("VM.Standard.A1.Flex", 1, 6, now=now)
        self.assertEqual(name, "ARM_2026-01-02")
        name = bf.generate_display_name("VM.Standard.E2.1.Micro", 1, 1, now=now)
        self.assertEqual(name, "AMD_2026-01-02")
        name = bf.generate_display_name("VM.Standard.E3.Flex", 2, 16)
        self.assertRegex(name, r"^bot-e3-flex-2c16g-\d{8}-\d{6}$")


class TestRootPassword(unittest.TestCase):
    def test_uuid_hex_format(self):
        password = bf.generate_root_password()
        self.assertEqual(len(password), 32)
        self.assertRegex(password, r"^[0-9a-f]{32}$")
        # Colons / hashes / quotes / backslashes / whitespace must never appear (YAML and chpasswd safety). 冒号/井号/引号/反斜杠/空白绝不能出现（YAML 与 chpasswd 安全）。
        for bad in ": #\"'\\` \t":
            self.assertNotIn(bad, password)

    def test_random_each_time(self):
        self.assertNotEqual(bf.generate_root_password(),
                            bf.generate_root_password())

    def test_fixed_password_from_env(self):
        # MBOT_FIXED_ROOT_PASSWORD 设置时，所有新机都用它，不再生成 UUID。
        with patch.dict(os.environ,
                        {"MBOT_FIXED_ROOT_PASSWORD": "MyFixedPass1!"}):
            self.assertEqual(bf.generate_root_password(), "MyFixedPass1!")
            self.assertEqual(bf.generate_root_password(), "MyFixedPass1!")

    def test_fixed_password_blank_falls_back_to_uuid(self):
        # 只含空白时视同未设置，回落到 UUID。
        with patch.dict(os.environ,
                        {"MBOT_FIXED_ROOT_PASSWORD": "   "}):
            password = bf.generate_root_password()
            self.assertEqual(len(password), 32)
            self.assertRegex(password, r"^[0-9a-f]{32}$")


class TestBuildUserData(unittest.TestCase):
    def test_none_when_empty(self):
        self.assertIsNone(bf.build_user_data(None, None))
        self.assertIsNone(bf.build_user_data(None, ""))
        self.assertIsNone(bf.build_user_data(None, "   \n"))

    def test_root_only(self):
        data = bf.build_user_data("Ab1!xxxx", None)
        self.assertTrue(data.startswith("#cloud-config"))
        self.assertIn("root:Ab1!xxxx", data)
        self.assertIn("ssh_pwauth: true", data)
        self.assertIn("expire: false", data)
        # Ubuntu images default to disable_root + no root password login, so changing the password alone is not enough; root login must also be allowed. Ubuntu 镜像默认 disable_root + 禁 root 密码登录，只改密码不够，必须同时放行 root 登录。
        self.assertIn("disable_root: false", data)
        self.assertIn("60-mbot-root-login.conf", data)
        self.assertIn("PermitRootLogin yes", data)
        self.assertIn("systemctl reload ssh || systemctl reload sshd",
                      data)

    def test_script_only_passthrough(self):
        data = bf.build_user_data(None, "#!/bin/bash\necho hi")
        self.assertEqual(data, "#!/bin/bash\necho hi")

    def test_script_only_adds_shebang(self):
        data = bf.build_user_data(None, "echo hi")
        self.assertTrue(data.startswith("#!/bin/bash\n"))
        self.assertIn("echo hi", data)

    def test_both_combined(self):
        data = bf.build_user_data("Ab1!xxxx", "echo hi")
        self.assertTrue(data.startswith("#cloud-config"))
        self.assertIn("root:Ab1!xxxx", data)
        self.assertIn("write_files:", data)
        self.assertIn(bf.STARTUP_SCRIPT_PATH, data)
        self.assertIn("runcmd:", data)
        self.assertIn("echo hi", data)
        # Script lines must be indented inside the content block (6 spaces). 脚本行应缩进在 content 块里（6 空格）。
        self.assertIn("      echo hi", data)
        # The password+script branch must also allow root login, and the reload must run before the script. 密码+脚本分支也要放行 root 登录；reload 要在跑脚本之前执行。
        self.assertIn("disable_root: false", data)
        self.assertIn("60-mbot-root-login.conf", data)
        self.assertIn("PermitRootLogin yes", data)
        reload_pos = data.index("systemctl reload ssh")
        script_pos = data.index(bf.STARTUP_SCRIPT_PATH,
                                data.index("runcmd:"))
        self.assertLess(reload_pos, script_pos)

    def test_cloud_config_script_conflicts_with_root(self):
        with self.assertRaises(ValueError):
            bf.build_user_data("Ab1!xxxx",
                               "#cloud-config\nruncmd:\n  - echo hi")
        data = bf.build_user_data(None, "#cloud-config\nruncmd:\n  - echo hi")
        self.assertTrue(data.startswith("#cloud-config"))


class TestFormFromPreset(unittest.TestCase):
    """form_from_preset：预设 -> 表单（编辑用，build_preset 的反向）。"""

    def setUp(self):
        _patched_images(self)

    def test_a1_round_trip(self):
        form = _filled_form()
        preset = bf.build_preset(form, FAKE_DEPLOY)
        back = bf.form_from_preset(preset)
        for key in ("cpu", "os", "ocpus", "memory_gb", "disk_gb", "count",
                    "delays", "ssh_key", "root_login", "public_ip", "ipv6"):
            self.assertEqual(back[key], form[key], key)
        self.assertIsNone(back["script"])
        self.assertEqual(bf.validate_form(back, FAKE_DEPLOY), [])
        rebuilt = bf.build_preset(back, FAKE_DEPLOY)
        self.assertEqual(rebuilt["shape"], preset["shape"])
        self.assertEqual(rebuilt["ocpus"], preset["ocpus"])
        self.assertEqual(rebuilt["memory_gb"], preset["memory_gb"])
        self.assertEqual(rebuilt["image_id"], preset["image_id"])
        self.assertEqual(rebuilt["min_delay"], preset["min_delay"])
        self.assertEqual(rebuilt["max_delay"], preset["max_delay"])

    def test_e2_round_trip(self):
        form = _filled_form(cpu="amd")
        preset = bf.build_preset(form, FAKE_DEPLOY)
        self.assertEqual(preset["shape"], "VM.Standard.E2.1.Micro")
        back = bf.form_from_preset(preset)
        self.assertEqual(back["cpu"], "amd")
        self.assertEqual(back["os"], "ubuntu")
        self.assertIsNone(back["ocpus"])
        self.assertIsNone(back["memory_gb"])
        self.assertEqual(bf.validate_form(back, FAKE_DEPLOY), [])
        rebuilt = bf.build_preset(back, FAKE_DEPLOY)
        self.assertEqual((rebuilt["ocpus"], rebuilt["memory_gb"]), (1, 1))

    def test_old_preset_without_cpu_os(self):
        preset = {"shape": "VM.Standard.A1.Flex", "ocpus": 2,
                  "memory_gb": 12, "image_id": FAKE_OL_AARCH64,
                  "ssh_keys_file": None, "root_login": True,
                  "assign_public_ip": False, "assign_ipv6": True,
                  "count": 2, "min_delay": 30, "max_delay": 30,
                  "startup_script": "echo hi"}
        back = bf.form_from_preset(preset)
        self.assertEqual(back["cpu"], "arm")
        self.assertEqual(back["os"], "centos")
        self.assertEqual(back["ocpus"], 2)
        self.assertEqual(back["memory_gb"], 12)
        self.assertEqual(back["count"], 2)
        self.assertEqual(back["delays"], [30])
        self.assertEqual(back["ssh_key"], "none")
        self.assertTrue(back["root_login"])
        self.assertFalse(back["public_ip"])
        self.assertTrue(back["ipv6"])
        self.assertEqual(back["script"]["content"], "echo hi")

    def test_unknown_image_kept_as_custom(self):
        preset = {"shape": "VM.Standard.E2.1.Micro", "ocpus": 1,
                  "memory_gb": 1,
                  "image_id": "ocid1.image.oc1..custom000",
                  "ssh_keys_file": "/tmp/k.pub"}
        back = bf.form_from_preset(preset)
        self.assertEqual(back["cpu"], "amd")
        self.assertEqual(back["os"], "ocid1.image.oc1..custom000")
        self.assertEqual(bf.validate_form(back, FAKE_DEPLOY), [])


class TestFmtAndKeyboard(unittest.TestCase):
    def test_fmt_form_text_shows_state(self):
        text = bf.fmt_form_text(_filled_form())
        self.assertIn("ARM", text)
        self.assertIn("Ubuntu", text)
        self.assertIn("1 核 / 6GB", text)
        self.assertIn("默认（47GB）", text)
        self.assertIn("区间 5–20 秒随机", text)
        self.assertIn("使用机器人公钥", text)
        text = bf.fmt_form_text(bf.new_form())
        self.assertIn("未选", text)
        self.assertIn("CentOS 已映射为 Oracle Linux", text)

    def test_fmt_form_text_e2_spec(self):
        text = bf.fmt_form_text(_filled_form(cpu="amd", ocpus=None,
                                             memory_gb=None))
        self.assertIn("固定 1 核 / 1GB", text)

    def test_keyboard_rows_and_callbacks(self):
        form = _filled_form()
        kb = bf.boot_form_keyboard(form)
        rows = kb.inline_keyboard
        self.assertEqual(len(rows), 17)
        self.assertEqual(rows[0][0].callback_data, "bf:oneclick:amd")
        callbacks = [b.callback_data for row in rows for b in row]
        self.assertIn("bf:oneclick:amd", callbacks)
        self.assertIn("bf:cpu:arm", callbacks)
        self.assertIn("bf:os:ubuntu", callbacks)
        self.assertIn("bf:delay:200", callbacks)
        self.assertIn("bf:dd:off", callbacks)
        self.assertIn("bf:dd:on", callbacks)
        self.assertIn("bf:confirm", callbacks)
        self.assertIn("bf:save", callbacks)
        self.assertIn("o:menu", callbacks)
        for cb in callbacks:
            self.assertLessEqual(len(cb.encode("utf-8")), 64)
        texts = [b.text for row in rows for b in row]
        self.assertIn("✅ ARM", texts)
        self.assertIn("✅ Ubuntu", texts)
        self.assertIn("✅ 5", texts)
        self.assertIn("✅ 20", texts)

    def test_keyboard_custom_mark(self):
        form = _filled_form(cpu="VM.Standard.E3.Flex")
        kb = bf.boot_form_keyboard(form)
        texts = [b.text for row in kb.inline_keyboard for b in row]
        self.assertIn("✅ 自定义", texts)

    def test_fmt_summary(self):
        preset = {
            "shape": "VM.Standard.A1.Flex", "ocpus": 1, "memory_gb": 6,
            "display_name": "bot-a1-1c6g-20261124-000000",
            "image_id": FAKE_IMAGE_AARCH64, "count": 2,
            "boot_volume_size_gbs": 100, "min_delay": 5, "max_delay": 20,
            "ssh_keys_file": "/opt/x/keys.pub", "root_login": True,
            "assign_public_ip": True, "assign_ipv6": True,
            "startup_script": "echo hi", "os": "ubuntu",
            "target_cpu": None, "note": None,
        }
        text = bf.fmt_boot_form_summary(preset, True, "可以创建")
        self.assertIn("A1.Flex（1 核 / 6GB）", text)
        self.assertIn("Ubuntu 24.04", text)
        self.assertIn("bot-a1-1c6g-20261124-000000-1 ～", text)
        self.assertIn("共 2 台", text)
        self.assertIn("100GB", text)
        self.assertIn("区间 5–20 秒随机", text)
        self.assertIn("使用机器人公钥", text)
        self.assertIn("root登录：开启", text)
        self.assertIn("IPv6：有", text)
        self.assertIn("已上传（7 字符）", text)
        self.assertIn("✅ 可以创建", text)

    def test_fmt_summary_shows_password_when_present(self):
        preset = {
            "shape": "VM.Standard.A1.Flex", "ocpus": 1, "memory_gb": 6,
            "display_name": "ARM_2026-01-01",
            "image_id": FAKE_IMAGE_AARCH64, "count": 1,
            "ssh_keys_file": "/opt/x/keys.pub", "root_login": True,
            "root_password": "0123456789abcdef0123456789abcdef",
            "assign_public_ip": True, "os": "ubuntu",
        }
        text = bf.fmt_boot_form_summary(preset, True, "可以创建")
        self.assertIn("root登录：开启", text)
        self.assertIn("root密码：", text)
        self.assertIn("0123456789abcdef0123456789abcdef", text)
        self.assertIn("注意📢：root密码机器人已保存，之后 SSH（开机后、DD 后）"
                      "会用它登录；请你也自己保存一份", text)

    def test_fmt_summary_old_preset_no_new_keys(self):
        preset = {
            "shape": "VM.Standard.E2.1.Micro", "ocpus": 1, "memory_gb": 1,
            "display_name": "e2-micro", "image_id": FAKE_IMAGE_X86_64,
            "ssh_keys_file": "/opt/x/keys.pub", "target_cpu": "7J13",
        }
        text = bf.fmt_boot_form_summary(preset, False, "E2 已满（2/2）")
        self.assertIn("E2.1.Micro", text)
        self.assertIn("e2-micro", text)
        self.assertIn("7J13", text)
        self.assertIn("❌ E2 已满", text)
        self.assertIn("默认（47GB）", text)
        self.assertIn("默认 25±5 秒", text)


class TestDefaultAmdForm(unittest.TestCase):
    def test_oneclick_amd_preset(self):
        form = bf.default_amd_form()
        self.assertEqual(form["cpu"], "amd")
        self.assertEqual(form["os"], "ubuntu")
        self.assertEqual(form["count"], 1)
        self.assertIsNone(form["disk_gb"])
        self.assertEqual(form["ssh_key"], "use")
        self.assertTrue(form["root_login"])
        self.assertTrue(form["public_ip"])
        self.assertTrue(form["ipv6"])
        self.assertTrue(form["dd"])
        self.assertEqual(form["delays"], [20])

    def test_oneclick_amd_builds_valid_preset(self):
        _patched_images(self)
        form = bf.default_amd_form()
        self.assertEqual(bf.validate_form(form, FAKE_DEPLOY), [])
        preset = bf.build_preset(form, FAKE_DEPLOY)
        self.assertEqual(preset["shape"], "VM.Standard.E2.1.Micro")
        self.assertEqual(preset["ocpus"], 1)
        self.assertEqual(preset["memory_gb"], 1)
        self.assertRegex(preset["display_name"], r"^AMD_\d{4}-\d{2}-\d{2}$")
        self.assertTrue(preset["root_login"])


class TestRegionDeployDefaults(unittest.TestCase):
    def test_region_plan_overrides_deploy_values(self):
        plan = {
            "availability_domain": "wgwP:AP-SEOUL-1-AD-1",
            "subnet_id": "ocid1.subnet.oc1.ap-seoul-1.seoul",
            "image_aarch64": "ocid1.image.oc1.ap-seoul-1.arm",
            "image_x86_64": "ocid1.image.oc1.ap-seoul-1.x86",
            "ol_aarch64": "ocid1.image.oc1.ap-seoul-1.olarm",
            "ol_x86_64": "ocid1.image.oc1.ap-seoul-1.olx86",
        }
        with patch("tgbot.config.get_region_plan", return_value=plan):
            deploy = bf.get_deploy_defaults(region="ap-seoul-1",
                                            profile="DEFAULT")
        self.assertEqual(deploy["availability_domain"],
                         "wgwP:AP-SEOUL-1-AD-1")
        self.assertEqual(deploy["subnet_id"],
                         "ocid1.subnet.oc1.ap-seoul-1.seoul")
        self.assertEqual(deploy["image_aarch64"],
                         "ocid1.image.oc1.ap-seoul-1.arm")
        self.assertEqual(deploy["ol_x86_64"],
                         "ocid1.image.oc1.ap-seoul-1.olx86")

    def test_region_without_plan_falls_back_to_env(self):
        _patched_images(self)
        with patch("tgbot.config.get_region_plan", return_value={}), \
                patch.dict("os.environ", {
                    "MBOT_PLAN_AD": "wgwP:AP-SINGAPORE-1-AD-1",
                    "MBOT_PLAN_SUBNET_ID": "ocid1.subnet.oc1.ap-singapore-1.s",
                    "MBOT_PLAN_SSH_KEYS_FILE": "/opt/x/keys.pub"}):
            deploy = bf.get_deploy_defaults(region="ap-seoul-1",
                                            profile="DEFAULT")
        self.assertEqual(deploy["availability_domain"],
                         "wgwP:AP-SINGAPORE-1-AD-1")
        self.assertEqual(deploy["image_aarch64"], FAKE_IMAGE_AARCH64)

    def test_profile_not_passed_never_uses_region_plan(self):
        # Without a profile, do not look up the region config (subnets belong to a tenancy and cannot be used across accounts). 不传 profile 时不查区域配置（子网按租户，不能跨账号用）。
        plan = {"availability_domain": "wgwP:AP-SEOUL-1-AD-1"}
        with patch("tgbot.config.get_region_plan",
                   return_value=plan) as mock_plan, \
                patch.dict("os.environ",
                           {"MBOT_PLAN_AD": "wgwP:AP-SINGAPORE-1-AD-1"}):
            deploy = bf.get_deploy_defaults(region="ap-seoul-1")
        mock_plan.assert_not_called()
        self.assertEqual(deploy["availability_domain"],
                         "wgwP:AP-SINGAPORE-1-AD-1")

    def test_resolve_image_uses_region_deploy(self):
        deploy = dict(FAKE_DEPLOY)
        deploy["image_aarch64"] = "ocid1.image.oc1.ap-seoul-1.arm"
        image, error = bf.resolve_image_id(
            _filled_form(cpu="arm", os="ubuntu"), deploy)
        self.assertIsNone(error)
        self.assertEqual(image, "ocid1.image.oc1.ap-seoul-1.arm")


class TestGetRegionPlan(unittest.TestCase):
    def _write_plans(self, content):
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8")
        with tmp:
            tmp.write(content)
        self.addCleanup(os.unlink, tmp.name)
        return tmp.name

    def test_double_key_lookup(self):
        path = self._write_plans(json.dumps({
            "DEFAULT": {"ap-seoul-1": {"subnet_id": "ocid1.subnet.seoul"}},
            "ACCOUNT2": {"us-sanjose-1":
                         {"subnet_id": "ocid1.subnet.sanjose"}},
        }))
        with patch.object(bot_config, "REGION_PLANS_FILE", path):
            self.assertEqual(
                bot_config.get_region_plan("DEFAULT", "ap-seoul-1"),
                {"subnet_id": "ocid1.subnet.seoul"})
            # Different accounts in the same region must not share configs. 同区域不同账号不能串用。
            self.assertEqual(
                bot_config.get_region_plan("ACCOUNT2", "ap-seoul-1"), {})
            self.assertEqual(
                bot_config.get_region_plan("DEFAULT", "us-sanjose-1"), {})

    def test_missing_file_and_bad_json_return_empty(self):
        with patch.object(bot_config, "REGION_PLANS_FILE",
                          "/nonexistent/region_plans.json"):
            self.assertEqual(
                bot_config.get_region_plan("DEFAULT", "ap-seoul-1"), {})
        path = self._write_plans("{not json")
        with patch.object(bot_config, "REGION_PLANS_FILE", path):
            self.assertEqual(
                bot_config.get_region_plan("DEFAULT", "ap-seoul-1"), {})


if __name__ == "__main__":
    unittest.main()
