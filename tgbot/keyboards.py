#!/usr/bin/env python3
"""Telegram 内联键盘（InlineKeyboard）。

callback_data 规范（Telegram 限制 64 字节，OCID 约 100 字节放不下，
所以一律用短键 + 序号，不放 OCID）：
  - "o:<key>"        /oracle 菜单项，key 见 ORACLE_MENU_ITEMS
  - "m:<key>"        主菜单项（start 菜单）
  - "i:list"         实例列表
  - "i:detail:<idx>" 实例详情（idx 是 context.user_data["instances"] 的下标）
  - "i:noop"         实例列表的分组标题按钮：点它只应答，不做任何事
  - "i:<act>:<idx>"  实例操作：start/stop/restart/terminate/ip/resize/
    rename（改名，需回复文字）/reinstall（重装，需输入实例名确认）/
    console（串行控制台）
  - "c:<act>:<idx>"  确认执行危险操作（terminate/stop/changeip/resize/
    rename/reinstall/delete/batchstart/batchstop/batchrestart/
    batchterminate/delbucket/delobj/createsender/delplan/restartsvc/
    restartbot/openport/upgrade；批量操作与 createsender/restartbot/
    openport/upgrade 时 idx 无用，传 0；delplan 时 idx 是计划 id）
  - "ba:toggle:<idx>" 批量操作：切换选中某台实例
  - "ba:next"         批量操作：选好实例，进入选动作
  - "ba:action:<act>" 批量操作：选动作（start/stop/restart/terminate）
  - "g:toggle"        流量守护：开关
  - "g:action"        流量守护：切换动作（仅通知/阻断断网/限速200K）
  - "g:threshold:<gb>" 流量守护：改阈值（GB）
  - "k:toggle:<idx>"  停机保活：切换某台实例的保活开关
    （idx 是 context.user_data["instances"] 的下标）
  - "s:add"           SSL 监控：添加域名（需回复文字）
  - "s:add_cancel"    SSL 监控：取消添加域名
  - "s:del:<idx>"     SSL 监控：删除某域名（idx 是域名列表下标）
  - "s:refresh"       SSL 监控：刷新各域名剩余天数
  - "x"              取消 / 关闭
  - "o:menu"         回 /oracle 菜单
  - "b:boot:<preset>" 开机预设选择：e2 / a1small / a1full
    （旧菜单 1 的回退路径，菜单 1 入口已改成 bf: 表单）
  - "b:go"           确认开始抢机
  - "b:load:<idx>"   快捷开机：用 presets.json 第 idx 条配置开机
  - "bf:<row>:<value>" 开机表单矩阵点选（row：cpu/os/ocpus/mem/disk/
    count/delay/key/root/pubip/ipv6/script；value 是预设值或 custom；
    延迟可多选两个；点已选值取消选择）
  - "bf:confirm"     开机表单：显示配置摘要确认页
  - "bf:save"        开机表单：保存为快捷开机
  - "bf:oneclick:amd" 开机表单：一键开机（默认 AMD，进确认页）
  - "bf:noop"        开机表单：行标签按钮（无操作）
  - "bf:cancel_input" 开机表单：取消自定义输入
  - "t:list"         任务列表
  - "t:cancel:<id>"  取消任务（id 是任务注册表里的短 ID，如 "g1"）
  - "p:<profile>"    切换 profile（多区账号先出二级区域菜单）
  - "pr:<profile>:<region>" 切换账号 + 工作区域
  - "bark:url" / "bark:key" / "bark:test" / "bark:toggle" / "bark:cancel_input"
    Bark 通知设置：设地址 / 设加密 Key / 测试推送 / 开关 / 取消输入
  - "q:list"         免费额度：回账号列表
  - "q:acct:<name>"  免费额度：点开某账号（多区账号出二级区域菜单，
    单区账号直接显示该区额度）
  - "q:region:<name>:<region>" 免费额度：显示某账号在某区域的额度
  - "v:<act>:<idx>"  磁盘操作：detail/resize/delete（idx 是 volumes 列表下标）
  - "os:objects:<idx>" 对象存储：列某 Bucket 的对象（idx 是 os_buckets 下标）
  - "os:delbucket:<idx>" 对象存储：删除 Bucket 确认提示
  - "os:delobj:<idx>"  对象存储：删除对象确认提示（idx 是 os_objects 下标）
  - "os:back"         对象存储：返回 Bucket 列表
  - "pl:toggle:<id>"  抢机计划：开关某计划（id 是计划 id，如 "p1"）
  - "pl:delete:<id>"  抢机计划：删除某计划确认提示
  - "cl:restart:<idx>" 客户端管理：重启某服务确认提示
    （idx 是 SERVICES 下标：0=面板 rbot-clone-web，1=机器人 rbot-clone-bot）
  - "domain:zone:<zone_id>"  /domain：查看某 Zone 的 DNS 记录
    （Cloudflare zone id 是 32 位十六进制，"domain:zone:" + 32 字符
    共 44 字节，未超 64 字节限制，可以直接放 id，不用下标）
  - "domain:back"  /domain：返回 Zone 列表
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from tgbot.monitoring import (TRAFFIC_GUARD_THRESHOLD_PRESETS,
                              fmt_threshold_gb,
                              traffic_guard_action_label)
from tgbot.services import (
    instance_group_title,
    short_shape,
    sort_instances_by_group,
)

# ================= 配置区（改这里） =================
# /oracle 菜单：(callback key, 显示文字)，1-34 项与 R-Bot 的 34 项菜单对应
# （来源：docs/references.md）；35 项以后是克隆新增（阶段 5）。
# key 是短标识，显示文字带原编号。
# 注意：13 号已删（用户要求 2026-11-24：菜单顶行已有“切换账号”按钮，
# 菜单里的 13. profile管理 是重复入口，没必要留）；其余编号保持不动，
# 不重新编号（1-34 与 R-Bot 的编号对应关系、用户叫惯的“菜单 19 流量”
# 这类叫法都不变）。切换账号功能本身还在（o:profiles 回调 + 顶行按钮）。
ORACLE_MENU_ITEMS = [
    ("boot", "1. 开机(刷机)"),
    ("ip", "2. ip管理"),
    ("resume", "3. 被暂停开机"),
    ("ipv6", "4. ipv6管理"),
    ("resize", "5. 升/降级实例"),
    ("instances", "6. 实例管理"),
    ("volumes", "7. 磁盘管理"),
    ("users", "8. 云账户管理"),
    ("monitor", "9. 实例状态监控通知"),
    ("autostart", "10. 实例状态监控自启"),
    ("openport", "11. 开放云面板端口"),
    ("workflows", "12. 工作流错误"),
    ("clients", "14. 客户端管理"),
    ("health", "15. 一键测活"),
    ("autoip", "16. 监控(自动换ip)"),
    ("presets", "17. 快捷开机"),
    ("subscription", "18. 订阅信息"),
    ("traffic", "19. 本月流量"),
    ("quota", "20. 查询配额"),
    ("tasks", "21. 查看运行中的任务"),
    ("load", "22. 查看客户端负载"),
    ("memballoon", "23. 占用25%内存"),
    ("memclear", "24. 清除缓存和占用"),
    ("cost", "25. 近3月花费查询"),
    ("mfa", "26. 清除所有2FA设备"),
    ("disable", "27. 一键禁用被封账户"),
    ("delapi", "28. 删除当前API"),
    ("daily", "29. 每日报告"),
    ("emails", "30. 批量查询邮箱"),
    ("restart", "31. 重启"),
    ("upgrade", "32. 升级客户端"),
    ("regions", "33. 自动拓展子区域"),
    ("logs", "34. 查询最新日志"),
    ("batch", "35. 批量操作"),
    ("a1health", "36. A1 配置体检"),
    ("traffic_guard", "37. 流量守护"),
    ("keepalive", "38. 停机保活"),
    ("sslwatch", "39. SSL 监控"),
    ("objectstorage", "40. 对象存储"),
    ("email", "41. Email 发件人"),
    ("plans", "42. 抢机计划"),
    ("bark", "43. Bark 通知设置"),
]
# 每页显示的菜单项数（2 列，自动分页；总页数随菜单项数变化）。
MENU_PAGE_SIZE = 18
# 按钮文字带动态开关状态的菜单项：key -> states 字典里的键。
# 生成菜单时传 states={...}，对应按钮会带上“(开启)”/“(关闭)”后缀；
# states 不传或不是 dict 时显示原文字（兼容旧调用）。
DYNAMIC_STATE_ITEMS = {
    "monitor": "monitor",
    "autostart": "autostart",
    "traffic_guard": "traffic_guard",
    "keepalive": "keepalive",
    "bark": "bark",
}
# =====================================================


def _menu_page(page):
    """返回第 page 页的菜单项（0 起），越界时返回最后一页。"""
    total_pages = (len(ORACLE_MENU_ITEMS) + MENU_PAGE_SIZE - 1) // MENU_PAGE_SIZE
    page = max(0, min(page, total_pages - 1))
    start = page * MENU_PAGE_SIZE
    return ORACLE_MENU_ITEMS[start:start + MENU_PAGE_SIZE], page, total_pages


def oracle_menu_keyboard(page=0, states=None, current_account=None):
    """`/oracle` 菜单键盘：2 列排列，带上一页/下一页。

    states：可选的开关状态字典（键见 DYNAMIC_STATE_ITEMS），
    传了之后监控类按钮会带上“(开启)”/“(关闭)”后缀。
    current_account：当前账号显示名（如“新加坡（3 个区）”），传了之
    后菜单顶行多一条“🔄 切换账号：<当前账号>”按钮（点开切换账号）。
    """
    items, page, total_pages = _menu_page(page)
    rows = []
    if current_account:
        rows.append([InlineKeyboardButton(
            f"🔄 切换账号：{current_account}",
            callback_data="o:profiles")])
    for i in range(0, len(items), 2):
        row = []
        for key, text in items[i:i + 2]:
            state_key = DYNAMIC_STATE_ITEMS.get(key)
            if state_key is not None and isinstance(states, dict):
                text = f"{text} ({'开启' if states.get(state_key) else '关闭'})"
            row.append(InlineKeyboardButton(text, callback_data=f"o:{key}"))
        rows.append(row)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ 上一页",
                                        callback_data=f"o:page:{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}",
                                    callback_data="x"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("下一页 ▶",
                                        callback_data=f"o:page:{page + 1}"))
    rows.append(nav)
    return InlineKeyboardMarkup(rows)


def main_menu_keyboard():
    """`/start` 主菜单：最常用的几个入口。"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("☁️ Oracle 菜单", callback_data="o:menu"),
         InlineKeyboardButton("🖥️ 实例列表", callback_data="i:list")],
        [InlineKeyboardButton("📊 查询配额", callback_data="o:quota"),
         InlineKeyboardButton("📈 本月流量", callback_data="o:traffic")],
        [InlineKeyboardButton("📋 运行中的任务", callback_data="o:tasks"),
         InlineKeyboardButton("❤️ 一键测活", callback_data="o:health")],
    ])


