#!/usr/bin/env python3
"""Telegram 机器人菜单 1（开机）的 M_Bot 风格按钮矩阵创建表单。

来源：实测记录「1. 开机」一节的实测记录。
表单每行给预设值 + 自定义，点一下打 ✅ 选中，再点取消；
延迟选一个 = 固定间隔，选两个 = 区间随机延迟；批量 vps 数；
root 密码随机选项。保留我方快捷开机预设（菜单 17 不动）。

本模块是纯逻辑（状态机 / 映射 / 校验 / user-data 生成 / 键盘渲染），
不直接调 Telegram API、不碰 OCI，单元测试不用 mock 就能跑。
async handler 在 tgbot/handlers_extra.py 里（boot_form_callback 等），
本模块只被它们调用。

表单状态（context.user_data["boot_form"]，普通 dict）：
  cpu: None | "arm" | "amd" | "intel" | 自定义形状名字符串
  os: None | "ubuntu" | "centos" | 自定义镜像 OCID 字符串
  ocpus: None | int；memory_gb: None | int
  disk_gb: None（默认）| int
  count: int | None（默认 1）
  delays: list[int]（0–2 个）
  ssh_key: "use"（默认）| "none"
  root_login: bool（默认 True：用户要求建完实例一定能 root 登录）
  public_ip: bool（默认 True）
  ipv6: bool（默认 False）
  script: None | {"name": str, "content": str}

实例名（用户 2026-11-24 定）：E2（AMD 免费）-> AMD_YYYY-MM-DD，
A1（ARM）-> ARM_YYYY-MM-DD（北京时间日期）；其他形状沿用旧格式。
表单顶部有一键开机（默认 AMD）按钮（bf:oneclick:amd），点后进确认页。

映射关系（取舍见 build_preset / resolve_* 的注释）：
  - CPU：ARM → VM.Standard.A1.Flex；AMD / INTEL → VM.Standard.E2.1.Micro
    （E2 是 AMD 芯片，免费层没有 Intel 机型，选 INTEL 时提示并按 E2 创建）。
  - 系统：Ubuntu → Canonical Ubuntu 24.04（按架构选 IMAGE_IDS）；
    CentOS → Oracle Linux（OCI 已无 CentOS 平台镜像，界面上注明，
    镜像 OCID 走环境变量 MBOT_IMAGE_OL_AARCH64 / MBOT_IMAGE_OL_X86_64）。
  - 不做"传输加密"和"强开ARM"（客户端专属功能，本项目无对应实现，
    不加假开关）。

安全说明：
  - 本模块不读取、不打印、不外传 API 私钥；镜像/子网/AD/公钥文件
    全部走环境变量，代码里只有占位符。
  - root 默认密码是 UUID4 的 32 位十六进制（只含 [0-9a-f]，没有
    YAML 与 chpasswd 的特殊字符）；密码只在确认页、启动消息与
    成功通知里显示，绝不写进 presets.json / 日志 / 任务详情（见
    handlers_extra 的 boot_go_callback 与 _run_grabber_thread）。
"""

import os
import re
import uuid
from datetime import datetime, timedelta, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from oci.grabber import TARGET_CPU
from web.config import IMAGE_IDS

BEIJING_TZ = timezone(timedelta(hours=8))

# ================= 配置区（改这里） =================
# 形状名。
SHAPE_A1_FLEX = "VM.Standard.A1.Flex"
SHAPE_E2_MICRO = "VM.Standard.E2.1.Micro"
# CPU 行预设：(callback 值, 按钮文字)。
CPU_OPTIONS = [("arm", "ARM"), ("amd", "AMD"), ("intel", "INTEL")]
# 系统行预设：(callback 值, 按钮文字)。CentOS 显示带 *，正文里注明
# 已映射为 Oracle Linux。
OS_OPTIONS = [("ubuntu", "Ubuntu"), ("centos", "CentOS*")]
# 核心 / 内存 / 硬盘 / 数量 / 延迟各行的预设值。
OCPU_OPTIONS = [1, 2, 4]  # A1 免费额度内单台上限 4 核
MEMORY_OPTIONS = [1, 6, 12, 24]  # A1 免费额度内单台上限 24GB
DISK_OPTIONS = [100, 150, 200]
COUNT_OPTIONS = [1, 2, 3, 4]
DELAY_OPTIONS = [1, 5, 10, 20, 40, 60, 100, 120, 150, 200]
# 延迟行最多同时选几个值（1 个 = 固定间隔，2 个 = 区间随机）。
DELAY_MAX_SELECTED = 2
# 自定义值校验范围（与 Web 创建表单的硬盘 47–200 一致）。
DISK_MIN_GB = 50  # 显式指定启动盘大小时 OCI 要求至少 50GB（不指定时用镜像默认 47GB）
DISK_MAX_GB = 200
COUNT_MIN = 1
COUNT_MAX = 10
OCPU_MIN = 1
OCPU_MAX = 80
MEMORY_MIN_GB = 1
MEMORY_MAX_GB = 1024
DELAY_MIN_SECONDS = 1
DELAY_MAX_SECONDS = 3600
# 开机脚本上限（字符数；粘贴文本与上传文件共用）。
SCRIPT_MAX_CHARS = 65536
# root 默认密码：UUID4 的 32 位十六进制（去掉连字符），与 R 探长
# 开机通知里的密码格式一致（32 位 hex）。只含 [0-9a-f]，没有冒号
# （chpasswd 按第一个冒号切分用户和密码）、没有 YAML/shell 特殊
# 字符，可直接放进 cloud-init 的 chpasswd 块；128 位熵，强度足够。
# 用户要求（2026-11-24）：抢机默认生成 UUID 当 root 密码。
# Oracle Linux 镜像 OCID（CentOS 映射目标）：按架构从环境变量读，
# 未配置时选 CentOS 会在确认页报错并提示环境变量名。
# 查法：oci compute image list --compartment-id <compartment OCID>
# --operating-system "Oracle Linux" --shape <形状>。
OL_IMAGE_IDS = {
    "aarch64": os.environ.get(
        "MBOT_IMAGE_OL_AARCH64", "ocid1.image.oc1..xxxxxxxx"),
    "x86_64": os.environ.get(
        "MBOT_IMAGE_OL_X86_64", "ocid1.image.oc1..xxxxxxxx"),
}
# 各类占位符（未配置部署值时校验报错用，与 web/tasks.py 的
# PLAN_DEFAULTS / web/config.py 的 IMAGE_IDS 保持一致）。
IMAGE_PLACEHOLDER = "ocid1.image.oc1..xxxxxxxx"
SUBNET_PLACEHOLDER = "ocid1.subnet.oc1..xxxxxxxx"
AD_PLACEHOLDER = "xxxx:AP-SINGAPORE-1-AD-1"
SSH_KEYS_PLACEHOLDER = "/path/to/keys.pub"
# 开机脚本在实例里的落盘路径（root 密码 + 脚本同时存在时，
# 脚本经 cloud-init write_files 写到这里再执行）。
STARTUP_SCRIPT_PATH = "/root/mbot-startup.sh"
# =====================================================

