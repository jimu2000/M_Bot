"""Regression tests for the v2.11 overlay: instance-list single-flight,
instance-detail cache + single-flight, and CPU-model SSH de-duplication.

Converted from ChatGPT's standalone script into a proper unittest module:
the original stubbed sys.modules['telegram']/['paramiko'] at import time,
which broke every other test module during discovery. The real packages
are installed in this repo's venv, so no stubs are needed.
"""
import asyncio
import unittest
from unittest import mock

import tgbot.handlers as h


class Ctx:
    def __init__(self):
        self.user_data = {"profile": "P1", "region": "R1"}


class TestListSingleflight(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._orig = {}
        for name in ("run_sync", "get_cli", "get_compartment",
                     "_current_account_label", "fmt_instance_list",
                     "instance_list_keyboard", "store_instances"):
            self._orig[name] = getattr(h, name, None)
        h.clear_instance_list_cache()

    def tearDown(self):
        for name, value in self._orig.items():
            if value is None:
                try:
                    delattr(h, name)
                except AttributeError:
                    pass
            else:
                setattr(h, name, value)
        h.clear_instance_list_cache()

    async def test_list_singleflight_and_warm_cache(self):
        calls = {"list": 0, "ip": 0}

        async def fake_run(func, *args, **kwargs):
            await asyncio.sleep(.05)
            if func is h.list_instances:
                calls["list"] += 1
                return [{"id": "ocid1", "display-name": "test",
                         "lifecycle-state": "RUNNING"}]
            if func is h.attach_public_ips_bulk:
                calls["ip"] += 1
                self.assertEqual(kwargs.get("max_workers"), 2)
                return ["203.0.113.4"]
            raise AssertionError(func)

        h.run_sync = fake_run
        h.get_cli = lambda ctx: object()
        h.get_compartment = lambda ctx: "compartment"
        h._current_account_label = lambda ctx: "account"
        h.fmt_instance_list = lambda *a, **k: "list text"
        h.instance_list_keyboard = lambda *a, **k: "keyboard"
        h.store_instances = lambda ctx, instances: ctx.user_data.update(
            instances=instances)
        contexts = [Ctx() for _ in range(12)]
        results = await asyncio.gather(
            *(h._load_instance_list(c) for c in contexts))
        self.assertEqual(calls, {"list": 1, "ip": 1})
        self.assertTrue(all(x == ("list text", "keyboard") for x in results))
        self.assertTrue(all(
            c.user_data["instances"][0]["public-ip"] == "203.0.113.4"
            for c in contexts))
        # Warm cache: no additional OCI calls.
        await h._load_instance_list(Ctx())
        self.assertEqual(calls, {"list": 1, "ip": 1})


class TestDetailSingleflight(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._orig_run_sync = h.run_sync
        h.clear_instance_detail_cache()

    def tearDown(self):
        h.run_sync = self._orig_run_sync
        h.clear_instance_detail_cache()

    async def test_detail_singleflight_and_cache(self):
        calls = {"detail": 0, "ip": 0}

        async def fake_run(func, *args, **kwargs):
            await asyncio.sleep(.05)
            if func is h.get_instance:
                calls["detail"] += 1
                return {"data": {"lifecycle-state": "RUNNING"}}
            if func is h.get_public_ip:
                calls["ip"] += 1
                return "203.0.113.4"
            raise AssertionError(func)

        h.run_sync = fake_run
        c = Ctx()
        cli = object()
        vals = await asyncio.gather(*(
            h._get_instance_detail_pair(c, cli, "ocid1", "comp")
            for _ in range(10)))
        self.assertEqual(calls, {"detail": 1, "ip": 1})
        self.assertTrue(all(v[1] == "203.0.113.4" for v in vals))
        await h._get_instance_detail_pair(c, cli, "ocid1", "comp")
        self.assertEqual(calls, {"detail": 1, "ip": 1})


class TestCpuSingleflight(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._orig = {}
        for name in ("run_sync", "get_cpu_model", "cached_cpu_model",
                     "run_remote_command"):
            self._orig[name] = getattr(h, name, None)
        h._CPU_MODEL_TASKS.clear()
        h._CPU_MODEL_COOLDOWN.clear()

    def tearDown(self):
        for name, value in self._orig.items():
            if value is None:
                try:
                    delattr(h, name)
                except AttributeError:
                    pass
            else:
                setattr(h, name, value)
        h._CPU_MODEL_TASKS.clear()
        h._CPU_MODEL_COOLDOWN.clear()

    async def test_cpu_singleflight_and_limit(self):
        calls = {"n": 0, "active": 0, "max": 0}

        async def fake_run(func, *args, **kwargs):
            calls["n"] += 1
            calls["active"] += 1
            calls["max"] = max(calls["max"], calls["active"])
            await asyncio.sleep(.05)
            calls["active"] -= 1
            return "AMD EPYC 7J13"

        h.run_sync = fake_run
        h.get_cpu_model = lambda *a, **k: "AMD EPYC 7J13"
        h.cached_cpu_model = lambda iid: None
        h.run_remote_command = lambda *a, **k: None
        vals = await asyncio.gather(*(
            h._get_cpu_model_deduped("same-id", {}) for _ in range(8)))
        self.assertEqual(calls["n"], 1)
        self.assertTrue(all(v == "AMD EPYC 7J13" for v in vals))


if __name__ == "__main__":
    unittest.main()
