#!/usr/bin/env python3
"""SDK 直调热路径测试：读接口不起子进程 + 模型转换 + 错误映射。

运行：python3 -m unittest tests.test_sdk_reads -v

覆盖：
  - 热路径读接口（实例/配额/启动盘/对象存储/备份）只调 SDK 方法，
    不起 OCI CLI 子进程（cli.run 未被调用，且 subprocess.run 被拦住）。
  - SDK 模型 -> CLI 兼容 dict 的转换（snake_case -> kebab-case，
    shape-config 嵌套字段、时间转 ISO 字符串）。
  - ServiceError 按状态/错误码映射回现有异常类（429 限流、404 未找到、
    容量不足、鉴权失败）。

全部 mock，不碰真实 API。
"""

import datetime
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oci import client as client_mod
from oci.boot_backup import (
    check_backup_quota,
    get_boot_volume_backup,
    list_boot_volume_backups,
)
from oci.client import (
    OCIClient,
    AuthError,
    NotFoundError,
    OCIError,
    OutOfCapacityError,
    RateLimitError,
    _load_sdk,
    _model_to_dict,
    _snake_to_kebab,
)
from oci.console import list_console_connections
from oci.email import list_iam_users, list_senders
from oci.grabber import Grabber
from oci.iam import get_group_id_by_name, list_api_keys
from oci.instance import get_image_arch, get_instance, list_instances
from oci.network import (get_public_ip, get_vnic, list_ipv6_for_vnic,
                         list_private_ips, list_public_ips, list_vnics,
                         repair_instance_ip)
from oci.objectstorage import get_namespace, list_buckets, list_objects
from oci.quota import (
    a1_health_check,
    can_launch,
    get_a1_usage,
    get_boot_volume_usage,
    get_e2_usage,
    get_free_tier_summary,
)
from oci.volume import (
    get_boot_volume,
    list_boot_volume_attachments,
    list_boot_volumes,
    list_orphan_boot_volumes,
    list_volumes,
    total_boot_volume_gb,
)

# Placeholders (no real OCID in tests). 占位符（测试里不出现真实 OCID）。
FAKE_COMPARTMENT = "ocid1.tenancy.oc1..xxxxxxxx"
FAKE_INSTANCE = "ocid1.instance.oc1..dddd"
FAKE_BV = "ocid1.bootvolume.oc1..aaaa"
FAKE_BV_ORPHAN = "ocid1.bootvolume.oc1..orph"
FAKE_BACKUP = "ocid1.bootvolumebackup.oc1..cccc"
FAKE_AD = "FAKE:AP-SINGAPORE-1-AD-1"
FAKE_BUCKET = "fake-bucket"
FAKE_NAMESPACE = "fake-namespace"
FAKE_VNIC = "ocid1.vnic.oc1..dddd"
FAKE_PRIVATE_IP = "ocid1.privateip.oc1..dddd"
FAKE_PUBLIC_IP = "ocid1.publicip.oc1..dddd"
FAKE_IP = "203.0.113.10"  # TEST-NET-3，文档保留地址，非真实 IP。
FAKE_GROUP = "ocid1.group.oc1..dddd"
FAKE_USER = "ocid1.user.oc1..dddd"
FAKE_CONNECTION = "ocid1.instanceconsoleconnection.oc1..dddd"


def _instance(state="RUNNING", shape="VM.Standard.A1.Flex",
              ocpus=1, memory_gb=6):
    return {
        "id": FAKE_INSTANCE,
        "display-name": "test-instance",
        "lifecycle-state": state,
        "availability-domain": FAKE_AD,
        "shape": shape,
        "shape-config": {"ocpus": ocpus, "memory-in-gbs": memory_gb},
        "time-created": "2026-01-01T10:00:00.000Z",
    }


def _volume(volume_id=FAKE_BV, size=47, state="AVAILABLE"):
    return {
        "id": volume_id,
        "display-name": "test-instance (boot volume)",
        "size-in-gbs": size,
        "lifecycle-state": state,
        "availability-domain": FAKE_AD,
        "time-created": "2026-01-01T10:00:00.000Z",
    }