# callback 行名 -> 表单键名（行名是 callback_data 里的短名）。
ROW_FORM_KEY = {
    "cpu": "cpu",
    "os": "os",
    "ocpus": "ocpus",
    "mem": "memory_gb",
    "disk": "disk_gb",
    "count": "count",
    "delay": "delays",
    "key": "ssh_key",
    "root": "root_login",
    "pubip": "public_ip",
    "ipv6": "ipv6",
    "script": "script",
}
# 单选行（点已选值 = 取消选择）。
_SINGLE_ROWS = ("cpu", "os", "ocpus", "mem", "disk", "count")
# 二值切换行（点当前值 = 无变化，点另一值 = 切换）。
_BINARY_ROWS = {
    "key": {"use": "use", "none": "none"},
    "root": {"off": False, "on": True},
    "pubip": {"yes": True, "no": False},
    "ipv6": {"no": False, "yes": True},
}
# 形状名校验：VM. / BM. 开头，点号分隔的段（例如 VM.Standard.A1.Flex）。
_SHAPE_RE = re.compile(r"^(VM|BM)\.[A-Za-z0-9]+(\.[A-Za-z0-9_-]+)+$")


def get_deploy_defaults(region=None, profile=None):
    """部署值（可用域 / 子网 / SSH 公钥文件 + 各架构镜像 OCID）。

    与 web/tasks.py 的 PLAN_DEFAULTS 用同一批环境变量，部署时只用配一次：
      MBOT_PLAN_AD / MBOT_PLAN_SUBNET_ID / MBOT_PLAN_SSH_KEYS_FILE。
    传了 region + profile 且 region_plans.json 里有该 (profile, region)
    配置时，可用域 / 子网 / 镜像用该区域的；否则用环境变量的默认部署值
    （主区）。profile 未传时不查区域配置（子网是按租户的，不能跨账号用）。
    """
    defaults = {
        "availability_domain": os.environ.get(
            "MBOT_PLAN_AD", AD_PLACEHOLDER),
        "subnet_id": os.environ.get(
            "MBOT_PLAN_SUBNET_ID", SUBNET_PLACEHOLDER),
        "ssh_keys_file": os.environ.get(
            "MBOT_PLAN_SSH_KEYS_FILE", SSH_KEYS_PLACEHOLDER),
        "image_aarch64": IMAGE_IDS.get("aarch64") or IMAGE_PLACEHOLDER,
        "image_x86_64": IMAGE_IDS.get("x86_64") or IMAGE_PLACEHOLDER,
        "ol_aarch64": OL_IMAGE_IDS.get("aarch64") or IMAGE_PLACEHOLDER,
        "ol_x86_64": OL_IMAGE_IDS.get("x86_64") or IMAGE_PLACEHOLDER,
    }
    if region and profile:
        from tgbot.config import get_region_plan
        plan = get_region_plan(profile, region)
        for key in ("availability_domain", "subnet_id", "image_aarch64",
                    "image_x86_64", "ol_aarch64", "ol_x86_64"):
            if plan.get(key):
                defaults[key] = plan[key]
    return defaults


# ---------------- 表单状态 ----------------

def new_form():
    """新建一个空表单（带 M_Bot 风格的默认值：公网 IP 有、IPv6 无、

    脚本无、公钥用机器人公钥、root 默认开启（用户要求建完实例一定能
    root 登录）、数量 1 台）。"""
    return {
        "cpu": None,
        "os": None,
        "ocpus": None,
        "memory_gb": None,
        "disk_gb": None,
        "count": 1,
        "delays": [],
        "ssh_key": "use",
        "root_login": True,
        "public_ip": True,
        "ipv6": False,
        "script": None,
    }


def default_amd_form():
    """一键开机（默认 AMD）的预填表单：E2.1.Micro + Ubuntu + 1 台 +

    镜像默认盘（47GB）+ 公钥 + root 登录开 + 公网 IP 有 + IPv6 关。
    名字由 build_preset 按规则生成（AMD_YYYY-MM-DD）。
    """
    form = new_form()
    form.update({"cpu": "amd", "os": "ubuntu"})
    return form


def _parse_int(raw, what):
    """把 callback / 输入的整数字符串转 int，失败返回 (None, 错误文案)。"""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None, f"{what}应为整数"
    return value, None


