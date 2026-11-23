#!/usr/bin/env python3
"""阶段 5 工作包 A：volume.py 新增函数的单元测试。

覆盖：启动盘 VPU 修改、列表精简字段新增 vpus-per-gb。
全部 mock OCIClient.run，不碰真实 API。

运行：python3 -m unittest tests.test_phase5_volume -v
"""

import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oci.volume import (VALID_VPUS_PER_GB, list_boot_volumes,
                        update_boot_volume_vpus)

# 占位符（测试里不出现真实 OCID）
FAKE_COMPARTMENT = "ocid1.tenancy.oc1..xxxxxxxx"
FAKE_BV = "ocid1.bootvolume.oc1..xxxxxxxx"


def _mock_cli(return_value):
    cli = MagicMock()
    cli.run.return_value = return_value
    return cli


class TestUpdateBootVolumeVpus(unittest.TestCase):
    def test_command_args_include_force(self):
        cli = _mock_cli({"data": {"id": FAKE_BV, "vpus-per-gb": 20}})
        result = update_boot_volume_vpus(cli, FAKE_BV, 20)
        args = cli.run.call_args[0][0]
        self.assertEqual(args[:3], ["bv", "boot-volume", "update"])
        self.assertIn("--boot-volume-id", args)
        self.assertEqual(args[args.index("--boot-volume-id") + 1], FAKE_BV)
        self.assertEqual(args[args.index("--vpus-per-gb") + 1], "20")
        # --force 必加：不加会交互确认挂到超时
        self.assertIn("--force", args)
        self.assertEqual(result["vpus-per-gb"], 20)

    def test_all_valid_values(self):
        for vpus in VALID_VPUS_PER_GB:
            with self.subTest(vpus=vpus):
                cli = _mock_cli({"data": {}})
                update_boot_volume_vpus(cli, FAKE_BV, vpus)
                args = cli.run.call_args[0][0]
                self.assertEqual(args[args.index("--vpus-per-gb") + 1],
                                 str(vpus))

    def test_string_number_accepted(self):
        """字符串 "20" 也接受（表单提交的值是字符串）。"""
        cli = _mock_cli({"data": {}})
        update_boot_volume_vpus(cli, FAKE_BV, "20")
        args = cli.run.call_args[0][0]
        self.assertEqual(args[args.index("--vpus-per-gb") + 1], "20")

    def test_invalid_value_raises(self):
        for bad in (0, 5, 15, 25, 40, 100, -10):
            with self.subTest(bad=bad):
                cli = _mock_cli({})
                with self.assertRaises(ValueError):
                    update_boot_volume_vpus(cli, FAKE_BV, bad)
                cli.run.assert_not_called()

    def test_non_integer_raises(self):
        for bad in ("abc", None):
            with self.subTest(bad=bad):
                cli = _mock_cli({})
                with self.assertRaises(ValueError):
                    update_boot_volume_vpus(cli, FAKE_BV, bad)
                cli.run.assert_not_called()


class TestListBootVolumesVpus(unittest.TestCase):
    def test_keeps_vpus_per_gb_when_present(self):
        cli = _mock_cli({"data": [{
            "id": FAKE_BV,
            "display-name": "boot-volume-a1",
            "size-in-gbs": 47,
            "lifecycle-state": "AVAILABLE",
            "availability-domain": "FAKE-AD-1",
            "vpus-per-gb": 10,
        }]})
        result = list_boot_volumes(cli, FAKE_COMPARTMENT)
        self.assertEqual(result[0]["vpus-per-gb"], 10)

    def test_no_vpus_key_when_absent(self):
        """API 不返回 vpus-per-gb 时精简列表里也不带该键。"""
        cli = _mock_cli({"data": [{
            "id": FAKE_BV,
            "display-name": "boot-volume-a1",
            "size-in-gbs": 47,
            "lifecycle-state": "AVAILABLE",
            "availability-domain": "FAKE-AD-1",
        }]})
        result = list_boot_volumes(cli, FAKE_COMPARTMENT)
        self.assertNotIn("vpus-per-gb", result[0])


if __name__ == "__main__":
    unittest.main()
