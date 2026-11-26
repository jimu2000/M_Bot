#!/usr/bin/env python3
"""grabber.py 的单元测试（mock OCIClient，不碰真实 API）。

运行：python3 -m unittest tests.test_grabber -v
注意：本测试绝不对真实 API 发起 launch / terminate；编排器正在真实抢机，
测试必须与它零干扰。所有 OCI 调用都是 mock。
"""

import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oci.client import (OCIClient, OutOfCapacityError, QuotaExceededError,
                        RateLimitError)
from oci.grabber import (BACKOFF_INITIAL, CREATE_INTERVAL, CREATE_JITTER,
                         Grabber)

# Placeholders (no real OCIDs appear in tests). 占位符（测试里不出现真实 OCID）。
FAKE_COMPARTMENT = "ocid1.tenancy.oc1..xxxxxxxx"
FAKE_AD = "xxxx:AP-SINGAPORE-1-AD-1"
FAKE_IMAGE = "ocid1.image.oc1.ap-singapore-1.xxxxxxxx"
FAKE_SUBNET = "ocid1.subnet.oc1.ap-singapore-1.xxxxxxxx"
FAKE_INSTANCE_A = "ocid1.instance.oc1.ap-singapore-1.aaaaaaaa"
FAKE_INSTANCE_B = "ocid1.instance.oc1.ap-singapore-1.bbbbbbbb"


def _make_grabber(cli=None, **kwargs):
    """造一个用假值（占位符）的 Grabber，cli 默认是 Mock。"""
    params = dict(
        cli=cli if cli is not None else MagicMock(spec=OCIClient),
        compartment_id=FAKE_COMPARTMENT,
        availability_domain=FAKE_AD,
        shape="VM.Standard.E2.1.Micro",
        image_id=FAKE_IMAGE,
        subnet_id=FAKE_SUBNET,
        display_name="test-instance",
        ssh_keys_file="/tmp/fake_keys.pub",
    )
    params.update(kwargs)
    return Grabber(**params)


def _launch_args(cli):
    """从 mock cli 的调用记录里取 launch 命令参数列表（应只调一次）。"""
    calls = [c.args[0] for c in cli.run_text.call_args_list
             if "launch" in c.args[0]]
    assert len(calls) == 1, f"launch 应只调一次，实际 {len(calls)} 次"
    return calls[0]


def _mock_launch_cli(launch_results=None, instances=None,
                     instance_state="TERMINATED"):
    """造一个 mock cli：launch 走 run_text，读接口走 SDK mock。

    launch_results: launch 调用的返回值序列（元素也可以是异常实例）。
    instances: find_existing 用的 sdk_list_instances 返回值（默认 []，
        即无同名实例；每项是 kebab-case 字典，含 id / time-created /
        lifecycle-state）。
    instance_state: get_instance_state 用的状态（sdk_get_instance 返回）。
    """
    cli = MagicMock(spec=OCIClient)
    launch_iter = iter(launch_results or [])

    def dispatch(args, **kwargs):
        if "launch" not in args:
            raise AssertionError(f"未预期的 run_text 调用：{args}")
        result = next(launch_iter)
        if isinstance(result, Exception):
            raise result
        return result

    cli.run_text.side_effect = dispatch
    cli.sdk_list_instances.return_value = (
        [] if instances is None else instances)
    cli.sdk_get_instance.return_value = {"lifecycle-state": instance_state}
    cli.sdk_list_vnics.return_value = []
    return cli


def _launch_once_with(**kwargs):
    """用给定 Grabber 参数做一次 try_launch_once，返回 (grabber, launch 参数)。"""
    cli = _mock_launch_cli(launch_results=[FAKE_INSTANCE_A])
    grabber = _make_grabber(cli=cli, **kwargs)
    grabber.try_launch_once()
    return grabber, _launch_args(cli)


