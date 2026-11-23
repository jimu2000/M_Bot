#!/usr/bin/env python3
"""web/sftp.py 的测试（mock + 路径穿越 + 真实回环 SFTP 端到端）。

运行：python3 -m unittest discover -s tests -v
或：   python3 -m unittest tests.test_web_sftp -v

测试主机只用 203.0.113.x（TEST-NET-3 文档段）或 127.0.0.1 回环，
绝不出现真实 IP/密码。回环 SFTP 服务器背后是临时目录，
不依赖系统 sshd。
"""

import io
import os
import socket
import stat
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import paramiko
from fastapi import FastAPI
from fastapi.testclient import TestClient

import web.sftp as sftp_module
from web.sftp import resolve_safe_path, router
from web.ssh import SSHSessionManager

HOME = "/home/testuser"

app = FastAPI()
app.include_router(router)


class _StubSessionStore:
    """测试用认证存储：只有 token=good-token 能登录。"""

    def get(self, token):
        return {"username": "tester"} if token == "good-token" else None


def _make_attr(name, mode, size=0, mtime=1700000000):
    """造一个 SFTPAttributes 替身（只含接口用到的字段）。"""
    return SimpleNamespace(filename=name, st_mode=mode,
                           st_size=size, st_mtime=mtime)


def _make_mock_sftp():
    """造一个 mock SFTPClient：normalize 默认原样返回（无符号链接）。"""
    sftp = MagicMock()
    sftp.normalize.side_effect = lambda p: p
    return sftp


def _make_entry(sftp=None, home=HOME):
    """造一条 manager 会话记录（sftp 已缓存，家目录已知）。"""
    return {"session": MagicMock(),
            "sftp": sftp if sftp is not None else _make_mock_sftp(),
            "sftp_home": home}


class TestResolveSafePath(unittest.TestCase):
    """resolve_safe_path 纯函数测试（路径穿越防护核心）。"""

    def test_empty_and_dot_return_home(self):
        self.assertEqual(resolve_safe_path(HOME, None), HOME)
        self.assertEqual(resolve_safe_path(HOME, ""), HOME)
        self.assertEqual(resolve_safe_path(HOME, "."), HOME)

    def test_relative_path(self):
        self.assertEqual(resolve_safe_path(HOME, "docs/a.txt"),
                         "/home/testuser/docs/a.txt")
        self.assertEqual(resolve_safe_path(HOME, "docs/"),
                         "/home/testuser/docs")

    def test_absolute_inside_home(self):
        self.assertEqual(resolve_safe_path(HOME, "/home/testuser/a.txt"),
                         "/home/testuser/a.txt")

    def test_internal_dotdot_allowed(self):
        # 合法的内部 .. 必须放行
        self.assertEqual(resolve_safe_path(HOME, "a/../b"),
                         "/home/testuser/b")
        self.assertEqual(resolve_safe_path(HOME, "a/b/../../c"),
                         "/home/testuser/c")

    def test_dotdot_escape_rejected(self):
        with self.assertRaises(ValueError):
            resolve_safe_path(HOME, "../")
        with self.assertRaises(ValueError):
            resolve_safe_path(HOME, "..")
        with self.assertRaises(ValueError):
            resolve_safe_path(HOME, "a/../../..")

    def test_etc_passwd_rejected(self):
        with self.assertRaises(ValueError):
            resolve_safe_path(HOME, "../../etc/passwd")
        with self.assertRaises(ValueError):
            resolve_safe_path(HOME, "/etc/passwd")

    def test_sibling_prefix_not_within(self):
        # /home/testuser2 不是 /home/testuser 的子目录（前缀陷阱）
        with self.assertRaises(ValueError):
            resolve_safe_path(HOME, "/home/testuser2/x")

    def test_nul_byte_rejected(self):
        with self.assertRaises(ValueError):
            resolve_safe_path(HOME, "a\x00b")
        with self.assertRaises(ValueError):
            resolve_safe_path("/home/a\x00b", "x")

    def test_home_must_be_absolute(self):
        with self.assertRaises(ValueError):
            resolve_safe_path("relative/home", "x")

    def test_home_root(self):
        self.assertEqual(resolve_safe_path("/", "etc/passwd"), "/etc/passwd")
        self.assertEqual(resolve_safe_path("/", ".."), "/")

    def test_home_trailing_slash(self):
        self.assertEqual(resolve_safe_path("/home/testuser/", "a.txt"),
                         "/home/testuser/a.txt")
        with self.assertRaises(ValueError):
            resolve_safe_path("/home/testuser/", "../etc/passwd")


