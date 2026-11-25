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
  - "b:edit:<idx>"   快捷开机：编辑第 idx 条配置（填进开机表单）
  - "b:del:<idx>"    快捷开机：删除第 idx 条配置（先确认）
  - "b:delok:<idx>"  快捷开机：确认删除第 idx 条配置
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
  - "bark:genkey" / "bark:genkey:go" / "bark:genkey:cancel"
    Bark 自动生成 Key：确认页 / 确定生成 / 取消（生成后旧 Key 立即失效）
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
  - "domain:add:<zone_id>"  /domain：添加记录（选类型）
  - "domain:addtype:<zone_id>:<TYPE>"  /domain：已选类型，开始输名称
  - "domain:add:confirm:<zone_id>"  /domain：确认添加记录
  - "domain:delpick:<zone_id>"  /domain：删除记录（选一条）
  - "domain:del:<zone_id>:<idx>"  /domain：删除某条记录的确认页
    （idx 是 user_data["domain_records"][zone_id] 的下标）
  - "domain:del:yes:<zone_id>:<idx>"  /domain：确认删除某条记录
  - "bd:start:<idx>"  绑定域名：从实例详情开始（idx 是 instances 下标）
  - "bd:zone:<idx>:<zidx>"  绑定域名：选主域名（zidx 是 Zone 列表下标）
  - "bd:overwrite"  绑定域名：确认覆盖已存在的同名 A 记录
  - "bd:cancel"  绑定域名：取消
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from tgbot import i18n as bot_i18n
from tgbot.cpu_policy import get_allowed_cpu_models
from tgbot.monitoring import (TRAFFIC_GUARD_THRESHOLD_PRESETS,
                              fmt_threshold_gb,
                              traffic_guard_action_label)
from tgbot.services import (
    instance_group_title,
    short_shape,
    sort_instances_by_group,
)

# ================= 配置区（改这里） =================
# M_Bot 开源仓库地址（/start 主菜单 GitHub 按钮与欢迎词共用）。
GITHUB_URL = "https://github.com/jimu2000/M_Bot"

# /oracle menu: (callback key, display text); items 1-34 are the numbered menu (source: docs/references.md), items 35+ were added later. /oracle 菜单：(callback key, 显示文字)，1-34 项是菜单编号（来源：docs/references.md）；35 项以后是后续新增。
# key is a short identifier; the display text carries the original number. key 是短标识，显示文字带原编号。
# Note: item 13 was removed (the menu top row already has a "切换账号" button, so menu item 13. profile管理 was a duplicate entry); the other numbers stay as-is and are never renumbered (1-34 are the numbers users call by habit, e.g. "菜单 19 流量"). Account switching itself is still there (the o:profiles callback + the top-row button). 注意：13 号已删（菜单顶行已有“切换账号”按钮，菜单里的 13. profile管理 是重复入口，没必要留）；其余编号保持不动，不重新编号（1-34 是用户叫惯的编号，如“菜单 19 流量”这类叫法都不变）。切换账号功能本身还在（o:profiles 回调 + 顶行按钮）。
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
    ("autoip", "16. GFW自动换IP"),
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
    ("rescue", "44. 救机"),
]
# Menu items per page (2 columns, auto-paginated; total pages follow the item count). 每页显示的菜单项数（2 列，自动分页；总页数随菜单项数变化）。
MENU_PAGE_SIZE = 22
# Menu items whose button text carries a dynamic on/off state: key -> a key in the states dict. 按钮文字带动态开关状态的菜单项：key -> states 字典里的键。
# When the menu is built with states={...}, matching buttons get an "(开启)" / "(关闭)" suffix; when states is missing or not a dict, the original text is shown (compatible with old callers). 生成菜单时传 states={...}，对应按钮会带上“(开启)”/“(关闭)”后缀；states 不传或不是 dict 时显示原文字（兼容旧调用）。
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


