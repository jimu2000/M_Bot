#!/usr/bin/env python3
"""v1.42.0 测试：抢机任务持久化 + 启动恢复、通知可观测性。

- 抢机启动时写 grab_spec（bot/web），每抢到一台更新已抢列表。
- 启动恢复：本来源带规格的抢机任务重跑（剩余台数正确），已抢满
  的标记完成并补发通知，无规格/非本来源的不恢复。
- 任务文件 0600；API 公开视图剥掉 grab_spec（含 root 密码）。
- Grabber 的 user_data 临时文件被外部删掉后能重建。
- 通知结果无论成败都写进任务日志（双通道成功 / Bark 未配置 /
  部分失败三种情况）。
"""

import os
import stat
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oci.grabber import GrabResult, Grabber
from tgbot import task_store
from tgbot.notify import fmt_notify_outcome
from tgbot.services import TaskRegistry as BotRegistry
from web import tasks as web_tasks
from web.tasks import TaskRegistry as WebRegistry
import tgbot.handlers_extra as he


def _cli_stub(profile="DEFAULT", region="ap-singapore-1"):
    return SimpleNamespace(profile=profile, region=region)


def _wait_for(predicate, timeout=5.0, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class _TempTasksFile(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "tasks.json")

    def tearDown(self):
        self._tmp.cleanup()


class TestFmtNotifyOutcome(unittest.TestCase):
    """fmt_notify_outcome：三种情况的文案。"""

    def test_both_sent(self):
        self.assertEqual(
            fmt_notify_outcome({"telegram": True, "bark": True,
                                "errors": []}),
            "通知：Telegram 已发送，Bark 已发送")

    def test_bark_unconfigured_is_skip_not_error(self):
        self.assertEqual(
            fmt_notify_outcome({"telegram": True, "bark": False,
                                "errors": []}),
            "通知：Telegram 已发送，Bark 未配置，已跳过")

    def test_bark_failed(self):
        self.assertEqual(
            fmt_notify_outcome({"telegram": True, "bark": False,
                                "errors": ["Bark：timeout"]}),
            "通知：Telegram 已发送，Bark 发送失败")

    def test_telegram_failed(self):
        self.assertEqual(
            fmt_notify_outcome({"telegram": False, "bark": True,
                                "errors": ["Telegram：bad request"]}),
            "通知：Telegram 发送失败，Bark 已发送")


class TestNotifyOutcomeLoggedWeb(_TempTasksFile):
    """web _notify_grab_success：无论成败都往任务日志写一行结果。"""

    def setUp(self):
        super().setUp()
        self.registry = WebRegistry(tasks_file=self.path)
        self.task_id = self.registry.create("grab", detail="抢机测试")
        self.params = {"display_name": "AMD_2026-11-25",
                       "shape": "VM.Standard.E2.1.Micro",
                       "ocpus": 1, "memory_gb": 1,
                       "boot_volume_size_gbs": 47}
        self.notify = {"token": "T", "chat_id": 654321,
                       "profile_label": "圣何塞", "region_label": "圣何塞",
                       "root_password": "pw" + "123",
                       "region_code": "us-sanjose-1"}
        self.result = GrabResult(instance_id="ocid1.instance.oc1..aaa1",
                                 releases=0, attempts=1, elapsed_seconds=3.0)

    def _logs(self):
        record = task_store.get_record(self.path, self.task_id)
        return "\n".join(record.get("logs", []))

    def _run_with(self, outcome):
        with patch.object(web_tasks, "wait_for_public_ip",
                          return_value="203.0.113.10"), \
                patch.object(web_tasks, "notify_boot_success",
                             return_value=outcome):
            web_tasks._notify_grab_success(
                self.registry, self.task_id, MagicMock(), self.params,
                self.result, self.notify)

    def test_both_channels_logged(self):
        self._run_with({"telegram": True, "bark": True, "errors": []})
        self.assertIn("通知：Telegram 已发送，Bark 已发送", self._logs())

    def test_bark_unconfigured_logged_as_skip(self):
        self._run_with({"telegram": True, "bark": False, "errors": []})
        self.assertIn("通知：Telegram 已发送，Bark 未配置，已跳过",
                      self._logs())

    def test_partial_failure_keeps_existing_wording(self):
        self._run_with({"telegram": True, "bark": False,
                        "errors": ["Bark：timeout"]})
        self.assertIn("开机成功通知部分失败：Bark：timeout", self._logs())


class TestNotifyOutcomeLoggedBot(_TempTasksFile):
    """bot _notify_boot_success：日志行 + task_id 为空时不写文件。"""

    def setUp(self):
        super().setUp()
        self.tasks = BotRegistry(tasks_file=self.path)
        self._tasks = patch.object(he, "TASKS", self.tasks)
        self._tasks.start()
        self.addCleanup(self._tasks.stop)
        self.task_id = self.tasks.register("grab", detail="抢机测试")
        self.preset = {"display_name": "ARM_2026-11-24", "count": 1,
                       "shape": "VM.Standard.A1.Flex",
                       "boot_volume_size_gbs": 47}
        self.notify = {"token": "T", "chat_id": 654321,
                       "profile_label": "新加坡", "region_label": "新加坡",
                       "root_password": "pw" + "123"}
        self.successes = [GrabResult(
            instance_id="ocid1.instance.oc1..aaa1", releases=0,
            attempts=1, elapsed_seconds=3.0)]

    def test_success_logged_to_task(self):
        with patch.object(he, "wait_for_public_ip",
                          return_value="203.0.113.10"), \
                patch.object(he, "notify_boot_success",
                             return_value={"telegram": True, "bark": True,
                                           "errors": []}):
            he._notify_boot_success(MagicMock(), self.preset,
                                    self.successes, self.notify,
                                    task_id=self.task_id)
        logs = "\n".join(
            task_store.get_record(self.path, self.task_id).get("logs", []))
        self.assertIn("通知：Telegram 已发送，Bark 已发送", logs)

    def test_no_task_id_does_not_write_file(self):
        with patch.object(he, "wait_for_public_ip",
                          return_value="203.0.113.10"), \
                patch.object(he, "notify_boot_success",
                             return_value={"telegram": True, "bark": True,
                                           "errors": []}):
            he._notify_boot_success(MagicMock(), self.preset,
                                    self.successes, self.notify)
        logs = task_store.get_record(self.path, self.task_id).get("logs", [])
        self.assertNotIn("通知：Telegram 已发送，Bark 已发送",
                         "\n".join(logs))


class TestGrabSpecWritten(_TempTasksFile):
    """抢机启动时写 grab_spec；token 不落盘；文件 0600。"""

    def test_bot_thread_writes_spec(self):
        tasks = BotRegistry(tasks_file=self.path)
        with patch.object(he, "TASKS", tasks):
            task_id = tasks.register("grab", detail="抢机测试")
            preset = {"display_name": "ARM_2026-11-24",
                      "shape": "VM.Standard.A1.Flex",
                      "availability_domain": "AD-1", "image_id": "img",
                      "subnet_id": "subnet", "ocpus": 1, "memory_gb": 6,
                      "count": 1}
            notify = {"token": "T", "chat_id": 654321,
                      "profile_label": "新加坡", "region_label": "新加坡",
                      "root_password": "pw" + "123"}
            result = GrabResult(instance_id="ocid1.instance.oc1..aaa1",
                                releases=0, attempts=2, elapsed_seconds=5.0)
            grabber = MagicMock()
            grabber.run.return_value = result
            with patch.object(he, "Grabber", return_value=grabber), \
                    patch.object(he, "save_preset"), \
                    patch.object(he, "wait_for_public_ip",
                                 return_value="203.0.113.10"), \
                    patch.object(he, "notify_boot_success",
                                 return_value={"telegram": True,
                                               "bark": False,
                                               "errors": []}):
                he._run_grabber_thread(task_id, _cli_stub(), "comp",
                                       preset, notify=notify)
        record = task_store.get_record(self.path, task_id)
        self.assertEqual(record["status"], "done")
        spec = record.get("grab_spec")
        self.assertIsNotNone(spec)
        self.assertEqual(spec["profile"], "DEFAULT")
        self.assertEqual(spec["region"], "ap-singapore-1")
        self.assertEqual(spec["compartment_id"], "comp")
        self.assertEqual(spec["params"]["display_name"], "ARM_2026-11-24")
        self.assertEqual(spec["params"]["shape"], "VM.Standard.A1.Flex")
        self.assertEqual(spec["notify"]["chat_id"], 654321)
        self.assertEqual(spec["notify"]["root_password"], "pw" + "123")
        self.assertNotIn("token", spec["notify"])
        self.assertEqual(len(spec["successes"]), 1)
        self.assertEqual(spec["successes"][0]["instance_id"],
                         "ocid1.instance.oc1..aaa1")
        self.assertEqual(spec["successes"][0]["attempts"], 2)
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode, 0o600)

    def test_web_thread_writes_spec(self):
        registry = WebRegistry(tasks_file=self.path)
        task_id = registry.create("grab", detail="抢机测试")
        params = {"display_name": "AMD_2026-11-25",
                  "shape": "VM.Standard.E2.1.Micro",
                  "availability_domain": "AD-1", "image_id": "img",
                  "subnet_id": "subnet", "ocpus": 1, "memory_gb": 1,
                  "boot_volume_size_gbs": 47}
        notify = {"token": "T", "chat_id": 654321,
                  "profile_label": "圣何塞", "region_label": "圣何塞",
                  "root_password": "pw" + "123",
                  "region_code": "us-sanjose-1"}
        result = GrabResult(instance_id="ocid1.instance.oc1..aaa1",
                            releases=0, attempts=1, elapsed_seconds=3.0)
        grabber = MagicMock()
        grabber.run.return_value = result
        with patch.object(web_tasks, "Grabber", return_value=grabber), \
                patch.object(web_tasks, "wait_for_public_ip",
                             return_value="203.0.113.10"), \
                patch.object(web_tasks, "notify_boot_success",
                             return_value={"telegram": True, "bark": True,
                                           "errors": []}):
            web_tasks._run_grabber(registry, task_id,
                                   _cli_stub("ACCOUNT2", "us-sanjose-1"),
                                   "comp", params, notify=notify)
        record = task_store.get_record(self.path, task_id)
        self.assertEqual(record["status"], "done")
        spec = record.get("grab_spec")
        self.assertIsNotNone(spec)
        self.assertEqual(spec["profile"], "ACCOUNT2")
        self.assertEqual(spec["region"], "us-sanjose-1")
        self.assertNotIn("token", spec["notify"])
        self.assertEqual(len(spec["successes"]), 1)
        self.assertEqual(spec["successes"][0]["instance_id"],
                         "ocid1.instance.oc1..aaa1")
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode, 0o600)


