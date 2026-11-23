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
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oci.client import (OCIClient, OutOfCapacityError, QuotaExceededError,
                        RateLimitError)
from oci.grabber import (BACKOFF_INITIAL, CREATE_INTERVAL, CREATE_JITTER,
                         Grabber)

# 占位符（测试里不出现真实 OCID）
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


def _launch_once_with(**kwargs):
    """用给定 Grabber 参数做一次 try_launch_once，返回 (grabber, launch 参数)。"""
    cli = MagicMock(spec=OCIClient)
    cli.run_text.side_effect = _run_text_dispatch(
        list_result="", launch_results=[FAKE_INSTANCE_A])
    grabber = _make_grabber(cli=cli, **kwargs)
    grabber.try_launch_once()
    return grabber, _launch_args(cli)


def _run_text_dispatch(list_result="", launch_results=None, get_result="TERMINATED"):
    """按命令类型分发 run_text 的 mock 返回值。

    list -> list_result（"" 表示无同名实例）；
    launch -> 按顺序取 launch_results 里的值（元素也可以是异常实例，会被抛出来）；
    get -> get_result（实例状态查询）。
    """
    launch_iter = iter(launch_results or [])

    def dispatch(args, **kwargs):
        if "list" in args:
            return list_result
        if "launch" in args:
            result = next(launch_iter)
            if isinstance(result, Exception):
                raise result
            return result
        if "get" in args:
            return get_result
        raise AssertionError(f"未预期的 run_text 调用：{args}")

    return dispatch


class TestTryLaunchOnce(unittest.TestCase):
    def test_success_returns_ocid(self):
        # ① try_launch_once 成功返回 OCID
        cli = MagicMock(spec=OCIClient)
        cli.run_text.side_effect = _run_text_dispatch(
            list_result="", launch_results=[FAKE_INSTANCE_A])
        grabber = _make_grabber(cli=cli)
        self.assertEqual(grabber.try_launch_once(), FAKE_INSTANCE_A)

    def test_launch_command_contains_no_retry(self):
        # 单次 try_launch_once 的命令构造检查：launch 必须带 --no-retry
        cli = MagicMock(spec=OCIClient)
        cli.run_text.side_effect = _run_text_dispatch(
            list_result="", launch_results=[FAKE_INSTANCE_A])
        grabber = _make_grabber(cli=cli)
        grabber.try_launch_once()
        launch_args = [c.args[0] for c in cli.run_text.call_args_list
                       if "launch" in c.args[0]]
        self.assertEqual(len(launch_args), 1)
        self.assertIn("--no-retry", launch_args[0])
        self.assertIn("--shape", launch_args[0])
        self.assertIn("VM.Standard.E2.1.Micro", launch_args[0])

    def test_existing_instance_reused_without_launch(self):
        # 防重复建：同名实例已存在时直接返回，不调 launch
        cli = MagicMock(spec=OCIClient)
        cli.run_text.side_effect = _run_text_dispatch(
            list_result=FAKE_INSTANCE_A)
        grabber = _make_grabber(cli=cli)
        self.assertEqual(grabber.try_launch_once(), FAKE_INSTANCE_A)
        launch_calls = [c for c in cli.run_text.call_args_list
                        if "launch" in c.args[0]]
        self.assertEqual(launch_calls, [])

    def test_out_of_capacity_returns_none(self):
        # ② 无容量返回 None（不抛异常，由 run() 决定重试）
        cli = MagicMock(spec=OCIClient)
        cli.run_text.side_effect = _run_text_dispatch(
            list_result="",
            launch_results=[OutOfCapacityError("Out of host capacity")])
        grabber = _make_grabber(cli=cli)
        self.assertIsNone(grabber.try_launch_once())

    def test_rate_limit_propagates(self):
        # 429 往上抛（由 run() 做指数退避）
        cli = MagicMock(spec=OCIClient)
        cli.run_text.side_effect = _run_text_dispatch(
            list_result="",
            launch_results=[RateLimitError("TooManyRequests")])
        grabber = _make_grabber(cli=cli)
        with self.assertRaises(RateLimitError):
            grabber.try_launch_once()

    def test_quota_propagates(self):
        # 配额不足往上抛（由 run() 停下）
        cli = MagicMock(spec=OCIClient)
        cli.run_text.side_effect = _run_text_dispatch(
            list_result="",
            launch_results=[QuotaExceededError("LimitExceeded")])
        grabber = _make_grabber(cli=cli)
        with self.assertRaises(QuotaExceededError):
            grabber.try_launch_once()


