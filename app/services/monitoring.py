import platform
import subprocess
import time
from typing import Any

import psutil


def _bytes_to_gb(value: int) -> float:
    return round(value / (1024**3), 2)


def collect_server_metrics(top_processes_limit: int = 5) -> dict[str, Any]:
    vm = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    net = psutil.net_io_counters()

    process_items = []
    for process in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
        info = process.info
        process_items.append(
            {
                "pid": info.get("pid"),
                "name": info.get("name"),
                "cpu_percent": round(float(info.get("cpu_percent") or 0), 2),
                "memory_percent": round(float(info.get("memory_percent") or 0), 2),
            }
        )

    process_items.sort(key=lambda item: (item["cpu_percent"], item["memory_percent"]), reverse=True)
    process_items = process_items[:top_processes_limit]

    uptime_seconds = int(time.time() - psutil.boot_time())
    return {
        "cpu_percent": psutil.cpu_percent(interval=0.2),
        "ram_used_gb": _bytes_to_gb(vm.used),
        "ram_total_gb": _bytes_to_gb(vm.total),
        "disk_used_gb": _bytes_to_gb(disk.used),
        "disk_total_gb": _bytes_to_gb(disk.total),
        "net_rx_mb": round(net.bytes_recv / (1024**2), 2),
        "net_tx_mb": round(net.bytes_sent / (1024**2), 2),
        "uptime_seconds": uptime_seconds,
        "top_processes": process_items,
    }


def collect_systemd_statuses(services: list[str]) -> dict[str, str]:
    if not services:
        return {}
    if platform.system().lower() != "linux":
        return {service: "unsupported" for service in services}

    result: dict[str, str] = {}
    for service in services:
        try:
            proc = subprocess.run(
                ["systemctl", "is-active", service],
                capture_output=True,
                text=True,
                check=False,
            )
            value = proc.stdout.strip() or proc.stderr.strip() or "unknown"
            result[service] = value
        except Exception:
            result[service] = "error"
    return result

