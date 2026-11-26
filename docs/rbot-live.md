# R-Bot 实机研究记录（2026-11-23）

用户已授权：为克隆参考需要，可安装/测试 R-Bot。

## 运行状态

- 服务器：某台圣何塞 E2.1.Micro 服务器（IP 已脱敏）
- 进程：`/root/r_client --configPath=/root/client_config --server.port=9527`
  （PID 693，自 2026-11-21 04:00 运行，CPU 0.0%，内存约 70MB）
- 监听：`*:9527`（TCP）
- 二进制：/root/r_client，83.5MB，2026-11-06
- 管理脚本：/root/sh_client_bot.sh，42KB（可读，含 start/stop/restart/upgrade/log 等命令）

## 面板测试（本机 curl）

- `https://127.0.0.1:9527/` → HTTP 302 → `/radiance-bot-client/roc/login`
- 登录页：HTTP 200，41KB，含 login/登录元素
- 外部无法访问（UFW 挡住 9527；R-Bot 启动时已自动在 OCI 安全组开放 9527，但 UFW 仍挡）

## 启动日志要点（/root/log_r_client.log）

- 启动时自动检测端口是否开放，未开放则尝试自动打开（调 OCI API 改安全组）
- 只加载了一个云配置：`oracle:DEFAULT/ap-singapore-1`
- 无 Azure/AWS/GCP/SolusVM 配置，均跳过

## 配置文件结构（/root/client_config，值已脱敏）

格式：INI 风格，分段标记 `xxx=begin` / `xxx=end`：

```
oci=begin
[DEFAULT]
user=<OCID>
fingerprint=<指纹>
tenancy=<OCID>
region=ap-singapore-1
key_file=<私钥路径>
oci=end

username=<面板用户名>
password=<redacted>

cf_email=<可空>
cf_account_key=<可空>

local_address=<可空，默认自动获取 IP>
local_url_name=<可空>
model=<可空，填 local 为无公网 IP 模式>

azure=begin ... azure=end
ssh=begin ... ssh=end   # 格式：ssh_<IP>=<连接配置>
solusvm=begin ... solusvm=end  # [racknerd-1] api/key/hash
```

- 配置加密：/root/client_config.key（44 字节，600 权限）
- OCI 私钥：/root/x.pem（1715 字节，600 权限）
- 环境文件：/root/rbot.env（32 字节，600 权限）
- 任务目录：/root/.task/（空，无运行中任务）

## 另一台芝加哥实例

用户说这里有安装，但实测**没有**（全盘搜索无 r_client/client_config/gz_client，
无 9527 监听，无 Java 进程，无 /root/rbot 目录）。

## 克隆参考价值

- 配置文件格式可直接借鉴（oci=begin/end 块结构清晰，多 profile 支持）。
- 面板 URL 路径结构：`/radiance-bot-client/roc/login`（登录页）。
- 启动时自动开安全组端口的行为值得注意（克隆版不要默认做这个，需用户明确授权）。
- 内存占用约 70MB（Java 客户端），Python 克隆版应更低。

---

## 深度研究（2026-11-23 晚，用户授权：新建测试 API + 重置面板凭据后登录研究）

### 测试 IAM 用户（用户授权新建）

- 用户名 rbot-test，邮箱 <测试邮箱已脱敏>
- OCID：ocid1.user.oc1..xxxxxxxx（已脱敏）
- 已加 Administrators 组；API 密钥指纹 <已脱敏>
- 私钥：本地 ~/.oci/<测试用户>/key.pem（600），服务器上 /root/x.pem（旧密钥已备份）
- 本地 OCI config 新增 [RBOT_TEST] profile；服务器的 client_config 已换成该用户（原密钥已失效，报 NotAuthenticated）
- 教训：`oci iam user create` 在 IDCS 租户必须带 --email；JMESPath 报错发生在 API 成功之后（第二次调用实际已建好用户，第三次报 409 冲突才发现）

### 面板凭据重置（/raninfo）

