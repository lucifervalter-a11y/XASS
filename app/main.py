import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit
from uuid import uuid4

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot_api import TelegramBotClient
from app.config import Settings, get_settings
from app.db import SessionLocal, get_session, init_db
from app.poller import telegram_polling_loop
from app.schemas import (
    AgentPairClaimPayload,
    AgentPairClaimResponse,
    ExternalNowPlayingPayload,
    HeartbeatPayload,
    HeartbeatResponse,
)
from app.scheduler import offline_check_loop
from app.enums import SaveMode
from app.models import HeartbeatSource, MediaAsset, MessageLog, MessageRevision
from app.services.agent_commands import (
    acknowledge_agent_commands,
    deliver_agent_commands,
    enqueue_agent_command,
    latest_agent_commands,
)
from app.services.agent_connection import build_connection_profile, normalize_server_origin
from app.services.agent_archive import archive_events_after, archive_summary, archive_target_map, is_archive_target, set_archive_target
from app.services.agent_installer import (
    build_installer_manifest,
    get_agent_installer,
    installer_public_info,
    issue_installer_ticket,
    verify_installer_ticket,
)
from app.services.agent_pairing import authenticate_agent_api_key, claim_pair_code_and_issue_key, issue_pair_code
from app.services.agent_updates import build_agent_package, build_update_manifest
from app.services.app_config import (
    cycle_save_mode,
    get_or_create_app_config,
    parse_time_range,
    set_away_for_minutes,
    set_away_message,
    set_away_mode,
    set_away_schedule,
    set_quiet_hours_window,
    set_save_mode,
    set_service_base_url,
    toggle_away_mode,
    toggle_quiet_hours,
)
from app.services.bot_identity import load_cached_bot_username, normalize_bot_username, save_cached_bot_username
from app.services.heartbeat import is_quiet_hours, list_sources, process_heartbeat
from app.services.miniapp import MiniAppUser, authenticate as miniapp_authenticate
from app.services.pwa_auth import (
    COOKIE_NAME as PWA_COOKIE_NAME,
    SESSION_AGE_SEC as PWA_SESSION_AGE_SEC,
    authenticate_session as pwa_authenticate_session,
    authenticate_telegram_login as pwa_authenticate_login,
    issue_session as issue_pwa_session,
    issue_action_proof,
    verify_action_proof,
)
from app.services.passkeys import (
    authentication_options as passkey_authentication_options,
    complete_authentication as passkey_complete_authentication,
    complete_registration as passkey_complete_registration,
    count_credentials as passkey_count_credentials,
    list_credentials as passkey_list_credentials,
    registration_options as passkey_registration_options,
    transaction_owner as passkey_transaction_owner,
)
from app.services.pwa_pairing import PwaPairingError, consume_pwa_pair_token, issue_pwa_pair_token
from app.services.monitoring import collect_server_metrics, collect_systemd_statuses
from app.services.music_card import build_music_card, build_search_links, fallback_music_card
from app.services.profile_editor import (
    ensure_profile_exists,
    load_profile,
    save_profile,
    save_profile_with_backup,
    validate_http_url,
)
from app.services.quotes_store import add_quote, delete_quote, ensure_quotes_exists, load_quotes, update_quote
from app.services.restart_notice import clear_restart_notice, get_restart_notice, save_restart_notice
from app.services.profile_runtime import set_profile_now_playing_source, sync_profile_now_playing_from_heartbeat, update_profile_discord, update_profile_now_playing_external
from app.services.projects_store import (
    append_audit_log as append_projects_audit_log,
    backup_json_file,
    create_project_id,
    ensure_projects_exists,
    ensure_site_config_exists,
    load_projects,
    normalize_project,
    save_projects,
)
from app.services.updater import get_update_status, restart_service, run_update
from app.storage import ensure_data_dirs
from app.telegram_handler import TelegramUpdateHandler

from sqlalchemy import case, func, select

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

APP_VERSION = "0.10.0"

settings = get_settings()
bot_client = TelegramBotClient(settings.bot_token) if settings.bot_token else None
update_handler = TelegramUpdateHandler(settings, bot_client)


def _restart_notice_chat_candidates(primary_chat_id: Any) -> list[int]:
    candidates: list[int] = []
    for value in (primary_chat_id, settings.notify_chat_id, settings.owner_user_id):
        try:
            parsed = int(value) if value is not None else 0
        except (TypeError, ValueError):
            parsed = 0
        if parsed and parsed not in candidates:
            candidates.append(parsed)
    return candidates


def _notify_chat_id(config_chat_id: int | None) -> int | None:
    if config_chat_id:
        return config_chat_id
    if settings.notify_chat_id:
        return settings.notify_chat_id
    if settings.owner_user_id:
        return settings.owner_user_id
    return None


def _verify_api_key(header_value: str | None, expected: str, reason: str) -> None:
    if not expected:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"{reason} key is not configured")
    if (header_value or "").strip() != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Invalid {reason} key")


def _resolve_external_now_playing_text(payload: ExternalNowPlayingPayload) -> str:
    direct_text = (payload.text or "").strip()
    if direct_text:
        return direct_text

    artist = (payload.artist or "").strip()
    title = (payload.title or payload.track or "").strip()
    if artist and title:
        return f"{artist} - {title}"
    if title:
        return title
    return ""


async def require_setup_api_key(x_api_key: str | None = Header(default=None)) -> None:
    _verify_api_key(x_api_key, settings.setup_api_key, "setup")


class WebhookSetupPayload(BaseModel):
    public_base_url: HttpUrl


class VkSaveTokenPayload(BaseModel):
    access_token: str
    user_id: int
    secret: str
    chat_id: int | None = None


class MiniSettingPayload(BaseModel):
    key: str
    value: Any = None


