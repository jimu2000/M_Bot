#!/usr/bin/env python3
"""按实例存 root 密码的单元测试（tgbot/instance_credentials）。

运行：python3 -m unittest tests.test_instance_credentials -v

覆盖：save/get/load、resolve（固定密码优先）、update IP、delete、
prune、文件 0600、损坏文件容错、密码绝不进日志。
凭据文件用临时路径，不污染真实数据目录；密码与 OCID、IP 全是占位符。
"""

import json
import os
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tgbot import instance_credentials as ic  # noqa: E402

# Placeholders (no real OCID / IP / password in tests). 占位符（测试里不出现真实 OCID / IP / 密码）。
FAKE_OCID = "ocid1.instance.oc1..credtest1"
FAKE_OCID_2 = "ocid1.instance.oc1..credtest2"
FAKE_OCID_3 = "ocid1.instance.oc1..credtest3"
FAKE_PASSWORD = "test-password-aaaaaaaaaaaa"
FAKE_PASSWORD_2 = "test-password-bbbbbbbbbbbb"
FAKE_IP = "203.0.113.10"


class _PatchedFileTestCase(unittest.TestCase):
    """凭据文件指到临时路径的基类。"""

    def setUp(self):
        fd, self.tmp_path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.tmp_path)  # Start with no file, like a fresh deploy. 先删掉，像全新部署一样从无文件开始。
        self._patch = patch.object(ic, "CREDENTIALS_FILE", self.tmp_path)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.addCleanup(lambda: os.path.exists(self.tmp_path)
                        and os.unlink(self.tmp_path))