class TestTryLaunchOnce(unittest.TestCase):
    def test_success_returns_ocid(self):
        cli = _mock_launch_cli(launch_results=[FAKE_INSTANCE_A])
        grabber = _make_grabber(cli=cli)
        self.assertEqual(grabber.try_launch_once(), FAKE_INSTANCE_A)

    def test_launch_command_contains_no_retry(self):
        cli = _mock_launch_cli(launch_results=[FAKE_INSTANCE_A])
        grabber = _make_grabber(cli=cli)
        grabber.try_launch_once()
        launch_args = [c.args[0] for c in cli.run_text.call_args_list
                       if "launch" in c.args[0]]
        self.assertEqual(len(launch_args), 1)
        self.assertIn("--no-retry", launch_args[0])
        self.assertIn("--shape", launch_args[0])
        self.assertIn("VM.Standard.E2.1.Micro", launch_args[0])

    def test_existing_instance_reused_without_launch(self):
        created = datetime.now(timezone.utc).isoformat()
        cli = _mock_launch_cli(instances=[{
            "id": FAKE_INSTANCE_A, "time-created": created}])
        grabber = _make_grabber(cli=cli)
        self.assertEqual(grabber.try_launch_once(), FAKE_INSTANCE_A)
        launch_calls = [c for c in cli.run_text.call_args_list
                        if "launch" in c.args[0]]
        self.assertEqual(launch_calls, [])

    def test_old_same_name_instance_is_not_reused(self):
        created = (datetime.now(timezone.utc)
                   - timedelta(hours=1)).isoformat()
        cli = _mock_launch_cli(
            instances=[{"id": FAKE_INSTANCE_A, "time-created": created}],
            launch_results=[FAKE_INSTANCE_B])
        grabber = _make_grabber(cli=cli)
        self.assertEqual(grabber.try_launch_once(), FAKE_INSTANCE_B)

    def test_missing_created_falls_back_to_old_behavior(self):
        cli = _mock_launch_cli(instances=[{"id": FAKE_INSTANCE_A}])
        grabber = _make_grabber(cli=cli)
        self.assertEqual(grabber.try_launch_once(), FAKE_INSTANCE_A)

    def test_terminated_same_name_is_not_reused(self):
        # A terminated same-name instance is never reused even when created within 15 minutes: reusing a dead machine turns the grab result into an unusable instance. 已终止的同名实例即使在 15 分钟内创建也不复用：复用死机会把抢机结果变成无法使用的实例。
        created = datetime.now(timezone.utc).isoformat()
        cli = _mock_launch_cli(
            instances=[{"id": FAKE_INSTANCE_A,
                        "time-created": created,
                        "lifecycle-state": "TERMINATED"}],
            launch_results=[FAKE_INSTANCE_B])
        grabber = _make_grabber(cli=cli)
        self.assertEqual(grabber.try_launch_once(), FAKE_INSTANCE_B)

    def test_stopped_same_name_is_not_reused(self):
        created = datetime.now(timezone.utc).isoformat()
        cli = _mock_launch_cli(
            instances=[{"id": FAKE_INSTANCE_A,
                        "time-created": created,
                        "lifecycle-state": "STOPPED"}],
            launch_results=[FAKE_INSTANCE_B])
        grabber = _make_grabber(cli=cli)
        self.assertEqual(grabber.try_launch_once(), FAKE_INSTANCE_B)

    def test_running_same_name_list_shape_is_reused(self):
        created = datetime.now(timezone.utc).isoformat()
        cli = _mock_launch_cli(instances=[{
            "id": FAKE_INSTANCE_A, "time-created": created,
            "lifecycle-state": "RUNNING"}])
        grabber = _make_grabber(cli=cli)
        self.assertEqual(grabber.try_launch_once(), FAKE_INSTANCE_A)
        launch_calls = [c for c in cli.run_text.call_args_list
                        if "launch" in c.args[0]]
        self.assertEqual(launch_calls, [])

    def test_newest_terminated_but_older_running_is_reused(self):
        now = datetime.now(timezone.utc)
        cli = _mock_launch_cli(instances=[
            {"id": FAKE_INSTANCE_B,
             "time-created": now.isoformat(),
             "lifecycle-state": "TERMINATED"},
            {"id": FAKE_INSTANCE_A,
             "time-created": (now - timedelta(minutes=5)).isoformat(),
             "lifecycle-state": "RUNNING"},
        ])
        grabber = _make_grabber(cli=cli)
        self.assertEqual(grabber.try_launch_once(), FAKE_INSTANCE_A)

    def test_find_existing_null_returns_none(self):
        cli = _mock_launch_cli(instances=[],
                               launch_results=[FAKE_INSTANCE_A])
        grabber = _make_grabber(cli=cli)
        self.assertEqual(grabber.try_launch_once(), FAKE_INSTANCE_A)

    def test_out_of_capacity_returns_none(self):
        cli = _mock_launch_cli(
            launch_results=[OutOfCapacityError("Out of host capacity")])
        grabber = _make_grabber(cli=cli)
        self.assertIsNone(grabber.try_launch_once())

    def test_rate_limit_propagates(self):
        cli = _mock_launch_cli(
            launch_results=[RateLimitError("TooManyRequests")])
        grabber = _make_grabber(cli=cli)
        with self.assertRaises(RateLimitError):
            grabber.try_launch_once()

    def test_quota_propagates(self):
        cli = _mock_launch_cli(
            launch_results=[QuotaExceededError("LimitExceeded")])
        grabber = _make_grabber(cli=cli)
        with self.assertRaises(QuotaExceededError):
            grabber.try_launch_once()


