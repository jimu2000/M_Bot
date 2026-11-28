#!/usr/bin/env python3
"""演示模式（MBOT_DEMO_MODE=1）的定向测试。

覆盖：
- demo/demo 登录成功，错密 401；非 demo 模式下 demo/demo 失败；
  admin 在 demo 模式下仍可登录；/api/auth/me 带 demoMode。
- 3 个演示租户、8 台模拟实例（状态过滤、详情、CPU 型号、启动盘）。
- 启动盘列表、孤儿盘、配额（汇总/A1/体检/can-launch）、备份列表与额度、
  任务列表/详情/取消/删除、云报告、创建默认值与创建实例。
- 写操作（启停/删除/扩容/创建/恢复等）只做假动作：把各路由会调的
  OCI 入口 patch 成抛 AssertionError，仍返回成功，证明不调 OCI。
- 模拟数据不含真实租户名、真实 IP、真实域名等真实信息。
- 登录页与实例页含“演示模式”提示。
- 演示模式不用登录：未登录直接进面板（自动按 demo 用户建会话），
  /login 跳面板首页；非演示模式未登录仍跳 /login、API 仍 401。
- Clarity 统计代码在全站页面加载（配了 MBOT_CLARITY_ID 时），
  不只登录页；没配 ID 时不加载。

不碰真实 API；测试密码哈希在 setUpClass 里生成并 patch 进 web.config。

运行：python3 -m unittest tests.test_demo_mode -v
"""

import os
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from web.app import create_app
from web.auth import LOGIN_GUARD, SESSIONS, hash_password

TEST_PASSWORD = "test-admin-password"

# Demo data uses only TEST-NET / example addresses; these must never appear
# in demo payloads. 演示数据只用 TEST-NET / 示例地址；以下真实信息绝不
# 能出现在演示响应里。
# 注意：这里绝不能列出真实租户名、真实域名或真实 IP 段——列出来本身
# 就是泄露（公开仓库）。改用结构检查：演示响应里只允许 TEST-NET
# 文档地址（RFC 5737：192.0.2.x / 198.51.100.x / 203.0.113.x）与
# DEMO_* 演示标识；任何其他公网 IPv4 都算真实信息泄露。
# Do NOT list real tenant names, domains or IP ranges here — listing
# them is itself a leak (public repo). Structural check instead: demo
# responses may only contain TEST-NET addresses (RFC 5737) and DEMO_*
# identifiers; any other public IPv4 counts as a real-data leak.
FORBIDDEN = (
    "FORBIDDEN-EXAMPLE-TENANT",
    "FORBIDDEN-EXAMPLE-DOMAIN",
)
# TEST-NET 文档地址段（RFC 5737），演示数据只许用这些。
_TESTNET_RE = re.compile(
    r"\b(?:192\.0\.2|198\.51\.100|203\.0\.113)\.\d{1,3}\b")
# 任何公网 IPv4（先找出来，再排除 TEST-NET）。
_ANY_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

SG = "DEMO_SG"
SJ = "DEMO_SJ"
CHI = "DEMO_CHI"


