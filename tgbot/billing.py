"""账单预警：每天检查各账号本月费用与付费实例。

搬自 ~/workspace/oracle-billing/check_billing.py（逻辑一致，
调用方式改走 OCI CLI，与机器人其余部分一致）：
- 每账号本月至今（MTD）费用 vs SGD 1.00 预算（用量 API）。
- 检测本月费用较上次检查的变化（state 文件记上次费用）。
- 列各订阅区域的实例；RUNNING 且形状不是免费规格（A1.Flex /
  E2.1.Micro）的付费实例自动 STOP（只停机不删除）。
- 检测免费额度超限（A1 > 4 OCPU / 24GB，E2 > 2 台）。

本模块是同步逻辑（OCI CLI 调用），定时任务在
tgbot/handlers_extra.billing_check_job 里，经 run_sync 调用。
NEVER_STOP_NAMES 是工作服务器的保护名单：即使分类误判也不停。
"""

import json
import os
from datetime import datetime, timezone

from tgbot import config as bot_config
from oci.instance import list_instances, stop_instance

BILLING_STATE_FILE = os.path.join(bot_config.DATA_DIR,
                                  "billing_state.json")
BUDGET_SGD = 1.00
FREE_SHAPES = {"VM.Standard.A1.Flex", "VM.Standard.E2.1.Micro"}
A1_OCPU_LIMIT = 4
A1_MEM_LIMIT = 24
E2_COUNT_LIMIT = 2
# 工作服务器：即使分类误判也绝不停（新旧名字都留着，防改名）。
NEVER_STOP_NAMES = {
    # 填你自己的工作服务器实例名（显示名），即使分类误判也绝不停。
    # 例："my-main-server",
}


def month_bounds(now=None):
    """本月起止（UTC 午夜）：(本月 1 日 00:00, 下月 1 日 00:00)。"""
    now = now or datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if now.month == 12:
        end = start.replace(year=now.year + 1, month=1)
    else:
        end = start.replace(month=now.month + 1)
    return start, end


def fetch_mtd_cost(cli, tenancy_id, now=None):
    """查某租户本月至今费用，返回 (总费用, 币种)。

    用量 API 要求日期精度到天（时/分/秒为 0），month_bounds 已保证。
    """
    start, end = month_bounds(now)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    result = cli.run(["usage-api", "usage-summary",
                      "request-summarized-usages",
                      "--tenant-id", tenancy_id,
                      "--time-usage-started", start.strftime(fmt),
                      "--time-usage-ended", end.strftime(fmt),
                      "--granularity", "MONTHLY"])
    data = result.get("data") or {} if isinstance(result, dict) else {}
    items = data.get("items") or []
    total = 0.0
    currency = "SGD"
    for item in items:
        total += float(item.get("computed-amount") or 0)
        # 零费用账号的币种可能是空白字符串（真实 API 实测：' '），
        # strip 后为空时保留默认值 SGD。
        cur = (item.get("currency") or "").strip()
        if cur:
            currency = cur
    return total, currency


def list_subscribed_regions(cli, tenancy_id):
    """查某租户已订阅且 READY 的区域代码列表。"""
    result = cli.run(["iam", "region-subscription", "list",
                      "--tenancy-id", tenancy_id])
    data = result.get("data") or [] if isinstance(result, dict) else []
    return [r.get("region-name") for r in data
            if r.get("status") == "READY" and r.get("region-name")]


