# 阶段 3 验证记录：Web 面板 + Web SSH

验证时间：2026-11-23（UTC 约 09:00）。验证人：阶段 3 协调员（亲自复查全部
代码 + 跑测试 + 真实 API 实测 + 真实 uvicorn 冒烟）。三个子代理并行开发，
协调员做集成检查与修复。

## 结论

- **354 个单元测试全过**（`python3 -m unittest discover -s tests`，约 8 秒）：
  220 个阶段 1/2 旧测试 + 134 个阶段 3 新测试。
- **29 项真实冒烟全过**（见“真实服务冒烟”）：登录/锁定/登出、8 个页面、
  真实 OCI 只读 API（实例/配额/启动盘均与已知状态一致）。
- **真实 uvicorn 服务冒烟全过**：`uvicorn web.app:app` 启动、登录、页面、
  /api/system 真实数据。
- **Web SSH 真实回环端到端通过**：进程内 paramiko SSH 服务器，真实 SSH
  协议验证欢迎横幅、`echo hello` 收发、错误密码拒绝。
- 写操作（启停/终止/扩容/删盘/抢机）**零次碰生产实例**，全部用 mock
  断言命令构造。
- 编排器全程未受干扰（验证结束时 PID 3836，第 127 次尝试，仍无容量）。

## 代码规模

