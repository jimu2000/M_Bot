#!/usr/bin/env python3
"""DD 重装 + 初始化流程测试（oci/dd_init.py 与 boot_form 的 DD 分支）。

SSH 全 mock，不连真实机器。密码只用测试用的假值，绝不出现真实凭据。
"""

import io
import unittest
from unittest import mock

from oci.dd_init import (
    DD_SSH_PORT,
    INIT_DONE_PATH,
    INIT_LOG_PATH,
    INIT_SCRIPT_PATH,
    ROOT_LOGIN_CONF,
    build_dd_script,
    run_dd_init,
    run_init_script,
    verify_password_login,
    wait_for_dd,
)
from tgbot.boot_form import (
    build_preset,
    build_user_data,
    new_form,
)

DEBIAN13_OS_RELEASE = (
    'PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\n'
    'NAME="Debian GNU/Linux"\n'
    'VERSION_ID="13"\n'
    'VERSION="13 (trixie)"\n'
    'ID=debian\n'
)

UBUNTU_OS_RELEASE = (
    'PRETTY_NAME="Ubuntu 24.04.5 LTS"\n'
    'NAME="Ubuntu"\n'
    'VERSION_ID="24.04"\n'
    'ID=ubuntu\n'
)


class _FakeStdout:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text.encode("utf-8")


class _FakeSFTPFile:
    def __init__(self, store, path):
        self._store = store
        self._path = path
        self._buf = io.StringIO()

    def write(self, data):
        self._buf.write(data)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._store[self._path] = self._buf.getvalue()
        return False


class _FakeSFTP:
    def __init__(self, store):
        self.store = store
        self.chmod_calls = []

    def file(self, path, mode):
        return _FakeSFTPFile(self.store, path)

    def chmod(self, path, mode):
        self.chmod_calls.append((path, mode))

    def close(self):
        pass


class _FakeSSHClient:
    """Paramiko SSHClient 的替身：exec_command 按命令返回预设输出。"""

    def __init__(self, outputs=None, sftp_store=None):
        # outputs: {命令前缀: (stdout, stderr)}
        self.outputs = outputs or {}
        self.commands = []
        self.sftp = _FakeSFTP(sftp_store if sftp_store is not None else {})
        self.closed = False

    def exec_command(self, command, timeout=None):
        self.commands.append(command)
        for prefix, (out, err) in self.outputs.items():
            if command.startswith(prefix):
                return None, _FakeStdout(out), _FakeStdout(err)
        return None, _FakeStdout(""), _FakeStdout("")

    def open_sftp(self):
        return self.sftp

    def close(self):
        self.closed = True


class TestBuildDdScript(unittest.TestCase):
    def test_dd_script_contains_installnet_command(self):
        pwd = "a" * 32  # 测试用假密码
        script = build_dd_script(pwd)
        self.assertTrue(script.startswith("#!/bin/bash"))
        self.assertIn("InstallNET.sh", script)
        self.assertIn("-debian 13", script)
        self.assertIn('-port "14422"', script)
        self.assertIn(f"-pwd '{pwd}'", script)
        self.assertIn('-swap "1024"', script)
        self.assertIn("leitbogioro", script)

    def test_dd_script_uses_given_password_only(self):
        # 密码是调用方传进来的参数；模块里没有任何硬编码密码。
        script = build_dd_script("test-password-123")
        self.assertIn("test-password-123", script)
        # 换一个密码，脚本里的密码也跟着换（证明没有写死）。
        script2 = build_dd_script("another-password-456")
        self.assertIn("another-password-456", script2)
        self.assertNotIn("test-password-123", script2)

    def test_dd_script_reboots_unconditionally(self):
        """InstallNET.sh 上游结尾只打印让手动输 reboot 就 exit 1，
        不自动重启；生成的脚本必须在它后面无条件 reboot，否则 DD
        永远等不到新系统而 30 分钟超时。"""
        pwd = "d" * 32
        script = build_dd_script(pwd)
        # reboot 另起一行，不在 && 链里（InstallNET.sh 正常走完
        # 也 exit 1，接链里永远跑不到）。
        self.assertIn("\nreboot\n", script)
        installnet_pos = script.index("bash InstallNET.sh")
        reboot_pos = script.index("\nreboot\n")
        self.assertGreater(reboot_pos, installnet_pos)
        self.assertNotIn("&& reboot", script[installnet_pos:reboot_pos])


class TestBuildUserDataDd(unittest.TestCase):
    def test_dd_returns_dd_script_not_startup_script(self):
        pwd = "b" * 32
        init = "#!/bin/bash\necho init\n"
        user_data = build_user_data(pwd, init, dd=True)
        self.assertIn("InstallNET.sh", user_data)
        self.assertIn("-debian 13", user_data)
        # 初始化脚本不拼进 user-data（DD 后经 SSH 执行）
        self.assertNotIn("echo init", user_data)

    def test_dd_requires_root_password(self):
        with self.assertRaises(ValueError):
            build_user_data(None, "#!/bin/bash\necho x\n", dd=True)

    def test_no_dd_unchanged(self):
        pwd = "c" * 32
        user_data = build_user_data(pwd, "#!/bin/bash\necho hi\n", dd=False)
        self.assertIn("echo hi", user_data)
        self.assertNotIn("InstallNET.sh", user_data)


