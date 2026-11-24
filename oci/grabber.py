#!/usr/bin/env python3
"""OCI 抢机循环：反复尝试创建实例，直到抢到为止（R-Bot 开源克隆的 oci 模块）。

原创实现，参考 R-Bot（semicons/java_oci_manage）的公开功能说明与
~/workspace/oracle-billing/e2_provision_orchestrator.py 里已验证的流程重写，
不是复制 R-Bot 的代码（R-Bot 是闭源二进制，无源码可抄）。

流程要点（已验证）：
  - 创建用 `oci compute instance launch --no-retry`（OCI CLI 默认内部重试会把
    单次调用拖到约 110 秒，加 --no-retry 后约 5.6 秒）。
  - 创建前先按显示名查已有实例，防某次创建成功但响应丢失导致重复建。
  - 无容量（OutOfCapacityError）时等 interval +/- jitter 再试（默认 25 +/- 5 秒，
    也可在构造时用 min_delay/max_delay 指定区间，见 Grabber.__init__）。
  - 429 限流（RateLimitError）时指数退避：60 -> 120 -> 240 -> 480 -> 600 秒上限。
  - 配额不足（QuotaExceededError）时停下并通知，重试没用，不要无限循环。
  - 抢到后如设了 target_cpu，用 cpu_check_hook 查 CPU 型号；不对就终止实例
    （--preserve-boot-volume false 永久删引导卷）+ 通知 + 继续抢。
  - Flex 形状（如 VM.Standard.A1.Flex）可用 ocpus/memory_gb 指定核数内存，
    以 --shape-config '{"ocpus":N,"memoryInGBs":M}' 传给 launch；固定规格形状
    （如 VM.Standard.E2.1.Micro）永远不传 shape-config。
  - 启动脚本（user_data）写进临时文件后以 --user-data-file 传给 launch；
    任务结束（run() 返回或抛异常）时自动删除临时文件。

OCI CLI 参数探查结论（CLI 3.96.3，`oci compute instance launch --help`）：
  - --shape-config：支持（COMPLEX TYPE，传 JSON 字符串）。
  - --boot-volume-size-in-gbs：支持（INTEGER）。
  - --assign-public-ip：支持（BOOLEAN）。
  - --assign-ipv6：不存在；IPv6 分配的实际参数名是 --assign-ipv6-ip
    （BOOLEAN，子网须支持 IPv6），assign_ipv6=True 时传它。
  - --user-data-file：支持（FILENAME，cloud-init 脚本/配置文件的路径）。

安全说明（重要）：
  - 本模块不读取、不打印、不外传 API 私钥。认证全部由 OCI CLI 自己读
    ~/.oci/config 与 key_file 完成，私钥只在本地用于请求签名。
  - 配置区在文件顶端，真实 OCID 一律用占位符，绝不写进代码。
  - 通知通过 on_event 回调交给调用方（Bark / Telegram 等），模块本身不依赖
    任何通知渠道；SSH 查 CPU 通过 cpu_check_hook 注入，模块本身不依赖 SSH。

用法示例（占位符）：
    from oci.client import OCIClient
    from oci.grabber import Grabber, TARGET_CPU

    cli = OCIClient(profile="DEFAULT", region="ap-singapore-1")
    grabber = Grabber(
        cli, compartment_id="ocid1.tenancy.oc1..xxxxxxxx",
        availability_domain="xxxx:AP-SINGAPORE-1-AD-1",
        shape="VM.Standard.E2.1.Micro",
        image_id="ocid1.image.oc1.ap-singapore-1.xxxxxxxx",
        subnet_id="ocid1.subnet.oc1.ap-singapore-1.xxxxxxxx",
        display_name="my-instance",
        ssh_keys_file="/path/to/keys.pub",
        target_cpu=TARGET_CPU,
        on_event=lambda event, msg: print(event, msg),
    )
    grabber.cpu_check_hook = lambda instance_id, ip: "AMD EPYC 7J13"
    result = grabber.run()
    print(result.instance_id, result.attempts, result.releases)

    A1.Flex 指定核数内存 + 启动脚本的示例：
    grabber = Grabber(
        cli, compartment_id="ocid1.tenancy.oc1..xxxxxxxx",
        availability_domain="xxxx:AP-SINGAPORE-1-AD-1",
        shape="VM.Standard.A1.Flex",
        image_id="ocid1.image.oc1.ap-singapore-1.xxxxxxxx",
        subnet_id="ocid1.subnet.oc1.ap-singapore-1.xxxxxxxx",
        display_name="a1-instance",
        ssh_keys_file="/path/to/keys.pub",
        ocpus=4, memory_gb=24, boot_volume_size_gbs=100,
        assign_public_ip=True, assign_ipv6=False,
        user_data="#!/bin/bash\necho hello",
        min_delay=20, max_delay=50,
    )
"""

