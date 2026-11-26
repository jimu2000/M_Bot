#!/usr/bin/env python3
"""Web 面板性能修复与 profile 切换的测试（TestClient + mock，不碰真实 API）。

覆盖（问题 1/2/3，见任务书）：
  - GET /api/profiles：返回已配置账号列表（name/region/regionLabel），
    区域中文名映射，未知区域回退原代码，未登录 401。
  - GET /api/instances：公网 IP 并行取（3 台各睡 0.3 秒仍在 0.75 秒内
    返回，串行要 0.9 秒以上）；结果与实例一一对应（保序）；
    服务端缓存命中 / 过期 / refresh=1 绕过 / 写操作失效 / 按 profile
    与 state 分键。
  - GET /api/cloud-report/summary：各 profile 并行汇总（同上计时法）；
    顺序与 PROFILES 一致；单账号失败时 group 带 error 继续汇总；
    缓存命中 / 过期 / refresh=1 绕过。
  - 前端：四页模板含动态 profile 下拉（实例页不再有写死的 DEFAULT
    选项与错误注释）；各页面 JS 含 fillProfileSelect / cachedApi /
    withProfile / 按前缀清缓存；app.js 含前端缓存模块
    （Map + 15 分钟 TTL + 按前缀清除）与 cloud_active_profile 键。

运行：python3 -m unittest tests.test_web_perf_profiles -v
"""

import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from oci.client import OCIError
from web import config
from web.app import create_app
from web.auth import LOGIN_GUARD, SESSIONS, hash_password
from web.routes import cloudreport as cloudreport_routes
from web.routes import instances as instances_routes
from web.routes.cloudreport import clear_cloud_report_cache
from web.routes.instances import clear_instance_list_cache
from web.routes.volumes import clear_volumes_cache
from web.routes.boot_backups import clear_boot_backups_cache
from web.routes.objectstorage import clear_objectstorage_cache
from web.routes.quota import clear_quota_cache

FAKE_INSTANCE = "ocid1.instance.oc1..testxxxx"
FAKE_INSTANCE_2 = "ocid1.instance.oc1..testyyyy"
FAKE_INSTANCE_3 = "ocid1.instance.oc1..testzzzz"

TEST_PASSWORD = "perf-test-password-2026"

# 3 RUNNING instances (timing test for parallel IP fetch). 3 台 RUNNING 实例（并行取 IP 的计时测试用）。
FAKE_INSTANCES_3 = [
    {"id": FAKE_INSTANCE, "display-name": "instance-1",
     "shape": "VM.Standard.A1.Flex", "lifecycle-state": "RUNNING",
     "time-created": "2026-11-11T10:00:00.000Z"},
    {"id": FAKE_INSTANCE_2, "display-name": "instance-2",
     "shape": "VM.Standard.A1.Flex", "lifecycle-state": "RUNNING",
     "time-created": "2026-11-12T10:00:00.000Z"},
    {"id": FAKE_INSTANCE_3, "display-name": "instance-3",
     "shape": "VM.Standard.E2.1.Micro", "lifecycle-state": "RUNNING",
     "time-created": "2026-11-13T10:00:00.000Z"},
]

# The public IP for each instance (verifies parallel results map one-to-one onto instances). 每台实例对应的公网 IP（验证并行结果与实例一一对应）。
FAKE_IPS = {
    FAKE_INSTANCE: "203.0.113.10",
    FAKE_INSTANCE_2: "203.0.113.11",
    FAKE_INSTANCE_3: "203.0.113.12",
}

# 3 fake accounts (the 3rd has an unknown region, verifying the regionLabel fallback to the raw code). 3 个假账号（第 3 个区域未知，验证 regionLabel 回退原代码）。
FAKE_PROFILES = {
    "DEFAULT": {"region": "ap-singapore-1",
                "compartment_id": "ocid1.tenancy.oc1..testxxxx"},
    "ACCOUNT2": {"region": "us-sanjose-1",
                 "compartment_id": "ocid1.tenancy.oc1..testyyyy"},
    "ACCOUNT3": {"region": "us-ashburn-1",
                 "compartment_id": "ocid1.tenancy.oc1..testzzzz"},
}