class TestPublicRecordStripsSpec(_TempTasksFile):
    """API 公开视图剥掉 grab_spec，原始记录不受影响。"""

    def test_web_registry_get_and_list_strip_spec(self):
        record = task_store.create_record(
            self.path, "web", "grab", detail="抢机测试", status="running")
        spec = task_store.build_grab_spec(
            "DEFAULT", "ap-singapore-1", "comp",
            {"display_name": "ARM_2026-11-24",
             "shape": "VM.Standard.A1.Flex"},
            {"chat_id": 1, "root_password": "pw" + "123"})
        task_store.update_record(self.path, record["id"],
                                 fields={"grab_spec": spec})
        registry = WebRegistry(tasks_file=self.path)
        self.assertNotIn("grab_spec", registry.get(record["id"]))
        views = registry.list()
        self.assertEqual(len(views), 1)
        self.assertNotIn("grab_spec", views[0])
        # The original record still has the spec (used for recovery). 原始记录仍有规格（恢复用）。
        self.assertIn("grab_spec",
                      task_store.get_record(self.path, record["id"]))


class TestResumeGrabs(_TempTasksFile):
    """启动恢复：重跑剩余台数、已满标记完成、无规格/异源不恢复。"""

    def setUp(self):
        super().setUp()
        self.tasks = BotRegistry(tasks_file=self.path)
        self._tasks = patch.object(he, "TASKS", self.tasks)
        self._tasks.start()
        self.addCleanup(self._tasks.stop)

    def _make_running_grab(self, origin="bot", count=2, successes=(),
                           kind="grab"):
        record = task_store.create_record(
            self.path, origin, kind, detail="抢机测试", status="running")
        params = {"display_name": "ARM_2026-11-24",
                  "shape": "VM.Standard.A1.Flex",
                  "availability_domain": "AD-1", "image_id": "img",
                  "subnet_id": "subnet", "ocpus": 1, "memory_gb": 6,
                  "count": count}
        notify = {"chat_id": 654321, "profile_label": "新加坡",
                  "region_label": "新加坡", "root_password": "pw" + "123"}
        spec = task_store.build_grab_spec(
            "DEFAULT", "ap-singapore-1", "comp", params, notify)
        spec["successes"] = list(successes)
        task_store.update_record(self.path, record["id"],
                                 fields={"grab_spec": spec})
        return record["id"], spec

    def _start_patches(self, grabber_result=None):
        grabber = MagicMock()
        if grabber_result is not None:
            grabber.run.return_value = grabber_result
        patchers = [
            patch.object(he, "Grabber", return_value=grabber),
            patch.object(he, "OCIClient",
                         return_value=_cli_stub()),
            patch.object(he, "save_preset"),
            patch.object(he, "wait_for_public_ip",
                         return_value="203.0.113.10"),
            patch.object(he, "notify_boot_success",
                         return_value={"telegram": True, "bark": False,
                                       "errors": []}),
        ]
        mocks = [p.start() for p in patchers]
        self.addCleanup(lambda: [p.stop() for p in patchers])
        return mocks

    def test_resume_relaunches_remaining_count(self):
        prior = {"instance_id": "ocid1.instance.oc1..aaa1", "attempts": 2,
                 "releases": 0, "elapsed_seconds": 5.0}
        task_id, _spec = self._make_running_grab(count=2, successes=[prior])
        new_result = GrabResult(instance_id="ocid1.instance.oc1..bbb2",
                                releases=0, attempts=1, elapsed_seconds=3.0)
        mocks = self._start_patches(grabber_result=new_result)
        he.resume_interrupted_grabs()
        done = _wait_for(
            lambda: task_store.get_record(self.path, task_id)["status"]
            == "done")
        self.assertTrue(done, "恢复的抢机应在 5 秒内完成")
        grabber_cls = mocks[0]
        # Grab only the 1 remaining instance (count=2, 1 already grabbed); the name continues the numbering with -2. 只抢剩余 1 台（count=2，已抢 1 台），名字接着编号 -2。
        self.assertEqual(grabber_cls.call_count, 1)
        self.assertEqual(grabber_cls.call_args[0][6], "ARM_2026-11-24-2")
        record = task_store.get_record(self.path, task_id)
        self.assertEqual(len(record["grab_spec"]["successes"]), 2)
        logs = "\n".join(record.get("logs", []))
        self.assertIn("🔄 重启后恢复抢机", logs)

    def test_already_complete_marks_done_and_notifies(self):
        prior = {"instance_id": "ocid1.instance.oc1..aaa1", "attempts": 2,
                 "releases": 0, "elapsed_seconds": 5.0}
        task_id, _spec = self._make_running_grab(count=1, successes=[prior])
        mocks = self._start_patches()
        he.resume_interrupted_grabs()
        record = task_store.get_record(self.path, task_id)
        self.assertEqual(record["status"], "done")
        mocks[0].assert_not_called()
        # Re-send the notice (background thread; wait until it calls notify_boot_success). 补发通知（后台线程，等它调到 notify_boot_success）。
        notify_mock = mocks[4]
        self.assertTrue(_wait_for(lambda: notify_mock.called),
                        "已抢满的任务应补发成功通知")

    def test_no_spec_and_other_origin_not_resumed(self):
        # A bot grab task without a spec: marked interrupted. 无规格的 bot 抢机任务：标为已中断。
        no_spec = task_store.create_record(
            self.path, "bot", "grab", detail="无规格", status="running")
        # A web-sourced task with a spec: bot recovery never touches it. web 来源的带规格任务：bot 恢复绝不碰。
        web_id, _spec = self._make_running_grab(origin="web", count=1)
        # A bot-sourced non-grab task: marked interrupted. bot 来源的非 grab 任务：标为已中断。
        other = task_store.create_record(
            self.path, "bot", "bootswap", detail="换盘", status="running")
        mocks = self._start_patches()
        he.resume_interrupted_grabs()
        self.assertEqual(
            task_store.get_record(self.path, no_spec["id"])["status"],
            "cancelled")
        self.assertEqual(
            task_store.get_record(self.path, web_id)["status"], "running")
        self.assertEqual(
            task_store.get_record(self.path, other["id"])["status"],
            "cancelled")
        mocks[0].assert_not_called()


