# 更新日志 / Changelog

> [English version](CHANGELOG.en.md)

版本规则：语义化版本（主版本.次版本.修订号）。
- 加功能：次版本 +1（如 1.0.0 → 1.1.0）
- 修 Bug / 小改动：修订号 +1（如 1.0.0 → 1.0.1）
- 大重构 / 不兼容改动：主版本 +1

## [Unreleased]

## [2.25.0] - 2026-11-28
- 实例详情加“系统版本”行（CPU 型号下面）：如 Debian 13.8、Ubuntu 22.04，SSH 读 `/etc/os-release` + `/etc/debian_version`（Debian 小版本只在 debian_version 里），结果缓存到 `tgbot/data/os_versions.json`；实例终止/重装成功后清缓存，DD 完成后也清缓存重新检测。实例卡片与机器人端详情同步显示（卡片上 CPU 型号下面一行）。
- 多 worker 重复拉起抢机修好：面板用 uvicorn `--workers 4` 启动时每个 worker 都会跑一遍启动恢复，同一抢机任务会被拉起 4 个重复线程（抢到时可能重复建机，面板上状态还会来回跳）。现在启动恢复先抢排他文件锁，只有拿到锁的 worker 恢复抢机，其余 worker 跳过；锁保留到进程结束，后启动的 worker 也拿不到。
- 串行控制台自动登录 root：控制台直连时终端页用保存的开机 root 密码自动输 `root` 与密码；密码不对（被改过）时只问一次修改后的密码重试，第二次失败停手并提示手动登录。没存密码时不自动输，只提示手动登录。
- 串行控制台两处修好：同一实例只保留一个连接（有 ACTIVE 的直接复用，不再报 IncorrectState），直连按钮防双击，连接后自动发回车唤醒 `login:` 提示。
- DD 后自动执行安全脚本（用户定）：DD 完成且公钥补回后自动跑 `scripts/dd_security.sh`——fail2ban（SSH 端口跟随系统实际端口，不写死 22/14422）、unattended-upgrades（每日自动更新，内核更新后 04:00 自动重启，自动删旧内核与无用依赖）、流量看门狗（每 10 分钟巡检，200GB 熔断，SSH 逃生门保留）、安全守护 + rkhunter、nginx/xray 自动升级。不关密码登录、不改 SSH 端口（这两步从一键安全脚本里去掉，DD 版不要）。Bark Key 从 `M_bot_config.conf` 的 [bark] 节取，不写死在脚本里。流量看门狗的 SSH 端口同样跟随系统（不再写死 14422）。
- DD 后自动补回 SSH 公钥：DD 会抹掉 authorized_keys，现在 DD 完成后先用 root 密码把 guard 公钥与用户公钥补回，再跑初始化脚本；补失败只提醒不阻断。看门狗 / CPU 检测 / 系统版本检测不再因 DD 掉钥匙而全灭。
- 顶栏三改：版本号显示在 logo 区，语言/主题下拉缩短（max-width 112/88px），顶栏改 fixed + body 加 padding-top。
- README 三改：H1 改“M_Bot 甲骨文云开源管理面板”，版本行去掉多余中英文括注，加中英双语目录（26 个锚点），英文区加粗体大标题；界面预览截图补回仓库（`docs/screenshots/*.png`，中英双语：中文区用中文截图，英文区用英文截图）。
- 实例卡片加“初始开机密码”行（v2.24.0 的密码接口复用）：默认只显示 ••••••••，点击复制真密码（真密码只放内存，不进 DOM）。

## [2.24.0] - 2026-11-28
- 网页版开机成功通知补齐 Telegram + Bark 双通道（与机器人端一致）：以前网页端抢机成功只记“Telegram 未配置”类错误、日志里报“部分失败”；现在任一通道未配置时只跳过该通道（不记错误、不报部分失败），两通道都未配置时静默跳过；真实发送失败仍记进任务日志。通知正文与机器人端同一份（含 root 密码）。机器人端本来就走同一份双通道函数，这次一起受益：以前机器人端 TG 未配时也会在日志里报“部分失败”，现在同样只记跳过摘要。
- 实例详情加“初始开机密码”行（网页版与机器人端同步）：密码来自 v2.23.0 的按实例凭据库；网页版默认只显示 ••••••••，点击复制真密码（真密码只放内存，不进 DOM），只对单台实例显式请求（新接口 `GET /api/instances/{id}/password`，管理员登录后可用；没存时返回 404，前端显示“未保存”），列表与详情接口都不带密码；机器人端实例详情里同样加一行（`初始开机密码：<code>密码</code>`，没存显示“未保存”，消息用 parse_mode="HTML" 发送，实例名同步做了 HTML 转义）。创建表单的 root 密码弹窗文案同步改了：不再说“面板不会再显示它”，改成“密码已保存，开机成功后可在实例详情里查看（已隐藏，点击可复制）”。
- 串行控制台“获取失败 IncorrectState”修好了：以前每次都直接 create 第二个连接，OCI 每台实例只允许一个 ACTIVE 连接，第二次就报 IncorrectState；现在先列出该实例已有的连接，有 ACTIVE 的直接复用（不新建），正在 CREATING 的等一会儿（最多约 10 秒）变 ACTIVE 就复用，FAILED 的先删掉再新建，正在 DELETING 的等它消失再新建；新建时撞上并发 IncorrectState 也会重新列一次复用 ACTIVE 的，不再把错误抛给用户。机器人端串行控制台走同一个 `get_serial_console`，同样受益（机器人端没有直连功能，只展示连接命令，不用改）。
- 串行控制台“直接连接”修好了：以前后端用 paramiko 直连控制台端点并开 "session" channel，控制台端点不接受这种通道（报 `ChannelException(3, 'Unknown channel type')`）；现在按 ssh 连接命令的 ProxyCommand 等价做法分两阶段：内层 Transport 连控制台端点（用户名是连接 OCID，注册密钥认证），在其上开 "direct-tcpip" 转发通道到实例 OCID:22，外层 SSHClient 以该通道作 socket 再认证并开交互 shell；内层 Transport 随会话一直活着，关会话时一并关闭。连接命令解析现在也返回实例 OCID（直连必须有它；没有实例 OCID 的旧格式命令会明确报 400）。

## [2.23.0] - 2026-11-28
- 按实例保存 root 密码（用户决定，推翻“密码绝不落盘”旧规则）：开机成功后机器人端与网页端都把 root 密码按实例 OCID 存进服务端凭据库 `tgbot/data/instance_credentials.json`（文件 0600，只供 .160 上 bot/web 服务端进程读）；密码绝不发给浏览器、绝不记日志、绝不进通知与任务导出。实例终止时删掉对应条目。
- 一键 SSH 回落到保存的密码：没设固定密码 `MBOT_FIXED_ROOT_PASSWORD` 时，实例卡片 SSH 直连改用开机时按实例存的那份密码（固定密码仍优先）；没有保存的密码时仍回落手动连接表单。DD 后初始化也一样：通知里没密码时按实例 OCID 读凭据库（重启恢复重跑不再因缺密码跳过 DD 初始化）。
- 开机成功通知文案改了：不再说“机器人不会记忆”，改成“root密码机器人已保存，之后 SSH（开机后、DD 后）会用它登录；请你也自己保存一份”（机器人与网页端一致，中英双语）。
- 终端页“关闭此SSH窗口”修好了：以前没有会话时点了没反应、有会话时也只关标签不离开页面；现在先关当前会话（调接口删服务端会话），再离开终端页——有上一页回上一页，直接打开的回实例列表。
- 串行控制台弹窗加“直接连接”按钮：后端按弹窗里的连接命令解析出 OCI 控制台主机（443 端口、用户名是连接 OCID），用服务端 RSA 密钥直连，在终端页直接开标签；复制连接命令与关闭按钮保留。
- 实例卡片 CPU 型号显示修好了：`norm()` 归一化时丢了 `cpuModel`，卡片永远显示 E2 回落的“AMD EPYC 7J13”；现在保留后端返回的真实型号（如 AMD EPYC 7551），没检测到的才回落 7J13。

## [2.22.0] - 2026-11-28
- 创建表单“目标 CPU”改成下拉选项（与机器人端一致）：原来是自由文本框，现在是“只抢 7J13 / 什么 CPU 都可以”两个选项，默认只抢 7J13；选“什么 CPU 都可以”时不筛 CPU，抢到就留。后端把 "any" 转成不筛（E2 默认补全不再把它盖回 7J13）。
- SSH 公钥文件默认路径改成 /root/admin_ssh_keys.pub（原来是 /opt/rbot-clone/web/data/ssh_keys.pub）：.160 上的部署配置已改，公钥文件已建好，表单自动填入新路径。

## [2.21.0] - 2026-11-28
- 网页版创建表单加 DD 开关：高级选项卡的启动脚本下方加了“DD重装（开 Debian13）”开关，默认开，与机器人端一致；之前网页版没有这个选项。DD 开关、密码与开机设定的随机密码全链路同一份：表单随机密码原样进 InstallNET `-pwd`、DD 后初始化 SSH、成功通知与前端弹窗。
- 修了任务恢复丢 DD 的 bug：`GRAB_PARAM_KEYS` 白名单漏了 `dd`、`init_script`，创建时 params 里有这两项、写盘时被滤掉，重启恢复重跑会丢 DD；已补上，机器人端共用同一白名单同受益。
- 救机面板 7 个问题修复（.88 卸盘救机实测发现）：
  - 停止按钮“关机”改成 Stop/停止，状态徽标改显示 OCI 原始状态码（STOPPED/STOPPING/RUNNING）。
  - 点实例名立即开详情弹窗（先填名字与“加载中…”占位），不再等接口回来才有反馈。
  - 引导卷备份加确认框（可输备份名、选 FULL/增量），按钮改主按钮样式，不再看着像禁用、点了直接建。
  - 启动卷“备份”按钮无响应已修（findVolume 按 volumeId||id 匹配，找不到时 toast 报错）。
  - 备份列表加“备份 ID”列（截断后 12 位），点备份名/ID 复制完整 OCID。
  - 账号切换下拉直接点选项的防御性修复。
  - 加载提速：新增 GET /api/instances/cpu-models（只读缓存，不调 OCI，本地中位数 6.5ms），启动盘页三路并行加载、各自渲染。
- 后端备份类型透传：`BACKUP_TYPES` + `validate_backup_type`，`CreateBackupBody.type`，非法值返回 400；免费备份满额时增量备份返回 400，不删除已有备份。

## [2.20.0] - 2026-11-28
- 没开机脚本也要跟踪 DD 完成并通知：DD 初始化跟踪以前只在提供了开机脚本时才启动，没脚本就不跟踪、不发 DD 完成通知（用户新建的 ARM_2026-11-28 就是因此没收到 DD 完成 Bark）。现改为只要开了 DD 且有 root 密码就跟踪：DD 完成后不跑脚本，只验证系统就绪（root 密码登录可用、sshd 运行中），同样发 DD 完成与初始化完成双通道通知；DD 完成通知的文案按有/无脚本区分（有脚本“开始执行初始化脚本”，没脚本“无初始化脚本”）。机器人与网页端开机/抢机成功后的 DD 门槛同步放宽。

## [2.19.0] - 2026-11-28
- 开机表单默认值改成用户偏好：延迟默认 20 秒、IPv6 默认有、DD 重装默认开（原来延迟无默认值、IPv6 默认无、DD 默认不开）。改动只影响交互式开机表单和一键开机（新 `new_user_form()`）；`new_form()` 保持中性（DD 关、IPv6 关、无延迟），预设与抢机任务不受影响。
- DD 重装选项改名：原来“开（Debian 13）”括号太长，改成“开 Debian13”，摘要文案里的名字同步改成 Debian13。
- /raninfo 菜单标签改名：原来“重置随机用户信息”名不副实，改成“重置Web管理密码”；功能本来就是重置面板管理员密码（随机新密码只发一次），命令仍是 /raninfo，功能未动。
- CPU 型号显示补全：A1 实例一律显示“A1配额内免费”；E2 实例未检测到型号时回落到完整型号“AMD EPYC 7J13”，不再只显示光秃秃的“AMD”；检测到了仍显示实测值。列表延迟补拉与详情重测两处都走同一规则。

## [2.18.0] - 2026-11-27
- DD 初始化后强制开回 root 密码登录：初始化脚本跑完后（不管脚本是否关了密码登录）写 `/etc/ssh/sshd_config.d/60-mbot-root-login.conf`（PermitRootLogin yes + PasswordAuthentication yes）并 reload sshd，开机时给的 root 密码继续可用；跑完后 M_Bot 再用 root 密码 SSH 进去验证状态（密码登录可用、sshd 运行中、Debian 13），验证结果写进初始化完成通知（Telegram + Bark 双通道）。机器人与网页端的初始化完成通知改成直接发事件消息，原来“初始化脚本已关掉密码登录，之后请用 SSH 密钥登录”的说法已作废；开机成功通知与 DD 完成通知本来就是双通道，未动。
- 实例卡片 SSH 一键直连：实例卡片点 SSH 直接用固定 root 密码自动登录 root（服务端环境变量 `MBOT_FIXED_ROOT_PASSWORD`，只在服务端内存里用，绝不返回前端、不记日志），端口先试 14422（DD / 加固脚本改的端口）再试 22（OCI 默认）；未设固定密码、实例无公网 IP、或自动登录失败时回落到手动连接表单（预填主机）。注意：固定 root 密码须先在 env 文件里设置并重启服务才生效，未设置时开机密码是随机 UUID、面板无处可读，无法自动登录。
- 终端顶栏“断开重连”改成“关闭此SSH窗口”：点一下关闭当前 SSH 会话窗口（标签与后端会话一起关），不再是断开重连；断线自动重连仍在（最多 3 次），重连失败的提示同步改成“请关闭本窗口后重新连接”。

## [2.17.0] - 2026-11-27
- 安装器加固：UFW 默认策略显式收紧为入站拒绝 / 出站放行（`ufw default deny incoming` / `ufw default allow outgoing`，在 `_firewall_ufw` 开头做；不做 `ufw reset`，升级重跑不会清空已有规则）。
- 安装器加入网络优化：TCP 缓冲调大（rmem/wmem 最大 64MB，`net.ipv4.tcp_rmem` / `tcp_wmem` 最大 64MB）+ 有 `tcp_bbr` 模块时切 BBR + fq，跨太平洋单流从 120–140 Mbps 涨到约 830 Mbps，全部写 `/etc/sysctl.conf` 与 `/etc/modules` 持久化，装机与升级都跑（幂等）。
- 安装器加入 Fail2Ban 防 SSH 爆破：`fail2ban` 已加进安装依赖；jail.local 照 VPS 初始化脚本（默认 bantime 60000 / sshd 段 864000），SSH 端口自动识别（sshd 实际监听端口 > sshd_config > 22，复用现成的 `detect_ssh_port`），封禁动作跟防火墙走（ufw 在就用 ufw，否则 iptables-multiport）。

## [2.16.3] - 2026-11-27
- 修复“重启机器人服务失败”误报：重启其实成功了（服务已重启），但机器人调 `systemctl restart rbot-clone-bot` 时，那个 systemctl 进程是机器人的子进程、落在同一个 cgroup 里，systemd 停服务会把它一起杀掉，它永远回不了 0，代码就以为重启失败。现重启机器人自身时加 `--no-block`，让 systemctl 下发任务就立即返回 0，之后 systemd 再停服务、起新进程。重启面板服务不受影响（不在机器人自己的 cgroup 里），照常等任务完成再确认。
- 开机 root 密码改放三重反引号代码块（和 /raninfo 重置密码一样）：抢机启动消息里“root 登录密码”点一下就能复制纯密码，不带尾空格/换行。重启不丢抢机任务：机器人与面板启动时都会按任务记录里的 grab_spec 把还在跑的抢机任务恢复重跑（剩余台数 = 总数 − 已抢到），bot-g5（E2.1.Micro·AMD_2026-11-27）规格完整，部署重启后会自动续跑。

## [2.16.2] - 2026-11-27
- 修复“运行中的任务”列表太乱：机器人菜单 21 叫“查看运行中的任务”，但一直调的是全部任务列表（`list_all()`），已完成、失败、取消的历史任务全都列出来。现只列运行中的任务（`TASKS.running()`），没任务时只显示“当前没有运行中的任务。”；刷新按钮走同一入口，同样只显示运行中的。
- 任务列表改成一行一个任务：每条只显示 label（规格·地区·实例名），不再显示 detail 进度行，列表一眼就能看完。
- /raninfo 重置密码后，新密码改放代码块里：Telegram 会给代码块一个复制按钮，点一下拷到的就是纯密码、后面不带空格；原来行内反引号长按选中会把后面的空格也带上，粘贴到登录框会失败。消息里现在先显示帐号（面板管理员用户名），下面再显示密码。

## [2.16.1] - 2026-11-27
- 修复“配置里删掉账号，列表里还在”：面板以 4 个 worker 运行，各 worker 的内存账号表与进程环境变量是启动时的快照——删账号只改了 env 文件 + 当次 worker，别的 worker 里还留着已删账号的 `MBOT_COMPARTMENT_*`，列表一刷又把它加回来。现账号表以 env 文件为准（`_build_profiles` 读 env 文件，不读残留的进程环境变量；文件不存在时回落到环境变量），且账号列表接口每次都从磁盘重读，不再用可能过期的 worker 内存。删账号后列表立即更新，不用重启面板。

## [2.16.0] - 2026-11-27
- 云账户页的“刷新”按钮现在真的重读配置：点一下就重读 /root/M_bot_config.conf 并重生成 OCI 配置、刷新账号列表，不用重启面板；账号有增减时会提示“机器人需重启后生效”（机器人是独立进程，仍需重启才认到新账号）。改了 .conf 加租户后点刷新即可，不用再重启服务。
- 私钥上传本来就已在账号列表里：每行账号都有“上传私钥”按钮（缺失时显示“缺失，需上传”），点开选 .pem 就写到配置里的 key_file 路径；解析配置的确认步骤里也能逐个上传。
- **新增用户改成最高管理权限**：面板 IAM 管理与机器人菜单 8 的“新增用户”现在创建后立即把用户加进 Administrators 组（最高管理权限），不再是无权限的空账号；加组失败时用户已创建，页面/机器人会如实提示原因（可手动加组或删除）。新用户仍需另行重置控制台密码才能登录。
- **添加 API 密钥（粘贴公钥 / 选择公钥文件）**：除了原来“生成新密钥对”，现在可以在面板 IAM 密钥管理与机器人用户详情里直接粘贴已有的 RSA 公钥（PEM）或选一个公钥文件，上传为该用户的 API Key；先校验是有效 RSA 公钥再上传，3 个 Key 的上限仍会检查；成功后返回指纹与状态。