def _fake_group(profile):
    """summarize_profile 的假返回值（面板形状的最小子集）。"""
    return {"profile": profile, "region": "ap-singapore-1",
            "instances": {"total": 1, "byState": {"RUNNING": 1}},
            "volumes": {"count": 1, "totalGB": 47},
            "quota": {"a1": {"usedOcpu": 1, "capOcpu": 4,
                             "usedMem": 6, "capMem": 24},
                      "e2": {"count": 0, "limit": 2}}}


class _Base(unittest.TestCase):
    """登录 + 清缓存的公共基类。"""

    @classmethod
    def setUpClass(cls):
        cls._hash_patcher = patch(
            "web.config.ADMIN_PASSWORD_HASH",
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
        clear_quota_cache()
        clear_boot_backups_cache()
        clear_objectstorage_cache()
        # VNIC 缓存函数不 mock 就会真的调 OCI（第 7 轮新增调用点）：
        # 本文件只测端点形状与缓存行为，VNIC 数据一律给空值。
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
        LOGIN_GUARD.reset()
        SESSIONS.reset()
        self.client = TestClient(self.app)
        self.anon = TestClient(self.app)
        self._login()

    def tearDown(self):
        self._data_dir_patcher.stop()
        self._tmp.cleanup()

    def _login(self):
        resp = self.client.post("/api/auth/login",
                                json={"username": "admin",
                                      "password": TEST_PASSWORD})
        self.assertEqual(resp.status_code, 200, resp.text)


class TestProfilesApi(_Base):
    """GET /api/profiles：账号列表接口。"""

    def test_list_profiles(self):
        with patch("web.config.PROFILES", FAKE_PROFILES), \
                patch("web.routes.profiles.get_region_counts",
                      return_value={"DEFAULT": 3, "ACCOUNT2": 1}):
            resp = self.client.get("/api/profiles")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["default"], "DEFAULT")
        self.assertEqual(
            [(p["name"], p["region"], p["regionLabel"], p["regionCount"],
              p["displayName"])
             for p in body["profiles"]],
            [("DEFAULT", "ap-singapore-1", "新加坡", 3, "新加坡（3 个区）"),
             ("ACCOUNT2", "us-sanjose-1", "圣何塞", 1, "圣何塞"),
             # An unlisted region falls back to the raw region code; no suffix is added when the region count is unknown. 未列出的区域回退显示原始区域代码；查不到区数时不加后缀。
             ("ACCOUNT3", "us-ashburn-1", "us-ashburn-1", None,
              "us-ashburn-1")])

    def test_list_profiles_anonymous_401(self):
        resp = self.anon.get("/api/profiles")
        self.assertEqual(resp.status_code, 401)

    def test_region_labels(self):
        self.assertEqual(config.get_region_label("ap-singapore-1"), "新加坡")
        self.assertEqual(config.get_region_label("us-sanjose-1"), "圣何塞")
        self.assertEqual(config.get_region_label("us-chicago-1"), "芝加哥")
        self.assertEqual(config.get_region_label("eu-frankfurt-1"),
                         "eu-frankfurt-1")

    def test_profile_display_name(self):
        """账号显示名：主区中文名；多区账号后缀（N 个区），与机器人一致。"""
        with patch("web.config.PROFILES", FAKE_PROFILES):
            self.assertEqual(config.profile_display_name("DEFAULT", 3),
                             "新加坡（3 个区）")
            self.assertEqual(config.profile_display_name("ACCOUNT2", 1),
                             "圣何塞")
            self.assertEqual(config.profile_display_name("ACCOUNT3", None),
                             "us-ashburn-1")