class MiniQuotePayload(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class MiniAgentCommandPayload(BaseModel):
    command: str
    payload: dict[str, Any] | None = None
    action_proof: str = Field(default="", max_length=2048)


class MiniArchiveTargetPayload(BaseModel):
    enabled: bool


class MiniAgentPairPayload(BaseModel):
    server_url: str = Field(default="", max_length=512)
    source_name: str = Field(default="", max_length=128)


class MiniPwaPairPayload(BaseModel):
    public_url: str = Field(default="", max_length=512)


class PwaPairExchangePayload(BaseModel):
    token: str = Field(min_length=32, max_length=256)


class PwaTelegramLoginPayload(BaseModel):
    id: int
    first_name: str = Field(default="", max_length=128)
    last_name: str = Field(default="", max_length=128)
    username: str = Field(default="", max_length=128)
    photo_url: str = Field(default="", max_length=1000)
    auth_date: int
    hash: str = Field(min_length=64, max_length=64)


class PasskeyStartPayload(BaseModel):
    purpose: str = Field(default="login", max_length=300)


class PasskeyCompletePayload(BaseModel):
    transaction: str = Field(min_length=20, max_length=256)
    credential: dict[str, Any]
    name: str = Field(default="", max_length=120)


class MiniSiteProfilePayload(BaseModel):
    name: str = Field(default="", max_length=120)
    title: str = Field(default="", max_length=180)
    bio: str = Field(default="", max_length=1200)
    username: str = Field(default="", max_length=80)
    telegram_url: str = Field(default="", max_length=500)
    avatar_url: str = Field(default="", max_length=500)
    quote: str = Field(default="", max_length=500)
    stack: list[str] = Field(default_factory=list, max_length=24)
    links: list[dict[str, str]] = Field(default_factory=list, max_length=16)


class MiniSiteProjectPayload(BaseModel):
    id: str = Field(default="", max_length=100)
    title: str = Field(default="", max_length=160)
    subtitle: str = Field(default="", max_length=240)
    description: str = Field(default="", max_length=2000)
    url: str = Field(default="", max_length=500)
    status: str = Field(default="dev", max_length=32)
    year_from: int = Field(default_factory=lambda: datetime.now(timezone.utc).year, ge=1970, le=2100)
    year_to: int = Field(default_factory=lambda: datetime.now(timezone.utc).year, ge=1970, le=2100)
    tags: list[str] = Field(default_factory=list, max_length=24)
    featured: bool = False
    cover_type: str = Field(default="image", max_length=16)
    cover_src: str = Field(default="", max_length=1000)


async def _run_bot_post_startup() -> None:
    """Finish Telegram setup without blocking readiness of the HTTP server."""
    if bot_client is None:
        return
    identity_cache = Path(getattr(settings, "telegram_bot_identity_cache_path", "./data/telegram_bot_identity.json"))
    if not normalize_bot_username(getattr(settings, "telegram_bot_username", "")):
        cached_username = load_cached_bot_username(identity_cache)
        if cached_username:
            settings.telegram_bot_username = cached_username
            logger.info("Telegram bot username restored from local cache: @%s", cached_username)
    if not normalize_bot_username(getattr(settings, "telegram_bot_username", "")) and hasattr(bot_client, "get_me"):
        try:
            bot_identity = await bot_client.get_me()
            discovered_username = normalize_bot_username(bot_identity.get("username"))
            if discovered_username:
                settings.telegram_bot_username = discovered_username
                save_cached_bot_username(identity_cache, discovered_username)
                logger.info("Telegram bot username discovered automatically: @%s", discovered_username)
        except Exception:
            logger.warning("Failed to discover Telegram bot username", exc_info=True)
    try:
        notice = get_restart_notice(settings)
        if isinstance(notice, dict):
            reason = str(notice.get("reason") or "перезапуск").strip() or "перезапуск"
            sent = False
            for chat_id in _restart_notice_chat_candidates(notice.get("chat_id")):
                try:
                    await bot_client.send_message(chat_id, f"✅ Сервис успешно перезапущен ({reason}).")
                    sent = True
                    break
                except Exception:
                    logger.warning("Failed to deliver restart success notice to chat_id=%s", chat_id)
            if sent:
                clear_restart_notice(settings)
            else:
                logger.warning("Restart success notice retained for next startup attempt")
    except Exception:
        logger.warning("Failed to process restart success notice", exc_info=True)

    try:
        async with SessionLocal() as session:
            config = await get_or_create_app_config(session, settings)
        raw_base = (getattr(config, "service_base_url", None) or "").strip() or (settings.profile_public_url or "").strip()
        if raw_base:
            parsed = urlsplit(raw_base)
            if parsed.scheme and parsed.netloc:
                miniapp_url = f"{parsed.scheme}://{parsed.netloc}/miniapp.php"
                await bot_client.set_chat_menu_button(
                    menu_button={"type": "web_app", "text": "XASS", "web_app": {"url": miniapp_url}}
                )
                logger.info("Chat menu button set to %s", miniapp_url)
    except Exception:
        logger.warning("Failed to set chat menu button", exc_info=True)

    try:
        await bot_client.set_my_commands(
            [
                {"command": "start", "description": "Панель управления"},
                {"command": "webapp", "description": "Открыть мини-приложение XASS"},
                {"command": "status", "description": "Статус heartbeat-источников"},
                {"command": "server", "description": "Метрики сервера"},
                {"command": "pc", "description": "Состояние ПК-агентов"},
                {"command": "chats", "description": "Сохранённые переписки"},
                {"command": "deleted", "description": "Удалённые сообщения"},
                {"command": "archive", "description": "Локальный архив на ПК"},
                {"command": "update", "description": "Обновление бота и сервиса"},
                {"command": "help", "description": "Все команды (.muz, .weather…)"},
            ]
        )
        logger.info("Bot commands registered")
    except Exception:
        logger.warning("Failed to set bot commands", exc_info=True)


async def _restart_after_mini_request(chat_id: int, reason: str) -> None:
    # This background task starts only after the HTTP response has been sent.
    await asyncio.sleep(0.35)
    save_restart_notice(settings, chat_id=chat_id, reason=reason)
    try:
        await asyncio.to_thread(restart_service, settings)
    except Exception:
        logger.exception("Mini App update applied, but service restart command failed")
        mode = (settings.service_restart_mode or "systemd").strip().lower()
        if mode == "systemd" and (os.environ.get("INVOCATION_ID") or os.getppid() == 1):
            os._exit(0)
        clear_restart_notice(settings)


async def _update_miniapp_menu_button(public_url: str) -> None:
    """Update Telegram separately so link creation never waits on its API."""
    client = bot_client
    if client is None:
        return
    try:
        await asyncio.wait_for(
            client.set_chat_menu_button(
                menu_button={
                    "type": "web_app",
                    "text": "XASS",
                    "web_app": {"url": f"{public_url}/miniapp.php"},
                }
            ),
            timeout=4.0,
        )
    except TimeoutError:
        logger.warning("Timed out updating Mini App menu button after PWA setup")
    except Exception:
        logger.warning("Failed to update Mini App menu button after PWA setup", exc_info=True)


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_data_dirs()
    ensure_profile_exists(Path(settings.profile_json_path))
    ensure_projects_exists(Path(settings.projects_json_path))
    ensure_site_config_exists(Path(settings.site_config_json_path))
    ensure_quotes_exists(Path(settings.quotes_json_path))
    await init_db()
    async with SessionLocal() as session:
        await get_or_create_app_config(session, settings)

    stop_event = asyncio.Event()
    tasks: list[asyncio.Task] = []
    tasks.append(asyncio.create_task(offline_check_loop(settings, bot_client, stop_event)))
    if settings.use_polling:
        tasks.append(
            asyncio.create_task(
                telegram_polling_loop(
                    settings=settings,
                    bot_client=bot_client,
                    update_handler=update_handler,
                    stop_event=stop_event,
                )
            )
        )
        logger.info("Startup mode: polling")
    else:
        logger.info("Startup mode: webhook")

    if bot_client:
        tasks.append(asyncio.create_task(_run_bot_post_startup()))
    try:
        yield
    finally:
        stop_event.set()
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        if bot_client:
            await bot_client.close()


app = FastAPI(
    title="Serverredus Telegram Business Control",
    version=APP_VERSION,
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/mini/ping")
async def mini_ping() -> dict[str, Any]:
    import sys
    return {"ok": True, "python": sys.version, "status": "backend running", "app_version": APP_VERSION}


@app.post("/agent/pair/claim", response_model=AgentPairClaimResponse)
async def agent_pair_claim(
    payload: AgentPairClaimPayload,
    session: AsyncSession = Depends(get_session),
) -> AgentPairClaimResponse:
    try:
        result = await claim_pair_code_and_issue_key(
            session,
            pair_code=payload.pair_code,
            source_name=payload.source_name,
            source_type=payload.source_type,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return AgentPairClaimResponse(
        ok=True,
        source_name=result.source_name,
        source_type=result.source_type,
        agent_api_key=result.agent_api_key,
        issued_at=result.issued_at,
    )


@app.post("/agent/heartbeat", response_model=HeartbeatResponse)
async def agent_heartbeat(
    payload: HeartbeatPayload,
    request: Request,
    session: AsyncSession = Depends(get_session),
    x_api_key: str | None = Header(default=None),
) -> HeartbeatResponse:
    auth = await authenticate_agent_api_key(
        session,
        api_key=x_api_key,
        global_agent_api_key=settings.agent_api_key,
    )
    if auth is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid agent key")

    # For per-agent keys we pin source_name on server side to keep identity stable.
    if auth.source_name and payload.source_name != auth.source_name:
        payload = payload.model_copy(update={"source_name": auth.source_name})

    await acknowledge_agent_commands(
        session,
        source_name=payload.source_name,
        results=payload.command_results,
    )

    config = await get_or_create_app_config(session, settings)
    source, recovered, is_new = await process_heartbeat(session, payload)
    await sync_profile_now_playing_from_heartbeat(session, settings, config.heartbeat_timeout_minutes)
    if isinstance(payload.discord, dict) and payload.discord:
        update_profile_discord(settings, payload.discord)

    if is_new and bot_client:
        chat_id = _notify_chat_id(config.notify_chat_id)
        if chat_id:
            await bot_client.send_message(
                chat_id,
                (
                    "Новый агент подключен к серверу.\n"
                    f"Текущее имя: {source.source_name}\n"
                    f"Тип: {source.source_type}\n"
                    "Если хотите переименовать:\n"
                    f"/pcname {source.source_name} MY_PC"
                ),
            )

    if recovered and bot_client and not is_quiet_hours(config, settings):
        chat_id = _notify_chat_id(config.notify_chat_id)
        if chat_id:
            await bot_client.send_message(
                chat_id,
                (
                    "Связь восстановлена.\n"
                    f"Источник: {source.source_name}\n"
                    f"Тип: {source.source_type}\n"
                    f"Последний heartbeat: {source.last_seen_at.isoformat()}"
                ),
            )

    commands = await deliver_agent_commands(session, source_name=source.source_name)
    archive_enabled = await is_archive_target(session, source.source_name)
    archive_events = (
        await archive_events_after(session, cursor=payload.archive_cursor, limit=100)
        if archive_enabled
        else []
    )
    manifest_base_url = str(request.base_url).rstrip("/")
    configured_base_url = (config.service_base_url or settings.profile_public_url or "").strip()
    if configured_base_url:
        configured_url = urlsplit(configured_base_url)
        if configured_url.scheme and configured_url.netloc:
            manifest_base_url = f"{configured_url.scheme}://{configured_url.netloc}"
    update_manifest = await asyncio.to_thread(
        build_update_manifest,
        settings,
        api_key=(x_api_key or "").strip(),
        base_url=manifest_base_url,
        current_version=payload.agent_version,
        current_revision=payload.agent_revision,
    ) if payload.agent_distribution != "installer" else None
    installer_manifest = await asyncio.to_thread(
        build_installer_manifest,
        settings,
        api_key=(x_api_key or "").strip(),
        base_url=manifest_base_url,
        current_version=payload.agent_version,
        current_revision=payload.agent_revision,
    ) if payload.agent_distribution == "installer" else None

    return HeartbeatResponse(
        ok=True,
        source_name=source.source_name,
        recovered=recovered,
        new_source=is_new,
        server_time=datetime.now(timezone.utc),
        server_version=APP_VERSION,
        update=update_manifest,
        installer_update=installer_manifest,
        commands=commands,
        archive_enabled=archive_enabled,
        archive_events=archive_events,
    )


@app.get("/agent/archive/media/{asset_id}")
async def agent_archive_media(
    asset_id: int,
    session: AsyncSession = Depends(get_session),
    x_api_key: str | None = Header(default=None),
    x_xass_source: str | None = Header(default=None),
) -> StreamingResponse:
    auth = await authenticate_agent_api_key(
        session,
        api_key=x_api_key,
        global_agent_api_key=settings.agent_api_key,
    )
    if auth is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid agent key")
    requested_source = str(x_xass_source or "").strip()
    if auth.source_name and requested_source and requested_source != auth.source_name:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Agent source mismatch")
    source_name = str(auth.source_name or requested_source).strip()
    if not source_name or not await is_archive_target(session, source_name):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Archive is disabled for this agent")
    asset = await session.get(MediaAsset, asset_id)
    if asset is None or not asset.archive_allowed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media is not enabled for local archive")
    return await _telegram_media_response(asset, session)


async def _telegram_media_response(
    asset: MediaAsset | None,
    session: AsyncSession,
    *,
    inline: bool = False,
) -> StreamingResponse:
    if asset is None or not asset.file_id or not bot_client:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media is unavailable")
    try:
        telegram_file = await bot_client.get_file(asset.file_id)
    except Exception as exc:
        logger.warning("Could not refresh Telegram media path for asset %s: %s", asset.id, exc)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media is unavailable") from exc
    file_path = str(telegram_file.get("file_path") or "").strip()
    if not file_path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media is unavailable")
    asset.telegram_file_path = file_path
    await session.commit()
    file_url = f"{bot_client.file_url}/{file_path.lstrip('/')}"
    try:
        upstream = await bot_client.client.send(
            bot_client.client.build_request("GET", file_url),
            stream=True,
        )
        upstream.raise_for_status()
    except Exception as exc:
        logger.warning("Could not open Telegram media stream for asset %s: %s", asset.id, exc)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Telegram media stream is unavailable") from exc

    async def stream() -> Any:
        try:
            async for chunk in upstream.aiter_bytes(1024 * 256):
                yield chunk
        finally:
            await upstream.aclose()

    media_type = asset.mime_type or "application/octet-stream"
    headers = {"Cache-Control": "private, no-store"}
    if inline:
        headers["Content-Disposition"] = f'inline; filename="xass-media-{asset.id}"'
    return StreamingResponse(stream(), media_type=media_type, headers=headers)


async def _agent_update_package_response(
    revision: str,
    session: AsyncSession,
    x_api_key: str | None,
) -> FileResponse:
    auth = await authenticate_agent_api_key(
        session,
        api_key=x_api_key,
        global_agent_api_key=settings.agent_api_key,
    )
    if auth is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid agent key")
    package = await asyncio.to_thread(build_agent_package, settings)
    if revision and revision != package.revision:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Agent package changed; refresh manifest")
    return FileResponse(
        package.path,
        media_type="application/zip",
        filename=f"xass-pc-{package.version}-{package.revision[:12]}.zip",
        headers={
            "ETag": f'"{package.sha256}"',
            "X-XASS-Revision": package.revision,
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )


@app.get("/agent/update/package/{revision}.zip")
async def agent_update_package_by_revision(
    revision: str,
    session: AsyncSession = Depends(get_session),
    x_api_key: str | None = Header(default=None),
) -> FileResponse:
    return await _agent_update_package_response(revision, session, x_api_key)


@app.get("/agent/update/package")
async def agent_update_package(
    revision: str = "",
    session: AsyncSession = Depends(get_session),
    x_api_key: str | None = Header(default=None),
) -> FileResponse:
    # Compatibility for clients that received a pre-0.4.3 manifest.
    return await _agent_update_package_response(revision, session, x_api_key)


def _agent_installer_response() -> FileResponse:
    installer = get_agent_installer(settings)
    if installer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Windows installer is not available yet")
    return FileResponse(
        installer.path,
        media_type="application/vnd.microsoft.portable-executable",
        filename=f"XASS-Setup-{installer.version}.exe",
        headers={
            "ETag": f'"{installer.sha256}"',
            "X-XASS-Version": installer.version,
            "X-XASS-Revision": installer.revision,
            "Cache-Control": "private, no-store",
        },
    )


@app.get("/agent/installer/{revision}.exe")
async def agent_installer_download(
    revision: str,
    session: AsyncSession = Depends(get_session),
    x_api_key: str | None = Header(default=None),
) -> FileResponse:
    auth = await authenticate_agent_api_key(
        session,
        api_key=x_api_key,
        global_agent_api_key=settings.agent_api_key,
    )
    if auth is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid agent key")
    installer = get_agent_installer(settings)
    if installer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Windows installer is not available yet")
    if revision != installer.revision:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Installer changed; refresh manifest")
    return _agent_installer_response()


@app.post("/profile/now-playing/external")
async def profile_now_playing_external(
    payload: ExternalNowPlayingPayload,
    x_api_key: str | None = Header(default=None),
) -> dict[str, Any]:
    profile = load_profile(Path(settings.profile_json_path))
    profile_key = str(profile.get("iphone_hook_key") or "").strip()
    env_key = (settings.iphone_now_playing_api_key or "").strip()
    incoming = (x_api_key or "").strip()
    accepted = {key for key in (profile_key, env_key) if key}
    if not accepted:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="iphone now playing key is not configured")
    if incoming not in accepted:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid iphone now playing key")
    resolved_text = _resolve_external_now_playing_text(payload)
    if not resolved_text:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Empty now playing payload. Send JSON with text, or artist+title.",
        )
    updated = update_profile_now_playing_external(settings, resolved_text, source=payload.source)
    return {
        "ok": True,
        "updated": updated,
        "source": payload.source,
        "text": resolved_text,
    }


@app.post("/api/vk/save-token")
async def vk_save_token(payload: VkSaveTokenPayload) -> dict[str, Any]:
    if not settings.setup_api_key or (payload.secret or "").strip() != settings.setup_api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid secret")
    token = (payload.access_token or "").strip()
    if len(token) < 20:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="access_token is too short")
    if payload.user_id <= 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="user_id must be positive")

    profile_path = Path(settings.profile_json_path)
    ensure_profile_exists(profile_path)
    profile = load_profile(profile_path)
    profile["vk_access_token"] = token
    profile["vk_user_id"] = payload.user_id
    profile["now_listening_source"] = "vk"
    profile["vk_connected_at"] = datetime.now(timezone.utc).isoformat()
    save_profile(profile_path, profile)

    if payload.chat_id and bot_client:
        try:
            await bot_client.send_message(
                int(payload.chat_id),
                (
                    "✅ ВКонтакте подключён!\n\n"
                    "Музыка из статуса ВК теперь будет обновляться автоматически.\n"
                    "Источник «сейчас слушаю» переключён на VK."
                ),
            )
        except Exception:
            logger.warning("Failed to deliver VK connect confirmation to chat_id=%s", payload.chat_id)

    return {"ok": True}


