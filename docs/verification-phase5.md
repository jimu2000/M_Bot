# 阶段 5 验证记录：补齐缺口（2026-11-23）

## 结论

阶段 5 全部 5 个工作包完成，全量测试 **1248 个全部通过，零回归**（基线 584，新增 664）。
协调员亲自复跑确认：`Ran 1248 tests in 28.2s, OK`。
Bot 构建成功（44 个 handler，JobQueue 正常）；FastAPI 冒烟：15 个新端点/页面全部可达
（API 未登录返回 401 而非 404，4 个新页面返回 200）。
脱敏检查通过：oci/、tgbot/、web/、clouds/、tests/ 无真实 IP/OCID。

未做真实验证（按任务要求）：所有功能只用 mock 测试，真实验证由主代理在测试实例上做。

## 各包完成情况

| 包 | 内容 | 新增测试 | 全量复跑 |
|---|---|---|---|
| A | OCI 批量操作 + 实例管理增强 | 97 | ✅ |
| B | Telegram 客户端工具 + 菜单 23/24 | 118 | ✅ |
| C | 监控增强 + 动态按钮状态 | 123 | ✅ |
| D | Web 面板补充 | 176 | ✅ |
| E | 深度研究新增缺口 | 150 | ✅ |
| **合计** | | **664** | **1248 全过** |

## A 包：OCI 批量操作与实例管理增强（97 测试）

新增函数（oci/instance.py）：`batch_start/stop/restart/terminate`
（逐台调现有函数，汇总 `{"succeeded", "failed": [{id, error}]}`；
terminate 显式 `preserve_boot_volume=False`）、`rename_instance`
（`--display-name` + `--force`，空名/>255 字符抛 ValueError）、
`reinstall_instance`（`--source-details` JSON 重建引导卷，docstring 写清
清空系统盘、保留 OCID 与 IP；`image_id=None` 时先 get 查当前镜像）、
`get_serial_console`（create + get，返回连接串/指纹/状态）。

oci/volume.py：`update_boot_volume_vpus`（`--vpus-per-gb` + `--force`，
只认 10/20/30）；列表精简字段新增 `vpus-per-gb`。

oci/quota.py：`a1_health_check`，返回 `{"overall_ok", "suggestions":
[{severity, message}]}`；error=超限，warning=用满，info=单台超 2 核/12GB
建议降配，全部正常时唯一一条 ok 建议。

入口：
- 批量操作：Telegram `/oracle` → 35. 批量操作（多选 → 选动作 → 二次确认 → 逐台汇报）；
  Web `/instances` → 勾选复选框 → 批量操作栏。
- 改名：Telegram 实例详情 → ✏️ 改名（回复文字 → 二次确认）；Web `/instances` → 每行「改名」。
- 重装：Telegram 实例详情 → 💿 重装系统（回复完整实例名 → 二次确认）；
  Web `/instances` → 每行「重装」（弹窗输入实例名确认）。
- 串行控制台：Telegram 实例详情 → 🖥️ 串行控制台；Web `/instances` → 每行「控制台」。
- 改 VPU：Web `/volumes` → 每行「改 VPU」（档位 10/20/30）。
- A1 体检：Telegram `/oracle` → 36. A1 配置体检；Web `/quota` → 底部体检卡片。

## B 包：Telegram 客户端工具 + 菜单 23/24（118 测试）

新文件：`tgbot/handlers_client.py`（六项实现，配置区顶端）、
`tests/test_tgbot_client_tools.py`（118 测试，全 mock）。

- **/command 远程执行 Shell**（29 测试）：发 `/command` → 选已保存主机
  （复用 `DATA_DIR/hosts.json`，与 Web 面板同一存储）→ 发命令文本 →
  Paramiko 执行回显。密码认证主机直接拒绝。黑名单 10 条
  （`rm -rf /`/`/*`、`mkfs`、`dd if=`、Fork 炸弹、`shutdown`/`reboot`/
  `init 0`/`halt`/`poweroff`、`> /dev/sd`）；超时 30 秒；输出超 20000
  字符截断、按 4000 字符分段。
- **/pubkey SSH 公钥管理**（21 测试）：列出（只显示类型+尾 8 位+注释）/
  添加（校验 ssh-rsa/ssh-ed25519/ecdsa- + base64 可解码 + blob 内嵌类型
  一致，私钥内容拒绝）/删除（二次确认）。存 `DATA_DIR/ssh_keys.json`，只存公钥。