## [2.15.0] - 2026-11-27
- 面板新增 8 个主题：森林（深绿暗色）、海洋（深蓝暗色）、曜石（近黑暗色）、落日（暖阳亮色）、樱花（淡粉亮色）、薄荷（清绿亮色）、冰蓝（冷蓝亮色）、沙漠（暖沙亮色），经典主题保留且仍为默认；顶栏主题下拉框可直接切换，选择存 localStorage，刷新不丢。
- 只有经典主题跟随明暗切换；星夜/森林/海洋/曜石固定暗色，落日/樱花/薄荷/冰蓝/沙漠固定亮色，在这些主题下点明暗切换按钮会提示“该主题固定为暗色/亮色”。新主题各带一层柔和的环境光晕（纯 CSS 渐变，不引用图片）。
- 主题名中英文同步（森林 Forest / 海洋 Ocean / 曜石 Obsidian / 落日 Sunset / 樱花 Sakura / 薄荷 Mint / 冰蓝 Ice / 沙漠 Desert）；登录页保持经典暗色不变。
- 修复“上传 API 配置识别不到”：粘贴的 config 用了已存在的 profile 名（如 [DEFAULT]）时，旧逻辑只按名字去重、新账号会被当成“全部已存在”丢掉；现按账号身份（租户 + 用户）去重，身份不同就是新账号，自动改名（DEFAULT → DEFAULT_2）导入并在页面与机器人端都提示改名；同一身份换名粘贴仍会跳过，不重复导入。

## [2.14.5] - 2026-11-27
- IAM 用户列表移动端 UI 重构：卡片改为头部（用户名 + 状态徽章并排）/ 邮箱 / 三按钮网格结构，按钮改用微质感样式（密钥管理灰、重置密码浅蓝、删除浅红），窄屏不再挤成一团；Toast 改为顶部居中悬浮，不再遮挡底部表单与按钮。
- **Bot 用户收回 manage users 权限（用户决定）**：Api_User_Policy 已从真实租户上删掉 `Allow group Group_for_Api_used to manage users in tenancy`，api_user 现在只有实例/卷/虚拟网络三项，不能再改别人密码、删用户、管 API Key；`create_bot_permission_user` 也不再补这条（以后点“创建 Bot 用户”不会再加回来）。IAM 用户管理只走管理员 profile（面板 DEFAULT）。机器人端与网页版的权限说明同步改掉。
- 说明：后端 Policy 更新与 test_iam.py 断言在 v2.14.4 / v2.14.3 已修好（--version-date 补当天日期、空备注默认描述），本次不再改动；传入空串 "" 与当天日期效果等价（都表示按当前策略语言版本评估）。

## [2.14.4] - 2026-11-27
- 修复“创建 Bot 用户”失败：已存在的 IAM 策略补语句时，`oci iam policy update` 只传了 `--statements`，OCI 要求改 statements 时必须同时传 `--version-date`（否则报 "both parameters must be specified"）。现沿用策略现有的 version-date，没有时用当天日期；Api_User_Policy 的 version-date 是 null，已用一次性测试策略在真实租户验证通过（不碰真实策略）。
- 补充说明：面板上点“重置密码”时，若当前 API 用户就是被重置的用户本人且 OCI 拒绝，属于 OCI 侧限制（初始管理员自重置返回 404），面板会给出诊断提示，不是面板主动拦截。

## [2.14.3] - 2026-11-27
- 修复 IAM 删除用户失败：`sdk_list_group_users` 调了 OCI SDK 里不存在的 `list_users_in_group`（直接报 AttributeError），改调官方接口 `list_user_group_memberships`（传租户 OCID + group_id，支持分页）；最后管理员判断改读成员关系里的 `user-id`。网页版与 Telegram 删用户共用同一条 `is_last_admin`，一次修好两边。
- 修复 IAM 新建用户备注留空失败：`--description` 是 CLI 必填参数，但服务端拒绝空字符串（InvalidParameter: description must not be blank）；备注为空时默认用 `Managed by M_Bot: <用户名>`，超过 400 字符直接拒绝。
- IAM 新建/删除失败不再把整段 ServiceError JSON 贴给用户，只取其中的 message 字段并翻成中文。
- 新增回归测试 `tests/test_iam_regressions_20261127.py`（5 项）：用真实 SDK 的 UserGroupMembership 模型验证最后管理员判断，不再只 mock 掉关键判断函数。

## [2.14.2] - 2026-11-27

- 修复 IAM 创建 Bot 用户/新增 API Key 时 PEM 私钥下载区被 OCI config 覆盖的问题；两个文件现在独立显示、复制和下载
- 优化一次性凭据区域的窄屏布局与按钮交互
- 当前 API 用户重置自身密码遇到 OCI 拒绝时，提供区分于管理员代重置的诊断提示；不绕过 OCI 权限校验

## [2.14.1] - 2026-11-27

- IAM 用户列表对所有用户统一显示“重置密码”，移除前端按当前 API 用户和控制台能力隐藏按钮的逻辑；Web 面板重置密码接口不再预先过滤用户，最终以 OCI IAM 响应为准，失败时把 OCI 错误映射为中文友好提示（不暴露原始 ServiceError JSON）；Telegram 重置流程同步明确提示；新增排查文档 docs/iam-password-reset-troubleshooting.md
- Bot 专用 API 用户策略新增 `Allow group Group_for_Api_used to manage users in tenancy`（租户级 IAM 用户管理高权限，可管理租户内所有 IAM 用户及其凭据；已存在策略时下次创建 Bot 用户会合并补齐），用于面板/机器人中的用户创建、删除、API Key 与控制台密码管理
- Telegram Oracle 菜单新增 Oracle 全球区域测速与 Cloudflare 优选 IP 入口
- 面板统一圆角、焦点态、触控尺寸与窄屏布局细节，IAM 用户列表改为手机端卡片式，改善手机端表格和 IAM 表单滚动/排列

## [2.13.1] - 2026-11-27

- 修 IAM 重置密码报错：三个用户都重置不了是有原因的——rbot-test 与 api_user 关了控制台登录（can-use-console-password=false），管理员账号就是当前 API 用户自己、OCI 不许用 API 重置自己的密码；现在重置前先预检这两种情况并直接说明原因，不再等 OCI 报错；OCI 报错时也不再把原始 ServiceError JSON 甩到提示框里，翻译成中文；不能重置密码的用户不显示“重置密码”按钮（列表接口返回 current_api_user_id，前端按 capabilities 与当前 API 用户判断）
- 新建用户/新增 API Key 后生成 OCI 配置文件：机器人端创建 Bot 用户后会发 config 片段，网页版之前只给私钥 PEM、没给配置文件；现在创建 Bot 用户与新增 API Key 都随私钥一起返回一段可直接用的 config（段名/user/fingerprint/key_file/tenancy/region），前端显示为可下载的 oci_config.txt，可直接粘贴到云账户页“添加租户”

## [2.13] - 2026-11-27

- 网页版 IAM 用户管理（与机器人菜单 8 同步）：云账户页新增“IAM 用户管理”卡片，可按账号列出 IAM 用户、新增普通用户、创建 Bot 专用 API 用户（api_user + Group_for_Api_used + Api_User_Policy，幂等复用）、给用户新增 API Key、重置控制台临时密码、删除用户；新增 API Key 的私钥与临时密码只在本次响应里返回一次，前端提供下载/复制，刷新后不再可查；删用户/删 Key 的保护与机器人端一致（禁删当前账号正在用的 API 用户/Key，禁删 Administrators 组最后一个管理员）
- 修云账户页手机排版：已配置账号表（7 列）与添加租户解析表（5 列）此前没有横向滚动容器，手机上把页面撑宽、左右列被截断；现在两个表都包进 .table-wrap，窄屏横向滚动

## [2.12] - 2026-11-27

- TG 机器人提速（补丁集成）：实例列表单飞（同一账号/区域/语言的并发请求只加载一次）+ VNIC 查询并发压到 2；实例详情 12 秒缓存 + 单飞；CPU 型号 SSH 检测单飞（同实例只测一次）+ 最多 2 并发 + 失败后 60 秒冷却；写操作清缓存时旧的在途加载不再把旧状态写回来
- IAM 云账户管理增强：新增 oci/iam.py（IAM 用户/API 密钥管理、RSA 密钥生成、Bot 专用 API 用户一键创建）；菜单 8 可增加普通用户、创建 Bot API 权限用户（api_user + Group_for_Api_used + Api_User_Policy）、给用户新增 API Key（私钥只发一次 TG 文件）、重置控制台临时密码；删用户/删 API Key 时仍禁删机器人当前在用的 API 用户/密钥
- 管理员保护（新增）：OCI 本身不拦删最后一个管理员，M_Bot 现在拦——删用户时先查 Administrators 组，目标是组里最后一个成员时拒绝删除并提示先加另一个管理员；删除确认页与最终确认两处都查
- 补丁集成说明：ChatGPT  overlay 补丁（tgbot/handlers.py、tgbot/handlers_extra.py、oci/iam.py）已合入并适配——修了补丁自带测试对 sys.modules['telegram'] 的覆盖（会拖垮全量测试发现）、attach_public_ips_bulk 的 max_workers 参数、CLI 改 SDK 后 list_users/list_api_keys 的测试 mock、create_user 的等待参数；全量 3223 测试全过

## [2.11] - 2026-11-27

- 多 worker 前置打通：会话、登录锁定、TG 验证码、一键登录共 4 个内存存储一起迁到 SQLite（web/data/mbot_sessions.db，WAL 模式，权限 600），多 worker 下登录态、锁定、验证码、一键登录全部跨 worker 一致；公开方法签名不变，17 处调用点零改动；验证码一次性消费与锁定计数用 BEGIN IMMEDIATE 事务保证原子性（两 worker 同时验同一码只能一人成功）。新增 web/auth_store.py（AuthDB）与 21 个跨 worker 测试；全量 3216 测试全过；双进程实测：worker 1 登录后 worker 2 的 /api/auth/me 返回 200，worker 2 登出后 worker 1 立即 401，worker 1 上 5 次错误密码后 worker 2 登录报 429
- 行为变化：登录会话重启后保留（以前内存存储重启即失效），仍按有效期过期（默认 12 小时，“记住我” 30 天），登出立即失效；多 worker 下各 worker 启动时不能互相清会话，所以启动时不再清会话
- 多 worker 开启方式：.160 的 rbot-clone-web.service 的 ExecStart 末尾加 --workers 4（服务文件只在 .160 上，不在仓库里）；4 worker 下 100/1000/10000 人压测待部署后实测

## [2.10] - 2026-11-27

- 强制刷新减负：面板每次强制刷新都重建 OCI 客户端，私钥解析（约 0.39 秒/次，61%）与 TLS/CA 重建（约 0.14 秒/次，22%）是 CPU 大头；现在 Web 层按账号+区域缓存复用 OCIClient（删账号时清掉该账号缓存），实例列表与 VNIC 挂载列表并行取。强制刷新 CPU 约 1.01 秒→约 0.34 秒（降 66%），墙钟约 2.69 秒→约 1.8 秒；日常点菜单（缓存命中）仍约 0 秒
- 核心修复：抢机后台线程写任务日志时，若共享文件所在目录被删（如测试清理临时目录），原先抛 FileNotFoundError 杀死线程、任务卡在 running；现在网页版与机器人版 TaskRegistry 的更新写盘失败只记 warning 不抛异常，线程继续收尾；测试 tearDown 先等抢机线程结束再删临时目录。新增 2 个回归测试；全量 3195 测试全过
- 多 worker 未做：会话、登录锁定、TG 验证码、一键登录共 4 个内存存储须一起迁到 SQLite 才能开多 worker，只迁会话会让登录锁定按 worker 计（安全倒退）且验证码跨 worker 校验失败；留到下一轮

## [2.09] - 2026-11-26

- Telegram 机器人提速：更新一次只处理一个改成并发处理 4 个（concurrent_updates(4)，连点不再排队）；实例列表加 30 秒缓存，重复点直接返回；取公网 IP 改批量接口（每台 3 次 SDK 调用降到 1+N 次）；额度按钮开头先应答，转圈立即消失；详情页查实例与查 IP 并行，CPU 型号 SSH 改后台刷新（先显示“检测中”）；OCIClient 按账号+区域缓存复用。实测：列表 2.8–3.2 秒→缓存命中约 0.3 秒，详情约 1.2 秒，额度约 1.5 秒
- 面板 CPU 突发修复：VNIC 挂载列表与公网 IP 映射进共享缓存并参与预热，冷切菜单不再每次调 1+N 次 SDK；共享缓存加单飞（singleflight），10 个并发冷请求只调 1 次 OCI；后台 SSH 测 CPU 型号限流（最多 2 个并发 + 60 秒冷却）；JSON 模型转换减分配。冷切菜单实例列表 2.51 秒耗 CPU 0.81 秒（修前实测），修后待部署实测

## [2.08] - 2026-11-26

- DD 流程修复：InstallNET.sh 上游结尾只打印“Input 'reboot' …”就 exit 1，不会自动重启（上游把 sleep 5 && reboot 注释掉了），DD 会一直等不到新系统而 30 分钟超时；生成脚本现在不管 InstallNET.sh 退出码是什么都在后面 sleep 5 再 reboot（成功时进安装器，失败时原地重启回原系统）。哨兵测试机 ARM_2026-11-26 的 DD 就是这么卡住的（串口日志显示还在原 Ubuntu 内核）

## [2.07] - 2026-11-26

- 统一配置文件 M_bot_config.conf 改放家目录：默认位置从安装目录改成 ~/M_bot_config.conf（root 安装时是 /root/M_bot_config.conf），安装器新装直接写那里，systemd 的 MBOT_CONFIG_FILE 也指过去；旧部署升级时启动自动把安装目录那份搬到家目录（只搬一次，不留软链），显式配了 MBOT_CONFIG_FILE 的部署不动；.env、管理员密码、oci_config 仍在安装目录

## [2.06] - 2026-11-26

- 统一配置文件 M_bot_config.conf：纯文本 .conf 格式（仿 R-Bot client_config.txt 样式，中英双语 `#` 注释），手动维护的三节 oci（多 profile，每项 user/fingerprint/key_file/tenancy/region）/cloudflare/bark 在前，44 个 `#` 分隔线后是暂时不用的预留节（username/password、local_address/model、azure、ssh、solusvm）；mbot 生成的文件全部搬到安装目录（.env、管理员密码、oci_config、M_bot_config.conf），/root 只留用户自己放的 .pem 私钥（key_file 写什么路径就读什么路径）；oci_config 启动时从 .conf 自动生成，不再手改；MBOT_* 那些 bot 自动写改的值继续留 env 文件。新增 mbot_config.py（解析、保存、迁移、生成 oci_config），附 M_bot_config.example.conf 样板
- 安装时证书方式自动检测：有域名 + Cloudflare 凭据（CF_Key/CF_Email 或 CF_API_TOKEN）走 cfdns 自动申请；有域名没凭据走 http；域名留空走 IP 证书；MBOT_INSTALL_CERT_METHOD 显式指定仍优先；更新模式也认该变量（以前更新时不读它，默认走 http）
- 安装面板端口 443 优选：装面板时先看 443 空不空闲，空闲就走 443（标准 HTTPS 端口），被 xray 之类占了就回落 9527；自签证书、IP 证书、域名证书三条路统一走优先端口；防火墙只放行面板实际用的端口；自检与最后打印的面板地址按实际端口显示

## [2.05] - 2026-11-26

- 新机支持“DD 重装 + 初始化”两阶段流程：建机表单加“DD重装”开关，开启后创建时先用 InstallNET.sh 重装 Debian 13（SSH 端口 14422、swap 1024MB），DD 完成后机器人后台经 SSH 上传初始化脚本并执行（系统更新、BBR、SSH 加固到 14422 密钥登录、UFW、Xray、Fail2Ban、CrowdSec、流量看门狗等），完成或失败都双通道推送；每台新机生成 UUID root 密码随开机信息推送。新增 oci/dd_init.py（DD 脚本生成、等 DD 完成、传脚本执行、超时与事件回调），Bot 开机表单与 Web 创建接口同步支持 dd 参数
- CPU 优化第 4–6 轮：实例列表页取公网 IP 从逐台查改成批量查（1+2N 次 SDK 调用降到 2+N 次）；Web 层新增共享 OCI 数据缓存（web/oci_cache.py，实例/启动盘原始列表按账号+区域+compartment 缓存 30 秒，实例、配额、磁盘三个端点共用，配额汇总内部实例列表只取一次）；后台缓存预热线程（每 10 秒把快过期的缓存条目提前刷一遍，只保暖不点火，面板闲置时不产生后台调用）。冷请求系统 CPU 从 42% 降到约 13%，闲置 35 秒后点面板配额接口 0.0 秒返回
- 演示模式：设 MBOT_DEMO_MODE=1 后面板返回模拟数据，demo/demo 可免登录直进，写操作只返回假成功，不调 OCI、不改真实状态
- 访问统计从 Clarity 换成自建 Matomo（MBOT_MATOMO_URL / MBOT_MATOMO_SITE_ID，两项都配了才加载统计代码）
- Web 前端 JS 修复（实例列表、配额、磁盘等页面的显示与交互问题）
- 开机成功通知带 CPU 型号（拿不到时按 Shape 推断 AMD/ARM）
- 新机 root 密码支持固定密码：环境变量 MBOT_FIXED_ROOT_PASSWORD 非空时所有新机都用它（只记一个就行），未设置时回落到每台生成 UUID；密码只存在部署服务器的环境变量文件里，面板不存、不读、不展示

