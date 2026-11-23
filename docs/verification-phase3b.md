# 阶段 3b 验证记录：SFTP 面板 + Grabber shape-config + 创建表单全字段接入

验证时间：2026-11-23（UTC 约 09:20）。验证人：阶段 3b 协调员（亲自复查全部
代码 + 跑测试 + 真实服务冒烟）。两个子代理并行开发，协调员做集成检查。

## 结论

- **438 个单元测试全过**（`python3 -m unittest discover -s tests`，约 9 秒）：
  354 个阶段 1/2/3 旧测试 + 84 个阶段 3b 新测试（SFTP 64 + Grabber/创建 20），
  **零回归**。
- **真实回环 SFTP 端到端通过**：进程内 paramiko SFTP 服务器，真实 SFTP 协议
  走完 list → upload → download → rename → delete 全流程 + mkdir + 在线编辑 +
  真实 symlink（→/etc）下载被 403。
- **真实服务冒烟全过**：TestClient 登录、SFTP 路由 404（会话不存在，证明
  路由已注册且鉴权生效）、创建页新文案、终端页 SFTP 面板标记、OpenAPI
  含 7 条 SFTP 路径。
- 写操作（launch 命令构造）**零次碰生产实例**，全部用 mock 断言命令。
- 编排器全程未受干扰（验证结束时 PID 3836，第 152 次尝试，仍无容量）。

## 代码规模

| 类别 | 文件 | 行数 |
|---|---|---|
| SFTP 后端 | web/sftp.py（新） | 558 |
| SFTP 测试 | tests/test_web_sftp.py（新） | 1003 |
| Grabber | oci/grabber.py（改，+8 参数） | 436 |
| 创建路由/任务 | web/routes/create.py、web/tasks.py（改） | — |
| 前端 | terminal.html / terminal.js / terminal.css、create.html / create.js（改） | — |
| Grabber/创建测试 | tests/test_grabber.py（+16）、tests/test_web_api.py（+4） | — |

## 一、SFTP 文件面板

### 后端（web/sftp.py，558 行）

- 复用 `web.ssh.manager`：`SFTPClient.from_transport(session.client.get_transport())`，
  每会话缓存一个 SFTPClient（`entry["sftp"]`），家目录首次 `normalize(".")`
  后缓存（`entry["sftp_home"]`）；`ssh.py` 的 `close_session` 改成先关
  SFTPClient 再关 SSH 会话（老记录无 `sftp` 键时 `.get` 兜底，37 个 SSH
  测试全过）。
- 8 个接口（7 条路径，file 分 GET/PUT），全部挂在 `router` 上并注册进
  app.py（与 ssh router 同样的 try/except 容错方式）：

  | 接口 | 说明 |
  |---|---|
  | GET …/sftp/list | 列目录，返回 `{path, home, entries}`，目录在前按名排序；条目含 name/size/mtime/is_dir/is_symlink/permissions/outside_home/broken |
  | GET …/sftp/download | StreamingResponse，64KB 分块，流完关闭句柄 |
  | POST …/sftp/upload | multipart 流式写，上限 100MB，超限 413 且删除已写一半的远端文件 |
  | POST …/sftp/mkdir | `{path, name}` 与 `{path}` 两种 body 都支持 |
  | DELETE …/sftp | 删文件或空目录；非空目录 400 提示先清空，不递归删；禁删家目录本身 |
  | POST …/sftp/rename | src/dst 双校验，禁重命名家目录本身 |
  | GET …/sftp/file | 读小文本文件，上限 1MB，非文本扩展名 415，非 UTF-8 400 |
  | PUT …/sftp/file | 写文本文件，上限 1MB，允许创建新文件 |

- 错误码：会话不存在 404、未登录 401、SFTP 不可用 502（友好文案，不漏
  paramiko 原始异常细节，测试已断言）。

### 路径穿越防护（硬性要求，协调员逐行复查）

- 纯函数 `resolve_safe_path(home, requested)`：`../`、`../../etc/passwd`、
  `/etc/passwd`、NUL 字节、非绝对 home 均抛 ValueError；`a/../b` 合法内部
  `..` 放行；`_is_within` 用 `home + "/"` 前缀比对，防了 `/home/testuser2`
  前缀陷阱。
- 符号链接：list 时对 symlink 用 `stat` 跟随判断目录 + `normalize` 解析
  真实路径，跳出者标 `outside_home: true`；download/file/delete/rename
  对目标先 `normalize` 解析再校验。
- 额外一层（原计划外，工人顺手堵的漏洞）：upload/mkdir/rename 的 dst /
  PUT 新文件时目标本身不存在无法解析，改解析**父目录**真实路径
  （`_resolve_parent`），防父目录是跳出 home 的链接。
- 协调员复查确认：所有接口在调用 SFTP 前都先过词法校验 + normalize 复核；
  PUT 的内层 `except OSError` 对 PermissionError（errno=None）会重新抛出，
  不会被误当成"文件不存在"放行。