def oracle_menu_keyboard(page=0, states=None, current_account=None,
                         lang="zh"):
    """`/oracle` 菜单键盘：2 列排列，带上一页/下一页。

    states：可选的开关状态字典（键见 DYNAMIC_STATE_ITEMS），
    传了之后监控类按钮会带上“(开启)”/“(关闭)”后缀。
    current_account：当前账号显示名（如“新加坡（3 个区）”），传了之
    后菜单顶行多一条“🔄 切换账号：<当前账号>”按钮（点开切换账号）。
    lang：菜单语言（"zh" / "en"）；菜单底部带语言切换按钮（o:lang）。
    """
    items, page, total_pages = _menu_page(page)
    rows = []
    if current_account:
        rows.append([InlineKeyboardButton(
            bot_i18n.t(lang, "oracle.switch_account",
                       account=current_account),
            callback_data="o:profiles")])
    for i in range(0, len(items), 2):
        row = []
        for key, _text in items[i:i + 2]:
            text = bot_i18n.menu_item_label(key, lang)
            state_key = DYNAMIC_STATE_ITEMS.get(key)
            if state_key is not None and isinstance(states, dict):
                state = bot_i18n.t(lang, "common.on" if states.get(state_key)
                                   else "common.off")
                text = f"{text} ({state})"
            row.append(InlineKeyboardButton(text, callback_data=f"o:{key}"))
        rows.append(row)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            bot_i18n.t(lang, "oracle.prev_page"),
            callback_data=f"o:page:{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}",
                                    callback_data="x"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(
            bot_i18n.t(lang, "oracle.next_page"),
            callback_data=f"o:page:{page + 1}"))
    rows.append(nav)
    lang_key = "lang.toggle_en" if lang == "zh" else "lang.toggle_zh"
    rows.append([InlineKeyboardButton(bot_i18n.t(lang, lang_key),
                                      callback_data="o:lang")])
    return InlineKeyboardMarkup(rows)


