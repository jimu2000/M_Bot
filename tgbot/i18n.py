#!/usr/bin/env python3
"""Telegram 机器人中英文切换（v2.01）。

语言偏好按 Telegram 用户 ID 持久化（与网页端 localStorage 的
mbot-lang 各自独立存，不强制同步）。默认简体中文。

存储：tgbot/data/user_lang.json，0600 权限，格式 {"<user_id>": "zh"|"en"}。
The language file path can be overridden with the MBOT_TGBOT_DATA_DIR
environment variable / 可用 MBOT_TGBOT_DATA_DIR 环境变量改数据目录。

用法：
  - lang_of(update, context)：当前用户的语言（先读 user_data 缓存，
    再读文件，默认 "zh"）；handler 里拿文案用 tr(update, context, key)。
  - t(lang, key, **kw)：按语言取字典文案并 format；缺键时回退中文，
    中文也没有时返回 key 本身（不崩）。
"""

import json
import os
import tempfile

from tgbot import config as bot_config

SUPPORTED_LANGS = ("zh", "en")
DEFAULT_LANG = "zh"
# 语言文件名 / Language preference file name.
LANG_FILENAME = "user_lang.json"

# Translation dict: key -> {zh, en}. English must be accurate and natural, not machine-translated. 翻译字典：键 -> {zh, en}。英文要准确自然，不要机翻腔。
STRINGS = {
    # ---- 通用 ----
    "common.cancel": {"zh": "已取消。", "en": "Cancelled."},
    "common.cancel_button": {"zh": "❌ 取消", "en": "❌ Cancel"},
    "common.back_oracle": {"zh": "◀ Oracle 菜单", "en": "◀ Oracle Menu"},
    "common.back": {"zh": "◀ 返回", "en": "◀ Back"},
    "common.confirm": {"zh": "✅ 确认执行", "en": "✅ Confirm"},
    "common.refresh": {"zh": "🔄 刷新", "en": "🔄 Refresh"},
    "common.on": {"zh": "开启", "en": "On"},
    "common.off": {"zh": "关闭", "en": "Off"},
    "common.none": {"zh": "无", "en": "None"},
    "common.unknown": {"zh": "未知", "en": "Unknown"},
    "common.detecting": {"zh": "检测中", "en": "Detecting…"},
    "common.not_implemented": {"zh": "该功能暂未实现。",
                               "en": "This feature is not implemented yet."},
    "common.unknown_command": {"zh": "未知命令，输入 /help 查看可用命令。",
                               "en": "Unknown command. Send /help to see "
                                     "available commands."},
    "common.denied": {"zh": "⛔ 未授权：你的 Telegram 账号不在白名单里，"
                            "无法使用本机器人。",
                      "en": "⛔ Unauthorized: your Telegram account is not "
                            "on the allowlist, so you can't use this bot."},
    "common.instance_gone": {"zh": "实例不存在或列表已过期，"
                                   "请重新打开实例列表。",
                             "en": "Instance not found or the list has "
                                   "expired. Please reopen the instance list."},
    "common.param_error": {"zh": "参数错误，请重试。",
                           "en": "Invalid parameter, please try again."},
    # ---- 语言切换 ----
    "lang.toggle_zh": {"zh": "🌐 语言：中文", "en": "🌐 Language: 中文"},
    "lang.toggle_en": {"zh": "🌐 Language: English",
                       "en": "🌐 Language: English"},
    "lang.switched_zh": {"zh": "✅ 已切换到中文。",
                         "en": "✅ Switched to Chinese."},
    "lang.switched_en": {"zh": "✅ 已切换到 English。",
                         "en": "✅ Switched to English."},
    "lang.command_hint": {"zh": "语言切换：点菜单里的“🌐 语言”按钮，"
                                "或发送 /lang 切换中英文。",
                          "en": "Switch language: tap the “🌐 Language” "
                                "button in the menu, or send /lang to toggle "
                                "between Chinese and English."},
    # ---- 欢迎 / 帮助 ----
    "welcome": {"zh": "👋 你好！我是 M-探长，M_Bot Oracle 开源管理面板的机器人。\n"
                      "\n"
                      "我可以帮你管理 Oracle Cloud 免费层实例：查看实例、启停、"
                      "换 IP、查配额、抢机等。\n"
                      "\n"
                      "GitHub 免费开源项目：https://github.com/jimu2000/M_Bot\n"
                      "\n"
                      "常用命令：\n"
                      "/oracle — Oracle 菜单（{count} 项功能）\n"
                      "/instances — 实例列表\n"
                      "/help — 命令清单",
                "en": "👋 Hi! I'm M-Detective, the bot for the M_Bot Oracle "
                      "open-source management panel.\n"
                      "\n"
                      "I can manage your Oracle Cloud free-tier instances: "
                      "list instances, start/stop, change IP, check quota, "
                      "grab instances, and more.\n"
                      "\n"
                      "GitHub Free Open Source Project: "
                      "https://github.com/jimu2000/M_Bot\n"
                      "\n"
                      "Common commands:\n"
                      "/oracle — Oracle menu ({count} features)\n"
                      "/instances — Instance list\n"
                      "/help — Command list"},
    "help": {"zh": "📖 命令清单：\n"
                   "\n"
                   "/start — 欢迎与主菜单\n"
                   "/help — 显示本清单\n"
                   "/oracle — Oracle 菜单（开机、实例、配额、流量等 {count} 项）\n"
                   "/instances — 列出当前账号的实例\n"
                   "/lang — 切换语言（中文 / English）\n"
                   "/bark — Bark 通知设置（地址 / 加密 Key / 测试推送 / 开关）\n"
                   "/rescue — 救机（前置检查 / 安全恢复 / 无盘救机 / 孤儿盘 / Netboot 向导 / 控制台）\n"
                   "/domain — 域名管理（Cloudflare DNS）\n"
                   "\n"
                   "其他云（需先配置对应账号）：\n"
                   "/azure — Azure 云管理\n"
                   "/aws — AWS 云管理\n"
                   "/gcp — GCP 云管理\n"
                   "/do — DigitalOcean 实例管理\n"
                   "/vf — VirtFusion 实例管理\n"
                   "/solusvm — SolusVM 面板管理\n"
                   "\n"
                   "客户端工具：\n"
                   "/oci — 上传甲骨文或 azure 配置\n"
                   "/command — 在已保存主机上远程执行 Shell 命令\n"
                   "/pubkey — 管理 SSH 公钥（列表/添加/删除）\n"
                   "/oproxy — 查看/设置机器人出站代理\n"
                   "/uploadapi — 添加 OCI API 配置（上传 config 文件或粘贴文本）\n"
                   "\n"
                   "管理：\n"
                   "/clearlock — 清除面板登录失败锁定\n"
                   "/raninfo — 重置面板管理员密码\n"
                   "/me — 查看我的信息\n"
                   "/admin — 管理员菜单（面板状态 / 重启服务 / 版本）\n"
                   "\n"
                   "实例操作都在消息下方的按钮里完成：点实例进详情，"
                   "再点启动 / 停止 / 重启 / 换 IP / 升降配 / 终止。"
                   "危险操作（停止、终止）执行前会二次确认。",
             "en": "📖 Command list:\n"
                   "\n"
                   "/start — Welcome and main menu\n"
                   "/help — Show this list\n"
                   "/oracle — Oracle menu (boot, instances, quota, traffic, "
                   "{count} features in total)\n"
                   "/instances — List instances of the current account\n"
                   "/lang — Switch language (中文 / English)\n"
                   "/bark — Bark notification settings (URL / encryption key / "
                   "test push / on-off)\n"
                   "/rescue — Rescue (preflight / safe restore / diskless "
                   "rebuild / orphans / netboot guide / console)\n"
                   "/domain — Domain management (Cloudflare DNS)\n"
                   "\n"
                   "Other clouds (configure the account first):\n"
                   "/azure — Azure\n"
                   "/aws — AWS\n"
                   "/gcp — GCP\n"
                   "/do — DigitalOcean instances\n"
                   "/vf — VirtFusion instances\n"
                   "/solusvm — SolusVM panel\n"
                   "\n"
                   "Client tools:\n"
                   "/oci — Upload Oracle or Azure config\n"
                   "/command — Run a shell command on a saved host\n"
                   "/pubkey — Manage SSH public keys (list / add / delete)\n"
                   "/oproxy — View / set the bot's outbound proxy\n"
                   "/uploadapi — Add an OCI API config (upload a config file "
                   "or paste text)\n"
                   "\n"
                   "Admin:\n"
                   "/clearlock — Clear the panel login lockout\n"
                   "/raninfo — Reset the panel admin password\n"
                   "/me — My info\n"
                   "/admin — Admin menu (panel status / restart services / "
                   "version)\n"
                   "\n"
                   "Instance actions live in the buttons below the message: "
                   "tap an instance for details, then start / stop / restart / "
                   "change IP / resize / terminate. Dangerous actions (stop, "
                   "terminate) ask for confirmation first."},
    # ---- Oracle 菜单 ----
    "oracle.menu_title": {"zh": "☁️ Oracle 菜单（共 {count} 项，点按钮使用）：",
                          "en": "☁️ Oracle menu ({count} features, tap a "
                                "button to use):"},
    "oracle.switch_account": {"zh": "🔄 切换账号：{account}",
                              "en": "🔄 Switch account: {account}"},
    "oracle.prev_page": {"zh": "◀ 上一页", "en": "◀ Previous"},
    "oracle.next_page": {"zh": "下一页 ▶", "en": "Next ▶"},
    "menu.main_oracle": {"zh": "☁️ Oracle 菜单", "en": "☁️ Oracle Menu"},
    "menu.main_instances": {"zh": "🖥️ 实例列表", "en": "🖥️ Instances"},
    "menu.main_quota": {"zh": "📊 查询配额", "en": "📊 Quota"},
    "menu.main_traffic": {"zh": "📈 本月流量", "en": "📈 This Month's Traffic"},
    "menu.main_tasks": {"zh": "📋 运行中的任务", "en": "📋 Running Tasks"},
    "menu.main_github": {"zh": "GitHub 免费开源项目",
                         "en": "GitHub Free Open Source Project"},
    # ---- /oracle 菜单项（编号与 keyboards.ORACLE_MENU_ITEMS 一致） ----
    "menu.boot": {"zh": "1. 开机(刷机)", "en": "1. Boot (Grab)"},
    "menu.ip": {"zh": "2. ip管理", "en": "2. IP Management"},
    "menu.resume": {"zh": "3. 被暂停开机", "en": "3. Resume Paused Boot"},
    "menu.ipv6": {"zh": "4. ipv6管理", "en": "4. IPv6 Management"},
    "menu.resize": {"zh": "5. 升/降级实例", "en": "5. Resize Instance"},
    "menu.instances": {"zh": "6. 实例管理", "en": "6. Instance Management"},
    "menu.volumes": {"zh": "7. 磁盘管理", "en": "7. Volume Management"},
    "menu.users": {"zh": "8. 云账户管理", "en": "8. Cloud Account Management"},
    "menu.monitor": {"zh": "9. 实例状态监控通知",
                     "en": "9. Instance Status Monitoring"},
    "menu.autostart": {"zh": "10. 实例状态监控自启",
                       "en": "10. Auto-Start on Monitor"},
    "menu.openport": {"zh": "11. 开放云面板端口", "en": "11. Open Panel Port"},
    "menu.workflows": {"zh": "12. 工作流错误", "en": "12. Workflow Errors"},
    "menu.clients": {"zh": "14. 客户端管理", "en": "14. Client Management"},
    "menu.health": {"zh": "15. 一键测活", "en": "15. One-Click Health Check"},
    "menu.autoip": {"zh": "16. GFW自动换IP", "en": "16. GFW Auto IP Change"},
    "menu.presets": {"zh": "17. 快捷开机", "en": "17. Quick Boot Presets"},
    "menu.subscription": {"zh": "18. 订阅信息", "en": "18. Subscription Info"},
    "menu.traffic": {"zh": "19. 本月流量", "en": "19. This Month's Traffic"},
    "menu.quota": {"zh": "20. 查询配额", "en": "20. Check Quota"},
    "menu.tasks": {"zh": "21. 查看运行中的任务", "en": "21. Running Tasks"},
    "menu.load": {"zh": "22. 查看客户端负载", "en": "22. Client Load"},
    "menu.memballoon": {"zh": "23. 占用25%内存", "en": "23. Use 25% Memory"},
    "menu.memclear": {"zh": "24. 清除缓存和占用",
                      "en": "24. Clear Cache & Balloon"},
    "menu.cost": {"zh": "25. 近3月花费查询", "en": "25. Last 3 Months Cost"},
    "menu.mfa": {"zh": "26. 清除所有2FA设备", "en": "26. Clear All 2FA Devices"},
    "menu.disable": {"zh": "27. 一键禁用被封账户",
                     "en": "27. Disable Banned Account"},
    "menu.delapi": {"zh": "28. 删除当前API", "en": "28. Delete Current API Key"},
    "menu.daily": {"zh": "29. 每日报告", "en": "29. Daily Report"},
    "menu.emails": {"zh": "30. 批量查询邮箱", "en": "30. Batch Email Lookup"},
    "menu.restart": {"zh": "31. 重启", "en": "31. Restart"},
    "menu.upgrade": {"zh": "32. 升级客户端", "en": "32. Upgrade Client"},
    "menu.regions": {"zh": "33. 自动拓展子区域",
                     "en": "33. Auto-Expand Sub-Regions"},
    "menu.logs": {"zh": "34. 查询最新日志", "en": "34. Latest Logs"},
    "menu.batch": {"zh": "35. 批量操作", "en": "35. Batch Operations"},
    "menu.a1health": {"zh": "36. A1 配置体检",
                      "en": "36. A1 Config Health Check"},
    "menu.traffic_guard": {"zh": "37. 流量守护", "en": "37. Traffic Guard"},
    "menu.keepalive": {"zh": "38. 停机保活", "en": "38. Stop-Protection"},
    "menu.sslwatch": {"zh": "39. SSL 监控", "en": "39. SSL Monitor"},
    "menu.objectstorage": {"zh": "40. 对象存储", "en": "40. Object Storage"},
    "menu.email": {"zh": "41. Email 发件人", "en": "41. Email Sender"},
    "menu.plans": {"zh": "42. 抢机计划", "en": "42. Grab Plans"},
    "menu.bark": {"zh": "43. Bark 通知设置", "en": "43. Bark Notifications"},
    "menu.rescue": {"zh": "44. 救机", "en": "44. Rescue"},
    "menu.oracleping": {"zh": "45. Oracle 全球区域测速",
                        "en": "45. Oracle Global Region Speed Test"},
    "menu.cfip": {"zh": "46. Cloudflare 优选 IP",
                  "en": "46. Cloudflare Preferred IP"},
    # ---- 实例列表 / 详情 ----
    "instances.list_title": {"zh": "[{profile}] 实例列表（{count} 台）：",
                             "en": "[{profile}] Instances ({count}):"},
    "instances.list_empty": {"zh": "[{profile}] 该账号下没有实例。",
                             "en": "[{profile}] No instances in this account."},
    "instances.list_none_visible": {"zh": "[{profile}] 没有可显示的实例"
                                          "（已终止的实例已隐藏）。",
                                    "en": "[{profile}] No instances to show "
                                          "(terminated ones are hidden)."},
    "instances.list_hint": {"zh": "点下面按钮查看详情或操作。",
                            "en": "Tap a button below for details or actions."},
    "instances.group_running": {"zh": "🟢 运行中", "en": "🟢 Running"},
    "instances.group_stopped": {"zh": "🟡 已停止", "en": "🟡 Stopped"},
    "instances.group_terminated": {"zh": "⚫ 已终止", "en": "⚫ Terminated"},
    "instances.state_running": {"zh": "运行中", "en": "Running"},
    "instances.state_stopped": {"zh": "已停止", "en": "Stopped"},
    "instances.state_terminated": {"zh": "已终止", "en": "Terminated"},
    "instances.group_other": {"zh": "❓ 其他", "en": "❓ Other"},
    "instances.detail_state": {"zh": "状态", "en": "State"},
    "instances.detail_shape": {"zh": "规格", "en": "Shape"},
    "instances.detail_cpu": {"zh": "CPU 型号", "en": "CPU model"},
    "instances.detail_ad": {"zh": "可用域", "en": "Availability domain"},
    "instances.detail_created": {"zh": "创建时间", "en": "Created"},
    "instances.detail_public_ip": {"zh": "公网 IP", "en": "Public IP"},
    "instances.detail_free_forever": {"zh": "永久免费",
                                      "en": "Free forever"},
    "instances.no_public_ip": {"zh": "无公网 IP", "en": "No public IP"},
    "instances.action_stop": {"zh": "⏹️ 停止", "en": "⏹️ Stop"},
    "instances.action_restart": {"zh": "🔄 重启", "en": "🔄 Restart"},
    "instances.action_start": {"zh": "▶️ 启动", "en": "▶️ Start"},
    "instances.action_change_ip": {"zh": "🔁 换 IP", "en": "🔁 Change IP"},
    "instances.action_resize": {"zh": "📐 升/降配", "en": "📐 Resize"},
    "instances.action_rename": {"zh": "✏️ 改名", "en": "✏️ Rename"},
    "instances.action_reinstall": {"zh": "💿 重装系统", "en": "💿 Reinstall OS"},
    "instances.action_boot_backup": {"zh": "💾 引导卷备份",
                                     "en": "💾 Boot Volume Backup"},
    "instances.action_boot_restore": {"zh": "💾 引导卷恢复",
                                      "en": "💾 Boot Volume Restore"},
    "instances.action_boot_backup_del": {"zh": "🗑️ 删除历史备份",
                                         "en": "🗑️ Delete Old Backups"},
    "instances.action_console": {"zh": "🖥️ 串行控制台",
                                 "en": "🖥️ Serial Console"},
    "instances.action_bind_domain": {"zh": "🌐 绑定域名",
                                     "en": "🌐 Bind Domain"},
    "instances.action_terminate": {"zh": "🗑️ 终止实例",
                                   "en": "🗑️ Terminate Instance"},
    "instances.action_list": {"zh": "◀ 实例列表", "en": "◀ Instance List"},
    # ---- 绑定域名（实例详情） ----
    "bind_domain.no_ip": {"zh": "实例「{name}」没有公网 IP，无法绑定域名。",
                          "en": "Instance “{name}” has no public IP, so no "
                                "domain can be bound to it."},
    "bind_domain.no_credentials": {"zh": "Cloudflare 凭据未配置，请先配置凭据"
                                          "（/domain 里可添加）。",
                                   "en": "Cloudflare credentials are not "
                                         "configured. Please set them up first "
                                         "(you can add them in /domain)."},
    "bind_domain.no_zones": {"zh": "Cloudflare 里没有 Zone，无法绑定域名。",
                             "en": "No zones found in Cloudflare, so no "
                                   "domain can be bound."},
    "bind_domain.pick_zone": {"zh": "给实例「{name}」（{ip}）绑定域名："
                                    "先选主域名。",
                              "en": "Bind a domain to instance “{name}” "
                                    "({ip}): pick the main domain first."},
    "bind_domain.input_subdomain": {"zh": "已选主域名 {zone}。\n"
                                          "请输入二级域名（主机名），如 www、app；"
                                          "只输 @ 表示绑定根域名。\n"
                                          "（直接发文字即可，不用加任何命令）",
                                    "en": "Main domain selected: {zone}.\n"
                                          "Send the subdomain (hostname), e.g. "
                                          "www or app; send just @ to bind the "
                                          "root domain.\n"
                                          "(Just send the text, no command "
                                          "needed.)"},
    "bind_domain.invalid_subdomain": {"zh": "❌ 二级域名格式不对：只允许字母、"
                                            "数字、-、_、.，或单独一个 @。"
                                            "请重新输入。",
                                      "en": "❌ Invalid subdomain: only "
                                            "letters, digits, -, _ and . are "
                                            "allowed, or a single @. Please "
                                            "send it again."},
    "bind_domain.exists_same": {"zh": "✅ {fqdn} 已指向 {ip}，不用再建。",
                                "en": "✅ {fqdn} already points to {ip}; "
                                      "nothing to create."},
    "bind_domain.exists_conflict": {"zh": "⚠️ {fqdn} 已有一条 A 记录指向 "
                                          "{old_ip}。\n要改成指向 {ip} 吗？",
                                    "en": "⚠️ {fqdn} already has an A record "
                                          "pointing to {old_ip}.\nChange it to "
                                          "point to {ip}?"},
    "bind_domain.overwrite": {"zh": "✅ 确认覆盖", "en": "✅ Confirm Overwrite"},
    "bind_domain.done_created": {"zh": "✅ 已绑定：{fqdn} → {ip}\n"
                                       "DNS 传播需要一点时间（通常几分钟），"
                                       "请稍后再访问验证。",
                                 "en": "✅ Bound: {fqdn} → {ip}\n"
                                       "DNS propagation takes a while "
                                       "(usually a few minutes); please "
                                       "check again later."},
    "bind_domain.done_updated": {"zh": "✅ 已更新：{fqdn} → {ip}"
                                       "（原指向 {old_ip}）\n"
                                       "DNS 传播需要一点时间（通常几分钟），"
                                       "请稍后再访问验证。",
                                 "en": "✅ Updated: {fqdn} → {ip} "
                                       "(previously {old_ip})\n"
                                       "DNS propagation takes a while "
                                       "(usually a few minutes); please "
                                       "check again later."},
    "bind_domain.cancelled": {"zh": "已取消绑定域名。",
                              "en": "Domain binding cancelled."},
    "bind_domain.expired": {"zh": "绑定流程已过期，请重新点“绑定域名”。",
                            "en": "This binding session has expired. Please "
                                  "tap “Bind Domain” again."},
    "bind_domain.cf_auth": {"zh": "❌ Cloudflare 未配置或认证失败："
                                  "请检查 Token 配置。",
                            "en": "❌ Cloudflare is not configured or "
                                  "authentication failed. Please check the "
                                  "token."},
    "bind_domain.cf_not_found": {"zh": "❌ Zone 或 DNS 记录不存在，"
                                       "请刷新列表后再试。",
                                 "en": "❌ Zone or DNS record not found. "
                                       "Please refresh the list and try again."},
    "bind_domain.cf_rate_limit": {"zh": "❌ Cloudflare 限流（429）："
                                        "请求太频繁，请稍后再试。",
                                  "en": "❌ Cloudflare rate limit (429): too "
                                        "many requests, please try again later."},
    "bind_domain.cf_failed": {"zh": "❌ Cloudflare 调用失败，请稍后再试。",
                              "en": "❌ Cloudflare request failed. Please try "
                                    "again later."},
    "instances.confirm_start": {"zh": "确认启动实例「{name}」？",
                                "en": "Start instance “{name}”?"},
    "instances.confirm_stop": {"zh": "确认停止实例「{name}」？",
                               "en": "Stop instance “{name}”?"},
    "instances.confirm_restart": {"zh": "确认重启实例「{name}」？",
                                  "en": "Restart instance “{name}”?"},
    "instances.confirm_terminate": {"zh": "⚠️ 确认终止实例「{name}」？\n"
                                          "\n"
                                          "<b>终止后将永久删除引导卷，"
                                          "数据无法恢复！</b>",
                                    "en": "⚠️ Terminate instance “{name}”?\n"
                                          "\n"
                                          "<b>The boot volume will be "
                                          "permanently deleted and data cannot "
                                          "be recovered!</b>"},
    "instances.done_start": {"zh": "✅ 已启动实例「{name}」。",
                             "en": "✅ Instance “{name}” started."},
    "instances.done_stop": {"zh": "✅ 已停止实例「{name}」。",
                            "en": "✅ Instance “{name}” stopped."},
    "instances.done_restart": {"zh": "✅ 已重启实例「{name}」。",
                               "en": "✅ Instance “{name}” restarted."},
    "instances.done_terminate": {"zh": "✅ 已终止实例「{name}」，引导卷已永久删除。",
                                 "en": "✅ Instance “{name}” terminated; the "
                                       "boot volume has been permanently "
                                       "deleted."},
    "instances.verb_start": {"zh": "启动", "en": "Start"},
    "instances.verb_stop": {"zh": "停止", "en": "Stop"},
    "instances.verb_restart": {"zh": "重启", "en": "Restart"},
    "instances.verb_terminate": {"zh": "终止", "en": "Terminate"},
    # ---- 错误提示 ----
    "error.capacity": {"zh": "❌ 无容量（Out of host capacity）：机房暂时没货，"
                             "请稍后再试。",
                       "en": "❌ Out of host capacity: no capacity right now, "
                             "please try again later."},
    "error.rate_limit": {"zh": "❌ API 限流（429）：请求太频繁，机器人已自动退避，"
                               "请稍后再试。",
                         "en": "❌ API rate limited (429): too many requests, "
                               "the bot has backed off automatically. Please "
                               "try again later."},
    "error.quota": {"zh": "❌ 配额不足：免费额度已用完，继续重试也没用，"
                          "请先释放资源或换账号。",
                    "en": "❌ Quota exceeded: the free tier is exhausted. "
                          "Retrying won't help — free up resources or switch "
                          "accounts first."},
    "error.auth": {"zh": "❌ 认证失败：请检查 ~/oci_config 与 API 密钥是否正确。",
                   "en": "❌ Authentication failed: please check ~/oci_config "
                         "and your API key."},
    "error.not_found": {"zh": "❌ 资源不存在：可能已被删除，请刷新列表后再试。",
                        "en": "❌ Resource not found: it may have been "
                              "deleted. Please refresh the list and retry."},
    "error.oci_failed": {"zh": "❌ OCI 调用失败：{detail}",
                         "en": "❌ OCI call failed: {detail}"},
    "error.generic": {"zh": "❌ 出错了：{detail}",
                      "en": "❌ Something went wrong: {detail}"},
    "error.message_gone": {"zh": "⚠️ 你点的那条消息已经不在了（可能已被清理），"
                                 "请重新发送命令打开菜单再点。",
                           "en": "⚠️ That message is gone (it may have been "
                                 "cleaned up). Please send the command again "
                                 "to reopen the menu."},
    # ---- 开机表单（菜单 1） ----
    "boot.form_title": {"zh": "🚀 开机配置（按钮矩阵）：",
                        "en": "🚀 Boot configuration (button matrix):"},
    "boot.label_cpu": {"zh": "CPU", "en": "CPU"},
    "boot.label_os": {"zh": "系统", "en": "OS"},
    "boot.label_spec": {"zh": "规格", "en": "Shape"},
    "boot.label_ocpus": {"zh": "核心", "en": "Cores"},
    "boot.label_memory": {"zh": "内存", "en": "Memory"},
    "boot.label_disk": {"zh": "硬盘", "en": "Disk"},
    "boot.label_count": {"zh": "数量", "en": "Count"},
    "boot.label_delay": {"zh": "延迟", "en": "Delay"},
    "boot.label_ssh_key": {"zh": "公钥", "en": "SSH key"},
    "boot.label_root": {"zh": "root登录", "en": "Root login"},
    "boot.label_pubip": {"zh": "公网IP", "en": "Public IP"},
    "boot.label_ipv6": {"zh": "IPv6", "en": "IPv6"},
    "boot.label_script": {"zh": "开机脚本", "en": "Startup script"},
    "boot.label_dd": {"zh": "DD重装", "en": "DD reinstall"},
    "boot.dd_on": {"zh": "开（Debian 13）", "en": "On (Debian 13)"},
    "boot.dd_off": {"zh": "不开", "en": "Off"},
    "boot.dd_summary": {"zh": "DD重装：开（Debian 13，SSH 14422，装完跑初始化脚本）",
                        "en": "DD reinstall: on (Debian 13, SSH 14422, "
                              "then run init script)"},
    "boot.dd_requires_root": {"zh": "开 DD 重装时必须开 root 登录（DD 后的 "
                               "SSH 靠 root 密码进）",
                              "en": "DD reinstall requires root login on "
                                    "(SSH after DD uses the root password)"},
    "boot.not_selected": {"zh": "未选", "en": "Not selected"},
    "boot.default_disk": {"zh": "默认（47GB）", "en": "Default (47GB)"},
    "boot.delay_not_selected": {"zh": "未选（默认 25±5 秒）",
                                "en": "Not selected (default 25±5s)"},
    "boot.delay_fixed": {"zh": "固定 {seconds} 秒",
                         "en": "Fixed {seconds}s"},
    "boot.delay_range": {"zh": "区间 {low}–{high} 秒随机",
                         "en": "Random {low}–{high}s"},
    "boot.delay_default": {"zh": "默认 25±5 秒", "en": "Default 25±5s"},
    "boot.spec_e2_fixed": {"zh": "固定 1 核 / 1GB（E2.1.Micro）",
                           "en": "Fixed 1 OCPU / 1GB (E2.1.Micro)"},
    "boot.spec_a1": {"zh": "{ocpus} 核 / {memory}GB",
                     "en": "{ocpus} OCPU / {memory}GB"},
    "boot.script_none": {"zh": "无", "en": "None"},
    "boot.script_uploaded": {"zh": "已上传（{chars} 字符）",
                             "en": "Uploaded ({chars} chars)"},
    "boot.use_bot_key": {"zh": "使用机器人公钥", "en": "Use bot SSH key"},
    "boot.no_key": {"zh": "不用公钥", "en": "No SSH key"},
    "boot.root_on": {"zh": "开启（密码随机）", "en": "On (random password)"},
    "boot.root_off": {"zh": "不开启", "en": "Off"},
    "boot.has": {"zh": "有", "en": "Yes"},
    "boot.has_not": {"zh": "无", "en": "No"},
    "boot.hint_toggle": {"zh": "点选项打 ✅ 选中，再点取消；延迟选一个=固定间隔，"
                               "选两个=区间随机。",
                         "en": "Tap an option to select it (✅), tap again to "
                               "deselect. One delay = fixed interval, two "
                               "delays = random interval."},
    "boot.hint_key_root": {"zh": "公钥和 root 登录至少选一项（不用公钥时必须开 "
                                "root 登录）。",
                           "en": "Pick at least one of SSH key or root login "
                                 "(root login is required when no SSH key is "
                                 "used)."},
    "boot.hint_centos": {"zh": "CentOS 已映射为 Oracle Linux（OCI 已无 CentOS "
                              "平台镜像）。",
                         "en": "CentOS is mapped to Oracle Linux (OCI no "
                               "longer offers CentOS platform images)."},
    "boot.custom": {"zh": "自定义", "en": "Custom"},
    "boot.default": {"zh": "默认", "en": "Default"},
    "boot.oneclick": {"zh": "🚀 一键开机（默认 AMD）",
                      "en": "🚀 One-click boot (default AMD)"},
    "boot.cpu_only": {"zh": "只抢 {models}", "en": "Only {models}"},
    "boot.cpu_any": {"zh": "什么 CPU 都可以", "en": "Any CPU is fine"},
    "boot.start_grab": {"zh": "🚀 开始抢机", "en": "🚀 Start Grabbing"},
    "boot.config_expired": {"zh": "配置已过期，请重新打开菜单 1（开机）。",
                            "en": "Configuration expired. Please reopen "
                                  "menu 1 (Boot)."},
    "boot.need_form": {"zh": "请先在菜单 1 里配好开机表单。",
                       "en": "Please set up the boot form in menu 1 first."},
    "boot.grab_started": {"zh": "🚀 抢机已启动：{label}。用菜单 21"
                                "（查看运行中的任务）查看进度。",
                          "en": "🚀 Grabbing started: {label}. Check progress "
                                "in menu 21 (Running Tasks)."},
    "boot.root_password_once": {"zh": "🔑 root 登录密码（只显示这一次，"
                                      "请立即记下）：",
                                "en": "🔑 Root password (shown only once — "
                                      "save it now):"},
    "boot.root_password_after": {"zh": "实例创建完成后用 root + 该密码登录。",
                                 "en": "Log in as root with this password "
                                       "after the instance is created."},
    "boot.confirm": {"zh": "✅ 确定开机", "en": "✅ Confirm Boot"},
    "boot.save_preset": {"zh": "💾 保存为快捷开机",
                         "en": "💾 Save as Quick Boot"},
    "boot.upload": {"zh": "上传", "en": "Upload"},
    "boot.summary_title": {"zh": "🚀 开机配置确认：",
                           "en": "🚀 Confirm boot configuration:"},
    "boot.summary_name": {"zh": "实例名", "en": "Instance name"},
    "boot.summary_names": {"zh": "实例名：{name}-1 ～ {name}-{count}"
                                  "（共 {count} 台）",
                           "en": "Instance names: {name}-1 to {name}-{count} "
                                 "({count} total)"},
    "boot.summary_os_custom": {"zh": "自定义镜像 …{tail}",
                               "en": "Custom image …{tail}"},
    "boot.summary_os_image": {"zh": "镜像 …{tail}", "en": "Image …{tail}"},
    "boot.summary_root_password": {"zh": "root密码：",
                                   "en": "Root password:"},
    "boot.summary_root_password_note": {"zh": "注意📢：root密码机器人不会记忆，"
                                             "请注意保存",
                                        "en": "Note 📢: the bot does not "
                                              "remember the root password — "
                                              "please save it now"},
    "boot.summary_target_cpu_only": {"zh": "目标 CPU：只留 {models}，"
                                           "不符就释放继续抢",
                                     "en": "Target CPU: only keep {models}; "
                                           "release and keep grabbing otherwise"},
    "boot.summary_target_cpu_any": {"zh": "目标 CPU：什么 CPU 都可以，抢到就留",
                                    "en": "Target CPU: any CPU is fine, keep "
                                          "whatever is grabbed"},
    "boot.summary_quota_check": {"zh": "配额预检：{result}{reason}",
                                 "en": "Quota pre-check: {result}{reason}"},
    "boot.summary_quota_short": {"zh": "配额不足，抢机也会停下，"
                                       "请先释放资源或换账号。",
                                 "en": "Quota is insufficient — grabbing will "
                                       "stop too. Free up resources or switch "
                                       "accounts first."},
    "boot.form_incomplete": {"zh": "❌ 配置还没填完：\n{errors}\n\n请继续点选表单。",
                             "en": "❌ Configuration is incomplete:\n{errors}\n\n"
                                   "Please keep selecting options in the form."},
    "boot.start_grabbing": {"zh": "🚀 开始抢机：{label}\n后台任务已启动，抢到后会通知你。",
                            "en": "🚀 Grabbing started: {label}\nThe background "
                                  "task is running; you'll be notified when an "
                                  "instance is grabbed."},
    "boot.grab_success": {"zh": "✅ 抢机成功：{label}\n实例 {name} 已开出，"
                                "公网 IP：{ip}",
                          "en": "✅ Grab succeeded: {label}\nInstance {name} is "
                                "up, public IP: {ip}"},
    "boot.grab_failed": {"zh": "❌ 抢机失败：{label}\n{reason}",
                         "en": "❌ Grab failed: {label}\n{reason}"},
    # ---- 配额 / 流量 / 花费 / 任务 ----
    "quota.title": {"zh": "[{profile}] 免费额度（{status}）：",
                    "en": "[{profile}] Free tier ({status}):"},
    "quota.ok": {"zh": "✅ 未超限", "en": "✅ Within limits"},
    "quota.over": {"zh": "⚠️ 已超限", "en": "⚠️ Over limits"},
    "quota.a1": {"zh": "A1.Flex：{used}/{limit} 核，{mem_used}/{mem_limit}GB",
                 "en": "A1.Flex: {used}/{limit} OCPUs, "
                       "{mem_used}/{mem_limit}GB"},
    "quota.e2": {"zh": "E2.1.Micro：{count}/{limit} 台",
                 "en": "E2.1.Micro: {count}/{limit} instances"},
    "quota.boot_volume": {"zh": "启动盘：{used}/{limit}GB",
                          "en": "Boot volumes: {used}/{limit}GB"},
    "quota.choose_account": {"zh": "免费额度：点下面账号查看：",
                             "en": "Free tier: tap an account below to view:"},
    "traffic.title": {"zh": "📈 本月流量", "en": "📈 This Month's Traffic"},
    "cost.title": {"zh": "💰 近 3 个月花费", "en": "💰 Last 3 Months Cost"},
    "tasks.title": {"zh": "📋 运行中的任务", "en": "📋 Running Tasks"},
    "tasks.none": {"zh": "当前没有运行中的任务。",
                   "en": "No tasks running right now."},
    "tasks.old_label": {"zh": "任务 {task_id}", "en": "Task {task_id}"},
    "tasks.cancelled": {"zh": "✅ 已取消：{label}。",
                        "en": "✅ Cancelled: {label}."},
    "tasks.cancel_failed": {"zh": "⚠️ {label} 不存在或已结束，无法取消。",
                            "en": "⚠️ {label} does not exist or has already "
                                  "finished; cannot cancel."},
    "tasks.cancel": {"zh": "❌ 取消 {label}", "en": "❌ Cancel {label}"},
    "health.title": {"zh": "❤️ 一键测活", "en": "❤️ One-Click Health Check"},
    "subscription.title": {"zh": "📋 订阅信息", "en": "📋 Subscription Info"},
    "subscription.title_single": {"zh": "📄 订阅信息",
                                  "en": "📄 Subscription Info"},
    "ip.title": {"zh": "🌐 IP 管理", "en": "🌐 IP Management"},
    "ipv6.title": {"zh": "🌐 IPv6 管理", "en": "🌐 IPv6 Management"},
    "resize.title": {"zh": "📐 升/降级实例", "en": "📐 Resize Instance"},
    "volumes.title": {"zh": "💾 磁盘管理", "en": "💾 Volume Management"},
    "users.title": {"zh": "👥 云账户管理", "en": "👥 Cloud Account Management"},
    "monitor.title": {"zh": "🔔 实例状态监控通知",
                      "en": "🔔 Instance Status Monitoring"},
    "profiles.title": {"zh": "🔄 切换账号", "en": "🔄 Switch Account"},
    "profiles.switched": {"zh": "✅ 已切换到账号：{account}",
                          "en": "✅ Switched to account: {account}"},
    "profiles.region_switched": {"zh": "✅ 已切换到账号：{account} · {region}",
                                 "en": "✅ Switched to account: {account} · {region}"},
    "profiles.choose_region": {"zh": "请选择工作区域：",
                               "en": "Please pick a working region:"},
    "profiles.current": {"zh": "当前账号：{label}\n点下面切换账号"
                               "（多区账号点开可切换工作区域）：",
                         "en": "Current account: {label}\nTap below to switch "
                               "accounts (multi-region accounts can also "
                               "switch working region):"},
    "profiles.choose_region2": {"zh": "账号：{label}\n点下面切换工作区域：",
                                "en": "Account: {label}\nTap below to switch "
                                      "the working region:"},
    "profiles.switch_failed": {"zh": "❌ 切换失败：{detail}",
                               "en": "❌ Switch failed: {detail}"},
    "profiles.invalid_region": {"zh": "❌ 无效的区域切换。",
                                "en": "❌ Invalid region switch."},
    "profiles.region_not_subscribed": {"zh": "❌ 账号未订阅该区域：{region}",
                                       "en": "❌ This account is not subscribed "
                                             "to region: {region}"},
    "quota.invalid_op": {"zh": "无效的额度操作。",
                         "en": "Invalid quota operation."},
    "quota.no_account": {"zh": "账号不存在，请重新打开免费额度。",
                         "en": "Account not found. Please reopen the quota "
                               "page."},
    "quota.choose_region": {"zh": "{label} 的订阅区域：点下面区域查看额度：",
                            "en": "{label} regions: tap a region to view its "
                                  "quota:"},
    "quota.back_regions": {"zh": "◀ 返回区域菜单",
                           "en": "◀ Back to Regions"},
    "quota.back_accounts": {"zh": "◀ 返回账号列表",
                            "en": "◀ Back to Accounts"},
    "traffic.empty": {"zh": "当前账号/区域没有运行中的实例。",
                      "en": "No running instances in the current "
                            "account/region."},
    "traffic.title_line": {"zh": "📈 本月流量（vnstat）：",
                           "en": "📈 This month's traffic (vnstat):"},
    "traffic.in_out": {"zh": "当月入站: {rx:.2f} GiB / 当月出站: {tx:.2f} GiB",
                       "en": "Inbound this month: {rx:.2f} GiB / Outbound: "
                             "{tx:.2f} GiB"},
    "traffic.no_data": {"zh": "流量：查不到（{reason}）",
                        "en": "Traffic: unavailable ({reason})"},
    "traffic.unknown_reason": {"zh": "未知原因", "en": "unknown reason"},
    "traffic.vnstat_fresh": {"zh": "（vnstat 刚自动安装，数据从现在开始统计）",
                             "en": "(vnstat was just installed; stats start "
                                   "from now)"},
    "traffic.total": {"zh": "合计：入站 {rx:.2f} GiB / 出站 {tx:.2f} GiB"
                            "（{count} 台）",
                      "en": "Total: inbound {rx:.2f} GiB / outbound "
                            "{tx:.2f} GiB ({count} instances)"},
    "traffic.guard_toggle": {"zh": "🛡️ {name}：{action}看门狗",
                             "en": "🛡️ {name}: Turn {action} watchdog"},
    "traffic.guard_off": {"zh": "关闭", "en": "off"},
    "traffic.guard_on": {"zh": "开启", "en": "on"},
    "cost.no_usage": {"zh": "近 {months} 个月没有用量记录。",
                      "en": "No usage records in the last {months} months."},
    "cost.total": {"zh": "💰 近 {months} 个月总花费：{total:.2f} {currency}",
                   "en": "💰 Total cost in the last {months} months: "
                         "{total:.2f} {currency}"},
    "cost.totals_title": {"zh": "💰 近 {months} 个月总花费（{count} 个账号）：",
                          "en": "💰 Total cost in the last {months} months "
                                "({count} accounts):"},
    "cost.failed": {"zh": "查询失败", "en": "Lookup failed"},
    "cost.grand_total": {"zh": "合计：{total:.2f} {currency}",
                         "en": "Grand total: {total:.2f} {currency}"},
    "cost.no_accounts": {"zh": "没有配置任何账号。",
                         "en": "No accounts configured."},
    "subscription.title_count": {"zh": "📄 订阅信息（{count} 个账号）：",
                                 "en": "📄 Subscriptions ({count} accounts):"},
    "subscription.failed": {"zh": "查询失败。", "en": "Lookup failed."},
    "subscription.name": {"zh": "名称", "en": "Name"},
    "subscription.home_region": {"zh": "Home 区域", "en": "Home region"},
    "subscription.description": {"zh": "描述", "en": "Description"},
    "subscription.unparseable": {"zh": "订阅信息：无法解析返回结果。",
                                 "en": "Subscription info: could not parse "
                                       "the response."},
    "subscription.no_accounts": {"zh": "没有配置任何账号。",
                                 "en": "No accounts configured."},
    "health.title_line": {"zh": "❤️ 一键测活：",
                          "en": "❤️ One-click health check:"},
    "health.line": {"zh": "• {label}：{total} 台实例，{running} 台运行中 ✅",
                    "en": "• {label}: {total} instances, {running} running ✅"},
    "changeip.pick_first": {"zh": "请先在实例列表里选一台实例，"
                                  "再点“换 IP”。",
                            "en": "Please pick an instance from the list "
                                  "first, then tap “Change IP”."},
    "changeip.confirm_detail": {"zh": "🔁 确认给实例 {name} 换公网 IP？\n\n"
                                      "当前 IP：{ip}\n"
                                      "⚠️ 换 IP 后旧 IP 立即释放，无法找回；"
                                      "请同步更新 DNS 记录、反代与客户端配置。",
                                "en": "🔁 Change the public IP of instance "
                                      "{name}?\n\n"
                                      "Current IP: {ip}\n"
                                      "⚠️ The old IP is released immediately "
                                      "and cannot be recovered; please update "
                                      "DNS records, reverse proxies, and "
                                      "client configs."},
    "changeip.confirm_button": {"zh": "🔁 确认换 IP",
                                "en": "🔁 Confirm IP Change"},
    "ip.create_button": {"zh": "➕ 创建保留 IP",
                         "en": "➕ Create Reserved IP"},
    "ip.delete_button": {"zh": "🗑 删除 {ip}", "en": "🗑 Delete {ip}"},
    "ip.overview_title": {"zh": "🌐 IP 管理：", "en": "🌐 IP Management:"},
    "ip.reserved": {"zh": "保留 IP：", "en": "Reserved IPs:"},
    "ip.no_reserved": {"zh": "• 无保留 IP", "en": "• No reserved IPs"},
    "ip.bound": {"zh": "已绑定", "en": "Attached"},
    "ip.unbound": {"zh": "未绑定", "en": "Unattached"},
    "ip.instance_ips": {"zh": "实例公网 IP：", "en": "Instance public IPs:"},
    "ip.no_instances": {"zh": "• 无实例", "en": "• No instances"},
    "ip.change_hint": {"zh": "换 IP：请在实例列表里选一台实例，点“换 IP”。",
                       "en": "To change an IP: pick an instance in the list "
                             "and tap “Change IP”."},
    "rename.pick_first": {"zh": "请先在实例列表里选一台实例，再点“改名”。",
                          "en": "Please pick an instance from the list "
                                "first, then tap “Rename”."},
    "reinstall.pick_first": {"zh": "请先在实例列表里选一台实例，"
                                   "再点“重装系统”。",
                             "en": "Please pick an instance from the list "
                                   "first, then tap “Reinstall OS”."},
    "console.pick_first": {"zh": "请先在实例列表里选一台实例，"
                                 "再点“串行控制台”。",
                           "en": "Please pick an instance from the list "
                                 "first, then tap “Serial Console”."},
    "console.title": {"zh": "🖥️ {name} 的串行控制台：",
                      "en": "🖥️ Serial console for {name}:"},
    "console.command": {"zh": "连接命令：", "en": "Connection command:"},
    "console.no_command": {"zh": "（未返回连接命令）",
                           "en": "(no connection command returned)"},
    "console.state": {"zh": "状态：", "en": "State:"},
    "console.fingerprint": {"zh": "指纹：", "en": "Fingerprint:"},
    "console.hint": {"zh": "用实例的 SSH 密钥连接；首次连接需接受主机密钥。",
                     "en": "Connect with the instance's SSH key; accept the "
                           "host key on first connection."},
    "boot.preset_title": {"zh": "🚀 开机配置：", "en": "🚀 Boot configuration:"},
    "boot.preset_name": {"zh": "实例名：", "en": "Instance name:"},
    "changeip.confirm": {"zh": "确认给实例「{name}」换一个新公网 IP？\n"
                               "旧 IP 会被释放。",
                         "en": "Change the public IP of instance “{name}”?\n"
                               "The old IP will be released."},
    "changeip.done": {"zh": "✅ {name} 已换 IP，新公网 IP：{ip}\n"
                            "⚠️ 旧 IP 已释放，无法找回；"
                            "请同步更新 DNS 记录、反代与客户端配置。",
                      "en": "✅ {name} got a new IP: {ip}\n"
                            "⚠️ The old IP has been released and cannot be "
                            "recovered; please update DNS records, reverse "
                            "proxies, and client configs."},
    "changeip.confirm": {"zh": "确认给实例「{name}」换一个新公网 IP？\n"
                               "旧 IP 会被释放。",
                         "en": "Change the public IP of instance “{name}”?\n"
                               "The old IP will be released."},
    "resize.expired": {"zh": "实例或目标规格已过期，请重新选择。",
                       "en": "The instance or target shape has expired. "
                             "Please select again."},
    "resize.done": {"zh": "✅ {name} 已改为 {ocpus} 核 / {memory}GB。\n"
                          "改规格时实例可能会重启，请稍后查看状态。",
                    "en": "✅ {name} resized to {ocpus} OCPUs / {memory}GB.\n"
                          "The instance may restart during the resize; "
                          "please check its status later."},
    "common.instance_gone_short": {"zh": "实例不存在或已过期，"
                                         "请重新打开实例列表。",
                                   "en": "Instance not found or expired. "
                                         "Please reopen the instance list."},
    "rename.empty": {"zh": "新实例名不能为空，改名已取消。",
                     "en": "The new instance name can't be empty; rename "
                           "cancelled."},
    "rename.too_long": {"zh": "新实例名不能超过 {max} 字符，改名已取消。",
                        "en": "The new instance name can't exceed {max} "
                              "characters; rename cancelled."},
    "rename.confirm": {"zh": "确认把实例「{name}」改名为「{new_name}」？",
                       "en": "Rename instance “{name}” to “{new_name}”?"},
    "rename.confirm_button": {"zh": "✅ 确认改名", "en": "✅ Confirm Rename"},
    "reinstall.mismatch": {"zh": "输入的实例名与实际不符，重装已取消。\n"
                                 "请重新点“重装系统”再试。",
                            "en": "The name you entered doesn't match; "
                                  "reinstall cancelled.\n"
                                  "Please tap “Reinstall OS” and try again."},
    "reinstall.final_confirm": {"zh": "⚠️ 最后确认：重装实例「{name}」？\n"
                                      "系统盘数据将全部清空，不可恢复；"
                                      "实例 OCID 与 IP 地址保留不变。",
                                "en": "⚠️ Final confirmation: reinstall "
                                      "instance “{name}”?\n"
                                      "All data on the boot volume will be "
                                      "wiped and cannot be recovered; the "
                                      "instance OCID and IP address stay the "
                                      "same."},
    "reinstall.confirm_button": {"zh": "💿 确认重装",
                                 "en": "💿 Confirm Reinstall"},
    "rename.prompt": {"zh": "✏️ 请回复这条消息，输入实例「{name}」的新名字：\n"
                            "（直接发文字即可，不用加任何命令）",
                      "en": "✏️ Please reply to this message with the new "
                            "name for instance “{name}”:\n"
                            "(just send text, no command needed)"},
    "rename.done": {"zh": "✅ 实例「{old_name}」已改名为「{new_name}」。",
                    "en": "✅ Instance “{old_name}” renamed to “{new_name}”."},
    "rename.expired": {"zh": "实例或新名字已过期，请重新改名。",
                       "en": "The instance or new name has expired. Please "
                             "rename again."},
    "reinstall.done": {"zh": "✅ 实例「{name}」重装已提交，系统盘正在重建。\n"
                            "请等待实例重启完成后再连接。",
                       "en": "✅ Reinstall submitted for instance “{name}”; "
                             "the boot volume is being rebuilt.\n"
                             "Please wait for the instance to finish "
                             "restarting before connecting."},
    "reinstall.prompt": {"zh": "⚠️ 重装系统将清空实例「{name}」系统盘的全部数据，"
                               "不可恢复；实例 OCID 与 IP 地址保留不变。\n\n"
                               "请回复这条消息，完整输入实例名「{name}」确认重装：",
                         "en": "⚠️ Reinstalling will wipe all data on "
                               "instance “{name}”'s boot volume — this cannot "
                               "be undone. The instance OCID and IP address "
                               "stay the same.\n\n"
                               "Please reply to this message with the full "
                               "instance name “{name}” to confirm the "
                               "reinstall:"},
    "common.wait": {"zh": "请稍等…", "en": "Please wait…"},
    "common.loading": {"zh": "正在查询…", "en": "Loading…"},
}


