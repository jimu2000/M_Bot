# 验证记录（阶段 1：OCI 核心）

验证日期：2026-11-23。验证账号：新加坡账号 A（DEFAULT / ap-singapore-1），
另用圣何塞账号 B（ACCOUNT2 / us-sanjose-1）验证了启动盘排除逻辑。
本文档里 OCID 与 IP 一律打码（只留后 4 位或前两段）。

## 单元测试（mock，不碰真实 API）

`python3 -m unittest discover -s tests`：**100 通过，0 失败**。

| 测试文件 | 测试数 | 覆盖要点 |
|---|---|---|
| test_client.py | 17 | 错误分类（无容量/429/配额/认证/不存在）、profile/region 追加、SUPPRESS_LABEL_WARNING、空输出、per-call 覆盖 |
| test_instance.py | 22 | 每个函数；终止默认删引导卷（断言 `--preserve-boot-volume false`）、`--force`、launch 的 `--no-retry` 默认存在 |
| test_network.py | 17 | 每个函数；change_public_ip 全流程调用顺序、无主 VNIC/内网 IP 时提前报错且不创建 IP |
| test_volume.py | 17 | 每个函数；孤儿盘判断（有/无挂载、DETACHED 仍算孤儿）、TERMINATED/TERMINATING 盘不计入总量 |
| test_quota.py | 15 | A1/E2/启动盘统计；can_launch 覆盖能开与不能开（E2 满、A1 OCPU 不够、A1 内存不够、启动盘不够、未知规格） |
| test_grabber.py | 12 | 成功返回 OCID、无容量返回 None、429 退避序列 [60, 120]、配额停下且 on_event 恰调一次、CPU 不对时终止并继续抢、抖动区间 [20, 30] |

## 真实 API 实测（只读操作）

| 功能 | 实测方法 | 结果 |
|---|---|---|
| client.run（实例列表） | list 新加坡实例 | 3 台，与已知状态一致（2 台 A1.Flex + 1 台 E2.1.Micro，全部 RUNNING） |
| client 错误分类 | get 不存在的实例 OCID | 正确抛 NotFoundError（见下方"发现的问题"第 1 条） |
| instance.list_instances | 同上 | 3 台，名称/shape/状态正确 |
| instance.get_instance | 用 list 结果动态取 OCID | 返回完整详情，AD 为 `wgwP:AP-SINGAPORE-1-AD-1` |
| instance.get_instance_state | 同上 | 返回 `RUNNING` |
| instance.find_instance_by_name | 存在/不存在的显示名 | 存在找到对应实例，不存在返回 None |
| instance 错误处理 | get_instance_state 对不存在的 OCID | 正确抛 NotFoundError |
| network.list_vnics | 动态取一台实例 | 1 个 VNIC（主 VNIC，内网 10.0.0.69） |
| network.get_public_ip | 同上 | 成功取到公网 IP（203.0.113.x） |
| network.list_public_ips | compartment 列表 | 1 个保留 IP（198.51.100.x，RESERVED，AVAILABLE，未绑定） |
| network.list_private_ips | 主 VNIC | 1 个内网 IP，主 IP 10.0.0.69 |
| network 错误处理 | get_vnic 对不存在的 VNIC id | 正确抛 NotFoundError |
| volume.list_boot_volumes | 新加坡 compartment | 3 盘，各 47GB，全 AVAILABLE |
| volume.total_boot_volume_gb | 新加坡 | 141GB |
| volume.total_boot_volume_gb | **圣何塞**（有 2 盘已删除） | **94GB**（正确排除了 2 盘 TERMINATED；旧实现会误算 188GB） |
| volume.list_orphan_boot_volumes | 新加坡 | 0 个（3 条挂载记录全为 ATTACHED） |
| volume.list_volumes | 新加坡 | 0 个块存储卷 |
| volume.get_boot_volume | 动态取一盘 | 详情正常 |
| volume 错误处理 | get_boot_volume 对不存在的 OCID | 正确抛 NotFoundError |
| quota.get_free_tier_summary | 新加坡 | A1 4/4 核（已满）、18/24GB；E2 1/2 台；启动盘 141/200GB；overall_ok=True |
| quota.can_launch E2.1.Micro | 新加坡 | (True, "可以创建") |
| quota.can_launch A1.Flex 1核6GB | 新加坡 | (False, "A1 OCPU 不足（已用 4/4 核，需要 1 核）") |
| quota.get_boot_volume_usage | 圣何塞 | 94GB，与 volume.total_boot_volume_gb 一致 |
| grabber.find_existing | 按显示名查实例 | 跑通，返回合法实例 OCID（…elva） |

