#!/usr/bin/env python3
"""Web 层共享 OCI 数据缓存（第 5 轮 CPU 优化：跨端点去重；第 6 轮：后台预热）。

问题：实例列表 / 配额 / 磁盘三个端点在冷请求里重复查同一份数据——
  /api/instances 调 list_instances，
  /api/volumes 调 list_boot_volumes + list_instances（实例名映射），
  /api/quota/a1 调 list_instances；
  5 个端点并行打过来时 list_instances 被重复调 3 次。
各端点自己的响应缓存（30 秒）只对“同一端点重复访问”有效，
跨端点去重要靠这一层：原始列表按 (profile, region, compartment)
缓存 30 秒，各端点按需精简，不再重复调 SDK。

只影响 Web 面板读接口：机器人抢机（Grabber）与 MCP 直接调 SDK，
不走这里，新鲜度不受影响。缓存只存原始列表，调用方只读不改
（slim_instances / quota 统计都只读原始项并新建结果）。

刷新：refresh=true 时绕过本缓存（与各端点响应缓存的 refresh 语义一致）。
写操作（启停/终止/改规格/扩容/删盘/抢机完成等）成功后调
clear_oci_cache() 清掉，避免面板显示旧数据。

第 6 轮后台预热（start_cache_prewarm）：守护线程每 10 秒扫一次缓存，
把“已有人访问过、且 10 秒内就要过期”的条目提前刷一遍——
只保暖、不点火：缓存里没有的条目（没人看过的账号/区域）不预热，
面板闲置时不产生后台 OCI 调用；有人在用时缓存永不过期，
冷请求变成缓存命中，点面板不再等 OCI。单条刷新失败只记日志，
不影响其他条目与线程本身。
"""

import logging
import threading
import time

logger = logging.getLogger(__name__)

# ==================== 配置区（放文件顶端，方便修改）====================
# 数据缓存 TTL（秒）：与各端点响应缓存的 30 秒一致。面板是只读展示，
# 30 秒内的旧数据可接受；机器人抢机不走这里，不受影响。
OCI_CACHE_TTL = 30
# 预热线程唤醒间隔（秒）：每轮只扫一次缓存，本身不调 OCI。
OCI_CACHE_PREWARM_INTERVAL = 10
# 提前刷新窗口（秒）：条目在这么久内过期就提前刷，保持缓存常热。
# 须小于 TTL，否则每轮都刷；与唤醒间隔相等时最坏在过期前一轮刷到。
OCI_CACHE_PREWARM_AHEAD = 10
# =====================================================================

_CACHE = {}
_CACHE_LOCK = threading.Lock()
_prewarm_thread = None
_prewarm_stop = threading.Event()
_prewarm_lock = threading.Lock()


def _key(cli, compartment_id, kind):
    return (cli.profile, cli.region, compartment_id, kind)


def _get(key, refresh):
    if refresh:
        return None
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
    if entry and time.monotonic() < entry["expires"]:
        return entry["value"]
    return None


def _put(key, value, cli=None, compartment_id=None):
    with _CACHE_LOCK:
        _CACHE[key] = {"expires": time.monotonic() + OCI_CACHE_TTL,
                       "value": value,
                       "cli": cli, "compartment_id": compartment_id}


def clear_oci_cache(profile=None):
    """清共享数据缓存：指定 profile 时只清该账号，否则全清。

    实例/启动盘写操作成功后调用；单元测试在 setUp 里调用，
    避免缓存跨测试串数据。
    """
    with _CACHE_LOCK:
        if profile is None:
            _CACHE.clear()
        else:
            for key in [k for k in _CACHE if k[0] == profile]:
                del _CACHE[key]


def cached_raw_instances(cli, compartment_id, refresh=False):
    """取实例原始列表（sdk_list_instances 的返回值），跨端点共享缓存。

    返回原始列表本身（调用方只读不改；精简用 oci.instance.slim_instances）。
    """
    key = _key(cli, compartment_id, "instances")
    cached = _get(key, refresh)
    if cached is not None:
        return cached
    items = cli.sdk_list_instances(compartment_id) or []
    _put(key, items, cli=cli, compartment_id=compartment_id)
    return items


def cached_raw_boot_volumes(cli, compartment_id, refresh=False):
    """取启动盘原始列表（sdk_list_boot_volumes 的返回值），跨端点共享缓存。

    返回原始列表本身（调用方只读不改；精简用 oci.volume.simplify_boot_volumes）。
    """
    key = _key(cli, compartment_id, "boot_volumes")
    cached = _get(key, refresh)
    if cached is not None:
        return cached
    items = cli.sdk_list_boot_volumes(compartment_id) or []
    _put(key, items, cli=cli, compartment_id=compartment_id)
    return items


def _prewarm_once(now=None):
    """刷一轮：把 10 秒内要过期的缓存条目提前刷新。

    只动已缓存的条目（cli 与 compartment_id 随条目存着，刷新时直接用）。
    单条失败记日志、不中断本轮；返回值是本轮刷新的条目数（测试用）。
    """
    now = time.monotonic() if now is None else now
    with _CACHE_LOCK:
        due = [(key, entry["cli"], entry["compartment_id"])
               for key, entry in _CACHE.items()
               if entry["cli"] is not None
               and entry["expires"] - now <= OCI_CACHE_PREWARM_AHEAD]
    refreshed = 0
    for key, cli, compartment_id in due:
        kind = key[3]
        try:
            if kind == "instances":
                cached_raw_instances(cli, compartment_id, refresh=True)
            elif kind == "boot_volumes":
                cached_raw_boot_volumes(cli, compartment_id, refresh=True)
            else:
                continue
            refreshed += 1
        except Exception as exc:  # noqa: BLE001 - 单条失败不影响其他条目
            logger.warning("缓存预热失败 %s：%s", key, exc)
    return refreshed


def _prewarm_loop():
    while not _prewarm_stop.wait(OCI_CACHE_PREWARM_INTERVAL):
        try:
            _prewarm_once()
        except Exception as exc:  # noqa: BLE001 - 线程本身不因单轮异常退出
            logger.warning("缓存预热轮次异常：%s", exc)


def start_cache_prewarm():
    """启动后台预热线程（幂等：已启动时直接返回）。

    面板启动时调用（web/app.py 的 lifespan）；演示模式不调
    （演示数据不走 OCI 缓存，预热线程无事可做）。
    """
    global _prewarm_thread
    with _prewarm_lock:
        if _prewarm_thread is not None and _prewarm_thread.is_alive():
            return
        _prewarm_stop.clear()
        _prewarm_thread = threading.Thread(target=_prewarm_loop,
                                           name="oci-cache-prewarm",
                                           daemon=True)
        _prewarm_thread.start()


def stop_cache_prewarm(timeout=5):
    """停掉预热线程（关机与测试用；未启动时直接返回）。"""
    global _prewarm_thread
    with _prewarm_lock:
        thread = _prewarm_thread
        _prewarm_thread = None
    if thread is None:
        return
    _prewarm_stop.set()
    thread.join(timeout=timeout)

