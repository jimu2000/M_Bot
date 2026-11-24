#!/usr/bin/env python3
"""付费账号标记（tgbot/paid_profiles.py）的单元测试（计划 v1.9.0）。

全部用临时文件，不碰真实配置。默认免费；标记后备份不限 5 个。
"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tgbot import paid_profiles


class PaidProfilesTestCase(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._path = os.path.join(self._tmp.name, "paid_profiles.json")
        self._patcher = patch.object(paid_profiles, "PAID_PROFILES_FILE",
                                     self._path)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._tmp.cleanup()


class TestPaidProfiles(PaidProfilesTestCase):

    def test_default_free_when_file_missing(self):
        self.assertEqual(paid_profiles.load_paid_profiles(), set())
        self.assertFalse(paid_profiles.is_paid_profile("DEFAULT"))

    def test_corrupt_file_means_all_free(self):
        with open(self._path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertEqual(paid_profiles.load_paid_profiles(), set())
        self.assertFalse(paid_profiles.is_paid_profile("DEFAULT"))

    def test_set_and_unset(self):
        self.assertTrue(paid_profiles.set_paid_profile("DEFAULT", True))
        self.assertTrue(paid_profiles.is_paid_profile("DEFAULT"))
        self.assertFalse(paid_profiles.is_paid_profile("ACCOUNT2"))
        self.assertFalse(paid_profiles.set_paid_profile("DEFAULT", False))
        self.assertFalse(paid_profiles.is_paid_profile("DEFAULT"))

    def test_round_trip_and_permissions(self):
        paid_profiles.save_paid_profiles({"ACCOUNT2", "DEFAULT"})
        self.assertEqual(paid_profiles.load_paid_profiles(),
                         {"ACCOUNT2", "DEFAULT"})
        self.assertEqual(oct(os.stat(self._path).st_mode & 0o777), "0o600")
        with open(self._path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["paid"], ["ACCOUNT2", "DEFAULT"])


if __name__ == "__main__":
    unittest.main()