def toggle_option(form, row, raw):
    """点选切换：点一下选中，再点取消；延迟行可同时选两个。

    row 是 callback 行名（cpu/os/ocpus/mem/disk/count/delay/key/root/
    pubip/ipv6/script），raw 是 callback 里的原始字符串值。
    返回 (changed, notice)：changed 表示表单是否变了（没变时调用方
    可以不刷新消息）；notice 非空时调用方用弹窗提示用户。
    """
    if row not in ROW_FORM_KEY:
        return False, "未知的表单行"
    key = ROW_FORM_KEY[row]

    if row == "delay":
        value, error = _parse_int(raw, "延迟")
        if error is not None or not (DELAY_MIN_SECONDS <= value <= DELAY_MAX_SECONDS):
            return False, (f"延迟应为 {DELAY_MIN_SECONDS}–"
                           f"{DELAY_MAX_SECONDS} 的整数（秒）")
        delays = form["delays"]
        if value in delays:
            delays.remove(value)
            return True, None
        if len(delays) >= DELAY_MAX_SELECTED:
            return False, "延迟最多选两个，先点已选的取消再选。"
        delays.append(value)
        return True, None

    if row == "script":
        # 脚本行只有"无"走切换（"上传"走 pending_input，不进这里）。
        if raw != "none":
            return False, "无效的选项"
        if form["script"] is None:
            return False, None
        form["script"] = None
        return True, None

    if row in _BINARY_ROWS:
        choices = _BINARY_ROWS[row]
        if raw not in choices:
            return False, "无效的选项"
        if form[key] == choices[raw]:
            return False, None
        form[key] = choices[raw]
        return True, None

    # 单选行：点已选值 = 取消选择。
    if row in ("cpu", "os"):
        allowed = dict(CPU_OPTIONS if row == "cpu" else OS_OPTIONS)
        if raw not in allowed:
            return False, "无效的选项"
        value = raw
    elif row == "disk" and raw == "default":
        # 点"默认"：已是默认时无变化，否则切回默认。
        if form[key] is None:
            return False, None
        form[key] = None
        return True, None
    else:
        value, error = _parse_int(raw, "选项")
        if error is not None:
            return False, "无效的选项"
    if form[key] == value:
        form[key] = None
        return True, None
    form[key] = value
    return True, None


def set_custom(form, row, text):
    """自定义输入：校验后写入表单。返回 (ok, error)，ok 为 False 时
    error 是中文错误文案，表单不变。row 是 callback 行名
    （cpu/os/ocpus/mem/disk/count/delay）。"""
    text = (text or "").strip()
    if row == "cpu":
        if not _SHAPE_RE.match(text):
            return False, "形状名格式不对（例如 VM.Standard.A1.Flex）"
        form["cpu"] = text
        return True, None
    if row == "os":
        if not text.startswith("ocid1.image.oc1.") or len(text) < 30:
            return False, "镜像 OCID 格式不对，应以 ocid1.image.oc1. 开头"
        form["os"] = text
        return True, None
    if row == "delay":
        value, error = _parse_int(text, "延迟")
        if error is not None or not (DELAY_MIN_SECONDS <= value <= DELAY_MAX_SECONDS):
            return False, (f"延迟应为 {DELAY_MIN_SECONDS}–"
                           f"{DELAY_MAX_SECONDS} 的整数（秒）")
        if value in form["delays"]:
            return True, None
        if len(form["delays"]) >= DELAY_MAX_SELECTED:
            return False, "延迟最多选两个，先取消一个再添加。"
        form["delays"].append(value)
        return True, None
    ranges = {
        "ocpus": (OCPU_MIN, OCPU_MAX, "核心数"),
        "mem": (MEMORY_MIN_GB, MEMORY_MAX_GB, "内存（GB）"),
        "disk": (DISK_MIN_GB, DISK_MAX_GB, "硬盘（GB）"),
        "count": (COUNT_MIN, COUNT_MAX, "数量"),
    }
    if row not in ranges:
        return False, "未知的表单行"
    low, high, what = ranges[row]
    value, error = _parse_int(text, what)
    if error is not None or not (low <= value <= high):
        return False, f"{what}应为 {low}–{high} 的整数"
    form[ROW_FORM_KEY[row]] = value
    return True, None


def set_script(form, content, name=None):
    """设置开机脚本（粘贴文本或上传文件的内容）。"""
    form["script"] = {"name": name or "开机脚本", "content": content}


# ---------------- 映射解析 ----------------

def resolve_shape(form):
    """CPU 类型 -> OCI 形状名。返回 (shape, note)，shape 为 None 表示未选。

    INTEL 没有免费机型：提示后按 E2.1.Micro（AMD）创建。
    """
    cpu = form.get("cpu")
    if cpu is None:
        return None, None
    if cpu == "arm":
        return SHAPE_A1_FLEX, None
    if cpu == "amd":
        return SHAPE_E2_MICRO, None
    if cpu == "intel":
        return SHAPE_E2_MICRO, "免费层无 Intel 机型，已按 E2.1.Micro（AMD）创建"
    return cpu, None  # 自定义形状名


def resolve_arch(form):
    """CPU 类型 -> 镜像架构。自定义形状无法从名字判断架构，默认 x86_64
    （免费 ARM 形状只有 A1.Flex，已由 ARM 选项覆盖）。"""
    return "aarch64" if form.get("cpu") == "arm" else "x86_64"


