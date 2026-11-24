#!/usr/bin/env python3
"""GFW 自动换 IP（菜单 16）的测试：Atlas 判定、配置存取、换 IP 循环。

不调真实 Atlas/OCI：Atlas 响应与 checker/changer 全部 mock。
"""

import io
import json
import os
import tempfile
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from tgbot import gfw


def _probe_result(rtts):
    """造一条探针结果：rtts 是每包的 rtt（None 表示超时丢包）。"""
    return {"prb_id": 1,
            "result": [{"rtt": r} if r is not None else {"x": "*"}
                       for r in rtts]}


class GfwEvaluateTests(unittest.TestCase):
    def test_all_reachable_is_healthy(self):
        verdict, ok, n, _ = gfw.evaluate_results(
            [_probe_result([180.0, 175.0, 190.0]) for _ in range(5)])
        self.assertEqual((verdict, ok, n), ("healthy", 5, 5))

    def test_none_reachable_is_blocked(self):
        verdict, ok, n, detail = gfw.evaluate_results(
            [_probe_result([None, None, None]) for _ in range(5)])
        self.assertEqual((verdict, ok, n), ("blocked", 0, 5))
        self.assertIn("0 个能 ping 通", detail)

    def test_majority_down_is_blocked(self):
        results = ([_probe_result([None, None, None]) for _ in range(4)]
                   + [_probe_result([150.0, None, None])])
        verdict, ok, n, _ = gfw.evaluate_results(results)
        self.assertEqual((verdict, ok, n), ("blocked", 1, 5))

    def test_half_up_is_healthy(self):
        # 5 个探针 3 个通（60%）→ 正常（阈值是“不到一半算被墙”）。
        results = ([_probe_result([150.0, None, None]) for _ in range(3)]
                   + [_probe_result([None, None, None]) for _ in range(2)])
        verdict, ok, n, _ = gfw.evaluate_results(results)
        self.assertEqual((verdict, ok, n), ("healthy", 3, 5))

    def test_one_packet_enough(self):
        # 探针只要 1 个包有回包就算可达（单个包丢包不算墙）。
        verdict, ok, n, _ = gfw.evaluate_results(
            [_probe_result([None, None, 200.0]) for _ in range(5)])
        self.assertEqual((verdict, ok, n), ("healthy", 5, 5))

    def test_too_few_reported_is_unknown(self):
        verdict, ok, n, detail = gfw.evaluate_results(
            [_probe_result([None, None, None]) for _ in range(2)])
        self.assertEqual((verdict, ok, n), ("unknown", 0, 2))
        self.assertIn("无法判定", detail)

    def test_empty_results_is_unknown(self):
        verdict, _, n, _ = gfw.evaluate_results([])
        self.assertEqual((verdict, n), ("unknown", 0))

    def test_malformed_entries_skipped(self):
        results = ([None, {}, {"result": "bogus"}, {"result": []}]
                   + [_probe_result([150.0]) for _ in range(3)])
        verdict, ok, n, _ = gfw.evaluate_results(results)
        self.assertEqual((verdict, ok, n), ("healthy", 3, 3))


class GfwConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._patch = patch.object(gfw.bot_config, "DATA_DIR", self.tmp.name)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_empty_config_when_missing(self):
        cfg = gfw.load_gfw_config()
        self.assertEqual(cfg, {"api_key": "", "enabled": {},
                               "last_check": {}})

    def test_save_load_roundtrip_and_perms(self):
        cfg = {"api_key": "k", "enabled": {"i1": {"profile": "DEFAULT"}},
               "last_check": {}}
        gfw.save_gfw_config(cfg)
        self.assertEqual(gfw.load_gfw_config(), cfg)
        mode = os.stat(os.path.join(self.tmp.name, "gfw.json")).st_mode
        self.assertEqual(mode & 0o777, 0o600)

    def test_corrupt_config_returns_empty(self):
        with open(os.path.join(self.tmp.name, "gfw.json"), "w") as fh:
            fh.write("{not json")
        self.assertEqual(gfw.load_gfw_config()["api_key"], "")

    def test_mask_api_key(self):
        self.assertEqual(gfw.mask_api_key(""), "未设置")
        self.assertEqual(gfw.mask_api_key("short"), "****")
        self.assertEqual(gfw.mask_api_key("12345678-90ab-cdef-1234"),
                         "1234…1234")

    def test_set_api_key_trims_and_masks(self):
        masked = gfw.set_api_key("  abcdefgh-1234-5678-90ab-cdef12345678  ")
        self.assertEqual(masked, "abcd…5678")
        self.assertTrue(gfw.is_gfw_configured())
        self.assertEqual(gfw.load_gfw_config()["api_key"],
                         "abcdefgh-1234-5678-90ab-cdef12345678")

    def test_enable_disable_instance(self):
        gfw.set_instance_enabled("i1", True, profile="DEFAULT",
                                 region="ap-singapore-1",
                                 name="web", ip="1.2.3.4")
        cfg = gfw.load_gfw_config()
        self.assertEqual(cfg["enabled"]["i1"]["name"], "web")
        self.assertEqual(cfg["enabled"]["i1"]["ip"], "1.2.3.4")
        gfw.set_instance_enabled("i1", False)
        self.assertEqual(gfw.load_gfw_config()["enabled"], {})

    def test_record_last_check(self):
        gfw.record_last_check("i1", {"status": "healthy", "changes": 0,
                                     "ip": "1.2.3.4", "detail": "ok"})
        entry = gfw.load_gfw_config()["last_check"]["i1"]
        self.assertEqual(entry["status"], "healthy")
        self.assertIn("at", entry)


class GfwAtlasClientTests(unittest.TestCase):
    def test_create_measurement_posts_country_cn(self):
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            seen["body"] = json.loads(req.data.decode("utf-8"))
            resp = MagicMock()
            resp.read.return_value = b'{"measurements": [424242]}'
            resp.__enter__.return_value = resp
            return resp

        with patch.object(gfw.urllib.request, "urlopen",
                          side_effect=fake_urlopen):
            msm_id = gfw.create_ping_measurement("KEY", "1.2.3.4")
        self.assertEqual(msm_id, 424242)
        self.assertIn("key=KEY", seen["url"])
        definition = seen["body"]["definitions"][0]
        self.assertEqual(definition["target"], "1.2.3.4")
        self.assertEqual(definition["type"], "ping")
        self.assertTrue(seen["body"]["is_oneoff"])
        self.assertEqual(seen["body"]["probes"][0]["value"], "CN")

    def test_create_measurement_no_id_raises(self):
        resp = MagicMock()
        resp.read.return_value = b'{"measurements": []}'
        resp.__enter__.return_value = resp
        with patch.object(gfw.urllib.request, "urlopen",
                          return_value=resp):
            with self.assertRaises(gfw.AtlasError):
                gfw.create_ping_measurement("KEY", "1.2.3.4")

    def test_http_error_wrapped_as_atlas_error(self):
        err = urllib.error.HTTPError(
            "https://atlas.ripe.net/api/v2/measurements/", 403,
            "Forbidden", {}, io.BytesIO(b'{"error": "bad key"}'))
        with patch.object(gfw.urllib.request, "urlopen",
                          side_effect=err):
            with self.assertRaises(gfw.AtlasError) as ctx:
                gfw.create_ping_measurement("BAD", "1.2.3.4")
        self.assertIn("403", str(ctx.exception))

    def test_check_ip_blocked_healthy_flow(self):
        results = [_probe_result([150.0]) for _ in range(5)]
        with patch.object(gfw, "create_ping_measurement",
                          return_value=99) as mk, \
             patch.object(gfw, "wait_measurement_results",
                          return_value=results) as wait:
            out = gfw.check_ip_blocked("KEY", "1.2.3.4")
        mk.assert_called_once_with("KEY", "1.2.3.4")
        wait.assert_called_once_with("KEY", 99)
        self.assertEqual(out["blocked"], False)
        self.assertEqual(out["measurement_id"], 99)

    def test_check_ip_blocked_api_failure_is_unknown(self):
        with patch.object(gfw, "create_ping_measurement",
                          side_effect=gfw.AtlasError("boom")):
            out = gfw.check_ip_blocked("KEY", "1.2.3.4")
        self.assertIsNone(out["blocked"])
        self.assertIn("error", out)

    def test_wait_stops_when_enough_results(self):
        calls = []

        def fake_fetch(key, msm_id):
            calls.append(1)
            if len(calls) < 3:
                return [_probe_result([1.0])]
            return [_probe_result([1.0]) for _ in range(5)]

        with patch.object(gfw, "fetch_measurement_results",
                          side_effect=fake_fetch), \
             patch.object(gfw.time, "sleep") as sleep:
            out = gfw.wait_measurement_results("K", 1, requested=5,
                                               timeout=60, interval=10)
        self.assertEqual(len(out), 5)
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleep.call_count, 2)


