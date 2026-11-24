#!/usr/bin/env python3
"""CPU 筛选规则配置（抢机只留指定 CPU 型号）。

规则存在 tgbot/data/cpu_policy.json（0600），默认只留 7J13 与 7745
（用户 2026-11-23 / 2026-11-24 定：E2 随机分配的 AMD CPU 里，7J13
最好、7745 常见，两者都留，其余释放继续抢；释放时永久删引导卷，
见 oci/grabber.py 的 terminate_instance）。

本模块还提供抢机时用的两个配套工具：
  - combined_boot_keys：开机时把 guard 公钥拼进启动 SSH keys，
    这样新实例里 guard 密钥能登录，CPU 检查才能 SSH 进去查。
  - make_cpu_check_hook：给 Grabber 注入的 cpu_check_hook，SSH 查
    CPU 型号；查不到返回 None（Grabber 收到 None 时暂留实例，
    不把没验明的机器释放掉）。
"""

import contextlib
import json
import os
import subprocess
import tempfile
import time
from collections import namedtuple

from tgbot import config as bot_config
from tgbot.cpu_model import get_cpu_model
from oci.network import wait_for_public_ip

# ================= 配置区（改这里） =================
# 默认允许的 CPU 型号子串（抢机只留含这些子串的 CPU）。
DEFAULT_ALLOWED_CPU_MODELS = ["7J13", "7745"]
# 规则文件（tgbot/data/ 下，0600；不存在/损坏时用默认值）。
POLICY_FILENAME = "cpu_policy.json"
# CPU 检查：等公网 IP 最长 60 秒；SSH 查不到时最多试 10 次、间隔 15 秒
# （新实例从 RUNNING 到 SSH 能登通常要 1-3 分钟）。
CPU_CHECK_IP_WAIT_SECONDS = 60
CPU_CHECK_SSH_ATTEMPTS = 10
CPU_CHECK_SSH_INTERVAL_SECONDS = 15
# =====================================================

# 开机用的 SSH 公钥文件：path 是传给 Grabber 的文件路径，
# guard_included 表示 guard 公钥是否已在该文件里（CPU 检查的前提）。
BootKeys = namedtuple("BootKeys", ["path", "guard_included"])


def policy_path():
    """规则文件的完整路径（每次都从 bot_config 读 DATA_DIR，方便测试替换）。"""
    return os.path.join(bot_config.DATA_DIR, POLICY_FILENAME)


def load_cpu_policy():
    """读规则文件；不存在/损坏/格式不对时返回默认规则。"""
    try:
        with open(policy_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {"allowed_cpu_models": list(DEFAULT_ALLOWED_CPU_MODELS)}
    if not isinstance(data, dict):
        return {"allowed_cpu_models": list(DEFAULT_ALLOWED_CPU_MODELS)}
    models = data.get("allowed_cpu_models")
    if not isinstance(models, list):
        return {"allowed_cpu_models": list(DEFAULT_ALLOWED_CPU_MODELS)}
    models = [str(m).strip() for m in models if str(m).strip()]
    if not models:
        return {"allowed_cpu_models": list(DEFAULT_ALLOWED_CPU_MODELS)}
    return {"allowed_cpu_models": models}


def get_allowed_cpu_models():
    """当前允许的 CPU 型号子串列表（永不为空）。"""
    return load_cpu_policy()["allowed_cpu_models"]


def save_cpu_policy(models):
    """保存允许的 CPU 型号列表（0600）；models 为空或非法时返回 False。"""
    if not isinstance(models, list):
        return False
    models = [str(m).strip() for m in models if str(m).strip()]
    if not models:
        return False
    try:
        os.makedirs(bot_config.DATA_DIR, exist_ok=True)
        path = policy_path()
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"allowed_cpu_models": models}, f, ensure_ascii=False)
        os.chmod(path, 0o600)
    except OSError:
        return False
    return True


def cpu_matches(model, allowed=None):
    """CPU 型号是否在允许列表里（子串匹配；model 为空时不匹配）。"""
    if not model:
        return False
    if allowed is None:
        allowed = get_allowed_cpu_models()
    return any(m in model for m in allowed)


def get_guard_public_key():
    """读 guard 密钥的公钥文本；未配置/读不到时返回 None。

    先读私钥路径 + ".pub"；没有 .pub 文件时用 ssh-keygen -y 从
    私钥推导（私钥无口令才能推导）。
    """
    key_path = bot_config.GUARD_SSH_KEY
    if not key_path:
        return None
    try:
        with open(key_path + ".pub", encoding="utf-8") as f:
            text = f.read().strip()
        if text:
            return text
    except OSError:
        pass
    try:
        proc = subprocess.run(
            ["ssh-keygen", "-y", "-f", key_path],
            capture_output=True, text=True, timeout=10)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except Exception:  # noqa: BLE001 - 推导失败就当没有公钥
        pass
    return None


@contextlib.contextmanager
def combined_boot_keys(user_keys_file, include_guard=True):
    """开机用的 SSH 公钥文件（上下文管理器）。

    include_guard 时把 guard 公钥拼进启动 keys（新实例里 guard
    密钥能登录，CPU 检查才能 SSH 进去）；guard 公钥已在用户 keys
    里时不拼临时文件，直接用原文件。include_guard 为 False 或
    guard 公钥读不到时直接用原文件（guard_included=False）。
    临时文件在退出时删除；原文件绝不改动。
    """
    guard_pubkey = get_guard_public_key() if include_guard else None
    user_text = ""
    if user_keys_file:
        try:
            with open(user_keys_file, encoding="utf-8") as f:
                user_text = f.read()
        except OSError:
            user_text = ""
    if not guard_pubkey or guard_pubkey in user_text:
        yield BootKeys(path=user_keys_file,
                       guard_included=bool(guard_pubkey))
        return
    combined = (user_text.rstrip("\n") + "\n" if user_text.strip() else "")
    combined += guard_pubkey + "\n"
    fd, temp_path = tempfile.mkstemp(prefix="mbot-boot-keys-",
                                     suffix=".pub")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(combined)
        yield BootKeys(path=temp_path, guard_included=True)
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass


def make_cpu_check_hook(cli, run_command,
                        wait_timeout=CPU_CHECK_IP_WAIT_SECONDS,
                        ssh_attempts=CPU_CHECK_SSH_ATTEMPTS,
                        ssh_interval=CPU_CHECK_SSH_INTERVAL_SECONDS):
    """给 Grabber 注入的 cpu_check_hook：SSH 查新实例的 CPU 型号。

    签名 hook(instance_id, public_ip) -> str | None：查到返回型号
    （如 "AMD EPYC 7J13"，同时写进 CPU 型号缓存）；IP 等不到或
    SSH 一直查不到返回 None（Grabber 收到 None 时暂留实例）。
    新实例 SSH 端口是 22（14422 是用户初始化脚本改的，刚开的机器
    还没有）；run_remote_command 自带 22/14422 回退。
    """
    def hook(instance_id, public_ip):
        ip = public_ip
        if not ip:
            ip = wait_for_public_ip(cli, instance_id,
                                    timeout_seconds=wait_timeout)
        if not ip:
            return None
        host = {"host": ip, "port": 22, "username": "root",
                "key_path": bot_config.GUARD_SSH_KEY}
        for _ in range(ssh_attempts):
            model = get_cpu_model(instance_id, host, run_command,
                                  force=True)
            if model:
                return model
            time.sleep(ssh_interval)
        return None
    return hook