class _SFTPTestCase(unittest.TestCase):
    """REST 测试基类：认证走 stub，manager 走 mock。"""

    def setUp(self):
        self._auth_patch = patch("web.ssh.session_store", _StubSessionStore())
        self._auth_patch.start()
        self.addCleanup(self._auth_patch.stop)
        self._mgr_patch = patch("web.sftp.manager")
        self.mock_manager = self._mgr_patch.start()
        self.addCleanup(self._mgr_patch.stop)
        self.sftp = _make_mock_sftp()
        self.entry = _make_entry(self.sftp)
        self.mock_manager.get.return_value = self.entry
        self.client = TestClient(app, raise_server_exceptions=False)

    def _auth_cookies(self):
        return {"rbot_session": "good-token"}

    def _no_session(self):
        self.mock_manager.get.return_value = None


class TestList(_SFTPTestCase):
    def test_list_success(self):
        self.sftp.listdir_attr.return_value = [
            _make_attr("docs", stat.S_IFDIR | 0o755),
            _make_attr("a.txt", stat.S_IFREG | 0o644, size=123),
        ]
        resp = self.client.get("/api/ssh/sessions/sid-1/sftp/list",
                               cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["path"], HOME)
        self.assertEqual(data["home"], HOME)
        # 目录在前、文件在后
        self.assertEqual([e["name"] for e in data["entries"]],
                         ["docs", "a.txt"])
        self.assertTrue(data["entries"][0]["is_dir"])
        self.assertEqual(data["entries"][1]["size"], 123)
        self.assertEqual(data["entries"][1]["permissions"], "-rw-r--r--")
        self.assertFalse(data["entries"][1]["outside_home"])

    def test_list_explicit_path(self):
        self.sftp.listdir_attr.return_value = []
        resp = self.client.get(
            "/api/ssh/sessions/sid-1/sftp/list?path=docs",
            cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["path"], "/home/testuser/docs")
        self.sftp.listdir_attr.assert_called_once_with("/home/testuser/docs")

    def test_list_traversal_rejected(self):
        resp = self.client.get(
            "/api/ssh/sessions/sid-1/sftp/list?path=../../etc",
            cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 400)
        self.sftp.listdir_attr.assert_not_called()

    def test_list_symlink_outside_marked(self):
        # escape -> /etc：stat 跟随解析是目录，normalize 解析出 home
        link = _make_attr("escape", stat.S_IFLNK | 0o777)
        self.sftp.listdir_attr.return_value = [link]
        self.sftp.stat.return_value = _make_attr(
            "etc", stat.S_IFDIR | 0o755)

        def fake_normalize(p):
            return "/etc" if p.endswith("/escape") else p
        self.sftp.normalize.side_effect = fake_normalize

        resp = self.client.get("/api/ssh/sessions/sid-1/sftp/list",
                               cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 200)
        entry = resp.json()["entries"][0]
        self.assertTrue(entry["is_symlink"])
        self.assertTrue(entry["is_dir"])  # 跟随链接，目标是目录
        self.assertTrue(entry["outside_home"])

    def test_list_broken_symlink(self):
        link = _make_attr("dead", stat.S_IFLNK | 0o777)
        self.sftp.listdir_attr.return_value = [link]
        self.sftp.stat.side_effect = OSError(2, "No such file")
        self.sftp.normalize.side_effect = lambda p: (_ for _ in ()).throw(
            OSError(2, "No such file")) if p.endswith("/dead") else p
        resp = self.client.get("/api/ssh/sessions/sid-1/sftp/list",
                               cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 200)
        entry = resp.json()["entries"][0]
        self.assertTrue(entry["broken"])
        self.assertFalse(entry["is_dir"])

    def test_list_session_not_found(self):
        self._no_session()
        resp = self.client.get("/api/ssh/sessions/nope/sftp/list",
                               cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 404)

    def test_list_unauthorized(self):
        resp = self.client.get("/api/ssh/sessions/sid-1/sftp/list")
        self.assertEqual(resp.status_code, 401)

    def test_list_missing_dir_404(self):
        self.sftp.listdir_attr.side_effect = OSError(2, "No such file")
        resp = self.client.get(
            "/api/ssh/sessions/sid-1/sftp/list?path=nodir",
            cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 404)


