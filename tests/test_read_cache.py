#!/usr/bin/env python3
"""读接口服务端缓存的单元测试（TestClient + mock OCI 函数，不碰真实 API）。

覆盖：配额（汇总/A1/体检/can-launch）、启动盘详情、对象存储 Bucket
列表、备份列表/额度。实例列表与云报告已有缓存，本文件只回归它们的
失效接线（写操作后配额缓存失效）。

运行：python3 -m unittest tests.test_read_cache -v

注意：
  - 测试密码用字符串拼接，不直接写字面量。
  - 缓存过期用直接改写缓存条目的 expires（不 sleep 等 30 秒）。
"""

import os
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from web import config
from web.app import create_app
from web.auth import LOGIN_GUARD, SESSIONS, hash_password
from web.routes.boot_backups import (
    _BOOT_BACKUPS_CACHE,
    clear_boot_backups_cache,
)
from web.routes.cloudreport import clear_cloud_report_cache
from web.routes.instances import clear_instance_list_cache
from web.routes.objectstorage import (
    _BUCKETS_CACHE,
    clear_objectstorage_cache,
)
from web.routes.quota import _QUOTA_CACHE, clear_quota_cache
from web.routes.volumes import _VOLUMES_CACHE, clear_volumes_cache
from web import tasks as tasks_mod
from web.tasks import TASKS

# Placeholders (no real OCIDs / IPs appear in tests). 占位符（测试里不出现真实 OCID / IP）。
FAKE_VOLUME = "ocid1.bootvolume.oc1..testxxxx"
FAKE_BACKUP = "ocid1.bootvolumebackup.oc1..testxxxx"
FAKE_BUCKET = "test-bucket"

# Test password built by concatenation (a literal would be redacted).
# 测试密码用拼接，不直接写字面量。
TEST_PASSWORD = "test-admin-" + "cache-2026"

FAKE_SUMMARY = {
    "a1": {"ocpu_used": 1, "ocpu_limit": 4, "ocpu_remaining": 3,
           "memory_used_gb": 6, "memory_limit_gb": 24,
           "memory_remaining_gb": 18, "instances": []},
    "e2": {"count": 1, "limit": 2, "remaining": 1, "instances": []},
    "boot_volume": {"used_gb": 47, "limit_gb": 200, "remaining_gb": 153,
                    "volumes": []},
    "overall_ok": True,
}

FAKE_A1_USAGE = {
    "ocpu_used": 1, "ocpu_limit": 4,
    "memory_used_gb": 6, "memory_limit_gb": 24,
    "instances": [],
}

FAKE_HEALTH = {"checks": [], "suggestions": []}

FAKE_VOLUMES = [
    {"id": FAKE_VOLUME, "display-name": "boot-volume-a1",
     "size-in-gbs": 47, "lifecycle-state": "AVAILABLE",
     "availability-domain": "AD-1"},
]

FAKE_BACKUPS = [
    {"id": FAKE_BACKUP, "display-name": "backup-1",
     "boot-volume-id": FAKE_VOLUME, "size-in-gbs": 47,
     "lifecycle-state": "AVAILABLE", "source-type": "MANUAL",
     "type": "FULL", "time-created": "2026-11-20T00:00:00Z",
     "image-id": None},
]

FAKE_BUCKETS = [
    {"name": FAKE_BUCKET, "time-created": "2026-11-20T00:00:00Z",
     "public-access-type": "NoPublicAccess", "storage-tier": "Standard"},
]


def _expire_all(cache):
    """把缓存里全部条目标记为已过期（模拟 30 秒 TTL 走完）。"""
    for entry in cache.values():
        entry["expires"] = time.monotonic() - 1


class ReadCacheTestCase(unittest.TestCase):
    """API 测试基类：建 app、临时 DATA_DIR、登录、清全部缓存。"""

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
        clear_quota_cache()
        clear_boot_backups_cache()
        clear_objectstorage_cache()
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


