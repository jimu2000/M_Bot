#!/usr/bin/env python3
"""instance.py 的单元测试（mock OCIClient，不碰真实 API）。

运行：python3 -m unittest tests.test_instance -v
"""

import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oci.client import NotFoundError
from oci.instance import (find_instance_by_name, get_image_arch,
                          get_instance, get_instance_state,
                          image_arch_from_name, launch_instance,
                          list_instances, restart_instance, start_instance,
                          stop_instance, terminate_instance)

# Placeholders (no real OCID in tests). 占位符（测试里不出现真实 OCID）。
FAKE_COMPARTMENT = "ocid1.tenancy.oc1..xxxxxxxx"
FAKE_INSTANCE = "ocid1.instance.oc1..xxxxxxxx"
FAKE_IMAGE = "ocid1.image.oc1..xxxxxxxx"
FAKE_SUBNET = "ocid1.subnet.oc1..xxxxxxxx"

FAKE_LIST_RESPONSE = {
    "data": [
        {
            "id": "ocid1.instance.oc1..aaaa",
            "display-name": "instance-a1",
            "shape": "VM.Standard.A1.Flex",
            "lifecycle-state": "RUNNING",
            "time-created": "2026-11-11T10:00:00.000Z",
            "region": "ap-singapore-1",
        },
        {
            "id": "ocid1.instance.oc1..bbbb",
            "display-name": "instance-e2",
            "shape": "VM.Standard.E2.1.Micro",
            "lifecycle-state": "STOPPED",
            "time-created": "2026-11-12T10:00:00.000Z",
            "region": "ap-singapore-1",
        },
    ]
}

FAKE_GET_RESPONSE = {
    "data": {
        "id": FAKE_INSTANCE,
        "display-name": "instance-a1",
        "shape": "VM.Standard.A1.Flex",
        "lifecycle-state": "RUNNING",
        "time-created": "2026-11-11T10:00:00.000Z",
    }
}


def _mock_cli(return_value=None):
    """造一个 mock CLI。

    cli.run 返回原值（写操作仍走 CLI，返回值包 {"data": ...}）；
    SDK 读方法（sdk_list_instances / sdk_get_instance / sdk_get_image）
    返回解包后的数据（不包 {"data": ...}），与真实 SDK 方法一致。
    """
    cli = MagicMock()
    cli.run.return_value = return_value
    unwrapped = (return_value.get("data", return_value)
                 if isinstance(return_value, dict) else return_value)
    if isinstance(unwrapped, list):
        cli.sdk_list_instances.return_value = unwrapped
    elif isinstance(unwrapped, dict):
        cli.sdk_get_instance.return_value = unwrapped
        cli.sdk_get_image.return_value = unwrapped
    return cli


class TestListInstances(unittest.TestCase):
    def test_returns_simplified_list(self):
        cli = _mock_cli(FAKE_LIST_RESPONSE)
        result = list_instances(cli, FAKE_COMPARTMENT)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["display-name"], "instance-a1")
        self.assertEqual(result[0]["shape"], "VM.Standard.A1.Flex")
        self.assertEqual(result[0]["lifecycle-state"], "RUNNING")
        self.assertEqual(result[1]["display-name"], "instance-e2")
        # The slim list keeps only 5 fields; extra fields are dropped. 精简列表只保留 5 个字段，多余字段被去掉。
        self.assertNotIn("region", result[0])
        self.assertEqual(set(result[0]),
                         {"id", "display-name", "shape",
                          "lifecycle-state", "time-created"})

    def test_command_includes_compartment(self):
        cli = _mock_cli({"data": []})
        list_instances(cli, FAKE_COMPARTMENT)
        cli.sdk_list_instances.assert_called_once_with(FAKE_COMPARTMENT)
        cli.run.assert_not_called()

    def test_empty_list(self):
        cli = _mock_cli({"data": []})
        self.assertEqual(list_instances(cli, FAKE_COMPARTMENT), [])