def main_menu_keyboard(lang="zh"):
    """`/start` 主菜单：最常用的几个入口。"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(bot_i18n.t(lang, "menu.main_oracle"),
                              callback_data="o:menu"),
         InlineKeyboardButton(bot_i18n.t(lang, "menu.main_instances"),
                              callback_data="i:list")],
        [InlineKeyboardButton(bot_i18n.t(lang, "menu.main_quota"),
                              callback_data="o:quota"),
         InlineKeyboardButton(bot_i18n.t(lang, "menu.main_traffic"),
                              callback_data="o:traffic")],
        [InlineKeyboardButton(bot_i18n.t(lang, "menu.main_tasks"),
                              callback_data="o:tasks"),
         InlineKeyboardButton(bot_i18n.t(lang, "menu.main_github"),
                              url=GITHUB_URL)],
        [InlineKeyboardButton(
            bot_i18n.t(lang, "lang.toggle_en" if lang == "zh"
                       else "lang.toggle_zh"),
            callback_data="o:lang")],
    ])


def _instance_button_text(inst, lang="zh"):
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
    state_word = {"RUNNING": bot_i18n.t(lang, "instances.state_running"),
                  "STOPPED": bot_i18n.t(lang, "instances.state_stopped"),
                  "TERMINATED": bot_i18n.t(lang, "instances.state_terminated"),
                  }.get(state, state or "?")
    return f"{name} · {shape} · {state_word}"


def instance_list_keyboard(instances, lang="zh"):
    """实例列表键盘：按状态分组，每组一行标题按钮 + 每台一个按钮。

    instances 是 instance.list_instances 返回的精简列表。
    分组顺序与 fmt_instance_list 一致（RUNNING → STOPPED →
    其他），已终止的不显示，共用 sort_instances_by_group。
    标题按钮的 callback_data 是 "i:noop"（点它只应答，不做任何事）；
    实例按钮的 callback_data 带原列表下标（不是分组后的下标），
    因为 store_instances 存的是原始顺序列表，回调按原下标取回。
    实例按钮文字带规格简称（A1/E2）与公网 IP（运行中才有）。
    """
    ordered = sort_instances_by_group(instances)
    counts = {}
    for _idx, inst in ordered:
        title = instance_group_title(inst.get("lifecycle-state"), lang=lang)
        counts[title] = counts.get(title, 0) + 1
    rows = []
    current_group = None
    for idx, inst in ordered:
        group = instance_group_title(inst.get("lifecycle-state"), lang=lang)
        if group != current_group:
            current_group = group
            count_text = (f"{counts[group]} 台" if lang == "zh"
                          else f"{counts[group]}")
            rows.append([InlineKeyboardButton(
                f"{group}（{count_text}）：", callback_data="i:noop")])
        rows.append([InlineKeyboardButton(
            _instance_button_text(inst, lang=lang),
            callback_data=f"i:detail:{idx}")])
    rows.append([InlineKeyboardButton(bot_i18n.t(lang, "common.refresh"),
                                      callback_data="i:list"),
                 InlineKeyboardButton(bot_i18n.t(lang, "common.back_oracle"),
                                      callback_data="o:menu")])
    return InlineKeyboardMarkup(rows)


def instance_actions_keyboard(idx, state="RUNNING", lang="zh"):
    """实例操作键盘：按当前状态显示可用的操作。

    idx 是 instances 列表下标；state 是 lifecycle-state。
    """
    rows = []
    if state == "RUNNING":
        rows.append([
            InlineKeyboardButton(bot_i18n.t(lang, "instances.action_stop"),
                                 callback_data=f"i:stop:{idx}"),
            InlineKeyboardButton(bot_i18n.t(lang, "instances.action_restart"),
                                 callback_data=f"i:restart:{idx}"),
        ])
    elif state == "STOPPED":
        rows.append([
            InlineKeyboardButton(bot_i18n.t(lang, "instances.action_start"),
                                 callback_data=f"i:start:{idx}"),
        ])
    rows.append([
        InlineKeyboardButton(bot_i18n.t(lang, "instances.action_change_ip"),
                             callback_data=f"i:ip:{idx}"),
        InlineKeyboardButton(bot_i18n.t(lang, "instances.action_resize"),
                             callback_data=f"i:resize:{idx}"),
    ])
    rows.append([
        InlineKeyboardButton(bot_i18n.t(lang, "instances.action_rename"),
                             callback_data=f"i:rename:{idx}"),
        InlineKeyboardButton(bot_i18n.t(lang, "instances.action_reinstall"),
                             callback_data=f"i:reinstall:{idx}"),
    ])
    if state in ("RUNNING", "STOPPED"):
        rows.append([
            InlineKeyboardButton(
                bot_i18n.t(lang, "instances.action_boot_backup"),
                callback_data=f"i:bootbackup:{idx}"),
            InlineKeyboardButton(
                bot_i18n.t(lang, "instances.action_boot_restore"),
                callback_data=f"i:bootswap:{idx}"),
        ])
        rows.append([
            InlineKeyboardButton(
                bot_i18n.t(lang, "instances.action_boot_backup_del"),
                callback_data=f"i:bootbackup-del:{idx}"),
        ])
    rows.append([
        InlineKeyboardButton(bot_i18n.t(lang, "instances.action_bind_domain"),
                             callback_data=f"bd:start:{idx}"),
    ])
    rows.append([
        InlineKeyboardButton(bot_i18n.t(lang, "instances.action_console"),
                             callback_data=f"i:console:{idx}"),
    ])
    rows.append([
        InlineKeyboardButton(bot_i18n.t(lang, "instances.action_terminate"),
                             callback_data=f"i:terminate:{idx}"),
        InlineKeyboardButton(bot_i18n.t(lang, "instances.action_list"),
                             callback_data="i:list"),
    ])
    return InlineKeyboardMarkup(rows)


def batch_select_keyboard(instances, selected, lang="zh"):
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
    next_text = ("➡️ 下一步：选动作" if lang == "zh"
                 else "➡️ Next: pick an action")
    rows.append([InlineKeyboardButton(next_text,
                                      callback_data="ba:next")])
    rows.append([InlineKeyboardButton(bot_i18n.t(lang, "common.back_oracle"),
                                      callback_data="o:menu")])
    return InlineKeyboardMarkup(rows)


def batch_action_keyboard(lang="zh"):
    """批量操作动作选择键盘（开机/关机/重启/删除）。"""
    labels = {
        "zh": ("▶️ 批量开机", "⏹️ 批量关机", "🔄 批量重启", "🗑️ 批量删除",
               "◀ 返回选择"),
        "en": ("▶️ Batch Start", "⏹️ Batch Stop", "🔄 Batch Restart",
               "🗑️ Batch Delete", "◀ Back to Selection"),
    }
    start, stop, restart, delete, back = labels.get(lang, labels["zh"])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(start, callback_data="ba:action:start"),
         InlineKeyboardButton(stop, callback_data="ba:action:stop")],
        [InlineKeyboardButton(restart, callback_data="ba:action:restart"),
         InlineKeyboardButton(delete, callback_data="ba:action:terminate")],
        [InlineKeyboardButton(back, callback_data="o:batch")],
    ])


def confirm_keyboard(action, idx, confirm_text=None, lang="zh"):
    """危险操作二次确认键盘。

    action: start/stop/restart/terminate/changeip/resize/delete 等；
    idx: instances（或 volumes）列表下标。
    """
    if confirm_text is None:
        confirm_text = bot_i18n.t(lang, "common.confirm")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(confirm_text,
                              callback_data=f"c:{action}:{idx}")],
        [InlineKeyboardButton(bot_i18n.t(lang, "common.cancel_button"),
                              callback_data="x")],
    ])


def boot_preset_keyboard(lang="zh"):
    """开机预设选择键盘（快速配置预填）。"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("AMD 微型 1C/1G（E2.1.Micro）",
                              callback_data="b:boot:e2")],
        [InlineKeyboardButton("ARM A1 1C/6G（小）",
                              callback_data="b:boot:a1small")],
        [InlineKeyboardButton("ARM A1 4C/24G（满配）",
                              callback_data="b:boot:a1full")],
        [InlineKeyboardButton(bot_i18n.t(lang, "common.back_oracle"),
                              callback_data="o:menu")],
    ])


