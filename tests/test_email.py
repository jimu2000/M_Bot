#!/usr/bin/env python3
"""email.py 的单元测试（mock cli.run，不碰真实 API）。

运行：python3 -m unittest tests.test_email -v

注意：
  - create_sender / create_smtp_credential 是写操作，测试只 mock
    命令构造检查，绝不对真实 API 执行。
  - SMTP 凭据密码只在创建时返回一次：测试里用的密码是假占位符，
    且绝不写进日志或快照。
"""

import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oci.email import (
    create_sender,
    create_smtp_credential,
    delete_sender,
    is_valid_email,
    list_iam_users,
    list_senders,
)

# Placeholders (no real OCID / password in tests). 占位符（测试里不出现真实 OCID / 密码）。
FAKE_COMPARTMENT = "ocid1.tenancy.oc1..xxxxxxxx"
FAKE_USER = "ocid1.user.oc1..xxxxxxxx"
FAKE_SENDER = "ocid1.emailsender.oc1..xxxxxxxx"
FAKE_PASSWORD = "fake-smtp-password-placeholder"


def _mock_cli(return_value):
    """造一个 cli.run 返回固定值的 mock OCIClient。"""
    cli = MagicMock()
    cli.run.return_value = return_value
    return cli


class TestIsValidEmail(unittest.TestCase):
    def test_valid(self):
        for good in ("noreply@example.com", "a@b.co",
                     "user.name+tag@sub.example.org"):
            self.assertTrue(is_valid_email(good), good)

    def test_invalid(self):
        for bad in ("", "no-at-sign", "@example.com", "a@b",
                    "a b@c.com", "a@.com", None, 123):
            self.assertFalse(is_valid_email(bad), bad)

    def test_strips_whitespace(self):
        self.assertTrue(is_valid_email("  noreply@example.com  "))


class TestListSenders(unittest.TestCase):
    def test_returns_simplified_list(self):
        cli = _mock_cli(None)
        cli.sdk_list_senders.return_value = [{
            "id": FAKE_SENDER,
            "email-address": "noreply@example.com",
            "lifecycle-state": "ACTIVE",
            "time-created": "2026-11-20T00:00:00Z",
            "compartment-id": FAKE_COMPARTMENT,
        }]
        result = list_senders(cli, FAKE_COMPARTMENT)
        self.assertEqual(result, [{
            "id": FAKE_SENDER,
            "email-address": "noreply@example.com",
            "lifecycle-state": "ACTIVE",
            "time-created": "2026-11-20T00:00:00Z",
        }])
        cli.sdk_list_senders.assert_called_once_with(FAKE_COMPARTMENT)
        cli.run.assert_not_called()

    def test_empty(self):
        cli = _mock_cli(None)
        cli.sdk_list_senders.return_value = []
        self.assertEqual(list_senders(cli, FAKE_COMPARTMENT), [])


class TestCreateSender(unittest.TestCase):
    def test_command_args(self):
        cli = _mock_cli({"data": {"id": FAKE_SENDER,
                                  "email-address": "noreply@example.com",
                                  "lifecycle-state": "CREATING"}})
        result = create_sender(cli, FAKE_COMPARTMENT,
                               "noreply@example.com")
        self.assertEqual(result["id"], FAKE_SENDER)
        cli.run.assert_called_once_with(
            ["email", "sender", "create",
             "--compartment-id", FAKE_COMPARTMENT,
             "--email-address", "noreply@example.com"])

    def test_strips_whitespace(self):
        cli = _mock_cli({"data": {}})
        create_sender(cli, FAKE_COMPARTMENT, "  noreply@example.com ")
        args = cli.run.call_args[0][0]
        self.assertEqual(args[-1], "noreply@example.com")

    def test_invalid_email_rejected(self):
        cli = _mock_cli({"data": {}})
        for bad in ("", "not-an-email", "a@b", "a b@c.com"):
            with self.assertRaises(ValueError, msg=bad):
                create_sender(cli, FAKE_COMPARTMENT, bad)
        cli.run.assert_not_called()


class TestDeleteSender(unittest.TestCase):
    def test_command_args(self):
        cli = _mock_cli({"data": {}})
        delete_sender(cli, FAKE_SENDER)
        cli.run.assert_called_once_with(
            ["email", "sender", "delete",
             "--sender-id", FAKE_SENDER, "--force"])


class TestListIamUsers(unittest.TestCase):
    def test_returns_simplified_list(self):
        cli = _mock_cli(None)
        cli.sdk_list_users.return_value = [{
            "id": FAKE_USER,
            "name": "admin-user",
            "email": "admin@example.com",
            "lifecycle-state": "ACTIVE",
            "description": "x",
        }]
        result = list_iam_users(cli, FAKE_COMPARTMENT)
        self.assertEqual(result, [{
            "id": FAKE_USER,
            "name": "admin-user",
            "email": "admin@example.com",
            "lifecycle-state": "ACTIVE",
        }])
        cli.sdk_list_users.assert_called_once_with(FAKE_COMPARTMENT)
        cli.run.assert_not_called()

    def test_empty(self):
        cli = _mock_cli(None)
        cli.sdk_list_users.return_value = []
        self.assertEqual(list_iam_users(cli, FAKE_COMPARTMENT), [])


class TestCreateSmtpCredential(unittest.TestCase):
    def test_command_args(self):
        """SMTP 凭据走 IAM 组（oci email 下没有 smtp-credential 子命令，
        OCI CLI 3.96.3 --help 实测核对），--description 必填。"""
        cli = _mock_cli({"data": {
            "id": "ocid1.smtpcredential.oc1..xxxxxxxx",
            "username": "ocid1.user.oc1..xxxxxxxx",
            "password": FAKE_PASSWORD,
            "description": "mbot",
        }})
        result = create_smtp_credential(cli, FAKE_USER)
        self.assertEqual(result["password"], FAKE_PASSWORD)
        cli.run.assert_called_once_with(
            ["iam", "smtp-credential", "create",
             "--user-id", FAKE_USER,
             "--description", "mbot"])

    def test_custom_description(self):
        cli = _mock_cli({"data": {}})
        create_smtp_credential(cli, FAKE_USER, description="my-mailer")
        args = cli.run.call_args[0][0]
        self.assertIn("my-mailer", args)

    def test_empty_user_id_rejected(self):
        cli = _mock_cli({"data": {}})
        for bad in ("", None):
            with self.assertRaises(ValueError, msg=bad):
                create_smtp_credential(cli, bad)
        cli.run.assert_not_called()

    def test_empty_description_rejected(self):
        cli = _mock_cli({"data": {}})
        with self.assertRaises(ValueError):
            create_smtp_credential(cli, FAKE_USER, description="")
        cli.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
