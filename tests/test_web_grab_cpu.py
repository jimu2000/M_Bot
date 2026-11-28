#!/usr/bin/env python3
"""web/tasks._run_grabber 的 CPU 筛选注入单元测试。

与机器人端 _run_grabber_thread 同逻辑：guard 公钥拼进启动 keys、
注入 cpu_check_hook、目标型号读配置；guard 未配置时写日志并抢到即留。
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oci.grabber import GrabResult
from tgbot.cpu_policy import BootKeys
from web import tasks as web_tasks


class TestRunGrabberCpuFilter(unittest.TestCase):

    def setUp(self):
        # Redirect the instance credential store to a temp file: the
        # notify tests save the root password on success, and tests must
        # not write it to the real data directory. 凭据库指到临时文件：
        # 通知测试成功后会保存 root 密码，测试不能写真实数据目录。
        self._cred_tmp = tempfile.TemporaryDirectory()
        self._cred_patch = patch(
            "tgbot.instance_credentials.CREDENTIALS_FILE",
            os.path.join(self._cred_tmp.name,
                         "instance_credentials.json"))
        self._cred_patch.start()
        self.addCleanup(self._cred_patch.stop)
        self.addCleanup(self._cred_tmp.cleanup)
        self.params = {
            "display_name": "AMD_2026-01-02",
            "shape": "VM.Standard.E2.1.Micro",
            "availability_domain": "AD-1", "image_id": "img",
            "subnet_id": "subnet", "ocpus": 1, "memory_gb": 1,
            "target_cpu": "7J13", "ssh_keys_file": "/tmp/user_keys.pub",
        }
        self.result = GrabResult(instance_id="ocid1.instance.oc1..aaa1",
                                 releases=0, attempts=1, elapsed_seconds=3.0)
        self.registry = MagicMock()
        # Pure in-memory mode: do not start the cross-process cancel watch thread. 纯内存模式：不启动跨进程取消监听线程。
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
        # A1 不筛 CPU：target_cpu 为空时不注入 hook、不拼 guard 密钥。
        # E2 留空会默认补上 7J13 筛选（见 test_e2_defaults_to_cpu_filter），
        # 所以“不过滤”只用 A1 测。
        params = dict(self.params)
        params["shape"] = "VM.Standard.A1.Flex"
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

    def test_e2_defaults_to_cpu_filter_when_target_cpu_empty(self):
        # 面板建 E2 抢机任务时 target_cpu 留空：默认补上 cpu_policy.json
        # 的允许型号（只留 7J13），不再静默抢到即留。
        params = dict(self.params)
        params.pop("target_cpu", None)
        fake = self._fake_grabber()
        hook = MagicMock()
        with patch.object(web_tasks, "Grabber",
                          return_value=fake) as mock_grabber_cls, \
                patch.object(web_tasks, "combined_boot_keys",
                             self._combined_keys("/tmp/combined.pub",
                                                 True)), \
                patch.object(web_tasks, "get_allowed_cpu_models",
                             return_value=["7J13"]), \
                patch.object(web_tasks, "make_cpu_check_hook",
                             return_value=hook) as mock_hook_fn:
            web_tasks._run_grabber(self.registry, "t1", MagicMock(),
                                   "comp", params)
        kwargs = mock_grabber_cls.call_args[1]
        self.assertEqual(kwargs["target_cpu"], ["7J13"])
        self.assertIs(fake.cpu_check_hook, hook)
        mock_hook_fn.assert_called_once()

    def test_e2_target_cpu_any_means_no_filter(self):
        # 面板 CPU 下拉选“什么 CPU 都可以”（target_cpu="any"）：明确
        # 不要筛 CPU，E2 默认补全不能把它盖回 7J13，抢到就留。
        params = dict(self.params)
        params["target_cpu"] = "any"
        fake = self._fake_grabber()
        with patch.object(web_tasks, "Grabber",
                          return_value=fake) as mock_grabber_cls, \
                patch.object(web_tasks, "combined_boot_keys",
                             self._combined_keys("/tmp/user_keys.pub",
                                                 False)) as mock_keys, \
                patch.object(web_tasks, "get_allowed_cpu_models",
                             return_value=["7J13"]) as mock_allowed, \
                patch.object(web_tasks, "make_cpu_check_hook") as mock_hook_fn:
            web_tasks._run_grabber(self.registry, "t1", MagicMock(),
                                   "comp", params)
        self.assertIsNone(mock_grabber_cls.call_args[1]["target_cpu"])
        mock_hook_fn.assert_not_called()
        mock_allowed.assert_not_called()
        self.assertFalse(mock_keys.call_args[1]["include_guard"])

    def test_e2_default_spec_contains_target_cpu(self):
        # 默认补上的筛选要进 grab_spec，重启恢复时同样筛 CPU。
        import tempfile

        from web import config as web_config
        from web.tasks import TaskRegistry
        import tgbot.task_store as task_store

        params = dict(self.params)
        params.pop("target_cpu", None)
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(web_config, "DATA_DIR", tmp):
                registry = TaskRegistry()
                task_id = registry.create("grab", detail="test")
                cli = MagicMock()
                cli.profile = "DEFAULT"
                cli.region = "ap-singapore-1"
                fake = self._fake_grabber()
                with patch.object(web_tasks, "Grabber",
                                  return_value=fake), \
                        patch.object(web_tasks, "combined_boot_keys",
                                     self._combined_keys("/tmp/combined.pub",
                                                         True)), \
                        patch.object(web_tasks, "get_allowed_cpu_models",
                                     return_value=["7J13"]), \
                        patch.object(web_tasks, "make_cpu_check_hook",
                                     return_value=MagicMock()):
                    web_tasks._run_grabber(registry, task_id, cli,
                                           "comp", params)
                record = task_store.get_record(registry.tasks_file, task_id)
                self.assertEqual(
                    record["grab_spec"]["params"]["target_cpu"], ["7J13"])

    def test_e2_default_no_guard_notify_unverified(self):
        # 默认筛 CPU 但 guard 未配：通知要带 CPU 未验明，不能静默留机。
        params = dict(self.params)
        params.pop("target_cpu", None)
        fake = self._fake_grabber()
        notify = {"token": "T", "chat_id": 654321,
                  "profile_label": "新加坡", "region_label": "新加坡",
                  "root_password": "pw" + "123"}
        with patch.object(web_tasks, "Grabber",
                          return_value=fake) as mock_grabber_cls, \
                patch.object(web_tasks, "combined_boot_keys",
                             self._combined_keys("/tmp/user_keys.pub",
                                                 False)), \
                patch.object(web_tasks, "get_allowed_cpu_models",
                             return_value=["7J13"]), \
                patch.object(web_tasks, "make_cpu_check_hook") as mock_hook_fn, \
                patch.object(web_tasks, "wait_for_public_ip",
                             return_value="203.0.113.10"), \
                patch.object(web_tasks, "notify_boot_success",
                             return_value={"telegram": True, "bark": False,
                                           "errors": []}) as notify_fn:
            web_tasks._run_grabber(self.registry, "t1", MagicMock(),
                                   "comp", params, notify=notify)
        self.assertIsNone(mock_grabber_cls.call_args[1]["target_cpu"])
        mock_hook_fn.assert_not_called()
        self.assertTrue(notify_fn.call_args[1]["cpu_unverified"])
        logs = [c.args[1] for c in self.registry.append_log.call_args_list]
        self.assertTrue(any("未配置 guard 密钥" in m for m in logs))

    def test_explicit_target_cpu_no_guard_notify_unverified(self):
        # ARM 全功能测试 Bug 2 核实：显式传 target_cpu=7J13 但 guard 未配，
        # 抢机不静默放行——通知带 CPU 未验明，任务日志记下筛选未生效。
        params = dict(self.params)
        params["target_cpu"] = "7J13"
        fake = self._fake_grabber()
        notify = {"token": "T", "chat_id": 654321,
                  "profile_label": "新加坡", "region_label": "新加坡",
                  "root_password": "pw" + "123"}
        with patch.object(web_tasks, "Grabber",
                          return_value=fake) as mock_grabber_cls, \
                patch.object(web_tasks, "combined_boot_keys",
                             self._combined_keys("/tmp/user_keys.pub",
                                                 False)), \
                patch.object(web_tasks, "get_allowed_cpu_models",
                             return_value=["7J13"]), \
                patch.object(web_tasks, "make_cpu_check_hook") as mock_hook_fn, \
                patch.object(web_tasks, "wait_for_public_ip",
                             return_value="203.0.113.10"), \
                patch.object(web_tasks, "notify_boot_success",
                             return_value={"telegram": True, "bark": False,
                                           "errors": []}) as notify_fn:
            web_tasks._run_grabber(self.registry, "t1", MagicMock(),
                                   "comp", params, notify=notify)
        self.assertIsNone(mock_grabber_cls.call_args[1]["target_cpu"])
        mock_hook_fn.assert_not_called()
        self.assertTrue(notify_fn.call_args[1]["cpu_unverified"])
        logs = [c.args[1] for c in self.registry.append_log.call_args_list]
        self.assertTrue(any("未配置 guard 密钥" in m for m in logs))

    def test_wrong_cpu_released_by_default_filter(self):
        # 默认筛选下抢到 7742：M_Bot 自己验出并释放（永删引导卷），
        # 继续抢到 7J13 才留，全程不用人工 SSH 核查。
        from tests.test_grabber import (_mock_launch_cli,
                                       FAKE_INSTANCE_A, FAKE_INSTANCE_B)
        from oci.grabber import Grabber as RealGrabber

        params = dict(self.params)
        params.pop("target_cpu", None)
        cli = _mock_launch_cli(
            launch_results=[FAKE_INSTANCE_A, FAKE_INSTANCE_B])
        cli.run.return_value = None
        hook = MagicMock(side_effect=["AMD EPYC 7742", "AMD EPYC 7J13"])
        with patch.object(web_tasks, "combined_boot_keys",
                          self._combined_keys("/tmp/combined.pub", True)), \
                patch.object(web_tasks, "get_allowed_cpu_models",
                             return_value=["7J13"]), \
                patch.object(web_tasks, "make_cpu_check_hook",
                             return_value=hook), \
                patch.object(RealGrabber, "get_public_ip",
                             return_value="203.0.113.10"), \
                patch("oci.grabber.time.sleep"):
            web_tasks._run_grabber(self.registry, "t1", cli,
                                   "comp", params)
        terminate_calls = [c for c in cli.run.call_args_list
                           if "terminate" in c.args[0]]
        self.assertEqual(len(terminate_calls), 1)
        self.assertIn(FAKE_INSTANCE_A, terminate_calls[0].args[0])
        result = self.registry.set_result.call_args[0][1]
        self.assertEqual(result["instance_id"], FAKE_INSTANCE_B)
        self.assertEqual(result["releases"], 1)
        logs = [c.args[1] for c in self.registry.append_log.call_args_list]
        self.assertTrue(any("CPU 型号：AMD EPYC 7J13" in m for m in logs))


if __name__ == "__main__":
    unittest.main()
