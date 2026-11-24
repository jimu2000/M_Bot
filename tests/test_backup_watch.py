"""备份创建完成通知（tgbot/backup_watch.py）的测试。"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tgbot import backup_watch as bw

FAKE_BACKUP = "ocid1.bootvolumebackup.oc1..testxxxx"


class TestBackupWatchThread(unittest.TestCase):
    """_backup_watch_thread：轮询结果双通道通知。"""

    @patch("tgbot.backup_watch.notify_dual")
    @patch("tgbot.backup_watch.wait_for_backup_available",
           return_value="AVAILABLE")
    def test_success_notifies(self, _mock_wait, mock_notify):
        bw._backup_watch_thread(MagicMock(), FAKE_BACKUP, "my-inst",
                                "my-backup", "token", 123)
        mock_notify.assert_called_once()
        args, kwargs = mock_notify.call_args
        self.assertEqual(args[0], "token")
        self.assertEqual(args[1], 123)
        self.assertIn("备份已完成", args[2])
        self.assertIn("my-inst", args[3])
        self.assertIn("my-backup", args[3])
        self.assertEqual(kwargs.get("group"), "bootbackup")

    @patch("tgbot.backup_watch.notify_dual")
    @patch("tgbot.backup_watch.wait_for_backup_available",
           return_value="FAULTY")
    def test_failure_notifies_with_state(self, _mock_wait, mock_notify):
        bw._backup_watch_thread(MagicMock(), FAKE_BACKUP, "my-inst",
                                "my-backup", "token", 123)
        args, _kwargs = mock_notify.call_args
        self.assertIn("备份失败", args[2])
        self.assertIn("FAULTY", args[3])

    @patch("tgbot.backup_watch.notify_dual")
    @patch("tgbot.backup_watch.wait_for_backup_available",
           side_effect=TimeoutError("timeout"))
    def test_timeout_notifies(self, _mock_wait, mock_notify):
        bw._backup_watch_thread(MagicMock(), FAKE_BACKUP, "my-inst",
                                "my-backup", "token", 123)
        args, _kwargs = mock_notify.call_args
        self.assertIn("备份超时", args[2])
        self.assertIn("30 分钟", args[3])

    @patch("tgbot.backup_watch.notify_dual")
    @patch("tgbot.backup_watch.wait_for_backup_available",
           side_effect=Exception("boom"))
    def test_lookup_error_notifies_unknown(self, _mock_wait, mock_notify):
        bw._backup_watch_thread(MagicMock(), FAKE_BACKUP, "my-inst",
                                "my-backup", "token", 123)
        args, _kwargs = mock_notify.call_args
        self.assertIn("备份状态未知", args[2])


class TestStartBackupWatch(unittest.TestCase):
    """start_backup_watch：缺 token/chat_id/备份 id 时不启动。"""

    @patch("tgbot.backup_watch.threading.Thread")
    def test_missing_token_no_thread(self, mock_thread):
        result = bw.start_backup_watch(MagicMock(), FAKE_BACKUP, "i", "b",
                                       "", 123)
        self.assertIsNone(result)
        mock_thread.assert_not_called()

    @patch("tgbot.backup_watch.threading.Thread")
    def test_missing_chat_id_no_thread(self, mock_thread):
        result = bw.start_backup_watch(MagicMock(), FAKE_BACKUP, "i", "b",
                                       "token", None)
        self.assertIsNone(result)
        mock_thread.assert_not_called()

    @patch("tgbot.backup_watch.threading.Thread")
    def test_missing_backup_id_no_thread(self, mock_thread):
        result = bw.start_backup_watch(MagicMock(), "", "i", "b",
                                       "token", 123)
        self.assertIsNone(result)
        mock_thread.assert_not_called()

    @patch("tgbot.backup_watch.threading.Thread")
    def test_starts_daemon_thread(self, mock_thread):
        thread = mock_thread.return_value
        result = bw.start_backup_watch(MagicMock(), FAKE_BACKUP, "i", "b",
                                       "token", 123)
        self.assertIs(result, thread)
        mock_thread.assert_called_once()
        self.assertTrue(mock_thread.call_args.kwargs.get("daemon"))
        self.assertEqual(mock_thread.call_args.kwargs.get("target"),
                         bw._backup_watch_thread)
        thread.start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