class TestQuotaCache(ReadCacheTestCase):
    """GET /api/quota、/api/quota/a1、/api/quota/a1-health、POST can-launch。"""

    @patch("web.routes.quota.get_free_tier_summary",
           return_value=FAKE_SUMMARY)
    def test_summary_second_call_uses_cache(self, mock_summary):
        r1 = self.client.get("/api/quota")
        r2 = self.client.get("/api/quota")
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r1.json(), r2.json())
        mock_summary.assert_called_once()

    @patch("web.routes.quota.get_free_tier_summary",
           return_value=FAKE_SUMMARY)
    def test_summary_refresh_bypasses_cache(self, mock_summary):
        self.client.get("/api/quota")
        r = self.client.get("/api/quota?refresh=true")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(mock_summary.call_count, 2)

    @patch("web.routes.quota.get_free_tier_summary",
           return_value=FAKE_SUMMARY)
    def test_summary_expired_cache_recalls_oci(self, mock_summary):
        self.client.get("/api/quota")
        _expire_all(_QUOTA_CACHE)
        r = self.client.get("/api/quota")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(mock_summary.call_count, 2)

    @patch("web.routes.quota.get_free_tier_summary",
           return_value=FAKE_SUMMARY)
    def test_summary_cache_keyed_by_profile(self, mock_summary):
        self.client.get("/api/quota")
        self.client.get("/api/quota?profile=ACCOUNT2")
        # Different profile -> separate cache entry -> OCI called again.
        # 不同账号是不同的缓存键，要再调一次 OCI。
        self.assertEqual(mock_summary.call_count, 2)

    @patch("web.routes.quota.get_a1_usage", return_value=FAKE_A1_USAGE)
    def test_a1_second_call_uses_cache(self, mock_usage):
        self.client.get("/api/quota/a1")
        r = self.client.get("/api/quota/a1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["a1"]["usedOcpu"], 1)
        mock_usage.assert_called_once()

    @patch("web.routes.quota.get_a1_usage", return_value=FAKE_A1_USAGE)
    def test_a1_refresh_bypasses_cache(self, mock_usage):
        self.client.get("/api/quota/a1")
        self.client.get("/api/quota/a1?refresh=true")
        self.assertEqual(mock_usage.call_count, 2)

    @patch("web.routes.quota.a1_health_check", return_value=FAKE_HEALTH)
    def test_a1_health_second_call_uses_cache(self, mock_health):
        self.client.get("/api/quota/a1-health")
        r = self.client.get("/api/quota/a1-health")
        self.assertEqual(r.status_code, 200)
        mock_health.assert_called_once()

    @patch("web.routes.quota.can_launch", return_value=(True, ""))
    def test_can_launch_cached_per_body(self, mock_can):
        body = {"shape": "VM.Standard.A1.Flex", "ocpus": 1, "memory_gb": 6}
        self.client.post("/api/quota/can-launch", json=body)
        r = self.client.post("/api/quota/can-launch", json=body)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["can_launch"])
        mock_can.assert_called_once()
        # Different spec -> different cache key -> OCI called again.
        # 不同规格是不同的缓存键，要再调一次 OCI。
        other = {"shape": "VM.Standard.E2.1.Micro",
                 "ocpus": 0, "memory_gb": 0}
        self.client.post("/api/quota/can-launch", json=other)
        self.assertEqual(mock_can.call_count, 2)

    @patch("web.routes.quota.can_launch", return_value=(True, ""))
    def test_can_launch_refresh_bypasses_cache(self, mock_can):
        body = {"shape": "VM.Standard.A1.Flex", "ocpus": 1, "memory_gb": 6}
        self.client.post("/api/quota/can-launch", json=body)
        self.client.post("/api/quota/can-launch?refresh=true", json=body)
        self.assertEqual(mock_can.call_count, 2)

    @patch("web.routes.quota.get_free_tier_summary",
           return_value=FAKE_SUMMARY)
    def test_quota_cache_cleared_by_volume_delete(self, mock_summary):
        # Quota cached, then a volume write op (delete) clears it, so the
        # next quota read calls OCI again. 配额已缓存，删盘写操作清掉它，
        # 下次读配额要重新调 OCI。
        self.client.get("/api/quota")
        with patch("web.routes.volumes.delete_boot_volume"):
            r = self.client.delete(f"/api/volumes/{FAKE_VOLUME}")
        self.assertEqual(r.status_code, 200)
        self.client.get("/api/quota")
        self.assertEqual(mock_summary.call_count, 2)

    @patch("web.routes.quota.get_free_tier_summary",
           return_value=FAKE_SUMMARY)
    def test_quota_cache_cleared_by_instance_terminate(self, mock_summary):
        # Quota cached, then instance terminate clears it.
        # 配额已缓存，终止实例清掉它，下次读配额要重新调 OCI。
        self.client.get("/api/quota")
        with patch("web.routes.instances.terminate_instance"):
            r = self.client.post(
                f"/api/instances/{FAKE_VOLUME}/terminate",
                json={"confirm": True, "preserve_boot_volume": True})
        self.assertEqual(r.status_code, 200)
        self.client.get("/api/quota")
        self.assertEqual(mock_summary.call_count, 2)


