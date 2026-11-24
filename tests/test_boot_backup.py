#!/usr/bin/env python3
"""boot_backup.py 的单元测试（mock OCIClient，不碰真实 API）。

运行：python3 -m unittest tests.test_boot_backup -v

注意：创建 / 删除 / 恢复是写操作，测试只 mock 命令构造检查，
绝不对真实 API 执行。
"""

import os
import re
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oci.boot_backup import (
    FREE_BACKUP_LIMIT,
    check_backup_quota,
    create_boot_volume_backup,
    create_boot_volume_backup_with_limit_retry,
    default_backup_name,
    delete_boot_volume_backup,
    get_boot_volume_backup,
    get_source_instance_shape,
    list_boot_volume_backups,
    restore_boot_volume_backup,
    wait_for_backup_available,
)
from oci.client import OCIError

# 占位符（测试里不出现真实 OCID）
FAKE_COMPARTMENT = "ocid1.tenancy.oc1..xxxxxxxx"
FAKE_BV = "ocid1.bootvolume.oc1..aaaa"
FAKE_BV_NEW = "ocid1.bootvolume.oc1..bbbb"
FAKE_BACKUP = "ocid1.bootvolumebackup.oc1..cccc"
FAKE_INSTANCE = "ocid1.instance.oc1..dddd"
FAKE_SUBNET = "ocid1.subnet.oc1..eeee"
FAKE_AD = "FAKE:AP-SINGAPORE-1-AD-1"


def _backup(backup_id, name, created, state="AVAILABLE", size=47):
    """一条备份记录（OCI CLI 返回的 kebab-case 字段）。"""
    return {
        "id": backup_id,
        "display-name": name,
        "boot-volume-id": FAKE_BV,
        "size-in-gbs": size,
        "lifecycle-state": state,
        "source-type": "MANUAL",
        "type": "FULL",
        "time-created": created,
        "image-id": "ocid1.image.oc1..ffff",
    }


class TestListBackups(unittest.TestCase):
    """列表：命令构造 + 按创建时间排序 + 字段精简。"""

    def test_list_command_and_sort(self):
        cli = MagicMock()
        # 故意打乱顺序返回，验证本地按 time-created 升序重排。
        cli.run.return_value = {"data": [
            _backup("ocid1.bootvolumebackup.oc1..new", "new",
                    "2026-11-24T10:00:00+00:00"),
            _backup("ocid1.bootvolumebackup.oc1..old", "old",
                    "2026-11-20T10:00:00+00:00"),
        ]}
        backups = list_boot_volume_backups(cli, FAKE_COMPARTMENT)
        args = cli.run.call_args[0][0]
        self.assertEqual(args[:3], ["bv", "boot-volume-backup", "list"])
        self.assertIn(FAKE_COMPARTMENT, args)
        self.assertIn("--all", args)
        self.assertEqual(backups[0]["display-name"], "old")
        self.assertEqual(backups[1]["display-name"], "new")
        self.assertEqual(backups[0]["size-in-gbs"], 47)
        self.assertEqual(backups[0]["type"], "FULL")

    def test_list_filter_by_boot_volume(self):
        cli = MagicMock()
        cli.run.return_value = {"data": []}
        list_boot_volume_backups(cli, FAKE_COMPARTMENT,
                                 boot_volume_id=FAKE_BV)
        args = cli.run.call_args[0][0]
        self.assertIn("--boot-volume-id", args)
        self.assertIn(FAKE_BV, args)

    def test_list_excludes_terminated_by_default(self):
        # 已删除的备份 OCI 侧仍会列出一段时间，默认不显示（与配额计数
        # 只数未删除的一致；浏览器实测 2026-11-24 发现列表里有
        # TERMINATED 行而配额显示 3/5 对不上）。
        cli = MagicMock()
        cli.run.return_value = {"data": [
            _backup("ocid1.bootvolumebackup.oc1..gone", "gone",
                    "2026-11-20T10:00:00+00:00", state="TERMINATED"),
            _backup("ocid1.bootvolumebackup.oc1..going", "going",
                    "2026-11-21T10:00:00+00:00", state="TERMINATING"),
            _backup("ocid1.bootvolumebackup.oc1..live", "live",
                    "2026-11-22T10:00:00+00:00"),
        ]}
        backups = list_boot_volume_backups(cli, FAKE_COMPARTMENT)
        self.assertEqual([b["display-name"] for b in backups], ["live"])

    def test_list_include_terminated(self):
        cli = MagicMock()
        cli.run.return_value = {"data": [
            _backup("ocid1.bootvolumebackup.oc1..gone", "gone",
                    "2026-11-20T10:00:00+00:00", state="TERMINATED"),
            _backup("ocid1.bootvolumebackup.oc1..live", "live",
                    "2026-11-22T10:00:00+00:00"),
        ]}
        backups = list_boot_volume_backups(cli, FAKE_COMPARTMENT,
                                           include_terminated=True)
        self.assertEqual([b["display-name"] for b in backups],
                         ["gone", "live"])