def resolve_image_id(form, deploy=None):
    """系统 -> 镜像 OCID。返回 (image_id, error)，error 非空时 image_id

    为 None。Ubuntu 按架构取 deploy 里的 image_*；CentOS 取 Oracle Linux
    镜像（ol_*）；自定义直接用输入的 OCID。deploy 未传时用默认部署值。
    """
    os_value = form.get("os")
    if os_value is None:
        return None, "未选系统"
    deploy = deploy or get_deploy_defaults()
    arch = resolve_arch(form)
    if os_value == "ubuntu":
        image_id = deploy.get(f"image_{arch}") or IMAGE_PLACEHOLDER
        if image_id == IMAGE_PLACEHOLDER:
            env = ("MBOT_IMAGE_AARCH64" if arch == "aarch64"
                   else "MBOT_IMAGE_X86_64")
            return None, (f"未配置 {arch} 的 Ubuntu 镜像"
                          f"（环境变量 {env} 或 region_plans.json）")
        return image_id, None
    if os_value == "centos":
        image_id = deploy.get(f"ol_{arch}") or IMAGE_PLACEHOLDER
        if image_id == IMAGE_PLACEHOLDER:
            env = ("MBOT_IMAGE_OL_AARCH64" if arch == "aarch64"
                   else "MBOT_IMAGE_OL_X86_64")
            return None, (f"未配置 {arch} 的 Oracle Linux 镜像"
                          f"（环境变量 {env} 或 region_plans.json）")
        return image_id, None
    return os_value, None  # 自定义镜像 OCID


def resolve_delay(form):
    """延迟选择 -> (min_delay, max_delay)，供 Grabber 构造用。

    未选返回 (None, None)（Grabber 用默认 25±5 秒）；选一个返回
    (v, v)（固定间隔）；选两个返回 (小, 大)（区间随机）。
    """
    delays = sorted(form.get("delays") or [])
    if not delays:
        return None, None
    if len(delays) == 1:
        return delays[0], delays[0]
    return delays[0], delays[-1]


def resolve_spec(form):
    """核心/内存 -> (ocpus, memory_gb)。E2.1.Micro 是固定规格 1 核/1GB，
    核心/内存选择不生效，统一归一为 (1, 1)。"""
    shape, _ = resolve_shape(form)
    if shape == SHAPE_E2_MICRO:
        return 1, 1
    return form.get("ocpus"), form.get("memory_gb")


# ---------------- 校验与预设生成 ----------------

def region_of_availability_domain(availability_domain):
    """从可用域字符串里解析区域代码。

    如 wgwP:AP-SINGAPORE-1-AD-1 -> ap-singapore-1；格式不对或为空
    时返回 None（调用方不做区域一致性判断）。
    与 web/routes/create.py 的 _ad_region 同逻辑（两边各一份，
    避免 web 与 tgbot 互相 import）。
    """
    if not availability_domain:
        return None
    parts = availability_domain.split(":")
    if len(parts) < 2:
        return None
    middle = parts[1]
    idx = middle.rfind("-AD-")
    if idx <= 0:
        return None
    return middle[:idx].lower()


def deploy_region_mismatch(deploy, region, profile=None):
    """部署值与工作区域不一致时返回错误文案，否则返回 None。

    get_deploy_defaults 对没有 region plan 的账号+区域会回落到主区
    （新加坡）默认值；直接拿它去别的区开机，OCI 会报
    CannotParseRequest（2026-11-24 用户在 ACCOUNT2/ACCOUNT3 上
    一键开机实测复现）。开机入口必须先调这个拦住。
    """
    if not region:
        return None
    ad_region = region_of_availability_domain(
        (deploy or {}).get("availability_domain"))
    if ad_region is None or ad_region == region:
        return None
    where = f"{profile}/{region}" if profile else region
    return (f"部署值是 {ad_region} 区的，与当前工作区域 {region} 不一致："
            f"{where} 还没有配置开机网络（可用域/子网/镜像），直接开机 "
            "Oracle 会报 CannotParseRequest。请先在 region_plans.json "
            "里给该账号+区域配好，或切到已配置的账号/区域再开机。")


def validate_form(form, deploy=None, region=None, profile=None):
    """校验表单完整性与部署配置，返回错误文案列表（空列表 = 通过）。

    deploy 是 get_deploy_defaults() 的返回值；不传时现读环境变量。
    region/profile 传了时额外校验部署值与工作区域是否一致（见
    deploy_region_mismatch）。
    """
    deploy = deploy or get_deploy_defaults()
    errors = []
    shape, _ = resolve_shape(form)
    if shape is None:
        errors.append("未选 CPU 类型")
    if form.get("os") is None:
        errors.append("未选系统")
    else:
        _, image_error = resolve_image_id(form, deploy)
        if image_error is not None:
            errors.append(image_error)
    if shape != SHAPE_E2_MICRO:
        # E2 是固定规格 1 核/1GB，不用选核心/内存；CPU 未选时也先提示
        # （多数用户选 ARM，一次把缺的都列出来）。
        if form.get("ocpus") is None:
            errors.append("未选核心数")
        if form.get("memory_gb") is None:
            errors.append("未选内存")
    if shape == SHAPE_A1_FLEX:
        # A1 免费额度内单台上限 4 核 / 24GB，超过会产生费用。
        if (form.get("ocpus") or 0) > 4:
            errors.append("A1 免费额度内单台上限 4 核")
        if (form.get("memory_gb") or 0) > 24:
            errors.append("A1 免费额度内单台上限 24GB 内存")
    disk = form.get("disk_gb")
    if disk is not None and not (DISK_MIN_GB <= disk <= DISK_MAX_GB):
        errors.append(f"硬盘应为 {DISK_MIN_GB}–{DISK_MAX_GB}GB")
    count = form.get("count")
    if count is None:
        errors.append("未选数量")
    elif not (COUNT_MIN <= count <= COUNT_MAX):
        errors.append(f"数量应为 {COUNT_MIN}–{COUNT_MAX} 台")
    for delay in form.get("delays") or []:
        if not (DELAY_MIN_SECONDS <= delay <= DELAY_MAX_SECONDS):
            errors.append(f"延迟 {delay} 秒超出范围 "
                          f"({DELAY_MIN_SECONDS}–{DELAY_MAX_SECONDS})")
            break
    if form.get("ssh_key") == "none" and not form.get("root_login"):
        errors.append("不用公钥时必须开启 root 登录，否则无法登录实例。")
    script = form.get("script") or {}
    if (script.get("content") or "").lstrip().startswith("#cloud-config") \
            and form.get("root_login"):
        errors.append("开机脚本已是 #cloud-config 格式时不能同时开 root "
                      "密码登录，请二选一。")
    if deploy.get("availability_domain") in (None, "", AD_PLACEHOLDER):
        errors.append("未配置可用域（环境变量 MBOT_PLAN_AD）")
    if deploy.get("subnet_id") in (None, "", SUBNET_PLACEHOLDER):
        errors.append("未配置子网（环境变量 MBOT_PLAN_SUBNET_ID）")
    if form.get("ssh_key") == "use" and deploy.get("ssh_keys_file") in (
            None, "", SSH_KEYS_PLACEHOLDER):
        errors.append("未配置 SSH 公钥文件（环境变量 MBOT_PLAN_SSH_KEYS_FILE）")
    if deploy.get("availability_domain") not in (None, "", AD_PLACEHOLDER):
        mismatch = deploy_region_mismatch(deploy, region, profile)
        if mismatch is not None:
            errors.append(mismatch)
    return errors