def _instance_button_text(inst):
    """实例按钮文字：名称 · A1/E2 · 公网 IP 或状态词。

    有公网 IP 时显示 IP（`名称 · E2 · 1.2.3.4`）；没有 IP 时
    显示状态词（运行中 / 已停止 / 已终止 / 原状态英文）。
    按钮就是唯一的列表，文字部分只剩标题，不再逐台重复。
    """
    name = inst.get("display-name") or inst.get("id", "")[-4:]
    shape = short_shape(inst.get("shape"))
    ip = inst.get("public-ip")
    if ip:
        return f"{name} · {shape} · {ip}"
    state = (inst.get("lifecycle-state") or "").upper()
    state_word = {"RUNNING": "运行中", "STOPPED": "已停止",
                  "TERMINATED": "已终止"}.get(state, state or "?")
    return f"{name} · {shape} · {state_word}"


def instance_list_keyboard(instances):
    """实例列表键盘：按状态分组，每组一行标题按钮 + 每台一个按钮。

    instances 是 instance.list_instances 返回的精简列表。
    分组顺序与 fmt_instance_list 一致（RUNNING → STOPPED →
    TERMINATED → 其他），共用 sort_instances_by_group。
    标题按钮的 callback_data 是 "i:noop"（点它只应答，不做任何事）；
    实例按钮的 callback_data 带原列表下标（不是分组后的下标），
    因为 store_instances 存的是原始顺序列表，回调按原下标取回。
    实例按钮文字带规格简称（A1/E2）与公网 IP（运行中才有）。
    """
    ordered = sort_instances_by_group(instances)
    counts = {}
    for _idx, inst in ordered:
        title = instance_group_title(inst.get("lifecycle-state"))
        counts[title] = counts.get(title, 0) + 1
    rows = []
    current_group = None
    for idx, inst in ordered:
        group = instance_group_title(inst.get("lifecycle-state"))
        if group != current_group:
            current_group = group
            rows.append([InlineKeyboardButton(
                f"{group}（{counts[group]} 台）：", callback_data="i:noop")])
        rows.append([InlineKeyboardButton(
            _instance_button_text(inst), callback_data=f"i:detail:{idx}")])
    rows.append([InlineKeyboardButton("🔄 刷新", callback_data="i:list"),
                 InlineKeyboardButton("◀ Oracle 菜单", callback_data="o:menu")])
    return InlineKeyboardMarkup(rows)