class DemoModeTestCase(unittest.TestCase):
    """演示模式测试基类：DEMO_MODE=1 建 app、临时 DATA_DIR、登录。"""

    @classmethod
    def setUpClass(cls):
        cls._demo_patcher = patch("web.config.DEMO_MODE", True)
        cls._demo_patcher.start()
        cls._hash_patcher = patch("web.config.ADMIN_PASSWORD_HASH",
                                  hash_password(TEST_PASSWORD))
        cls._hash_patcher.start()
        cls.app = create_app()

    @classmethod
    def tearDownClass(cls):
        cls._hash_patcher.stop()
        cls._demo_patcher.stop()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._data_dir_patcher = patch("web.config.DATA_DIR", self._tmp.name)
        self._data_dir_patcher.start()
        LOGIN_GUARD.reset()
        SESSIONS.reset()
        self.client = TestClient(self.app)
        self.anon = TestClient(self.app)
        self._login()

    def tearDown(self):
        self._data_dir_patcher.stop()
        self._tmp.cleanup()

    def _login(self, username="demo", password="demo"):
        return self.client.post("/api/auth/login",
                                json={"username": username,
                                      "password": password})

    def assert_no_real_data(self, payload):
        """递归检查响应里不含任何真实租户名/IP/域名。"""
        text = repr(payload)
        for needle in FORBIDDEN:
            self.assertNotIn(needle, text, f"演示数据含真实信息：{needle}")
        # 结构检查：演示响应里的公网 IPv4 必须全是 TEST-NET 文档地址，
        # 出现其他公网 IP 就是真实信息泄露。私网/回环/链路本地地址不算。
        # Structural check: every public IPv4 in a demo response must be
        # a TEST-NET address; private/loopback/link-local are fine.
        for ip in _ANY_IPV4_RE.findall(text):
            first = int(ip.split(".")[0])
            second = int(ip.split(".")[1])
            if first == 10 or first == 127:
                continue
            if first == 172 and 16 <= second <= 31:
                continue
            if first == 192 and second == 168:
                continue
            if first == 169 and second == 254:
                continue
            self.assertRegex(
                ip, _TESTNET_RE,
                f"演示数据含非 TEST-NET 公网 IP（真实信息泄露）：{ip}")


class TestDemoLogin(DemoModeTestCase):
    """demo/demo 登录与 admin 登录。"""

    def test_demo_login_succeeds(self):
        resp = self._login("demo", "demo")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body.get("ok"))
        self.assertEqual(body.get("username"), "demo")
        self.assertTrue(body.get("demoMode"))

    def test_demo_login_wrong_password(self):
        resp = self._login("demo", "wrong")
        self.assertEqual(resp.status_code, 401)

    def test_admin_login_still_works_in_demo_mode(self):
        resp = self._login("admin", TEST_PASSWORD)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json().get("username"), "admin")
        self.assertTrue(resp.json().get("demoMode"))

    def test_me_reports_demo_mode(self):
        resp = self.client.get("/api/auth/me")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json().get("demoMode"))
        self.assertEqual(resp.json().get("username"), "demo")

    def test_login_page_redirects_to_overview_in_demo_mode(self):
        # Demo mode has no login page: /login redirects to the panel home
        # (visitors enter directly, auto-logged in as demo). 演示模式
        # 没有登录页：/login 直接跳面板首页（访客直接进，自动按 demo
        # 用户登录）。
        resp = self.anon.get("/login", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers.get("location"), "/overview")

    def test_instances_page_shows_demo_badge(self):
        resp = self.client.get("/instances")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("演示模式", resp.text)


class TestDemoProfiles(DemoModeTestCase):
    """3 个演示租户。"""

    def test_three_demo_profiles(self):
        resp = self.client.get("/api/profiles")
        self.assertEqual(resp.status_code, 200, resp.text)
        profiles = resp.json()["profiles"]
        self.assertEqual([p["name"] for p in profiles],
                         ["DEMO_SG", "DEMO_SJ", "DEMO_CHI"])
        self.assertEqual(profiles[0]["displayName"], "新加坡演示")
        self.assertEqual(profiles[1]["displayName"], "圣何塞演示")
        self.assertEqual(profiles[2]["displayName"], "芝加哥演示")
        self.assertEqual(profiles[0]["region"], "ap-singapore-1")
        self.assert_no_real_data(resp.json())

    def test_profile_mutation_blocked_in_demo_mode(self):
        # 账号增删改的是真实 OCI 配置，演示模式下直接拒绝。
        resp = self.client.delete(f"/api/profiles/{SG}")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("演示模式", resp.json().get("error", ""))
        resp = self.client.post("/api/profiles/parse",
                                json={"config_text": "[X]\nuser=ocid1.user.oc1..x"})
        self.assertEqual(resp.status_code, 400)