### 前端（终端页右侧滑出面板）

- terminal.html：面板容器（面包屑/列表/上传/新建目录/关闭）+ 在线编辑弹窗。
- terminal.js：SFTP 占位 alert 已替换为 `SFTPPanel` 类——list 渲染（目录
  双击进入、上级按钮、大小/mtime）、下载（a 标签跳转）、上传（file input
  + 拖拽到面板）、删除（confirm）、改名/新建目录（prompt）、在线编辑
  （小文本文件双击开弹窗）；无会话时 toast 提示；切标签自动按新会话刷新，
  无会话时自动关闭面板。文件名用 `textContent` 渲染，防文件名 XSS。
- terminal.css：面板 420px，transform 滑入动画，复用 `--term-*` 变量，
  另有 `data-mode="light"` 亮色覆盖。
- `node --check` 通过（terminal.js 与 create.js）。

### SFTP 测试（tests/test_web_sftp.py，64 个）

- mock：list/download/upload/mkdir/delete/rename/file 读写各有成功 + 失败
  用例（404/401/400/403/413/415/502 全覆盖），SFTP 缓存只建一次、家目录
  缓存、`close_session` 关闭 SFTP。
- 路径穿越专用：11 个 `resolve_safe_path` 单元测试 + mock 与回环两层的
  symlink 跳出测试。
- 真实回环 SFTP：进程内 paramiko 服务器（`SFTPServerInterface` 背后临时
  目录，不依赖系统 sshd），真实走完 list → upload → download → rename →
  delete 全流程，另有 mkdir + 在线编辑 + 真实 symlink（→/etc）下载被 403。
  调试中踩的两个坑已写进测试注释：覆写 `check_channel_subsystem_request`
  会阻止 handler 启动导致客户端挂死；服务端 `mkdir/rename/remove/rmdir`
  必须返回 `SFTP_OK` 而非 None。

## 二、Grabber shape-config（oci/grabber.py）

新增 8 个可选参数（全部有默认值，tgbot 调用处未改、行为不变）；命令构造
抽成 `_build_launch_args()`，`run()` 拆出 `_run_loop()` 以便 try/finally
清理临时文件。

| 参数 | 默认 | launch 行为 |
|---|---|---|
| `ocpus` / `memory_gb` | None | Flex 形状（以 `.Flex` 结尾）且两者都传时加 `--shape-config '{"ocpus":N,"memoryInGBs":M}'`；E2.1.Micro 等固定规格即使传了也忽略；缺一个就不传 |
| `boot_volume_size_gbs` | None | 传了加 `--boot-volume-size-in-gbs N`，不传则不加（用 OCI 默认） |
| `assign_public_ip` | True | `--assign-public-ip` 按值传 `true`/`false`（原先写死 true） |
| `assign_ipv6` | False | True 时加 `--assign-ipv6-ip true`（见下方探查结论） |
| `user_data` | None | 非空时写临时文件（`tempfile`，`rbot-user-data-*.sh`），传 `--user-data-file`；空时不传；`run()` 结束时（正常返回或抛异常）自动删除，`cleanup()` 可手动删；文件惰性创建、多次 launch 共用 |
| `min_delay` / `max_delay` | None | 都传且都 >0 时 interval=(min+max)/2、jitter=(max-min)/2；否则用常量 25±5；min>max 时构造即抛 ValueError |

### OCI CLI 探查结论（CLI 3.96.3，`oci compute instance launch --help` 只读探查）

- `--shape-config`：支持（COMPLEX TYPE，JSON 字符串）。
- `--boot-volume-size-in-gbs`：支持（INTEGER）。
- `--assign-public-ip`：支持（BOOLEAN）。
- **`--assign-ipv6`：不存在**；IPv6 分配的实际参数名是 **`--assign-ipv6-ip`**
  （BOOLEAN，子网须支持 IPv6）。实现按真实参数名传，docstring 与测试
  注释均注明。
- `--user-data-file`：支持（FILENAME，cloud-init 脚本/配置文件路径）。

## 三、创建表单字段接入后端

- `web/routes/create.py`：CreateBody 新增 `boot_volume_size_gbs: int = 50`
  （Field ge=47 le=200，超范围 422）、`assign_public_ip: bool = True`、
  `assign_ipv6: bool = False`、`min_delay/max_delay: int = 0`（ge=0）、
  `user_data: str = ""`；`max < min` 时路由返回 400（在预检前校验）；
  params 透传全部新字段 + ocpus/memory_gb；模块 docstring 删掉过时说明。
- `web/tasks.py`：`_run_grabber` 把新字段传给 Grabber（ocpus/memory_gb/
  min/max 为 0 按未填转 None；其余用 `params.get` 兜底，旧调用方缺字段
  也能跑）；`start_grab_task` docstring 更新。