class TestGetBackup(unittest.TestCase):
    """查单个备份。"""

    def test_get(self):
        cli = MagicMock()
        cli.run.return_value = {"data": _backup(
            FAKE_BACKUP, "b1", "2026-11-23T10:00:00+00:00")}
        backup = get_boot_volume_backup(cli, FAKE_BACKUP)
        args = cli.run.call_args[0][0]
        self.assertEqual(args[:3], ["bv", "boot-volume-backup", "get"])
        self.assertIn(FAKE_BACKUP, args)
        self.assertEqual(backup["id"], FAKE_BACKUP)


class TestCreateBackup(unittest.TestCase):
    """创建备份：FULL 类型 + 默认命名 + 显式命名。"""

    def test_create_with_explicit_name(self):
        cli = MagicMock()
        cli.run.return_value = {"data": _backup(
            FAKE_BACKUP, "my-backup", "2026-11-24T10:00:00+00:00")}
        backup = create_boot_volume_backup(cli, FAKE_BV, "my-backup")
        args = cli.run.call_args[0][0]
        self.assertEqual(args[:3], ["bv", "boot-volume-backup", "create"])
        self.assertIn("--type", args)
        self.assertEqual(args[args.index("--type") + 1], "FULL")
        self.assertIn("my-backup", args)
        self.assertEqual(backup["display-name"], "my-backup")

    def test_create_default_name_uses_volume_name(self):
        cli = MagicMock()
        # create 先查卷名（oci.volume.get_boot_volume），再 create。
        with patch("oci.volume.get_boot_volume") as mock_get:
            mock_get.return_value = {"display-name": "my-instance"}
            cli.run.return_value = {"data": _backup(
                FAKE_BACKUP, "my-instance-backup-20261124-1000",
                "2026-11-24T10:00:00+00:00")}
            create_boot_volume_backup(cli, FAKE_BV)
        args = cli.run.call_args[0][0]
        name = args[args.index("--display-name") + 1]
        self.assertTrue(re.match(r"my-instance-backup-\d{8}-\d{4}$", name),
                        f"默认名格式不对：{name}")

    def test_create_default_name_falls_back_when_volume_lookup_fails(self):
        cli = MagicMock()
        with patch("oci.volume.get_boot_volume",
                   side_effect=Exception("boom")):
            cli.run.return_value = {"data": _backup(
                FAKE_BACKUP, "boot-volume-backup-20261124-1000",
                "2026-11-24T10:00:00+00:00")}
            create_boot_volume_backup(cli, FAKE_BV)
        args = cli.run.call_args[0][0]
        name = args[args.index("--display-name") + 1]
        self.assertTrue(re.match(r"boot-volume-backup-\d{8}-\d{4}$", name),
                        f"兜底名格式不对：{name}")


class TestDefaultBackupName(unittest.TestCase):
    """默认备份名格式：<名字>-backup-YYYYMMDD-HHMM。"""

    def test_format(self):
        name = default_backup_name("my-instance")
        self.assertTrue(re.match(r"my-instance-backup-\d{8}-\d{4}$", name),
                        name)

    def test_empty_base_falls_back(self):
        name = default_backup_name("")
        self.assertTrue(name.startswith("boot-volume-backup-"), name)

    def test_uses_configured_timezone_time(self):
        # 名字里的时间戳用 _backup_name_now()（北京时间），不是 UTC：
        # 固定它返回 2026-11-24 04:30，名字后缀必须是 20261124-0430。
        from datetime import datetime

        fixed = datetime(2026, 11, 24, 4, 30)
        with patch("oci.boot_backup._backup_name_now",
                   return_value=fixed):
            name = default_backup_name("my-instance")
        self.assertEqual(name, "my-instance-backup-20261124-0430")