class TestRun(unittest.TestCase):
    @patch("oci.grabber.time.sleep")
    def test_rate_limit_backoff_doubles(self, mock_sleep):
        cli = _mock_launch_cli(
            launch_results=[RateLimitError("TooManyRequests"),
                            RateLimitError("TooManyRequests"),
                            FAKE_INSTANCE_A])
        events = []
        grabber = _make_grabber(cli=cli, on_event=lambda e, m: events.append(e))
        result = grabber.run()
        self.assertEqual(result.instance_id, FAKE_INSTANCE_A)
        self.assertEqual(result.attempts, 3)
        sleep_seconds = [c.args[0] for c in mock_sleep.call_args_list]
        self.assertEqual(sleep_seconds, [BACKOFF_INITIAL, BACKOFF_INITIAL * 2])
        self.assertEqual(events.count("rate_limited"), 2)
        self.assertIn("done", events)

    @patch("oci.grabber.time.sleep")
    def test_quota_stops_and_emits_event(self, mock_sleep):
        cli = _mock_launch_cli(
            launch_results=[QuotaExceededError("LimitExceeded")])
        on_event = MagicMock()
        grabber = _make_grabber(cli=cli, on_event=on_event)
        with self.assertRaises(QuotaExceededError):
            grabber.run()
        quota_calls = [c for c in on_event.call_args_list
                       if c.args and c.args[0] == "quota"]
        self.assertEqual(len(quota_calls), 1)
        launch_calls = [c for c in cli.run_text.call_args_list
                        if "launch" in c.args[0]]
        self.assertEqual(len(launch_calls), 1)

    @patch("oci.grabber.time.sleep")
    def test_wrong_cpu_terminates_and_continues(self, mock_sleep):
        cli = _mock_launch_cli(
            launch_results=[FAKE_INSTANCE_A, FAKE_INSTANCE_B])
        cli.run.return_value = None
        events = []
        grabber = _make_grabber(cli=cli, target_cpu="7J13",
                                on_event=lambda e, m: events.append(e))
        grabber.cpu_check_hook = MagicMock(
            side_effect=["AMD EPYC 7742", "AMD EPYC 7J13"])
        with patch.object(Grabber, "get_public_ip",
                          return_value="203.0.113.10") as mock_ip:
            result = grabber.run()
        self.assertEqual(result.instance_id, FAKE_INSTANCE_B)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(result.releases, 1)
        self.assertEqual(grabber.cpu_check_hook.call_count, 2)
        self.assertEqual(mock_ip.call_count, 2)
        terminate_calls = [c for c in cli.run.call_args_list
                           if "terminate" in c.args[0]]
        self.assertEqual(len(terminate_calls), 1)
        terminate_args = terminate_calls[0].args[0]
        self.assertIn(FAKE_INSTANCE_A, terminate_args)
        self.assertIn("--preserve-boot-volume", terminate_args)
        self.assertIn("false", terminate_args)
        self.assertIn("--force", terminate_args)
        self.assertIn("wrong_cpu", events)
        self.assertIn("done", events)

    @patch("oci.grabber.time.sleep")
    def test_target_cpu_list_keeps_any_match(self, mock_sleep):
        cli = _mock_launch_cli(
            launch_results=[FAKE_INSTANCE_A, FAKE_INSTANCE_B])
        cli.run.return_value = None
        events = []
        grabber = _make_grabber(cli=cli, target_cpu=["7J13", "7745"],
                                on_event=lambda e, m: events.append(e))
        grabber.cpu_check_hook = MagicMock(
            side_effect=["AMD EPYC 7551", "AMD EPYC 7745"])
        with patch.object(Grabber, "get_public_ip",
                          return_value="203.0.113.10"):
            result = grabber.run()
        self.assertEqual(result.instance_id, FAKE_INSTANCE_B)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(result.releases, 1)
        self.assertEqual(result.cpu_model, "AMD EPYC 7745")
        self.assertIn("wrong_cpu", events)
        self.assertIn("done", events)

    @patch("oci.grabber.time.sleep")
    def test_cpu_check_none_keeps_instance(self, mock_sleep):
        cli = _mock_launch_cli(launch_results=[FAKE_INSTANCE_A])
        events = []
        grabber = _make_grabber(cli=cli, target_cpu=["7J13", "7745"],
                                on_event=lambda e, m: events.append(e))
        grabber.cpu_check_hook = MagicMock(return_value=None)
        with patch.object(Grabber, "get_public_ip",
                          return_value="203.0.113.10"):
            result = grabber.run()
        self.assertEqual(result.instance_id, FAKE_INSTANCE_A)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(result.releases, 0)
        self.assertIsNone(result.cpu_model)
        self.assertIn("cpu_check_failed", events)
        self.assertIn("done", events)
        terminate_calls = [c for c in cli.run.call_args_list
                           if "terminate" in c.args[0]]
        self.assertEqual(len(terminate_calls), 0)

    @patch("oci.grabber.time.sleep")
    def test_cpu_check_raises_keeps_instance(self, mock_sleep):
        cli = _mock_launch_cli(launch_results=[FAKE_INSTANCE_A])
        events = []
        grabber = _make_grabber(cli=cli, target_cpu="7J13",
                                on_event=lambda e, m: events.append(e))
        grabber.cpu_check_hook = MagicMock(
            side_effect=RuntimeError("ssh boom"))
        with patch.object(Grabber, "get_public_ip",
                          return_value="203.0.113.10"):
            result = grabber.run()
        self.assertEqual(result.instance_id, FAKE_INSTANCE_A)
        self.assertEqual(result.releases, 0)
        self.assertIsNone(result.cpu_model)
        self.assertIn("cpu_check_failed", events)
        self.assertIn("done", events)

    def test_target_cpus_normalized(self):
        self.assertEqual(_make_grabber(target_cpu="7J13").target_cpus,
                         ["7J13"])
        self.assertEqual(
            _make_grabber(target_cpu=["7J13", "7745"]).target_cpus,
            ["7J13", "7745"])
        self.assertEqual(_make_grabber(target_cpu=None).target_cpus, [])
        self.assertEqual(_make_grabber(target_cpu="").target_cpus, [])
        self.assertEqual(_make_grabber().target_cpus, [])

    @patch("oci.grabber.time.sleep")
    def test_no_capacity_retries_then_succeeds(self, mock_sleep):
        cli = _mock_launch_cli(
            launch_results=[OutOfCapacityError("Out of host capacity"),
                            FAKE_INSTANCE_A])
        grabber = _make_grabber(cli=cli)
        result = grabber.run()
        self.assertEqual(result.instance_id, FAKE_INSTANCE_A)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(result.releases, 0)
        self.assertGreaterEqual(result.elapsed_seconds, 0)
        self.assertEqual(mock_sleep.call_count, 1)
        wait = mock_sleep.call_args[0][0]
        self.assertGreaterEqual(wait, CREATE_INTERVAL - CREATE_JITTER)
        self.assertLessEqual(wait, CREATE_INTERVAL + CREATE_JITTER)

    @patch("oci.grabber.time.sleep")
    def test_max_attempts_raises(self, mock_sleep):
        cli = _mock_launch_cli(
            launch_results=[OutOfCapacityError("Out of host capacity")] * 5)
        grabber = _make_grabber(cli=cli)
        with self.assertRaises(RuntimeError):
            grabber.run(max_attempts=3)
        launch_calls = [c for c in cli.run_text.call_args_list
                        if "launch" in c.args[0]]
        self.assertEqual(len(launch_calls), 3)