class TestGetInstance(unittest.TestCase):
    def test_returns_full_json(self):
        cli = _mock_cli(FAKE_GET_RESPONSE)
        result = get_instance(cli, FAKE_INSTANCE)
        # SDK 直调返回解包后的字典（不包 {"data": ...}）。
        self.assertEqual(result, FAKE_GET_RESPONSE["data"])
        cli.sdk_get_instance.assert_called_once_with(FAKE_INSTANCE)
        cli.run.assert_not_called()

    def test_not_found_propagates(self):
        cli = MagicMock()
        cli.sdk_get_instance.side_effect = NotFoundError(
            "NotAuthorizedOrNotFound")
        with self.assertRaises(NotFoundError):
            get_instance(cli, FAKE_INSTANCE)


class TestGetInstanceState(unittest.TestCase):
    def test_returns_state_string(self):
        cli = _mock_cli(FAKE_GET_RESPONSE)
        self.assertEqual(get_instance_state(cli, FAKE_INSTANCE), "RUNNING")

    def test_unwrapped_response(self):
        # Tolerate a response without the {"data": ...} wrapper. 容忍不带 {"data": ...} 包装的响应。
        cli = _mock_cli({"lifecycle-state": "STOPPED"})
        self.assertEqual(get_instance_state(cli, FAKE_INSTANCE), "STOPPED")

    def test_not_found_propagates(self):
        cli = MagicMock()
        cli.sdk_get_instance.side_effect = NotFoundError(
            "NotAuthorizedOrNotFound")
        with self.assertRaises(NotFoundError):
            get_instance_state(cli, FAKE_INSTANCE)


class TestLaunchInstance(unittest.TestCase):
    def _launch(self, cli, **kwargs):
        defaults = dict(compartment_id=FAKE_COMPARTMENT,
                        availability_domain="AD-1",
                        shape="VM.Standard.E2.1.Micro",
                        image_id=FAKE_IMAGE,
                        subnet_id=FAKE_SUBNET,
                        display_name="test-instance",
                        ssh_keys_file="/root/.ssh/id_ed25519.pub")
        defaults.update(kwargs)
        return launch_instance(cli, **defaults)

    def test_returns_instance_ocid(self):
        cli = _mock_cli({"data": {"id": "ocid1.instance.oc1..newid"}})
        ocid = self._launch(cli)
        self.assertEqual(ocid, "ocid1.instance.oc1..newid")

    def test_includes_no_retry_by_default(self):
        cli = _mock_cli({"data": {"id": "ocid1.instance.oc1..newid"}})
        self._launch(cli)
        args = cli.run.call_args[0][0]
        self.assertIn("--no-retry", args)

    def test_no_retry_can_be_disabled(self):
        cli = _mock_cli({"data": {"id": "ocid1.instance.oc1..newid"}})
        self._launch(cli, no_retry=False)
        args = cli.run.call_args[0][0]
        self.assertNotIn("--no-retry", args)

    def test_assign_public_ip_default_true(self):
        cli = _mock_cli({"data": {"id": "ocid1.instance.oc1..newid"}})
        self._launch(cli)
        args = cli.run.call_args[0][0]
        i = args.index("--assign-public-ip")
        self.assertEqual(args[i + 1], "true")

    def test_assign_public_ip_false(self):
        cli = _mock_cli({"data": {"id": "ocid1.instance.oc1..newid"}})
        self._launch(cli, assign_public_ip=False)
        args = cli.run.call_args[0][0]
        i = args.index("--assign-public-ip")
        self.assertEqual(args[i + 1], "false")

    def test_command_includes_all_params(self):
        cli = _mock_cli({"data": {"id": "ocid1.instance.oc1..newid"}})
        self._launch(cli)
        args = cli.run.call_args[0][0]
        self.assertEqual(args[:3], ["compute", "instance", "launch"])
        for flag, value in [
                ("--compartment-id", FAKE_COMPARTMENT),
                ("--availability-domain", "AD-1"),
                ("--shape", "VM.Standard.E2.1.Micro"),
                ("--image-id", FAKE_IMAGE),
                ("--subnet-id", FAKE_SUBNET),
                ("--display-name", "test-instance"),
                ("--ssh-authorized-keys-file", "/root/.ssh/id_ed25519.pub")]:
            self.assertIn(flag, args)
            self.assertEqual(args[args.index(flag) + 1], value)