- **/oproxy 代理设置**（10 测试）：查看/设置/清除（清除二次确认），
  校验 `http://`/`https://`/`socks5://` 开头；存 `DATA_DIR/proxy.json`；
  设置后提示“重启机器人服务后生效”。
- **/uploadapi 添加 API**（19 测试）：发 config 文件或粘贴文本 → 校验
  `[PROFILE]` 段 + user/fingerprint/tenancy/region/key_file 五键 →
  二次确认 → 追加写入 `~/.oci/config`；同名 profile 不覆盖，提示改名重传。
  私钥不经过 Telegram。
- **菜单 23 占用 25% 内存**（入口 `/oracle` → 23）：选运行中实例 →
  二次确认 → SSH 启动 `nohup python3 -c "…读 /proc/meminfo 算 25%…
  bytearray 分配并逐页触碰…sleep 10^9…" & echo $!`，PID 记入
  `DATA_DIR/mem_hog.json`（`{OCID: {pid, started, host}}`）；同一实例
  已有记录时拒绝重复启动。
- **菜单 24 清除缓存和占用**（入口 `/oracle` → 24）：选实例（有占用进程
  的标 🧠）→ 二次确认 → 杀 PID（不存在则忽略）→
  `sync; echo 3 > /proc/sys/vm/drop_caches`（非 root 用 `sudo sh -c` 包住
  重定向）→ 删记录 → 汇报。

注意：`/help` 清单未更新（handlers.py 按分工规则未动，主代理后续把四个
新命令加进帮助文本）。memhog 的 SSH 默认密钥假设与占用进程实际 RSS
效果需真实验证；占用进程 `sleep 10^9` 约 31 年，实例重启后需重新占用。

## C 包：监控增强 + 动态按钮状态（123 测试）

新文件：`tgbot/monitoring.py`（纯逻辑与配置持久化）、
`tests/test_monitoring.py`（41）、`tests/test_cloudflare_ssl.py`（11）、
`tests/test_tgbot_monitoring.py`（71）。

- **流量守护**（`/oracle` → 37）：配置 `DATA_DIR/traffic_guard.json`
  `{"enabled": false, "threshold_gb": 10240, "action": "notify",
  "last_triggered": null, "chat_id": null}`，默认关闭/10240GB/仅通知。
  菜单可开关、切动作（仅通知/自动关机）、改阈值（1024/5120/10240/20480GB
  预设）。流量复用现有用量 API（与菜单 19 同一查询）。`shutdown` 时停掉
  默认账号下全部 RUNNING 实例并通知；触发后 24 小时内不重复执行。
- **停机保活**（`/oracle` → 38）：配置 `DATA_DIR/keepalive.json`
  `{"实例 OCID": true/false}`，默认全关；失败计数与停试名单在
  `DATA_DIR/keepalive_state.json`。STOPPED 且开启的实例自动 START 并通知；
  连续 3 次失败后停试并通知，需到菜单里重新开启才恢复。
- **SSL 监控**（`/oracle` → 39）：`clouds/cloudflare.py` 加
  `check_ssl_expiry(domain, port=443)`（标准库 ssl 直连，返回
  `{"domain","expires","days_left"}`；失败抛带域名的 `CloudflareError`）。
  域名列表 `DATA_DIR/ssl_watch.json`。每日 08:30 的 `ssl_check` job 在到期
  前 30/14/7/1 天各提醒一次（迟加入的域名一次性记下已跨过的阈值、只发一条）。
- **动态按钮状态**（补充任务）：9/10/37/38 号按钮文字动态带
  `(开启)`/`(关闭)` 后缀；开关切换后刷新菜单。12 个专用测试。

注意：A 包复跑时曾报 C 包 2 个 SSL 测试失败（阈值列表顺序问题），C 包在
完成前已修复，最终全量 1248 全过。

## D 包：Web 面板补充（176 测试）

新文件：`oci/objectstorage.py`、`oci/email.py`、`tgbot/client_ops.py`、
`web/routes/objectstorage.py`、`web/routes/email.py`、`web/routes/plans.py`、
`web/templates/objectstorage.html`、`web/templates/email.html`、
`web/static/js/objectstorage.js`、`web/static/js/email.js`、
`web/static/js/settings.js` + 6 个测试文件。

