"""Offline unit tests for M_Bot IAM additions; no real OCI calls.

Converted from ChatGPT's standalone script into a proper unittest module
so it runs under the normal test discovery. The placeholder email
api@example.com stands in for the operator's real address.
"""
import os
import tempfile
import unittest

from oci.iam import (
    build_oci_config,
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
        self.policies = []

    def sdk_list_group_users(self, compartment_id, group_id):
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
            return {"data": list(self.policies)}
        if cmd == ["iam", "policy", "create"]:
            return {"data": {"id": "policy-ocid"}}
        if cmd == ["iam", "policy", "update"]:
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

    def test_ui_password_reset_translates_oci_errors(self):
        # OCI 拒绝时不把原始 ServiceError JSON 甩给调用方，翻译成中文。
        class FailingOCI(FakeOCI):
            def __init__(self, message):
                super().__init__()
                self.message = message

            def run(self, args, timeout=None):
                raise RuntimeError(self.message)

        with self.assertRaises(RuntimeError) as ctx:
            create_or_reset_ui_password(
                FailingOCI("ServiceError: NotAuthorizedOrNotFound"),
                "user-ocid")
        self.assertIn("manage users in tenancy", str(ctx.exception))
        with self.assertRaises(RuntimeError) as ctx:
            create_or_reset_ui_password(
                FailingOCI("ServiceError: IdcsConversionError"),
                "user-ocid")
        self.assertIn("ACTIVE", str(ctx.exception))

    def test_build_oci_config(self):
        config = build_oci_config(
            "api_user", "ocid1.user.oc1..testuser0001",
            "aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99",
            "ocid1.tenancy.oc1..testtenant1", "ap-singapore-1")
        self.assertEqual(config,
                         "[api_user]\n"
                         "user=ocid1.user.oc1..testuser0001\n"
                         "fingerprint=aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99\n"
                         "key_file=/root/oci_api_key.pem\n"
                         "tenancy=ocid1.tenancy.oc1..testtenant1\n"
                         "region=ap-singapore-1\n")
        with self.assertRaises(ValueError):
            build_oci_config("", "user-ocid", "fp", "tenancy", "region")

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
        policy_call = next(c for c in cli.calls
                           if c[:3] == ["iam", "policy", "create"])
        policy_json = policy_call[policy_call.index("--statements") + 1]
        self.assertIn("manage users in tenancy", policy_json)

    def test_existing_policy_is_extended_without_losing_old_statements(self):
        cli = FakeOCI()
        cli.policies = [{
            "id": "policy-ocid", "name": "Api_User_Policy",
            "statements": [
                "Allow group Group_for_Api_used to manage instance-family in tenancy",
                "Allow group Group_for_Api_used to manage volume-family in tenancy",
                "Allow group Group_for_Api_used to manage virtual-network-family in tenancy",
            ],
        }]
        create_bot_permission_user(
            cli, "tenancy-ocid", "Group_for_Api_used", "api_user",
            "api@example.com", "Api_User_Policy")
        update_call = next(c for c in cli.calls
                           if c[:3] == ["iam", "policy", "update"])
        statements = update_call[update_call.index("--statements") + 1]
        self.assertIn("manage users in tenancy", statements)
        self.assertIn("manage instance-family", statements)
        self.assertIn("--force", update_call)

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
        # sdk_list_group_users 返回成员关系，user-id 才是用户 OCID。
        cli.group_users = {"group-ocid": [{"user-id": "user-ocid"}]}
        self.assertTrue(is_last_admin(cli, "tenancy-ocid", "user-ocid"))
        # 组里还有其他成员 -> 不是最后一个。
        cli.group_users = {"group-ocid": [{"user-id": "user-ocid"},
                                          {"user-id": "other-ocid"}]}
        self.assertFalse(is_last_admin(cli, "tenancy-ocid", "user-ocid"))
        # 目标用户不在 Administrators 组里 -> 不是管理员。
        cli.group_users = {"group-ocid": [{"user-id": "other-ocid"}]}
        self.assertFalse(is_last_admin(cli, "tenancy-ocid", "user-ocid"))
        # 没有 Administrators 组 -> 不拦。
        cli.groups = []
        self.assertFalse(is_last_admin(cli, "tenancy-ocid", "user-ocid"))


if __name__ == "__main__":
    unittest.main()