def instance_actions_keyboard(idx, state="RUNNING"):
    """实例操作键盘：按当前状态显示可用的操作。

    idx 是 instances 列表下标；state 是 lifecycle-state。
    """
    rows = []
    if state == "RUNNING":
        rows.append([
            InlineKeyboardButton("⏹️ 停止", callback_data=f"i:stop:{idx}"),
            InlineKeyboardButton("🔄 重启", callback_data=f"i:restart:{idx}"),
        ])
    elif state == "STOPPED":
        rows.append([
            InlineKeyboardButton("▶️ 启动", callback_data=f"i:start:{idx}"),
        ])
    rows.append([
        InlineKeyboardButton("🔁 换 IP", callback_data=f"i:ip:{idx}"),
        InlineKeyboardButton("📐 升/降配", callback_data=f"i:resize:{idx}"),
    ])
    rows.append([
        InlineKeyboardButton("✏️ 改名", callback_data=f"i:rename:{idx}"),
        InlineKeyboardButton("💿 重装系统", callback_data=f"i:reinstall:{idx}"),
    ])
    rows.append([
        InlineKeyboardButton("🖥️ 串行控制台", callback_data=f"i:console:{idx}"),
    ])
    rows.append([
        InlineKeyboardButton("🗑️ 终止实例", callback_data=f"i:terminate:{idx}"),
        InlineKeyboardButton("◀ 实例列表", callback_data="i:list"),
    ])
    return InlineKeyboardMarkup(rows)


