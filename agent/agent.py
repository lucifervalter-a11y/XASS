import argparse
import platform
import socket
import time
from datetime import datetime, timezone
from typing import Any

import httpx
import psutil

from now_playing import get_active_activity, get_now_playing


def _disk_path() -> str:
    return "C:\\" if platform.system().lower() == "windows" else "/"


def collect_metrics(include_processes: bool, top_n: int = 5) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    vm = psutil.virtual_memory()
    disk = psutil.disk_usage(_disk_path())
    net = psutil.net_io_counters()

    metrics = {
        "cpu_percent": psutil.cpu_percent(interval=0.2),
        "ram_used_percent": vm.percent,
        "ram_used_mb": round(vm.used / (1024**2), 2),
        "ram_total_mb": round(vm.total / (1024**2), 2),
        "disk_used_percent": disk.percent,
        "disk_used_gb": round(disk.used / (1024**3), 2),
        "disk_total_gb": round(disk.total / (1024**3), 2),
        "net_rx_mb": round(net.bytes_recv / (1024**2), 2),
        "net_tx_mb": round(net.bytes_sent / (1024**2), 2),
        "uptime_seconds": int(time.time() - psutil.boot_time()),
    }

    processes: list[dict[str, Any]] = []
    if include_processes:
        for process in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
            info = process.info
            processes.append(
                {
                    "pid": info.get("pid"),
                    "name": info.get("name"),
                    "cpu_percent": float(info.get("cpu_percent") or 0),
                    "memory_percent": round(float(info.get("memory_percent") or 0), 2),
                }
            )
        processes.sort(key=lambda item: (item["cpu_percent"], item["memory_percent"]), reverse=True)
        processes = processes[:top_n]

    return metrics, processes


def build_payload(
    source_name: str,
    source_type: str,
    include_processes: bool,
    include_now_playing: bool,
    include_activity: bool,
) -> dict[str, Any]:
    metrics, processes = collect_metrics(include_processes=include_processes)
    now_playing = get_now_playing() if include_now_playing else None
    activity = get_active_activity() if include_activity else {}
    active_app = (activity.get("title") or activity.get("process")) if isinstance(activity, dict) else None
    return {
        "source_name": source_name,
        "source_type": source_type,
        "metrics": metrics,
        "processes": processes,
        "now_playing": now_playing,
        "active_app": active_app,
        "activity": activity if isinstance(activity, dict) else {},
        "tags": [platform.system(), platform.node(), datetime.now(timezone.utc).isoformat()],
    }


def run_agent(
    server_url: str,
    api_key: str,
    source_name: str,
    source_type: str,
    interval_sec: int,
    include_processes: bool,
    include_now_playing: bool,
    include_activity: bool,
    trust_env_proxy: bool,
) -> None:
    if not api_key:
        raise RuntimeError("AGENT_API_KEY is empty")

    endpoint = f"{server_url.rstrip('/')}/agent/heartbeat"
    headers = {"X-Api-Key": api_key}

    normalized_source_name = source_name.strip() or socket.gethostname()

    print(
        f"[agent] endpoint={endpoint} source_name={normalized_source_name} source_type={source_type} "
        f"trust_env_proxy={trust_env_proxy}"
    )
    with httpx.Client(timeout=20, trust_env=trust_env_proxy) as client:
        while True:
            payload = build_payload(
                source_name=normalized_source_name,
                source_type=source_type,
                include_processes=include_processes,
                include_now_playing=include_now_playing,
                include_activity=include_activity,
            )
            try:
                response = client.post(endpoint, headers=headers, json=payload)
                response.raise_for_status()
                body = response.json()
                print(f"[agent] heartbeat ok recovered={body.get('recovered')} at {body.get('server_time')}")
            except Exception as exc:
                print(f"[agent] heartbeat failed: {exc}")
            time.sleep(interval_sec)


def main() -> None:
    parser = argparse.ArgumentParser(description="serverredus heartbeat agent")
    parser.add_argument("--server-url", default="http://127.0.0.1:8000")
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--source-name", default=socket.gethostname())
    parser.add_argument("--source-type", default="PC_AGENT")
    parser.add_argument("--interval-sec", type=int, default=60)
    parser.add_argument("--include-processes", action="store_true")
    parser.add_argument("--disable-now-playing", action="store_true")
    parser.add_argument("--disable-activity", action="store_true")
    parser.add_argument(
        "--trust-env-proxy",
        action="store_true",
        help="Use system/env proxy settings for HTTP requests (disabled by default).",
    )
    args = parser.parse_args()

    run_agent(
        server_url=args.server_url,
        api_key=args.api_key,
        source_name=args.source_name,
        source_type=args.source_type,
        interval_sec=args.interval_sec,
        include_processes=args.include_processes,
        include_now_playing=not args.disable_now_playing,
        include_activity=not args.disable_activity,
        trust_env_proxy=args.trust_env_proxy,
    )


if __name__ == "__main__":
    main()