## [2.04] - 2026-11-25

- 任务管理可以删除任务（用户 2026-11-25 反馈）：已结束的任务操作列加“删除”按钮，确认后删掉任务记录和日志；运行中的任务没有删除按钮，须先取消（后端也拦：删运行中的任务报 400）。新增 DELETE /api/tasks/{id}、TaskRegistry.delete()、task_store.delete_record()；状态按当前语言显示（中文模式 DONE→已完成、CANCELLED→已取消、RUNNING→运行中、FAILED→失败等，英文模式显示英文原文），新增 i18n 的 TASK_STATUS_ZH 与 taskStatusLabel()，与实例/启动盘状态的既有本地化模式一致
- DNS 管理看不到删除按钮（用户 2026-11-25 反馈）：删除按钮代码本来就有，是“内容”列太宽把它挤出可视区了；修法：内容列加 dns-content 样式（限宽 320px、允许换行），操作列（编辑/删除）不再被挤出可视区
- 实例卡片 CPU 型号显示“未检测”（用户 2026-11-25 反馈）：没检测到型号时按 Shape 推断架构显示（A1.Flex→ARM、E2→AMD），已知形状不再显示“未检测”；检测到了仍显示具体型号。新增 instances.js 的 cpuModelLabel()，卡片与详情页共用
- 自动升级同时检查 nginx（用户 2026-11-25 要求）：每天凌晨 4 点的定时自动升级原来只查 mbot，现在同时查 mbot 与 nginx；两者都已是最新版时只记日志跳过，不碰服务；只升有新版的那一个。新增 install.sh 的 nginx_update_available()（apt 已装版 vs 候选版比对）与 upgrade_nginx()（--only-upgrade、--force-confold 保留现有配置、校验通过后重载）；mbot 检查失败时不再直接返回，会继续做 nginx 检查

## [2.03] - 2026-11-25

- 脱敏修复（用户 2026-11-25 发现）：公开版 install.sh 的注释里（546-547 行）写了用户的真实域名；修法：注释里的真实域名改成 example.com（中英文括号都改），公开版全树现已无该域名；面板的访问域名从未进过公开版；只改注释，未改任何功能代码

## [2.02] - 2026-11-25

- 绑定域名输二级域名后无反馈（用户 2026-11-25 手机实测）：根因是 python-telegram-bot 每 group 只跑第一个匹配的 handler，三个文字 handler 全在默认 group 0，先注册的改名/重装文字 handler 把所有纯文字消息吞了，绑定域名的 handler 永远跑不到；修法：文字 handler 按用途分 group（改名/重装 group 0、绑定域名 group 1、客户端工具 group 2、/oci 粘贴 group 3），各自只认自己的 user_data 状态；另加三道防线：绑定流程开始时清掉残留的 pending_input，改名/重装与客户端工具 handler 见到 bind_domain_pending 已选 Zone 时不消费，/oci handler 同样让路；顺带发现并修了 /oci 粘贴文本也被吞的同类问题；输完二级域名后必有明确反馈（成功/已存在/冲突确认/格式错/CF 报错各分支原有逻辑不变，现都能走到）
- “Message is not modified” 报错：全库 100 处 query.edit_message_text 改走 menu_tracker.safe_edit，编辑前先比对当前文本与 markup，完全相同时跳过编辑、不调 API；底层仍报时只记 debug，不刷 ERROR、不发给用户
- “Message to edit not found”：safe_edit 编辑失败（消息已删/不存在）时降级发新消息；全局 error_handler 里这条也不再把 Telegram 原文发给用户，改发人话“你点的那条消息已经不在了，请重新发送命令打开菜单”
- /domain DNS 列表看不到已绑定记录、删不了（用户 2026-11-25 手机实测）：Bot 端：列表里 A 记录且内容 IP 是 M_Bot 管理的实例公网 IP 时标“🔗 已绑定到实例 {名}”，末尾汇总已绑定条数；实例映射优先用缓存的实例列表，缓存为空时尽力从 OCI 拉一次，失败降级为无标注、不阻塞列表；删除选记录时已绑定的排前面并带 🔗 实例名，确认页加警告（已绑定：删后实例失去域名指向；MX/TXT/CNAME 等非地址记录：删可能影响邮件/验证）；删完回执“已删除 {记录}”，只删选中的一条。Web 端：GET /api/dns/zones/{zone_id}/records 每条记录加 bound_instance 字段（A 记录且 IP 等于实例公网 IP 时为实例显示名，否则 null；OCI 失败降级为全 null 不阻塞列表），前端显示“🔗 已绑定到实例 {名}”徽标，删除确认里对已绑定与 MX/TXT/CNAME 加警告
- 登录页 GitHub 链接去重与改名（用户 2026-11-25 手机实测）：2.01 加的“GitHub开源主页”那行删掉（与页脚“GitHub 项目”重复，登录页上两个 GitHub 链接），只留页脚一个；页脚“GitHub 项目”改名“GitHub 免费开源项目”，链接地址不变（仍指向 https://github.com/jimu2000/M_Bot，新标签页打开）；英文模式同步改（“GitHub Free Open Source Project”），登录页 GitHub 链接只出现一次
- /start 欢迎与主菜单加 GitHub（用户 2026-11-25）：/start 主菜单的“❤️ 一键测活”按钮换成“GitHub 免费开源项目”URL 按钮，点开 https://github.com/jimu2000/M_Bot；一键测活功能本身保留，仍在 /oracle 菜单第 15 项；/start 欢迎消息正文在“常用命令”上方加一行“GitHub 免费开源项目：https://github.com/jimu2000/M_Bot”，链接以纯文本显示出来，英文模式对应写 “GitHub Free Open Source Project: https://github.com/jimu2000/M_Bot”
- 脱敏修复（2.02 脱敏检查挖出）：救机 Netboot 向导的 TFTP 节点列表里硬编码了用户自备的新加坡节点真实 IP（2.01 已带进公开仓库）；修法：代码里只留示例占位地址 203.0.113.10，自己的节点改填环境变量 MBOT_TFTP_NODE（部署时写进 .160 的 /root/.rbot-clone.env 即可恢复原来的默认节点），Web API 的 ?tftp_server= 参数覆盖仍可用。注意：2.01 的 GitHub 历史里仍有这个 IP，须重写历史才能彻底清除（与待办的 Git 历史重写同一件事）
- ARM 面板全功能测试挖出的 Bug（测试对象是 v1.43.0 的 ARM 面板，2.01 里同样存在）：①高危：STOPPED 实例从实例列表消失——根因是后端 GET /api/instances 早就有 state=all（含全部非 TERMINATED 实例），但前端实例列表加载与 CPU 型号补拉两处一直调默认 state=running，停掉的实例从列表消失、没法从面板再点启动（升降配期间实例也会因非 RUNNING 消失）；修法：两处都改成 state=all，STOPPED 卡片有“启动”按钮；②中危核实：CPU 筛选未生效仍放行抢机——2.01 已修（guard 未配时置 cpu_unverified、通知显示“CPU 未验明”，面板建抢机默认带 7J13-only 筛选），本版加测试核实：显式传 target_cpu=7J13 且无 guard 密钥时，Grabber 不带 CPU 筛选、不调 make_cpu_check_hook、通知标 cpu_unverified、任务日志写明“未配置 guard 密钥”；③低危：Web SSH 终端横幅仍是旧品牌“R-BOT · Web SSH”，重画成 M_Bot 五字形横幅（M_BOT_ART），横幅行改成“M_Bot · Web SSH”，全库非测试源码里已无 R-BOT/R_Bot 残留；④低危：云报告启动盘计数短暂滞后——根因是 GET /api/cloud-report/summary 整份缓存 60 秒，终止/创建后 60 秒内仍是旧数，clear_cloud_report_cache 只被测试调用、mutation 处从未失效；修法：在报告会受影响的 mutation 处加失效调用（实例批量操作/单个启停重启/终止、从备份恢复、启动盘扩容与删除、抢机建机成功、换盘成功），与现有的实例/启动盘缓存失效并列；⑤低危：删备份的 confirm 必须走 query 参数——DELETE /api/boot-backups/{backup_id} 现同时接受 query ?confirm=true 与请求体 {"confirm": true}，都不传时 400 报错写明两种传法；⑥低危：建抢机时镜像架构与形状不匹配未事前校验——aarch64 镜像 + E2 形状要等抢机任务跑到 OCI 才报 InvalidParameter；修法：前端提交前用下拉选项的 data-arch 对形状要求的架构（E2 要 x86_64、A1 要 aarch64），不匹配直接报错；后端 POST /api/create 在 resolve_image_id 后同样校验（占位符的架构取自后缀不调 OCI，真实 OCID 查 image get 从显示名推断架构——OCI 的 image get 没有 architecture 字段，推断不出时不阻塞创建），不匹配返回 400 明文（写清镜像架构、形状要的架构、该换镜像还是改形状），不启动 Grabber

**验证**：全量测试 2941 个全过（2.01 基线 2893，净增 48 个）。新增用例：绑定域名分发 4、safe_edit 6、error_handler 2、Bot 域名标注与删除 12、Web 域名标注 6、登录页与 /start GitHub 3、STOPPED 实例列表 1、CPU 筛选核实 1、云报告缓存失效 1、删备份 body 确认 2、镜像架构校验 10、TFTP 节点环境变量 1。本版只做本地改动，未部署到服务器；部署后需真机验证（手机上绑定域名输二级域名有反馈、/domain 列表能看到已绑定标注并能删、停掉的实例仍在列表里能点启动、aarch64 镜像 + E2 形状提交即报不匹配）。

## [2.01] - 2026-11-25

- Web 中英文切换：顶栏右上角语言下拉（简体中文 / English）真正可用，默认中文；切换后整站文案（导航、按钮、标题、提示）跟着变，登录页没有顶栏、页内另放一个切换入口，共用同一套逻辑；语言选择存 localStorage（键 mbot-lang），刷新不丢；JS 动态渲染的文案也跟着切（概览页云报告表格与主机卡片、实例页状态徽标与已选台数等），覆盖登录、概览/云报告、实例、创建、设置/导航等主要页面；翻译字典在 web/static/js/i18n.js，data-i18n 标注静态文案；语言设置三个选项：中文、英文、跟随浏览器语言（选跟随后按浏览器语言自动切，只认 zh 开头→中文、en 开头→英文，俄文/日文/法文等其他语言一律回落到英文，面板永远不显示不支持的语言；手动选中文/英文后固定，不再跟随；“跟随”模式本身也存 localStorage，刷新后仍跟随）
- Bot 中英文切换：Telegram 机器人支持中文/英文切换，切换入口在 Oracle 菜单与主菜单底部的“🌐 Language”按钮 + /lang 命令；语言选择按用户存 tgbot/data/user_lang.json（0600，原子写入），每用户独立；切换后菜单、提示、按钮文案跟着变，覆盖主菜单、Oracle 菜单 1–44 项、实例/开机表单/配额/流量/费用等主流程；约 60 处深层文案（fmt_error、磁盘/备份、GFW、Bark、域名/CF、对象存储、后台通知）暂未覆盖；Web 与 Bot 的语言偏好各自独立存，不强制同步；翻译字典在 tgbot/i18n.py
- 云报告状态中文显示：概览页云报告表格里的实例状态改中文显示，RUNNING→运行中、TERMINATED→已终止、STOPPED→已停止、PROVISIONING→创建中、STARTING→启动中、STOPPING→停止中、TERMINATING→终止中、CREATING→创建中，未知状态兜底显示原文；英文模式下显示 OCI 原文；只改显示文字，数据与 GET /api/cloud-report/summary 不变；实例页状态徽标同步改中文
- GitHub 链接补齐：登录页“默认凭据请查看部署文档”那行上方再加一行“GitHub开源主页”，带链接 https://github.com/jimu2000/M_Bot（新标签页打开），页脚已有的“GitHub 项目”保留；内页顶栏 GitHub 图标（Feather 描边）在手机端（≤900px）与电脑端都可见、不被顶栏其他元素挤掉或隐藏（手机端收起 logo 副标题、缩间距、下拉限宽，图标 flex-shrink: 0）
- 静态文件防缓存：/static 挂载处加 Cache-Control: no-cache，浏览器每次加载都重新校验，不再按启发式缓存旧 JS/CSS（v1.45.0 云报告表格推上 .160 后手机 Safari 半天仍是旧文字版就是这个原因）；不用改模板里的 20 处静态引用、不用加 ?v= 版本号
- 自动升级定时任务去重：setup_auto_upgrade() 写 /etc/cron.d/mbot-auto-upgrade（每天凌晨 4 点）的同时，清理 root 本地 crontab 里旧的 mbot 自动升级任务（只删同时含 auto-upgrade 与 mbot/install.sh 的行，其余内容不动；crontab 为空或无 crontab 命令时不报错），只留新写的 cron.d，避免一天升级两次；MBOT_INSTALL_NO_AUTO_UPGRADE 非空时仍只删 cron.d 并跳过
- 开机成功通知加 CPU 型号：抢机时 guard SSH 实测的 CPU 型号顺下来（GrabResult 新增 cpu_model 字段，重启恢复时经任务记录一并保存），通知里“cpu类型”行有型号时显示具体型号（如“cpu类型：AMD EPYC 7J13”、A1 显示“cpu类型：Ampere Altra”），拿不到型号时回落到原来的 AMD/ARM，CPU 未验明提醒行保留；Bot 与面板双端同时改，TG 与 Bark 用同一份文本
- 面板抢机默认筛 CPU：Web 建 E2 抢机任务时 target_cpu 留空不再是不筛选，_run_grabber 按 cpu_policy.json 的允许型号（默认只留 7J13）自动补上，grab_spec 里同样带上，重启恢复时也筛；抢到后 M_Bot 自己走 guard SSH 验 CPU，非 7J13（如 7742/7551）自动释放并永删引导卷，不再靠人工 SSH 核查；抢到的型号写进任务日志（done 事件）与 cpu_models.json 缓存；guard 未配时任务日志与通知都明确写“CPU 未验明”（修了 cpu_filter_unavailable 事件不置 cpu_unverified 的缺口，Bot 与 Web 双端同修）；创建表单的“留空表示不筛选”提示已改成“留空默认只留 7J13”。Bot 端 E2 默认已是 7J13-only（TARGET_CPU），无需改
- 实例详情加“绑定域名”（Bot 与 Web 双端）：实例详情里加“🌐 绑定域名”入口，点开先选主域名（Cloudflare Zone，复用现有 /domain 与 cf_config 的凭据），再输二级域名（www/app 或 @ 表示根域名），建一条 A 记录指向实例公网 IP；建好后回显“完整域名 → IP”并提示 DNS 传播需要一点时间；无公网 IP 时 Web 按钮隐藏、Bot 提示无法绑定；二级域名只允许字母数字、-、_、. 或 @；同名 A 记录已存在时已同指则提示不用再建、不同指则确认覆盖后才更新；凭据未配时走现有 CF 凭据流程
- 全库注释清理：按用户定的注释规范，全库不必要的注释去掉（解释显而易见代码的、历史流水账、重复代码本身的），必须人工修改的地方才留注释且用中英文同时标注（配置项、需用户填的值、部署时要改的路径、复杂逻辑说明、安全警告、API 特殊行为）；共 187 个文件，删 1328 条、保留并补成双语 1527 条；测试文件同样处理
- 救机功能（Bot 与 Web 双端，菜单 44 / 救机页，ARM 实测结论已验证）：①安全恢复先建后删（备份→恢复盘→原生换盘→删旧盘，全程额度预检，不够先提示腾额度；旧盘是备份源盘时不自动删旧盘，留给用户手动处理）；②无盘救机按实测改成“从备份新建实例”向导（不做挂盘救回原实例的按钮：直接启动 409、从镜像建盘无此 API、恢复盘/克隆盘 attach 一律 409、无盘时原生换盘 400，五条路全灭；流程是选备份→恢复成盘→launch 新实例，复用原 SSH 密钥，并明示新 OCID、新公网 IP、旧实例无法复活；无备份时明确提示无法凭空恢复）；③孤儿盘检测清理按实测规则（无任何挂载记录 + 源自备份恢复，不能只看 AVAILABLE），标明占用额度，一键删除腾空间；④Netboot 救机向导（盘在、系统砖了：创建控制台连接、EFI Shell 命令、就近 TFTP 节点、netboot.xyz 菜单指引、Alpine 临时系统→DD 步骤，抢 ESC 与菜单方向键标为手动步骤）；⑤救机前置检查（开机/恢复/删盘/终止前自动检查实例状态、启动盘挂载、额度余量、备份源盘关系，给出能不能做、缺什么、风险是什么）；⑥控制台连接管理（一键创建/删除串行控制台连接，显示 SSH 连接命令；只接受 RSA 密钥，ed25519 会被 OCI 拒绝）；删盘前检查备份源盘关系，是源盘时拦截并说明后果。核心逻辑在 oci/rescue.py（Web 与 Bot 共用）与 oci/console.py，全部 mock 测试覆盖

**验证**：全量测试 2893 个全过（v1.45.0 基线 2691；2.01 新增 202 个：救机 82、Web 中英文 36、Bot 中英文 42、绑定域名 32、静态防缓存 1、自动升级去重 1、CPU 型号通知 3、CPU 筛选默认 5）。本版只做本地改动，未部署到服务器；部署后需真机验证（手机上切英文看整站文案、云报告中文状态、GitHub 链接手机/电脑端可见；救机功能需在真机上走一遍安全恢复与无盘救机流程）。

## [1.45.0] - 2026-11-25

