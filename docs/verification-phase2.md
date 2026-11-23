# 阶段 2 验证记录：Telegram 机器人

验证时间：2026-11-23（UTC 约 08:30）。验证人：阶段 2 协调员（亲自复查全部
代码 + 跑测试 + 真实 API 实测）。用户尚未提供机器人 Token，真实 Telegram
联调未做（见“需 Token 后再测”）。

## 结论

- **220 个单元测试全过**（`python3 -m unittest discover -s tests`，约 1 秒）。
- **11 项真实验证全过**：9 项只读 API 实测 + bot 构建 + 73 个 callback_data
  路由全覆盖。
- 写操作（创建、终止、启停、换 IP、删盘、删 MFA）**零次碰生产实例**，
  全部用 mock 断言命令构造。
- 集成检查发现并修复了 7 个问题（见“协调员修复记录”），其中 2 个是
  不经真实 API 实测发现不了的用法错误。

## 代码规模

| 文件 | 行数 | 说明 |
|---|---|---|
| tgbot/handlers.py | 399 | 核心命令：/start /help /oracle /instances、实例列表/详情/操作、二次确认 |
| tgbot/handlers_extra.py | 1591 | 扩展：24 项菜单 + 换 IP 确认 + 2 个 JobQueue 任务 |
| tgbot/bot.py | 148 | 入口：命令/回调注册、定时任务注册 |
| tgbot/keyboards.py | 216 | 全部内联键盘（34 项菜单、实例操作、确认等） |
| tgbot/services.py | 318 | 白名单、profile、实例存取、格式化、任务注册表 |
| tgbot/config.py | 110 | 配置区（Token/白名单走环境变量，OCID 占位符） |
| tests/test_tgbot_handlers.py | 574 | 核心命令测试（43 个） |
| tests/test_tgbot_handlers_extra.py | 1161 | 扩展命令测试（73 个，含协调员补的 5 个） |

## 单元测试（220 个全过）

组成：oci 模块 104 个（阶段 1 的 100 个 + 抢机可取消回归 4 个）+
核心命令 43 个 + 扩展命令 73 个。

覆盖要点：
- 白名单拒绝（命令 + 回调，未授权用户直接拒绝且不执行 OCI 调用）。
- 34 项菜单 dispatch：24 项已实现返回 True 且有回复；11 项未实现返回
  False 且不回复（含一个不存在的 key）。
- 实例全流程：列表（有/无/失败）、详情（无效下标、无缓存、查 IP 失败不影响
  详情）、各操作确认流程（终止含“永久删除引导卷”警告 + HTML parse_mode）、
  确认执行成功/失败。
- 写操作 mock 断言：换 IP 调 change_public_ip；升降配调
  `compute instance update --shape-config {"ocpus":..,"memoryInGBs":..}`；
  删盘调 delete_boot_volume；删 MFA 逐个调 delete_mfa_device；终止只传
  `(cli, id)`，靠默认 `preserve_boot_volume=False` 删引导卷。
- 抢机：Grabber 必须 mock（测试里不真抢）；任务注册/取消/线程收尾；配额不足
  与取消的任务状态落盘。
- JobQueue 任务：开关关闭时直接返回；状态变化通知；自启成功/失败通知；
  每日报告开关与内容。
- 用量时间窗口截断到午夜（真实 API  bug 修复后的回归测试）。

## 真实 API 只读验证（9 项全过，DEFAULT/新加坡）

验证脚本：`/tmp/verify_phase2.py`（临时脚本，不进仓库）。全部只读，
未创建/修改/删除任何资源。

| 验证项 | 结果 |
|---|---|
| 用量 API MONTHLY（流量菜单） | 通过：4 条用量记录 |
| 用量 API MONTHLY 求和（费用菜单） | 通过：**近 3 个月总花费 13.46 SGD，与已知 11 月账单一致** |
| 订阅信息（iam tenancy get） | 通过：租户名 <已脱敏>，home SIN |
| IAM 用户列表 | 通过：2 个用户 |
| 工作请求列表 | 通过：10 条 |
| 已订阅区域 | 通过：3 个区域，含 ap-singapore-1 |
| IPv6 列表（运行中实例主 VNIC） | 通过：1 个 IPv6 |
| MFA 设备列表 | 通过：1 个设备 |
| 配额汇总 | 通过：A1.Flex 4.0/4 核，18.0/24GB（与已知状态一致） |

## bot 构建与路由验证（假 Token，不联网）

- `build_application()` 构建成功：4 个命令（start/help/oracle/instances）+
  20 个 handler（含 2 个 JobQueue 定时任务）。
- **73 个 callback_data 全部有路由**：枚举全部键盘（菜单两页、主菜单、
  实例列表/操作、确认×8 种 action、开机预设/确认、任务、profile、
  磁盘列表/操作/扩容、升降配预设）产生的 callback_data，逐个匹配已注册的
  CallbackQueryHandler 正则，无一漏网。

## 协调员修复记录（集成检查发现的 7 个问题）

1. **换 IP 流程断链（功能 bug）**：`handle_confirm("changeip")` 已实现，
   但没有任何按钮发出 `c:changeip:<idx>`——实例操作 `i:ip:<idx>` 转发到
   菜单 2 的 IP 总览就没下文了。修复：handlers.py 把实例操作 "ip" 转发为
   "changeip"；handlers_extra 新增 `handle_changeip_prompt`（显示当前 IP +
   警告 + 确认按钮）；补 4 个测试。