class TestDemoInstances(DemoModeTestCase):
    """模拟实例列表、详情、状态过滤。"""

    def test_list_all_profiles(self):
        # 默认 state=running：只含 RUNNING；state=all：含全部非 TERMINATED。
        for profile, expected_running, expected_all in (
                (SG, 3, 4), (SJ, 2, 2), (CHI, 1, 2)):
            resp = self.client.get("/api/instances",
                                   params={"profile": profile})
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertEqual(len(resp.json()["instances"]),
                             expected_running)
            resp = self.client.get("/api/instances",
                                   params={"profile": profile,
                                           "state": "all"})
            self.assertEqual(resp.status_code, 200, resp.text)
            instances = resp.json()["instances"]
            self.assertEqual(len(instances), expected_all)
            self.assert_no_real_data(resp.json())

    def test_list_default_profile_is_singapore_demo(self):
        resp = self.client.get("/api/instances")
        self.assertEqual(resp.status_code, 200, resp.text)
        names = [i["name"] for i in resp.json()["instances"]]
        self.assertIn("DEMO-ARM-01", names)
        self.assertIn("DEMO-AMD-01", names)

    def test_instance_fields_look_real(self):
        resp = self.client.get("/api/instances",
                               params={"profile": SG})
        inst = next(i for i in resp.json()["instances"]
                    if i["name"] == "DEMO-ARM-01")
        self.assertEqual(inst["shape"], "VM.Standard.A1.Flex")
        self.assertEqual(inst["state"], "RUNNING")
        self.assertEqual(inst["publicIp"], "203.0.113.11")
        self.assertEqual(inst["ocpus"], 1)
        self.assertEqual(inst["memoryInGBs"], 6)
        self.assertEqual(inst["cpuModel"], "Ampere Altra")

    def test_state_all_includes_stopped(self):
        resp = self.client.get("/api/instances",
                               params={"profile": SG, "state": "all"})
        self.assertEqual(resp.status_code, 200, resp.text)
        instances = resp.json()["instances"]
        stopped = next(i for i in instances if i["name"] == "DEMO-AMD-02")
        self.assertEqual(stopped["state"], "STOPPED")
        self.assertIsNone(stopped["publicIp"])

    def test_instance_detail(self):
        resp = self.client.get(
            "/api/instances/ocid1.instance.oc1..demoinstance0001",
            params={"profile": SG})
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["instance"]["display-name"], "DEMO-ARM-01")
        self.assertEqual(body["cpu_model"], "Ampere Altra")
        self.assertEqual(body["boot_volume_size_gbs"], 47)
        self.assertEqual(body["boot_volume_state"], "AVAILABLE")

    def test_instance_detail_not_found(self):
        resp = self.client.get("/api/instances/ocid1.instance.oc1..nope",
                               params={"profile": SG})
        self.assertEqual(resp.status_code, 404)

    def test_unknown_profile(self):
        resp = self.client.get("/api/instances",
                               params={"profile": "NOPE"})
        self.assertEqual(resp.status_code, 400)


class TestDemoVolumes(DemoModeTestCase):
    """模拟启动盘与孤儿盘。"""

    def test_volumes_list(self):
        resp = self.client.get("/api/volumes", params={"profile": SG})
        self.assertEqual(resp.status_code, 200, resp.text)
        volumes = resp.json()["volumes"]
        self.assertEqual(len(volumes), 4)
        attached = next(v for v in volumes
                        if v["displayName"] == "DEMO-ARM-01 (Boot Volume)")
        self.assertTrue(attached["attached"])
        self.assertEqual(attached["instanceName"], "DEMO-ARM-01")
        self.assert_no_real_data(resp.json())

    def test_orphans(self):
        resp = self.client.get("/api/volumes/orphans",
                               params={"profile": CHI})
        self.assertEqual(resp.status_code, 200, resp.text)
        orphans = resp.json()["volumes"]
        self.assertEqual(len(orphans), 1)
        self.assertEqual(orphans[0]["displayName"],
                         "DEMO-CHI-ORPHAN-01 (Boot Volume)")
        resp = self.client.get("/api/volumes/orphans",
                               params={"profile": SG})
        self.assertEqual(resp.json()["volumes"], [])

    def test_volume_detail(self):
        resp = self.client.get(
            "/api/volumes/ocid1.bootvolume.oc1..demovolume0001",
            params={"profile": SG})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["volume"]["display-name"],
                         "DEMO-ARM-01 (Boot Volume)")