def generate_display_name(shape, ocpus, memory_gb, now=None):
    """生成实例显示名（用户 2026-11-24 定：免费实例按类型 + 北京时间日期）。

    E2（AMD 免费）-> AMD_YYYY-MM-DD；A1（ARM）-> ARM_YYYY-MM-DD；
    其他形状沿用旧格式 bot-<短名>-<核>c<内存>g-<UTC 时间戳>。
    同日同类型第二台由调用方加后缀去重（见 handlers_extra 的
    uniquify_display_name）；Grabber 的 find_existing 只认 15 分钟内
    同名实例为重复响应，不会把旧实例误当新开的。
    now 供测试注入（aware datetime，未传则取当前北京时间）。
    """
    if shape in (SHAPE_A1_FLEX, SHAPE_E2_MICRO):
        moment = now or datetime.now(BEIJING_TZ)
        date = moment.astimezone(BEIJING_TZ).strftime("%Y-%m-%d")
        prefix = "ARM" if shape == SHAPE_A1_FLEX else "AMD"
        return f"{prefix}_{date}"
    short = (shape or "vm").replace("VM.Standard.", "").replace(
        "BM.Standard.", "").lower().replace(".", "-").replace("_", "-")[:20]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"bot-{short}-{ocpus}c{memory_gb}g-{stamp}"


def build_preset(form, deploy=None):
    """表单 -> Grabber 预设 dict（与 BOOT_PRESETS 同构，可直接存
    pending_boot / presets.json）。校验不通过时抛 ValueError（文案为
    validate_form 的错误，用中文分号连接）。"""
    errors = validate_form(form, deploy)
    if errors:
        raise ValueError("；".join(errors))
    deploy = deploy or get_deploy_defaults()
    shape, note = resolve_shape(form)
    image_id, _ = resolve_image_id(form, deploy)
    ocpus, memory_gb = resolve_spec(form)
    min_delay, max_delay = resolve_delay(form)
    script = form.get("script") or {}
    return {
        "shape": shape,
        "ocpus": ocpus,
        "memory_gb": memory_gb,
        # E2 沿用旧 e2 预设的做法：只留 7J13（Grabber 收到 target_cpu 后
        # 在 cpu_check_hook 注入时筛 CPU；机器人目前未注入 hook，
        # 该字段暂不生效，见 handlers_extra._run_grabber_thread）。
        "target_cpu": TARGET_CPU if shape == SHAPE_E2_MICRO else None,
        "display_name": generate_display_name(shape, ocpus, memory_gb),
        "image_id": image_id,
        "subnet_id": deploy["subnet_id"],
        "availability_domain": deploy["availability_domain"],
        # 不用公钥时 ssh_keys_file 为 None，Grabber 不传
        # --ssh-authorized-keys-file（配合 root 密码登录）。
        "ssh_keys_file": (None if form.get("ssh_key") == "none"
                          else deploy["ssh_keys_file"]),
        "count": form.get("count") or 1,
        "boot_volume_size_gbs": form.get("disk_gb"),
        "assign_public_ip": bool(form.get("public_ip", True)),
        "assign_ipv6": bool(form.get("ipv6")),
        "startup_script": script.get("content"),
        "root_login": bool(form.get("root_login")),
        "min_delay": min_delay,
        "max_delay": max_delay,
        # 以下三项供确认页与快捷开机展示用（旧版保存的预设没有它们，
        # 展示时用 .get 兜底）。
        "cpu": form.get("cpu"),
        "os": form.get("os"),
        "note": note,
    }