def normalize_lang(value):
    """语言值归一化：zh/zh-CN/中文 -> "zh"，en/en-US -> "en"，其余 None。"""
    if not isinstance(value, str):
        return None
    lowered = value.strip().lower()
    if lowered in ("zh", "zh-cn", "zh-tw", "zh-hans", "chinese", "中文"):
        return "zh"
    if lowered in ("en", "en-us", "en-gb", "english"):
        return "en"
    return None


def get_lang_file():
    """语言偏好文件路径（数据目录 + LANG_FILENAME）。"""
    return os.path.join(bot_config.DATA_DIR, LANG_FILENAME)


def load_lang_map(path=None):
    """读语言偏好映射 {user_id(str): lang}；文件不存在或格式不对时 {}。"""
    path = path or get_lang_file()
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(user_id): normalize_lang(lang) or DEFAULT_LANG
            for user_id, lang in data.items()}


def save_lang_map(mapping, path=None):
    """原子写语言偏好映射，文件权限 0600。"""
    path = path or get_lang_file()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=parent or ".", prefix=".user-lang-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(mapping, handle, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    os.chmod(path, 0o600)
    return path


def get_user_lang(user_id, path=None):
    """某用户的语言偏好；未设置时默认中文。"""
    if user_id is None:
        return DEFAULT_LANG
    return load_lang_map(path).get(str(user_id), DEFAULT_LANG)