class TestBuildPresetDd(unittest.TestCase):
    FAKE_DEPLOY = {
        "availability_domain": "wgwP:AP-SINGAPORE-1-AD-1",
        "subnet_id": "ocid1.subnet.oc1.ap-singapore-1.ssssss",
        "ssh_keys_file": "/opt/rbot-clone/web/data/ssh_keys.pub",
        "image_aarch64": "ocid1.image.oc1.ap-singapore-1.aaa",
        "image_x86_64": "ocid1.image.oc1.ap-singapore-1.bbb",
        "ol_aarch64": "ocid1.image.oc1.ap-singapore-1.ol111",
        "ol_x86_64": "ocid1.image.oc1.ap-singapore-1.ol222",
    }

    def test_preset_carries_dd_flag(self):
        form = new_form()
        form.update({"cpu": "amd", "os": "ubuntu", "dd": True,
                     "root_login": True})
        preset = build_preset(form, deploy=self.FAKE_DEPLOY)
        self.assertTrue(preset["dd"])

    def test_preset_dd_defaults_off(self):
        form = new_form()
        form.update({"cpu": "amd", "os": "ubuntu"})
        preset = build_preset(form, deploy=self.FAKE_DEPLOY)
        self.assertFalse(preset["dd"])

    def test_dd_requires_root_login(self):
        form = new_form()
        form.update({"cpu": "amd", "os": "ubuntu", "dd": True,
                     "root_login": False})
        with self.assertRaises(ValueError):
            build_preset(form, deploy=self.FAKE_DEPLOY)


class TestWaitForDd(unittest.TestCase):
    def test_debian13_detected(self):
        client = _FakeSSHClient(
            {"cat /etc/os-release": (DEBIAN13_OS_RELEASE, "")})
        with mock.patch("oci.dd_init._ssh_connect",
                        return_value=client):
            self.assertTrue(wait_for_dd("1.2.3.4", "pw",
                                        sleep=lambda s: None))

    def test_ubuntu_not_counted_as_dd_done(self):
        client = _FakeSSHClient(
            {"cat /etc/os-release": (UBUNTU_OS_RELEASE, "")})
        with mock.patch("oci.dd_init._ssh_connect",
                        return_value=client):
            self.assertFalse(wait_for_dd("1.2.3.4", "pw", timeout=0.05,
                                         poll_interval=0.01,
                                         sleep=lambda s: None))

    def test_ssh_unreachable_times_out(self):
        with mock.patch("oci.dd_init._ssh_connect",
                        side_effect=Exception("no route")):
            self.assertFalse(wait_for_dd("1.2.3.4", "pw", timeout=0.05,
                                         poll_interval=0.01,
                                         sleep=lambda s: None))


class TestRunInitScript(unittest.TestCase):
    def test_init_script_uploaded_and_run(self):
        store = {}
        client = _FakeSSHClient({
            f"test -f {INIT_DONE_PATH}": ("0\n", ""),
            f"tail -n 50 {INIT_LOG_PATH}": ("done ok\n", ""),
        }, sftp_store=store)
        with mock.patch("oci.dd_init._ssh_connect",
                        return_value=client):
            ok, code, log = run_init_script(
                "1.2.3.4", "pw", "#!/bin/bash\necho init\n",
                sleep=lambda s: None)
        self.assertTrue(ok)
        self.assertEqual(code, 0)
        self.assertIn("done ok", log)
        # 脚本已传到实例上且给了执行权限
        self.assertEqual(store[INIT_SCRIPT_PATH], "#!/bin/bash\necho init\n")
        self.assertIn((INIT_SCRIPT_PATH, 0o755), client.sftp.chmod_calls)
        # 以 nohup 后台跑
        self.assertTrue(any("nohup" in c for c in client.commands))

    def test_init_script_reenables_password_login(self):
        # 脚本跑完后强制开回密码登录（不管用户脚本是否关了它），
        # 完成标记仍取用户脚本的退出码。
        store = {}
        client = _FakeSSHClient({
            f"test -f {INIT_DONE_PATH}": ("0\n", ""),
            f"tail -n 50 {INIT_LOG_PATH}": ("done ok\n", ""),
        }, sftp_store=store)
        with mock.patch("oci.dd_init._ssh_connect",
                        return_value=client):
            ok, code, log = run_init_script(
                "1.2.3.4", "pw", "#!/bin/bash\necho init\n",
                sleep=lambda s: None)
        self.assertTrue(ok)
        self.assertEqual(code, 0)
        cmd = "\n".join(client.commands)
        self.assertIn("PasswordAuthentication yes", cmd)
        self.assertIn("PermitRootLogin yes", cmd)
        self.assertIn(ROOT_LOGIN_CONF, cmd)
        self.assertIn(f"echo $CODE > {INIT_DONE_PATH}", cmd)

    def test_init_script_nonzero_exit(self):
        client = _FakeSSHClient({
            f"test -f {INIT_DONE_PATH}": ("3\n", ""),
            f"tail -n 50 {INIT_LOG_PATH}": ("failed here\n", ""),
        })
        with mock.patch("oci.dd_init._ssh_connect",
                        return_value=client):
            ok, code, log = run_init_script(
                "1.2.3.4", "pw", "#!/bin/bash\nexit 3\n",
                sleep=lambda s: None)
        self.assertFalse(ok)
        self.assertEqual(code, 3)
        self.assertIn("failed here", log)

    def test_init_script_timeout(self):
        client = _FakeSSHClient(
            {f"test -f {INIT_DONE_PATH}": ("", "")})
        with mock.patch("oci.dd_init._ssh_connect",
                        return_value=client):
            ok, code, log = run_init_script(
                "1.2.3.4", "pw", "#!/bin/bash\necho x\n",
                timeout=0.05, poll_interval=0.01,
                sleep=lambda s: None)
        self.assertFalse(ok)
        self.assertIsNone(code)


