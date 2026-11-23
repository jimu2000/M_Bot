#!/usr/bin/env python3
"""objectstorage.py 的单元测试（mock cli.run，不碰真实 API）。

运行：python3 -m unittest tests.test_objectstorage -v

注意：create / delete / upload 是写操作，测试只 mock 命令构造检查，
绝不对真实 API 执行。
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oci.objectstorage import (
    create_bucket,
    delete_bucket,
    delete_object,
    get_namespace,
    list_buckets,
    list_objects,
    upload_object,
)

# 占位符（测试里不出现真实 OCID / 命名空间）
FAKE_COMPARTMENT = "ocid1.tenancy.oc1..xxxxxxxx"
FAKE_NAMESPACE = "fake-namespace"
FAKE_BUCKET = "test-bucket"


def _mock_cli(return_value):
    """造一个 cli.run 返回固定值的 mock OCIClient。"""
    cli = MagicMock()
    cli.run.return_value = return_value
    return cli


class TestGetNamespace(unittest.TestCase):
    def test_returns_namespace_string(self):
        cli = _mock_cli({"data": FAKE_NAMESPACE})
        self.assertEqual(get_namespace(cli), FAKE_NAMESPACE)
        cli.run.assert_called_once_with(["os", "ns", "get"])

    def test_bad_response_raises(self):
        for bad in ({"data": []}, {"data": ""}, {"data": None}, {}):
            cli = _mock_cli(bad)
            with self.assertRaises(ValueError, msg=bad):
                get_namespace(cli)


class TestListBuckets(unittest.TestCase):
    def test_returns_simplified_list(self):
        cli = _mock_cli({"data": [{
            "name": FAKE_BUCKET,
            "time-created": "2026-11-20T00:00:00Z",
            "public-access-type": "NoPublicAccess",
            "storage-tier": "Standard",
            "compartment-id": FAKE_COMPARTMENT,
        }]})
        result = list_buckets(cli, FAKE_COMPARTMENT, namespace=FAKE_NAMESPACE)
        self.assertEqual(result, [{
            "name": FAKE_BUCKET,
            "time-created": "2026-11-20T00:00:00Z",
            "public-access-type": "NoPublicAccess",
            "storage-tier": "Standard",
        }])
        cli.run.assert_called_once_with(
            ["os", "bucket", "list",
             "--compartment-id", FAKE_COMPARTMENT,
             "--namespace-name", FAKE_NAMESPACE])

    def test_namespace_auto_resolved(self):
        """不传 namespace 时先查一次 oci os ns get。"""
        cli = MagicMock()
        cli.run.side_effect = [
            {"data": FAKE_NAMESPACE},
            {"data": []},
        ]
        self.assertEqual(list_buckets(cli, FAKE_COMPARTMENT), [])
        self.assertEqual(cli.run.call_count, 2)
        self.assertEqual(cli.run.call_args_list[0][0][0],
                         ["os", "ns", "get"])
        self.assertEqual(cli.run.call_args_list[1][0][0],
                         ["os", "bucket", "list",
                          "--compartment-id", FAKE_COMPARTMENT,
                          "--namespace-name", FAKE_NAMESPACE])

    def test_empty(self):
        cli = _mock_cli({"data": []})
        self.assertEqual(list_buckets(cli, FAKE_COMPARTMENT,
                                      namespace=FAKE_NAMESPACE), [])


class TestCreateBucket(unittest.TestCase):
    def test_command_args(self):
        cli = _mock_cli({"data": {"name": FAKE_BUCKET}})
        result = create_bucket(cli, FAKE_COMPARTMENT, FAKE_BUCKET,
                               namespace=FAKE_NAMESPACE)
        self.assertEqual(result, {"name": FAKE_BUCKET})
        cli.run.assert_called_once_with(
            ["os", "bucket", "create",
             "--compartment-id", FAKE_COMPARTMENT,
             "--name", FAKE_BUCKET,
             "--namespace-name", FAKE_NAMESPACE])

    def test_invalid_name_rejected(self):
        cli = _mock_cli({"data": {}})
        for bad in ("", "UPPER", "has space", "-leading-dash",
                    ".leading-dot", "a" * 64, "under_score"):
            with self.assertRaises(ValueError, msg=bad):
                create_bucket(cli, FAKE_COMPARTMENT, bad,
                              namespace=FAKE_NAMESPACE)
        cli.run.assert_not_called()

    def test_valid_names_accepted(self):
        cli = _mock_cli({"data": {}})
        for good in ("a", "bucket-1", "my.bucket.name", "b" * 63):
            create_bucket(cli, FAKE_COMPARTMENT, good,
                          namespace=FAKE_NAMESPACE)
        self.assertEqual(cli.run.call_count, 4)


class TestDeleteBucket(unittest.TestCase):
    def test_command_args_with_force(self):
        cli = _mock_cli(None)
        self.assertIsNone(delete_bucket(cli, FAKE_BUCKET,
                                        namespace=FAKE_NAMESPACE))
        cli.run.assert_called_once_with(
            ["os", "bucket", "delete",
             "--bucket-name", FAKE_BUCKET,
             "--namespace-name", FAKE_NAMESPACE,
             "--force"])

    def test_empty_flag(self):
        """empty=True 时追加 --empty（连对象一起删）。"""
        cli = _mock_cli(None)
        delete_bucket(cli, FAKE_BUCKET, namespace=FAKE_NAMESPACE, empty=True)
        cli.run.assert_called_once_with(
            ["os", "bucket", "delete",
             "--bucket-name", FAKE_BUCKET,
             "--namespace-name", FAKE_NAMESPACE,
             "--force", "--empty"])

    def test_invalid_name_rejected(self):
        cli = _mock_cli(None)
        with self.assertRaises(ValueError):
            delete_bucket(cli, "BAD NAME", namespace=FAKE_NAMESPACE)
        cli.run.assert_not_called()


class TestListObjects(unittest.TestCase):
    def test_returns_simplified_list(self):
        cli = _mock_cli({"data": [{
            "name": "file.txt",
            "size": 1024,
            "time-created": "2026-11-20T00:00:00Z",
            "md5": "abc123",
        }]})
        result = list_objects(cli, FAKE_BUCKET, namespace=FAKE_NAMESPACE)
        self.assertEqual(result, [{
            "name": "file.txt",
            "size": 1024,
            "time-created": "2026-11-20T00:00:00Z",
        }])
        cli.run.assert_called_once_with(
            ["os", "object", "list",
             "--bucket-name", FAKE_BUCKET,
             "--namespace-name", FAKE_NAMESPACE])

    def test_empty(self):
        cli = _mock_cli({"data": []})
        self.assertEqual(list_objects(cli, FAKE_BUCKET,
                                      namespace=FAKE_NAMESPACE), [])


class TestUploadObject(unittest.TestCase):
    def test_command_args_with_force(self):
        """put 带 --force（同名对象已存在时覆盖）。"""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"hello")
            path = f.name
        try:
            cli = _mock_cli({"data": {"name": "file.txt"}})
            result = upload_object(cli, FAKE_BUCKET, "file.txt", path,
                                   namespace=FAKE_NAMESPACE)
            self.assertEqual(result, {"name": "file.txt"})
            cli.run.assert_called_once_with(
                ["os", "object", "put",
                 "--bucket-name", FAKE_BUCKET,
                 "--name", "file.txt",
                 "--file", path,
                 "--namespace-name", FAKE_NAMESPACE,
                 "--force"])
        finally:
            os.unlink(path)

    def test_content_type_passed(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"x")
            path = f.name
        try:
            cli = _mock_cli({"data": {}})
            upload_object(cli, FAKE_BUCKET, "a.json", path,
                          namespace=FAKE_NAMESPACE,
                          content_type="application/json")
            args = cli.run.call_args[0][0]
            self.assertIn("--content-type", args)
            self.assertIn("application/json", args)
        finally:
            os.unlink(path)

    def test_missing_file_rejected(self):
        cli = _mock_cli({"data": {}})
        with self.assertRaises(ValueError):
            upload_object(cli, FAKE_BUCKET, "file.txt",
                          "/tmp/does-not-exist-api-user.txt",
                          namespace=FAKE_NAMESPACE)
        cli.run.assert_not_called()

    def test_empty_object_name_rejected(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"x")
            path = f.name
        try:
            cli = _mock_cli({"data": {}})
            with self.assertRaises(ValueError):
                upload_object(cli, FAKE_BUCKET, "", path,
                              namespace=FAKE_NAMESPACE)
            cli.run.assert_not_called()
        finally:
            os.unlink(path)


class TestDeleteObject(unittest.TestCase):
    def test_command_args_with_force(self):
        cli = _mock_cli(None)
        self.assertIsNone(delete_object(cli, FAKE_BUCKET, "file.txt",
                                        namespace=FAKE_NAMESPACE))
        cli.run.assert_called_once_with(
            ["os", "object", "delete",
             "--bucket-name", FAKE_BUCKET,
             "--name", "file.txt",
             "--namespace-name", FAKE_NAMESPACE,
             "--force"])

    def test_empty_object_name_rejected(self):
        cli = _mock_cli(None)
        with self.assertRaises(ValueError):
            delete_object(cli, FAKE_BUCKET, "", namespace=FAKE_NAMESPACE)
        cli.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
