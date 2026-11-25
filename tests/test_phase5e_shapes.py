#!/usr/bin/env python3
"""阶段 5 工作包 E：API 响应形状对齐的测试（TestClient + mock）。

覆盖：
  - GET /api/instances：面板形状、RUNNING 过滤、shape-config 取 OCPU/内存。
  - GET /api/volumes：面板形状、挂载关系富化、富化失败降级。
  - GET /api/quota/a1：cap 用免费上限 4/24、over 计算。
  - GET /api/system/health：面板形状各字段。
  - GET /api/monitor/rules：复用 tgbot 监控开关。
  - oci 层：list_instances(include_shape_config) / list_boot_volumes 的
    time-created / list_boot_volume_attachments(include_id)。

不碰真实 API；测试用 203.0.113.x 与假 OCID。
"""

import os
import secrets
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from oci.client import OCIError
from oci.instance import list_instances
from oci.volume import list_boot_volume_attachments, list_boot_volumes
from web.app import create_app
from web.auth import LOGIN_GUARD, SESSIONS, hash_password
from web.routes.cloudreport import clear_cloud_report_cache
from web.routes.instances import clear_instance_list_cache
from web.routes.volumes import clear_volumes_cache

FAKE_INSTANCE = "ocid1.instance.oc1..testxxxx"
FAKE_INSTANCE_2 = "ocid1.instance.oc1..testyyyy"
FAKE_INSTANCE_3 = "ocid1.instance.oc1..testzzzz"
FAKE_VOLUME = "ocid1.bootvolume.oc1..testxxxx"
FAKE_VOLUME_2 = "ocid1.bootvolume.oc1..testyyyy"
FAKE_ATTACHMENT = "ocid1.bootvolumeattachment.oc1..testxxxx"
FAKE_COMPARTMENT = "ocid1.tenancy.oc1..testxxxx"
FAKE_IP = "203.0.113.10"

# Test password: generated randomly at runtime (no fixed password is ever written in code). 测试密码：运行时随机生成（不在代码里写任何固定口令）。
TEST_PASSWORD = secrets.token_urlsafe(16)

FAKE_INSTANCES_RAW = [
    {"id": FAKE_INSTANCE, "display-name": "instance-a1",
     "shape": "VM.Standard.A1.Flex", "lifecycle-state": "RUNNING",
     "time-created": "2026-11-11T10:00:00.000Z",
     "shape-config": {"ocpus": 2, "memory-in-gbs": 12}},
    {"id": FAKE_INSTANCE_2, "display-name": "instance-e2",
     "shape": "VM.Standard.E2.1.Micro", "lifecycle-state": "STOPPED",
     "time-created": "2026-11-12T10:00:00.000Z",
     "shape-config": {"ocpus": 1, "memory-in-gbs": 1}},
]

FAKE_VOLUMES_RAW = [
    {"id": FAKE_VOLUME, "display-name": "boot-volume-a1",
     "size-in-gbs": 47, "lifecycle-state": "AVAILABLE",
     "availability-domain": "AD-1", "vpus-per-gb": 10,
     "time-created": "2026-11-11T10:00:00.000Z"},
    {"id": FAKE_VOLUME_2, "display-name": "boot-volume-orphan",
     "size-in-gbs": 50, "lifecycle-state": "AVAILABLE",
     "availability-domain": "AD-1",
     "time-created": "2026-11-12T10:00:00.000Z"},
]


class Phase5eShapesTestCase(unittest.TestCase):
    """API 测试基类：建 app、临时 DATA_DIR、登录辅助方法。"""

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
        # Clear the server cache (instance list / cloud report) so cached data does not leak across tests. 清服务端缓存（实例列表 / 云报告），避免缓存跨测试串数据。
        clear_instance_list_cache()
        clear_volumes_cache()
        clear_cloud_report_cache()
        LOGIN_GUARD.reset()
        SESSIONS.reset()
        self.client = TestClient(self.app)
        self.anon = TestClient(self.app)

    def tearDown(self):
        self._data_dir_patcher.stop()
        self._tmp.cleanup()

    def _login(self):
        return self.client.post("/api/auth/login",
                                json={"username": "admin",
                                      "password": TEST_PASSWORD})


