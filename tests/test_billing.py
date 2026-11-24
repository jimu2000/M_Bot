#!/usr/bin/env python3
"""账单预警（tgbot/billing.py）与每日账单任务的单元测试。

OCI 调用全部 mock；自动停付费实例的分支用 mock 验证，
绝不真停任何实例。
"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from tgbot import billing as bill
from tgbot import config as bot_config
import tgbot.handlers_extra as he

TEST_USER_ID = 123456
TEST_CHAT_ID = 654321


def make_context():
    context = MagicMock()
    context.user_data = {}
    context.bot_data = {}
    context.bot = MagicMock()
    context.bot.send_message = AsyncMock()
    context.bot.delete_message = AsyncMock()
    return context


class TestMonthBounds(unittest.TestCase):

    def test_mid_month(self):
        start, end = bill.month_bounds(
            datetime(2026, 11, 24, 15, 30, tzinfo=timezone.utc))
        self.assertEqual(start, datetime(2026, 11, 1, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 12, 1, tzinfo=timezone.utc))

    def test_december_rollover(self):
        start, end = bill.month_bounds(
            datetime(2026, 12, 31, tzinfo=timezone.utc))
        self.assertEqual(start, datetime(2026, 12, 1, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2027, 1, 1, tzinfo=timezone.utc))


class TestFetchMtdCost(unittest.TestCase):

    def test_sums_amounts_and_uses_month_bounds(self):
        cli = MagicMock()
        cli.run.return_value = {"data": {"items": [
            {"computed-amount": 13.46, "currency": "SGD"},
            {"computed-amount": 0.5, "currency": "SGD"},
        ]}}
        total, currency = bill.fetch_mtd_cost(
            cli, "ocid1.tenancy.oc1..aaa",
            now=datetime(2026, 11, 24, tzinfo=timezone.utc))
        self.assertAlmostEqual(total, 13.96)
        self.assertEqual(currency, "SGD")
        args = cli.run.call_args[0][0]
        self.assertEqual(args[:3],
                         ["usage-api", "usage-summary",
                          "request-summarized-usages"])
        self.assertIn("2026-11-01T00:00:00Z", args)
        self.assertIn("2026-12-01T00:00:00Z", args)
        self.assertIn("MONTHLY", args)

    def test_empty_items_zero(self):
        cli = MagicMock()
        cli.run.return_value = {"data": {"items": []}}
        total, currency = bill.fetch_mtd_cost(cli, "tenancy")
        self.assertEqual(total, 0.0)
        self.assertEqual(currency, "SGD")

    def test_blank_currency_falls_back_to_sgd(self):
        # 真实 API 实测：零费用账号返回一个 amount 0.0、currency ' '
        # 的条目，空白币种要回退到 SGD。
        cli = MagicMock()
        cli.run.return_value = {"data": {"items": [
            {"computed-amount": 0.0, "currency": " "},
        ]}}
        total, currency = bill.fetch_mtd_cost(cli, "tenancy")
        self.assertEqual(total, 0.0)
        self.assertEqual(currency, "SGD")


class TestListSubscribedRegions(unittest.TestCase):

    def test_only_ready_regions(self):
        cli = MagicMock()
        cli.run.return_value = {"data": [
            {"region-name": "ap-singapore-1", "status": "READY"},
            {"region-name": "ap-seoul-1", "status": "READY"},
            {"region-name": "us-ashburn-1", "status": "NOT_SUBSCRIBED"},
        ]}
        self.assertEqual(bill.list_subscribed_regions(cli, "tenancy"),
                         ["ap-singapore-1", "ap-seoul-1"])


class TestCheckAccountBilling(unittest.TestCase):

    def setUp(self):
        self._profiles = patch.object(
            bot_config, "PROFILES",
            {"DEFAULT": {"region": "ap-singapore-1",
                         "compartment_id": "ocid1.tenancy.oc1..aaa"}})
        self._profiles.start()
        self.addCleanup(self._profiles.stop)
        self.cli = MagicMock()
        self._client = patch.object(bot_config, "get_oci_client",
                                    return_value=self.cli)
        self._client.start()
        self.addCleanup(self._client.stop)
        self._cost = patch.object(bill, "fetch_mtd_cost",
                                  return_value=(0.0, "SGD"))
        self._cost.start()
        self.addCleanup(self._cost.stop)
        self._regions = patch.object(bill, "list_subscribed_regions",
                                     return_value=["ap-singapore-1"])
        self._regions.start()
        self.addCleanup(self._regions.stop)

    def _instances(self, *specs):
        """specs: (name, shape, state, ocpus, mem)。"""
        return [{"display-name": name, "id": f"ocid1.instance.oc1..{i}",
                 "shape": shape, "lifecycle-state": state,
                 "shape-config": {"ocpus": ocpus, "memory-in-gbs": mem}}
                for i, (name, shape, state, ocpus, mem) in enumerate(specs)]

    def test_free_shapes_only_no_stop(self):
        insts = self._instances(
            ("web-server-1", "VM.Standard.A1.Flex", "RUNNING", 1, 6),
            ("AMD_2026-11-24", "VM.Standard.E2.1.Micro", "RUNNING", 1, 1),
            ("old", "VM.Standard.A1.Flex", "STOPPED", 2, 12),
        )
        with patch.object(bill, "list_instances", return_value=insts), \
                patch.object(bill, "stop_instance") as mock_stop:
            res = bill.check_account_billing("DEFAULT")
        mock_stop.assert_not_called()
        self.assertEqual(res["stopped"], [])
        self.assertEqual(res["paid_running"], [])
        self.assertEqual(res["a1_ocpus"], 1)
        self.assertEqual(res["a1_mem"], 6)
        self.assertEqual(res["e2_count"], 1)
        self.assertEqual(res["free_over"], [])

    def test_paid_running_is_stopped(self):
        insts = self._instances(
            ("paid-e5", "VM.Standard.E5.Flex", "RUNNING", 2, 16),
            ("free", "VM.Standard.A1.Flex", "RUNNING", 1, 6),
        )
        with patch.object(bill, "list_instances", return_value=insts), \
                patch.object(bill, "stop_instance") as mock_stop:
            res = bill.check_account_billing("DEFAULT")
        mock_stop.assert_called_once()
        self.assertEqual(mock_stop.call_args[0][1],
                         "ocid1.instance.oc1..0")
        self.assertEqual(len(res["stopped"]), 1)
        self.assertEqual(res["stopped"][0]["name"], "paid-e5")
        self.assertEqual(len(res["paid_running"]), 1)

    def test_never_stop_names_protected(self):
        insts = self._instances(
            ("web-server-1", "VM.Standard.E5.Flex", "RUNNING", 2, 16),
        )
        with patch.object(bill, "list_instances", return_value=insts), \
                patch.object(bill, "NEVER_STOP_NAMES", {"web-server-1"}), \
                patch.object(bill, "stop_instance") as mock_stop:
            res = bill.check_account_billing("DEFAULT")
        mock_stop.assert_not_called()
        self.assertEqual(res["stopped"], [])
        # 仍记录为付费运行中（告警里能看到），只是不停。
        self.assertEqual(len(res["paid_running"]), 1)

    def test_stop_failure_recorded(self):
        insts = self._instances(
            ("paid-e5", "VM.Standard.E5.Flex", "RUNNING", 2, 16),
        )
        with patch.object(bill, "list_instances", return_value=insts), \
                patch.object(bill, "stop_instance",
                             side_effect=RuntimeError("api error")):
            res = bill.check_account_billing("DEFAULT")
        self.assertEqual(res["stopped"], [])
        self.assertEqual(len(res["stop_errors"]), 1)
        self.assertEqual(res["stop_errors"][0]["instance"], "paid-e5")

    def test_free_tier_overruns(self):
        insts = self._instances(
            ("a1-1", "VM.Standard.A1.Flex", "RUNNING", 4, 24),
            ("a1-2", "VM.Standard.A1.Flex", "RUNNING", 1, 6),
            ("e2-1", "VM.Standard.E2.1.Micro", "RUNNING", 1, 1),
            ("e2-2", "VM.Standard.E2.1.Micro", "RUNNING", 1, 1),
            ("e2-3", "VM.Standard.E2.1.Micro", "RUNNING", 1, 1),
        )
        with patch.object(bill, "list_instances", return_value=insts), \
                patch.object(bill, "stop_instance"):
            res = bill.check_account_billing("DEFAULT")
        self.assertEqual(len(res["free_over"]), 2)
        self.assertIn("A1 超免费额度", res["free_over"][0])
        self.assertIn("E2.1.Micro 3 台", res["free_over"][1])

    def test_region_list_failure_does_not_abort(self):
        with patch.object(bill, "list_instances",
                          side_effect=RuntimeError("region down")), \
                patch.object(bill, "stop_instance"):
            res = bill.check_account_billing("DEFAULT")
        self.assertEqual(res["instances"][0]["region"], "ap-singapore-1")
        self.assertIn("region down", res["instances"][0]["error"])

    def test_over_budget_flag(self):
        self._cost.stop()
        with patch.object(bill, "fetch_mtd_cost",
                          return_value=(13.46, "SGD")), \
                patch.object(bill, "list_instances", return_value=[]):
            res = bill.check_account_billing("DEFAULT")
        self.assertTrue(res["over_budget"])
        self.assertEqual(res["mtd_cost"], 13.46)


class TestBillingState(unittest.TestCase):

    def test_roundtrip_and_corrupt(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "billing_state.json")
            self.assertEqual(bill.load_billing_state(path), {})
            state = {"DEFAULT": {"mtd_cost": 13.46,
                                 "checked_at": "2026-11-24T00:00:00Z"}}
            bill.save_billing_state(state, path)
            self.assertEqual(bill.load_billing_state(path), state)
            with open(path, "w", encoding="utf-8") as f:
                f.write("{bad")
            self.assertEqual(bill.load_billing_state(path), {})


class TestCollectBillingAlerts(unittest.TestCase):

    def _res(self, profile="DEFAULT", **kw):
        base = {"profile": profile, "mtd_cost": 0.0, "currency": "SGD",
                "budget": 1.0, "over_budget": False, "stopped": [],
                "stop_errors": [], "free_over": [], "paid_running": []}
        base.update(kw)
        return base

    def test_no_change_no_alerts(self):
        res = [self._res(mtd_cost=0.0)]
        self.assertEqual(
            bill.collect_billing_alerts(
                res, {"DEFAULT": {"mtd_cost": 0.0}}), [])

    def test_first_run_no_prev_no_cost_alert(self):
        # 首次运行无上次状态、费用为 0：不告警。
        self.assertEqual(bill.collect_billing_alerts([self._res()], {}), [])

    def test_over_budget_alone_no_alert(self):
        # 已超预算但无新增收费（已知历史账单）：不重复告警。
        res = [self._res(mtd_cost=13.46, over_budget=True)]
        self.assertEqual(
            bill.collect_billing_alerts(
                res, {"DEFAULT": {"mtd_cost": 13.46}}), [])

    def test_cost_increase_alert(self):
        res = [self._res(mtd_cost=0.5)]
        alerts = bill.collect_billing_alerts(
            res, {"DEFAULT": {"mtd_cost": 0.2}})
        self.assertEqual(len(alerts), 1)
        self.assertIn("本月费用新增 0.3 SGD", alerts[0])
        self.assertIn("当前 0.50 SGD", alerts[0])
        self.assertIn("预算 1.00 SGD", alerts[0])

    def test_tiny_increase_no_alert(self):
        # 增加 <= 0.01（四舍五入噪声）：不告警。
        res = [self._res(mtd_cost=0.205)]
        self.assertEqual(
            bill.collect_billing_alerts(
                res, {"DEFAULT": {"mtd_cost": 0.2}}), [])

    def test_protected_paid_instance_alert(self):
        res = [self._res(
            paid_running=[{"name": "protected-server",
                           "shape": "VM.Standard.E5.Flex",
                           "region": "ap-singapore-1"}])]
        alerts = bill.collect_billing_alerts(res, {})
        self.assertEqual(len(alerts), 1)
        self.assertIn("在跑但未停（在保护名单）", alerts[0])

    def test_cost_decrease_no_alert(self):
        res = [self._res(mtd_cost=0.1)]
        self.assertEqual(
            bill.collect_billing_alerts(
                res, {"DEFAULT": {"mtd_cost": 0.5}}), [])

    def test_stopped_and_errors_and_over(self):
        res = [self._res(
            stopped=[{"name": "paid-e5", "shape": "VM.Standard.E5.Flex",
                      "region": "ap-singapore-1"}],
            stop_errors=[{"instance": "paid-e4", "error": "api error"}],
            free_over=["A1 超免费额度: 5 OCPU / 30 GB (上限 4 OCPU / 24 GB)"])]
        alerts = bill.collect_billing_alerts(res, {})
        self.assertEqual(len(alerts), 3)
        self.assertIn("已自动停掉付费实例 paid-e5", alerts[0])
        self.assertIn("停付费实例 paid-e4 失败", alerts[1])
        self.assertIn("A1 超免费额度", alerts[2])

    def test_check_error_alert(self):
        res = [{"profile": "DEFAULT", "error": "api down"}]
        alerts = bill.collect_billing_alerts(res, {})
        self.assertEqual(len(alerts), 1)
        self.assertIn("账单检查失败", alerts[0])

    def test_fmt_billing_alerts(self):
        text = bill.fmt_billing_alerts(["a", "b"])
        self.assertIn("Oracle 账单预警", text)
        self.assertIn("• a", text)
        self.assertIn("• b", text)


class TestBillingCheckJob(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self._profiles = patch.object(
            bot_config, "PROFILES",
            {"DEFAULT": {"region": "ap-singapore-1",
                         "compartment_id": "ocid1.tenancy.oc1..aaa"}})
        self._profiles.start()
        self.addCleanup(self._profiles.stop)
        self._allowed = patch.object(bot_config, "ALLOWED_USER_IDS",
                                     {TEST_USER_ID})
        self._allowed.start()
        self.addCleanup(self._allowed.stop)
        self.saved_state = None

        def _save(state, path=None):
            self.saved_state = state
            return state
        self._save = patch.object(he, "save_billing_state",
                                  side_effect=_save)
        self._save.start()
        self.addCleanup(self._save.stop)

    async def test_no_alerts_silent_but_saves_state(self):
        res = {"profile": "DEFAULT", "mtd_cost": 0.0, "currency": "SGD",
               "budget": 1.0, "over_budget": False, "stopped": [],
               "stop_errors": [], "free_over": []}
        context = make_context()
        with patch.object(he, "check_account_billing",
                          return_value=res), \
                patch.object(he, "load_billing_state",
                             return_value={"DEFAULT": {"mtd_cost": 0.0}}), \
                patch.object(he, "notify_dual") as mock_notify:
            await he.billing_check_job(context)
        mock_notify.assert_not_called()
        self.assertEqual(self.saved_state["DEFAULT"]["mtd_cost"], 0.0)

    async def test_alerts_notify_dual_with_bark_chat_id(self):
        res = {"profile": "DEFAULT", "mtd_cost": 13.46, "currency": "SGD",
               "budget": 1.0, "over_budget": True, "stopped": [],
               "stop_errors": [], "free_over": [], "paid_running": []}
        context = make_context()
        with patch.object(he, "check_account_billing",
                          return_value=res), \
                patch.object(he, "load_billing_state",
                             return_value={"DEFAULT": {"mtd_cost": 10.0}}), \
                patch.object(he, "load_bark_config",
                             return_value={"chat_id": TEST_CHAT_ID}), \
                patch.object(he, "notify_dual",
                             return_value={"telegram": True, "bark": True,
                                           "errors": []}) as mock_notify:
            await he.billing_check_job(context)
        mock_notify.assert_called_once()
        args, kwargs = mock_notify.call_args
        self.assertEqual(args[1], TEST_CHAT_ID)
        self.assertIn("账单预警", args[2])
        self.assertIn("13.46 SGD", args[3])
        self.assertEqual(kwargs.get("group"), "oracle-billing")

    async def test_alerts_fall_back_to_allowed_user(self):
        res = {"profile": "DEFAULT", "mtd_cost": 0.5, "currency": "SGD",
               "budget": 1.0, "over_budget": False, "stopped": [],
               "stop_errors": [], "free_over": []}
        context = make_context()
        with patch.object(he, "check_account_billing",
                          return_value=res), \
                patch.object(he, "load_billing_state",
                             return_value={"DEFAULT": {"mtd_cost": 0.2}}), \
                patch.object(he, "load_bark_config",
                             return_value={"chat_id": None}), \
                patch.object(he, "notify_dual",
                             return_value={"telegram": True, "bark": False,
                                           "errors": []}) as mock_notify:
            await he.billing_check_job(context)
        mock_notify.assert_called_once()
        self.assertEqual(mock_notify.call_args[0][1], TEST_USER_ID)

    async def test_account_error_alerts(self):
        context = make_context()
        with patch.object(he, "check_account_billing",
                          side_effect=RuntimeError("api down")), \
                patch.object(he, "load_billing_state", return_value={}), \
                patch.object(he, "load_bark_config",
                             return_value={"chat_id": TEST_CHAT_ID}), \
                patch.object(he, "notify_dual",
                             return_value={"telegram": True, "bark": False,
                                           "errors": []}) as mock_notify:
            await he.billing_check_job(context)
        mock_notify.assert_called_once()
        self.assertIn("账单检查失败",
                      mock_notify.call_args[0][3])
        # 检查失败的账号不写进 state（下次仍按旧费用比对）。
        self.assertEqual(self.saved_state, {})

    async def test_paid_instance_alert_pins(self):
        res = {"profile": "DEFAULT", "mtd_cost": 13.46, "currency": "SGD",
               "budget": 1.0, "over_budget": True,
               "stopped": [{"name": "paid-e5", "shape": "VM.Standard.E5.Flex",
                            "region": "ap-singapore-1"}],
               "stop_errors": [], "free_over": [],
               "paid_running": [{"name": "paid-e5"}]}
        context = make_context()
        with patch.object(he, "check_account_billing",
                          return_value=res), \
                patch.object(he, "load_billing_state",
                             return_value={"DEFAULT": {"mtd_cost": 13.46}}), \
                patch.object(he, "load_bark_config",
                             return_value={"chat_id": TEST_CHAT_ID}), \
                patch.object(he, "notify_dual",
                             return_value={"telegram": True, "bark": True,
                                           "errors": []}) as mock_notify:
            await he.billing_check_job(context)
        mock_notify.assert_called_once()
        self.assertTrue(mock_notify.call_args[1].get("pin"))

    async def test_fee_only_alert_does_not_pin(self):
        res = {"profile": "DEFAULT", "mtd_cost": 0.5, "currency": "SGD",
               "budget": 1.0, "over_budget": False, "stopped": [],
               "stop_errors": [], "free_over": [], "paid_running": []}
        context = make_context()
        with patch.object(he, "check_account_billing",
                          return_value=res), \
                patch.object(he, "load_billing_state",
                             return_value={"DEFAULT": {"mtd_cost": 0.2}}), \
                patch.object(he, "load_bark_config",
                             return_value={"chat_id": TEST_CHAT_ID}), \
                patch.object(he, "notify_dual",
                             return_value={"telegram": True, "bark": False,
                                           "errors": []}) as mock_notify:
            await he.billing_check_job(context)
        mock_notify.assert_called_once()
        self.assertFalse(mock_notify.call_args[1].get("pin"))


if __name__ == "__main__":
    unittest.main()