def batch_select_keyboard(instances, selected):
    """批量操作多选键盘：点实例切换选中状态。

    instances 是 instance.list_instances 返回的精简列表；
    selected 是已选中实例的下标集合（set[int]）。
    """
    rows = []
    for idx, inst in enumerate(instances):
        name = inst.get("display-name") or inst.get("id", "")[-4:]
        state = inst.get("lifecycle-state", "?")
        mark = "✅" if idx in selected else "☐"
        rows.append([InlineKeyboardButton(
            f"{mark} {name} ({state})",
            callback_data=f"ba:toggle:{idx}")])
    rows.append([InlineKeyboardButton("➡️ 下一步：选动作",
                                      callback_data="ba:next")])
    rows.append([InlineKeyboardButton("◀ Oracle 菜单", callback_data="o:menu")])
    return InlineKeyboardMarkup(rows)


def batch_action_keyboard():
    """批量操作动作选择键盘（开机/关机/重启/删除）。"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ 批量开机", callback_data="ba:action:start"),
         InlineKeyboardButton("⏹️ 批量关机", callback_data="ba:action:stop")],
        [InlineKeyboardButton("🔄 批量重启", callback_data="ba:action:restart"),
         InlineKeyboardButton("🗑️ 批量删除", callback_data="ba:action:terminate")],
        [InlineKeyboardButton("◀ 返回选择", callback_data="o:batch")],
    ])


def confirm_keyboard(action, idx, confirm_text="✅ 确认执行"):
    """危险操作二次确认键盘。

    action: start/stop/restart/terminate/changeip/resize/delete 等；
    idx: instances（或 volumes）列表下标。
    """
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(confirm_text,
                              callback_data=f"c:{action}:{idx}")],
        [InlineKeyboardButton("❌ 取消", callback_data="x")],
    ])


def boot_preset_keyboard():
    """开机预设选择键盘（R-Bot 的快速配置预填）。"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("AMD 微型 1C/1G（E2.1.Micro）",
                              callback_data="b:boot:e2")],
        [InlineKeyboardButton("ARM A1 1C/6G（小）",
                              callback_data="b:boot:a1small")],
        [InlineKeyboardButton("ARM A1 4C/24G（满配）",
                              callback_data="b:boot:a1full")],
        [InlineKeyboardButton("◀ Oracle 菜单", callback_data="o:menu")],
    ])


