#!/usr/bin/env python3
"""阶段 5 工作包 E：OCI 代理设置的测试。

覆盖：
  - oci.client.get_oci_proxy / proxy_env：环境变量优先、文件兜底、
    非法协议拒绝、文件损坏降级。
  - OCIClient.run_raw：代理只进子进程 env，不改全局 os.environ。
  - GET/POST /api/settings/oci-proxy：查看/设置/清除/非法地址 400、
    环境变量优先提示。
不真调 OCI CLI（subprocess 全 mock）。
"""

import json
import os
import secrets
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from oci.client import OCIClient, get_oci_proxy, proxy_env
from web.app import create_app
from web.auth import LOGIN_GUARD, SESSIONS, hash_password

# 测试密码：运行时随机生成（不在代码里写任何固定口令）。
TEST_PASSWORD = secrets.token_urlsafe(16)
PROXY = "http://127.0.0.1:7890"


class ProxyResolutionTest(unittest.TestCase):
    """get_oci_proxy / proxy_env 解析逻辑。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.proxy_file = os.path.join(self._tmp.name, "oci_proxy.json")

    def _write_proxy_file(self, data):
        with open(self.proxy_file, "w") as f:
            json.dump(data, f)

    def test_env_var_wins(self):
        self._write_proxy_file({"proxy": "http://127.0.0.1:1111"})
        with patch.dict(os.environ, {"MBOT_OCI_PROXY": PROXY}):
            self.assertEqual(get_oci_proxy(proxy_file=self.proxy_file), PROXY)

    def test_file_fallback(self):
        self._write_proxy_file({"proxy": PROXY})
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(get_oci_proxy(proxy_file=self.proxy_file), PROXY)

    def test_no_proxy(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(get_oci_proxy(proxy_file=self.proxy_file))

    def test_empty_proxy_file_value(self):
        self._write_proxy_file({"proxy": ""})
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(get_oci_proxy(proxy_file=self.proxy_file))

    def test_corrupt_file(self):
        with open(self.proxy_file, "w") as f:
            f.write("{损坏")
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(get_oci_proxy(proxy_file=self.proxy_file))

    def test_invalid_scheme_rejected(self):
        self._write_proxy_file({"proxy": "ftp://127.0.0.1:21"})
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(get_oci_proxy(proxy_file=self.proxy_file))
        with patch.dict(os.environ, {"MBOT_OCI_PROXY": "example.com:8080"}):
            self.assertIsNone(get_oci_proxy(proxy_file=self.proxy_file))

    def test_socks5_allowed(self):
        with patch.dict(os.environ,
                        {"MBOT_OCI_PROXY": "socks5://127.0.0.1:1080"}):
            self.assertEqual(get_oci_proxy(proxy_file=self.proxy_file),
                             "socks5://127.0.0.1:1080")

    def test_proxy_env_dict(self):
        env = proxy_env(PROXY)
        self.assertEqual(env["HTTPS_PROXY"], PROXY)
        self.assertEqual(env["HTTP_PROXY"], PROXY)
        self.assertEqual(env["https_proxy"], PROXY)
        self.assertEqual(env["http_proxy"], PROXY)
        self.assertEqual(proxy_env(None, proxy_file=self.proxy_file), {})


class OciClientProxyTest(unittest.TestCase):
    """OCIClient.run_raw：代理只进子进程 env。"""

    @patch("oci.client.subprocess.run")
    def test_proxy_in_subprocess_env(self, mock_run):
        mock_run.return_value = SimpleNamespace(returncode=0, stdout="{}",
                                                stderr="")
        cli = OCIClient(proxy=PROXY)
        cli.run_raw(["compute", "instance", "list"])
        env = mock_run.call_args[1]["env"]
        self.assertEqual(env["HTTPS_PROXY"], PROXY)
        self.assertEqual(env["HTTP_PROXY"], PROXY)
        self.assertEqual(env["SUPPRESS_LABEL_WARNING"], "True")
        # 全局环境没被改。
        self.assertNotEqual(os.environ.get("HTTPS_PROXY"), PROXY)

    @patch("oci.client.subprocess.run")
    def test_no_proxy_no_env(self, mock_run):
        """无代理配置时不向子进程 env 注入代理变量。"""
        mock_run.return_value = SimpleNamespace(returncode=0, stdout="{}",
                                                stderr="")
        # 用干净环境（本机沙箱自己设了 HTTPS_PROXY，会被原样继承）。
        with patch("oci.client.get_oci_proxy", return_value=None), \
             patch.dict(os.environ, {}, clear=True):
            cli = OCIClient()
            cli.run_raw(["compute", "instance", "list"])
        env = mock_run.call_args[1]["env"]
        self.assertNotIn("HTTPS_PROXY", env)
        self.assertNotIn("HTTP_PROXY", env)


class ProxySettingsApiTest(unittest.TestCase):
    """GET/POST /api/settings/oci-proxy。"""

    @classmethod
    def setUpClass(cls):
        cls._hash_patcher = patch("web.config.ADMIN_PASSWORD_HASH",
                                  hash_password(TEST_PASSWORD))
        cls._hash_patcher.start()
        cls.app = create_app()

    @classmethod
    def tearDownClass(cls):
        cls._hash_patcher.stop()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._data_dir_patcher = patch("web.config.DATA_DIR", self._tmp.name)
        self._data_dir_patcher.start()
        LOGIN_GUARD.reset()
        SESSIONS.reset()
        self.client = TestClient(self.app)
        self.anon = TestClient(self.app)
        self.client.post("/api/auth/login",
                         json={"username": "admin",
                               "password": TEST_PASSWORD})

    def tearDown(self):
        self._data_dir_patcher.stop()
        self._tmp.cleanup()

    def test_get_no_proxy(self):
        with patch.dict(os.environ, {}, clear=True):
            r = self.client.get("/api/settings/oci-proxy")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["source"], "none")
        self.assertIsNone(r.json()["proxy"])

    def test_set_and_get(self):
        r = self.client.post("/api/settings/oci-proxy",
                             json={"proxy": PROXY})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["proxy"], PROXY)
        self.assertEqual(r.json()["source"], "file")
        self.assertIn("立即生效", r.json()["message"])
        # 文件落盘。
        with open(os.path.join(self._tmp.name, "oci_proxy.json")) as f:
            self.assertEqual(json.load(f)["proxy"], PROXY)
        # GET 读回。
        with patch.dict(os.environ, {}, clear=True):
            r = self.client.get("/api/settings/oci-proxy")
        self.assertEqual(r.json()["proxy"], PROXY)
        self.assertEqual(r.json()["source"], "file")

    def test_set_invalid_scheme_400(self):
        r = self.client.post("/api/settings/oci-proxy",
                             json={"proxy": "example.com:8080"})
        self.assertEqual(r.status_code, 400)

    def test_clear_proxy(self):
        self.client.post("/api/settings/oci-proxy", json={"proxy": PROXY})
        r = self.client.post("/api/settings/oci-proxy", json={"proxy": ""})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["proxy"])
        with patch.dict(os.environ, {}, clear=True):
            r = self.client.get("/api/settings/oci-proxy")
        self.assertEqual(r.json()["source"], "none")

    def test_env_var_priority_noted(self):
        self.client.post("/api/settings/oci-proxy", json={"proxy": PROXY})
        with patch.dict(os.environ, {"MBOT_OCI_PROXY":
                                     "http://127.0.0.1:9999"}):
            r = self.client.post("/api/settings/oci-proxy",
                                 json={"proxy": PROXY})
        self.assertEqual(r.json()["source"], "env")
        self.assertEqual(r.json()["effectiveProxy"], "http://127.0.0.1:9999")
        self.assertIn("环境变量", r.json()["message"])

    def test_anon_401(self):
        r = self.anon.get("/api/settings/oci-proxy")
        self.assertEqual(r.status_code, 401)
        r = self.anon.post("/api/settings/oci-proxy",
                           json={"proxy": PROXY})
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