class TestInstanceShape(Phase5eShapesTestCase):
    """GET /api/instances 的 面板形状。"""

    def setUp(self):
        super().setUp()
        self._login()

    @patch("web.routes.instances.get_public_ip", return_value=FAKE_IP)
    @patch("web.routes.instances.list_instances",
           return_value=FAKE_INSTANCES_RAW)
    def test_shape_fields(self, mock_list, mock_ip):
        """新字段齐全：ocpus/memoryInGBs 取自 shape-config。"""
        r = self.client.get("/api/instances")
        self.assertEqual(r.status_code, 200)
        instances = r.json()["instances"]
        self.assertEqual(len(instances), 1)
        inst = instances[0]
        self.assertEqual(inst["id"], FAKE_INSTANCE)
        self.assertEqual(inst["name"], "instance-a1")
        self.assertEqual(inst["publicIp"], FAKE_IP)
        self.assertEqual(inst["shape"], "VM.Standard.A1.Flex")
        self.assertEqual(inst["state"], "RUNNING")
        self.assertEqual(inst["ocpus"], 2)
        self.assertEqual(inst["memoryInGBs"], 12)
        self.assertEqual(inst["region"], "ap-singapore-1")
        self.assertEqual(inst["timeCreated"], "2026-11-11T10:00:00.000Z")

    @patch("web.routes.instances.get_public_ip", return_value=None)
    @patch("web.routes.instances.list_instances",
           return_value=FAKE_INSTANCES_RAW)
    def test_stopped_filtered_out(self, mock_list, mock_ip):
        """STOPPED 实例不在列表里。"""
        r = self.client.get("/api/instances")
        ids = [i["id"] for i in r.json()["instances"]]
        self.assertNotIn(FAKE_INSTANCE_2, ids)

    @patch("web.routes.instances.get_public_ip", return_value=None)
    @patch("web.routes.instances.list_instances",
           return_value=FAKE_INSTANCES_RAW + [
               {"id": FAKE_INSTANCE_3, "display-name": "instance-gone",
                "shape": "VM.Standard.E2.1.Micro",
                "lifecycle-state": "TERMINATED",
                "time-created": "2026-11-13T10:00:00.000Z",
                "shape-config": {"ocpus": 1, "memory-in-gbs": 1}}])
    def test_state_all_includes_stopped_not_terminated(self, mock_list,
                                                       mock_ip):
        """?state=all 含 STOPPED（可从面板启动），仍排除 TERMINATED。"""
        r = self.client.get("/api/instances?state=all")
        self.assertEqual(r.status_code, 200)
        ids = [i["id"] for i in r.json()["instances"]]
        self.assertIn(FAKE_INSTANCE, ids)
        self.assertIn(FAKE_INSTANCE_2, ids)
        self.assertNotIn(FAKE_INSTANCE_3, ids)

    def test_anon_401(self):
        r = self.anon.get("/api/instances")
        self.assertEqual(r.status_code, 401)


