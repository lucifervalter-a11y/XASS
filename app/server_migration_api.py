from __future__ import annotations

import asyncio
import logging
import secrets
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.db import get_session
from app.services.server_backup import create_snapshot, resolved
from app.services.server_transfers import TransferStore, encode_code

logger = logging.getLogger(__name__)


class BackupPayload(BaseModel):
    mode: Literal["backup", "migration"] = "backup"
    passphrase: str = Field(default="", max_length=256)


def build_router(settings, require_owner, require_proof, public_origin):
    router = APIRouter()
    root = Path(__file__).resolve().parent.parent
    directory = resolved(root, settings.server_backup_dir)
    # A bearer ticket must be the only public route to the archive.
    if directory.is_relative_to(root):
        raise ValueError("SERVER_BACKUP_DIR должен находиться вне каталога сайта")
    store = TransferStore(directory)

    async def authorize(user=Depends(require_owner), session=Depends(get_session),
                        x_telegram_init_data: str | None = Header(default=None),
                        x_xass_action_proof: str | None = Header(default=None)):
        await require_proof(session=session, user=user, telegram_init_data=x_telegram_init_data or "",
                            action_proof=x_xass_action_proof or "", purpose="server:backup")
        return user

    async def build(job, password):
        try:
            await asyncio.to_thread(create_snapshot, root, settings, store.path(job), password=password)
            store.finish(job)
        except Exception:
            logger.exception("Server snapshot failed: %s", job)
            store.finish(job, "Копия не создана. Проверьте свободное место, доступ к файлам и pg_dump в журнале сервера")

    @router.post("/api/mini/server-backups")
    async def create(payload: BackupPayload, request: Request, background: BackgroundTasks,
                     response: Response, user=Depends(authorize)):
        password = secrets.token_urlsafe(32) if payload.mode == "migration" else payload.passphrase
        if not 12 <= len(password) <= 256:
            raise HTTPException(400, "Пароль должен содержать от 12 до 256 символов")
        origin = public_origin(request)[1]
        try:
            if payload.mode == "migration":
                encode_code(origin, "x" * 43, password)
            job = store.create()
            code = encode_code(origin, store.ticket(job), password) if payload.mode == "migration" else ""
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        background.add_task(build, job, password)
        response.headers["Cache-Control"] = "no-store"
        return {"ok": True, "job": store.get(job), "code": code, "code_ttl_seconds": 3600}

    @router.get("/api/mini/server-backups/{job}")
    async def status(job: str, response: Response, user=Depends(require_owner)):
        try:
            item = store.get(job)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        response.headers["Cache-Control"] = "no-store"
        return {"ok": True, "job": item}

    @router.post("/api/mini/server-backups/{job}/download-ticket")
    async def ticket(job: str, response: Response, user=Depends(authorize)):
        try:
            if store.get(job)["state"] != "ready":
                raise ValueError("Копия пока не готова")
            token = store.ticket(job, ttl=300)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        response.headers["Cache-Control"] = "no-store"
        return {"ok": True, "ticket": token}

    @router.post("/api/mini/server-backups/{job}/revoke")
    async def revoke(job: str, user=Depends(require_owner)):
        try:
            store.revoke(job)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"ok": True}

    @router.post("/api/server-transfer/download")
    async def download(request: Request, authorization: str | None = Header(default=None)):
        # Header for CLI, POST body for native browser downloads (no token in URLs/logs).
        token = (authorization or "").removeprefix("Bearer ")
        if not token:
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 1024:
                    raise HTTPException(413, "Запрос слишком большой")
            token = parse_qs(body.decode("ascii", errors="ignore")).get("ticket", [""])[0]
        try:
            path = store.consume(token)
        except ValueError as exc:
            raise HTTPException(410, str(exc)) from exc
        return FileResponse(path, filename="xass-server-" + path.stem + ".xass-server",
                            media_type="application/octet-stream", headers={"Cache-Control": "private, no-store"})

    return router
