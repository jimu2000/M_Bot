#!/usr/bin/env python3
"""Web 面板 API 的集成测试（TestClient + mock oci 函数，不碰真实 API）。

运行：python3 -m unittest tests.test_web_api -v

注意：
  - 测试密码哈希在 setUpClass 里生成并 patch 进 web.config，
    不使用任何真实密码。
  - 主机 CRUD 测试用临时 DATA_DIR，不污染真实数据目录。
  - 所有写操作（启停/终止/扩容/删盘/抢机）全部 mock，不碰真实生产实例。
"""

import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import ANY, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from oci.client import NotFoundError, RateLimitError
from oci.grabber import GrabResult
from web import config
from web.app import create_app
from web.auth import LOGIN_GUARD, SESSIONS, hash_password
from web.tasks import TASKS

# 占位符（测试里不出现真实 OCID / IP）
FAKE_INSTANCE = "ocid1.instance.oc1..testxxxx"
FAKE_INSTANCE_2 = "ocid1.instance.oc1..testyyyy"
FAKE_VOLUME = "ocid1.bootvolume.oc1..testxxxx"
FAKE_IMAGE = "ocid1.image.oc1..testxxxx"
FAKE_SUBNET = "ocid1.subnet.oc1..testxxxx"
FAKE_IP = "203.0.113.10"

TEST_PASSWORD = "test-password"

FAKE_INSTANCES = [
    {"id": FAKE_INSTANCE, "display-name": "instance-a1",
     "shape": "VM.Standard.A1.Flex", "lifecycle-state": "RUNNING",
     "time-created": "2026-11-11T10:00:00.000Z"},
    {"id": FAKE_INSTANCE_2, "display-name": "instance-e2",
     "shape": "VM.Standard.E2.1.Micro", "lifecycle-state": "STOPPED",
     "time-created": "2026-11-12T10:00:00.000Z"},
]

FAKE_VOLUMES = [
    {"id": FAKE_VOLUME, "display-name": "boot-volume-a1",
     "size-in-gbs": 47, "lifecycle-state": "AVAILABLE",
     "availability-domain": "AD-1"},
]

FAKE_SUMMARY = {
    "a1": {"ocpu_used": 1, "ocpu_limit": 4, "ocpu_remaining": 3,
           "memory_used_gb": 6, "memory_limit_gb": 24,
           "memory_remaining_gb": 18, "instances": []},
    "e2": {"count": 1, "limit": 2, "remaining": 1, "instances": []},
    "boot_volume": {"used_gb": 47, "limit_gb": 200, "remaining_gb": 153,
                    "volumes": []},
    "overall_ok": True,
}

CREATE_BODY = {
    "shape": "VM.Standard.E2.1.Micro",
    "image_id": FAKE_IMAGE,
    "subnet_id": FAKE_SUBNET,
    "availability_domain": "AD-1",
    "display_name": "test-instance",
    "ssh_keys_file": "/tmp/test-keys.pub",
    "target_cpu": "7J13",
}


class BlockingGrabber:
    """抢机测试替身：run() 阻塞 30 秒（daemon 线程，不影响测试退出）。"""

    def __init__(self, *args, **kwargs):
        pass

    def run(self):
        time.sleep(30)
        return GrabResult(instance_id=FAKE_INSTANCE, attempts=1,
                          releases=0, elapsed_seconds=1.0)


