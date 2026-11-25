#!/usr/bin/env python3
"""系统资源路由：本机 CPU / 内存 / 硬盘 / 网络速率。

只用标准库（读 /proc/stat、/proc/meminfo、shutil.disk_usage、
/proc/net/dev），不依赖 psutil。
CPU 使用率与网络速率需要两次采样，中间间隔 SAMPLE_INTERVAL 秒。

  - GET /api/system：原有嵌套格式（顶栏资源监控栏用，保持不变）。
  - GET /api/system/health：面板形状（/client/health 风格，阶段 5
    工作包 E），含 CPU 型号/核数、内存/磁盘 GB 与占用率、网速、
    进程 RSS MB、uptime 秒。
"""

import os
import shutil
import time

from fastapi import APIRouter, Depends

from web.deps import get_current_user

router = APIRouter()

# Interval (seconds) between the two samples. 两次采样之间的间隔（秒）。
SAMPLE_INTERVAL = 0.1


def _read_cpu_times():
    """读 /proc/stat 第一行，返回 (总 jiffies, 空闲 jiffies)。"""
    with open("/proc/stat", encoding="utf-8") as f:
        parts = f.readline().split()
    values = [int(x) for x in parts[1:]]
    total = sum(values)
    # idle = idle + iowait (columns 4 and 5). 空闲 = idle + iowait（第 4、5 列）。
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return total, idle


def _read_net_bytes():
    """读 /proc/net/dev，返回除 lo 外所有网卡的 (接收字节, 发送字节) 总和。"""
    rx_total = 0
    tx_total = 0
    with open("/proc/net/dev", encoding="utf-8") as f:
        for line in f:
            if ":" not in line:
                continue
            name, rest = line.split(":", 1)
            if name.strip() == "lo":
                continue
            fields = rest.split()
            rx_total += int(fields[0])
            tx_total += int(fields[8])
    return rx_total, tx_total


def _read_meminfo():
    """读 /proc/meminfo，返回 {键: KB 数}。"""
    mem = {}
    with open("/proc/meminfo", encoding="utf-8") as f:
        for line in f:
            key, rest = line.split(":", 1)
            mem[key.strip()] = int(rest.split()[0])
    return mem


def _read_cpu_model():
    """读 /proc/cpuinfo 第一块 CPU 的 model name；读不到返回 "unknown"。"""
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("model name"):
                    _key, _, value = line.partition(":")
                    return value.strip() or "unknown"
    except OSError:
        pass
    return "unknown"