def form_from_preset(preset):
    """已保存的快捷开机预设 -> 开机表单 dict（编辑用，build_preset 的反向）。

    优先用预设里存的 cpu/os（build_preset 会存）；旧版预设没有这两项
    时反推：形状 E2 -> cpu=amd、A1 -> cpu=arm；镜像 OCID 对得上部署值
    里的 Ubuntu/OL 镜像时反推 os，对不上时把 OCID 本身当自定义镜像
    （resolve_image_id 会原样返回，仍能开机）。
    E2 是固定规格，表单里 ocpus/memory_gb 留 None（resolve_spec 归一
    为 1/1）。display_name 不进表单（build_preset 会重新生成；编辑保
    存时由调用方把原名写回去，见 handlers_extra._boot_form_save）。
    """
    form = new_form()
    preset = preset or {}
    shape = preset.get("shape") or ""
    cpu = preset.get("cpu")
    if cpu is None:
        if shape == SHAPE_E2_MICRO:
            cpu = "amd"
        elif shape == SHAPE_A1_FLEX:
            cpu = "arm"
    form["cpu"] = cpu
    os_value = preset.get("os")
    if os_value is None:
        image_id = preset.get("image_id")
        deploy = get_deploy_defaults()
        if image_id in (deploy.get("image_aarch64"),
                        deploy.get("image_x86_64")):
            os_value = "ubuntu"
        elif image_id in (deploy.get("ol_aarch64"),
                          deploy.get("ol_x86_64")):
            os_value = "centos"
        elif image_id:
            os_value = image_id  # 自定义镜像 OCID
    form["os"] = os_value
    if shape != SHAPE_E2_MICRO:
        form["ocpus"] = preset.get("ocpus")
        form["memory_gb"] = preset.get("memory_gb")
    form["disk_gb"] = preset.get("boot_volume_size_gbs")
    form["count"] = preset.get("count") or 1
    min_delay = preset.get("min_delay")
    max_delay = preset.get("max_delay")
    if min_delay is None and max_delay is None:
        form["delays"] = []
    elif min_delay == max_delay:
        form["delays"] = [min_delay]
    else:
        form["delays"] = [min_delay, max_delay]
    form["ssh_key"] = "none" if preset.get("ssh_keys_file") is None else "use"
    form["root_login"] = bool(preset.get("root_login"))
    form["public_ip"] = bool(preset.get("assign_public_ip", True))
    form["ipv6"] = bool(preset.get("assign_ipv6"))
    script = preset.get("startup_script")
    form["script"] = ({"name": "开机脚本", "content": script}
                      if script else None)
    return form


# ---------------- root 密码与 user-data ----------------

def generate_root_password():
    """生成默认 root 密码：UUID4 的 32 位十六进制（去掉连字符）。

    只含 [0-9a-f]，可直接放进 cloud-init 的 chpasswd 块标量里，
    不用转义；128 位熵。每次调用生成一个全新的 UUID。
    """
    return uuid.uuid4().hex


def _normalize_script(script):
    """开机脚本归一化：去掉首尾空白；全空白时返回空字符串；
    既不是 #! 脚本也不是 #cloud-config 时自动补 #!/bin/bash
    （cloud-init 靠文件头判断类型，无文件头会被忽略）。"""
    text = (script or "").strip()
    if not text:
        return ""
    if text.startswith("#!") or text.startswith("#cloud-config"):
        return text
    return "#!/bin/bash\n" + text


def build_user_data(root_password=None, startup_script=None):
    """生成传给 Grabber 的 user-data（cloud-init 内容）。

    - 只有 root 密码：#cloud-config，chpasswd 设 root 密码 + ssh_pwauth。
    - 只有脚本：原样返回（自动补 #!/bin/bash 头）。
    - 两者都有：#cloud-config，chpasswd + write_files 写脚本到
      STARTUP_SCRIPT_PATH 再 runcmd 执行。
    - 都没有：返回 None（Grabber 不传 --user-data-file）。
    脚本本身已是 #cloud-config 时不能与 root 密码共存（两份
    cloud-config 无法合并），抛 ValueError。

    有 root 密码时还必须加两样（2026-11-24 实测：Ubuntu 24.04 镜像
    默认 disable_root: true，root 的 key 会被加强制命令，只能看到
    "Please login as the user ubuntu"；且 PermitRootLogin 默认
    prohibit-password，密码也登不进）：
    - disable_root: false：cloud-init 不再给 root 的 key 加强制命令，
      密钥登录 root 直接可用；
    - write_files 写 /etc/ssh/sshd_config.d/60-mbot-root-login.conf
      （PermitRootLogin yes）+ runcmd reload sshd：密码登录 root 可用
      （ssh 服务名 Ubuntu 是 ssh、Oracle Linux 是 sshd，两边都试）。
    """
    script = _normalize_script(startup_script)
    if root_password and script.startswith("#cloud-config"):
        raise ValueError("开机脚本已是 #cloud-config 格式时不能同时开 "
                         "root 密码登录，请二选一。")
    if not root_password and not script:
        return None
    if root_password and not script:
        return ("#cloud-config\n"
                "disable_root: false\n"
                "chpasswd:\n"
                "  list: |\n"
                f"    root:{root_password}\n"
                "  expire: false\n"
                "ssh_pwauth: true\n"
                "write_files:\n"
                "  - path: /etc/ssh/sshd_config.d/60-mbot-root-login.conf\n"
                '    permissions: "0644"\n'
                "    content: |\n"
                "      PermitRootLogin yes\n"
                "runcmd:\n"
                "  - systemctl reload ssh || systemctl reload sshd || true\n")
    if script and not root_password:
        return script
    lines = [
        "#cloud-config",
        "disable_root: false",
        "chpasswd:",
        "  list: |",
        f"    root:{root_password}",
        "  expire: false",
        "ssh_pwauth: true",
        "write_files:",
        f"  - path: {STARTUP_SCRIPT_PATH}",
        '    permissions: "0755"',
        "    content: |",
    ]
    for line in script.split("\n"):
        lines.append("      " + line if line else "")
    lines.extend([
        "  - path: /etc/ssh/sshd_config.d/60-mbot-root-login.conf",
        '    permissions: "0644"',
        "    content: |",
        "      PermitRootLogin yes",
        "runcmd:",
        "  - systemctl reload ssh || systemctl reload sshd || true",
        f"  - {STARTUP_SCRIPT_PATH}",
    ])
    return "\n".join(lines) + "\n"


# ---------------- 展示与键盘 ----------------