def boot_confirm_keyboard(preset=None, lang="zh"):
    """开机确认键盘（点开始抢机后进后台任务）。

    E2 才有 CPU 选项行（只抢配置里的型号 / 什么 CPU 都可以），放在
    “开始抢机”上面；当前选中的带 ✅。型号列表读 cpu_policy.json，
    默认 7J13。A1 不筛 CPU，不显示该行。
    preset 为 None 时按旧行为（无选项行，兼容旧调用）。
    """
    rows = []
    if preset is not None and preset.get("shape") == "VM.Standard.E2.1.Micro":
        only_allowed = bool(preset.get("target_cpu"))
        cpu_label = bot_i18n.t(lang, "boot.cpu_only",
                               models="/".join(get_allowed_cpu_models()))
        rows.append([
            InlineKeyboardButton(
                ("✅ " if only_allowed else "") + cpu_label,
                callback_data="b:cpu:7j13"),
            InlineKeyboardButton(
                ("✅ " if not only_allowed else "")
                + bot_i18n.t(lang, "boot.cpu_any"),
                callback_data="b:cpu:any"),
        ])
    rows.append([InlineKeyboardButton(bot_i18n.t(lang, "boot.start_grab"),
                                      callback_data="b:go")])
    rows.append([InlineKeyboardButton(bot_i18n.t(lang, "common.cancel_button"),
                                      callback_data="x")])
    return InlineKeyboardMarkup(rows)