class TestVolumeShape(Phase5eShapesTestCase):
    """GET /api/volumes 的 面板形状。"""

    def setUp(self):
        super().setUp()
        self._login()

    @patch("web.routes.volumes.list_instances",
           return_value=[{"id": FAKE_INSTANCE, "display-name": "instance-a1"}])
    @patch("web.routes.volumes.list_boot_volume_attachments",
           return_value=[{"id": FAKE_ATTACHMENT,
                          "boot-volume-id": FAKE_VOLUME,
                          "instance-id": FAKE_INSTANCE,
                          "lifecycle-state": "ATTACHED"}])
    @patch("web.routes.volumes.list_boot_volumes",
           return_value=FAKE_VOLUMES_RAW)
    def test_attached_volume_shape(self, mock_list, mock_atts, mock_insts):
        """已挂载盘：attachmentId/instanceId/instanceName/attached 齐全。"""
        r = self.client.get("/api/volumes")
        self.assertEqual(r.status_code, 200)
        volumes = r.json()["volumes"]
        self.assertEqual(len(volumes), 2)
        vol = volumes[0]
        self.assertEqual(vol["volumeId"], FAKE_VOLUME)
        self.assertEqual(vol["attachmentId"], FAKE_ATTACHMENT)
        self.assertEqual(vol["displayName"], "boot-volume-a1")
        self.assertEqual(vol["sizeInGBs"], 47)
        self.assertEqual(vol["vpusPerGB"], 10)
        self.assertEqual(vol["lifecycleState"], "AVAILABLE")
        self.assertEqual(vol["instanceId"], FAKE_INSTANCE)
        self.assertEqual(vol["instanceName"], "instance-a1")
        self.assertTrue(vol["attached"])
        self.assertEqual(vol["attachmentState"], "ATTACHED")
        self.assertEqual(vol["availabilityDomain"], "AD-1")
        self.assertEqual(vol["timeCreated"], "2026-11-11T10:00:00.000Z")
        self.assertFalse(volumes[1]["attached"])
        self.assertIsNone(volumes[1]["instanceId"])
        # The attachment query is per AD (both volumes share one AD, so one call) and carries include_id=True. 挂载查询按 AD 调（两盘同 AD，只查一次），且带 include_id=True。
        mock_atts.assert_called_once()
        _args, kwargs = mock_atts.call_args
        self.assertTrue(kwargs.get("include_id"))

    @patch("web.routes.volumes.list_instances",
           side_effect=OCIError("boom"))
    @patch("web.routes.volumes.list_boot_volume_attachments",
           side_effect=OCIError("boom"))
    @patch("web.routes.volumes.list_boot_volumes",
           return_value=FAKE_VOLUMES_RAW)
    def test_enrichment_failure_degrades(self, mock_list, mock_atts,
                                         mock_insts):
        """挂载/实例查询失败时列表仍返回，挂载信息为空（降级）。"""
        r = self.client.get("/api/volumes")
        self.assertEqual(r.status_code, 200)
        volumes = r.json()["volumes"]
        self.assertEqual(len(volumes), 2)
        self.assertFalse(volumes[0]["attached"])
        self.assertIsNone(volumes[0]["instanceName"])

    def test_anon_401(self):
        r = self.anon.get("/api/volumes")
        self.assertEqual(r.status_code, 401)


