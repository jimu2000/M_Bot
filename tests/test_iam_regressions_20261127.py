#!/usr/bin/env python3
"""IAM 回归测试（2026-01-04，v2.14.3）。

覆盖两个真实故障（面板截图复现）：
  1. 删用户时调了 OCI SDK 里不存在的 list_users_in_group，
     直接报 AttributeError，删除保护根本没跑到。
  2. 新建用户备注留空时传 --description ""，服务端拒绝空描述
     （InvalidParameter: description must not be blank）。

本文件不用 mock 盖住关键判断：is_last_admin 直接跑真实逻辑，
组员数据用真实 SDK 的 UserGroupMembership 模型经 _model_to_dict
转出来（与线上 sdk_list_group_users 返回的形状一致），再断言
最后管理员判断对错。
"""
import unittest
from unittest.mock import MagicMock

from oci.client import OCIClient, _load_sdk, _model_to_dict
from oci.iam import create_user, is_last_admin


def _real_membership(user_id, group_id="ocid1.group.oc1..test"):
    """用真实 SDK 模型造一个组成员关系，再按线上路径转成 kebab-case 字典。"""
    sdk = _load_sdk()
    model = sdk.identity.models.UserGroupMembership(
        user_id=user_id, group_id=group_id,
        id="ocid1.membership.oc1..test",
        compartment_id="ocid1.tenancy.oc1..test",
        lifecycle_state="ACTIVE")
    return _model_to_dict(model)


class _FakeCli:
    """只替掉 CLI 的 SDK 读接口，不替 is_last_admin 本身。"""

    def __init__(self, memberships):
        self._memberships = memberships

    def sdk_list_groups(self, tenancy_id):
        return [{"name": "Administrators", "id": "ocid1.group.oc1..test"}]

    def sdk_list_group_users(self, compartment_id, group_id):
        assert compartment_id == "ocid1.tenancy.oc1..test"
        assert group_id == "ocid1.group.oc1..test"
        return list(self._memberships)


class TestIamRegressions(unittest.TestCase):
    def test_blank_description_gets_nonempty_default(self):
        cli = MagicMock()
        cli.run.return_value = {"data": {"id": "ocid1.user.test"}}
        create_user(cli, "ocid1.tenancy.test", "alice",
                    "alice@example.com", "")
        args = cli.run.call_args.args[0]
        desc = args[args.index("--description") + 1]
        self.assertEqual(desc, "Managed by M_Bot: alice")
        self.assertLessEqual(len(desc), 400)

    def test_blank_description_none_gets_default(self):
        cli = MagicMock()
        cli.run.return_value = {"data": {"id": "ocid1.user.test"}}
        create_user(cli, "ocid1.tenancy.test", "bob",
                    "bob@example.com", None)
        args = cli.run.call_args.args[0]
        self.assertEqual(args[args.index("--description") + 1],
                         "Managed by M_Bot: bob")

    def test_long_description_rejected(self):
        cli = MagicMock()
        with self.assertRaises(ValueError):
            create_user(cli, "ocid1.tenancy.test", "alice",
                        "alice@example.com", "x" * 401)
        cli.run.assert_not_called()

    def test_sdk_list_group_users_calls_membership_api(self):
        """sdk_list_group_users 必须调 list_user_group_memberships，
        而不是不存在的 list_users_in_group（v2.14.2 的故障根因）。"""
        cli = object.__new__(OCIClient)
        identity = MagicMock(spec=["list_user_group_memberships"])
        # 故意不给 list_users_in_group：如果代码还调它，这里会 AttributeError。
        membership = _real_membership("ocid1.user.oc1..aaa")
        resp = MagicMock()
        resp.data = [membership]
        identity.list_user_group_memberships.return_value = resp
        cli._sdk_service_client = MagicMock(return_value=identity)
        captured = {}

        def fake_paginate(self, operation, list_fn, *args, **kwargs):
            captured["operation"] = operation
            captured["list_fn"] = list_fn
            captured["args"] = args
            captured["kwargs"] = kwargs
            out = list_fn(*args, **kwargs)
            return getattr(out, "data", out)

        cli._sdk_paginate = fake_paginate.__get__(cli, OCIClient)
        result = cli.sdk_list_group_users("ocid1.tenancy.oc1..test",
                                          "ocid1.group.oc1..test")
        self.assertIs(captured["list_fn"],
                      identity.list_user_group_memberships)
        self.assertEqual(captured["args"], ("ocid1.tenancy.oc1..test",))
        self.assertEqual(captured["kwargs"],
                         {"group_id": "ocid1.group.oc1..test"})
        self.assertEqual(result[0]["user-id"], "ocid1.user.oc1..aaa")

    def test_is_last_admin_with_real_model_dicts(self):
        """最后管理员判断用真实模型转出的字典跑，不 mock is_last_admin。"""
        # 组里只剩目标用户一人 -> 是最后一个管理员。
        cli = _FakeCli([_real_membership("ocid1.user.oc1..aaa")])
        self.assertTrue(is_last_admin(cli, "ocid1.tenancy.oc1..test",
                                      "ocid1.user.oc1..aaa"))
        # 组里还有其他成员 -> 不是最后一个。
        cli = _FakeCli([_real_membership("ocid1.user.oc1..aaa"),
                        _real_membership("ocid1.user.oc1..bbb")])
        self.assertFalse(is_last_admin(cli, "ocid1.tenancy.oc1..test",
                                       "ocid1.user.oc1..aaa"))
        # 目标用户不在组里 -> 不是管理员。
        cli = _FakeCli([_real_membership("ocid1.user.oc1..bbb")])
        self.assertFalse(is_last_admin(cli, "ocid1.tenancy.oc1..test",
                                       "ocid1.user.oc1..aaa"))


if __name__ == "__main__":
    unittest.main()
