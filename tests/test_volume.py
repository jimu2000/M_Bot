#!/usr/bin/env python3
"""volume.py 的单元测试（mock subprocess，不碰真实 API）。

运行：python3 -m unittest tests.test_volume -v

注意：resize / delete 是写操作，测试只 mock 命令构造检查，
绝不对真实 API 执行。
"""

import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oci.client import NotFoundError
from oci.volume import (delete_boot_volume, get_boot_volume,
                        list_boot_volume_attachments, list_boot_volumes,
                        list_orphan_boot_volumes, list_volumes,
                        resize_boot_volume, total_boot_volume_gb)

# 占位符（测试里不出现真实 OCID）
FAKE_COMPARTMENT = "ocid1.tenancy.oc1..xxxxxxxx"
FAKE_BV_ATTACHED = "ocid1.bootvolume.oc1..aaaa"
FAKE_BV_ORPHAN = "ocid1.bootvolume.oc1..bbbb"
FAKE_INSTANCE = "ocid1.instance.oc1..cccc"
FAKE_VOLUME = "ocid1.volume.oc1..dddd"


def _attached_boot_volume():
    """挂在实例上的启动盘。

    注意：启动盘没有 IN-USE 状态，挂在运行中实例上的盘同样是 AVAILABLE，
    且启动盘资源本身不带 instance-id（实测：2026-11-23，新加坡 3 台在跑
    实例的启动盘全是 AVAILABLE、无 instance-id）。所以挂载关系只能查
    attachment 列表，不能看 lifecycle-state 或 instance-id。
    """
    return {
        "id": FAKE_BV_ATTACHED,
        "display-name": "boot-volume-attached",
        "size-in-gbs": 47,
        "lifecycle-state": "AVAILABLE",
        "availability-domain": "FAKE-AD-1",
    }


def _orphan_boot_volume():
    """孤儿启动盘（AVAILABLE，无 instance-id）：释放实例时保留下来的盘。"""
    return {
        "id": FAKE_BV_ORPHAN,
        "display-name": "boot-volume-orphan",
        "size-in-gbs": 47,
        "lifecycle-state": "AVAILABLE",
        "availability-domain": "FAKE-AD-1",
    }


def _terminated_boot_volume():
    """已删除的启动盘（TERMINATED）：不占免费额度，统计时要排除。"""
    return {
        "id": "ocid1.bootvolume.oc1..eeee",
        "display-name": "boot-volume-terminated",
        "size-in-gbs": 47,
        "lifecycle-state": "TERMINATED",
        "availability-domain": "FAKE-AD-1",
    }


def _terminating_boot_volume():
    """删除中的启动盘（TERMINATING）：不占免费额度，统计时要排除。"""
    return {
        "id": "ocid1.bootvolume.oc1..ffff",
        "display-name": "boot-volume-terminating",
        "size-in-gbs": 47,
        "lifecycle-state": "TERMINATING",
        "availability-domain": "FAKE-AD-1",
    }


def _mock_cli(return_value):
    """造一个 cli.run 返回固定值的 mock OCIClient。"""
    cli = MagicMock()
    cli.run.return_value = return_value
    return cli


class TestListBootVolumes(unittest.TestCase):
    def test_returns_simplified_list(self):
        cli = _mock_cli({"data": [_attached_boot_volume(),
                                  _orphan_boot_volume()]})
        result = list_boot_volumes(cli, FAKE_COMPARTMENT)
        self.assertEqual(len(result), 2)
        # 精简字段齐全（availability-domain 保留，供孤儿盘判断查挂载用）
        self.assertEqual(result[0]["id"], FAKE_BV_ATTACHED)
        self.assertEqual(result[0]["display-name"], "boot-volume-attached")
        self.assertEqual(result[0]["size-in-gbs"], 47)
        self.assertEqual(result[0]["lifecycle-state"], "AVAILABLE")
        self.assertEqual(result[0]["availability-domain"], "FAKE-AD-1")
        # 真实 API 不返回 instance-id，两盘都不带该键
        self.assertNotIn("instance-id", result[0])
        self.assertNotIn("instance-id", result[1])

    def test_keeps_instance_id_when_present(self):
        # 兜底逻辑：API 将来如果返回 instance-id，要保留下来
        item = _attached_boot_volume()
        item["instance-id"] = FAKE_INSTANCE
        cli = _mock_cli({"data": [item]})
        result = list_boot_volumes(cli, FAKE_COMPARTMENT)
        self.assertEqual(result[0]["instance-id"], FAKE_INSTANCE)

    def test_command_args(self):
        cli = _mock_cli({"data": []})
        list_boot_volumes(cli, FAKE_COMPARTMENT)
        cli.run.assert_called_once_with(
            ["bv", "boot-volume", "list",
             "--compartment-id", FAKE_COMPARTMENT])

    def test_empty_compartment(self):
        cli = _mock_cli({"data": []})
        self.assertEqual(list_boot_volumes(cli, FAKE_COMPARTMENT), [])


