from __future__ import annotations

import hashlib
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_DIR = ROOT / ".venv"
REQUIREMENTS = ROOT / "requirements.txt"
STAMP = VENV_DIR / ".requirements.sha256"


def _venv_python() -> Path:
    if sys.platform == "win32":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def _requirements_digest() -> str:
    return hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()


def _dependencies_ready(python: Path, digest: str) -> bool:
    try:
        if STAMP.read_text(encoding="utf-8").strip() != digest:
            return False
    except OSError:
        return False
    result = subprocess.run(
        [str(python), "-c", "import httpx, psutil, PIL, pystray, tkinterdnd2"],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def main() -> int:
    if sys.version_info < (3, 11):
        print("[xass] Нужен Python 3.11 или новее.")
        return 2
    if not REQUIREMENTS.is_file():
        print(f"[xass] Не найден файл зависимостей: {REQUIREMENTS}")
        return 2

    python = _venv_python()
    if not python.is_file():
        print("[xass] Создаю изолированное окружение Python…")
        venv.EnvBuilder(with_pip=True, clear=VENV_DIR.exists()).create(VENV_DIR)

    digest = _requirements_digest()
    if _dependencies_ready(python, digest):
        print("[xass] Зависимости уже установлены.")
        return 0

    print("[xass] Устанавливаю зависимости приложения…")
    result = subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--retries",
            "4",
            "--timeout",
            "30",
            "-r",
            str(REQUIREMENTS),
        ],
        cwd=str(ROOT),
        check=False,
    )
    if result.returncode != 0:
        print("[xass] Не удалось установить зависимости. Проверьте интернет и повторите запуск.")
        return result.returncode or 1

    STAMP.write_text(digest, encoding="utf-8")
    if not _dependencies_ready(python, digest):
        print("[xass] Проверка установленных зависимостей не пройдена.")
        return 1
    print("[xass] Зависимости установлены.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
