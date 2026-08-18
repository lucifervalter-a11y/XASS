import argparse
import ctypes
import json
import os
import platform
import random
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import psutil

from client_update import (
    DATA_ROOT,
    clear_command_results,
    current_revision,
    current_version,
    download_installer_update,
    download_update,
    is_installer_build,
    launch_installer_update,
    launch_update_helper,
    load_command_results,
    store_command_result,
    update_operation,
    write_agent_status,
)
from discord_presence import get_discord_activity
from now_playing import get_active_activity, get_now_playing
from archive_store import apply_archive_events, archive_cursor, archive_root, archive_status, cleanup_archive
from remote_tools import (
    capture_screenshot,
    clipboard_get,
    clipboard_set,
    delete_file,
    list_files,
    receive_uploaded_file,
    upload_requested_file,
)
from network_client import create_http_client
try:
    from runtime_state import acquire_single_instance, atomic_write_json, configure_utf8_logging, load_json_object
except ModuleNotFoundError:
    from pc_client.runtime_state import acquire_single_instance, atomic_write_json, configure_utf8_logging, load_json_object

CONFIG_PATH = DATA_ROOT / "config.json"
PROCESSED_COMMANDS_PATH = DATA_ROOT / ".processed-commands.json"
_last_heartbeat_latency_ms = 0.0
_last_heartbeat_error = ""
_last_heartbeat_error_at = ""
_last_server_version = ""


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


def _response_preview(text: str, limit: int = 220) -> str:
    cleaned = (text or "").replace("\r", " ").replace("\n", " ").strip()
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[:limit]}..."


def _parse_json_body(response: httpx.Response) -> dict[str, Any] | None:
    try:
        payload = response.json()
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _build_server_candidates(value: str) -> list[str]:
    normalized = normalize_server_url(value)
    parsed = urlsplit(normalized)
    if not parsed.hostname:
        return [normalized]

    base_path = parsed.path.rstrip("/")
    if parsed.port is not None or base_path:
        return [normalized]

    host = parsed.hostname
    candidates = [normalized]
    if parsed.scheme == "https":
        candidates.extend(
            [
                f"https://{host}:8001",
                f"http://{host}:8001",
                f"http://{host}:8000",
            ]
        )
    else:
        candidates.extend(
            [
                f"http://{host}:8001",
                f"http://{host}:8000",
            ]
        )

    seen: set[str] = set()
    unique: list[str] = []
    for item in candidates:
        item = item.rstrip("/")
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
    return unique


def discover_backend_url(server_url: str) -> str:
    candidates = _build_server_candidates(server_url)
    with create_http_client(server_url, timeout=8, trust_env=False) as client:
        for candidate in candidates:
            health_url = f"{candidate}/health"
            try:
                response = client.get(health_url)
            except Exception:
                continue
            if response.status_code >= 400:
                continue
            payload = _parse_json_body(response)
            if isinstance(payload, dict) and str(payload.get("status") or "").lower() == "ok":
                return candidate
    return normalize_server_url(server_url)


def load_config() -> dict[str, Any]:
    return load_json_object(CONFIG_PATH, restore_backup=True)


def save_config(data: dict[str, Any]) -> None:
    atomic_write_json(CONFIG_PATH, data, backup=True)


def _processed_commands() -> dict[str, Any]:
    payload = load_json_object(PROCESSED_COMMANDS_PATH)
    rows = payload.get("commands")
    return {str(key): value for key, value in rows.items() if isinstance(value, dict)} if isinstance(rows, dict) else {}


def command_was_processed(command_id: int) -> bool:
    return str(int(command_id)) in _processed_commands()