- **对象存储**：Web 侧边栏「对象存储」→ `/objectstorage`（列/建/删 Bucket，
  点进列对象、上传、删对象，删除二次确认）；Telegram `/oracle` → 40. 对象存储
  （只读列表 + 查看对象 + 删除二次确认）。
- **Email Delivery**：Web 侧边栏「Email」→ `/email`（创建发件人 + 选 IAM 用户
  生成 SMTP 凭据，凭据只显示一次，关闭时清掉页面密码）；
  Telegram `/oracle` → 41. Email 发件人（输入邮箱 → 二次确认 → 创建，
  SMTP 凭据只在 Web 生成，不进聊天记录）。
  ⚠️ 任务原文命令是错的：`oci email smtp-credential create` 在 CLI 3.96.3 不存在，
  已改用真实命令 `oci iam smtp-credential create --user-id --description`
  （description 必填），测试已按真实命令断言。
- **修改面板密码**：顶栏齿轮 → 改密码弹窗；`POST /api/auth/change-password`
  （验旧密码 + 新密码 ≥8 位含字母数字；原子写入 `config.ENV_FILE`，
  600 权限，保留其他行；返回“重启 Web 面板服务后生效”）。
- **抢机计划**：任务页 `/tasks` 下方「抢机计划」区（添加/删除/开关）；
  Telegram `/oracle` → 42. 抢机计划（列计划 + 开关 + 删除二次确认）。
  计划存 `DATA_DIR/grabber_plans.json`；调度器默认关闭，需部署时设
  `RBOT_ENABLE_PLAN_SCHEDULER=1`；`web/tasks.py` 的 `PLAN_DEFAULTS`
  （可用域/镜像/子网/SSH 公钥文件）仍是占位符，部署时须填真实值。
  cron 校验用 APScheduler 自身 `CronTrigger.from_crontab`。
- **开放云面板端口**（`/oracle` → 11）：二次确认 + 明确警告外网暴露风险；
  执行 = 安全组加 ingress 规则（已存在则跳过）+ UFW 放行。
  ⚠️ `PANEL_SECURITY_LIST_ID` 是占位符，未配置时菜单提示先配置，不执行。
- **客户端管理**（`/oracle` → 14）：显示 rbot-clone-web / rbot-clone-bot 的
  `systemctl is-active` 状态，可重启任一（二次确认）。
- **重启机器人服务**（`/oracle` → 31）：二次确认；先回复“正在重启”再
  `systemctl restart rbot-clone-bot`。
- **升级客户端**（`/oracle` → 32）：二次确认 + 提示“约 1 分钟不可用”；
  git pull --ff-only + pip install -r requirements.txt + 先重启面板再重启
  机器人；非 git 仓库时提示手动升级。

## E 包：深度研究新增缺口（150 测试）

新文件：`web/routes/monitor.py`、`web/routes/cloudreport.py`、
`web/routes/batch.py`、`web/routes/favorites.py`、`web/routes/cert.py`、
`web/routes/inspect.py`、`web/routes/settings.py`、`web/captcha.py`、
`web/telegram_notify.py`、`scripts/deploy.sh`、`docs/deployment.md`、
`docs/phase5-todo.md` + 10 个测试文件。

- **API 响应形状对齐**：实例列表 `{id, name, publicIp, shape, state, ocpus,
  memoryInGBs, region, timeCreated}`（只含 RUNNING）；磁盘列表
  `{volumeId, attachmentId, displayName, sizeInGBs, vpusPerGB,
  lifecycleState, instanceId, instanceName, attached, attachmentState,
  availabilityDomain, timeCreated}`；A1 配额 `{capOcpu, capMem, usedOcpu,
  usedMem, over, overOcpu, overMem, instances}`（cap 用免费上限 4/24）；
  健康检查 `GET /api/system/health`（/proc 实现，psutil 未安装）；
  监控规则 `GET /api/monitor/rules`（`{monitorEnabled, autoOpenEnabled,
  rules: []}`）。前端 JS 已同步新字段；新旧双写过渡。
  ⚠️ 实例列表现为 RUNNING-only（按规范），STOPPED 实例不再出现在实例页，
  无法从面板列表里启动已停实例；建议后续加 `?state=all` 参数，本包未擅自偏离规范。
- **SFTP 分块上传**：init/chunk/progress/complete/cancel（1MB 分块，
  `DATA_DIR/sftp_uploads/{uploadId}/`，complete 后清理，24h 旧分块清理）；
  终端页上传 >1MB 文件自动分块，带进度条 + 取消。双前缀注册
  （`/api/ssh/sessions/{id}/sftp/upload/*` 与 `/api/terminal/{id}/sftp/upload/*` 别名）。