class TestCreateWithLimitRetry(unittest.TestCase):
    """带重试的创建：只重试 LimitExceeded，其他错误立即抛出。"""

    def test_success_first_try(self):
        cli = MagicMock()
        expected = _backup(FAKE_BACKUP, "b1",
                           "2026-11-24T10:00:00+00:00")
        with patch("oci.boot_backup.create_boot_volume_backup",
                   return_value=expected) as mock_create:
            result = create_boot_volume_backup_with_limit_retry(
                cli, FAKE_BV, "b1")
        self.assertEqual(result, expected)
        mock_create.assert_called_once_with(cli, FAKE_BV, "b1")

    def test_limit_exceeded_then_success(self):
        cli = MagicMock()
        expected = _backup(FAKE_BACKUP, "b1",
                           "2026-11-24T10:00:00+00:00")
        limit_err = OCIError("ServiceError: LimitExceeded: "
                             "you have reached your limit.")
        with patch("oci.boot_backup.create_boot_volume_backup",
                   side_effect=[limit_err, expected]) as mock_create, \
                patch("oci.boot_backup.time.sleep") as mock_sleep:
            result = create_boot_volume_backup_with_limit_retry(
                cli, FAKE_BV)
        self.assertEqual(result, expected)
        self.assertEqual(mock_create.call_count, 2)
        mock_sleep.assert_called_once()

    def test_non_limit_error_raises_immediately(self):
        cli = MagicMock()
        other_err = OCIError("ServiceError: NotFound: no such volume.")
        with patch("oci.boot_backup.create_boot_volume_backup",
                   side_effect=other_err) as mock_create, \
                patch("oci.boot_backup.time.sleep") as mock_sleep:
            with self.assertRaises(OCIError):
                create_boot_volume_backup_with_limit_retry(cli, FAKE_BV)
        mock_create.assert_called_once()
        mock_sleep.assert_not_called()

    def test_all_retries_exhausted_raises_last_error(self):
        cli = MagicMock()
        limit_err = OCIError("ServiceError: LimitExceeded: limit.")
        with patch("oci.boot_backup.create_boot_volume_backup",
                   side_effect=limit_err) as mock_create, \
                patch("oci.boot_backup.time.sleep"):
            with self.assertRaises(OCIError):
                create_boot_volume_backup_with_limit_retry(cli, FAKE_BV)
        from oci.boot_backup import CREATE_LIMIT_RETRY_ATTEMPTS
        self.assertEqual(mock_create.call_count,
                         CREATE_LIMIT_RETRY_ATTEMPTS)


class TestDeleteBackup(unittest.TestCase):
    """删除备份：必须带 --force（防交互挂起）。"""

    def test_delete_has_force(self):
        cli = MagicMock()
        cli.run.return_value = None
        delete_boot_volume_backup(cli, FAKE_BACKUP)
        args = cli.run.call_args[0][0]
        self.assertEqual(args[:3], ["bv", "boot-volume-backup", "delete"])
        self.assertIn("--force", args)
        self.assertIn(FAKE_BACKUP, args)


class TestCheckQuota(unittest.TestCase):
    """配额检查：满 / 未满 / 最早备份取对 / 已删的不计。"""

    def _cli_with(self, backups):
        cli = MagicMock()
        cli.run.return_value = {"data": backups}
        return cli

    def test_not_full(self):
        cli = self._cli_with([
            _backup("ocid1.bootvolumebackup.oc1..1", "b1",
                    "2026-11-20T10:00:00+00:00"),
            _backup("ocid1.bootvolumebackup.oc1..2", "b2",
                    "2026-11-21T10:00:00+00:00"),
        ])
        count, limit, full, oldest = check_backup_quota(cli, FAKE_COMPARTMENT)
        self.assertEqual(count, 2)
        self.assertEqual(limit, FREE_BACKUP_LIMIT)
        self.assertEqual(limit, 5)
        self.assertFalse(full)
        self.assertEqual(oldest["display-name"], "b1")

    def test_full_returns_oldest(self):
        backups = [_backup(f"ocid1.bootvolumebackup.oc1..{i}", f"b{i}",
                           f"2026-11-2{i}T10:00:00+00:00")
                   for i in range(5)]
        cli = self._cli_with(backups)
        count, limit, full, oldest = check_backup_quota(cli, FAKE_COMPARTMENT)
        self.assertEqual(count, 5)
        self.assertTrue(full)
        self.assertEqual(oldest["display-name"], "b0")

    def test_terminated_not_counted(self):
        cli = self._cli_with([
            _backup("ocid1.bootvolumebackup.oc1..1", "b1",
                    "2026-11-20T10:00:00+00:00", state="TERMINATED"),
            _backup("ocid1.bootvolumebackup.oc1..2", "b2",
                    "2026-11-21T10:00:00+00:00", state="TERMINATING"),
            _backup("ocid1.bootvolumebackup.oc1..3", "b3",
                    "2026-11-22T10:00:00+00:00"),
        ])
        count, _limit, full, oldest = check_backup_quota(
            cli, FAKE_COMPARTMENT)
        self.assertEqual(count, 1)
        self.assertFalse(full)
        self.assertEqual(oldest["display-name"], "b3")

    def test_empty(self):
        cli = self._cli_with([])
        count, _limit, full, oldest = check_backup_quota(
            cli, FAKE_COMPARTMENT)
        self.assertEqual(count, 0)
        self.assertFalse(full)
        self.assertIsNone(oldest)


