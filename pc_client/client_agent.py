import argparse
import json
import platform
import socket
import time
from pathlib import Path
from typing import Any

import httpx
import psutil

from now_playing import get_active_activity, get_now_playing

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"


def _disk_path() -> str:
    return "C:\\" if platform.system().lower() == "windows" else "/"


def normalize_server_url(value: str) -> str:
    raw = value.strip()
    if not raw:
        return "http://127.0.0.1:8001"
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw.rstrip("/")
    if ":" in raw:
        return f"http://{raw}".rstrip("/")
    return f"http://{raw}:8001".rstrip("/")


def load_config() -> dict[str, Any]:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {}


def save_config(data: dict[str, Any]) -> None:
    CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


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
        for proc in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
            info = proc.info
            processes.append(
                {
                    "pid": info.get("pid"),
                    "name": info.get("name"),
                    "cpu_percent": float(info.get("cpu_percent") or 0),
                    "memory_percent": round(float(info.get("memory_percent") or 0), 2),
                }
            )
        processes.sort(key=lambda x: (x["cpu_percent"], x["memory_percent"]), reverse=True)
        processes = processes[:top_n]

    return metrics, processes


def build_payload(config: dict[str, Any]) -> dict[str, Any]:
    include_processes = bool(config.get("include_processes", True))
    include_now_playing = bool(config.get("include_now_playing", True))
    include_activity = bool(config.get("include_activity", True))
    metrics, processes = collect_metrics(include_processes=include_processes)
    now_playing = get_now_playing() if include_now_playing else None
    activity = get_active_activity() if include_activity else {}
    active_app = (activity.get("title") or activity.get("process")) if isinstance(activity, dict) else None

    return {
        "source_name": config["source_name"],
        "source_type": config.get("source_type") or "PC_AGENT",
        "metrics": metrics,
        "processes": processes,
        "now_playing": now_playing,
        "active_app": active_app,
        "activity": activity if isinstance(activity, dict) else {},
        "tags": [platform.system(), platform.node()],
    }


def claim_pair_code(
    *,
    server_url: str,
    pair_code: str,
    source_name: str,
    source_type: str,
) -> dict[str, Any]:
    endpoint = f"{server_url.rstrip('/')}/agent/pair/claim"
    payload = {
        "pair_code": pair_code.strip(),
        "source_name": source_name,
        "source_type": source_type,
    }

    with httpx.Client(timeout=20, trust_env=False) as client:
        response = client.post(endpoint, json=payload)

    if response.status_code >= 400:
        detail = ""
        try:
            detail = response.json().get("detail") or ""
        except Exception:
            detail = response.text.strip()
        raise RuntimeError(f"pair failed: HTTP {response.status_code} {detail}".strip())

    body = response.json()
    if not body.get("ok"):
        raise RuntimeError("pair failed: server returned ok=false")
    if not body.get("agent_api_key"):
        raise RuntimeError("pair failed: server did not return agent_api_key")
    return body


def _prompt_with_default(prompt: str, default: str) -> str:
    value = input(f"{prompt} [{default}]: ").strip()
    return value or default


def setup_wizard(existing: dict[str, Any] | None = None) -> dict[str, Any]:
    existing = existing or {}
    print("=== Настройка PC-агента ===")

    default_server = normalize_server_url(str(existing.get("server_url") or "http://127.0.0.1:8001"))
    server_url = normalize_server_url(_prompt_with_default("IP или URL сервера (пример 1.2.3.4 или https://host)", default_server))

    suggested = str(existing.get("source_name") or socket.gethostname())
    source_name = _prompt_with_default("Имя компьютера", suggested)
    source_type = str(existing.get("source_type") or "PC_AGENT").strip() or "PC_AGENT"

    pair_code = input("Код привязки (из /agents), Enter если хотите ввести AGENT_API_KEY: ").strip()
    api_key = ""
    if pair_code:
        result = claim_pair_code(
            server_url=server_url,
            pair_code=pair_code,
            source_name=source_name,
            source_type=source_type,
        )
        api_key = str(result.get("agent_api_key") or "").strip()
        source_name = str(result.get("source_name") or source_name)
        source_type = str(result.get("source_type") or source_type)
        print(f"[pc-client] pairing ok, source_name={source_name}")
    else:
        api_key = input("AGENT_API_KEY: ").strip()

    if not api_key:
        raise RuntimeError("Пустой ключ агента")

    interval_raw = input("Интервал heartbeat в секундах [30]: ").strip()
    interval_sec = int(interval_raw) if interval_raw.isdigit() and int(interval_raw) > 0 else int(existing.get("interval_sec") or 30)

    data = {
        "server_url": server_url,
        "api_key": api_key,
        "source_name": source_name,
        "source_type": source_type,
        "interval_sec": interval_sec,
        "include_processes": bool(existing.get("include_processes", True)),
        "include_now_playing": bool(existing.get("include_now_playing", True)),
        "include_activity": bool(existing.get("include_activity", True)),
        "trust_env_proxy": bool(existing.get("trust_env_proxy", False)),
    }
    save_config(data)
    print(f"Конфиг сохранен: {CONFIG_PATH}")
    return data


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serverredus PC agent client")
    parser.add_argument("--server-url")
    parser.add_argument("--pair-code")
    parser.add_argument("--api-key")
    parser.add_argument("--source-name")
    parser.add_argument("--source-type", default=None)
    parser.add_argument("--interval-sec", type=int, default=None)
    parser.add_argument("--include-processes", action="store_true")
    parser.add_argument("--disable-now-playing", action="store_true")
    parser.add_argument("--disable-activity", action="store_true")
    parser.add_argument("--trust-env-proxy", action="store_true")
    parser.add_argument("--init-only", action="store_true")
    return parser


