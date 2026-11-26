#!/usr/bin/env python3
"""network.py 的单元测试（mock subprocess，不碰真实 API）。

运行：python3 -m unittest tests.test_network -v

写操作（create / assign / delete / change）只做 mock 测试与命令构造
检查，绝不对真实 API 执行——换 IP 会断掉正在跑的服务。
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, call, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oci.client import NotFoundError, OCIClient, OCIError
from oci.network import (assign_public_ip, attach_public_ips,
                         attach_public_ips_bulk, change_public_ip,
                         create_reserved_ip, delete_public_ip, get_public_ip,
                         get_vnic, list_private_ips, list_public_ips,
                         list_vnics, wait_for_public_ip)

# Placeholders (no real OCID / IP in tests). 占位符（测试里不出现真实 OCID / IP）。
FAKE_COMPARTMENT = "ocid1.tenancy.oc1..xxxxxxxx"
FAKE_INSTANCE = "ocid1.instance.oc1..xxxxxxxx"
FAKE_VNIC = "ocid1.vnic.oc1..xxxxxxxx"
FAKE_VNIC_2 = "ocid1.vnic.oc1..yyyyyyyy"
FAKE_PUBLIC_IP = "ocid1.publicip.oc1..xxxxxxxx"
FAKE_PRIVATE_IP = "ocid1.privateip.oc1..xxxxxxxx"
FAKE_IP = "203.0.113.10"  # TEST-NET-3, a documentation-reserved address, not a real IP. TEST-NET-3，文档保留地址，非真实 IP。


def _mock_cli():
    """返回一个 mock 的 OCIClient（只 mock run 方法）。"""
    return MagicMock(spec=OCIClient)


class TestListVnics(unittest.TestCase):
    def setUp(self):
        self.cli = _mock_cli()

    def test_returns_simplified_list(self):
        self.cli.sdk_list_vnics.return_value = [
            {"id": FAKE_VNIC, "display-name": "vnic-1",
             "public-ip": FAKE_IP, "private-ip": "10.0.0.5",
             "is-primary": True, "lifecycle-state": "AVAILABLE",
             "mac-address": "00:11:22:33:44:55"},
            {"id": FAKE_VNIC_2, "display-name": "vnic-2",
             "public-ip": None, "private-ip": "10.0.0.6",
             "is-primary": False},
        ]
        result = list_vnics(self.cli, FAKE_INSTANCE)
        self.assertEqual(len(result), 2)
        # Only the slim fields are kept; extra fields (e.g. mac-address) are dropped. 只保留精简字段，多余字段（如 mac-address）被去掉。
        self.assertEqual(result[0], {
            "id": FAKE_VNIC, "display-name": "vnic-1",
            "public-ip": FAKE_IP, "private-ip": "10.0.0.5",
            "is-primary": True})
        self.assertIsNone(result[1]["public-ip"])
        self.cli.sdk_list_vnics.assert_called_once_with(
            FAKE_INSTANCE, compartment_id=None)
        self.cli.run.assert_not_called()

    def test_empty_instance_returns_empty_list(self):
        self.cli.sdk_list_vnics.return_value = []
        self.assertEqual(list_vnics(self.cli, FAKE_INSTANCE), [])
        self.cli.run.assert_not_called()


class TestGetPublicIp(unittest.TestCase):
    def setUp(self):
        self.cli = _mock_cli()

    def test_returns_primary_vnic_public_ip(self):
        self.cli.sdk_list_vnics.return_value = [
            {"id": FAKE_VNIC_2, "public-ip": "203.0.113.99",
             "is-primary": False},
            {"id": FAKE_VNIC, "public-ip": FAKE_IP, "is-primary": True},
        ]
        self.assertEqual(get_public_ip(self.cli, FAKE_INSTANCE), FAKE_IP)

    def test_no_public_ip_returns_none(self):
        self.cli.sdk_list_vnics.return_value = [
            {"id": FAKE_VNIC, "public-ip": None, "is-primary": True},
        ]
        self.assertIsNone(get_public_ip(self.cli, FAKE_INSTANCE))

    def test_no_vnics_returns_none(self):
        self.cli.sdk_list_vnics.return_value = []
        self.assertIsNone(get_public_ip(self.cli, FAKE_INSTANCE))


class TestGetVnic(unittest.TestCase):
    def setUp(self):
        self.cli = _mock_cli()

    def test_returns_vnic_detail(self):
        detail = {"id": FAKE_VNIC, "display-name": "vnic-1",
                  "public-ip": FAKE_IP, "private-ip": "10.0.0.5",
                  "is-primary": True, "lifecycle-state": "AVAILABLE"}
        self.cli.sdk_get_vnic.return_value = detail
        self.assertEqual(get_vnic(self.cli, FAKE_VNIC), detail)
        self.cli.sdk_get_vnic.assert_called_once_with(FAKE_VNIC)
        self.cli.run.assert_not_called()

    def test_not_found_raises(self):
        self.cli.sdk_get_vnic.side_effect = NotFoundError(
            "NotAuthorizedOrNotFound")
        with self.assertRaises(NotFoundError):
            get_vnic(self.cli, "ocid1.vnic.oc1..doesnotexist")


class TestListPublicIps(unittest.TestCase):
    def setUp(self):
        self.cli = _mock_cli()

    def test_returns_simplified_list(self):
        self.cli.sdk_list_public_ips.return_value = [
            {"id": FAKE_PUBLIC_IP, "ip-address": FAKE_IP,
             "lifetime": "RESERVED", "lifecycle-state": "ASSIGNED",
             "assigned-entity-id": FAKE_PRIVATE_IP,
             "display-name": "my-ip", "scope": "REGION"},
            {"id": "ocid1.publicip.oc1..yyyyyyyy", "ip-address": "203.0.113.20",
             "lifetime": "EPHEMERAL", "lifecycle-state": "ASSIGNED",
             "assigned-entity-id": None},
        ]
        result = list_public_ips(self.cli, FAKE_COMPARTMENT)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0], {
            "id": FAKE_PUBLIC_IP, "ip-address": FAKE_IP,
            "lifetime": "RESERVED", "lifecycle-state": "ASSIGNED",
            "assigned-entity": FAKE_PRIVATE_IP})
        self.assertEqual(result[1]["lifetime"], "EPHEMERAL")
        self.cli.sdk_list_public_ips.assert_called_once_with(
            FAKE_COMPARTMENT, availability_domain=None)
        self.cli.run.assert_not_called()

    def test_empty_returns_empty_list(self):
        self.cli.sdk_list_public_ips.return_value = []
        self.assertEqual(list_public_ips(self.cli, FAKE_COMPARTMENT), [])
        self.cli.run.assert_not_called()


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
        self.cli.run.return_value = None
        self.assertIsNone(delete_public_ip(self.cli, FAKE_PUBLIC_IP))
        self.cli.run.assert_called_once_with(
            ["network", "public-ip", "delete",
             "--public-ip-id", FAKE_PUBLIC_IP, "--force"])


class TestListPrivateIps(unittest.TestCase):
    def setUp(self):
        self.cli = _mock_cli()

    def test_returns_simplified_list(self):
        self.cli.sdk_list_private_ips.return_value = [
            {"id": FAKE_PRIVATE_IP, "ip-address": "10.0.0.5",
             "is-primary": True, "display-name": "primary",
             "hostname-label": "host1"},
            {"id": "ocid1.privateip.oc1..yyyyyyyy", "ip-address": "10.0.0.6",
             "is-primary": False, "display-name": None},
        ]
        result = list_private_ips(self.cli, FAKE_VNIC)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0], {
            "id": FAKE_PRIVATE_IP, "ip-address": "10.0.0.5",
            "is-primary": True, "display-name": "primary"})
        self.cli.sdk_list_private_ips.assert_called_once_with(FAKE_VNIC)
        self.cli.run.assert_not_called()


class TestChangePublicIp(unittest.TestCase):
    def setUp(self):
        self.cli = _mock_cli()

    def _setup_reads(self, old_ip_present=True):
        """换 IP 全流程的读接口返回值（SDK 直调）。

        新流程（2026-11-23 真实验证后修正）：先删旧临时 IP 再绑新 IP，
        因为 OCI 拒绝给已绑公网 IP 的内网 IP 再绑一个（409 Conflict）。
        读接口走 SDK，写接口（删/建/绑）仍走 cli.run。
        """
        # 1. list-vnics: the primary VNIC. 1. list-vnics：主 VNIC。
        self.cli.sdk_list_vnics.return_value = [
            {"id": FAKE_VNIC, "display-name": "vnic-1",
             "public-ip": "203.0.113.1", "private-ip": "10.0.0.5",
             "is-primary": True}]
        # 2. list private-ip: the primary private IP. 2. list private-ip：主内网 IP。
        self.cli.sdk_list_private_ips.return_value = [
            {"id": FAKE_PRIVATE_IP, "ip-address": "10.0.0.5",
             "is-primary": True, "display-name": "primary"}]
        # 3. instance get: fetch the AD. 3. instance get：取 AD。
        self.cli.sdk_get_instance.return_value = {
            "id": FAKE_INSTANCE, "availability-domain": "AD-1"}
        # 4. list public-ip (AD-scoped): the old ephemeral IP already attached. 4. list public-ip（AD 作用域）：已绑的旧临时 IP。
        self.cli.sdk_list_public_ips.return_value = [
            {"id": "ocid1.publicip.oc1..oldip",
             "ip-address": "203.0.113.1", "lifetime": "EPHEMERAL",
             "lifecycle-state": "ASSIGNED",
             "assigned-entity-id": FAKE_PRIVATE_IP}
        ] if old_ip_present else []

    def _setup_writes(self, old_ip_present=True):
        """换 IP 全流程的写接口返回值（cli.run，按顺序）。"""
        responses = []
        if old_ip_present:
            # 5. delete the old ephemeral IP. 5. delete 旧临时 IP。
            responses.append(None)
        responses += [
            # 6. create a reserved IP: the new IP. 6. create 保留 IP：新 IP。
            {"data": {"id": FAKE_PUBLIC_IP, "ip-address": FAKE_IP,
                      "lifetime": "RESERVED"}},
            # 7. update to attach it. 7. update 绑定。
            {"data": {"id": FAKE_PUBLIC_IP, "ip-address": FAKE_IP,
                      "lifecycle-state": "ASSIGNED"}},
        ]
        self.cli.run.side_effect = responses

    def test_full_flow_returns_new_ip_and_calls_in_order(self):
        self._setup_reads()
        self._setup_writes()
        result = change_public_ip(self.cli, FAKE_COMPARTMENT, FAKE_INSTANCE)
        self.assertEqual(result, FAKE_IP)
        # 写接口的调用顺序与参数（先删旧 IP 再绑新 IP）。
        self.assertEqual(self.cli.run.call_args_list, [
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
        # 读接口走了 SDK，没起子进程。
        self.cli.sdk_list_vnics.assert_called_once_with(
            FAKE_INSTANCE, compartment_id=FAKE_COMPARTMENT)
        self.cli.sdk_list_private_ips.assert_called_once_with(FAKE_VNIC)
        self.cli.sdk_get_instance.assert_called_once_with(FAKE_INSTANCE)
        self.cli.sdk_list_public_ips.assert_called_once_with(
            FAKE_COMPARTMENT, availability_domain="AD-1")

    def test_no_old_ip_skips_delete(self):
        """内网 IP 上没有已绑公网 IP 时，不调 delete，直接建新 IP。"""
        self._setup_reads(old_ip_present=False)
        self._setup_writes(old_ip_present=False)
        result = change_public_ip(self.cli, FAKE_COMPARTMENT, FAKE_INSTANCE)
        self.assertEqual(result, FAKE_IP)
        delete_calls = [c for c in self.cli.run.call_args_list
                        if c[0][0][:3] == ["network", "public-ip", "delete"]]
        self.assertEqual(delete_calls, [])
        self.assertEqual(self.cli.run.call_count, 2)

    def test_custom_display_name(self):
        self._setup_reads()
        self._setup_writes()
        change_public_ip(self.cli, FAKE_COMPARTMENT, FAKE_INSTANCE,
                         display_name="my-new-ip")
        create_call = self.cli.run.call_args_list[1]
        self.assertIn("my-new-ip", create_call[0][0])

    def test_no_primary_vnic_raises_before_creating_ip(self):
        self.cli.sdk_list_vnics.return_value = [
            {"id": FAKE_VNIC, "is-primary": False}]
        with self.assertRaises(ValueError):
            change_public_ip(self.cli, FAKE_COMPARTMENT, FAKE_INSTANCE)
        # 只查了 VNIC 就停下，没有创建任何 IP，也没调写接口。
        self.cli.sdk_list_private_ips.assert_not_called()
        self.cli.run.assert_not_called()

    def test_no_primary_private_ip_raises_before_creating_ip(self):
        self.cli.sdk_list_vnics.return_value = [
            {"id": FAKE_VNIC, "is-primary": True}]
        self.cli.sdk_list_private_ips.return_value = [
            {"id": FAKE_PRIVATE_IP, "is-primary": False}]
        with self.assertRaises(ValueError):
            change_public_ip(self.cli, FAKE_COMPARTMENT, FAKE_INSTANCE)
        # 只查了 VNIC + 内网 IP 就停下，没有创建任何 IP。
        self.cli.sdk_get_instance.assert_not_called()
        self.cli.run.assert_not_called()


class TestAttachPublicIps(unittest.TestCase):
    """attach_public_ips：并行补 IP、跳过已终止、单台失败记 None。"""

    def setUp(self):
        self.cli = _mock_cli()

    def _inst(self, inst_id, state):
        return {"id": inst_id, "display-name": inst_id[-4:],
                "shape": "VM.Standard.A1.Flex", "lifecycle-state": state,
                "time-created": "2026-11-20T00:00:00Z"}

    def test_attaches_ips_and_returns_same_list(self):
        instances = [self._inst("ocid1.instance.oc1..aaaa", "RUNNING"),
                     self._inst("ocid1.instance.oc1..bbbb", "STOPPED")]
        with patch("oci.network.get_public_ip",
                   side_effect=["203.0.113.10", None]) as mock_get:
            result = attach_public_ips(self.cli, instances)
        self.assertIs(result, instances)
        self.assertEqual(instances[0]["public-ip"], "203.0.113.10")
        self.assertIsNone(instances[1]["public-ip"])
        self.assertEqual(mock_get.call_count, 2)

    def test_skips_terminated_and_terminating(self):
        instances = [self._inst("ocid1.instance.oc1..aaaa", "RUNNING"),
                     self._inst("ocid1.instance.oc1..bbbb", "TERMINATED"),
                     self._inst("ocid1.instance.oc1..cccc", "TERMINATING")]
        with patch("oci.network.get_public_ip",
                   return_value=FAKE_IP) as mock_get:
            attach_public_ips(self.cli, instances)
        # Only the RUNNING instance is queried; terminated / terminating ones are not. 只查了 RUNNING 的那台，已终止/终止中的不查。
        mock_get.assert_called_once_with(
            self.cli, "ocid1.instance.oc1..aaaa")
        self.assertEqual(instances[0]["public-ip"], FAKE_IP)
        self.assertIsNone(instances[1]["public-ip"])
        self.assertIsNone(instances[2]["public-ip"])

    def test_single_failure_becomes_none(self):
        instances = [self._inst("ocid1.instance.oc1..aaaa", "RUNNING"),
                     self._inst("ocid1.instance.oc1..bbbb", "RUNNING")]
        with patch("oci.network.get_public_ip",
                   side_effect=[OCIError("boom"), FAKE_IP]):
            attach_public_ips(self.cli, instances)
        self.assertIsNone(instances[0]["public-ip"])
        self.assertEqual(instances[1]["public-ip"], FAKE_IP)

    def test_missing_id_not_queried(self):
        instances = [{"display-name": "no-id",
                      "lifecycle-state": "RUNNING"}]
        with patch("oci.network.get_public_ip") as mock_get:
            attach_public_ips(self.cli, instances)
        mock_get.assert_not_called()
        self.assertIsNone(instances[0]["public-ip"])

    def test_empty_list(self):
        with patch("oci.network.get_public_ip") as mock_get:
            self.assertEqual(attach_public_ips(self.cli, []), [])
        mock_get.assert_not_called()


class TestAttachPublicIpsBulk(unittest.TestCase):
    """attach_public_ips_bulk：一次挂载列表 + 每台一次 get-vnic。"""

    def setUp(self):
        self.cli = _mock_cli()

    def _inst(self, inst_id, state):
        return {"id": inst_id, "display-name": inst_id[-4:],
                "shape": "VM.Standard.A1.Flex", "lifecycle-state": state,
                "time-created": "2026-11-20T00:00:00Z"}

    def _att(self, inst_id, vnic_id):
        return {"instance-id": inst_id, "vnic-id": vnic_id}

    def _vnic(self, vnic_id, ip, primary=True):
        return {"id": vnic_id, "public-ip": ip, "is-primary": primary}

    def test_one_attachment_list_for_all_instances(self):
        instances = [self._inst("ocid1.instance.oc1..aaaa", "RUNNING"),
                     self._inst("ocid1.instance.oc1..bbbb", "RUNNING")]
        self.cli.sdk_list_vnic_attachments.return_value = [
            self._att("ocid1.instance.oc1..aaaa", "ocid1.vnic.oc1..1111"),
            self._att("ocid1.instance.oc1..bbbb", "ocid1.vnic.oc1..2222")]
        self.cli.sdk_get_vnic.side_effect = [
            self._vnic("ocid1.vnic.oc1..1111", "203.0.113.10"),
            self._vnic("ocid1.vnic.oc1..2222", "203.0.113.11")]
        result = attach_public_ips_bulk(self.cli, instances,
                                        FAKE_COMPARTMENT)
        self.assertEqual(result, ["203.0.113.10", "203.0.113.11"])
        # 挂载列表只调一次且不带 instance_id（compartment 级）；每台一次 get-vnic。
        self.cli.sdk_list_vnic_attachments.assert_called_once_with(
            FAKE_COMPARTMENT)
        self.assertEqual(self.cli.sdk_get_vnic.call_count, 2)

    def test_skips_terminated_and_missing_id(self):
        instances = [self._inst("ocid1.instance.oc1..aaaa", "RUNNING"),
                     self._inst("ocid1.instance.oc1..bbbb", "TERMINATED"),
                     {"display-name": "no-id", "lifecycle-state": "RUNNING"}]
        self.cli.sdk_list_vnic_attachments.return_value = [
            self._att("ocid1.instance.oc1..aaaa", "ocid1.vnic.oc1..1111"),
            self._att("ocid1.instance.oc1..bbbb", "ocid1.vnic.oc1..2222")]
        self.cli.sdk_get_vnic.return_value = self._vnic(
            "ocid1.vnic.oc1..1111", FAKE_IP)
        result = attach_public_ips_bulk(self.cli, instances,
                                        FAKE_COMPARTMENT)
        self.assertEqual(result, [FAKE_IP, None, None])
        # 已终止与无 id 的实例不查 get-vnic。
        self.cli.sdk_get_vnic.assert_called_once_with("ocid1.vnic.oc1..1111")

    def test_single_get_vnic_failure_becomes_none(self):
        instances = [self._inst("ocid1.instance.oc1..aaaa", "RUNNING"),
                     self._inst("ocid1.instance.oc1..bbbb", "RUNNING")]
        self.cli.sdk_list_vnic_attachments.return_value = [
            self._att("ocid1.instance.oc1..aaaa", "ocid1.vnic.oc1..1111"),
            self._att("ocid1.instance.oc1..bbbb", "ocid1.vnic.oc1..2222")]
        self.cli.sdk_get_vnic.side_effect = [
            NotFoundError("oci network vnic get 失败：404"),
            self._vnic("ocid1.vnic.oc1..2222", FAKE_IP)]
        result = attach_public_ips_bulk(self.cli, instances,
                                        FAKE_COMPARTMENT)
        self.assertEqual(result, [None, FAKE_IP])

    def test_bulk_list_failure_falls_back_to_per_instance(self):
        instances = [self._inst("ocid1.instance.oc1..aaaa", "RUNNING"),
                     self._inst("ocid1.instance.oc1..bbbb", "RUNNING")]
        self.cli.sdk_list_vnic_attachments.side_effect = OCIError("boom")
        with patch("oci.network.get_public_ip",
                   side_effect=[FAKE_IP, None]) as mock_get:
            result = attach_public_ips_bulk(self.cli, instances,
                                            FAKE_COMPARTMENT)
        self.assertEqual(result, [FAKE_IP, None])
        self.assertEqual(mock_get.call_count, 2)

    def test_multi_vnic_picks_primary(self):
        instances = [self._inst("ocid1.instance.oc1..aaaa", "RUNNING")]
        self.cli.sdk_list_vnic_attachments.return_value = [
            self._att("ocid1.instance.oc1..aaaa", "ocid1.vnic.oc1..1111"),
            self._att("ocid1.instance.oc1..aaaa", "ocid1.vnic.oc1..2222")]
        self.cli.sdk_get_vnic.side_effect = [
            self._vnic("ocid1.vnic.oc1..1111", None, primary=False),
            self._vnic("ocid1.vnic.oc1..2222", FAKE_IP, primary=True)]
        result = attach_public_ips_bulk(self.cli, instances,
                                        FAKE_COMPARTMENT)
        self.assertEqual(result, [FAKE_IP])

    def test_empty_list_no_sdk_calls(self):
        result = attach_public_ips_bulk(self.cli, [], FAKE_COMPARTMENT)
        self.assertEqual(result, [])
        self.cli.sdk_list_vnic_attachments.assert_not_called()
        self.cli.sdk_get_vnic.assert_not_called()

    def test_get_vnic_calls_run_in_parallel(self):
        """3 台各睡 0.3 秒的 get-vnic 并行跑，总耗时远小于串行 0.9 秒。"""
        import time as _time

        instances = [self._inst("ocid1.instance.oc1..aaaa", "RUNNING"),
                     self._inst("ocid1.instance.oc1..bbbb", "RUNNING"),
                     self._inst("ocid1.instance.oc1..cccc", "RUNNING")]
        self.cli.sdk_list_vnic_attachments.return_value = [
            self._att("ocid1.instance.oc1..aaaa", "ocid1.vnic.oc1..1111"),
            self._att("ocid1.instance.oc1..bbbb", "ocid1.vnic.oc1..2222"),
            self._att("ocid1.instance.oc1..cccc", "ocid1.vnic.oc1..3333")]

        def _slow_get_vnic(vnic_id):
            _time.sleep(0.3)
            return self._vnic(vnic_id, "203.0.113.10")

        self.cli.sdk_get_vnic.side_effect = _slow_get_vnic
        start = _time.monotonic()
        result = attach_public_ips_bulk(self.cli, instances,
                                        FAKE_COMPARTMENT)
        elapsed = _time.monotonic() - start
        self.assertEqual(result, ["203.0.113.10"] * 3)
        self.assertLess(elapsed, 0.75, "耗时 %.2fs，疑似串行取 VNIC" % elapsed)

    def test_ip_map_path_makes_no_sdk_calls(self):
        """第 7 轮：attachments + ip_map 都传时 0 次 SDK 调用，结果一致。"""
        instances = [self._inst("ocid1.instance.oc1..aaaa", "RUNNING"),
                     self._inst("ocid1.instance.oc1..bbbb", "RUNNING")]
        attachments = [
            self._att("ocid1.instance.oc1..aaaa", "ocid1.vnic.oc1..1111"),
            self._att("ocid1.instance.oc1..bbbb", "ocid1.vnic.oc1..2222")]
        ip_map = {
            "ocid1.vnic.oc1..1111": {"is-primary": True,
                                     "public-ip": "203.0.113.10"},
            "ocid1.vnic.oc1..2222": {"is-primary": True,
                                     "public-ip": "203.0.113.11"}}
        result = attach_public_ips_bulk(self.cli, instances,
                                        FAKE_COMPARTMENT,
                                        attachments=attachments,
                                        ip_map=ip_map)
        self.assertEqual(result, ["203.0.113.10", "203.0.113.11"])
        self.cli.sdk_list_vnic_attachments.assert_not_called()
        self.cli.sdk_get_vnic.assert_not_called()

    def test_ip_map_only_still_fetches_attachments(self):
        """只传 ip_map：挂载列表仍调 SDK，get-vnic 不调。"""
        instances = [self._inst("ocid1.instance.oc1..aaaa", "RUNNING")]
        self.cli.sdk_list_vnic_attachments.return_value = [
            self._att("ocid1.instance.oc1..aaaa", "ocid1.vnic.oc1..1111")]
        ip_map = {"ocid1.vnic.oc1..1111": {"is-primary": True,
                                           "public-ip": FAKE_IP}}
        result = attach_public_ips_bulk(self.cli, instances,
                                        FAKE_COMPARTMENT, ip_map=ip_map)
        self.assertEqual(result, [FAKE_IP])
        self.cli.sdk_list_vnic_attachments.assert_called_once_with(
            FAKE_COMPARTMENT)
        self.cli.sdk_get_vnic.assert_not_called()

    def test_attachments_only_still_calls_get_vnic(self):
        """只传 attachments：get-vnic 仍按旧路径调，结果一致。"""
        instances = [self._inst("ocid1.instance.oc1..aaaa", "RUNNING")]
        self.cli.sdk_list_vnic_attachments.return_value = [
            self._att("ocid1.instance.oc1..aaaa", "ocid1.vnic.oc1..1111")]
        self.cli.sdk_get_vnic.return_value = self._vnic(
            "ocid1.vnic.oc1..1111", FAKE_IP)
        # 注意：attachments 传了，挂载列表不调 SDK；get-vnic 仍调一次。
        result = attach_public_ips_bulk(
            self.cli, instances, FAKE_COMPARTMENT,
            attachments=[self._att("ocid1.instance.oc1..aaaa",
                                   "ocid1.vnic.oc1..1111")])
        self.assertEqual(result, [FAKE_IP])
        self.cli.sdk_list_vnic_attachments.assert_not_called()
        self.cli.sdk_get_vnic.assert_called_once_with(
            "ocid1.vnic.oc1..1111")

    def test_ip_map_missing_vnic_is_none(self):
        """映射里缺失的 VNIC 按无公网 IP 处理，不报错。"""
        instances = [self._inst("ocid1.instance.oc1..aaaa", "RUNNING")]
        attachments = [self._att("ocid1.instance.oc1..aaaa",
                                 "ocid1.vnic.oc1..1111")]
        result = attach_public_ips_bulk(self.cli, instances,
                                        FAKE_COMPARTMENT,
                                        attachments=attachments, ip_map={})
        self.assertEqual(result, [None])
        self.cli.sdk_list_vnic_attachments.assert_not_called()
        self.cli.sdk_get_vnic.assert_not_called()

    def test_ip_map_primary_without_ip_returns_none(self):
        """主 VNIC 本身无 IP 时返回 None（与旧 _fetch_bulk 语义一致：
        第一个主 VNIC 说了算，不继续找后面的 VNIC）。"""
        instances = [self._inst("ocid1.instance.oc1..aaaa", "RUNNING")]
        attachments = [
            self._att("ocid1.instance.oc1..aaaa", "ocid1.vnic.oc1..1111"),
            self._att("ocid1.instance.oc1..aaaa", "ocid1.vnic.oc1..2222")]
        ip_map = {
            "ocid1.vnic.oc1..1111": {"is-primary": True, "public-ip": None},
            "ocid1.vnic.oc1..2222": {"is-primary": True,
                                     "public-ip": FAKE_IP}}
        result = attach_public_ips_bulk(self.cli, instances,
                                        FAKE_COMPARTMENT,
                                        attachments=attachments,
                                        ip_map=ip_map)
        self.assertEqual(result, [None])


class TestWaitForPublicIp(unittest.TestCase):
    """wait_for_public_ip：轮询等刚开出的实例分配公网 IP。"""

    def test_immediate_hit_no_sleep(self):
        with patch("oci.network.get_public_ip",
                   return_value="203.0.113.10") as mock_get, \
                patch("oci.network.time.monotonic",
                      side_effect=[0]) as mock_clock, \
                patch("oci.network.time.sleep") as mock_sleep:
            ip = wait_for_public_ip(MagicMock(), FAKE_INSTANCE,
                                    timeout_seconds=60, interval_seconds=5)
        self.assertEqual(ip, "203.0.113.10")
        mock_get.assert_called_once()
        mock_sleep.assert_not_called()
        self.assertEqual(mock_clock.call_count, 1)

    def test_retry_then_success(self):
        with patch("oci.network.get_public_ip",
                   side_effect=[None, None, "203.0.113.10"]) as mock_get, \
                patch("oci.network.time.monotonic",
                      side_effect=[0, 0, 0]), \
                patch("oci.network.time.sleep") as mock_sleep:
            ip = wait_for_public_ip(MagicMock(), FAKE_INSTANCE,
                                    timeout_seconds=60, interval_seconds=5)
        self.assertEqual(ip, "203.0.113.10")
        self.assertEqual(mock_get.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)
        mock_sleep.assert_called_with(5)

    def test_timeout_returns_none(self):
        with patch("oci.network.get_public_ip",
                   return_value=None) as mock_get, \
                patch("oci.network.time.monotonic",
                      side_effect=[0, 0, 11]), \
                patch("oci.network.time.sleep") as mock_sleep:
            ip = wait_for_public_ip(MagicMock(), FAKE_INSTANCE,
                                    timeout_seconds=10, interval_seconds=5)
        self.assertIsNone(ip)
        self.assertEqual(mock_get.call_count, 2)
        mock_sleep.assert_called_once_with(5)

    def test_query_exception_tolerated(self):
        with patch("oci.network.get_public_ip",
                   side_effect=[RuntimeError("boom"),
                                "203.0.113.10"]) as mock_get, \
                patch("oci.network.time.monotonic",
                      side_effect=[0, 0]), \
                patch("oci.network.time.sleep") as mock_sleep:
            ip = wait_for_public_ip(MagicMock(), FAKE_INSTANCE,
                                    timeout_seconds=60, interval_seconds=5)
        self.assertEqual(ip, "203.0.113.10")
        self.assertEqual(mock_get.call_count, 2)
        mock_sleep.assert_called_once_with(5)


if __name__ == "__main__":
    unittest.main()
