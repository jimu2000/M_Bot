# 阶段 4 验证记录：Cloudflare DNS + 其他云框架 + MCP 服务器

验证时间：2026-11-23（UTC 约 09:25）。验证人：阶段 4 协调员（亲自复查全部
代码 + 跑全量测试 + 真实服务冒烟）。4 个子代理并行开发，协调员做集成检查。

## 结论

- **583 个单元测试全过**（`python3 -m unittest discover -s tests`，约 10 秒）：
  438 个阶段 1/2/3/3b 旧测试 + **145 个阶段 4 新测试**，**零回归**。
- **真实服务冒烟全过**：TestClient 启动真实应用，OpenAPI 含全部 7 个 DNS
  操作，/dns 未登录 302、API 未登录 401（路由已注册且鉴权生效）。
- **MCP 真实 stdio 冒烟通过**：initialize → tools/list → tools/call 三步
  正常，`cf_list_zones` 未配凭据时按预期返回 ok:false 错误信封。
- 写操作（CF 创建/更新/删除记录）**零次碰真实资源**，全部用 mock 断言。
- **真实 Cloudflare API 验证待主代理做**（见"未验证"一节）：阶段 4 工人
  按要求绝不读取用户真实 CF 凭据，只读验证需主代理用安全方式跑。
- 编排器全程未受干扰（验证结束时 PID 3836，第 167 次尝试，仍无容量）。

## 代码规模

| 类别 | 文件 | 行数 |
|---|---|---|
| CF 客户端 | clouds/cloudflare.py（新） | 317 |
| 云抽象接口 + 5 stub | clouds/base.py + aws/gcp/azure/do/solusvm.py（新） | 543 |
| MCP 服务器 | mcp_server.py（新） | 435 |
| Web DNS 路由 | web/routes/dns.py（新） | 184 |
| Web DNS 前端 | templates/dns.html + static/js/dns.js（新） | 359 |
| Telegram /domain | tgbot/handlers_extra.py、keyboards.py、bot.py（改） | 约 +200 |
| 测试 | test_cloudflare / test_clouds_stubs / test_mcp_server / test_web_dns / test_tgbot_domain（新） | 1813 |

## 一、Cloudflare DNS 管理

### 客户端（clouds/cloudflare.py，317 行，34 个测试）

- httpx 同步客户端（0.28.1 已装，无需安装）。配置区顶端：API_BASE_URL、
  DEFAULT_TIMEOUT、DEFAULT_PER_PAGE、环境变量名 CF_API_TOKEN / CF_API_KEY /
  CF_EMAIL，全部占位符说明。
- 两种认证：API Token（Bearer）优先，否则 Global Key + Email
  （X-Auth-Key / X-Auth-Email）；两者都没有时构造即抛
  CloudflareError(kind="config")。
- CloudflareError 带 kind：config / auth（401/403）/ not_found（404 或
  error code 7000/7003/7007/7008）/ rate_limit（429，Retry-After 只记录
  在 retry_after 不重试）/ api（其他 success=false）/ network（httpx 异常）。
- 接口：list_zones（分页取全）、get_zone、list_dns_records（分页取全）、
  create/update/delete_dns_record、set_proxied（GET 后 PUT 只改 proxied）、
  模块级 get_client()。create/update 校验 type 只允许
  A/AAAA/CNAME/TXT/MX/NS/SRV/CAA，非法抛 ValueError。
- 踩坑已处理：httpx 的 base_url + 绝对路径 "/zones" 会顶掉 /client/v4
  前缀，改为不设 base_url、内部拼完整 URL（测试断言完整路径）。
- 测试全部用 httpx.MockTransport，无真实请求；数据全是文档保留值。

### Web DNS 页面（25 个测试）

- 6 个 API（web/routes/dns.py）：GET zones、GET records（?type=&name= 过滤）、
  POST 创建、PUT 更新、DELETE 删除、POST proxied 切换；全部需登录。
  type 用 Literal[A/AAAA/CNAME/TXT/MX]，非法 422。错误映射：config/auth→502
  固定文案、not_found→404、rate_limit→429、其余→502，**不返回 API 原始
  错误细节**。
- 页面：GET /dns（pages.py 新增，current_page="dns"），侧边栏"任务管理"
  后加"DNS 管理"链接（base.html），模板 dns.html（Zone 下拉、记录表格、
  添加/编辑弹窗表单、类型+名称筛选），dns.js（RBot.api 调接口、
  textContent 渲染防 XSS、删除用 RBot.confirmDialog 二次确认、代理状态
  可点击切换）。`node --check` 通过。
- 并行开发时的容错：dns.py 对 clouds.cloudflare 的 import 包了
  try/except ImportError，未就绪时用本地替身 CloudflareError（同样有
  .kind），接口报 502。协调员已核实：真实模块落地后
  `dns.get_client is clouds.cloudflare.get_client` 为 True，替身未触发。

### Telegram /domain（18 个测试）