## 写操作验证（未对真实 API 执行）

以下写操作**没有对真实 API 执行过任何一次**（编排器正在真实抢机，测试不能干扰它；
生产实例不能乱动）：

| 功能 | 验证方式 |
|---|---|
| instance.launch_instance | mock 断言命令构造（含 `--no-retry`、各参数）；命令本身与编排器已验证的 launch 命令一致 |
| instance.start/stop/restart | mock 断言命令构造（`--action START/STOP/RESET`） |
| instance.terminate_instance | mock 断言默认 `--preserve-boot-volume false` + `--force`；命令与编排器已验证的 terminate 命令一致 |
| network.create/assign/delete/change | mock 断言命令构造与 change_public_ip 的 4 步调用顺序 |
| volume.resize/delete | mock 断言命令构造（update 带 `--size-in-gbs`、delete 带 `--force`）；`size_in_gbs <= 0` 抛 ValueError 且不发命令 |
| grabber.try_launch_once / run | 全部 mock（OCIClient 是 MagicMock）；429 退避、配额停下、CPU 筛选释放流程均有 mock 测试覆盖 |

## 验证中发现并修复的问题

1. **OCI 的 NotAuthorizedOrNotFound（client.py）**：对不存在的资源，OCI 返回 code
   `NotAuthorizedOrNotFound`（404，防枚举设计），原先被误判为 AuthError（因为
   "NotAuthorized" 是它的子串且排在前面）。已把 `NotAuthorizedOrNotFound` 提前到
   `NotAuthorized` 之前，正确分类为 NotFoundError，并加了回归测试。
2. **`public-ip list` 必须带 `--scope`（network.py）**：初版没带，真实 API 报错
   `Missing option(s) --scope`。已修复为 `--scope REGION`，并发现 REGION 作用域
   只返回保留 IP（临时 IP 是可用区级的），已记入文档字符串。
3. **孤儿盘不能只看 lifecycle-state（volume.py）**：启动盘没有 IN-USE 状态，挂在
   运行中实例上的盘同样是 AVAILABLE，且不带 instance-id 字段。第一版按
   "AVAILABLE + 无 instance-id" 判断，把新加坡 3 盘全误判成孤儿。已改为查挂载关系
   （`oci compute boot-volume-attachment list`，在 compute 组下、--availability-domain
   必填），孤儿 = AVAILABLE 且无 ATTACHED/ATTACHING 记录。修正后实测 0 个孤儿。
4. **启动盘总量要排除已删除的盘（volume.py + quota.py）**：`bv boot-volume list`
   会返回 TERMINATED 的盘。旧实现全算进去，圣何塞会误算 188GB（实际 94GB）。
   已改为排除 TERMINATING/TERMINATED，两模块口径统一，圣何塞实测 94GB 一致。
   同时 quota.py 原先只统计 AVAILABLE 的盘，已同步改为排除法（两者在新加坡结果
   相同，因为启动盘没有 IN-USE 状态）。

## 没测的（为什么）

- **写操作对真实 API 的端到端**：创建/终止/启停/换 IP/扩容/删盘都不能在生产实例
  上试。命令构造与编排器已验证的命令一致（launch、terminate），其余靠 mock 断言。
  等新 E2 抢到后，可用那台新实例做一次真实的启停测试（不影响生产）。
- **grabber.run 的真实抢机循环**：编排器正在真实抢机，grabber 的循环不能同时跑
  （会重复创建）。grabber 的逻辑与编排器同源，单次 launch 与防重复建查询已实测，
  循环部分靠 mock 测试覆盖。
- **多 profile / 多 region 的写操作**：同上，写操作一律没测。读操作已在 DEFAULT
 （新加坡）与 ACCOUNT2（圣何塞）两个 profile 上实测通过。
- **芝加哥账号 C（ACCOUNT3）**：未实测（该账号只有 1 台 A1，无 E2 机型，
  与阶段 1 的 E2 抢机无关）。client.py 的 per-call profile/region 覆盖有 mock 测试。

## 附带发现（与阶段 1 无关，但值得跟进）

- **新加坡 compartment 里有个未绑定的保留公网 IP**（198.51.100.x，RESERVED，
  AVAILABLE，未绑定任何实例）。未绑定的保留 IP 在 OCI 里可能产生费用，建议查一下
  账单，确认是否需要删除。这是 network.list_public_ips 实测时发现的。