def _read_process_rss_mb():
    """读 /proc/self/status 的 VmRSS（当前进程常驻内存），返回 MB。"""
    try:
        with open("/proc/self/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    kb = int(line.split()[1])
                    return round(kb / 1024, 1)
    except (OSError, ValueError, IndexError):
        pass
    return 0.0


def _read_uptime_seconds():
    """读 /proc/uptime 的开机秒数；读不到返回 0。"""
    try:
        with open("/proc/uptime", encoding="utf-8") as f:
            return int(float(f.read().split()[0]))
    except (OSError, ValueError, IndexError):
        return 0


def get_health_status(sample_interval=SAMPLE_INTERVAL):
    """采样并返回 面板形状的健康状态（/client/health 风格）。

    {cpu: {model, cores, usagePercent},
     memory: {totalGB, usedGB, freeGB, usagePercent},
     disk: {totalGB, usedGB, freeGB, usagePercent},
     network: {rxBytesPerSec, txBytesPerSec, sampledAt},
     processRssInMB, uptimeSeconds}。
    sampledAt 是采样完成时的 epoch 秒。GB 保留 1 位小数。
    """
    total1, idle1 = _read_cpu_times()
    rx1, tx1 = _read_net_bytes()
    time.sleep(sample_interval)
    total2, idle2 = _read_cpu_times()
    rx2, tx2 = _read_net_bytes()

    total_delta = total2 - total1
    idle_delta = idle2 - idle1
    cpu_percent = 0.0 if total_delta <= 0 else 100.0 * (1 - idle_delta / total_delta)

    mem = _read_meminfo()
    mem_total_kb = mem.get("MemTotal", 0)
    mem_avail_kb = mem.get("MemAvailable", 0)
    mem_used_kb = mem_total_kb - mem_avail_kb
    mem_percent = (100.0 * mem_used_kb / mem_total_kb) if mem_total_kb else 0.0

    disk = shutil.disk_usage("/")
    disk_percent = (100.0 * disk.used / disk.total) if disk.total else 0.0

    gb = 1024 ** 3
    return {
        "cpu": {
            "model": _read_cpu_model(),
            "cores": os.cpu_count() or 0,
            "usagePercent": round(cpu_percent, 1),
        },
        "memory": {
            "totalGB": round(mem_total_kb / 1024 / 1024, 1),
            "usedGB": round(mem_used_kb / 1024 / 1024, 1),
            "freeGB": round(mem_avail_kb / 1024 / 1024, 1),
            "usagePercent": round(mem_percent, 1),
        },
        "disk": {
            "totalGB": round(disk.total / gb, 1),
            "usedGB": round(disk.used / gb, 1),
            "freeGB": round(disk.free / gb, 1),
            "usagePercent": round(disk_percent, 1),
        },
        "network": {
            "rxBytesPerSec": int((rx2 - rx1) / sample_interval),
            "txBytesPerSec": int((tx2 - tx1) / sample_interval),
            "sampledAt": int(time.time()),
        },
        "processRssInMB": _read_process_rss_mb(),
        "uptimeSeconds": _read_uptime_seconds(),
    }


def get_system_status(sample_interval=SAMPLE_INTERVAL):
    """采样并返回本机资源状态（CPU/内存/硬盘百分比与网络速率）。"""
    total1, idle1 = _read_cpu_times()
    rx1, tx1 = _read_net_bytes()
    time.sleep(sample_interval)
    total2, idle2 = _read_cpu_times()
    rx2, tx2 = _read_net_bytes()

    total_delta = total2 - total1
    idle_delta = idle2 - idle1
    cpu_percent = 0.0 if total_delta <= 0 else 100.0 * (1 - idle_delta / total_delta)

    mem = _read_meminfo()
    mem_total_kb = mem.get("MemTotal", 0)
    mem_avail_kb = mem.get("MemAvailable", 0)
    mem_used_kb = mem_total_kb - mem_avail_kb
    mem_percent = (100.0 * mem_used_kb / mem_total_kb) if mem_total_kb else 0.0

    disk = shutil.disk_usage("/")
    disk_percent = (100.0 * disk.used / disk.total) if disk.total else 0.0

    return {
        "cpu": {"percent": round(cpu_percent, 1)},
        "memory": {
            "total_mb": round(mem_total_kb / 1024, 1),
            "used_mb": round(mem_used_kb / 1024, 1),
            "available_mb": round(mem_avail_kb / 1024, 1),
            "percent": round(mem_percent, 1),
        },
        "disk": {
            "total_gb": round(disk.total / (1024 ** 3), 1),
            "used_gb": round(disk.used / (1024 ** 3), 1),
            "free_gb": round(disk.free / (1024 ** 3), 1),
            "percent": round(disk_percent, 1),
        },
        "network": {
            "rx_bytes_per_sec": int((rx2 - rx1) / sample_interval),
            "tx_bytes_per_sec": int((tx2 - tx1) / sample_interval),
        },
    }


@router.get("/api/system")
def system_status(username: str = Depends(get_current_user)):
    """返回本机资源状态（顶栏资源监控栏用）。"""
    return {"ok": True, **get_system_status()}


@router.get("/api/system/health")
def system_health(username: str = Depends(get_current_user)):
    """返回 面板形状的健康状态（CPU 型号/核数、内存/磁盘 GB、
    网速、进程 RSS、uptime）。"""
    return {"ok": True, **get_health_status()}