class TestInstancesParallelAndCache(_Base):
    """GET /api/instances：并行取 IP + 服务端缓存。"""

    def _slow_bulk(self, cli, instances, compartment_id, **kwargs):
        """模拟一次批量取 IP（约 0.3 秒）：旧的逐台串行要 0.9 秒以上。"""
        time.sleep(0.3)
        return [FAKE_IPS[inst["id"]] for inst in instances]

    def test_public_ips_fetched_in_parallel(self):
        with patch("web.routes.instances.cached_raw_instances",
                   return_value=FAKE_INSTANCES_3), \
             patch("web.routes.instances.attach_public_ips_bulk",
                   side_effect=self._slow_bulk) as mock_bulk:
            start = time.monotonic()
            resp = self.client.get("/api/instances")
            elapsed = time.monotonic() - start
        self.assertEqual(resp.status_code, 200)
        # 一次批量调用（约 0.3 秒），远小于旧的逐台串行 0.9 秒。
        self.assertLess(elapsed, 0.75, "耗时 %.2fs，疑似串行取 IP" % elapsed)
        self.assertEqual(mock_bulk.call_count, 1)
        # Results map one-to-one onto instances (order preserved). 结果与实例一一对应（保序）。
        got = {inst["id"]: inst["publicIp"]
               for inst in resp.json()["instances"]}
        self.assertEqual(got, FAKE_IPS)

    def test_list_cache_hit(self):
        with patch("web.routes.instances.cached_raw_instances",
                   return_value=FAKE_INSTANCES_3) as mock_list, \
             patch("web.routes.instances.attach_public_ips_bulk",
                   return_value=["203.0.113.10", "203.0.113.11",
                                 "203.0.113.12"]):
            self.assertEqual(self.client.get("/api/instances").status_code, 200)
            self.assertEqual(self.client.get("/api/instances").status_code, 200)
        # The second call hits the cache; list_instances was called only once. 第二次命中缓存，list_instances 只调了一次。
        self.assertEqual(mock_list.call_count, 1)

    def test_list_cache_refresh_bypass(self):
        with patch("web.routes.instances.cached_raw_instances",
                   return_value=FAKE_INSTANCES_3) as mock_list, \
             patch("web.routes.instances.attach_public_ips_bulk",
                   return_value=["203.0.113.10", "203.0.113.11",
                                 "203.0.113.12"]):
            self.client.get("/api/instances")
            resp = self.client.get("/api/instances?refresh=1")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(mock_list.call_count, 2)

    def test_list_cache_expiry(self):
        with patch("web.routes.instances.cached_raw_instances",
                   return_value=FAKE_INSTANCES_3) as mock_list, \
             patch("web.routes.instances.attach_public_ips_bulk",
                   return_value=["203.0.113.10", "203.0.113.11",
                                 "203.0.113.12"]):
            self.client.get("/api/instances")
            entry = instances_routes._LIST_CACHE[
                ("DEFAULT", "ap-singapore-1", "running")]
            entry["expires"] = time.monotonic() - 1
            self.client.get("/api/instances")
        self.assertEqual(mock_list.call_count, 2)

    def test_list_cache_keyed_by_profile_and_state(self):
        with patch("web.config.PROFILES", FAKE_PROFILES), \
             patch("web.deps.get_profile_regions",
                   return_value={"DEFAULT": ["ap-singapore-1",
                                             "ap-seoul-1"],
                                 "ACCOUNT2": ["us-sanjose-1"]}), \
             patch("web.routes.instances.cached_raw_instances",
                   return_value=FAKE_INSTANCES_3) as mock_list, \
             patch("web.routes.instances.attach_public_ips_bulk",
                   return_value=["203.0.113.10", "203.0.113.11",
                                 "203.0.113.12"]):
            self.client.get("/api/instances")
            self.client.get("/api/instances?profile=ACCOUNT2")
            self.client.get("/api/instances?state=all")
            self.client.get("/api/instances?region=ap-seoul-1")
        # Different profile / state / region are different cache keys, each queried once. 不同 profile / 不同 state / 不同 region 是不同缓存键，各调一次。
        self.assertEqual(mock_list.call_count, 4)

    def test_mutation_invalidates_cache(self):
        with patch("web.routes.instances.cached_raw_instances",
                   return_value=FAKE_INSTANCES_3) as mock_list, \
             patch("web.routes.instances.attach_public_ips_bulk",
                   return_value=["203.0.113.10", "203.0.113.11",
                                 "203.0.113.12"]), \
             patch("web.routes.instances.start_instance",
                   return_value=None):
            self.client.get("/api/instances")
            resp = self.client.post(
                "/api/instances/%s/start" % FAKE_INSTANCE)
            self.assertEqual(resp.status_code, 200)
            self.client.get("/api/instances")
        # After a write, the cache is invalidated and the list is re-queried. 写操作后缓存失效，列表重新查询。
        self.assertEqual(mock_list.call_count, 2)


