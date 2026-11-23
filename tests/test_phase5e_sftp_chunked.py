#!/usr/bin/env python3
"""阶段 5 工作包 E：SFTP 分块上传带进度的测试。

覆盖：init/chunk/progress/complete/cancel 全流程（/api/ssh/sessions 与
/api/terminal 两种前缀）、分块校验、临时分块清理、超期清理、路径校验。
SFTP 层全部 mock，不碰真实服务器；临时分块写临时 DATA_DIR。
"""

import io
import json
import os
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI
from fastapi.testclient import TestClient

import web.sftp as sftp_module
from web.sftp import CHUNK_UPLOAD_SIZE, MAX_UPLOAD_BYTES, router

HOME = "/home/testuser"
SESSION_ID = "session-1234"

app = FastAPI()
app.include_router(router)


class _StubSessionStore:
    """测试用认证存储：只有 token=<redacted> 能登录。"""

    def get(self, token):
        return {"username": "tester"} if token == "good-token" else None


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


class ChunkedUploadTestCase(unittest.TestCase):
    """分块上传测试基类：认证走 stub，manager 走 mock，DATA_DIR 走临时目录。"""

    def setUp(self):
        self._auth_patch = patch("web.ssh.session_store", _StubSessionStore())
        self._auth_patch.start()
        self.addCleanup(self._auth_patch.stop)
        self._mgr_patch = patch("web.sftp.manager")
        self.mock_manager = self._mgr_patch.start()
        self.addCleanup(self._mgr_patch.stop)
        self.mock_sftp = _make_mock_sftp()
        self.mock_manager.get.return_value = _make_entry(self.mock_sftp)
        self._tmp = tempfile.TemporaryDirectory()
        self._data_dir_patcher = patch("web.config.DATA_DIR", self._tmp.name)
        self._data_dir_patcher.start()
        self.addCleanup(self._data_dir_patcher.stop)
        self.addCleanup(self._tmp.cleanup)
        self.client = TestClient(app)
        self.headers = {"Cookie": "rbot_session=good-token"}

    def _init(self, filename="big.bin", size=2 * CHUNK_UPLOAD_SIZE + 100,
              path=None, prefix="/api/ssh/sessions"):
        body = {"filename": filename, "size": size}
        if path is not None:
            body["path"] = path
        return self.client.post(
            f"{prefix}/{SESSION_ID}/sftp/upload/init",
            json=body, headers=self.headers)

    def _chunk(self, upload_id, index, data, prefix="/api/ssh/sessions"):
        return self.client.post(
            f"{prefix}/{SESSION_ID}/sftp/upload/chunk"
            f"?uploadId={upload_id}&chunkIndex={index}",
            files={"file": ("chunk", io.BytesIO(data))},
            headers=self.headers)

    def _upload_all_chunks(self, upload_id, size, prefix="/api/ssh/sessions"):
        """按 CHUNK_UPLOAD_SIZE 把 size 字节全部分块上传（内容为 b'x'）。"""
        total_chunks = (size + CHUNK_UPLOAD_SIZE - 1) // CHUNK_UPLOAD_SIZE
        for i in range(total_chunks):
            if i == total_chunks - 1:
                n = size - i * CHUNK_UPLOAD_SIZE
            else:
                n = CHUNK_UPLOAD_SIZE
            r = self._chunk(upload_id, i, b"x" * n, prefix=prefix)
            self.assertEqual(r.status_code, 200, r.text)
        return total_chunks


