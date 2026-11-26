#!/usr/bin/env python3
"""oci/iam.py 的单元测试（mock cli.run，不碰真实 API）。

运行：python3 -m unittest tests.test_iam -v

注意：update_user / delete_user / delete_api_key 是写操作，
测试只 mock 命令构造检查，绝不对真实 API 执行。
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oci.iam import (
    MAX_DESCRIPTION_LEN,
    add_user_to_group,
    create_user,
    create_or_reset_ui_password,
    delete_api_key,
    delete_user,
    get_group_id_by_name,
    get_profile_user_id,
    list_api_keys,
    update_user,
)

# Placeholders (no real OCID in tests). 占位符（测试里不出现真实 OCID）。
FAKE_USER = "ocid1.user.oc1..xxxxxxxx"
FAKE_USER2 = "ocid1.user.oc1..yyyyyyyy"
FAKE_FP = "aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99"


def _mock_cli(return_value):
    """造一个 cli.run 返回固定值的 mock OCIClient。"""
    cli = MagicMock()
    cli.run.return_value = return_value
    return cli


class TestUpdateUser(unittest.TestCase):
    def test_description_only(self):
        cli = _mock_cli({"data": {"id": FAKE_USER, "description": "new"}})
        result = update_user(cli, FAKE_USER, description="new")
        self.assertEqual(result["description"], "new")
        cli.run.assert_called_once_with(
            ["iam", "user", "update", "--user-id", FAKE_USER,
             "--description", "new", "--force"])

    def test_email_only(self):
        cli = _mock_cli({"data": {"id": FAKE_USER,
                                  "email": "a@example.com"}})
        update_user(cli, FAKE_USER, email="a@example.com")
        cli.run.assert_called_once_with(
            ["iam", "user", "update", "--user-id", FAKE_USER,
             "--email", "a@example.com", "--force"])

    def test_both(self):
        cli = _mock_cli({"data": {}})
        update_user(cli, FAKE_USER, description="d",
                    email="a@example.com")
        cli.run.assert_called_once_with(
            ["iam", "user", "update", "--user-id", FAKE_USER,
             "--description", "d", "--email", "a@example.com", "--force"])

    def test_neither_rejected(self):
        cli = _mock_cli({"data": {}})
        with self.assertRaises(ValueError):
            update_user(cli, FAKE_USER)
        cli.run.assert_not_called()

    def test_empty_user_id_rejected(self):
        cli = _mock_cli({"data": {}})
        with self.assertRaises(ValueError):
            update_user(cli, "", description="d")
        cli.run.assert_not_called()

    def test_description_too_long_rejected(self):
        cli = _mock_cli({"data": {}})
        with self.assertRaises(ValueError):
            update_user(cli, FAKE_USER,
                        description="x" * (MAX_DESCRIPTION_LEN + 1))
        cli.run.assert_not_called()


class TestDeleteUser(unittest.TestCase):
    def test_command_args(self):
        cli = _mock_cli(None)
        self.assertIsNone(delete_user(cli, FAKE_USER))
        cli.run.assert_called_once_with(
            ["iam", "user", "delete", "--user-id", FAKE_USER, "--force"])

    def test_empty_user_id_rejected(self):
        cli = _mock_cli(None)
        with self.assertRaises(ValueError):
            delete_user(cli, "")
        cli.run.assert_not_called()


class TestListApiKeys(unittest.TestCase):
    def test_returns_simplified_list(self):
        cli = _mock_cli(None)
        cli.sdk_list_api_keys.return_value = [{
            "fingerprint": FAKE_FP,
            "lifecycle-state": "ACTIVE",
            "time-created": "2026-11-20T00:00:00Z",
            "key-value": "ssh-rsa AAAA...（不应返回）",
            "user-id": FAKE_USER,
        }]
        result = list_api_keys(cli, FAKE_USER)
        self.assertEqual(result, [{
            "fingerprint": FAKE_FP,
            "lifecycle-state": "ACTIVE",
            "time-created": "2026-11-20T00:00:00Z",
        }])
        cli.sdk_list_api_keys.assert_called_once_with(FAKE_USER)
        cli.run.assert_not_called()

    def test_empty(self):
        cli = _mock_cli(None)
        cli.sdk_list_api_keys.return_value = []
        self.assertEqual(list_api_keys(cli, FAKE_USER), [])

    def test_empty_user_id_rejected(self):
        cli = _mock_cli({"data": []})
        with self.assertRaises(ValueError):
            list_api_keys(cli, "")
        cli.run.assert_not_called()


class TestDeleteApiKey(unittest.TestCase):
    def test_command_args(self):
        cli = _mock_cli(None)
        self.assertIsNone(delete_api_key(cli, FAKE_USER, FAKE_FP))
        cli.run.assert_called_once_with(
            ["iam", "user", "api-key", "delete", "--user-id", FAKE_USER,
             "--fingerprint", FAKE_FP, "--force"])

    def test_empty_fingerprint_rejected(self):
        cli = _mock_cli(None)
        with self.assertRaises(ValueError):
            delete_api_key(cli, FAKE_USER, "")
        cli.run.assert_not_called()


class TestGetProfileUserId(unittest.TestCase):
    def _write_config(self, content):
        tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".config", delete=False, encoding="utf-8")
        tmp.write(content)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        return tmp.name

    def test_reads_default_profile(self):
        path = self._write_config(
            "[DEFAULT]\n"
            f"user={FAKE_USER}\n"
            "fingerprint=aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99\n"
            "tenancy=ocid1.tenancy.oc1..xxxxxxxx\n"
            "region=ap-singapore-1\n"
            "key_file=/root/1.pem\n")
        self.assertEqual(get_profile_user_id("DEFAULT", path), FAKE_USER)

    def test_reads_other_profile(self):
        path = self._write_config(
            "[DEFAULT]\n"
            f"user={FAKE_USER}\n"
            "[ACCOUNT2]\n"
            f"user={FAKE_USER2}\n")
        self.assertEqual(get_profile_user_id("ACCOUNT2", path), FAKE_USER2)

    def test_missing_profile_returns_none(self):
        path = self._write_config(f"[DEFAULT]\nuser={FAKE_USER}\n")
        self.assertIsNone(get_profile_user_id("NOSUCH", path))

    def test_missing_user_field_returns_none(self):
        path = self._write_config("[DEFAULT]\nregion=ap-singapore-1\n")
        self.assertIsNone(get_profile_user_id("DEFAULT", path))

    def test_missing_file_returns_none(self):
        self.assertIsNone(get_profile_user_id(
            "DEFAULT", "/tmp/no-such-oci-config-12345"))


class TestCreateUser(unittest.TestCase):
    """create_user：命令构造（写操作，只 mock 检查，不真执行）。"""

    def test_with_email_and_description(self):
        cli = _mock_cli({"data": {"id": FAKE_USER, "name": "newadmin",
                                  "email": "a@example.com"}})
        result = create_user(cli, "ocid1.tenancy.oc1..xxxx", "newadmin",
                             "a@example.com", description="admin user")
        self.assertEqual(result["name"], "newadmin")
        cli.run.assert_called_once_with(
            ["iam", "user", "create",
             "--compartment-id", "ocid1.tenancy.oc1..xxxx",
             "--name", "newadmin", "--email", "a@example.com",
             "--description", "admin user",
             "--wait-for-state", "ACTIVE",
             "--wait-interval-seconds", "2",
             "--max-wait-seconds", "60"],
            timeout=90)

    def test_email_only_no_description(self):
        cli = _mock_cli({"data": {"id": FAKE_USER}})
        create_user(cli, "ocid1.tenancy.oc1..xxxx", "newadmin",
                    "a@example.com")
        # 备注为空时不再传空字符串（服务端拒绝空描述），
        # 改用默认描述 Managed by M_Bot: <用户名>（v2.14.3）。
        cli.run.assert_called_once_with(
            ["iam", "user", "create",
             "--compartment-id", "ocid1.tenancy.oc1..xxxx",
             "--name", "newadmin", "--email", "a@example.com",
             "--description", "Managed by M_Bot: newadmin",
             "--wait-for-state", "ACTIVE",
             "--wait-interval-seconds", "2",
             "--max-wait-seconds", "60"],
            timeout=90)


class TestGroupHelpers(unittest.TestCase):
    """get_group_id_by_name / add_user_to_group。"""

    def test_find_admin_group(self):
        cli = _mock_cli(None)
        cli.sdk_list_groups.return_value = [
            {"id": "ocid1.group.oc1..aaaa", "name": "Group_for_Api_used"},
            {"id": "ocid1.group.oc1..bbbb", "name": "Administrators"},
        ]
        self.assertEqual(
            get_group_id_by_name(cli, "ocid1.tenancy.oc1..xxxx",
                                 "Administrators"),
            "ocid1.group.oc1..bbbb")
        cli.sdk_list_groups.assert_called_once_with(
            "ocid1.tenancy.oc1..xxxx")
        cli.run.assert_not_called()

    def test_group_not_found_returns_none(self):
        cli = _mock_cli(None)
        cli.sdk_list_groups.return_value = [
            {"id": "ocid1.group.oc1..aaaa", "name": "Other"},
        ]
        self.assertIsNone(get_group_id_by_name(
            cli, "ocid1.tenancy.oc1..xxxx", "Administrators"))

    def test_empty_group_list_returns_none(self):
        cli = _mock_cli(None)
        cli.sdk_list_groups.return_value = []
        self.assertIsNone(get_group_id_by_name(
            cli, "ocid1.tenancy.oc1..xxxx", "Administrators"))

    def test_add_user_to_group(self):
        cli = _mock_cli({})
        add_user_to_group(cli, FAKE_USER, "ocid1.group.oc1..bbbb")
        cli.run.assert_called_once_with(
            ["iam", "group", "add-user", "--user-id", FAKE_USER,
             "--group-id", "ocid1.group.oc1..bbbb"])


if __name__ == "__main__":
    unittest.main()


class TestResetConsolePassword(unittest.TestCase):
    def test_uses_official_oci_cli_command(self):
        cli = _mock_cli({"data": {"password": "TempPass123!"}})
        self.assertEqual(create_or_reset_ui_password(cli, FAKE_USER),
                         "TempPass123!")
        cli.run.assert_called_once_with([
            "iam", "user", "ui-password", "create-or-reset",
            "--user-id", FAKE_USER])

    def test_maps_not_authorized_without_leaking_service_json(self):
        cli = _mock_cli(None)
        cli.run.side_effect = RuntimeError(
            'ServiceError: {"code":"NotAuthorizedOrNotFound",'
            '"message":"PATCH request failed", "opc-request-id":"secret-id"}')
        with self.assertRaises(RuntimeError) as caught:
            create_or_reset_ui_password(cli, FAKE_USER)
        message = str(caught.exception)
        self.assertIn("Administrators", message)
        self.assertIn("Identity Domains", message)
        self.assertNotIn("secret-id", message)
        self.assertNotIn("ServiceError", message)

    def test_rejects_empty_user_id_before_call(self):
        cli = _mock_cli(None)
        with self.assertRaises(ValueError):
            create_or_reset_ui_password(cli, "")
        cli.run.assert_not_called()
