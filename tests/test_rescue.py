#!/usr/bin/env python3
"""救机功能核心逻辑测试（v2.01，mock OCIClient，不碰真实 API）。

运行：python3 -m unittest tests.test_rescue -v

覆盖：额度检查 / 备份源盘检查与删盘拦截 / 孤儿盘实测判定规则 /
前置检查各操作 / 安全恢复（额度拦截、旧盘是源盘时不自动删）/
无盘救机方案与重建 / Netboot 向导 / 控制台连接 RSA 校验。
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oci.console import (
    create_console_connection,
    delete_console_connection,
    list_console_connections,
    validate_rsa_public_key,
)
from oci.rescue import (
    check_delete_volume_safety,
    check_volume_quota,
    delete_boot_volume_guarded,
    is_rescue_orphan,
    list_rescue_orphans,
    netboot_guide,
    plan_diskless_rescue,
    preflight_check,
    rebuild_diskless_from_backup,
    safe_restore,
    total_rescue_orphan_gb,
)

FAKE_COMPARTMENT = "ocid1.tenancy.oc1..xxxxxxxx"
FAKE_INSTANCE = "ocid1.instance.oc1..dddd"
FAKE_BV_OLD = "ocid1.bootvolume.oc1..aaaa"
FAKE_BV_ORPHAN = "ocid1.bootvolume.oc1..orph"
FAKE_BV_INUSE = "ocid1.bootvolume.oc1..inuse"
FAKE_BACKUP = "ocid1.bootvolumebackup.oc1..cccc"
FAKE_AD = "FAKE:AP-SINGAPORE-1-AD-1"

RSA_PUB = ("ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQC7fakefakefake "
           "test@example")
ED_PUB = ("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeFakeFake "
          "test@example")


def _instance(state="RUNNING", name="test-instance"):
    return {"data": {
        "id": FAKE_INSTANCE,
        "display-name": name,
        "lifecycle-state": state,
        "availability-domain": FAKE_AD,
        "shape": "VM.Standard.A1.Flex",
    }}


def _attachment(volume_id, state="ATTACHED"):
    return {"id": "ocid1.bootvolumeattachment.oc1..eeee",
            "boot-volume-id": volume_id,
            "instance-id": FAKE_INSTANCE,
            "lifecycle-state": state}


def _backup(backup_id=FAKE_BACKUP, volume_id=FAKE_BV_OLD, state="AVAILABLE",
            size=47):
    return {"id": backup_id,
            "display-name": "test-backup",
            "boot-volume-id": volume_id,
            "size-in-gbs": size,
            "lifecycle-state": state,
            "source-type": "MANUAL",
            "type": "FULL",
            "time-created": "2026-11-24T10:00:00+00:00"}


def _volume(volume_id, size=47, ad=FAKE_AD):
    return {"id": volume_id,
            "display-name": "vol-" + volume_id[-4:],
            "size-in-gbs": size,
            "lifecycle-state": "AVAILABLE",
            "availability-domain": ad}


class _RescueCli:
    """按命令路由的假 CLI。"""

    def __init__(self, instance_state="RUNNING", attached_volume=FAKE_BV_OLD,
                 attachment_state="ATTACHED", used_gb=141,
                 backups=None, volumes=None, volume_details=None,
                 attachments=None):
        self.instance_state = instance_state
        self.attached_volume = attached_volume
        self.attachment_state = attachment_state
        self.used_gb = used_gb
        self.backups = backups if backups is not None else [_backup()]
        self.volumes = volumes if volumes is not None else [
            _volume(FAKE_BV_OLD, size=used_gb)]
        self.volume_details = volume_details or {}
        self.attachments = attachments
        self.calls = []
        self.deleted = []

    # --- SDK 读方法（返回解包后的数据） ---
    def sdk_get_instance(self, instance_id, region=None):
        return _instance(self.instance_state)["data"]

    def sdk_list_boot_volume_attachments(self, compartment_id,
                                         availability_domain, region=None):
        if self.attachments is not None:
            return list(self.attachments)
        if self.attached_volume is None:
            return []
        return [_attachment(self.attached_volume, self.attachment_state)]

    def sdk_list_boot_volumes(self, compartment_id, region=None):
        return list(self.volumes)

    def sdk_get_boot_volume(self, boot_volume_id, region=None):
        return self.volume_details.get(
            boot_volume_id, {"id": boot_volume_id, "source-details": {}})

    def sdk_list_boot_volume_backups(self, compartment_id,
                                     boot_volume_id=None, region=None):
        return list(self.backups)

    def sdk_get_boot_volume_backup(self, backup_id, region=None):
        return _backup()

    def run(self, args):
        self.calls.append(list(args))
        head = args[:3]
        if head == ["compute", "instance", "get"]:
            return _instance(self.instance_state)
        if head == ["compute", "boot-volume-attachment", "list"]:
            if self.attachments is not None:
                return {"data": list(self.attachments)}
            if self.attached_volume is None:
                return {"data": []}
            return {"data": [_attachment(self.attached_volume,
                                         self.attachment_state)]}
        if head == ["bv", "boot-volume", "list"]:
            return {"data": list(self.volumes)}
        if head == ["bv", "boot-volume", "get"]:
            volume_id = args[args.index("--boot-volume-id") + 1]
            return {"data": self.volume_details.get(
                volume_id, {"id": volume_id, "source-details": {}})}
        if head == ["bv", "boot-volume-backup", "list"]:
            return {"data": list(self.backups)}
        if head == ["bv", "boot-volume-backup", "get"]:
            return {"data": _backup()}
        if head == ["bv", "boot-volume", "delete"]:
            volume_id = args[args.index("--boot-volume-id") + 1]
            self.deleted.append(volume_id)
            return None
        raise AssertionError(f"未预期的 OCI 调用：{args}")


class TestVolumeQuota(unittest.TestCase):
    """额度检查：当前用量 + 新盘大小 ≤ 200GB。"""

    def test_enough_space(self):
        cli = _RescueCli(used_gb=141)
        result = check_volume_quota(cli, FAKE_COMPARTMENT, 47)
        self.assertTrue(result["ok"])
        self.assertEqual(result["used_gb"], 141)
        self.assertEqual(result["remaining_gb"], 59)

    def test_not_enough_space(self):
        cli = _RescueCli(used_gb=188)
        result = check_volume_quota(cli, FAKE_COMPARTMENT, 47)
        self.assertFalse(result["ok"])
        self.assertEqual(result["remaining_gb"], 12)


class TestDeleteVolumeSafety(unittest.TestCase):
    """删盘前备份源盘检查。"""

    def test_source_volume_detected(self):
        cli = _RescueCli(backups=[_backup()])
        result = check_delete_volume_safety(cli, FAKE_COMPARTMENT,
                                            FAKE_BV_OLD)
        self.assertTrue(result["is_backup_source"])
        self.assertEqual(len(result["backups"]), 1)
        self.assertIn("源盘", result["warning"])

    def test_non_source_volume_clean(self):
        cli = _RescueCli(backups=[_backup()])
        result = check_delete_volume_safety(cli, FAKE_COMPARTMENT,
                                            FAKE_BV_ORPHAN)
        self.assertFalse(result["is_backup_source"])
        self.assertEqual(result["backups"], [])

    def test_guarded_delete_blocks_backup_source(self):
        cli = _RescueCli(backups=[_backup()])
        with self.assertRaises(ValueError) as ctx:
            delete_boot_volume_guarded(cli, FAKE_COMPARTMENT, FAKE_BV_OLD,
                                       confirm=True)
        self.assertIn("拦截", str(ctx.exception))
        self.assertEqual(cli.deleted, [])

    def test_guarded_delete_allows_with_explicit_flag(self):
        cli = _RescueCli(backups=[_backup()])
        result = delete_boot_volume_guarded(
            cli, FAKE_COMPARTMENT, FAKE_BV_OLD, confirm=True,
            allow_backup_source=True)
        self.assertTrue(result["deleted"])
        self.assertTrue(result["was_backup_source"])
        self.assertEqual(cli.deleted, [FAKE_BV_OLD])

    def test_guarded_delete_requires_confirm(self):
        cli = _RescueCli(backups=[])
        with self.assertRaises(ValueError):
            delete_boot_volume_guarded(cli, FAKE_COMPARTMENT, FAKE_BV_OLD,
                                       confirm=False)
        self.assertEqual(cli.deleted, [])


class TestRescueOrphanRule(unittest.TestCase):
    """孤儿盘实测判定规则：无任何挂载记录 + 源自备份恢复。"""

    def test_orphan_needs_backup_source_type(self):
        detail = {"id": FAKE_BV_ORPHAN,
                  "source-details": {"type": "bootVolumeBackup"}}
        self.assertTrue(is_rescue_orphan(detail, set()))
        detail_image = {"id": FAKE_BV_ORPHAN,
                        "source-details": {"type": "image"}}
        self.assertFalse(is_rescue_orphan(detail_image, set()))
        detail_none = {"id": FAKE_BV_ORPHAN, "source-details": {}}
        self.assertFalse(is_rescue_orphan(detail_none, set()))

    def test_any_attachment_record_disqualifies(self):
        detail = {"id": FAKE_BV_ORPHAN,
                  "source-details": {"type": "bootVolumeBackup"}}
        self.assertFalse(is_rescue_orphan(detail, {FAKE_BV_ORPHAN}))

    def test_list_orphans_uses_real_rule(self):
        volumes = [_volume(FAKE_BV_INUSE), _volume(FAKE_BV_ORPHAN),
                   _volume(FAKE_BV_OLD)]
        details = {
            FAKE_BV_ORPHAN: {
                "id": FAKE_BV_ORPHAN,
                "source-details": {"type": "bootVolumeBackup",
                                   "bootVolumeBackupId": FAKE_BACKUP}},
            FAKE_BV_OLD: {
                "id": FAKE_BV_OLD,
                "source-details": {"type": "image"}},
        }
        cli = _RescueCli(volumes=volumes, volume_details=details,
                         attachments=[_attachment(FAKE_BV_INUSE)],
                         backups=[])
        found = list_rescue_orphans(cli, FAKE_COMPARTMENT)
        self.assertEqual([o["id"] for o in found], [FAKE_BV_ORPHAN])
        self.assertEqual(found[0]["source-backup-id"], FAKE_BACKUP)
        self.assertEqual(total_rescue_orphan_gb(found), 47)

    def test_available_in_use_volume_is_not_orphan(self):
        # An in-use volume also shows AVAILABLE, so judging by status alone misclassifies it; the attachment record decides. 在用盘也显示 AVAILABLE，只看状态会误判；挂载记录说了算。
        volumes = [_volume(FAKE_BV_INUSE)]
        cli = _RescueCli(volumes=volumes,
                         attachments=[_attachment(FAKE_BV_INUSE)],
                         backups=[])
        self.assertEqual(list_rescue_orphans(cli, FAKE_COMPARTMENT), [])


class TestPreflightCheck(unittest.TestCase):
    """救机前置检查：各操作的拦截与警告。"""

    def test_start_blocked_when_diskless(self):
        cli = _RescueCli(attached_volume=None, backups=[])
        result = preflight_check(cli, FAKE_COMPARTMENT, FAKE_INSTANCE,
                                 "start")
        self.assertFalse(result["can_do"])
        self.assertTrue(any("无盘救机" in b for b in result["blockers"]))
        self.assertFalse(result["facts"]["has_boot_volume"])

    def test_start_ok_when_stopped_with_disk(self):
        cli = _RescueCli(instance_state="STOPPED", backups=[])
        result = preflight_check(cli, FAKE_COMPARTMENT, FAKE_INSTANCE,
                                 "start")
        self.assertTrue(result["can_do"])

    def test_restore_blocked_by_quota(self):
        cli = _RescueCli(used_gb=188, backups=[])
        result = preflight_check(cli, FAKE_COMPARTMENT, FAKE_INSTANCE,
                                 "restore", needed_gb=47)
        self.assertFalse(result["can_do"])
        self.assertTrue(any("额度不足" in b for b in result["blockers"]))

    def test_restore_blocked_when_diskless(self):
        cli = _RescueCli(attached_volume=None, backups=[])
        result = preflight_check(cli, FAKE_COMPARTMENT, FAKE_INSTANCE,
                                 "restore", needed_gb=47)
        self.assertFalse(result["can_do"])
        self.assertTrue(any("无盘救机" in b for b in result["blockers"]))

    def test_delete_volume_blocked_when_backup_source(self):
        cli = _RescueCli(backups=[_backup()])
        result = preflight_check(cli, FAKE_COMPARTMENT, FAKE_INSTANCE,
                                 "delete_volume", volume_id=FAKE_BV_OLD)
        self.assertFalse(result["can_do"])
        self.assertTrue(result["facts"]["is_backup_source"])
        self.assertEqual(result["facts"]["backup_count"], 1)

    def test_delete_volume_blocked_when_attached(self):
        cli = _RescueCli(backups=[])
        result = preflight_check(cli, FAKE_COMPARTMENT, FAKE_INSTANCE,
                                 "delete_volume", volume_id=FAKE_BV_OLD)
        self.assertFalse(result["can_do"])
        self.assertTrue(any("挂在实例上" in b for b in result["blockers"]))

    def test_terminate_warns_when_backup_source(self):
        cli = _RescueCli(backups=[_backup()])
        result = preflight_check(cli, FAKE_COMPARTMENT, FAKE_INSTANCE,
                                 "terminate")
        self.assertTrue(result["can_do"])
        self.assertTrue(any("源盘" in w for w in result["warnings"]))

    def test_rebuild_blocked_when_disk_present(self):
        cli = _RescueCli(backups=[])
        result = preflight_check(cli, FAKE_COMPARTMENT, FAKE_INSTANCE,
                                 "rebuild_from_backup")
        self.assertFalse(result["can_do"])
        self.assertTrue(any("安全恢复" in b for b in result["blockers"]))

    def test_rebuild_warns_about_new_ocid_and_ip(self):
        cli = _RescueCli(attached_volume=None, backups=[])
        result = preflight_check(cli, FAKE_COMPARTMENT, FAKE_INSTANCE,
                                 "rebuild_from_backup")
        self.assertTrue(result["can_do"])
        self.assertTrue(any("新 OCID" in w for w in result["warnings"]))

    def test_unknown_action_rejected(self):
        cli = _RescueCli(backups=[])
        with self.assertRaises(ValueError):
            preflight_check(cli, FAKE_COMPARTMENT, FAKE_INSTANCE, "nope")


class TestSafeRestore(unittest.TestCase):
    """安全恢复：额度拦截、旧盘是源盘时不自动删。"""

    def test_quota_shortage_refuses_before_touching_instance(self):
        cli = _RescueCli(used_gb=188, backups=[])
        with patch("oci.rescue.swap_boot_volume") as mock_swap:
            with self.assertRaises(ValueError) as ctx:
                safe_restore(cli, FAKE_COMPARTMENT, FAKE_INSTANCE,
                             FAKE_BACKUP)
            self.assertIn("额度不足", str(ctx.exception))
            mock_swap.assert_not_called()

    def test_backup_source_old_volume_not_auto_deleted(self):
        cli = _RescueCli(backups=[_backup()])
        with patch("oci.rescue.swap_boot_volume",
                   return_value={"old_volume_deleted": False}) as mock_swap:
            result = safe_restore(cli, FAKE_COMPARTMENT, FAKE_INSTANCE,
                                  FAKE_BACKUP)
            _, kwargs = mock_swap.call_args
            self.assertFalse(kwargs["delete_old_volume"])
            self.assertTrue(result["warnings"])

    def test_normal_old_volume_auto_deleted(self):
        cli = _RescueCli(backups=[])
        with patch("oci.rescue.swap_boot_volume",
                   return_value={"old_volume_deleted": True}) as mock_swap:
            result = safe_restore(cli, FAKE_COMPARTMENT, FAKE_INSTANCE,
                                  FAKE_BACKUP)
            _, kwargs = mock_swap.call_args
            self.assertTrue(kwargs["delete_old_volume"])
            self.assertEqual(result["warnings"], [])

    def test_explicit_allow_deletes_backup_source_old_volume(self):
        cli = _RescueCli(backups=[_backup()])
        with patch("oci.rescue.swap_boot_volume",
                   return_value={"old_volume_deleted": True}) as mock_swap:
            safe_restore(cli, FAKE_COMPARTMENT, FAKE_INSTANCE, FAKE_BACKUP,
                         allow_delete_backup_source=True)
            _, kwargs = mock_swap.call_args
            self.assertTrue(kwargs["delete_old_volume"])


class TestDisklessRescue(unittest.TestCase):
    """无盘救机：方案文案与重建守卫。"""

    def test_plan_offers_rebuild_when_backups_exist(self):
        cli = _RescueCli(attached_volume=None, backups=[_backup()])
        plan = plan_diskless_rescue(cli, FAKE_COMPARTMENT, FAKE_INSTANCE)
        self.assertFalse(plan["has_boot_volume"])
        self.assertTrue(plan["can_rebuild"])
        self.assertIn("无法原地救回", plan["message"])
        self.assertIn("新的 OCID", plan["cost_note"])
        self.assertEqual(len(plan["backups"]), 1)

    def test_plan_no_backups_cannot_rebuild(self):
        cli = _RescueCli(attached_volume=None, backups=[])
        plan = plan_diskless_rescue(cli, FAKE_COMPARTMENT, FAKE_INSTANCE)
        self.assertFalse(plan["can_rebuild"])
        self.assertIn("无法凭空恢复", plan["message"])

    def test_plan_with_disk_points_to_safe_restore(self):
        cli = _RescueCli(backups=[_backup()])
        plan = plan_diskless_rescue(cli, FAKE_COMPARTMENT, FAKE_INSTANCE)
        self.assertTrue(plan["has_boot_volume"])
        self.assertFalse(plan["can_rebuild"])
        self.assertIn("安全恢复", plan["message"])

    def test_rebuild_refuses_when_disk_present(self):
        cli = _RescueCli(backups=[_backup()])
        with patch("oci.rescue.restore_boot_volume_backup") as mock_restore:
            with self.assertRaises(ValueError):
                rebuild_diskless_from_backup(
                    cli, FAKE_COMPARTMENT, FAKE_INSTANCE, FAKE_BACKUP,
                    availability_domain=FAKE_AD,
                    subnet_id="ocid1.subnet.oc1..xxxx",
                    display_name="rescued")
            mock_restore.assert_not_called()

    def test_rebuild_delegates_to_restore(self):
        cli = _RescueCli(attached_volume=None, backups=[_backup()])
        with patch("oci.rescue.restore_boot_volume_backup",
                   return_value={"instance_id": "ocid1.instance.oc1..new"}) \
                as mock_restore:
            result = rebuild_diskless_from_backup(
                cli, FAKE_COMPARTMENT, FAKE_INSTANCE, FAKE_BACKUP,
                availability_domain=FAKE_AD,
                subnet_id="ocid1.subnet.oc1..xxxx",
                display_name="rescued")
            self.assertEqual(result["instance_id"],
                             "ocid1.instance.oc1..new")
            mock_restore.assert_called_once()


class TestNetbootGuide(unittest.TestCase):
    """Netboot 向导：EFI 命令 / TFTP 节点 / 手动步骤标记。"""

    def test_guide_structure(self):
        guide = netboot_guide(FAKE_INSTANCE, region="ap-singapore-1")
        self.assertIn("FS0:", guide["efi_commands"])
        self.assertIn("ifconfig -s eth0 dhcp", guide["efi_commands"])
        self.assertEqual(guide["tftp_server"], "203.0.113.10")
        self.assertTrue(any(node["host"] == "203.0.113.10"
                            for node in guide["tftp_nodes"]))
        manual = [s for s in guide["console_steps"] if s["manual"]]
        self.assertTrue(any("ESC" in s["text"] for s in manual))
        self.assertTrue(any("Alpine" in s["text"]
                            for s in guide["menu_steps"]))
        self.assertTrue(guide["dd_steps"])

    def test_tftp_node_from_env(self):
        """MBOT_TFTP_NODE 设了自己的 TFTP 节点时，向导默认用它
        （真实 IP 只放部署环境变量，不写进公开代码）。"""
        import importlib

        import oci.rescue
        with patch.dict(os.environ,
                        {"MBOT_TFTP_NODE": "203.0.113.55"}):
            importlib.reload(oci.rescue)
            guide = oci.rescue.netboot_guide(
                FAKE_INSTANCE, region="ap-singapore-1")
            self.assertEqual(guide["tftp_server"], "203.0.113.55")
        importlib.reload(oci.rescue)


class TestConsoleConnections(unittest.TestCase):
    """控制台连接：RSA 校验与 CRUD 命令。"""

    def test_rsa_key_accepted(self):
        self.assertEqual(validate_rsa_public_key(RSA_PUB), RSA_PUB)

    def test_ed25519_key_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            validate_rsa_public_key(ED_PUB)
        self.assertIn("ssh-rsa", str(ctx.exception))
        self.assertIn("ssh-ed25519", str(ctx.exception))

    def test_empty_key_rejected(self):
        with self.assertRaises(ValueError):
            validate_rsa_public_key("  ")

    def _write_key(self, content):
        fd, path = tempfile.mkstemp(suffix=".pub")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        self.addCleanup(os.unlink, path)
        return path

    def test_create_uses_rsa_key_file(self):
        key_path = self._write_key(RSA_PUB)

        class _ConsoleCli:
            def __init__(self):
                self.calls = []

            def run(self, args):
                self.calls.append(list(args))
                if args[2] == "create":
                    return {"data": {
                        "id": "ocid1.instanceconsoleconnection.oc1..xxxx",
                        "connection-string": "ssh -o ProxyCommand=...",
                        "fingerprint": "aa:bb",
                        "lifecycle-state": "CREATING"}}
                return {"data": {"lifecycle-state": "ACTIVE"}}

            def sdk_get_instance_console_connection(self, connection_id):
                # 跟进查详情走 SDK 直调。
                return {"id": connection_id,
                        "lifecycle-state": "ACTIVE"}

        cli = _ConsoleCli()
        result = create_console_connection(cli, FAKE_INSTANCE,
                                           ssh_public_key_file=key_path)
        self.assertEqual(result["lifecycle-state"], "ACTIVE")
        self.assertIn("ssh -o ProxyCommand", result["connection-string"])
        create_call = cli.calls[0]
        self.assertEqual(
            create_call[create_call.index("--ssh-public-key-file") + 1],
            key_path)

    def test_create_rejects_ed25519_key_file(self):
        key_path = self._write_key(ED_PUB)

        class _NeverCalledCli:
            def run(self, args):
                raise AssertionError("ed25519 密钥不应发出任何 OCI 调用")

            def sdk_get_instance_console_connection(self, connection_id):
                raise AssertionError("ed25519 密钥不应发出任何 OCI 调用")

        with self.assertRaises(ValueError):
            create_console_connection(_NeverCalledCli(), FAKE_INSTANCE,
                                      ssh_public_key_file=key_path)

    def test_create_missing_key_file(self):
        class _NeverCalledCli:
            def run(self, args):
                raise AssertionError("缺密钥文件不应发出任何 OCI 调用")

            def sdk_get_instance_console_connection(self, connection_id):
                raise AssertionError("缺密钥文件不应发出任何 OCI 调用")

        with self.assertRaises(ValueError):
            create_console_connection(
                _NeverCalledCli(), FAKE_INSTANCE,
                ssh_public_key_file="/tmp/does-not-exist-rsa.pub")

    def test_list_and_delete(self):
        class _Cli:
            def __init__(self):
                self.calls = []

            def run(self, args):
                self.calls.append(list(args))
                return None

            def sdk_list_instance_console_connections(
                    self, compartment_id, instance_id=None):
                return [{
                    "id": "ocid1.instanceconsoleconnection.oc1..xxxx",
                    "instance-id": FAKE_INSTANCE,
                    "connection-string": "ssh ...",
                    "lifecycle-state": "ACTIVE",
                    "time-created": "2026-11-25T00:00:00+00:00"}]

        cli = _Cli()
        connections = list_console_connections(cli, FAKE_COMPARTMENT,
                                               FAKE_INSTANCE)
        self.assertEqual(len(connections), 1)
        self.assertEqual(connections[0]["lifecycle-state"], "ACTIVE")
        delete_console_connection(
            cli, "ocid1.instanceconsoleconnection.oc1..xxxx")
        self.assertEqual(cli.calls[-1][2], "delete")


if __name__ == "__main__":
    unittest.main()