class TestDemoQuota(DemoModeTestCase):
    """模拟配额：汇总 / A1 / 体检 / can-launch。"""

    def test_quota_summary(self):
        resp = self.client.get("/api/quota", params={"profile": SG})
        self.assertEqual(resp.status_code, 200, resp.text)
        summary = resp.json()["summary"]
        self.assertEqual(summary["a1"]["ocpu_used"], 3)
        self.assertEqual(summary["a1"]["ocpu_limit"], 4)
        self.assertEqual(summary["a1"]["memory_used_gb"], 18)
        self.assertEqual(summary["e2"]["count"], 1)
        self.assertEqual(summary["e2"]["limit"], 2)
        self.assertEqual(summary["boot_volume"]["used_gb"], 188)
        self.assertEqual(summary["boot_volume"]["limit_gb"], 200)
        self.assertTrue(summary["overall_ok"])

    def test_a1_quota(self):
        resp = self.client.get("/api/quota/a1", params={"profile": SJ})
        self.assertEqual(resp.status_code, 200, resp.text)
        a1 = resp.json()["a1"]
        self.assertEqual(a1["usedOcpu"], 4)
        self.assertEqual(a1["capOcpu"], 4)
        self.assertEqual(a1["usedMem"], 24)

    def test_a1_health(self):
        resp = self.client.get("/api/quota/a1-health",
                               params={"profile": SG})
        self.assertEqual(resp.status_code, 200, resp.text)
        health = resp.json()["health"]
        self.assertIn("overall_ok", health)
        self.assertIn("suggestions", health)

    def test_can_launch(self):
        resp = self.client.post(
            "/api/quota/can-launch",
            params={"profile": SG},
            json={"shape": "VM.Standard.E2.1.Micro"})
        self.assertEqual(resp.status_code, 200, resp.text)
        # SG 的启动盘只剩 12GB（188/200），47GB 的新盘放不下。
        self.assertFalse(resp.json()["can_launch"])
        resp = self.client.post(
            "/api/quota/can-launch",
            params={"profile": SJ},
            json={"shape": "VM.Standard.E2.1.Micro"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["can_launch"])


class TestDemoBackups(DemoModeTestCase):
    """模拟备份列表与额度。"""

    def test_backups_list(self):
        resp = self.client.get("/api/boot-backups", params={"profile": SG})
        self.assertEqual(resp.status_code, 200, resp.text)
        backups = resp.json()["backups"]
        self.assertEqual(len(backups), 2)
        self.assertEqual(backups[0]["displayName"],
                         "DEMO-ARM-01-backup-20261124-0900")
        self.assertEqual(backups[0]["lifecycleState"], "AVAILABLE")
        self.assertEqual(backups[1]["displayName"],
                         "DEMO-AMD-01-backup-20261125-1000")
        self.assertEqual(backups[1]["lifecycleState"], "CREATING")
        self.assert_no_real_data(resp.json())

    def test_backup_quota(self):
        resp = self.client.get("/api/boot-backups/quota",
                               params={"profile": SG})
        self.assertEqual(resp.status_code, 200, resp.text)
        quota = resp.json()
        self.assertEqual(quota["count"], 2)
        self.assertEqual(quota["limit"], 5)
        self.assertFalse(quota["full"])
        self.assertFalse(quota["paid"])


class TestDemoTasks(DemoModeTestCase):
    """模拟任务列表/详情/取消/删除。"""

    def test_tasks_list(self):
        resp = self.client.get("/api/tasks")
        self.assertEqual(resp.status_code, 200, resp.text)
        tasks = resp.json()["tasks"]
        self.assertEqual(len(tasks), 4)
        statuses = {t["id"]: t["status"] for t in tasks}
        self.assertEqual(statuses["demo-grab-1"], "running")
        self.assertEqual(statuses["demo-grab-2"], "done")
        self.assertEqual(statuses["demo-bootswap-1"], "failed")
        self.assertEqual(statuses["demo-create-1"], "cancelled")

    def test_task_detail(self):
        resp = self.client.get("/api/tasks/demo-grab-2")
        self.assertEqual(resp.status_code, 200, resp.text)
        task = resp.json()["task"]
        self.assertEqual(task["kind"], "grab")
        self.assertEqual(task["status"], "done")
        self.assertIn("logs", task)

    def test_task_not_found(self):
        resp = self.client.get("/api/tasks/nope")
        self.assertEqual(resp.status_code, 404)

    def test_cancel_running_task(self):
        resp = self.client.post("/api/tasks/demo-grab-1/cancel")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json().get("demo"))

    def test_cancel_finished_task_rejected(self):
        resp = self.client.post("/api/tasks/demo-grab-2/cancel")
        self.assertEqual(resp.status_code, 400)

    def test_delete_finished_task(self):
        resp = self.client.delete("/api/tasks/demo-grab-2")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json().get("demo"))

    def test_delete_running_task_rejected(self):
        resp = self.client.delete("/api/tasks/demo-grab-1")
        self.assertEqual(resp.status_code, 400)


