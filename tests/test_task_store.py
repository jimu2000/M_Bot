"""共享任务存储测试（v1.41.0）：bot 与网页共用 tasks.json。"""
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

from tgbot import task_store
from tgbot.services import TaskRegistry as BotRegistry
from web.tasks import TaskRegistry as WebRegistry


class TaskStoreTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "tasks.json")

    def tearDown(self):
        self._tmp.cleanup()


class TestTasksPath(TaskStoreTestCase):
    def test_env_var_wins(self):
        with patch.dict(os.environ,
                        {"MBOT_TASKS_FILE": "/tmp/custom-tasks.json"}):
            self.assertEqual(task_store.tasks_path(),
                             "/tmp/custom-tasks.json")

    def test_default_is_web_data_dir(self):
        with patch.dict(os.environ, {}, clear=True):
            from web import config

            expected = os.path.join(config.DATA_DIR, "tasks.json")
            self.assertEqual(task_store.tasks_path(), expected)


class TestCreateRecord(TaskStoreTestCase):
    def test_ids_are_origin_prefixed_and_unique(self):
        first = task_store.create_record(self.path, "bot", "grab",
                                         "抢机 A", "A")
        second = task_store.create_record(self.path, "web", "grab",
                                          "抢机 B", "B")
        third = task_store.create_record(self.path, "bot", "grab",
                                         "抢机 C", "C")
        self.assertEqual(first["id"], "bot-g1")
        self.assertEqual(second["id"], "web-g1")
        self.assertEqual(third["id"], "bot-g2")
        self.assertEqual(first["status"], "pending")
        self.assertEqual(first["origin"], "bot")
        self.assertEqual(first["kind"], "grab")
        self.assertFalse(first["cancel_requested"])

    def test_bootswap_kind_letter(self):
        record = task_store.create_record(self.path, "web", "bootswap",
                                          "恢复", "恢复")
        self.assertEqual(record["id"], "web-b1")

    def test_legacy_ids_count_toward_web_numbering(self):
        # Old web task ids are unprefixed g1/g2: new allocations must avoid them and never collide. 旧版网页任务 ID 是无前缀的 g1/g2：新分配要避让，不能撞号。
        task_store._write_records_unlocked(
            self.path, [{"id": "g1", "kind": "grab", "status": "done",
                         "detail": "", "label": "旧", "logs": [],
                         "result": None, "created_at": "",
                         "origin": "web", "cancel_requested": False}])
        record = task_store.create_record(self.path, "web", "grab",
                                          "新抢机", "新")
        self.assertEqual(record["id"], "web-g2")

    def test_legacy_record_normalizes_origin(self):
        task_store._write_records_unlocked(
            self.path, [{"id": "g1", "kind": "grab", "status": "done",
                         "detail": "", "label": "旧", "logs": [],
                         "result": None, "created_at": ""}])
        record = task_store.get_record(self.path, "g1")
        self.assertEqual(record["origin"], "web")
        self.assertFalse(record["cancel_requested"])


class TestUpdateAndCancel(TaskStoreTestCase):
    def setUp(self):
        super().setUp()
        self.record = task_store.create_record(
            self.path, "bot", "grab", "抢机", "抢机", status="running")

    def test_update_status_and_detail(self):
        ok = task_store.update_record(self.path, self.record["id"],
                                      status="done", detail="完成了")
        self.assertTrue(ok)
        record = task_store.get_record(self.path, self.record["id"])
        self.assertEqual(record["status"], "done")
        self.assertEqual(record["detail"], "完成了")

    def test_update_appends_log(self):
        task_store.update_record(self.path, self.record["id"],
                                 log="第 1 次尝试")
        task_store.update_record(self.path, self.record["id"],
                                 log="第 2 次尝试")
        record = task_store.get_record(self.path, self.record["id"])
        self.assertEqual(len(record["logs"]), 2)
        self.assertIn("第 1 次尝试", record["logs"][0])
        self.assertIn("第 2 次尝试", record["logs"][1])

    def test_update_missing_record_returns_false(self):
        self.assertFalse(task_store.update_record(self.path, "bot-g9",
                                                  status="done"))

    def test_request_cancel_running(self):
        self.assertTrue(task_store.request_cancel(self.path,
                                                  self.record["id"]))
        record = task_store.get_record(self.path, self.record["id"])
        self.assertTrue(record["cancel_requested"])

    def test_request_cancel_terminal_returns_false(self):
        task_store.update_record(self.path, self.record["id"],
                                 status="done")
        self.assertFalse(task_store.request_cancel(self.path,
                                                   self.record["id"]))

    def test_request_cancel_missing_returns_false(self):
        self.assertFalse(task_store.request_cancel(self.path, "bot-g9"))