class TestVolumesCache(Phase5eShapesTestCase):
    """GET /api/volumes 与 /api/volumes/orphans 的服务端 30 秒缓存
    （v1.41.0：冷加载约 5 秒，缓存后重复访问直接返回）。"""

    def setUp(self):
        super().setUp()
        self._login()

    @patch("web.routes.volumes.list_instances", return_value=[])
    @patch("web.routes.volumes.list_boot_volume_attachments",
           return_value=[])
    @patch("web.routes.volumes.list_boot_volumes",
           return_value=FAKE_VOLUMES_RAW)
    def test_second_list_served_from_cache(self, mock_list, mock_atts,
                                           mock_insts):
        """30 秒内第二次请求不重新查 OCI。"""
        r1 = self.client.get("/api/volumes")
        r2 = self.client.get("/api/volumes")
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r1.json(), r2.json())
        mock_list.assert_called_once()

    @patch("web.routes.volumes.list_instances", return_value=[])
    @patch("web.routes.volumes.list_boot_volume_attachments",
           return_value=[])
    @patch("web.routes.volumes.list_boot_volumes",
           return_value=FAKE_VOLUMES_RAW)
    def test_refresh_bypasses_cache(self, mock_list, mock_atts,
                                    mock_insts):
        """refresh=1 绕过缓存，重新查 OCI。"""
        self.client.get("/api/volumes")
        self.client.get("/api/volumes?refresh=1")
        self.assertEqual(mock_list.call_count, 2)

    @patch("web.routes.volumes.list_instances", return_value=[])
    @patch("web.routes.volumes.list_boot_volume_attachments",
           return_value=[])
    @patch("web.routes.volumes.list_boot_volumes",
           return_value=FAKE_VOLUMES_RAW)
    @patch("web.routes.volumes.delete_boot_volume", return_value=None)
    def test_delete_clears_cache(self, mock_delete, mock_list, mock_atts,
                                 mock_insts):
        """删除启动盘后缓存清掉，下一次列表重新查 OCI。"""
        self.client.get("/api/volumes")
        r = self.client.delete(f"/api/volumes/{FAKE_VOLUME}")
        self.assertEqual(r.status_code, 200)
        self.client.get("/api/volumes")
        self.assertEqual(mock_list.call_count, 2)

    @patch("web.routes.volumes.list_instances",
           return_value=[{"id": FAKE_INSTANCE,
                          "display-name": "instance-a1"}])
    @patch("web.routes.volumes.list_boot_volume_attachments",
           return_value=[{"id": FAKE_ATTACHMENT,
                          "boot-volume-id": FAKE_VOLUME,
                          "instance-id": FAKE_INSTANCE,
                          "lifecycle-state": "ATTACHED"}])
    @patch("web.routes.volumes.list_boot_volumes",
           return_value=FAKE_VOLUMES_RAW)
    def test_orphans_derives_from_volumes_list(self, mock_list, mock_atts,
                                               mock_insts):
        """孤儿盘复用启动盘列表再过滤（只返回未挂载的 AVAILABLE 盘），
        且与列表接口共用缓存（两次请求只查一次 OCI）。"""
        r = self.client.get("/api/volumes/orphans")
        self.assertEqual(r.status_code, 200)
        orphans = r.json()["volumes"]
        self.assertEqual(len(orphans), 1)
        self.assertEqual(orphans[0]["volumeId"], FAKE_VOLUME_2)
        self.client.get("/api/volumes")
        mock_list.assert_called_once()

    @patch("web.routes.volumes.list_instances", return_value=[])
    @patch("web.routes.volumes.list_boot_volume_attachments",
           return_value=[])
    @patch("web.routes.volumes.list_boot_volumes",
           return_value=FAKE_VOLUMES_RAW)
    def test_orphans_refresh_bypasses_cache(self, mock_list, mock_atts,
                                            mock_insts):
        """孤儿盘 refresh=1 绕过缓存，重新查 OCI。"""
        self.client.get("/api/volumes/orphans")
        self.client.get("/api/volumes/orphans?refresh=1")
        self.assertEqual(mock_list.call_count, 2)

    def test_clear_volumes_cache_profile_scoped(self):
        """按 profile 清缓存时其他账号的条目不受影响。"""
        from web.routes import volumes as volumes_routes

        volumes_routes._VOLUMES_CACHE[("DEFAULT", "ap-singapore-1",
                                       "list")] = {"expires": 999999,
                                                   "payload": {}}
        volumes_routes._VOLUMES_CACHE[("ACCOUNT2", "us-sanjose-1",
                                       "list")] = {"expires": 999999,
                                                   "payload": {}}
        clear_volumes_cache("DEFAULT")
        self.assertNotIn(("DEFAULT", "ap-singapore-1", "list"),
                         volumes_routes._VOLUMES_CACHE)
        self.assertIn(("ACCOUNT2", "us-sanjose-1", "list"),
                      volumes_routes._VOLUMES_CACHE)
        clear_volumes_cache()
    """GET /api/quota/a1 的 面板形状。"""

    def setUp(self):
        super().setUp()
        self._login()

    @patch("web.routes.quota.get_a1_usage")
    def test_under_limit(self, mock_usage):
        mock_usage.return_value = {
            "ocpu_used": 2, "ocpu_limit": 4,
            "memory_used_gb": 12, "memory_limit_gb": 24,
            "instances": [{"id": FAKE_INSTANCE, "display_name": "a1",
                           "ocpus": 2, "memory_gb": 12}],
        }
        r = self.client.get("/api/quota/a1")
        self.assertEqual(r.status_code, 200)
        a1 = r.json()["a1"]
        # cap is the free-tier cap 4/24, not current usage. cap 是免费上限 4/24，不是当前用量。
        self.assertEqual(a1["capOcpu"], 4)
        self.assertEqual(a1["capMem"], 24)
        self.assertEqual(a1["usedOcpu"], 2)
        self.assertEqual(a1["usedMem"], 12)
        self.assertFalse(a1["over"])
        self.assertEqual(a1["overOcpu"], 0)
        self.assertEqual(a1["overMem"], 0)
        self.assertEqual(a1["instances"], [{
            "id": FAKE_INSTANCE, "name": "a1", "ocpus": 2,
            "memoryInGBs": 12, "state": "RUNNING",
        }])

    @patch("web.routes.quota.get_a1_usage")
    def test_over_limit(self, mock_usage):
        mock_usage.return_value = {
            "ocpu_used": 5, "ocpu_limit": 4,
            "memory_used_gb": 30, "memory_limit_gb": 24,
            "instances": [],
        }
        r = self.client.get("/api/quota/a1")
        a1 = r.json()["a1"]
        self.assertTrue(a1["over"])
        self.assertEqual(a1["overOcpu"], 1)
        self.assertEqual(a1["overMem"], 6)

    def test_anon_401(self):
        r = self.anon.get("/api/quota/a1")
        self.assertEqual(r.status_code, 401)