class TestDemoCloudReport(DemoModeTestCase):
    """模拟云报告。"""

    def test_cloud_report(self):
        resp = self.client.get("/api/cloud-report/summary")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(len(body["groups"]), 3)
        labels = [g["profileLabel"] for g in body["groups"]]
        self.assertEqual(labels, ["新加坡演示", "圣何塞演示", "芝加哥演示"])
        sg = body["groups"][0]
        self.assertEqual(sg["instances"]["total"], 4)
        self.assertEqual(sg["instances"]["byState"]["RUNNING"], 3)
        self.assertEqual(sg["instances"]["byState"]["STOPPED"], 1)
        self.assertEqual(sg["volumes"]["count"], 4)
        self.assertEqual(sg["quota"]["a1"]["usedOcpu"], 3)
        self.assert_no_real_data(resp.json())


class TestDemoCreate(DemoModeTestCase):
    """模拟创建默认值与创建实例（假成功）。"""

    def test_create_defaults(self):
        resp = self.client.get("/api/create/defaults",
                               params={"profile": SG})
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["region"], "ap-singapore-1")
        self.assertEqual(body["availability_domain"],
                         "DEMO:AP-SINGAPORE-1-AD-1")

    def test_create_instance_fake_success(self):
        resp = self.client.post(
            "/api/create",
            json={
                "profile": SJ,
                "shape": "VM.Standard.E2.1.Micro",
                "image_id": "ocid1.image.oc1..demoimage0001",
                "subnet_id": "ocid1.subnet.oc1..demosubnet0001",
                "availability_domain": "demo-AD-1",
                "display_name": "DEMO-TEST-01",
                "boot_volume_size_gbs": 50,
            })
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body.get("demo"))
        self.assertEqual(body.get("task_id"), "demo-grab-1")
        self.assertEqual(body.get("root_password"), "DEMO-0000-0000-0000")

    def test_create_instance_quota_check_still_applies(self):
        # SG 启动盘只剩 3GB，放不下 47GB 的新盘：假成功也要先过配额预检。
        resp = self.client.post(
            "/api/create",
            json={
                "profile": SG,
                "shape": "VM.Standard.E2.1.Micro",
                "image_id": "ocid1.image.oc1..demoimage0001",
                "subnet_id": "ocid1.subnet.oc1..demosubnet0001",
                "availability_domain": "demo-AD-1",
                "display_name": "DEMO-TEST-02",
                "boot_volume_size_gbs": 50,
            })
        self.assertEqual(resp.status_code, 400)


