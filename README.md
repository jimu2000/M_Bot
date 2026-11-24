# M_Bot Oracle开源管理面板

> **当前版本：v1.12.0** · 更新记录见 [CHANGELOG.md](CHANGELOG.md)（[英文版 / English](CHANGELOG.en.md)）

[中文](#中文) | [English](#english)

---

## 中文

M_Bot 是一个开源的 Oracle Cloud 管理面板：Telegram 机器人 + Web 面板 + Web SSH +
抢机 + Cloudflare DNS + MCP 服务器。功能设计参考了 R-Bot（R 探长）的公开功能
说明，代码全部独立编写、全部可见、可审计。

> ⚠️ 免责：本项目与 R-Bot / semicons 无任何关联。R-Bot 是其作者的产品，
> 而且相对成熟；本项目是按公开功能说明独立实现的开源项目，代码全部
> 独立编写，与 R-Bot 不共享代码。

### 功能

**OCI 核心（`oci/`，调官方 OCI CLI，不用 SDK）**
- 实例：列表 / 详情 / 创建 / 启动 / 停止 / 重启 / 终止（默认同时删除引导卷，
  防扣费）/ 改名 / 升降配 / 重装系统 / 串行控制台
- 批量操作：批量启动 / 停止 / 重启 / 删除
- 网络：换公网 IP、保留 IP 管理、IPv6 创建 / 删除 / 列表
- 磁盘：启动盘列表 / 扩容 / 删除 / 孤儿盘清理 / VPU 修改
- 配额：免费额度汇总、A1 配置体检、开机前配额预检（`can_launch`）
- 对象存储：Bucket 创建 / 删除 / 对象上传 / 下载 / 列表
- 邮件服务：Email Delivery 发件人管理
- 抢机（`grabber.py`）：`--no-retry` 快速失败、随机抖动间隔、429 指数退避、
  配额不足检测（停机并通知，不无限重试）、CPU 型号筛选（如只留 AMD EPYC 7J13）

**Telegram 机器人（`tgbot/`，python-telegram-bot）**
- `/oracle` 菜单 42 项：开机抢机、IP 管理、实例管理、磁盘管理、云账户、
  监控通知、配额、流量、费用、批量操作、A1 体检、流量守护、停机保活、
  SSL 到期提醒、对象存储、Email 发件人、抢机计划……
- GFW 自动换 IP（菜单 16）：每 30 分钟从中国大陆探针（RIPE Atlas）
  ping 已开启的实例公网 IP，被墙自动换 IP 再检测，直到换到可用的为止；
  每次换 IP 发双通道通知（TG + Bark，没设 Bark 就只发 TG）
- 开机默认生成 UUID root 密码（32 位十六进制），确认页就显示给用户，
  抢机成功后 Telegram + Bark 双通道通知里带密码（网页版开机同样双通道
  通知，提交后弹窗显示一次密码）
- Bark 通知设置（菜单 43 / `/bark` 命令 / 面板“Bark 通知”页，三端共用
  同一份配置）：AES-256-CBC 加密推送，地址 + 32 位 Key + 测试推送 + 开关；
  Key 可一键自动生成（32 位随机 hex），生成后附二维码，Bark App 扫码添加
- `/domain`：Cloudflare Zone 与 DNS 记录查询
- 客户端工具：`/command` 远程 Shell（危险命令黑名单）、`/pubkey` 公钥管理、
  `/oproxy` 代理设置、`/uploadapi` 上传 OCI 配置
- 白名单硬规则：非白名单用户任何命令直接拒绝；定时任务：实例监控、
  每日报告、SSL 到期检查

**Web 面板（`web/`，FastAPI + WebSocket + Paramiko + xterm.js）**
- 登录（pbkdf2 哈希、失败锁定翻倍最长 30 分钟、图形验证码可选、
  Telegram 验证码登录可选）
- 页面：总览 / 实例 / 创建实例 / 配额 / 启动盘 / 任务 / GFW 换 IP / Bark 通知 /
  终端 / DNS 管理 / 对象存储 / 邮件服务 / 证书管理 / 巡检计划 / 云报告
- Web SSH：多标签、断线重连 + 快照回放、会话挂起、空闲清理
- SFTP 文件面板：列表 / 下载 / 上传（分块上传带进度）/ 新建目录 / 删除 /
  重命名 / 在线编辑，三层路径穿越防护
- 批量命令、终端收藏夹、抢机计划（cron 调度）、OCI 代理设置

**Cloudflare DNS（`clouds/cloudflare.py`）**
- Zone 列表、DNS 记录增删改查、代理开关、SSL 证书到期检查

**MCP 服务器（`mcp_server.py`，标准库 JSON-RPC over stdio，零新增依赖）**
- 6 个工具：列实例、实例详情、配额、免费额度汇总、列 Zone、列 DNS 记录

**其他云**：`clouds/` 下有 AWS / GCP / Azure / DigitalOcean / SolusVM 的
抽象接口与 stub（待实现，需对应云账号才能真实验证）。

### GFW 自动换 IP（菜单 16，v1.10.0 新增）

代理服务器的公网 IP 被墙后，国内客户端就连不上了。这个功能自动盯着它：

- **检测**：每 30 分钟用 [RIPE Atlas](https://atlas.ripe.net) 从中国大陆
  探针（电信/联通/移动混合，5 个探针各发 3 个 ping 包）ping 已开启的实例
  公网 IP；上报探针少于 3 个时结论为“未知”，不换 IP；多数探针不通即判定被墙。
- **换 IP**：被墙就自动换 IP（先删旧临时 IP 再绑新保留 IP），换完重新检测；
  新 IP 还是被墙就继续换；单轮最多换 5 次，换满仍被墙就暂停，等下个周期。
- **通知**：每次换 IP 都发双通道通知（Telegram + Bark）；Bark 是可选项，
  没设置就只发 Telegram。本来就正常时不发通知，不打扰。
- **开关**：逐台开启/关闭（机器人菜单 16 或面板“GFW 换 IP”页，两边共用配置）；
  也可以点“立即检查”马上测一次。

设置 RIPE Atlas API Key（一次，免费）：

1. 在 [atlas.ripe.net](https://atlas.ripe.net) 注册账号（RIPE NCC Access 账号）；
2. 在 My Credits 页面点 **Claim 50,000 free credits** 领 5 万免费积分
   （一次检测约几十积分，够用几个月；长期用可以在自己服务器上跑一个
   RIPE Atlas 软件探针，每天赚约 2 万积分）；
3. 在 [Keys 页面](https://atlas.ripe.net/keys/) 创建一个
   “create a new user-defined measurement” 类型的 Key；
4. 把 Key 发给机器人（菜单 16 → 设置 API Key）或填到面板“GFW 换 IP”页。
   Key 只存服务器本机（`tgbot/data/gfw.json`，0600），不回显、不记日志。

为什么选 RIPE Atlas：itdog 自 2026-10 起任务端点强制点选人机验证，
无人值守的定时监控没法用；17CE / BOCE 的官方 API 要账号 + 付费积分；
Atlas 是 RIPE NCC 的正规免费服务，有稳定 REST API。注意：ping 不通
不等于百分百被墙（也可能是线路问题），这里的结论是“疑似被墙”；
实例本身必须回 ping（ICMP 没被安全组禁掉），否则检测永远是“被墙”。

### 快速开始

```bash
git clone https://github.com/jimu2000/M_Bot.git
cd M_Bot
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
pip install oci-cli          # OCI 调用走官方 CLI
```

**1. 配 OCI 认证**（三种方式任选其一，推荐第一种）：

```bash
# 方式一：oci setup config 交互生成（推荐）
oci setup config
# 方式二：手写 ~/.oci/config，填 user / fingerprint / tenancy / region / key_file
```

API 私钥（key_file）说明：私钥文件是 OCI 控制台生成的 `.pem` 文件，下载后放到
服务器上，例如 `/root/1.pem`（路径可自定），权限设 600。
获取方法：OCI 控制台 → 右上角用户头像 → My Profile → API Keys →
Add API Key → Generate API Key Pair → 下载私钥与配置文件片段。
也可以用命令生成：`openssl genrsa -out /root/1.pem 2048`，再把公钥贴到控制台添加。

**2. 配环境变量**（部署值一律走环境变量，不写进代码）：

```bash
# 必填：根 compartment（即 tenancy OCID），多账号时各填一个
export RBOT_COMPARTMENT_DEFAULT="ocid1.tenancy.oc1..xxxxxxxx"
export RBOT_COMPARTMENT_ACCOUNT2="ocid1.tenancy.oc1..xxxxxxxx"   # 可选
export RBOT_COMPARTMENT_ACCOUNT3="ocid1.tenancy.oc1..xxxxxxxx"   # 可选
# 镜像 OCID（创建实例用，各区域不同，用 oci compute image list 查）
export RBOT_IMAGE_AARCH64="ocid1.image.oc1..xxxxxxxx"
export RBOT_IMAGE_X86_64="ocid1.image.oc1..xxxxxxxx"
# Telegram 机器人（用机器人时填）
export RBOT_TELEGRAM_TOKEN="123456789:xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
export RBOT_ALLOWED_USERS="123456789"   # 你的 Telegram 用户 ID，逗号分隔多个
# Web 面板（用面板时填）
export RBOT_WEB_PASSWORD_HASH="$(python3 -c "import hashlib,os;print(hashlib.pbkdf2_hmac('sha256',b'你的密码',os.urandom(16),100000).hex())")"
export RBOT_WEB_SECRET="$(openssl rand -hex 32)"
# 可选：OCI CLI 路径、Cloudflare 凭据、串行控制台 SSH 公钥
export RBOT_OCI_CLI="/opt/rbot-clone/venv/bin/oci"
export CF_API_TOKEN="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
export RBOT_CONSOLE_SSH_KEY_FILE="/opt/rbot-clone/web/data/console_rsa.pub"
```

**3. 启动**：

```bash
# Telegram 机器人
python3 -m tgbot.bot
# Web 面板（默认只绑 127.0.0.1:9527，用 SSH 隧道访问）
uvicorn web.app:app --host 127.0.0.1 --port 9527
# MCP 服务器（stdio，供 Claude Desktop 等客户端调用）
python3 mcp_server.py
```

生产部署建议用 systemd 托管两个服务，见 `docs/deployment.md`
（含原子升级脚本 `scripts/deploy.sh`）。

### 配置规范

- **配置区在文件顶端**：每个模块的常量、阈值、路径都在文件开头的配置区，
  改配置不用翻代码。
- **部署值走环境变量**：compartment、镜像 OCID、子网、安全组、Token、密码哈希
  一律从环境变量注入，源码里只有占位符（如 `ocid1.tenancy.oc1..xxxxxxxx`）。
- **私钥只在本地用**：OCI 私钥只交给官方 OCI CLI 做本地请求签名，代码不读、
  不打印、不外传。

### 测试

```bash
python3 -m unittest discover -s tests -t .
# Ran 1318 tests ... OK
```

1318 个单元测试（mock 为主，写操作零次碰生产）。真实 API 验证记录见
`docs/verification*.md`：只读操作对真实 OCI / Cloudflare API 实测通过；
写操作（创建 / 换 IP / 升降配 / 扩容 / 释放等）在测试实例上真实跑通过。

### 项目结构

```
M_Bot/
  oci/          OCI 客户端（client/instance/network/volume/quota/grabber/email/objectstorage）
  tgbot/        Telegram 机器人（bot/handlers/handlers_extra/keyboards/services/monitoring）
  web/          Web 面板（FastAPI 路由、Web SSH、SFTP、任务调度）
  clouds/       Cloudflare 客户端 + 其他云抽象接口与 stub
  mcp_server.py MCP 服务器（JSON-RPC over stdio）
  scripts/      部署脚本（原子升级）
  tests/        单元测试（1318 个）
  docs/         设计与验证文档
```

### 明确不做的功能

- 付费权益体系（充值、权益开关）——对应功能（IAM 用户管理、用量统计、
  多 profile 管理等）全部免费开放
- 删除当前 API（等于机器人自杀）
- 禁用被封账户（不可逆操作，建议人工处理）

---

## English

M_Bot is an open-source Oracle Cloud management panel written in Python:
Telegram bot + web panel + web SSH + instance grabber + Cloudflare DNS + MCP
server. Its feature design references [R-Bot](https://github.com/semicons/java_oci_manage)'s
(R 探长) public feature documentation; all code here is independently written,
fully visible and auditable.

> ⚠️ Disclaimer: This project is not affiliated with R-Bot / semicons
> in any way. R-Bot is its author's product, and a relatively mature one;
> this project is an independent open-source implementation based on its
> public feature documentation, written from scratch with no shared code.

### Features

**OCI core (`oci/`, drives the official OCI CLI, no SDK)**
- Instances: list / detail / create / start / stop / reboot / terminate
  (boot volume deleted by default, so nothing keeps billing) / rename /
  resize / reinstall / serial console
- Batch operations: batch start / stop / reboot / terminate
- Networking: change public IP, reserved IP management, IPv6 create / delete / list
- Volumes: boot volume list / resize / delete / orphan cleanup / VPU change
- Quotas: free-tier summary, A1 shape health check, pre-launch quota check
- Object Storage: bucket create / delete / object upload / download / list
- Email Delivery: sender management
- Grabber (`grabber.py`): `--no-retry` fast fail, jittered intervals,
  exponential backoff on 429, quota-exhaustion detection (stops and notifies
  instead of retrying forever), CPU model filtering (e.g. keep only AMD EPYC 7J13)

**Telegram bot (`tgbot/`, python-telegram-bot)**
- `/oracle` menu with 42 items: launch/grab, IP management, instance
  management, disk management, cloud accounts, monitoring, quotas, traffic,
  costs, batch operations, A1 health check, traffic guard, keep-alive,
  SSL expiry alerts, object storage, email senders, grabber plans…
- GFW auto IP change (menu 16): every 30 minutes, pings enabled instances'
  public IPs from mainland China probes (RIPE Atlas); if blocked, changes
  the IP and re-checks until one works; notifies on every change via
  Telegram + Bark (Bark optional — Telegram only if not configured)
- Boot generates a UUID root password by default (32 hex chars), shown on
  the confirmation page and included in the Telegram + Bark dual-channel
  success notification (web-panel launches notify the same way, and the
  password is shown once in a dialog after submit)
- Bark notification settings (menu 43 / `/bark` command / panel "Bark"
  page, all sharing one config): AES-256-CBC encrypted push, with
  address + 32-char key + test push + on/off switch; the key can be
  auto-generated (random 32-char hex) with a QR code the Bark app scans
- `/domain`: Cloudflare zone and DNS record lookup
- Client tools: `/command` remote shell (dangerous-command blocklist),
  `/pubkey` key management, `/oproxy` proxy settings, `/uploadapi` OCI config upload
- Strict allowlist: non-allowlisted users are rejected on every command;
  scheduled jobs: instance monitoring, daily report, SSL expiry checks

**Web panel (`web/`, FastAPI + WebSocket + Paramiko + xterm.js)**
- Login (pbkdf2 hashing, doubling lockout up to 30 minutes, optional image
  captcha, optional Telegram code login)
- Pages: overview / instances / create / quota / boot volumes / tasks /
  GFW IP change / Bark notifications / terminal / DNS / object storage /
  email / certificates / inspection plans / cloud report
- Web SSH: multi-tab, reconnect with snapshot replay, session suspend, idle cleanup
- SFTP file panel: list / download / chunked upload with progress / mkdir /
  delete / rename / online edit, three-layer path traversal protection
- Batch commands, terminal favorites, grabber plans (cron scheduling), OCI proxy settings

**Cloudflare DNS (`clouds/cloudflare.py`)**
- List zones, DNS record CRUD, proxy toggle, SSL certificate expiry checks

**MCP server (`mcp_server.py`, stdlib JSON-RPC over stdio, zero new dependencies)**
- 6 tools: list instances, instance detail, quota, free-tier summary,
  list zones, list DNS records

**Other clouds**: `clouds/` has an abstract interface plus stubs for
AWS / GCP / Azure / DigitalOcean / SolusVM (not implemented yet — real
verification needs accounts on those clouds).

### GFW auto IP change (menu 16, new in v1.10.0)

When a proxy server's public IP gets blocked from mainland China, clients
there can't connect. This feature watches for that:

- **Check**: every 30 minutes, pings enabled instances' public IPs from
  mainland China probes via [RIPE Atlas](https://atlas.ripe.net)
  (5 probes, 3 packets each, mixed telecom/unicom/mobile). Fewer than 3
  reporting probes means "unknown" — no action. A majority failing means
  blocked.
- **Change**: on blocked, changes the public IP (delete the old ephemeral
  IP, attach a new reserved one) and re-checks; keeps changing while the
  new IP is still blocked; at most 5 changes per cycle, then pauses until
  the next cycle.
- **Notify**: every IP change sends a dual notification (Telegram + Bark;
  Bark is optional — Telegram only if not configured). Healthy checks stay
  silent.
- **Toggle**: per-instance on/off from menu 16 in the bot or the
  "GFW 换 IP" page in the web panel (shared config).

One-time RIPE Atlas API key setup (free):

1. Register at [atlas.ripe.net](https://atlas.ripe.net) (RIPE NCC Access account);
2. Click **Claim 50,000 free credits** on the My Credits page (one check
   costs tens of credits; for long-term use, run a RIPE Atlas software
   probe on your own server to earn ~21,600 credits per day);
3. Create a key of type "create a new user-defined measurement" on the
   [Keys page](https://atlas.ripe.net/keys/);
4. Send the key to the bot (menu 16 → Set API Key) or paste it on the
   web panel's "GFW 换 IP" page. The key is stored only on the server
   (`tgbot/data/gfw.json`, 0600), never echoed or logged.

Why RIPE Atlas: itdog has forced click-captcha verification on its task
endpoints since 2026-10 (unusable for unattended monitoring), and 17CE /
BOCE require paid credits; Atlas is a free service from RIPE NCC with a
stable REST API. Note that ping failing is evidence of "suspected"
blocking, not proof, and the instance itself must answer ICMP (not
blocked by its security list), otherwise every check reports blocked.

### Quick start

```bash
git clone https://github.com/jimu2000/M_Bot.git
cd M_Bot
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
pip install oci-cli          # OCI calls go through the official CLI
```

**1. OCI authentication** (pick one, first recommended):

```bash
# Option 1: interactive setup (recommended)
oci setup config
# Option 2: write ~/.oci/config by hand (user / fingerprint / tenancy / region / key_file)
```

API private key (`key_file`): the `.pem` file generated in the OCI console.
Download it to your server, e.g. `/root/1.pem` (any path works), `chmod 600`.
How to get it: OCI console → user avatar (top right) → My Profile → API Keys →
Add API Key → Generate API Key Pair → download the private key and config snippet.
Or generate locally: `openssl genrsa -out /root/1.pem 2048`, then paste the
public key into the console.

**2. Environment variables** (all deployment values come from env vars, never code):

```bash
# Required: root compartment (i.e. tenancy OCID); one per account
export RBOT_COMPARTMENT_DEFAULT="ocid1.tenancy.oc1..xxxxxxxx"
export RBOT_COMPARTMENT_ACCOUNT2="ocid1.tenancy.oc1..xxxxxxxx"   # optional
export RBOT_COMPARTMENT_ACCOUNT3="ocid1.tenancy.oc1..xxxxxxxx"   # optional
# Image OCIDs (for instance creation; differ per region, see oci compute image list)
export RBOT_IMAGE_AARCH64="ocid1.image.oc1..xxxxxxxx"
export RBOT_IMAGE_X86_64="ocid1.image.oc1..xxxxxxxx"
# Telegram bot (if you use it)
export RBOT_TELEGRAM_TOKEN="123456789:xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
export RBOT_ALLOWED_USERS="123456789"   # your Telegram user ID, comma-separated
# Web panel (if you use it)
export RBOT_WEB_PASSWORD_HASH="$(python3 -c "import hashlib,os;print(hashlib.pbkdf2_hmac('sha256',b'your-password',os.urandom(16),100000).hex())")"
export RBOT_WEB_SECRET="$(openssl rand -hex 32)"
# Optional: OCI CLI path, Cloudflare credentials, serial console SSH public key
export RBOT_OCI_CLI="/opt/rbot-clone/venv/bin/oci"
export CF_API_TOKEN="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
export RBOT_CONSOLE_SSH_KEY_FILE="/opt/rbot-clone/web/data/console_rsa.pub"
```

**3. Run**:

```bash
# Telegram bot
python3 -m tgbot.bot
# Web panel (binds 127.0.0.1:9527 only; reach it via SSH tunnel)
uvicorn web.app:app --host 127.0.0.1 --port 9527
# MCP server (stdio, for Claude Desktop and similar clients)
python3 mcp_server.py
```

For production, run both services under systemd — see `docs/deployment.md`
(includes the atomic upgrade script `scripts/deploy.sh`).

### Configuration rules

- **Config block at the top of each file**: constants, thresholds and paths live
  in the config section at the head of every module — no digging through code.
- **Deployment values via environment variables**: compartments, image OCIDs,
  subnets, security lists, tokens and password hashes are all injected from
  env vars; the source only contains placeholders (e.g. `ocid1.tenancy.oc1..xxxxxxxx`).
- **Private keys stay local**: the OCI private key is only handed to the
  official OCI CLI for local request signing; the code never reads, prints
  or transmits it.

### Tests

```bash
python3 -m unittest discover -s tests -t .
# Ran 1318 tests ... OK
```

1318 unit tests (mostly mocked; write operations never touch production).
Real-API verification notes live in `docs/verification*.md`: read-only calls
verified against the real OCI / Cloudflare APIs; write operations (create /
change IP / resize / extend / terminate) verified for real on test instances.

### Deliberately not implemented

- A paid privileges system (payments, feature gates) — the equivalent features
  are free for everyone here
- Deleting the current API key (would kill the bot itself)
- Disabling banned accounts (irreversible; do it manually)

## License

MIT — see [LICENSE](LICENSE).