class TestHealthShape(Phase5eShapesTestCase):
    """GET /api/system/health 的 面板形状。"""

    def setUp(self):
        super().setUp()
        self._login()

    def test_health_fields(self):
        """各字段存在且类型正确（读本机 /proc，不碰网络）。"""
        r = self.client.get("/api/system/health")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        cpu = body["cpu"]
        self.assertIn("model", cpu)
        self.assertIsInstance(cpu["model"], str)
        self.assertGreater(cpu["cores"], 0)
        self.assertGreaterEqual(cpu["usagePercent"], 0)
        self.assertLessEqual(cpu["usagePercent"], 100)
        mem = body["memory"]
        self.assertGreater(mem["totalGB"], 0)
        self.assertGreaterEqual(mem["usedGB"], 0)
        self.assertGreaterEqual(mem["freeGB"], 0)
        self.assertGreaterEqual(mem["usagePercent"], 0)
        self.assertLessEqual(mem["usagePercent"], 100)
        disk = body["disk"]
        self.assertGreater(disk["totalGB"], 0)
        self.assertGreaterEqual(disk["usagePercent"], 0)
        net = body["network"]
        self.assertIn("rxBytesPerSec", net)
        self.assertIn("txBytesPerSec", net)
        self.assertGreater(net["sampledAt"], 0)
        self.assertGreaterEqual(body["processRssInMB"], 0)
        self.assertGreaterEqual(body["uptimeSeconds"], 0)

    def test_system_endpoint_unchanged(self):
        """原 /api/system 保持旧格式不变（顶栏资源栏用）。"""
        r = self.client.get("/api/system")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("percent", body["cpu"])
        self.assertIn("total_mb", body["memory"])

    def test_anon_401(self):
        r = self.anon.get("/api/system/health")
        self.assertEqual(r.status_code, 401)