class TestChunkedUploadFlow(ChunkedUploadTestCase):
    """init -> chunk -> progress -> complete 全流程。"""

    def test_full_flow(self):
        size = 2 * CHUNK_UPLOAD_SIZE + 100
        r = self._init(size=size)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        upload_id = body["uploadId"]
        self.assertEqual(body["chunkSize"], CHUNK_UPLOAD_SIZE)
        self.assertEqual(body["totalChunks"], 3)

        # 分块上传：第一块后进度约 1/3。
        r = self._chunk(upload_id, 0, b"x" * CHUNK_UPLOAD_SIZE)
        self.assertEqual(r.status_code, 200)
        progress = r.json()
        self.assertEqual(progress["receivedBytes"], CHUNK_UPLOAD_SIZE)
        self.assertEqual(progress["totalBytes"], size)
        self.assertEqual(progress["chunksReceived"], 1)
        self.assertEqual(progress["totalChunks"], 3)
        self.assertAlmostEqual(progress["percent"],
                               100.0 * CHUNK_UPLOAD_SIZE / size, places=1)

        # progress 接口。
        r = self.client.get(
            f"/api/ssh/sessions/{SESSION_ID}/sftp/upload/progress"
            f"?uploadId={upload_id}", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["chunksReceived"], 1)

        # 传完剩余块。
        self._chunk(upload_id, 1, b"x" * CHUNK_UPLOAD_SIZE)
        self._chunk(upload_id, 2, b"x" * 100)

        # complete：远端按序写入，临时分块清理。
        remote_file = MagicMock()
        self.mock_sftp.open.return_value = remote_file
        r = self.client.post(
            f"/api/ssh/sessions/{SESSION_ID}/sftp/upload/complete",
            json={"uploadId": upload_id}, headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["size"], size)
        self.assertEqual(r.json()["path"], f"{HOME}/big.bin")
        written = b"".join(
            call.args[0] for call in remote_file.write.call_args_list)
        self.assertEqual(len(written), size)
        # 临时目录已清理。
        upload_dir = os.path.join(self._tmp.name, "sftp_uploads", upload_id)
        self.assertFalse(os.path.exists(upload_dir))

    def test_terminal_prefix_works(self):
        """R-Bot 风格的 /api/terminal/{id}/sftp/upload/* 前缀同样可用。"""
        size = 100
        r = self._init(size=size, prefix="/api/terminal")
        self.assertEqual(r.status_code, 200)
        upload_id = r.json()["uploadId"]
        r = self._chunk(upload_id, 0, b"x" * 100, prefix="/api/terminal")
        self.assertEqual(r.status_code, 200)
        self.mock_sftp.open.return_value = MagicMock()
        r = self.client.post(
            f"/api/terminal/{SESSION_ID}/sftp/upload/complete",
            json={"uploadId": upload_id}, headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)

    def test_reupload_chunk_overwrites(self):
        """同一 chunkIndex 重传时覆盖，不重复计字节。"""
        size = CHUNK_UPLOAD_SIZE + 10
        upload_id = self._init(size=size).json()["uploadId"]
        self._chunk(upload_id, 0, b"a" * CHUNK_UPLOAD_SIZE)
        r = self._chunk(upload_id, 0, b"b" * CHUNK_UPLOAD_SIZE)
        self.assertEqual(r.json()["receivedBytes"], CHUNK_UPLOAD_SIZE)
        self.assertEqual(r.json()["chunksReceived"], 1)

    def test_complete_incomplete_400(self):
        """分块不齐时 complete 返回 400，临时分块保留（可重试）。"""
        size = 2 * CHUNK_UPLOAD_SIZE
        upload_id = self._init(size=size).json()["uploadId"]
        self._chunk(upload_id, 0, b"x" * CHUNK_UPLOAD_SIZE)
        r = self.client.post(
            f"/api/ssh/sessions/{SESSION_ID}/sftp/upload/complete",
            json={"uploadId": upload_id}, headers=self.headers)
        self.assertEqual(r.status_code, 400)
        self.assertIn("分块不完整", r.json()["detail"])
        upload_dir = os.path.join(self._tmp.name, "sftp_uploads", upload_id)
        self.assertTrue(os.path.exists(upload_dir))


