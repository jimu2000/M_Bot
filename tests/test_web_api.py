#!/usr/bin/env python3
"""Web 面板 API 的集成测试（TestClient + mock oci 函数，不碰真实 API）。

运行：python3 -m unittest tests.test_web_api -v

注意：
  - 测试密码哈希在 setUpClass 里生成并 patch 进 web.config，
    不使用任何真实密码。
  - 主机 CRUD 测试用临时 DATA_DIR，不污染真实数据目录。
  - 所有写操作（启停/终止/扩容/删盘/抢机）全部 mock，不碰真实生产实例。
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import ANY, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from oci.client import NotFoundError, RateLimitError
from oci.grabber import GrabResult
from oci.instance import slim_instances
from tgbot import task_store
from tgbot.cpu_policy import BootKeys
from web import config
from web import deps as deps_mod
from web.app import create_app
from web.auth import LOGIN_GUARD, SESSIONS, hash_password
from web.deps import effective_region, get_profile_regions, resolve_profile
from web.oci_cache import clear_oci_cache
from web.routes.cloudreport import clear_cloud_report_cache
from web.routes.instances import clear_instance_list_cache
from web.routes.volumes import clear_volumes_cache
from web.routes.boot_backups import clear_boot_backups_cache
from web.routes.objectstorage import clear_objectstorage_cache
from web.routes.quota import clear_quota_cache
from web import tasks as tasks_mod
from web.tasks import TASKS, _run_grabber

# Placeholders (no real OCIDs / IPs appear in tests). 占位符（测试里不出现真实 OCID / IP）。
FAKE_INSTANCE = "ocid1.instance.oc1..testxxxx"
FAKE_INSTANCE_2 = "ocid1.instance.oc1..testyyyy"
FAKE_VOLUME = "ocid1.bootvolume.oc1..testxxxx"
FAKE_IMAGE = "ocid1.image.oc1..testxxxx"
FAKE_SUBNET = "ocid1.subnet.oc1..testxxxx"
FAKE_IP = "203.0.113.10"

TEST_PASSWORD = "test-password"

FAKE_INSTANCES = [
    {"id": FAKE_INSTANCE, "display-name": "instance-a1",
     "shape": "VM.Standard.A1.Flex", "lifecycle-state": "RUNNING",
     "time-created": "2026-11-11T10:00:00.000Z"},
    {"id": FAKE_INSTANCE_2, "display-name": "instance-e2",
     "shape": "VM.Standard.E2.1.Micro", "lifecycle-state": "STOPPED",
     "time-created": "2026-11-12T10:00:00.000Z"},
]

FAKE_VOLUMES = [
    {"id": FAKE_VOLUME, "display-name": "boot-volume-a1",
     "size-in-gbs": 47, "lifecycle-state": "AVAILABLE",
     "availability-domain": "AD-1"},
]

FAKE_SUMMARY = {
    "a1": {"ocpu_used": 1, "ocpu_limit": 4, "ocpu_remaining": 3,
           "memory_used_gb": 6, "memory_limit_gb": 24,
           "memory_remaining_gb": 18, "instances": []},
    "e2": {"count": 1, "limit": 2, "remaining": 1, "instances": []},
    "boot_volume": {"used_gb": 47, "limit_gb": 200, "remaining_gb": 153,
                    "volumes": []},
    "overall_ok": True,
}

CREATE_BODY = {
    "shape": "VM.Standard.E2.1.Micro",
    "image_id": FAKE_IMAGE,
    "subnet_id": FAKE_SUBNET,
    "availability_domain": "AD-1",
    "display_name": "test-instance",
    "ssh_keys_file": "/tmp/test-keys.pub",
    "target_cpu": "7J13",
}


class BlockingGrabber:
    """抢机测试替身：run() 阻塞 30 秒（daemon 线程，不影响测试退出）。"""

    def __init__(self, *args, **kwargs):
        pass

    def run(self):
        time.sleep(30)
        return GrabResult(instance_id=FAKE_INSTANCE, attempts=1,
                          releases=0, elapsed_seconds=1.0)


class WebApiTestCase(unittest.TestCase):
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
        # Redirect the instance credential store to the same temp dir:
        # the create/terminate API tests run the real grabber thread
        # (which saves the root password on success) and the real
        # terminate path (which deletes it); both must stay in the temp
        # dir, not the real data directory. 凭据库指到同一临时目录：
        # 创建/终止接口测试跑真实抢机线程（成功后保存 root 密码）与真实
        # 终止路径（删凭据），都不能写真实数据目录。
        self._cred_patcher = patch(
            "tgbot.instance_credentials.CREDENTIALS_FILE",
            os.path.join(self._tmp.name, "instance_credentials.json"))
        self._cred_patcher.start()
        # Clear the server cache (instance list / cloud report) so cached data does not leak across tests. 清服务端缓存（实例列表 / 云报告），避免缓存跨测试串数据。
        clear_instance_list_cache()
        clear_oci_cache()
        clear_volumes_cache()
        clear_cloud_report_cache()
        clear_quota_cache()
        clear_boot_backups_cache()
        clear_objectstorage_cache()
        LOGIN_GUARD.reset()
        SESSIONS.reset()
        TASKS.reset()
        # Host-independent: never let a real OCI SDK call happen in web
        # tests. The cached_raw_* helpers call these SDK list methods;
        # patching them to [] makes tests that forget to mock cached_raw_*
        # pass on a host with no OCI config (e.g. clean HOME on .160),
        # where sdk.config.from_file would raise ConfigFileNotFound.
        # 主机无关：Web 测试里绝不真的调 OCI SDK。cached_raw_* 会调
        # 这三个 SDK 列表方法；把它们打成 []，漏 mock cached_raw_* 的
        # 测试在没有 OCI 配置的主机上也不会炸 ConfigFileNotFound。
        for _target in (
                "oci.client.OCIClient.sdk_list_instances",
                "oci.client.OCIClient.sdk_list_boot_volumes",
                "oci.client.OCIClient.sdk_list_vnic_attachments"):
            _p = patch(_target, return_value=[])
            _p.start()
            self.addCleanup(_p.stop)
        self.client = TestClient(self.app)
        self.anon = TestClient(self.app)

    def tearDown(self):
        # 等后台抢机线程收尾再删临时目录：_run_grabber 在任务结束后
        # 还要往 tasks.json 写完成日志；不等它就删目录，线程写盘时
        # 会撞上已删的目录（曾致 tearDown OSError: Directory not
        # empty）。等 5 秒，超时也不阻塞（写盘失败已被 TaskRegistry
        # 兜底，不再杀线程）。
        for thread in threading.enumerate():
            if getattr(thread, "_target", None) is _run_grabber:
                thread.join(timeout=5.0)
        self._data_dir_patcher.stop()
        self._cred_patcher.stop()
        self._tmp.cleanup()

    def _login(self, username="admin", password=TEST_PASSWORD):
        """登录并返回响应（成功时 TestClient 自动保存 cookie）。"""
        return self.client.post("/api/auth/login",
                                json={"username": username,
                                      "password": password})

    def _wait_task_status(self, task_id, statuses, timeout=5.0):
        """轮询任务直到状态进入 statuses，返回任务字典。"""
        deadline = time.time() + timeout
        task = None
        while time.time() < deadline:
            r = self.client.get(f"/api/tasks/{task_id}")
            task = r.json()["task"]
            if task["status"] in statuses:
                return task
            time.sleep(0.05)
        return task


class TestAuthApi(WebApiTestCase):
    """登录 / 登出 / 锁定。"""

    def test_login_success(self):
        r = self._login()
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.assertEqual(r.json()["username"], "admin")
        self.assertIn(config.SESSION_COOKIE, r.cookies)

    def test_me_after_login(self):
        self._login()
        r = self.client.get("/api/auth/me")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"ok": True, "username": "admin",
                                    "demoMode": False})

    def test_login_wrong_password(self):
        r = self._login(password="wrong-password")
        self.assertEqual(r.status_code, 401)
        self.assertFalse(r.json()["ok"])

    def test_login_wrong_username(self):
        r = self._login(username="not-admin")
        self.assertEqual(r.status_code, 401)

    def test_login_lockout_429(self):
        """5 次失败后第 6 次返回 429 + 剩余秒数，正确密码也被挡。"""
        for _ in range(5):
            r = self._login(password="wrong")
            self.assertEqual(r.status_code, 401)
        r = self._login(password="wrong")
        self.assertEqual(r.status_code, 429)
        self.assertFalse(r.json()["ok"])
        self.assertIn("remaining_seconds", r.json())
        self.assertGreater(r.json()["remaining_seconds"], 0)
        r = self._login()
        self.assertEqual(r.status_code, 429)

    def test_logout(self):
        self._login()
        r = self.client.post("/api/auth/logout")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        r = self.client.get("/api/auth/me")
        self.assertEqual(r.status_code, 401)

    def test_unauthenticated_api_401(self):
        """未登录调 API 返回 401（信封格式）。"""
        for method, path in [
            ("get", "/api/instances"),
            ("get", "/api/volumes"),
            ("get", "/api/quota"),
            ("get", "/api/tasks"),
            ("get", "/api/hosts"),
            ("get", "/api/system"),
            ("get", "/api/auth/me"),
        ]:
            r = getattr(self.anon, method)(path)
            self.assertEqual(r.status_code, 401, path)
            self.assertFalse(r.json()["ok"])
        r = self.anon.post(f"/api/instances/{FAKE_INSTANCE}/start")
        self.assertEqual(r.status_code, 401)


class TestInstancesApi(WebApiTestCase):
    """实例列表 / 详情 / 启停 / 重启 / 终止。"""

    def setUp(self):
        super().setUp()
        # VNIC 缓存函数不 mock 就会真的调 OCI（第 7 轮新增调用点）：
        # 列表测试只关心端点形状，VNIC 数据一律给空值；接线由
        # test_list_passes_cached_vnic_data_to_bulk 单独验证。
        self._vnic_atts_patcher = patch(
            "web.routes.instances.cached_raw_vnic_attachments",
            return_value=[])
        self._vnic_ips_patcher = patch(
            "web.routes.instances.cached_vnic_public_ips",
            return_value={})
        self._vnic_atts_patcher.start()
        self._vnic_ips_patcher.start()
        self.addCleanup(self._vnic_atts_patcher.stop)
        self.addCleanup(self._vnic_ips_patcher.stop)
        self._login()

    @patch("web.routes.instances.attach_public_ips_bulk", return_value=[FAKE_IP])
    @patch("web.routes.instances.cached_raw_instances", return_value=FAKE_INSTANCES)
    def test_list_passes_cached_vnic_data_to_bulk(self, mock_list, mock_ip):
        """第 7 轮：列表把缓存的挂载列表与 IP 映射传给批量取 IP。"""
        r = self.client.get("/api/instances")
        self.assertEqual(r.status_code, 200)
        mock_ip.assert_called_once()
        _args, kwargs = mock_ip.call_args
        self.assertEqual(kwargs.get("attachments"), [])
        self.assertEqual(kwargs.get("ip_map"), {})

    @patch("web.routes.instances.attach_public_ips_bulk", return_value=[FAKE_IP])
    @patch("web.routes.instances.cached_raw_vnic_attachments",
           return_value=[])
    @patch("web.routes.instances.cached_raw_instances",
           return_value=FAKE_INSTANCES)
    def test_list_refresh_passes_refresh_to_both_sources(
            self, mock_list, mock_att, mock_ip):
        """第 2 轮：refresh=true 时实例列表与挂载列表都强制重取。

        两者并行取（ThreadPoolExecutor），refresh 标志必须透传给
        两边，否则并行化会悄悄把强制刷新降级成缓存命中。"""
        r = self.client.get("/api/instances?refresh=true")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["instances"]), 1)
        _args, kwargs = mock_list.call_args
        self.assertTrue(kwargs.get("refresh"))
        _args, kwargs = mock_att.call_args
        self.assertTrue(kwargs.get("refresh"))

    @patch("web.routes.instances.attach_public_ips_bulk", return_value=[FAKE_IP])
    @patch("web.routes.instances.cached_raw_instances", return_value=FAKE_INSTANCES)
    @patch("web.routes.instances.slim_instances", wraps=slim_instances)
    def test_list_instances(self, mock_slim, mock_list, mock_ip):
        """面板形状：只含 RUNNING 实例，新字段齐全，旧字段双写。"""
        r = self.client.get("/api/instances")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(len(body["instances"]), 1)
        inst = body["instances"][0]
        self.assertEqual(inst["id"], FAKE_INSTANCE)
        self.assertEqual(inst["name"], "instance-a1")
        self.assertEqual(inst["publicIp"], FAKE_IP)
        self.assertEqual(inst["shape"], "VM.Standard.A1.Flex")
        self.assertEqual(inst["state"], "RUNNING")
        self.assertEqual(inst["region"], "ap-singapore-1")
        self.assertEqual(inst["timeCreated"], "2026-11-11T10:00:00.000Z")
        self.assertIsNone(inst["ocpus"])
        self.assertIsNone(inst["memoryInGBs"])
        self.assertEqual(inst["public-ip"], FAKE_IP)
        self.assertEqual(inst["display-name"], "instance-a1")
        self.assertEqual(inst["lifecycle-state"], "RUNNING")
        mock_list.assert_called_once()
        # 路由现在把 include_shape_config 交给 slim_instances（第 5 轮去重后
        # 原始列表走共享缓存，字段裁剪仍由 slim_instances 做）。
        _args, kwargs = mock_slim.call_args
        self.assertTrue(kwargs.get("include_shape_config"))

    @patch("web.routes.instances.attach_public_ips_bulk", return_value=[FAKE_IP])
    @patch("web.routes.instances.cached_raw_instances", return_value=FAKE_INSTANCES)
    @patch("web.routes.instances.resolve_profile")
    def test_list_instances_region_param(self, mock_resolve, mock_list,
                                         mock_ip):
        """?region= 透传给 resolve_profile，响应带回生效的区域。"""
        mock_cli = MagicMock()
        mock_cli.region = "ap-seoul-1"
        mock_resolve.return_value = (
            mock_cli, "ocid1.tenancy.oc1..testxxxx")
        r = self.client.get("/api/instances?region=ap-seoul-1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["region"], "ap-seoul-1")
        mock_resolve.assert_called_once_with(None, "ap-seoul-1")

    @patch("web.deps.get_profile_regions",
           return_value={"DEFAULT": ["ap-singapore-1"]})
    def test_list_instances_unsubscribed_region_400(self, mock_regions):
        """账号未订阅的区域返回 400（不透传给 OCI）。"""
        r = self.client.get("/api/instances?region=eu-frankfurt-1")
        self.assertEqual(r.status_code, 400)
        self.assertIn("未订阅", r.json()["error"])

    @patch("web.routes.instances.attach_public_ips_bulk", return_value=[None])
    @patch("web.routes.instances.cached_raw_instances", return_value=FAKE_INSTANCES)
    def test_list_instances_ip_failure_tolerated(self, mock_list, mock_ip):
        """单台实例取 IP 失败时列表仍返回，IP 为 None。"""
        r = self.client.get("/api/instances")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["instances"][0]["public-ip"])
        self.assertIsNone(r.json()["instances"][0]["publicIp"])

    @patch("web.routes.instances.get_boot_volume",
           return_value={"id": FAKE_VOLUME, "size-in-gbs": 47,
                         "vpus-per-gb": 10, "lifecycle-state": "AVAILABLE"})
    @patch("web.routes.instances.list_boot_volume_attachments",
           return_value=[{"boot-volume-id": FAKE_VOLUME,
                          "instance-id": FAKE_INSTANCE,
                          "lifecycle-state": "ATTACHED"}])
    @patch("web.routes.instances.list_ipv6_for_vnic",
           return_value=[{"ip-address": "2603:c024:c021:1d01::1"}])
    @patch("web.routes.instances.list_vnics",
           return_value=[{"id": "ocid1.vnic.oc1..testxxxx",
                          "public-ip": FAKE_IP,
                          "private-ip": "10.0.0.5",
                          "is-primary": True}])
    @patch("web.routes.instances.get_public_ip", return_value=FAKE_IP)
    @patch("web.routes.instances.get_instance")
    def test_get_instance(self, mock_get, mock_ip, mock_vnics, mock_ipv6,
                          mock_atts, mock_vol):
        mock_get.return_value = {"data": {"id": FAKE_INSTANCE,
                                          "display-name": "instance-a1",
                                          "lifecycle-state": "RUNNING",
                                          "availability-domain": "AD-1"}}
        r = self.client.get(f"/api/instances/{FAKE_INSTANCE}")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["instance"]["display-name"], "instance-a1")
        self.assertEqual(body["public_ip"], FAKE_IP)
        self.assertEqual(body["private_ip"], "10.0.0.5")
        self.assertEqual(body["ipv6_addresses"],
                         ["2603:c024:c021:1d01::1"])
        self.assertEqual(body["boot_volume_size_gbs"], 47)
        self.assertEqual(body["boot_volume_id"], FAKE_VOLUME)
        self.assertEqual(body["boot_volume_vpus_per_gb"], 10)
        self.assertEqual(body["boot_volume_state"], "AVAILABLE")
        self.assertEqual(body["boot_volume_attachment_state"], "ATTACHED")

    @patch("web.routes.instances.get_instance",
           side_effect=NotFoundError("not found"))
    def test_get_instance_not_found_404(self, mock_get):
        r = self.client.get(f"/api/instances/{FAKE_INSTANCE}")
        self.assertEqual(r.status_code, 404)
        self.assertFalse(r.json()["ok"])

    @patch("web.routes.instances.cached_raw_instances",
           side_effect=RateLimitError("429 TooManyRequests"))
    def test_list_rate_limited_429(self, mock_list):
        r = self.client.get("/api/instances")
        self.assertEqual(r.status_code, 429)

    def test_unknown_profile_400(self):
        r = self.client.get("/api/instances?profile=NOPE")
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.json()["ok"])

    @patch("web.routes.instances.start_instance")
    def test_start(self, mock_start):
        r = self.client.post(f"/api/instances/{FAKE_INSTANCE}/start")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["action"], "start")
        mock_start.assert_called_once_with(ANY, FAKE_INSTANCE)

    @patch("web.routes.instances.stop_instance")
    def test_stop(self, mock_stop):
        r = self.client.post(f"/api/instances/{FAKE_INSTANCE}/stop")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["action"], "stop")
        mock_stop.assert_called_once_with(ANY, FAKE_INSTANCE)

    @patch("web.routes.instances.restart_instance")
    def test_restart(self, mock_restart):
        r = self.client.post(f"/api/instances/{FAKE_INSTANCE}/restart")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["action"], "restart")
        mock_restart.assert_called_once_with(ANY, FAKE_INSTANCE)

    @patch("web.routes.instances.terminate_instance")
    def test_terminate_without_confirm_400(self, mock_terminate):
        """终止缺 confirm 返回 400，且不调用终止函数。"""
        r = self.client.post(f"/api/instances/{FAKE_INSTANCE}/terminate",
                             json={})
        self.assertEqual(r.status_code, 400)
        r = self.client.post(f"/api/instances/{FAKE_INSTANCE}/terminate",
                             json={"confirm": False})
        self.assertEqual(r.status_code, 400)
        mock_terminate.assert_not_called()

    @patch("web.routes.instances.terminate_instance")
    def test_terminate_with_confirm(self, mock_terminate):
        """confirm=true 时终止（不传 preserve_boot_volume，用默认 False
        永久删引导卷）。"""
        r = self.client.post(f"/api/instances/{FAKE_INSTANCE}/terminate",
                             json={"confirm": True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["action"], "terminate")
        self.assertFalse(r.json()["preserve_boot_volume"])
        mock_terminate.assert_called_once_with(
            ANY, FAKE_INSTANCE, preserve_boot_volume=False)

    @patch("web.routes.instances.terminate_instance")
    def test_terminate_preserve_boot_volume(self, mock_terminate):
        """preserve_boot_volume=true 时终止并保留引导卷
        （前端“删除（保留启动盘）”用）。"""
        r = self.client.post(f"/api/instances/{FAKE_INSTANCE}/terminate",
                             json={"confirm": True,
                                   "preserve_boot_volume": True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["action"], "terminate")
        self.assertTrue(r.json()["preserve_boot_volume"])
        mock_terminate.assert_called_once_with(
            ANY, FAKE_INSTANCE, preserve_boot_volume=True)

    @patch("web.routes.instances.terminate_instance")
    def test_terminate_preserve_without_confirm_400(self, mock_terminate):
        """保留引导卷也要 confirm=true，缺确认返回 400 且不调用终止。"""
        r = self.client.post(f"/api/instances/{FAKE_INSTANCE}/terminate",
                             json={"preserve_boot_volume": True})
        self.assertEqual(r.status_code, 400)
        mock_terminate.assert_not_called()

    @patch("web.routes.instances.terminate_instance",
           side_effect=NotFoundError("not found"))
    def test_terminate_not_found_404(self, mock_terminate):
        r = self.client.post(f"/api/instances/{FAKE_INSTANCE}/terminate",
                             json={"confirm": True})
        self.assertEqual(r.status_code, 404)


class TestWatchdogApi(WebApiTestCase):
    """v1.21.0：实例详情里的流量看门狗状态与开关。"""

    def setUp(self):
        super().setUp()
        self._login()

    PROBE_ENABLED = ("SCRIPT=1\nAUTO=1\nCRON=1\nMODE=0\n"
                     "TRIPPED=0\nLIMITED=0\n")
    PROBE_NOT_INSTALLED = "SCRIPT=0\nAUTO=0\nCRON=0\nTRIPPED=0\nLIMITED=0\n"

    def _patch_host(self):
        """mock 公网 IP + SSH 密钥（_watchdog_host 能建成）。"""
        return (patch("web.routes.instances._safe_public_ip",
                      return_value=FAKE_IP),
                patch("tgbot.config.GUARD_SSH_KEY",
                      "/root/.ssh/rbot_guard"))

    def test_get_status_enabled(self):
        ip_patch, key_patch = self._patch_host()
        with ip_patch, key_patch, patch(
                "web.routes.instances.run_remote_command",
                return_value=(0, self.PROBE_ENABLED, "")):
            r = self.client.get(f"/api/instances/{FAKE_INSTANCE}/watchdog")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["watchdog"]["enabled"])
        self.assertEqual(body["watchdog"]["mode"], 0)
        self.assertIn("阻断模式", body["label"])

    def test_get_status_not_installed(self):
        ip_patch, key_patch = self._patch_host()
        with ip_patch, key_patch, patch(
                "web.routes.instances.run_remote_command",
                return_value=(0, self.PROBE_NOT_INSTALLED, "")):
            r = self.client.get(f"/api/instances/{FAKE_INSTANCE}/watchdog")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["watchdog"]["installed"])
        self.assertIn("未安装", r.json()["label"])

    def test_get_status_no_key(self):
        with patch("web.routes.instances._safe_public_ip",
                   return_value=FAKE_IP), \
                patch("tgbot.config.GUARD_SSH_KEY", ""):
            r = self.client.get(f"/api/instances/{FAKE_INSTANCE}/watchdog")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["watchdog"])

    def test_post_enable(self):
        ip_patch, key_patch = self._patch_host()
        with ip_patch, key_patch, patch(
                "web.routes.instances.install_watchdog",
                return_value={"action": "installed", "test_ok": True,
                              "mode": 0}) as mock_install:
            r = self.client.post(
                f"/api/instances/{FAKE_INSTANCE}/watchdog",
                json={"enabled": True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["action"], "installed")
        mock_install.assert_called_once()

    def test_post_reenable(self):
        ip_patch, key_patch = self._patch_host()
        with ip_patch, key_patch, patch(
                "web.routes.instances.install_watchdog",
                return_value={"action": "enabled", "test_ok": True,
                              "mode": 0}) as mock_install:
            r = self.client.post(
                f"/api/instances/{FAKE_INSTANCE}/watchdog",
                json={"enabled": True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["action"], "enabled")
        self.assertIn("重新开启", r.json()["message"])
        self.assertNotIn("升级", r.json()["message"])
        mock_install.assert_called_once()

    def test_post_disable(self):
        ip_patch, key_patch = self._patch_host()
        with ip_patch, key_patch, patch(
                "web.routes.instances.disable_watchdog",
                return_value={"action": "disabled"}) as mock_off:
            r = self.client.post(
                f"/api/instances/{FAKE_INSTANCE}/watchdog",
                json={"enabled": False})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["action"], "disabled")
        mock_off.assert_called_once()

    def test_post_no_bark_400(self):
        from tgbot.watchdog import WatchdogError
        ip_patch, key_patch = self._patch_host()
        with ip_patch, key_patch, patch(
                "web.routes.instances.install_watchdog",
                side_effect=WatchdogError("机器人还没配 Bark")):
            r = self.client.post(
                f"/api/instances/{FAKE_INSTANCE}/watchdog",
                json={"enabled": True})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Bark", r.json()["error"])


class TestCpuModelApi(WebApiTestCase):
    """v1.24.0：实例 CPU 型号（列表 cpuModel / 详情 cpu_model / 强制重测）。"""

    def setUp(self):
        super().setUp()
        self._login()

    def _patch_host(self):
        """mock 公网 IP + SSH 密钥（_watchdog_host 能建成）。"""
        return (patch("web.routes.instances._safe_public_ip",
                      return_value=FAKE_IP),
                patch("tgbot.config.GUARD_SSH_KEY",
                      "/root/.ssh/rbot_guard"))

    @patch("web.routes.instances._refresh_cpu_models_background")
    @patch("web.routes.instances.load_os_versions",
           return_value={FAKE_INSTANCE: {"version": "Debian 13.7",
                                         "checked_at": "2026-11-28T00:00:00+00:00"}})
    @patch("web.routes.instances.load_cpu_models",
           return_value={FAKE_INSTANCE: {"model": "AMD EPYC 7J13",
                                         "checked_at": "2026-11-24T00:00:00+00:00"}})
    @patch("web.routes.instances.attach_public_ips_bulk", return_value=[FAKE_IP])
    @patch("web.routes.instances.cached_raw_instances", return_value=FAKE_INSTANCES)
    def test_list_includes_os_version(self, mock_list, mock_ip,
                                      mock_cache, mock_os, mock_bg):
        r = self.client.get("/api/instances")
        self.assertEqual(r.status_code, 200)
        inst = r.json()["instances"][0]
        self.assertEqual(inst["osVersion"], "Debian 13.7")

    @patch("web.routes.instances._refresh_cpu_models_background")
    @patch("web.routes.instances.load_cpu_models",
           return_value={FAKE_INSTANCE: {"model": "AMD EPYC 7J13",
                                         "checked_at": "2026-11-24T00:00:00+00:00"}})
    @patch("web.routes.instances.attach_public_ips_bulk", return_value=[FAKE_IP])
    @patch("web.routes.instances.cached_raw_instances", return_value=FAKE_INSTANCES)
    def test_list_includes_cpu_model(self, mock_list, mock_ip,
                                     mock_cache, mock_bg):
        r = self.client.get("/api/instances")
        self.assertEqual(r.status_code, 200)
        inst = r.json()["instances"][0]
        self.assertEqual(inst["cpuModel"], "AMD EPYC 7J13")
        mock_bg.assert_called_once()

    @patch("web.routes.instances._refresh_cpu_models_background")
    @patch("web.routes.instances.load_cpu_models", return_value={})
    @patch("web.routes.instances.attach_public_ips_bulk", return_value=[FAKE_IP])
    @patch("web.routes.instances.cached_raw_instances", return_value=FAKE_INSTANCES)
    def test_list_cpu_model_none_when_uncached(self, mock_list, mock_ip,
                                               mock_cache, mock_bg):
        r = self.client.get("/api/instances")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["instances"][0]["cpuModel"])

    @patch("web.routes.instances.get_cpu_model")
    @patch("web.routes.instances.cached_cpu_model",
           return_value="AMD EPYC 7742")
    @patch("web.routes.instances.get_public_ip", return_value=FAKE_IP)
    @patch("web.routes.instances.get_instance")
    def test_detail_cpu_model_cached(self, mock_get, mock_ip,
                                     mock_cached, mock_fetch):
        mock_get.return_value = {"data": {"id": FAKE_INSTANCE,
                                          "display-name": "instance-e2",
                                          "lifecycle-state": "RUNNING",
                                          "availability-domain": "AD-1"}}
        r = self.client.get(f"/api/instances/{FAKE_INSTANCE}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["cpu_model"], "AMD EPYC 7742")
        mock_fetch.assert_not_called()

    @patch("web.routes.instances.get_cpu_model",
           return_value="Ampere Altra")
    @patch("web.routes.instances.cached_cpu_model", return_value=None)
    @patch("web.routes.instances.get_public_ip", return_value=FAKE_IP)
    @patch("web.routes.instances.get_instance")
    def test_detail_cpu_model_fetched_when_missing(self, mock_get, mock_ip,
                                                   mock_cached, mock_fetch):
        mock_get.return_value = {"data": {"id": FAKE_INSTANCE,
                                          "display-name": "instance-a1",
                                          "lifecycle-state": "RUNNING",
                                          "availability-domain": "AD-1"}}
        r = self.client.get(f"/api/instances/{FAKE_INSTANCE}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["cpu_model"], "Ampere Altra")
        mock_fetch.assert_called_once()

    @patch("web.routes.instances.get_os_version")
    @patch("web.routes.instances.cached_os_version",
           return_value="Debian 13.7")
    @patch("web.routes.instances.get_cpu_model",
           return_value="Ampere Altra")
    @patch("web.routes.instances.cached_cpu_model", return_value=None)
    @patch("web.routes.instances.get_public_ip", return_value=FAKE_IP)
    @patch("web.routes.instances.get_instance")
    def test_detail_os_version_cached(self, mock_get, mock_ip,
                                      mock_cached_cpu, mock_fetch_cpu,
                                      mock_cached_os, mock_fetch_os):
        mock_get.return_value = {"data": {"id": FAKE_INSTANCE,
                                          "display-name": "instance-a1",
                                          "lifecycle-state": "RUNNING",
                                          "availability-domain": "AD-1"}}
        r = self.client.get(f"/api/instances/{FAKE_INSTANCE}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["os_version"], "Debian 13.7")
        mock_fetch_os.assert_not_called()

    @patch("web.routes.instances.get_os_version",
           return_value="Debian 13.7")
    @patch("web.routes.instances.cached_os_version", return_value=None)
    @patch("web.routes.instances.get_cpu_model",
           return_value="Ampere Altra")
    @patch("web.routes.instances.cached_cpu_model", return_value=None)
    @patch("web.routes.instances.get_public_ip", return_value=FAKE_IP)
    @patch("web.routes.instances.get_instance")
    def test_detail_os_version_fetched_when_missing(self, mock_get, mock_ip,
                                                    mock_cached_cpu,
                                                    mock_fetch_cpu,
                                                    mock_cached_os,
                                                    mock_fetch_os):
        mock_get.return_value = {"data": {"id": FAKE_INSTANCE,
                                          "display-name": "instance-a1",
                                          "lifecycle-state": "RUNNING",
                                          "availability-domain": "AD-1"}}
        r = self.client.get(f"/api/instances/{FAKE_INSTANCE}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["os_version"], "Debian 13.7")
        mock_fetch_os.assert_called_once()

    def test_post_refresh_success(self):
        ip_patch, key_patch = self._patch_host()
        with ip_patch, key_patch, patch(
                "web.routes.instances.get_cpu_model",
                return_value="AMD EPYC 7J13") as mock_fetch:
            r = self.client.post(
                f"/api/instances/{FAKE_INSTANCE}/cpu-model")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["cpu_model"], "AMD EPYC 7J13")
        self.assertIn("AMD EPYC 7J13", body["message"])
        _args, kwargs = mock_fetch.call_args
        self.assertTrue(kwargs.get("force"))

    def test_post_refresh_ssh_failure(self):
        ip_patch, key_patch = self._patch_host()
        with ip_patch, key_patch, patch(
                "web.routes.instances.get_cpu_model", return_value=None):
            r = self.client.post(
                f"/api/instances/{FAKE_INSTANCE}/cpu-model")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["cpu_model"])
        self.assertIn("失败", r.json()["message"])

    def test_post_refresh_no_key(self):
        with patch("web.routes.instances._safe_public_ip",
                   return_value=FAKE_IP), \
                patch("tgbot.config.GUARD_SSH_KEY", ""):
            r = self.client.post(
                f"/api/instances/{FAKE_INSTANCE}/cpu-model")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["cpu_model"])

    @patch("web.routes.instances.load_cpu_models",
           return_value={FAKE_INSTANCE: {"model": "AMD EPYC 7J13",
                                         "checked_at": "2026-11-28T00:00:00+00:00"},
                         FAKE_INSTANCE_2: {"model": None},
                         "ocid1.instance.oc1..testzzzz": {}})
    def test_cpu_models_endpoint_reads_cache_only(self, mock_load):
        # The delayed CPU-model refetch on the instances page uses this lightweight endpoint: it only reads the cache file, never calls OCI, and only returns instances that actually have a model. 实例页延迟补拉用的轻量接口：只读缓存文件、不调 OCI，只返回确实有型号的实例。
        r = self.client.get("/api/instances/cpu-models")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(),
                         {"ok": True,
                          "cpu_models": {FAKE_INSTANCE: "AMD EPYC 7J13"}})
        mock_load.assert_called_once_with()

    @patch("web.routes.instances.load_cpu_models", return_value={})
    def test_cpu_models_endpoint_empty_cache(self, mock_load):
        r = self.client.get("/api/instances/cpu-models")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"ok": True, "cpu_models": {}})

    def test_cpu_models_endpoint_requires_login(self):
        r = self.anon.get("/api/instances/cpu-models")
        self.assertEqual(r.status_code, 401)


class TestVolumesApi(WebApiTestCase):
    """磁盘列表 / 详情 / 扩容 / 删除 / 孤儿盘。"""

    def setUp(self):
        super().setUp()
        self._login()

    @patch("web.routes.volumes.cached_raw_instances", return_value=[])
    @patch("web.routes.volumes.list_boot_volume_attachments", return_value=[])
    @patch("web.routes.volumes.cached_raw_boot_volumes", return_value=FAKE_VOLUMES)
    def test_list_volumes(self, mock_list, mock_atts, mock_insts):
        """面板形状：新字段齐全，旧字段双写；无挂载时 attached=false。"""
        r = self.client.get("/api/volumes")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["volumes"]), 1)
        vol = r.json()["volumes"][0]
        self.assertEqual(vol["volumeId"], FAKE_VOLUME)
        self.assertEqual(vol["displayName"], "boot-volume-a1")
        self.assertEqual(vol["sizeInGBs"], 47)
        self.assertEqual(vol["lifecycleState"], "AVAILABLE")
        self.assertEqual(vol["availabilityDomain"], "AD-1")
        self.assertFalse(vol["attached"])
        self.assertIsNone(vol["attachmentId"])
        self.assertIsNone(vol["instanceId"])
        self.assertIsNone(vol["instanceName"])
        self.assertEqual(vol["size-in-gbs"], 47)
        self.assertEqual(vol["id"], FAKE_VOLUME)

    @patch("web.routes.volumes.get_boot_volume", return_value=FAKE_VOLUMES[0])
    def test_get_volume(self, mock_get):
        r = self.client.get(f"/api/volumes/{FAKE_VOLUME}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["volume"]["id"], FAKE_VOLUME)

    @patch("web.routes.volumes.cached_raw_instances", return_value=[])
    @patch("web.routes.volumes.list_boot_volume_attachments", return_value=[])
    @patch("web.routes.volumes.cached_raw_boot_volumes",
           return_value=FAKE_VOLUMES)
    def test_orphans(self, mock_list, mock_atts, mock_insts):
        """孤儿盘从启动盘列表过滤而来（无挂载记录的 AVAILABLE 盘），
        同样返回 面板形状。"""
        r = self.client.get("/api/volumes/orphans")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["volumes"]), 1)
        self.assertEqual(r.json()["volumes"][0]["volumeId"], FAKE_VOLUME)
        self.assertFalse(r.json()["volumes"][0]["attached"])

    @patch("web.routes.volumes.resize_boot_volume")
    @patch("web.routes.volumes.get_boot_volume", return_value=FAKE_VOLUMES[0])
    def test_resize_ok(self, mock_get, mock_resize):
        resized = dict(FAKE_VOLUMES[0], **{"size-in-gbs": 100})
        mock_resize.return_value = resized
        r = self.client.post(f"/api/volumes/{FAKE_VOLUME}/resize",
                             json={"size_in_gbs": 100})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["volume"]["size-in-gbs"], 100)
        mock_resize.assert_called_once_with(ANY, FAKE_VOLUME, 100)

    @patch("web.routes.volumes.resize_boot_volume")
    @patch("web.routes.volumes.get_boot_volume", return_value=FAKE_VOLUMES[0])
    def test_resize_not_larger_400(self, mock_get, mock_resize):
        """新容量不大于当前容量时返回 400，不调用扩容。"""
        for size in (47, 10):
            r = self.client.post(f"/api/volumes/{FAKE_VOLUME}/resize",
                                 json={"size_in_gbs": size})
            self.assertEqual(r.status_code, 400, size)
        mock_resize.assert_not_called()

    @patch("web.routes.volumes.delete_boot_volume")
    def test_delete_volume(self, mock_delete):
        r = self.client.delete(f"/api/volumes/{FAKE_VOLUME}")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        mock_delete.assert_called_once_with(ANY, FAKE_VOLUME)

    @patch("web.routes.volumes.get_boot_volume",
           side_effect=NotFoundError("not found"))
    def test_get_volume_not_found_404(self, mock_get):
        r = self.client.get(f"/api/volumes/{FAKE_VOLUME}")
        self.assertEqual(r.status_code, 404)


class TestQuotaApi(WebApiTestCase):
    """配额汇总 / can-launch。"""

    def setUp(self):
        super().setUp()
        self._login()

    @patch("web.routes.quota.get_free_tier_summary",
           return_value=FAKE_SUMMARY)
    def test_quota_summary(self, mock_summary):
        r = self.client.get("/api/quota")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["profile"], "DEFAULT")
        self.assertEqual(body["profileLabel"], "新加坡")
        self.assertEqual(body["summary"]["a1"]["ocpu_limit"], 4)
        self.assertTrue(body["summary"]["overall_ok"])

    @patch("web.routes.quota.can_launch", return_value=(True, "可以创建"))
    def test_can_launch_ok(self, mock_can):
        r = self.client.post("/api/quota/can-launch",
                             json={"shape": "VM.Standard.E2.1.Micro"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["can_launch"])
        self.assertEqual(r.json()["reason"], "可以创建")

    @patch("web.routes.quota.can_launch",
           return_value=(False, "E2 已满（2/2）"))
    def test_can_launch_no(self, mock_can):
        r = self.client.post("/api/quota/can-launch",
                             json={"shape": "VM.Standard.E2.1.Micro"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["can_launch"])
        self.assertIn("E2 已满", r.json()["reason"])


class TestCreateAndTasksApi(WebApiTestCase):
    """创建实例（抢机任务）/ 任务列表 / 取消。"""

    def setUp(self):
        super().setUp()
        self._login()

    @patch("web.tasks.notify_boot_success",
            return_value={"telegram": True, "bark": False, "errors": []})
    @patch("web.tasks.wait_for_public_ip", return_value="203.0.113.10")
    @patch("web.tasks.Grabber")
    @patch("web.routes.create.can_launch", return_value=(True, "可以创建"))
    def test_create_starts_grab_task(self, mock_can, mock_grabber_cls,
                                     mock_ip, mock_notify):
        """预检通过则建任务、后台跑 Grabber，返回 task_id。"""
        mock_grabber_cls.return_value.run.return_value = GrabResult(
            instance_id=FAKE_INSTANCE, attempts=3, releases=1,
            elapsed_seconds=95.0)
        r = self.client.post("/api/create", json=CREATE_BODY)
        self.assertEqual(r.status_code, 200)
        task_id = r.json()["task_id"]
        self.assertTrue(task_id)
        # Root login is on by default: the response returns the generated UUID password (only this once). 默认 root 登录开启：响应带回生成的 UUID 密码（只返回这一次）。
        self.assertRegex(r.json()["root_password"], r"^[0-9a-f]{32}$")
        task = self._wait_task_status(task_id, ("done", "failed"))
        self.assertEqual(task["status"], "done")
        self.assertEqual(task["result"]["instance_id"], FAKE_INSTANCE)
        self.assertEqual(task["result"]["attempts"], 3)
        self.assertEqual(task["result"]["releases"], 1)
        r = self.client.post(f"/api/tasks/{task_id}/cancel")
        self.assertEqual(r.status_code, 400)

    @patch("web.tasks.Grabber")
    @patch("web.routes.create.can_launch",
           return_value=(False, "E2 已满（2/2）"))
    def test_create_quota_check_fails_400(self, mock_can, mock_grabber_cls):
        """预检不通过返回 400，不启动 Grabber。"""
        r = self.client.post("/api/create", json=CREATE_BODY)
        self.assertEqual(r.status_code, 400)
        self.assertIn("E2 已满", r.json()["error"])
        mock_grabber_cls.assert_not_called()

    @patch("web.tasks.Grabber")
    @patch("web.routes.create.get_image_arch", return_value="aarch64")
    @patch("web.routes.create.can_launch", return_value=(True, "可以创建"))
    def test_create_image_arch_mismatch_400(self, mock_can, mock_arch,
                                            mock_grabber_cls):
        """aarch64 镜像 + E2 形状：事前校验 400，不启动 Grabber。"""
        r = self.client.post("/api/create", json=CREATE_BODY)
        self.assertEqual(r.status_code, 400)
        self.assertIn("镜像架构与形状不匹配", r.json()["error"])
        self.assertIn("x86_64", r.json()["error"])
        mock_grabber_cls.assert_not_called()

    @patch("web.tasks.Grabber")
    @patch("web.routes.create.get_image_arch", return_value="x86_64")
    @patch("web.routes.create.can_launch", return_value=(True, "可以创建"))
    def test_create_image_arch_match_passes(self, mock_can, mock_arch,
                                            mock_grabber_cls):
        """x86_64 镜像 + E2 形状：校验通过，任务正常创建。"""
        mock_grabber_cls.return_value.run.return_value = GrabResult(
            instance_id=FAKE_INSTANCE, attempts=1, releases=0,
            elapsed_seconds=1.0)
        r = self.client.post("/api/create", json=CREATE_BODY)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["task_id"])

    @patch("web.tasks.Grabber")
    @patch("web.routes.create.get_image_arch", return_value=None)
    @patch("web.routes.create.can_launch", return_value=(True, "可以创建"))
    def test_create_image_arch_unknown_skips_check(self, mock_can, mock_arch,
                                                   mock_grabber_cls):
        """镜像架构推断不出时不阻塞创建（抢机失败时 OCI 自己报错）。"""
        mock_grabber_cls.return_value.run.return_value = GrabResult(
            instance_id=FAKE_INSTANCE, attempts=1, releases=0,
            elapsed_seconds=1.0)
        r = self.client.post("/api/create", json=CREATE_BODY)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["task_id"])

    @patch("web.tasks.Grabber")
    @patch("web.routes.create.get_image_arch")
    @patch("web.routes.create.can_launch", return_value=(True, "可以创建"))
    def test_create_placeholder_arch_mismatch_no_oci_call(
            self, mock_can, mock_arch, mock_grabber_cls):
        """占位符镜像的架构取自后缀：aarch64 占位符 + E2 直接 400，
        且不调 OCI 查镜像（mock 未被调用）。"""
        body = dict(CREATE_BODY)
        body["image_id"] = "ocid1.image.oc1..xxxxxxxx-aarch64"
        with patch.dict("web.routes.create.config.IMAGE_IDS",
                        {"aarch64": "ocid1.image.oc1..realxxxx"}):
            r = self.client.post("/api/create", json=body)
        self.assertEqual(r.status_code, 400)
        self.assertIn("镜像架构与形状不匹配", r.json()["error"])
        mock_arch.assert_not_called()
        mock_grabber_cls.assert_not_called()

    def test_create_missing_field_422(self):
        """缺必填字段返回 422（信封格式）。"""
        body = dict(CREATE_BODY)
        del body["image_id"]
        r = self.client.post("/api/create", json=body)
        self.assertEqual(r.status_code, 422)
        self.assertFalse(r.json()["ok"])

    @patch("web.tasks.combined_boot_keys")
    @patch("web.tasks.get_allowed_cpu_models",
            return_value=["7J13", "7745"])
    @patch("web.tasks.make_cpu_check_hook", return_value=MagicMock())
    @patch("web.tasks.notify_boot_success",
            return_value={"telegram": True, "bark": False, "errors": []})
    @patch("web.tasks.wait_for_public_ip", return_value="203.0.113.10")
    @patch("web.tasks.Grabber")
    @patch("web.routes.create.can_launch", return_value=(True, "可以创建"))
    def test_create_full_fields_passed_to_grabber(self, mock_can,
                                                  mock_grabber_cls,
                                                  mock_ip, mock_notify,
                                                  mock_hook_fn, mock_allowed,
                                                  mock_keys):
        """带全字段创建时，全部新字段透传给 Grabber（阶段 3b 接入）。

        target_cpu 非空时按 CPU 规则翻译成配置里的型号列表
        （默认 7J13），启动 keys 用拼进 guard 公钥的合并文件。
        """
        mock_keys.return_value.__enter__.return_value = BootKeys(
            path="/tmp/combined.pub", guard_included=True)
        mock_grabber_cls.return_value.run.return_value = GrabResult(
            instance_id=FAKE_INSTANCE, attempts=1, releases=0,
            elapsed_seconds=5.0)
        body = dict(CREATE_BODY)
        body.update({
            "shape": "VM.Standard.A1.Flex",
            "ocpus": 4,
            "memory_gb": 24,
            "boot_volume_size_gbs": 100,
            "assign_public_ip": False,
            "assign_ipv6": True,
            "min_delay": 20,
            "max_delay": 50,
            "user_data": "#!/bin/bash\necho hi\n",
            "root_login": False,
        })
        r = self.client.post("/api/create", json=body)
        self.assertEqual(r.status_code, 200)
        task_id = r.json()["task_id"]
        self._wait_task_status(task_id, ("done", "failed"))
        mock_grabber_cls.assert_called_once()
        kwargs = mock_grabber_cls.call_args.kwargs
        self.assertEqual(kwargs["ocpus"], 4)
        self.assertEqual(kwargs["memory_gb"], 24)
        self.assertEqual(kwargs["boot_volume_size_gbs"], 100)
        self.assertFalse(kwargs["assign_public_ip"])
        self.assertTrue(kwargs["assign_ipv6"])
        self.assertEqual(kwargs["min_delay"], 20)
        self.assertEqual(kwargs["max_delay"], 50)
        self.assertEqual(kwargs["user_data"], "#!/bin/bash\necho hi\n")
        self.assertEqual(kwargs["target_cpu"], ["7J13", "7745"])
        args = mock_grabber_cls.call_args.args
        self.assertEqual(args[3], "VM.Standard.A1.Flex")
        self.assertEqual(args[7], "/tmp/combined.pub")

    @patch("web.tasks.start_dd_init")
    @patch("web.tasks.notify_boot_success",
            return_value={"telegram": True, "bark": False, "errors": []})
    @patch("web.tasks.wait_for_public_ip", return_value="203.0.113.10")
    @patch("web.tasks.Grabber")
    @patch("web.routes.create.can_launch", return_value=(True, "可以创建"))
    def test_create_dd_true_persists_dd_and_init_script(
            self, mock_can, mock_grabber_cls, mock_ip, mock_notify,
            mock_dd_init):
        """DD 开时任务规格里存 dd 与 init_script，Grabber 拿到 DD 脚本。

        前端 DD 开关默认勾选（与机器人端一致）：dd=true 时 user-data
        是 InstallNET 重装脚本，启动脚本原文改作 DD 后的初始化脚本，
        两者都写进 grab_spec，服务重启恢复后仍能继续 DD。
        """
        mock_grabber_cls.return_value.run.return_value = GrabResult(
            instance_id=FAKE_INSTANCE, attempts=1, releases=0,
            elapsed_seconds=5.0)
        # The DD password must be the same one the form set (here the
        # frontend-prefilled random UUID, sent as root_password). DD 密码
        # 必须与开机表单设的随机密码是同一份（这里模拟前端预填的 UUID）。
        form_password = "0123456789abcdef" * 2
        body = dict(CREATE_BODY)
        body.update({
            "dd": True,
            "user_data": "#!/bin/bash\necho hi\n",
            "root_password": form_password,
        })
        r = self.client.post("/api/create", json=body)
        self.assertEqual(r.status_code, 200)
        # The response returns the same password (shown once in the
        # frontend modal). 响应带回同一份密码（前端弹窗只显示这一次）。
        self.assertEqual(r.json()["root_password"], form_password)
        task_id = r.json()["task_id"]
        self._wait_task_status(task_id, ("done", "failed"))
        # The persisted grab spec carries dd + init_script (restart resume needs them). 持久化的抢机规格里带 dd 与 init_script（重启恢复要靠它们继续 DD）。
        record = task_store.get_record(TASKS.tasks_file, task_id)
        spec = record.get("grab_spec") or {}
        params = spec.get("params") or {}
        self.assertTrue(params.get("dd"))
        self.assertEqual(params.get("init_script"), "#!/bin/bash\necho hi\n")
        # The Grabber gets the DD (InstallNET) user-data, not the raw script. Grabber 拿到的是 DD（InstallNET）user-data，不是脚本原文。
        kwargs = mock_grabber_cls.call_args.kwargs
        self.assertIn("InstallNET.sh", kwargs["user_data"])
        self.assertNotIn("echo hi", kwargs["user_data"])
        # The DD reinstall script embeds the SAME password the form set
        # (InstallNET -pwd), so the DD-reinstalled system logs in with
        # the password the user saw. DD 重装脚本里嵌的是同一份表单密码
        # （InstallNET -pwd），DD 后的系统就用这个密码登录。
        self.assertIn("-pwd '" + form_password + "'",
                      kwargs["user_data"])
        # DD init is started with the combined script (security hardening
        # first, then the user's own script; mocked: no real SSH wait
        # thread). DD 初始化被启动，传的是合并后的脚本（先安全加固、再用
        # 户自己的脚本；已 mock，不起真等 DD 的线程）。
        deadline = time.time() + 2.0
        while not mock_dd_init.called and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(mock_dd_init.called, "DD 开时应起 DD 初始化")
        combined = mock_dd_init.call_args.args[2]
        self.assertIn("#!/bin/bash\necho hi", combined)
        self.assertIn("DD 安全加固", combined)
        # DD init SSH logs in with the same form password. DD 初始化
        # SSH 登录用的也是同一份表单密码。
        self.assertEqual(mock_dd_init.call_args.args[1], form_password)

    @patch("web.tasks.notify_boot_success",
            return_value={"telegram": True, "bark": False, "errors": []})
    @patch("web.tasks.wait_for_public_ip", return_value="203.0.113.10")
    @patch("web.tasks.Grabber")
    @patch("web.routes.create.can_launch", return_value=(True, "可以创建"))
    def test_create_root_password_generated_and_notified(
            self, mock_can, mock_grabber_cls, mock_ip, mock_notify):
        """v1.11.0：默认 root 登录开启时后端生成 UUID 密码，响应带回，
        Grabber 收到含 chpasswd 的 cloud-init，抢机成功后调双通道通知
        （通知里带同一份密码）。"""
        mock_grabber_cls.return_value.run.return_value = GrabResult(
            instance_id=FAKE_INSTANCE, attempts=1, releases=0,
            elapsed_seconds=5.0)
        r = self.client.post("/api/create", json=CREATE_BODY)
        self.assertEqual(r.status_code, 200)
        password = r.json()["root_password"]
        self.assertRegex(password, r"^[0-9a-f]{32}$")
        # Wait for the grabber thread to build its Grabber (the thread writes the task file first; when POST returns it may not have reached the Grabber yet, so reading call_args directly would race). 等抢机线程构造 Grabber（线程里先写任务文件，POST 返回时不一定已调到 Grabber，直接读 call_args 会抢跑）。
        deadline = time.time() + 2.0
        while not mock_grabber_cls.called and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(mock_grabber_cls.called, "应已构造 Grabber")
        kwargs = mock_grabber_cls.call_args.kwargs
        self.assertIn("chpasswd", kwargs["user_data"])
        self.assertIn(password, kwargs["user_data"])
        task_id = r.json()["task_id"]
        self._wait_task_status(task_id, ("done", "failed"))
        deadline = time.time() + 2.0
        while not mock_notify.called and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(mock_notify.called, "抢机成功后应调双通道通知")
        args, kwargs = mock_notify.call_args
        self.assertEqual(args[8][0]["name"], "test-instance")
        self.assertEqual(args[8][0]["public_ip"], "203.0.113.10")
        self.assertEqual(kwargs["root_password"], password)
        self.assertTrue(kwargs["ssh_key_set"])

    @patch("web.tasks.notify_boot_success",
            return_value={"telegram": True, "bark": False, "errors": []})
    @patch("web.tasks.wait_for_public_ip", return_value="203.0.113.10")
    @patch("web.tasks.Grabber")
    @patch("web.routes.create.can_launch", return_value=(True, "可以创建"))
    def test_create_frontend_password_used(
            self, mock_can, mock_grabber_cls, mock_ip, mock_notify):
        """前端传来的密码原样使用（响应回显同一份）。"""
        mock_grabber_cls.return_value.run.return_value = GrabResult(
            instance_id=FAKE_INSTANCE, attempts=1, releases=0,
            elapsed_seconds=5.0)
        body = dict(CREATE_BODY)
        body["root_password"] = "0123456789abcdef0123456789abcdef"
        r = self.client.post("/api/create", json=body)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["root_password"],
                         "0123456789abcdef0123456789abcdef")
        deadline = time.time() + 2.0
        while not mock_grabber_cls.called and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(mock_grabber_cls.called, "应已构造 Grabber")
        kwargs = mock_grabber_cls.call_args.kwargs
        self.assertIn("0123456789abcdef0123456789abcdef",
                      kwargs["user_data"])
        self._wait_task_status(r.json()["task_id"], ("done", "failed"))

    @patch("web.tasks.notify_boot_success",
            return_value={"telegram": True, "bark": False, "errors": []})
    @patch("web.tasks.wait_for_public_ip", return_value="203.0.113.10")
    @patch("web.tasks.Grabber")
    @patch("web.routes.create.can_launch", return_value=(True, "可以创建"))
    def test_create_root_login_false_no_password(
            self, mock_can, mock_grabber_cls, mock_ip, mock_notify):
        """root 登录关闭时不生成密码，响应 root_password 为空，脚本原文
        直通。"""
        mock_grabber_cls.return_value.run.return_value = GrabResult(
            instance_id=FAKE_INSTANCE, attempts=1, releases=0,
            elapsed_seconds=5.0)
        body = dict(CREATE_BODY)
        body.update({"root_login": False,
                     "user_data": "#!/bin/bash\necho hi\n"})
        r = self.client.post("/api/create", json=body)
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["root_password"])
        deadline = time.time() + 2.0
        while not mock_grabber_cls.called and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(mock_grabber_cls.called, "应已构造 Grabber")
        kwargs = mock_grabber_cls.call_args.kwargs
        self.assertEqual(kwargs["user_data"], "#!/bin/bash\necho hi\n")
        self._wait_task_status(r.json()["task_id"], ("done", "failed"))

    @patch("web.routes.create.can_launch", return_value=(True, "可以创建"))
    def test_create_cloud_config_script_with_root_400(self, mock_can):
        """脚本本身是 #cloud-config 时与 root 密码冲突，返回 400。"""
        body = dict(CREATE_BODY)
        body["user_data"] = "#cloud-config\nruncmd:\n  - echo hi\n"
        r = self.client.post("/api/create", json=body)
        self.assertEqual(r.status_code, 400)
        self.assertIn("cloud-config", r.json()["error"])

    def test_create_boot_volume_out_of_range_422(self):
        """引导卷 <50 或 >200 返回 422（pydantic 范围校验）。

        47 是镜像默认大小，不显式传（前端发 null）；48/49 同样非法。
        """
        for bad_size in (46, 47, 48, 49, 201):
            body = dict(CREATE_BODY)
            body["boot_volume_size_gbs"] = bad_size
            r = self.client.post("/api/create", json=body)
            self.assertEqual(r.status_code, 422, f"size={bad_size}")
            self.assertFalse(r.json()["ok"])

    def test_create_boot_volume_boundary_ok(self):
        """引导卷 50 / 200 边界值与 null 可通过校验（进预检流程，不被 422 挡）。"""
        with patch("web.routes.create.can_launch",
                   return_value=(False, "E2 已满（2/2）")):
            for size in (50, 200, None):
                body = dict(CREATE_BODY)
                body["boot_volume_size_gbs"] = size
                r = self.client.post("/api/create", json=body)
                self.assertEqual(r.status_code, 400, f"size={size}")

    def test_create_region_mismatch_400(self):
        """可用域与所选区域不一致时返回 400（不把主区 AD 提交到别的区）。"""
        profiles = {
            "DEFAULT": {"region": "ap-singapore-1",
                        "compartment_id": "ocid1.tenancy.oc1..testxxxx"},
            "ACCOUNT2": {"region": "us-sanjose-1",
                         "compartment_id": "ocid1.tenancy.oc1..testyyyy"},
        }
        body = dict(CREATE_BODY)
        body["profile"] = "ACCOUNT2"
        body["availability_domain"] = "wgwP:AP-SINGAPORE-1-AD-1"
        with patch.dict(config.PROFILES, profiles, clear=True):
            r = self.client.post("/api/create", json=body)
        self.assertEqual(r.status_code, 400)
        self.assertIn("不一致", r.json()["error"])

    def test_create_max_delay_less_than_min_400(self):
        """max_delay < min_delay 返回 400。"""
        body = dict(CREATE_BODY)
        body["min_delay"] = 50
        body["max_delay"] = 20
        r = self.client.post("/api/create", json=body)
        self.assertEqual(r.status_code, 400)
        self.assertIn("最大延迟", r.json()["error"])

    def test_create_a1_over_free_tier_400(self):
        """A1 超过免费额度上限（4 核 / 24GB）返回 400。"""
        body = dict(CREATE_BODY)
        body["shape"] = "VM.Standard.A1.Flex"
        body["ocpus"] = 8
        body["memory_gb"] = 12
        r = self.client.post("/api/create", json=body)
        self.assertEqual(r.status_code, 400)
        self.assertIn("4 OCPU / 24GB", r.json()["error"])
        body["ocpus"] = 4
        body["memory_gb"] = 48
        r = self.client.post("/api/create", json=body)
        self.assertEqual(r.status_code, 400)
        self.assertIn("4 OCPU / 24GB", r.json()["error"])

    @patch("web.tasks.notify_boot_success",
            return_value={"telegram": True, "bark": False, "errors": []})
    @patch("web.tasks.wait_for_public_ip", return_value="203.0.113.10")
    @patch("web.tasks.Grabber")
    @patch("web.routes.create.can_launch", return_value=(True, "可以创建"))
    def test_create_without_ssh_keys_file_ok(self, mock_can,
                                             mock_grabber_cls, mock_ip,
                                             mock_notify):
        """不传 ssh_keys_file 也能创建（可选项，Grabber 收到空串后
        不传 --ssh-authorized-keys-file，只靠 root 密码登录）。"""
        mock_grabber_cls.return_value.run.return_value = GrabResult(
            instance_id=FAKE_INSTANCE, attempts=1, releases=0,
            elapsed_seconds=5.0)
        body = dict(CREATE_BODY)
        del body["ssh_keys_file"]
        r = self.client.post("/api/create", json=body)
        self.assertEqual(r.status_code, 200)
        task_id = r.json()["task_id"]
        self._wait_task_status(task_id, ("done", "failed"))
        mock_grabber_cls.assert_called_once()
        self.assertEqual(mock_grabber_cls.call_args.args[7], "")

    @patch("web.tasks.notify_boot_success",
            return_value={"telegram": True, "bark": False, "errors": []})
    @patch("web.tasks.wait_for_public_ip", return_value="203.0.113.10")
    @patch("web.tasks.Grabber")
    @patch("web.routes.create.can_launch", return_value=(True, "可以创建"))
    @patch("web.deps.get_profile_regions",
           return_value={"DEFAULT": ["ap-singapore-1", "ap-seoul-1"]})
    def test_create_with_region_uses_region_client(
            self, mock_regions, mock_can, mock_grabber_cls, mock_ip,
            mock_notify):
        """body 带 region 时 Grabber 用该区域的 OCIClient 创建。"""
        mock_grabber_cls.return_value.run.return_value = GrabResult(
            instance_id=FAKE_INSTANCE, attempts=1, releases=0,
            elapsed_seconds=5.0)
        body = dict(CREATE_BODY)
        body["region"] = "ap-seoul-1"
        r = self.client.post("/api/create", json=body)
        self.assertEqual(r.status_code, 200)
        task_id = r.json()["task_id"]
        self._wait_task_status(task_id, ("done", "failed"))
        mock_grabber_cls.assert_called_once()
        cli = mock_grabber_cls.call_args.args[0]
        self.assertEqual(cli.region, "ap-seoul-1")

    @patch("web.deps.get_profile_regions",
           return_value={"DEFAULT": ["ap-singapore-1"]})
    def test_create_unsubscribed_region_400(self, mock_regions):
        """创建时传未订阅的区域返回 400，不启动 Grabber。"""
        body = dict(CREATE_BODY)
        body["region"] = "eu-frankfurt-1"
        r = self.client.post("/api/create", json=body)
        self.assertEqual(r.status_code, 400)
        self.assertIn("未订阅", r.json()["error"])

    @patch("web.tasks.Grabber", BlockingGrabber)
    @patch("web.routes.create.can_launch", return_value=(True, "可以创建"))
    def test_cancel_running_task(self, mock_can):
        """取消运行中的任务返回 200，任务状态变为 cancelled。"""
        r = self.client.post("/api/create", json=CREATE_BODY)
        task_id = r.json()["task_id"]
        task = self._wait_task_status(task_id, ("running",))
        self.assertEqual(task["status"], "running")
        r = self.client.post(f"/api/tasks/{task_id}/cancel")
        self.assertEqual(r.status_code, 200)
        r = self.client.get(f"/api/tasks/{task_id}")
        self.assertEqual(r.json()["task"]["status"], "cancelled")

    def test_task_not_found(self):
        r = self.client.get("/api/tasks/g999")
        self.assertEqual(r.status_code, 404)
        r = self.client.post("/api/tasks/g999/cancel")
        self.assertEqual(r.status_code, 404)
        r = self.client.delete("/api/tasks/g999")
        self.assertEqual(r.status_code, 404)

    @patch("web.tasks.Grabber", BlockingGrabber)
    @patch("web.routes.create.can_launch", return_value=(True, "可以创建"))
    def test_delete_finished_task(self, mock_can):
        """已结束的任务可以删除，删后列表里不再有它。"""
        r = self.client.post("/api/create", json=CREATE_BODY)
        task_id = r.json()["task_id"]
        self.client.post(f"/api/tasks/{task_id}/cancel")
        r = self.client.delete(f"/api/tasks/{task_id}")
        self.assertEqual(r.status_code, 200)
        r = self.client.get(f"/api/tasks/{task_id}")
        self.assertEqual(r.status_code, 404)
        r = self.client.get("/api/tasks")
        self.assertEqual(r.json()["tasks"], [])

    @patch("web.tasks.Grabber", BlockingGrabber)
    @patch("web.routes.create.can_launch", return_value=(True, "可以创建"))
    def test_delete_running_task_returns_400(self, mock_can):
        """运行中的任务不能直接删除，须先取消。"""
        r = self.client.post("/api/create", json=CREATE_BODY)
        task_id = r.json()["task_id"]
        self._wait_task_status(task_id, ("running",))
        r = self.client.delete(f"/api/tasks/{task_id}")
        self.assertEqual(r.status_code, 400)
        self.assertIn("运行中", r.json()["error"])

    @patch("web.tasks.notify_boot_success",
            return_value={"telegram": True, "bark": False, "errors": []})
    @patch("web.tasks.wait_for_public_ip", return_value="203.0.113.10")
    @patch("web.tasks.Grabber")
    @patch("web.routes.create.can_launch", return_value=(True, "可以创建"))
    def test_list_tasks(self, mock_can, mock_grabber_cls, mock_ip,
                        mock_notify):
        mock_grabber_cls.return_value.run.return_value = GrabResult(
            instance_id=FAKE_INSTANCE, attempts=1, releases=0,
            elapsed_seconds=5.0)
        r = self.client.get("/api/tasks")
        self.assertEqual(r.json()["tasks"], [])
        self.client.post("/api/create", json=CREATE_BODY)
        r = self.client.get("/api/tasks")
        self.assertEqual(len(r.json()["tasks"]), 1)
        self.assertEqual(r.json()["tasks"][0]["kind"], "grab")