import json
import os
import random
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from oci.client import OCIError, OutOfCapacityError, QuotaExceededError, RateLimitError

# ================= 配置区（改这里） =================
# 抢机轮询间隔（秒）：无容量时等这么久再试。API 约 6 秒 + 检查约 2 秒，
# 整轮约 30 秒。
CREATE_INTERVAL = 25
# 抢机抖动范围（秒）：实际间隔为 CREATE_INTERVAL +/- CREATE_JITTER，
# 即 20~30 秒随机，借鉴 R-Bot 的延迟区间做法，避免固定节律被限流。
CREATE_JITTER = 5
# 429 限流指数退避的初始等待（秒），每次翻倍。
BACKOFF_INITIAL = 60
# 429 限流指数退避的上限（秒）。
BACKOFF_MAX = 600
# 默认目标 CPU 型号（E2 抢机用：只留 AMD EPYC 7J13 的）。
TARGET_CPU = "7J13"
# 等公网 IP 分配：最多查多少次、每次间隔多少秒（默认 30 次 x 20 秒 = 10 分钟）。
PUBLIC_IP_MAX_POLLS = 30
PUBLIC_IP_POLL_INTERVAL = 20
# 等实例终止：最多查多少次、每次间隔多少秒（默认 40 次 x 15 秒 = 10 分钟）。
TERMINATE_MAX_POLLS = 40
TERMINATE_POLL_INTERVAL = 15
# 防重复建的时间窗（秒）：同名实例只有在最近这么久内创建的才算
# “上次创建成功但响应丢失”（默认 15 分钟）。实例名改成日期格式
# （AMD_YYYY-MM-DD）后，同名旧实例是之前建的，不能误当新开的。
DEDUP_WINDOW_SECONDS = 900
# 防重复建只认“活着”的实例：已终止 / 停止 / 删除中的同名实例
# 不是“上次创建成功但响应丢失”，复用它会把一台死机当成抢机成功
# 返回（2026-11-24 首尔实测：第二轮验证复用了 15 分钟内被终止的
# 测试机，导致备份创建报“启动卷已是 Terminated”）。状态查不到时
# 按旧行为保守处理（认作活着，不冒险重复建）。
DEDUP_LIVE_STATES = frozenset({"RUNNING", "PROVISIONING", "STARTING"})
# =====================================================


@dataclass
class GrabResult:
    """抢机完成后的结果（含统计，供调用方通知时用，如“共尝试 N 次，总耗时 T”）。"""

    instance_id: str       # 最终保留的实例 OCID
    attempts: int          # 尝试次数（含直接复用同名实例的那次）
    releases: int          # 因 CPU 不对而释放的实例数
    elapsed_seconds: float  # 从 run() 开始到抢到的总耗时（秒）


class GrabCancelledError(RuntimeError):
    """抢机被用户取消（stop_event 置位）。"""