class TestGetSourceInstanceShape(unittest.TestCase):
    """来源形状：卷 -> 挂载 -> 实例链路；查不到返回 None。"""

    def test_flex_shape_detected(self):
        cli = MagicMock()
        backup = _backup(FAKE_BACKUP, "b1", "2026-11-23T10:00:00+00:00")
        with patch("oci.volume.get_boot_volume") as mock_get_vol, \
             patch("oci.volume.list_boot_volume_attachments") as mock_atts, \
             patch("oci.instance.get_instance") as mock_get_inst:
            mock_get_vol.return_value = {"availability-domain": FAKE_AD}
            mock_atts.return_value = [{"boot-volume-id": FAKE_BV,
                                       "instance-id": FAKE_INSTANCE}]
            mock_get_inst.return_value = {
                "shape": "VM.Standard.A1.Flex",
                "shape-config": {"ocpus": 2, "memory-in-gbs": 12}}
            result = get_source_instance_shape(cli, FAKE_COMPARTMENT, backup)
        self.assertEqual(result, ("VM.Standard.A1.Flex", 2, 12))

    def test_fixed_shape_returns_none_specs(self):
        cli = MagicMock()
        backup = _backup(FAKE_BACKUP, "b1", "2026-11-23T10:00:00+00:00")
        with patch("oci.volume.get_boot_volume") as mock_get_vol, \
             patch("oci.volume.list_boot_volume_attachments") as mock_atts, \
             patch("oci.instance.get_instance") as mock_get_inst:
            mock_get_vol.return_value = {"availability-domain": FAKE_AD}
            mock_atts.return_value = [{"boot-volume-id": FAKE_BV,
                                       "instance-id": FAKE_INSTANCE}]
            mock_get_inst.return_value = {"shape": "VM.Standard.E2.1.Micro"}
            result = get_source_instance_shape(cli, FAKE_COMPARTMENT, backup)
        self.assertEqual(result, ("VM.Standard.E2.1.Micro", None, None))

    def test_source_gone_returns_none(self):
        cli = MagicMock()
        backup = _backup(FAKE_BACKUP, "b1", "2026-11-23T10:00:00+00:00")
        # 来源启动卷已删：get_boot_volume 抛异常，函数返回 None 不抛。
        with patch("oci.volume.get_boot_volume",
                   side_effect=Exception("not found")):
            result = get_source_instance_shape(cli, FAKE_COMPARTMENT, backup)
        self.assertIsNone(result)

    def test_no_boot_volume_id_returns_none(self):
        cli = MagicMock()
        result = get_source_instance_shape(cli, FAKE_COMPARTMENT, {})
        self.assertIsNone(result)