class TestCreateDefaultsApi(WebApiTestCase):
    """GET /api/create/defaults：创建表单的网络默认值自动填充。"""

    def setUp(self):
        super().setUp()
        self._login()
        self._key_file = os.path.join(self._tmp.name, "keys.pub")
        with open(self._key_file, "w", encoding="utf-8") as handle:
            handle.write("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHRlc3QtdGVzdA== test-key\n")

    def _env(self, **overrides):
        env = {
            "MBOT_PLAN_AD": "test:AP-SINGAPORE-1-AD-1",
            "MBOT_PLAN_SUBNET_ID": FAKE_SUBNET,
            "MBOT_PLAN_SSH_KEYS_FILE": self._key_file,
        }
        env.update(overrides)
        return env

    @patch("tgbot.config.REGION_PLANS_FILE", "/nonexistent/plans.json")
    def test_defaults_returns_env_values(self):
        """环境变量里的部署值原样返回，存在的密钥文件带路径。"""
        with patch.dict(os.environ, self._env()):
            r = self.client.get("/api/create/defaults")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["profile"], "DEFAULT")
        self.assertEqual(body["region"], "ap-singapore-1")
        self.assertEqual(body["availability_domain"],
                         "test:AP-SINGAPORE-1-AD-1")
        self.assertEqual(body["subnet_id"], FAKE_SUBNET)
        self.assertEqual(body["ssh_keys_file"], self._key_file)
        self.assertTrue(body["ssh_keys_file_exists"])

    @patch("tgbot.config.REGION_PLANS_FILE", "/nonexistent/plans.json")
    def test_defaults_missing_ssh_key_file_empty(self):
        """密钥文件不存在时 ssh_keys_file 返回空串（不填），其余照常。"""
        with patch.dict(os.environ, self._env(
                MBOT_PLAN_SSH_KEYS_FILE="/nonexistent/keys.pub")):
            r = self.client.get("/api/create/defaults")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["ssh_keys_file"], "")
        self.assertFalse(body["ssh_keys_file_exists"])
        self.assertEqual(body["availability_domain"],
                         "test:AP-SINGAPORE-1-AD-1")
        self.assertEqual(body["subnet_id"], FAKE_SUBNET)

    @patch("tgbot.config.REGION_PLANS_FILE", "/nonexistent/plans.json")
    def test_defaults_unconfigured_empty(self):
        """未配置部署值时三项都返回空串（占位符不外露）。"""
        with patch.dict(os.environ, {
                "MBOT_PLAN_AD": "",
                "MBOT_PLAN_SUBNET_ID": "",
                "MBOT_PLAN_SSH_KEYS_FILE": ""}):
            r = self.client.get("/api/create/defaults")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["availability_domain"], "")
        self.assertEqual(body["subnet_id"], "")
        self.assertEqual(body["ssh_keys_file"], "")
        self.assertFalse(body["ssh_keys_file_exists"])

    def test_defaults_region_plan_override(self):
        """region_plans.json 里有该 (profile, region) 时用该区域的
        可用域/子网（密钥文件仍按环境变量的，不随区域变）。"""
        plans_path = os.path.join(self._tmp.name, "region_plans.json")
        with open(plans_path, "w", encoding="utf-8") as handle:
            json.dump({"DEFAULT": {"ap-singapore-1": {
                "availability_domain": "plan:AP-SINGAPORE-1-AD-1",
                "subnet_id": "ocid1.subnet.oc1..planxxxx"}}}, handle)
        with patch("tgbot.config.REGION_PLANS_FILE", plans_path), \
                patch.dict(os.environ, self._env()):
            r = self.client.get("/api/create/defaults")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["availability_domain"],
                         "plan:AP-SINGAPORE-1-AD-1")
        self.assertEqual(body["subnet_id"], "ocid1.subnet.oc1..planxxxx")
        self.assertEqual(body["ssh_keys_file"], self._key_file)

    def test_defaults_unknown_profile_400(self):
        r = self.client.get("/api/create/defaults?profile=NOPE")
        self.assertEqual(r.status_code, 400)
        self.assertIn("未知 profile", r.json()["error"])

    def test_defaults_region_param_uses_region_plan(self):
        """?region=ap-seoul-1 时用 (DEFAULT, ap-seoul-1) 的区域配置。"""
        plans_path = os.path.join(self._tmp.name, "region_plans.json")
        with open(plans_path, "w", encoding="utf-8") as handle:
            json.dump({"DEFAULT": {"ap-seoul-1": {
                "availability_domain": "seoul:AP-SEOUL-1-AD-1",
                "subnet_id": "ocid1.subnet.oc1..seoulxxxx"}}}, handle)
        with patch("tgbot.config.REGION_PLANS_FILE", plans_path), \
                patch.dict(os.environ, self._env()):
            r = self.client.get("/api/create/defaults?region=ap-seoul-1")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["region"], "ap-seoul-1")
        self.assertEqual(body["availability_domain"],
                         "seoul:AP-SEOUL-1-AD-1")
        self.assertEqual(body["subnet_id"], "ocid1.subnet.oc1..seoulxxxx")

    @patch("tgbot.config.REGION_PLANS_FILE", "/nonexistent/plans.json")
    def test_defaults_other_region_env_fallback_blank(self):
        """所选账号的区域不是部署默认值所属的区域时，可用域/子网留空
        （不把主区的配置填到别的账号），密钥文件照常返回。"""
        profiles = {
            "DEFAULT": {"region": "ap-singapore-1",
                        "compartment_id": "ocid1.tenancy.oc1..testxxxx"},
            "ACCOUNT2": {"region": "us-sanjose-1",
                         "compartment_id": "ocid1.tenancy.oc1..testyyyy"},
        }
        with patch.dict(config.PROFILES, profiles, clear=True), \
                patch.dict(os.environ, self._env()):
            r = self.client.get("/api/create/defaults?profile=ACCOUNT2")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["region"], "us-sanjose-1")
        self.assertEqual(body["availability_domain"], "")
        self.assertEqual(body["subnet_id"], "")
        self.assertEqual(body["ssh_keys_file"], self._key_file)
        self.assertTrue(body["ssh_keys_file_exists"])

    def test_defaults_requires_login(self):
        r = self.anon.get("/api/create/defaults")
        self.assertEqual(r.status_code, 401)