def _backup(backup_id=FAKE_BACKUP, state="AVAILABLE"):
    return {
        "id": backup_id,
        "display-name": "test-backup",
        "boot-volume-id": FAKE_BV,
        "size-in-gbs": 47,
        "lifecycle-state": state,
        "source-type": "MANUAL",
        "type": "FULL",
        "time-created": "2026-01-01T10:00:00.000Z",
    }


def _attachment(volume_id=FAKE_BV, state="ATTACHED"):
    return {
        "id": "ocid1.bootvolumeattachment.oc1..eeee",
        "boot-volume-id": volume_id,
        "instance-id": FAKE_INSTANCE,
        "lifecycle-state": state,
    }


def _vnic(vnic_id=FAKE_VNIC, public_ip=FAKE_IP, is_primary=True):
    return {
        "id": vnic_id,
        "display-name": "vnic-1",
        "public-ip": public_ip,
        "private-ip": "10.0.0.5",
        "is-primary": is_primary,
        "lifecycle-state": "AVAILABLE",
    }


def _mock_cli():
    """SDK 读方法返回假数据的 MagicMock cli；run 一旦被调就报错。"""
    cli = MagicMock()
    cli.run.side_effect = AssertionError("热路径读接口不应调 cli.run（起子进程）")
    cli.sdk_list_instances.return_value = [_instance()]
    cli.sdk_get_instance.return_value = _instance()
    cli.sdk_get_image.return_value = {
        "id": "ocid1.image.oc1..xxxx",
        "display-name": "Canonical-Ubuntu-24.04-aarch64-2024.10.31-0",
    }
    cli.sdk_list_boot_volumes.return_value = [_volume()]
    cli.sdk_get_boot_volume.return_value = _volume()
    cli.sdk_list_volumes.return_value = []
    cli.sdk_list_boot_volume_attachments.return_value = [_attachment()]
    cli.sdk_get_namespace.return_value = FAKE_NAMESPACE
    cli.sdk_list_buckets.return_value = [{
        "name": FAKE_BUCKET, "namespace": FAKE_NAMESPACE,
        "time-created": "2026-01-01T10:00:00.000Z",
    }]
    cli.sdk_list_objects.return_value = [{
        "name": "backup-1.tar.gz", "size": 1234,
        "time-created": "2026-01-01T10:00:00.000Z",
    }]
    cli.sdk_list_boot_volume_backups.return_value = [_backup()]
    cli.sdk_get_boot_volume_backup.return_value = _backup()
    # 第 3 轮新增的 SDK 读方法。
    cli.sdk_list_vnic_attachments.return_value = [{
        "id": "ocid1.vnicattachment.oc1..eeee",
        "vnic-id": FAKE_VNIC,
        "instance-id": FAKE_INSTANCE,
        "lifecycle-state": "ATTACHED",
    }]
    cli.sdk_get_vnic.return_value = _vnic()
    cli.sdk_list_vnics.return_value = [_vnic()]
    cli.sdk_list_public_ips.return_value = [{
        "id": FAKE_PUBLIC_IP, "ip-address": FAKE_IP,
        "lifetime": "RESERVED", "lifecycle-state": "ASSIGNED",
        "assigned-entity-id": FAKE_PRIVATE_IP,
    }]
    cli.sdk_list_ipv6s.return_value = [{
        "id": "ocid1.ipv6.oc1..eeee", "ip-address": "2001:db8::1",
        "lifecycle-state": "ASSIGNED",
    }]
    cli.sdk_list_private_ips.return_value = [{
        "id": FAKE_PRIVATE_IP, "ip-address": "10.0.0.5",
        "is-primary": True, "display-name": "primary",
    }]
    cli.sdk_list_instance_console_connections.return_value = [{
        "id": FAKE_CONNECTION, "instance-id": FAKE_INSTANCE,
        "connection-string": "ssh ...",
        "lifecycle-state": "ACTIVE",
        "time-created": "2026-01-01T10:00:00.000Z",
    }]
    cli.sdk_get_instance_console_connection.return_value = {
        "id": FAKE_CONNECTION, "instance-id": FAKE_INSTANCE,
        "connection-string": "ssh ...",
        "lifecycle-state": "ACTIVE",
    }
    cli.sdk_list_groups.return_value = [{
        "id": FAKE_GROUP, "name": "Admins",
        "lifecycle-state": "ACTIVE",
    }]
    cli.sdk_list_api_keys.return_value = [{
        "fingerprint": "aa:bb:cc",
        "lifecycle-state": "ACTIVE",
        "time-created": "2026-01-01T10:00:00.000Z",
    }]
    cli.sdk_list_users.return_value = [{
        "id": FAKE_USER, "name": "api_user",
        "email": "api@example.com", "lifecycle-state": "ACTIVE",
    }]
    cli.sdk_list_senders.return_value = [{
        "id": "ocid1.emailsender.oc1..eeee",
        "email-address": "noreply@example.com",
        "lifecycle-state": "ACTIVE",
        "time-created": "2026-01-01T10:00:00.000Z",
    }]
    return cli


