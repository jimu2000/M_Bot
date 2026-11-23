#!/usr/bin/env python3
"""tgbot.monitoring 的单元测试（阶段 5 工作包 C）。

纯同步逻辑测试，不调 OCI API、不发 Telegram 消息、不连真实 SSL。
配置读写一律用临时 DATA_DIR（patch tgbot.config.DATA_DIR），
不污染真实数据目录。
"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tgbot import config as bot_config
from tgbot import monitoring as mon

FAKE_OCID_1 = "ocid1.instance.oc1..aaa1fake"
FAKE_OCID_2 = "ocid1.instance.oc1..bbb2fake"


class MonitoringTestCase(unittest.TestCase):
    """基类：临时 DATA_DIR。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._data_dir = patch.object(bot_config, "DATA_DIR", self._tmp.name)
        self._data_dir.start()
        self.addCleanup(self._data_dir.stop)

    def write_json(self, filename, data):
        path = os.path.join(self._tmp.name, filename)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return path

    def read_json(self, filename):
        path = os.path.join(self._tmp.name, filename)
        with open(path, encoding="utf-8") as f:
            return json.load(f)


class TestTrafficGuardConfig(MonitoringTestCase):

    def test_defaults_when_file_missing(self):
        cfg = mon.load_traffic_guard()
        self.assertEqual(cfg, {
            "enabled": False,
            "threshold_gb": 200,
            "action": "notify",
            "last_triggered": None,
            "chat_id": None,
        })

    def test_corrupted_file_returns_defaults(self):
        path = os.path.join(self._tmp.name, mon.TRAFFIC_GUARD_FILE)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
        cfg = mon.load_traffic_guard()
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["threshold_gb"], 200)

    def test_save_and_load_roundtrip(self):
        cfg = {
            "enabled": True,
            "threshold_gb": 5120,
            "action": "block",
            "last_triggered": "2026-11-23T10:00:00+00:00",
            "chat_id": 654321,
        }
        mon.save_traffic_guard(cfg)
        self.assertEqual(mon.load_traffic_guard(), cfg)

    def test_invalid_action_falls_back_to_notify(self):
        self.write_json(mon.TRAFFIC_GUARD_FILE,
                        {"enabled": True, "action": "explode"})
        self.assertEqual(mon.load_traffic_guard()["action"], "notify")

    def test_invalid_threshold_falls_back_to_default(self):
        for bad in (0, -5, "abc", None):
            with self.subTest(bad=bad):
                self.write_json(mon.TRAFFIC_GUARD_FILE,
                                {"threshold_gb": bad})
                self.assertEqual(
                    mon.load_traffic_guard()["threshold_gb"], 200)

    def test_integer_threshold_stays_integer(self):
        self.write_json(mon.TRAFFIC_GUARD_FILE, {"threshold_gb": 5120.0})
        threshold = mon.load_traffic_guard()["threshold_gb"]
        self.assertEqual(threshold, 5120)
        self.assertIsInstance(threshold, int)


class TestShouldTriggerTrafficGuard(MonitoringTestCase):

    def setUp(self):
        super().setUp()
        self.now = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)

    def _cfg(self, **overrides):
        cfg = mon.default_traffic_guard()
        cfg.update(overrides)
        return cfg

    def test_disabled_never_triggers(self):
        cfg = self._cfg(enabled=False)
        self.assertFalse(mon.should_trigger_traffic_guard(cfg, 99999,
                                                          now=self.now))

    def test_below_threshold_does_not_trigger(self):
        cfg = self._cfg(enabled=True, threshold_gb=10240)
        self.assertFalse(mon.should_trigger_traffic_guard(cfg, 10239.9,
                                                          now=self.now))

    def test_at_threshold_triggers(self):
        cfg = self._cfg(enabled=True, threshold_gb=10240)
        self.assertTrue(mon.should_trigger_traffic_guard(cfg, 10240,
                                                         now=self.now))

    def test_above_threshold_triggers(self):
        cfg = self._cfg(enabled=True, threshold_gb=10240)
        self.assertTrue(mon.should_trigger_traffic_guard(cfg, 15000,
                                                         now=self.now))

    def test_within_24h_cooldown_does_not_trigger(self):
        last = (self.now - timedelta(hours=23, minutes=59)).isoformat()
        cfg = self._cfg(enabled=True, last_triggered=last)
        self.assertFalse(mon.should_trigger_traffic_guard(cfg, 15000,
                                                          now=self.now))

    def test_after_24h_cooldown_triggers_again(self):
        last = (self.now - timedelta(hours=24, minutes=1)).isoformat()
        cfg = self._cfg(enabled=True, last_triggered=last)
        self.assertTrue(mon.should_trigger_traffic_guard(cfg, 15000,
                                                         now=self.now))

    def test_bad_last_triggered_is_ignored(self):
        cfg = self._cfg(enabled=True, last_triggered="not-a-date")
        self.assertTrue(mon.should_trigger_traffic_guard(cfg, 15000,
                                                         now=self.now))