class TestRegionDeps(unittest.TestCase):
    """web/deps.py 的区域解析：有效区域、未订阅、订阅列表查不到时放行。"""

    def setUp(self):
        # Clear the subscription cache so tests do not leak data into each other. 清订阅缓存，避免测试互相串数据。
        deps_mod._profile_regions = {}
        deps_mod._profile_regions_at = 0

    def tearDown(self):
        deps_mod._profile_regions = {}
        deps_mod._profile_regions_at = 0

    @patch("web.deps.get_profile_regions",
           return_value={"DEFAULT": ["ap-singapore-1", "ap-seoul-1"]})
    def test_resolve_profile_region_override(self, mock_regions):
        cli, _compartment = resolve_profile("DEFAULT", "ap-seoul-1")
        self.assertEqual(cli.profile, "DEFAULT")
        self.assertEqual(cli.region, "ap-seoul-1")

    @patch("web.deps.get_profile_regions",
           return_value={"DEFAULT": ["ap-singapore-1", "ap-seoul-1"]})
    def test_resolve_profile_unsubscribed_region(self, mock_regions):
        with self.assertRaises(ValueError) as ctx:
            resolve_profile("DEFAULT", "eu-frankfurt-1")
        self.assertIn("未订阅", str(ctx.exception))

    @patch("web.deps.get_profile_regions", return_value={})
    def test_resolve_profile_fail_open_when_list_unavailable(
            self, mock_regions):
        cli, _compartment = resolve_profile("DEFAULT", "ap-seoul-1")
        self.assertEqual(cli.region, "ap-seoul-1")

    def test_resolve_profile_no_region_uses_home(self):
        cli, _compartment = resolve_profile("DEFAULT")
        self.assertEqual(cli.region, "ap-singapore-1")

    def test_effective_region(self):
        self.assertEqual(effective_region("DEFAULT", None),
                         "ap-singapore-1")
        self.assertEqual(effective_region("DEFAULT", "ap-seoul-1"),
                         "ap-seoul-1")

    @patch("web.deps.OCIClient")
    def test_get_profile_regions_caches(self, mock_cli_cls):
        # The test only checks the cache logic, so pin the account set to one account. 测试只测缓存逻辑，账号集固定为 1 个。
        profiles = {"DEFAULT": {"region": "ap-singapore-1",
                                "compartment_id": "ocid1.tenancy.oc1..aaa"}}
        mock_cli_cls.return_value.run.return_value = {
            "data": [{"region-name": "ap-singapore-1"},
                     {"region-name": "ap-seoul-1"}]}
        with patch.object(config, "PROFILES", profiles):
            first = get_profile_regions()
            second = get_profile_regions()
        self.assertEqual(first,
                         {"DEFAULT": ["ap-singapore-1", "ap-seoul-1"]})
        self.assertEqual(second, first)
        self.assertEqual(mock_cli_cls.return_value.run.call_count, 1)


