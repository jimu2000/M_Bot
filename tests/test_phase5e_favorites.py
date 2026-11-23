#!/usr/bin/env python3
"""阶段 5 工作包 E：终端收藏夹的测试（TestClient）。

覆盖：GET/POST/DELETE /api/term-favorites、{version, items} 形状、
校验（空名/空命令/超长）、文件持久化、损坏文件降级。
"""

import json
import os
import secrets
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from web.app import create_app
from web.auth import LOGIN_GUARD, SESSIONS, hash_password

# 测试密码：运行时随机生成（不在代码里写任何固定口令）。
TEST_PASSWORD = secrets.token_urlsafe(16)


class FavoritesTestCase(unittest.TestCase):
    """API 测试基类：建 app、临时 DATA_DIR、登录。"""

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

    def _favorites_file(self):
        return os.path.join(self._tmp.name, "term_favorites.json")


class TestFavoritesCrud(FavoritesTestCase):
    """收藏夹 CRUD。"""

    def test_empty_list(self):
        r = self.client.get("/api/term-favorites")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["version"], 1)
        self.assertEqual(body["items"], [])

    def test_create_and_list(self):
        r = self.client.post("/api/term-favorites", json={
            "name": "查看磁盘", "command": "df -h"})
        self.assertEqual(r.status_code, 201)
        item = r.json()["item"]
        self.assertEqual(item["name"], "查看磁盘")
        self.assertEqual(item["command"], "df -h")
        self.assertEqual(item["hostId"], "")
        self.assertTrue(item["id"])

        r = self.client.get("/api/term-favorites")
        self.assertEqual(len(r.json()["items"]), 1)
        self.assertEqual(r.json()["items"][0]["id"], item["id"])

        # 文件落盘，结构为 {version, items}。
        with open(self._favorites_file(), encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["version"], 1)
        self.assertEqual(len(data["items"]), 1)

    def test_create_with_host_id(self):
        r = self.client.post("/api/term-favorites", json={
            "name": "看日志", "command": "tail -f /var/log/syslog",
            "hostId": "host-a"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["item"]["hostId"], "host-a")

    def test_delete(self):
        item = self.client.post("/api/term-favorites", json={
            "name": "a", "command": "uptime"}).json()["item"]
        r = self.client.delete(f"/api/term-favorites/{item['id']}")
        self.assertEqual(r.status_code, 200)
        r = self.client.get("/api/term-favorites")
        self.assertEqual(r.json()["items"], [])

    def test_delete_unknown_404(self):
        r = self.client.delete("/api/term-favorites/nope")
        self.assertEqual(r.status_code, 404)

    def test_create_empty_name_400(self):
        r = self.client.post("/api/term-favorites", json={
            "name": "  ", "command": "uptime"})
        self.assertEqual(r.status_code, 400)

    def test_create_empty_command_400(self):
        r = self.client.post("/api/term-favorites", json={
            "name": "a", "command": "  "})
        self.assertEqual(r.status_code, 400)

    def test_create_command_too_long_400(self):
        r = self.client.post("/api/term-favorites", json={
            "name": "a", "command": "x" * 2001})
        self.assertEqual(r.status_code, 400)

    def test_corrupt_file_degrades_to_empty(self):
        with open(self._favorites_file(), "w") as f:
            f.write("{不是 json")
        r = self.client.get("/api/term-favorites")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["items"], [])

    def test_anon_401(self):
        r = self.anon.get("/api/term-favorites")
        self.assertEqual(r.status_code, 401)
        r = self.anon.post("/api/term-favorites",
                           json={"name": "a", "command": "uptime"})
        self.assertEqual(r.status_code, 401)
        r = self.anon.delete("/api/term-favorites/x")
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
