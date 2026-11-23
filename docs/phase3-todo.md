# 阶段 3 待办（Web 面板）

## 已完成（2026-11-23）

- Web SSH 终端：FastAPI + WebSocket + Paramiko + xterm.js（`web/ssh.py`、
  `web/templates/terminal.html`、`web/static/js/terminal.js`、
  `web/static/css/terminal.css`）
  - 多标签、会话 UUID、output_buffer 快照、断线重连（5 分钟宽限期）
  - 欢迎横幅（R-BOT ASCII + uname/uptime/df/free）
  - 多行粘贴保护、空闲清理（30 分钟）、终端右上角 CPU/MEM/DISK 轮询
- 会话挂起：关页面不断开（WS 断开后会话保留 5 分钟，重连回放快照），
  本质上已覆盖 R-Bot“挂起”语义（最多 20 个会话）。

## 已完成：SFTP 文件面板（阶段 3b，2026-11-23）

- 后端 `web/sftp.py`：复用 `SSHSession` 的 transport 开 SFTPClient
  （每会话缓存一个，`close_session` 时一并关闭；家目录首次用
  `normalize(".")` 取并缓存）。REST 接口：GET …/sftp/list（列表）、
  GET …/sftp/download（StreamingResponse 分块）、POST …/sftp/upload
  （multipart 流式，上限 100MB）、POST …/sftp/mkdir、DELETE …/sftp、
  POST …/sftp/rename、GET/PUT …/sftp/file（在线编辑，上限 1MB，
  仅文本扩展名）。
- 前端：终端页**右侧**滑出面板（420px，任务要求，与 R-Bot 原版的下方
  抽屉不同），面包屑路径、双击进目录/打开文件、拖拽上传、在线编辑
  弹窗；切标签时按新会话刷新列表。
- 安全：`resolve_safe_path()` 把所有路径限定在家目录内（防 `..` 穿越、
  绝对路径、NUL 字节）；符号链接用 `sftp.normalize()` 解析真实路径后
  复核，跳出者标记 `outside_home` 且下载/打开时拒绝；新建文件时还
  解析父目录真实路径（防父目录是跳出链接）；SFTP 不可用时返回 502
  友好错误，不漏 paramiko 异常细节。
- 测试 `tests/test_web_sftp.py`：64 个（mock + 路径穿越专用 +
  进程内真实 paramiko SFTP 回环服务器全流程）。

与 R-Bot 原版 SFTP 的剩余差异（未做）：
- 传输管理器（上传/下载进度条、速度、队列）：本次只有状态文字提示，
  无进度条；多文件上传是逐个串行，无并发/队列管理。
- 断点续传：上传/下载都不支持断点续传。
- 在线编辑无语法高亮：纯 textarea，无行号、无 CodeMirror/Monaco。
- 无兼容模式：OpenWrt 等无 SFTP 的设备只返回 502，未做 `ls`/`cat`/
  `scp` 命令兜底。
- 无文件权限修改（chmod）、无压缩/解压、无远程文件搜索。
- 面板在右侧而非 R-Bot 原版的终端下方。

## 未实现：分屏 / 批量命令

- 分屏：左右一分为二、一键克隆当前连接。前端工作量中等（两个 xterm
  实例共享一个会话或克隆新会话），可放在 SFTP 之后做。
- 批量命令：勾选多台主机群发命令、结果分开展示。依赖主机列表页与
  SSHSession 的 exec 模式（非交互 channel），可与分屏一起做。