- handlers_extra.py：`handle_domain`（/domain，白名单 @require_allowed，
  列 Zone + 内联键盘，Zone 列表存 user_data）、`domain_callback`
  （domain:zone:<id> 查记录、domain:back 回列表）、格式化函数（行格式
  `类型 名称 -> 内容`，proxied 标 🟠）、fmt_cf_error（kind→中文，不泄露
  原始细节）；记录超 20 条截断并提示总数。
- keyboards.py：domain_zones_keyboard / domain_records_keyboard，
  callback_data 前缀 "domain:"（zone id 32 位十六进制，44 字节，未超
  64 字节限制）。
- bot.py：注册 CommandHandler("domain") 与 CallbackQueryHandler
  (pattern=r"^domain:")。同样有 ImportError 替身容错，协调员已核实
  真实模块已接上。

## 二、其他云框架（28 个测试）

- clouds/base.py：CloudProvider 抽象基类，11 个抽象方法（方法名与
  oci/instance.py、oci/network.py 对齐：list/get/create/start/stop/
  restart/terminate_instance、list_public_ips、change_public_ip、
  list_volumes、get_quota），返回统一 snake_case 形状；类属性
  PROVIDER_NAME / display_name；terminate docstring 保留 OCI 硬规则
  （默认同时删引导卷）。
- 5 个 stub（各 72–77 行）：AWSProvider（boto3）、GCPProvider
  （google-cloud-compute）、AzureProvider（azure-mgmt-compute）、
  DOProvider（pydo/REST）、SolusVMProvider（SolusVM API v1，注明 R-Bot
  原版用它管 racknerd 等 VPS）。文件头写明凭据环境变量、SDK 与官方
  文档链接；全部 11 个方法统一抛
  NotImplementedError("尚未实现：<厂商> <方法名>")；__init__ 只保存
  配置、不做真实连接、缺凭据不报错。
- clouds/__init__.py 追加 6 个类导出（原有 docstring 未动）。
- 测试：基类 __abstractmethods__ 与 11 方法清单一致、抽象基类不可实例化；
  每厂商 5 个测试，全部 11 个方法都断言抛错（55 个 subTest）。

## 三、MCP 服务器（40 个测试）

- **实现方式：标准库最小 JSON-RPC over stdio，非 MCP Python SDK**。
  `pip install mcp` 装不上：系统 Python 被 PEP 668 锁定，`pip install`
  与 `pip install --user` 均被拒。按任务要求改用标准库实现，零新增
  依赖；行为与 FastMCP 版一致（同 6 个工具、同参数、同返回值结构），
  将来装上 SDK 后可直接把工具函数挂到 FastMCP 上。
- 配置区顶端：服务器名 rbot-clone、版本 0.1.0、默认 profile/region 从
  RBOT_OCI_PROFILE / RBOT_OCI_REGION 读（默认 DEFAULT / ap-singapore-1，
  注明仅为示例值），RBOT_OCI_COMPARTMENT_ID 默认占位符 OCID。