2. **用量 API 粒度 TOTAL 不存在（真实 API 发现）**：`handle_cost` 与
   `daily_report_job` 传 `--granularity TOTAL`，真实 API 报
   "Unknown Granularity"（用量 API 只认 DAILY/HOURLY/MONTHLY）。
   修复：费用汇总改用 MONTHLY 再求和；更新测试断言。
3. **用量 API 时间精度（真实 API 发现）**：时间窗口带时/分/秒，真实 API 报
   "Passed UTC date does not have the right precision"。修复：
   `usage_time_window` 截断到午夜（结束=今天 00:00，开始=往前推 90 天
   00:00）；补回归测试。
4. **bot.py 缺少 r:/v: 路由（集成缺口）**：升降配预设（`r:`）与磁盘操作
   （`v:detail/resize/volsize/delete`）没有任何 handler 注册，点按钮无响应。
   修复：补 5 个 CallbackQueryHandler。
5. **测试隔离缺陷**：`test_forward_no_extra_module` 靠删 sys.modules 条目
   模拟模块缺失，但真实模块落盘后会从磁盘重新 import 成功，测试误调真实
   handler（还花了 3.2 秒疑似碰真实 API）。修复：条目置 None（import 系统
   直接抛 ImportError）；handlers.py 的转发函数改用 getattr，模块存在但
   函数缺失时也能降级。
6. **扩展测试用真实 IP（脱敏违规）**：测试文件里出现用户真实公网 IP
   真实 IP（已删除）。修复：全部换成文档网段 203.0.113.x。
7. **文档字符串错误**：handlers_extra 的模块 docstring 称 r:/v: 回调“由
   handlers.py 直接转发”（实际由 bot.py 直接注册）；_DISPATCH 注释称
   restart“由 handlers.py / bot.py 处理”（实际未实现）。均已修正。

## 需 Token 后再测（用户提供机器人 Token 后）

1. 机器人启动：`RBOT_TELEGRAM_TOKEN` + `RBOT_ALLOWED_USERS` 环境变量，
   `python3 -m tgbot.bot`，看 polling 是否正常收消息。
2. 真实 Telegram 走一遍：/start、/oracle 菜单翻页、每项菜单点一遍看回复。
3. 实例操作全流程（用测试实例或只读操作验证，**不要在生产实例上点终止**）：
   列表→详情→停止/启动二次确认→换 IP 确认→升降配确认。
4. 抢机端到端：先确认 BOOT_PRESETS 已填真实镜像/子网/AD；建议先用小预设
   在有空闲配额的账号上试一次，验证任务进度与取消。
5. 监控 job：开监控，等 10 分钟看状态变化通知；开自启，停一台测试实例看
   是否自动拉起（生产实例勿试）。
6. 每日报告：开每日报告，等次日 08:00 看推送（或临时改 DAILY_REPORT_TIME
   验证后改回）。
7. 白名单：用非白名单账号发命令，确认被拒绝。

## 已知设计说明（不是 bug）

- 扩展模块的 `_reply` 对回调用 `reply_text` 发新消息（不编辑原消息），
  核心模块用 `edit_message_text` 编辑原消息。两者混用是刻意选择：
  扩展菜单多为查询类结果，发新消息保留历史；如需统一，后续可改。
- 磁盘扩容点选目标容量后直接执行，不再二次确认（选容量本身就是明确选择；
  扩容不可逆但不破坏数据）。
- 菜单 5（升/降配）与换 IP 依赖 `pending_idx`（从实例列表点进来时写入）；
  直接从 /oracle 菜单点这两项且之前没选过实例时，会提示先选实例。
- 监控与每日报告只看默认 profile（DEFAULT）的实例；多账号监控待后续版本。
- 抢机预设里的 image_id / subnet_id / AD / ssh_keys_file 都是占位符，
  部署时必须填真实值，否则抢机启动即失败（can_launch 预检会先报错）。

## 环境说明

- 本机（Muse VM）构建 Application 时，httpx 会读 NO_PROXY；本机 NO_PROXY
  含 `[::1]`，httpx 解析报 `InvalidURL: Invalid port: ':1]'`。验证时清掉
  代理变量后构建成功。**这是本机环境怪癖，与代码无关**；部署到用户服务器
  上不会有这个问题（那里的 NO_PROXY 正常）。
- 依赖：`python-telegram-bot[job-queue]>=22.0`（含 APScheduler，供
  JobQueue 定时任务用）。本机已装 22.8 + APScheduler 3.11.3
  （`pip3 install --user --break-system-packages`，因 PEP 668 需加
  --break-system-packages）。

## 脱敏检查

- `tgbot/` 与 `tests/` 全部 Python 文件：无真实 IP、无真实 OCID（20 位以上
  后缀）、无 Token。测试用占位 OCID（`..xxxx`/`..test`/`..aaa1`）与文档网段
  203.0.113.x。
- 注意：`docs/rbot-live.md` 与 `docs/verification.md` 是内部验证记录，含少量
  真实 IP（无真实 OCID）。**公开到 GitHub 前必须清理这两份文档**（阶段 1
  遗留，非阶段 2 引入）。