class TestInstanceActions(unittest.TestCase):
    def test_start(self):
        cli = _mock_cli({})
        start_instance(cli, FAKE_INSTANCE)
        args = cli.run.call_args[0][0]
        self.assertEqual(args[:3], ["compute", "instance", "action"])
        self.assertIn("--action", args)
        self.assertEqual(args[args.index("--action") + 1], "START")
        self.assertIn(FAKE_INSTANCE, args)

    def test_stop(self):
        cli = _mock_cli({})
        stop_instance(cli, FAKE_INSTANCE)
        args = cli.run.call_args[0][0]
        self.assertEqual(args[args.index("--action") + 1], "STOP")

    def test_restart_uses_reset(self):
        cli = _mock_cli({})
        restart_instance(cli, FAKE_INSTANCE)
        args = cli.run.call_args[0][0]
        self.assertEqual(args[args.index("--action") + 1], "RESET")


class TestTerminateInstance(unittest.TestCase):
    def test_default_deletes_boot_volume(self):
        """硬规则：默认必须永久删除引导卷。"""
        cli = _mock_cli({})
        terminate_instance(cli, FAKE_INSTANCE)
        args = cli.run.call_args[0][0]
        self.assertEqual(args[:3], ["compute", "instance", "terminate"])
        self.assertIn("--preserve-boot-volume", args)
        i = args.index("--preserve-boot-volume")
        self.assertEqual(args[i + 1], "false")

    def test_preserve_boot_volume_true(self):
        cli = _mock_cli({})
        terminate_instance(cli, FAKE_INSTANCE, preserve_boot_volume=True)
        args = cli.run.call_args[0][0]
        i = args.index("--preserve-boot-volume")
        self.assertEqual(args[i + 1], "true")

    def test_includes_force_flag(self):
        # A non-interactive call must skip the confirmation prompt. 非交互调用必须跳过确认提示。
        cli = _mock_cli({})
        terminate_instance(cli, FAKE_INSTANCE)
        args = cli.run.call_args[0][0]
        self.assertIn("--force", args)


class TestFindInstanceByName(unittest.TestCase):
    def test_found(self):
        cli = _mock_cli(FAKE_LIST_RESPONSE)
        found = find_instance_by_name(cli, FAKE_COMPARTMENT, "instance-e2")
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], "ocid1.instance.oc1..bbbb")
        self.assertEqual(found["shape"], "VM.Standard.E2.1.Micro")

    def test_not_found_returns_none(self):
        cli = _mock_cli(FAKE_LIST_RESPONSE)
        self.assertIsNone(
            find_instance_by_name(cli, FAKE_COMPARTMENT, "no-such-name"))


class TestImageArch(unittest.TestCase):
    """镜像架构推断（Bug 6：建抢机前校验镜像架构与形状）。"""

    def test_aarch64_from_name(self):
        self.assertEqual(
            image_arch_from_name("Canonical-Ubuntu-24.04-aarch64-2026.11.05-0"),
            "aarch64")

    def test_x86_64_from_name(self):
        self.assertEqual(
            image_arch_from_name("Oracle-Linux-9.5-x86_64-2026.10.01-0"),
            "x86_64")
        self.assertEqual(image_arch_from_name("CentOS-8-amd64"), "x86_64")

    def test_no_marker_returns_none(self):
        self.assertIsNone(image_arch_from_name("my-custom-image"))
        self.assertIsNone(image_arch_from_name(None))
        self.assertIsNone(image_arch_from_name(123))

    def test_get_image_arch_queries_image_get(self):
        cli = MagicMock()
        cli.sdk_get_image.return_value = {
            "display-name": "Canonical-Ubuntu-24.04-aarch64-2026.11.05-0"}
        self.assertEqual(get_image_arch(cli, FAKE_IMAGE), "aarch64")
        cli.sdk_get_image.assert_called_once_with(FAKE_IMAGE)
        cli.run.assert_not_called()

    def test_get_image_arch_failure_returns_none(self):
        cli = MagicMock()
        cli.sdk_get_image.side_effect = Exception("NotAuthorizedOrNotFound")
        self.assertIsNone(get_image_arch(cli, FAKE_IMAGE))


if __name__ == "__main__":
    unittest.main()
