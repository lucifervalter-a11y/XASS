import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel, HttpUrl
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
from app.services.agent_pairing import authenticate_agent_api_key, claim_pair_code_and_issue_key
from app.services.app_config import get_or_create_app_config
from app.services.heartbeat import is_quiet_hours, list_sources, process_heartbeat
from app.services.monitoring import collect_server_metrics, collect_systemd_statuses
from app.services.profile_editor import ensure_profile_exists, load_profile
from app.services.profile_runtime import sync_profile_now_playing_from_heartbeat, update_profile_now_playing_external
from app.services.projects_store import ensure_projects_exists, ensure_site_config_exists
from app.storage import ensure_data_dirs
from app.telegram_handler import TelegramUpdateHandler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

settings = get_settings()
bot_client = TelegramBotClient(settings.bot_token) if settings.bot_token else None
update_handler = TelegramUpdateHandler(settings, bot_client)


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


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_data_dirs()
    ensure_profile_exists(Path(settings.profile_json_path))
    ensure_projects_exists(Path(settings.projects_json_path))
    ensure_site_config_exists(Path(settings.site_config_json_path))
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
    version="0.3.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


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

    config = await get_or_create_app_config(session, settings)
    source, recovered, is_new = await process_heartbeat(session, payload)
    await sync_profile_now_playing_from_heartbeat(session, settings, config.heartbeat_timeout_minutes)

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

    return HeartbeatResponse(
        ok=True,
        source_name=source.source_name,
        recovered=recovered,
        new_source=is_new,
        server_time=datetime.now(timezone.utc),
    )


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
