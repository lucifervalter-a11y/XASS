from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

CLIENT_ROOT = Path(__file__).resolve().parent
VERSION_PATH = CLIENT_ROOT / "version.json"
REVISION_PATH = CLIENT_ROOT / ".installed-revision"
RESULTS_PATH = CLIENT_ROOT / ".command-results.json"
UPDATE_ROOT = CLIENT_ROOT / ".updates"
UPDATE_MARKER = UPDATE_ROOT / ".in-progress"


def current_version() -> str:
    try:
        payload = json.loads(VERSION_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return "0.0.0"
    return str(payload.get("version") or "0.0.0").strip() or "0.0.0"


def current_revision() -> str:
    try:
        return REVISION_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _manifest_message(manifest: dict[str, Any]) -> bytes:
    return (
        f"{manifest.get('version', '')}\n{manifest.get('revision', '')}\n"
        f"{manifest.get('sha256', '')}\n{manifest.get('url', '')}"
    ).encode("utf-8")


def verify_manifest(manifest: dict[str, Any], api_key: str) -> bool:
    signature = str(manifest.get("signature") or "")
    if len(signature) != 64 or not api_key:
        return False
    expected = hmac.new(api_key.encode("utf-8"), _manifest_message(manifest), hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_archive(archive: zipfile.ZipFile) -> None:
    infos = archive.infolist()
    if len(infos) > 250:
        raise RuntimeError("update package contains too many files")
    total_size = 0
    for info in infos:
        raw_name = info.filename.replace("\\", "/")
        name = PurePosixPath(raw_name)
        has_drive = bool(name.parts and ":" in name.parts[0])
        if name.is_absolute() or ".." in name.parts or not name.parts or has_drive:
            raise RuntimeError("unsafe path in update package")
        total_size += max(0, int(info.file_size))
    if total_size > 150 * 1024 * 1024:
        raise RuntimeError("update package is too large")


def download_update(
    manifest: dict[str, Any],
    *,
    api_key: str,
    trust_env: bool = False,
    progress: Callable[[str], None] | None = None,
) -> Path:
    import httpx

    if not verify_manifest(manifest, api_key):
        raise RuntimeError("update manifest signature is invalid")
    url = str(manifest.get("url") or "").strip()
    expected_sha = str(manifest.get("sha256") or "").strip().lower()
    revision = str(manifest.get("revision") or "").strip()
    if not url or len(expected_sha) != 64 or not revision:
        raise RuntimeError("update manifest is incomplete")

    # Every downloader gets a private staging directory. The desktop UI and its
    # background agent can briefly overlap while an old version is restarting;
    # sharing package.zip made those two writers corrupt each other's download.
    attempt_id = uuid4().hex
    stage_root = UPDATE_ROOT / f"{revision[:16]}-{attempt_id[:8]}"
    stage_root.mkdir(parents=True, exist_ok=False)
    package_path = stage_root / "package.zip"
    extract_path = stage_root / "staged"
    try:
        with httpx.Client(timeout=90, trust_env=trust_env, follow_redirects=True) as client:
            for attempt in range(2):
                if progress:
                    progress("Скачивание обновления…" if attempt == 0 else "Повторная загрузка обновления…")
                download_url = url if attempt == 0 else _cache_busted_url(url, attempt_id)
                headers = {
                    "X-Api-Key": api_key,
                    "Accept-Encoding": "identity",
                    "Cache-Control": "no-cache",
                }
                with client.stream("GET", download_url, headers=headers) as response:
                    response.raise_for_status()
                    with package_path.open("wb") as handle:
                        for chunk in response.iter_bytes():
                            handle.write(chunk)
                actual_sha = _sha256(package_path)
                if actual_sha == expected_sha:
                    break
                package_path.unlink(missing_ok=True)
            else:
                raise RuntimeError("update package checksum mismatch after retry")

        extract_path.mkdir(parents=True, exist_ok=False)
        with zipfile.ZipFile(package_path, "r") as archive:
            _validate_archive(archive)
            archive.extractall(extract_path)
        if progress:
            progress("Пакет проверен")
        return extract_path
    except Exception:
        shutil.rmtree(stage_root, ignore_errors=True)
        raise


def _cache_busted_url(url: str, token: str) -> str:
    parsed = urlsplit(url)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query.append(("download", token))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def launch_update_helper(
    stage_path: Path,
    manifest: dict[str, Any],
    *,
    command_id: int | None,
    restart_target: str,
    minimized: bool = False,
) -> None:
    helper = CLIENT_ROOT / "updater_helper.py"
    args = [
        sys.executable,
        str(helper),
        "--pid",
        str(os.getpid()),
        "--stage",
        str(stage_path),
        "--target",
        str(CLIENT_ROOT),
        "--version",
        str(manifest.get("version") or "0.0.0"),
        "--revision",
        str(manifest.get("revision") or ""),
        "--restart-target",
        restart_target,
    ]
    if command_id is not None:
        args.extend(["--command-id", str(command_id)])
    if minimized:
        args.append("--minimized")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    UPDATE_ROOT.mkdir(parents=True, exist_ok=True)
    marker_payload = {
        "pid": 0,
        "revision": str(manifest.get("revision") or ""),
        "started_at": time.time(),
    }
    UPDATE_MARKER.write_text(json.dumps(marker_payload), encoding="utf-8")
    try:
        helper_process = subprocess.Popen(args, cwd=str(CLIENT_ROOT), creationflags=creationflags)
    except Exception:
        UPDATE_MARKER.unlink(missing_ok=True)
        raise
    marker_payload["pid"] = helper_process.pid
    try:
        UPDATE_MARKER.write_text(json.dumps(marker_payload), encoding="utf-8")
    except OSError:
        pass


def load_command_results() -> list[dict[str, Any]]:
    try:
        payload = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    return payload if isinstance(payload, list) else []


def store_command_result(command_id: int, ok: bool, message: str, details: dict[str, Any] | None = None) -> None:
    rows = [row for row in load_command_results() if int(row.get("id", -1)) != int(command_id)]
    rows.append({"id": int(command_id), "ok": bool(ok), "message": str(message)[:1000], "details": details or {}})
    temporary = RESULTS_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(rows[-50:], ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(RESULTS_PATH)


def clear_command_results(command_ids: list[int]) -> None:
    ids = {int(item) for item in command_ids}
    remaining = [row for row in load_command_results() if int(row.get("id", -1)) not in ids]
    if remaining:
        RESULTS_PATH.write_text(json.dumps(remaining, ensure_ascii=False, indent=2), encoding="utf-8")
    elif RESULTS_PATH.exists():
        RESULTS_PATH.unlink()
