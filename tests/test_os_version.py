#!/usr/bin/env python3
"""系统版本检测的单元测试（解析 / 缓存 / 获取，不碰真实 SSH）。

运行：python3 -m unittest tests.test_os_version -v

覆盖：tgbot.os_version 的 parse_os_version / load_os_versions /
cached_os_version / save_os_version / delete_os_version /
prune_os_versions / fetch_os_version / get_os_version。
缓存文件用临时路径，不污染真实数据目录。
"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tgbot import os_version as ov  # noqa: E402

# Placeholders (no real OCID / IP in tests). 占位符（测试里不出现真实 OCID / IP）。
FAKE_OCID = "ocid1.instance.oc1..ostest1"
FAKE_OCID_2 = "ocid1.instance.oc1..ostest2"
FAKE_HOST = {"host": "203.0.113.10", "port": 14422, "username": "root",
             "key_path": "/root/.ssh/rbot_guard"}

DEBIAN_OS_RELEASE = """PRETTY_NAME="Debian GNU/Linux 13 (trixie)"
NAME="Debian GNU/Linux"
VERSION_ID="13"
VERSION="13 (trixie)"
ID=debian
HOME_URL="https://www.debian.org/"
"""

UBUNTU_OS_RELEASE = """PRETTY_NAME="Ubuntu 22.04.3 LTS"
NAME="Ubuntu"
VERSION_ID="22.04"
VERSION="22.04.3 LTS (Jammy Jellyfish)"
ID=ubuntu
"""

ORACLE_OS_RELEASE = """PRETTY_NAME="Oracle Linux Server 9.4"
NAME="Oracle Linux Server"
VERSION_ID="9.4"
ID="ol"
"""


def _os_output(os_release, debian_version=""):
    return os_release + "---DEBIAN_VERSION---\n" + debian_version + "\n"


class TestParseOsVersion(unittest.TestCase):
    """parse_os_version：命令输出 -> 短系统版本。"""

    def test_debian_point_release(self):
        # Debian 13.7: the point release only lives in /etc/debian_version.
        # Debian 13.7：小版本只在 /etc/debian_version 里。
        self.assertEqual(
            ov.parse_os_version(_os_output(DEBIAN_OS_RELEASE, "13.7")),
            "Debian 13.7")

    def test_debian_major_only_when_no_debian_version(self):
        self.assertEqual(
            ov.parse_os_version(_os_output(DEBIAN_OS_RELEASE, "")),
            "Debian 13")

    def test_ubuntu(self):
        self.assertEqual(
            ov.parse_os_version(_os_output(UBUNTU_OS_RELEASE, "")),
            "Ubuntu 22.04")

    def test_oracle_linux(self):
        self.assertEqual(
            ov.parse_os_version(_os_output(ORACLE_OS_RELEASE, "")),
            "Oracle Linux 9.4")

    def test_empty_and_garbage(self):
        self.assertIsNone(ov.parse_os_version(""))
        self.assertIsNone(ov.parse_os_version(None))
        self.assertIsNone(ov.parse_os_version("hello world\n"))
        self.assertIsNone(ov.parse_os_version("---DEBIAN_VERSION---\n"))


class TestOsVersionCache(unittest.TestCase):
    """缓存读写：临时文件，不污染真实数据目录。"""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.path)
        self.patch = patch.object(ov, "OS_VERSIONS_FILE", self.path)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        if os.path.exists(self.path):
            os.unlink(self.path)
        tmp = self.path + ".tmp"
        if os.path.exists(tmp):
            os.unlink(tmp)

    def test_save_and_cached(self):
        self.assertIsNone(ov.cached_os_version(FAKE_OCID))
        ov.save_os_version(FAKE_OCID, "Debian 13.7")
        self.assertEqual(ov.cached_os_version(FAKE_OCID), "Debian 13.7")
        # File is 0600 and valid JSON. 文件 0600 且是合法 JSON。
        self.assertEqual(oct(os.stat(self.path).st_mode & 0o777), "0o600")
        with open(self.path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data[FAKE_OCID]["version"], "Debian 13.7")

    def test_delete(self):
        ov.save_os_version(FAKE_OCID, "Debian 13.7")
        ov.delete_os_version(FAKE_OCID)
        self.assertIsNone(ov.cached_os_version(FAKE_OCID))

    def test_prune(self):
        ov.save_os_version(FAKE_OCID, "Debian 13.7")
        ov.save_os_version(FAKE_OCID_2, "Ubuntu 22.04")
        ov.prune_os_versions([FAKE_OCID])
        self.assertEqual(ov.cached_os_version(FAKE_OCID), "Debian 13.7")
        self.assertIsNone(ov.cached_os_version(FAKE_OCID_2))

    def test_load_corrupt_file_returns_empty(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(ov.load_os_versions(), {})


class TestGetOsVersion(unittest.TestCase):
    """get_os_version：缓存优先，缺失时 SSH 检测并写缓存。"""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.path)
        self.patch = patch.object(ov, "OS_VERSIONS_FILE", self.path)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        if os.path.exists(self.path):
            os.unlink(self.path)
        tmp = self.path + ".tmp"
        if os.path.exists(tmp):
            os.unlink(tmp)

    def _fake_run(self, output, code=0):
        def run(host, command, timeout=None):
            self.assertIn("os-release", command)
            return (code, output, "")
        return run

    def test_fetch_and_cache(self):
        run = self._fake_run(_os_output(DEBIAN_OS_RELEASE, "13.7"))
        self.assertEqual(
            ov.get_os_version(FAKE_OCID, FAKE_HOST, run), "Debian 13.7")
        # Second call hits the cache and does not SSH again.
        # 第二次走缓存，不再 SSH。
        calls = []
        def counting_run(host, command, timeout=None):
            calls.append(command)
            return (0, "", "")
        self.assertEqual(
            ov.get_os_version(FAKE_OCID, FAKE_HOST, counting_run),
            "Debian 13.7")
        self.assertEqual(calls, [])

    def test_fetch_failure_returns_none(self):
        run = self._fake_run("", code=1)
        self.assertIsNone(ov.get_os_version(FAKE_OCID, FAKE_HOST, run))

    def test_force_rechecks(self):
        ov.save_os_version(FAKE_OCID, "Debian 13.7")
        run = self._fake_run(_os_output(UBUNTU_OS_RELEASE, ""))
        self.assertEqual(
            ov.get_os_version(FAKE_OCID, FAKE_HOST, run, force=True),
            "Ubuntu 22.04")
        self.assertEqual(ov.cached_os_version(FAKE_OCID), "Ubuntu 22.04")


if __name__ == "__main__":
    unittest.main()