注：本版原定 v1.44.0，发布前改号为 v1.45.0。
- 安装测试挖出的 5 个问题全修：①安装版 systemd 单元加 MBOT_WEB_ENV_FILE（面板与机器人读写 env 走安装版 /root/.mbot.env，不再写进 stray 的 ~/.rbot-clone.env，删账号 env_removed:false 修好）；②③多账号 compartment 缺口与 install.sh:422 占位符：安装时从 OCI 配置的 tenancy 推导 compartment，不再写 ocid1.tenancy.oc1..xxxxxxxx 占位符；面板/机器人侧占位符当未配置、从 tenancy 推导，干净安装+复制 oci_config 后未配 compartment 的账号也能显示；④cfdns 传播检查走公共解析器：lego 加 --dns.resolvers 1.1.1.1:53,8.8.8.8:53（不用本机 OCI 解析器 169.254.169.254 的 1800 秒负缓存），报错文案改准（不再误导说 Token 权限不对），失败时 lego 输出留一份日志；⑤更新模式 CERT_METHOD 也从 MBOT_INSTALL_CERT_METHOD 读，不再默认走 http
- 面板漏洞测试挖出的 3 项全修：⑥M-1 FastAPI 自动文档未授权暴露：create_app() 里 docs_url/redoc_url/openapi_url 全设 None（/docs、/redoc、/openapi.json 均 404）；⑦L-2 会话 Cookie 缺 Secure：登录会话 cookie 按请求协议决定 Secure（nginx HTTPS 时 Secure，http 直连与测试时不设，否则回环登录收不到 cookie）；⑧L-4 422 校验错误泄露服务器路径：校验错误处理器只返回字段级错误，不含文件路径与行号
- ⑨Ubuntu nft 空表不回落 UFW：install.sh 防火墙处理检测到空的 filter 表（无 input 链/规则）时回落启用 UFW，已有规则的表只警告；另补 ip6 filter
- ⑩两处 HTML 终止确认框实例名未转义：单个终止（tgbot/handlers.py）与批量终止（tgbot/handlers_extra.py）的实例名经 html.escape 转义
- ⑪nginx 安全加固同步进仓库（v1.43.0 已在本地改好）：安全响应头（HSTS/X-Frame-Options 等）、扫描器 UA 拦截、Cloudflare 真实 IP 三组变量进三个 HTTPS vhost 模板
- ⑫网页版加 GitHub 项目链接（https://github.com/jimu2000/M_Bot）：登录页登录框下方脚注区加“GitHub 项目”文字链接，内页顶栏加 GitHub 图标（Feather 描边，与其他图标同风格），均新标签页打开、低调不抢眼
- ⑬云报告改表格：概览页“云报告”卡片原来每区域一段密集文字，手机上看着乱；改成 HTML 表格（区域/实例/启动盘/A1/E2 五列：区域含中文名与 region 代码，实例分 RUNNING/TERMINATED，A1 含核与内存的已用/总量），每区域一行，查询失败的账号也占一行；表格包在 .table-wrap 里，小屏横向滚动、不溢出

**验证**：全量测试 2691 个全过（新增 16 个测试方法：安装脚本 5 个、compartment 配置 4 个、面板安全 3 个、HTML 转义 2 个、GitHub 链接 1 个、云报告表格 1 个；另有 5 个既有测试因 compartment 改动打 PROFILES 补丁，使其不依赖本机 oci_config）。本版只做本地+staging，未部署到服务器；部署后需真机验证。
- 安装脚本的 nginx 仓库从 stable 分支换成 mainline 分支：新装机器直接装上 nginx 最新版（如 1.31.10，与现有 4 台一致），不再停在 stable 的 1.30.x；仓库密钥与 signed-by 不变（mainline 与 stable 共用同一把签名密钥），加不上仓库时仍回落发行版版本

**验证**：全量测试 2672 个全过（test_install_script.py 新增 mainline 路径断言）。真机验证：一台测试机已按同一办法从 stable 1.30.8 切到 mainline 1.31.10，nginx -t 通过、面板登录页 200。

## [1.42.3] - 2026-11-25
- 纠正 v1.42.2 更新说明里的错误说法：iOS 的验证码自动填充不是只扫短信和邮件，它能从 Bark 推送通知里抓码（2026-11-24 晚真机实测，键盘显示"来自 Bark"+ 6 位码，点一下就填进去）；v1.25.0"从通知中心抓码自动填充，已实测"的说法是对的，v1.42.2 里说它是误判是错的。相关代码对比已核实：v1.25.0 与现在的登录页输入框、登录页 JS、Bark 推送标题正文都一字没动，早上不弹是 iOS 这边时灵时不灵（码到时键盘已开着、建议条不刷新），不是代码坏了；同改 auth.py 注释里的同一句错误说法

**验证**：仅文档与注释改动，无逻辑改动；全量测试 2672 个全过。

## [1.42.2] - 2026-11-25
- Bark 登录码通知点一下直接登录：send_bark 新增 url 参数（传了就放进加密前的 payload，不传时 payload 里不含 url 键，现有调用行为不变）；登录码 Bark 推送带上与正文相同的一键登录链接，点通知直接打开面板登录，不用再手动输 6 位码
- iOS 的验证码自动填充能从 Bark 通知里抓码（2026-11-24 晚实测，键盘显示"来自 Bark"+ 6 位码，点一下就填进去），但时灵时不灵；点通知直接登录是最稳的零输入路径（注：v1.42.2 发布时这条曾错写成"iOS 只扫短信和邮件"，于 v1.42.3 纠正）
- 注：1.42.1 未发布（原预留给 Markdown 启动确认回归修复，审计结论是代码无漏点、无需修复，故跳过该版本号）

**验证**：全量测试 2672 个全过（新增 4 个：tests/test_bark.py 2 个，传 url 时 payload 含 url、不传时不含 url 键；tests/test_phase5e_auth.py 2 个，登录码 Bark 的 url 就是 magic 登录链接、正文与 url 是同一条链接）。真机验证：部署后在手机上点一次 Bark 登录通知，确认直接打开面板并登录。

## [1.42.0] - 2026-11-25
- CPU 规则收紧为只留 7J13（用户定：7551 与 7745 都抛弃重抢）：tgbot/cpu_policy.py 的默认允许列表改成 ["7J13"]，确认页按钮与目标 CPU 文案随规则动态显示（"只抢 7J13"）；已有规则文件（tgbot/data/cpu_policy.json）里的列表仍优先于默认值，部署时需同步改掉
- 抢机任务持久化 + 启动恢复（更新版本不再丢正在抢机的任务）：抢机启动时把完整规格（账号/区域/compartment/全部 Grabber 参数/通知信息/已抢列表）写进共享任务记录的 grab_spec，每抢到一台更新一次；服务重启后本来源带规格的抢机任务自动恢复重跑（剩余台数 = 总数 - 已抢到），已抢满的标记完成并补发成功通知，无规格或非本来源的任务不恢复；token 从环境变量现读，绝不落盘；任务文件每次写盘都 chmod 0600（与 bark.json 同一先例），API 返回的任务视图剥掉 grab_spec
- Grabber 的 user_data 临时文件被外部删掉后能重建（之前只建一次，文件被误删后每次 launch 都因文件不存在失败）
- 通知可观测性：抢机成功通知无论成败都在任务日志里留一行结果（"通知：Telegram 已发送，Bark 已发送" / "Bark 未配置，已跳过"；部分失败沿用原有文案），机器人端同时记一条日志（进 journal），推送丢了有据可查
- 实例页 CPU 型号不再显示陈旧空值：实例列表请求不再走前端 15 分钟缓存（每次进页重新拉，后端 30 秒服务端缓存已足够保护）；渲染后若有实例的 CPU 型号为空，5 秒后带 refresh=1 重新拉一次列表，只更新卡片上的 CPU 型号文字
- 实例页启动盘弹窗加"恢复启动卷"按钮（在"备份启动卷"旁、"取消"之前），复用实例详情页已有的引导卷恢复流程与后端

**验证**：全量测试 2668 个全过（新增 17 个：tests/test_v1420_grab_resume.py，覆盖规格写盘、恢复重跑台数、已满标记完成、0600 权限、无规格/异源不恢复、user_data 重建、通知日志三种情况；另更新 CPU 规则相关的默认断言与文案断言）。真机验证：前端 CPU 补拉与恢复按钮需在浏览器里验证；部署后重启机器人验证抢机恢复。

## [1.41.1] - 2026-11-24
- 孤儿盘接口不再单独跑一遍查询：复用启动盘列表缓存再过滤（孤儿 = AVAILABLE 且未挂载，与 oci.volume.list_orphan_boot_volumes 的判断一致），冷加载约 5.0 秒 → 约 3.4 秒；启动盘页冷加载总计约 13.5 秒 → 约 5.1 秒（与 v1.41.0 更新说明一致）

**验证**：全量测试 2651 个全过（tests/test_phase5e_shapes.py 的孤儿盘缓存测试改成 2 个：复用列表缓存、refresh 绕过；tests/test_web_api.py 的 test_orphans 随新逻辑改 mock）。真机验证：部署后复测 /api/volumes/orphans 冷加载耗时。

## [1.41.0] - 2026-11-24
- CPU 筛选规则写进配置并真正接线（以前\"只抢 7J13\"是摆设，hook 从未注入）：新增 tgbot/cpu_policy.py，规则放 tgbot/data/cpu_policy.json（0600），默认只留 7J13/7745；抢机时把 guard 公钥拼进启动 SSH keys（新增 combined_boot_keys），Grabber 注入 cpu_check_hook，抢到先 SSH 验 CPU，不符就释放（永删引导卷）继续抢；CPU 查不到时暂留实例并标\"未验明\"，不释放未验明的机器；guard 密钥未配置时抢到即留并在详情里写明；机器人与网页抢机同步改，按钮文案随规则动态显示
- Grabber 的 target_cpu 支持列表（多型号任一匹配即留），hook 返回 None 或抛异常时按未验明暂留
- 机器人与网页的任务互通：两边共用 web/data/tasks.json（新增 tgbot/task_store.py，文件锁保护读-改-写，原子替换写盘），机器人里能看到网页的抢机任务，网页上能看到机器人的；任一端取消对方的任务时写 cancel_requested，另一端的监听线程置 stop_event 停下抢机；任务 ID 带来源前缀（bot-g1 / web-g1），旧版无前缀 ID 仍兼容；服务启动时只恢复本来源未结束的任务
- E2.1.Micro 规格显示加\"永久免费\"（机器人详情/预设与网页实例卡片/详情弹窗同步）
- 网页加载优化：启动盘列表与孤儿盘列表加 30 秒服务端缓存（与实例列表同款，refresh=1 绕过，扩容/改 VPU/删除/终止/恢复后自动清缓存）；挂载关系与实例名两个查询并行跑；实例页的实例与启动盘两个请求并行发，启动盘页的卷/孤儿盘、备份/配额各并行发；实测冷加载：实例页约 9.8 秒 → 约 4.7 秒，启动盘页约 13.5 秒 → 约 5.2 秒

**验证**：全量测试 2650 个全过（新增 74 个：tests/test_cpu_policy.py 24 个、tests/test_task_store.py 27 个、tests/test_web_grab_cpu.py 4 个，其余在 test_grabber.py、test_notify.py、test_v1220_cpu_tasks.py、test_tgbot_instance_list.py、test_web_api.py、test_phase5e_shapes.py；另修 3 处测试抢跑：POST 后立即读 Grabber call_args 的测试加等待、Grabber patch 提前退出的测试把等待留在 patch 里）。真机验证：.160 上 /api/instances 与 /api/volumes 各接口耗时实测后定方案；部署后复测冷加载耗时。

## [1.40.1] - 2026-11-24
- 修复点了"开始抢机"后收不到"抢机已启动"确认的问题：有 root 密码时启动回复整条走 Markdown 解析，实例名里的下划线（AMD_2026-11-25）被当成斜体标记、Telegram 拒绝发送（Can't parse entities）；动态的实例名现在先经 escape_markdown 转义再拼进消息，任务列表里的名字保持原样不转义

**验证**：全量测试 2576 个全过（新增 1 个回归测试 tests/test_boot_form_handlers.py 的 test_start_reply_escapes_underscore_in_name）。

## [1.40.0] - 2026-11-24
- 快捷开机（菜单 17）新增编辑与删除：每条配置一行三个按钮，🚀 开机 / ✏️ 编辑 / 🗑️ 删除；编辑把配置反向填进开机表单（新增 tgbot/boot_form.py 的 form_from_preset，旧版预设缺 cpu/os 时按形状与镜像反推），保存后原地更新、名字保持原样；删除先确认再删，删完刷新列表并显示已删除提示
- 修复抢机成功后通知里 IP 一直是"获取中"：原来只重试 3 次、每次隔 3 秒（不到 10 秒就放弃），OCI 分配公网 IP 经常要 30 秒以上；改成先等 60 秒（新增 oci/network.py 的 wait_for_public_ip），60 秒还没 IP 就先发通知、后台跟进线程继续等（新增 tgbot/notify.py 的 start_ip_followup，最长 10 分钟），IP 一出来补发一条 Telegram 消息；机器人与网页抢机同步改
- 修复新开的 Ubuntu 实例无法 root 登录：原来 user-data 只有改密码 + ssh_pwauth，但 Ubuntu 镜像默认 disable_root: true（root 的 key 被加强制命令，只能看到 "Please login as the user ubuntu"）且 PermitRootLogin 默认禁密码；build_user_data 现在加 disable_root: false + 写 sshd drop-in（PermitRootLogin yes）并 reload sshd（ssh/sshd 两个服务名都试）；机器人与网页共用同一个函数，一次改好

**验证**：全量测试 2575 个全过（新增 21 个：tests/test_network.py 4 个、tests/test_notify.py 3 个、tests/test_web_grab_notify.py 2 个、tests/test_boot_form.py 4 个、tests/test_boot_form_handlers.py 8 个；另有 12 个旧测试随新逻辑改 mock）。真机验证：.160 上 wait_for_public_ip 对已有 IP 的实例立即返回；新开的 Ubuntu 实例上手动验证 root 登录修复有效（清强制命令 + PermitRootLogin yes 后 ssh root 可登）；生成的 user-data 经 YAML 解析校验合法。

## [1.39.0] - 2026-11-24
- 新增定时自动升级：安装时自动设置 cron（每天凌晨 4 点服务器本地时间），跑 `install.sh auto-upgrade`；先比对本地与远端版本，已是最新版时只记日志不重启服务，有新版时才拉代码、装依赖、重启在跑或已设自启的服务并做健康检查
- 自动升级失败时发 Bark 通知（标题"M_Bot 自动升级失败"，含主机名、原因与日志路径；Bark 未配置或未开启时静默跳过，通知异常绝不影响升级流程）
- cron 用临时副本执行 install.sh，避免 git pull 更新 install.sh 本身时正在运行的脚本被改写；`MBOT_INSTALL_NO_AUTO_UPGRADE` 非空时不设 cron
- 新增子命令 `install.sh auto-upgrade`（手动跑一次检查+升级）与 `install.sh setup-auto-upgrade`（已有安装单独启用 cron，不动服务）；卸载时删掉自动升级 cron 与日志
- 修复 health_check 把已停用的机器人判为失败：机器人单元存在但已禁用+未运行时只报 info 跳过，不再 warn 并判失败（否则故意停掉机器人的机器每次自动升级都会误报失败）

**验证**：全量测试 2554 个全过（新增 tests/test_install_script.py 9 个）。真机验证：.101 上手动跑 auto-upgrade 报告"已是最新版"且服务未重启；.129 从 v1.31.0 经 upgrade 升到 v1.39.0 后启用 cron。

## [1.38.1] - 2026-11-24
- 修复删除账号后配置文件多出一个空行：remove_oci_profile 删掉末尾段后无条件补了一个 `\n`，而添加时根本不加分隔空行，导致删段后文件与原来不再逐字节一致；改成只删段内行、其余内容原样保留
- 修复升级时拉起被特意停掉的服务：`install.sh upgrade` 对所有已装服务无条件 restart，把停掉并禁用的机器人也拉起来了；改成只重启在跑或已设自启的服务

**验证**：全量测试 2545 个全过（新增 2 个：tests/test_web_oci_profiles.py 1 个，覆盖添加再删除后文件逐字节一致；tests/test_install_script.py 1 个，覆盖升级只重启在跑或已设自启的服务）。.160 真机验证：verify_v138.py 19/19 全过（假账号 FAKEDEL01 添加后删除，配置与 env 均逐字节还原）。

## [1.38.0] - 2026-11-24
- 云账户页已配置账号可删除：每行新增删除按钮（点按前二次确认），删除配置段 + compartment 环境变量；私钥文件只有在没有其他账号共用时才一并删除
- 删除保护：DEFAULT 是默认账号不能删；至少保留一个账号；面板列表删除后立即更新，机器人需重启后生效
- 新增接口 DELETE /api/profiles/{profile}；新增 tgbot/handlers_client.py 的 remove_oci_profile（文本级删段，其余内容逐字节保留）与 tgbot/config.py 的 remove_env_file_vars

**验证**：全量测试 2543 个全过（新增 tests/test_web_oci_profiles.py 7 个，覆盖删除成功与配置/env/私钥的清理、共用私钥保留、删 DEFAULT/删最后一个/未知账号/未登录的拒绝、删段不影响其他段与 [cloudflare] 段）。.160 真机验证：新建假账号 FAKEDEL01 后经接口删除，配置段、env 变量、私钥文件均已清理，三个真实账号逐项核对无变化。

## [1.37.2] - 2026-11-24
- 云账户页已配置账号可直接补传私钥：已配置账号列表新增私钥状态列（已就位/缺失，需上传）与上传私钥控件，文件写到该账号配置里的 key_file 路径（600 权限），多账号共用同一私钥时传一次就全好了；私钥已就位时可替换（替换前会确认）
- 新增接口 POST /api/profiles/{profile}/key：内容必须是私钥文件（超 16KB 拒绝），路径必须在 home 下；GET /api/profiles 列表新增 key_file 与 key_ready 字段

**验证**：全量测试 2536 个全过（新增 tests/test_web_oci_profiles.py 8 个，覆盖列表私钥状态、补传成功与 600 权限、非私钥/超大/越出 home/未知账号/未登录的拒绝）。.101 真机验证：面板重启后云账户页三账号均显示私钥缺失，补传接口实测可用，私钥就位后云报告恢复正常。