class TestDemoWriteOpsDoNotCallOci(DemoModeTestCase):
    """写操作假成功且不调 OCI：把各路由的 OCI 入口 patch 成抛错。"""

    def test_instance_actions(self):
        inst = "ocid1.instance.oc1..demoinstance0001"
        with patch("web.routes.instances.cached_raw_instances",
                   side_effect=AssertionError("OCI called")), \
             patch("web.routes.instances.get_instance",
                   side_effect=AssertionError("OCI called")), \
             patch("web.routes.instances.resolve_profile",
                   side_effect=AssertionError("OCI called")), \
             patch("web.routes.instances.start_instance",
                   side_effect=AssertionError("OCI called")), \
             patch("web.routes.instances.stop_instance",
                   side_effect=AssertionError("OCI called")), \
             patch("web.routes.instances.restart_instance",
                   side_effect=AssertionError("OCI called")), \
             patch("web.routes.instances.terminate_instance",
                   side_effect=AssertionError("OCI called")), \
             patch("web.routes.instances.rename_instance",
                   side_effect=AssertionError("OCI called")):
            for action in ("start", "stop", "restart"):
                resp = self.client.post(
                    f"/api/instances/{inst}/{action}",
                    params={"profile": SG})
                self.assertEqual(resp.status_code, 200, resp.text)
                self.assertTrue(resp.json().get("demo"))
            resp = self.client.post(
                f"/api/instances/{inst}/rename",
                params={"profile": SG},
                json={"name": "DEMO-RENAMED"})
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertTrue(resp.json().get("demo"))
            resp = self.client.post(
                f"/api/instances/{inst}/terminate",
                params={"profile": SG},
                json={"confirm": True})
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertTrue(resp.json().get("demo"))
            resp = self.client.post(
                f"/api/instances/{inst}/change-ip",
                params={"profile": SG},
                json={"confirm": True})
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertEqual(resp.json().get("new_ip"), "203.0.113.99")
            resp = self.client.post(
                f"/api/instances/{inst}/boot-backups",
                params={"profile": SG})
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertTrue(resp.json().get("demo"))
            resp = self.client.post(
                f"/api/instances/{inst}/restore-boot-volume",
                params={"profile": SG},
                json={"backup_id": "ocid1.bootvolumebackup.oc1..demobackup0001",
                      "confirm": True})
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertTrue(resp.json().get("demo"))

    def test_volumes_write_ops(self):
        vol = "ocid1.bootvolume.oc1..demovolume0001"
        orphan = "ocid1.bootvolume.oc1..demovolume0023"
        with patch("web.routes.volumes.cached_raw_boot_volumes",
                   side_effect=AssertionError("OCI called")), \
             patch("web.routes.volumes.get_boot_volume",
                   side_effect=AssertionError("OCI called")), \
             patch("web.routes.volumes.resolve_profile",
                   side_effect=AssertionError("OCI called")), \
             patch("web.routes.volumes.resize_boot_volume",
                   side_effect=AssertionError("OCI called")), \
             patch("web.routes.volumes.update_boot_volume_vpus",
                   side_effect=AssertionError("OCI called")), \
             patch("web.routes.volumes.delete_boot_volume",
                   side_effect=AssertionError("OCI called")):
            resp = self.client.post(
                f"/api/volumes/{vol}/resize",
                params={"profile": SG},
                json={"size_in_gbs": 60})
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertEqual(resp.json()["volume"]["size-in-gbs"], 60)
            resp = self.client.post(
                f"/api/volumes/{vol}/vpus",
                params={"profile": SG},
                json={"vpus_per_gb": 20})
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertEqual(resp.json()["volume"]["vpus-per-gb"], 20)
            resp = self.client.delete(
                f"/api/volumes/{orphan}",
                params={"profile": CHI})
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertTrue(resp.json().get("demo"))
            # 已挂载的盘不能删（与真实规则一致）。
            resp = self.client.delete(f"/api/volumes/{vol}",
                                      params={"profile": SG})
            self.assertEqual(resp.status_code, 400)

    def test_backup_write_ops(self):
        with patch("web.routes.boot_backups.list_boot_volume_backups",
                   side_effect=AssertionError("OCI called")), \
             patch("web.routes.boot_backups.resolve_profile",
                   side_effect=AssertionError("OCI called")), \
             patch("web.routes.boot_backups.create_boot_volume_backup",
                   side_effect=AssertionError("OCI called")), \
             patch("web.routes.boot_backups.delete_boot_volume_backup",
                   side_effect=AssertionError("OCI called")), \
             patch("web.routes.boot_backups.restore_boot_volume_backup",
                   side_effect=AssertionError("OCI called")):
            resp = self.client.post(
                "/api/boot-backups",
                params={"profile": SG},
                json={"boot_volume_id":
                      "ocid1.bootvolume.oc1..demovolume0001"})
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertTrue(resp.json().get("demo"))
            resp = self.client.delete(
                "/api/boot-backups/ocid1.bootvolumebackup.oc1..demobackup0001",
                params={"profile": SG, "confirm": True})
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertTrue(resp.json().get("demo"))
            resp = self.client.post(
                "/api/boot-backups/ocid1.bootvolumebackup.oc1..demobackup0001/restore",
                params={"profile": SG},
                json={"confirm": True})
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertTrue(resp.json().get("demo"))
            resp = self.client.post(
                "/api/boot-backups/delete-batch",
                params={"profile": SG},
                json={"backup_ids":
                      ["ocid1.bootvolumebackup.oc1..demobackup0001"]})
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertTrue(resp.json().get("demo"))

    def test_read_endpoints_do_not_call_oci(self):
        with patch("web.routes.instances.cached_raw_instances",
                   side_effect=AssertionError("OCI called")), \
             patch("web.routes.instances.resolve_profile",
                   side_effect=AssertionError("OCI called")), \
             patch("web.routes.volumes.cached_raw_boot_volumes",
                   side_effect=AssertionError("OCI called")), \
             patch("web.routes.volumes.resolve_profile",
                   side_effect=AssertionError("OCI called")), \
             patch("web.routes.quota.get_free_tier_summary",
                   side_effect=AssertionError("OCI called")), \
             patch("web.routes.quota.resolve_profile",
                   side_effect=AssertionError("OCI called")), \
             patch("web.routes.boot_backups.list_boot_volume_backups",
                   side_effect=AssertionError("OCI called")), \
             patch("web.routes.boot_backups.resolve_profile",
                   side_effect=AssertionError("OCI called")), \
             patch("web.routes.cloudreport.list_instances",
                   side_effect=AssertionError("OCI called")), \
             patch("web.routes.profiles.get_region_counts",
                   side_effect=AssertionError("OCI called")):
            for url in ("/api/instances?profile=DEMO_SG",
                        "/api/volumes?profile=DEMO_SG",
                        "/api/volumes/orphans?profile=DEMO_CHI",
                        "/api/quota?profile=DEMO_SG",
                        "/api/quota/a1?profile=DEMO_SJ",
                        "/api/quota/a1-health?profile=DEMO_SG",
                        "/api/boot-backups?profile=DEMO_SG",
                        "/api/boot-backups/quota?profile=DEMO_SG",
                        "/api/cloud-report/summary",
                        "/api/profiles"):
                resp = self.client.get(url)
                self.assertEqual(resp.status_code, 200, f"{url}: {resp.text}")