class WebApiTestCase(unittest.TestCase):
    """API 测试基类：建 app、临时 DATA_DIR、登录辅助方法。"""

    @classmethod
    def setUpClass(cls):
        # 生成测试密码哈希并 patch 进 config（绕过启动检查）。
        cls._hash_patcher = patch("web.config.ADMIN_PASSWORD_HASH",
                                  hash_password(TEST_PASSWORD))
        cls._hash_patcher.start()
        cls.app = create_app()

    @classmethod
    def tearDownClass(cls):
        cls._hash_patcher.stop()

    def setUp(self):
        # 临时数据目录（hosts.json / tasks.json 写这里，不污染真实目录）。
        self._tmp = tempfile.TemporaryDirectory()
        self._data_dir_patcher = patch("web.config.DATA_DIR", self._tmp.name)
        self._data_dir_patcher.start()
        LOGIN_GUARD.reset()
        SESSIONS.reset()
        TASKS.reset()
        self.client = TestClient(self.app)
        self.anon = TestClient(self.app)

    def tearDown(self):
        self._data_dir_patcher.stop()
        self._tmp.cleanup()

    def _login(self, username="admin", password=TEST_PASSWORD):
        """登录并返回响应（成功时 TestClient 自动保存 cookie）。"""
        return self.client.post("/api/auth/login",
                                json={"username": username,
                                      "password": password})

    def _wait_task_status(self, task_id, statuses, timeout=5.0):
        """轮询任务直到状态进入 statuses，返回任务字典。"""
        deadline = time.time() + timeout
        task = None
        while time.time() < deadline:
            r = self.client.get(f"/api/tasks/{task_id}")
            task = r.json()["task"]
            if task["status"] in statuses:
                return task
            time.sleep(0.05)
        return task


class TestAuthApi(WebApiTestCase):
    """登录 / 登出 / 锁定。"""

    def test_login_success(self):
        r = self._login()
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.assertEqual(r.json()["username"], "admin")
        self.assertIn(config.SESSION_COOKIE, r.cookies)

    def test_me_after_login(self):
        self._login()
        r = self.client.get("/api/auth/me")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"ok": True, "username": "admin"})

    def test_login_wrong_password(self):
        r = self._login(password="wrong-password")
        self.assertEqual(r.status_code, 401)
        self.assertFalse(r.json()["ok"])

    def test_login_wrong_username(self):
        r = self._login(username="not-admin")
        self.assertEqual(r.status_code, 401)

    def test_login_lockout_429(self):
        """5 次失败后第 6 次返回 429 + 剩余秒数，正确密码也被挡。"""
        for _ in range(5):
            r = self._login(password="wrong")
            self.assertEqual(r.status_code, 401)
        r = self._login(password="wrong")
        self.assertEqual(r.status_code, 429)
        self.assertFalse(r.json()["ok"])
        self.assertIn("remaining_seconds", r.json())
        self.assertGreater(r.json()["remaining_seconds"], 0)
        # 锁定时正确密码也进不去。
        r = self._login()
        self.assertEqual(r.status_code, 429)

    def test_logout(self):
        self._login()
        r = self.client.post("/api/auth/logout")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        # 登出后 me 返回 401。
        r = self.client.get("/api/auth/me")
        self.assertEqual(r.status_code, 401)

    def test_unauthenticated_api_401(self):
        """未登录调 API 返回 401（信封格式）。"""
        for method, path in [
            ("get", "/api/instances"),
            ("get", "/api/volumes"),
            ("get", "/api/quota"),
            ("get", "/api/tasks"),
            ("get", "/api/hosts"),
            ("get", "/api/system"),
            ("get", "/api/auth/me"),
        ]:
            r = getattr(self.anon, method)(path)
            self.assertEqual(r.status_code, 401, path)
            self.assertFalse(r.json()["ok"])
        r = self.anon.post(f"/api/instances/{FAKE_INSTANCE}/start")
        self.assertEqual(r.status_code, 401)