def set_user_lang(user_id, lang, path=None):
    """设置某用户的语言偏好并持久化；返回归一化后的语言。"""
    lang = normalize_lang(lang) or DEFAULT_LANG
    mapping = load_lang_map(path)
    mapping[str(user_id)] = lang
    save_lang_map(mapping, path)
    return lang


def toggle_user_lang(user_id, path=None):
    """切换某用户的语言（zh <-> en）并持久化；返回切换后的语言。"""
    current = get_user_lang(user_id, path)
    new_lang = "en" if current == "zh" else "zh"
    return set_user_lang(user_id, new_lang, path)


def lang_of(update, context=None):
    """当前更新对应用户的语言。

    优先读 context.user_data["lang"] 缓存（切换后立即生效，不用等
    下次读文件）；没有缓存时按 effective_user.id 读文件；都拿不到
    时默认中文。
    """
    if context is not None:
        try:
            cached = context.user_data.get("lang")
        except AttributeError:
            cached = None
        lang = normalize_lang(cached)
        if lang is not None:
            return lang
    user = getattr(update, "effective_user", None) if update is not None else None
    user_id = getattr(user, "id", None) if user is not None else None
    return get_user_lang(user_id)


def set_lang_context(context, lang):
    """把语言写进 context.user_data 缓存（切换后立即生效用）。"""
    try:
        context.user_data["lang"] = normalize_lang(lang) or DEFAULT_LANG
    except AttributeError:
        pass


def t(lang, key, **kwargs):
    """按语言取文案并 format；缺键回退中文，中文也没有返回 key。"""
    lang = normalize_lang(lang) or DEFAULT_LANG
    entry = STRINGS.get(key)
    if entry is None:
        return key
    text = entry.get(lang)
    if text is None:
        text = entry.get(DEFAULT_LANG)
    if text is None:
        return key
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return text
    return text


def tr(update, context, key, **kwargs):
    """handler 里的快捷取文案：按当前用户的语言取 key。"""
    return t(lang_of(update, context), key, **kwargs)


def menu_item_label(key, lang="zh"):
    """Oracle 菜单项 key -> 显示文字（带编号）；未知 key 返回 key。"""
    return t(lang, "menu." + key, **{})