class NonDemoModeTestCase(unittest.TestCase):
    """非 demo 模式下 demo/demo 不能登录。"""

    @classmethod
    def setUpClass(cls):
        cls._hash_patcher = patch("web.config.ADMIN_PASSWORD_HASH",
                                  hash_password(TEST_PASSWORD))
        cls._hash_patcher.start()
        cls.app = create_app()

    @classmethod
    def tearDownClass(cls):
        cls._hash_patcher.stop()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._data_dir_patcher = patch("web.config.DATA_DIR", self._tmp.name)
        self._data_dir_patcher.start()
        LOGIN_GUARD.reset()
        SESSIONS.reset()
        self.client = TestClient(self.app)

    def tearDown(self):
        self._data_dir_patcher.stop()
        self._tmp.cleanup()

    def test_demo_login_fails_when_demo_mode_off(self):
        resp = self.client.post("/api/auth/login",
                                json={"username": "demo",
                                      "password": "demo"})
        self.assertEqual(resp.status_code, 401)

    def test_me_reports_demo_mode_off(self):
        self.client.post("/api/auth/login",
                         json={"username": "admin",
                               "password": TEST_PASSWORD})
        resp = self.client.get("/api/auth/me")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertFalse(resp.json().get("demoMode"))

    def test_anon_overview_redirects_to_login(self):
        # Non-demo mode still requires login: the home page redirects to
        # /login for anonymous visitors. 非演示模式仍需登录：未登录访
        # 问首页跳回 /login。
        resp = self.client.get("/overview", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers.get("location"), "/login")

    def test_anon_api_returns_401(self):
        resp = self.client.get("/api/instances")
        self.assertEqual(resp.status_code, 401)

    def test_login_page_still_shown_and_has_clarity(self):
        # The login page still exists in non-demo mode, and its Clarity
        # stats code still loads when an ID is configured. 非演示模式
        # 登录页仍在，配了 Clarity ID 时统计代码仍加载。
        with patch("web.config.CLARITY_ID", "test-clarity-0000"):
            resp = self.client.get("/login")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("clarity.ms/tag/", resp.text)
        self.assertIn("test-clarity-0000", resp.text)