class TestInstancesApi(WebApiTestCase):
    """实例列表 / 详情 / 启停 / 重启 / 终止。"""

    def setUp(self):
        super().setUp()
        self._login()

    @patch("web.routes.instances.get_public_ip", return_value=FAKE_IP)
    @patch("web.routes.instances.list_instances", return_value=FAKE_INSTANCES)
    def test_list_instances(self, mock_list, mock_ip):
        """R-Bot 形状：只含 RUNNING 实例，新字段齐全，旧字段双写。"""
        r = self.client.get("/api/instances")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        # FAKE_INSTANCES 里 1 台 RUNNING + 1 台 STOPPED，只返回 RUNNING 的。
        self.assertEqual(len(body["instances"]), 1)
        inst = body["instances"][0]
        self.assertEqual(inst["id"], FAKE_INSTANCE)
        self.assertEqual(inst["name"], "instance-a1")
        self.assertEqual(inst["publicIp"], FAKE_IP)
        self.assertEqual(inst["shape"], "VM.Standard.A1.Flex")
        self.assertEqual(inst["state"], "RUNNING")
        self.assertEqual(inst["region"], "ap-singapore-1")
        self.assertEqual(inst["timeCreated"], "2026-11-11T10:00:00.000Z")
        # FAKE 数据无 shape-config，ocpus/memoryInGBs 为 None。
        self.assertIsNone(inst["ocpus"])
        self.assertIsNone(inst["memoryInGBs"])
        # 旧字段双写（过渡期）。
        self.assertEqual(inst["public-ip"], FAKE_IP)
        self.assertEqual(inst["display-name"], "instance-a1")
        self.assertEqual(inst["lifecycle-state"], "RUNNING")
        mock_list.assert_called_once()
        # 列表调用时带 include_shape_config=True（取 OCPU/内存用）。
        _args, kwargs = mock_list.call_args
        self.assertTrue(kwargs.get("include_shape_config"))

    @patch("web.routes.instances.get_public_ip",
           side_effect=NotFoundError("no vnic"))
    @patch("web.routes.instances.list_instances", return_value=FAKE_INSTANCES)
    def test_list_instances_ip_failure_tolerated(self, mock_list, mock_ip):
        """单台实例取 IP 失败时列表仍返回，IP 为 None。"""
        r = self.client.get("/api/instances")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["instances"][0]["public-ip"])
        self.assertIsNone(r.json()["instances"][0]["publicIp"])

    @patch("web.routes.instances.get_public_ip", return_value=FAKE_IP)
    @patch("web.routes.instances.get_instance")
    def test_get_instance(self, mock_get, mock_ip):
        mock_get.return_value = {"data": {"id": FAKE_INSTANCE,
                                          "display-name": "instance-a1",
                                          "lifecycle-state": "RUNNING"}}
        r = self.client.get(f"/api/instances/{FAKE_INSTANCE}")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["instance"]["display-name"], "instance-a1")
        self.assertEqual(body["public_ip"], FAKE_IP)

    @patch("web.routes.instances.get_instance",
           side_effect=NotFoundError("not found"))
    def test_get_instance_not_found_404(self, mock_get):
        r = self.client.get(f"/api/instances/{FAKE_INSTANCE}")
        self.assertEqual(r.status_code, 404)
        self.assertFalse(r.json()["ok"])

    @patch("web.routes.instances.list_instances",
           side_effect=RateLimitError("429 TooManyRequests"))
    def test_list_rate_limited_429(self, mock_list):
        r = self.client.get("/api/instances")
        self.assertEqual(r.status_code, 429)

    def test_unknown_profile_400(self):
        r = self.client.get("/api/instances?profile=NOPE")
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.json()["ok"])

    @patch("web.routes.instances.start_instance")
    def test_start(self, mock_start):
        r = self.client.post(f"/api/instances/{FAKE_INSTANCE}/start")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["action"], "start")
        mock_start.assert_called_once_with(ANY, FAKE_INSTANCE)

    @patch("web.routes.instances.stop_instance")
    def test_stop(self, mock_stop):
        r = self.client.post(f"/api/instances/{FAKE_INSTANCE}/stop")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["action"], "stop")
        mock_stop.assert_called_once_with(ANY, FAKE_INSTANCE)

    @patch("web.routes.instances.restart_instance")
    def test_restart(self, mock_restart):
        r = self.client.post(f"/api/instances/{FAKE_INSTANCE}/restart")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["action"], "restart")
        mock_restart.assert_called_once_with(ANY, FAKE_INSTANCE)

    @patch("web.routes.instances.terminate_instance")
    def test_terminate_without_confirm_400(self, mock_terminate):
        """终止缺 confirm 返回 400，且不调用终止函数。"""
        r = self.client.post(f"/api/instances/{FAKE_INSTANCE}/terminate",
                             json={})
        self.assertEqual(r.status_code, 400)
        r = self.client.post(f"/api/instances/{FAKE_INSTANCE}/terminate",
                             json={"confirm": False})
        self.assertEqual(r.status_code, 400)
        mock_terminate.assert_not_called()

    @patch("web.routes.instances.terminate_instance")
    def test_terminate_with_confirm(self, mock_terminate):
        """confirm=true 时终止（不传 preserve_boot_volume，用默认 False
        永久删引导卷）。"""
        r = self.client.post(f"/api/instances/{FAKE_INSTANCE}/terminate",
                             json={"confirm": True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["action"], "terminate")
        mock_terminate.assert_called_once_with(ANY, FAKE_INSTANCE)

    @patch("web.routes.instances.terminate_instance",
           side_effect=NotFoundError("not found"))
    def test_terminate_not_found_404(self, mock_terminate):
        r = self.client.post(f"/api/instances/{FAKE_INSTANCE}/terminate",
                             json={"confirm": True})
        self.assertEqual(r.status_code, 404)


class TestVolumesApi(WebApiTestCase):
    """磁盘列表 / 详情 / 扩容 / 删除 / 孤儿盘。"""

    def setUp(self):
        super().setUp()
        self._login()

    @patch("web.routes.volumes.list_instances", return_value=[])
    @patch("web.routes.volumes.list_boot_volume_attachments", return_value=[])
    @patch("web.routes.volumes.list_boot_volumes", return_value=FAKE_VOLUMES)
    def test_list_volumes(self, mock_list, mock_atts, mock_insts):
        """R-Bot 形状：新字段齐全，旧字段双写；无挂载时 attached=false。"""
        r = self.client.get("/api/volumes")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["volumes"]), 1)
        vol = r.json()["volumes"][0]
        self.assertEqual(vol["volumeId"], FAKE_VOLUME)
        self.assertEqual(vol["displayName"], "boot-volume-a1")
        self.assertEqual(vol["sizeInGBs"], 47)
        self.assertEqual(vol["lifecycleState"], "AVAILABLE")
        self.assertEqual(vol["availabilityDomain"], "AD-1")
        self.assertFalse(vol["attached"])
        self.assertIsNone(vol["attachmentId"])
        self.assertIsNone(vol["instanceId"])
        self.assertIsNone(vol["instanceName"])
        # 旧字段双写（过渡期）。
        self.assertEqual(vol["size-in-gbs"], 47)
        self.assertEqual(vol["id"], FAKE_VOLUME)

    @patch("web.routes.volumes.get_boot_volume", return_value=FAKE_VOLUMES[0])
    def test_get_volume(self, mock_get):
        r = self.client.get(f"/api/volumes/{FAKE_VOLUME}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["volume"]["id"], FAKE_VOLUME)

    @patch("web.routes.volumes.list_instances", return_value=[])
    @patch("web.routes.volumes.list_boot_volume_attachments", return_value=[])
    @patch("web.routes.volumes.list_orphan_boot_volumes",
           return_value=FAKE_VOLUMES)
    def test_orphans(self, mock_orphans, mock_atts, mock_insts):
        r = self.client.get("/api/volumes/orphans")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["volumes"]), 1)
        # 孤儿盘同样返回 R-Bot 形状。
        self.assertEqual(r.json()["volumes"][0]["volumeId"], FAKE_VOLUME)
        self.assertFalse(r.json()["volumes"][0]["attached"])

    @patch("web.routes.volumes.resize_boot_volume")
    @patch("web.routes.volumes.get_boot_volume", return_value=FAKE_VOLUMES[0])
    def test_resize_ok(self, mock_get, mock_resize):
        resized = dict(FAKE_VOLUMES[0], **{"size-in-gbs": 100})
        mock_resize.return_value = resized
        r = self.client.post(f"/api/volumes/{FAKE_VOLUME}/resize",
                             json={"size_in_gbs": 100})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["volume"]["size-in-gbs"], 100)
        mock_resize.assert_called_once_with(ANY, FAKE_VOLUME, 100)

    @patch("web.routes.volumes.resize_boot_volume")
    @patch("web.routes.volumes.get_boot_volume", return_value=FAKE_VOLUMES[0])
    def test_resize_not_larger_400(self, mock_get, mock_resize):
        """新容量不大于当前容量时返回 400，不调用扩容。"""
        for size in (47, 10):
            r = self.client.post(f"/api/volumes/{FAKE_VOLUME}/resize",
                                 json={"size_in_gbs": size})
            self.assertEqual(r.status_code, 400, size)
        mock_resize.assert_not_called()

    @patch("web.routes.volumes.delete_boot_volume")
    def test_delete_volume(self, mock_delete):
        r = self.client.delete(f"/api/volumes/{FAKE_VOLUME}")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        mock_delete.assert_called_once_with(ANY, FAKE_VOLUME)

    @patch("web.routes.volumes.get_boot_volume",
           side_effect=NotFoundError("not found"))
    def test_get_volume_not_found_404(self, mock_get):
        r = self.client.get(f"/api/volumes/{FAKE_VOLUME}")
        self.assertEqual(r.status_code, 404)


