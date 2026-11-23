#!/usr/bin/env python3
"""阶段 5 工作包 A：instance.py 新增函数的单元测试。

覆盖：批量启停/重启/终止、改名、重装系统、串行控制台。
全部 mock OCIClient.run，不碰真实 API。

运行：python3 -m unittest tests.test_phase5_instance -v
"""

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oci.client import OCIError
from oci.instance import (
    MAX_DISPLAY_NAME_LEN,
    batch_restart,
    batch_start,
    batch_stop,
    batch_terminate,
    get_serial_console,
    reinstall_instance,
    rename_instance,
)

# 占位符（测试里不出现真实 OCID）
FAKE_INSTANCE_1 = "ocid1.instance.oc1..aaaa"
FAKE_INSTANCE_2 = "ocid1.instance.oc1..bbbb"
FAKE_IMAGE = "ocid1.image.oc1..xxxxxxxx"
FAKE_CONNECTION = "ocid1.instanceconsoleconnection.oc1..xxxxxxxx"


def _mock_cli(return_value=None):
    cli = MagicMock()
    cli.run.return_value = return_value
    return cli


class TestBatchOperations(unittest.TestCase):
    def test_batch_start_calls_start_per_instance(self):
        cli = _mock_cli({})
        result = batch_start(cli, [FAKE_INSTANCE_1, FAKE_INSTANCE_2])
        self.assertEqual(result, {"succeeded": [FAKE_INSTANCE_1,
                                                FAKE_INSTANCE_2],
                                  "failed": []})
        self.assertEqual(cli.run.call_count, 2)
        for call in cli.run.call_args_list:
            args = call[0][0]
            self.assertEqual(args[:3], ["compute", "instance", "action"])
            self.assertEqual(args[args.index("--action") + 1], "START")

    def test_batch_stop_uses_stop(self):
        cli = _mock_cli({})
        result = batch_stop(cli, [FAKE_INSTANCE_1])
        self.assertEqual(result["succeeded"], [FAKE_INSTANCE_1])
        args = cli.run.call_args[0][0]
        self.assertEqual(args[args.index("--action") + 1], "STOP")

    def test_batch_restart_uses_reset(self):
        cli = _mock_cli({})
        result = batch_restart(cli, [FAKE_INSTANCE_1])
        self.assertEqual(result["succeeded"], [FAKE_INSTANCE_1])
        args = cli.run.call_args[0][0]
        self.assertEqual(args[args.index("--action") + 1], "RESET")

    def test_batch_terminate_deletes_boot_volume_by_default(self):
        """硬规则：批量删除默认永久删除各实例的引导卷。"""
        cli = _mock_cli({})
        result = batch_terminate(cli, [FAKE_INSTANCE_1, FAKE_INSTANCE_2])
        self.assertEqual(result["succeeded"],
                         [FAKE_INSTANCE_1, FAKE_INSTANCE_2])
        self.assertEqual(cli.run.call_count, 2)
        for call in cli.run.call_args_list:
            args = call[0][0]
            self.assertEqual(args[:3], ["compute", "instance", "terminate"])
            i = args.index("--preserve-boot-volume")
            self.assertEqual(args[i + 1], "false")
            self.assertIn("--force", args)

    def test_partial_failure_continues(self):
        """单台失败记下来，继续处理其他台。"""
        cli = MagicMock()
        cli.run.side_effect = [None, OCIError("boom")]
        result = batch_start(cli, [FAKE_INSTANCE_1, FAKE_INSTANCE_2])
        self.assertEqual(result["succeeded"], [FAKE_INSTANCE_1])
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(result["failed"][0]["id"], FAKE_INSTANCE_2)
        self.assertIn("boom", result["failed"][0]["error"])

    def test_all_fail(self):
        cli = MagicMock()
        cli.run.side_effect = OCIError("nope")
        result = batch_stop(cli, [FAKE_INSTANCE_1, FAKE_INSTANCE_2])
        self.assertEqual(result["succeeded"], [])
        self.assertEqual(len(result["failed"]), 2)

    def test_empty_list_calls_nothing(self):
        cli = _mock_cli({})
        result = batch_restart(cli, [])
        self.assertEqual(result, {"succeeded": [], "failed": []})
        cli.run.assert_not_called()