class TestVerifyPasswordLogin(unittest.TestCase):
    def test_verify_ok(self):
        client = _FakeSSHClient({
            "grep -E": ("ID=debian\nVERSION_ID=\"13\"\nactive\n"
                        "PasswordAuthentication yes\n", ""),
        })
        with mock.patch("oci.dd_init._ssh_connect",
                        return_value=client):
            ok, detail = verify_password_login("1.2.3.4", "pw")
        self.assertTrue(ok)
        self.assertIn("已验证可用", detail)

    def test_verify_ssh_login_fails(self):
        with mock.patch("oci.dd_init._ssh_connect",
                        side_effect=Exception("no route")):
            ok, detail = verify_password_login("1.2.3.4", "pw")
        self.assertFalse(ok)
        self.assertIn("登录失败", detail)

    def test_verify_status_mismatch(self):
        # 密码能登，但 drop-in 里没有 PasswordAuthentication yes。
        client = _FakeSSHClient({
            "grep -E": ("ID=debian\nVERSION_ID=\"13\"\nactive\n", ""),
        })
        with mock.patch("oci.dd_init._ssh_connect",
                        return_value=client):
            ok, detail = verify_password_login("1.2.3.4", "pw")
        self.assertFalse(ok)
        self.assertIn("状态不符", detail)


class TestRunDdInit(unittest.TestCase):
    def test_full_flow_emits_events(self):
        events = []
        with mock.patch("oci.dd_init.wait_for_dd",
                        return_value=True), \
                mock.patch("oci.dd_init.run_init_script",
                           return_value=(True, 0, "ok")), \
                mock.patch("oci.dd_init.verify_password_login",
                           return_value=(True, "root 密码登录已验证可用，"
                                               "sshd 运行中。")):
            result = run_dd_init("1.2.3.4", "pw", "#!/bin/bash\necho x\n",
                                 instance_name="AMD_2026-11-26",
                                 on_event=lambda t, m: events.append(t))
        self.assertTrue(result)
        self.assertIn("dd_done", events)
        self.assertIn("init_done", events)

    def test_full_flow_verify_failed_still_init_done(self):
        # 脚本已成功，验证失败不改 init 结果，但 init_done 里要带提醒。
        events = []
        messages = []
        with mock.patch("oci.dd_init.wait_for_dd",
                        return_value=True), \
                mock.patch("oci.dd_init.run_init_script",
                           return_value=(True, 0, "ok")), \
                mock.patch("oci.dd_init.verify_password_login",
                           return_value=(False, "密码 SSH 登录失败：no route")):
            result = run_dd_init("1.2.3.4", "pw", "#!/bin/bash\necho x\n",
                                 instance_name="AMD_2026-11-26",
                                 on_event=lambda t, m: (events.append(t),
                                                        messages.append(m)))
        self.assertTrue(result)
        self.assertIn("init_done", events)
        self.assertIn("验证失败", " ".join(messages))

    def test_dd_timeout_reports_failure(self):
        events = []
        with mock.patch("oci.dd_init.wait_for_dd",
                        return_value=False):
            result = run_dd_init("1.2.3.4", "pw", "#!/bin/bash\necho x\n",
                                 instance_name="AMD_2026-11-26",
                                 on_event=lambda t, m: events.append(t))
        self.assertFalse(result)
        self.assertIn("dd_timeout", events)


if __name__ == "__main__":
    unittest.main()