class TestSaveAndGet(_PatchedFileTestCase):
    """save_credential / get_credential / get_root_password。"""

    def test_roundtrip_keeps_all_fields(self):
        ic.save_credential(FAKE_OCID, FAKE_PASSWORD, ip=FAKE_IP,
                           instance_name="AMD_2026-01-05")
        entry = ic.get_credential(FAKE_OCID)
        self.assertEqual(entry["root_password"], FAKE_PASSWORD)
        self.assertEqual(entry["ip"], FAKE_IP)
        self.assertEqual(entry["instance_name"], "AMD_2026-01-05")
        self.assertIn("updated_at", entry)
        self.assertEqual(ic.get_root_password(FAKE_OCID), FAKE_PASSWORD)

    def test_unknown_instance_returns_none(self):
        self.assertIsNone(ic.get_credential(FAKE_OCID))
        self.assertIsNone(ic.get_root_password(FAKE_OCID))
        self.assertIsNone(ic.get_root_password(""))

    def test_resave_overwrites(self):
        ic.save_credential(FAKE_OCID, FAKE_PASSWORD, ip=FAKE_IP)
        ic.save_credential(FAKE_OCID, FAKE_PASSWORD_2, ip="203.0.113.20")
        self.assertEqual(ic.get_root_password(FAKE_OCID), FAKE_PASSWORD_2)
        self.assertEqual(ic.get_credential(FAKE_OCID)["ip"],
                         "203.0.113.20")

    def test_empty_id_or_password_is_noop(self):
        ic.save_credential("", FAKE_PASSWORD)
        ic.save_credential(FAKE_OCID, "")
        self.assertFalse(os.path.exists(self.tmp_path))

    def test_file_mode_0600(self):
        ic.save_credential(FAKE_OCID, FAKE_PASSWORD, ip=FAKE_IP)
        mode = stat.S_IMODE(os.stat(self.tmp_path).st_mode)
        self.assertEqual(mode, 0o600)

    def test_corrupt_file_loads_empty(self):
        with open(self.tmp_path, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(ic.load_credentials(), {})
        self.assertIsNone(ic.get_root_password(FAKE_OCID))

    def test_password_never_in_logs(self):
        # The password must not appear in any log record emitted while
        # saving / deleting (only the instance OCID tail may be logged).
        # 保存 / 删除时任何日志里都不许出现密码（只许记 OCID 后 4 位）。
        with self.assertLogs("tgbot.instance_credentials",
                             level="DEBUG") as logs:
            ic.save_credential(FAKE_OCID, FAKE_PASSWORD, ip=FAKE_IP)
            ic.delete_credential(FAKE_OCID)
        joined = "\n".join(logs.output)
        self.assertNotIn(FAKE_PASSWORD, joined)
        self.assertNotIn(FAKE_IP, joined)


class TestResolve(_PatchedFileTestCase):
    """resolve_root_password：固定密码优先，再看凭据库。"""

    def test_fixed_password_wins(self):
        ic.save_credential(FAKE_OCID, FAKE_PASSWORD, ip=FAKE_IP)
        with patch.dict(os.environ,
                        {"MBOT_FIXED_ROOT_PASSWORD": "fixed-pw-test"}):
            self.assertEqual(ic.resolve_root_password(FAKE_OCID),
                             "fixed-pw-test")

    def test_stored_password_as_fallback(self):
        ic.save_credential(FAKE_OCID, FAKE_PASSWORD, ip=FAKE_IP)
        with patch.dict(os.environ, {"MBOT_FIXED_ROOT_PASSWORD": ""}):
            self.assertEqual(ic.resolve_root_password(FAKE_OCID),
                             FAKE_PASSWORD)

    def test_none_when_neither(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(ic.resolve_root_password(FAKE_OCID))


class TestUpdateIp(_PatchedFileTestCase):
    """update_credential_ip：后到的 IP 补记。"""

    def test_updates_ip_only(self):
        ic.save_credential(FAKE_OCID, FAKE_PASSWORD, ip=None,
                           instance_name="AMD_2026-01-05")
        ic.update_credential_ip(FAKE_OCID, "203.0.113.30")
        entry = ic.get_credential(FAKE_OCID)
        self.assertEqual(entry["ip"], "203.0.113.30")
        self.assertEqual(entry["root_password"], FAKE_PASSWORD)
        self.assertEqual(entry["instance_name"], "AMD_2026-01-05")

    def test_noop_without_entry(self):
        ic.update_credential_ip(FAKE_OCID, FAKE_IP)
        self.assertFalse(os.path.exists(self.tmp_path))
        self.assertIsNone(ic.get_credential(FAKE_OCID))


class TestDeleteAndPrune(_PatchedFileTestCase):
    """delete_credential / prune_credentials：终止后清理。"""

    def test_delete_removes_entry(self):
        ic.save_credential(FAKE_OCID, FAKE_PASSWORD, ip=FAKE_IP)
        ic.save_credential(FAKE_OCID_2, FAKE_PASSWORD_2, ip=FAKE_IP)
        ic.delete_credential(FAKE_OCID)
        self.assertIsNone(ic.get_credential(FAKE_OCID))
        self.assertEqual(ic.get_root_password(FAKE_OCID_2),
                         FAKE_PASSWORD_2)

    def test_delete_unknown_is_noop(self):
        ic.delete_credential(FAKE_OCID)  # Must not raise on a missing file. 文件不存在也不报错。
        self.assertFalse(os.path.exists(self.tmp_path))

    def test_prune_removes_stale_only(self):
        ic.save_credential(FAKE_OCID, FAKE_PASSWORD, ip=FAKE_IP)
        ic.save_credential(FAKE_OCID_2, FAKE_PASSWORD_2, ip=FAKE_IP)
        ic.save_credential(FAKE_OCID_3, FAKE_PASSWORD, ip=FAKE_IP)
        removed = ic.prune_credentials([FAKE_OCID_2])
        self.assertEqual(removed, 2)
        self.assertEqual(ic.get_root_password(FAKE_OCID_2),
                         FAKE_PASSWORD_2)
        self.assertIsNone(ic.get_credential(FAKE_OCID))
        self.assertIsNone(ic.get_credential(FAKE_OCID_3))

    def test_prune_nothing_stale_returns_zero(self):
        ic.save_credential(FAKE_OCID, FAKE_PASSWORD, ip=FAKE_IP)
        with open(self.tmp_path, encoding="utf-8") as f:
            before = f.read()
        self.assertEqual(ic.prune_credentials([FAKE_OCID]), 0)
        with open(self.tmp_path, encoding="utf-8") as f:
            after = f.read()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