def _cpu_label(form):
    """CPU 行的当前值展示文字（未选时返回 None）。"""
    cpu = form.get("cpu")
    if cpu is None:
        return None
    return {"arm": "ARM（A1.Flex）",
            "amd": "AMD（E2.1.Micro）",
            "intel": "INTEL（→E2.1.Micro）"}.get(cpu, cpu)


def _os_label(form):
    """系统行的当前值展示文字（未选时返回 None）。"""
    os_value = form.get("os")
    if os_value is None:
        return None
    if os_value == "ubuntu":
        return "Ubuntu 24.04"
    if os_value == "centos":
        return "CentOS（→Oracle Linux）"
    return f"自定义镜像 …{os_value[-4:]}"


def _delay_label(form):
    """延迟行的当前值展示文字。"""
    delays = sorted(form.get("delays") or [])
    if not delays:
        return "未选（默认 25±5 秒）"
    if len(delays) == 1:
        return f"固定 {delays[0]} 秒"
    return f"区间 {delays[0]}–{delays[-1]} 秒随机"


def fmt_form_text(form):
    """表单消息正文：当前各行选择 + 操作提示。"""
    cpu = _cpu_label(form) or "未选"
    os_text = _os_label(form) or "未选"
    ocpus = form.get("ocpus")
    memory_gb = form.get("memory_gb")
    shape, _ = resolve_shape(form)
    if shape == SHAPE_E2_MICRO:
        spec = "固定 1 核 / 1GB（E2.1.Micro）"
    else:
        spec = (f"{ocpus if ocpus is not None else '未选'} 核 / "
                f"{memory_gb if memory_gb is not None else '未选'}GB")
    disk = form.get("disk_gb")
    disk_text = "默认（47GB）" if disk is None else f"{disk}GB"
    count = form.get("count")
    count_text = "未选" if count is None else f"{count} 台"
    script = form.get("script")
    script_text = ("无" if script is None
                   else f"已上传（{len(script.get('content') or '')} 字符）")
    lines = [
        "🚀 开机配置（按钮矩阵）：",
        "",
        f"CPU：{cpu}",
        f"系统：{os_text}",
        f"规格：{spec}",
        f"硬盘：{disk_text}",
        f"数量：{count_text}",
        f"延迟：{_delay_label(form)}",
        f"公钥：{'使用机器人公钥' if form.get('ssh_key') == 'use' else '不用公钥'}",
        f"root登录：{'开启（密码随机）' if form.get('root_login') else '不开启'}",
        f"公网IP：{'有' if form.get('public_ip') else '无'}",
        f"IPv6：{'有' if form.get('ipv6') else '无'}",
        f"开机脚本：{script_text}",
        "",
        "点选项打 ✅ 选中，再点取消；延迟选一个=固定间隔，选两个=区间随机。",
        "公钥和 root 登录至少选一项（不用公钥时必须开 root 登录）。",
        "CentOS 已映射为 Oracle Linux（OCI 已无 CentOS 平台镜像）。",
    ]
    return "\n".join(lines)


def _opt(label, selected, callback):
    """单个选项按钮：选中时前面加 ✅。"""
    return InlineKeyboardButton(("✅ " if selected else "") + label,
                                callback_data=callback)


def _label(text):
    """行标签按钮：点它无操作（callback bf:noop 只消掉转圈）。"""
    return InlineKeyboardButton(text, callback_data="bf:noop")


def _is_custom_selected(form, key, presets):
    """某行当前值是不是自定义值（不在预设列表里的已选值）。"""
    value = form.get(key)
    return value is not None and value not in presets


