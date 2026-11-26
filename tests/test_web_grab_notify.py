#!/usr/bin/env python3
"""web/tasks._notify_grab_success 的单元测试。

抢机成功推送的网页版分支：等 IP（wait_for_public_ip）与跟进线程
（start_ip_followup），与机器人端 _notify_boot_success 同逻辑。
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oci.grabber import GrabResult
from web import tasks as web_tasks


class TestNotifyGrabSuccess(unittest.TestCase):

    def setUp(self):
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