class TestRecordTrafficGuardTrigger(MonitoringTestCase):

    def test_records_timestamp_and_persists(self):
        cfg = mon.default_traffic_guard()
        cfg["enabled"] = True
        now = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
        mon.record_traffic_guard_trigger(cfg, now=now)
        self.assertEqual(cfg["last_triggered"], now.isoformat())
        saved = self.read_json(mon.TRAFFIC_GUARD_FILE)
        self.assertEqual(saved["last_triggered"], now.isoformat())
        # 刚触发完，立即再判断应被冷却挡住。
        self.assertFalse(mon.should_trigger_traffic_guard(cfg, 99999,
                                                          now=now))


class TestCurrentMonthTransferGb(MonitoringTestCase):

    def test_sums_only_current_month_data_transfer(self):
        now = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
        items = [
            {"service": "Data Transfer", "time-usage-started":
             "2026-11-01T00:00:00Z", "computed-quantity": 100.5},
            {"service": "Data Transfer", "time-usage-started":
             "2026-11-15T00:00:00Z", "computed-quantity": 200},
            # 上个月的不算。
            {"service": "Data Transfer", "time-usage-started":
             "2026-10-31T00:00:00Z", "computed-quantity": 9999},
            # 非流量服务不算。
            {"service": "Compute", "time-usage-started":
             "2026-11-10T00:00:00Z", "computed-quantity": 8888},
            # 坏数据跳过。
            {"service": "Data Transfer", "time-usage-started":
             "2026-11-10T00:00:00Z", "computed-quantity": "bad"},
            {"service": "Data Transfer", "time-usage-started":
             "2026-11-10T00:00:00Z"},
        ]
        self.assertAlmostEqual(
            mon.current_month_transfer_gb(items, now=now), 300.5)

    def test_empty_items_is_zero(self):
        self.assertEqual(mon.current_month_transfer_gb([]), 0.0)
        self.assertEqual(mon.current_month_transfer_gb(None), 0.0)


class TestKeepaliveConfig(MonitoringTestCase):

    def test_empty_when_file_missing(self):
        self.assertEqual(mon.load_keepalive(), {})

    def test_save_and_load_roundtrip(self):
        cfg = {FAKE_OCID_1: True, FAKE_OCID_2: False}
        mon.save_keepalive(cfg)
        self.assertEqual(mon.load_keepalive(), cfg)

    def test_enabled_instances_only_true(self):
        cfg = {FAKE_OCID_1: True, FAKE_OCID_2: False}
        self.assertEqual(mon.keepalive_enabled_instances(cfg), {FAKE_OCID_1})
        self.assertEqual(mon.keepalive_enabled_instances({}), set())
        self.assertEqual(mon.keepalive_enabled_instances(None), set())

    def test_state_defaults(self):
        state = mon.load_keepalive_state()
        self.assertEqual(state, {"chat_id": None, "failures": {},
                                 "gave_up": []})

    def test_state_roundtrip(self):
        state = {"chat_id": 654321,
                 "failures": {FAKE_OCID_1: 2},
                 "gave_up": [FAKE_OCID_2]}
        mon.save_keepalive_state(state)
        self.assertEqual(mon.load_keepalive_state(), state)


class TestKeepaliveFailures(MonitoringTestCase):

    def test_failure_count_increments(self):
        state = mon.load_keepalive_state()
        count, gave_up = mon.keepalive_record_failure(state, FAKE_OCID_1)
        self.assertEqual((count, gave_up), (1, False))
        count, gave_up = mon.keepalive_record_failure(state, FAKE_OCID_1)
        self.assertEqual((count, gave_up), (2, False))
        self.assertEqual(state["failures"][FAKE_OCID_1], 2)
        self.assertEqual(state["gave_up"], [])

    def test_third_failure_gives_up(self):
        state = mon.load_keepalive_state()
        for _ in range(2):
            mon.keepalive_record_failure(state, FAKE_OCID_1)
        count, gave_up = mon.keepalive_record_failure(state, FAKE_OCID_1)
        self.assertEqual((count, gave_up), (3, True))
        self.assertIn(FAKE_OCID_1, state["gave_up"])
        # 再失败也不重复进停试名单。
        mon.keepalive_record_failure(state, FAKE_OCID_1)
        self.assertEqual(state["gave_up"].count(FAKE_OCID_1), 1)

    def test_success_clears_failures_but_not_gave_up(self):
        state = {"chat_id": None,
                 "failures": {FAKE_OCID_1: 3},
                 "gave_up": [FAKE_OCID_1]}
        mon.keepalive_record_success(state, FAKE_OCID_1)
        self.assertNotIn(FAKE_OCID_1, state["failures"])
        self.assertIn(FAKE_OCID_1, state["gave_up"])

    def test_reset_clears_both(self):
        state = {"chat_id": None,
                 "failures": {FAKE_OCID_1: 3},
                 "gave_up": [FAKE_OCID_1]}
        mon.keepalive_reset_instance(state, FAKE_OCID_1)
        self.assertEqual(state["failures"], {})
        self.assertEqual(state["gave_up"], [])

    def test_failures_are_per_instance(self):
        state = mon.load_keepalive_state()
        mon.keepalive_record_failure(state, FAKE_OCID_1)
        mon.keepalive_record_failure(state, FAKE_OCID_1)
        mon.keepalive_record_failure(state, FAKE_OCID_2)
        self.assertEqual(state["failures"][FAKE_OCID_1], 2)
        self.assertEqual(state["failures"][FAKE_OCID_2], 1)