class _NoSubprocessMixin:
    """断言子进程没被起：subprocess.run 一旦被调就报错。"""

    def _guard_subprocess(self):
        return patch.object(
            client_mod.subprocess, "run",
            side_effect=AssertionError("热路径读接口不应起子进程"))


class TestHotPathReadsNoSubprocess(_NoSubprocessMixin, unittest.TestCase):
    """热路径读接口：只走 SDK，不起 OCI CLI 子进程。"""

    def test_instance_reads(self):
        cli = _mock_cli()
        with self._guard_subprocess():
            result = list_instances(cli, FAKE_COMPARTMENT,
                                    include_shape_config=True)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["display-name"], "test-instance")
            self.assertEqual(result[0]["shape-config"]["ocpus"], 1)
            detail = get_instance(cli, FAKE_INSTANCE)
            self.assertEqual(detail["lifecycle-state"], "RUNNING")
            self.assertEqual(get_image_arch(cli, "ocid1.image.oc1..xxxx"),
                             "aarch64")
        cli.run.assert_not_called()

    def test_quota_reads(self):
        cli = _mock_cli()
        with self._guard_subprocess():
            a1 = get_a1_usage(cli, FAKE_COMPARTMENT)
            self.assertEqual(a1["ocpu_used"], 1)
            self.assertEqual(a1["memory_used_gb"], 6)
            e2 = get_e2_usage(cli, FAKE_COMPARTMENT)
            self.assertEqual(e2["count"], 0)
            bv = get_boot_volume_usage(cli, FAKE_COMPARTMENT)
            self.assertEqual(bv["used_gb"], 47)
            summary = get_free_tier_summary(cli, FAKE_COMPARTMENT)
            self.assertIn("a1", summary)
            health = a1_health_check(cli, FAKE_COMPARTMENT)
            self.assertTrue(health["overall_ok"])
            self.assertTrue(can_launch(cli, FAKE_COMPARTMENT,
                                       "VM.Standard.E2.1.Micro"))
        cli.run.assert_not_called()

    def test_volume_reads(self):
        cli = _mock_cli()
        with self._guard_subprocess():
            volumes = list_boot_volumes(cli, FAKE_COMPARTMENT)
            self.assertEqual(len(volumes), 1)
            self.assertEqual(volumes[0]["size-in-gbs"], 47)
            detail = get_boot_volume(cli, FAKE_BV)
            self.assertEqual(detail["id"], FAKE_BV)
            self.assertEqual(list_volumes(cli, FAKE_COMPARTMENT), [])
            atts = list_boot_volume_attachments(cli, FAKE_COMPARTMENT,
                                                FAKE_AD)
            self.assertEqual(atts[0]["boot-volume-id"], FAKE_BV)
            self.assertEqual(total_boot_volume_gb(cli, FAKE_COMPARTMENT),
                             47)
            # 挂着的盘不是孤儿盘。
            self.assertEqual(
                list_orphan_boot_volumes(cli, FAKE_COMPARTMENT), [])
        cli.run.assert_not_called()

    def test_orphan_volume_found_without_attachment(self):
        """无挂载关系的盘是孤儿盘（仍走 SDK，不起子进程）。"""
        cli = _mock_cli()
        cli.sdk_list_boot_volumes.return_value = [
            _volume(), _volume(FAKE_BV_ORPHAN)]
        cli.sdk_list_boot_volume_attachments.return_value = []
        with self._guard_subprocess():
            orphans = list_orphan_boot_volumes(cli, FAKE_COMPARTMENT)
        # 挂载关系为空时两块盘都算孤儿盘。
        self.assertEqual([o["id"] for o in orphans],
                         [FAKE_BV, FAKE_BV_ORPHAN])
        cli.run.assert_not_called()

    def test_objectstorage_reads(self):
        cli = _mock_cli()
        with self._guard_subprocess():
            self.assertEqual(get_namespace(cli), FAKE_NAMESPACE)
            buckets = list_buckets(cli, FAKE_COMPARTMENT)
            self.assertEqual(buckets[0]["name"], FAKE_BUCKET)
            objects = list_objects(cli, FAKE_BUCKET)
            self.assertEqual(objects[0]["name"], "backup-1.tar.gz")
        cli.run.assert_not_called()

    def test_backup_reads(self):
        cli = _mock_cli()
        with self._guard_subprocess():
            backups = list_boot_volume_backups(cli, FAKE_COMPARTMENT)
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0]["id"], FAKE_BACKUP)
            detail = get_boot_volume_backup(cli, FAKE_BACKUP)
            self.assertEqual(detail["lifecycle-state"], "AVAILABLE")
            count, limit, full, earliest = check_backup_quota(
                cli, FAKE_COMPARTMENT)
            self.assertEqual(count, 1)
            self.assertEqual(limit, 5)
            self.assertFalse(full)
            self.assertEqual(earliest["id"], FAKE_BACKUP)
        cli.run.assert_not_called()