- 用户在 Telegram 用 /raninfo 两次确认后重置面板账号密码（32 位 hex 各一）
- 我已把新凭据写入服务器的 client_config（原文件已备份），重启后日志显示"已成功添加URL"（客户端向 Bot 服务器注册）
- ⚠️ 凭据明文只存服务器的 client_config，不记入任何文档与记忆

### 管理脚本 sh_client_bot.sh（1026 行，脚本副本不随仓库公开）

- systemd 单元 rbot.service：Type=simple，Restart=always，RestartSec=10，EnvironmentFile=rbot.env（EXTRA_ARGS=--server.port=9527）
- 配置加密：client_config.key 首启自动生成；配置文件内另有副本，只拷配置文件迁移也能恢复
- data/ 目录：SSH 密钥库、代理、SSL 证书等数据（两台测试服务器上均不存在，说明未用过这些功能）
- 原子升级：下载到脚本目录内临时目录（同文件系统 mv 才原子，避开路由器 /tmp 空间不足），校验二进制存在，配置文件不存在才从包内拷贝，mv 覆盖
- 架构分包（GitHub Releases）：mac_aarch、aarch、x86（需 AVX/AVX2/SSE4.2）、x86_compatible
- 健康检查：GET https://127.0.0.1:9527/radiance-bot-client/roc/api/client/health（curl -skf）
- Java 内触发升级：用 systemd-run 建临时单元逃离 cgroup，或 setsid 脱离；升级锁 .r_client.upgrading.lock（flock 优先，否则时间戳软锁 54 秒新鲜度）
- 端口解析优先级：Java 传入 > 进程命令行 --server.port > 监听探测 > .r_client.port 文件 > 默认 9527
- BusyBox/OpenWrt 兼容：nohup shim、/proc 兜底、uclient-fetch 兼容 wget 参数
- 卸载需手输 YES，删二进制/配置/密钥/日志/systemd 单元/data//.task/

### 面板登录机制（实测）

- 登录页 41KB，单页内联 JS；POST /roc/api/login，JSON {username, password}，可选 captchaToken/captchaAnswer
- CSRF：XSRF-TOKEN cookie → 请求头 X-XSRF-TOKEN（Spring Security 风格）；不带则 403
- 验证码：GET /roc/api/captcha/login/required 返回 {"required":false}（当前不需要）
- 另有 Telegram 验证码登录：/roc/api/telegram/send-code + /verify（带重发冷却）
- 登录成功 {"success":true,"message":"登录成功"}，随后跳转 CTX + '/'
- 所有静态 JS（i18n.js、res.js）未登录时返回 401 {"code":401,"message":"会话已过期，请重新登录"}

### 权限标志（GET /roc/api/user/current，当前用户几乎全 false）

返回 26 个 *Allowed 布尔值，即闪电付费功能的开关：awsChangeIp、consoleRescue、doReservedIp、mcp、checkAlive、awsUsage、emailSend、dns、termKeepBoot、ipv4、theme（唯一 true）、quickBoot、ipv6、reserveIp、shell、email、cloudSync、forceArm、doUsage、vpu、cert、doUserData、oss、bootVol、resetImage。面板按这些标志启用/禁用功能。

### 面板 API 全表（binary strings 提取 + 登录后实测，约 120 个端点）