class TestSslWatchConfig(MonitoringTestCase):

    def test_defaults_when_file_missing(self):
        self.assertEqual(mon.load_ssl_watch(),
                         {"domains": [], "reminded": {}, "chat_id": None})

    def test_save_and_load_roundtrip(self):
        cfg = {"domains": ["example.com", "www.example.org"],
               "reminded": {"example.com": [14, 30]},
               "chat_id": 654321}
        mon.save_ssl_watch(cfg)
        self.assertEqual(mon.load_ssl_watch(), cfg)

    def test_reminded_thresholds_normalized_and_sorted(self):
        self.write_json(mon.SSL_WATCH_FILE,
                        {"domains": ["example.com"],
                         "reminded": {"example.com": [14, 30, 14]}})
        self.assertEqual(mon.load_ssl_watch()["reminded"],
                         {"example.com": [14, 30]})


class TestSslReminderDue(MonitoringTestCase):

    def test_no_due_when_far_from_expiry(self):
        due, new = mon.ssl_reminder_due(45, [])
        self.assertEqual((due, new), ([], []))

    def test_due_at_30_days(self):
        due, new = mon.ssl_reminder_due(30, [])
        self.assertEqual(due, [30])
        self.assertEqual(new, [30])

    def test_already_reminded_threshold_not_repeated(self):
        due, new = mon.ssl_reminder_due(25, [30])
        self.assertEqual((due, new), ([], [30]))

    def test_next_threshold_due_at_14(self):
        due, new = mon.ssl_reminder_due(14, [30])
        self.assertEqual(due, [14])
        self.assertEqual(new, [14, 30])

    def test_late_added_domain_catches_up_all_crossed(self):
        # 域名刚加入时只剩 5 天：30/14/7 三个阈值都已跨过，一次性都记上，
        # 调用方只发一条提醒，避免连发三条。
        due, new = mon.ssl_reminder_due(5, [])
        self.assertEqual(due, [30, 14, 7])
        self.assertEqual(new, [7, 14, 30])

    def test_renewal_prunes_reminded_thresholds(self):
        # 证书续期后剩余天数回升到 90 天：已提醒的阈值全部清掉，
        # 下次到期时可重新提醒。
        due, new = mon.ssl_reminder_due(90, [30, 14, 7, 1])
        self.assertEqual((due, new), ([], []))

    def test_partial_renewal_prunes_only_uncrossed(self):
        # 剩余 20 天：30 仍跨过（保留），14/7/1 未跨过（剪掉）。
        due, new = mon.ssl_reminder_due(20, [30, 14])
        self.assertEqual((due, new), ([], [30]))

    def test_expired_cert_due_all_unreminded(self):
        due, new = mon.ssl_reminder_due(-3, [30])
        self.assertEqual(due, [14, 7, 1])
        self.assertEqual(new, [1, 7, 14, 30])

    def test_one_day_left(self):
        due, new = mon.ssl_reminder_due(1, [30, 14, 7])
        self.assertEqual(due, [1])
        self.assertEqual(new, [1, 7, 14, 30])


class TestDomainValidation(MonitoringTestCase):

    def test_normalize_strips_scheme_path_port(self):
        self.assertEqual(mon.normalize_domain("https://Example.COM:443/a/b"),
                         "example.com")
        self.assertEqual(mon.normalize_domain("  www.example.org. "),
                         "www.example.org")
        self.assertEqual(mon.normalize_domain("example.com?x=1"),
                         "example.com")

    def test_valid_domains(self):
        for domain in ("example.com", "www.example.org",
                       "a-b.example.co.uk", "xn--nxasmq6b.example"):
            with self.subTest(domain=domain):
                self.assertTrue(mon.is_valid_domain(domain))

    def test_invalid_domains(self):
        for domain in ("", "example", "not a domain", "a..b",
                       "-bad.com", "bad-.com", ".example.com",
                       "exa_mple.com", "a" * 64 + ".com"):
            with self.subTest(domain=domain):
                self.assertFalse(mon.is_valid_domain(domain))


if __name__ == "__main__":
    unittest.main()