def mark_command_processed(command_id: int, command_name: str) -> None:
    rows = _processed_commands()
    rows[str(int(command_id))] = {
        "command": str(command_name)[:64],
        "processed_at": time.time(),
    }
    ordered = sorted(rows.items(), key=lambda item: float((item[1] or {}).get("processed_at") or 0))[-250:]
    atomic_write_json(PROCESSED_COMMANDS_PATH, {"commands": dict(ordered)}, backup=False)


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
    if _last_heartbeat_latency_ms > 0:
        metrics["heartbeat_latency_ms"] = round(_last_heartbeat_latency_ms, 1)
    now_playing = get_now_playing() if include_now_playing else None
    activity = get_active_activity() if include_activity else {}
    active_app = (activity.get("title") or activity.get("process")) if isinstance(activity, dict) else None
    discord = get_discord_activity()

    payload: dict[str, Any] = {
        "source_name": config["source_name"],
        "source_type": config.get("source_type") or "PC_AGENT",
        "metrics": metrics,
        "processes": processes,
        "now_playing": now_playing,
        "active_app": active_app,
        "activity": activity if isinstance(activity, dict) else {},
        "tags": [platform.system(), platform.node()],
        "agent_version": current_version(),
        "agent_revision": current_revision(),
        "agent_distribution": "installer" if is_installer_build() else "source",
        "command_results": load_command_results(),
        "archive_cursor": archive_cursor(config),
        "archive_status": archive_status(config),
        "last_error": _last_heartbeat_error,
        "last_error_at": _last_heartbeat_error_at,
        "server_version_seen": _last_server_version,
        "system": {
            "platform": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "hostname": platform.node(),
        },
    }
    if discord is not None:
        payload["discord"] = discord
    return payload


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

    with create_http_client(server_url, timeout=20, trust_env=False) as client:
        response = client.post(endpoint, json=payload)

    body = _parse_json_body(response)
    if response.status_code >= 400:
        detail = ""
        if isinstance(body, dict):
            detail = str(body.get("detail") or body.get("description") or "").strip()
        if not detail:
            detail = _response_preview(response.text)
        raise RuntimeError(f"pair failed: HTTP {response.status_code} {detail}".strip())

    if not isinstance(body, dict):
        content_type = response.headers.get("content-type", "")
        preview = _response_preview(response.text)
        raise RuntimeError(
            "pair failed: backend returned non-JSON response. "
            f"Check --server-url (current: {server_url}). "
            f"content-type={content_type!r}, body={preview!r}"
        )
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
        discovered_url = discover_backend_url(server_url)
        if discovered_url != server_url:
            print(f"[pc-client] backend autodetect: {server_url} -> {discovered_url}")
            server_url = discovered_url
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
        "auto_update": bool(existing.get("auto_update", True)),
        "desktop_managed": bool(existing.get("desktop_managed", False)),
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
    parser.add_argument("--no-auto-update", action="store_true")
    parser.add_argument("--desktop-managed", action="store_true")
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
    if args.no_auto_update:
        config["auto_update"] = False
        updated = True
    if args.desktop_managed:
        config["desktop_managed"] = True

    pair_code = (args.pair_code or "").strip()
    if pair_code:
        server_url = normalize_server_url(str(config.get("server_url") or "http://127.0.0.1:8001"))
        discovered_url = discover_backend_url(server_url)
        if discovered_url != server_url:
            print(f"[pc-client] backend autodetect: {server_url} -> {discovered_url}")
            server_url = discovered_url
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


def _restart_agent(config: dict[str, Any], command_id: int) -> str:
    store_command_result(command_id, True, "Агент перезапущен")
    if config.get("desktop_managed"):
        return "restart"
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve())],
        cwd=str(Path(__file__).resolve().parent),
        creationflags=creationflags,
    )
    return "restart"


def _lock_workstation(command_id: int) -> None:
    if os.name != "nt":
        store_command_result(command_id, False, "Блокировка доступна только на Windows")
        return
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.LockWorkStation.argtypes = []
        user32.LockWorkStation.restype = ctypes.c_bool
        if not user32.LockWorkStation():
            error = ctypes.get_last_error()
            raise OSError(error, "LockWorkStation failed")
        store_command_result(command_id, True, "Экран Windows заблокирован")
        print("[pc-client] Windows workstation locked")
    except Exception as exc:
        store_command_result(command_id, False, f"Не удалось заблокировать Windows: {exc}")
        print(f"[pc-client] lock failed: {exc}")