class TestDownload(_SFTPTestCase):
    def _setup_file(self, content=b"hello world", size=11):
        mock_file = MagicMock()
        mock_file.read.side_effect = [content, b""]
        self.sftp.open.return_value = mock_file
        self.sftp.stat.return_value = _make_attr(
            "a.txt", stat.S_IFREG | 0o644, size=size)
        return mock_file

    def test_download_success_streaming(self):
        mock_file = self._setup_file()
        resp = self.client.get(
            "/api/ssh/sessions/sid-1/sftp/download?path=a.txt",
            cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b"hello world")
        self.assertIn("attachment", resp.headers["content-disposition"])
        self.assertIn("a.txt", resp.headers["content-disposition"])
        mock_file.close.assert_called()  # 流完要关文件
        # 分块读：CHUNK_SIZE 传入 read
        mock_file.read.assert_called_with(sftp_module.CHUNK_SIZE)

    def test_download_symlink_outside_rejected(self):
        self.sftp.normalize.side_effect = lambda p: (
            "/etc/passwd" if p.endswith("escape") else p)
        resp = self.client.get(
            "/api/ssh/sessions/sid-1/sftp/download?path=escape",
            cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 403)
        self.sftp.open.assert_not_called()

    def test_download_directory_rejected(self):
        self.sftp.stat.return_value = _make_attr(
            "docs", stat.S_IFDIR | 0o755)
        resp = self.client.get(
            "/api/ssh/sessions/sid-1/sftp/download?path=docs",
            cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 400)

    def test_download_traversal_rejected(self):
        resp = self.client.get(
            "/api/ssh/sessions/sid-1/sftp/download?path=/etc/passwd",
            cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 400)

    def test_download_not_found(self):
        self._setup_file()
        self.sftp.stat.side_effect = OSError(2, "No such file")
        resp = self.client.get(
            "/api/ssh/sessions/sid-1/sftp/download?path=nope.txt",
            cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 404)

    def test_download_session_not_found_and_unauthorized(self):
        self._no_session()
        resp = self.client.get(
            "/api/ssh/sessions/nope/sftp/download?path=a.txt",
            cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 404)
        self.mock_manager.get.return_value = self.entry
        resp = self.client.get(
            "/api/ssh/sessions/sid-1/sftp/download?path=a.txt")
        self.assertEqual(resp.status_code, 401)


class TestUpload(_SFTPTestCase):
    def test_upload_success(self):
        mock_file = MagicMock()
        self.sftp.open.return_value = mock_file
        resp = self.client.post(
            "/api/ssh/sessions/sid-1/sftp/upload?path=docs",
            files={"file": ("up.txt", b"upload-content")},
            cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["path"], "/home/testuser/docs/up.txt")
        self.assertEqual(resp.json()["size"], 14)
        self.sftp.open.assert_called_once_with(
            "/home/testuser/docs/up.txt", "wb")
        mock_file.write.assert_called_once_with(b"upload-content")

    def test_upload_default_home(self):
        self.sftp.open.return_value = MagicMock()
        resp = self.client.post(
            "/api/ssh/sessions/sid-1/sftp/upload",
            files={"file": ("x.txt", b"1")},
            cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["path"], "/home/testuser/x.txt")

    def test_upload_oversize_413_and_cleanup(self):
        mock_file = MagicMock()
        self.sftp.open.return_value = mock_file
        with patch("web.sftp.MAX_UPLOAD_BYTES", 5):
            resp = self.client.post(
                "/api/ssh/sessions/sid-1/sftp/upload",
                files={"file": ("big.bin", b"0123456789")},
                cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 413)
        # 超限后删除已写一半的远端文件
        self.sftp.remove.assert_called_once_with("/home/testuser/big.bin")

    def test_upload_filename_traversal_sanitized(self):
        # 文件名里的 ../ 被 basename 剥掉，不会跳出目标目录
        self.sftp.open.return_value = MagicMock()
        resp = self.client.post(
            "/api/ssh/sessions/sid-1/sftp/upload?path=docs",
            files={"file": ("../../evil.txt", b"x")},
            cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 200)
        self.sftp.open.assert_called_once_with(
            "/home/testuser/docs/evil.txt", "wb")

    def test_upload_parent_symlink_outside_rejected(self):
        # 目标目录是跳出 home 的符号链接 -> 403
        self.sftp.normalize.side_effect = lambda p: (
            "/etc" if p.endswith("/escape") else p)
        resp = self.client.post(
            "/api/ssh/sessions/sid-1/sftp/upload?path=escape",
            files={"file": ("x.txt", b"1")},
            cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 403)
        self.sftp.open.assert_not_called()

    def test_upload_session_not_found_and_unauthorized(self):
        self._no_session()
        resp = self.client.post(
            "/api/ssh/sessions/nope/sftp/upload",
            files={"file": ("x.txt", b"1")}, cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 404)
        self.mock_manager.get.return_value = self.entry
        resp = self.client.post(
            "/api/ssh/sessions/sid-1/sftp/upload",
            files={"file": ("x.txt", b"1")})
        self.assertEqual(resp.status_code, 401)


