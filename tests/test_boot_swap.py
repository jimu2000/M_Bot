#!/usr/bin/env python3
"""boot_swap.py 的单元测试（mock OCIClient，不碰真实 API）。

运行：python3 -m unittest tests.test_boot_swap -v

注意：换盘是破坏性写操作，测试只 mock 命令构造检查，
绝不对真实 API 执行。
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oci.boot_swap import (
    BootSwapError,
    attach_boot_volume,
    check_boot_volume_space,
    detach_boot_volume,
    get_boot_volume_attachment,
    replace_boot_volume,
    swap_boot_volume,
    wait_for_boot_volume_attachment,
    wait_for_instance_state,
)
from oci.client import OCIError

# 占位符（测试里不出现真实 OCID）
FAKE_COMPARTMENT = "ocid1.tenancy.oc1..xxxxxxxx"
FAKE_BV_OLD = "ocid1.bootvolume.oc1..aaaa"
FAKE_BV_NEW = "ocid1.bootvolume.oc1..bbbb"
FAKE_BACKUP = "ocid1.bootvolumebackup.oc1..cccc"
FAKE_INSTANCE = "ocid1.instance.oc1..dddd"
FAKE_ATTACHMENT = "ocid1.bootvolumeattachment.oc1..eeee"
FAKE_AD = "FAKE:AP-SINGAPORE-1-AD-1"


def _instance_detail(state="RUNNING"):
    return {"data": {
        "id": FAKE_INSTANCE,
        "display-name": "test-instance",
        "lifecycle-state": state,
        "availability-domain": FAKE_AD,
        "shape": "VM.Standard.A1.Flex",
    }}


def _attachment(state="ATTACHED", volume_id=None):
    return {"data": [{
        "id": FAKE_ATTACHMENT,
        "boot-volume-id": volume_id or FAKE_BV_OLD,
        "instance-id": FAKE_INSTANCE,
        "lifecycle-state": state,
    }]}


def _backup(state="AVAILABLE", size=47):
    return {"data": {
        "id": FAKE_BACKUP,
        "display-name": "test-backup",
        "boot-volume-id": FAKE_BV_OLD,
        "size-in-gbs": size,
        "lifecycle-state": state,
        "source-type": "MANUAL",
        "type": "FULL",
        "time-created": "2026-11-24T10:00:00+00:00",
    }}


def _boot_volumes(total_gb=141):
    """total_boot_volume_gb 用的 bv boot-volume list 返回。"""
    return {"data": [{
        "id": FAKE_BV_OLD,
        "display-name": "test-instance (boot volume)",
        "size-in-gbs": total_gb,
        "lifecycle-state": "AVAILABLE",
        "availability-domain": FAKE_AD,
    }]}


class _SwapCli:
    """按命令路由的假 CLI：instance get 按状态序列返回，原生换盘成功后
    挂载关系切换成新盘（模拟 OCI 换盘生效）。"""

    REPLACE_HEAD = ["compute", "instance",
                    "update-instance-update-instance-source-via-boot-volume-details"]

    def __init__(self, get_states, fail_replace=False,
                 fail_replace_after_attach=False, never_attach_new=False,
                 backup_state="AVAILABLE",
                 backup_size=47, used_gb=141, attachment_state="ATTACHED",
                 attachment_present=True, fail_delete=False):
        self.get_states = list(get_states)
        self.get_calls = 0
        self.calls = []
        self.fail_replace = fail_replace
        self.fail_replace_after_attach = fail_replace_after_attach
        self.never_attach_new = never_attach_new
        self.backup_state = backup_state
        self.backup_size = backup_size
        self.used_gb = used_gb
        self.attachment_state = attachment_state
        self.attachment_present = attachment_present
        self.fail_delete = fail_delete
        self._replace_called = False

    def run(self, args):
        self.calls.append(list(args))
        head = args[:3]
        if head == ["bv", "boot-volume-backup", "get"]:
            return _backup(self.backup_state, self.backup_size)
        if head == ["compute", "instance", "get"]:
            state = self.get_states[min(self.get_calls,
                                        len(self.get_states) - 1)]
            self.get_calls += 1
            return _instance_detail(state)
        if head == ["compute", "boot-volume-attachment", "list"]:
            if not self.attachment_present:
                return {"data": []}
            if self._replace_called and not self.never_attach_new:
                return _attachment("ATTACHED", FAKE_BV_NEW)
            return _attachment(self.attachment_state)
        if head == ["bv", "boot-volume", "list"]:
            return _boot_volumes(self.used_gb)
        if head == ["bv", "boot-volume", "create"]:
            return {"data": {"id": FAKE_BV_NEW}}
        if head == ["bv", "boot-volume", "delete"]:
            if self.fail_delete:
                raise OCIError("delete new volume failed")
            return {}
        if head == self.REPLACE_HEAD:
            if self.fail_replace_after_attach:
                self._replace_called = True
                raise OCIError("replace failed after attach")
            if self.fail_replace:
                raise OCIError("replace failed")
            self._replace_called = True
            return {}
        return {}


class TestGetAttachment(unittest.TestCase):
    """get_boot_volume_attachment：按实例找 ATTACHED 的挂载记录。"""

    def test_finds_attached(self):
        cli = MagicMock()
        cli.run.side_effect = [
            _instance_detail("RUNNING"),
            _attachment("ATTACHED"),
        ]
        att = get_boot_volume_attachment(cli, FAKE_COMPARTMENT, FAKE_INSTANCE)
        self.assertEqual(att["id"], FAKE_ATTACHMENT)
        self.assertEqual(att["boot-volume-id"], FAKE_BV_OLD)
        # list 必须带 include_id（detach 要用挂载记录自身的 id）。
        list_args = cli.run.call_args_list[1][0][0]
        self.assertEqual(list_args[:3],
                         ["compute", "boot-volume-attachment", "list"])
        self.assertIn(FAKE_AD, list_args)

    def test_none_when_no_match(self):
        cli = MagicMock()
        cli.run.side_effect = [
            _instance_detail("RUNNING"),
            {"data": [{"id": "ocid1.bootvolumeattachment.oc1..xxxx",
                       "boot-volume-id": FAKE_BV_OLD,
                       "instance-id": "ocid1.instance.oc1..other",
                       "lifecycle-state": "ATTACHED"}]},
        ]
        self.assertIsNone(
            get_boot_volume_attachment(cli, FAKE_COMPARTMENT, FAKE_INSTANCE))

    def test_none_when_no_ad(self):
        cli = MagicMock()
        cli.run.return_value = {"data": {"id": FAKE_INSTANCE}}
        self.assertIsNone(
            get_boot_volume_attachment(cli, FAKE_COMPARTMENT, FAKE_INSTANCE))


class TestWaitForState(unittest.TestCase):
    """wait_for_instance_state：轮询到目标状态 / 超时。"""

    def test_reaches_target(self):
        cli = MagicMock()
        cli.run.side_effect = [
            _instance_detail("STOPPING"),
            _instance_detail("STOPPED"),
        ]
        with patch("oci.boot_swap.time.sleep"):
            state = wait_for_instance_state(cli, FAKE_INSTANCE, "STOPPED",
                                            timeout_seconds=300)
        self.assertEqual(state, "STOPPED")

    def test_timeout(self):
        cli = MagicMock()
        cli.run.return_value = _instance_detail("STOPPING")
        with patch("oci.boot_swap.time.sleep"), \
                patch("oci.boot_swap.time.time",
                      side_effect=[0, 1000, 1000]):
            with self.assertRaises(TimeoutError):
                wait_for_instance_state(cli, FAKE_INSTANCE, "STOPPED",
                                        timeout_seconds=300)


class TestCheckSpace(unittest.TestCase):
    """check_boot_volume_space：剩 59GB 时 47GB 的盘放得下，60GB 放不下。"""

    def test_enough(self):
        cli = _SwapCli(["RUNNING"], used_gb=141)
        ok, remaining = check_boot_volume_space(cli, FAKE_COMPARTMENT, 47)
        self.assertTrue(ok)
        self.assertEqual(remaining, 59)

    def test_not_enough(self):
        cli = _SwapCli(["RUNNING"], used_gb=141)
        ok, remaining = check_boot_volume_space(cli, FAKE_COMPARTMENT, 60)
        self.assertFalse(ok)
        self.assertEqual(remaining, 59)


class TestAttachDetachCommands(unittest.TestCase):
    """attach/detach 命令构造：无 --type，带 --wait-for-state 与 --force。"""

    def test_detach_command(self):
        cli = MagicMock()
        cli.run.return_value = {}
        detach_boot_volume(cli, FAKE_ATTACHMENT)
        args = cli.run.call_args[0][0]
        self.assertEqual(args[:3],
                         ["compute", "boot-volume-attachment", "detach"])
        self.assertIn(FAKE_ATTACHMENT, args)
        self.assertIn("DETACHED", args)
        # detach 会弹确认提示，不加 --force 非交互下直接 Abort。
        self.assertIn("--force", args)

    def test_attach_command(self):
        cli = MagicMock()
        cli.run.return_value = {}
        attach_boot_volume(cli, FAKE_BV_NEW, FAKE_INSTANCE)
        args = cli.run.call_args[0][0]
        self.assertEqual(args[:3],
                         ["compute", "boot-volume-attachment", "attach"])
        self.assertIn(FAKE_BV_NEW, args)
        self.assertIn(FAKE_INSTANCE, args)
        self.assertIn("ATTACHED", args)
        self.assertNotIn("--type", args)


class TestReplaceCommand(unittest.TestCase):
    """replace_boot_volume 命令构造：原生换盘接口 + --force，不加 wait。"""

    def test_replace_command(self):
        cli = MagicMock()
        cli.run.return_value = {}
        replace_boot_volume(cli, FAKE_INSTANCE, FAKE_BV_NEW)
        args = cli.run.call_args[0][0]
        self.assertEqual(
            args[:3],
            ["compute", "instance",
             "update-instance-update-instance-source-via-boot-volume-details"])
        self.assertIn(FAKE_INSTANCE, args)
        self.assertIn(FAKE_BV_NEW, args)
        # update 命令会弹确认提示，必须加 --force；实例本来就在
        # RUNNING，加 --wait-for-state 会立即返回，不能加。
        self.assertIn("--force", args)
        self.assertNotIn("--wait-for-state", args)


class TestWaitForAttachment(unittest.TestCase):
    """wait_for_boot_volume_attachment：轮询直到挂上新盘。"""

    def _cli_with_lists(self, attachments):
        """list 按顺序返回给定的挂载记录，get 永远返回 RUNNING 详情。"""
        cli = MagicMock()
        queue = list(attachments)

        def _run(args):
            if args[:3] == ["compute", "instance", "get"]:
                return _instance_detail("RUNNING")
            if args[:3] == ["compute", "boot-volume-attachment", "list"]:
                return queue.pop(0) if queue else attachments[-1]
            return {}

        cli.run.side_effect = _run
        return cli

    def test_returns_when_new_volume_attached(self):
        cli = self._cli_with_lists([_attachment("ATTACHED"),
                                    _attachment("ATTACHED", FAKE_BV_NEW)])
        with patch("oci.boot_swap.time.sleep"):
            found = wait_for_boot_volume_attachment(
                cli, FAKE_COMPARTMENT, FAKE_INSTANCE, FAKE_BV_NEW, 60)
        self.assertEqual(found["boot-volume-id"], FAKE_BV_NEW)

    def test_timeout(self):
        cli = self._cli_with_lists([_attachment("ATTACHED")])
        with patch("oci.boot_swap.time.sleep"):
            with self.assertRaises(TimeoutError):
                wait_for_boot_volume_attachment(
                    cli, FAKE_COMPARTMENT, FAKE_INSTANCE, FAKE_BV_NEW, 0)


class TestSwapHappyPath(unittest.TestCase):
    """swap_boot_volume 全流程：建新盘 → 原生换盘 → 回到原状态。"""

    def test_running_instance(self):
        cli = _SwapCli(["RUNNING", "RUNNING", "RUNNING"])
        steps = []
        with patch("oci.boot_swap.time.sleep"):
            result = swap_boot_volume(cli, FAKE_COMPARTMENT, FAKE_INSTANCE,
                                      FAKE_BACKUP, on_step=steps.append)
        self.assertEqual(result["old_boot_volume_id"], FAKE_BV_OLD)
        self.assertEqual(result["new_boot_volume_id"], FAKE_BV_NEW)
        self.assertFalse(result["rolled_back"])
        self.assertTrue(result["was_running"])
        self.assertTrue(result["old_volume_deleted"])
        self.assertEqual(len(steps), 3)
        self.assertTrue(steps[0].startswith("①"))
        self.assertTrue(steps[1].startswith("②"))
        self.assertTrue(steps[2].startswith("③"))
        # 关键命令都执行了，且顺序正确：建盘 → 原生换盘 → 删旧盘。
        heads = [tuple(c[:3]) for c in cli.calls]
        create_i = heads.index(("bv", "boot-volume", "create"))
        replace_i = heads.index(tuple(_SwapCli.REPLACE_HEAD))
        delete_i = heads.index(("bv", "boot-volume", "delete"))
        self.assertLess(create_i, replace_i)
        self.assertLess(replace_i, delete_i)
        replace_args = cli.calls[replace_i]
        self.assertIn(FAKE_BV_NEW, replace_args)
        self.assertIn(FAKE_INSTANCE, replace_args)
        self.assertIn("--force", replace_args)
        # 删的是旧盘（不是新盘），且带 --force（delete 会弹确认）。
        delete_args = cli.calls[delete_i]
        self.assertIn(FAKE_BV_OLD, delete_args)
        self.assertNotIn(FAKE_BV_NEW, delete_args)
        self.assertIn("--force", delete_args)
        # 没有手动停机/拆盘/挂盘/开机命令（OCI 原生换盘内部处理）。
        for call in cli.calls:
            self.assertNotEqual(call[:3], ["compute", "instance", "action"])
            self.assertNotEqual(
                call[:3],
                ["compute", "boot-volume-attachment", "detach"])
            self.assertNotEqual(
                call[:3],
                ["compute", "boot-volume-attachment", "attach"])
        # 新盘与实例同一 AD，且从备份建。
        create_args = cli.calls[create_i]
        self.assertIn(FAKE_BACKUP, create_args)
        self.assertIn(FAKE_AD, create_args)
        self.assertIn("AVAILABLE", create_args)

    def test_stopped_instance_stays_stopped(self):
        cli = _SwapCli(["STOPPED", "STOPPED", "STOPPED"])
        steps = []
        with patch("oci.boot_swap.time.sleep"):
            result = swap_boot_volume(cli, FAKE_COMPARTMENT, FAKE_INSTANCE,
                                      FAKE_BACKUP, on_step=steps.append)
        self.assertFalse(result["was_running"])
        self.assertTrue(result["old_volume_deleted"])
        self.assertEqual(len(steps), 3)
        actions = [c for c in cli.calls
                   if c[:3] == ["compute", "instance", "action"]]
        self.assertEqual(actions, [])

    def test_old_volume_delete_failure_still_succeeds(self):
        """删旧盘失败不影响恢复结果，只标记 old_volume_deleted=False。"""
        cli = _SwapCli(["RUNNING", "RUNNING", "RUNNING"],
                       fail_delete=True)
        steps = []
        with patch("oci.boot_swap.time.sleep"):
            result = swap_boot_volume(cli, FAKE_COMPARTMENT, FAKE_INSTANCE,
                                      FAKE_BACKUP, on_step=steps.append)
        self.assertFalse(result["old_volume_deleted"])
        self.assertTrue(result["was_running"])
        self.assertEqual(result["new_boot_volume_id"], FAKE_BV_NEW)
        self.assertEqual(len(steps), 3)
        # 删盘命令确实发了（只是执行失败），且删的是旧盘。
        deletes = [c for c in cli.calls
                   if c[:3] == ["bv", "boot-volume", "delete"]]
        self.assertEqual(len(deletes), 1)
        self.assertIn(FAKE_BV_OLD, deletes[0])


class TestSwapReplaceFailure(unittest.TestCase):
    """原生换盘失败：确认 OCI 回滚结论，新盘未挂上时删掉。"""

    def test_replace_api_failure_rolls_back(self):
        cli = _SwapCli(["RUNNING", "RUNNING", "RUNNING"],
                       fail_replace=True)
        with patch("oci.boot_swap.time.sleep"):
            with self.assertRaises(BootSwapError) as ctx:
                swap_boot_volume(cli, FAKE_COMPARTMENT, FAKE_INSTANCE,
                                 FAKE_BACKUP)
        err = ctx.exception
        self.assertTrue(err.rolled_back)
        self.assertEqual(err.old_boot_volume_id, FAKE_BV_OLD)
        self.assertEqual(err.new_boot_volume_id, FAKE_BV_NEW)
        self.assertIn("自动回滚", str(err))
        # 新盘未挂上，已删掉。
        deletes = [c for c in cli.calls
                   if c[:3] == ["bv", "boot-volume", "delete"]]
        self.assertEqual(len(deletes), 1)
        self.assertIn(FAKE_BV_NEW, deletes[0])

    def test_replace_failure_cleanup_failure_noted(self):
        cli = _SwapCli(["RUNNING", "RUNNING", "RUNNING"],
                       fail_replace=True, fail_delete=True)
        with patch("oci.boot_swap.time.sleep"):
            with self.assertRaises(BootSwapError) as ctx:
                swap_boot_volume(cli, FAKE_COMPARTMENT, FAKE_INSTANCE,
                                 FAKE_BACKUP)
        self.assertTrue(ctx.exception.rolled_back)
        self.assertIn("删除失败", str(ctx.exception))

    def test_replace_failed_after_attach_keeps_new_volume(self):
        """换盘调用失败但新盘已挂上：不删盘，rolled_back=False。"""
        cli = _SwapCli(["RUNNING", "RUNNING", "RUNNING"],
                       fail_replace_after_attach=True)
        with patch("oci.boot_swap.time.sleep"):
            with self.assertRaises(BootSwapError) as ctx:
                swap_boot_volume(cli, FAKE_COMPARTMENT, FAKE_INSTANCE,
                                 FAKE_BACKUP)
        err = ctx.exception
        self.assertFalse(err.rolled_back)
        self.assertIn("新盘已经挂上", str(err))
        deletes = [c for c in cli.calls
                   if c[:3] == ["bv", "boot-volume", "delete"]]
        self.assertEqual(deletes, [])

    def test_attachment_poll_timeout_confirms_rollback(self):
        """挂载轮询超时、旧盘仍挂着：按回滚成功处理并删新盘。"""
        cli = _SwapCli(["RUNNING", "RUNNING", "RUNNING"],
                       never_attach_new=True)
        with patch("oci.boot_swap.time.sleep"), \
                patch("oci.boot_swap.SWAP_REPLACE_MAX_SECONDS", 0):
            with self.assertRaises(BootSwapError) as ctx:
                swap_boot_volume(cli, FAKE_COMPARTMENT, FAKE_INSTANCE,
                                 FAKE_BACKUP)
        err = ctx.exception
        self.assertTrue(err.rolled_back)
        self.assertIn("自动回滚", str(err))
        deletes = [c for c in cli.calls
                   if c[:3] == ["bv", "boot-volume", "delete"]]
        self.assertEqual(len(deletes), 1)

    def test_state_wait_timeout_after_attach(self):
        """新盘已挂上但实例迟迟未回 RUNNING：不删盘，报手动检查。"""
        cli = _SwapCli(["RUNNING", "RUNNING", "STOPPED"])
        with patch("oci.boot_swap.time.sleep"), \
                patch("oci.boot_swap.SWAP_STATE_MAX_SECONDS", 0):
            with self.assertRaises(BootSwapError) as ctx:
                swap_boot_volume(cli, FAKE_COMPARTMENT, FAKE_INSTANCE,
                                 FAKE_BACKUP)
        err = ctx.exception
        self.assertFalse(err.rolled_back)
        self.assertIn("未回到", str(err))
        deletes = [c for c in cli.calls
                   if c[:3] == ["bv", "boot-volume", "delete"]]
        self.assertEqual(deletes, [])


class TestSwapValidation(unittest.TestCase):
    """前置校验：任一项不通过都不动实例。"""

    def test_backup_not_available(self):
        cli = _SwapCli(["RUNNING"], backup_state="CREATING")
        with self.assertRaises(ValueError) as ctx:
            swap_boot_volume(cli, FAKE_COMPARTMENT, FAKE_INSTANCE,
                             FAKE_BACKUP)
        self.assertIn("AVAILABLE", str(ctx.exception))
        heads = [tuple(c[:3]) for c in cli.calls]
        self.assertNotIn(("bv", "boot-volume", "create"), heads)

    def test_instance_terminated(self):
        cli = _SwapCli(["TERMINATED", "TERMINATED"])
        with self.assertRaises(ValueError):
            swap_boot_volume(cli, FAKE_COMPARTMENT, FAKE_INSTANCE,
                             FAKE_BACKUP)

    def test_no_attachment(self):
        cli = _SwapCli(["RUNNING", "RUNNING"], attachment_present=False)
        with self.assertRaises(ValueError) as ctx:
            swap_boot_volume(cli, FAKE_COMPARTMENT, FAKE_INSTANCE,
                             FAKE_BACKUP)
        self.assertIn("启动盘", str(ctx.exception))

    def test_quota_insufficient(self):
        cli = _SwapCli(["RUNNING", "RUNNING"], used_gb=160)
        with self.assertRaises(ValueError) as ctx:
            swap_boot_volume(cli, FAKE_COMPARTMENT, FAKE_INSTANCE,
                             FAKE_BACKUP)
        self.assertIn("额度不足", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