- `web/templates/create.html`：提示改为"A1 按所填 OCPU / 内存创建
  （--shape-config）；E2 是固定规格…"；启动脚本 placeholder 改为"可选：
  实例首次启动时执行的 cloud-init 脚本（bash）"。
- `web/static/js/create.js`：头部注释与 collectPayload 注释更新，payload
  原样（已发送全部字段）。

### Grabber/创建测试（20 个新增）

- `tests/test_grabber.py` 16 个（TestLaunchOptions 12 个：A1 shape-config
  JSON 解析、E2 永不传、缺一不传、引导卷、公网 IP true/false、IPv6
  true/false、user_data 文件内容与空值、run() 清理临时文件；
  TestDelayRange 4 个：20–50 区间采样、默认 25±5、只传一个回退默认、
  min>max 抛错）。
- `tests/test_web_api.py` 4 个（全字段透传断言 Grabber 构造 kwargs、
  引导卷 46/201 → 422、47/200 边界通过、max<min → 400）。
- 更新 1 个旧断言：`tests/test_web_pages.py` 的创建页检查把"暂未实现"
  换成新 placeholder 文案"cloud-init 脚本（bash）"（功能已实现，
  旧文案已不存在）。
- `/tmp` 无 `rbot-user-data-*` 残留文件（协调员核查）。

## 真实服务冒烟（协调员，/tmp/smoke_phase3b.py，临时脚本不进仓库）

| 验证项 | 结果 |
|---|---|
| 登录 200 | 通过 |
| SFTP list（不存在的会话）→ 404"会话不存在" | 通过（路由已注册，鉴权生效） |
| 创建页 200，含 cloud-init 与 shape-config 文案，无"暂未实现" | 通过 |
| 终端页 200，含 sftp-panel 标记；terminal.js 含 SFTPPanel 类 | 通过 |
| OpenAPI 含 7 条 SFTP 路径 | 通过 |

## 协调员复查记录

1. **SFTP 代码逐行复查**：路径校验层（词法 + normalize + 父目录解析）
   三层完整；上传超限删半写文件；下载流式；SFTP 不可用时 502 不漏堆栈；
   `close_session` 先关 SFTP 再关 SSH，`.get` 兜底老记录。未发现安全问题。
2. **Grabber 代码复查**：`_build_launch_args` 构造正确；shape-config 只对
   `.Flex` 形状；`--no-retry` 保留；临时文件 try/finally 清理。
3. **全量测试**：438 个全过（9 秒），SSH 测试仍是 37 个，无回归。
4. **脱敏检查**：`web/sftp.py`、`tests/test_web_sftp.py`、`oci/grabber.py`、
   `web/routes/create.py`、`web/tasks.py`、terminal.js/html 全扫：无真实 IP、
   无真实 OCID（20 位以上后缀）、无密码/Token。测试用占位 OCID
   （`..testxxxx`）与文档网段 203.0.113.x、回环 127.0.0.1。
5. **JS 语法**：`node --check` 通过（terminal.js、create.js）。

## 已知小缺口（不阻塞，可后续修）

- **下载文件名未做引号转义**：`Content-Disposition` 的 filename 直接取远端
  文件名，含 `"` 或换行符的文件名可能破坏响应头。风险很低（文件名来自
  用户自己有 SSH 权限的远端主机），后续可用 `urllib.parse.quote` 或
  RFC 5987 编码加固。
- **上传文件名为 `..`/`.` 时未显式拒绝**：目前靠 `_is_within` 校验 + 远端
  open 失败兜底，无实际风险，但可加显式 400 更干净。
- **删除 symlink 指向的目录时行为依赖服务端**：`stat` 跟随链接判为目录后
  调 `rmdir`，部分 SFTP 服务端对 symlink 调 rmdir 会报错（只是 UX 问题，
  不删错东西）。

## 与 R-Bot 原版的剩余差异（SFTP 部分，未做）

- 传输管理器：无上传/下载进度条、速度显示、队列管理；多文件上传逐个串行，
  只有状态文字。
- 断点续传：上传/下载均不支持。
- 在线编辑无语法高亮：纯 textarea，无行号、无 CodeMirror/Monaco。
- 无兼容模式：OpenWrt 等无 SFTP 的设备只返回 502，未做 `ls`/`cat`/`scp`
  命令兜底。
- 无 chmod 权限修改、无压缩/解压、无远程文件搜索。
- 面板位置在右侧（按任务要求），而非 R-Bot 原版的终端下方抽屉。

## 与 R-Bot 原版的剩余差异（创建表单部分）

- 数量字段仍为 disabled（一次只开 1 台，R-Bot 支持批量开多台）。
- 无区域/AD 自动下拉（仍需手填 AD 与子网 OCID）。
- 启动脚本只支持 cloud-init 文本，不支持 R-Bot 的脚本库/模板选择。
