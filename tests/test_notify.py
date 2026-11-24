#!/usr/bin/env python3
"""双通道通知（tgbot/notify.py）与抢机成功推送的单元测试。

Telegram/Bark 的 HTTP 层全部 mock；抢机线程的通知分支用
mock Grabber + mock 通知函数验证，不真抢机。
"""

import json
import unittest
from unittest.mock import MagicMock, patch

from tgbot import notify as notify_mod
import tgbot.handlers_extra as he
from tgbot.services import TaskRegistry
from oci.grabber import GrabResult


class TestSendTelegramMessage(unittest.TestCase):

    def _ok_response(self):
        resp = MagicMock()
        resp.read.return_value = b'{"ok": true, "result": {}}'
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        return resp

    def test_success(self):
        captured = {}
        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["data"] = json.loads(req.data.decode("utf-8"))
            return self._ok_response()
        with patch.object(notify_mod.urllib.request, "urlopen",
                          side_effect=fake_urlopen):
            ok, err = notify_mod.send_telegram_message(
                "TOKEN123", 654321, "hello")
        self.assertTrue(ok, err)
        self.assertEqual(
            captured["url"],
            "https://api.telegram.org/botTOKEN123/sendMessage")
        self.assertEqual(captured["data"]["chat_id"], 654321)
        self.assertEqual(captured["data"]["text"], "hello")

    def test_missing_token_or_chat(self):
        ok, err = notify_mod.send_telegram_message("", 1, "x")
        self.assertFalse(ok)
        ok, err = notify_mod.send_telegram_message("T", None, "x")
        self.assertFalse(ok)

    def test_api_not_ok_returns_error(self):
        resp = MagicMock()
        resp.read.return_value = (
            b'{"ok": false, "description": "chat not found"}')
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        with patch.object(notify_mod.urllib.request, "urlopen",
                          return_value=resp):
            ok, err = notify_mod.send_telegram_message("T", 1, "x")
        self.assertFalse(ok)
        self.assertIn("chat not found", err)


class TestFmtBootSuccess(unittest.TestCase):

    def test_single_instance_with_password(self):
        # v1.11.0：照 R 探长成功消息的字段结构重排。
        text = notify_mod.fmt_boot_success(
            "新加坡", "首尔", "VM.Standard.A1.Flex", 1, 6, 47,
            [{"name": "ARM_2026-11-24", "public_ip": "203.0.113.10",
              "attempts": 1, "elapsed_seconds": 3.9}],
            root_password="pw123", region_code="ap-seoul-1",
            ssh_key_set=True)
        self.assertIn("🎉 恭喜，开机成功！", text)
        self.assertIn("账号：新加坡", text)
        self.assertIn("cpu类型：ARM", text)
        self.assertIn("cpu数量：1 核", text)
        self.assertIn("内存大小：6 GB", text)
        self.assertIn("硬盘选择：47 GB", text)
        self.assertIn("root密码：pw123", text)
        self.assertIn("公钥：已设置", text)
        self.assertIn("开机数量：第 1/1 个", text)
        self.assertIn("实例：ARM_2026-11-24", text)
        self.assertIn("抢机次数：1 次", text)
        self.assertIn("花费时间：4 秒", text)
        self.assertIn("开机地区：ap-seoul-1（首尔）", text)
        self.assertIn("ip：203.0.113.10", text)
        self.assertIn("注意📢：root密码机器人不会记忆，请注意保存", text)

    def test_no_password_no_line(self):
        text = notify_mod.fmt_boot_success(
            "新加坡", "新加坡", "VM.Standard.E2.1.Micro", 1, 1, 47,
            [{"name": "AMD_2026-11-24", "public_ip": None,
              "attempts": 3, "elapsed_seconds": 120.5}],
            root_password=None)
        self.assertIn("cpu类型：AMD", text)
        self.assertNotIn("root密码", text)
        self.assertNotIn("注意📢", text)
        self.assertIn("公钥：未设置", text)
        self.assertIn("获取中", text)

    def test_default_disk_and_region_fallback(self):
        # 硬盘不指定显示"默认"；没有区域代码时只显示中文名。
        text = notify_mod.fmt_boot_success(
            "新加坡", "新加坡", "VM.Standard.A1.Flex", 1, 6, None,
            [{"name": "ARM_2026-11-24", "public_ip": "203.0.113.10",
              "attempts": 1, "elapsed_seconds": 6.2}])
        self.assertIn("硬盘选择：默认", text)
        self.assertIn("开机地区：新加坡", text)

    def test_batch_lists_each(self):
        text = notify_mod.fmt_boot_success(
            "新加坡", "新加坡", "VM.Standard.A1.Flex", 1, 6, 47,
            [{"name": "ARM_2026-11-24-1", "public_ip": "1.2.3.4",
              "attempts": 1, "elapsed_seconds": 5.0},
             {"name": "ARM_2026-11-24-2", "public_ip": "1.2.3.5",
              "attempts": 2, "elapsed_seconds": 40.0}],
            region_code="ap-singapore-1")
        self.assertIn("第 1/2 个：ARM_2026-11-24-1", text)
        self.assertIn("第 2/2 个：ARM_2026-11-24-2", text)
        self.assertIn("ip：1.2.3.4", text)
        self.assertIn("ip：1.2.3.5", text)
        self.assertIn("开机地区：ap-singapore-1（新加坡）", text)