class TestGetBootVolume(unittest.TestCase):
    def test_returns_detail(self):
        detail = _attached_boot_volume()
        cli = _mock_cli({"data": detail})
        result = get_boot_volume(cli, FAKE_BV_ATTACHED)
        self.assertEqual(result, detail)
        cli.run.assert_called_once_with(
            ["bv", "boot-volume", "get",
             "--boot-volume-id", FAKE_BV_ATTACHED])

    def test_not_found_propagates(self):
        cli = MagicMock()
        cli.run.side_effect = NotFoundError("not found")
        with self.assertRaises(NotFoundError):
            get_boot_volume(cli, "ocid1.bootvolume.oc1..nonexistent")


class TestListVolumes(unittest.TestCase):
    def test_returns_simplified_list(self):
        cli = _mock_cli({"data": [{
            "id": FAKE_VOLUME,
            "display-name": "data-volume",
            "size-in-gbs": 50,
            "lifecycle-state": "AVAILABLE",
            "availability-domain": "FAKE-AD-1",
        }]})
        result = list_volumes(cli, FAKE_COMPARTMENT)
        self.assertEqual(result, [{
            "id": FAKE_VOLUME,
            "display-name": "data-volume",
            "size-in-gbs": 50,
            "lifecycle-state": "AVAILABLE",
        }])
        cli.run.assert_called_once_with(
            ["bv", "volume", "list",
             "--compartment-id", FAKE_COMPARTMENT])


class TestResizeBootVolume(unittest.TestCase):
    def test_command_args(self):
        # 只检查命令构造，不对真实 API 执行
        cli = _mock_cli({"data": {"id": FAKE_BV_ATTACHED,
                                  "size-in-gbs": 100}})
        resize_boot_volume(cli, FAKE_BV_ATTACHED, 100)
        cli.run.assert_called_once_with(
            ["bv", "boot-volume", "update",
             "--boot-volume-id", FAKE_BV_ATTACHED,
             "--size-in-gbs", "100",
             # --force 必加：不加时 OCI CLI 交互确认会挂到超时
             # （2026-11-23 真实验证）。
             "--force"])

    def test_invalid_size_rejected(self):
        cli = _mock_cli({"data": {}})
        with self.assertRaises(ValueError):
            resize_boot_volume(cli, FAKE_BV_ATTACHED, 0)
        with self.assertRaises(ValueError):
            resize_boot_volume(cli, FAKE_BV_ATTACHED, -10)
        cli.run.assert_not_called()


class TestDeleteBootVolume(unittest.TestCase):
    def test_command_args(self):
        # 只检查命令构造，不对真实 API 执行
        cli = _mock_cli(None)
        self.assertIsNone(delete_boot_volume(cli, FAKE_BV_ORPHAN))
        cli.run.assert_called_once_with(
            ["bv", "boot-volume", "delete",
             "--boot-volume-id", FAKE_BV_ORPHAN,
             "--force"])


def _mock_cli_with_attachments(boot_volumes, attachments):
    """mock 两路调用：boot-volume list 返回磁盘，attachment list 返回挂载关系。"""
    cli = MagicMock()

    def _run(args):
        if args[:2] == ["bv", "boot-volume"]:
            return {"data": boot_volumes}
        if args[:2] == ["compute", "boot-volume-attachment"]:
            return {"data": attachments}
        raise AssertionError(f"unexpected args: {args}")

    cli.run.side_effect = _run
    return cli