class TestRecoverInterrupted(TaskStoreTestCase):
    def test_only_own_origin_is_recovered(self):
        bot_rec = task_store.create_record(self.path, "bot", "grab",
                                           "bot 抢机", "bot",
                                           status="running")
        web_rec = task_store.create_record(self.path, "web", "grab",
                                           "web 抢机", "web",
                                           status="running")
        done_rec = task_store.create_record(self.path, "bot", "grab",
                                            "已完成", "已完成",
                                            status="done")
        task_store.recover_interrupted(self.path, "bot")
        self.assertEqual(
            task_store.get_record(self.path, bot_rec["id"])["status"],
            "cancelled")
        self.assertEqual(
            task_store.get_record(self.path, web_rec["id"])["status"],
            "running")
        self.assertEqual(
            task_store.get_record(self.path, done_rec["id"])["status"],
            "done")
        logs = task_store.get_record(self.path, bot_rec["id"])["logs"]
        self.assertTrue(any("服务重启" in line for line in logs))

    def test_clear_records(self):
        task_store.create_record(self.path, "bot", "grab", "A", "A")
        task_store.clear_records(self.path)
        self.assertEqual(task_store.load_records(self.path), [])


class TestCancelWatcher(TaskStoreTestCase):
    def test_watcher_sets_stop_event_on_cancel_request(self):
        record = task_store.create_record(self.path, "web", "grab",
                                          "抢机", "抢机", status="running")
        stop_event = threading.Event()
        task_store.start_cancel_watcher(self.path, record["id"],
                                        stop_event, interval=0.05)
        task_store.request_cancel(self.path, record["id"])
        self.assertTrue(stop_event.wait(2.0))

    def test_watcher_exits_on_terminal_status(self):
        record = task_store.create_record(self.path, "web", "grab",
                                          "抢机", "抢机", status="running")
        stop_event = threading.Event()
        thread = task_store.start_cancel_watcher(
            self.path, record["id"], stop_event, interval=0.05)
        task_store.update_record(self.path, record["id"], status="done")
        thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive())
        self.assertFalse(stop_event.is_set())

    def test_watcher_exits_when_record_disappears(self):
        record = task_store.create_record(self.path, "web", "grab",
                                          "抢机", "抢机", status="running")
        stop_event = threading.Event()
        thread = task_store.start_cancel_watcher(
            self.path, record["id"], stop_event, interval=0.05)
        task_store.clear_records(self.path)
        thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive())

    def test_watcher_noop_without_path_or_event(self):
        record = task_store.create_record(self.path, "web", "grab",
                                          "抢机", "抢机", status="running")
        self.assertIsNone(task_store.start_cancel_watcher(
            None, record["id"], threading.Event()))
        self.assertIsNone(task_store.start_cancel_watcher(
            self.path, record["id"], None))