# ─────────────────────────── Telegram Mini App API ───────────────────────────


async def require_mini_user(
    request: Request,
    x_telegram_init_data: str | None = Header(default=None),
) -> MiniAppUser:
    user = miniapp_authenticate(x_telegram_init_data or "", settings)
    if user is None:
        user = pwa_authenticate_session(request.cookies.get(PWA_COOKIE_NAME, ""), settings)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Telegram authentication")
    return user


async def require_mini_owner(
    request: Request,
    x_telegram_init_data: str | None = Header(default=None),
) -> MiniAppUser:
    user = await require_mini_user(request, x_telegram_init_data)
    if not user.is_owner:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Owner only")
    return user


def _public_origin(request: Request) -> tuple[str, str]:
    forwarded_host = str(request.headers.get("x-forwarded-host") or request.url.hostname or "").split(",", 1)[0].strip()
    host = forwarded_host.split(":", 1)[0]
    forwarded_proto = str(request.headers.get("x-forwarded-proto") or request.url.scheme or "https").split(",", 1)[0].strip().lower()
    origin = f"{forwarded_proto}://{forwarded_host}"
    return host, origin


@app.get("/api/pwa/config")
async def pwa_config(request: Request) -> dict[str, Any]:
    session_user = pwa_authenticate_session(request.cookies.get(PWA_COOKIE_NAME, ""), settings)
    identity_cache = Path(getattr(settings, "telegram_bot_identity_cache_path", "./data/telegram_bot_identity.json"))
    bot_username = normalize_bot_username(getattr(settings, "telegram_bot_username", ""))
    if not bot_username:
        bot_username = load_cached_bot_username(identity_cache)
        if bot_username:
            settings.telegram_bot_username = bot_username
    if not bot_username and bot_client is not None and hasattr(bot_client, "get_me"):
        try:
            bot_identity = await asyncio.wait_for(bot_client.get_me(), timeout=3.0)
            bot_username = normalize_bot_username(bot_identity.get("username"))
            if bot_username:
                settings.telegram_bot_username = bot_username
                save_cached_bot_username(identity_cache, bot_username)
        except Exception:
            logger.warning("Could not resolve bot username for PWA login")
    forwarded_host = str(request.headers.get("x-forwarded-host") or request.url.hostname or "").strip().lower()
    public_host = forwarded_host.split(",", maxsplit=1)[0].split(":", maxsplit=1)[0]
    forwarded_proto = str(request.headers.get("x-forwarded-proto") or request.url.scheme or "").split(",", maxsplit=1)[0].strip().lower()
    requirements = {
        "bot_token": bool(settings.bot_token),
        "bot_username": bool(bot_username),
        "owner_user_id": bool(settings.owner_user_id),
        "https": forwarded_proto == "https" or public_host in {"localhost", "127.0.0.1"},
    }
    login_ready = all(requirements.values())
    async with SessionLocal() as passkey_session:
        passkey_count = await passkey_count_credentials(passkey_session, settings.owner_user_id) if settings.owner_user_id else 0
    return {
        "ok": True,
        "authenticated": bool(session_user),
        "bot_username": bot_username,
        "login_ready": login_ready,
        "domain": public_host,
        "domain_verification": "telegram_only",
        "requirements": requirements,
        "passkey_available": bool(passkey_count),
        "passkey_count": passkey_count,
    }