class TestNotifyBootSuccess(unittest.TestCase):

    def test_both_channels(self):
        with patch.object(notify_mod, "send_telegram_message",
                          return_value=(True, "")) as tg, \
                patch.object(notify_mod, "send_bark",
                             return_value=(True, "")) as bark:
            result = notify_mod.notify_boot_success(
                "T", 1, "新加坡", "新加坡", "VM.Standard.E2.1.Micro",
                1, 1, 47,
                [{"name": "AMD_2026-11-24", "public_ip": "1.2.3.4",
                  "attempts": 1, "elapsed_seconds": 3.0}])
        self.assertTrue(result["telegram"])
        self.assertTrue(result["bark"])
        self.assertEqual(result["errors"], [])
        self.assertIn("开机成功", tg.call_args[0][2])
        self.assertEqual(bark.call_args[0][0], "🎉 恭喜，开机成功！")

    def test_bark_disabled_is_not_an_error(self):
        with patch.object(notify_mod, "send_telegram_message",
                          return_value=(True, "")), \
                patch.object(notify_mod, "send_bark",
                             return_value=(False, "Bark 推送未开启")):
            result = notify_mod.notify_boot_success(
                "T", 1, "新加坡", "新加坡", "VM.Standard.E2.1.Micro",
                1, 1, 47,
                [{"name": "AMD_2026-11-24", "public_ip": None,
                  "attempts": 1, "elapsed_seconds": 3.0}])
        self.assertTrue(result["telegram"])
        self.assertFalse(result["bark"])
        self.assertEqual(result["errors"], [])

    def test_never_raises(self):
        with patch.object(notify_mod, "send_telegram_message",
                          side_effect=RuntimeError("boom")), \
                patch.object(notify_mod, "send_bark",
                             side_effect=RuntimeError("boom")):
            result = notify_mod.notify_boot_success(
                "T", 1, "新加坡", "新加坡", "VM.Standard.E2.1.Micro",
                1, 1, 47,
                [{"name": "AMD_2026-11-24", "public_ip": None,
                  "attempts": 1, "elapsed_seconds": 3.0}])
        self.assertFalse(result["telegram"])
        self.assertFalse(result["bark"])
        self.assertEqual(len(result["errors"]), 2)