class TestCrossProcessVisibility(TaskStoreTestCase):
    """两个注册表指向同一文件，模拟 bot 与网页两个进程。"""

    def test_bot_task_visible_to_web_and_vice_versa(self):
        bot = BotRegistry(tasks_file=self.path, origin="bot")
        web = WebRegistry(tasks_file=self.path)
        bot_id = bot.register("grab", label="bot 抢机")
        web_id = web.create("grab", "web 抢机", label="web 抢机")
        self.assertEqual(bot_id, "bot-g1")
        self.assertEqual(web_id, "web-g1")
        # The web side can see the bot's tasks, and the bot side can see the web's tasks. 网页端能看到 bot 的任务，bot 端能看到网页的任务。
        self.assertEqual(web.get(bot_id)["label"], "bot 抢机")
        self.assertEqual(bot.get(web_id)["label"], "web 抢机")
        web_ids = {t["id"] for t in web.list()}
        bot_ids = {t["id"] for t in bot.list_all()}
        self.assertEqual(web_ids, {"bot-g1", "web-g1"})
        self.assertEqual(bot_ids, {"bot-g1", "web-g1"})

    def test_bot_update_visible_to_web(self):
        bot = BotRegistry(tasks_file=self.path, origin="bot")
        web = WebRegistry(tasks_file=self.path)
        task_id = bot.register("grab", label="bot 抢机")
        bot.update(task_id, detail="第 3 次尝试，无容量")
        task = web.get(task_id)
        self.assertEqual(task["detail"], "第 3 次尝试，无容量")
        self.assertTrue(any("第 3 次尝试" in line
                            for line in task["logs"]))

    def test_web_update_visible_to_bot(self):
        bot = BotRegistry(tasks_file=self.path, origin="bot")
        web = WebRegistry(tasks_file=self.path)
        task_id = web.create("grab", "web 抢机")
        web.append_log(task_id, "第 2 次尝试")
        web.set_status(task_id, "done")
        task = bot.get(task_id)
        self.assertEqual(task["status"], "done")
        self.assertEqual(task["detail"], "第 2 次尝试")

    def test_bot_cancel_requests_web_task_cancel(self):
        bot = BotRegistry(tasks_file=self.path, origin="bot")
        web = WebRegistry(tasks_file=self.path)
        task_id = web.create("grab", "web 抢机")
        self.assertTrue(bot.cancel(task_id))
        self.assertTrue(web.get(task_id)["cancel_requested"])
        # After the cancel request is sent, the web side's watch thread should set stop_event. 取消请求发出后，网页端的监听线程应置位 stop_event。
        stop_event = web.stop_event(task_id)
        thread = task_store.start_cancel_watcher(
            self.path, task_id, stop_event, interval=0.05)
        self.assertTrue(stop_event.wait(2.0))
        thread.join(timeout=2.0)

    def test_web_cancel_requests_bot_task_cancel(self):
        bot = BotRegistry(tasks_file=self.path, origin="bot")
        web = WebRegistry(tasks_file=self.path)
        task_id = bot.register("grab", label="bot 抢机")
        self.assertTrue(web.cancel(task_id))
        self.assertTrue(bot.get(task_id)["cancel_requested"])

    def test_cancel_done_task_returns_false(self):
        bot = BotRegistry(tasks_file=self.path, origin="bot")
        web = WebRegistry(tasks_file=self.path)
        task_id = web.create("grab", "web 抢机")
        web.set_status(task_id, "done")
        self.assertFalse(bot.cancel(task_id))

    def test_recover_only_own_origin(self):
        bot = BotRegistry(tasks_file=self.path, origin="bot")
        web = WebRegistry(tasks_file=self.path)
        bot_id = bot.register("grab", label="bot 抢机")
        web_id = web.create("grab", "web 抢机")
        bot.recover()
        self.assertEqual(bot.get(bot_id)["status"], "cancelled")
        # The web task is still pending (that is the state before its thread starts), and the bot's recovery must not touch it. 网页任务还是 pending（线程未启动时就是 pending），bot 的恢复不该动它。
        self.assertEqual(web.get(web_id)["status"], "pending")
        web.recover()
        self.assertEqual(web.get(web_id)["status"], "cancelled")

    def test_web_own_task_cancel_stops_locally(self):
        web = WebRegistry(tasks_file=self.path)
        task_id = web.create("grab", "web 抢机")
        stop_event = web.stop_event(task_id)
        self.assertTrue(web.cancel(task_id))
        self.assertTrue(stop_event.is_set())
        self.assertEqual(web.get(task_id)["status"], "cancelled")


class TestBotRegistryMemoryMode(unittest.TestCase):
    """tasks_file=None 时保持旧的纯内存行为（已有测试的基线）。"""

    def test_memory_mode_still_works(self):
        registry = BotRegistry()
        task_id = registry.register("grab", "抢机")
        self.assertEqual(task_id, "g1")
        registry.update(task_id, status="done", detail="完成")
        self.assertEqual(registry.get(task_id)["status"], "done")
        self.assertEqual(len(registry.list_all()), 1)


if __name__ == "__main__":
    unittest.main()