class TestWebResumeGrabs(_TempTasksFile):
    """web 端启动恢复：重跑 + 已满标记完成。"""

    def test_web_resume_relaunches(self):
        registry = WebRegistry(tasks_file=self.path)
        record = task_store.create_record(
            self.path, "web", "grab", detail="抢机测试", status="running")
        params = {"display_name": "AMD_2026-11-25",
                  "shape": "VM.Standard.E2.1.Micro",
                  "availability_domain": "AD-1", "image_id": "img",
                  "subnet_id": "subnet", "ocpus": 1, "memory_gb": 1,
                  "boot_volume_size_gbs": 47}
        spec = task_store.build_grab_spec(
            "ACCOUNT2", "us-sanjose-1", "comp", params,
            {"chat_id": 654321, "root_password": "pw" + "123"})
        task_store.update_record(self.path, record["id"],
                                 fields={"grab_spec": spec})
        result = GrabResult(instance_id="ocid1.instance.oc1..aaa1",
                            releases=0, attempts=1, elapsed_seconds=3.0)
        grabber = MagicMock()
        grabber.run.return_value = result
        with patch.object(web_tasks, "Grabber",
                          return_value=grabber) as grabber_cls, \
                patch.object(web_tasks, "OCIClient",
                             return_value=_cli_stub("ACCOUNT2",
                                                    "us-sanjose-1")), \
                patch.object(web_tasks, "wait_for_public_ip",
                             return_value="203.0.113.10"), \
                patch.object(web_tasks, "notify_boot_success",
                             return_value={"telegram": True, "bark": True,
                                           "errors": []}):
            web_tasks.resume_interrupted_grabs(registry)
            done = _wait_for(
                lambda: task_store.get_record(
                    self.path, record["id"])["status"] == "done")
        self.assertTrue(done, "恢复的 web 抢机应在 5 秒内完成")
        self.assertEqual(grabber_cls.call_count, 1)
        record = task_store.get_record(self.path, record["id"])
        logs = "\n".join(record.get("logs", []))
        self.assertIn("🔄 重启后恢复抢机", logs)


class TestUserDataFileRecreated(unittest.TestCase):
    """Grabber：user_data 临时文件被外部删掉后，下次 launch 前重建。"""

    def test_deleted_file_is_recreated(self):
        grabber = Grabber(
            cli=MagicMock(), compartment_id="comp",
            availability_domain="AD-1", shape="VM.Standard.A1.Flex",
            image_id="img", subnet_id="subnet",
            display_name="test-instance", ssh_keys_file=None,
            user_data="#!/bin/bash\necho hello")
        first = grabber._ensure_user_data_file()
        self.assertTrue(os.path.exists(first))
        with open(first, encoding="utf-8") as f:
            self.assertEqual(f.read(), "#!/bin/bash\necho hello")
        os.unlink(first)
        self.assertFalse(os.path.exists(first))
        second = grabber._ensure_user_data_file()
        self.assertTrue(os.path.exists(second))
        with open(second, encoding="utf-8") as f:
            self.assertEqual(f.read(), "#!/bin/bash\necho hello")
        grabber.cleanup()
        self.assertFalse(os.path.exists(second))


if __name__ == "__main__":
    unittest.main()