class TestRenameInstance(unittest.TestCase):
    def test_command_args_include_force(self):
        cli = _mock_cli({"data": {"id": FAKE_INSTANCE_1,
                                  "display-name": "new-name"}})
        result = rename_instance(cli, FAKE_INSTANCE_1, "new-name")
        args = cli.run.call_args[0][0]
        self.assertEqual(args[:3], ["compute", "instance", "update"])
        self.assertIn("--instance-id", args)
        self.assertEqual(args[args.index("--instance-id") + 1],
                         FAKE_INSTANCE_1)
        self.assertEqual(args[args.index("--display-name") + 1], "new-name")
        # --force 必加：不加会交互确认挂到超时
        self.assertIn("--force", args)
        self.assertEqual(result["display-name"], "new-name")

    def test_strips_whitespace(self):
        cli = _mock_cli({"data": {}})
        rename_instance(cli, FAKE_INSTANCE_1, "  padded-name  ")
        args = cli.run.call_args[0][0]
        self.assertEqual(args[args.index("--display-name") + 1],
                         "padded-name")

    def test_empty_name_raises(self):
        cli = _mock_cli({})
        for bad in ("", "   ", None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    rename_instance(cli, FAKE_INSTANCE_1, bad)
        cli.run.assert_not_called()

    def test_too_long_name_raises(self):
        cli = _mock_cli({})
        with self.assertRaises(ValueError):
            rename_instance(cli, FAKE_INSTANCE_1,
                            "x" * (MAX_DISPLAY_NAME_LEN + 1))
        cli.run.assert_not_called()

    def test_max_length_name_ok(self):
        cli = _mock_cli({"data": {}})
        rename_instance(cli, FAKE_INSTANCE_1,
                        "x" * MAX_DISPLAY_NAME_LEN)
        cli.run.assert_called_once()


class TestReinstallInstance(unittest.TestCase):
    def test_with_explicit_image(self):
        cli = _mock_cli({"data": {"id": FAKE_INSTANCE_1}})
        reinstall_instance(cli, FAKE_INSTANCE_1, image_id=FAKE_IMAGE)
        # 指定镜像时只调一次 update，不用先 get
        cli.run.assert_called_once()
        args = cli.run.call_args[0][0]
        self.assertEqual(args[:3], ["compute", "instance", "update"])
        self.assertIn("--source-details", args)
        details = json.loads(args[args.index("--source-details") + 1])
        self.assertEqual(details, {"sourceType": "image",
                                   "imageId": FAKE_IMAGE})
        self.assertIn("--force", args)

    def test_without_image_reuses_current(self):
        """不传 image_id 时先 get 查当前镜像，再用它重装。"""
        cli = MagicMock()

        def _run(args, **kwargs):
            if args[:3] == ["compute", "instance", "get"]:
                return {"data": {"id": FAKE_INSTANCE_1,
                                 "source-details": {"image-id": FAKE_IMAGE}}}
            return {"data": {"id": FAKE_INSTANCE_1}}

        cli.run.side_effect = _run
        reinstall_instance(cli, FAKE_INSTANCE_1)
        self.assertEqual(cli.run.call_count, 2)
        update_args = cli.run.call_args_list[1][0][0]
        details = json.loads(
            update_args[update_args.index("--source-details") + 1])
        self.assertEqual(details["imageId"], FAKE_IMAGE)

    def test_without_image_and_no_source_details_raises(self):
        """get 不到当前镜像时抛 ValueError，且不调 update。"""
        cli = MagicMock()
        cli.run.return_value = {"data": {"id": FAKE_INSTANCE_1}}
        with self.assertRaises(ValueError):
            reinstall_instance(cli, FAKE_INSTANCE_1)
        # 只调了 get，没调 update
        cli.run.assert_called_once()
        args = cli.run.call_args[0][0]
        self.assertEqual(args[:3], ["compute", "instance", "get"])

    def test_returns_updated_detail(self):
        cli = _mock_cli({"data": {"id": FAKE_INSTANCE_1,
                                  "lifecycle-state": "UPDATING"}})
        result = reinstall_instance(cli, FAKE_INSTANCE_1,
                                    image_id=FAKE_IMAGE)
        self.assertEqual(result["lifecycle-state"], "UPDATING")


class TestGetSerialConsole(unittest.TestCase):
    @patch("oci.instance.os.path.isfile", return_value=True)
    def test_create_then_get(self, _mock_isfile):
        cli = MagicMock()

        def _run(args, **kwargs):
            if args[1] == "instance-console-connection" and args[2] == "create":
                return {"data": {
                    "id": FAKE_CONNECTION,
                    "connection-string":
                        "ssh -i key " + FAKE_CONNECTION + "@instance-console",
                    "fingerprint": "aa:bb:cc",
                    "lifecycle-state": "CREATING"}}
            if args[1] == "instance-console-connection" and args[2] == "get":
                return {"data": {"id": FAKE_CONNECTION,
                                 "lifecycle-state": "ACTIVE"}}
            raise AssertionError(f"unexpected command: {args}")

        cli.run.side_effect = _run
        result = get_serial_console(cli, FAKE_INSTANCE_1,
                                    ssh_public_key_file="/tmp/fake-key.pub")
        self.assertEqual(cli.run.call_count, 2)
        create_args = cli.run.call_args_list[0][0][0]
        self.assertEqual(create_args[:3],
                         ["compute", "instance-console-connection", "create"])
        self.assertIn(FAKE_INSTANCE_1, create_args)
        # OCI CLI 必填参数 --ssh-public-key-file（真实验证挖出的 bug）
        self.assertIn("--ssh-public-key-file", create_args)
        self.assertIn("/tmp/fake-key.pub", create_args)
        get_args = cli.run.call_args_list[1][0][0]
        self.assertEqual(get_args[:3],
                         ["compute", "instance-console-connection", "get"])
        self.assertIn(FAKE_CONNECTION, get_args)
        # create 的连接串保留，get 的最新状态覆盖旧状态
        self.assertIn("ssh -i key", result["connection-string"])
        self.assertEqual(result["fingerprint"], "aa:bb:cc")
        self.assertEqual(result["lifecycle-state"], "ACTIVE")
        self.assertEqual(result["id"], FAKE_CONNECTION)

    @patch("oci.instance.os.path.isfile", return_value=True)
    def test_create_without_id_skips_get(self, _mock_isfile):
        """create 未返回 id 时不调 get，字段为 None。"""
        cli = _mock_cli({"data": {}})
        result = get_serial_console(cli, FAKE_INSTANCE_1,
                                    ssh_public_key_file="/tmp/fake-key.pub")
        cli.run.assert_called_once()
        self.assertIsNone(result["id"])
        self.assertIsNone(result["connection-string"])
        self.assertIsNone(result["lifecycle-state"])

    @patch("oci.instance.os.path.isfile", return_value=False)
    def test_missing_key_file_raises(self, _mock_isfile):
        """公钥文件不存在时抛 ValueError，不调 OCI。"""
        cli = _mock_cli({"data": {}})
        with self.assertRaises(ValueError):
            get_serial_console(cli, FAKE_INSTANCE_1,
                               ssh_public_key_file="/tmp/no-such-key.pub")
        cli.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