def task_list_keyboard(tasks, lang="zh"):
    """任务列表键盘：每条运行中任务一个取消按钮。

    tasks 是 TaskRegistry.list_all() 返回的列表，每项含 id/kind/status。
    按钮文字用任务的 label（在抢什么机器），没有 label 的旧任务
    退回短 ID；callback_data 始终是短 ID。
    """
    rows = []
    for task in tasks:
        if task.get("status") in ("running", "pending"):
            label = task.get("label") or task.get("id")
            rows.append([InlineKeyboardButton(
                bot_i18n.t(lang, "tasks.cancel", label=label),
                callback_data=f"t:cancel:{task.get('id')}")])
    rows.append([InlineKeyboardButton(bot_i18n.t(lang, "common.refresh"),
                                      callback_data="o:tasks"),
                 InlineKeyboardButton(bot_i18n.t(lang, "common.back_oracle"),
                                      callback_data="o:menu")])
    return InlineKeyboardMarkup(rows)


def profile_keyboard(profiles, current, labels=None, lang="zh"):
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
    rows.append([InlineKeyboardButton(bot_i18n.t(lang, "common.back_oracle"),
                                      callback_data="o:menu")])
    return InlineKeyboardMarkup(rows)


def profile_regions_keyboard(profile, regions, current=None, lang="zh"):
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
    back_text = ("◀ 返回账号列表" if lang == "zh"
                 else "◀ Back to Accounts")
    rows.append([InlineKeyboardButton(back_text,
                                     callback_data="o:profiles")])
    return InlineKeyboardMarkup(rows)


def quota_accounts_keyboard(labels, lang="zh"):
    """额度页账号列表键盘：labels 是 {profile: 显示名}（主区中文名，
    多区账号后缀区数，如“新加坡（3 个区）”，与切换菜单一致）。

    每行一个账号按钮（q:acct:<profile>），最后一行回 Oracle 菜单。
    """
    rows = [[InlineKeyboardButton(label, callback_data=f"q:acct:{name}")]
            for name, label in labels.items()]
    rows.append([InlineKeyboardButton(bot_i18n.t(lang, "common.back_oracle"),
                                      callback_data="o:menu")])
    return InlineKeyboardMarkup(rows)


def quota_regions_keyboard(profile, regions, lang="zh"):
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
    back_text = ("◀ 返回账号列表" if lang == "zh"
                 else "◀ Back to Accounts")
    rows.append([InlineKeyboardButton(back_text,
                                     callback_data="q:list")])
    return InlineKeyboardMarkup(rows)


def back_to_oracle_keyboard(lang="zh"):
    """通用返回按钮（回 /oracle 菜单）。"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(bot_i18n.t(lang, "common.back_oracle"),
                              callback_data="o:menu")],
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
    rows.append([InlineKeyboardButton("⚙️ 凭据管理", callback_data="cf:manage")])
    rows.append([InlineKeyboardButton("◀ Oracle 菜单", callback_data="o:menu")])
    return InlineKeyboardMarkup(rows)


def cf_config_keyboard():
    """`/domain` 未配置凭据时的键盘：手动添加 / 从服务器提取。"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ 手动添加凭据", callback_data="cf:add")],
        [InlineKeyboardButton("🔍 从服务器提取", callback_data="cf:extract")],
        [InlineKeyboardButton("◀ Oracle 菜单", callback_data="o:menu")],
    ])


def cf_add_mode_keyboard():
    """手动添加凭据：选 API Token 还是 Global API Key + 邮箱。"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("API Token（推荐）",
                              callback_data="cf:add:token")],
        [InlineKeyboardButton("Global API Key + 邮箱",
                              callback_data="cf:add:key")],
        [InlineKeyboardButton("❌ 取消", callback_data="cf:cancel")],
    ])


def cf_manage_keyboard():
    """凭据管理页键盘。"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 更换凭据", callback_data="cf:add")],
        [InlineKeyboardButton("🗑️ 删除已保存凭据",
                              callback_data="cf:delete")],
        [InlineKeyboardButton("🌐 查看 Zone 列表",
                              callback_data="domain:back")],
        [InlineKeyboardButton("◀ Oracle 菜单", callback_data="o:menu")],
    ])