class TestProfilesApi(WebApiTestCase):
    """GET /api/profiles：账号列表带订阅区域（二级菜单的数据源）。"""

    def setUp(self):
        super().setUp()
        self._login()

    @patch("web.routes.profiles.get_region_counts",
           return_value={"DEFAULT": 3})
    @patch("web.routes.profiles.get_profile_regions",
           return_value={"DEFAULT": ["ap-seoul-1", "ap-singapore-1",
                                     "us-sanjose-1"]})
    def test_profiles_include_regions(self, mock_regions, mock_counts):
        r = self.client.get("/api/profiles")
        self.assertEqual(r.status_code, 200)
        profs = {p["name"]: p for p in r.json()["profiles"]}
        prof = profs["DEFAULT"]
        self.assertEqual(prof["regionCount"], 3)
        codes = [entry["code"] for entry in prof["regions"]]
        self.assertEqual(codes, ["ap-singapore-1", "ap-seoul-1",
                                 "us-sanjose-1"])
        self.assertTrue(prof["regions"][0]["isHome"])
        self.assertFalse(prof["regions"][1]["isHome"])
        self.assertEqual(prof["regions"][0]["label"], "新加坡")
        self.assertEqual(prof["regions"][1]["label"], "首尔")

    @patch("web.routes.profiles.get_region_counts", return_value={})
    @patch("web.routes.profiles.get_profile_regions", return_value={})
    def test_profiles_regions_fallback_to_home(self, mock_regions,
                                               mock_counts):
        r = self.client.get("/api/profiles")
        self.assertEqual(r.status_code, 200)
        profs = {p["name"]: p for p in r.json()["profiles"]}
        prof = profs["DEFAULT"]
        self.assertEqual(prof["regions"],
                         [{"code": "ap-singapore-1", "label": "新加坡",
                           "isHome": True}])