class Grabber:
    """抢机循环：反复 launch，直到抢到符合要求的实例。"""

    def __init__(self, cli, compartment_id, availability_domain, shape,
                 image_id, subnet_id, display_name, ssh_keys_file,
                 target_cpu=None, on_event=None, stop_event=None,
                 ocpus=None, memory_gb=None, boot_volume_size_gbs=None,
                 assign_public_ip=True, assign_ipv6=False, user_data=None,
                 min_delay=None, max_delay=None):
        """参数：
        cli: OCIClient 实例（认证由 OCI CLI 自己处理）。
        compartment_id / availability_domain / shape / image_id / subnet_id:
            创建实例所需的 OCID / AD / 形状（调用方传真实值，不要写进本文件）。
        display_name: 实例显示名；创建前按它查已有实例，防重复建。
        ssh_keys_file: SSH 公钥文件路径（传给 --ssh-authorized-keys-file）。
            None / 空字符串时不传该参数（不用公钥时配合 root 密码登录，
            见 tgbot/boot_form.py 的 root 登录选项；OCI launch 本身不强制
            要求 SSH 公钥）。
        target_cpu: 目标 CPU 型号子串（如 "7J13"），None 表示不查 CPU、抢到就留。
        on_event: 可选回调 on_event(event_type, message)，事件类型：
            "attempt"（每次尝试）、"launched"（抢到）、"wrong_cpu"（CPU 不对已释放）、
            "rate_limited"（429 退避）、"quota"（配额不足停下）、"done"（完成）、
            "cancelled"（被取消）。
        stop_event: 可选 threading.Event，置位后 run() 在下一次循环检查时
            抛 GrabCancelledError 停下（Telegram 任务取消用）。不传则不可取消。
        ocpus / memory_gb: Flex 形状（如 VM.Standard.A1.Flex）的核数与内存
            （GB），两者都传时 launch 加 --shape-config
            '{"ocpus":N,"memoryInGBs":M}'；固定规格形状（如 E2.1.Micro）
            即使传了也忽略，永远不传 shape-config。缺一不可，缺一个就不传。
        boot_volume_size_gbs: 引导卷大小（GB），传了则 launch 加
            --boot-volume-size-in-gbs；None 表示用 OCI 默认值。
        assign_public_ip: 是否分配公网 IP（默认 True），launch 的
            --assign-public-ip 按此值传 "true"/"false"。
        assign_ipv6: 是否分配 IPv6 地址（默认 False）。True 时 launch 加
            --assign-ipv6-ip true（CLI 3.96.3 的实际参数名是
            --assign-ipv6-ip，没有 --assign-ipv6；子网须支持 IPv6）。
        user_data: 启动脚本内容（字符串，cloud-init 脚本）。非空时写进
            临时文件后以 --user-data-file 传给 launch；空字符串/None 时不传。
            临时文件由 run() 结束时自动删除；只调 try_launch_once 时调用方
            可调 cleanup() 手动删除。
        min_delay / max_delay: 抢机延迟区间（秒），两者都传且都 > 0 时
            interval=(min+max)/2、jitter=(max-min)/2；否则用模块常量
            CREATE_INTERVAL / CREATE_JITTER（默认 25 +/- 5 秒）。
            min > max 时抛 ValueError。
        """
        self.cli = cli
        self.compartment_id = compartment_id
        self.availability_domain = availability_domain
        self.shape = shape
        self.image_id = image_id
        self.subnet_id = subnet_id
        self.display_name = display_name
        self.ssh_keys_file = ssh_keys_file
        self.target_cpu = target_cpu
        self.on_event = on_event
        self.stop_event = stop_event
        self.ocpus = ocpus
        self.memory_gb = memory_gb
        self.boot_volume_size_gbs = boot_volume_size_gbs
        self.assign_public_ip = assign_public_ip
        self.assign_ipv6 = assign_ipv6
        self.user_data = user_data
        # 抢机延迟区间：都传且都 > 0 时按区间算，否则用模块常量默认值。
        if min_delay and max_delay:
            if min_delay > max_delay:
                raise ValueError(
                    f"min_delay（{min_delay}）不能大于 max_delay（{max_delay}）")
            self._interval = (min_delay + max_delay) / 2
            self._jitter = (max_delay - min_delay) / 2
        else:
            self._interval = CREATE_INTERVAL
            self._jitter = CREATE_JITTER
        # 启动脚本临时文件路径（惰性创建，见 _ensure_user_data_file）。
        self._user_data_file = None
        # 可注入的 CPU 检查函数，签名 cpu_check_hook(instance_id, public_ip) -> str，
        # 返回 CPU 型号字符串（如 "AMD EPYC 7J13"）。默认 None（不查 CPU）。
        # SSH 查 CPU 的逻辑由调用方提供（编排器里已有实现），模块本身不依赖 SSH。
        self.cpu_check_hook = None

    # ---------------- 内部小工具 ----------------

    def _emit(self, event_type, message):
        """调 on_event 回调（没设回调时静默跳过）。"""
        if self.on_event is not None:
            self.on_event(event_type, message)

    def _next_interval(self):
        """下一次重试前的等待秒数：实例的 interval +/- jitter。

        默认 25 +/- 5 秒；构造时传了 min_delay/max_delay 时按区间算。
        """
        return self._interval + random.uniform(-self._jitter, self._jitter)

    def _ensure_user_data_file(self):
        """user_data 非空时确保临时文件已建好，返回路径；空时返回 None。

        文件只建一次（多次 launch 共用），run() 结束时由 cleanup() 删除；
        cleanup() 后再调会重新创建（run() 可重复调用）。
        """
        if not self.user_data:
            return None
        if self._user_data_file is None:
            with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", prefix="rbot-user-data-",
                    suffix=".sh", delete=False) as f:
                f.write(self.user_data)
                self._user_data_file = f.name
        return self._user_data_file

    def cleanup(self):
        """删除启动脚本临时文件（如果有）。run() 结束时自动调。"""
        if self._user_data_file is not None:
            try:
                os.unlink(self._user_data_file)
            except OSError:
                pass
            self._user_data_file = None

    def _sleep(self, seconds):
        """可中断的等待：stop_event 置位时立即返回 True，否则等满返回 False。

        取消后调用方应在下一次循环检查点抛 GrabCancelledError。
        """
        if self.stop_event is not None:
            return self.stop_event.wait(seconds)
        time.sleep(seconds)
        return False

    def _check_cancelled(self):
        """stop_event 置位时发 cancelled 事件并抛 GrabCancelledError。"""
        if self.stop_event is not None and self.stop_event.is_set():
            self._emit("cancelled", "抢机已取消。")
            raise GrabCancelledError("抢机已被用户取消")

    # ---------------- OCI 查询 ----------------

    def find_existing(self, now=None):
        """按显示名查已有实例，防某次创建成功但响应丢失导致重复建。

        只有在 DEDUP_WINDOW_SECONDS（默认 15 分钟）内创建的同名实例
        才算“上次创建成功但响应丢失”，返回它的 OCID；更早的同名实例
        是之前建的，返回 None（调用方正常新建）。只认活着的实例
        （RUNNING / PROVISIONING / STARTING）：已终止、已停止或删除
        中的同名实例一律不认——复用一台死机会把抢机结果变成一台
        无法使用的实例（2026-11-24 首尔实测教训）。
        查不到创建时间或时间解析失败时按旧行为处理（有同名活着的
        实例就返回 OCID，不冒险重复建）。
        now 供测试注入（aware datetime，未传则取当前 UTC 时间）。
        """
        out = self.cli.run_text(
            ["compute", "instance", "list",
             "--compartment-id", self.compartment_id,
             "--display-name", self.display_name,
             "--query", "data[].{id: id, created: \"time-created\", "
                        "state: \"lifecycle-state\"}"])
        if not out or out.strip() in ("null", "", "[]"):
            return None
        try:
            found = json.loads(out)
        except ValueError:
            return None
        # 兼容旧查询（data[0] 返回单个对象）与新查询（data[] 返回列表）。
        if isinstance(found, dict):
            found = [found]
        if not isinstance(found, list):
            return None
        candidates = []
        for item in found:
            if not isinstance(item, dict):
                continue
            if not item.get("id"):
                continue
            state = (item.get("state") or "").upper()
            # 状态明确且不在活着的集合里：死机，不认。
            if state and state not in DEDUP_LIVE_STATES:
                continue
            candidates.append(item)
        if not candidates:
            return None

        def _created_at(item):
            try:
                created = datetime.fromisoformat(
                    str(item.get("created")).replace("Z", "+00:00"))
            except (ValueError, TypeError):
                return None
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            return created

        # 取最新的一个；创建时间解析不了的排最前（沿用旧行为：不冒险
        # 重复建，直接认作上次创建的实例）。
        dated = [(_created_at(item), item) for item in candidates]
        dated.sort(key=lambda pair: (
            pair[0] is None,
            pair[0] or datetime.min.replace(tzinfo=timezone.utc)))
        created_at, newest = dated[-1]
        if created_at is None:
            return newest.get("id")
        now = now or datetime.now(timezone.utc)
        age = (now - created_at).total_seconds()
        if 0 <= age <= DEDUP_WINDOW_SECONDS:
            return newest.get("id")
        return None

    def get_public_ip(self, instance_id):
        """轮询取实例的公网 IP（刚创建时可能还没分配），超时抛 RuntimeError。

        取消（stop_event 置位）时抛 GrabCancelledError。
        """
        for _ in range(PUBLIC_IP_MAX_POLLS):
            out = self.cli.run_text(
                ["compute", "instance", "list-vnics",
                 "--instance-id", instance_id,
                 "--query", 'data[0]."public-ip"', "--raw-output"])
            if out and out != "null":
                return out
            if self._sleep(PUBLIC_IP_POLL_INTERVAL):
                raise GrabCancelledError("等公网 IP 时抢机被取消")
        raise RuntimeError(f"实例已创建但 {PUBLIC_IP_MAX_POLLS * PUBLIC_IP_POLL_INTERVAL // 60} "
                           f"分钟未取到公网 IP：{instance_id[-4:]}")

    def get_instance_state(self, instance_id):
        """查实例生命周期状态（如 RUNNING / TERMINATED），查不到返回 ""。"""
        try:
            return self.cli.run_text(
                ["compute", "instance", "get", "--instance-id", instance_id,
                 "--query", 'data."lifecycle-state"', "--raw-output"])
        except OCIError:
            return ""

    # ---------------- 创建 / 终止 ----------------

    def _build_launch_args(self):
        """构造 launch 命令参数列表（不含开头的 oci，由 OCIClient 补）。

        可选参数按构造时的值决定是否出现：
          - ssh_keys_file 为 None/空时不传 --ssh-authorized-keys-file
            （不用公钥时配合 root 密码登录）。
          - Flex 形状（shape 以 .Flex 结尾）且 ocpus/memory_gb 都传了：
            --shape-config '{"ocpus":N,"memoryInGBs":M}'；固定规格形状
            （如 E2.1.Micro）永远不传。
          - boot_volume_size_gbs 传了：--boot-volume-size-in-gbs N。
          - --assign-public-ip 按 assign_public_ip 传 "true"/"false"。
          - assign_ipv6=True：--assign-ipv6-ip true（CLI 3.96.3 的实际
            参数名，没有 --assign-ipv6）。
          - user_data 非空：--user-data-file <临时文件路径>。
        """
        args = [
            "compute", "instance", "launch",
            "--compartment-id", self.compartment_id,
            "--availability-domain", self.availability_domain,
            "--shape", self.shape, "--image-id", self.image_id,
            "--subnet-id", self.subnet_id,
            "--assign-public-ip",
            "true" if self.assign_public_ip else "false",
            "--display-name", self.display_name,
        ]
        # 不用公钥时（ssh_keys_file 为 None/空）不传该参数；OCI launch
        # 本身不强制要求 SSH 公钥，配合 root 密码登录即可进实例。
        if self.ssh_keys_file:
            args += ["--ssh-authorized-keys-file", self.ssh_keys_file]
        if (self.shape.endswith(".Flex") and self.ocpus is not None
                and self.memory_gb is not None):
            shape_config = json.dumps({"ocpus": self.ocpus,
                                       "memoryInGBs": self.memory_gb})
            args += ["--shape-config", shape_config]
        if self.boot_volume_size_gbs is not None:
            args += ["--boot-volume-size-in-gbs",
                     str(self.boot_volume_size_gbs)]
        if self.assign_ipv6:
            args += ["--assign-ipv6-ip", "true"]
        user_data_file = self._ensure_user_data_file()
        if user_data_file is not None:
            args += ["--user-data-file", user_data_file]
        args += ["--no-retry", "--query", "data.id", "--raw-output"]
        return args

    def try_launch_once(self):
        """单次创建尝试。

        先查同名实例（find_existing），有就直接返回它的 OCID（不重复建）；
        没有则 launch（--no-retry，避免 OCI CLI 内部重试把单次调用拖到约 110 秒）。
        成功返回实例 OCID；无容量（OutOfCapacityError）返回 None；
        限流（RateLimitError）/ 配额不足（QuotaExceededError）等往上抛，由 run() 处理。
        """
        existing = self.find_existing()
        if existing:
            return existing
        try:
            out = self.cli.run_text(self._build_launch_args())
        except OutOfCapacityError:
            return None
        if out and out.startswith("ocid1.instance"):
            return out
        raise OCIError(f"launch 返回了非预期的输出：{str(out)[:200]}")

    def terminate_instance(self, instance_id):
        """终止实例并永久删除引导卷（--preserve-boot-volume false），等到 TERMINATED。

        注意：引导卷永久删除，不留着扣钱；删除后无法恢复。
        """
        self.cli.run(
            ["compute", "instance", "terminate", "--instance-id", instance_id,
             "--preserve-boot-volume", "false", "--force"])
        for _ in range(TERMINATE_MAX_POLLS):
            if self.get_instance_state(instance_id) in ("TERMINATED", ""):
                return
            time.sleep(TERMINATE_POLL_INTERVAL)
        raise RuntimeError(f"终止实例后 {TERMINATE_MAX_POLLS * TERMINATE_POLL_INTERVAL // 60} "
                           f"分钟未到 TERMINATED 状态：{instance_id[-4:]}")

    # ---------------- 主循环 ----------------

    def run(self, max_attempts=None):
        """抢机主循环，直到抢到符合要求的实例，返回 GrabResult（含统计）。

        max_attempts: 最多尝试次数（None 表示无限抢，直到抢到或配额不足停下）。
        达到 max_attempts 仍没抢到时抛 RuntimeError。
        配额不足时调 on_event("quota") 后抛 QuotaExceededError 停下。
        无论正常返回还是抛异常，结束时都删除启动脚本临时文件（cleanup）。
        """
        try:
            return self._run_loop(max_attempts)
        finally:
            self.cleanup()

    def _run_loop(self, max_attempts):
        """run() 的循环体（抽出来以便 run() 用 try/finally 做清理）。"""
        started = time.monotonic()
        attempts = 0
        releases = 0
        backoff = BACKOFF_INITIAL
        while True:
            self._check_cancelled()
            if max_attempts is not None and attempts >= max_attempts:
                raise RuntimeError(
                    f"达到最大尝试次数 {max_attempts}，仍未抢到 {self.shape} 实例")
            attempts += 1
            self._emit("attempt",
                       f"第 {attempts} 次尝试创建 {self.shape}（{self.display_name}）...")
            try:
                instance_id = self.try_launch_once()
            except RateLimitError as exc:
                # 429 限流：指数退避，不要硬刷。
                self._emit("rate_limited",
                           f"遇到 429 限流，退避 {backoff} 秒后再试。{exc}")
                self._sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)
                continue
            except QuotaExceededError as exc:
                # 配额不足（不是缺货）：重试没用，通知后停下。
                self._emit("quota", f"配额不足，停止抢机：{exc}")
                raise
            backoff = BACKOFF_INITIAL  # 非 429 结果则重置退避
            if instance_id is None:
                # 无容量：等 interval +/- jitter 再试。
                wait = self._next_interval()
                self._emit("attempt",
                           f"第 {attempts} 次失败（无容量），{wait:.0f} 秒后重试。")
                self._sleep(wait)
                continue
            self._emit("launched", f"抢到了！实例 {instance_id[-4:]}")
            if self.target_cpu and self.cpu_check_hook is not None:
                public_ip = self.get_public_ip(instance_id)
                cpu = self.cpu_check_hook(instance_id, public_ip)
                if self.target_cpu not in cpu:
                    self._emit("wrong_cpu",
                               f"CPU 是 {cpu}（不是 {self.target_cpu}），"
                               f"释放实例并永久删除引导卷，继续抢。")
                    self.terminate_instance(instance_id)
                    releases += 1
                    continue
            elapsed = time.monotonic() - started
            self._emit("done",
                       f"抢机完成：实例 {instance_id[-4:]}，共尝试 {attempts} 次，"
                       f"累计释放 {releases} 台 CPU 不符，总耗时 {elapsed:.0f} 秒。")
            return GrabResult(instance_id=instance_id, attempts=attempts,
                              releases=releases, elapsed_seconds=elapsed)