class TestGrabberThreadNotify(unittest.TestCase):
    """_run_grabber_thread 的通知分支：成功后调通知，失败不调。"""

    def setUp(self):
        self.tasks = TaskRegistry()
        self._tasks = patch.object(he, "TASKS", self.tasks)
        self._tasks.start()
        self.addCleanup(self._tasks.stop)
        self.preset = {
            "display_name": "ARM_2026-11-24", "shape": "VM.Standard.A1.Flex",
            "availability_domain": "AD-1", "image_id": "img",
            "subnet_id": "subnet", "ocpus": 1, "memory_gb": 6,
            "count": 1,
        }
        self.notify = {"token": "T", "chat_id": 654321,
                       "profile_label": "新加坡", "region_label": "首尔",
                       "root_password": "pw123"}

    def _fake_grabber(self, result):
        grabber = MagicMock()
        grabber.run.return_value = result
        return grabber

    def test_success_notifies_with_ip(self):
        result = GrabResult(instance_id="ocid1.instance.oc1..aaa1", releases=0,
                            attempts=2, elapsed_seconds=45.0)
        task_id = self.tasks.register("grab")
        with patch.object(he, "Grabber",
                          return_value=self._fake_grabber(result)), \
                patch.object(he, "get_public_ip",
                             return_value="203.0.113.10") as get_ip, \
                patch.object(he, "save_preset"), \
                patch.object(he, "notify_boot_success",
                             return_value={"telegram": True, "bark": False,
                                           "errors": []}) as notify_fn, \
                patch.object(he.time, "sleep"):
            he._run_grabber_thread(task_id, MagicMock(), "comp",
                                   self.preset, notify=self.notify)
        self.assertEqual(self.tasks.get(task_id)["status"], "done")
        self.assertTrue(notify_fn.called)
        args = notify_fn.call_args
        self.assertEqual(args[0][0], "T")
        self.assertEqual(args[0][1], 654321)
        self.assertEqual(args[0][2], "新加坡")
        self.assertEqual(args[0][3], "首尔")
        self.assertEqual(args[0][4], "VM.Standard.A1.Flex")
        self.assertEqual(args[0][7], 47)  # 默认盘
        instances = args[0][8]
        self.assertEqual(instances[0]["name"], "ARM_2026-11-24")
        self.assertEqual(instances[0]["public_ip"], "203.0.113.10")
        self.assertEqual(instances[0]["attempts"], 2)
        self.assertEqual(args[1]["root_password"], "pw123")
        get_ip.assert_called_once()

    def test_no_notify_arg_skips_notification(self):
        result = GrabResult(instance_id="ocid1.instance.oc1..aaa1", releases=0,
                            attempts=1, elapsed_seconds=3.0)
        task_id = self.tasks.register("grab")
        with patch.object(he, "Grabber",
                          return_value=self._fake_grabber(result)), \
                patch.object(he, "save_preset"), \
                patch.object(he, "notify_boot_success") as notify_fn:
            he._run_grabber_thread(task_id, MagicMock(), "comp",
                                   self.preset)
        self.assertFalse(notify_fn.called)

    def test_ip_retry_then_give_up(self):
        result = GrabResult(instance_id="ocid1.instance.oc1..aaa1", releases=0,
                            attempts=1, elapsed_seconds=3.0)
        task_id = self.tasks.register("grab")
        with patch.object(he, "Grabber",
                          return_value=self._fake_grabber(result)), \
                patch.object(he, "get_public_ip", return_value=None), \
                patch.object(he, "save_preset"), \
                patch.object(he, "notify_boot_success",
                             return_value={"telegram": True, "bark": False,
                                           "errors": []}) as notify_fn, \
                patch.object(he.time, "sleep") as sleep_mock:
            he._run_grabber_thread(task_id, MagicMock(), "comp",
                                   self.preset, notify=self.notify)
        # 查 3 次 IP（2 次 sleep 间隔），最后一次不 sleep。
        self.assertEqual(sleep_mock.call_count, 2)
        instances = notify_fn.call_args[0][8]
        self.assertIsNone(instances[0]["public_ip"])


class TestSendTelegramMessageFull(unittest.TestCase):
    """send_telegram_message_full 返回 message_id 供置顶用。"""

    def _ok_response(self, body):
        resp = MagicMock()
        resp.read.return_value = body
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        return resp

    def test_success_returns_message_id(self):
        resp = self._ok_response(
            b'{"ok": true, "result": {"message_id": 42}}')
        with patch.object(notify_mod.urllib.request, "urlopen",
                          return_value=resp):
            ok, err, mid = notify_mod.send_telegram_message_full(
                "T", 1, "x")
        self.assertTrue(ok, err)
        self.assertEqual(mid, 42)

    def test_failure_message_id_none(self):
        resp = self._ok_response(
            b'{"ok": false, "description": "chat not found"}')
        with patch.object(notify_mod.urllib.request, "urlopen",
                          return_value=resp):
            ok, err, mid = notify_mod.send_telegram_message_full(
                "T", 1, "x")
        self.assertFalse(ok)
        self.assertIsNone(mid)


class TestPinTelegramMessage(unittest.TestCase):

    def _ok_response(self):
        resp = MagicMock()
        resp.read.return_value = b'{"ok": true, "result": true}'
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        return resp

    def test_pin_success(self):
        captured = {}
        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["data"] = json.loads(req.data.decode("utf-8"))
            return self._ok_response()
        with patch.object(notify_mod.urllib.request, "urlopen",
                          side_effect=fake_urlopen):
            ok, err = notify_mod.pin_telegram_message("T", 1, 42)
        self.assertTrue(ok, err)
        self.assertEqual(
            captured["url"],
            "https://api.telegram.org/botT/pinChatMessage")
        self.assertEqual(captured["data"]["message_id"], 42)
        self.assertTrue(captured["data"]["disable_notification"])

    def test_pin_missing_message_id(self):
        ok, _err = notify_mod.pin_telegram_message("T", 1, None)
        self.assertFalse(ok)

    def test_pin_api_error(self):
        resp = MagicMock()
        resp.read.return_value = (
            b'{"ok": false, "description": "not enough rights"}')
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        with patch.object(notify_mod.urllib.request, "urlopen",
                          return_value=resp):
            ok, err = notify_mod.pin_telegram_message("T", 1, 42)
        self.assertFalse(ok)
        self.assertIn("not enough rights", err)