## [1.37.1] - 2026-11-24
- 修复重跑一键安装误删机器人服务：更新模式下判断 Token 的 grep 用了 BRE 不支持的 `.+`，永远匹配不到，导致每次重跑都判定“没 Token”而停掉并删除机器人单元；改成 BRE 能用的 `.`，且 env 文件丢失但机器人单元还在时保留机器人
- 修复重跑安装后面板仍跑旧代码：写 systemd 单元后用 `systemctl enable --now`，对已在跑的服务不重启，旧进程一直加载旧代码（/accounts 404 就是这么来的）；改成 enable + restart，更新后服务真正重启

**验证**：全量测试 2528 个全过（新增 tests/test_install_script.py 3 个，覆盖 Token 判断正则的 BRE 安全、单元写入用 restart、env 丢失时保留机器人）。.101 真机验证：重跑修复版一键安装，机器人单元恢复，面板进程重启后 /accounts 正常、证书页正确读出 certbot 证书。

## [1.37.0] - 2026-11-24
- 证书管理页现在能读 install.sh 装的证书：状态接口按 acme.sh → certbot → lego → nginx（ssl_certificate 指向的文件）的顺序找证书，响应里新增 source 字段标明来源；nginx 来源的域名从路径或证书 CN 推导。续期仍只走 acme.sh，certbot/lego 来源的证书页面会提示由其自动续期、面板不做续期
- acme.sh 扫描跳过 ca.cer（中间证书），只读叶子证书；ECC 证书目录 `<domain>_ecc` 的域名不再带 _ecc 后缀显示，`?domain=` 也能找到 _ecc 目录里的证书
- 一键安装保证最新版：install.sh 新增共用函数 update_code，安装（已有代码时）与 upgrade 子命令都走它——先 git pull，快进失败时改用 fetch + reset 强制同步到最新版，两条路都失败直接停住报错，绝不再静默用旧代码继续装；装完与升完都打印版本号
- 一键安装全程无交互：nginx 仓库密钥写入加 `--batch --yes`，密钥环文件已存在时直接覆盖，不再弹 Overwrite 提问把安装挂起（之前重跑安装会在这一步停住，代码根本没更新到）
- README 的 Token 教程补一条警告：同一个 Token 只在一台机器上跑一个机器人，两台机器共用 Token 会互相抢消息

**验证**：全量测试 2525 个全过（新增 tests/test_phase5e_cert.py 9 个，覆盖 certbot/lego/nginx 回退、域名参数、来源优先级、续期仍只走 acme.sh、ECC 目录跳过中间证书；tests/test_install_script.py 4 个，覆盖 update_code 回退与报错、安装与升级共用、装完打印版本、密钥环写入非交互）。.160 真机验证：证书状态来源仍为 acme.sh，域名去掉 _ecc 后缀、有效期改显示叶子证书（2027-02-21）；.101 上关闭标准输入完整重跑一键安装，全程无提问、结束时版本为 v1.37.0，证书页正确读出 certbot 证书。

## [1.36.0] - 2026-11-24
- 网页版新增云账户页（/accounts）：添加租户与机器人 /uploadapi 同步——粘贴 config 文本或上传 config 文件自动解析（支持 R-Bot 格式、一次多个账号），已存在的账号自动过滤跳过；私钥文件（.pem）在确认步骤直接上传（内容校验必须是私钥、600 权限保存、路径强制在 home 下），不经过 Telegram；配置里的 [cloudflare] 段一并保存；compartment 环境变量自动补写；新账号重启机器人与面板后生效
- 解析→确认两步流程：解析返回 pending_id（10 分钟有效、一次性），确认时才写配置；响应里不回显 OCID、指纹与 Cloudflare 凭据值
- README 教程补充：找不到自己的机器人时可直接开 t.me/<用户名>

**验证**：全量测试 2512 个全过（新增 tests/test_web_oci_profiles.py 21 个，覆盖解析/过滤/R-Bot 格式/云凭据不回显/pem 上传与校验/路径防穿越/一次性确认/未登录拒绝）。.160 真机验证 13/13 通过：/accounts 页、解析、提交假账号（配置 + 600 权限 pem + env 变量均写入）后完整还原，配置与 env 字节一致。

## [1.35.5] - 2026-11-24

### 文档
- README 补上 OCI API 密钥详细教程（中英双语）：config 文本与私钥分两条路（config 发给自己的机器人私聊 /uploadapi，私钥 scp 到服务器、不走 Telegram）；密钥对生成、公钥上传控制台、tenancy/user OCID 位置、config 格式（key_file 任意 /root 下路径均可，`oci setup config` 默认的 `~/.oci/oci_api_key.pem` 不用挪）、多账号与 R-Bot 格式、怎么找到自己的机器人、旧 `~/.oci/config` 只作回退、验证方法；install.sh 头注释同步

### 验证
- 全量测试 2491 个全过（仅文档与注释改动，无代码行为变化）

## [1.35.4] - 2026-11-24

### 文档
- README 补上 Telegram 机器人 Token 详细教程（中英双语）：Token 与机器人 ID、用户 ID 的区别；新建机器人（/newbot）与已有机器人找回 Token（/token）两条路；Token 泄露后在 @BotFather 里 Revoke 重生；install.sh 头注释同步

### 验证
- 全量测试 2491 个全过（仅文档与注释改动，无代码行为变化）

## [1.35.3] - 2026-11-24

### 变更
- 登录页统计代码换回 Microsoft Clarity（MBOT_CLARITY_ID 环境变量配置，未配置不加载；Clicky 代码与 MBOT_CLICKY_ID 移除）

### 验证
- 全量测试 2491 个全过（2 个登录页统计测试随之更换：Clarity 加载/未配置两种情况）
- 真机验证：部署到 .160 后重启面板，登录页 HTML 含 Clarity 脚本、无 Clicky

## [1.35.2] - 2026-11-24

### 新增
- 备份创建完成发 Bark 通知（用户要求 2026-11-24：创建和恢复都要 Bark 通知；之前备份创建只回 Telegram“创建中”，完成后没有任何通知）：新模块 `tgbot/backup_watch.py`——创建成功后起 daemon 线程轮询备份状态（`oci/boot_backup.py` 新增 `wait_for_backup_available`，30 秒一查，最长 30 分钟），到 AVAILABLE 后用 notify_dual 发 Telegram + Bark 双通道通知“💾 备份已完成”；失败（FAULTY 等终态）发“备份失败”，超时发“备份超时”，轮询异常发“备份状态未知”。机器人端三处创建点（菜单 7、实例详情“引导卷备份”、满额删最旧再建）与网页端创建接口（POST /api/instances/{id}/boot-backups）共用；token/chat_id/备份 id 缺失时不启动线程。恢复的通知本来就有（notify_dual 双通道），本次实测 Bark 通道正常（测试推送成功）

### 验证
- 全量测试 2491 个全过（v1.35.2 新增 13 个：轮询辅助 5 个——立即可用、CREATING/REQUEST_RECEIVED 后可用、FAULTY 终态、超时；通知线程 8 个——成功/失败/超时/异常四种通知文案、缺 token/chat_id/备份 id 不启动、daemon 线程启动；另有 8 个既有创建测试改成 mock watcher 并断言 watcher 被调用）
- 真机验证：部署到 .160 后重启机器人与面板，服务正常、日志无报错；Bark 通道已用生产代码实测推送成功

## [1.35.1] - 2026-11-24

### 变更
- “没有可用备份”提示改清楚（用户要求 2026-11-24：删旧盘后机器人说实例没可用备份，用户以为备份被删了）：原地恢复只列当前启动盘的备份，换盘前旧盘的备份不列出来，容易误会。机器人端：当前盘没可用备份时，顺手查一下账号里其他盘的可用备份，有就附上“注：账号里还有 N 个其他启动盘的可用备份（比如换盘前旧盘的备份），删盘不会删备份；原地恢复只列当前启动盘的备份，旧备份可从备份页恢复成新实例”；查不到也不影响主提示。网页端同步：GET /api/instances/{id}/boot-backups 在当前盘无备份时返回 other_available_count，恢复弹窗用同一句提示

### 验证
- v1.35.1 新增 4 个测试：机器人端有旧备份时提示含注、查旧备份失败时主提示不受影响；网页端空备份返回 other_available_count、查失败时为 0

## [1.35.0] - 2026-11-24

### 变更
- 引导卷恢复成功后自动删除旧启动盘，不留旧盘占额度（用户要求 2026-11-24）：`swap_boot_volume` 在换盘成功、实例回到换盘前状态后，自动删除换下来的旧盘（OCI 拒绝删除仍在用的盘，万一旧盘还挂着只会报错，不会误删）；删除失败不影响恢复结果，返回里标记 `old_volume_deleted=False`，机器人与网页端的成功消息里警告用户手动删除。机器人端选择页、确认页（流程加第 ⑤ 步“删除旧启动盘”）与网页端实例页提示文案同步更新；成功消息的旧盘行改成“已删除，额度已释放”/“自动删除失败，请手动删除”

### 验证
- 全量测试 2474 个全过（新增 3 个：换盘成功路径断言删旧盘命令（删旧盘不删新盘、带 --force、在换盘之后）与 `old_volume_deleted=True`、删盘失败恢复仍成功且标记 False、机器人与网页端删盘失败警告文案；另有 4 个既有测试随行为更新：成功路径进度步数 2→3、机器人与网页端成功消息断言改成“已删除，额度已释放”）
- 真机验证：部署到 .160 后重启机器人与面板，服务正常、日志无报错；真实换盘验证需用户下次恢复时实测（旧盘应自动删除，额度同步释放）

## [1.34.0] - 2026-11-24

### 新增
- 每聊天只留一个活动选择框（用户要求 2026-11-24：聊天里不要堆一堆旧菜单，点完删掉多余的选择框）：新模块 `tgbot/menu_tracker.py` 统一管理——带 inline 键盘的消息是选择框，发新选择框时先删掉旧的；不带键盘的结果消息发出时，选择框已完成使命，一并删掉，只留结果；点按钮后先发新消息再删被点的那条（回复要先发，删了就没法回复它）；同一回调里多次回复时，只有第一次走回复，之后改走 `bot.send_message`（被点的消息已删，回复它会报 Bad Request）；删除失败（消息已删 / 超 48 小时 / 无权限）只记日志，不影响主流程
- 三个 handler 模块的 `_reply`（共 511 处调用点）统一改成 `menu_tracker.reply` 的薄包装（补一个 `context` 参数）；`/start`、`/oracle`、`/instances` 三个命令入口也走同一套跟踪；`x` 取消回调把消息编辑成“已取消”后清空跟踪；提示清理时同步清掉已删提示的选择框跟踪

### 变更
- 开机表单“取消自定义输入”：以前只编辑提示说“表单未变”（旧表单还在）；现在提示发出时旧表单已被选择框清理删掉，取消改成重发表单继续填（与输入完成后的做法一致），提示消息由 `_reply` 的清理删掉

### 验证
- 全量测试 2471 个全过（新增 `tests/test_menu_tracker.py` 10 个：回调发选择框删被点消息并跟踪新的、删之前跟踪的旧选择框、发结果删选择框并清空跟踪、同一回调第二次回复走 `send_message`、命令发选择框/结果的跟踪、删除失败不影响回复、无消息无 chat 返回 None、forget/delete_live_menu 的 except 行为；另有 16 个既有测试随行为更新：双回复流程改查合并通道、表单取消改断言重发表单、监控开关第二次点按用新回调模拟、`_reply` 直接调用补 context 参数；11 个测试文件的 `make_context` 脚手架补齐 `bot.send_message`/`bot.delete_message` 两个 AsyncMock）
- 真机验证：部署到 .160 后重启机器人，服务正常、日志无报错；点按清理效果需用户在手机上实测确认（实例详情 → 引导卷恢复 → 备份列表，聊天里应只剩当前这一个选择框）

## [1.33.0] - 2026-11-24

### 变更
- 登录页统计从 Microsoft Clarity 换成 Clicky（用户要求 2026-11-24）：配置了 `MBOT_CLICKY_ID` 时加载 Clicky 跟踪脚本（`static.getclicky.com/js`，带站点 ID）与页脚徽章链接；未配置时不加载，公开仓库不硬编码任何站点 ID；Clarity 代码与 `MBOT_CLARITY_ID` 已全部移除（仍只加登录页，未扩到全站）

### 验证
- 全量测试 2461 个全过（登录页统计两项测试改成 Clicky：已配置时脚本 + 徽章在位且无 Clarity，未配置时两者都不加载）
- 真机验证：部署到 .160 后登录页 HTML 含 Clicky 脚本与徽章、无 Clarity；浏览器端到端未能完成——Clicky 的静态域名在 Cloudflare 后面，对机房 IP 返回 403 拦截页（我方所有出口都是 Oracle 机房 IP），真实访客（住宅/手机 IP）不受影响，用户可在手机浏览器打开登录页后在 Clicky 后台看到访问记录

## [1.32.0] - 2026-11-24

### 新增
- 域名自动绑定：有域名时发证前先校验解析，没解析到本机且有 Cloudflare 凭据就自动建/改 A 记录指到本机（DNS-only，不开代理——代理会挡 http-01 验证），再等解析生效（最多 60 秒）；凭据优先级：环境变量 > `~/oci_config` 的 `[cloudflare]` 段（`load_cf_credentials`，与 cfdns 共用）；没凭据或绑定失败时只警告，发证失败会回落自签，安装不中断
- 安装后自检：首次安装跑完健康检查后自动跑四项验证并打印结果——证书（Let's Encrypt 颁发者 + 有效期内 + 域名匹配，自签时只验握手）、登录页（公网地址返回 200，LE 证书严格校验链）、真实登录（admin + 安装密码调登录接口，要 200 且拿到会话 cookie）、http 跳转（域名 + 证书成功时 http 应 301 跳 https）；http-01 发证成功时附注外网 80 端口已证明可达

### 验证
- 全量测试 2461 个全过（新增 4 个：CF 凭据加载 1 个、域名自动绑定 1 个、安装后自检 1 个、安装流程调用自检 1 个）
- 真机验证：.129 上删 DNS 记录 + 卸载 + 重装，自动绑定与自检全程实测通过（见下方 v1.32.0 安装验证记录）

## [1.31.0] - 2026-11-24

### 新增
- 安装脚本自动识别本机防火墙类型：ufw / firewalld / nftables / iptables，哪个在跑就用哪个放行 SSH + 80 + 443 + 9527；都没在跑时优先启用 ufw，否则跳过（端口默认就是通的）；nft/iptables 规则插到链首（drop 前面），并提醒重启会丢、建议改用 ufw 或 firewalld 管理
- OCI 实例安装时经 API 自动放行云端安全列表：读实例元数据确认是 OCI 后，用 OCI CLI 给主网卡所在子网的安全列表（挂了 NSG 时改加 NSG 规则）补 80/443/9527 的入站规则；认证优先用已有的 OCI 配置文件，没有时试实例主体；已放行（包括全协议规则）时跳过不重复加；任何一步失败都只警告 + 打印手动操作步骤，安装不中断
- 机器人 Token 可留空：安装时不填 Token 则只装面板，不建机器人服务（面板登录、实例管理都能正常用，只是没有 Telegram 登录码与机器人菜单）；以后想加机器人，在环境文件里填好 Token 与用户 ID 后重跑安装即可；管理命令（start/stop/restart/status/log/upgrade）只操作实际存在的服务
- 9527 被别的服务占了时不再硬挡：防火墙与云端安全列表都不放行它（反代绑不上，放行只会把别人的服务暴露出去）；setup_nginx 里有域名且 443 空闲时只警告、面板走 443，否则才报错退出（.129 实测：R-Bot 客户端占着 9527）

### 修复
- certbot 域名证书路径改用 `certonly --webroot`：之前用 `certbot --nginx`（改 nginx 配置 + 重载），443 被 xray 占了时重载失败、证发了也白发；现在 certbot 只发证，443 的 vhost 由安装脚本自己写，443 被占时面板改走 9527（与 cfdns 路径一致）；续期挂 deploy hook（续了新证后重载 nginx），80 端口保留验证入口供续期用

### 验证
- 全量测试 2457 个全过（新增 7 个：防火墙识别 1 个、防火墙后端 1 个、OCI 云端放行 1 个、规则补齐逻辑 1 个、Token 可选 1 个、certbot webroot 回落 1 个、9527 被占用时跳过 1 个；另有 5 个旧测试随改名更新）
- 规则补齐逻辑单独跑了 6 个用例全过（全协议规则视为已放行、TCP 端口范围覆盖、NSG 增量规则带 direction、EGRESS 规则不算放行、空规则全补）
- 真机验证：.101 上跑完整安装（域名 + Let's Encrypt 证书），见下方 v1.31.0 安装验证记录

## [1.30.0] - 2026-11-24

### 新增
- OCI 配置文件可放 Cloudflare 凭据：`~/oci_config`（即 `/root/oci_config`）里加 `[cloudflare]` 段（`cf_token`，或 `cf_key`/`cf_account_key` + `cf_email`），机器人 `/domain` 与面板 DNS 页会用它；凭据优先级：机器人/面板里保存的 > OCI 配置 `[cloudflare]` 段 > 环境变量；凭据管理页来源文案同步显示
- `/uploadapi` 与 `/oci` 导入时保留 `[cloudflare]` 段：R-Bot 格式配置里如有该段，确认后一并追加到配置文件（已有该段时不覆盖；只有该段、没有新 profile 时也可单独确认保存；凭据值绝不在聊天里回显）
- 安装时 cfdns 凭据先读 OCI 配置：`cfdns` 验证方式下，凭据优先级是环境变量 > `~/oci_config` 的 `[cloudflare]` 段 > 交互输入；Key + 邮箱组合也会写进 `/etc/lego/env`（之前只支持 Token）
- nginx 从 nginx.org 官方仓库装最新版：安装脚本加官方仓库（stable 分支，仓库 URL 按系统自动区分 debian/ubuntu），不再用发行版自带的旧版；密钥放专用 keyring + `signed-by`（只信任 nginx.org 的包），apt pinning 900 保证以后升级也走官方仓库；仓库加不上时回落发行版版本，安装不中断；装完把实际版本号打出来
- 安装 nginx 时保留已有配置：`apt-get install` 加 `--force-confold`，机器上已有改过的 nginx 配置时不覆盖（新装时没影响）

