# 部署验证记录：Web 面板部署

验证时间：2026-11-23（UTC 约 09:30–09:40）。验证人：部署协调员。

## 部署结果

- **部署成功**。Web 面板跑在 部署服务器（圣何塞，A1.Flex 4/24GB）上，
  systemd 服务 `rbot-clone-web` 常驻，`active (running)`，内存约 51MB，
  只绑 `127.0.0.1:9527`（回环，不对外暴露）。
- 代码目录：`/opt/rbot-clone`（16MB，含 venv；venv 另含 Python 依赖）。
- OCI CLI：`pip install oci-cli` 装进项目 venv（3.96.3，与本地同版本），
  `oci` 在 `/opt/rbot-clone/venv/bin/oci`。
  （注：任务原定用官方安装脚本；pip 是 Oracle 官方另一分发方式，
  结果等价且 PATH 更简单，改用 pip。）
- OCI 凭据：`/root/.oci/config` + `/root/.oci/oci_api_key.pem`（均为 600），
  key_file 路径已改成 部署服务器 上的路径；三账号 `iam region list` 均返回 44 个区域。
- 管理员密码：随机生成，保存于 `/root/.rbot-clone-admin-password`（600，
  24 字符）；哈希 + 会话密钥存于 `/root/.rbot-clone.env`（600），
  由 systemd 经 `EnvironmentFile` 读入。密码明文未写入任何报告或文档。
- Web 配置（部署版 `/opt/rbot-clone/web/config.py`，本地源码仍为占位符）：
  PROFILES 三账号（DEFAULT 新加坡 / ACCOUNT2 圣何塞 / ACCOUNT3 芝加哥，
  compartment 均为各账号根 compartment）；IMAGE_IDS 为新加坡 Ubuntu 24.04
  双架构镜像（aarch64 2026.11.05-0、x86_64 2026.11.05-0）；DATA_DIR 指向
  `/opt/rbot-clone/web/data`。
- CF 凭据未填（DNS 功能暂不可用，等用户决定）。

## 部署中发现并修复的 bug

**`oci/client.py` 的 OCI CLI 路径写死为 `~/oci-venv/bin/oci`**
（阶段 1 遗留，本地开发环境路径）。部署到 部署服务器 后首次验证时
`/api/instances` 报 500，日志为
`FileNotFoundError: '/root/oci-venv/bin/oci'`。
修复（已同步回本地源码 `~/workspace/rbot-clone/oci/client.py`）：
解析顺序改为环境变量 `RBOT_OCI_CLI` → `shutil.which("oci")` →
回退 `~/oci-venv/bin/oci`。本地 583 个测试重跑全过，
本地解析结果不变（仍为 `~/oci-venv/bin/oci`）；部署服务器 上解析为
`/opt/rbot-clone/venv/bin/oci`（经 systemd 的 PATH）。

## 验证结果（25 项，24 通过，1 项为预期差异）

验证脚本在 部署服务器 本机运行（密码只在 部署服务器 本机读，不打印、不回传），
只做只读操作，未创建/修改/删除任何云资源。

| 验证项 | 结果 |
|---|---|
| 登录页 200 / 错误密码 401 / 正确密码登录 / 会话 cookie | 通过 |
| /api/auth/me 返回 admin | 通过 |
| 概览页登录后 200 | 通过 |
| 页面 /instances /quota /volumes /create /tasks /terminal 均 200 | 通过 |
| /api/system 返回真实数据 | 通过 |
| 实例列表 DEFAULT = 3 台（实例 A、实例 B、实例 C） | 通过 |
| 实例列表 ACCOUNT2：返回 3 台，其中 1 台 TERMINATED | 预期差异（见下） |
| 实例列表 ACCOUNT3 = 1 台（实例 D） | 通过 |
| 配额 DEFAULT：A1 4/4 核、18/24GB | 通过 |
| 配额 ACCOUNT2：A1 4/4 核、24/24GB | 通过 |
| 配额 ACCOUNT3：A1 2/4 核、10/24GB | 通过 |
| Web SSH connect 成功（127.0.0.1:14422，临时密钥） | 通过 |
| Web SSH 回环 `echo RBOT_SSH_OK` 收到回显 | 通过 |
| Web SSH 会话关闭 | 通过 |
| 临时密钥已清理（authorized_keys 无残留、密钥文件已删） | 通过 |
| 登出成功 / 登出后 me 401 | 通过 |

**预期差异说明**：ACCOUNT2 返回 3 台而非预期的 2 台，因为 OCI 的
`instance list` 默认返回已终止实例；第 3 台（26-10-30 03:06）经 OCI CLI
直查确为 `TERMINATED`（今天早些时候用户下令释放的那台），面板正确显示
其 `lifecycle-state=TERMINATED` 且无公网 IP，另两台为 RUNNING 且有 IP。
面板行为正确（忠实呈现 API 数据 + 状态）；如需默认隐藏已终止实例，
可后续在 `list_instances` 加过滤（产品决策，未擅自改）。

## 未验证的部分

- **真实浏览器查看页面**：本机无浏览器，未做截图对比；页面验证靠
  HTTP 状态码与内容关键字。建议用户用 SSH 隧道访问后肉眼看一遍。
- **Telegram 机器人**：本次只部署 Web 面板；机器人需用户提供 Bot Token
  后再部署。
- **Cloudflare DNS 功能**：CF 凭据未填，DNS 页面/API 未实测。
- **写操作**（创建/启停/终止/抢机）：部署验证只做只读，未在真实实例上试。
- **SFTP**：阶段 3b 已实现但本次未单独测（只测了 Web SSH 终端通道）。
- **MCP 服务器**：未部署（`mcp_server.py` 已随代码复制到 部署服务器，但未配服务）。

## 其他说明

- 部署服务器 上的 xray 全程 active，未动。
- R-Bot 测试服务器 上的 R-Bot 未动。
- 部署期间本机 VM 发生一次替换（uptime 显示重启，约 09:28 UTC），
  E2 抢机编排器随之被杀；看门狗 cron 于 09:36 自动按断点重启续跑
  （stage=create，attempts=172 → 173），部署工作未受影响。
- 面板访问方式（给用户）：`ssh -p 14422 -L 9527:127.0.0.1:9527 root@<部署服务器IP>`
  后浏览器打开 `http://127.0.0.1:9527/login`，用户名 admin，
  密码在服务器 `/root/.rbot-clone-admin-password` 里看。