class TestNotifyDualPin(unittest.TestCase):
    """notify_dual(pin=True)：发完消息后置顶，置顶失败只记错误。"""

    def test_pin_called_on_success(self):
        with patch.object(notify_mod, "send_telegram_message_full",
                          return_value=(True, "", 42)) as send, \
                patch.object(notify_mod, "pin_telegram_message",
                             return_value=(True, "")) as pin, \
                patch.object(notify_mod, "send_bark",
                             return_value=(False, "Bark 推送未开启")):
            result = notify_mod.notify_dual("T", 1, "标题", "正文",
                                            pin=True)
        self.assertTrue(result["telegram"])
        self.assertEqual(result["errors"], [])
        pin.assert_called_once_with("T", 1, 42)
        self.assertIn("标题", send.call_args[0][2])

    def test_pin_not_called_without_flag(self):
        with patch.object(notify_mod, "send_telegram_message_full",
                          return_value=(True, "", 42)), \
                patch.object(notify_mod, "pin_telegram_message") as pin, \
                patch.object(notify_mod, "send_bark",
                             return_value=(False, "Bark 推送未开启")):
            notify_mod.notify_dual("T", 1, "标题", "正文")
        pin.assert_not_called()

    def test_pin_failure_recorded_not_fatal(self):
        with patch.object(notify_mod, "send_telegram_message_full",
                          return_value=(True, "", 42)), \
                patch.object(notify_mod, "pin_telegram_message",
                             return_value=(False, "not enough rights")), \
                patch.object(notify_mod, "send_bark",
                             return_value=(True, "")):
            result = notify_mod.notify_dual("T", 1, "标题", "正文",
                                            pin=True)
        self.assertTrue(result["telegram"])
        self.assertTrue(result["bark"])
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("置顶失败", result["errors"][0])


class TestFmtPaidInstanceAlert(unittest.TestCase):

    def test_paid_alert_text(self):
        text = notify_mod.fmt_paid_instance_alert(
            "新加坡", "首尔", "VM.Standard.E5.Flex",
            [{"name": "paid-1", "public_ip": "203.0.113.10"},
             {"name": "paid-2", "public_ip": None}])
        self.assertIn("账号：新加坡", text)
        self.assertIn("地区：首尔", text)
        self.assertIn("VM.Standard.E5.Flex", text)
        self.assertIn("不在免费层内", text)
        self.assertIn("paid-1：203.0.113.10", text)
        self.assertIn("paid-2：获取中", text)
        self.assertIn("尽快停止或释放", text)


class TestPaidInstanceHook(unittest.TestCase):
    """_notify_boot_success：付费形状额外发置顶提醒，免费形状不发。"""

    def setUp(self):
        self.preset = {"display_name": "test-1", "count": 1,
                       "shape": "VM.Standard.E5.Flex",
                       "boot_volume_size_gbs": 50}
        self.notify = {"token": "T", "chat_id": 654321,
                       "profile_label": "新加坡", "region_label": "首尔",
                       "root_password": ""}
        self.successes = [GrabResult(
            instance_id="ocid1.instance.oc1..aaa1", releases=0,
            attempts=1, elapsed_seconds=3.0)]

    def test_paid_shape_pins_alert(self):
        with patch.object(he, "get_public_ip",
                          return_value="203.0.113.10"), \
                patch.object(he, "notify_boot_success",
                             return_value={"telegram": True, "bark": False,
                                           "errors": []}), \
                patch.object(he, "notify_dual",
                             return_value={"telegram": True, "bark": False,
                                           "errors": []}) as dual:
            he._notify_boot_success(MagicMock(), self.preset,
                                    self.successes, self.notify)
        dual.assert_called_once()
        args, kwargs = dual.call_args
        self.assertEqual(args[0], "T")
        self.assertEqual(args[1], 654321)
        self.assertIn("付费实例提醒", args[2])
        self.assertIn("VM.Standard.E5.Flex", args[3])
        self.assertTrue(kwargs.get("pin"))
        self.assertEqual(kwargs.get("group"), "paid-instance")

    def test_free_shape_no_alert(self):
        self.preset["shape"] = "VM.Standard.A1.Flex"
        with patch.object(he, "get_public_ip",
                          return_value="203.0.113.10"), \
                patch.object(he, "notify_boot_success",
                             return_value={"telegram": True, "bark": False,
                                           "errors": []}), \
                patch.object(he, "notify_dual") as dual:
            he._notify_boot_success(MagicMock(), self.preset,
                                    self.successes, self.notify)
        dual.assert_not_called()


if __name__ == "__main__":
    unittest.main()