### 验证
- 全量测试 2450 个全过（新增 29 个：OCI 配置 CF 来源 9 个、[cloudflare] 段解析与保存 12 个、install.sh 静态测试 4 个、DNS 凭据接口 2 个、凭据管理页文案 2 个）
- 真机验证（.160）：部署后机器人 `/domain` 正常（凭据仍走环境变量，来源显示正确）；OCI 配置 CF 来源与段保存走生产代码路径验证通过
- nginx 官方仓库与 IP 证书真实签发留待用户在 .101 上安装测试验证

## [1.29.0] - 2026-11-24

### 新增
- 无域名自动申请 Let's Encrypt IP 证书：安装时不填域名，改用服务器公网 IP 申请证书（lego v5.7.0，`shortlived`，有效期 160 小时约 6 天），面板 `https://IP:9527` 不再报证书警告；验证走 http-01（nginx 80 端口 serve webroot），申请失败时回落自签证书，安装不中断
- IP 证书自动续期：续期脚本 + cron 每 2 天凌晨 3 点跑一次（lego v5 里 `run` 兼续期，短效证书默认剩一半寿命时续；deploy hook 在证书更新后重载 nginx）；80 端口验证 vhost 保留，续期不用停 nginx
- 有域名新增 `cfdns` 验证方式：安装时选 `http`（80 端口验证，默认，certbot，老路不变）或 `cfdns`（Cloudflare DNS-01，lego，需 CF API Token，Zone:DNS:Edit + Zone:Zone:Read 权限；验证不占端口；Token 只存 `/etc/lego/env`，600 权限，续期也用它）
- 域名面板 443 被占时改走 9527：cfdns 发证后若 443 已被 xray 之类服务占了，面板改监听 9527（证书照样有效），安装完成提示给带端口的地址，不误导

### 修复
- 健康检查不再只查端口在听：443 可能被 xray 占着，只查端口会误报正常；现查 nginx 进程占着 9527 或 443 才算反代起来了（此前域名部署下健康检查还会对 9527 误报）
- UFW 改成始终放行 80 端口：IP 证书 http-01 验证（申请与续期）都要用 80，不能只在有域名时放行

### 验证
- 全量测试 2421 个全过（install.sh 静态测试新增 8 个：lego 版本与架构、IP 证书 shortlived/webroot、cfdns 提问与 Token、续期自动化、IP nginx 配置、UFW 放行 80、443 回落、健康检查）
- lego v5.7.0 真实二进制已核实：`--profile`、`--http.webroot`、`--dns cloudflare`、`--deploy-hook` 均在；不带 `--email` 也能在 LE staging 注册账号（lego 用 `noemail@example.com` 占位）；IP 证书真实签发留待用户在 .101 上安装测试验证

## [1.28.1] - 2026-11-24

### 修复
- 文档里的 OCI 配置路径跟上 v1.28.0：安装脚本（头部说明与安装完成提示）与 README（中英两节的准备清单与手动安装说明）里的 `~/.oci/` 改成 `/root/` 下（配置 `~/oci_config`，私钥 `/root/xxx.pem`）；`oci setup config` 生成的旧路径 `~/.oci/config` 仍可读、首次用 `/uploadapi` 导入时自动迁移，现已在文档里写明
- 认证失败提示里的配置路径同步改成 `~/oci_config`

### 验证
- 全量测试 2413 个全过

## [1.28.0] - 2026-11-24

### 新增
- OCI 配置改放 root 家目录：机器人管理的 OCI 配置文件从 `~/.oci/config` 改成 `~/oci_config`（部署为 root 时即 `/root/oci_config`），与 R-Bot 的放置习惯一致；旧部署首次通过机器人上传配置时，旧文件内容自动迁移过去（旧文件保留作备份），CLI 调用在迁移前自动回退读旧文件，不断线
- /oci 与 /uploadapi 支持 R-Bot 格式：整份 client_config 里 `oci=begin` … `oci=end` 块可一次放多个 `[PROFILE]`，一次全部导入；块外的 username/password/azure 等内容忽略不解析
- key_file 归一化：已在 root 家目录下的绝对路径保留原样（如 `/root/1.pem`）；其余一律改成 `/root/<PROFILE>.pem`；原 key_file 已在服务器上的，写入后自动复制到新路径（600 权限），否则提示 scp
- 新账号导入后自动可用：写入配置的同时往 env 文件里加 `MBOT_COMPARTMENT_<PROFILE>`（值取该 profile 的 tenancy OCID，即根 compartment），重启机器人后新账号直接出现在切换菜单里；账号表改为动态生成（`MBOT_COMPARTMENT_*` 环境变量注册，region 从配置文件读），机器人端与网页端同步
- 同主区多账号显示名区分：多个账号同在新加坡时，第一个保持“新加坡”，后面的加“·profile”后缀（如“新加坡·sg1”），否则切换菜单里分不清；机器人端与网页端同步
- OCI 配置路径统一解析（`oci/client.py` 的 `resolve_oci_config_path`）：`MBOT_OCI_CONFIG_FILE` > `OCI_CLI_CONFIG_FILE` > `~/oci_config` > `~/.oci/config`，每次 CLI 调用经环境变量传给子进程

### 修复
- `profile_exists_in_config` 查不到 `[DEFAULT]` 段：configparser 默认把 DEFAULT 当特殊段，`has_section("DEFAULT")` 永远返回 False，现用换名的 default_section 修复

### 验证
- 全量测试 2413 个全过（含新增的路径解析、迁移、R-Bot 格式解析、key 归一化与复制、多 profile 导入、动态账号表、同区显示名区分测试）
- 真机验证（.160 上跑）：现有 4 个 profile 从 `~/.oci/config` 迁移到 `~/oci_config`，内容逐项核对一致；机器人与面板重启后实例列表正常；用一份 R-Bot 格式配置一次导入两个测试账号，确认页、写入、账号切换变量与切换菜单显示（“新加坡·sg1 / 新加坡·sg2”）均正常，验证后已按用户要求撤掉测试账号（用户确认那是很久以前的旧配置，他只有 3 个账号），其余 profile 逐项核对无变化

## [1.27.0] - 2026-11-24

### 新增
- /domain 记录管理（机器人端）：记录列表加“➕ 添加记录”和“🗑️ 删除记录”按钮，此前机器人端只能看记录，不能增删
- 添加记录：选类型（A/AAAA/CNAME/TXT/MX）→ 输入名称（www 或 @ 根域名）→ 输入内容（按类型校验：A 必须是 IPv4、AAAA 必须是 IPv6、CNAME/MX 必须是域名，格式不对可重发）→ 确认页（显示 Zone/类型/名称/内容，TTL 自动、代理关闭）→ 创建；创建后自动刷新记录列表
- 删除记录：选一条记录 → 确认页显示完整记录内容（删除立即生效、不可恢复）→ 确认删除；删除后自动刷新记录列表；记录按下标取自机器人存下的当前 Zone 列表，过期时提示刷新
- 手机端导航抽屉（面板）：此前 ≤900px 宽的屏幕上侧边栏被 `display: none` 整个藏掉，手机上 DNS 管理、配额、任务管理等页面完全没有入口；现在顶栏加汉堡按钮，侧边栏收进左侧抽屉（点按钮打开，点遮罩/导航链接/ESC 关闭），桌面端侧边栏常驻不变

### 修复
- 删掉本地 web/data/tasks.json 里的过期测试任务残留（08:00 一次测试运行留下的假抢机任务），它会让 test_tasks_api_empty_list 单独运行时失败；该文件是运行时生成的，不进仓库

### 验证
- 全量测试 2357 个全过（含新增的记录名/内容校验、添加与删除全流程、手机导航抽屉渲染与样式测试）
- 真机验证（.160 上跑，16/16 通过）：机器人端 CF 客户端真实建一条 TXT 测试记录（mbot-verify-127）再删掉，前后列表确认出现/消失；面板 DNS 接口同样真实增删一条测试记录；概览页含汉堡按钮与抽屉遮罩，CSS 不再藏侧边栏
- 手机视口实测（390×844，真实浏览器经 SOCKS 代理登录）：抽屉默认关闭（left -252），点汉堡后打开（left 0，DNS 管理可见），点 DNS 管理进 /dns 页，添加记录按钮在

## [1.26.0] - 2026-11-24

### 新增
- Cloudflare 凭据管理（机器人 + 面板同步）：/domain 未配置凭据时不再只报错误，给出“手动添加凭据”和“从服务器提取”两个按钮；Zone 列表键盘加“⚙️ 凭据管理”入口
- 手动添加：API Token（推荐，一步输入）或 Global API Key + 邮箱（两步输入）；保存前先调 Cloudflare API 校验（列 Zone），校验通过才保存，不通过不保存；用户发的含凭据消息机器人会尽力删除，不留明文；凭据值绝不回显、不写日志
- 从服务器提取：选一台运行中、有公网 IP 的实例，机器人/面板经 SSH 只读它上面的证书脚本（1.sh）里的 CF_Key / CF_Email 两行，提取到后先校验，通过才保存
- 凭据管理页：显示当前凭据来源（机器人/面板里保存的 / 服务器环境变量里的）、认证方式与脱敏后的邮箱；可更换凭据、删除已保存的凭据文件（只删文件，不动环境变量）
- 新模块 tgbot/cf_config.py：凭据存 tgbot/data/cloudflare.json（0600 权限，原子写入），配置文件优先、环境变量兜底；clouds/cloudflare.py 本身不改（只接受环境变量或显式参数），新模块读出凭据后显式传参构造客户端
- 面板 DNS 页加凭据卡片：状态显示、手动添加表单（Token / Key+邮箱切换）、从服务器提取（实例下拉 + 提取按钮）、删除已保存凭据；新接口 GET/POST/DELETE /api/dns/credentials 与 POST /api/dns/credentials/extract

### 验证
- 全量测试 2322 个全过（含新增的凭据文件存取/优先级/提取解析/校验、机器人添加与提取流程、网页凭据接口测试）
- 真机验证：面板提取接口从新加坡生产机（.139）的 1.sh 真实提取，校验通过（6 个 Zone）并保存；验证后删除文件配置，恢复环境变量来源

## [1.25.0] - 2026-11-24

### 新增
- Telegram 一键登录链接：发验证码时消息里同时带一条“点这里直接登录”链接（/login?token=...），点开即登录，不用再手动输 6 位码；链接一次性、5 分钟过期，token 32 字节随机，比 6 位数字码更难猜
- 新接口 POST /api/auth/telegram/magic-login：消费一键登录 token，正确则建立会话；新存储 web/auth.py 的 MagicLoginStore（一次性、过期失效、发码失败时随验证码一起作废）；新配置 MBOT_PANEL_URL（面板公网地址，拼链接用；未配置时用请求 Host 拼）
- 登录码双通道：发码时如 Bark 已配置且开启，验证码与一键登录链接同步推一条加密 Bark，Telegram 与 Bark 任一渠道收到都能进；Bark 推送失败或异常不影响登录
- 验证码框加 autocomplete="one-time-code"，Telegram 表单去掉 autocomplete="off"：手机浏览器现在能在键盘上方提示短信/邮件里收到的一次性验证码，点一下自动填入（注意：Telegram 消息里的码不在手机系统的自动填充来源里，Telegram 场景请用上面的一键登录链接）

### 验证
- 全量测试 2237 个全过（含新增的一键登录 token 存储、发码链接、magic-login 接口、登录页自动填充标记测试）

## [1.24.0] - 2026-11-24

### 新增
- 实例 CPU 型号显示：列表卡片与详情弹窗在 Shape 下加“CPU 型号”行（如 AMD EPYC 7J13、AMD EPYC 7742、Ampere Altra），机器人实例详情同步加该行；OCI API 不返回宿主机型号，型号经 SSH 读实例 /proc/cpuinfo（A1 兜底 lscpu）取得
- 新模块 tgbot/cpu_model.py：型号解析（取短型号）、持久缓存（tgbot/data/cpu_models.json，型号终身不变、查到一次不再重复 SSH）、实例终止时自动清缓存
- 列表后台自动检测：打开列表后，未缓存型号的运行中实例在后台自动检测，下次刷新即显示；详情弹窗加“🔄 重新检测 CPU”按钮（强制重测），接口 POST /api/instances/{id}/cpu-model

### 验证
- 全量测试 2223 个全过（含新增的 CPU 型号解析/缓存/获取、Web API、详情文案测试）

## [1.23.0] - 2026-11-24

### 新增
- 登录页换成 Uiverse 开源设计（Form by iZOXVL，MIT 许可）：蓝紫渐变卡片 + 下划线浮动标签，纯 CSS 复刻，不引入 Tailwind；密码登录、Telegram 验证码登录、图形验证码、锁定倒计时、Clarity 统计全部保留
- 记住我：登录页勾选后服务端会话与 cookie 同时延长到 30 天（默认 12 小时不变）；配置项 REMEMBER_ME_DAYS（web/config.py）
- 忘记密码入口：点击提示用 Telegram 机器人 /raninfo 命令重置面板密码（面板无自助找回）

### 改进
- 登录页去掉旧版左右分栏与终端装饰，改居中单卡片；功能文案按面板实际改编（用户名登录、默认凭据提示保留）；移动端卡片内边距缩小

### 验证
- 全量测试 2185 个全过（含新增的记住我 cookie/会话有效期、登录页新设计元素测试）

## [1.22.0] - 2026-11-24

### 新增
- 开机确认页 CPU 选项（E2 规格，放在“开始抢机”上面）：只抢 7J13 / 什么 CPU 都可以，当前选中带 ✅，点选后摘要就地刷新；摘要里目标 CPU 行始终显示（以前只在选了 7J13 时才显示）；A1 不筛 CPU，不显示该行

### 改进
- 任务列表（菜单 21）显示在抢什么机器（规格 + 区域 + 实例名），取消按钮同理；不再显示 g1（grab, running, …）这类内部编号与英文类型；抢机启动消息也显示机器说明，不再只给任务 ID；网页版任务页第一列同步改成任务说明
- 实例列表彻底隐藏已终止的实例（机器人端；网页版此前已隐藏），标题总数只计可显示的；全是已终止时提示“没有可显示的实例（已终止的实例已隐藏）”

### 修复
- 看门狗重开文案修正：新版已装、只是 cron 被摘时重新开启，action 从“upgraded”改成“enabled”，机器人与网页端不再误报“已升级到自动识别端口版”，改报“已重新开启”（.121 真机验证挖出）

### 验证
- 全量测试 2181 个全过（含新增的 CPU 选项 / 任务 label / 已终止隐藏 / 看门狗重开文案测试）

## [1.21.0] - 2026-11-24

### 新增
- 菜单 19（本月流量）流量看门狗开关（机器人端 + 网页版）：每台主机可开可关；开启 = 安装看门狗脚本 + 挂 cron（每 10 分钟 check）+ 跑 test；关闭 = 摘 cron + 解除熔断；菜单 19 每台加看门狗状态行（已开启/未开启 + 阻断/限速模式）
- 看门狗模板脚本 scripts/oracle_traffic_guard.py：SSH 端口自动识别（sshd 实际监听端口 > sshd_config > 22），不再写死；Bark Key 不再硬编码进脚本，改读 /root/.traffic_guard_bark（600）或 BARK_KEY / BARK_ENC_KEY / BARK_SERVER 环境变量；已装旧版脚本的机器点开启时自动从旧脚本提取 Key 沿用（提不出时才用机器人 Bark 配置）
- 网页版实例详情加“流量看门狗”行与开关（GET / POST /api/instances/{id}/watchdog）

### 验证
- 全量测试 2153 个全过（含模板脚本、安装编排、机器人回调、网页 API 的新测试）；首尔测试机 probe 识别通过（未安装状态正确识别）；.121 真机验证：升级后状态（新版脚本 + Bark 配置 + cron）已就位、禁用（摘 cron）、网页重开、手动 check、15:00 定时 check 触发 state 更新均通过（熔断 trip 未在生产机上测）

## [1.20.0] - 2026-11-24

### 新增
- 一键安装脚本 install.sh（用户要求 2026-11-24，照 R-Bot 模板改编）：Debian / Ubuntu，root 运行一条命令装好面板 + 机器人 + nginx 反代 + UFW；首次运行是安装，再次运行是更新并重启；管理子命令 start / stop / restart / status / log / upgrade / uninstall
- 安装配置两种方式：交互提问（Token、用户 ID、面板密码、域名），或非交互环境变量 MBOT_INSTALL_TOKEN / MBOT_INSTALL_USER_ID / MBOT_INSTALL_PASSWORD / MBOT_INSTALL_DOMAIN；密码留空自动生成 16 位随机密码，只显示一次并存 /root/.mbot-admin-password（600）
- 域名自动申请 Let's Encrypt 证书（certbot --nginx），失败回落自签证书（面板走 https://IP:9527），回落时面板地址给 IP 而不是域名
- 面板只绑回环内部端口（127.0.0.1:18001），nginx 对外反代（有域名 443，没域名 9527 ssl）；内部与对外端口必须不同，否则 uvicorn 绑不上
- UFW 开启前先放行当前 SSH 端口（sshd 实际监听端口 > sshd_config > 22），不会把自己锁在外面；9527 始终放行（域名回落时也要用）
- 清理 Oracle 云镜像自带的 iptables REJECT 规则：它挡在 UFW 链前面，会把面板端口挡住（外网 No route to host，本机正常）；UFW 开启后删掉它，实时链与 /etc/iptables/rules.v4 各删一次，文件先备份
- apt 锁等待 + 重试：新装系统首次启动 unattended-upgrades 占着 apt 锁，直接 apt-get 会失败退出；等锁释放（最多 10 分钟），update / install 各重试 3 次
- README 快速开始改成一键安装（中英双语），旧的手动安装步骤改名“手动安装”保留在后

### 修复
- 安装脚本在 nohup / 非交互运行时 stdin 是 EOF，read 返回非零导致 set -e 直接杀掉脚本：每个 read 后加 || true，空输入按“未提供”处理（首尔测试机实测）
- 密码哈希生成时 python -c import 不到 web.auth（安装脚本当前目录不一定是安装目录）：加 PYTHONPATH 指到安装目录，出错时打出 stderr（首尔测试机实测）