class TestMkdirDeleteRename(_SFTPTestCase):
    def test_mkdir_with_name(self):
        resp = self.client.post(
            "/api/ssh/sessions/sid-1/sftp/mkdir",
            json={"path": "docs", "name": "newdir"},
            cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["path"], "/home/testuser/docs/newdir")
        self.sftp.mkdir.assert_called_once_with("/home/testuser/docs/newdir")

    def test_mkdir_path_only(self):
        resp = self.client.post(
            "/api/ssh/sessions/sid-1/sftp/mkdir",
            json={"path": "a/b"},
            cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 200)
        self.sftp.mkdir.assert_called_once_with("/home/testuser/a/b")

    def test_mkdir_bad_name(self):
        resp = self.client.post(
            "/api/ssh/sessions/sid-1/sftp/mkdir",
            json={"path": "", "name": "../evil"},
            cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 400)

    def test_mkdir_traversal(self):
        resp = self.client.post(
            "/api/ssh/sessions/sid-1/sftp/mkdir",
            json={"path": "../../tmp/x"},
            cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 400)
        self.sftp.mkdir.assert_not_called()

    def test_mkdir_exists(self):
        self.sftp.mkdir.side_effect = OSError(17, "File exists")
        resp = self.client.post(
            "/api/ssh/sessions/sid-1/sftp/mkdir",
            json={"path": "docs"},
            cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 400)

    def test_delete_file(self):
        self.sftp.stat.return_value = _make_attr(
            "a.txt", stat.S_IFREG | 0o644, size=1)
        resp = self.client.request(
            "DELETE", "/api/ssh/sessions/sid-1/sftp?path=a.txt",
            cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 200)
        self.sftp.remove.assert_called_once_with("/home/testuser/a.txt")
        self.sftp.rmdir.assert_not_called()

    def test_delete_empty_dir(self):
        self.sftp.stat.return_value = _make_attr(
            "d", stat.S_IFDIR | 0o755)
        resp = self.client.request(
            "DELETE", "/api/ssh/sessions/sid-1/sftp?path=d",
            cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 200)
        self.sftp.rmdir.assert_called_once_with("/home/testuser/d")

    def test_delete_nonempty_dir_400(self):
        self.sftp.stat.return_value = _make_attr(
            "d", stat.S_IFDIR | 0o755)
        self.sftp.rmdir.side_effect = OSError(39, "Directory not empty")
        resp = self.client.request(
            "DELETE", "/api/ssh/sessions/sid-1/sftp?path=d",
            cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 400)
        self.assertIn("非空", resp.json()["detail"])

    def test_delete_home_itself_rejected(self):
        resp = self.client.request(
            "DELETE", "/api/ssh/sessions/sid-1/sftp?path=.",
            cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 400)

    def test_delete_not_found(self):
        self.sftp.normalize.side_effect = OSError(2, "No such file")
        resp = self.client.request(
            "DELETE", "/api/ssh/sessions/sid-1/sftp?path=nope",
            cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 404)

    def test_rename_success(self):
        resp = self.client.post(
            "/api/ssh/sessions/sid-1/sftp/rename",
            json={"src": "a.txt", "dst": "b.txt"},
            cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 200)
        self.sftp.rename.assert_called_once_with(
            "/home/testuser/a.txt", "/home/testuser/b.txt")

    def test_rename_traversal(self):
        resp = self.client.post(
            "/api/ssh/sessions/sid-1/sftp/rename",
            json={"src": "a.txt", "dst": "/etc/passwd"},
            cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 400)
        self.sftp.rename.assert_not_called()

    def test_rename_dst_parent_symlink_outside(self):
        self.sftp.normalize.side_effect = lambda p: (
            "/etc" if p.endswith("/escape") else p)
        resp = self.client.post(
            "/api/ssh/sessions/sid-1/sftp/rename",
            json={"src": "a.txt", "dst": "escape/a.txt"},
            cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 403)

    def test_crud_session_not_found_and_unauthorized(self):
        self._no_session()
        resp = self.client.post(
            "/api/ssh/sessions/nope/sftp/mkdir", json={"path": "x"},
            cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 404)
        resp = self.client.request(
            "DELETE", "/api/ssh/sessions/nope/sftp?path=x",
            cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 404)
        resp = self.client.post(
            "/api/ssh/sessions/nope/sftp/rename",
            json={"src": "a", "dst": "b"}, cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 404)
        self.mock_manager.get.return_value = self.entry
        resp = self.client.post(
            "/api/ssh/sessions/sid-1/sftp/mkdir", json={"path": "x"})
        self.assertEqual(resp.status_code, 401)
        resp = self.client.request(
            "DELETE", "/api/ssh/sessions/sid-1/sftp?path=x")
        self.assertEqual(resp.status_code, 401)
        resp = self.client.post(
            "/api/ssh/sessions/sid-1/sftp/rename",
            json={"src": "a", "dst": "b"})
        self.assertEqual(resp.status_code, 401)


