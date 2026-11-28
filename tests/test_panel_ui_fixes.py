#!/usr/bin/env python3
"""M_Bot 面板 7 个 UI bug 修复的回归测试（2026-11-28 卸盘救机时浏览器实测发现）。

用 tests/dom_harness.js 跑真实的 i18n.js + app.js + 页面脚本（stub
DOM / fetch），覆盖 bug 1（停止按钮与状态码文案）、bug 2（详情立即
反馈）、bug 4（启动卷备份按钮静默失效）、bug 5（备份 ID 列与复制）、
bug 6（下拉选项直接点）、bug 7（实例/启动盘页并行加载与轻量 CPU 接口）。
后端接口的测试在 test_web_boot_backup.py、test_web_boot_backup_instance.py
（备份名字/类型透传、增量满额 400）与 test_web_api.py::TestCpuModelApi
（GET /api/instances/cpu-models）。

运行：python3 -m unittest tests.test_panel_ui_fixes -v

注意：全部占位符，不出现真实 OCID / IP / 租户名。
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE = shutil.which("node")
HARNESS = os.path.join(ROOT, "tests", "dom_harness.js")

# Placeholders (no real OCIDs / IPs in tests). 占位符（测试里不出现真实 OCID / IP）。
RUNNING_ID = "ocid1.instance.oc1..testrunning"
STOPPED_ID = "ocid1.instance.oc1..teststopped"
VOLUME_ID = "ocid1.bootvolume.oc1..testvol001"
BACKUP_ID = "ocid1.bootvolumebackup.oc1..testabcdef123456"

PROFILES = {
    "profiles": [
        {"name": "DEFAULT", "displayName": "新加坡",
         "region": "ap-singapore-1",
         "regions": [{"code": "ap-singapore-1", "label": "新加坡",
                      "isHome": True}]},
        {"name": "ACCOUNT2", "displayName": "圣何塞",
         "region": "us-sanjose-1",
         "regions": [{"code": "us-sanjose-1", "label": "圣何塞",
                      "isHome": True}]},
    ],
    "default": "DEFAULT",
}

INSTANCES = [
    {"id": RUNNING_ID, "display-name": "TEST-AMD-01",
     "shape": "VM.Standard.E2.1.Micro", "lifecycle-state": "RUNNING",
     "shape-config": {"ocpus": 1, "memory-in-gbs": 1},
     "time-created": "2026-11-28T00:00:00.000Z"},
    {"id": STOPPED_ID, "display-name": "TEST-ARM-01",
     "shape": "VM.Standard.A1.Flex", "lifecycle-state": "STOPPED",
     "shape-config": {"ocpus": 1, "memory-in-gbs": 6},
     "time-created": "2026-11-28T01:00:00.000Z"},
]

# The volume record carries ONLY volumeId (no dual-written id): that is
# exactly the shape that made findVolume miss and the backup button do
# nothing. 卷记录只有 volumeId、没有双写 id：正是让 findVolume 查不到、
# 备份按钮静默无反应的那种形状。
VOLUME = {"volumeId": VOLUME_ID, "displayName": "TEST-VOL-01",
          "sizeInGBs": 50, "vpusPerGB": 10,
          "lifecycleState": "AVAILABLE",
          "instanceId": RUNNING_ID, "instanceName": "TEST-AMD-01"}

BACKUP = {"backupId": BACKUP_ID, "displayName": "TEST-BACKUP-01",
          "bootVolumeId": VOLUME_ID, "sizeInGBs": 50,
          "lifecycleState": "AVAILABLE", "type": "FULL",
          "timeCreated": "2026-11-28T04:00:00.000Z"}

INSTANCE_DETAIL = {
    "instance": {"id": RUNNING_ID, "display-name": "TEST-AMD-01",
                 "shape": "VM.Standard.E2.1.Micro",
                 "lifecycle-state": "RUNNING",
                 "shape-config": {"ocpus": 1, "memory-in-gbs": 1},
                 "availability-domain": "AD-1",
                 "time-created": "2026-11-28T00:00:00.000Z"},
    "cpu_model": None,
    "public_ip": "203.0.113.10",
    "private_ip": "10.0.0.5",
    "ipv6_addresses": [],
    "boot_volume_size_gbs": 50,
    "boot_volume_vpus_per_gb": 10,
}

# Fake serial-console connection string (placeholder OCIDs only).
# 串行控制台连接命令（占位 OCID）。
CONSOLE_CONNECTION_STRING = (
    "ssh -o ProxyCommand='ssh -W %h:%p -p 443 "
    "ocid1.instanceconsoleconnection.oc1..testconn@"
    "instance-console.ap-singapore-1.oci.oraclecloud.com' "
    "ocid1.instance.oc1..testrunning")

INSTANCES_FETCH = {
    "GET /api/profiles": PROFILES,
    "GET /api/instances": {"instances": INSTANCES},
    "GET /api/volumes": {"volumes": []},
}

VOLUMES_FETCH = {
    "GET /api/profiles": PROFILES,
    "GET /api/volumes": {"volumes": [VOLUME]},
    "GET /api/volumes/orphans": {"orphans": []},
    "GET /api/boot-backups": {"backups": [BACKUP]},
    "GET /api/boot-backups/quota": {"paid": False, "count": 1,
                                    "limit": 5, "full": False},
    "POST /api/boot-backups": {
        "ok": True,
        "backup": {"backupId": "ocid1.bootvolumebackup.oc1..testnew001",
                   "displayName": "my-vol-backup",
                   "lifecycle-state": "CREATING"},
        "count": 2, "limit": 5},
}


def run_scenario(scenario):
    """Run one harness scenario, return the parsed JSON output."""
    with tempfile.NamedTemporaryFile("w", suffix=".json",
                                    delete=False) as f:
        json.dump(scenario, f)
        path = f.name
    try:
        out = subprocess.run([NODE, HARNESS, path],
                             capture_output=True, text=True,
                             timeout=60, check=True)
    finally:
        os.unlink(path)
    return json.loads(out.stdout)


def op_results(run, op):
    return [r for r in run["results"] if r["op"] == op]


def op_value(run, op, index=0):
    values = op_results(run, op)
    return values[index]["value"] if values else None


def fetch_calls(run, method=None, part=None):
    calls = run["fetchCalls"]
    if method is not None:
        calls = [c for c in calls if c["method"] == method]
    if part is not None:
        calls = [c for c in calls if part in c["url"]]
    return calls


class TestBug1StopButtonAndStateCodes(unittest.TestCase):
    """Bug 1：停止按钮文案“关机”→“停止/Stop”，状态文案中文翻译→OCI 原始码。"""

    def test_zh_stop_button_and_raw_state_codes(self):
        run = run_scenario({
            "page": "instances",
            "fetch": INSTANCES_FETCH,
            "steps": [{"op": "domContentLoaded"},
                      {"op": "wait", "ms": 100},
                      {"op": "read", "id": "instance-grid",
                       "prop": "innerHTML"},
                      {"op": "errors"}],
        })
        grid = op_value(run, "read")
        self.assertIn("停止", grid)
        self.assertIn("STOPPED", grid)
        self.assertIn("RUNNING", grid)
        self.assertNotIn("关机", grid)
        self.assertNotIn("停止中", grid)
        self.assertNotIn("已停止", grid)
        self.assertNotIn("运行中", grid)
        self.assertEqual(op_value(run, "errors"), [])

    def test_en_stop_button_and_raw_state_codes(self):
        run = run_scenario({
            "page": "instances",
            "seedStorage": {"mbot-lang": "en"},
            "fetch": INSTANCES_FETCH,
            "steps": [{"op": "domContentLoaded"},
                      {"op": "wait", "ms": 100},
                      {"op": "read", "id": "instance-grid",
                       "prop": "innerHTML"},
                      {"op": "errors"}],
        })
        grid = op_value(run, "read")
        self.assertIn("Stop", grid)
        self.assertIn("STOPPED", grid)
        self.assertIn("RUNNING", grid)
        self.assertNotIn("关机", grid)
        self.assertNotIn("停止", grid)
        self.assertNotIn("停止中", grid)
        self.assertNotIn("已停止", grid)
        self.assertNotIn("运行中", grid)
        self.assertEqual(op_value(run, "errors"), [])


class TestBug2DetailOpensImmediately(unittest.TestCase):
    """Bug 2：点实例名立即开弹窗填“加载中…”，不等详情接口回来。"""

    def test_detail_modal_opens_before_fetch_returns(self):
        fetch = dict(INSTANCES_FETCH)
        fetch["GET /api/instances/" + RUNNING_ID] = dict(
            INSTANCE_DETAIL, __delay=400)
        run = run_scenario({
            "page": "instances",
            "fetch": fetch,
            "steps": [{"op": "domContentLoaded"},
                      {"op": "wait", "ms": 100},
                      {"op": "tbodyClick", "tbody": "instance-grid",
                       "act": "detail", "rowId": RUNNING_ID,
                       "rowAttr": "data-instance-id"},
                      {"op": "read", "id": "detail-modal",
                       "prop": "hidden"},
                      {"op": "read", "id": "detail-name",
                       "prop": "textContent"},
                      {"op": "read", "id": "detail-state",
                       "prop": "textContent"},
                      {"op": "wait", "ms": 600},
                      {"op": "read", "id": "detail-state",
                       "prop": "innerHTML"},
                      {"op": "errors"}],
        })
        reads = op_results(run, "read")
        # Immediately after the click (detail fetch still in flight):
        # the modal is already open with the name and a loading state.
        # 点完立即（详情接口还在飞）：弹窗已打开，有名字、状态是加载中。
        self.assertFalse(reads[0]["value"])
        self.assertEqual(reads[1]["value"], "TEST-AMD-01")
        self.assertEqual(reads[2]["value"], "加载中…")
        # Once the detail fetch returns, the state badge shows the raw code.
        # 接口回来后状态徽标显示原始码。
        self.assertIn("RUNNING", reads[3]["value"])
        self.assertEqual(op_value(run, "errors"), [])


class TestDetailRootPassword(unittest.TestCase):
    """v2.24.0：实例详情“初始开机密码”行——默认隐藏、点击复制；没存密码时显示“未保存”。"""

    def _fetch(self, password_response):
        fetch = dict(INSTANCES_FETCH)
        fetch["GET /api/instances/" + RUNNING_ID] = INSTANCE_DETAIL
        fetch["GET /api/instances/" + RUNNING_ID + "/password"] = \
            password_response
        return fetch

    def _open_detail_steps(self):
        return [{"op": "domContentLoaded"},
                {"op": "wait", "ms": 100},
                {"op": "tbodyClick", "tbody": "instance-grid",
                 "act": "detail", "rowId": RUNNING_ID,
                 "rowAttr": "data-instance-id"},
                {"op": "wait", "ms": 150},
                {"op": "read", "id": "detail-root-password",
                 "prop": "textContent"},
                {"op": "fireClick", "id": "detail-root-password"},
                {"op": "wait", "ms": 50},
                {"op": "copied"},
                {"op": "fetchCalls"},
                {"op": "errors"}]

    def test_detail_shows_masked_password_and_click_copies(self):
        run = run_scenario({
            "page": "instances",
            "fetch": self._fetch({"ok": True,
                                  "root_password": "pw-test-123"}),
            "steps": self._open_detail_steps(),
        })
        reads = op_results(run, "read")
        # Masked by default; the real password is never rendered in the DOM. 默认只显示掩码，真密码不进 DOM。
        self.assertEqual(reads[0]["value"], "••••••••")
        self.assertEqual(op_value(run, "copied"), "pw-test-123")
        # The password endpoint is fetched only for this one instance.
        # 密码接口只为这一台实例拉一次。
        self.assertTrue(fetch_calls(run, "GET", "/password"))
        self.assertEqual(op_value(run, "errors"), [])

    def test_detail_shows_not_saved_when_no_password(self):
        run = run_scenario({
            "page": "instances",
            "fetch": self._fetch({"__status": 404,
                                  "__body": {"ok": False,
                                             "error": "该实例未保存初始开机密码"}}),
            "steps": self._open_detail_steps(),
        })
        reads = op_results(run, "read")
        self.assertEqual(reads[0]["value"], "未保存")
        # Clicking with no stored password copies nothing.
        # 没存密码时点了也不复制。
        self.assertIsNone(op_value(run, "copied"))
        self.assertEqual(op_value(run, "errors"), [])


class TestCardRootPassword(unittest.TestCase):
    """卡片“初始开机密码”行（公网 IP 下一行）：默认隐藏、带复制按钮；没存密码时显示“未保存”；实例名后有“（查看实例详情）”。"""

    def _fetch(self, password_response):
        fetch = dict(INSTANCES_FETCH)
        fetch["GET /api/instances/" + RUNNING_ID + "/password"] = \
            password_response
        return fetch

    def test_card_shows_masked_password_and_copy_button(self):
        run = run_scenario({
            "page": "instances",
            "fetch": self._fetch({"ok": True,
                                  "root_password": "pw-test-123"}),
            "steps": [{"op": "domContentLoaded"},
                      {"op": "wait", "ms": 600},
                      {"op": "read", "id": "instance-grid",
                       "prop": "innerHTML"},
                      {"op": "tbodyClick", "tbody": "instance-grid",
                       "act": "copy-password", "rowId": RUNNING_ID,
                       "rowAttr": "data-instance-id"},
                      {"op": "wait", "ms": 50},
                      {"op": "copied"},
                      {"op": "fetchCalls"},
                      {"op": "errors"}],
        })
        grid = op_value(run, "read")
        # Password row is on the card, below the public IP row, masked.
        # 密码行在卡片上（公网 IP 下一行），默认只显示掩码。
        self.assertIn("初始开机密码", grid)
        self.assertIn("••••••••", grid)
        # The real password is never rendered in the DOM. 真密码不进 DOM。
        self.assertNotIn("pw-test-123", grid)
        # Instance name carries an explicit "(view instance details)" link.
        # 实例名后有“（查看实例详情）”。
        self.assertIn("(查看实例详情)", grid)
        # Copy button copies the stored password. 复制按钮复制存的那份密码。
        self.assertEqual(op_value(run, "copied"), "pw-test-123")
        # The password endpoint is fetched per visible instance.
        # 密码接口为每台可见实例拉一次。
        self.assertTrue(fetch_calls(run, "GET", "/password"))
        self.assertEqual(op_value(run, "errors"), [])

    def test_card_shows_not_saved_when_no_password(self):
        run = run_scenario({
            "page": "instances",
            "fetch": self._fetch({"__status": 404,
                                  "__body": {"ok": False,
                                             "error": "该实例未保存初始开机密码"}}),
            "steps": [{"op": "domContentLoaded"},
                      {"op": "wait", "ms": 600},
                      {"op": "read", "id": "instance-grid",
                       "prop": "innerHTML"},
                      {"op": "errors"}],
        })
        grid = op_value(run, "read")
        self.assertIn("初始开机密码", grid)
        self.assertIn("未保存", grid)
        self.assertEqual(op_value(run, "errors"), [])


class TestBug4BackupButton(unittest.TestCase):
    """Bug 4：启动卷“备份”按钮点了没反应（findVolume 只认 id，不认 volumeId）。"""

    def test_backup_opens_dialog_and_creates_with_name_and_type(self):
        run = run_scenario({
            "page": "volumes",
            "fetch": VOLUMES_FETCH,
            "steps": [{"op": "domContentLoaded"},
                      {"op": "wait", "ms": 100},
                      {"op": "tbodyClick", "tbody": "volume-tbody",
                       "act": "backup", "rowId": VOLUME_ID},
                      {"op": "modalState"},
                      {"op": "modalInput", "selector": ".backup-name-input",
                       "value": "my-vol-backup"},
                      {"op": "modalInput", "selector": ".backup-type-select",
                       "value": "INCREMENTAL"},
                      {"op": "modalClick", "act": "ok"},
                      {"op": "wait", "ms": 100},
                      {"op": "fetchCalls"},
                      {"op": "toasts"},
                      {"op": "errors"}],
        })
        modal = op_results(run, "modalState")[0]
        self.assertEqual(modal["open"], 1)
        # The dialog pre-fills the default name "<volume>-backup-YYYYMMDD-HHMM".
        # 确认框预填默认名“<卷名>-backup-年月日-时分”。
        self.assertRegex(modal["nameValue"] or "",
                         r"^TEST-VOL-01-backup-\d{8}-\d{4}$")
        self.assertTrue(modal["hasFull"])
        self.assertTrue(modal["hasIncr"])
        posts = fetch_calls(run, method="POST", part="/api/boot-backups")
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["body"]["boot_volume_id"], VOLUME_ID)
        self.assertEqual(posts[0]["body"]["display_name"], "my-vol-backup")
        self.assertEqual(posts[0]["body"]["type"], "INCREMENTAL")
        toasts = op_value(run, "toasts")
        self.assertTrue(any(t["cls"].endswith("success") for t in toasts))
        self.assertEqual(op_value(run, "errors"), [])

    def test_missing_volume_toasts_instead_of_silence(self):
        fetch = dict(VOLUMES_FETCH)
        fetch["GET /api/volumes"] = {"volumes": []}
        run = run_scenario({
            "page": "volumes",
            "fetch": fetch,
            "steps": [{"op": "domContentLoaded"},
                      {"op": "wait", "ms": 100},
                      {"op": "tbodyClick", "tbody": "volume-tbody",
                       "act": "backup", "rowId": VOLUME_ID},
                      {"op": "toasts"},
                      {"op": "fetchCalls"},
                      {"op": "errors"}],
        })
        toasts = op_value(run, "toasts")
        self.assertTrue(any("找不到启动卷" in t["text"] for t in toasts))
        self.assertEqual(fetch_calls(run, method="POST",
                                     part="/api/boot-backups"), [])
        self.assertEqual(op_value(run, "errors"), [])


class TestBug5BackupIdColumn(unittest.TestCase):
    """Bug 5：备份列表加“备份 ID”列（截断显示），点备份名/ID 复制完整 ID。"""

    def test_backup_row_shows_truncated_id_and_copies_full_id(self):
        run = run_scenario({
            "page": "volumes",
            "fetch": VOLUMES_FETCH,
            "steps": [{"op": "domContentLoaded"},
                      {"op": "wait", "ms": 100},
                      {"op": "read", "id": "backup-tbody",
                       "prop": "innerHTML"},
                      {"op": "copyIdClick", "tbody": "backup-tbody",
                       "rowId": BACKUP_ID},
                      {"op": "wait", "ms": 50},
                      {"op": "copied"},
                      {"op": "errors"}],
        })
        html = op_value(run, "read")
        self.assertIn("data-copy-id", html)
        self.assertIn(BACKUP_ID, html)
        # Truncated display: only the last 12 chars, with an ellipsis.
        # 截断显示：只留后 12 位，带省略号。
        self.assertIn("…abcdef123456", html)
        self.assertEqual(op_value(run, "copied"), BACKUP_ID)
        self.assertEqual(op_value(run, "errors"), [])


class TestBug6SelectOptionClick(unittest.TestCase):
    """Bug 6：直接点账号下拉的选项就能切换（不再只认 select 事件）。"""

    def test_clicking_option_switches_account(self):
        run = run_scenario({
            "page": "volumes",
            "fetch": VOLUMES_FETCH,
            "steps": [{"op": "domContentLoaded"},
                      {"op": "wait", "ms": 100},
                      {"op": "selectClickOption", "id": "profile-select",
                       "value": "ACCOUNT2"},
                      {"op": "wait", "ms": 100},
                      {"op": "fetchCalls"},
                      {"op": "errors"}],
        })
        click = op_results(run, "selectClickOption")[0]
        self.assertEqual(click["value"], "ACCOUNT2")
        # The switch re-loads the lists for the new account.
        # 切换后按新账号重新拉列表。
        self.assertTrue(any("profile=ACCOUNT2" in c["url"]
                            for c in run["fetchCalls"]))
        self.assertEqual(op_value(run, "errors"), [])


class TestBug7LoadingSpeed(unittest.TestCase):
    """Bug 7：启动盘页三路并行拉；实例页 CPU 补拉走轻量接口，不重拉列表。"""

    def test_volumes_loads_lists_in_parallel(self):
        fetch = dict(VOLUMES_FETCH)
        fetch["GET /api/volumes"] = dict({"volumes": [VOLUME]}, __delay=300)
        run = run_scenario({
            "page": "volumes",
            "fetch": fetch,
            "steps": [{"op": "domContentLoaded"},
                      {"op": "wait", "ms": 100},
                      {"op": "fetchCalls"},
                      {"op": "wait", "ms": 400},
                      {"op": "errors"}],
        })
        urls = [c["url"] for c in run["fetchCalls"]]
        # At ~100ms the volume list is still in flight (300ms delay), yet
        # the backup list must already have been requested: the three lists
        # go out in parallel instead of one after another.
        # 约 100ms 时卷列表还在飞（延迟 300ms），备份列表必须已经发出去：
        # 三路并行，而不是一路由完再发下一路。
        self.assertTrue(any("/api/volumes" in u for u in urls))
        self.assertTrue(any("/api/boot-backups" in u for u in urls))
        self.assertEqual(op_value(run, "errors"), [])

    def test_instances_cpu_refresh_uses_light_endpoint(self):
        fetch = dict(INSTANCES_FETCH)
        fetch["GET /api/instances/cpu-models"] = {
            "cpu_models": {RUNNING_ID: "AMD EPYC 7J13"}}
        run = run_scenario({
            "page": "instances",
            "fetch": fetch,
            "steps": [{"op": "domContentLoaded"},
                      {"op": "wait", "ms": 100},
                      {"op": "fetchCalls"},
                      {"op": "wait", "ms": 5200},
                      {"op": "fetchCalls"},
                      {"op": "errors"}],
        })
        first = op_results(run, "fetchCalls")[0]["value"]
        list_calls = [c for c in first
                      if c["method"] == "GET" and "/api/instances" in c["url"]
                      and "cpu-models" not in c["url"]
                      and "/password" not in c["url"]]
        self.assertEqual(len(list_calls), 1)
        self.assertFalse(any("refresh=1" in c["url"] for c in first))
        self.assertFalse(any("cpu-models" in c["url"] for c in first))
        second = op_results(run, "fetchCalls")[1]["value"]
        cpu_calls = [c for c in second
                     if c["method"] == "GET" and "cpu-models" in c["url"]]
        self.assertEqual(len(cpu_calls), 1)
        # Still exactly one full list fetch: the refetch did not re-pull the list.
        # 全量列表仍只拉了一次：补拉没有重拉列表。
        list_calls_after = [c for c in second
                            if c["method"] == "GET"
                            and "/api/instances" in c["url"]
                            and "cpu-models" not in c["url"]
                            and "/password" not in c["url"]]
        self.assertEqual(len(list_calls_after), 1)
        self.assertEqual(op_value(run, "errors"), [])


class TestNormKeepsCpuModel(unittest.TestCase):
    """norm() 必须保留 cpuModel：API 返回真实型号时卡片显示它，而不是 E2 回落的 7J13。"""

    def test_card_shows_real_model_and_fallback_once(self):
        fetch = {
            "GET /api/profiles": PROFILES,
            "GET /api/instances": {"instances": [
                {"id": RUNNING_ID, "name": "TEST-AMD-7551",
                 "shape": "VM.Standard.E2.1.Micro", "state": "RUNNING",
                 "ocpus": 1, "memoryInGBs": 1, "publicIp": "203.0.113.10",
                 "timeCreated": "2026-11-28T00:00:00.000Z",
                 "cpuModel": "AMD EPYC 7551"},
                {"id": STOPPED_ID, "name": "TEST-AMD-UNKNOWN",
                 "shape": "VM.Standard.E2.1.Micro", "state": "STOPPED",
                 "ocpus": 1, "memoryInGBs": 1, "publicIp": None,
                 "timeCreated": "2026-11-28T01:00:00.000Z"},
            ]},
            "GET /api/volumes": {"volumes": []},
        }
        run = run_scenario({
            "page": "instances",
            "fetch": fetch,
            "steps": [{"op": "domContentLoaded"},
                      {"op": "wait", "ms": 100},
                      {"op": "read", "id": "instance-grid",
                       "prop": "innerHTML"},
                      {"op": "errors"}],
        })
        grid = op_value(run, "read")
        self.assertIn("AMD EPYC 7551", grid)
        # Only the instance without cpuModel falls back to 7J13. 没型号的那台才回落 7J13。
        self.assertEqual(grid.count("AMD EPYC 7J13"), 1)
        self.assertEqual(op_value(run, "errors"), [])


class TestTerminalCloseButton(unittest.TestCase):
    """终端页“关闭此SSH窗口”：关当前会话并离开终端页；没有会话时也要离开。"""

    def _run(self, **extra):
        scenario = {
            "page": "terminal",
            "fetch": {},
            "steps": [{"op": "domContentLoaded"},
                      {"op": "wait", "ms": 50},
                      {"op": "fireClick", "id": "btn-close-tab"},
                      {"op": "wait", "ms": 100},
                      {"op": "nav"},
                      {"op": "errors"}],
        }
        scenario.update(extra)
        return run_scenario(scenario)

    def test_no_session_direct_open_goes_to_instances(self):
        # Directly opened (no history): fall back to the instance list.
        # 直接打开（没有上一页）：回实例列表。
        run = self._run(historyLength=1)
        nav = op_results(run, "nav")[0]
        self.assertEqual(nav["href"], "/instances")
        self.assertFalse(nav["historyBack"])
        self.assertEqual(op_value(run, "errors"), [])

    def test_no_session_with_history_goes_back(self):
        # Came from the instance list: history.back(). 从实例列表来的：回上一页。
        run = self._run(historyLength=2)
        nav = op_results(run, "nav")[0]
        self.assertEqual(nav["href"], "")
        self.assertTrue(nav["historyBack"])
        self.assertEqual(op_value(run, "errors"), [])

    def test_with_session_closes_it_then_leaves(self):
        # One-click SSH opened a session; closing ends it server-side
        # (DELETE /api/ssh/sessions/<id>) and then leaves the page.
        # 一键 SSH 已开会话；关闭时先调接口关掉服务端会话，再离开页面。
        run = run_scenario({
            "page": "terminal",
            "historyLength": 1,
            "locationSearch": "?instance=" + RUNNING_ID +
                              "&profile=DEFAULT&region=ap-singapore-1"
                              "&host=203.0.113.10",
            "fetch": {"POST /api/ssh/connect-instance":
                      {"session_id": "sid-9", "username": "root",
                       "host": "203.0.113.10"}},
            "steps": [{"op": "domContentLoaded"},
                      {"op": "wait", "ms": 100},
                      {"op": "fireClick", "id": "btn-close-tab"},
                      {"op": "wait", "ms": 100},
                      {"op": "nav"},
                      {"op": "fetchCalls"},
                      {"op": "errors"}],
        })
        nav = op_results(run, "nav")[0]
        self.assertEqual(nav["href"], "/instances")
        self.assertFalse(nav["historyBack"])
        deletes = [c for c in op_value(run, "fetchCalls")
                   if c["method"] == "DELETE"
                   and "/api/ssh/sessions/sid-9" in c["url"]]
        self.assertEqual(len(deletes), 1)
        self.assertEqual(op_value(run, "errors"), [])


class TestConsoleDirectConnectDoubleClick(unittest.TestCase):
    """串行控制台“直接连接”防重复点击（Bug 1 前端）：点两下只发一个
    POST，第二下因按钮已禁用被忽略；否则两个 POST 会在同一个 OCI 控制
    台连接上开两个 SSH 客户端，OCI 会踢掉一个（"Another ssh client
    has connected."）。"""

    def test_double_click_posts_once(self):
        fetch = dict(INSTANCES_FETCH)
        fetch["GET /api/instances/" + RUNNING_ID + "/console"] = {
            "ok": True, "instance_id": RUNNING_ID,
            "console": {"connection-string": CONSOLE_CONNECTION_STRING,
                        "lifecycle-state": "ACTIVE",
                        "fingerprint": "SHA256:test"},
        }
        fetch["POST /api/ssh/connect-console"] = {
            "session_id": "sid-9",
            "host": "instance-console.ap-singapore-1.oci.oraclecloud.com",
            "username": "ocid1.instanceconsoleconnection.oc1..testconn",
            "port": 443, "instance_id": RUNNING_ID,
            "__delay": 400,
        }
        run = run_scenario({
            "page": "instances",
            "fetch": fetch,
            "steps": [{"op": "domContentLoaded"},
                      {"op": "wait", "ms": 100},
                      {"op": "tbodyClick", "tbody": "instance-grid",
                       "act": "console", "rowId": RUNNING_ID,
                       "rowAttr": "data-instance-id"},
                      {"op": "wait", "ms": 150},
                      {"op": "fireClick", "id": "btn-connect-console"},
                      {"op": "fireClick", "id": "btn-connect-console"},
                      {"op": "read", "id": "btn-connect-console",
                       "prop": "disabled"},
                      {"op": "fetchCalls"},
                      {"op": "wait", "ms": 600},
                      {"op": "fetchCalls"},
                      {"op": "nav"},
                      {"op": "errors"}],
        })
        reads = op_results(run, "read")
        # The button is disabled as soon as the first click fires, while
        # the POST is still in flight. 第一下点完、POST 还在飞时按钮已禁用。
        self.assertTrue(reads[0]["value"])

        def _posts(index):
            return [c for c in op_value(run, "fetchCalls", index)
                    if c["method"] == "POST"
                    and "/api/ssh/connect-console" in c["url"]]
        # Exactly one POST while the first is still in flight, and still
        # one after the delayed response returns. POST 还在飞时只有一个，
        # 延迟响应回来后仍只有一个。
        self.assertEqual(len(_posts(0)), 1)
        self.assertEqual(len(_posts(1)), 1)
        nav = op_results(run, "nav")[0]
        self.assertIn("/terminal?session=sid-9", nav["href"])
        self.assertEqual(op_value(run, "errors"), [])


