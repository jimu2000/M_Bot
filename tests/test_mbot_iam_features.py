"""Offline unit tests for M_Bot IAM additions; no real OCI calls.

Converted from ChatGPT's standalone script into a proper unittest module
so it runs under the normal test discovery. The placeholder email
api@example.com stands in for the operator's real address.
"""
import os
import tempfile
import unittest

from oci.iam import (
    create_api_key,
    create_bot_permission_user,
    create_or_reset_ui_password,
    get_profile_tenancy_id,
    get_profile_api_fingerprint,
    is_last_admin,
)


class FakeOCI:
    profile = "DEFAULT"
    region = "ap-singapore-1"

    def __init__(self):
        self.calls = []
        self.public_key_path = None
        self.api_keys = []
        self.groups = []
        self.group_users = {}

    def sdk_list_group_users(self, group_id):
        return list(self.group_users.get(group_id, []))

    def sdk_list_api_keys(self, user_id):
        return list(self.api_keys)

    def sdk_list_groups(self, tenancy_id):
        return list(self.groups)

    def sdk_list_users(self, tenancy_id):
        return []

    def run(self, args, timeout=None):
        self.calls.append(list(args))
        cmd = args[:3]
        if cmd == ["iam", "user", "ui-password"]:
            return {"data": {"password": "one-time-password"}}
        if cmd == ["iam", "group", "create"]:
            return {"data": {"id": "group-ocid"}}
        if cmd == ["iam", "user", "create"]:
            assert "--description" in args
            return {"data": {"id": "user-ocid", "name": "api_user",
                             "email": "api@example.com"}}
        if cmd == ["iam", "policy", "list"]:
            return {"data": []}
        if cmd == ["iam", "policy", "create"]:
            return {"data": {"id": "policy-ocid"}}
        if cmd == ["iam", "user", "list-groups"]:
            return {"data": []}
        if cmd == ["iam", "group", "add-user"]:
            return None
        if cmd == ["iam", "user", "api-key"]:
            self.public_key_path = args[args.index("--key-file") + 1]
            assert os.path.exists(self.public_key_path)
            return {"data": {"fingerprint": "aa:bb:cc"}}
        raise AssertionError(args)


class TestIamFeatures(unittest.TestCase):
    def test_ui_password_reset(self):
        cli = FakeOCI()
        self.assertEqual(
            create_or_reset_ui_password(cli, "user-ocid"),
            "one-time-password")
        pwd_call = next(
            c for c in cli.calls if c[:3] == ["iam", "user", "ui-password"])
        self.assertEqual(
            pwd_call,
            ["iam", "user", "ui-password", "create-or-reset",
             "--user-id", "user-ocid"])

    def test_bot_permission_user(self):
        cli = FakeOCI()
        bot = create_bot_permission_user(
            cli, "tenancy-ocid", "Group_for_Api_used", "api_user",
            "api@example.com", "Api_User_Policy")
        self.assertEqual(bot["user_id"], "user-ocid")
        self.assertTrue(bot["group_created"])
        self.assertTrue(bot["user_created"])
        self.assertTrue(bot["policy_created"])
        self.assertTrue(
            any(c[:3] == ["iam", "group", "add-user"] for c in cli.calls))

    def test_api_key_generation_and_limit(self):
        cli = FakeOCI()
        key = create_api_key(cli, "user-ocid")
        self.assertEqual(key["fingerprint"], "aa:bb:cc")
        self.assertTrue(key["private_key_pem"].startswith(b"-----BEGIN"))
        self.assertTrue(cli.public_key_path)
        self.assertFalse(os.path.exists(cli.public_key_path))
        cli.api_keys = [{"fingerprint": str(i)} for i in range(3)]
        with self.assertRaises(ValueError) as ctx:
            create_api_key(cli, "user-ocid")
        self.assertIn("3 个", str(ctx.exception))

    def test_profile_lookup_without_reading_key(self):
        # Profile identity fields are read without touching the
        # private-key path.
        with tempfile.NamedTemporaryFile("w", encoding="utf-8") as cfg:
            cfg.write("[DEFAULT]\nuser=user-ocid\nfingerprint=AA:BB:CC\n"
                      "tenancy=tenancy-ocid\nkey_file=/not/read/private.pem\n")
            cfg.flush()
            self.assertEqual(
                get_profile_tenancy_id("DEFAULT", cfg.name),
                "tenancy-ocid")
            self.assertEqual(
                get_profile_api_fingerprint("DEFAULT", cfg.name),
                "aa:bb:cc")

    def test_is_last_admin(self):
        cli = FakeOCI()
        cli.groups = [{"name": "Administrators", "id": "group-ocid"}]
        # 组里只剩目标用户一人 -> 是最后一个管理员。
        cli.group_users = {"group-ocid": [{"id": "user-ocid"}]}
        self.assertTrue(is_last_admin(cli, "tenancy-ocid", "user-ocid"))
        # 组里还有其他成员 -> 不是最后一个。
        cli.group_users = {"group-ocid": [{"id": "user-ocid"},
                                          {"id": "other-ocid"}]}
        self.assertFalse(is_last_admin(cli, "tenancy-ocid", "user-ocid"))
        # 目标用户不在 Administrators 组里 -> 不是管理员。
        cli.group_users = {"group-ocid": [{"id": "other-ocid"}]}
        self.assertFalse(is_last_admin(cli, "tenancy-ocid", "user-ocid"))
        # 没有 Administrators 组 -> 不拦。
        cli.groups = []
        self.assertFalse(is_last_admin(cli, "tenancy-ocid", "user-ocid"))


if __name__ == "__main__":
    unittest.main()