class TestDemoNoLogin(DemoModeTestCase):
    """演示模式去掉登录：未登录直接进面板。"""

    def test_anon_overview_returns_200_and_sets_session(self):
        resp = self.anon.get("/overview")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn("演示模式", resp.text)
        # A demo session cookie was auto-created for the visitor.
        # 访客已自动拿到一个 demo 会话 cookie。
        self.assertIn("mbot_session", self.anon.cookies)

    def test_anon_login_redirects_to_overview(self):
        resp = self.anon.get("/login", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers.get("location"), "/overview")
        resp = self.anon.get("/login")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn("演示模式", resp.text)

    def test_anon_api_works_without_login(self):
        resp = self.anon.get("/api/instances")
        self.assertEqual(resp.status_code, 200, resp.text)
        resp = self.anon.get("/api/auth/me")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json().get("username"), "demo")
        self.assertTrue(resp.json().get("demoMode"))

    def test_demo_login_endpoint_still_works(self):
        # The login API itself still works in demo mode (and a failed
        # login still returns 401). 演示模式下登录接口本身仍可用，
        # 错密仍返回 401。
        resp = self.anon.post("/api/auth/login",
                              json={"username": "demo",
                                    "password": "demo"})
        self.assertEqual(resp.status_code, 200, resp.text)
        resp = self.anon.post("/api/auth/login",
                              json={"username": "demo",
                                    "password": "wrong"})
        self.assertEqual(resp.status_code, 401)


class TestClaritySitewide(DemoModeTestCase):
    """Clarity 统计代码在全站页面加载（配了 ID 时），不只登录页。"""

    def test_inner_pages_include_clarity_when_configured(self):
        with patch("web.config.CLARITY_ID", "test-clarity-0000"):
            for url in ("/overview", "/instances", "/quota"):
                resp = self.client.get(url)
                self.assertEqual(resp.status_code, 200, url)
                self.assertIn("clarity.ms/tag/", resp.text, url)
                self.assertIn("test-clarity-0000", resp.text, url)

    def test_inner_pages_exclude_clarity_when_not_configured(self):
        with patch("web.config.CLARITY_ID", ""):
            resp = self.client.get("/overview")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertNotIn("clarity.ms/tag/", resp.text)


if __name__ == "__main__":
    unittest.main()