class TestNotifyGrabSuccess(unittest.TestCase):
    """_notify_grab_success：网页抢机成功后的双通道推送（v1.11.0）。"""

    def setUp(self):
        # Redirect the instance credential store to a temp file: the
        # notify path saves the root password on success, and tests must
        # not write it to the real data directory. 凭据库指到临时文件：
        # 通知成功后会保存 root 密码，测试不能写真实数据目录。
        self._cred_tmp = tempfile.TemporaryDirectory()
        self._cred_patch = patch(
            "tgbot.instance_credentials.CREDENTIALS_FILE",
            os.path.join(self._cred_tmp.name,
                         "instance_credentials.json"))
        self._cred_patch.start()
        self.addCleanup(self._cred_patch.stop)
        self.addCleanup(self._cred_tmp.cleanup)
        self.registry = MagicMock()
        self.params = {
            "display_name": "ARM_2026-11-24",
            "shape": "VM.Standard.A1.Flex",
            "ocpus": 1, "memory_gb": 6,
            "boot_volume_size_gbs": 47,
            "ssh_keys_file": "/tmp/test-keys.pub",
        }
        self.result = GrabResult(
            instance_id=FAKE_INSTANCE, attempts=2, releases=0,
            elapsed_seconds=45.0)
        self.notify = {"token": "T", "chat_id": 654321,
                       "profile_label": "新加坡", "region_label": "首尔",
                       "region_code": "ap-seoul-1",
                       "root_password": "0123456789abcdef0123456789abcdef"}

    def test_free_shape_notifies_no_pin_alert(self):
        with patch.object(tasks_mod, "wait_for_public_ip",
                          return_value=FAKE_IP), \
                patch.object(tasks_mod, "notify_boot_success",
                             return_value={"telegram": True, "bark": False,
                                           "errors": []}) as notify_fn, \
                patch.object(tasks_mod, "notify_dual") as dual:
            tasks_mod._notify_grab_success(
                self.registry, "g1", MagicMock(), self.params,
                self.result, self.notify)
        notify_fn.assert_called_once()
        args, kwargs = notify_fn.call_args
        self.assertEqual(args[0], "T")
        self.assertEqual(args[1], 654321)
        self.assertEqual(args[2], "新加坡")
        self.assertEqual(args[3], "首尔")
        self.assertEqual(args[4], "VM.Standard.A1.Flex")
        self.assertEqual(args[7], 47)
        self.assertEqual(args[8][0]["name"], "ARM_2026-11-24")
        self.assertEqual(args[8][0]["public_ip"], FAKE_IP)
        self.assertEqual(args[8][0]["attempts"], 2)
        self.assertEqual(kwargs["root_password"], self.notify["root_password"])
        self.assertEqual(kwargs["region_code"], "ap-seoul-1")
        self.assertTrue(kwargs["ssh_key_set"])
        dual.assert_not_called()

    def test_paid_shape_pins_alert(self):
        self.params["shape"] = "VM.Standard.E5.Flex"
        with patch.object(tasks_mod, "wait_for_public_ip",
                          return_value=FAKE_IP), \
                patch.object(tasks_mod, "notify_boot_success",
                             return_value={"telegram": True, "bark": False,
                                           "errors": []}), \
                patch.object(tasks_mod, "notify_dual",
                             return_value={"telegram": True, "bark": False,
                                           "errors": []}) as dual:
            tasks_mod._notify_grab_success(
                self.registry, "g1", MagicMock(), self.params,
                self.result, self.notify)
        dual.assert_called_once()
        args, kwargs = dual.call_args
        self.assertEqual(args[0], "T")
        self.assertEqual(args[1], 654321)
        self.assertIn("付费实例提醒", args[2])
        self.assertIn("VM.Standard.E5.Flex", args[3])
        self.assertTrue(kwargs.get("pin"))
        self.assertEqual(kwargs.get("group"), "paid-instance")

    def test_ip_lookup_failure_does_not_block(self):
        """wait 超时返回 None 时照常通知（IP 写"获取中"），并起跟进线程。

        轮询重试行为本身在 test_network.TestWaitForPublicIp 里测。
        """
        with patch.object(tasks_mod, "wait_for_public_ip",
                          return_value=None), \
                patch.object(tasks_mod, "notify_boot_success",
                             return_value={"telegram": True, "bark": False,
                                           "errors": []}) as notify_fn, \
                patch.object(tasks_mod, "start_ip_followup") as followup:
            tasks_mod._notify_grab_success(
                self.registry, "g1", MagicMock(), self.params,
                self.result, self.notify)
        self.assertIsNone(notify_fn.call_args[0][8][0]["public_ip"])
        followup.assert_called_once()
        args = followup.call_args[0]
        self.assertEqual(args[1], "ARM_2026-11-24")
        self.assertEqual(args[2], "T")
        self.assertEqual(args[3], 654321)
        with patch.object(tasks_mod, "get_public_ip",
                          return_value=FAKE_IP) as get_ip:
            self.assertEqual(args[0](), FAKE_IP)
        get_ip.assert_called_once()
        self.assertEqual(get_ip.call_args[0][1], FAKE_INSTANCE)

    def test_notify_exception_logged_not_raised(self):
        with patch.object(tasks_mod, "wait_for_public_ip",
                          return_value=FAKE_IP), \
                patch.object(tasks_mod, "notify_boot_success",
                             side_effect=RuntimeError("boom")):
            tasks_mod._notify_grab_success(
                self.registry, "g1", MagicMock(), self.params,
                self.result, self.notify)
        self.registry.append_log.assert_called_once()
        self.assertIn("通知失败",
                      self.registry.append_log.call_args[0][1])