class TestFileReadWrite(_SFTPTestCase):
    def _setup_text_file(self, content=b"line1\nline2\n", size=None):
        mock_file = MagicMock()
        mock_file.__enter__.return_value.read.return_value = content
        self.sftp.open.return_value = mock_file
        self.sftp.stat.return_value = _make_attr(
            "a.txt", stat.S_IFREG | 0o644,
            size=size if size is not None else len(content))
        return mock_file

    def test_read_file_success(self):
        self._setup_text_file()
        resp = self.client.get(
            "/api/ssh/sessions/sid-1/sftp/file?path=a.txt",
            cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["content"], "line1\nline2\n")
        self.assertEqual(resp.json()["size"], 12)

    def test_read_file_no_extension_allowed(self):
        self._setup_text_file(b"#!/bin/sh\n")
        resp = self.client.get(
            "/api/ssh/sessions/sid-1/sftp/file?path=script",
            cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 200)

    def test_read_file_binary_ext_415(self):
        resp = self.client.get(
            "/api/ssh/sessions/sid-1/sftp/file?path=photo.png",
            cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 415)
        self.sftp.open.assert_not_called()

    def test_read_file_oversize_413(self):
        self._setup_text_file(size=sftp_module.MAX_EDIT_BYTES + 1)
        resp = self.client.get(
            "/api/ssh/sessions/sid-1/sftp/file?path=big.txt",
            cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 413)

    def test_read_file_not_utf8_400(self):
        self._setup_text_file(content=b"\xff\xfe\x00\x01")
        resp = self.client.get(
            "/api/ssh/sessions/sid-1/sftp/file?path=a.txt",
            cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 400)

    def test_read_file_symlink_outside_403(self):
        self.sftp.normalize.side_effect = lambda p: (
            "/etc/passwd" if p.endswith("escape") else p)
        resp = self.client.get(
            "/api/ssh/sessions/sid-1/sftp/file?path=escape",
            cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 403)

    def test_write_file_success(self):
        mock_file = MagicMock()
        self.sftp.open.return_value = mock_file
        resp = self.client.put(
            "/api/ssh/sessions/sid-1/sftp/file",
            json={"path": "a.txt", "content": "new content"},
            cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["size"], 11)
        mock_file.__enter__.return_value.write.assert_called_once_with(
            b"new content")

    def test_write_new_file_allowed(self):
        # 新文件：normalize 抛 errno 2，改查父目录后允许创建
        def fake_normalize(p):
            if p.endswith("/new.txt"):
                raise OSError(2, "No such file")
            return p
        self.sftp.normalize.side_effect = fake_normalize
        mock_file = MagicMock()
        self.sftp.open.return_value = mock_file
        resp = self.client.put(
            "/api/ssh/sessions/sid-1/sftp/file",
            json={"path": "new.txt", "content": "hi"},
            cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 200)

    def test_write_file_oversize_413(self):
        with patch("web.sftp.MAX_EDIT_BYTES", 5):
            resp = self.client.put(
                "/api/ssh/sessions/sid-1/sftp/file",
                json={"path": "a.txt", "content": "0123456789"},
                cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 413)

    def test_write_file_binary_ext_415(self):
        resp = self.client.put(
            "/api/ssh/sessions/sid-1/sftp/file",
            json={"path": "a.png", "content": "x"},
            cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 415)

    def test_file_session_not_found_and_unauthorized(self):
        self._no_session()
        resp = self.client.get(
            "/api/ssh/sessions/nope/sftp/file?path=a.txt",
            cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 404)
        resp = self.client.put(
            "/api/ssh/sessions/nope/sftp/file",
            json={"path": "a.txt", "content": "x"},
            cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 404)
        self.mock_manager.get.return_value = self.entry
        resp = self.client.get("/api/ssh/sessions/sid-1/sftp/file?path=a.txt")
        self.assertEqual(resp.status_code, 401)
        resp = self.client.put(
            "/api/ssh/sessions/sid-1/sftp/file",
            json={"path": "a.txt", "content": "x"})
        self.assertEqual(resp.status_code, 401)