class TestModelToDict(unittest.TestCase):
    """SDK 模型 -> CLI 兼容 dict：snake_case 转 kebab-case。"""

    def test_instance_model_converts(self):
        sdk = _load_sdk()
        model = sdk.core.models.Instance(
            id=FAKE_INSTANCE,
            display_name="test-instance",
            lifecycle_state="RUNNING",
            shape="VM.Standard.A1.Flex",
            shape_config=sdk.core.models.InstanceShapeConfig(
                ocpus=1, memory_in_gbs=6),
            time_created=datetime.datetime(
                2026, 11, 24, 10, 0, 0,
                tzinfo=datetime.timezone.utc),
        )
        d = _model_to_dict(model)
        self.assertEqual(d["display-name"], "test-instance")
        self.assertEqual(d["lifecycle-state"], "RUNNING")
        self.assertEqual(d["shape-config"]["memory-in-gbs"], 6)
        self.assertEqual(d["shape-config"]["ocpus"], 1)
        # 时间已转成 ISO 字符串。
        self.assertIsInstance(d["time-created"], str)
        self.assertTrue(d["time-created"].startswith("2026-01-01"))

    def test_boot_volume_model_keeps_vpus(self):
        sdk = _load_sdk()
        model = sdk.core.models.BootVolume(
            id=FAKE_BV,
            display_name="test-instance (boot volume)",
            size_in_gbs=47,
            lifecycle_state="AVAILABLE",
            vpus_per_gb=10,
        )
        d = _model_to_dict(model)
        self.assertEqual(d["size-in-gbs"], 47)
        self.assertEqual(d["vpus-per-gb"], 10)


class TestSnakeToKebab(unittest.TestCase):
    """第 7 轮：_snake_to_kebab 减分配——输出与重建版完全一致，
    只是不含下划线的子树直接复用，不重建。"""

    def test_converts_snake_to_kebab(self):
        d = {"display_name": "x",
             "shape_config": {"ocpus": 1, "memory_in_gbs": 6},
             "freeform_tags": {"role": "web"}}
        self.assertEqual(_snake_to_kebab(d), {
            "display-name": "x",
            "shape-config": {"ocpus": 1, "memory-in-gbs": 6},
            "freeform-tags": {"role": "web"}})

    def test_already_kebab_dict_returned_as_is(self):
        d = {"display-name": "x", "freeform-tags": {"role": "web"}}
        self.assertIs(_snake_to_kebab(d), d)

    def test_unchanged_subtree_shared(self):
        inner = {"role": "web"}
        d = {"freeform_tags": inner}
        out = _snake_to_kebab(d)
        self.assertEqual(out, {"freeform-tags": {"role": "web"}})
        # 内层没有下划线，直接复用，不重建。
        self.assertIs(out["freeform-tags"], inner)

    def test_tuple_still_becomes_list(self):
        self.assertEqual(_snake_to_kebab(({"a_b": 1},)), [{"a-b": 1}])
        self.assertIsInstance(_snake_to_kebab((1, 2)), list)

    def test_scalars_pass_through(self):
        self.assertEqual(_snake_to_kebab("a_b"), "a_b")
        self.assertEqual(_snake_to_kebab(42), 42)
        self.assertIsNone(_snake_to_kebab(None))


