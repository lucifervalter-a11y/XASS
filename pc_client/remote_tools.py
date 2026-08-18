from __future__ import annotations

import io
import mimetypes
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

try:
    from PIL import ImageGrab
except ImportError:  # pragma: no cover - dependency bootstrap handles this in builds
    ImageGrab = None


MAX_CLIPBOARD_CHARS = 64 * 1024
MAX_LIST_ENTRIES = 250
MAX_FILE_BYTES = 32 * 1024 * 1024
ROOT_LABELS = {
    "desktop": "Рабочий стол",
    "downloads": "Загрузки",
    "documents": "Документы",
    "xass_files": "XASS Files",
}


def allowed_roots(data_root: Path) -> dict[str, Path]:
    home = Path(os.environ.get("USERPROFILE") or Path.home()).resolve()
    roots = {
        "desktop": home / "Desktop",
        "downloads": home / "Downloads",
        "documents": home / "Documents",
        "xass_files": data_root / "XASS Files",
    }
    for path in roots.values():
        path.mkdir(parents=True, exist_ok=True)
    return {key: value.resolve() for key, value in roots.items()}


def _safe_target(data_root: Path, root_name: object, relative_path: object = "", *, must_exist: bool = True) -> tuple[Path, Path]:
    roots = allowed_roots(data_root)
    key = str(root_name or "").strip().lower()
    if key not in roots:
        raise ValueError("Недоступная папка")
    raw = str(relative_path or "").replace("\\", "/").strip().strip("/")
    parts = [part for part in raw.split("/") if part not in {"", "."}]
    if any(part == ".." or "\x00" in part or ":" in part for part in parts):
        raise ValueError("Выход за пределы разрешённой папки запрещён")
    root = roots[key]
    target = root.joinpath(*parts).resolve(strict=False)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("Выход за пределы разрешённой папки запрещён") from exc
    if must_exist and not target.exists():
        raise FileNotFoundError("Файл или папка не найдены")
    if target.is_symlink():
        raise ValueError("Ссылки на другие папки недоступны")
    return root, target