### 验证
- 首尔 A1 测试机完整验证：全新安装（自签证书）→ 外网登录页 200 → 自动生成密码登录成功 → 更新重跑 → 卸载重装 → 域名证书失败回落自签 → 重启后服务自启、面板仍可访问；机器人用假 Token 时如实报 InvalidToken

## [1.19.0] - 2026-11-24

### 新增
- 菜单 25 近 3 个月花费改默认一次查全部账号（用户要求 2026-11-24）：逐账号列费用，最后附合计；单账号查失败显示“查询失败”，不影响其他账号
- 云账户（菜单 8）加“增加管理员账号”（用户要求 2026-11-24）：用户名 → 邮箱两步输入，二次确认后创建 IAM 用户并加进 Administrators 组；oci/iam.py 新增 create_user / get_group_id_by_name / add_user_to_group
- 登录页加 Microsoft Clarity 统计（用户要求 2026-11-24）：项目 ID 走 MBOT_CLARITY_ID 环境变量，未配置时不加载，公开仓库不硬编码 ID；目前只加登录页

### 修复
- 用量 API 的 0 元项币种有时是单个空格（" " 是真值），会覆盖掉真正的 SGD 导致费用行不显示币种：新增 _usage_currency 统一 strip 后取币种，三个费用格式化函数共用（2026-11-24 真实数据实测，圣何塞账号）

## [1.18.0] - 2026-11-24

### 新增
- 实例详情加引导卷备份（用户要求 2026-11-24）：备份与恢复放一排，备份在前（机器人端与网页版同步）；点备份直接给该实例当前启动盘做 FULL 备份
- 备份满 5 个先删再建（用户要求 2026-11-24）：免费账号备份满 5 个时，实例详情点备份不再自动删最早备份，改提示“备份已满 5/5 个，请先删除旧备份再创建”，并给删除历史备份入口；网页版同样返回 409 并弹出删除弹窗（菜单 7 / 启动盘页的自动删最早逻辑不变）
- 实例详情加删除历史备份（用户要求 2026-11-24）：列出本实例当前启动盘的备份，多选删除（机器人端勾选键盘 + 二次确认，网页版复选框 + 批量删除接口）
- 切换账号/区域不弹新框（用户要求 2026-11-24）：账号列表、区域二级菜单都在原来那条消息里改，切换完成回主菜单也一样；编辑失败时退回新发一条
- Oracle 菜单每页 22 项（用户要求 2026-11-24：每页加两行）：第一页显示到 23 号，42 项共 2 页

## [1.17.2] - 2026-11-24

### 修复
- 品牌字眼扫荡（用户反馈 2026-11-24：仓库里不要出现 R-Bot 克隆字眼）：全仓库清掉“R-Bot / 克隆”相关措辞——注释与文档字符串里的“R-Bot 风格/形状”改成面板形状、M_Bot 风格，README 免责声明重写（不再点名原工具），deploy.sh 标题与 systemd 服务描述改成 M_Bot；内部标识同步改名：环境变量前缀 RBOT_ → MBOT_（部署机的 env 文件同步改，旧名不再读取）、JS 命名空间 window.RBot → window.MBot、会话 cookie rbot_session → mbot_session、MCP 服务器名与 SMTP 默认描述改成 mbot、临时文件与 localStorage 键名前缀改成 mbot。部署侧的服务名与路径（rbot-clone-web/bot、/opt/rbot-clone、/root/.rbot-clone.env）仍是旧名，改它们等于迁移部署，另行安排

## [1.17.1] - 2026-11-24

### 修复
- 一键开机在圣何塞/芝加哥账号上报错 CannotParseRequest（用户反馈 2026-11-24：一键开机出错了）：根因是这两个账号没有区域开机配置，`get_deploy_defaults` 静默回落到新加坡部署值（新加坡可用域/子网/镜像），而 CLI 指向该账号自己的区，跨区请求被 OCI 判 CannotParseRequest；现开机表单校验新增区域一致性检查（可用域所属区域与工作区域不一致时直接报错，提示先配好该区域或切到已配置的账号/区域），一键开机、保存预设、加载预设、确认后启动四处都拦；网页版创建接口同步加区域校验。另：圣何塞（ACCOUNT2）与芝加哥（ACCOUNT3）的区域开机配置已补齐（可用域/子网/Ubuntu 24.04 与 OL 9.8 双架构镜像，配置只放部署机不进仓库），探针验证通过：圣何塞真开出一台 E2（随即删除），芝加哥返回 LimitExceeded，请求解析均正常
- 网页创建默认硬盘 47GB 一直失败：显式指定启动盘大小时 OCI 最少要求 50GB，47 只能是镜像默认值；创建表单硬盘留 47 或空时改发 null（用镜像默认 47GB），48/49 明确报错，显式指定只接受 50–200；表单下方加说明文字

## [1.17.0] - 2026-11-24

### 功能
- 引导卷恢复到实例（用户要求 2026-11-24：增加一个引导卷恢复到实例；语义经用户确认：替换这台的启动盘，停机换盘，原地恢复）：实例详情页新增“💾 引导卷恢复”按钮（只对运行中 / 已停止的实例显示），点开列出该实例当前启动盘的 AVAILABLE 备份，选备份并确认后后台执行：从备份创建新启动盘（与实例同一可用域）→ 调 OCI 原生换盘接口替换启动盘（`update-instance-source-via-boot-volume-details`，OCI 自己停机→换盘→恢复原状态；在运行的回到运行中，原来已停止的保持停止）；换盘失败时实查挂载关系与实例状态确认回滚结论：旧盘仍挂着且状态已恢复说明 OCI 已自动回滚，删掉未挂上的新盘；新盘已挂上则绝不删盘并报手动检查；旧盘只拆不删，仍占启动盘额度，确认新盘没问题后请在启动盘页面手动删除；新盘创建前做启动盘额度预检（200GB 免费额度），不够时直接拒绝不动实例；同一实例同时只允许一个换盘任务。进度与结果走 TG + Bark 双通道通知。实现说明：手动拆盘+挂盘行不通，从备份恢复的启动盘调 attach 会被 OCI 拒绝（Conflict，只能挂回原实例），必须走原生换盘接口；首尔实机验证通过
- 网页版同步：实例详情弹窗新增“引导卷恢复”按钮（只对运行中 / 已停止的实例显示），点开弹窗选备份并确认，起后台任务跑换盘，跳任务页看进度；新增 GET /api/instances/{id}/boot-backups（该实例启动盘的可用备份列表）与 POST /api/instances/{id}/restore-boot-volume（body {backup_id, confirm}，confirm 必须为 true；同一实例已有换盘任务在跑时返回 409）

## [1.16.0] - 2026-11-24

### 功能
- 本月流量（菜单 19）自动安装依赖（用户要求 2026-11-24：每个 VPS 里如果没有安装 vnstat / python3 都需要默认安装）：查流量时某台主机没装 vnstat（stderr 含 "vnstat ... not found" 才认，其他失败不触发），机器人自动 SSH 上去跑 `apt-get update && apt-get install -y vnstat python3 curl && systemctl enable --now vnstat`（非交互，三件套齐了直接返回不碰 apt），装好重试一次查询；刚装好的主机在流量行下标注"vnstat 刚自动安装，数据从现在开始统计"（新装的 vnstat 没有历史数据）；安装失败时显示"vnstat 未安装，自动安装失败"（apt 被占用或无网络时稍后重试），不影响其他台。只支持 Debian/Ubuntu（apt-get 系）；安装超时 300 秒

## [1.15.0] - 2026-11-24

### 修复
- 账号名扫荡（用户反馈 2026-11-24：一键测活消息里显示 DEFAULT / ACCOUNT2 / ACCOUNT3，要求检查其他选项是否也需要替换）：全仓排查用户可见的账号名，凡是直接显示 OCI 配置键名的地方一律改成主区中文名——机器人端：菜单 15 一键测活（多区账号后缀区数）、/me 命令（账号列表与各账号 RUNNING 数）、菜单 2 实例列表标题（用顶行同一套显示名，非主区工作区域后缀区域名）、菜单 3 被暂停开机列表标题、每日报告的配额标题、A1 配置体检标题、删 API 用户时的“当前账号”提示；网页端：配额页顶部账号名、总览页云报告各账号名（后端新增 profileLabel 字段，前端优先用它，旧缓存没有时回落配置键名）。以下仍显示配置键名是正确的，未改：上传 OCI 配置（/uploadapi 与 /oci 流程）里用户正在添加的 profile 键名本身

## [1.14.0] - 2026-11-24

### 功能
- 网页版多区订阅二级菜单（用户反馈 2026-11-24：网页版没有多区订阅的二级菜单，机器人端 v1.7.3 早有了）：实例 / 启动盘 / 配额 / 创建实例 / GFW 换 IP 五个带账号下拉的页面，账号旁新增区域下拉——多区订阅的账号才显示（单区账号隐藏，与机器人端一致）；切换区域后该页数据按新区域重新加载，切换账号时区域重置为新账号的主区；选择存 localStorage，各页共享
- 后端区域透传：GET /api/profiles 每个账号新增 regions 列表（订阅的全部区域，主区排第一，含中文名与 isHome 标记；订阅列表查不到时只含主区）；resolve_profile / get_oci_client 新增 region 参数（不传用主区，传了校验必须在该账号订阅区域里，否则 400；订阅列表查不到时放行，与机器人端一致）；带 ?profile= 的接口统一新增 ?region=（实例 15 个、启动盘 6 个、备份 7 个、配额 4 个、邮件 4 个、对象存储 6 个、GFW 实例列表各接口）；创建接口 body 新增 region 字段，创建表单默认值接口也接受 ?region=（按 (profile, region) 取区域配置，首尔账号切到首尔时自动填首尔的子网）；实例列表服务端缓存键加区域维度，前端缓存键同步加区域

## [1.13.0] - 2026-11-24

### 功能
- 创建表单网络字段自动填充（用户反馈 2026-11-24：可用域/子网不知道怎么填，应该自动选择）：新增 GET /api/create/defaults 接口，按所选账号的区域从部署配置里取可用域与子网（与机器人开机表单同一套 get_deploy_defaults：环境变量 MBOT_PLAN_* 是主区默认值，region_plans.json 里有该区域配置时用该区域的），打开创建页与切换账号时自动填入，仍可手动修改；未配置时返回空串留空手填；所选账号的区域与部署默认值所属区域不符时可用域/子网也留空，不把主区的子网填到别的账号导致创建失败
- SSH 公钥文件改为可选项（用户反馈 2026-11-24：有上传的密钥就填，没有就不填）：服务器上已有公钥文件时自动填入路径，没有时留空；POST /api/create 不再要求 ssh_keys_file，留空时 Grabber 不传 --ssh-authorized-keys-file，只靠 root 密码登录（root 登录默认开启）

## [1.12.0] - 2026-11-24

### 功能
- Bark 加密 Key 自动生成（用户要求 2026-11-24）：菜单 43 与面板“Bark 通知”页新增“🎲 自动生成 Key”按钮，一键生成 32 位随机 Key（16 字节随机数 hex 编码，等价于 `openssl rand -hex 16`；注意 `openssl rand -hex 32` 得到 64 位，太长了不能直接当 Key）。生成前先出确认页（旧 Key 生成后立即失效）；生成后 Key 只显示一次，并附二维码，Bark App 扫码就能添加（也可以手动复制）。机器人端发二维码图片，网页端弹窗里显示二维码；segno 未安装时没有二维码，只显示文字 Key

## [1.11.0] - 2026-11-24

### 功能
- 开机默认 root 密码改 UUID（用户要求 2026-11-24）：抢机默认生成 UUID4 的 32 位十六进制当 root 密码；确认页就显示密码（机器人不会记忆，提醒保存），点“开始抢机”后复用同一份；网页版创建页同步：root 登录开关默认开启，密码框默认预填随机 UUID，可重新生成，提交后弹窗只显示一次
- 开机成功通知照 R 探长格式重排（用户截图反馈 2026-11-24）：标题“🎉 恭喜，开机成功！”，字段按账号 / cpu 类型 / cpu 数量 / 内存大小 / 硬盘选择 / root 密码 / 公钥 / 开机数量（第 i/N 个）/ 花费时间 / 抢机次数 / 开机地区（区域代码 + 中文名）/ ip 的顺序，末尾带“注意📢：root 密码机器人不会记忆”提醒；网页版开机也做双通道通知（TG + Bark，Bark 未设就只推 TG），付费形状额外发置顶提醒
- Bark 补齐（用户反馈 2026-11-24：机器人和网页里都找不到 Bark 设置）：机器人新增顶层命令 /bark 直达设置页（菜单 43 还在）；面板新增“Bark 通知”页（地址 + 32 位 Key + 测试推送 + 开关），与机器人共用 tgbot/data/bark.json；命令菜单 20 → 21 个
- 账号列表显示实际订阅区数（用户反馈 2026-11-24）：切换账号列表用顿号格式（如“新加坡、3 个区”），区数实查订阅区域算出来；只有一个主区的账号不加后缀

### 修复
- Grabber 防重复建误把已终止实例当成功（真 Bug，2026-11-24 首尔实测定位）：15 分钟窗口内同名 TERMINATED 实例也被当作“上次创建成功”返回，现只认 RUNNING / PROVISIONING / STARTING 状态的实例
- 切换账号/区域后直接回主菜单（用户反馈 2026-11-24）：切换成功时编辑当前消息变回 /oracle 主菜单（带确认行），同时删除中间的账号/区域列表消息，不再留多条残留；双击回调 5 秒内去重，不再重复发两条确认
- 已删除的启动盘彻底不显示（用户要求 2026-11-24，推翻 v1.8.1 的分组/折叠方案）：机器人菜单 7 与网页 Volumes 页都过滤掉 TERMINATED / TERMINATING 的盘，已删除分区删除
- 品牌残留扫荡（用户截图反馈 2026-11-24）：登录页大标题与 Logo 字母 R 改成 M_Bot / M，顶栏 Logo 字母 R 改 M，/start 欢迎语“我是 M_Bot 机器人”改成 M-探长；代码注释与 JS 内部命名空间里的旧品牌字样是内部实现，不影响用户看到的内容
- README 免责声明按用户给定文案更新（中英双语）；新增 CHANGELOG.en.md 英文版更新日志，今后每版双语同步

## [1.10.0] - 2026-11-24

### 功能
- GFW 自动换 IP（菜单 16，用户要求 2026-11-24）：菜单 16 从占位改成“GFW自动换IP”。每 30 分钟从中国大陆探针（RIPE Atlas，电信/联通/移动混合）ping 已开启的实例公网 IP，多数探针不通即判定被墙，自动换 IP 再检测，直到换到可用的为止；每次换 IP 都发双通道通知（TG + Bark，没设 Bark 就只发 TG——Bark 是可选项）；单轮最多换 5 次，换满仍被墙就暂停等下个周期；上报探针少于 3 个时结论为未知，不换 IP
- 检测走 RIPE Atlas 正规 REST API（新模块 tgbot/gfw.py）：itdog 自 2026-10 起任务端点强制点选验证码、17CE/BOCE 要付费积分，Atlas 免费账号可领 5 万积分，一次检测约几十积分；API Key 在菜单 16 里设置（只存本机 0600，不回显），设置步骤见 README；网页版同步：面板新增“GFW 换 IP”页（Key 设置 + 逐台开关 + 上次检查 + 立即检查），与机器人共用 tgbot/data/gfw.json

## [1.9.0] - 2026-11-24

### 功能
- 备份多选删除（用户要求 2026-11-24）：机器人备份列表新增“多选删除”模式（逐个勾选 ☑️/☐，再点“删除所选”二次确认，单个失败不影响其他）；网页版 Volumes 页备份表加复选框列 + 全选 + “删除所选”按钮（新接口 POST /api/boot-backups/delete-batch，逐个删除并返回成功/失败清单）
- 付费账号开关（用户要求 2026-11-24）：每账号一个“付费”标记（默认免费），机器人备份列表与网页配额区都能切换，两端共用 tgbot/data/paid_profiles.json；付费账号备份不限 5 个（配额接口返回 paid=true、limit=null、full=false）
- 免费账号备份满了自动删最旧的再建（用户要求 2026-11-24：不用等确认）：机器人与网页创建备份时都不再弹窗确认，服务端直接删最早备份再创建（带 LimitExceeded 重试），结果消息里说明删了哪个；原 deloldestbb 确认分支保留（旧消息按钮误点仍能用）
- 非免费规格实例置顶提醒 + Bark（用户要求 2026-11-24，一定做）：开机成功后若形状不在免费层（A1.Flex / E2.1.Micro 之外），额外发一条置顶提醒（Telegram 消息置顶，1:1 私聊实测可用）+ Bark 推送；每日账单检查发现付费实例（停了 / 停失败 / 在保护名单里跑着）时同样置顶告警，纯费用变化不置顶
- 删掉 /oracle 菜单里的 13. profile管理（用户要求 2026-11-24：菜单顶行已有“切换账号”按钮，菜单里的 13 号是重复入口，没必要留）；其余编号保持不动（不重新编号），切换账号功能本身不变（o:profiles 回调 + 顶行按钮 + 二级区域菜单都还在），菜单总数 43 → 42

## [1.8.1] - 2026-11-24

### 修复
- 磁盘列表清理（用户反馈 2026-11-24：截图里 11 块已删除的旧测试盘把 3 块在用的盘淹没了，文字和按钮还重复两份）：机器人菜单 7 按状态分组（🟢 可用在前、⚫ 已删除在后单独分组，组头按钮点它只应答），文字只留标题 + 各组数量（容量标在可用组后面，不再跟总数混在一起），按钮是唯一的列表（与 v1.7.0 实例列表改版同模式）；孤儿盘标记移到按钮上（🗑️）
- 已删除的启动盘详情页不再给扩容 / 备份 / 删除按钮（墓碑记录上这些操作都无效）；扩容 / 备份 / 删除回调加状态守卫，旧消息按钮误点时提示“该启动盘已删除，无法操作”
- 网页版 Volumes 页同步：主表只放未删除的盘（AVAILABLE 在前），已删除的盘收进默认折叠的分区（点开才看），已删除行的操作列显示“已删除”不给按钮；加载中 / 加载失败时分区自动隐藏，防切账号旧数据闪烁