def cf_delete_confirm_keyboard():
    """删除已保存凭据的二次确认键盘。"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑️ 确认删除",
                              callback_data="cf:delete:yes")],
        [InlineKeyboardButton("❌ 取消", callback_data="cf:manage")],
    ])


def cf_extract_hosts_keyboard(hosts):
    """从服务器提取：选一台运行中的实例（hosts 是精简实例列表）。"""
    rows = []
    for idx, inst in enumerate(hosts):
        name = inst.get("display-name") or (inst.get("id") or "?")[-6:]
        ip = inst.get("public-ip") or ""
        rows.append([InlineKeyboardButton(
            f"{name}（{ip}）", callback_data=f"cf:extract:{idx}")])
    rows.append([InlineKeyboardButton("❌ 取消", callback_data="cf:cancel")])
    return InlineKeyboardMarkup(rows)


def cf_cancel_keyboard():
    """输入凭据时的取消按钮。"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ 取消", callback_data="cf:cancel")],
    ])


def domain_records_keyboard(zone_id, records):
    """`/domain` 记录列表键盘：添加 / 删除 / 刷新 / 返回 Zone 列表。

    records 是当前 Zone 的记录列表（保留在签名里，供后续扩展）。
    """
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ 添加记录",
                              callback_data=f"domain:add:{zone_id}")],
        [InlineKeyboardButton("🗑️ 删除记录",
                              callback_data=f"domain:delpick:{zone_id}")],
        [InlineKeyboardButton("🔄 刷新",
                              callback_data=f"domain:zone:{zone_id}"),
         InlineKeyboardButton("◀ Zone 列表", callback_data="domain:back")],
    ])


# /domain command: max DNS records shown in one message; beyond that, truncate and show the total. /domain 命令：单条消息最多显示的 DNS 记录数，超出时截断并提示总数。
# The bot and the web panel share this same limit; deleting records also lists only the first that many. 机器人与网页端共用同一上限；删除选记录时也只列前这么多条。
DOMAIN_RECORDS_LIMIT = 20


# Record types offered when adding a record via /domain (the five common ones Cloudflare allows; NS/SRV/CAA are uncommon, not offered by the bot, but present in the web form). /domain 添加记录时可选的类型（Cloudflare 允许的常用五种；NS/SRV/CAA 不常用，机器人端不提供，网页端表单里有）。
DNS_ADD_TYPES = ("A", "AAAA", "CNAME", "TXT", "MX")


def domain_add_type_keyboard(zone_id):
    """`/domain` 添加记录：选记录类型（两列），取消回记录列表。"""
    rows = []
    for i in range(0, len(DNS_ADD_TYPES), 2):
        row = []
        for rtype in DNS_ADD_TYPES[i:i + 2]:
            row.append(InlineKeyboardButton(
                rtype, callback_data=f"domain:addtype:{zone_id}:{rtype}"))
        rows.append(row)
    rows.append([InlineKeyboardButton(
        "❌ 取消", callback_data=f"domain:zone:{zone_id}")])
    return InlineKeyboardMarkup(rows)


def domain_add_confirm_keyboard(zone_id):
    """`/domain` 添加记录：确认页键盘（确认添加 / 取消回记录列表）。"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ 确认添加",
                              callback_data=f"domain:add:confirm:{zone_id}")],
        [InlineKeyboardButton("❌ 取消",
                              callback_data=f"domain:zone:{zone_id}")],
    ])


def domain_add_cancel_keyboard(zone_id):
    """`/domain` 添加记录：输入名称/内容时的取消按钮。"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ 取消添加",
                              callback_data=f"domain:zone:{zone_id}")],
    ])


def domain_delete_pick_keyboard(zone_id, records, bound_map=None):
    """`/domain` 删除记录：每条记录一个按钮（只列前 20 条，与展示一致）。

    records 是 user_data 里存的当前 Zone 记录列表；idx 是下标。
    bound_map（公网 IP -> 实例名）给出时，已绑定到实例的 A 记录排
    在前面并带 🔗 实例名，方便用户先找到要删的绑定记录。
    """
    bound_map = bound_map or {}
    shown = list(enumerate(records[:DOMAIN_RECORDS_LIMIT]))

    def _is_bound(item):
        _, record = item
        return (record.get("type") == "A"
                and record.get("content") in bound_map)

    shown.sort(key=lambda item: (not _is_bound(item), item[0]))
    rows = []
    for idx, record in shown:
        label = (f"🗑️ {record.get('type', '?')} "
                 f"{record.get('name', '?')}")
        if _is_bound((idx, record)):
            label += f" 🔗{bound_map[record['content']]}"
        rows.append([InlineKeyboardButton(
            label, callback_data=f"domain:del:{zone_id}:{idx}")])
    rows.append([InlineKeyboardButton(
        "◀ 返回记录列表", callback_data=f"domain:zone:{zone_id}")])
    return InlineKeyboardMarkup(rows)