def boot_confirm_keyboard():
    """开机确认键盘（点开始抢机后进后台任务）。"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 开始抢机", callback_data="b:go")],
        [InlineKeyboardButton("❌ 取消", callback_data="x")],
    ])


def task_list_keyboard(tasks):
    """任务列表键盘：每条运行中任务一个取消按钮。

    tasks 是 TaskRegistry.list_all() 返回的列表，每项含 id/kind/status。
    """
    rows = []
    for task in tasks:
        if task.get("status") in ("running", "pending"):
            rows.append([InlineKeyboardButton(
                f"❌ 取消 {task.get('id')}（{task.get('kind')}）",
                callback_data=f"t:cancel:{task.get('id')}")])
    rows.append([InlineKeyboardButton("🔄 刷新", callback_data="o:tasks"),
                 InlineKeyboardButton("◀ Oracle 菜单", callback_data="o:menu")])
    return InlineKeyboardMarkup(rows)


def profile_keyboard(profiles, current, labels=None):
    """profile 切换键盘：profiles 是 profile 名列表，current 是当前 profile。

    labels 是 {profile: 显示名}（主区中文名，多区账号后缀区数，如
    "新加坡（3 个区）"）；不传时显示 profile 名（兼容旧调用）。
    callback_data 始终是 profile 名（p:<name>），显示名只影响按钮文本。
    """
    rows = []
    for name in profiles:
        mark = "✅ " if name == current else ""
        text = (labels or {}).get(name, name)
        rows.append([InlineKeyboardButton(f"{mark}{text}",
                                          callback_data=f"p:{name}")])
    rows.append([InlineKeyboardButton("◀ Oracle 菜单", callback_data="o:menu")])
    return InlineKeyboardMarkup(rows)


def profile_regions_keyboard(profile, regions, current=None):
    """切换账号的二级区域菜单：某账号的订阅区域列表。

    regions 是 list_regions 的返回（region-name / is-home-region）。
    每行一个区域按钮（pr:<profile>:<region>），Home 区域后缀“（Home）”，
    当前工作区域前缀 ✅；最后一行回账号列表（o:profiles）。
    """
    from tgbot.config import get_region_label

    rows = []
    for region in regions:
        code = region.get("region-name") or region.get("region-key") or "?"
        label = get_region_label(code)
        if region.get("is-home-region"):
            label += "（Home）"
        if current and code == current:
            label = f"✅ {label}"
        rows.append([InlineKeyboardButton(
            label, callback_data=f"pr:{profile}:{code}")])
    rows.append([InlineKeyboardButton("◀ 返回账号列表",
                                     callback_data="o:profiles")])
    return InlineKeyboardMarkup(rows)


def quota_accounts_keyboard(labels):
    """额度页账号列表键盘：labels 是 {profile: 显示名}（主区中文名，
    多区账号后缀区数，如“新加坡（3 个区）”，与切换菜单一致）。

    每行一个账号按钮（q:acct:<profile>），最后一行回 Oracle 菜单。
    """
    rows = [[InlineKeyboardButton(label, callback_data=f"q:acct:{name}")]
            for name, label in labels.items()]
    rows.append([InlineKeyboardButton("◀ Oracle 菜单", callback_data="o:menu")])
    return InlineKeyboardMarkup(rows)


def quota_regions_keyboard(profile, regions):
    """额度页二级菜单：某账号的订阅区域列表。

    regions 是 list_regions 的返回（region-name / is-home-region）。
    每行一个区域按钮（q:region:<profile>:<region>），Home 区域后缀
    “（Home）”；最后一行回账号列表。
    """
    from tgbot.config import get_region_label

    rows = []
    for region in regions:
        code = region.get("region-name") or region.get("region-key") or "?"
        label = get_region_label(code)
        if region.get("is-home-region"):
            label += "（Home）"
        rows.append([InlineKeyboardButton(
            label, callback_data=f"q:region:{profile}:{code}")])
    rows.append([InlineKeyboardButton("◀ 返回账号列表",
                                     callback_data="q:list")])
    return InlineKeyboardMarkup(rows)


def back_to_oracle_keyboard():
    """通用返回按钮（回 /oracle 菜单）。"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("◀ Oracle 菜单", callback_data="o:menu")],
    ])