class TestSdkErrorMapping(unittest.TestCase):
    """ServiceError 按状态/错误码映射回现有异常类。"""

    def _wrap(self, status, code, message):
        sdk = _load_sdk()
        err = sdk.exceptions.ServiceError(
            status=status, code=code, message=message, headers={})
        cli = OCIClient(profile="DEFAULT", region="ap-singapore-1")
        return cli, err

    def test_429_too_many_requests(self):
        cli, err = self._wrap(429, "TooManyRequests", "Too many requests")
        with self.assertRaises(RateLimitError):
            cli._sdk_wrap("test op",
                          lambda: (_ for _ in ()).throw(err))

    def test_404_not_found(self):
        cli, err = self._wrap(404, "NotAuthorizedOrNotFound",
                              "Not authorized")
        with self.assertRaises(NotFoundError):
            cli._sdk_wrap("test op",
                          lambda: (_ for _ in ()).throw(err))

    def test_401_auth(self):
        cli, err = self._wrap(401, "NotAuthenticated",
                              "The required information to complete "
                              "authentication was not provided")
        with self.assertRaises(AuthError):
            cli._sdk_wrap("test op",
                          lambda: (_ for _ in ()).throw(err))

    def test_400_out_of_capacity(self):
        cli, err = self._wrap(400, "OutOfCapacity",
                              "Out of capacity for shape "
                              "VM.Standard.A1.Flex")
        with self.assertRaises(OutOfCapacityError):
            cli._sdk_wrap("test op",
                          lambda: (_ for _ in ()).throw(err))

    def test_unknown_code_falls_back_to_oci_error(self):
        cli, err = self._wrap(500, "InternalError",
                              "Something went wrong")
        with self.assertRaises(OCIError):
            cli._sdk_wrap("test op",
                          lambda: (_ for _ in ()).throw(err))

    def test_config_error_becomes_auth_error(self):
        sdk = _load_sdk()
        err = sdk.exceptions.InvalidConfig(
            "config file is missing required keys")
        cli = OCIClient(profile="DEFAULT", region="ap-singapore-1")
        with self.assertRaises(AuthError):
            cli._sdk_wrap("test op",
                          lambda: (_ for _ in ()).throw(err))