class TestHostsApi(WebApiTestCase):
    """已保存 SSH 主机 CRUD（临时 DATA_DIR）。"""

    def setUp(self):
        super().setUp()
        self._login()

    def _hosts_file_text(self):
        path = os.path.join(self._tmp.name, "hosts.json")
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_crud_flow(self):
        r = self.client.get("/api/hosts")
        self.assertEqual(r.json()["hosts"], [])
        r = self.client.post("/api/hosts", json={
            "name": "sg-139", "host": FAKE_IP, "port": 14422,
            "username": "root", "auth_type": "password",
            "password": "s3cret-pw-12345", "tags": ["sg"],
            "notes": "测试主机"})
        self.assertEqual(r.status_code, 201)
        host = r.json()["host"]
        self.assertTrue(host["id"])
        self.assertEqual(host["name"], "sg-139")
        self.assertNotIn("password", host)
        self.assertIsNone(host["last_used"])
        text = self._hosts_file_text()
        self.assertNotIn("s3cret-pw-12345", text)
        with open(os.path.join(self._tmp.name, "hosts.json"),
                  encoding="utf-8") as f:
            stored = json.load(f)
        self.assertNotIn("password", stored[0])
        r = self.client.get("/api/hosts")
        self.assertEqual(len(r.json()["hosts"]), 1)
        r = self.client.put(f"/api/hosts/{host['id']}",
                            json={"name": "sg-139-renamed", "notes": "改过"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["host"]["name"], "sg-139-renamed")
        self.assertEqual(r.json()["host"]["host"], FAKE_IP)
        r = self.client.delete(f"/api/hosts/{host['id']}")
        self.assertEqual(r.status_code, 200)
        r = self.client.get("/api/hosts")
        self.assertEqual(r.json()["hosts"], [])

    def test_create_key_host(self):
        r = self.client.post("/api/hosts", json={
            "name": "key-host", "host": FAKE_IP,
            "auth_type": "key", "key_path": "/home/user/.ssh/id_ed25519"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["host"]["key_path"],
                         "/home/user/.ssh/id_ed25519")

    def test_create_key_host_without_key_path_400(self):
        r = self.client.post("/api/hosts", json={
            "name": "key-host", "host": FAKE_IP, "auth_type": "key"})
        self.assertEqual(r.status_code, 400)

    def test_create_bad_auth_type_400(self):
        r = self.client.post("/api/hosts", json={
            "name": "x", "host": FAKE_IP, "auth_type": "token"})
        self.assertEqual(r.status_code, 400)

    def test_create_bad_port_400(self):
        r = self.client.post("/api/hosts", json={
            "name": "x", "host": FAKE_IP, "port": 70000})
        self.assertEqual(r.status_code, 400)

    def test_update_and_delete_not_found_404(self):
        r = self.client.put("/api/hosts/no-such-id", json={"name": "x"})
        self.assertEqual(r.status_code, 404)
        r = self.client.delete("/api/hosts/no-such-id")
        self.assertEqual(r.status_code, 404)

    def test_hosts_persist_to_temp_data_dir(self):
        """hosts.json 写到临时 DATA_DIR（不污染真实目录）。"""
        self.client.post("/api/hosts", json={"name": "h", "host": FAKE_IP})
        path = os.path.join(self._tmp.name, "hosts.json")
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["host"], FAKE_IP)


class TestSystemApi(WebApiTestCase):
    """系统资源接口。"""

    def setUp(self):
        super().setUp()
        self._login()

    def test_system_status_structure(self):
        r = self.client.get("/api/system")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        for key in ("cpu", "memory", "disk", "network"):
            self.assertIn(key, body)
        self.assertIn("percent", body["cpu"])
        self.assertIn("percent", body["memory"])
        self.assertIn("percent", body["disk"])
        for section in ("cpu", "memory", "disk"):
            self.assertGreaterEqual(body[section]["percent"], 0)
            self.assertLessEqual(body[section]["percent"], 100)
        self.assertGreater(body["memory"]["total_mb"], 0)
        self.assertGreater(body["disk"]["total_gb"], 0)
        self.assertGreaterEqual(body["network"]["rx_bytes_per_sec"], 0)
        self.assertGreaterEqual(body["network"]["tx_bytes_per_sec"], 0)


class TestPages(WebApiTestCase):
    """HTML 页面路由（模板缺失时返回占位页，不报错）。"""

    def test_login_page_no_auth(self):
        r = self.anon.get("/login")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/html", r.headers["content-type"])

    def test_login_page_new_design(self):
        """v1.23.0：登录页是 Uiverse 渐变卡片设计，保留全部功能元素。"""
        r = self.anon.get("/login")
        self.assertEqual(r.status_code, 200)
        html = r.text
        for marker in ("M_Bot Oracle开源管理面板", "login-card", "remember-me",
                       "forgot-password-link", "login-form",
                       "telegram-form", "captcha-block", "login-error",
                       "lockout-info", "login-btn", "tg-send-btn",
                       "tg-login-btn", "tab-password", "tab-telegram",
                       "one-time-code"):
            self.assertIn(marker, html, marker)
        self.assertNotIn("login-side", html)
        self.assertNotIn("login-logo", html)

    def test_login_page_tg_code_autofill(self):
        """v1.25.0：验证码框用 one-time-code，手机浏览器才能在键盘上方
        提示短信/邮件里的一次性验证码；Telegram 表单不能带
        autocomplete="off"（会把这个提示压住）。"""
        r = self.anon.get("/login")
        html = r.text
        self.assertIn('id="tg-code"', html)
        self.assertIn('autocomplete="one-time-code"', html)
        self.assertNotIn('id="telegram-form" autocomplete="off"', html)

    def test_root_redirects_to_overview(self):
        r = self.anon.get("/", follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["location"], "/overview")

    def test_protected_pages_redirect_when_logged_out(self):
        for path in ("/overview", "/instances", "/create", "/volumes",
                     "/tasks", "/terminal"):
            r = self.anon.get(path, follow_redirects=False)
            self.assertEqual(r.status_code, 302, path)
            self.assertEqual(r.headers["location"], "/login", path)

    def test_protected_pages_ok_when_logged_in(self):
        self._login()
        for path in ("/overview", "/instances", "/create", "/volumes",
                     "/tasks", "/terminal"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)
            self.assertIn("text/html", r.headers["content-type"])

    def test_create_page_has_defaults_hint(self):
        """创建页提示可用域/子网自动填充，SSH 公钥文件是可选项。"""
        self._login()
        r = self.client.get("/create")
        self.assertEqual(r.status_code, 200)
        html = r.text
        self.assertIn("自动填充", html)
        self.assertIn("SSH 公钥文件路径（面板服务器上的路径，可选）", html)
        self.assertNotIn(
            'id="ssh-keys-file" placeholder="留空表示不用 SSH 公钥" required',
            html)

    def test_region_dropdown_on_profile_pages(self):
        """有账号下拉的 5 个页面都有二级区域下拉（region-select）。"""
        self._login()
        for path in ("/instances", "/volumes", "/quota", "/create", "/gfw"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)
            self.assertIn('id="region-select"', r.text, path)
            self.assertIn('id="region-wrap"', r.text, path)


if __name__ == "__main__":
    unittest.main()