class TestListBootVolumeAttachments(unittest.TestCase):
    def test_returns_simplified_list(self):
        cli = _mock_cli({"data": [{
            "boot-volume-id": FAKE_BV_ATTACHED,
            "instance-id": FAKE_INSTANCE,
            "lifecycle-state": "ATTACHED",
            "availability-domain": "FAKE-AD-1",
        }]})
        result = list_boot_volume_attachments(cli, FAKE_COMPARTMENT,
                                              "FAKE-AD-1")
        self.assertEqual(result, [{
            "boot-volume-id": FAKE_BV_ATTACHED,
            "instance-id": FAKE_INSTANCE,
            "lifecycle-state": "ATTACHED",
        }])
        cli.run.assert_called_once_with(
            ["compute", "boot-volume-attachment", "list",
             "--compartment-id", FAKE_COMPARTMENT,
             "--availability-domain", "FAKE-AD-1"])


class TestListOrphanBootVolumes(unittest.TestCase):
    def test_only_orphan_returned(self):
        # 两盘都是 AVAILABLE（一盘有实例挂载、一盘无实例），只返回孤儿盘。
        # 这正是真实 API 的情况：只看 lifecycle-state 会把两盘都误判成孤儿。
        cli = _mock_cli_with_attachments(
            [_attached_boot_volume(), _orphan_boot_volume()],
            [{"boot-volume-id": FAKE_BV_ATTACHED,
              "instance-id": FAKE_INSTANCE,
              "lifecycle-state": "ATTACHED"}])
        result = list_orphan_boot_volumes(cli, FAKE_COMPARTMENT)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], FAKE_BV_ORPHAN)
        self.assertEqual(result[0]["lifecycle-state"], "AVAILABLE")

    def test_no_orphans(self):
        # 两盘都有 ATTACHED 记录，无孤儿
        cli = _mock_cli_with_attachments(
            [_attached_boot_volume(), _orphan_boot_volume()],
            [{"boot-volume-id": FAKE_BV_ATTACHED,
              "instance-id": FAKE_INSTANCE,
              "lifecycle-state": "ATTACHED"},
             {"boot-volume-id": FAKE_BV_ORPHAN,
              "instance-id": FAKE_INSTANCE,
              "lifecycle-state": "ATTACHED"}])
        self.assertEqual(list_orphan_boot_volumes(cli, FAKE_COMPARTMENT), [])

    def test_detached_attachment_does_not_count(self):
        # 挂载记录是 DETACHED（已分离）时，盘仍算孤儿
        cli = _mock_cli_with_attachments(
            [_orphan_boot_volume()],
            [{"boot-volume-id": FAKE_BV_ORPHAN,
              "instance-id": FAKE_INSTANCE,
              "lifecycle-state": "DETACHED"}])
        result = list_orphan_boot_volumes(cli, FAKE_COMPARTMENT)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], FAKE_BV_ORPHAN)


class TestTotalBootVolumeGb(unittest.TestCase):
    def test_sums_all_boot_volumes(self):
        # 全部启动盘都计入（IN-USE 与 AVAILABLE 都占免费额度）
        cli = _mock_cli({"data": [_attached_boot_volume(),
                                  _orphan_boot_volume()]})
        self.assertEqual(total_boot_volume_gb(cli, FAKE_COMPARTMENT), 94)

    def test_excludes_terminated_and_terminating(self):
        # 已删除（TERMINATED）与删除中（TERMINATING）的盘不占额度，要排除。
        # 回归测试：圣何塞删过盘的账号，旧实现会把已删除的盘也算进去。
        cli = _mock_cli({"data": [_attached_boot_volume(),
                                  _terminated_boot_volume(),
                                  _terminating_boot_volume()]})
        self.assertEqual(total_boot_volume_gb(cli, FAKE_COMPARTMENT), 47)

    def test_empty_is_zero(self):
        cli = _mock_cli({"data": []})
        self.assertEqual(total_boot_volume_gb(cli, FAKE_COMPARTMENT), 0)


if __name__ == "__main__":
    unittest.main()