class TestRestore(unittest.TestCase):
    """恢复：两步流程（建卷 -> 开机）+ 失败清理 + 非 AVAILABLE 拒绝。"""

    def _cli_for_restore(self, shape_args=None):
        """cli.run 按命令分发：get 备份 / create 卷 / launch 实例。"""
        cli = MagicMock()

        def _run(args):
            if args[:3] == ["bv", "boot-volume-backup", "get"]:
                return {"data": _backup(
                    FAKE_BACKUP, "b1", "2026-11-23T10:00:00+00:00", size=50)}
            if args[:3] == ["bv", "boot-volume", "create"]:
                return {"data": {"id": FAKE_BV_NEW,
                                 "lifecycle-state": "AVAILABLE"}}
            if args[:3] == ["compute", "instance", "launch"]:
                return {"data": {"id": FAKE_INSTANCE,
                                 "lifecycle-state": "PROVISIONING"}}
            raise AssertionError(f"unexpected command: {args}")

        cli.run.side_effect = _run
        return cli

    def test_restore_two_steps_with_explicit_shape(self):
        cli = self._cli_for_restore()
        result = restore_boot_volume_backup(
            cli, FAKE_COMPARTMENT, FAKE_BACKUP,
            availability_domain=FAKE_AD, subnet_id=FAKE_SUBNET,
            display_name="restored-1", ssh_keys_file="/tmp/keys.pub",
            shape="VM.Standard.A1.Flex", ocpus=2, memory_gb=12)
        self.assertEqual(result["instance_id"], FAKE_INSTANCE)
        self.assertEqual(result["boot_volume_id"], FAKE_BV_NEW)
        self.assertEqual(result["shape"], "VM.Standard.A1.Flex")
        self.assertEqual(result["ocpus"], 2)
        self.assertEqual(result["memory_gb"], 12)
        calls = [c[0][0] for c in cli.run.call_args_list]
        # 第 1 步：从备份建启动卷（带 --boot-volume-backup-id 与等待参数）。
        create_call = next(c for c in calls
                           if c[:3] == ["bv", "boot-volume", "create"])
        self.assertIn("--boot-volume-backup-id", create_call)
        self.assertIn(FAKE_BACKUP, create_call)
        self.assertIn("--wait-for-state", create_call)
        self.assertIn("AVAILABLE", create_call)
        # 第 2 步：用新启动卷开机（--source-boot-volume-id，无 image 参数）。
        launch_call = next(c for c in calls
                           if c[:3] == ["compute", "instance", "launch"])
        self.assertIn("--source-boot-volume-id", launch_call)
        self.assertIn(FAKE_BV_NEW, launch_call)
        self.assertIn("--shape", launch_call)
        self.assertIn("VM.Standard.A1.Flex", launch_call)
        self.assertIn("--shape-config", launch_call)
        self.assertIn("--no-retry", launch_call)
        self.assertNotIn("--image-id", launch_call)
        self.assertNotIn("--source-details", launch_call)
        self.assertNotIn("--boot-volume-size-in-gbs", launch_call)

    def test_restore_uses_source_shape_when_not_given(self):
        cli = self._cli_for_restore()
        with patch("oci.boot_backup.get_source_instance_shape") as mock_shape:
            mock_shape.return_value = ("VM.Standard.E2.1.Micro", None, None)
            result = restore_boot_volume_backup(
                cli, FAKE_COMPARTMENT, FAKE_BACKUP,
                availability_domain=FAKE_AD, subnet_id=FAKE_SUBNET,
                display_name="restored-1", ssh_keys_file="")
        self.assertEqual(result["shape"], "VM.Standard.E2.1.Micro")
        launch_call = next(c[0][0] for c in cli.run.call_args_list
                           if c[0][0][:3] == ["compute", "instance", "launch"])
        # E2 固定规格：不传 --shape-config；ssh_keys_file 为空时不传公钥参数。
        self.assertNotIn("--shape-config", launch_call)
        self.assertNotIn("--ssh-authorized-keys-file", launch_call)

    def test_restore_defaults_to_a1_when_source_unknown(self):
        cli = self._cli_for_restore()
        with patch("oci.boot_backup.get_source_instance_shape",
                   return_value=None):
            result = restore_boot_volume_backup(
                cli, FAKE_COMPARTMENT, FAKE_BACKUP,
                availability_domain=FAKE_AD, subnet_id=FAKE_SUBNET,
                display_name="restored-1", ssh_keys_file="")
        self.assertEqual(result["shape"], "VM.Standard.A1.Flex")
        self.assertEqual(result["ocpus"], 1)
        self.assertEqual(result["memory_gb"], 6)

    def test_restore_rejects_non_available_backup(self):
        cli = MagicMock()
        cli.run.return_value = {"data": _backup(
            FAKE_BACKUP, "b1", "2026-11-23T10:00:00+00:00",
            state="CREATING")}
        with self.assertRaises(ValueError) as ctx:
            restore_boot_volume_backup(
                cli, FAKE_COMPARTMENT, FAKE_BACKUP,
                availability_domain=FAKE_AD, subnet_id=FAKE_SUBNET,
                display_name="restored-1", ssh_keys_file="")
        self.assertIn("CREATING", str(ctx.exception))
        # 非 AVAILABLE 时只查了一次备份，没建卷没开机。
        self.assertEqual(cli.run.call_count, 1)

    def test_restore_launch_failure_cleans_up_new_volume(self):
        cli = MagicMock()

        def _run(args):
            if args[:3] == ["bv", "boot-volume-backup", "get"]:
                return {"data": _backup(
                    FAKE_BACKUP, "b1", "2026-11-23T10:00:00+00:00")}
            if args[:3] == ["bv", "boot-volume", "create"]:
                return {"data": {"id": FAKE_BV_NEW,
                                 "lifecycle-state": "AVAILABLE"}}
            if args[:3] == ["compute", "instance", "launch"]:
                raise Exception("out of capacity")
            raise AssertionError(f"unexpected command: {args}")

        cli.run.side_effect = _run
        with patch("oci.volume.delete_boot_volume") as mock_delete:
            with self.assertRaises(Exception):
                restore_boot_volume_backup(
                    cli, FAKE_COMPARTMENT, FAKE_BACKUP,
                    availability_domain=FAKE_AD, subnet_id=FAKE_SUBNET,
                    display_name="restored-1", ssh_keys_file="",
                    shape="VM.Standard.A1.Flex", ocpus=1, memory_gb=6)
            # launch 失败后尽力删掉第 1 步建出来的启动卷，不留孤儿盘。
            mock_delete.assert_called_once_with(cli, FAKE_BV_NEW)