class TestCloudReportParallelAndCache(_Base):
    """GET /api/cloud-report/summary：并行汇总 + 服务端缓存。"""

    def _slow_summarize(self, profile):
        time.sleep(0.3)
        return _fake_group(profile)

    def test_profiles_summarized_in_parallel(self):
        with patch("web.config.PROFILES", FAKE_PROFILES), \
             patch("web.routes.cloudreport.summarize_profile",
                   side_effect=self._slow_summarize):
            start = time.monotonic()
            resp = self.client.get("/api/cloud-report/summary")
            elapsed = time.monotonic() - start
        self.assertEqual(resp.status_code, 200)
        # Parallel: 3 accounts at 0.3s each, total far below the serial 0.9s. 并行：3 个账号各 0.3 秒，总耗时远小于串行的 0.9 秒。
        self.assertLess(elapsed, 0.75, "耗时 %.2fs，疑似串行汇总" % elapsed)
        # Order matches PROFILES (map preserves order). 顺序与 PROFILES 一致（map 保序）。
        self.assertEqual(
            [g["profile"] for g in resp.json()["groups"]],
            ["DEFAULT", "ACCOUNT2", "ACCOUNT3"])

    def test_failed_profile_keeps_error_group(self):
        def _summarize(profile):
            if profile == "ACCOUNT2":
                raise OCIError("quota exceeded", 500)
            return _fake_group(profile)

        with patch("web.config.PROFILES", FAKE_PROFILES), \
             patch("web.routes.cloudreport.summarize_profile",
                   side_effect=_summarize):
            resp = self.client.get("/api/cloud-report/summary")
        self.assertEqual(resp.status_code, 200)
        groups = resp.json()["groups"]
        self.assertEqual(len(groups), 3)
        self.assertEqual(groups[1]["profile"], "ACCOUNT2")
        self.assertIn("error", groups[1])
        self.assertNotIn("error", groups[0])
        self.assertNotIn("error", groups[2])

    def test_report_cache_hit(self):
        with patch("web.config.PROFILES", FAKE_PROFILES), \
             patch("web.routes.cloudreport.summarize_profile",
                   side_effect=_fake_group) as mock_sum:
            self.assertEqual(
                self.client.get("/api/cloud-report/summary").status_code, 200)
            self.assertEqual(
                self.client.get("/api/cloud-report/summary").status_code, 200)
        # The second call hits the cache; each account was summarized only once. 第二次命中缓存，每账号只汇总了一次。
        self.assertEqual(mock_sum.call_count, 3)

    def test_report_cache_refresh_bypass(self):
        with patch("web.config.PROFILES", FAKE_PROFILES), \
             patch("web.routes.cloudreport.summarize_profile",
                   side_effect=_fake_group) as mock_sum:
            self.client.get("/api/cloud-report/summary")
            resp = self.client.get("/api/cloud-report/summary?refresh=1")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(mock_sum.call_count, 6)

    def test_report_cache_expiry(self):
        with patch("web.config.PROFILES", FAKE_PROFILES), \
             patch("web.routes.cloudreport.summarize_profile",
                   side_effect=_fake_group) as mock_sum:
            self.client.get("/api/cloud-report/summary")
            entry = cloudreport_routes._REPORT_CACHE["summary"]
            entry["expires"] = time.monotonic() - 1
            self.client.get("/api/cloud-report/summary")
        self.assertEqual(mock_sum.call_count, 6)