class TestVolumeDetailCache(ReadCacheTestCase):
    """GET /api/volumes/{id} 详情缓存。"""

    @patch("web.routes.volumes.get_boot_volume",
           return_value=FAKE_VOLUMES[0])
    def test_detail_second_call_uses_cache(self, mock_get):
        r1 = self.client.get(f"/api/volumes/{FAKE_VOLUME}")
        r2 = self.client.get(f"/api/volumes/{FAKE_VOLUME}")
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r1.json(), r2.json())
        mock_get.assert_called_once()

    @patch("web.routes.volumes.get_boot_volume",
           return_value=FAKE_VOLUMES[0])
    def test_detail_refresh_bypasses_cache(self, mock_get):
        self.client.get(f"/api/volumes/{FAKE_VOLUME}")
        self.client.get(f"/api/volumes/{FAKE_VOLUME}?refresh=true")
        self.assertEqual(mock_get.call_count, 2)

    @patch("web.routes.volumes.get_boot_volume",
           return_value=FAKE_VOLUMES[0])
    def test_detail_expired_cache_recalls_oci(self, mock_get):
        self.client.get(f"/api/volumes/{FAKE_VOLUME}")
        _expire_all(_VOLUMES_CACHE)
        self.client.get(f"/api/volumes/{FAKE_VOLUME}")
        self.assertEqual(mock_get.call_count, 2)

    @patch("web.routes.volumes.get_boot_volume",
           return_value=FAKE_VOLUMES[0])
    def test_detail_cleared_by_delete(self, mock_get):
        # Detail cached, then deleting the volume clears it.
        # 详情已缓存，删盘清掉它，下次读详情要重新调 OCI。
        self.client.get(f"/api/volumes/{FAKE_VOLUME}")
        with patch("web.routes.volumes.delete_boot_volume"):
            r = self.client.delete(f"/api/volumes/{FAKE_VOLUME}")
        self.assertEqual(r.status_code, 200)
        self.client.get(f"/api/volumes/{FAKE_VOLUME}")
        self.assertEqual(mock_get.call_count, 2)

    @patch("web.routes.volumes.cached_raw_boot_volumes",
           return_value=FAKE_VOLUMES)
    @patch("web.routes.volumes._format_volumes",
           return_value=[{"volumeId": FAKE_VOLUME}])
    def test_list_second_call_uses_cache(self, mock_fmt, mock_list):
        # Regression: the pre-existing list cache still works.
        # 回归：已有的列表缓存仍有效。
        self.client.get("/api/volumes")
        r = self.client.get("/api/volumes")
        self.assertEqual(r.status_code, 200)
        mock_list.assert_called_once()


class TestObjectstorageCache(ReadCacheTestCase):
    """GET /api/objectstorage/buckets 列表缓存。"""

    @patch("web.routes.objectstorage.get_namespace",
           return_value="fake-namespace")
    @patch("web.routes.objectstorage.list_buckets",
           return_value=FAKE_BUCKETS)
    def test_buckets_second_call_uses_cache(self, mock_list, mock_ns):
        r1 = self.client.get("/api/objectstorage/buckets")
        r2 = self.client.get("/api/objectstorage/buckets")
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r1.json(), r2.json())
        mock_list.assert_called_once()
        mock_ns.assert_called_once()

    @patch("web.routes.objectstorage.get_namespace",
           return_value="fake-namespace")
    @patch("web.routes.objectstorage.list_buckets",
           return_value=FAKE_BUCKETS)
    def test_buckets_refresh_bypasses_cache(self, mock_list, mock_ns):
        self.client.get("/api/objectstorage/buckets")
        self.client.get("/api/objectstorage/buckets?refresh=true")
        self.assertEqual(mock_list.call_count, 2)

    @patch("web.routes.objectstorage.get_namespace",
           return_value="fake-namespace")
    @patch("web.routes.objectstorage.list_buckets",
           return_value=FAKE_BUCKETS)
    def test_buckets_expired_cache_recalls_oci(self, mock_list, mock_ns):
        self.client.get("/api/objectstorage/buckets")
        _expire_all(_BUCKETS_CACHE)
        self.client.get("/api/objectstorage/buckets")
        self.assertEqual(mock_list.call_count, 2)

    @patch("web.routes.objectstorage.get_namespace",
           return_value="fake-namespace")
    @patch("web.routes.objectstorage.list_buckets",
           return_value=FAKE_BUCKETS)
    def test_buckets_cleared_by_create(self, mock_list, mock_ns):
        # Buckets cached, then creating a bucket clears the cache.
        # 列表已缓存，新建 Bucket 清掉它，下次读要重新调 OCI。
        self.client.get("/api/objectstorage/buckets")
        with patch("web.routes.objectstorage.create_bucket",
                   return_value=FAKE_BUCKETS[0]):
            r = self.client.post("/api/objectstorage/buckets",
                                 json={"name": "new-bucket"})
        self.assertEqual(r.status_code, 201)
        self.client.get("/api/objectstorage/buckets")
        self.assertEqual(mock_list.call_count, 2)

    @patch("web.routes.objectstorage.get_namespace",
           return_value="fake-namespace")
    @patch("web.routes.objectstorage.list_buckets",
           return_value=FAKE_BUCKETS)
    def test_buckets_cleared_by_delete(self, mock_list, mock_ns):
        # Buckets cached, then deleting a bucket clears the cache.
        # 列表已缓存，删除 Bucket 清掉它，下次读要重新调 OCI。
        self.client.get("/api/objectstorage/buckets")
        with patch("web.routes.objectstorage.delete_bucket"):
            r = self.client.delete(
                f"/api/objectstorage/buckets/{FAKE_BUCKET}")
        self.assertEqual(r.status_code, 200)
        self.client.get("/api/objectstorage/buckets")
        self.assertEqual(mock_list.call_count, 2)