class TestWaitForBackupAvailable(unittest.TestCase):
    """等备份 AVAILABLE（备份创建完成通知的轮询）。"""

    def test_available_immediately(self):
        cli = MagicMock()
        cli.run.return_value = {"data": _backup(FAKE_BACKUP, "b1",
                                                "2026-11-23T10:00:00+00:00")}
        state = wait_for_backup_available(cli, FAKE_BACKUP)
        self.assertEqual(state, "AVAILABLE")
        self.assertEqual(cli.run.call_count, 1)

    @patch("oci.boot_backup.time.sleep")
    def test_creating_then_available(self, _mock_sleep):
        cli = MagicMock()
        cli.run.side_effect = [
            {"data": _backup(FAKE_BACKUP, "b1",
                             "2026-11-23T10:00:00+00:00",
                             state="CREATING")},
            {"data": _backup(FAKE_BACKUP, "b1",
                             "2026-11-23T10:00:00+00:00")},
        ]
        state = wait_for_backup_available(cli, FAKE_BACKUP)
        self.assertEqual(state, "AVAILABLE")
        self.assertEqual(cli.run.call_count, 2)

    @patch("oci.boot_backup.time.sleep")
    def test_request_received_then_available(self, _mock_sleep):
        cli = MagicMock()
        cli.run.side_effect = [
            {"data": _backup(FAKE_BACKUP, "b1",
                             "2026-11-23T10:00:00+00:00",
                             state="REQUEST_RECEIVED")},
            {"data": _backup(FAKE_BACKUP, "b1",
                             "2026-11-23T10:00:00+00:00")},
        ]
        state = wait_for_backup_available(cli, FAKE_BACKUP)
        self.assertEqual(state, "AVAILABLE")

    def test_faulty_is_terminal(self):
        cli = MagicMock()
        cli.run.return_value = {"data": _backup(
            FAKE_BACKUP, "b1", "2026-11-23T10:00:00+00:00", state="FAULTY")}
        state = wait_for_backup_available(cli, FAKE_BACKUP)
        self.assertEqual(state, "FAULTY")
        self.assertEqual(cli.run.call_count, 1)

    def test_timeout(self):
        cli = MagicMock()
        cli.run.return_value = {"data": _backup(
            FAKE_BACKUP, "b1", "2026-11-23T10:00:00+00:00",
            state="CREATING")}
        with self.assertRaises(TimeoutError):
            wait_for_backup_available(cli, FAKE_BACKUP, timeout_seconds=0)


if __name__ == "__main__":
    unittest.main()