class TestSFTPClientLifecycle(_SFTPTestCase):
    """SFTPClient 缓存 / 不可用 502 / 会话关闭时一并关闭。"""

    def test_sftp_created_once_and_cached(self):
        # entry 里没有缓存的 sftp：首次调用创建，第二次复用
        mock_client = MagicMock()
        transport = MagicMock()
        transport.is_active.return_value = True
        mock_client.get_transport.return_value = transport
        mock_session = MagicMock()
        mock_session.client = mock_client
        entry = {"session": mock_session, "sftp": None,
                 "sftp_home": HOME}
        self.mock_manager.get.return_value = entry
        self.sftp.listdir_attr.return_value = []
        with patch.object(paramiko.SFTPClient, "from_transport",
                          return_value=self.sftp) as mock_from:
            resp1 = self.client.get("/api/ssh/sessions/sid-1/sftp/list",
                                    cookies=self._auth_cookies())
            resp2 = self.client.get("/api/ssh/sessions/sid-1/sftp/list",
                                    cookies=self._auth_cookies())
        self.assertEqual(resp1.status_code, 200)
        self.assertEqual(resp2.status_code, 200)
        mock_from.assert_called_once_with(transport)
        self.assertIs(entry["sftp"], self.sftp)

    def test_sftp_home_cached_via_normalize(self):
        mock_client = MagicMock()
        transport = MagicMock()
        transport.is_active.return_value = True
        mock_client.get_transport.return_value = transport
        mock_session = MagicMock()
        mock_session.client = mock_client
        entry = {"session": mock_session, "sftp": self.sftp,
                 "sftp_home": None}
        self.mock_manager.get.return_value = entry
        self.sftp.normalize.side_effect = lambda p: (
            HOME if p == "." else p)
        self.sftp.listdir_attr.return_value = []
        resp = self.client.get("/api/ssh/sessions/sid-1/sftp/list",
                               cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(entry["sftp_home"], HOME)

    def test_sftp_unavailable_502(self):
        mock_client = MagicMock()
        transport = MagicMock()
        transport.is_active.return_value = True
        mock_client.get_transport.return_value = transport
        mock_session = MagicMock()
        mock_session.client = mock_client
        entry = {"session": mock_session, "sftp": None,
                 "sftp_home": HOME}
        self.mock_manager.get.return_value = entry
        with patch.object(paramiko.SFTPClient, "from_transport",
                          side_effect=paramiko.SSHException("no sftp")):
            resp = self.client.get("/api/ssh/sessions/sid-1/sftp/list",
                                   cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 502)
        self.assertIn("SFTP", resp.json()["detail"])
        # 502 里不能漏 paramiko 原始异常细节
        self.assertNotIn("no sftp", resp.json()["detail"])

    def test_dead_transport_502(self):
        mock_client = MagicMock()
        transport = MagicMock()
        transport.is_active.return_value = False
        mock_client.get_transport.return_value = transport
        mock_session = MagicMock()
        mock_session.client = mock_client
        entry = {"session": mock_session, "sftp": None,
                 "sftp_home": HOME}
        self.mock_manager.get.return_value = entry
        resp = self.client.get("/api/ssh/sessions/sid-1/sftp/list",
                               cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 502)

    def test_close_session_closes_cached_sftp(self):
        # 真实 manager：close_session 要一并关闭 entry["sftp"]
        mgr = SSHSessionManager(start_cleanup_thread=False)
        mock_session = MagicMock()
        mock_sftp = MagicMock()
        mgr._sessions["sid-1"] = {
            "session": mock_session, "sftp": mock_sftp,
            "sftp_home": HOME,
        }
        self.assertTrue(mgr.close_session("sid-1"))
        mock_sftp.close.assert_called_once()
        mock_session.close.assert_called_once()

    def test_close_session_without_sftp_key_ok(self):
        # 老记录没有 "sftp" 键时 close_session 也不能报错
        mgr = SSHSessionManager(start_cleanup_thread=False)
        mock_session = MagicMock()
        mgr._sessions["sid-1"] = {"session": mock_session}
        self.assertTrue(mgr.close_session("sid-1"))
        mock_session.close.assert_called_once()


# ================= 真实回环 SFTP 端到端测试 =================

class _LoopbackSFTPInterface(paramiko.SFTPServerInterface):
    """最小 SFTP 服务器实现：背后是一个临时目录（家目录）。

    canonicalize 用 os.path.realpath 解析符号链接，与真实 sftp-server
    行为一致（客户端靠它识别跳出 home 的链接）。
    """

    def __init__(self, server, root):
        self.root = root

    def _real(self, path):
        """远端绝对路径 -> 本地真实路径。"""
        if os.path.isabs(path):
            if path == self.root or path.startswith(self.root + os.sep):
                rel = os.path.relpath(path, self.root)
                return os.path.join(self.root, rel)
            return path  # 家目录外：原样（仅测试，客户端校验会挡）
        return os.path.join(self.root, path)

    def canonicalize(self, path):
        if path in (".", ""):
            return os.path.realpath(self.root)
        mapped = self._real(path) if os.path.isabs(path) \
            else os.path.join(self.root, path)
        return os.path.realpath(mapped)

    def list_folder(self, path):
        real = self._real(path)
        out = []
        for name in os.listdir(real):
            attr = paramiko.SFTPAttributes.from_stat(
                os.lstat(os.path.join(real, name)))
            attr.filename = name
            out.append(attr)
        return out

    def stat(self, path):
        return paramiko.SFTPAttributes.from_stat(os.stat(self._real(path)))

    def lstat(self, path):
        return paramiko.SFTPAttributes.from_stat(os.lstat(self._real(path)))

    def open(self, path, flags, attr):
        real = self._real(path)
        fd = os.open(real, flags, 0o644)
        if flags & os.O_RDWR:
            mode = "r+b"
        elif flags & os.O_WRONLY:
            mode = "ab" if (flags & os.O_APPEND) else "wb"
        else:
            mode = "rb"
        f = os.fdopen(fd, mode)
        handle = paramiko.SFTPHandle(flags)
        handle.readfile = f
        handle.writefile = f
        return handle

    # 注意：paramiko 的 SFTPServer._process 会把这几个方法的返回值当
    # SFTP 状态码传给 _send_status，必须返回 SFTP_OK（返回 None 会
    # 让服务端抛 TypeError，客户端只看到通用 Failure）。
    def mkdir(self, path, attr=None):
        os.mkdir(self._real(path))
        return paramiko.SFTP_OK

    def rmdir(self, path):
        os.rmdir(self._real(path))
        return paramiko.SFTP_OK

    def remove(self, path):
        os.remove(self._real(path))
        return paramiko.SFTP_OK

    def rename(self, oldpath, newpath):
        os.rename(self._real(oldpath), self._real(newpath))
        return paramiko.SFTP_OK


class _LoopbackSFTPServer(paramiko.ServerInterface):
    """进程内最小 SSH 服务器：密码认证 + sftp 子系统。

    注意：不要覆写 check_channel_subsystem_request——默认实现会查
    set_subsystem_handler 注册的表并启动 SFTPServer 线程；覆写成
    只返回 True 会让子系统请求成功却无人处理，客户端永远等不到
    SFTP 版本回应而挂死。
    """

    def check_auth_password(self, username, password):
        if username == "testuser" and password == "testpass":
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED


class TestLoopbackSFTP(unittest.TestCase):
    """真实 SFTP 协议回环测试：paramiko 起服务器，API 真走 SFTP。

    不依赖系统 sshd，只用 127.0.0.1 随机高端口 + 临时目录。
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = os.path.realpath(cls._tmp.name)
        # 预置文件：hello.txt + 空子目录 + 跳出 home 的符号链接
        with open(os.path.join(cls.root, "hello.txt"), "w") as f:
            f.write("hello sftp\n")
        os.mkdir(os.path.join(cls.root, "subdir"))
        os.symlink("/etc", os.path.join(cls.root, "escape"))

        cls.host_key = paramiko.RSAKey.generate(1024)
        cls.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        cls.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        cls.sock.bind(("127.0.0.1", 0))
        cls.sock.listen(5)
        cls.sock.settimeout(0.5)
        cls.port = cls.sock.getsockname()[1]
        cls.stop_event = threading.Event()
        cls.thread = threading.Thread(target=cls._serve, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.stop_event.set()
        cls.thread.join(timeout=15)
        cls.sock.close()
        cls._tmp.cleanup()

    @classmethod
    def _serve(cls):
        while not cls.stop_event.is_set():
            try:
                conn, _ = cls.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            transport = paramiko.Transport(conn)
            transport.add_server_key(cls.host_key)
            transport.set_subsystem_handler(
                "sftp", paramiko.SFTPServer,
                _LoopbackSFTPInterface, cls.root)
            try:
                transport.start_server(server=_LoopbackSFTPServer())
            except Exception:
                transport.close()
                continue
            # 保持 transport 活着，直到客户端断开或测试结束
            while transport.is_active() and not cls.stop_event.is_set():
                threading.Event().wait(0.1)
            transport.close()

    def setUp(self):
        self._auth_patch = patch("web.ssh.session_store", _StubSessionStore())
        self._auth_patch.start()
        self.addCleanup(self._auth_patch.stop)
        self._mgr_patch = patch("web.sftp.manager")
        self.mock_manager = self._mgr_patch.start()
        self.addCleanup(self._mgr_patch.stop)
        self.client = TestClient(app, raise_server_exceptions=False)

        # 真实 SSHClient 连回环服务器，拼一条 manager 记录
        self.ssh_client = paramiko.SSHClient()
        self.ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.ssh_client.connect("127.0.0.1", self.port,
                                username="testuser", password="testpass")
        self.entry = {"session": SimpleNamespace(client=self.ssh_client),
                      "sftp": None, "sftp_home": None}
        self.mock_manager.get.return_value = self.entry

    def tearDown(self):
        sftp = self.entry.get("sftp")
        if sftp is not None:
            try:
                sftp.close()
            except Exception:
                pass
        self.ssh_client.close()

    def _cookies(self):
        return {"rbot_session": "good-token"}

    def test_full_flow_list_upload_download_rename_delete(self):
        # 1. list：家目录就是临时目录，预置文件都在
        resp = self.client.get("/api/ssh/sessions/sid-1/sftp/list",
                               cookies=self._cookies())
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["home"], self.root)
        names = {e["name"]: e for e in data["entries"]}
        self.assertIn("hello.txt", names)
        self.assertIn("subdir", names)
        self.assertTrue(names["subdir"]["is_dir"])
        # 跳出 home 的符号链接被标记
        self.assertTrue(names["escape"]["is_symlink"])
        self.assertTrue(names["escape"]["outside_home"])

        # 2. upload
        resp = self.client.post(
            "/api/ssh/sessions/sid-1/sftp/upload",
            files={"file": ("up.txt", b"uploaded-by-test")},
            cookies=self._cookies())
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(os.path.exists(os.path.join(self.root, "up.txt")))

        # 3. download：内容一致
        resp = self.client.get(
            "/api/ssh/sessions/sid-1/sftp/download?path=up.txt",
            cookies=self._cookies())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b"uploaded-by-test")

        # 4. rename
        resp = self.client.post(
            "/api/ssh/sessions/sid-1/sftp/rename",
            json={"src": "up.txt", "dst": "renamed.txt"},
            cookies=self._cookies())
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(os.path.exists(os.path.join(self.root, "up.txt")))
        self.assertTrue(os.path.exists(os.path.join(self.root, "renamed.txt")))

        # 5. delete
        resp = self.client.request(
            "DELETE", "/api/ssh/sessions/sid-1/sftp?path=renamed.txt",
            cookies=self._cookies())
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(os.path.exists(os.path.join(self.root, "renamed.txt")))

    def test_loopback_mkdir_edit_and_symlink_blocked(self):
        # mkdir + 在线编辑（PUT 新文件 + GET 读回）
        resp = self.client.post(
            "/api/ssh/sessions/sid-1/sftp/mkdir",
            json={"path": "subdir", "name": "inner"},
            cookies=self._cookies())
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(os.path.isdir(
            os.path.join(self.root, "subdir", "inner")))

        resp = self.client.put(
            "/api/ssh/sessions/sid-1/sftp/file",
            json={"path": "subdir/note.txt", "content": "回环编辑测试\n"},
            cookies=self._cookies())
        self.assertEqual(resp.status_code, 200)
        resp = self.client.get(
            "/api/ssh/sessions/sid-1/sftp/file?path=subdir/note.txt",
            cookies=self._cookies())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["content"], "回环编辑测试\n")

        # 真实符号链接跳出：下载 escape/passwd 必须被 403 挡住
        resp = self.client.get(
            "/api/ssh/sessions/sid-1/sftp/download?path=escape/passwd",
            cookies=self._cookies())
        self.assertEqual(resp.status_code, 403)

        # 路径穿越：下载 /etc/passwd 必须被 400 挡住
        resp = self.client.get(
            "/api/ssh/sessions/sid-1/sftp/download?path=/etc/passwd",
            cookies=self._cookies())
        self.assertEqual(resp.status_code, 400)

        # 清理：删掉本测试建的文件/目录
        self.client.request(
            "DELETE", "/api/ssh/sessions/sid-1/sftp?path=subdir/note.txt",
            cookies=self._cookies())
        self.client.request(
            "DELETE", "/api/ssh/sessions/sid-1/sftp?path=subdir/inner",
            cookies=self._cookies())


if __name__ == "__main__":
    unittest.main()