@app.post("/api/pwa/login")
async def pwa_login(payload: PwaTelegramLoginPayload, response: Response) -> dict[str, Any]:
    user = pwa_authenticate_login(payload.model_dump(exclude_unset=True), settings)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Telegram login is invalid or this account is not the owner")
    response.set_cookie(
        PWA_COOKIE_NAME,
        issue_pwa_session(user, settings),
        max_age=PWA_SESSION_AGE_SEC,
        httponly=True,
        secure=settings.pwa_cookie_secure,
        samesite="lax",
        path="/",
    )
    return {"ok": True, "user": {"id": user.user_id, "first_name": user.first_name, "username": user.username}}


@app.post("/api/pwa/exchange")
async def pwa_exchange(
    payload: PwaPairExchangePayload,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        user_id = await consume_pwa_pair_token(session, payload.token)
    except PwaPairingError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    if not settings.owner_user_id or user_id != settings.owner_user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Ссылка выпущена не для текущего владельца")

    user = MiniAppUser(
        user_id=user_id,
        first_name="Владелец",
        last_name="",
        username="",
        is_owner=True,
    )
    response.set_cookie(
        PWA_COOKIE_NAME,
        issue_pwa_session(user, settings),
        max_age=PWA_SESSION_AGE_SEC,
        httponly=True,
        secure=settings.pwa_cookie_secure,
        samesite="lax",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return {"ok": True}


@app.post("/api/pwa/logout")
async def pwa_logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(PWA_COOKIE_NAME, path="/", secure=settings.pwa_cookie_secure, samesite="lax")
    return {"ok": True}


@app.post("/api/pwa/passkeys/login/options")
async def pwa_passkey_login_options(
    request: Request,
    payload: PasskeyStartPayload,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    rp_id, origin = _public_origin(request)
    try:
        result = await passkey_authentication_options(
            session,
            owner_user_id=settings.owner_user_id,
            rp_id=rp_id,
            origin=origin,
            purpose=(payload.purpose or "login").strip() or "login",
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {"ok": True, **result}


@app.post("/api/pwa/passkeys/login/verify")
async def pwa_passkey_login_verify(
    payload: PasskeyCompletePayload,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        credential, purpose = await passkey_complete_authentication(
            session,
            transaction=payload.transaction,
            credential=payload.credential,
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    user = MiniAppUser(
        user_id=credential.owner_user_id,
        first_name="Владелец",
        last_name="",
        username="",
        is_owner=True,
    )
    if purpose == "login":
        response.set_cookie(
            PWA_COOKIE_NAME,
            issue_pwa_session(user, settings),
            max_age=PWA_SESSION_AGE_SEC,
            httponly=True,
            secure=settings.pwa_cookie_secure,
            samesite="lax",
            path="/",
        )
    response.headers["Cache-Control"] = "no-store"
    return {
        "ok": True,
        "purpose": purpose,
        "action_proof": issue_action_proof(user.user_id, purpose, settings) if purpose != "login" else "",
    }


@app.post("/api/pwa/passkeys/register/options")
async def pwa_passkey_register_options(
    request: Request,
    user: MiniAppUser = Depends(require_mini_owner),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    rp_id, origin = _public_origin(request)
    try:
        result = await passkey_registration_options(
            session,
            owner_user_id=user.user_id,
            owner_name=user.first_name or user.username or "Владелец XASS",
            rp_id=rp_id,
            origin=origin,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return {"ok": True, **result}


@app.post("/api/pwa/passkeys/register/verify")
async def pwa_passkey_register_verify(
    payload: PasskeyCompletePayload,
    user: MiniAppUser = Depends(require_mini_owner),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        transaction_owner = passkey_transaction_owner(payload.transaction, "register")
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if transaction_owner != user.user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Owner mismatch")
    try:
        credential = await passkey_complete_registration(
            session,
            transaction=payload.transaction,
            credential=payload.credential,
            name=payload.name,
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {"ok": True, "credential": {"id": credential.id, "name": credential.name}}


def _now_source_label(value: str) -> str:
    return {"pc_agent": "PC", "iphone": "iPhone", "vk": "VK"}.get(value, value or "pc_agent")


def _build_mini_status(profile: dict[str, Any]) -> dict[str, Any]:
    now_text = str(profile.get("now_listening_text") or "").strip()
    weather_text = str(profile.get("weather_text") or "").strip()
    source = str(profile.get("now_listening_source") or settings.now_playing_source_default or "pc_agent").strip().lower()
    vk_uid = profile.get("vk_user_id")
    vk_connected = bool(str(profile.get("vk_access_token") or "").strip())
    discord_active = bool(profile.get("discord_active"))
    discord_updated_raw = str(profile.get("discord_updated_at") or "")
    discord_fresh = False
    if discord_updated_raw:
        try:
            from datetime import timezone as _tz
            ts = datetime.fromisoformat(discord_updated_raw)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=_tz.utc)
            discord_fresh = (datetime.now(_tz.utc) - ts).total_seconds() < 300
        except Exception:
            pass
    return {
        "name": str(profile.get("name") or ""),
        "title": str(profile.get("title") or ""),
        "avatar_url": str(profile.get("avatar_url") or ""),
        "now_listening": now_text,
        "weather": weather_text,
        "now_source": source,
        "now_source_label": _now_source_label(source),
        "discord_active": discord_active,
        "discord_fresh": discord_fresh,
        "discord_game": profile.get("discord_game"),
        "discord_elapsed_sec": profile.get("discord_elapsed_sec"),
        "discord_tag": str(profile.get("discord_tag") or ""),
        "vk_connected": vk_connected,
        "vk_user_id": vk_uid if vk_connected else None,
        "vk_connected_at": str(profile.get("vk_connected_at") or ""),
    }


@app.get("/api/mini/bootstrap")
async def mini_bootstrap(
    user: MiniAppUser = Depends(require_mini_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    config = await get_or_create_app_config(session, settings)
    profile = load_profile(Path(settings.profile_json_path))
    sources = await list_sources(session)
    latest_commands = await latest_agent_commands(session, [item.source_name for item in sources])
    archive_targets = await archive_target_map(session)
    archive = await archive_summary(session)
    passkeys = await passkey_list_credentials(session, user.user_id) if user.is_owner else []
    metrics = collect_server_metrics(top_processes_limit=settings.top_processes_limit)
    services = collect_systemd_statuses(settings.monitored_services)
    quotes = load_quotes(Path(settings.quotes_json_path))

    return {
        "ok": True,
        "app_version": APP_VERSION,
        "user": {
            "id": user.user_id,
            "first_name": user.first_name,
            "username": user.username,
            "is_owner": user.is_owner,
        },
        "status": _build_mini_status(profile),
        "settings": {
            "save_mode": config.save_mode,
            "timeout_minutes": config.heartbeat_timeout_minutes,
            "quiet_enabled": bool(config.quiet_hours_enabled),
            "quiet_start": config.quiet_hours_start_minute,
            "quiet_end": config.quiet_hours_end_minute,
            "away_enabled": bool(config.away_mode_enabled),
            "away_until_at": config.away_until_at.isoformat() if config.away_until_at else None,
            "away_message": config.away_mode_message or "",
            "away_schedule_enabled": bool(config.away_schedule_enabled),
            "away_schedule_start": config.away_schedule_start_minute,
            "away_schedule_end": config.away_schedule_end_minute,
            "service_base_url": config.service_base_url or "",
        },
        "sources": [
            {
                "id": item.id,
                "source_name": item.source_name,
                "source_type": item.source_type,
                "is_online": item.is_online,
                "last_seen_at": item.last_seen_at.isoformat() if item.last_seen_at else None,
                "last_payload": item.last_payload,
                "agent_version": str((item.last_payload or {}).get("agent_version") or "0.0.0"),
                "agent_revision": str((item.last_payload or {}).get("agent_revision") or ""),
                "latest_command": (
                    {
                        "id": latest_commands[item.source_name].id,
                        "command": latest_commands[item.source_name].command,
                        "status": latest_commands[item.source_name].status,
                        "created_at": latest_commands[item.source_name].created_at.isoformat(),
                        "result": latest_commands[item.source_name].result or {},
                    }
                    if item.source_name in latest_commands
                    else None
                ),
                "archive_enabled": bool(archive_targets.get(item.source_name) and archive_targets[item.source_name].enabled),
            }
            for item in sources
        ],
        "metrics": metrics,
        "services": services,
        "windows_installer": installer_public_info(settings),
        "quotes_count": len(quotes),
        "archive": archive,
        "passkeys": [
            {
                "id": item.id,
                "name": item.name,
                "created_at": item.created_at.isoformat(),
                "last_used_at": item.last_used_at.isoformat() if item.last_used_at else None,
                "backed_up": bool(item.backed_up),
            }
            for item in passkeys
        ],
        "vk_app_id": settings.vk_app_id or (int(str(profile.get("vk_app_id") or "").strip()) if str(profile.get("vk_app_id") or "").strip().isdigit() else None),
    }


def _mini_public_profile(profile: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "name",
        "title",
        "bio",
        "username",
        "telegram_url",
        "avatar_url",
        "quote",
        "stack",
        "links",
        "now_listening_text",
        "weather_text",
    )
    return {key: profile.get(key) for key in fields}


def _normalize_saved_avatar_url(value: str) -> str:
    raw = (value or "").strip().replace("\\", "/")
    relative = raw.lstrip("/")
    if relative.startswith("data/avatars/") and ".." not in relative.split("/"):
        return "/" + relative
    return validate_http_url(raw, field_name="avatar_url")


def _avatar_extension(content_type: str, body: bytes) -> str | None:
    media_type = (content_type or "").split(";", maxsplit=1)[0].strip().lower()
    if media_type in {"image/jpeg", "image/jpg"} and body.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if media_type == "image/png" and body.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if media_type == "image/webp" and len(body) >= 12 and body.startswith(b"RIFF") and body[8:12] == b"WEBP":
        return "webp"
    return None


@app.get("/api/mini/site")
async def mini_site(
    user: MiniAppUser = Depends(require_mini_user),
) -> dict[str, Any]:
    profile = load_profile(Path(settings.profile_json_path))
    projects = load_projects(Path(settings.projects_json_path))
    return {
        "ok": True,
        "profile": _mini_public_profile(profile),
        "projects": projects,
        "public_url": (settings.profile_public_url or "").strip(),
        "can_edit": user.is_owner,
    }


@app.post("/api/mini/site/profile")
async def mini_site_profile_save(
    payload: MiniSiteProfilePayload,
    user: MiniAppUser = Depends(require_mini_owner),
) -> dict[str, Any]:
    profile_path = Path(settings.profile_json_path)
    profile = load_profile(profile_path)
    links: list[dict[str, str]] = []
    try:
        for item in payload.links:
            label = str(item.get("label") or "").strip()[:60]
            raw_url = str(item.get("url") or "").strip()
            if not label or not raw_url:
                continue
            links.append({"label": label, "url": validate_http_url(raw_url, field_name="url")})
        telegram_url = validate_http_url(payload.telegram_url, field_name="telegram_url")
        avatar_url = _normalize_saved_avatar_url(payload.avatar_url)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    profile.update(
        {
            "name": payload.name.strip(),
            "title": payload.title.strip(),
            "bio": payload.bio.strip(),
            "username": payload.username.strip().lstrip("@"),
            "telegram_url": telegram_url,
            "avatar_url": avatar_url,
            "quote": payload.quote.strip(),
            "stack": [str(item).strip()[:60] for item in payload.stack if str(item).strip()],
            "links": links,
        }
    )
    saved, _backup, changed = save_profile_with_backup(
        profile_path=profile_path,
        backup_dir=Path(settings.profile_backups_dir),
        audit_log_path=Path(settings.profile_audit_log_path),
        actor_user_id=user.user_id,
        action="miniapp_profile_save",
        profile_data=profile,
    )
    return {"ok": True, "profile": _mini_public_profile(saved), "changed_fields": changed}


@app.post("/api/mini/site/avatar")
async def mini_site_avatar_upload(
    request: Request,
    user: MiniAppUser = Depends(require_mini_owner),
) -> dict[str, Any]:
    body = await request.body()
    if not body:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Выберите изображение")
    if len(body) > 8 * 1024 * 1024:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Аватар должен быть меньше 8 МБ")
    extension = _avatar_extension(request.headers.get("content-type", ""), body)
    if extension is None:
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="Поддерживаются JPG, PNG и WebP")

    avatars_dir = Path(settings.profile_avatars_dir)
    avatars_dir.mkdir(parents=True, exist_ok=True)
    file_name = f"avatar-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}.{extension}"
    destination = avatars_dir / file_name
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_bytes(body)
    temporary.replace(destination)

    resolved_root = Path.cwd().resolve()
    try:
        avatar_url = "/" + destination.resolve().relative_to(resolved_root).as_posix()
    except ValueError:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Каталог аватаров должен находиться внутри XASS")

    profile_path = Path(settings.profile_json_path)
    profile = load_profile(profile_path)
    profile["avatar_url"] = avatar_url
    saved, _backup, changed = save_profile_with_backup(
        profile_path=profile_path,
        backup_dir=Path(settings.profile_backups_dir),
        audit_log_path=Path(settings.profile_audit_log_path),
        actor_user_id=user.user_id,
        action="miniapp_avatar_upload",
        profile_data=profile,
        payload={"file_name": file_name, "size": len(body)},
    )
    return {"ok": True, "avatar_url": avatar_url, "profile": _mini_public_profile(saved), "changed_fields": changed}


@app.post("/api/mini/site/projects")
async def mini_site_project_save(
    payload: MiniSiteProjectPayload,
    user: MiniAppUser = Depends(require_mini_owner),
) -> dict[str, Any]:
    if not payload.title.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Название проекта обязательно")
    projects_path = Path(settings.projects_json_path)
    projects = load_projects(projects_path)
    project_id = payload.id.strip()
    existing_ids = {str(item.get("id") or "") for item in projects}
    if not project_id:
        project_id = create_project_id(payload.title, existing_ids)
    project = normalize_project(
        {
            "id": project_id,
            "title": payload.title,
            "subtitle": payload.subtitle,
            "description": payload.description,
            "url": payload.url,
            "status": payload.status,
            "years": {"from": payload.year_from, "to": payload.year_to},
            "tags": payload.tags,
            "featured": payload.featured,
            "cover": {"type": payload.cover_type, "src": payload.cover_src},
            "sort": next(
                (int(item.get("sort") or 100) for item in projects if str(item.get("id")) == project_id),
                100 + len(projects) * 10,
            ),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        fallback_id=project_id,
    )
    replaced = False
    for index, item in enumerate(projects):
        if str(item.get("id")) == project_id:
            projects[index] = project
            replaced = True
            break
    if not replaced:
        projects.append(project)
    if project["featured"]:
        for item in projects:
            item["featured"] = str(item.get("id")) == project_id

    backup = backup_json_file(projects_path, Path(settings.projects_backups_dir), "projects")
    saved = save_projects(projects_path, projects)
    append_projects_audit_log(
        Path(settings.projects_audit_log_path),
        user.user_id,
        "miniapp_project_save",
        {"project_id": project_id, "created": not replaced, "backup_path": str(backup) if backup else None},
    )
    return {"ok": True, "project": next(item for item in saved if item["id"] == project_id), "projects": saved}


@app.delete("/api/mini/site/projects/{project_id}")
async def mini_site_project_delete(
    project_id: str,
    user: MiniAppUser = Depends(require_mini_owner),
) -> dict[str, Any]:
    projects_path = Path(settings.projects_json_path)
    projects = load_projects(projects_path)
    remaining = [item for item in projects if str(item.get("id")) != project_id]
    if len(remaining) == len(projects):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Проект не найден")
    if remaining and not any(bool(item.get("featured")) for item in remaining):
        remaining[0]["featured"] = True
    backup = backup_json_file(projects_path, Path(settings.projects_backups_dir), "projects")
    saved = save_projects(projects_path, remaining)
    append_projects_audit_log(
        Path(settings.projects_audit_log_path),
        user.user_id,
        "miniapp_project_delete",
        {"project_id": project_id, "backup_path": str(backup) if backup else None},
    )
    return {"ok": True, "projects": saved}


@app.post("/api/mini/agents/pair-code")
async def mini_agent_pair_code(
    request: Request,
    payload: MiniAgentPairPayload | None = None,
    user: MiniAppUser = Depends(require_mini_owner),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    result = await issue_pair_code(
        session,
        actor_user_id=user.user_id,
        ttl_minutes=settings.agent_pair_code_ttl_minutes,
        code_length=settings.agent_pair_code_length,
    )
    config = await get_or_create_app_config(session, settings)
    requested_server = (payload.server_url if payload else "").strip()
    if requested_server and normalize_server_origin(requested_server) is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid server URL")

    forwarded_origin = ""
    forwarded_host = (request.headers.get("x-forwarded-host") or "").strip()
    forwarded_proto = (request.headers.get("x-forwarded-proto") or "https").split(",", 1)[0].strip()
    if forwarded_host:
        forwarded_origin = f"{forwarded_proto}://{forwarded_host}"
    server_url = next(
        (
            normalized
            for candidate in (
                requested_server,
                config.service_base_url or "",
                settings.profile_public_url or "",
                forwarded_origin,
                str(request.base_url),
            )
            if (normalized := normalize_server_origin(candidate)) is not None
        ),
        None,
    )
    if server_url is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Public server URL is not configured")
    connection = build_connection_profile(
        server_url=server_url,
        pair_code=result.code,
        expires_at=result.expires_at,
        source_name=payload.source_name if payload else "",
    )
    return {
        "ok": True,
        "code": result.code,
        "expires_at": result.expires_at.isoformat(),
        "ttl_minutes": result.ttl_minutes,
        "server_url": server_url,
        "connection": connection,
        "connection_file_name": "xass-connect.xass",
    }


@app.post("/api/mini/pwa/pair-link")
async def mini_pwa_pair_link(
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    payload: MiniPwaPairPayload | None = None,
    user: MiniAppUser = Depends(require_mini_owner),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    requested_url = (payload.public_url if payload else "").strip()
    forwarded_host = (request.headers.get("x-forwarded-host") or "").split(",", 1)[0].strip()
    forwarded_proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "https").split(",", 1)[0].strip()
    forwarded_origin = f"{forwarded_proto}://{forwarded_host}" if forwarded_host else ""
    public_url = next(
        (
            normalized
            for candidate in (requested_url, forwarded_origin, str(request.base_url))
            if (normalized := normalize_server_origin(candidate)) is not None
        ),
        None,
    )
    if public_url is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Укажите полный публичный адрес XASS")
    parsed = urlsplit(public_url)
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Для iPhone нужен HTTPS-адрес")

    config = await get_or_create_app_config(session, settings)
    if config.service_base_url != public_url:
        await set_service_base_url(session, config, public_url, user.user_id)

    result = await issue_pwa_pair_token(session, actor_user_id=user.user_id)
    link = f"{public_url}/miniapp.php?standalone=1#pair={quote(result.token, safe='')}"
    response.headers["Cache-Control"] = "no-store"

    menu_update_scheduled = bot_client is not None
    if menu_update_scheduled:
        background_tasks.add_task(_update_miniapp_menu_button, public_url)

    return {
        "ok": True,
        "public_url": public_url,
        "link": link,
        "expires_at": result.expires_at.isoformat(),
        "ttl_minutes": result.ttl_minutes,
        "menu_update_scheduled": menu_update_scheduled,
    }


@app.get("/api/mini/agent-installer")
async def mini_agent_installer_info(
    user: MiniAppUser = Depends(require_mini_owner),
) -> dict[str, object]:
    return {"ok": True, **installer_public_info(settings)}


@app.get("/api/mini/agent-installer/download")
async def mini_agent_installer_download(
    user: MiniAppUser = Depends(require_mini_owner),
) -> FileResponse:
    return _agent_installer_response()


@app.get("/api/mini/agent-installer/ticket")
async def mini_agent_installer_ticket(
    user: MiniAppUser = Depends(require_mini_owner),
) -> dict[str, object]:
    try:
        ticket = issue_installer_ticket(settings, user_id=user.user_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return {
        "ok": True,
        "expires_in": 180,
        "download_path": f"/api/agent-installer/download?ticket={quote(ticket, safe='')}",
    }


@app.get("/api/agent-installer/download")
async def agent_installer_ticket_download(ticket: str = "") -> FileResponse:
    if not verify_installer_ticket(settings, ticket):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Download link is invalid or expired")
    return _agent_installer_response()


@app.post("/api/mini/agents/{source_name}/commands")
async def mini_agent_command(
    source_name: str,
    payload: MiniAgentCommandPayload,
    x_telegram_init_data: str | None = Header(default=None),
    user: MiniAppUser = Depends(require_mini_owner),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    source = await session.scalar(select(HeartbeatSource).where(HeartbeatSource.source_name == source_name))
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    telegram_user = miniapp_authenticate(x_telegram_init_data or "", settings)
    if payload.command in {"lock", "restart"} and telegram_user is None:
        purpose = f"agent:{payload.command}:{source.source_name}"
        if await passkey_count_credentials(session, user.user_id) and not verify_action_proof(
            payload.action_proof,
            user.user_id,
            purpose,
            settings,
        ):
            raise HTTPException(status_code=status.HTTP_428_PRECONDITION_REQUIRED, detail="Подтвердите действие через Face ID / Passkey")
    try:
        item = await enqueue_agent_command(
            session,
            source_name=source.source_name,
            command=payload.command,
            payload=payload.payload,
            actor_user_id=user.user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {
        "ok": True,
        "command": {
            "id": item.id,
            "source_name": item.source_name,
            "command": item.command,
            "status": item.status,
            "created_at": item.created_at.isoformat(),
        },
    }


@app.post("/api/mini/agents/{source_name}/archive")
async def mini_agent_archive_target(
    source_name: str,
    payload: MiniArchiveTargetPayload,
    user: MiniAppUser = Depends(require_mini_owner),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        target = await set_archive_target(
            session,
            source_name=source_name,
            enabled=payload.enabled,
            actor_user_id=user.user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return {"ok": True, "source_name": target.source_name, "enabled": bool(target.enabled)}


@app.post("/api/mini/setting")
async def mini_setting(
    payload: MiniSettingPayload,
    user: MiniAppUser = Depends(require_mini_owner),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    config = await get_or_create_app_config(session, settings)
    key = (payload.key or "").strip()
    value = payload.value
    actor = user.user_id

    try:
        if key == "save_mode_cycle":
            config = await cycle_save_mode(session, config, actor)
        elif key == "save_mode":
            config = await set_save_mode(session, config, SaveMode(str(value)), actor)
        elif key == "timeout":
            minutes = max(1, min(int(value), 1440))
            config.heartbeat_timeout_minutes = minutes
            await session.commit()
            await session.refresh(config)
        elif key == "quiet_toggle":
            config = await toggle_quiet_hours(session, config, actor)
        elif key == "quiet_window":
            start_minute, end_minute = parse_time_range(str(value))
            config = await set_quiet_hours_window(session, config, start_minute=start_minute, end_minute=end_minute, actor_user_id=actor)
        elif key == "away_toggle":
            config = await toggle_away_mode(session, config, actor)
        elif key == "away_off":
            config = await set_away_mode(session, config, False, actor)
        elif key == "away_for":
            config = await set_away_for_minutes(session, config, minutes=int(value), actor_user_id=actor)
        elif key == "away_message":
            config = await set_away_message(session, config, str(value), actor)
        elif key == "away_schedule":
            enabled = bool(value.get("enabled")) if isinstance(value, dict) else False
            rng = str(value.get("range") or "") if isinstance(value, dict) else ""
            start_minute = end_minute = None
            if rng:
                start_minute, end_minute = parse_time_range(rng)
            config = await set_away_schedule(session, config, enabled=enabled, start_minute=start_minute, end_minute=end_minute, actor_user_id=actor)
        elif key == "now_source":
            source_aliases = {"pc": "pc_agent", "pc_agent": "pc_agent", "iphone": "iphone", "ios": "iphone", "vk": "vk"}
            target = source_aliases.get(str(value).strip().lower())
            if not target:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown source")
            set_profile_now_playing_source(settings, target)
            await sync_profile_now_playing_from_heartbeat(session, settings, config.heartbeat_timeout_minutes)
        elif key == "discord_tag":
            tag = str(value or "").strip()
            profile_path = Path(settings.profile_json_path)
            ensure_profile_exists(profile_path)
            p = load_profile(profile_path)
            p["discord_tag"] = tag
            save_profile(profile_path, p)
        elif key == "vk_app_id":
            raw = str(value or "").strip()
            if raw and not raw.isdigit():
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="VK App ID должен быть числом")
            profile_path = Path(settings.profile_json_path)
            ensure_profile_exists(profile_path)
            p = load_profile(profile_path)
            p["vk_app_id"] = raw
            save_profile(profile_path, p)
        else:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown setting key: {key}")
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    profile = load_profile(Path(settings.profile_json_path))
    return {
        "ok": True,
        "settings": {
            "save_mode": config.save_mode,
            "timeout_minutes": config.heartbeat_timeout_minutes,
            "quiet_enabled": bool(config.quiet_hours_enabled),
            "quiet_start": config.quiet_hours_start_minute,
            "quiet_end": config.quiet_hours_end_minute,
            "away_enabled": bool(config.away_mode_enabled),
            "away_until_at": config.away_until_at.isoformat() if config.away_until_at else None,
            "away_message": config.away_mode_message or "",
            "away_schedule_enabled": bool(config.away_schedule_enabled),
            "away_schedule_start": config.away_schedule_start_minute,
            "away_schedule_end": config.away_schedule_end_minute,
        },
        "status": _build_mini_status(profile),
    }


@app.get("/api/mini/logs")
async def mini_logs(
    user: MiniAppUser = Depends(require_mini_user),
    session: AsyncSession = Depends(get_session),
    limit: int = 30,
) -> dict[str, Any]:
    bounded = max(1, min(int(limit), 100))
    rows = list(await session.scalars(select(MessageLog).order_by(MessageLog.id.desc()).limit(bounded)))
    logs = [
        {
            "id": row.id,
            "chat_title": row.chat_title or "",
            "chat_type": row.chat_type,
            "from_username": row.from_username or "",
            "direction": row.direction,
            "text": (row.text_content or "")[:400],
            "deleted": bool(row.deleted),
            "edited": row.edited_at is not None,
            "date": (row.message_date or row.created_at).isoformat() if (row.message_date or row.created_at) else None,
        }
        for row in rows
    ]
    return {"ok": True, "logs": logs}


@app.get("/api/mini/conversations")
async def mini_conversations(
    user: MiniAppUser = Depends(require_mini_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    summary_rows = (
        await session.execute(
            select(
                MessageLog.chat_id,
                func.count(MessageLog.id),
                func.sum(case((MessageLog.deleted.is_(True), 1), else_=0)),
                func.max(MessageLog.id),
            )
            .group_by(MessageLog.chat_id)
            .order_by(func.max(MessageLog.id).desc())
        )
    ).all()
    latest_ids = [int(row[3]) for row in summary_rows if row[3] is not None]
    latest_rows = list(await session.scalars(select(MessageLog).where(MessageLog.id.in_(latest_ids)))) if latest_ids else []
    latest_by_id = {row.id: row for row in latest_rows}
    chats: list[dict[str, Any]] = []
    for source_chat_id, message_count, deleted_count, latest_id in summary_rows:
        latest = latest_by_id.get(int(latest_id))
        if latest is None:
            continue
        chats.append(
            {
                "chat_id": source_chat_id,
                "title": latest.chat_title or latest.from_username or str(source_chat_id),
                "chat_type": latest.chat_type,
                "count": int(message_count or 0),
                "deleted": int(deleted_count or 0),
                "last_text": (latest.text_content or "Медиа / сообщение без текста")[:160],
                "last_date": (latest.message_date or latest.created_at).isoformat(),
            }
        )
    return {"ok": True, "chats": chats, "archive": await archive_summary(session)}


@app.get("/api/mini/conversations/{chat_id}")
async def mini_conversation_messages(
    chat_id: int,
    user: MiniAppUser = Depends(require_mini_user),
    session: AsyncSession = Depends(get_session),
    limit: int = 100,
    before: int = 0,
    deleted_only: bool = False,
) -> dict[str, Any]:
    query = select(MessageLog).where(MessageLog.chat_id == chat_id)
    if before > 0:
        query = query.where(MessageLog.id < before)
    if deleted_only:
        query = query.where(MessageLog.deleted.is_(True))
    bounded = max(1, min(limit, 200))
    fetched = list(await session.scalars(query.order_by(MessageLog.id.desc()).limit(bounded + 1)))
    has_more = len(fetched) > bounded
    rows = fetched[:bounded]
    messages: list[dict[str, Any]] = []
    for row in reversed(rows):
        revisions = list(
            await session.scalars(
                select(MessageRevision).where(MessageRevision.message_id == row.id).order_by(MessageRevision.id.asc())
            )
        )
        assets = list(
            await session.scalars(
                select(MediaAsset).where(
                    MediaAsset.message_id == row.id,
                    MediaAsset.archive_allowed.is_(True),
                )
            )
        )
        messages.append(
            {
                "id": row.id,
                "telegram_message_id": row.telegram_message_id,
                "chat_id": row.chat_id,
                "chat_title": row.chat_title or "",
                "from_username": row.from_username or "",
                "direction": row.direction,
                "text": row.text_content or "",
                "deleted": bool(row.deleted),
                "deleted_at": row.deleted_at.isoformat() if row.deleted_at else None,
                "edited": row.edited_at is not None,
                "date": (row.message_date or row.created_at).isoformat(),
                "revisions": [
                    {"event": item.event_type, "text": item.text_content or "", "date": item.created_at.isoformat()}
                    for item in revisions
                ],
                "media": [
                    {"id": asset.id, "type": asset.media_type, "mime_type": asset.mime_type or "", "size": asset.file_size}
                    for asset in assets
                ],
            }
        )
    return {"ok": True, "chat_id": chat_id, "messages": messages, "has_more": has_more}


@app.get("/api/mini/media/{asset_id}")
async def mini_conversation_media(
    asset_id: int,
    user: MiniAppUser = Depends(require_mini_user),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    asset = await session.get(MediaAsset, asset_id)
    if asset is None or not asset.archive_allowed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media is not enabled in the current save mode")
    return await _telegram_media_response(asset, session, inline=True)


@app.get("/api/mini/music")
async def mini_music(
    q: str = "",
    user: MiniAppUser = Depends(require_mini_user),
) -> dict[str, Any]:
    query = (q or "").strip()
    if not query:
        profile = load_profile(Path(settings.profile_json_path))
        query = str(profile.get("now_listening_text") or "").strip()
    if not query:
        return {"ok": False, "detail": "Нет трека для поиска"}
    try:
        card = await asyncio.wait_for(build_music_card(query), timeout=3.0)
    except Exception:
        card = fallback_music_card(query)
    links = build_search_links(card)
    return {
        "ok": bool(card.query),
        "query": card.query,
        "artist": card.artist,
        "title": card.title,
        "album": card.album,
        "artwork_url": card.artwork_url,
        "album_url": card.album_url,
        "links": links,
    }


@app.get("/api/mini/quotes")
async def mini_quotes_list(user: MiniAppUser = Depends(require_mini_user)) -> dict[str, Any]:
    quotes = load_quotes(Path(settings.quotes_json_path))
    return {"ok": True, "quotes": quotes}


@app.post("/api/mini/quotes")
async def mini_quotes_add(
    payload: MiniQuotePayload,
    user: MiniAppUser = Depends(require_mini_owner),
) -> dict[str, Any]:
    entry = add_quote(Path(settings.quotes_json_path), payload.text)
    if entry is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Пустая цитата")
    quotes = load_quotes(Path(settings.quotes_json_path))
    return {"ok": True, "added": entry, "quotes": quotes}


@app.delete("/api/mini/quotes/{quote_id}")
async def mini_quotes_delete(
    quote_id: str,
    user: MiniAppUser = Depends(require_mini_owner),
) -> dict[str, Any]:
    removed = delete_quote(Path(settings.quotes_json_path), quote_id)
    if not removed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Цитата не найдена")
    quotes = load_quotes(Path(settings.quotes_json_path))
    return {"ok": True, "quotes": quotes}


@app.put("/api/mini/quotes/{quote_id}")
async def mini_quotes_update(
    quote_id: str,
    payload: MiniQuotePayload,
    user: MiniAppUser = Depends(require_mini_owner),
) -> dict[str, Any]:
    updated = update_quote(Path(settings.quotes_json_path), quote_id, payload.text)
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Цитата не найдена или текст пуст")
    return {"ok": True, "updated": updated, "quotes": load_quotes(Path(settings.quotes_json_path))}


@app.get("/api/mini/vk-url")
async def mini_vk_url(
    chat_id: int | None = None,
    user: MiniAppUser = Depends(require_mini_owner),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    profile = load_profile(Path(settings.profile_json_path))
    profile_app_id_raw = str(profile.get("vk_app_id") or "").strip()
    profile_app_id = int(profile_app_id_raw) if profile_app_id_raw.isdigit() else None
    resolved_app_id = settings.vk_app_id or profile_app_id
    if not resolved_app_id:
        return {"ok": False, "detail": "VK_APP_ID не задан — укажите его в карточке ВКонтакте в мини-апп"}
    config = await get_or_create_app_config(session, settings)
    base = (config.service_base_url or settings.profile_public_url or "").strip().rstrip("/")
    # service_base_url may point at /profile.php; reduce to scheme+host.
    if base:
        split = urlsplit(base)
        if split.scheme and split.netloc:
            base = f"{split.scheme}://{split.netloc}"
    if not base:
        base = "https://redvps.site"
    app_id = int(resolved_app_id)
    version = (settings.vk_api_version or "5.199").strip() or "5.199"
    secret = (settings.setup_api_key or "").strip()
    redirect_target = f"{base}/vk-auth.php?secret={quote(secret, safe='')}"
    if chat_id:
        redirect_target += f"&chat_id={int(chat_id)}"
    oauth_url = (
        "https://oauth.vk.com/authorize"
        f"?client_id={app_id}"
        "&display=mobile"
        f"&redirect_uri={quote(redirect_target, safe='')}"
        "&scope=status,offline"
        "&response_type=token"
        f"&v={version}"
    )
    return {"ok": True, "url": oauth_url}


@app.get("/api/mini/update-status")
async def mini_update_status(
    user: MiniAppUser = Depends(require_mini_owner),
) -> dict[str, Any]:
    upd = await asyncio.to_thread(get_update_status, settings, include_release_notes=False)
    def _commit_dict(c: Any) -> dict[str, Any] | None:
        if c is None:
            return None
        return {"short_hash": c.short_hash, "subject": c.subject, "author": c.author, "date": c.date_iso}
    return {
        "ok": True,
        "branch": upd.branch,
        "has_updates": upd.has_updates,
        "current": _commit_dict(upd.current),
        "remote": _commit_dict(upd.remote),
        "commits": [_commit_dict(c) for c in upd.commits],
        "errors": upd.errors,
    }


@app.post("/api/mini/run-update")
async def mini_run_update(
    background_tasks: BackgroundTasks,
    user: MiniAppUser = Depends(require_mini_owner),
) -> dict[str, Any]:
    result = await asyncio.to_thread(run_update, settings, execute_restart=False)
    update_applied = bool(
        result.ok
        and result.before is not None
        and result.after is not None
        and result.before.full_hash != result.after.full_hash
    )
    restart_scheduled = bool(update_applied and result.restart_required)
    if restart_scheduled:
        background_tasks.add_task(_restart_after_mini_request, user.user_id, "после обновления из Mini App")
    def _commit_dict(c: Any) -> dict[str, Any] | None:
        if c is None:
            return None
        return {"short_hash": c.short_hash, "subject": c.subject}
    return {
        "ok": result.ok,
        "branch": result.branch,
        "before": _commit_dict(result.before),
        "after": _commit_dict(result.after),
        "steps": result.steps,
        "restart_performed": result.restart_performed,
        "restart_scheduled": restart_scheduled,
        "error": result.error,
    }


@app.post("/api/mini/restart")
async def mini_restart(
    background_tasks: BackgroundTasks,
    user: MiniAppUser = Depends(require_mini_owner),
) -> dict[str, Any]:
    background_tasks.add_task(_restart_after_mini_request, user.user_id, "по команде из Mini App")
    return {"ok": True, "restart_scheduled": True}


@app.get("/server/metrics", dependencies=[Depends(require_setup_api_key)])
async def server_metrics() -> dict[str, Any]:
    metrics = collect_server_metrics(top_processes_limit=settings.top_processes_limit)
    services = collect_systemd_statuses(settings.monitored_services)
    return {"metrics": metrics, "services": services}


@app.get("/heartbeat/sources", dependencies=[Depends(require_setup_api_key)])
async def heartbeat_sources(session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    sources = await list_sources(session)
    return [
        {
            "source_name": item.source_name,
            "source_type": item.source_type,
            "is_online": item.is_online,
            "last_seen_at": item.last_seen_at,
            "last_payload": item.last_payload,
        }
        for item in sources
    ]


@app.post("/telegram/setup-webhook", dependencies=[Depends(require_setup_api_key)])
async def telegram_setup_webhook(payload: WebhookSetupPayload) -> dict[str, Any]:
    if not bot_client:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="BOT_TOKEN is not configured")

    webhook_url = f"{str(payload.public_base_url).rstrip('/')}/telegram/webhook/{settings.telegram_webhook_path}"
    result = await bot_client.set_webhook(webhook_url, settings.telegram_secret_token or None)
    return {"ok": True, "webhook_url": webhook_url, "result": result}


@app.post("/telegram/webhook/{secret_path}")
async def telegram_webhook(secret_path: str, request: Request) -> dict[str, bool]:
    if secret_path != settings.telegram_webhook_path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Webhook path not found")

    if settings.telegram_secret_token:
        header_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if header_secret != settings.telegram_secret_token:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Telegram secret token")

    update = await request.json()
    async with SessionLocal() as session:
        await update_handler.handle_update(session, update)
    return {"ok": True}