def _sleep_workstation(command_id: int) -> None:
    if os.name != "nt":
        store_command_result(command_id, False, "Сон доступен только на Windows")
        return
    try:
        powrprof = ctypes.WinDLL("PowrProf", use_last_error=True)
        suspend = powrprof.SetSuspendState
        suspend.argtypes = [ctypes.c_bool, ctypes.c_bool, ctypes.c_bool]
        suspend.restype = ctypes.c_bool
        if not suspend(False, True, False):
            raise OSError(ctypes.get_last_error(), "SetSuspendState failed")
        store_command_result(command_id, True, "Windows переведена в сон")
    except Exception as exc:
        store_command_result(command_id, False, f"Не удалось перевести Windows в сон: {exc}")


def _power_command(command_id: int, *, reboot: bool, delay_sec: int) -> None:
    if os.name != "nt":
        store_command_result(command_id, False, "Команда питания доступна только на Windows")
        return
    delay = max(0, min(int(delay_sec), 3600))
    action = "/r" if reboot else "/s"
    label = "перезагружена" if reboot else "выключена"
    try:
        subprocess.Popen(
            ["shutdown.exe", action, "/t", str(delay), "/d", "p:0:0"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        store_command_result(command_id, True, f"Windows будет {label} через {delay} сек.")
    except OSError as exc:
        store_command_result(command_id, False, f"Не удалось выполнить команду питания: {exc}")


def _open_archive_folder(config: dict[str, Any], command_id: int) -> None:
    if os.name != "nt":
        store_command_result(command_id, False, "Открытие папки доступно только в Windows-приложении")
        return
    try:
        folder = archive_root(config)
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(str(folder))
        store_command_result(command_id, True, f"Папка архива открыта: {folder}")
    except OSError as exc:
        store_command_result(command_id, False, f"Не удалось открыть папку архива: {exc}")


def _handle_workspace_command(
    command_name: str,
    command_payload: dict[str, Any],
    *,
    command_id: int,
    config: dict[str, Any],
    source_name: str,
    client: httpx.Client,
) -> bool:
    supported = {
        "screenshot", "files_list", "file_download", "file_upload", "file_delete",
        "clipboard_get", "clipboard_set",
    }
    if command_name not in supported:
        return False
    try:
        common = {
            "endpoint": str(config["server_url"]),
            "api_key": str(config["api_key"]),
            "source_name": source_name,
        }
        if command_name == "screenshot":
            details = capture_screenshot(client, command_id=command_id, **common)
            store_command_result(command_id, True, "Снимок экрана получен", details)
        elif command_name == "files_list":
            details = list_files(DATA_ROOT, command_payload.get("root"), command_payload.get("path"))
            store_command_result(command_id, True, f"Папка открыта: {details['root_label']}", details)
        elif command_name == "file_download":
            details = upload_requested_file(
                DATA_ROOT, client, command_id=command_id,
                root_name=command_payload.get("root"), relative_path=command_payload.get("path"), **common,
            )
            store_command_result(command_id, True, f"Файл готов: {details['filename']}", details)
        elif command_name == "file_upload":
            details = receive_uploaded_file(
                DATA_ROOT, client, root_name=command_payload.get("root"),
                relative_path=command_payload.get("path"), asset_token=command_payload.get("asset_token"),
                filename=command_payload.get("filename"), **common,
            )
            store_command_result(command_id, True, f"Файл сохранён: {details['filename']}", details)
        elif command_name == "file_delete":
            details = delete_file(DATA_ROOT, command_payload.get("root"), command_payload.get("path"))
            store_command_result(command_id, True, f"Файл удалён: {details['name']}", details)
        elif command_name == "clipboard_get":
            text = clipboard_get()
            store_command_result(command_id, True, "Текст получен из буфера ПК", {"text": text, "length": len(text)})
        elif command_name == "clipboard_set":
            length = clipboard_set(command_payload.get("text"))
            store_command_result(command_id, True, "Текст отправлен в буфер ПК", {"length": length})
        return True
    except Exception as exc:
        store_command_result(command_id, False, f"{command_name}: {exc}")
        print(f"[pc-client] {command_name} failed: {exc}", flush=True)
        return True


def _apply_update(config: dict[str, Any], manifest: dict[str, Any], command_id: int | None) -> str | None:
    try:
        version = str(manifest.get("version") or "")
        revision = str(manifest.get("revision") or "")
        with update_operation(version, revision) as operation:
            def report(message: str) -> None:
                print(f"[pc-client] {message}", flush=True)
                operation.phase("downloading", message)

            stage = download_update(
                manifest,
                api_key=str(config.get("api_key") or ""),
                trust_env=bool(config.get("trust_env_proxy", False)),
                progress=report,
            )
            operation.phase("verifying", "Пакет проверен")
            launch_update_helper(
                stage,
                manifest,
                command_id=command_id,
                restart_target="none" if config.get("desktop_managed") else "agent",
            )
        print(
            f"[pc-client] update staged: {manifest.get('version')} "
            f"({str(manifest.get('revision') or '')[:12]})"
        )
        return "update"
    except Exception as exc:
        print(f"[pc-client] update failed: {exc}")
        if command_id is not None:
            store_command_result(command_id, False, f"Ошибка обновления: {exc}")
        return None


def _apply_installer_update(config: dict[str, Any], manifest: dict[str, Any], command_id: int | None) -> str | None:
    try:
        version = str(manifest.get("version") or "")
        revision = str(manifest.get("revision") or "")
        with update_operation(version, revision) as operation:
            def report(message: str) -> None:
                print(f"[pc-client] {message}", flush=True)
                operation.phase("downloading", message)

            installer = download_installer_update(
                manifest,
                api_key=str(config.get("api_key") or ""),
                trust_env=bool(config.get("trust_env_proxy", False)),
                progress=report,
            )
            operation.phase("verifying", "Установщик проверен")
            if command_id is not None:
                store_command_result(command_id, True, f"Запущена установка XASS {manifest.get('version')}")
            launch_installer_update(
                installer,
                wait_pid=os.getpid(),
                expected_version=version,
                expected_revision=revision,
            )
        print(f"[pc-client] installer update staged: {manifest.get('version')}")
        return "installer_update"
    except Exception as exc:
        print(f"[pc-client] installer update failed: {exc}")
        if command_id is not None:
            store_command_result(command_id, False, f"Ошибка обновления установщика: {exc}")
        return None


def run_agent(config: dict[str, Any]) -> str:
    global _last_heartbeat_error, _last_heartbeat_error_at, _last_heartbeat_latency_ms, _last_server_version
    endpoint = f"{config['server_url'].rstrip('/')}/agent/heartbeat"
    headers = {"X-Api-Key": config["api_key"]}
    interval_sec = int(config.get("interval_sec", 30))
    source_name = str(config.get("source_name") or socket.gethostname())
    source_type = str(config.get("source_type") or "PC_AGENT")
    trust_env_proxy = bool(config.get("trust_env_proxy", False))
    write_agent_status("connecting", detail=f"Подключение к {endpoint}")
    print(
        f"[pc-client] endpoint={endpoint} source_name={source_name} "
        f"source_type={source_type} trust_env_proxy={trust_env_proxy}",
        flush=True,
    )

    failed_auto_revision = ""
    consecutive_failures = 0
    sleep_seconds = 0.0
    with create_http_client(
        str(config["server_url"]),
        timeout=20,
        trust_env=trust_env_proxy,
    ) as client:
        while True:
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
            payload = build_payload({**config, "source_name": source_name, "source_type": source_type})
            sent_result_ids = [
                int(item.get("id"))
                for item in payload.get("command_results", [])
                if str(item.get("id", "")).isdigit()
            ]
            try:
                heartbeat_started = time.perf_counter()
                response = client.post(endpoint, headers=headers, json=payload)
                _last_heartbeat_latency_ms = (time.perf_counter() - heartbeat_started) * 1000
                response.raise_for_status()
                body = _parse_json_body(response)
                if not isinstance(body, dict):
                    content_type = response.headers.get("content-type", "")
                    preview = _response_preview(response.text)
                    raise RuntimeError(
                        "heartbeat failed: backend returned non-JSON response. "
                        f"Check server URL ({config['server_url']}). "
                        f"content-type={content_type!r}, body={preview!r}"
                    )
                _last_server_version = str(body.get("server_version") or "")[:32]
                msg = (
                    f"[pc-client] ok recovered={body.get('recovered')} at {body.get('server_time')} "
                    f"latency={_last_heartbeat_latency_ms:.0f}ms"
                )
                if body.get("new_source"):
                    msg += " | новый агент зарегистрирован"
                write_agent_status(
                    "online",
                    detail=msg,
                    server_time=str(body.get("server_time") or ""),
                    latency_ms=round(_last_heartbeat_latency_ms, 1),
                    agent_version=current_version(),
                    server_version=_last_server_version,
                    last_error="",
                )
                print(msg, flush=True)
                consecutive_failures = 0
                sleep_seconds = float(min(interval_sec, 5) if config.get("archive_enabled") else interval_sec)
                commands = body.get("commands") if isinstance(body.get("commands"), list) else []
                if sent_result_ids:
                    still_pending = {
                        int(command.get("id"))
                        for command in commands
                        if isinstance(command, dict) and str(command.get("id", "")).isdigit()
                    }
                    clear_command_results([item for item in sent_result_ids if item not in still_pending])

                manifest = body.get("update") if isinstance(body.get("update"), dict) else None
                installer_manifest = body.get("installer_update") if isinstance(body.get("installer_update"), dict) else None
                archive_result = apply_archive_events(config, body, client=client, headers=headers)
                if archive_result.get("saved"):
                    print(
                        f"[pc-client] archive saved={archive_result['saved']} "
                        f"cursor={archive_result['cursor']} errors={archive_result.get('errors', 0)}",
                        flush=True,
                    )
                update_command_id: int | None = None
                for command in commands:
                    if not isinstance(command, dict):
                        continue
                    try:
                        command_id = int(command.get("id"))
                    except (TypeError, ValueError):
                        continue
                    command_name = str(command.get("command") or "").strip().lower()
                    command_payload = command.get("payload") if isinstance(command.get("payload"), dict) else {}
                    if command_was_processed(command_id):
                        continue
                    # Persist before execution. If the process dies after a power or
                    # lock command, the same server delivery cannot execute it twice.
                    mark_command_processed(command_id, command_name)
                    if command_name == "lock":
                        _lock_workstation(command_id)
                        continue
                    if command_name == "sleep":
                        _sleep_workstation(command_id)
                        continue
                    if command_name in {"reboot", "shutdown"}:
                        _power_command(
                            command_id,
                            reboot=command_name == "reboot",
                            delay_sec=int(command_payload.get("delay_sec") or 0),
                        )
                        continue
                    if command_name == "ping":
                        store_command_result(
                            command_id,
                            True,
                            f"Соединение активно, задержка {_last_heartbeat_latency_ms:.0f} мс",
                            {"latency_ms": round(_last_heartbeat_latency_ms, 1)},
                        )
                        continue
                    if command_name == "open_archive":
                        _open_archive_folder(config, command_id)
                        continue
                    if command_name == "cleanup_archive":
                        result = cleanup_archive(config, force=True)
                        store_command_result(
                            command_id,
                            True,
                            f"Локальный архив очищен: {result['removed_files']} файлов",
                            result,
                        )
                        continue
                    if _handle_workspace_command(
                        command_name,
                        command_payload,
                        command_id=command_id,
                        config=config,
                        source_name=source_name,
                        client=client,
                    ):
                        continue
                    if command_name == "check_update":
                        update_info = installer_manifest if is_installer_build() else manifest
                        available = bool(update_info and update_info.get("available"))
                        version = str((update_info or {}).get("version") or current_version())
                        store_command_result(
                            command_id,
                            True,
                            f"Доступно обновление {version}" if available else "Версия уже актуальна",
                            {"available": available, "version": version},
                        )
                        continue
                    if command_name == "restart":
                        return _restart_agent(config, command_id)
                    if command_name == "update":
                        update_command_id = command_id
                        continue
                    store_command_result(command_id, False, f"Неизвестная команда агента: {command_name or 'empty'}")

                if update_command_id is not None:
                    if is_installer_build() and installer_manifest and installer_manifest.get("available"):
                        update_result = _apply_installer_update(config, installer_manifest, update_command_id)
                        if update_result:
                            return update_result
                    elif not is_installer_build() and manifest and manifest.get("available"):
                        update_result = _apply_update(config, manifest, update_command_id)
                        if update_result:
                            return update_result
                    else:
                        store_command_result(update_command_id, True, "Версия уже актуальна")
                elif is_installer_build() and installer_manifest and installer_manifest.get("available") and bool(config.get("auto_update", True)):
                    revision = str(installer_manifest.get("revision") or "")
                    if revision and revision != failed_auto_revision:
                        update_result = _apply_installer_update(config, installer_manifest, None)
                        if update_result:
                            return update_result
                        failed_auto_revision = revision
                elif manifest and manifest.get("available") and bool(config.get("auto_update", True)):
                    revision = str(manifest.get("revision") or "")
                    if revision and revision != failed_auto_revision:
                        update_result = _apply_update(config, manifest, None)
                        if update_result:
                            return update_result
                        failed_auto_revision = revision
            except Exception as exc:
                error = f"[pc-client] heartbeat failed: {exc}"
                _last_heartbeat_error = str(exc)[:1000]
                _last_heartbeat_error_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                write_agent_status(
                    "offline",
                    detail=error,
                    latency_ms=round(_last_heartbeat_latency_ms, 1),
                    agent_version=current_version(),
                    server_version=_last_server_version,
                    last_error=str(exc)[:1000],
                )
                print(error, flush=True)
                consecutive_failures += 1
                base_delay = min(90.0, 3.0 * (2 ** min(consecutive_failures - 1, 5)))
                sleep_seconds = base_delay + random.uniform(0.0, min(3.0, base_delay * 0.2))


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
    if "auto_update" not in config:
        config["auto_update"] = True
    if "desktop_managed" not in config:
        config["desktop_managed"] = False
    if "archive_folder" not in config:
        config["archive_folder"] = ""
    if "archive_enabled" not in config:
        config["archive_enabled"] = False
    return config


def _run_main() -> None:
    try:
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

        server_url = normalize_server_url(str(config.get("server_url") or "http://127.0.0.1:8001"))
        discovered_url = discover_backend_url(server_url)
        if discovered_url != server_url:
            print(f"[pc-client] backend autodetect: {server_url} -> {discovered_url}")
            config["server_url"] = discovered_url
            save_config(config)

        if args.init_only:
            print("[pc-client] init-only completed")
            return

        reason = run_agent(config)
        if reason in {"update", "restart"}:
            raise SystemExit(75)
        if reason == "installer_update":
            raise SystemExit(76)
    except KeyboardInterrupt:
        print("\n[pc-client] stopped by user")
    except Exception as exc:
        print(f"[pc-client] error: {exc}")
        print("[pc-client] hint: проверьте --server-url (должен вести на backend API), pair-code и доступность /health")
        raise SystemExit(1)


def main() -> None:
    if "--desktop-managed" not in sys.argv:
        configure_utf8_logging()
    instance = acquire_single_instance("XASS-background-agent")
    if instance is None:
        print("[pc-client] агент уже запущен; второй экземпляр не создан", flush=True)
        return
    try:
        _run_main()
    finally:
        instance.close()


if __name__ == "__main__":
    main()