- **批量命令**：终端页「批量命令」按钮；`POST /api/batch/exec`、
  `GET /api/batch/{id}`、`POST /api/batch/{id}/cancel`；复用 B 包危险命令黑名单。
- **终端收藏夹**：终端页「收藏」侧栏；`GET/POST/DELETE /api/term-favorites`；
  存 `DATA_DIR/term_favorites.json`（`{version: 1, items: [...]}`）。
- **证书管理**：侧边栏「证书管理」→ `/cert`；`GET /api/cert/status`
  （`{acmeEnabled, domain, issuer, notBefore, notAfter, remainingHours,
  status}`，无证书时 `status="none"`）、`POST /api/cert/renew`（异步任务）。
- **巡检计划**：侧边栏「巡检计划」→ `/inspect`；`GET/POST/DELETE
  /api/inspect-schedules`；调度器默认关闭，需 `RBOT_ENABLE_INSPECT_SCHEDULER=1`；
  手动「立即巡检」不受影响；巡检摘要经 Telegram 推送。
- **云报告**：概览页云报告卡片；`GET /api/cloud-report/summary`
  （`{groups: [...], generatedAtEpochSec}`）。
- **图形验证码登录**：`RBOT_CAPTCHA_ENABLED=1` 时登录页显示（默认关闭）；
  `GET /api/auth/captcha`、`POST /api/auth/login`（带 captcha_id/captcha_answer）；
  Pillow 未安装时 SVG 字符画兜底。
- **Telegram 验证码登录**：登录页「Telegram 验证码登录」页签；
  `POST /api/auth/telegram/send-code` / `/verify`；6 位码、5 分钟过期、
  一次性、60 秒发码冷却；验证码绝不写日志。
- **OCI 代理**：仅 API，无前端页；`GET/POST /api/settings/oci-proxy`；
  环境变量 `RBOT_OCI_PROXY` 优先于 `oci_proxy.json` 文件；只注入 OCI CLI
  子进程 env，不改全局。
- **部署脚本**：`scripts/deploy.sh`（`bash -n` 通过，全占位符）：
  systemd `Restart=always` + `RestartSec=10`；原子升级；`flock` 升级锁；
  端口解析（参数 > 进程命令行 > 文件 > 默认 9527）；
  `install|upgrade|rollback|start|stop|restart|status|health`。
  `docs/deployment.md` 注明未借鉴的点：自动开安全组端口、BusyBox/OpenWrt
  兼容、架构分包、配置加密。
- **标注待做**（`docs/phase5-todo.md`）：主机按 IP 解析会话
  （`/host/sessions/resolve-by-ip`）、终端粘贴图片（`/terminal/paste-image`）。

## /oracle 菜单现状（42 项）

原 34 项中已实现 31 项：
- 本阶段新实现：11 开放云面板端口、14 客户端管理、23 占用 25% 内存、
  24 清除缓存和占用、31 重启、32 升级客户端。
- 新增菜单项：35 批量操作、36 A1 配置体检、37 流量守护、38 停机保活、
  39 SSL 监控、40 对象存储、41 Email 发件人、42 抢机计划。
- 仍未实现（按设计）：16 监控（自动换 IP，需用户定故障判定规则）、
  27 一键禁用被封账户（不可逆，建议人工）、28 删除当前 API（故意不做，机器人自杀）。
- 闪电权限体系跳过；IAM 用户管理、用量统计、profiles 管理在克隆版免费开放，不做权限门。

## 待主代理真实验证的功能清单

全部功能均需在新建的测试实例上真实验证（用户要求）。入口路径见各包小节。
部署前需填的占位符：
- `PANEL_SECURITY_LIST_ID`（开放云面板端口，tgbot/handlers_extra.py）
- `PLAN_DEFAULTS`（抢机计划，web/tasks.py：可用域/镜像/子网/SSH 公钥文件）
- 调度器开关：`RBOT_ENABLE_PLAN_SCHEDULER=1`（抢机计划）、
  `RBOT_ENABLE_INSPECT_SCHEDULER=1`（巡检计划）