- 认证/会话：/login、/captcha*、/telegram/send-code|verify、/session、/session/suspended、/session/ttl（{"ttlMs":0}）、/user/current、/ws-token（返回 WebSocket token）
- 客户端：/client、/client/health（返回 CPU 型号/核数/占用、内存/磁盘 GB 与占用率、网速 rx/tx、进程 RSS MB、uptime 秒）、/cert/renew、/cert/status（acmeEnabled/ip/domain/issuer/notBefore/notAfter/remainingHours/status）
- OCI：/cloud/oci/instances（需 ?profile=，返回 id/name/publicIp/shape/state/ocpus/memoryInGBs/region/timeCreated，只含 RUNNING）、/cloud/oci/create、/cloud/oci/volumes（volumeId/attachmentId/displayName/sizeInGBs/vpusPerGB/lifecycleState/instanceId/instanceName/attached/attachmentState/availabilityDomain/timeCreated）、/cloud/oci/stats、/cloud/oci/stats/usage（⚡403）、/cloud/oci/profiles（⚡403）、/cloud/oci/object-storage、/cloud/oci/email、/cloud/oci/console（串行控制台连接列表）、/cloud/oci/a1-quota-all（capOcpu/capMem/usedOcpu/usedMem/over/instances；注：cap 显示为当前用量 2/12 非免费上限 4/24）、/oci/users（⚡403）
- 其他云：/cloud/aws*（instances/create/lightsail/sg/stats）、/cloud/azure/instances、/cloud/gcp*（create/machine-types/stats）、/cloud/do*（reserved-ips）、/cloud/solusvm/nodes、/cloud/virtfusion/instances、/cloud/dns、/cloud/dns/zones（未配 CF 凭据时 400 提示"未配置 Cloudflare 凭据（cf_email / cf_account_key），请在设置中配置"）、/cloud/context、/cloud/ssh-keys
- 主机/终端：/host、/host/sessions、/host/sessions/resolve-by-ip、/host/sync、/terminal、/terminal/paste-image、/term-favorites（{version, items}）、/batch、/batch/exec、/batch/cancel/（批量命令）
- SFTP：/sftp、/sftp/autocomplete、/sftp/download、/sftp/upload*（分块上传：init/chunk/complete/progress/cancel/reroll/prewarm，带进度）
- 设置：/settings（cf_api_token/cf_email/cf_account_key/local_address/local_url_name/model/acme_*）、/settings/config-status（各云 configured + profiles 列表）、/settings/monitor、/settings/monitor-auto（POST only）、/settings/oci-proxy（需 ?name=）、/settings/oci-proxy/names、/settings/profile（POST only）、/settings/profile/copy、/settings/profile-detail、/settings/reload-config、/settings/upload-config、/settings/upload-gcp-json
- 监控：/monitor/rules、/monitor/rules/、/monitor/history（需 ?host=）、/domain-monitor、/cloud-monitor/rules（{monitorEnabled, autoOpenEnabled, rules}，当前全关）、/cloud-monitor、/inspect-schedule、/cloud-report/summary（{groups, dnsRecords, generatedAtEpochSec}）
- 其他：/credentials、/credentials/auto-save、/proxies、/registration（/bind、/status：{registered, credentialsVerified, username, botUrl, password, autoGenerated}）、/payment/info（返回 base64 付款二维码图片，585KB）、/support/contact、/support/status（{canUseQs, blocked, blockUntil}）、/mcp、/mcp4

### 各云功能清单（GET /roc/api/cloud/context）

- oci：overview, instances, a1-audit, monitor-rules, users, create, tasks, object-storage, email-delivery, console（configured: true）
- aws：overview, instances, lightsail, create
- azure：instances
- gcp：overview, instances, create
- do：overview, instances, create
- cloudflare：dns
- domain：domain-monitor（configured: true）
- solusvm：nodes
- virtfusion：instances
- multiProfileEnabled: false

### 版本对比（两台服务器上的客户端版本）

- 服务器 A：2026-11-06 二进制，83,559,536 字节，config 2155 字节，含 rssh_crypto 块（43 字符 base64 令牌，远程 SSH 加密）
- 服务器 B：2026-09-07 二进制，74,974,320 字节，config 1921 字节，无 rssh_crypto 块
- 两台均无 data/ 目录，.task/ 均为空
- 服务器 B 经跳板机 + SSH agent 转发连通（跳板机上无私钥，需 -A；IPv6 地址需加方括号进 ProxyCommand -W 参数）

### 克隆新增缺口（API 表面揭示，阶段 5 未覆盖）

- SFTP 分块上传带进度（init/chunk/complete/progress/cancel）
- 批量命令执行（/batch/exec + 取消）
- 终端收藏夹（term-favorites）
- Telegram 验证码登录面板
- 登录验证码（captcha）
- 证书管理（ACME 续期/状态）
- OCI 代理设置（oci-proxy）
- 巡检计划（inspect-schedule）
- 云报告汇总（cloud-report/summary）
- 主机按 IP 解析会话（resolve-by-ip）
- 终端粘贴图片（paste-image）