- 6 个工具（`get_tools()` 可取）：oci_list_instances、
  oci_instance_detail、oci_quota、oci_free_tier_summary、cf_list_zones、
  cf_list_dns_records。错误返回 {"ok": false, "error": "..."} 而非抛异常
  （捕获 OCIError/CloudflareError/ValueError，CF 错误含 .kind 时拼进
  消息）；JSON-RPC 支持 initialize / tools/list / tools/call，
  notifications/* 忽略不回；工具级错误置 isError: true；标准错误码
  -32601/-32602/-32603/-32700。
- 与任务约定的 3 处偏差（均为按真实代码确认后的必要调整，协调员认可）：
  1. **compartment_id 参数**：底层 list_instances / get_free_tier_summary
     都必填 compartment_id，4 个 OCI 工具各加可选 compartment_id=""
     参数，空则用配置区默认值。
  2. **详情函数名**：instance.py 里没有 get_instance_detail，真实函数是
     get_instance，已按实际调用（返回时解包 {"data": …}）。
  3. **public_ip 不可用**：list_instances 只返回 id/display-name/shape/
     lifecycle-state/time-created 五个字段，无公网 IP（拿公网 IP 要再按
     实例查 VNIC，本工具不做额外调用）。返回字段为
     id/name/shape/state/created。
- oci_quota 与 oci_free_tier_summary 的区分：前者返回三项原始用量明细，
  后者返回汇总（used/limit/remaining + overall_ok），避免重复。
- clouds.cloudflare 导入做容错（ImportError 时 CF 工具返回"模块未就绪"，
  不拖垮服务器）。工人实测：真实 CF 模块落地后接口与约定完全一致。
- 修过一个真 bug：工具函数 oci_quota 与导入的 oci.quota 模块同名遮蔽，
  已把模块别名改为 oci_quota_mod。
- 测试：工具注册（6 个名字/参数清单/required）、每个工具 mock 调用参数
  透传与返回值结构、错误路径（ok:false 不抛异常）、CF 模块缺失容错
  （sys.modules 阻塞 + importlib.reload 模拟 ImportError）、JSON-RPC
  round-trip 与 serve_stdio 的 StringIO 端到端会话。

## 真实服务冒烟（协调员，/tmp/smoke_phase4.py，临时脚本不进仓库）

| 验证项 | 结果 |
|---|---|
| OpenAPI 含 5 条 DNS 路径、7 个操作（6 API + 1 页面） | 通过 |
| GET /dns 未登录 → 302 /login | 通过 |
| GET /api/dns/zones 未登录 → 401 | 通过（路由已注册，鉴权生效） |
| GET records 未登录 → 401 | 通过 |
| MCP stdio：initialize → tools/list → tools/call | 通过（cf_list_zones 未配凭据返回错误信封，预期行为） |

注：本版 FastAPI 的 app.routes 含 _IncludedRouter 包装，枚举不可靠，
冒烟改查 OpenAPI（与阶段 3b 做法一致）。records 路径 GET+POST 复用、
record_id 路径 PUT+DELETE 复用，故 5 条路径对应 7 个操作。

## 协调员复查记录

1. **全量测试**：583 个全过（约 10 秒），438 旧测试零回归。
2. **接口对接核实**：`web.routes.dns.get_client`、
   `tgbot.handlers_extra.get_client` 均已是 clouds.cloudflare 的真实
   get_client（`is` 比较为 True），并行开发期的 ImportError 替身已不触发。
3. **代码逐行复查**：dns.py（错误映射、Literal 校验、固定文案不泄露）、
   cloudflare.py 前 120 行（配置区、认证、_request 分类）、tgbot domain
   注册（bot.py 两处、keyboards 两函数、handlers_extra 约 200 行新增）。
   未发现安全问题。
4. **脱敏检查**：全部阶段 4 新文件 grep 扫描（真实 IP 段、20 位以上 OCID
   后缀、指纹格式）：无命中。测试用 example.com、203.0.113.x、假 zone id、
   占位 OCID（..testxxxx）。
5. **JS 语法**：`node --check web/static/js/dns.js` 通过。
6. **编排器**：验证结束时 PID 3836 正常运行（第 167 次尝试），未受干扰。

## 未验证（待主代理做）

- **真实 Cloudflare API 只读验证**：列 Zone、列 DNS 记录。阶段 4 工人按
  任务要求绝不读取用户真实 CF 凭据、全部用 mock 测试。主代理需用安全
  方式（环境变量注入真实 Token，不写进代码、不回显）跑一次：
  `CF_API_TOKEN=... python3 -c "from clouds.cloudflare import get_client; ..."`
  列 Zone 与其中两个域名的记录，确认返回形状与 mock 一致。
- **真实 Telegram /domain 联调**：与阶段 2 相同，待用户提供机器人 Token
  后做（mock 测试已覆盖命令与回调全路径）。
- **真实浏览器看 DNS 页面**：与阶段 3 相同，本机无浏览器，部署后建议
  真实浏览器看一遍（TestClient 已验证页面 200 与 dns.js 引用）。

## 已知小缺口（不阻塞，可后续修）

- **MCP 用标准库而非官方 SDK**：功能等价，但协议细节（如新版 MCP 的
  elicitation、roots）未实现；部署到支持 MCP 的客户端前建议用真实
  客户端（如 Claude Desktop）跑一次兼容性测试。
- **Web DNS 页无分页**：记录数很多的 Zone 一次全拉（CF 客户端已翻页取全，
  前端直接渲染全量）；后续可加前端分页。
- **domain_records_keyboard 的 records 参数未用**：当前记录只读展示，
  未挂单条操作按钮（删除/切换代理），签名保留供后续扩展。
- **CF 客户端无重试**：429 只记录 Retry-After 不重试；Web 层直接返回 429
  给用户，后续可在客户端加一次退避重试。
- **其他云全是 stub**：AWS/GCP/Azure/DO/SolusVM 均未实现，调任何方法都抛
  NotImplementedError；VirtFusion 连 stub 都没有（R-Bot 顶层命令有
  /virtfusion，阶段 4 未排入）。

## 与 R-Bot 原版的剩余差异（阶段 4 范围）

- DNS 管理：原版侧边栏有 DNS 管理（本阶段已做）；原版还支持 Cloudflare
  的 Page Rules /  workers 等，本克隆只做 DNS 记录管理。
- 其他云：原版 /aws /gcp /azure /do /solusvm /virtfusion 均可实际管理实例，
  本克隆只有接口 + stub，未实现任何一家。
- MCP：原版无 MCP（这是克隆的增值功能，超出原版）。
- 对象存储 / 邮件服务：原版侧边栏有这两项，本克隆未做（不在阶段 4 范围）。

## 真实 CF API 只读验证（主代理 2026-11-23 完成）

- 方法：在服务器上跑临时脚本 verify_cf_live.py（已删除），凭据从环境变量读（不打印），只做 list_zones + list_dns_records 只读操作。
- 结果：6 个 Zone（域名已脱敏），全部 active；每 Zone 记录数 6-14 条；记录形状检查通过（id/type/name/content/proxied/ttl/zone_id/zone_name 8 个键齐全）。
- 其中一个 Zone 有 7 条记录（A:1、MX:3、TXT:3），与 CF Email Routing 配置一致。
- 临时文件已清理（/tmp/cf-verify 删除，服务器上为验证装的 python3-httpx 保留，无害）。