def apply_cli_overrides(config: dict[str, Any], args: argparse.Namespace) -> tuple[dict[str, Any], bool]:
    updated = False

    if args.server_url:
        config["server_url"] = normalize_server_url(args.server_url)
        updated = True
    if args.source_name:
        config["source_name"] = args.source_name.strip()[:128] or socket.gethostname()
        updated = True
    if args.source_type:
        config["source_type"] = args.source_type.strip() or "PC_AGENT"
        updated = True
    if args.interval_sec and args.interval_sec > 0:
        config["interval_sec"] = int(args.interval_sec)
        updated = True
    if args.api_key:
        config["api_key"] = args.api_key.strip()
        updated = True

    if args.include_processes:
        config["include_processes"] = True
        updated = True
    if args.disable_now_playing:
        config["include_now_playing"] = False
        updated = True
    if args.disable_activity:
        config["include_activity"] = False
        updated = True
    if args.trust_env_proxy:
        config["trust_env_proxy"] = True
        updated = True

    pair_code = (args.pair_code or "").strip()
    if pair_code:
        server_url = normalize_server_url(str(config.get("server_url") or "http://127.0.0.1:8001"))
        source_name = str(config.get("source_name") or socket.gethostname())
        source_type = str(config.get("source_type") or "PC_AGENT")
        result = claim_pair_code(
            server_url=server_url,
            pair_code=pair_code,
            source_name=source_name,
            source_type=source_type,
        )
        config["server_url"] = server_url
        config["api_key"] = str(result.get("agent_api_key") or "").strip()
        config["source_name"] = str(result.get("source_name") or source_name)
        config["source_type"] = str(result.get("source_type") or source_type)
        updated = True
        print(f"[pc-client] pairing ok, source_name={config['source_name']}")

    return config, updated


def run_agent(config: dict[str, Any]) -> None:
    endpoint = f"{config['server_url'].rstrip('/')}/agent/heartbeat"
    headers = {"X-Api-Key": config["api_key"]}
    interval_sec = int(config.get("interval_sec", 30))
    source_name = str(config.get("source_name") or socket.gethostname())
    source_type = str(config.get("source_type") or "PC_AGENT")
    trust_env_proxy = bool(config.get("trust_env_proxy", False))
    print(
        f"[pc-client] endpoint={endpoint} source_name={source_name} "
        f"source_type={source_type} trust_env_proxy={trust_env_proxy}"
    )

    with httpx.Client(timeout=20, trust_env=trust_env_proxy) as client:
        while True:
            payload = build_payload({**config, "source_name": source_name, "source_type": source_type})
            try:
                response = client.post(endpoint, headers=headers, json=payload)
                response.raise_for_status()
                body = response.json()
                msg = f"[pc-client] ok recovered={body.get('recovered')} at {body.get('server_time')}"
                if body.get("new_source"):
                    msg += " | новый агент зарегистрирован"
                print(msg)
            except Exception as exc:
                print(f"[pc-client] heartbeat failed: {exc}")
            time.sleep(interval_sec)


def ensure_minimal_defaults(config: dict[str, Any]) -> dict[str, Any]:
    if not config.get("source_name"):
        config["source_name"] = socket.gethostname()
    if not config.get("source_type"):
        config["source_type"] = "PC_AGENT"
    if not config.get("interval_sec"):
        config["interval_sec"] = 30
    if "include_processes" not in config:
        config["include_processes"] = True
    if "include_now_playing" not in config:
        config["include_now_playing"] = True
    if "include_activity" not in config:
        config["include_activity"] = True
    if "trust_env_proxy" not in config:
        config["trust_env_proxy"] = False
    return config


def main() -> None:
    args = build_arg_parser().parse_args()
    config = ensure_minimal_defaults(load_config())
    config, changed_by_args = apply_cli_overrides(config, args)

    if not config.get("server_url") or not config.get("api_key"):
        config = setup_wizard(config)
        changed_by_args = True
    else:
        if changed_by_args:
            save_config(config)
        print(f"Используется конфиг: {CONFIG_PATH}")

    if args.init_only:
        print("[pc-client] init-only completed")
        return

    run_agent(config)


if __name__ == "__main__":
    main()