class TestConsoleLoginPromptPoke(unittest.TestCase):
    """串行控制台登录提示（Bug 2）：经 ?session= 打开控制台标签时，首次
    WS 连上后自动发一个回车唤出 login 提示，不用用户先按回车。"""

    def test_console_tab_pokes_enter_once_on_connect(self):
        run = run_scenario({
            "page": "terminal",
            "locationSearch": "?session=sid-9",
            "fetch": {},
            "steps": [{"op": "domContentLoaded"},
                      {"op": "wait", "ms": 150},
                      {"op": "wsSends"},
                      {"op": "errors"}],
        })
        sends = [json.loads(s) for s in op_value(run, "wsSends")]
        # A resize first, then exactly one bare "\r" to wake getty.
        # 先发 resize，再发一个回车唤醒 getty，且只发一次。
        self.assertEqual(sends[0]["type"], "resize")
        pokes = [s for s in sends
                 if s.get("type") == "input" and s.get("data") == "\r"]
        self.assertEqual(len(pokes), 1)
        self.assertEqual(op_value(run, "errors"), [])


class TestConsoleAutoLogin(unittest.TestCase):
    """串行控制台自动登录：经 ?session= + ?instance= 打开时，用保存的
    开机 root 密码自动输 login / Password 提示；密码不对时只问一次
    修改后的密码。"""

    def _run(self, fetch, steps):
        return run_scenario({
            "page": "terminal",
            "locationSearch": "?session=sid-9&console_instance=" + RUNNING_ID,
            "fetch": fetch,
            "steps": steps,
        })

    def test_auto_login_types_root_and_password(self):
        fetch = {
            "GET /api/instances/" + RUNNING_ID + "/password": {
                "ok": True, "root_password": "pw-1234",
            },
        }
        run = self._run(fetch, [
            {"op": "domContentLoaded"},
            {"op": "wait", "ms": 150},
            {"op": "wsReceive", "data": "Debian GNU/Linux 13\r\nlogin: "},
            {"op": "wait", "ms": 50},
            {"op": "wsSends"},
            {"op": "wsReceive", "data": "Password: "},
            {"op": "wait", "ms": 50},
            {"op": "wsSends"},
            {"op": "errors"},
        ])
        sends = [json.loads(s) for s in op_value(run, "wsSends", 0)]
        # After the login prompt: root + Enter. 看到 login: 后发 root 回车。
        self.assertIn({"type": "input", "data": "root\r"}, sends)
        sends2 = [json.loads(s) for s in op_value(run, "wsSends", 1)]
        # After the password prompt: the saved password + Enter.
        # 看到 Password: 后发保存的密码 + 回车。
        self.assertIn({"type": "input", "data": "pw-1234\r"}, sends2)
        self.assertEqual(op_value(run, "errors"), [])

    def test_no_saved_password_types_nothing(self):
        fetch = {
            "GET /api/instances/" + RUNNING_ID + "/password": {
                "__status": 404,
                "__body": {"ok": False, "error": "该实例未保存初始开机密码"},
            },
        }
        run = self._run(fetch, [
            {"op": "domContentLoaded"},
            {"op": "wait", "ms": 150},
            {"op": "wsReceive", "data": "Debian GNU/Linux 13\r\nlogin: "},
            {"op": "wait", "ms": 50},
            {"op": "wsSends"},
            {"op": "errors"},
        ])
        sends = [json.loads(s) for s in op_value(run, "wsSends")]
        # No saved password: only the resize + the getty poke, no root.
        # 没存密码：只有 resize 与唤醒回车，不自动输 root。
        typed = [s for s in sends
                 if s.get("type") == "input" and s.get("data") == "root\r"]
        self.assertEqual(typed, [])
        self.assertEqual(op_value(run, "errors"), [])


if __name__ == "__main__":
    unittest.main()