class TestSdkCallSignatures(unittest.TestCase):
    """回归：SDK 方法的真实签名必须接受我们传参的方式。

    教训（2026-01-02）：BlockstorageClient.list_boot_volumes /
    list_volumes 的真实签名是 (self, **kwargs)，compartment_id 只能
    关键字传；mock 测试拦不住这种签名不匹配，.160 上线后报 500 才发现。
    这里直接对真实 SDK 的方法做 inspect.signature 断言。
    """

    def _accepts(self, method, name, positional=False):
        import inspect
        sig = inspect.signature(method)
        params = sig.parameters
        if positional:
            # 位置传参：必须有同名的位置参数，**kwargs 不算
            if name not in params:
                return False
            kind = params[name].kind
            return kind in (inspect.Parameter.POSITIONAL_ONLY,
                            inspect.Parameter.POSITIONAL_OR_KEYWORD)
        # 关键字传参：有 **kwargs 就接受任何关键字参数
        if any(p.kind == inspect.Parameter.VAR_KEYWORD
               for p in params.values()):
            return True
        if name not in params:
            return False
        kind = params[name].kind
        return kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        inspect.Parameter.KEYWORD_ONLY)

    def test_blockstorage_list_methods_accept_compartment_as_kwarg(self):
        sdk = _load_sdk()
        for method_name in ("list_boot_volumes", "list_volumes"):
            method = getattr(sdk.core.BlockstorageClient, method_name)
            self.assertTrue(
                self._accepts(method, "compartment_id"),
                "%s 不接受 compartment_id 关键字参数" % method_name)

    def test_other_list_methods_accept_positional_args(self):
        sdk = _load_sdk()
        self.assertTrue(self._accepts(
            sdk.core.ComputeClient.list_instances,
            "compartment_id", positional=True))
        self.assertTrue(self._accepts(
            sdk.core.BlockstorageClient.list_boot_volume_backups,
            "compartment_id", positional=True))
        self.assertTrue(self._accepts(
            sdk.object_storage.ObjectStorageClient.list_buckets,
            "compartment_id", positional=True))

    def _param_names(self, method):
        """返回 SDK 方法的参数名列表（按声明顺序，不含 self）。"""
        import inspect
        return [name for name in inspect.signature(method).parameters
                if name != "self"]

    def test_boot_volume_attachments_arg_order(self):
        """回归：第 2 轮曾把参数顺序传反的真 bug。

        list_boot_volume_attachments 的真实签名是
        (availability_domain, compartment_id)，可用域在前；
        旧代码按 (compartment_id, availability_domain) 位置传，
        线上孤儿盘查询报 400，mock 测试拦不住。这里断言参数顺序。
        """
        sdk = _load_sdk()
        names = self._param_names(
            sdk.core.ComputeClient.list_boot_volume_attachments)
        self.assertEqual(names[:2],
                         ["availability_domain", "compartment_id"])

    def test_public_ips_scope_is_first_positional(self):
        """list_public_ips 的 scope 是第一个必填位置参数。"""
        sdk = _load_sdk()
        names = self._param_names(
            sdk.core.VirtualNetworkClient.list_public_ips)
        self.assertEqual(names[:2], ["scope", "compartment_id"])
        self.assertTrue(self._accepts(
            sdk.core.VirtualNetworkClient.list_public_ips,
            "availability_domain"))

    def test_other_arg_orders(self):
        """其余新方法的参数顺序与传参方式（位置/关键字）必须与真实签名一致。"""
        sdk = _load_sdk()
        self.assertEqual(
            self._param_names(
                sdk.core.ComputeClient.list_vnic_attachments)[:1],
            ["compartment_id"])
        self.assertTrue(self._accepts(
            sdk.core.ComputeClient.list_vnic_attachments,
            "instance_id"))
        self.assertEqual(
            self._param_names(
                sdk.object_storage.ObjectStorageClient.list_buckets)[:2],
            ["namespace_name", "compartment_id"])
        self.assertEqual(
            self._param_names(
                sdk.object_storage.ObjectStorageClient.list_objects)[:2],
            ["namespace_name", "bucket_name"])
        self.assertEqual(
            self._param_names(
                sdk.core.ComputeClient.list_instance_console_connections)[:1],
            ["compartment_id"])
        self.assertTrue(self._accepts(
            sdk.core.ComputeClient.list_instance_console_connections,
            "instance_id"))
        self.assertTrue(self._accepts(
            sdk.core.ComputeClient.get_instance_console_connection,
            "instance_console_connection_id", positional=True))
        self.assertTrue(self._accepts(
            sdk.identity.IdentityClient.list_groups,
            "compartment_id", positional=True))
        self.assertTrue(self._accepts(
            sdk.identity.IdentityClient.list_api_keys,
            "user_id", positional=True))
        self.assertTrue(self._accepts(
            sdk.identity.IdentityClient.list_users,
            "compartment_id", positional=True))
        self.assertTrue(self._accepts(
            sdk.email.EmailClient.list_senders,
            "compartment_id", positional=True))
        self.assertTrue(self._accepts(
            sdk.core.VirtualNetworkClient.get_vnic,
            "vnic_id", positional=True))
        self.assertTrue(self._accepts(
            sdk.core.ComputeClient.list_instances, "display_name"))

    def test_ipv6_and_private_ip_methods_are_kwargs_only(self):
        """list_ipv6s / list_private_ips 只有 **kwargs 签名，
        vnic_id 必须用关键字传，位置传会报 TypeError。"""
        sdk = _load_sdk()
        for method_name in ("list_ipv6s", "list_private_ips"):
            method = getattr(sdk.core.VirtualNetworkClient, method_name)
            self.assertTrue(self._accepts(method, "vnic_id"),
                            "%s 不接受 vnic_id 关键字参数" % method_name)
            self.assertFalse(self._accepts(method, "vnic_id",
                                           positional=True),
                             "%s 不应接受 vnic_id 位置参数" % method_name)