class TestChunkedUploadValidation(ChunkedUploadTestCase):
    """分块上传的校验与边界。"""

    def test_init_oversize_413(self):
        r = self._init(size=MAX_UPLOAD_BYTES + 1)
        self.assertEqual(r.status_code, 413)

    def test_init_zero_size_400(self):
        r = self._init(size=0)
        self.assertEqual(r.status_code, 400)

    def test_init_bad_filename_400(self):
        r = self._init(filename="../evil")
        # basename 后为 "evil"，合法；真正非法的是空名与点号名。
        self.assertEqual(r.status_code, 200)
        r = self._init(filename="..")
        self.assertEqual(r.status_code, 400)

    def test_init_path_escape_400(self):
        r = self._init(path="../../etc")
        self.assertEqual(r.status_code, 400)

    def test_chunk_index_out_of_range_400(self):
        upload_id = self._init(size=100).json()["uploadId"]
        r = self._chunk(upload_id, 1, b"x" * 10)
        self.assertEqual(r.status_code, 400)
        r = self._chunk(upload_id, -1, b"x" * 10)
        self.assertEqual(r.status_code, 400)

    def test_chunk_wrong_size_400(self):
        """分块字节数与应有大小不符时 400。"""
        upload_id = self._init(size=100).json()["uploadId"]
        r = self._chunk(upload_id, 0, b"x" * 50)
        self.assertEqual(r.status_code, 400)
        r = self._chunk(upload_id, 0, b"x" * 101)
        self.assertEqual(r.status_code, 400)

    def test_unknown_upload_404(self):
        r = self.client.get(
            f"/api/ssh/sessions/{SESSION_ID}/sftp/upload/progress"
            f"?uploadId={'0' * 32}", headers=self.headers)
        self.assertEqual(r.status_code, 404)

    def test_bad_upload_id_404(self):
        """uploadId 含路径穿越字符时按不存在处理（不拼进路径）。"""
        r = self.client.get(
            f"/api/ssh/sessions/{SESSION_ID}/sftp/upload/progress"
            f"?uploadId=..%2F..%2Fetc", headers=self.headers)
        self.assertEqual(r.status_code, 404)

    def test_wrong_session_404(self):
        """上传任务不属于该会话时 404。"""
        upload_id = self._init(size=100).json()["uploadId"]
        r = self.client.get(
            "/api/ssh/sessions/other-session/sftp/upload/progress"
            f"?uploadId={upload_id}", headers=self.headers)
        self.assertEqual(r.status_code, 404)

    def test_no_session_404(self):
        self.mock_manager.get.return_value = None
        r = self._init(size=100)
        self.assertEqual(r.status_code, 404)

    def test_anon_401(self):
        r = self.client.post(
            f"/api/ssh/sessions/{SESSION_ID}/sftp/upload/init",
            json={"filename": "a.bin", "size": 10})
        self.assertEqual(r.status_code, 401)


class TestChunkedUploadCancel(ChunkedUploadTestCase):
    """取消上传：需 confirm=true，删除临时分块。"""

    def test_cancel_requires_confirm(self):
        upload_id = self._init(size=100).json()["uploadId"]
        r = self.client.post(
            f"/api/ssh/sessions/{SESSION_ID}/sftp/upload/cancel",
            json={"uploadId": upload_id}, headers=self.headers)
        self.assertEqual(r.status_code, 400)
        # 未取消，任务还在。
        r = self.client.get(
            f"/api/ssh/sessions/{SESSION_ID}/sftp/upload/progress"
            f"?uploadId={upload_id}", headers=self.headers)
        self.assertEqual(r.status_code, 200)

    def test_cancel_removes_temp(self):
        upload_id = self._init(size=100).json()["uploadId"]
        self._chunk(upload_id, 0, b"x" * 100)
        upload_dir = os.path.join(self._tmp.name, "sftp_uploads", upload_id)
        self.assertTrue(os.path.exists(upload_dir))
        r = self.client.post(
            f"/api/ssh/sessions/{SESSION_ID}/sftp/upload/cancel",
            json={"uploadId": upload_id, "confirm": True},
            headers=self.headers)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(os.path.exists(upload_dir))
        # 再查进度已是 404。
        r = self.client.get(
            f"/api/ssh/sessions/{SESSION_ID}/sftp/upload/progress"
            f"?uploadId={upload_id}", headers=self.headers)
        self.assertEqual(r.status_code, 404)


class TestStaleUploadCleanup(ChunkedUploadTestCase):
    """超期临时分块由 init 时顺带清理。"""

    def test_stale_dir_removed_on_init(self):
        # 手工造一个超期（25 小时前）的上传目录。
        stale_id = "a" * 32
        stale_dir = os.path.join(self._tmp.name, "sftp_uploads", stale_id)
        os.makedirs(stale_dir)
        with open(os.path.join(stale_dir, "meta.json"), "w") as f:
            json.dump({"uploadId": stale_id, "sessionId": SESSION_ID,
                       "filename": "old.bin", "size": 10,
                       "createdAt": time.time() - 25 * 3600,
                       "chunks": []}, f)
        with open(os.path.join(stale_dir, "0.part"), "wb") as f:
            f.write(b"x" * 10)
        # 新建一次上传会触发清理。
        r = self._init(size=100)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(os.path.exists(stale_dir))

    def test_fresh_dir_kept_on_init(self):
        fresh_id = "b" * 32
        fresh_dir = os.path.join(self._tmp.name, "sftp_uploads", fresh_id)
        os.makedirs(fresh_dir)
        with open(os.path.join(fresh_dir, "meta.json"), "w") as f:
            json.dump({"uploadId": fresh_id, "sessionId": SESSION_ID,
                       "filename": "new.bin", "size": 10,
                       "createdAt": time.time(), "chunks": []}, f)
        r = self._init(size=100)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(os.path.exists(fresh_dir))


if __name__ == "__main__":
    unittest.main()