class TestMonitorRules(Phase5eShapesTestCase):
    """GET /api/monitor/rules：复用 tgbot 监控开关。"""

    def setUp(self):
        super().setUp()
        self._login()

    @patch("web.routes.monitor.load_keepalive", return_value={})
    @patch("web.routes.monitor.load_traffic_guard",
           return_value={"enabled": False})
    def test_all_off(self, mock_guard, mock_keep):
        r = self.client.get("/api/monitor/rules")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["monitorEnabled"])
        self.assertFalse(body["autoOpenEnabled"])
        self.assertEqual(body["rules"], [])

    @patch("web.routes.monitor.load_keepalive",
           return_value={FAKE_INSTANCE: True})
    @patch("web.routes.monitor.load_traffic_guard",
           return_value={"enabled": False})
    def test_keepalive_on(self, mock_guard, mock_keep):
        """任一实例开停机保活 -> monitorEnabled 与 autoOpenEnabled 都为真。"""
        r = self.client.get("/api/monitor/rules")
        body = r.json()
        self.assertTrue(body["monitorEnabled"])
        self.assertTrue(body["autoOpenEnabled"])
        self.assertEqual(body["rules"], [])

    @patch("web.routes.monitor.load_keepalive", return_value={})
    @patch("web.routes.monitor.load_traffic_guard",
           return_value={"enabled": True})
    def test_traffic_guard_on(self, mock_guard, mock_keep):
        """流量守护开 -> monitorEnabled 真，autoOpenEnabled 假。"""
        r = self.client.get("/api/monitor/rules")
        body = r.json()
        self.assertTrue(body["monitorEnabled"])
        self.assertFalse(body["autoOpenEnabled"])

    def test_anon_401(self):
        r = self.anon.get("/api/monitor/rules")
        self.assertEqual(r.status_code, 401)


class TestOciLayerParams(unittest.TestCase):
    """oci 层为形状对齐新增的参数（默认行为不变）。"""

    def test_list_instances_include_shape_config(self):
        cli = MagicMock()
        cli.run.return_value = {"data": [{
            "id": FAKE_INSTANCE, "display-name": "a1",
            "shape": "VM.Standard.A1.Flex", "lifecycle-state": "RUNNING",
            "time-created": "2026-11-11T10:00:00.000Z",
            "shape-config": {"ocpus": 2, "memory-in-gbs": 12},
            "region": "ap-singapore-1",
        }]}
        result = list_instances(cli, FAKE_COMPARTMENT,
                                include_shape_config=True)
        self.assertEqual(result[0]["shape-config"]["ocpus"], 2)
        self.assertNotIn("region", result[0])
        # By default no shape-config is included (the existing slim shape is unchanged). 默认不带 shape-config（原有精简形状不变）。
        result2 = list_instances(cli, FAKE_COMPARTMENT)
        self.assertNotIn("shape-config", result2[0])

    def test_list_boot_volumes_keeps_time_created(self):
        cli = MagicMock()
        cli.run.return_value = {"data": [{
            "id": FAKE_VOLUME, "display-name": "bv",
            "size-in-gbs": 47, "lifecycle-state": "AVAILABLE",
            "availability-domain": "AD-1",
            "time-created": "2026-11-11T10:00:00.000Z",
        }]}
        result = list_boot_volumes(cli, FAKE_COMPARTMENT)
        self.assertEqual(result[0]["time-created"],
                         "2026-11-11T10:00:00.000Z")

    def test_list_attachments_include_id(self):
        cli = MagicMock()
        cli.run.return_value = {"data": [{
            "id": FAKE_ATTACHMENT, "boot-volume-id": FAKE_VOLUME,
            "instance-id": FAKE_INSTANCE, "lifecycle-state": "ATTACHED",
        }]}
        result = list_boot_volume_attachments(cli, FAKE_COMPARTMENT, "AD-1",
                                              include_id=True)
        self.assertEqual(result[0]["id"], FAKE_ATTACHMENT)
        # By default no id is included (the existing slim shape is unchanged). 默认不带 id（原有精简形状不变）。
        result2 = list_boot_volume_attachments(cli, FAKE_COMPARTMENT, "AD-1")
        self.assertNotIn("id", result2[0])


if __name__ == "__main__":
    unittest.main()