def domain_zones_keyboard(zones):
    """`/domain` Zone 列表键盘：每 Zone 一个按钮。

    zones 是 CloudflareClient.list_zones() 的返回值，每项含 id/name。
    """
    rows = []
    for zone in zones:
        name = zone.get("name") or zone.get("id", "?")
        status = zone.get("status") or ""
        label = f"🌐 {name}" + (f"（{status}）" if status != "active" else "")
        rows.append([InlineKeyboardButton(
            label, callback_data=f"domain:zone:{zone.get('id')}")])
    rows.append([InlineKeyboardButton("◀ Oracle 菜单", callback_data="o:menu")])
    return InlineKeyboardMarkup(rows)


def domain_records_keyboard(zone_id, records):
    """`/domain` 记录列表键盘：刷新 + 返回 Zone 列表。

    records 是当前 Zone 的记录列表（保留在签名里，供后续按记录
    操作时扩展；当前版本记录只做只读列表展示，不挂单条按钮）。
    """
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 刷新",
                              callback_data=f"domain:zone:{zone_id}"),
         InlineKeyboardButton("◀ Zone 列表", callback_data="domain:back")],
    ])


def traffic_guard_keyboard(cfg, presets=None):
    """流量守护菜单键盘：开关 / 切换动作 / 阈值预设 / 解除限制。

    cfg 是 tgbot.monitoring.load_traffic_guard() 的返回值；
    presets 是阈值预设（GB），默认 monitoring 的
    TRAFFIC_GUARD_THRESHOLD_PRESETS，当前阈值标 ✅。
    动作按钮三选一循环：仅通知 → 阻断断网 → 限速200K。
    """
    if presets is None:
        presets = TRAFFIC_GUARD_THRESHOLD_PRESETS
    enabled = bool(cfg.get("enabled"))
    action = cfg.get("action", "notify")
    threshold = cfg.get("threshold_gb")
    rows = [
        [InlineKeyboardButton("✅ 关闭流量守护" if enabled else "🚀 开启流量守护",
                              callback_data="g:toggle")],
        [InlineKeyboardButton(
            f"动作：{traffic_guard_action_label(action)}（点切换）",
            callback_data="g:action")],
    ]
    for i in range(0, len(presets), 2):
        row = []
        for gb in presets[i:i + 2]:
            mark = "✅ " if gb == threshold else ""
            row.append(InlineKeyboardButton(f"{mark}阈值 {fmt_threshold_gb(gb)}",
                                            callback_data=f"g:threshold:{gb}"))
        rows.append(row)
    rows.append([InlineKeyboardButton("🔓 解除限制（取消阻断/限速）",
                                      callback_data="g:unblock")])
    rows.append([InlineKeyboardButton("◀ Oracle 菜单", callback_data="o:menu")])
    return InlineKeyboardMarkup(rows)