class GfwCycleTests(unittest.TestCase):
    def setUp(self):
        self.notes = []
        self.instance = {"id": "i1", "display-name": "web",
                         "public-ip": "1.1.1.1"}

    def _notify(self, title, body):
        self.notes.append((title, body))

    def _blocked_then_healthy(self, key, ip):
        if ip == "1.1.1.1":
            return {"blocked": True, "detail": "国内 5 个探针 0 个能 ping 通"}
        return {"blocked": False, "detail": "国内 5 个探针 5 个能 ping 通"}

    def test_healthy_instance_no_change_no_notify(self):
        out = gfw.run_instance_cycle(
            MagicMock(), "comp", self.instance, "KEY", self._notify,
            checker=lambda k, ip: {"blocked": False, "detail": "ok"},
            changer=MagicMock())
        self.assertEqual(out["status"], "healthy")
        self.assertEqual(out["changes"], 0)
        self.assertEqual(self.notes, [])

    def test_blocked_then_recovered_one_change_one_note(self):
        changer = MagicMock(return_value="2.2.2.2")
        out = gfw.run_instance_cycle(
            MagicMock(), "comp", self.instance, "KEY", self._notify,
            checker=self._blocked_then_healthy, changer=changer)
        self.assertEqual(out["status"], "recovered")
        self.assertEqual(out["changes"], 1)
        self.assertEqual(out["ip"], "2.2.2.2")
        changer.assert_called_once()
        self.assertEqual(len(self.notes), 1)
        title, body = self.notes[0]
        self.assertIn("换 IP", title)
        self.assertIn("1.1.1.1", body)
        self.assertIn("2.2.2.2", body)

    def test_still_blocked_loops_and_notifies_each_time(self):
        ips = iter(["2.2.2.2", "3.3.3.3"])
        changer = MagicMock(side_effect=lambda *a: next(ips))

        def checker(key, ip):
            if ip in ("1.1.1.1", "2.2.2.2"):
                return {"blocked": True,
                        "detail": f"{ip} 国内 5 个探针 0 个能 ping 通"}
            return {"blocked": False, "detail": "国内 5 个探针 5 个能 ping 通"}

        out = gfw.run_instance_cycle(
            MagicMock(), "comp", self.instance, "KEY", self._notify,
            checker=checker, changer=changer)
        self.assertEqual(out["status"], "recovered")
        self.assertEqual(out["changes"], 2)
        self.assertEqual(changer.call_count, 2)
        # 第一次换完仍被墙一条通知，第二次换完恢复一条通知。
        self.assertEqual(len(self.notes), 2)
        self.assertIn("仍被墙", self.notes[0][0])
        self.assertIn("3.3.3.3", self.notes[1][1])

    def test_exhausted_after_max_changes(self):
        changer = MagicMock(side_effect=["2.2.2.2", "3.3.3.3", "4.4.4.4"])
        out = gfw.run_instance_cycle(
            MagicMock(), "comp", self.instance, "KEY", self._notify,
            checker=lambda k, ip: {"blocked": True,
                                   "detail": "国内 5 个探针 0 个能 ping 通"},
            changer=changer, max_changes=2)
        self.assertEqual(out["status"], "exhausted")
        self.assertEqual(out["changes"], 2)
        self.assertEqual(changer.call_count, 2)
        # 两次换 IP 通知 + 一条“已暂停”通知。
        self.assertEqual(len(self.notes), 3)
        self.assertIn("已暂停", self.notes[-1][0])

    def test_unknown_verdict_no_change_no_notify(self):
        out = gfw.run_instance_cycle(
            MagicMock(), "comp", self.instance, "KEY", self._notify,
            checker=lambda k, ip: {"blocked": None, "detail": "上报太少"},
            changer=MagicMock())
        self.assertEqual(out["status"], "unknown")
        self.assertEqual(self.notes, [])

    def test_new_ip_unknown_stops_after_notify(self):
        calls = []

        def checker(key, ip):
            calls.append(ip)
            if ip == "1.1.1.1":
                return {"blocked": True, "detail": "国内 0/5"}
            return {"blocked": None, "detail": "上报太少"}

        out = gfw.run_instance_cycle(
            MagicMock(), "comp", self.instance, "KEY", self._notify,
            checker=checker, changer=MagicMock(return_value="2.2.2.2"))
        self.assertEqual(out["status"], "unknown")
        self.assertEqual(out["changes"], 1)
        self.assertEqual(len(self.notes), 1)
        self.assertIn("检测无结论", self.notes[0][1])

    def test_changer_failure_notifies_and_returns_error(self):
        out = gfw.run_instance_cycle(
            MagicMock(), "comp", self.instance, "KEY", self._notify,
            checker=lambda k, ip: {"blocked": True, "detail": "国内 0/5"},
            changer=MagicMock(side_effect=RuntimeError("OCI 挂了")))
        self.assertEqual(out["status"], "error")
        self.assertEqual(len(self.notes), 1)
        self.assertIn("失败", self.notes[0][0])

    def test_no_public_ip_skipped(self):
        inst = {"id": "i1", "display-name": "web"}
        out = gfw.run_instance_cycle(
            MagicMock(), "comp", inst, "KEY", self._notify,
            checker=MagicMock(), changer=MagicMock())
        self.assertEqual(out["status"], "no_ip")
        self.assertEqual(self.notes, [])


class GfwMonitorCycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._patch = patch.object(gfw.bot_config, "DATA_DIR", self.tmp.name)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.addCleanup(self.tmp.cleanup)
        self.notes = []

    def _notify(self, title, body):
        self.notes.append((title, body))

    def test_no_key_returns_empty(self):
        gfw.save_gfw_config({"api_key": "", "enabled": {"i1": {}},
                             "last_check": {}})
        self.assertEqual(gfw.run_monitor_cycle(self._notify), [])

    def test_no_enabled_returns_empty(self):
        gfw.save_gfw_config({"api_key": "K", "enabled": {},
                             "last_check": {}})
        self.assertEqual(gfw.run_monitor_cycle(self._notify), [])

    def test_cycle_runs_each_instance_and_records(self):
        gfw.save_gfw_config({
            "api_key": "K",
            "enabled": {"i1": {"profile": "DEFAULT"},
                        "i2": {"profile": "DEFAULT"}},
            "last_check": {}})

        def resolver(enabled):
            return ([{"instance_id": "i1", "profile": "DEFAULT",
                      "region": "ap-singapore-1",
                      "instance": {"id": "i1", "display-name": "web",
                                   "public-ip": "1.1.1.1"},
                      "cli": MagicMock(), "compartment_id": "c"}],
                    {"i2": "实例未运行（STOPPED）"})

        out = gfw.run_monitor_cycle(
            self._notify,
            checker=lambda k, ip: {"blocked": False, "detail": "ok"},
            changer=MagicMock(), instance_resolver=resolver)
        self.assertEqual(len(out), 2)
        by_id = {r["instance_id"]: r for r in out}
        self.assertEqual(by_id["i1"]["status"], "healthy")
        self.assertEqual(by_id["i2"]["status"], "skipped")
        last = gfw.load_gfw_config()["last_check"]
        self.assertEqual(last["i1"]["status"], "healthy")
        self.assertEqual(last["i2"]["status"], "skipped")


if __name__ == "__main__":
    unittest.main()
