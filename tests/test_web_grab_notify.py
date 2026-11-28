#!/usr/bin/env python3
"""web/tasks._notify_grab_success 的单元测试。

抢机成功推送的网页版分支：等 IP（wait_for_public_ip）与跟进线程
（start_ip_followup），与机器人端 _notify_boot_success 同逻辑。
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oci.grabber import GrabResult
from tgbot.instance_credentials import get_root_password
from web import tasks as web_tasks


class TestNotifyGrabSuccess(unittest.TestCase):

    def setUp(self):
        # Redirect the instance credential store to a temp file: the
        # notify path saves the root password on success, and tests must
        # not write it to the real data directory. 凭据库指到临时文件：
        # 通知成功后会保存 root 密码，测试不能写真实数据目录。
        self._cred_tmp = tempfile.TemporaryDirectory()
        self._cred_patch = patch(
            "tgbot.instance_credentials.CREDENTIALS_FILE",
            os.path.join(self._cred_tmp.name,
                         "instance_credentials.json"))
        self._cred_patch.start()
        self.addCleanup(self._cred_patch.stop)
        self.addCleanup(self._cred_tmp.cleanup)
        self.params = {"display_name": "AMD_2026-01-02",
                       "shape": "VM.Standard.E2.1.Micro",
                       "ocpus": 1, "memory_gb": 1,
                       "boot_volume_size_gbs": 47,
                       "ssh_keys_file": "/root/keys.pub"}
        self.notify = {"token": "T", "chat_id": 654321,
                       "profile_label": "圣何塞", "region_label": "圣何塞",
                       "root_password": "pw123",
                       "region_code": "us-sanjose-1"}
        self.result = GrabResult(instance_id="ocid1.instance.oc1..aaa1",
                                 releases=0, attempts=1, elapsed_seconds=3.0)
        self.registry = MagicMock()

    def test_ip_found_no_followup(self):
        with patch.object(web_tasks, "wait_for_public_ip",
                          return_value="203.0.113.10") as wait_ip, \
                patch.object(web_tasks, "start_ip_followup") as followup, \
                patch.object(web_tasks, "notify_boot_success",
                             return_value={"telegram": True, "bark": False,
                                           "errors": []}) as notify_fn:
            web_tasks._notify_grab_success(self.registry, "t1", MagicMock(),
                                           self.params, self.result,
                                           self.notify)
        wait_ip.assert_called_once()
        self.assertEqual(wait_ip.call_args[0][1],
                         "ocid1.instance.oc1..aaa1")
        followup.assert_not_called()
        instances = notify_fn.call_args[0][8]
        self.assertEqual(instances[0]["public_ip"], "203.0.113.10")
        # The root password is saved per instance for later SSH / DD
        # logins. 开机成功后 root 密码按实例存好，供之后 SSH / DD 登录。
        self.assertEqual(
            get_root_password("ocid1.instance.oc1..aaa1"), "pw123")

    def test_ip_missing_starts_followup(self):
        with patch.object(web_tasks, "wait_for_public_ip",
                          return_value=None), \
                patch.object(web_tasks, "start_ip_followup") as followup, \
                patch.object(web_tasks, "notify_boot_success",
                             return_value={"telegram": True, "bark": False,
                                           "errors": []}) as notify_fn:
            web_tasks._notify_grab_success(self.registry, "t1", MagicMock(),
                                           self.params, self.result,
                                           self.notify)
        instances = notify_fn.call_args[0][8]
        self.assertIsNone(instances[0]["public_ip"])
        followup.assert_called_once()
        args = followup.call_args[0]
        self.assertEqual(args[1], "AMD_2026-01-02")
        self.assertEqual(args[2], "T")
        self.assertEqual(args[3], 654321)


if __name__ == "__main__":
    unittest.main()
