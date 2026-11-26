#!/usr/bin/env python3
"""第 2 轮性能修复测试：Web 层 OCIClient 按 (profile, region) 缓存复用。

覆盖 web/deps.py：
  - get_oci_client：同一账号+区域两次调用返回同一对象（只建一次
    OCIClient）；区域不同是另一个客户端；未知 profile 仍抛
    ValueError；显式 region 不在订阅列表时抛 ValueError。
  - clear_oci_client_cache：不传 profile 全清；传 profile 只清该账号。

画像依据：强制刷新时每次新建 OCIClient 都要重建 service client，
SDK 重读私钥并解析（load_pem_private_key 约 0.08 秒/次调用，
每次刷新约 5 次调用合计约 0.39 秒）+ 建 TLS 连接、加载 CA，
占单次强制刷新 CPU（0.64 秒）的约 83%。复用后私钥只解析一次。
"""

import unittest
from unittest import mock

from web import deps


class TestWebOciClientCache(unittest.TestCase):
    """get_oci_client 按 (profile, region) 缓存复用，不再每次新建。"""

    _PROFILES = {
        "DEFAULT": {"region": "ap-singapore-1",
                    "compartment_id": "ocid1.tenancy.oc1..aaa"},
        "ACCOUNT2": {"region": "us-sanjose-1",
                     "compartment_id": "ocid1.tenancy.oc1..bbb"},
    }

    def setUp(self):
        deps.clear_oci_client_cache()

    def tearDown(self):
        deps.clear_oci_client_cache()

    def _patch_config(self):
        """把 get_profile_config 与订阅区域列表换成测试用的假数据。"""

        def _fake_get_profile_config(profile=None):
            name = profile or "DEFAULT"
            cfg = self._PROFILES.get(name)
            if cfg is None:
                raise ValueError(f"未知 profile：{name}")
            return cfg

        cfg = mock.patch.object(
            deps.config, "get_profile_config",
            side_effect=_fake_get_profile_config)
        regions = mock.patch.object(
            deps, "get_profile_regions",
            return_value={"DEFAULT": ["ap-singapore-1", "ap-seoul-1"],
                          "ACCOUNT2": ["us-sanjose-1"]})
        return cfg, regions

    def test_same_profile_region_returns_same_client(self):
        """同一账号+区域两次调用返回同一对象（只建一次）。"""
        cfg, regions = self._patch_config()
        with cfg, regions, mock.patch(
                "web.deps.OCIClient") as mock_cls:
            # Fresh object per construction: identity only holds when
            # the second call reuses the cached client. 每次构造返回
            # 新对象：只有缓存复用时两次才拿到同一对象。
            mock_cls.side_effect = lambda profile, region: object()
            first = deps.get_oci_client(profile="DEFAULT")
            second = deps.get_oci_client(profile="DEFAULT")
        self.assertIs(first, second)
        mock_cls.assert_called_once_with(profile="DEFAULT",
                                         region="ap-singapore-1")

    def test_different_region_returns_different_client(self):
        """区域不同是另一个客户端；不同账号也是另一个客户端。"""
        cfg, regions = self._patch_config()
        with cfg, regions, mock.patch(
                "web.deps.OCIClient") as mock_cls:
            mock_cls.side_effect = lambda profile, region: (
                f"{profile}:{region}")
            home = deps.get_oci_client(profile="DEFAULT")
            seoul = deps.get_oci_client(
                profile="DEFAULT", region="ap-seoul-1")
            other = deps.get_oci_client(profile="ACCOUNT2")
        self.assertEqual(home, "DEFAULT:ap-singapore-1")
        self.assertEqual(seoul, "DEFAULT:ap-seoul-1")
        self.assertEqual(other, "ACCOUNT2:us-sanjose-1")
        self.assertEqual(mock_cls.call_count, 3)

    def test_clear_all_rebuilds_client(self):
        """全清后下次调用重建客户端（新对象）。"""
        cfg, regions = self._patch_config()
        with cfg, regions, mock.patch(
                "web.deps.OCIClient") as mock_cls:
            mock_cls.side_effect = lambda profile, region: object()
            first = deps.get_oci_client(profile="DEFAULT")
            deps.clear_oci_client_cache()
            second = deps.get_oci_client(profile="DEFAULT")
        self.assertIsNot(first, second)
        self.assertEqual(mock_cls.call_count, 2)

    def test_clear_one_profile_keeps_other(self):
        """只清某账号：该账号重建，其他账号仍复用旧对象。"""
        cfg, regions = self._patch_config()
        with cfg, regions, mock.patch(
                "web.deps.OCIClient") as mock_cls:
            mock_cls.side_effect = lambda profile, region: object()
            default_first = deps.get_oci_client(profile="DEFAULT")
            other_first = deps.get_oci_client(profile="ACCOUNT2")
            deps.clear_oci_client_cache(profile="DEFAULT")
            default_second = deps.get_oci_client(profile="DEFAULT")
            other_second = deps.get_oci_client(profile="ACCOUNT2")
        self.assertIsNot(default_first, default_second)
        self.assertIs(other_first, other_second)

    def test_unknown_profile_raises(self):
        """未知 profile 仍抛 ValueError（不建客户端）。"""
        cfg, regions = self._patch_config()
        with cfg, regions, mock.patch(
                "web.deps.OCIClient") as mock_cls:
            with self.assertRaises(ValueError):
                deps.get_oci_client(profile="NOPE")
            mock_cls.assert_not_called()

    def test_unsubscribed_region_raises(self):
        """显式 region 不在订阅列表时抛 ValueError。"""
        cfg, regions = self._patch_config()
        with cfg, regions, mock.patch(
                "web.deps.OCIClient") as mock_cls:
            with self.assertRaises(ValueError):
                deps.get_oci_client(profile="DEFAULT",
                                    region="eu-frankfurt-1")
            mock_cls.assert_not_called()


if __name__ == "__main__":
    unittest.main()