def boot_form_keyboard(form):
    """按钮矩阵键盘（开机表单布局：每行标签 + 预设 + 自定义）。

    callback_data 规范（见 tgbot/keyboards.py 模块 docstring 的 bf: 一节）：
      bf:<row>:<value> 点选（row 见 ROW_FORM_KEY；value 是预设值或 custom）
      bf:confirm 确定开机（进配置摘要确认页）
      bf:save 保存为快捷开机
      bf:oneclick:amd 一键开机（默认 AMD：E2.1.Micro + Ubuntu + root 开）
      bf:noop 行标签（无操作）
    """
    cpu = form.get("cpu")
    os_value = form.get("os")
    ocpus = form.get("ocpus")
    memory_gb = form.get("memory_gb")
    disk = form.get("disk_gb")
    count = form.get("count")
    delays = form.get("delays") or []
    rows = [
        [InlineKeyboardButton("🚀 一键开机（默认 AMD）",
                              callback_data="bf:oneclick:amd")],
        [_label("CPU▸"),
         _opt("ARM", cpu == "arm", "bf:cpu:arm"),
         _opt("AMD", cpu == "amd", "bf:cpu:amd"),
         _opt("INTEL", cpu == "intel", "bf:cpu:intel"),
         _opt("自定义", _is_custom_selected(
             form, "cpu", ["arm", "amd", "intel"]), "bf:cpu:custom")],
        [_label("系统▸"),
         _opt("Ubuntu", os_value == "ubuntu", "bf:os:ubuntu"),
         _opt("CentOS*", os_value == "centos", "bf:os:centos"),
         _opt("自定义", _is_custom_selected(
             form, "os", ["ubuntu", "centos"]), "bf:os:custom")],
        [_label("核心▸")] + [
            _opt(str(n), ocpus == n, f"bf:ocpus:{n}") for n in OCPU_OPTIONS
        ] + [_opt("自定义", _is_custom_selected(
            form, "ocpus", OCPU_OPTIONS), "bf:ocpus:custom")],
        [_label("内存▸")] + [
            _opt(str(n), memory_gb == n, f"bf:mem:{n}") for n in MEMORY_OPTIONS
        ] + [_opt("自定义", _is_custom_selected(
            form, "memory_gb", MEMORY_OPTIONS), "bf:mem:custom")],
        [_label("硬盘▸"),
         _opt("默认", disk is None, "bf:disk:default")] + [
            _opt(str(n), disk == n, f"bf:disk:{n}") for n in DISK_OPTIONS
        ] + [_opt("自定义", _is_custom_selected(
            form, "disk_gb", DISK_OPTIONS + [None]), "bf:disk:custom")],
        [_label("数量▸")] + [
            _opt(str(n), count == n, f"bf:count:{n}") for n in COUNT_OPTIONS
        ] + [_opt("自定义", _is_custom_selected(
            form, "count", COUNT_OPTIONS), "bf:count:custom")],
        [_label("延迟▸")] + [
            _opt(str(n), n in delays, f"bf:delay:{n}")
            for n in DELAY_OPTIONS[:5]
        ],
        [_label("延迟▸")] + [
            _opt(str(n), n in delays, f"bf:delay:{n}")
            for n in DELAY_OPTIONS[5:]
        ] + [_opt("自定义", any(d not in DELAY_OPTIONS for d in delays),
                  "bf:delay:custom")],
        [_label("公钥▸"),
         _opt("使用机器人公钥", form.get("ssh_key") == "use", "bf:key:use"),
         _opt("不用公钥", form.get("ssh_key") == "none", "bf:key:none")],
        [_label("root▸"),
         _opt("不开启", not form.get("root_login"), "bf:root:off"),
         _opt("开启(密码随机)", bool(form.get("root_login")), "bf:root:on")],
        [_label("公网IP▸"),
         _opt("有", bool(form.get("public_ip")), "bf:pubip:yes"),
         _opt("无", not form.get("public_ip"), "bf:pubip:no")],
        [_label("IPv6▸"),
         _opt("无", not form.get("ipv6"), "bf:ipv6:no"),
         _opt("有", bool(form.get("ipv6")), "bf:ipv6:yes")],
        [_label("脚本▸"),
         _opt("无", form.get("script") is None, "bf:script:none"),
         _opt("上传", form.get("script") is not None, "bf:script:upload")],
        [InlineKeyboardButton("✅ 确定开机", callback_data="bf:confirm"),
         InlineKeyboardButton("💾 保存为快捷开机", callback_data="bf:save")],
        [InlineKeyboardButton("◀ Oracle 菜单", callback_data="o:menu")],
    ]
    return InlineKeyboardMarkup(rows)


def fmt_boot_form_summary(preset, ok, reason):
    """配置摘要确认页（表单确认 / 菜单 17 一键开机共用）。

    preset 是 build_preset 的产物或 presets.json 里保存的旧预设
    （旧预设没有 count / root_login 等新字段，一律用 .get 兜底）。
    """
    shape = (preset.get("shape") or "?").replace("VM.Standard.", "")
    count = preset.get("count", 1) or 1
    lines = [
        "🚀 开机配置确认：",
        "",
        f"规格：{shape}（{preset.get('ocpus')} 核 / "
        f"{preset.get('memory_gb')}GB）",
    ]
    os_value = preset.get("os")
    if os_value == "ubuntu":
        os_text = "Ubuntu 24.04"
    elif os_value == "centos":
        os_text = "Oracle Linux（CentOS 映射）"
    elif os_value:
        os_text = f"自定义镜像 …{str(os_value)[-4:]}"
    else:
        os_text = f"镜像 …{(preset.get('image_id') or '')[-4:]}"
    lines.append(f"系统：{os_text}")
    name = preset.get("display_name", "?")
    if count == 1:
        lines.append(f"实例名：{name}")
    else:
        lines.append(f"实例名：{name}-1 ～ {name}-{count}（共 {count} 台）")
    disk = preset.get("boot_volume_size_gbs")
    lines.append(f"硬盘：{'默认（47GB）' if disk is None else f'{disk}GB'}")
    min_delay = preset.get("min_delay")
    max_delay = preset.get("max_delay")
    if min_delay and max_delay:
        if min_delay == max_delay:
            delay_text = f"固定 {min_delay} 秒"
        else:
            delay_text = f"区间 {min_delay}–{max_delay} 秒随机"
    else:
        delay_text = "默认 25±5 秒"
    lines.append(f"延迟：{delay_text}")
    lines.append("公钥：" + ("使用机器人公钥" if preset.get("ssh_keys_file")
                             else "不用公钥"))
    if preset.get("root_login"):
        lines.append("root登录：开启")
        # 确认页就显示密码（照 R 探长格式）：密码在进确认页时生成，
        # 存在 pending_boot 里（仅内存），点“开始抢机”后复用同一份。
        password = preset.get("root_password")
        if password:
            lines.append("root密码：")
            lines.append(password)
            lines.append("注意📢：root密码机器人不会记忆，请注意保存")
    else:
        lines.append("root登录：不开启")
    lines.append("公网IP：" + ("有" if preset.get("assign_public_ip", True)
                               else "无"))
    lines.append("IPv6：" + ("有" if preset.get("assign_ipv6") else "无"))
    script = preset.get("startup_script")
    lines.append("开机脚本：" + (f"已上传（{len(script)} 字符）"
                                 if script else "无"))
    if preset.get("shape") == SHAPE_E2_MICRO:
        if preset.get("target_cpu"):
            lines.append(f"目标 CPU：只留含 {preset['target_cpu']} 的，"
                         "不符就释放继续抢")
        else:
            lines.append("目标 CPU：什么 CPU 都可以，抢到就留")
    if preset.get("note"):
        lines.append(f"⚠️ {preset['note']}")
    lines.append("")
    lines.append(f"配额预检：{'✅ ' if ok else '❌ '}{reason}")
    if not ok:
        lines.append("配额不足，抢机也会停下，请先释放资源或换账号。")
    return "\n".join(lines)
