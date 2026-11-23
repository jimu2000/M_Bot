#!/usr/bin/env python3
"""network.py 的单元测试（mock subprocess，不碰真实 API）。

运行：python3 -m unittest tests.test_network -v

写操作（create / assign / delete / change）只做 mock 测试与命令构造
检查，绝不对真实 API 执行——换 IP 会断掉正在跑的服务。
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, call

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oci.client import NotFoundError, OCIClient
from oci.network import (assign_public_ip, change_public_ip,
                         create_reserved_ip, delete_public_ip, get_public_ip,
                         get_vnic, list_private_ips, list_public_ips,
                         list_vnics)

# 占位符（测试里不出现真实 OCID / IP）
FAKE_COMPARTMENT = "ocid1.tenancy.oc1..xxxxxxxx"
FAKE_INSTANCE = "ocid1.instance.oc1..xxxxxxxx"
FAKE_VNIC = "ocid1.vnic.oc1..xxxxxxxx"
FAKE_VNIC_2 = "ocid1.vnic.oc1..yyyyyyyy"
FAKE_PUBLIC_IP = "ocid1.publicip.oc1..xxxxxxxx"
FAKE_PRIVATE_IP = "ocid1.privateip.oc1..xxxxxxxx"
FAKE_IP = "203.0.113.10"  # TEST-NET-3，文档保留地址，非真实 IP


def _mock_cli():
    """返回一个 mock 的 OCIClient（只 mock run 方法）。"""
    return MagicMock(spec=OCIClient)


class TestListVnics(unittest.TestCase):
    def setUp(self):
        self.cli = _mock_cli()

    def test_returns_simplified_list(self):
        self.cli.run.return_value = {"data": [
            {"id": FAKE_VNIC, "display-name": "vnic-1",
             "public-ip": FAKE_IP, "private-ip": "10.0.0.5",
             "is-primary": True, "lifecycle-state": "AVAILABLE",
             "mac-address": "00:11:22:33:44:55"},
            {"id": FAKE_VNIC_2, "display-name": "vnic-2",
             "public-ip": None, "private-ip": "10.0.0.6",
             "is-primary": False},
        ]}
        result = list_vnics(self.cli, FAKE_INSTANCE)
        self.assertEqual(len(result), 2)
        # 只保留精简字段，多余字段（如 mac-address）被去掉
        self.assertEqual(result[0], {
            "id": FAKE_VNIC, "display-name": "vnic-1",
            "public-ip": FAKE_IP, "private-ip": "10.0.0.5",
            "is-primary": True})
        self.assertIsNone(result[1]["public-ip"])
        self.cli.run.assert_called_once_with(
            ["compute", "instance", "list-vnics",
             "--instance-id", FAKE_INSTANCE])

    def test_empty_instance_returns_empty_list(self):
        self.cli.run.return_value = {"data": []}
        self.assertEqual(list_vnics(self.cli, FAKE_INSTANCE), [])


class TestGetPublicIp(unittest.TestCase):
    def setUp(self):
        self.cli = _mock_cli()

    def test_returns_primary_vnic_public_ip(self):
        self.cli.run.return_value = {"data": [
            {"id": FAKE_VNIC_2, "public-ip": "203.0.113.99",
             "is-primary": False},
            {"id": FAKE_VNIC, "public-ip": FAKE_IP, "is-primary": True},
        ]}
        self.assertEqual(get_public_ip(self.cli, FAKE_INSTANCE), FAKE_IP)

    def test_no_public_ip_returns_none(self):
        self.cli.run.return_value = {"data": [
            {"id": FAKE_VNIC, "public-ip": None, "is-primary": True},
        ]}
        self.assertIsNone(get_public_ip(self.cli, FAKE_INSTANCE))

    def test_no_vnics_returns_none(self):
        self.cli.run.return_value = {"data": []}
        self.assertIsNone(get_public_ip(self.cli, FAKE_INSTANCE))


class TestGetVnic(unittest.TestCase):
    def setUp(self):
        self.cli = _mock_cli()

    def test_returns_vnic_detail(self):
        detail = {"id": FAKE_VNIC, "display-name": "vnic-1",
                  "public-ip": FAKE_IP, "private-ip": "10.0.0.5",
                  "is-primary": True, "lifecycle-state": "AVAILABLE"}
        self.cli.run.return_value = {"data": detail}
        self.assertEqual(get_vnic(self.cli, FAKE_VNIC), detail)
        self.cli.run.assert_called_once_with(
            ["network", "vnic", "get", "--vnic-id", FAKE_VNIC])

    def test_not_found_raises(self):
        self.cli.run.side_effect = NotFoundError("NotAuthorizedOrNotFound")
        with self.assertRaises(NotFoundError):
            get_vnic(self.cli, "ocid1.vnic.oc1..doesnotexist")


class TestListPublicIps(unittest.TestCase):
    def setUp(self):
        self.cli = _mock_cli()

    def test_returns_simplified_list(self):
        self.cli.run.return_value = {"data": [
            {"id": FAKE_PUBLIC_IP, "ip-address": FAKE_IP,
             "lifetime": "RESERVED", "lifecycle-state": "ASSIGNED",
             "assigned-entity-id": FAKE_PRIVATE_IP,
             "display-name": "my-ip", "scope": "REGION"},
            {"id": "ocid1.publicip.oc1..yyyyyyyy", "ip-address": "203.0.113.20",
             "lifetime": "EPHEMERAL", "lifecycle-state": "ASSIGNED",
             "assigned-entity-id": None},
        ]}
        result = list_public_ips(self.cli, FAKE_COMPARTMENT)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0], {
            "id": FAKE_PUBLIC_IP, "ip-address": FAKE_IP,
            "lifetime": "RESERVED", "lifecycle-state": "ASSIGNED",
            "assigned-entity": FAKE_PRIVATE_IP})
        self.assertEqual(result[1]["lifetime"], "EPHEMERAL")
        self.cli.run.assert_called_once_with(
            ["network", "public-ip", "list",
             "--compartment-id", FAKE_COMPARTMENT,
             "--scope", "REGION"])

    def test_empty_returns_empty_list(self):
        self.cli.run.return_value = {"data": []}
        self.assertEqual(list_public_ips(self.cli, FAKE_COMPARTMENT), [])


class TestCreateReservedIp(unittest.TestCase):
    def setUp(self):
        self.cli = _mock_cli()

    def test_creates_reserved_ip_and_returns_id_and_address(self):
        self.cli.run.return_value = {
            "data": {"id": FAKE_PUBLIC_IP, "ip-address": FAKE_IP,
                     "lifetime": "RESERVED", "lifecycle-state": "ASSIGNING"}}
        ip_id, address = create_reserved_ip(self.cli, FAKE_COMPARTMENT,
                                            "my-reserved-ip")
        self.assertEqual(ip_id, FAKE_PUBLIC_IP)
        self.assertEqual(address, FAKE_IP)
        self.cli.run.assert_called_once_with(
            ["network", "public-ip", "create",
             "--compartment-id", FAKE_COMPARTMENT,
             "--lifetime", "RESERVED",
             "--display-name", "my-reserved-ip"])


class TestAssignPublicIp(unittest.TestCase):
    def setUp(self):
        self.cli = _mock_cli()

    def test_assigns_to_private_ip(self):
        updated = {"id": FAKE_PUBLIC_IP, "ip-address": FAKE_IP,
                   "lifecycle-state": "ASSIGNED",
                   "assigned-entity-id": FAKE_PRIVATE_IP}
        self.cli.run.return_value = {"data": updated}
        result = assign_public_ip(self.cli, FAKE_PUBLIC_IP, FAKE_PRIVATE_IP)
        self.assertEqual(result, updated)
        self.cli.run.assert_called_once_with(
            ["network", "public-ip", "update",
             "--public-ip-id", FAKE_PUBLIC_IP,
             "--private-ip-id", FAKE_PRIVATE_IP])


class TestDeletePublicIp(unittest.TestCase):
    def setUp(self):
        self.cli = _mock_cli()

    def test_delete_uses_force_flag(self):
        self.cli.run.return_value = None  # delete 成功时无输出
        self.assertIsNone(delete_public_ip(self.cli, FAKE_PUBLIC_IP))
        self.cli.run.assert_called_once_with(
            ["network", "public-ip", "delete",
             "--public-ip-id", FAKE_PUBLIC_IP, "--force"])


class TestListPrivateIps(unittest.TestCase):
    def setUp(self):
        self.cli = _mock_cli()

    def test_returns_simplified_list(self):
        self.cli.run.return_value = {"data": [
            {"id": FAKE_PRIVATE_IP, "ip-address": "10.0.0.5",
             "is-primary": True, "display-name": "primary",
             "hostname-label": "host1"},
            {"id": "ocid1.privateip.oc1..yyyyyyyy", "ip-address": "10.0.0.6",
             "is-primary": False, "display-name": None},
        ]}
        result = list_private_ips(self.cli, FAKE_VNIC)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0], {
            "id": FAKE_PRIVATE_IP, "ip-address": "10.0.0.5",
            "is-primary": True, "display-name": "primary"})
        self.cli.run.assert_called_once_with(
            ["network", "private-ip", "list", "--vnic-id", FAKE_VNIC])


class TestChangePublicIp(unittest.TestCase):
    def setUp(self):
        self.cli = _mock_cli()

    def _full_flow_responses(self, old_ip_present=True):
        """换 IP 全流程 cli.run 调用的返回值（按顺序）。

        新流程（2026-11-23 真实验证后修正）：先删旧临时 IP 再绑新 IP，
        因为 OCI 拒绝给已绑公网 IP 的内网 IP 再绑一个（409 Conflict）。
        """
        responses = [
            # 1. list-vnics：主 VNIC
            {"data": [{"id": FAKE_VNIC, "display-name": "vnic-1",
                       "public-ip": "203.0.113.1", "private-ip": "10.0.0.5",
                       "is-primary": True}]},
            # 2. list private-ip：主内网 IP
            {"data": [{"id": FAKE_PRIVATE_IP, "ip-address": "10.0.0.5",
                       "is-primary": True, "display-name": "primary"}]},
            # 3. instance get：取 AD
            {"data": {"id": FAKE_INSTANCE,
                      "availability-domain": "AD-1"}},
            # 4. list public-ip（AD 作用域）：已绑的旧临时 IP
            {"data": [{"id": "ocid1.publicip.oc1..oldip",
                       "ip-address": "203.0.113.1", "lifetime": "EPHEMERAL",
                       "lifecycle-state": "ASSIGNED",
                       "assigned-entity-id": FAKE_PRIVATE_IP}]
             if old_ip_present else []},
        ]
        if old_ip_present:
            # 5. delete 旧临时 IP
            responses.append(None)
        responses += [
            # 6. create 保留 IP：新 IP
            {"data": {"id": FAKE_PUBLIC_IP, "ip-address": FAKE_IP,
                      "lifetime": "RESERVED"}},
            # 7. update 绑定
            {"data": {"id": FAKE_PUBLIC_IP, "ip-address": FAKE_IP,
                      "lifecycle-state": "ASSIGNED"}},
        ]
        return responses

    def test_full_flow_returns_new_ip_and_calls_in_order(self):
        self.cli.run.side_effect = self._full_flow_responses()
        result = change_public_ip(self.cli, FAKE_COMPARTMENT, FAKE_INSTANCE)
        self.assertEqual(result, FAKE_IP)
        # 断言调用顺序与参数（先删旧 IP 再绑新 IP）
        self.assertEqual(self.cli.run.call_args_list, [
            call(["compute", "instance", "list-vnics",
                  "--instance-id", FAKE_INSTANCE]),
            call(["network", "private-ip", "list", "--vnic-id", FAKE_VNIC]),
            call(["compute", "instance", "get",
                  "--instance-id", FAKE_INSTANCE]),
            call(["network", "public-ip", "list",
                  "--compartment-id", FAKE_COMPARTMENT,
                  "--scope", "AVAILABILITY_DOMAIN",
                  "--availability-domain", "AD-1"]),
            call(["network", "public-ip", "delete",
                  "--public-ip-id", "ocid1.publicip.oc1..oldip",
                  "--force"]),
            call(["network", "public-ip", "create",
                  "--compartment-id", FAKE_COMPARTMENT,
                  "--lifetime", "RESERVED",
                  "--display-name", "changed-ip"]),
            call(["network", "public-ip", "update",
                  "--public-ip-id", FAKE_PUBLIC_IP,
                  "--private-ip-id", FAKE_PRIVATE_IP]),
        ])

    def test_no_old_ip_skips_delete(self):
        """内网 IP 上没有已绑公网 IP 时，不调 delete，直接建新 IP。"""
        self.cli.run.side_effect = self._full_flow_responses(
            old_ip_present=False)
        result = change_public_ip(self.cli, FAKE_COMPARTMENT, FAKE_INSTANCE)
        self.assertEqual(result, FAKE_IP)
        delete_calls = [c for c in self.cli.run.call_args_list
                        if c[0][0][:3] == ["network", "public-ip", "delete"]]
        self.assertEqual(delete_calls, [])
        self.assertEqual(self.cli.run.call_count, 6)

    def test_custom_display_name(self):
        self.cli.run.side_effect = self._full_flow_responses()
        change_public_ip(self.cli, FAKE_COMPARTMENT, FAKE_INSTANCE,
                         display_name="my-new-ip")
        create_call = self.cli.run.call_args_list[5]
        self.assertIn("my-new-ip", create_call[0][0])

    def test_no_primary_vnic_raises_before_creating_ip(self):
        self.cli.run.return_value = {"data": [
            {"id": FAKE_VNIC, "is-primary": False}]}
        with self.assertRaises(ValueError):
            change_public_ip(self.cli, FAKE_COMPARTMENT, FAKE_INSTANCE)
        # 只查了 VNIC 就停下，没有创建任何 IP
        self.assertEqual(self.cli.run.call_count, 1)

    def test_no_primary_private_ip_raises_before_creating_ip(self):
        self.cli.run.side_effect = [
            {"data": [{"id": FAKE_VNIC, "is-primary": True}]},
            {"data": [{"id": FAKE_PRIVATE_IP, "is-primary": False}]},
        ]
        with self.assertRaises(ValueError):
            change_public_ip(self.cli, FAKE_COMPARTMENT, FAKE_INSTANCE)
        # 只查了 VNIC + 内网 IP 就停下，没有创建任何 IP
        self.assertEqual(self.cli.run.call_count, 2)


if __name__ == "__main__":
    unittest.main()