- 图形验证码开关：`RBOT_CAPTCHA_ENABLED=1`（默认关闭）
- `/help` 清单需补四个新命令（/command /pubkey /oproxy /uploadapi）
- 建议后续：实例列表加 `?state=all` 参数（否则 STOPPED 实例无法从面板启动）

## 主代理部署修复（2026-11-23 晚，部署 部署服务器 时发现并修复）

1. **实例列表 `?state=all` 已加**（web/routes/instances.py）：默认仍 RUNNING-only；
   `?state=all` 返回全部非 TERMINATED 实例（STOPPED 可从面板启动）。
   新增 1 个测试，全量 1249 全过。
2. **/help 已补四个新命令**（tgbot/handlers.py）：/command /pubkey /oproxy /uploadapi。
3. **部署配置全部改环境变量**（此前 rsync 覆盖了 部署服务器 上已填真实值的配置，
   导致面板报 Invalid compartmentId 502）：
   - web/config.py 与 tgbot/config.py 的 PROFILES：RBOT_COMPARTMENT_DEFAULT /
     RBOT_COMPARTMENT_ACCOUNT2 / RBOT_COMPARTMENT_ACCOUNT3（未设的账号不加入）
   - web/config.py 的 IMAGE_IDS：RBOT_IMAGE_AARCH64 / RBOT_IMAGE_X86_64
   - DATA_DIR：RBOT_WEB_DATA_DIR / RBOT_TGBOT_DATA_DIR
   - tgbot/handlers_extra.py 的 PANEL_SECURITY_LIST_ID：RBOT_PANEL_SECURITY_LIST_ID
   - web/tasks.py 的 PLAN_DEFAULTS：RBOT_PLAN_AD / RBOT_PLAN_IMAGE_ID /
     RBOT_PLAN_SUBNET_ID / RBOT_PLAN_SSH_KEYS_FILE
   - 部署服务器 的 /root/.rbot-clone.env 已填全部 16 个 RBOT_ 变量；源码保持占位符脱敏。
4. **rsync 教训**：同步部署时不要用 --delete（会删掉服务器上的 data/ 运行时数据，
   本次误删 web/data/known_hosts，已重建目录；以后同步加 --exclude web/data --exclude tgbot/data）。
5. **部署冒烟（2026-11-23 22:3x，部署服务器 真实数据）**：登录 200；实例列表 200（3 台 RUNNING）；
   实例列表 ?state=all 200；配额 200（A1 2/4 核 12/24GB）；启动盘 200（3×47GB=141GB）；
   健康检查 200（4 核/23.4GB）；监控规则 200；云报告 200（4 台含 1 台 TERMINATED）；
   证书状态 200；对象存储/Email/证书/巡检 4 个新页面均 200。
6. **机器人部署后故障（2026-11-23 22:48 用户截图发现，主代理漏验证）**：
   机器人报 `[Errno 2] No such file or directory: '/root/oci-venv/bin/oci'`。
   根因：rbot-clone-bot 的 systemd 单元没有设 PATH（web 单元有），
   RBOT_OCI_CLI 也未设置，shutil.which("oci") 返回 None 后回退到不存在的旧路径。
   修复：env 文件加 RBOT_OCI_CLI=/opt/rbot-clone/venv/bin/oci；
   bot 单元补 PATH 与 SUPPRESS_LABEL_WARNING=True（与 web 单元一致）。
   教训：部署后必须 Telegram 端到端实测，不能只测 Web 面板。
   修复后 /instances 实测通过（5 台：3 RUNNING + 2 TERMINATED）。
7. **串行控制台 bug（真实验证挖出，2026-11-23 22:5x）**：
   `get_serial_console` 未传 --ssh-public-key-file，OCI CLI 报 Missing option。
   修复：加 ssh_public_key_file 参数（环境变量 RBOT_CONSOLE_SSH_KEY_FILE
   或 ~/.ssh/id_ed25519.pub 兜底，文件不存在抛 ValueError）。
   另发现 OCI 串行控制台**只认 RSA 密钥，ed25519 报 Invalid ssh public key type**。
   部署服务器 上已生成专用 RSA 密钥对（/opt/rbot-clone/web/data/console_rsa，
   私钥 600，公钥 console_rsa.pub），env 已指向它；用 新加坡服务器 实测：
   创建连接成功（状态 CREATING，返回连接串与指纹），随后删除清理。
   注意：用户要实际连串行控制台需从 部署服务器 上用该 RSA 私钥连
   （连接串是 ssh -o ProxyCommand 走 443 端口）。
