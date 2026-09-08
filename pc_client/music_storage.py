"""Private, credential-namespaced music replicas; never scans the PC implicitly."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shutil
import threading
import time
from urllib.parse import urlsplit

try:
    from network_client import create_http_client
except ModuleNotFoundError:
    from pc_client.network_client import create_http_client

CHUNK_BYTES = 512 * 1024
MAX_BYTES = 256 * 1024 * 1024
HEX = re.compile(r"^[a-f0-9]{64}$")
ID = re.compile(r"^[a-f0-9]{32}$")


class StorageError(ValueError):
    pass


def _reparse(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400) if path.exists() or path.is_symlink() else False


def checksum(path: Path) -> tuple[str, int]:
    value, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK_BYTES), b""):
            size += len(chunk)
            if size > MAX_BYTES:
                raise StorageError("Музыкальный файл больше 256 МБ")
            value.update(chunk)
    return value.hexdigest(), size


def store_root(data_root: Path, config: dict) -> Path:
    origin = str(config.get("server_url") or "").rstrip("/")
    parsed = urlsplit(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise StorageError("Некорректный адрес привязанного сервера")
    namespace = hashlib.sha256((origin + "\n" + str(config.get("api_key") or "")).encode()).hexdigest()
    root = data_root / "music-store" / namespace
    for path in (data_root, root.parent, root):
        if _reparse(path):
            raise StorageError("Каталог музыки не должен быть ссылкой")
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root.resolve()


def stored_path(root: Path, digest: str) -> Path:
    if not isinstance(digest, str) or not HEX.fullmatch(digest):
        raise StorageError("Некорректный идентификатор музыки")
    path = root / (digest + ".bin")
    if _reparse(root) or _reparse(path):
        raise StorageError("Музыкальное хранилище содержит ссылку")
    return path


def _checked_job(job):
    if not isinstance(job, dict) or not ID.fullmatch(str(job.get("id", ""))) or not HEX.fullmatch(str(job.get("sha256", ""))):
        raise StorageError("Некорректное задание хранилища")
    if type(job.get("size")) is not int or not 0 < job["size"] <= MAX_BYTES or job.get("operation") not in {"replicate", "restore"}:
        raise StorageError("Некорректные параметры передачи")
    return job["id"], job["sha256"], job["size"]


def transfer(client, base: str, root: Path, job: dict):
    job_id, digest, size = _checked_job(job)
    destination = stored_path(root, digest)
    endpoint = base + "/agent/music-storage/jobs/" + job_id
    started = time.monotonic()
    if job["operation"] == "replicate":
        if not destination.exists() or checksum(destination) != (digest, size):
            partial = root / (job_id + ".part")
            if _reparse(partial):
                raise StorageError("Небезопасный временный файл")
            offset = partial.stat().st_size if partial.exists() else 0
            if offset > size:
                raise StorageError("Размер временной копии изменился")
            if shutil.disk_usage(root).free - (size - offset) < 32 * 1024 * 1024:
                raise StorageError("Недостаточно места для копии музыки")
            while offset < size:
                if time.monotonic() - started > 900:
                    raise StorageError("Передача приостановлена; будет продолжена позже")
                # Stream even a fixed-size protocol chunk: a bad server cannot allocate arbitrary RAM.
                data = bytearray()
                with client.stream("GET", endpoint + "/chunk", params={"offset": offset}, follow_redirects=False) as response:
                    response.raise_for_status()
                    if response.headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
                        raise StorageError("Сжатые блоки хранилища не поддерживаются")
                    for chunk in response.iter_bytes(chunk_size=64 * 1024):
                        if time.monotonic() - started > 900:
                            raise StorageError("Передача приостановлена; будет продолжена позже")
                        data.extend(chunk)
                        if len(data) > CHUNK_BYTES or offset + len(data) > size:
                            raise StorageError("Сервер прислал слишком большой блок")
                if not data:
                    raise StorageError("Пустой блок музыкального файла")
                with partial.open("ab") as stream:
                    stream.write(data); stream.flush(); os.fsync(stream.fileno())
                offset += len(data)
            if checksum(partial) != (digest, size):
                partial.unlink()  # Our exact incomplete transfer, not a user original or verified replica.
                raise StorageError("Копия не прошла проверку SHA256")
            partial.replace(destination)
    else:
        if not destination.is_file() or checksum(destination) != (digest, size):
            client.post(endpoint + "/unavailable").raise_for_status()
            return
        offset = job.get("offset", 0)
        if type(offset) is not int or not 0 <= offset <= size:
            raise StorageError("Некорректное смещение восстановления")
        with destination.open("rb") as stream:
            stream.seek(offset)
            while offset < size:
                if time.monotonic() - started > 900:
                    raise StorageError("Восстановление приостановлено; будет продолжено позже")
                chunk = stream.read(min(CHUNK_BYTES, size - offset))
                if not chunk:
                    raise StorageError("Копия изменилась во время восстановления")
                response = client.put(endpoint + "/chunk", params={"offset": offset}, content=chunk)
                response.raise_for_status()
                returned = response.json().get("offset")
                if type(returned) is not int or not offset + len(chunk) <= returned <= size:
                    raise StorageError("Сервер не подтвердил принятый блок")
                offset = returned
                stream.seek(offset)
    if checksum(destination) != (digest, size):
        raise StorageError("Копия изменилась перед подтверждением")
    client.post(endpoint + "/finish", json={"sha256": digest, "size": size}).raise_for_status()


def selected_directory(data_root: Path, root_name: str, relative: str) -> Path:
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    roots = {"desktop": home / "Desktop", "downloads": home / "Downloads",
             "documents": home / "Documents", "xass_files": data_root / "XASS Files"}
    if root_name not in roots or not isinstance(relative, str):
        raise StorageError("Выберите папку в разрешённом корне файлового менеджера")
    raw = relative.replace("\\", "/")
    if raw.startswith("/") or ":" in raw or "\x00" in raw or ".." in raw.split("/"):
        raise StorageError("Выход за пределы выбранной папки запрещён")
    root = roots[root_name]
    current = root
    for ancestor in [root.parent, root, *[root.joinpath(*raw.split("/")[:index]) for index in range(1, len(raw.split("/")) + 1)]]:
        if _reparse(ancestor):
            raise StorageError("Ссылки и junction-каталоги нельзя импортировать")
        current = ancestor
    if not current.is_dir():
        raise StorageError("Выбранная папка не найдена")
    current.resolve().relative_to(root.resolve())
    return current.resolve()


def import_directory(client, base: str, data_root: Path, run: dict):
    if not isinstance(run, dict) or not ID.fullmatch(str(run.get("id", ""))):
        raise StorageError("Некорректное задание импорта")
    folder = selected_directory(data_root, run.get("root"), run.get("path", ""))
    paths, skipped, errors = [], 0, [0]
    def inaccessible(_):
        errors[0] += 1
    for directory, subdirs, names in os.walk(folder, followlinks=False, onerror=inaccessible):
        safe_dirs = []
        for name in sorted(subdirs, key=str.casefold):
            candidate = Path(directory) / name
            if _reparse(candidate):
                errors[0] += 1
            else:
                candidate.resolve().relative_to(folder); safe_dirs.append(name)
        subdirs[:] = safe_dirs
        for name in names:
            candidate = Path(directory) / name
            if _reparse(candidate):
                errors[0] += 1; continue
            if candidate.suffix.lower() not in {".mp3", ".wav", ".flac", ".ogg", ".m4a"}:
                skipped += 1; continue
            candidate.resolve().relative_to(folder)
            paths.append(candidate)
            if len(paths) > 100000:
                raise StorageError("Выберите папку с не более чем 100000 аудиофайлами")
    paths.sort(key=lambda value: (value.relative_to(folder).as_posix().casefold(), value.relative_to(folder).as_posix()))
    endpoint = base + "/agent/music-storage/imports/" + run["id"]
    file_ids = []
    for ordinal, path in enumerate(paths):
        try:
            # Recheck link and containment immediately before reading the selected original.
            selected_directory(data_root, run["root"], str(Path(run.get("path", "")) / path.parent.relative_to(folder)))
            if _reparse(path):
                raise StorageError("Файл стал ссылкой")
            before = path.stat()
            digest, size = checksum(path)
            after = path.stat()
            if size <= 0 or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise StorageError("Файл изменился во время чтения")
            path_key = hashlib.sha256(str(path).casefold().encode("utf-8")).hexdigest()
            response = client.post(endpoint + "/files", json={"path_key": path_key, "sha256": digest,
                "size": size, "filename": path.name, "ordinal": ordinal})
            response.raise_for_status(); registered = response.json()
            file_id, offset = registered.get("file_id"), registered.get("offset")
            if not ID.fullmatch(str(file_id)) or type(offset) is not int or not 0 <= offset <= size:
                raise StorageError("Сервер вернул некорректный файл импорта")
            file_endpoint = endpoint + "/files/" + file_id
            if not registered.get("track_id"):
                with path.open("rb") as stream:
                    stream.seek(offset)
                    while offset < size:
                        data = stream.read(min(CHUNK_BYTES, size - offset))
                        if not data:
                            raise StorageError("Исходный файл изменился")
                        result = client.put(file_endpoint + "/chunk", params={"offset": offset}, content=data)
                        result.raise_for_status()
                        next_offset = result.json().get("offset")
                        if type(next_offset) is not int or not offset + len(data) <= next_offset <= size:
                            raise StorageError("Сервер не подтвердил блок импорта")
                        offset = next_offset; stream.seek(offset)
                result = client.post(file_endpoint + "/finish")
                # Bad/changed audio is reported explicitly, never converted to a fake track.
                if result.status_code in {409, 422}:
                    errors[0] += 1; continue
                result.raise_for_status()
            file_ids.append(file_id)
        except (OSError, StorageError):
            errors[0] += 1
    client.post(endpoint + "/finish", json={"file_ids": file_ids, "skipped": skipped, "errors": errors[0]}).raise_for_status()


class StorageWorker:
    def __init__(self):
        self._guard = threading.Lock()
        self._thread = None
        self._next = 0.0
        self._status = {"version": 1, "state": "idle", "error": ""}

    def snapshot(self):
        with self._guard:
            return dict(self._status)

    def submit(self, config, data_root, *, force=False):
        if not str(config.get("api_key", "")).startswith("ag_"):
            return
        with self._guard:
            if (self._thread and self._thread.is_alive()) or (not force and time.monotonic() < self._next):
                return
            self._next = time.monotonic() + 30
            snapshot = dict(config)
            self._thread = threading.Thread(target=self._run, args=(snapshot, Path(data_root)), name="xass-music-storage", daemon=True)
            self._thread.start()

    def _run(self, config, data_root):
        try:
            base = str(config["server_url"]).rstrip("/")
            with create_http_client(base, timeout=20, trust_env=bool(config.get("trust_env_proxy", False)), follow_redirects=False) as client:
                client.headers.update({"X-Api-Key": str(config["api_key"]), "Accept-Encoding": "identity"})
                response = client.get(base + "/agent/music-storage/jobs")
                if response.status_code == 404:  # Compatible with a server not upgraded yet.
                    return
                response.raise_for_status()
                body = response.json()
                jobs = body.get("jobs", [])
                if not isinstance(jobs, list) or len(jobs) > 10:
                    raise StorageError("Некорректная очередь хранилища")
                for job in jobs:
                    with self._guard:
                        self._status.update(state="transferring", error="")
                    transfer(client, base, store_root(data_root, config), job)
                for run in body.get("imports", [])[:1]:
                    import_directory(client, base, data_root, run)
                with self._guard:
                    self._status.update(state="idle", error="")
        except Exception:
            with self._guard:
                self._status.update(state="retrying", error="Передача музыки не завершена. Проверьте связь и место на диске.")


_worker = StorageWorker()


def storage_tick(config, data_root, *, force=False):
    _worker.submit(config, data_root, force=force)


def storage_snapshot():
    return _worker.snapshot()
