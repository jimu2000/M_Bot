# 参考资料（R-Bot 克隆）

## R-Bot 官方文档（功能定义来源）

- README：https://github.com/semicons/java_oci_manage
- 开机教程（含抢机机制）：https://raw.githubusercontent.com/semicons/java_oci_manage/main/md/boot-oracle.md
- 机器人指令说明：https://raw.githubusercontent.com/semicons/java_oci_manage/main/md/BOT-README.md
- 已实现功能清单：https://raw.githubusercontent.com/semicons/java_oci_manage/main/md/function.md
- 安装与配置：https://raw.githubusercontent.com/semicons/java_oci_manage/main/md/install.md
- 常用操作教程：https://raw.githubusercontent.com/semicons/java_oci_manage/main/md/howto.md

### /oracle 键盘菜单（34 项，Telegram 机器人阶段 2 的实现清单）

1. 开机(刷ARM) 2. ip管理 3. 被暂停开机 4. ipv6管理 5. 升/降级实例
6. 实例管理 7. 磁盘管理 8. 云账户管理 9. 实例状态监控通知 10. 实例状态监控自启
11. 开放云面板端口 12. 甲骨文工作流错误 13. profile管理 14. 客户端管理
15. 一键测活 16. 监控(自动换ip) 17. 快捷开机 18. 甲骨文订阅信息
19. 近3月流量查询 20. 查询配额 21. 查看运行中的任务 22. 查看客户端负载
23. 占用25%内存 24. 清除缓存和占用 25. 近3月花费查询 26. 清除所有2FA设备
27. 一键禁用被封账户 28. 删除当前API 29. 每日报告 30. 批量查询邮箱
31. 重启 32. 升级客户端 33. 自动拓展子区域 34. 查询最新日志

### 顶层机器人命令

/oracle /aws /gcp /azure /do /solusvm /virtfusion /domain /oci /command /pubkey
/uploadapi /oproxy /bindclient /me /clearlock

**跳过付费功能**：/qrcode、/codeflash、/flash、/migrate、/getflash、/qs
（"闪电权限"充值体系，开源克隆不需要）。

### 值得借鉴的设计点

- `/oci` 上传配置时只写 key_file 路径，私钥文件自己上传到服务器（密钥不经过聊天）。
- 抢机：延迟区间（随机抖动）、配额不足与缺货区分、成功通知带尝试次数与耗时。
- 快速配置预填：AMD 微型 1C/1G、ARM A1 2C/12G 一键填好参数。
- 双端架构：API 私钥只存本地客户端，不经过机器人服务器。

## GitHub 开源参考项目

### OCI + Telegram（阶段 1、2 参考）

| 项目 | 地址 | 许可证 | 参考点 |
|---|---|---|---|
| OCI-OcC-Fix | github.com/mosesman831/OCI-OcC-Fix | — | Python + OCI SDK + pyTelegramBotAPI，持续抢机 + Telegram 通知，configuration.ini 配置 |
| hermes-oci-free-tier-report | github.com/pgen0x/hermes-oci-free-tier-report | MIT | 免费额度日报（Telegram/Discord/WhatsApp），实例主体认证（盘上无 API Key），多频道表格推送 |
| Oracle-Ampere-A1-Hunter | github.com/SID1ART/Oracle-Ampere-A1-Hunter | MIT | Python + OCI SDK，抢机 + Telegram 通知 + Gradio 网页实时日志 |

### Web SSH 终端（阶段 3 参考）

| 项目 | 地址 | 许可证 | 参考点 |
|---|---|---|---|
| webssh | github.com/huashengdun/webssh | MIT | 经典 Python Web SSH：tornado + paramiko + xterm.js，密码/密钥认证，全屏、可调大小 |
| py-web-ssh | pypi.org/project/py-web-ssh | — | FastAPI + Paramiko + xterm.js，会话 UUID、断线重连、终端快照、文件上传下载 |
| RemoteHive | github.com/mikedattolo/RemoteHive | MIT | FastAPI + React + xterm.js + asyncssh，多主机管理、SSH/VNC/RDP |
| django-aws-terminal-websocket | github.com/agusmakmun/django-aws-terminal-websocket | MIT | Django + Channels + xterm.js（Django 栈，参考性较低） |

### 选型建议

- **Web SSH 后端**：参考 py-web-ssh 的 FastAPI + Paramiko + WebSocket 做法（与我们的 FastAPI 面板同栈），不要用 tornado（webssh 的老栈）。
- **Telegram 机器人**：python-telegram-bot（主流，文档全），参考 OCI-OcC-Fix 的 pyTelegramBotAPI 通知模式。
- **OCI 调用**：继续用官方 OCI CLI（subprocess），不要换 OCI Python SDK——CLI 已验证可用，SDK 要重测一遍认证与错误处理。
