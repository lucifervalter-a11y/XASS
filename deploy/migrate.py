#!/usr/bin/env python3
"""Export, download and restore a complete XASS application snapshot."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import Settings  # noqa: E402
from app.services.server_backup import MAX_BYTES, create_snapshot, decrypt_file, restore_snapshot  # noqa: E402
from app.services.server_transfers import decode_code  # noqa: E402


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("Перенаправление источника запрещено; создайте код на конечном HTTPS-адресе")


def receive(code: str, destination: Path, postgres_url: str = ""):
    if destination.exists() or destination.is_symlink():
        raise ValueError("Каталог назначения уже существует")
    payload = decode_code(code)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".xass-download-", dir=destination.parent) as tmp:
        archive = Path(tmp) / "server.xass-server"
        request = urllib.request.Request(
            payload["origin"] + "/proxy.php?_binary=1&_p=%2Fapi%2Fserver-transfer%2Fdownload",
            data=b"", method="POST", headers={"Authorization": "Bearer " + payload["ticket"]},
        )
        total = 0
        with urllib.request.build_opener(NoRedirect).open(request, timeout=120) as response, archive.open("xb") as output:
            os.chmod(archive, 0o600)
            if response.status != 200 or response.headers.get("X-XASS-Status", "200") != "200":
                raise ValueError("Источник отклонил код: он истёк, использован или копия ещё не готова")
            while block := response.read(1024 * 1024):
                total += len(block)
                if total > MAX_BYTES + 16384:
                    raise ValueError("Архив превышает лимит")
                output.write(block)
        return restore_snapshot(archive, destination, password=payload["password"], postgres_url=postgres_url)


def activate(root: Path, source_stopped: bool):
    if not source_stopped:
        raise ValueError("Сначала остановите backend и агент на старом сервере; затем передайте --source-stopped")
    marker = root / ".migration-pending"
    if not marker.is_file():
        raise ValueError("В этом каталоге нет ожидающей активации миграции")
    # The installer configures nginx/systemd separately. Activation only removes the guard.
    marker.unlink()
    print("Копия активирована. Теперь можно запустить backend и переключить HTTPS-домен.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("--root", type=Path, default=Path.cwd())
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--recipient-key", type=Path)
    export.add_argument("--system-config", action="store_true", help="Also include readable nginx, TLS and XASS systemd configuration")
    for name in ("restore", "receive"):
        sub = commands.add_parser(name)
        sub.add_argument("--destination", type=Path, required=True)
        sub.add_argument("--postgres", action="store_true", help="Prompt for a NEW EMPTY PostgreSQL database URL")
        if name == "restore":
            sub.add_argument("--archive", type=Path, required=True)
            sub.add_argument("--private-key", type=Path)
    decrypt = commands.add_parser("decrypt")
    decrypt.add_argument("--archive", type=Path, required=True)
    decrypt.add_argument("--output", type=Path, required=True)
    decrypt.add_argument("--private-key", type=Path)
    activation = commands.add_parser("activate")
    activation.add_argument("--root", type=Path, default=Path.cwd())
    activation.add_argument("--source-stopped", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.command == "export":
            root = args.root.resolve()
            os.chdir(root)
            public_key = args.recipient_key.read_bytes() if args.recipient_key else b""
            password = "" if public_key else getpass.getpass("Пароль архива (минимум 12 символов): ")
            if not public_key and password != getpass.getpass("Повторите пароль: "):
                raise ValueError("Пароли не совпадают")
            manifest = create_snapshot(root, Settings(), args.output, password=password,
                                       public_key=public_key, system_config=args.system_config)
            print(json.dumps({"ok": True, "revision": manifest["revision"], "database": manifest["database"],
                              "skipped_links": len(manifest["skipped_links"]),
                              "unreadable_system_paths": len(manifest["unreadable_system_paths"]), "bytes": args.output.stat().st_size}))
        elif args.command in ("restore", "receive"):
            postgres = getpass.getpass("DATABASE_URL новой пустой PostgreSQL БД: ") if args.postgres else ""
            if args.command == "receive":
                code = getpass.getpass("Вставьте одноразовый код XASS: ").strip()
                manifest = receive(code, args.destination.absolute(), postgres)
            else:
                key = args.private_key.read_bytes() if args.private_key else b""
                manifest = restore_snapshot(args.archive, args.destination, private_key=key,
                                            password="" if key else getpass.getpass("Пароль архива: "), postgres_url=postgres)
            print("Данные восстановлены. Ревизия: " + manifest["revision"])
            print("Активация заблокирована до остановки старого сервера. См. docs/SERVER_MIGRATION.md.")
        elif args.command == "decrypt":
            key = args.private_key.read_bytes() if args.private_key else b""
            decrypt_file(args.archive, args.output, private_key=key, password="" if key else getpass.getpass("Пароль архива: "))
            print("Архив проверен и расшифрован.")
        elif args.command == "activate":
            activate(args.root, args.source_stopped)
    except Exception as exc:
        # Errors may contain URLs/paths, but never print received codes or key material.
        print("Ошибка: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