## [1.8.0] - 2026-11-24

### 功能
- Bark 通知（新模块 tgbot/bark.py + 菜单 43“Bark 通知设置”）：AES-256-CBC 加密推送（与 send_bark.py 同规格，cryptography 加密、openssl 命令行兜底，两者逐字节一致）；菜单里设 Bark 地址、32 位加密 Key、测试推送（不依赖开关）、开关；配置存 tgbot/data/bark.json（0600 权限），地址脱敏显示、Key 绝不回显；环境变量 MBOT_BARK_URL / MBOT_BARK_KEY 可作默认值
- 开机成功双通道推送（TG + Bark）：抢机成功后自动推送，格式照 R 探长成功消息（账号、地区、CPU 类型/数量、内存、硬盘、抢机次数、花费时间、公网 IP；设了 root 密码才带密码行）；Telegram 直接 HTTP 调 Bot API（daemon 线程里也能发）；公网 IP 查不到时重试 3 次，仍查不到写“获取中”；通知失败只记日志，不影响抢机任务
- 菜单 19 流量改 vnstat（用户要求 2026-11-24）：不再查 OCI 用量接口（出站恒 0.00 GB 是错的），改用用户自己的 vnstat 口径——每台 IP + 中文城市名（ip-api.com）+ 当月入站/出站 GiB，末尾合计；连不上的实例单独标原因；菜单改名“19. 本月流量”
- 账单预警每日任务（搬 check_billing.py 的逻辑，改走 OCI CLI）：每天北京时间早上 8 点检查三账号本月费用 vs SGD 1 预算；付费 RUNNING 实例自动 STOP（只停机不删除，工作服务器保护名单绝不停）；免费额度超限（A1 > 4 核/24GB、E2 > 2 台）告警；有变化才双通道通知（费用新增 > 0.01、停了付费实例、超限、检查失败），无变化静默；已超预算但无新增时不重复打扰（已知历史账单不每天告警）

## [1.7.3] - 2026-11-24

### 功能
- 菜单 1 表单顶部加一键开机（默认 AMD）：E2.1.Micro + Ubuntu + 1 台 + 镜像默认盘（47GB）+ 公钥 + root 登录开 + 公网 IP，点后进确认页（原菜单 1 预设一键的模式，不覆盖用户正在填的表单；表单过期时也能用）
- 实例命名改短（用户反馈 2026-11-24）：AMD 免费实例 `AMD_YYYY-MM-DD`、ARM 实例 `ARM_YYYY-MM-DD`（北京时间日期）；其他形状沿用旧格式；面板创建页留空自动生成的名字同规则
- 创建表单 root 登录默认开启（用户要求建完实例一定能 root 登录）；一键 AMD 也默认开 root
- 区域二级菜单（用户反馈 2026-11-24）：菜单 13 点多区账号先出二级区域菜单（实际读取订阅区域，Home 标“（Home）”，当前区域标 ✅），点区域切换工作区域；/oracle 菜单顶行与切换菜单显示当前区域（如“新加坡 · 首尔”）；切账号时区域重置为新账号主区
- 区域开机配置（region_plans.json，按 profile + 区域双键查）：切换工作区域后开机用该区域的可用域 / 子网 / 镜像；未配的区域回退到主区环境变量默认值；子网按租户隔离，不同账号同区域不串用

### 修复
- Grabber 防重复建只认 15 分钟内创建的同名实例为“上次创建成功但响应丢失”（日期名同日再开机不再误把旧实例当新开的）；查不到创建时间时按旧行为处理
- 开机启动前实例名去重：同日同类型再开机时自动加 -2、-3 后缀（批量开机整组检查），去重失败不阻塞开机
- 实例名日期用北京时间（UTC 时间转北京时间后再取日期，避免凌晨时段日期差一天）

## [1.7.2] - 2026-11-24

### 功能
- 流量守护（菜单 37）按用户自己的 auto_limit.sh 重做（用户截图反馈 2026-11-24）
  - 动作三选一（按钮循环切换）：仅通知 / 阻断断网（iptables 熔断，切断外网出站，SSH 逃生门保留）/ 限速200K（tc htb 限流到 1600kbit ≈ 200KB/s）；原“自动关机”动作取消（旧配置里的 shutdown 读出来回退到仅通知）
  - 阈值改低：预设 10GB / 20GB / 200GB / 9TB（9216GB 封顶），默认 200GB（照脚本的 LIMIT_GB）；整 TB 的阈值显示为 TB（如 9TB）
  - 本月流量改用 vnstat 自然月统计（SSH 到实例上查并汇总，替代一直显示 0.00GB 的 OCI 用量接口）；菜单里注明几台可达、几台连不上
  - 新增“🔓 解除限制”按钮：在运行中实例上删阻断链与限速规则（也能清掉主机上 auto_limit.sh 触发的限制）
  - 触发时按动作在全部运行中实例上执行，逐台汇报结果；未配置 SSH 密钥时只能仅通知（菜单与触发时都明确提示）
  - 守护用专用 SSH 密钥（环境变量 MBOT_GUARD_SSH_KEY，部署时生成并把公钥加到各实例）

## [1.7.1] - 2026-11-24

### 功能
- 菜单 20 免费额度改三级导航：先出账号列表（每个账号点开看详情），订阅了多个区域的账号出二级区域菜单（Home 区域标“（Home）”），点区域看该账号在该区域的额度（标题如 `[新加坡 · 首尔]`，OCI 客户端按区域查询）；单区账号点开直接显示，不出二级菜单（用户截图反馈 2026-11-24）
- /oracle 菜单顶行加切换账号按钮（`🔄 切换账号：新加坡（3 个区）`，点开菜单 13），不用再翻到菜单 13 才能切换；菜单显示名只读缓存不调 OCI，机器人启动时后台预热订阅区域缓存（用户截图反馈 2026-11-24）
- 面板账号下拉与机器人切换菜单统一：显示主区中文名，多区账号后缀区数（如 `新加坡（3 个区）`）；`/api/profiles` 新增 `regionCount` 与 `displayName`（订阅区数查 `iam region-subscription list`，缓存 1 小时）
- 菜单 18 订阅信息：有几个账号显示几个（原来只显示当前账号），每个账号一段（主区中文名 + 租户名 + Home 区域 + 描述），单个账号查失败时单列“查询失败”不影响其他账号（用户截图反馈 2026-11-24）
- 区域中文名新增首尔（ap-seoul-1），机器人与面板同步

### 修复
- 菜单 8 云账户用户列表：文字只剩标题 + 数量，按钮是唯一的列表（`👤 用户名`，非 ACTIVE 后缀中文状态词），邮箱在详情页看；原来文字列表和按钮列表同时出现、邮箱换行看着乱（用户截图反馈 2026-11-24，与实例列表同款改法）
- 菜单 13 账号切换：按主区中文名显示账号（新加坡 / 圣何塞 / 芝加哥），订阅了多个区域的账号后缀区数（如 `新加坡（3 个区）`）；区数查 `iam region-subscription list`，缓存 1 小时，查失败时只显示主区名；切换确认消息也用显示名
- 操作流程消息清理（用户截图反馈 2026-11-24：改完名只留结果这一条信息）：文字输入提示在用户回复后删除（发新提示前先删旧提示，重复点按钮不再攒多条）；二次确认框在操作完成后删除，只留结果消息；覆盖改名、重装、用户改描述/邮箱、删除用户、从备份恢复、SSL 加域名、Email 发件人八个流程

## [1.7.0] - 2026-11-24

### 功能
- 启动卷备份与恢复（M_Bot 独有）：面板启动盘页 + 机器人菜单 7，双边都有
  - 创建 FULL 全量备份，默认名 `<卷名>-backup-YYYYMMDD-HHMM`（北京时间）
  - 免费额度检查（上限 5 个/区域）：满了提示最早备份信息，可一键删除最早备份并继续
  - 从备份恢复出一台新实例（原机不动）：两步走（先从备份建启动卷，再用新盘开机），形状默认用来源实例的（查不到用 A1 1核/6GB），恢复前做 A1/E2 与启动盘额度预检
  - 备份列表只显示未删除的备份（TERMINATED / TERMINATING 不显示，与配额计数一致）
- 创建实例默认启动盘 50GB → 47GB（与 OCI 镜像默认一致；API 默认值、创建页输入框、三个预设卡片同步改）
- 机器人实例列表按状态分组显示（运行中→已停止→已终止→其他，空组不显示；已终止的不隐藏，单独分组）；按钮就是唯一的列表，每台按钮显示 `名称 · A1/E2 · 公网 IP`（无 IP 时显示状态词），文字只剩标题 + 各组数量，不再逐台重复（用户反馈：原来文字列表和按钮列表同时出现两份）

### 修复
- 删最早备份后立即创建撞上 OCI 配额同步延迟报 LimitExceeded：创建失败时自动重试（最多 5 次，间隔 15 秒；实测 2026-11-24 芝加哥）
- Volumes 页切换账号：先清空列表并显示“加载中…”（含配额标题，不再闪旧账号的数字），加载加代次 guard，快速切换时旧代次的异步结果不再写回，避免新旧数据串台（浏览器实测 2026-11-24 挖出：第一版只清空了列表，配额标题仍闪旧数字）
- 备份名时间戳从 UTC 改成北京时间，与面板显示的创建时间一致（浏览器实测 2026-11-24）

## [1.6.0] - 2026-11-24

### 功能
- 实例页改卡片布局：卡片显示名称+状态徽标、公网 IP、Shape、规格、启动盘（大小·VPU+状态徽标+已附加徽标）、创建时间；按钮矩阵：SSH、关机/启动/重启、串行控制台、重命名、升降级（仅 A1）、修复、重置镜像、IP 管理、启动盘、删除、删除（保留启动盘）
- IP 管理弹窗：换 IP / 附加 IPv6 / 删除 IPv6 集合在一个弹窗里
- 启动盘弹窗：显示大小 / VPU / 状态 / 挂载状态 / OCID，可在线扩容（只能扩不能缩）
- 删除（保留启动盘）：终止实例但保留引导卷，之后可在启动盘页删除；确认框写明占用 200GB 免费额度

### 修复
- 切换 Profile 时先清空列表再加载，修掉 v1.3.0 切换时一帧旧数据闪过的问题

## [1.5.0] - 2026-11-24

### 功能
- Telegram 命令菜单：机器人启动时注册 20 个命令到 Telegram 菜单（/start /help /oracle /instances /domain /azure /aws /gcp /do /vf /solusvm /oci /command /pubkey /uploadapi /oproxy /clearlock /raninfo /me /admin），对照 机器人小助理的菜单文案
- /me：查看我的信息（TG ID、用户名、面板版本、各账号 RUNNING 实例数）
- /admin：管理员菜单（面板状态、重启面板/机器人服务、查看版本，重启需二次确认）
- /oci：上传 Oracle 或 Azure 配置文件（校验后追加 profile；私钥不经 Telegram，需自行 scp）
- /clearlock：清除面板登录失败锁定（重启面板服务，锁定状态在面板进程内存里）
- /raninfo：重置面板管理员随机密码（pbkdf2-SHA256 哈希写 env、明文写密码文件 600、重启面板，新密码只私发一次）
- 六个云命令（/azure /aws /gcp /do /vf /solusvm）：未配置时回复配置方法，已配置但模块是 stub 时直说功能尚未实现，绝不编造数据

## [1.4.0] - 2026-11-24

### 功能
- 实例操作补全：详情弹窗（公网/私网 IP、IPv6 地址、启动盘大小）、换 IP、附加/删除 IPv6、升/降配 A1、一键修复
- 换 IP：先删旧临时 IP 再绑新保留 IP（OCI 拒绝给已绑公网 IP 的内网 IP 再绑新 IP），须 confirm=true
- 升/降配：仅 A1.Flex，1–4 核 / 1–24GB 免费额度校验，直接改规格由 OCI 自动重启（显式关机曾卡 17 分钟）
- 一键修复：查实例状态/主 VNIC/公网 IP，缺公网 IP 时自动补一个临时 IP 并报告三项检查结果
- IPv6 三函数从 Telegram 处理器移入 oci/network.py，面板与机器人共用同一实现

### 修复
- 换 IP 时判断"有无公网 IP"改用 VNIC 的 public-ip 字段：AD 作用域的公网 IP 列表查不到 REGION 作用域保留 IP，会误判为无 IP

## [1.3.0] - 2026-11-24

### 性能
- 实例页：各实例公网 IP 并行获取 + 服务端 30 秒缓存 + 前端 15 分钟缓存；冷启动约 5 秒（原来 10–20 秒），缓存命中毫秒级
- 云报告：三个账号并行汇总 + 两层缓存；冷启动约 7 秒（原来 30–60 秒以上），总览页加刷新按钮

### 功能
- 新增 GET /api/profiles：返回账号列表（名字 + 区域 + 中文区域名）
- 实例 / 启动盘 / 配额 / 创建四页加动态账号下拉（"名字 · 区域中文名"），切换后带 ?profile= 重载，localStorage 记忆上次选择；无独立区域选择器（区域是账号自带属性）
- 创建页加自定义镜像 OCID 输入框：非新加坡账号创建时可填该区域镜像

### 修复
- 实例页的终止 / 改名 / 重装 / 控制台 / 批量操作之前都不带 ?profile=，在非默认账号下会误操作默认账号，现全部带上当前账号（启动盘页同理）

## [1.2.2] - 2026-11-24

### 修复
- 创建实例页 A1 规格上限改成免费额度：单台最大 4 OCPU / 24GB（原来显示硬件上限 80 核 / 512GB，容易误开付费规格）；前后端双重校验，超过返回 400
- Telegram 菜单 1 表单同步：核心选项 1/2/4、内存选项 1/6/12/24，自定义值同样按 4 核 / 24GB 校验
- 创建页镜像提示改成人话（不再提 web/config.py 内部文件名）

## [1.2.1] - 2026-11-24

### 改动
- Web 面板品牌统一改成 M_Bot：全部页面标题、顶栏 Logo、登录页、Telegram 验证码短信签名统一改为 M_Bot Oracle开源管理面板

## [1.2.0] - 2026-11-24

### 功能
- 菜单 1（开机）改成 M_Bot 风格按钮矩阵创建表单：CPU 类型 / 系统 / 核心 / 内存 / 硬盘 / 数量 / 延迟每行点选（再点取消），延迟选两个值为区间随机
- 批量创建：一次最多 4 台，配额预检按数量×规格计算；root 登录随机密码（只在启动消息里显示一次）；公网 IP / IPv6 开关；开机脚本支持上传文件或粘贴文本
- 表单可保存为快捷开机（菜单 17 一键开机）；CentOS 映射为 Oracle Linux（需配 MBOT_IMAGE_OL_* 镜像）
- 修复旧 Bug：旧预设流程没把 OCPU/内存传给 Grabber，A1 预设实际都按默认规格创建

## [1.0.1] - 2026-11-24

### 改动
- 项目改名：M-探长 → **M_Bot Oracle开源管理面板**（仓库 jimu2000/m-tanzhang → jimu2000/M_Bot，旧地址自动跳转；Telegram 机器人仍叫 M-探长）

## [1.0.0] - 2026-11-24

首个完整版本。Telegram 机器人 /oracle 菜单 42 项，Web 面板 + Web SSH，1318 个单元测试全过，已部署验证。

### 功能
- OCI 实例全生命周期：创建（抢机）、列表、详情、启停、重启、终止、升降配、改名、重装、串行控制台
- IP 管理：保留 IP 创建/删除、实例换 IP、IPv6 创建/删除/列表
- 磁盘管理：启动盘列表/扩容、VPU 修改、孤儿盘清理
- 云账户管理：用户详情、改描述、改邮箱、API 密钥列表/删除、删除用户（自用账号保护）
- 批量操作：批量启停/重启/删除、A1 配置体检
- 监控：实例监控通知、监控自启、流量守护（仅通知/自动关机）、停机保活、域名 SSL 到期提醒、每日报告
- 客户端工具：远程 Shell（/command）、公钥管理（/pubkey）、代理管理（/oproxy）、上传 API 配置（/uploadapi）
- 抢机计划、对象存储、Email Delivery、证书管理、巡检计划、云报告
- Cloudflare DNS 管理（Web 面板 + /domain）
- Web 面板：登录锁定、会话管理、图形验证码（可选）、Telegram 验证码登录、SFTP 文件面板（分块上传带进度）、批量命令、终端收藏夹
- MCP 服务器（6 个工具，JSON-RPC over stdio）
- 内存占用/清除（防闲置回收）、一键测活、快捷开机预设

### 安全
- 配置全部走环境变量，源码无真实凭据
- 写操作二次确认；终止实例默认永久删除引导卷
- 危险命令黑名单（rm -rf /、mkfs、dd 等）
- 登录失败锁定（5 次起，时长翻倍，30 分钟封顶）

## 2.14.1 (2026-11-27)

- 修复 IAM 用户管理在窄屏下表格挤压、操作按钮纵向堆叠的问题：用户列表改为响应式卡片布局，手机端操作按钮三列自适应。
- Bot API 专用用户策略新增 `manage users in tenancy`，支持面板/机器人管理 IAM 用户和重置控制台密码；该权限可管理租户内所有 IAM 用户及其凭据，创建前明确提示高权限范围。
- 修复控制台密码重置失败时把 OCI 原始 ServiceError JSON、请求 ID 和 endpoint 直接显示给用户的问题；Web 面板与 Telegram 共用的重置函数现在返回可操作的中文诊断提示。
- 完善 `NotAuthorizedOrNotFound` 的排查指引：检查 API 用户 tenancy 级管理用户凭据权限、目标用户状态及 Identity Domains 类型。OCI 权限拒绝仍由 Oracle 服务端执行，应用不会尝试绕过。
- 增加 IAM 密码重置命令及错误脱敏单元测试。
