# 部署文档（阶段 5 工作包 E）

M_Bot Web 面板的部署步骤、R-Bot 管理脚本借鉴点与回滚方法。

部署脚本模板：`scripts/deploy.sh`（占位符，部署前按实际修改顶部配置区）。

## 部署步骤

### 1. 准备服务器

- 一台 Linux 服务器（Debian/Ubuntu，Python 3.10+，已装 git）。
- 面板只监听 127.0.0.1，不要直接把 9527 暴露到公网；外网访问用 SSH 隧道
  或在前面加反向代理 + HTTPS。
- OCI CLI：面板调 OCI 时走官方 OCI CLI（subprocess），服务器上需装好
  `oci` 且 `~/.oci/config` 里有对应 profile（认证由 OCI CLI 自己处理，
  面板不读私钥）。

### 2. 生成管理员密码哈希与环境文件

```bash
cd /opt/rbot-clone        # 安装目录（见 deploy.sh 配置区 INSTALL_DIR）
python3 -m web.auth       # 交互输入密码，打印哈希（只显示一次）
```

把哈希与会话密钥写进环境文件（600 权限，systemd 经 EnvironmentFile 读入）：

```bash
cat > /root/.rbot-clone.env <<'EOF'
RBOT_WEB_PASSWORD_HASH='<上一步生成的哈希>'
RBOT_WEB_SECRET='<随机长字符串，如 openssl rand -hex 32 的输出>'
EOF
chmod 600 /root/.rbot-clone.env
```

可选环境变量（同一文件里加）：

| 变量 | 说明 |
| --- | --- |
| `RBOT_WEB_USER` | 管理员用户名，默认 admin |
| `RBOT_CAPTCHA_ENABLED=1` | 开启登录图形验证码 |
| `RBOT_TELEGRAM_TOKEN` | Telegram 机器人 Token（Telegram 验证码登录与巡检通知用） |
| `RBOT_ALLOWED_USERS` | 白名单 Telegram 用户 ID（逗号分隔） |
| `RBOT_OCI_PROXY` | OCI CLI 代理（如 `http://127.0.0.1:7890`），优先级高于面板设置 |
| `RBOT_ENABLE_PLAN_SCHEDULER=1` | 开启抢机计划调度器 |
| `RBOT_ENABLE_INSPECT_SCHEDULER=1` | 开启巡检计划调度器 |
| `RBOT_ACME_HOME` | acme.sh 家目录，默认 `~/.acme.sh` |
| `RBOT_CERT_DOMAIN` | 证书页默认域名（不设时自动检测） |

### 3. 首次部署

```bash
# 先改 scripts/deploy.sh 顶部的配置区：INSTALL_DIR / REPO_URL / SERVICE_USER
./scripts/deploy.sh install [--port=xxxx]
```

install 做的事：克隆代码 → 建 venv 装依赖 → 检查环境文件 →
写 systemd 单元 → 启动服务。端口不传时用默认 9527。

### 4. 访问

```bash
ssh -L 9527:127.0.0.1:9527 <用户>@<面板服务器 IP>
# 然后打开 http://127.0.0.1:9527/login
```

## 从 R-Bot 管理脚本借鉴的点

来源：`docs/rbot-sh_client_bot.sh`（R-Bot 的 sh_client_bot.sh，1026 行）
与 `docs/rbot-live.md`「深度研究」一节。

| 借鉴点 | R-Bot 的做法 | 克隆版的做法（deploy.sh） |
| --- | --- | --- |
| 自动拉起 | systemd `Restart=always` + `RestartSec=10` | 同款（`write_systemd_unit`） |
| 原子升级 | 下载到脚本目录内临时目录（同文件系统 mv 才原子，避开路由器 /tmp 空间不足），校验二进制存在，配置文件不存在才从包内拷贝，mv 覆盖 | `cmd_upgrade`：`mktemp -d` 在安装目录内建临时目录 → git clone → 校验关键文件 → 备份当前版 → 停服务 → mv 覆盖 → 装依赖 → 起服务 |
| 升级锁 | `.r_client.upgrading.lock`（flock 优先，否则时间戳软锁 54 秒新鲜度） | `flock -n` 非阻塞锁（拿不到锁直接退出，不做软锁：克隆版只跑在标准 Linux 上，flock 可用） |
| 端口解析优先级 | Java 传入 > 进程命令行 `--server.port` > 监听探测 > `.r_client.port` 文件 > 默认 9527 | 参数 `--port=` > 进程命令行（uvicorn --port）> 端口文件 `.port` > 默认 9527（`resolve_port`） |
| 健康检查 | `GET https://127.0.0.1:9527/.../health`（curl -skf） | `health_check`：服务在跑 + 端口在监听（进程级；面板 `/api/system/health` 需登录会话，接口级检查请登录后手动调） |
| 备份与回滚 | 升级前配置文件不存在才拷贝（保留旧配置） | 升级前把当前代码拷到 `.backup/`（不备 `web/data` 与 venv），`rollback` 子命令恢复 |

没借鉴的点（有意不做）：

- 启动时自动在 OCI 安全组开放面板端口——克隆版不做，需用户明确授权才动安全组。
- BusyBox/OpenWrt 兼容（nohup shim、uclient-fetch）——克隆版只支持标准 Linux。
- 架构分包（mac_aarch/aarch/x86）——克隆版是 Python 源码部署，无二进制分包问题。
- 配置加密（client_config.key）——克隆版凭据放环境文件（600 权限），不做应用层加密。

## 升级与回滚

```bash
./scripts/deploy.sh upgrade [--port=xxxx]   # 原子升级（先备份，失败停在校验步不动线上版）
./scripts/deploy.sh rollback                # 回滚到上一次备份的版本
./scripts/deploy.sh health                  # 健康检查（服务 + 端口监听）
```

升级流程细节：

1. 拿升级锁（flock，非阻塞；已有升级在进行时直接退出）。
2. 新版 clone 到安装目录内的临时目录（同文件系统，mv 原子）。
3. 校验关键文件（`web/app.py`、`requirements.txt`、`web/config.py`）齐全，
   缺了就停，不动线上版本。
4. 备份当前版本到 `.backup/`（只备代码，不备 `web/data/` 运行时数据与 venv）。
5. 停服务 → mv 覆盖 → 装依赖 → 起服务 → 健康检查。
6. 健康检查失败时打印日志查看命令与回滚命令，不自动回滚
   （避免“升级失败 → 自动回滚 → 回滚也失败”的连锁误判，人工确认后再回滚）。

回滚：停服务 → 把 `.backup/` 拷回 → 起服务 → 健康检查。
注意回滚不恢复 `web/data/`（运行时数据升级时本来就没动过）。

## 日志与常用命令

```bash
systemctl status rbot-clone-web
journalctl -u rbot-clone-web -f
./scripts/deploy.sh restart
./scripts/deploy.sh status
```

改密码后需重启服务才生效（运行中的进程读的是启动时加载的哈希）。
改 OCI 代理设置不用重启（每次 OCI 调用时重新解析，立即生效）。