class TestRun(unittest.TestCase):
    @patch("oci.grabber.time.sleep")
    def test_rate_limit_backoff_doubles(self, mock_sleep):
        # ③ run 循环里 429 触发退避，等待时间翻倍（60 -> 120）
        cli = MagicMock(spec=OCIClient)
        cli.run_text.side_effect = _run_text_dispatch(
            list_result="",
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
        # ④ 配额不足时 run 抛 QuotaExceededError 且调了 on_event("quota")
        cli = MagicMock(spec=OCIClient)
        cli.run_text.side_effect = _run_text_dispatch(
            list_result="",
            launch_results=[QuotaExceededError("LimitExceeded")])
        on_event = MagicMock()
        grabber = _make_grabber(cli=cli, on_event=on_event)
        with self.assertRaises(QuotaExceededError):
            grabber.run()
        quota_calls = [c for c in on_event.call_args_list
                       if c.args and c.args[0] == "quota"]
        self.assertEqual(len(quota_calls), 1)
        # 配额不足直接停下，不应再重试（launch 只调了一次）
        launch_calls = [c for c in cli.run_text.call_args_list
                        if "launch" in c.args[0]]
        self.assertEqual(len(launch_calls), 1)

    @patch("oci.grabber.time.sleep")
    def test_wrong_cpu_terminates_and_continues(self, mock_sleep):
        # ⑤ CPU 不对时终止实例并继续抢，最终返回第二个实例
        cli = MagicMock(spec=OCIClient)
        cli.run_text.side_effect = _run_text_dispatch(
            list_result="",
            launch_results=[FAKE_INSTANCE_A, FAKE_INSTANCE_B],
            get_result="TERMINATED")
        cli.run.return_value = None  # terminate 不返回 JSON
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
        # terminate 只调了一次（第一台），且带永久删引导卷参数
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
    def test_no_capacity_retries_then_succeeds(self, mock_sleep):
        # 无容量时按 interval 等待后重试，抢到后返回统计
        cli = MagicMock(spec=OCIClient)
        cli.run_text.side_effect = _run_text_dispatch(
            list_result="",
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
        # 达到 max_attempts 仍没抢到时抛 RuntimeError
        cli = MagicMock(spec=OCIClient)
        cli.run_text.side_effect = _run_text_dispatch(
            list_result="",
            launch_results=[OutOfCapacityError("Out of host capacity")] * 5)
        grabber = _make_grabber(cli=cli)
        with self.assertRaises(RuntimeError):
            grabber.run(max_attempts=3)
        launch_calls = [c for c in cli.run_text.call_args_list
                        if "launch" in c.args[0]]
        self.assertEqual(len(launch_calls), 3)


class TestJitter(unittest.TestCase):
    def test_interval_within_range(self):
        # ⑥ 抖动：interval 计算在 [20, 30] 范围内
        grabber = _make_grabber()
        for _ in range(200):
            wait = grabber._next_interval()
            self.assertGreaterEqual(wait, CREATE_INTERVAL - CREATE_JITTER)
            self.assertLessEqual(wait, CREATE_INTERVAL + CREATE_JITTER)


class TestCancellation(unittest.TestCase):
    def test_preset_stop_event_cancels_before_launch(self):
        # ⑦ stop_event 事先置位：run() 立即抛 GrabCancelledError，不发 launch
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
        # ⑧ 运行中置位 stop_event：第一次无容量后即取消，不再重试
        import threading

        from oci.grabber import GrabCancelledError

        stop_event = threading.Event()
        cli = MagicMock(spec=OCIClient)
        cli.run_text.side_effect = _run_text_dispatch(
            list_result="",
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
        # _sleep 用 stop_event.wait 实现，time.sleep 不应被调到
        mock_sleep.assert_not_called()

    def test_sleep_returns_immediately_when_stopped(self):
        # ⑨ _sleep 在 stop_event 置位时立即返回 True（不真等）
        import threading
        import time as time_module

        stop_event = threading.Event()
        stop_event.set()
        grabber = _make_grabber(stop_event=stop_event)
        started = time_module.monotonic()
        self.assertTrue(grabber._sleep(600))
        self.assertLess(time_module.monotonic() - started, 1.0)

    def test_get_public_ip_cancelled(self):
        # ⑩ 等公网 IP 时取消：抛 GrabCancelledError
        import threading

        from oci.grabber import GrabCancelledError

        stop_event = threading.Event()
        stop_event.set()
        cli = MagicMock(spec=OCIClient)
        cli.run_text.return_value = ""  # 还没分配 IP
        grabber = _make_grabber(cli=cli, stop_event=stop_event)
        with self.assertRaises(GrabCancelledError):
            grabber.get_public_ip(FAKE_INSTANCE_A)


class TestLaunchOptions(unittest.TestCase):
    """launch 命令的可选参数构造（阶段 3b：shape-config 等接入）。"""

    def test_a1_flex_shape_config(self):
        # A1.Flex + ocpus=4/memory_gb=24 时 launch 含 --shape-config，
        # JSON 解析后 ocpus=4、memoryInGBs=24（注意 memoryInGBs 的大小写）。
        _, args = _launch_once_with(shape="VM.Standard.A1.Flex",
                                    ocpus=4, memory_gb=24)
        self.assertIn("--shape-config", args)
        value = args[args.index("--shape-config") + 1]
        config = json.loads(value)
        self.assertEqual(config, {"ocpus": 4, "memoryInGBs": 24})

    def test_e2_never_gets_shape_config(self):
        # E2.1.Micro 是固定规格：即使传了 ocpus/memory 也不传 shape-config。
        _, args = _launch_once_with(shape="VM.Standard.E2.1.Micro",
                                    ocpus=4, memory_gb=24)
        self.assertNotIn("--shape-config", args)

    def test_flex_shape_config_requires_both_values(self):
        # Flex 形状只传 ocpus（缺 memory_gb）时不传 shape-config。
        _, args = _launch_once_with(shape="VM.Standard.A1.Flex",
                                    ocpus=4, memory_gb=None)
        self.assertNotIn("--shape-config", args)

    def test_boot_volume_size(self):
        # boot_volume_size_gbs=100 时含 --boot-volume-size-in-gbs 100。
        _, args = _launch_once_with(boot_volume_size_gbs=100)
        self.assertIn("--boot-volume-size-in-gbs", args)
        self.assertEqual(args[args.index("--boot-volume-size-in-gbs") + 1],
                         "100")

    def test_boot_volume_size_default_absent(self):
        # 不传 boot_volume_size_gbs 时不加该参数（用 OCI 默认值）。
        _, args = _launch_once_with()
        self.assertNotIn("--boot-volume-size-in-gbs", args)

    def test_assign_public_ip_false(self):
        # assign_public_ip=False 时 --assign-public-ip false。
        _, args = _launch_once_with(assign_public_ip=False)
        self.assertEqual(args[args.index("--assign-public-ip") + 1], "false")

    def test_assign_public_ip_default_true(self):
        # 默认（不传）时 --assign-public-ip true，与旧行为一致。
        _, args = _launch_once_with()
        self.assertEqual(args[args.index("--assign-public-ip") + 1], "true")

    def test_assign_ipv6_true(self):
        # assign_ipv6=True 时含 --assign-ipv6-ip true。
        # 注意：OCI CLI 3.96.3 没有 --assign-ipv6 参数，IPv6 分配的实际
        # 参数名是 --assign-ipv6-ip（`oci compute instance launch --help`
        # 实测），所以实现传的是 --assign-ipv6-ip。
        _, args = _launch_once_with(assign_ipv6=True)
        self.assertIn("--assign-ipv6-ip", args)
        self.assertEqual(args[args.index("--assign-ipv6-ip") + 1], "true")
        self.assertNotIn("--assign-ipv6", args)

    def test_assign_ipv6_default_false(self):
        # 默认（不传）时不加 IPv6 参数。
        _, args = _launch_once_with()
        self.assertNotIn("--assign-ipv6-ip", args)

    def test_user_data_file_content(self):
        # user_data 非空时含 --user-data-file，且指向的文件内容就是脚本原文。
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
        # user_data 为空字符串 / None 时不传 --user-data-file。
        for empty in ("", None):
            _, args = _launch_once_with(user_data=empty)
            self.assertNotIn("--user-data-file", args)

    @patch("oci.grabber.time.sleep")
    def test_run_cleans_up_user_data_file(self, mock_sleep):
        # run() 结束（正常返回）后启动脚本临时文件被删除。
        cli = MagicMock(spec=OCIClient)
        cli.run_text.side_effect = _run_text_dispatch(
            list_result="", launch_results=[FAKE_INSTANCE_A])
        grabber = _make_grabber(cli=cli, user_data="#!/bin/bash\necho hi\n")
        path = grabber._ensure_user_data_file()
        self.assertTrue(os.path.exists(path))
        grabber.run()
        self.assertFalse(os.path.exists(path))
        self.assertIsNone(grabber._user_data_file)


class TestDelayRange(unittest.TestCase):
    """min_delay/max_delay 到 interval/jitter 的映射。"""

    def test_custom_delay_range(self):
        # Grabber(min_delay=20, max_delay=50) 的 _next_interval 多次采样
        # 都落在 [20, 50] 内（interval=35、jitter=15）。
        grabber = _make_grabber(min_delay=20, max_delay=50)
        self.assertEqual(grabber._interval, 35)
        self.assertEqual(grabber._jitter, 15)
        for _ in range(200):
            wait = grabber._next_interval()
            self.assertGreaterEqual(wait, 20)
            self.assertLessEqual(wait, 50)

    def test_default_delay_unchanged(self):
        # 不传 min/max 时仍为 25 +/- 5（模块常量默认值）。
        grabber = _make_grabber()
        self.assertEqual(grabber._interval, CREATE_INTERVAL)
        self.assertEqual(grabber._jitter, CREATE_JITTER)

    def test_partial_delay_falls_back_to_default(self):
        # 只传一个（另一个为 None/0）时回退到默认 25 +/- 5。
        for kwargs in ({"min_delay": 20}, {"max_delay": 50},
                       {"min_delay": 20, "max_delay": 0}):
            grabber = _make_grabber(**kwargs)
            self.assertEqual(grabber._interval, CREATE_INTERVAL)
            self.assertEqual(grabber._jitter, CREATE_JITTER)

    def test_min_greater_than_max_raises(self):
        # min_delay > max_delay 时构造即抛 ValueError。
        with self.assertRaises(ValueError):
            _make_grabber(min_delay=50, max_delay=20)


if __name__ == "__main__":
    unittest.main()