class TestQuotaApi(WebApiTestCase):
    """配额汇总 / can-launch。"""

    def setUp(self):
        super().setUp()
        self._login()

    @patch("web.routes.quota.get_free_tier_summary",
           return_value=FAKE_SUMMARY)
    def test_quota_summary(self, mock_summary):
        r = self.client.get("/api/quota")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["summary"]["a1"]["ocpu_limit"], 4)
        self.assertTrue(body["summary"]["overall_ok"])

    @patch("web.routes.quota.can_launch", return_value=(True, "可以创建"))
    def test_can_launch_ok(self, mock_can):
        r = self.client.post("/api/quota/can-launch",
                             json={"shape": "VM.Standard.E2.1.Micro"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["can_launch"])
        self.assertEqual(r.json()["reason"], "可以创建")

    @patch("web.routes.quota.can_launch",
           return_value=(False, "E2 已满（2/2）"))
    def test_can_launch_no(self, mock_can):
        r = self.client.post("/api/quota/can-launch",
                             json={"shape": "VM.Standard.E2.1.Micro"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["can_launch"])
        self.assertIn("E2 已满", r.json()["reason"])


class TestCreateAndTasksApi(WebApiTestCase):
    """创建实例（抢机任务）/ 任务列表 / 取消。"""

    def setUp(self):
        super().setUp()
        self._login()

    @patch("web.tasks.Grabber")
    @patch("web.routes.create.can_launch", return_value=(True, "可以创建"))
    def test_create_starts_grab_task(self, mock_can, mock_grabber_cls):
        """预检通过则建任务、后台跑 Grabber，返回 task_id。"""
        mock_grabber_cls.return_value.run.return_value = GrabResult(
            instance_id=FAKE_INSTANCE, attempts=3, releases=1,
            elapsed_seconds=95.0)
        r = self.client.post("/api/create", json=CREATE_BODY)
        self.assertEqual(r.status_code, 200)
        task_id = r.json()["task_id"]
        self.assertTrue(task_id)
        # 等后台线程完成。
        task = self._wait_task_status(task_id, ("done", "failed"))
        self.assertEqual(task["status"], "done")
        self.assertEqual(task["result"]["instance_id"], FAKE_INSTANCE)
        self.assertEqual(task["result"]["attempts"], 3)
        self.assertEqual(task["result"]["releases"], 1)
        # 已结束的任务不能取消。
        r = self.client.post(f"/api/tasks/{task_id}/cancel")
        self.assertEqual(r.status_code, 400)

    @patch("web.tasks.Grabber")
    @patch("web.routes.create.can_launch",
           return_value=(False, "E2 已满（2/2）"))
    def test_create_quota_check_fails_400(self, mock_can, mock_grabber_cls):
        """预检不通过返回 400，不启动 Grabber。"""
        r = self.client.post("/api/create", json=CREATE_BODY)
        self.assertEqual(r.status_code, 400)
        self.assertIn("E2 已满", r.json()["error"])
        mock_grabber_cls.assert_not_called()

    def test_create_missing_field_422(self):
        """缺必填字段返回 422（信封格式）。"""
        body = dict(CREATE_BODY)
        del body["image_id"]
        r = self.client.post("/api/create", json=body)
        self.assertEqual(r.status_code, 422)
        self.assertFalse(r.json()["ok"])

    @patch("web.tasks.Grabber")
    @patch("web.routes.create.can_launch", return_value=(True, "可以创建"))
    def test_create_full_fields_passed_to_grabber(self, mock_can,
                                                  mock_grabber_cls):
        """带全字段创建时，全部新字段透传给 Grabber（阶段 3b 接入）。"""
        mock_grabber_cls.return_value.run.return_value = GrabResult(
            instance_id=FAKE_INSTANCE, attempts=1, releases=0,
            elapsed_seconds=5.0)
        body = dict(CREATE_BODY)
        body.update({
            "shape": "VM.Standard.A1.Flex",
            "ocpus": 4,
            "memory_gb": 24,
            "boot_volume_size_gbs": 100,
            "assign_public_ip": False,
            "assign_ipv6": True,
            "min_delay": 20,
            "max_delay": 50,
            "user_data": "#!/bin/bash\necho hi\n",
        })
        r = self.client.post("/api/create", json=body)
        self.assertEqual(r.status_code, 200)
        task_id = r.json()["task_id"]
        self._wait_task_status(task_id, ("done", "failed"))
        mock_grabber_cls.assert_called_once()
        kwargs = mock_grabber_cls.call_args.kwargs
        self.assertEqual(kwargs["ocpus"], 4)
        self.assertEqual(kwargs["memory_gb"], 24)
        self.assertEqual(kwargs["boot_volume_size_gbs"], 100)
        self.assertFalse(kwargs["assign_public_ip"])
        self.assertTrue(kwargs["assign_ipv6"])
        self.assertEqual(kwargs["min_delay"], 20)
        self.assertEqual(kwargs["max_delay"], 50)
        self.assertEqual(kwargs["user_data"], "#!/bin/bash\necho hi\n")
        self.assertEqual(kwargs["target_cpu"], "7J13")
        # 位置参数仍是原来的 8 个（cli/compartment/AD/shape/image/subnet/
        # display_name/ssh_keys_file），A1 形状也正确传到位置参数里。
        args = mock_grabber_cls.call_args.args
        self.assertEqual(args[3], "VM.Standard.A1.Flex")

    def test_create_boot_volume_out_of_range_422(self):
        """引导卷 <47 或 >200 返回 422（pydantic 范围校验）。"""
        for bad_size in (46, 201):
            body = dict(CREATE_BODY)
            body["boot_volume_size_gbs"] = bad_size
            r = self.client.post("/api/create", json=body)
            self.assertEqual(r.status_code, 422, f"size={bad_size}")
            self.assertFalse(r.json()["ok"])

    def test_create_boot_volume_boundary_ok(self):
        """引导卷 47 / 200 边界值可通过校验（进预检流程，不被 422 挡）。"""
        with patch("web.routes.create.can_launch",
                   return_value=(False, "E2 已满（2/2）")):
            for size in (47, 200):
                body = dict(CREATE_BODY)
                body["boot_volume_size_gbs"] = size
                r = self.client.post("/api/create", json=body)
                # 校验通过后走到配额预检（mock 不通过）返回 400，而不是 422。
                self.assertEqual(r.status_code, 400, f"size={size}")

    def test_create_max_delay_less_than_min_400(self):
        """max_delay < min_delay 返回 400。"""
        body = dict(CREATE_BODY)
        body["min_delay"] = 50
        body["max_delay"] = 20
        r = self.client.post("/api/create", json=body)
        self.assertEqual(r.status_code, 400)
        self.assertIn("最大延迟", r.json()["error"])

    @patch("web.tasks.Grabber", BlockingGrabber)
    @patch("web.routes.create.can_launch", return_value=(True, "可以创建"))
    def test_cancel_running_task(self, mock_can):
        """取消运行中的任务返回 200，任务状态变为 cancelled。"""
        r = self.client.post("/api/create", json=CREATE_BODY)
        task_id = r.json()["task_id"]
        task = self._wait_task_status(task_id, ("running",))
        self.assertEqual(task["status"], "running")
        r = self.client.post(f"/api/tasks/{task_id}/cancel")
        self.assertEqual(r.status_code, 200)
        r = self.client.get(f"/api/tasks/{task_id}")
        self.assertEqual(r.json()["task"]["status"], "cancelled")

    def test_task_not_found(self):
        r = self.client.get("/api/tasks/g999")
        self.assertEqual(r.status_code, 404)
        r = self.client.post("/api/tasks/g999/cancel")
        self.assertEqual(r.status_code, 404)

    @patch("web.tasks.Grabber")
    @patch("web.routes.create.can_launch", return_value=(True, "可以创建"))
    def test_list_tasks(self, mock_can, mock_grabber_cls):
        mock_grabber_cls.return_value.run.return_value = GrabResult(
            instance_id=FAKE_INSTANCE, attempts=1, releases=0,
            elapsed_seconds=5.0)
        r = self.client.get("/api/tasks")
        self.assertEqual(r.json()["tasks"], [])
        self.client.post("/api/create", json=CREATE_BODY)
        r = self.client.get("/api/tasks")
        self.assertEqual(len(r.json()["tasks"]), 1)
        self.assertEqual(r.json()["tasks"][0]["kind"], "grab")


class TestHostsApi(WebApiTestCase):
    """已保存 SSH 主机 CRUD（临时 DATA_DIR）。"""

    def setUp(self):
        super().setUp()
        self._login()

    def _hosts_file_text(self):
        path = os.path.join(self._tmp.name, "hosts.json")
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_crud_flow(self):
        # 列表初始为空。
        r = self.client.get("/api/hosts")
        self.assertEqual(r.json()["hosts"], [])
        # 新建（带 password 字段，应被丢弃不存储）。
        r = self.client.post("/api/hosts", json={
            "name": "sg-139", "host": FAKE_IP, "port": 14422,
            "username": "root", "auth_type": "password",
            "password": "s3cret-pw-12345", "tags": ["sg"],
            "notes": "测试主机"})
        self.assertEqual(r.status_code, 201)
        host = r.json()["host"]
        self.assertTrue(host["id"])
        self.assertEqual(host["name"], "sg-139")
        self.assertNotIn("password", host)
        self.assertIsNone(host["last_used"])
        # 文件里绝不能有密码明文，也不能有 password 字段
        # （auth_type 的值 "password" 本身是允许的）。
        text = self._hosts_file_text()
        self.assertNotIn("s3cret-pw-12345", text)
        with open(os.path.join(self._tmp.name, "hosts.json"),
                  encoding="utf-8") as f:
            stored = json.load(f)
        self.assertNotIn("password", stored[0])
        # 列表里有一台。
        r = self.client.get("/api/hosts")
        self.assertEqual(len(r.json()["hosts"]), 1)
        # 更新。
        r = self.client.put(f"/api/hosts/{host['id']}",
                            json={"name": "sg-139-renamed", "notes": "改过"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["host"]["name"], "sg-139-renamed")
        self.assertEqual(r.json()["host"]["host"], FAKE_IP)  # 未改字段保留
        # 删除。
        r = self.client.delete(f"/api/hosts/{host['id']}")
        self.assertEqual(r.status_code, 200)
        r = self.client.get("/api/hosts")
        self.assertEqual(r.json()["hosts"], [])

    def test_create_key_host(self):
        r = self.client.post("/api/hosts", json={
            "name": "key-host", "host": FAKE_IP,
            "auth_type": "key", "key_path": "/home/user/.ssh/id_ed25519"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["host"]["key_path"],
                         "/home/user/.ssh/id_ed25519")

    def test_create_key_host_without_key_path_400(self):
        r = self.client.post("/api/hosts", json={
            "name": "key-host", "host": FAKE_IP, "auth_type": "key"})
        self.assertEqual(r.status_code, 400)

    def test_create_bad_auth_type_400(self):
        r = self.client.post("/api/hosts", json={
            "name": "x", "host": FAKE_IP, "auth_type": "token"})
        self.assertEqual(r.status_code, 400)

    def test_create_bad_port_400(self):
        r = self.client.post("/api/hosts", json={
            "name": "x", "host": FAKE_IP, "port": 70000})
        self.assertEqual(r.status_code, 400)

    def test_update_and_delete_not_found_404(self):
        r = self.client.put("/api/hosts/no-such-id", json={"name": "x"})
        self.assertEqual(r.status_code, 404)
        r = self.client.delete("/api/hosts/no-such-id")
        self.assertEqual(r.status_code, 404)

    def test_hosts_persist_to_temp_data_dir(self):
        """hosts.json 写到临时 DATA_DIR（不污染真实目录）。"""
        self.client.post("/api/hosts", json={"name": "h", "host": FAKE_IP})
        path = os.path.join(self._tmp.name, "hosts.json")
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["host"], FAKE_IP)


class TestSystemApi(WebApiTestCase):
    """系统资源接口。"""

    def setUp(self):
        super().setUp()
        self._login()

    def test_system_status_structure(self):
        r = self.client.get("/api/system")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        for key in ("cpu", "memory", "disk", "network"):
            self.assertIn(key, body)
        self.assertIn("percent", body["cpu"])
        self.assertIn("percent", body["memory"])
        self.assertIn("percent", body["disk"])
        for section in ("cpu", "memory", "disk"):
            self.assertGreaterEqual(body[section]["percent"], 0)
            self.assertLessEqual(body[section]["percent"], 100)
        self.assertGreater(body["memory"]["total_mb"], 0)
        self.assertGreater(body["disk"]["total_gb"], 0)
        self.assertGreaterEqual(body["network"]["rx_bytes_per_sec"], 0)
        self.assertGreaterEqual(body["network"]["tx_bytes_per_sec"], 0)


class TestPages(WebApiTestCase):
    """HTML 页面路由（模板缺失时返回占位页，不报错）。"""

    def test_login_page_no_auth(self):
        r = self.anon.get("/login")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/html", r.headers["content-type"])

    def test_root_redirects_to_overview(self):
        r = self.anon.get("/", follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["location"], "/overview")

    def test_protected_pages_redirect_when_logged_out(self):
        for path in ("/overview", "/instances", "/create", "/volumes",
                     "/tasks", "/terminal"):
            r = self.anon.get(path, follow_redirects=False)
            self.assertEqual(r.status_code, 302, path)
            self.assertEqual(r.headers["location"], "/login", path)

    def test_protected_pages_ok_when_logged_in(self):
        self._login()
        for path in ("/overview", "/instances", "/create", "/volumes",
                     "/tasks", "/terminal"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)
            self.assertIn("text/html", r.headers["content-type"])


if __name__ == "__main__":
    unittest.main()