class TestJitter(unittest.TestCase):
    def test_interval_within_range(self):
        grabber = _make_grabber()
        for _ in range(200):
            wait = grabber._next_interval()
            self.assertGreaterEqual(wait, CREATE_INTERVAL - CREATE_JITTER)
            self.assertLessEqual(wait, CREATE_INTERVAL + CREATE_JITTER)


class TestCancellation(unittest.TestCase):
    def test_preset_stop_event_cancels_before_launch(self):
        import threading

        from oci.grabber import GrabCancelledError

        stop_event = threading.Event()
        stop_event.set()
        cli = MagicMock(spec=OCIClient)
        events = []
        grabber = _make_grabber(
            cli=cli, stop_event=stop_event,
            on_event=lambda e, m: events.append(e))
        with self.assertRaises(GrabCancelledError):
            grabber.run()
        self.assertEqual(cli.run_text.call_count, 0)
        self.assertIn("cancelled", events)

    @patch("oci.grabber.time.sleep")
    def test_stop_during_run_cancels_after_first_attempt(self, mock_sleep):
        import threading

        from oci.grabber import GrabCancelledError

        stop_event = threading.Event()
        cli = _mock_launch_cli(
            launch_results=[OutOfCapacityError("Out of host capacity")] * 5)

        def on_event(event_type, message):
            if event_type == "attempt":
                stop_event.set()

        grabber = _make_grabber(cli=cli, stop_event=stop_event,
                                on_event=on_event)
        with self.assertRaises(GrabCancelledError):
            grabber.run()
        launch_calls = [c for c in cli.run_text.call_args_list
                        if "launch" in c.args[0]]
        self.assertEqual(len(launch_calls), 1)
        mock_sleep.assert_not_called()

    def test_sleep_returns_immediately_when_stopped(self):
        import threading
        import time as time_module

        stop_event = threading.Event()
        stop_event.set()
        grabber = _make_grabber(stop_event=stop_event)
        started = time_module.monotonic()
        self.assertTrue(grabber._sleep(600))
        self.assertLess(time_module.monotonic() - started, 1.0)

    def test_get_public_ip_cancelled(self):
        import threading

        from oci.grabber import GrabCancelledError

        stop_event = threading.Event()
        stop_event.set()
        cli = _mock_launch_cli()
        grabber = _make_grabber(cli=cli, stop_event=stop_event)
        with self.assertRaises(GrabCancelledError):
            grabber.get_public_ip(FAKE_INSTANCE_A)


