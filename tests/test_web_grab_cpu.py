#!/usr/bin/env python3
"""web/tasks._run_grabber 的 CPU 筛选注入单元测试。

与机器人端 _run_grabber_thread 同逻辑：guard 公钥拼进启动 keys、
注入 cpu_check_hook、目标型号读配置；guard 未配置时写日志并抢到即留。
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oci.grabber import GrabResult
from tgbot.cpu_policy import BootKeys
from web import tasks as web_tasks


class TestRunGrabberCpuFilter(unittest.TestCase):

    def setUp(self):
        self.params = {
            "display_name": "AMD_2026-11-25",
            "shape": "VM.Standard.E2.1.Micro",
            "availability_domain": "AD-1", "image_id": "img",
            "subnet_id": "subnet", "ocpus": 1, "memory_gb": 1,
            "target_cpu": "7J13", "ssh_keys_file": "/tmp/user_keys.pub",
        }
        self.result = GrabResult(instance_id="ocid1.instance.oc1..aaa1",
                                 releases=0, attempts=1, elapsed_seconds=3.0)
        self.registry = MagicMock()
        # 纯内存模式：不启动跨进程取消监听线程。
        self.registry.tasks_file = None
        self.stop_event = MagicMock()
        self.stop_event.is_set.return_value = False
        self.registry.stop_event.return_value = self.stop_event

    def _fake_grabber(self):
        grabber = MagicMock()
        grabber.run.return_value = self.result
        return grabber

    def _combined_keys(self, path, guard_included):
        cm = MagicMock()
        cm.return_value.__enter__.return_value = BootKeys(
            path=path, guard_included=guard_included)
        return cm

    def test_filter_on_injects_hook_and_combined_keys(self):
        fake = self._fake_grabber()
        hook = MagicMock()
        with patch.object(web_tasks, "Grabber",
                          return_value=fake) as mock_grabber_cls, \
                patch.object(web_tasks, "combined_boot_keys",
                             self._combined_keys("/tmp/combined.pub",
                                                 True)), \
                patch.object(web_tasks, "get_allowed_cpu_models",
                             return_value=["7J13", "7745"]), \
                patch.object(web_tasks, "make_cpu_check_hook",
                             return_value=hook) as mock_hook_fn:
            web_tasks._run_grabber(self.registry, "t1", MagicMock(),
                                   "comp", self.params)
        kwargs = mock_grabber_cls.call_args[1]
        self.assertEqual(kwargs["target_cpu"], ["7J13", "7745"])
        self.assertEqual(mock_grabber_cls.call_args[0][7],
                         "/tmp/combined.pub")
        self.assertIs(fake.cpu_check_hook, hook)
        mock_hook_fn.assert_called_once()
        self.registry.set_status.assert_any_call("t1", "done")

    def test_filter_on_no_guard_logs_warning(self):
        fake = self._fake_grabber()
        with patch.object(web_tasks, "Grabber",
                          return_value=fake) as mock_grabber_cls, \
                patch.object(web_tasks, "combined_boot_keys",
                             self._combined_keys("/tmp/user_keys.pub",
                                                 False)), \
                patch.object(web_tasks, "get_allowed_cpu_models",
                             return_value=["7J13", "7745"]), \
                patch.object(web_tasks, "make_cpu_check_hook") as mock_hook_fn:
            web_tasks._run_grabber(self.registry, "t1", MagicMock(),
                                   "comp", self.params)
        self.assertIsNone(mock_grabber_cls.call_args[1]["target_cpu"])
        mock_hook_fn.assert_not_called()
        logs = [c.args[1] for c in self.registry.append_log.call_args_list]
        self.assertTrue(any("未配置 guard 密钥" in m for m in logs))

    def test_filter_off_no_hook(self):
        params = dict(self.params)
        params["target_cpu"] = None
        fake = self._fake_grabber()
        with patch.object(web_tasks, "Grabber",
                          return_value=fake) as mock_grabber_cls, \
                patch.object(web_tasks, "combined_boot_keys",
                             self._combined_keys("/tmp/user_keys.pub",
                                                 False)) as mock_keys, \
                patch.object(web_tasks, "make_cpu_check_hook") as mock_hook_fn:
            web_tasks._run_grabber(self.registry, "t1", MagicMock(),
                                   "comp", params)
        self.assertIsNone(mock_grabber_cls.call_args[1]["target_cpu"])
        mock_hook_fn.assert_not_called()
        self.assertFalse(mock_keys.call_args[1]["include_guard"])

    def test_cpu_check_failed_flag_reaches_notify(self):
        fake = self._fake_grabber()
        mock_grabber_cls = MagicMock(return_value=fake)

        def run_side_effect():
            on_event = mock_grabber_cls.call_args[1]["on_event"]
            on_event("cpu_check_failed", "CPU 查不到型号，暂留实例。")
            return self.result

        fake.run.side_effect = run_side_effect
        notify = {"token": "T", "chat_id": 654321,
                  "profile_label": "圣何塞", "region_label": "圣何塞",
                  "root_password": "pw" + "123"}
        with patch.object(web_tasks, "Grabber", mock_grabber_cls), \
                patch.object(web_tasks, "combined_boot_keys",
                             self._combined_keys("/tmp/combined.pub",
                                                 True)), \
                patch.object(web_tasks, "get_allowed_cpu_models",
                             return_value=["7J13", "7745"]), \
                patch.object(web_tasks, "make_cpu_check_hook",
                             return_value=MagicMock()), \
                patch.object(web_tasks, "wait_for_public_ip",
                             return_value="203.0.113.10"), \
                patch.object(web_tasks, "notify_boot_success",
                             return_value={"telegram": True, "bark": False,
                                           "errors": []}) as notify_fn:
            web_tasks._run_grabber(self.registry, "t1", MagicMock(),
                                   "comp", self.params, notify=notify)
        self.assertTrue(notify_fn.call_args[1]["cpu_unverified"])


if __name__ == "__main__":
    unittest.main()