def domain_delete_confirm_keyboard(zone_id, idx):
    """`/domain` 删除记录：二次确认键盘（确认删除 / 取消回选记录）。"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "🗑️ 确认删除",
            callback_data=f"domain:del:yes:{zone_id}:{idx}")],
        [InlineKeyboardButton(
            "❌ 取消", callback_data=f"domain:delpick:{zone_id}")],
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
    """Bark 通知设置键盘：设地址 / 设 Key / 自动生成 Key /
    测试推送 / 开关。

    enabled：推送开关；configured：地址 + 32 位 Key 是否配齐。
    测试推送不依赖开关（配齐即可测）。自动生成 Key 先出确认页
    （生成后旧 Key 立即失效）。
    """
    rows = [
        [InlineKeyboardButton("🔗 设置 Bark 地址", callback_data="bark:url"),
         InlineKeyboardButton("🔑 设置加密 Key", callback_data="bark:key")],
        [InlineKeyboardButton("🎲 自动生成 Key", callback_data="bark:genkey"),
         InlineKeyboardButton("📲 测试推送", callback_data="bark:test")],
        [InlineKeyboardButton("✅ 关闭 Bark 推送" if enabled else "🚀 开启 Bark 推送",
                              callback_data="bark:toggle")],
        [InlineKeyboardButton("◀ Oracle 菜单", callback_data="o:menu")],
    ]
    return InlineKeyboardMarkup(rows)


def bark_genkey_confirm_keyboard():
    """自动生成 Bark Key 前的确认键盘（生成后旧 Key 立即失效）。"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ 确定生成", callback_data="bark:genkey:go")],
        [InlineKeyboardButton("❌ 取消", callback_data="bark:genkey:cancel")],
    ])


def bark_input_cancel_keyboard():
    """Bark 设置等用户回复文字时的取消键盘。"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ 取消", callback_data="bark:cancel_input")],
    ])


def gfw_keyboard(instances, enabled_ids, configured):
    """GFW 自动换 IP（菜单 16）键盘：每台实例一个开关按钮。

    instances：当前账号/区域的 RUNNING 实例 list（带 public-ip，
    下标与 user_data["gfw_instances"] 对应）；enabled_ids：已开启
    监控的实例 id 集合；configured：是否已设置 Atlas API Key。
    没配 Key 时“立即检查”按钮不出现（点了也跑不起来，先去设置 Key）。
    """
    rows = []
    for idx, inst in enumerate(instances):
        name = inst.get("display-name") or "?"
        ip = inst.get("public-ip") or "无公网 IP"
        mark = "✅" if inst.get("id") in enabled_ids else "☐"
        rows.append([InlineKeyboardButton(
            f"{mark} {name} · {ip}",
            callback_data=f"gfw:toggle:{idx}")])
    action_row = []
    if configured:
        action_row.append(InlineKeyboardButton(
            "🔍 立即检查", callback_data="gfw:check"))
    action_row.append(InlineKeyboardButton(
        "🔑 设置 API Key", callback_data="gfw:key"))
    rows.append(action_row)
    rows.append([InlineKeyboardButton("◀ Oracle 菜单",
                                     callback_data="o:menu")])
    return InlineKeyboardMarkup(rows)


def gfw_input_cancel_keyboard():
    """GFW 设置等用户回复 API Key 时的取消键盘。"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ 取消", callback_data="gfw:cancel_input")],
    ])
