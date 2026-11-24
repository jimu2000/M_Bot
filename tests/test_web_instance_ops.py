#!/usr/bin/env python3
"""Web 面板实例操作补全的测试（v1.4.0 第 1 批搬运）：换 IP、IPv6 附加/删除、
升降配、尝试修复、实例详情附加字段。

运行：python3 -m unittest tests.test_web_instance_ops -v

注意：
  - TestClient + mock oci 函数，不碰真实 API；oci 层测试用 MagicMock cli。
  - 测试密码哈希在 setUpClass 里生成并 patch 进 web.config，
    不使用任何真实密码。
  - 全部占位符，不出现真实 OCID / IP。
"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import ANY, MagicMock, call, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from oci.client import NotFoundError, OCIError
from oci.network import (
    create_ephemeral_public_ip,
    create_ipv6,
    delete_ipv6,
    get_primary_vnic,
    list_ipv6_for_vnic,
    repair_instance_ip,
)
from oci.instance import resize_instance
from oci.quota import check_a1_single_instance_cap
from web import config
from web.app import create_app
from web.auth import LOGIN_GUARD, SESSIONS, hash_password
from web.routes.cloudreport import clear_cloud_report_cache
from web.routes.instances import clear_instance_list_cache
from web.routes.volumes import clear_volumes_cache
from web.tasks import TASKS

# 占位符（测试里不出现真实 OCID / IP）
FAKE_INSTANCE = "ocid1.instance.oc1..testxxxx"
FAKE_VNIC = "ocid1.vnic.oc1..testxxxx"
FAKE_PRIVATE_IP = "ocid1.privateip.oc1..testxxxx"
FAKE_IPV6_1 = "ocid1.ipv6.oc1..test1111"
FAKE_IPV6_2 = "ocid1.ipv6.oc1..test2222"
FAKE_VOLUME = "ocid1.bootvolume.oc1..testxxxx"
FAKE_OLD_IP = "203.0.113.10"
FAKE_NEW_IP = "203.0.113.99"
FAKE_IPV6_ADDR = "2603:c024:c021:1d01::1"

TEST_PASSWORD = "test-admin-password"

FAKE_INSTANCES = [
    {"id": FAKE_INSTANCE, "display-name": "instance-a1",
     "shape": "VM.Standard.A1.Flex", "lifecycle-state": "RUNNING",
     "time-created": "2026-11-11T10:00:00.000Z"},
]

FAKE_A1_DETAIL = {"data": {"id": FAKE_INSTANCE,
                           "display-name": "instance-a1",
                           "shape": "VM.Standard.A1.Flex",
                           "lifecycle-state": "RUNNING",
                           "shape-config": {"ocpus": 2,
                                            "memory-in-gbs": 12},
                           "availability-domain": "AD-1",
                           "time-created": "2026-11-11T10:00:00.000Z"}}


class InstanceOpsTestCase(unittest.TestCase):
    """API 测试基类：建 app、临时 DATA_DIR、登录（同 test_web_api 模式）。"""

    @classmethod
    def setUpClass(cls):
        cls._hash_patcher = patch("web.config.ADMIN_PASSWORD_HASH",
                                  hash_password(TEST_PASSWORD))
        cls._hash_patcher.start()
        cls.app = create_app()

    @classmethod
    def tearDownClass(cls):
        cls._hash_patcher.stop()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._data_dir_patcher = patch("web.config.DATA_DIR", self._tmp.name)
        self._data_dir_patcher.start()
        clear_instance_list_cache()
        clear_volumes_cache()
        clear_cloud_report_cache()
        LOGIN_GUARD.reset()
        SESSIONS.reset()
        TASKS.reset()
        self.client = TestClient(self.app)
        self._login()

    def tearDown(self):
        self._data_dir_patcher.stop()
        self._tmp.cleanup()

    def _login(self, username="admin", password=None):
        return self.client.post("/api/auth/login",
                                json={"username": username,
                                      "password": password or TEST_PASSWORD})


class TestChangeIpApi(InstanceOpsTestCase):
    """POST /api/instances/{id}/change-ip。"""

    @patch("web.routes.instances.change_public_ip",
           return_value=FAKE_NEW_IP)
    @patch("web.routes.instances.get_public_ip", return_value=FAKE_OLD_IP)
    def test_change_ip_success(self, mock_ip, mock_change):
        r = self.client.post(f"/api/instances/{FAKE_INSTANCE}/change-ip",
                             json={"confirm": True})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["old_ip"], FAKE_OLD_IP)
        self.assertEqual(body["new_ip"], FAKE_NEW_IP)
        mock_change.assert_called_once_with(ANY, ANY, FAKE_INSTANCE)

    @patch("web.routes.instances.change_public_ip")
    def test_change_ip_without_confirm_400(self, mock_change):
        r = self.client.post(f"/api/instances/{FAKE_INSTANCE}/change-ip",
                             json={})
        self.assertEqual(r.status_code, 400)
        r = self.client.post(f"/api/instances/{FAKE_INSTANCE}/change-ip",
                             json={"confirm": False})
        self.assertEqual(r.status_code, 400)
        mock_change.assert_not_called()

    @patch("web.routes.instances.change_public_ip",
           side_effect=ValueError("实例没有主 VNIC，无法换 IP"))
    @patch("web.routes.instances.get_public_ip", return_value=None)
    def test_change_ip_no_primary_vnic_400(self, mock_ip, mock_change):
        r = self.client.post(f"/api/instances/{FAKE_INSTANCE}/change-ip",
                             json={"confirm": True})
        self.assertEqual(r.status_code, 400)
        self.assertIn("主 VNIC", r.json()["error"])

    @patch("web.routes.instances.change_public_ip",
           side_effect=NotFoundError("not found"))
    @patch("web.routes.instances.get_public_ip", return_value=None)
    def test_change_ip_not_found_404(self, mock_ip, mock_change):
        r = self.client.post(f"/api/instances/{FAKE_INSTANCE}/change-ip",
                             json={"confirm": True})
        self.assertEqual(r.status_code, 404)

    @patch("web.routes.instances.change_public_ip",
           return_value=FAKE_NEW_IP)
    @patch("web.routes.instances.get_public_ip", return_value=FAKE_OLD_IP)
    @patch("web.routes.instances.resolve_profile",
           return_value=(MagicMock(), "ocid1.compartment.oc1..testxxxx"))
    def test_change_ip_passes_profile(self, mock_resolve, mock_ip,
                                      mock_change):
        """?profile=ACCOUNT2 要传给 resolve_profile（不要重蹈遗漏 bug）。"""
        r = self.client.post(
            f"/api/instances/{FAKE_INSTANCE}/change-ip?profile=ACCOUNT2",
            json={"confirm": True})
        self.assertEqual(r.status_code, 200)
        mock_resolve.assert_called_once_with("ACCOUNT2", None)

    @patch("web.routes.instances.change_public_ip",
           return_value=FAKE_NEW_IP)
    @patch("web.routes.instances.get_public_ip", return_value=FAKE_OLD_IP)
    @patch("web.routes.instances.list_instances",
           return_value=FAKE_INSTANCES)
    def test_change_ip_clears_list_cache(self, mock_list, mock_ip,
                                         mock_change):
        """换 IP 成功后清实例列表服务端缓存，下次列表重新查询。"""
        self.client.get("/api/instances")
        self.client.get("/api/instances")
        self.assertEqual(mock_list.call_count, 1)  # 第二次命中缓存
        r = self.client.post(f"/api/instances/{FAKE_INSTANCE}/change-ip",
                             json={"confirm": True})
        self.assertEqual(r.status_code, 200)
        self.client.get("/api/instances")
        self.assertEqual(mock_list.call_count, 2)  # 缓存已清，重新查询


class TestIpv6Api(InstanceOpsTestCase):
    """POST / DELETE /api/instances/{id}/ipv6。"""

    @patch("web.routes.instances.create_ipv6",
           return_value={"ip-address": FAKE_IPV6_ADDR})
    @patch("web.routes.instances.get_primary_vnic",
           return_value={"id": FAKE_VNIC, "is-primary": True})
    def test_add_ipv6_success(self, mock_vnic, mock_create):
        r = self.client.post(f"/api/instances/{FAKE_INSTANCE}/ipv6")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["ipv6"], FAKE_IPV6_ADDR)
        mock_create.assert_called_once_with(ANY, FAKE_VNIC)

    @patch("web.routes.instances.get_primary_vnic",
           side_effect=ValueError("实例没有主 VNIC"))
    def test_add_ipv6_no_vnic_400(self, mock_vnic):
        r = self.client.post(f"/api/instances/{FAKE_INSTANCE}/ipv6")
        self.assertEqual(r.status_code, 400)
        self.assertIn("主 VNIC", r.json()["error"])

    @patch("web.routes.instances.delete_ipv6")
    @patch("web.routes.instances.list_ipv6_for_vnic",
           return_value=[{"id": FAKE_IPV6_1, "ip-address": FAKE_IPV6_ADDR},
                         {"id": FAKE_IPV6_2,
                          "ip-address": "2603:c024:c021:1d01::2"}])
    @patch("web.routes.instances.get_primary_vnic",
           return_value={"id": FAKE_VNIC, "is-primary": True})
    def test_delete_ipv6_all(self, mock_vnic, mock_list, mock_delete):
        """删除该实例的全部 IPv6，返回删掉的地址列表。"""
        r = self.client.delete(f"/api/instances/{FAKE_INSTANCE}/ipv6")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(len(body["deleted"]), 2)
        self.assertEqual(mock_delete.call_count, 2)
        mock_delete.assert_has_calls([call(ANY, FAKE_IPV6_1),
                                      call(ANY, FAKE_IPV6_2)],
                                     any_order=True)

    @patch("web.routes.instances.delete_ipv6")
    @patch("web.routes.instances.list_ipv6_for_vnic", return_value=[])
    @patch("web.routes.instances.get_primary_vnic",
           return_value={"id": FAKE_VNIC, "is-primary": True})
    def test_delete_ipv6_none_ok(self, mock_vnic, mock_list, mock_delete):
        """无 IPv6 时返回 ok + 明确提示，不报错、不调删除。"""
        r = self.client.delete(f"/api/instances/{FAKE_INSTANCE}/ipv6")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["deleted"], [])
        self.assertIn("没有 IPv6", body["message"])
        mock_delete.assert_not_called()

    @patch("web.routes.instances.get_primary_vnic",
           side_effect=ValueError("实例没有主 VNIC"))
    def test_delete_ipv6_no_vnic_400(self, mock_vnic):
        r = self.client.delete(f"/api/instances/{FAKE_INSTANCE}/ipv6")
        self.assertEqual(r.status_code, 400)


class TestResizeApi(InstanceOpsTestCase):
    """POST /api/instances/{id}/resize。"""

    @patch("web.routes.instances.resize_instance")
    @patch("web.routes.instances.get_instance",
           return_value=FAKE_A1_DETAIL)
    def test_resize_success(self, mock_get, mock_resize):
        r = self.client.post(f"/api/instances/{FAKE_INSTANCE}/resize",
                             json={"ocpus": 4, "memory_gb": 24,
                                   "confirm": True})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["ocpus"], 4)
        self.assertEqual(body["memory_gb"], 24)
        mock_resize.assert_called_once_with(ANY, FAKE_INSTANCE, 4, 24)

    @patch("web.routes.instances.resize_instance")
    @patch("web.routes.instances.get_instance",
           return_value=FAKE_A1_DETAIL)
    def test_resize_without_confirm_400(self, mock_get, mock_resize):
        r = self.client.post(f"/api/instances/{FAKE_INSTANCE}/resize",
                             json={"ocpus": 4, "memory_gb": 24})
        self.assertEqual(r.status_code, 400)
        mock_resize.assert_not_called()

    @patch("web.routes.instances.resize_instance")
    @patch("web.routes.instances.get_instance",
           return_value={"data": {"id": FAKE_INSTANCE,
                                  "shape": "VM.Standard.E2.1.Micro",
                                  "lifecycle-state": "RUNNING"}})
    def test_resize_e2_rejected_400(self, mock_get, mock_resize):
        """E2 固定规格不支持升/降配。"""
        r = self.client.post(f"/api/instances/{FAKE_INSTANCE}/resize",
                             json={"ocpus": 2, "memory_gb": 12,
                                   "confirm": True})
        self.assertEqual(r.status_code, 400)
        self.assertIn("固定规格", r.json()["error"])
        mock_resize.assert_not_called()

    @patch("web.routes.instances.resize_instance")
    @patch("web.routes.instances.get_instance",
           return_value={"data": {"id": FAKE_INSTANCE,
                                  "shape": "VM.Standard.E3.Flex",
                                  "lifecycle-state": "RUNNING"}})
    def test_resize_other_shape_rejected_400(self, mock_get, mock_resize):
        """非 A1 形状拒绝升/降配。"""
        r = self.client.post(f"/api/instances/{FAKE_INSTANCE}/resize",
                             json={"ocpus": 2, "memory_gb": 12,
                                   "confirm": True})
        self.assertEqual(r.status_code, 400)
        self.assertIn("只支持 A1.Flex", r.json()["error"])
        mock_resize.assert_not_called()

    @patch("web.routes.instances.resize_instance")
    @patch("web.routes.instances.get_instance",
           return_value=FAKE_A1_DETAIL)
    def test_resize_over_free_tier_cap_400(self, mock_get, mock_resize):
        """A1 超过免费额度上限（4 核 / 24GB）返回 400。"""
        r = self.client.post(f"/api/instances/{FAKE_INSTANCE}/resize",
                             json={"ocpus": 8, "memory_gb": 12,
                                   "confirm": True})
        self.assertEqual(r.status_code, 400)
        self.assertIn("4 OCPU / 24GB", r.json()["error"])
        r = self.client.post(f"/api/instances/{FAKE_INSTANCE}/resize",
                             json={"ocpus": 4, "memory_gb": 48,
                                   "confirm": True})
        self.assertEqual(r.status_code, 400)
        self.assertIn("4 OCPU / 24GB", r.json()["error"])
        mock_resize.assert_not_called()

    @patch("web.routes.instances.resize_instance")
    @patch("web.routes.instances.get_instance",
           return_value=FAKE_A1_DETAIL)
    def test_resize_invalid_values_400(self, mock_get, mock_resize):
        r = self.client.post(f"/api/instances/{FAKE_INSTANCE}/resize",
                             json={"ocpus": 0, "memory_gb": 12,
                                   "confirm": True})
        self.assertEqual(r.status_code, 400)
        mock_resize.assert_not_called()


class TestRepairApi(InstanceOpsTestCase):
    """POST /api/instances/{id}/repair。"""

    @patch("web.routes.instances.repair_instance_ip",
           return_value={"instance_id": FAKE_INSTANCE, "state": "RUNNING",
                         "public_ip": FAKE_OLD_IP, "repaired": False,
                         "new_ip": None,
                         "checks": [{"name": "公网 IP", "status": "ok",
                                     "detail": "已绑定，无需修复"}]})
    def test_repair_ok_no_action(self, mock_repair):
        """公网 IP 正常时返回报告，repaired=False。"""
        r = self.client.post(f"/api/instances/{FAKE_INSTANCE}/repair")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertFalse(body["report"]["repaired"])
        self.assertEqual(body["report"]["public_ip"], FAKE_OLD_IP)

    @patch("web.routes.instances.repair_instance_ip",
           return_value={"instance_id": FAKE_INSTANCE, "state": "RUNNING",
                         "public_ip": FAKE_NEW_IP, "repaired": True,
                         "new_ip": FAKE_NEW_IP,
                         "checks": [{"name": "公网 IP", "status": "fixed",
                                     "detail": "缺失，已重新附加"}]})
    def test_repair_fixed(self, mock_repair):
        """缺失公网 IP 时修复，返回新 IP。"""
        r = self.client.post(f"/api/instances/{FAKE_INSTANCE}/repair")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["report"]["repaired"])
        self.assertEqual(body["report"]["new_ip"], FAKE_NEW_IP)

    @patch("web.routes.instances.repair_instance_ip",
           side_effect=ValueError("实例已终止，无法修复"))
    def test_repair_terminated_400(self, mock_repair):
        r = self.client.post(f"/api/instances/{FAKE_INSTANCE}/repair")
        self.assertEqual(r.status_code, 400)
        self.assertIn("已终止", r.json()["error"])


class TestDetailExtras(InstanceOpsTestCase):
    """GET /api/instances/{id} 的附加字段（内网 IP / IPv6 / 启动盘）。"""

    @patch("web.routes.instances.get_boot_volume",
           return_value={"id": FAKE_VOLUME, "size-in-gbs": 47,
                         "vpus-per-gb": 10, "lifecycle-state": "AVAILABLE"})
    @patch("web.routes.instances.list_boot_volume_attachments",
           return_value=[{"boot-volume-id": FAKE_VOLUME,
                          "instance-id": FAKE_INSTANCE,
                          "lifecycle-state": "ATTACHED"}])
    @patch("web.routes.instances.list_ipv6_for_vnic",
           return_value=[{"ip-address": FAKE_IPV6_ADDR}])
    @patch("web.routes.instances.list_vnics",
           return_value=[{"id": FAKE_VNIC, "public-ip": FAKE_OLD_IP,
                          "private-ip": "10.0.0.5", "is-primary": True}])
    @patch("web.routes.instances.get_public_ip", return_value=FAKE_OLD_IP)
    @patch("web.routes.instances.get_instance",
           return_value=FAKE_A1_DETAIL)
    def test_detail_extras(self, mock_get, mock_ip, mock_vnics, mock_ipv6,
                           mock_atts, mock_vol):
        r = self.client.get(f"/api/instances/{FAKE_INSTANCE}")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["private_ip"], "10.0.0.5")
        self.assertEqual(body["ipv6_addresses"], [FAKE_IPV6_ADDR])
        self.assertEqual(body["boot_volume_size_gbs"], 47)
        self.assertEqual(body["boot_volume_id"], FAKE_VOLUME)
        self.assertEqual(body["boot_volume_vpus_per_gb"], 10)
        self.assertEqual(body["boot_volume_state"], "AVAILABLE")
        self.assertEqual(body["boot_volume_attachment_state"], "ATTACHED")

    @patch("web.routes.instances.list_boot_volume_attachments",
           side_effect=OCIError("bv list failed"))
    @patch("web.routes.instances.list_vnics",
           side_effect=OCIError("vnics failed"))
    @patch("web.routes.instances.get_public_ip", return_value=FAKE_OLD_IP)
    @patch("web.routes.instances.get_instance",
           return_value=FAKE_A1_DETAIL)
    def test_detail_extras_failure_tolerated(self, mock_get, mock_ip,
                                             mock_vnics, mock_atts):
        """附加字段查询失败时详情仍返回，附加字段为 None。"""
        r = self.client.get(f"/api/instances/{FAKE_INSTANCE}")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["instance"]["display-name"], "instance-a1")
        self.assertIsNone(body["private_ip"])
        self.assertIsNone(body["ipv6_addresses"])
        self.assertIsNone(body["boot_volume_size_gbs"])
        self.assertIsNone(body["boot_volume_id"])
        self.assertIsNone(body["boot_volume_vpus_per_gb"])
        self.assertIsNone(body["boot_volume_state"])
        self.assertIsNone(body["boot_volume_attachment_state"])

    @patch("web.routes.instances.list_boot_volume_attachments",
           return_value=[])
    @patch("web.routes.instances.list_vnics", return_value=[])
    @patch("web.routes.instances.get_public_ip", return_value=None)
    @patch("web.routes.instances.get_instance",
           return_value=FAKE_A1_DETAIL)
    def test_detail_no_vnic(self, mock_get, mock_ip, mock_vnics,
                            mock_atts):
        """无 VNIC 时内网 IP/IPv6 为 None，启动盘查不到为 None。"""
        r = self.client.get(f"/api/instances/{FAKE_INSTANCE}")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIsNone(body["private_ip"])
        self.assertIsNone(body["ipv6_addresses"])
        self.assertIsNone(body["boot_volume_size_gbs"])
        self.assertIsNone(body["boot_volume_id"])
        self.assertIsNone(body["boot_volume_vpus_per_gb"])
        self.assertIsNone(body["boot_volume_state"])
        self.assertIsNone(body["boot_volume_attachment_state"])


class TestOciInstanceOpsHelpers(unittest.TestCase):
    """oci 层新函数：只调 cli.run（MagicMock，不发真实请求）。"""

    def test_resize_instance_cli_args(self):
        """升降配：compute instance update --shape-config JSON --force。"""
        cli = MagicMock()
        cli.run.return_value = {"data": {"id": FAKE_INSTANCE}}
        result = resize_instance(cli, FAKE_INSTANCE, 4, 24)
        self.assertEqual(result["id"], FAKE_INSTANCE)
        args = cli.run.call_args[0][0]
        self.assertEqual(args[:3], ["compute", "instance", "update"])
        self.assertIn("--force", args)
        cfg = json.loads(args[args.index("--shape-config") + 1])
        self.assertEqual(cfg, {"ocpus": 4, "memoryInGBs": 24})

    def test_resize_instance_invalid_values(self):
        cli = MagicMock()
        with self.assertRaises(ValueError):
            resize_instance(cli, FAKE_INSTANCE, 0, 12)
        with self.assertRaises(ValueError):
            resize_instance(cli, FAKE_INSTANCE, 2, 0)
        cli.run.assert_not_called()

    def test_check_a1_single_instance_cap(self):
        self.assertIsNone(check_a1_single_instance_cap(4, 24))
        self.assertIsNone(check_a1_single_instance_cap(1, 6))
        self.assertIsNotNone(check_a1_single_instance_cap(8, 12))
        self.assertIsNotNone(check_a1_single_instance_cap(4, 48))
        self.assertIn("4 OCPU / 24GB",
                      check_a1_single_instance_cap(8, 12))

    def test_get_primary_vnic_no_primary(self):
        cli = MagicMock()
        cli.run.return_value = {"data": [{"id": FAKE_VNIC,
                                          "is-primary": False}]}
        with self.assertRaises(ValueError):
            get_primary_vnic(cli, FAKE_INSTANCE)

    def test_ipv6_helpers_cli_args(self):
        """IPv6 三个函数已移到 oci/network.py，命令与原来一致。"""
        cli = MagicMock()
        cli.run.return_value = {"data": [{"ip-address": FAKE_IPV6_ADDR}]}
        result = list_ipv6_for_vnic(cli, FAKE_VNIC)
        self.assertEqual(result[0]["ip-address"], FAKE_IPV6_ADDR)
        args = cli.run.call_args[0][0]
        self.assertEqual(args[:3], ["network", "ipv6", "list"])
        self.assertIn(FAKE_VNIC, args)

        cli.run.return_value = {"data": {"ip-address": FAKE_IPV6_ADDR}}
        result = create_ipv6(cli, FAKE_VNIC)
        self.assertEqual(result["ip-address"], FAKE_IPV6_ADDR)
        args = cli.run.call_args[0][0]
        self.assertEqual(args[:3], ["network", "ipv6", "create"])

        cli.run.return_value = None
        delete_ipv6(cli, FAKE_IPV6_1)
        args = cli.run.call_args[0][0]
        self.assertEqual(args[:3], ["network", "ipv6", "delete"])
        self.assertIn("--force", args)

    def test_create_ephemeral_public_ip_args(self):
        cli = MagicMock()
        cli.run.return_value = {"data": {"id": "ocid1.publicip.oc1..t",
                                         "ip-address": FAKE_NEW_IP}}
        ip_id, ip = create_ephemeral_public_ip(
            cli, "ocid1.compartment.oc1..t", FAKE_PRIVATE_IP)
        self.assertEqual(ip, FAKE_NEW_IP)
        args = cli.run.call_args[0][0]
        self.assertEqual(args[:3], ["network", "public-ip", "create"])
        self.assertIn("EPHEMERAL", args)
        self.assertIn(FAKE_PRIVATE_IP, args)

    def test_repair_instance_ip_already_ok(self):
        """已有公网 IP 时不做任何改动，repaired=False。"""
        cli = MagicMock()
        cli.run.side_effect = [
            {"data": {"lifecycle-state": "RUNNING"}},
            {"data": [{"id": FAKE_VNIC, "display-name": "vnic-1",
                       "public-ip": FAKE_OLD_IP, "private-ip": "10.0.0.5",
                       "is-primary": True}]},
        ]
        report = repair_instance_ip(cli, "ocid1.compartment.oc1..t",
                                    FAKE_INSTANCE)
        self.assertFalse(report["repaired"])
        self.assertEqual(report["public_ip"], FAKE_OLD_IP)
        self.assertIsNone(report["new_ip"])
        self.assertEqual(cli.run.call_count, 2)  # 只查不写

    def test_repair_instance_ip_missing_creates(self):
        """缺失公网 IP 时创建并绑定临时 IP，repaired=True。"""
        cli = MagicMock()
        cli.run.side_effect = [
            {"data": {"lifecycle-state": "RUNNING"}},
            {"data": [{"id": FAKE_VNIC, "display-name": "vnic-1",
                       "public-ip": None, "private-ip": "10.0.0.5",
                       "is-primary": True}]},
            {"data": [{"id": FAKE_PRIVATE_IP, "ip-address": "10.0.0.5",
                       "is-primary": True}]},
            {"data": {"id": "ocid1.publicip.oc1..t",
                      "ip-address": FAKE_NEW_IP}},
        ]
        report = repair_instance_ip(cli, "ocid1.compartment.oc1..t",
                                    FAKE_INSTANCE)
        self.assertTrue(report["repaired"])
        self.assertEqual(report["new_ip"], FAKE_NEW_IP)
        self.assertEqual(report["public_ip"], FAKE_NEW_IP)
        self.assertEqual(cli.run.call_count, 4)
        create_args = cli.run.call_args[0][0]
        self.assertEqual(create_args[:3],
                         ["network", "public-ip", "create"])
        self.assertIn("EPHEMERAL", create_args)

    def test_repair_instance_ip_terminated(self):
        cli = MagicMock()
        cli.run.return_value = {"data": {"lifecycle-state": "TERMINATED"}}
        with self.assertRaises(ValueError):
            repair_instance_ip(cli, "ocid1.compartment.oc1..t",
                               FAKE_INSTANCE)


if __name__ == "__main__":
    unittest.main()