class TestLaunchOptions(unittest.TestCase):
    """launch 命令的可选参数构造（阶段 3b：shape-config 等接入）。"""

    def test_a1_flex_shape_config(self):
        # A1.Flex + ocpus=4/memory_gb=24: launch includes --shape-config, and the parsed JSON has ocpus=4, memoryInGBs=24 (note the memoryInGBs casing). A1.Flex + ocpus=4/memory_gb=24 时 launch 含 --shape-config，JSON 解析后 ocpus=4、memoryInGBs=24（注意 memoryInGBs 的大小写）。
        _, args = _launch_once_with(shape="VM.Standard.A1.Flex",
                                    ocpus=4, memory_gb=24)
        self.assertIn("--shape-config", args)
        value = args[args.index("--shape-config") + 1]
        config = json.loads(value)
        self.assertEqual(config, {"ocpus": 4, "memoryInGBs": 24})

    def test_e2_never_gets_shape_config(self):
        _, args = _launch_once_with(shape="VM.Standard.E2.1.Micro",
                                    ocpus=4, memory_gb=24)
        self.assertNotIn("--shape-config", args)

    def test_flex_shape_config_requires_both_values(self):
        _, args = _launch_once_with(shape="VM.Standard.A1.Flex",
                                    ocpus=4, memory_gb=None)
        self.assertNotIn("--shape-config", args)

    def test_boot_volume_size(self):
        _, args = _launch_once_with(boot_volume_size_gbs=100)
        self.assertIn("--boot-volume-size-in-gbs", args)
        self.assertEqual(args[args.index("--boot-volume-size-in-gbs") + 1],
                         "100")

    def test_boot_volume_size_default_absent(self):
        _, args = _launch_once_with()
        self.assertNotIn("--boot-volume-size-in-gbs", args)

    def test_assign_public_ip_false(self):
        _, args = _launch_once_with(assign_public_ip=False)
        self.assertEqual(args[args.index("--assign-public-ip") + 1], "false")

    def test_assign_public_ip_default_true(self):
        _, args = _launch_once_with()
        self.assertEqual(args[args.index("--assign-public-ip") + 1], "true")

    def test_assign_ipv6_true(self):
        # Note: OCI CLI 3.96.3 has no --assign-ipv6 flag; the actual flag for IPv6 assignment is --assign-ipv6-ip (verified with `oci compute instance launch --help`), so the implementation passes --assign-ipv6-ip. 注意：OCI CLI 3.96.3 没有 --assign-ipv6 参数，IPv6 分配的实际参数名是 --assign-ipv6-ip（`oci compute instance launch --help` 实测），所以实现传的是 --assign-ipv6-ip。
        _, args = _launch_once_with(assign_ipv6=True)
        self.assertIn("--assign-ipv6-ip", args)
        self.assertEqual(args[args.index("--assign-ipv6-ip") + 1], "true")
        self.assertNotIn("--assign-ipv6", args)

    def test_assign_ipv6_default_false(self):
        _, args = _launch_once_with()
        self.assertNotIn("--assign-ipv6-ip", args)

    def test_user_data_file_content(self):
        script = "#!/bin/bash\necho hello\n"
        grabber, args = _launch_once_with(user_data=script)
        try:
            self.assertIn("--user-data-file", args)
            path = args[args.index("--user-data-file") + 1]
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read(), script)
        finally:
            grabber.cleanup()

    def test_user_data_empty_absent(self):
        for empty in ("", None):
            _, args = _launch_once_with(user_data=empty)
            self.assertNotIn("--user-data-file", args)

    def test_no_ssh_keys_file_omits_parameter(self):
        _, args = _launch_once_with(ssh_keys_file=None)
        self.assertNotIn("--ssh-authorized-keys-file", args)

    def test_empty_ssh_keys_file_omits_parameter(self):
        _, args = _launch_once_with(ssh_keys_file="")
        self.assertNotIn("--ssh-authorized-keys-file", args)

    def test_ssh_keys_file_present_by_default(self):
        _, args = _launch_once_with()
        self.assertIn("--ssh-authorized-keys-file", args)
        self.assertEqual(
            args[args.index("--ssh-authorized-keys-file") + 1],
            "/tmp/fake_keys.pub")

    @patch("oci.grabber.time.sleep")
    def test_run_cleans_up_user_data_file(self, mock_sleep):
        cli = _mock_launch_cli(launch_results=[FAKE_INSTANCE_A])
        grabber = _make_grabber(cli=cli, user_data="#!/bin/bash\necho hi\n")
        path = grabber._ensure_user_data_file()
        self.assertTrue(os.path.exists(path))
        grabber.run()
        self.assertFalse(os.path.exists(path))
        self.assertIsNone(grabber._user_data_file)


class TestDelayRange(unittest.TestCase):
    """min_delay/max_delay 到 interval/jitter 的映射。"""

    def test_custom_delay_range(self):
        grabber = _make_grabber(min_delay=20, max_delay=50)
        self.assertEqual(grabber._interval, 35)
        self.assertEqual(grabber._jitter, 15)
        for _ in range(200):
            wait = grabber._next_interval()
            self.assertGreaterEqual(wait, 20)
            self.assertLessEqual(wait, 50)

    def test_default_delay_unchanged(self):
        grabber = _make_grabber()
        self.assertEqual(grabber._interval, CREATE_INTERVAL)
        self.assertEqual(grabber._jitter, CREATE_JITTER)

    def test_partial_delay_falls_back_to_default(self):
        for kwargs in ({"min_delay": 20}, {"max_delay": 50},
                       {"min_delay": 20, "max_delay": 0}):
            grabber = _make_grabber(**kwargs)
            self.assertEqual(grabber._interval, CREATE_INTERVAL)
            self.assertEqual(grabber._jitter, CREATE_JITTER)

    def test_min_greater_than_max_raises(self):
        with self.assertRaises(ValueError):
            _make_grabber(min_delay=50, max_delay=20)


if __name__ == "__main__":
    unittest.main()