class TestProfileTemplates(_Base):
    """前端：动态 profile 下拉 + 前端缓存模块（静态断言）。"""

    def _page(self, path):
        resp = self.client.get(path)
        self.assertEqual(resp.status_code, 200, path)
        return resp.text

    def _static_js(self, name):
        resp = self.client.get("/static/js/%s" % name)
        self.assertEqual(resp.status_code, 200, name)
        return resp.text

    def test_instances_page_dynamic_profile_select(self):
        html = self._page("/instances")
        self.assertIn('id="profile-select"', html)
        self.assertNotIn('<option value="DEFAULT"', html)
        self.assertNotIn("后端目前只暴露 DEFAULT", html)

    def test_volumes_quota_create_pages_have_profile_select(self):
        self.assertIn('id="profile-select"', self._page("/volumes"))
        self.assertIn('id="profile-select"', self._page("/quota"))
        create_html = self._page("/create")
        self.assertIn('id="profile"', create_html)
        # Custom image OCID (fill the target region's image for cross-region creation). 自定义镜像 OCID（跨区域创建时填该区域的镜像）。
        self.assertIn('id="image-id-custom"', create_html)

    def test_overview_page_has_cloud_report_refresh(self):
        self.assertIn('id="btn-refresh-cloud-report"',
                      self._page("/overview"))

    def test_app_js_frontend_cache_module(self):
        js = self._static_js("app.js")
        # Frontend cache: Map + 15-minute TTL + prefix-based clearing. 前端缓存：Map + 15 分钟 TTL + 按前缀清除。
        self.assertIn("new Map()", js)
        self.assertIn("15 * 60 * 1000", js)
        self.assertIn("cacheGet", js)
        self.assertIn("cacheSet", js)
        self.assertIn("cacheClear", js)
        self.assertIn("cachedApi", js)
        # Profile switching: same localStorage key name as the panel. profile 切换：与面板同款 localStorage 键名。
        self.assertIn("cloud_active_profile", js)
        self.assertIn("fillProfileSelect", js)
        self.assertIn("withProfile", js)
        self.assertIn("/api/profiles", js)

    def test_page_js_uses_profile_and_cache(self):
        instances_js = self._static_js("instances.js")
        self.assertIn("fillProfileSelect", instances_js)
        self.assertIn("cachedApi", instances_js)
        self.assertIn("withProfile", instances_js)
        self.assertIn('cacheClear("inst:")', instances_js)
        self.assertIn("refresh=1", instances_js)

        volumes_js = self._static_js("volumes.js")
        self.assertIn("fillProfileSelect", volumes_js)
        self.assertIn("cachedApi", volumes_js)
        self.assertIn("vol:list:", volumes_js)
        self.assertIn('cacheClear("vol:")', volumes_js)

        quota_js = self._static_js("quota.js")
        self.assertIn("fillProfileSelect", quota_js)
        self.assertIn("cachedApi", quota_js)
        self.assertIn("quota:summary:", quota_js)

        create_js = self._static_js("create.js")
        self.assertIn("fillProfileSelect", create_js)
        self.assertIn("withProfile", create_js)
        self.assertIn("image-id-custom", create_js)

        overview_js = self._static_js("overview.js")
        self.assertIn("cachedApi", overview_js)
        self.assertIn('"cloudreport"', overview_js)
        self.assertIn("refresh=1", overview_js)
        self.assertIn("btn-refresh-cloud-report", overview_js)


if __name__ == "__main__":
    unittest.main()