class TestSdkArgOrderRegression(unittest.TestCase):
    """回归：SDK 直调方法实际传给 SDK 的参数顺序/方式必须与真实签名一致。

    只断言签名的测试拦不住“方法名对了、参数顺序错了”的调用；
    这里直接拦截 _sdk_paginate，断言传给 SDK 方法的参数。
    """

    def test_boot_volume_attachments_passes_ad_first(self):
        cli = OCIClient(profile="DEFAULT", region="ap-singapore-1")
        mock_compute = MagicMock()
        captured = {}

        def fake_paginate(operation, list_fn, *args, **kwargs):
            captured["fn"] = list_fn
            captured["args"] = args
            captured["kwargs"] = kwargs
            return []

        with patch.object(OCIClient, "_sdk_service_client",
                          return_value=mock_compute):
            with patch.object(OCIClient, "_sdk_paginate",
                              side_effect=fake_paginate):
                result = cli.sdk_list_boot_volume_attachments(
                    FAKE_COMPARTMENT, FAKE_AD)
        self.assertEqual(result, [])
        self.assertIs(captured["fn"],
                      mock_compute.list_boot_volume_attachments)
        # 可用域在前、compartment 在后（关键字传参，顺序不再是隐患）。
        self.assertEqual(captured["args"], ())
        self.assertEqual(captured["kwargs"],
                         {"availability_domain": FAKE_AD,
                          "compartment_id": FAKE_COMPARTMENT})

    def test_public_ips_scope_first(self):
        cli = OCIClient(profile="DEFAULT", region="ap-singapore-1")
        mock_vnet = MagicMock()
        captured = {}

        def fake_paginate(operation, list_fn, *args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return []

        with patch.object(OCIClient, "_sdk_service_client",
                          return_value=mock_vnet):
            with patch.object(OCIClient, "_sdk_paginate",
                              side_effect=fake_paginate):
                cli.sdk_list_public_ips(FAKE_COMPARTMENT)
                self.assertEqual(captured["args"],
                                 ("REGION", FAKE_COMPARTMENT))
                self.assertEqual(captured["kwargs"], {})
                cli.sdk_list_public_ips(FAKE_COMPARTMENT,
                                        availability_domain=FAKE_AD)
                self.assertEqual(captured["args"],
                                 ("AVAILABILITY_DOMAIN", FAKE_COMPARTMENT))
                self.assertEqual(captured["kwargs"],
                                 {"availability_domain": FAKE_AD})

    def test_list_vnics_composite(self):
        """sdk_list_vnics：给了 compartment 就不查实例；没给就先查实例取 compartment。"""
        cli = OCIClient(profile="DEFAULT", region="ap-singapore-1")
        with patch.object(OCIClient, "sdk_get_instance") as mock_get, \
                patch.object(OCIClient, "sdk_list_vnic_attachments",
                             return_value=[{"vnic-id": FAKE_VNIC}]) as mock_list, \
                patch.object(OCIClient, "sdk_get_vnic",
                             return_value=_vnic()) as mock_vnic:
            result = cli.sdk_list_vnics(FAKE_INSTANCE,
                                        compartment_id=FAKE_COMPARTMENT)
        mock_get.assert_not_called()
        mock_list.assert_called_once_with(
            FAKE_COMPARTMENT, instance_id=FAKE_INSTANCE, region=None)
        mock_vnic.assert_called_once_with(FAKE_VNIC, region=None)
        self.assertEqual(result, [_vnic()])

        inst = _instance()
        inst["compartment-id"] = FAKE_COMPARTMENT
        with patch.object(OCIClient, "sdk_get_instance",
                          return_value=inst) as mock_get, \
                patch.object(OCIClient, "sdk_list_vnic_attachments",
                             return_value=[]) as mock_list:
            result = cli.sdk_list_vnics(FAKE_INSTANCE)
        mock_get.assert_called_once_with(FAKE_INSTANCE, region=None)
        mock_list.assert_called_once_with(
            FAKE_COMPARTMENT, instance_id=FAKE_INSTANCE, region=None)
        self.assertEqual(result, [])


class TestRound3ReadsNoSubprocess(_NoSubprocessMixin, unittest.TestCase):
    """第 3 轮：网络 / 抢机 / 控制台 / IAM / 邮件读接口只走 SDK，不起子进程。"""

    def test_network_reads(self):
        cli = _mock_cli()
        with self._guard_subprocess():
            vnics = list_vnics(cli, FAKE_INSTANCE)
            self.assertEqual(len(vnics), 1)
            self.assertEqual(vnics[0]["public-ip"], FAKE_IP)
            self.assertEqual(get_public_ip(cli, FAKE_INSTANCE), FAKE_IP)
            self.assertEqual(get_vnic(cli, FAKE_VNIC)["id"], FAKE_VNIC)
            ips = list_public_ips(cli, FAKE_COMPARTMENT)
            self.assertEqual(ips[0]["ip-address"], FAKE_IP)
            ips_ad = list_public_ips(cli, FAKE_COMPARTMENT,
                                     availability_domain=FAKE_AD)
            self.assertEqual(len(ips_ad), 1)
            ipv6s = list_ipv6_for_vnic(cli, FAKE_VNIC)
            self.assertEqual(ipv6s[0]["ip-address"], "2001:db8::1")
            privates = list_private_ips(cli, FAKE_VNIC)
            self.assertEqual(privates[0]["ip-address"], "10.0.0.5")
        cli.run.assert_not_called()

    def test_repair_instance_ip_with_ip_present(self):
        cli = _mock_cli()
        with self._guard_subprocess():
            report = repair_instance_ip(cli, FAKE_COMPARTMENT, FAKE_INSTANCE)
        self.assertFalse(report["repaired"])
        self.assertEqual(report["public_ip"], FAKE_IP)
        cli.run.assert_not_called()

    def test_grabber_reads(self):
        cli = _mock_cli()
        cli.run_text.return_value = FAKE_INSTANCE
        cli.sdk_list_instances.return_value = []
        grabber = Grabber(cli, FAKE_COMPARTMENT, FAKE_AD,
                          "VM.Standard.E2.1.Micro", "ocid1.image.oc1..xxxx",
                          "ocid1.subnet.oc1..xxxx", "test-instance",
                          ssh_keys_file=None)
        with self._guard_subprocess():
            self.assertEqual(grabber.try_launch_once(), FAKE_INSTANCE)
            self.assertEqual(grabber.get_instance_state(FAKE_INSTANCE),
                             "RUNNING")
            self.assertEqual(grabber.get_public_ip(FAKE_INSTANCE), FAKE_IP)
        cli.run.assert_not_called()

    def test_grabber_get_instance_state_error_returns_empty(self):
        cli = _mock_cli()
        cli.sdk_get_instance.side_effect = OCIError("boom")
        grabber = Grabber(cli, FAKE_COMPARTMENT, FAKE_AD,
                          "VM.Standard.E2.1.Micro", "ocid1.image.oc1..xxxx",
                          "ocid1.subnet.oc1..xxxx", "test-instance",
                          ssh_keys_file=None)
        self.assertEqual(grabber.get_instance_state(FAKE_INSTANCE), "")

    def test_console_list(self):
        cli = _mock_cli()
        with self._guard_subprocess():
            conns = list_console_connections(cli, FAKE_COMPARTMENT,
                                             FAKE_INSTANCE)
        self.assertEqual(len(conns), 1)
        self.assertEqual(conns[0]["lifecycle-state"], "ACTIVE")
        cli.run.assert_not_called()

    def test_iam_reads(self):
        cli = _mock_cli()
        with self._guard_subprocess():
            self.assertEqual(
                get_group_id_by_name(cli, FAKE_COMPARTMENT, "Admins"),
                FAKE_GROUP)
            self.assertIsNone(
                get_group_id_by_name(cli, FAKE_COMPARTMENT, "Nope"))
            keys = list_api_keys(cli, FAKE_USER)
            self.assertEqual(keys[0]["fingerprint"], "aa:bb:cc")
        cli.run.assert_not_called()

    def test_email_reads(self):
        cli = _mock_cli()
        with self._guard_subprocess():
            senders = list_senders(cli, FAKE_COMPARTMENT)
            self.assertEqual(senders[0]["email-address"],
                             "noreply@example.com")
            users = list_iam_users(cli, FAKE_COMPARTMENT)
            self.assertEqual(users[0]["name"], "api_user")
        cli.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
