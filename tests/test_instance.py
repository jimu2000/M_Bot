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
from oci.instance import (find_instance_by_name, get_instance,
                          get_instance_state, launch_instance,
                          list_instances, restart_instance, start_instance,
                          stop_instance, terminate_instance)

# 占位符（测试里不出现真实 OCID）
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
            "region": "ap-singapore-1",  # 精简列表里不应出现
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
    cli = MagicMock()
    cli.run.return_value = return_value
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
        # 精简列表只保留 5 个字段，多余字段被去掉
        self.assertNotIn("region", result[0])
        self.assertEqual(set(result[0]),
                         {"id", "display-name", "shape",
                          "lifecycle-state", "time-created"})

    def test_command_includes_compartment(self):
        cli = _mock_cli({"data": []})
        list_instances(cli, FAKE_COMPARTMENT)
        args = cli.run.call_args[0][0]
        self.assertEqual(args[:3], ["compute", "instance", "list"])
        self.assertIn("--compartment-id", args)
        self.assertIn(FAKE_COMPARTMENT, args)

    def test_empty_list(self):
        cli = _mock_cli({"data": []})
        self.assertEqual(list_instances(cli, FAKE_COMPARTMENT), [])


class TestGetInstance(unittest.TestCase):
    def test_returns_full_json(self):
        cli = _mock_cli(FAKE_GET_RESPONSE)
        result = get_instance(cli, FAKE_INSTANCE)
        self.assertEqual(result, FAKE_GET_RESPONSE)
        args = cli.run.call_args[0][0]
        self.assertEqual(args[:3], ["compute", "instance", "get"])
        self.assertIn(FAKE_INSTANCE, args)

    def test_not_found_propagates(self):
        cli = MagicMock()
        cli.run.side_effect = NotFoundError("NotAuthorizedOrNotFound")
        with self.assertRaises(NotFoundError):
            get_instance(cli, FAKE_INSTANCE)


class TestGetInstanceState(unittest.TestCase):
    def test_returns_state_string(self):
        cli = _mock_cli(FAKE_GET_RESPONSE)
        self.assertEqual(get_instance_state(cli, FAKE_INSTANCE), "RUNNING")

    def test_unwrapped_response(self):
        # 容忍不带 {"data": ...} 包装的响应
        cli = _mock_cli({"lifecycle-state": "STOPPED"})
        self.assertEqual(get_instance_state(cli, FAKE_INSTANCE), "STOPPED")

    def test_not_found_propagates(self):
        cli = MagicMock()
        cli.run.side_effect = NotFoundError("NotAuthorizedOrNotFound")
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
        # 非交互调用必须跳过确认提示
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


if __name__ == "__main__":
    unittest.main()