class TestBootBackupsCache(ReadCacheTestCase):
    """GET /api/boot-backups、GET /api/boot-backups/quota 缓存。"""

    @patch("web.routes.boot_backups.list_boot_volume_backups",
           return_value=FAKE_BACKUPS)
    def test_list_second_call_uses_cache(self, mock_list):
        r1 = self.client.get("/api/boot-backups")
        r2 = self.client.get("/api/boot-backups")
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r1.json(), r2.json())
        mock_list.assert_called_once()

    @patch("web.routes.boot_backups.list_boot_volume_backups",
           return_value=FAKE_BACKUPS)
    def test_list_refresh_bypasses_cache(self, mock_list):
        self.client.get("/api/boot-backups")
        self.client.get("/api/boot-backups?refresh=true")
        self.assertEqual(mock_list.call_count, 2)

    @patch("web.routes.boot_backups.list_boot_volume_backups",
           return_value=FAKE_BACKUPS)
    def test_list_expired_cache_recalls_oci(self, mock_list):
        self.client.get("/api/boot-backups")
        _expire_all(_BOOT_BACKUPS_CACHE)
        self.client.get("/api/boot-backups")
        self.assertEqual(mock_list.call_count, 2)

    @patch("web.routes.boot_backups.check_backup_quota",
           return_value=(2, 5, False, FAKE_BACKUPS[0]))
    def test_quota_second_call_uses_cache(self, mock_quota):
        r1 = self.client.get("/api/boot-backups/quota")
        r2 = self.client.get("/api/boot-backups/quota")
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r1.json(), r2.json())
        mock_quota.assert_called_once()

    @patch("web.routes.boot_backups.check_backup_quota",
           return_value=(2, 5, False, FAKE_BACKUPS[0]))
    def test_quota_refresh_bypasses_cache(self, mock_quota):
        self.client.get("/api/boot-backups/quota")
        self.client.get("/api/boot-backups/quota?refresh=true")
        self.assertEqual(mock_quota.call_count, 2)

    @patch("web.routes.boot_backups.list_boot_volume_backups",
           return_value=FAKE_BACKUPS)
    def test_list_cleared_by_delete(self, mock_list):
        # List cached, then deleting a backup clears it.
        # 列表已缓存，删除备份清掉它，下次读要重新调 OCI。
        self.client.get("/api/boot-backups")
        with patch("web.routes.boot_backups.delete_boot_volume_backup"):
            r = self.client.delete(
                f"/api/boot-backups/{FAKE_BACKUP}?confirm=true")
        self.assertEqual(r.status_code, 200)
        self.client.get("/api/boot-backups")
        self.assertEqual(mock_list.call_count, 2)

    @patch("web.routes.boot_backups.list_boot_volume_backups",
           return_value=FAKE_BACKUPS)
    def test_list_cleared_by_create(self, mock_list):
        # List cached, then creating a backup clears it.
        # 列表已缓存，新建备份清掉它，下次读要重新调 OCI。
        self.client.get("/api/boot-backups")
        with patch("web.routes.boot_backups.is_paid_profile",
                   return_value=True), \
             patch("web.routes.boot_backups.create_boot_volume_backup",
                   return_value=FAKE_BACKUPS[0]):
            r = self.client.post("/api/boot-backups",
                                 json={"boot_volume_id": FAKE_VOLUME})
        self.assertEqual(r.status_code, 200)
        self.client.get("/api/boot-backups")
        self.assertEqual(mock_list.call_count, 2)

    @patch("web.routes.boot_backups.check_backup_quota",
           return_value=(2, 5, False, FAKE_BACKUPS[0]))
    def test_quota_cleared_by_set_paid(self, mock_quota):
        # Quota cached, then changing the paid flag clears it (the flag
        # changes the quota payload). 额度已缓存，改付费标记清掉它。
        self.client.get("/api/boot-backups/quota")
        with patch("web.routes.boot_backups.set_paid_profile"):
            r = self.client.post("/api/boot-backups/paid",
                                 json={"paid": True})
        self.assertEqual(r.status_code, 200)
        self.client.get("/api/boot-backups/quota")
        self.assertEqual(mock_quota.call_count, 2)


if __name__ == "__main__":
    unittest.main()