def list_files(data_root: Path, root_name: object, relative_path: object = "") -> dict[str, Any]:
    root, folder = _safe_target(data_root, root_name, relative_path)
    if not folder.is_dir():
        raise NotADirectoryError("Выбранный путь не является папкой")
    rows: list[dict[str, Any]] = []
    for item in folder.iterdir():
        if item.is_symlink():
            continue
        try:
            resolved = item.resolve()
            resolved.relative_to(root)
            stat = item.stat()
        except (OSError, ValueError):
            continue
        rows.append({
            "name": item.name, "type": "directory" if item.is_dir() else "file",
            "size": 0 if item.is_dir() else int(stat.st_size),
            "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        })
    rows.sort(key=lambda item: (item["type"] != "directory", item["name"].casefold()))
    relative = str(folder.relative_to(root)).replace("\\", "/")
    return {
        "root": str(root_name).lower(), "root_label": ROOT_LABELS.get(str(root_name).lower(), str(root_name)),
        "path": "" if relative == "." else relative, "entries": rows[:MAX_LIST_ENTRIES],
        "truncated": len(rows) > MAX_LIST_ENTRIES,
    }


def delete_file(data_root: Path, root_name: object, relative_path: object) -> dict[str, Any]:
    root, target = _safe_target(data_root, root_name, relative_path)
    if target == root or not target.is_file():
        raise ValueError("Можно удалить только файл внутри разрешённой папки")
    size = target.stat().st_size
    name = target.name
    target.unlink()
    return {"name": name, "size": int(size)}


def _upload_bytes(
    client: httpx.Client,
    *,
    endpoint: str,
    api_key: str,
    source_name: str,
    command_id: int,
    kind: str,
    filename: str,
    content_type: str,
    body: bytes,
) -> dict[str, Any]:
    response = client.post(
        f"{endpoint.rstrip('/')}/agent/workspace/assets/{int(command_id)}",
        params={"kind": kind},
        headers={
            "X-Api-Key": api_key, "X-XASS-Source": source_name,
            "X-XASS-Filename": filename[:180], "Content-Type": content_type,
        },
        content=body,
        timeout=60.0,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok") or not isinstance(payload.get("asset"), dict):
        raise RuntimeError("Сервер не подтвердил загрузку")
    return payload["asset"]


def capture_screenshot(
    client: httpx.Client,
    *,
    endpoint: str,
    api_key: str,
    source_name: str,
    command_id: int,
) -> dict[str, Any]:
    if os.name != "nt" or ImageGrab is None:
        raise RuntimeError("Screenshot доступен только в Windows-сборке XASS")
    image = ImageGrab.grab(all_screens=True)
    if image.width > 1800:
        ratio = 1800 / image.width
        image = image.resize((1800, max(1, int(image.height * ratio))))
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, "JPEG", quality=82, optimize=True)
    body = buffer.getvalue()
    asset = _upload_bytes(
        client, endpoint=endpoint, api_key=api_key, source_name=source_name, command_id=command_id,
        kind="screenshot", filename=f"{source_name}-screen.jpg", content_type="image/jpeg", body=body,
    )
    return {"asset_token": asset["token"], "size": asset["size"], "sha256": asset["sha256"], "created_at": asset["created_at"]}


def upload_requested_file(
    data_root: Path,
    client: httpx.Client,
    *,
    endpoint: str,
    api_key: str,
    source_name: str,
    command_id: int,
    root_name: object,
    relative_path: object,
) -> dict[str, Any]:
    root, target = _safe_target(data_root, root_name, relative_path)
    if target == root or not target.is_file():
        raise ValueError("Можно скачать только файл")
    size = target.stat().st_size
    if size > MAX_FILE_BYTES:
        raise ValueError("Файл превышает лимит 32 МБ")
    asset = _upload_bytes(
        client, endpoint=endpoint, api_key=api_key, source_name=source_name, command_id=command_id,
        kind="file_download", filename=target.name,
        content_type=mimetypes.guess_type(target.name)[0] or "application/octet-stream", body=target.read_bytes(),
    )
    return {"asset_token": asset["token"], "filename": target.name, "size": size, "sha256": asset["sha256"]}


def receive_uploaded_file(
    data_root: Path,
    client: httpx.Client,
    *,
    endpoint: str,
    api_key: str,
    source_name: str,
    root_name: object,
    relative_path: object,
    asset_token: object,
    filename: object,
) -> dict[str, Any]:
    _, folder = _safe_target(data_root, root_name, relative_path, must_exist=False)
    folder.mkdir(parents=True, exist_ok=True)
    if not folder.is_dir():
        raise NotADirectoryError("Папка назначения недоступна")
    safe_name = re.sub(r"[\x00-\x1f<>:\"/\\|?*]+", "_", Path(str(filename or "xass-file.bin")).name).strip(" .")[:180]
    if not safe_name:
        safe_name = "xass-file.bin"
    target = (folder / safe_name).resolve(strict=False)
    _, checked = _safe_target(data_root, root_name, str(target.relative_to(allowed_roots(data_root)[str(root_name).lower()])), must_exist=False)
    target = checked
    stem, suffix, counter = target.stem, target.suffix, 2
    while target.exists():
        target = target.with_name(f"{stem} ({counter}){suffix}")
        counter += 1
    response = client.get(
        f"{endpoint.rstrip('/')}/agent/workspace/assets/{str(asset_token)}",
        headers={"X-Api-Key": api_key, "X-XASS-Source": source_name}, timeout=60.0,
    )
    response.raise_for_status()
    if len(response.content) > MAX_FILE_BYTES:
        raise ValueError("Файл превышает лимит 32 МБ")
    temporary = target.with_suffix(target.suffix + ".xass-downloading")
    try:
        temporary.write_bytes(response.content)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return {"filename": target.name, "path": str(target), "size": target.stat().st_size}


def clipboard_get() -> str:
    if os.name != "nt":
        raise RuntimeError("Clipboard доступен только на Windows")
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    try:
        value = root.clipboard_get()
    except tk.TclError:
        value = ""
    finally:
        root.destroy()
    return str(value)[:MAX_CLIPBOARD_CHARS]


def clipboard_set(value: object) -> int:
    if os.name != "nt":
        raise RuntimeError("Clipboard доступен только на Windows")
    text = str(value or "")[:MAX_CLIPBOARD_CHARS]
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    try:
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update()
    finally:
        root.destroy()
    return len(text)