def keepalive_keyboard(instances, cfg, state=None):
    """停机保活菜单键盘：每台实例一个开关按钮。

    instances 是 instance.list_instances 返回的精简列表；
    cfg 是 load_keepalive() 的返回值；state 是 load_keepalive_state()
    的返回值（停试的实例标 ⚠️）。
    """
    gave_up = set((state or {}).get("gave_up") or [])
    rows = []
    for idx, inst in enumerate(instances):
        ocid = inst.get("id")
        name = inst.get("display-name") or (ocid or "")[-4:]
        mark = "✅" if cfg.get(ocid) else "⬜"
        warn = " ⚠️已停试" if ocid in gave_up else ""
        rows.append([InlineKeyboardButton(f"{mark} {name}{warn}",
                                          callback_data=f"k:toggle:{idx}")])
    rows.append([InlineKeyboardButton("◀ Oracle 菜单", callback_data="o:menu")])
    return InlineKeyboardMarkup(rows)


def ssl_watch_keyboard(domains):
    """SSL 监控菜单键盘：添加域名 + 每域名一个删除按钮 + 刷新。"""
    rows = [[InlineKeyboardButton("➕ 添加域名", callback_data="s:add")]]
    for idx, domain in enumerate(domains):
        rows.append([InlineKeyboardButton(f"🗑️ 删除 {domain}",
                                          callback_data=f"s:del:{idx}")])
    rows.append([InlineKeyboardButton("🔄 刷新", callback_data="s:refresh"),
                 InlineKeyboardButton("◀ Oracle 菜单", callback_data="o:menu")])
    return InlineKeyboardMarkup(rows)


def ssl_add_cancel_keyboard():
    """SSL 监控添加域名时的取消键盘（等用户回复文字时用）。"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ 取消添加", callback_data="s:add_cancel")],
    ])


def bark_keyboard(enabled, configured):
    """Bark 通知设置键盘：设地址 / 设 Key / 测试推送 / 开关。

    enabled：推送开关；configured：地址 + 32 位 Key 是否配齐。
    测试推送不依赖开关（配齐即可测）。
    """
    rows = [
        [InlineKeyboardButton("🔗 设置 Bark 地址", callback_data="bark:url"),
         InlineKeyboardButton("🔑 设置加密 Key", callback_data="bark:key")],
        [InlineKeyboardButton("📲 测试推送", callback_data="bark:test")],
        [InlineKeyboardButton("✅ 关闭 Bark 推送" if enabled else "🚀 开启 Bark 推送",
                              callback_data="bark:toggle")],
        [InlineKeyboardButton("◀ Oracle 菜单", callback_data="o:menu")],
    ]
    return InlineKeyboardMarkup(rows)


def bark_input_cancel_keyboard():
    """Bark 设置等用户回复文字时的取消键盘。"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ 取消", callback_data="bark:cancel_input")],
    ])