| 类别 | 文件 | 行数 |
|---|---|---|
| 后端 Python | web/*.py + web/routes/*.py | 2435 |
| 模板 | web/templates/*.html（9 个） | 841 |
| CSS | web/static/css/*.css（3 个） | 988 |
| JS | web/static/js/*.js（9 个） | 1677 |
| 测试 | tests/test_web_*.py（5 个） | 1935 |
| **合计** | | **7876** |

主要文件：

| 文件 | 行数 | 说明 |
|---|---|---|
| web/app.py | ~110 | FastAPI 入口（create_app 工厂 + 模块级 app 实例） |
| web/auth.py | 198 | pbkdf2 密码哈希、登录锁定、会话 |
| web/config.py | ~140 | 配置区（HOST/PORT/锁定参数/PROFILES/IMAGE_IDS） |
| web/deps.py | 79 | 登录依赖、OCI 异常映射、统一错误信封 |
| web/tasks.py | 280 | 后台任务注册表（抢机任务，持久化 tasks.json） |
| web/ssh.py | 671 | Web SSH（Paramiko 会话管理 + WebSocket + 欢迎横幅） |
| web/routes/*.py | ~900 | auth/system/instances/volumes/quota/create/hosts/pages |
| web/templates/*.html | 841 | base/login/overview/instances/create/quota/volumes/tasks/terminal |
| web/static/js/*.js | 1677 | app/login/overview/instances/create/quota/volumes/tasks/terminal |

## 单元测试（354 个全过）

组成：oci 104 + tgbot 116（阶段 1/2 旧测试）+ web 134。

阶段 3 新测试：
- test_web_auth（19 个）：哈希/验证 roundtrip、错误密码失败、锁定翻倍
  （1→2→4→8→16→30 封顶）、成功登录清零、会话过期/注销。
- test_web_api（49 个）：TestClient + mock oci 函数，覆盖登录/锁定 429、
  未登录 401、实例列表/详情/启停/重启/终止（缺 confirm 400）、磁盘列表/
  扩容/删盘/孤儿盘、配额、can-launch、创建任务（mock Grabber）、任务
  列表/取消、主机 CRUD（临时 DATA_DIR，验证密码不落盘）、/api/system。
- test_web_ssh（37 个）：mock paramiko 的会话管理、WebSocket 消息处理
  （input/resize/ping、base64 output、未登录 4401、会话不存在 4404）、
  断线后会话保留、**进程内真实 SSH 回环端到端**（见下）。
- test_web_pages（17 个）：6 个页面渲染 200 + 关键元素 + 侧边栏高亮 +
  静态资源 + 无真实密钥 + 无 xterm 引用（除 terminal 页）。
- test_web_integration（12 个，协调员补写）：镜像解析、/quota 页、
  current_page/username 透传、侧边栏去重、8 页全渲染、模块级 app 实例。

## Web SSH 真实回环端到端（test_web_ssh.py）

用 paramiko 在进程内起最小 SSH 服务器（Transport + ServerInterface，
密码认证 + exec + shell 回显，127.0.0.1 随机端口），`SSHSession` 真连它：
- 欢迎横幅含 R-BOT ASCII + uname/uptime 输出。
- known_hosts 落盘到 DATA_DIR。
- `echo hello` 收发正确。
- 错误密码抛 AuthenticationException。
- 不依赖系统 sshd，真实 SSH 协议。

## 真实服务冒烟（29 项全过，/tmp/smoke_phase3.py，临时脚本不进仓库）

TestClient + 运行时从 ~/.oci/config 读真实 compartment（只放内存，
不写仓库文件、不打印）。全部只读，未创建/修改/删除任何资源。

| 验证项 | 结果 |
|---|---|
| 错误密码 401 / 正确密码登录 / me 返回 admin | 通过 |
| 8 个页面登录后 200、未登录 302 到 /login | 通过（16 项） |
| 真实实例列表 3 台 | 通过：名称与已知 3 台一致 |
| 真实配额 A1 4/4 核、18/24GB | 通过 |
| 真实配额 E2 1/2 台 | 通过 |
| 真实配额启动盘 141/200GB | 通过 |
| 启动盘 3 盘各 47GB / 孤儿盘 0 个 | 通过 |
| /api/system 返回真实 CPU% | 通过（2.5%） |
| 5 次失败后锁定 429，锁定时正确密码也进不去 | 通过 |
| 登出后 me 401 | 通过 |

## 真实 uvicorn 冒烟

`uvicorn web.app:app --host 127.0.0.1 --port 19527`（测试端口）：
- 未登录 /overview → 302 到 /login。
- 登录 → `{"ok":true,"username":"admin"}`，cookie 生效。
- 登录后 /overview → 200，/quota → 200。
- /api/system 返回真实数据（CPU 30.8%、内存 94.4%、硬盘 2.7%）。
- /api/quota（占位 compartment）→ 502 错误信封（符合预期，部署时配真实值）。
- 冒烟后服务器已停，无残留进程。

## 页面与原版截图对照（协调员亲自看图核对）

对照原版 terminal-list.jpg / cloud-create.jpg：
- **还原了**：三栏布局（顶栏 logo+语言+主题+明暗+退出 / 资源栏环形
  CPU·内存·硬盘仪表 + 网络↓↑ / 左侧边栏）；主机卡片（名称+密码/密钥徽标、
  平台行、标签 chips、IP 一键复制、用户名、上次使用+绿点、连接/编辑/删除）；
  创建实例四卡表单（基础/系统/网络与安全/高级）+ 快捷开机卡片网格 +
  蓝色"开始创建"；登录页（密码登录/Telegram 双标签，Telegram 为占位提示）；
  星夜主题深紫底 + CSS 星空点缀 + 右下角 CSS 月牙；实例状态徽标。
- **取舍了**：⚡付费功能全部去掉；星夜的二次元插画用纯 CSS 渐变星空+月牙
  代替；8 种主题只做经典+星夜两个；云厂商标签（Oracle/Azure/SolusVM）未做
  （只接 Oracle）；对象存储/DNS/用户管理/邮件服务页面不在本次范围；
  主机卡片的平台信息行显示备注（后端无 platform 字段）。
- **未做浏览器截图对比**：本机无浏览器，尝试装 Playwright + Chromium 时
  下载步骤触发审批未及时确认而超时，按规则未重试。页面验证靠 TestClient
  HTML 断言（17 个页面测试 + 12 个集成测试）与协调员读模板/截图人工对照。
  部署到用户服务器后建议用真实浏览器看一遍。

## 协调员修复记录（集成检查发现的 7 个问题）

1. **`uvicorn web.app:app` 启动失败（部署 bug）**：app.py 只有
   create_app 工厂，没有模块级 app 实例，标准启动方式报
   "Attribute app not found"。修复：哈希已设置时创建模块级 `app`
   实例；补 reload 测试。
2. **镜像 OCID 前后端不匹配（功能缺口）**：前端下拉传架构占位符
   （`..xxxxxxxx-aarch64`/`-x86`），后端要求真实 image_id，无解析逻辑，
   创建必失败。修复：config.py 加 IMAGE_IDS 查表（含查真实镜像的 OCI
   命令注释）；create.py 加 resolve_image_id()（占位符查表、真实 OCID
   直通、未配置报 400 提示去填 IMAGE_IDS）；补 6 个测试。
3. **`/quota` 死链**：侧边栏有配额链接但无页面路由，点过去 404。修复：
   pages.py 加 GET /quota + 新写 quota.html / quota.js（调 /api/quota，
   三项额度进度条）；补 2 个测试。
4. **模板上下文缺失**：pages.py 没传 current_page/username，侧边栏高亮
   与顶栏用户名全靠 JS 兜底。修复：_render() 统一传 current_page（由模板
   文件名推导）与 username（由 session 取）；补测试验证高亮与用户名渲染。
5. **侧边栏重复链接**："概览"与"主机"都指向 /overview。修复：合并为一个
   "主机"链接（该页本身就是主机卡片页），顶栏保留图标快捷方式；补测试。
6. **requirements.txt 重复段**：后端与 SSH 两个子代理各追加了一段阶段 3
   依赖。修复：合并为一段，保留已验证版本号注释。
7. **create.html 提示误导**：原提示称"后端按所选架构解析真实镜像"，但当时
   后端并无此逻辑。修复 2 落地后该提示成真，同步更新文案指向 IMAGE_IDS。

## 已知设计说明（不是 bug）

- **A1 抢机不传 shape-config**：oci/grabber.py 未支持，表单填的 OCPU/内存
  只用于配额预检，抢机按 OCI 默认形状配置创建。前端与 create.py 均已注明。
  如需按规格开，需先给 Grabber 加 shape-config（阶段 1 模块）。
- **创建表单部分字段后端暂未使用**：硬盘大小、数量、公网 IP/IPv6 开关、
  最小/最大延迟、启动脚本——前端随请求发送，后端暂未接入 Grabber。
- **SFTP 未实现**：终端页 SFTP 按钮为占位提示；计划见 docs/phase3-todo.md
  （阶段 3b：web/sftp.py + 滑出面板，含路径穿越防护与上传上限）。
- **分屏与批量命令未实现**：见 docs/phase3-todo.md，排在 SFTP 之后。
- **会话挂起已覆盖**：WS 断开后会话保留 5 分钟 + 快照回放，最多 20 会话。
- **终端角标是面板服务器资源**：/api/system 返回面板服务器自身的资源，
  不是远端 SSH 主机的；标题已注明。如需远端资源需另做每会话 exec 轮询。
- **实例列表无 OCPU/内存**：OCI list 不返回 shape-config，前端缺失时显示
  "—"，实例名下方补了创建时间。
- **主机不存密码**：hosts.json 只存 key_path 或 auth_type；密码认证的主机
  连接时每次现输密码（测试验证了文件里无密码明文）。
- **监控与每日报告只看默认 profile**：与阶段 2 一致，多账号监控待后续版本。

## 脱敏检查

- `web/`、`tests/test_web_*.py`、`requirements.txt`、`docs/phase3-todo.md`
  全扫：无真实 IP、无真实 OCID（20 位以上后缀）、无密码/Token。
  测试用占位 OCID（`..testxxxx`）与文档网段 203.0.113.x、回环 127.0.0.1。
- 注意：`docs/rbot-live.md` 与 `docs/verification.md` 是内部验证记录，
  含少量真实 IP（无真实 OCID）。**公开到 GitHub 前必须清理这两份文档**
  （阶段 1 遗留，非阶段 3 引入）。

## 部署说明（给阶段 3 试用）

```bash
cd ~/workspace/rbot-clone
pip install -r requirements.txt
python3 -m web.auth            # 交互生成管理员密码哈希
export RBOT_WEB_PASSWORD_HASH='<生成的哈希>'
export RBOT_WEB_SECRET='<任意随机长字符串>'   # 可选，不设则每次启动随机
# 填真实值：web/config.py 的 PROFILES（compartment）与 IMAGE_IDS（镜像 OCID）
uvicorn web.app:app --host 127.0.0.1 --port 9527
# 浏览器打开 http://127.0.0.1:9527/login
```

注意：面板默认只监听 127.0.0.1；要从外网访问请在前面加反向代理 + HTTPS，
不要直接把 9527 暴露到公网。OCI CLI 与 ~/.oci/config 须在面板服务器上配好。
