#!/usr/bin/env python3
"""实例列表分组 + IP 显示的单元测试（fmt / 键盘，不碰真实 API）。

运行：cd ~/workspace/rbot-clone && python3 -m unittest tests.test_tgbot_instance_list -v

覆盖：tgbot.services 的 short_shape / fmt_instance_list /
sort_instances_by_group / instance_group_title，以及
tgbot.keyboards 的 instance_list_keyboard。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tgbot.keyboards import instance_list_keyboard  # noqa: E402
from tgbot.services import (  # noqa: E402
    fmt_instance_detail,
    fmt_instance_list,
    instance_group_title,
    short_shape,
    sort_instances_by_group,
)

# 占位符（测试里不出现真实 OCID / IP；203.0.113.0/24 是文档专用网段）
FAKE_IP = "203.0.113.10"


def _inst(name, state, ip=None, shape="VM.Standard.A1.Flex"):
    """一条实例精简记录（list_instances 返回的形状）。"""
    inst = {
        "id": f"ocid1.instance.oc1..{name}",
        "display-name": name,
        "shape": shape,
        "lifecycle-state": state,
        "time-created": "2026-11-20T00:00:00Z",
    }
    if ip is not None:
        inst["public-ip"] = ip
    return inst


# 故意打乱顺序：已终止在前，运行中在中间，验证分组排序。
# run-2 是 E2，验证按钮上 A1/E2 区分。
MIXED_INSTANCES = [
    _inst("term-1", "TERMINATED"),
    _inst("run-1", "RUNNING", FAKE_IP),
    _inst("stop-1", "STOPPED"),
    _inst("run-2", "RUNNING", shape="VM.Standard.E2.1.Micro"),
    _inst("prov-1", "PROVISIONING"),
]


class TestSortInstancesByGroup(unittest.TestCase):
    """分组排序 helper：组顺序 + 原下标保留 + 组内原顺序。"""

    def test_group_order(self):
        # 已终止的 term-1 已过滤，不出现在排序结果里。
        ordered = sort_instances_by_group(MIXED_INSTANCES)
        states = [inst["lifecycle-state"] for _idx, inst in ordered]
        self.assertEqual(states, ["RUNNING", "RUNNING", "STOPPED",
                                  "PROVISIONING"])

    def test_original_indices_preserved(self):
        ordered = sort_instances_by_group(MIXED_INSTANCES)
        names = [(idx, inst["display-name"]) for idx, inst in ordered]
        self.assertEqual(names, [(1, "run-1"), (3, "run-2"), (2, "stop-1"),
                                 (4, "prov-1")])

    def test_terminated_filtered(self):
        # 全是已终止：过滤后为空。
        self.assertEqual(
            sort_instances_by_group([_inst("term-1", "TERMINATED"),
                                     _inst("term-2", "terminated")]), [])

    def test_empty(self):
        self.assertEqual(sort_instances_by_group([]), [])


class TestInstanceGroupTitle(unittest.TestCase):
    """分组标题：已知状态有对应标题，未知状态归入其他。"""

    def test_known_states(self):
        self.assertEqual(instance_group_title("RUNNING"), "🟢 运行中")
        self.assertEqual(instance_group_title("STOPPED"), "🟡 已停止")
        self.assertEqual(instance_group_title("TERMINATED"), "⚫ 已终止")

    def test_unknown_state_is_other(self):
        self.assertEqual(instance_group_title("PROVISIONING"), "❓ 其他")
        self.assertEqual(instance_group_title(""), "❓ 其他")
        self.assertEqual(instance_group_title(None), "❓ 其他")


class TestShortShape(unittest.TestCase):
    """规格简称：只留第一段，按钮上放得下。"""

    def test_a1_and_e2(self):
        self.assertEqual(short_shape("VM.Standard.A1.Flex"), "A1")
        self.assertEqual(short_shape("VM.Standard.E2.1.Micro"), "E2")
        self.assertEqual(short_shape("VM.Standard.E5.Flex"), "E5")

    def test_already_stripped(self):
        self.assertEqual(short_shape("A1.Flex"), "A1")
        self.assertEqual(short_shape("E2.1.Micro"), "E2")

    def test_empty(self):
        self.assertEqual(short_shape(""), "?")
        self.assertEqual(short_shape(None), "?")


class TestFmtInstanceList(unittest.TestCase):
    """列表文字部分：只剩标题 + 各组数量 + 操作提示。

    每台实例的信息在按钮上，文字不再逐台重复（否则文字
    列表和按钮列表会同时出现两份）。
    """

    def test_header_only_no_per_instance_lines(self):
        # 已终止的 term-1 已过滤：总数 4 台，不出现“已终止”组。
        text = fmt_instance_list(MIXED_INSTANCES, "DEFAULT")
        self.assertEqual(
            text,
            "[DEFAULT] 实例列表（4 台）：\n"
            "🟢 运行中 2 台 · 🟡 已停止 1 台 · ❓ 其他 1 台\n"
            "\n"
            "点下面按钮查看详情或操作。")
        # 实例名 / IP 不出现在文字里，只在按钮上。
        self.assertNotIn("run-1", text)
        self.assertNotIn(FAKE_IP, text)

    def test_empty_group_omitted(self):
        # 只有运行中和已终止：已终止的过滤后只剩运行中 1 台。
        instances = [_inst("run-1", "RUNNING"),
                     _inst("term-1", "TERMINATED")]
        text = fmt_instance_list(instances, "DEFAULT")
        self.assertEqual(
            text,
            "[DEFAULT] 实例列表（1 台）：\n"
            "🟢 运行中 1 台\n"
            "\n"
            "点下面按钮查看详情或操作。")

    def test_all_terminated_hidden(self):
        # 全是已终止：提示没有可显示的实例（已终止已隐藏）。
        text = fmt_instance_list([_inst("term-1", "TERMINATED")], "DEFAULT")
        self.assertEqual(text, "[DEFAULT] 没有可显示的实例"
                               "（已终止的实例已隐藏）。")

    def test_single_group(self):
        text = fmt_instance_list([_inst("run-1", "RUNNING")], "DEFAULT")
        self.assertEqual(text, "[DEFAULT] 实例列表（1 台）：\n"
                               "🟢 运行中 1 台\n\n"
                               "点下面按钮查看详情或操作。")

    def test_empty(self):
        self.assertEqual(fmt_instance_list([], "DEFAULT"),
                         "[DEFAULT] 该账号下没有实例。")


class TestInstanceListKeyboard(unittest.TestCase):
    """实例列表键盘：分组标题行 + 按钮顺序 + 原下标。"""

    def _rows(self, keyboard):
        """键盘行转 [(文字, callback_data)]，便于断言。"""
        return [[(btn.text, btn.callback_data) for btn in row]
                for row in keyboard.inline_keyboard]

    def test_group_headers_and_order(self):
        # 已终止组已隐藏：没有“已终止”标题行与 term-1 按钮。
        rows = self._rows(instance_list_keyboard(MIXED_INSTANCES))
        texts = [row[0][0] for row in rows]
        self.assertEqual(texts, [
            "🟢 运行中（2 台）：",
            f"run-1 · A1 · {FAKE_IP}",
            "run-2 · E2 · 运行中",
            "🟡 已停止（1 台）：",
            "stop-1 · A1 · 已停止",
            "❓ 其他（1 台）：",
            "prov-1 · A1 · PROVISIONING",
            "🔄 刷新",
        ])

    def test_button_text_shows_shape_and_ip(self):
        # 运行中有 IP：名称 · A1/E2 · IP；E2 与 A1 能区分。
        rows = self._rows(instance_list_keyboard(MIXED_INSTANCES))
        texts = [row[0][0] for row in rows]
        self.assertIn(f"run-1 · A1 · {FAKE_IP}", texts)
        self.assertIn("run-2 · E2 · 运行中", texts)
        # 已停止没有 IP，显示状态词；已终止的 term-1 已隐藏。
        self.assertIn("stop-1 · A1 · 已停止", texts)
        self.assertNotIn("term-1 · A1 · 已终止", texts)

    def test_header_buttons_are_noop(self):
        rows = self._rows(instance_list_keyboard(MIXED_INSTANCES))
        noop_rows = [row for row in rows
                     if row[0][1] == "i:noop"]
        self.assertEqual(len(noop_rows), 3)
        self.assertEqual([row[0][0] for row in noop_rows], [
            "🟢 运行中（2 台）：",
            "🟡 已停止（1 台）：",
            "❓ 其他（1 台）：",
        ])

    def test_instance_buttons_use_original_indices(self):
        # 关键：callback_data 必须是原列表下标（store_instances 存的
        # 是原始顺序列表，回调按原下标取回），不能用分组后的下标。
        # term-1 在原列表里是第 0 个，但已终止已隐藏，不出现。
        rows = self._rows(instance_list_keyboard(MIXED_INSTANCES))
        detail = {row[0][0]: row[0][1] for row in rows
                  if row[0][1].startswith("i:detail:")}
        self.assertEqual(detail, {
            f"run-1 · A1 · {FAKE_IP}": "i:detail:1",
            "run-2 · E2 · 运行中": "i:detail:3",
            "stop-1 · A1 · 已停止": "i:detail:2",
            "prov-1 · A1 · PROVISIONING": "i:detail:4",
        })

    def test_footer_buttons_kept(self):
        rows = self._rows(instance_list_keyboard(MIXED_INSTANCES))
        self.assertEqual(rows[-1], [("🔄 刷新", "i:list"),
                                    ("◀ Oracle 菜单", "o:menu")])

    def test_empty_instances_only_footer(self):
        rows = self._rows(instance_list_keyboard([]))
        self.assertEqual(rows, [[("🔄 刷新", "i:list"),
                                 ("◀ Oracle 菜单", "o:menu")]])

    def test_empty_group_omitted(self):
        instances = [_inst("run-1", "RUNNING"),
                     _inst("term-1", "TERMINATED")]
        rows = self._rows(instance_list_keyboard(instances))
        texts = [row[0][0] for row in rows]
        self.assertNotIn("🟡 已停止（0 台）：", texts)
        self.assertNotIn("❓ 其他（0 台）：", texts)
        self.assertIn("🟢 运行中（1 台）：", texts)
        # 已终止的 term-1 已隐藏，不出现标题行与按钮。
        self.assertNotIn("⚫ 已终止（1 台）：", texts)
        self.assertNotIn("term-1 · A1 · 已终止", texts)


class TestFmtInstanceDetail(unittest.TestCase):
    """v1.24.0：实例详情文案里的 CPU 型号行。"""

    def _detail(self):
        return {
            "id": "ocid1.instance.oc1..detailtest",
            "display-name": "e2-test",
            "shape": "VM.Standard.E2.1.Micro",
            "lifecycle-state": "RUNNING",
            "availability-domain": "AD-1",
            "time-created": "2026-11-14T14:19:44.000Z",
            "shape-config": {"ocpus": 1, "memory-in-gbs": 1},
        }

    def test_cpu_model_shown(self):
        text = fmt_instance_detail(self._detail(), FAKE_IP, "AMD EPYC 7J13")
        self.assertIn("CPU 型号：AMD EPYC 7J13", text)
        self.assertIn("规格：E2.1.Micro（1 核 / 1GB）", text)

    def test_cpu_model_unknown_when_none(self):
        text = fmt_instance_detail(self._detail(), FAKE_IP)
        self.assertIn("CPU 型号：未知", text)

    def test_detail_still_unpacks_data_wrapper(self):
        text = fmt_instance_detail({"data": self._detail()}, FAKE_IP,
                                   "Ampere Altra")
        self.assertIn("🖥️ e2-test", text)
        self.assertIn("CPU 型号：Ampere Altra", text)


if __name__ == "__main__":
    unittest.main()
