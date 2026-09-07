"""Persistent backup jobs and atomically consumed, short-lived transfer tickets."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

TTL = 3600


def encode_code(origin: str, ticket: str, password: str) -> str:
    parsed = urlsplit(origin)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("Для переноса нужен публичный HTTPS-адрес сервера")
    raw = json.dumps({"v": 1, "origin": origin.rstrip("/"), "ticket": ticket, "password": password}, separators=(",", ":")).encode()
    return "XASS1." + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_code(code: str) -> dict:
    try:
        if not code.startswith("XASS1.") or len(code) > 4096:
            raise ValueError()
        encoded = code[6:]
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        if payload["v"] != 1 or not 12 <= len(payload["password"]) <= 256 or len(payload["ticket"]) != 43:
            raise ValueError()
        encode_code(payload["origin"], payload["ticket"], payload["password"])
        return payload
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Неверный код переноса XASS") from exc


class TransferStore:
    def __init__(self, directory: Path):
        self.directory = directory

    @contextmanager
    def connect(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        db = self.directory / "transfers.sqlite3"
        connection = sqlite3.connect(db, timeout=15)
        os.chmod(db, 0o600)
        connection.row_factory = sqlite3.Row
        try:
            connection.executescript(
                "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, created REAL, expires REAL, state TEXT, error TEXT);"
                "CREATE TABLE IF NOT EXISTS tickets (hash TEXT PRIMARY KEY, job_id TEXT, expires REAL, used INTEGER DEFAULT 0);"
            )
            with connection:
                yield connection
        finally:
            connection.close()

    def cleanup(self):
        now = time.time()
        with self.connect() as db:
            expired = db.execute("SELECT id FROM jobs WHERE expires < ?", (now,)).fetchall()
            for row in expired:
                self.path(row["id"]).unlink(missing_ok=True)
            db.execute("DELETE FROM tickets WHERE expires < ?", (now,))
            db.execute("DELETE FROM jobs WHERE expires < ?", (now,))
            db.execute("UPDATE jobs SET state='failed', error='Задача прервана; создайте копию заново' WHERE state='building' AND created < ?", (now - TTL,))

    def create(self) -> str:
        self.cleanup()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM jobs WHERE state='building'").fetchone():
                raise ValueError("Копия уже создаётся. Дождитесь завершения")
            job = secrets.token_hex(16)
            db.execute("INSERT INTO jobs VALUES (?, ?, ?, 'building', '')", (job, time.time(), time.time() + 86400))
            return job

    def finish(self, job: str, error: str = ""):
        with self.connect() as db:
            db.execute("UPDATE jobs SET state=?, error=? WHERE id=?", ("failed" if error else "ready", error, job))

    def path(self, job: str) -> Path:
        if len(job) != 32 or any(c not in "0123456789abcdef" for c in job):
            raise ValueError("Неверный идентификатор копии")
        return self.directory / (job + ".xass-server")

    def get(self, job: str) -> dict:
        self.path(job)
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=? AND expires>?", (job, time.time())).fetchone()
        if row is None:
            raise ValueError("Копия не найдена или срок хранения истёк")
        item = dict(row)
        item["size"] = self.path(job).stat().st_size if item["state"] == "ready" and self.path(job).exists() else 0
        return item

    def ticket(self, job: str, ttl: int = TTL) -> str:
        self.get(job)
        token = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute("INSERT INTO tickets(hash, job_id, expires) VALUES (?, ?, ?)",
                       (hashlib.sha256(token.encode()).hexdigest(), job, time.time() + ttl))
        return token

    def consume(self, token: str) -> Path:
        if len(token) != 43:
            raise ValueError("Код недействителен, использован или просрочен")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT t.job_id, j.state FROM tickets t JOIN jobs j ON j.id=t.job_id "
                             "WHERE t.hash=? AND t.used=0 AND t.expires>? AND j.expires>?",
                             (hashlib.sha256(token.encode()).hexdigest(), time.time(), time.time())).fetchone()
            if row is None:
                raise ValueError("Код недействителен, использован или просрочен")
            if row["state"] != "ready" or not self.path(row["job_id"]).is_file():
                raise ValueError("Копия пока не готова")
            db.execute("UPDATE tickets SET used=1 WHERE hash=?", (hashlib.sha256(token.encode()).hexdigest(),))
            return self.path(row["job_id"])

    def revoke(self, job: str):
        self.get(job)
        with self.connect() as db:
            db.execute("UPDATE tickets SET used=1 WHERE job_id=?", (job,))