def check_account_billing(profile, now=None):
    """检查一个账号的账单与实例，返回结果字典。

    付费 RUNNING 实例会被自动 STOP（结果里 stopped 列出已停的，
    stop_errors 列出停失败的）；工作服务器在 NEVER_STOP_NAMES 里
    的绝不停。免费额度超限记在 free_over 里。
    """
    cfg = bot_config.get_profile_config(profile)
    tenancy_id = cfg["compartment_id"]
    cli = bot_config.get_oci_client(profile=profile)
    mtd_cost, currency = fetch_mtd_cost(cli, tenancy_id, now)
    regions = list_subscribed_regions(cli, tenancy_id)

    instances = []
    stopped = []
    paid_found = []
    stop_errors = []
    a1_ocpus = 0.0
    a1_mem = 0.0
    e2_count = 0
    for region in regions:
        region_cli = bot_config.get_oci_client(profile=profile,
                                               region=region)
        try:
            region_instances = list_instances(region_cli, tenancy_id)
        except Exception as exc:  # noqa: BLE001 - 单区失败不影响其他区
            instances.append({"region": region, "error": str(exc)})
            continue
        for inst in region_instances or []:
            shape_config = inst.get("shape-config") or {}
            ocpus = shape_config.get("ocpus")
            mem = shape_config.get("memory-in-gbs")
            rec = {
                "name": inst.get("display-name"),
                "id": inst.get("id"),
                "shape": inst.get("shape"),
                "state": inst.get("lifecycle-state"),
                "region": region,
                "ocpus": ocpus,
                "memory": mem,
            }
            instances.append(rec)
            if inst.get("lifecycle-state") != "RUNNING":
                continue
            if inst.get("shape") == "VM.Standard.A1.Flex":
                a1_ocpus += ocpus or 0
                a1_mem += mem or 0
            elif inst.get("shape") == "VM.Standard.E2.1.Micro":
                e2_count += 1
            elif inst.get("shape") not in FREE_SHAPES:
                paid_found.append(rec)
                if inst.get("display-name") in NEVER_STOP_NAMES:
                    continue
                try:
                    stop_instance(region_cli, inst["id"])
                    stopped.append(rec)
                except Exception as exc:  # noqa: BLE001
                    stop_errors.append({"instance": inst.get("display-name"),
                                        "error": str(exc)})

    free_over = []
    if a1_ocpus > A1_OCPU_LIMIT or a1_mem > A1_MEM_LIMIT:
        free_over.append(
            f"A1 超免费额度: {a1_ocpus} OCPU / {a1_mem} GB "
            f"(上限 4 OCPU / 24 GB)")
    if e2_count > E2_COUNT_LIMIT:
        free_over.append(f"E2.1.Micro {e2_count} 台 (免费上限 2 台)")

    return {
        "profile": profile,
        "mtd_cost": round(mtd_cost, 4),
        "currency": currency,
        "budget": BUDGET_SGD,
        "over_budget": mtd_cost > BUDGET_SGD,
        "regions": regions,
        "instances": instances,
        "paid_running": paid_found,
        "stopped": stopped,
        "stop_errors": stop_errors,
        "a1_ocpus": a1_ocpus,
        "a1_mem": a1_mem,
        "e2_count": e2_count,
        "free_over": free_over,
    }


def load_billing_state(path=None):
    """读上次检查的费用状态：{profile: {"mtd_cost", "checked_at"}}。"""
    path = path or BILLING_STATE_FILE
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_billing_state(state, path=None):
    """写费用状态（只记各账号本月费用与检查时间）。"""
    path = path or BILLING_STATE_FILE
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    return state


def collect_billing_alerts(results, prev_state):
    """从检查结果与上次状态收集需要通知的告警（空列表表示无变化）。

    告警条件（与 check_billing.py 一致）：
    - 自动停了付费实例 / 停付费实例失败；
    - 付费实例在跑但未停（在保护名单里）；
    - 本月费用较上次检查增加超过 0.01（新增收费）；
    - 免费额度超限；
    - 某账号检查失败。
    注意：不单独对“超预算”告警——本月费用已超预算但无新增时
    （如 某账号本月已知的 13.46 历史账单）不重复打扰；
    有新增收费时 delta 告警会带上当前费用与预算。
    """
    alerts = []
    for res in results:
        profile = res.get("profile", "?")
        label = bot_config.profile_display_name(profile)
        if res.get("error"):
            alerts.append(f"[{label}] 账单检查失败：{res['error']}")
            continue
        prev = (prev_state or {}).get(profile, {})
        prev_cost = prev.get("mtd_cost")
        cost = res.get("mtd_cost", 0)
        currency = res.get("currency", "SGD")
        stopped_names = {s.get("name") for s in res.get("stopped") or []}
        for stopped in res.get("stopped") or []:
            alerts.append(
                f"[{label}] 已自动停掉付费实例 {stopped.get('name')} "
                f"({stopped.get('shape')}, {stopped.get('region')})")
        for err in res.get("stop_errors") or []:
            alerts.append(
                f"[{label}] 停付费实例 {err.get('instance')} 失败："
                f"{err.get('error')}")
        for paid in res.get("paid_running") or []:
            if paid.get("name") not in stopped_names:
                alerts.append(
                    f"[{label}] 发现付费实例在跑但未停（在保护名单）："
                    f"{paid.get('name')} ({paid.get('shape')}, "
                    f"{paid.get('region')})")
        if prev_cost is not None and cost - prev_cost > 0.01:
            delta = round(cost - prev_cost, 4)
            alerts.append(
                f"[{label}] 本月费用新增 {delta} {currency}，"
                f"当前 {cost:.2f} {currency}"
                f"（预算 {res.get('budget', BUDGET_SGD):.2f} {currency}）")
        for over in res.get("free_over") or []:
            alerts.append(f"[{label}] {over}，超出部分会收费")
    return alerts


def fmt_billing_alerts(alerts):
    """告警通知全文。"""
    lines = ["⚠️ Oracle 账单预警：", ""]
    lines.extend(f"• {alert}" for alert in alerts)
    return "\n".join(lines)
