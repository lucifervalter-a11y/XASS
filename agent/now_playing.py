import ctypes
import json
import platform
import shutil
import subprocess
from typing import Any

import psutil


def _run_powershell(command: str, timeout_sec: int = 8) -> tuple[str, str]:
    wrapped = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        "$OutputEncoding=[System.Text.Encoding]::UTF8; "
        + command
    )
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", wrapped],
        capture_output=True,
        text=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_sec,
    )
    return (proc.stdout or "").strip(), (proc.stderr or "").strip()


def _collect_windows_media_sessions() -> list[dict[str, Any]]:
    command = (
        "$ErrorActionPreference='Stop'; "
        "try{Add-Type -AssemblyName System.Runtime.WindowsRuntime | Out-Null}catch{}; "
        "$asTaskMethod=([System.WindowsRuntimeSystemExtensions].GetMethods() | "
        "Where-Object { $_.Name -eq 'AsTask' -and $_.IsGenericMethodDefinition -and $_.GetGenericArguments().Count -eq 1 -and $_.GetParameters().Count -eq 1 } | "
        "Select-Object -First 1); "
        "function Resolve-AsyncResult([object]$operation, [Type]$resultType){"
        "if($null -eq $operation -or $null -eq $resultType){ return $null }; "
        "try{ "
        "$generic=$script:asTaskMethod.MakeGenericMethod(@($resultType)); "
        "$task=$generic.Invoke($null, @($operation)); "
        "if(-not $task.Wait(4000)){ return $null }; "
        "return $task.Result "
        "}catch{}; "
        "try{ return $operation.GetAwaiter().GetResult() }catch{}; "
        "return $null "
        "}; "
        "$managerType=[Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager,"
        "Windows.Media.Control,ContentType=WindowsRuntime]; "
        "$propsType=[Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties,"
        "Windows.Media.Control,ContentType=WindowsRuntime]; "
        "$manager=Resolve-AsyncResult ($managerType::RequestAsync()) $managerType; "
        "if(-not $manager){'[]'; exit 0}; "
        "$sessions=@($manager.GetSessions()); "
        "if($sessions.Count -eq 0){"
        "try{$cur=$manager.GetCurrentSession(); if($cur){$sessions=@($cur)}}catch{}"
        "}; "
        "$rows=@(); "
        "foreach($session in $sessions){"
        "$status=-1; $artist=''; $title=''; $app=''; $album=''; "
        "try{$info=$session.GetPlaybackInfo(); if($info){$status=[int]$info.PlaybackStatus}}catch{}; "
        "try{$props=Resolve-AsyncResult ($session.TryGetMediaPropertiesAsync()) $propsType; if($props){$artist=($props.Artist+'').Trim(); $title=($props.Title+'').Trim(); $album=($props.AlbumTitle+'').Trim()}}catch{}; "
        "try{$app=($session.SourceAppUserModelId+'').Trim()}catch{}; "
        "$rows += [pscustomobject]@{status=$status; artist=$artist; title=$title; album=$album; app=$app}"
        "}; "
        "if($rows.Count -eq 0){'[]'; exit 0}; "
        "$rows | ConvertTo-Json -Compress"
    )

    try:
        raw, stderr = _run_powershell(command)
    except Exception:
        return []
    if not raw:
        return []
    if raw and raw[0] not in "[{":
        # Some shells prepend non-JSON noise; keep only JSON tail.
        start = min((idx for idx in (raw.find("["), raw.find("{")) if idx >= 0), default=-1)
        if start >= 0:
            raw = raw[start:]
    if not raw:
        return []
    if stderr and "exception" in stderr.lower() and raw == "[]":
        return []

    try:
        parsed = json.loads(raw)
    except Exception:
        return []

    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return []

    rows: list[dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "status": int(item.get("status", -1)) if str(item.get("status", "")).lstrip("-").isdigit() else -1,
                "artist": str(item.get("artist") or "").strip(),
                "title": str(item.get("title") or "").strip(),
                "album": str(item.get("album") or "").strip(),
                "app": str(item.get("app") or "").strip(),
            }
        )
    return rows


def debug_windows_media_sessions() -> list[dict[str, Any]]:
    if platform.system().lower() != "windows":
        return []
    return _collect_windows_media_sessions()


def _windows_now_playing() -> str | None:
    sessions = _collect_windows_media_sessions()
    if not sessions:
        return None

    def _score(row: dict[str, Any]) -> tuple[int, int, int, int]:
        status = int(row.get("status", -1))
        has_title = 1 if row.get("title") else 0
        has_artist = 1 if row.get("artist") else 0
        has_album = 1 if row.get("album") else 0
        if status == 4:  # Playing
            rank = 3
        elif status == 5:  # Paused
            rank = 2
        elif status >= 0:
            rank = 1
        else:
            rank = 0
        return (rank, has_title, has_artist, has_album)

    best = max(sessions, key=_score)
    artist = str(best.get("artist") or "").strip()
    title = str(best.get("title") or "").strip()
    app = str(best.get("app") or "").strip()

    if artist and title:
        return f"{artist} - {title}"
    if title:
        return title
    if artist:
        return artist
    if app:
        return app
    return None


def _linux_now_playing() -> str | None:
    if not shutil.which("playerctl"):
        return None
    try:
        proc = subprocess.run(
            ["playerctl", "metadata", "--format", "{{artist}} - {{title}}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        )
        value = (proc.stdout or "").strip()
        return value or None
    except Exception:
        return None


def get_now_playing() -> str | None:
    system = platform.system().lower()
    if system == "windows":
        return _windows_now_playing()
    if system == "linux":
        return _linux_now_playing()
    return None


def _windows_active_window() -> tuple[str | None, str | None]:
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None, None

    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return None, None

    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    title = buffer.value.strip() or None

    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    process_name = None
    try:
        if pid.value:
            process_name = psutil.Process(pid.value).name()
    except Exception:
        process_name = None
    return title, process_name


def get_active_activity() -> dict[str, Any]:
    if platform.system().lower() != "windows":
        return {}

    title, process_name = _windows_active_window()
    if not title and not process_name:
        return {}

    lower_title = (title or "").lower()
    lower_proc = (process_name or "").lower()
    if "chatgpt" in lower_title:
        return {"kind": "chatgpt", "text": "пользователь в ChatGPT", "title": title, "process": process_name}
    if "chrome" in lower_proc or "msedge" in lower_proc or "firefox" in lower_proc:
        return {"kind": "browser", "text": f"открыт браузер: {title or process_name}", "title": title, "process": process_name}
    return {"kind": "app", "text": f"открыто приложение: {title or process_name}", "title": title, "process": process_name}
