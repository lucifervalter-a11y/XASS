import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

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
from app.enums import SaveMode
from app.models import MessageLog
from app.services.agent_pairing import authenticate_agent_api_key, claim_pair_code_and_issue_key
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
    toggle_away_mode,
    toggle_quiet_hours,
)
from app.services.heartbeat import is_quiet_hours, list_sources, process_heartbeat
from app.services.miniapp import MiniAppUser, authenticate as miniapp_authenticate
from app.services.monitoring import collect_server_metrics, collect_systemd_statuses
from app.services.music_card import build_music_card, build_search_links
from app.services.profile_editor import ensure_profile_exists, load_profile, save_profile
from app.services.quotes_store import add_quote, delete_quote, ensure_quotes_exists, load_quotes
from app.services.restart_notice import clear_restart_notice, get_restart_notice
from app.services.profile_runtime import set_profile_now_playing_source, sync_profile_now_playing_from_heartbeat, update_profile_discord, update_profile_now_playing_external
from app.services.projects_store import ensure_projects_exists, ensure_site_config_exists
from app.services.updater import get_update_status, run_update
from app.storage import ensure_data_dirs
from app.telegram_handler import TelegramUpdateHandler

from sqlalchemy import select

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

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
    text: str


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
        try:
            notice = get_restart_notice(settings)
            if isinstance(notice, dict):
                reason = str(notice.get("reason") or "перезапуск").strip() or "перезапуск"
                sent = False
                for chat_id in _restart_notice_chat_candidates(notice.get("chat_id")):
                    try:
                        await bot_client.send_message(
                            chat_id,
                            f"✅ Сервис успешно перезапущен ({reason}).",
                        )
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
            async with SessionLocal() as _s:
                _cfg = await get_or_create_app_config(_s, settings)
            _raw_base = (
                (getattr(_cfg, "service_base_url", None) or "").strip()
                or (settings.profile_public_url or "").strip()
            )
            if _raw_base:
                _split = urlsplit(_raw_base)
                if _split.scheme and _split.netloc:
                    _miniapp_url = f"{_split.scheme}://{_split.netloc}/miniapp.php"
                    await bot_client.set_chat_menu_button(
                        menu_button={"type": "web_app", "text": "XASS", "web_app": {"url": _miniapp_url}}
                    )
                    logger.info("Chat menu button set to %s", _miniapp_url)
        except Exception:
            logger.warning("Failed to set chat menu button", exc_info=True)
        try:
            await bot_client.set_my_commands([
                {"command": "start", "description": "Панель управления"},
                {"command": "webapp", "description": "Открыть мини-приложение XASS"},
                {"command": "status", "description": "Статус heartbeat-источников"},
                {"command": "server", "description": "Метрики сервера"},
                {"command": "pc", "description": "Состояние ПК-агентов"},
                {"command": "update", "description": "Обновление бота и сервиса"},
                {"command": "help", "description": "Все команды (.muz, .weather…)"},
            ])
            logger.info("Bot commands registered")
        except Exception:
            logger.warning("Failed to set bot commands", exc_info=True)
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


@app.get("/api/mini/ping")
async def mini_ping() -> dict[str, Any]:
    import sys
    return {"ok": True, "python": sys.version, "status": "backend running"}


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
    x_telegram_init_data: str | None = Header(default=None),
) -> MiniAppUser:
    user = miniapp_authenticate(x_telegram_init_data or "", settings)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Telegram authentication")
    return user


async def require_mini_owner(
    x_telegram_init_data: str | None = Header(default=None),
) -> MiniAppUser:
    user = await require_mini_user(x_telegram_init_data)
    if not user.is_owner:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Owner only")
    return user


def _now_source_label(value: str) -> str:
    return {"pc_agent": "PC", "iphone": "iPhone", "vk": "VK"}.get(value, value or "pc_agent")


def _build_mini_status(profile: dict[str, Any]) -> dict[str, Any]:
    now_text = str(profile.get("now_listening_text") or "").strip()
    weather_text = str(profile.get("weather_text") or "").strip()
    source = str(profile.get("now_listening_source") or settings.now_playing_source_default or "pc_agent").strip().lower()
    vk_uid = profile.get("vk_user_id")
    vk_connected = bool(str(profile.get("vk_access_token") or "").strip())
    return {
        "name": str(profile.get("name") or ""),
        "title": str(profile.get("title") or ""),
        "avatar_url": str(profile.get("avatar_url") or ""),
        "now_listening": now_text,
        "weather": weather_text,
        "now_source": source,
        "now_source_label": _now_source_label(source),
        "discord_active": bool(profile.get("discord_active")),
        "discord_game": profile.get("discord_game"),
        "discord_elapsed_sec": profile.get("discord_elapsed_sec"),
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
    metrics = collect_server_metrics(top_processes_limit=settings.top_processes_limit)
    services = collect_systemd_statuses(settings.monitored_services)
    quotes = load_quotes(Path(settings.quotes_json_path))

    return {
        "ok": True,
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
        },
        "sources": [
            {
                "source_name": item.source_name,
                "source_type": item.source_type,
                "is_online": item.is_online,
                "last_seen_at": item.last_seen_at.isoformat() if item.last_seen_at else None,
                "last_payload": item.last_payload,
            }
            for item in sources
        ],
        "metrics": metrics,
        "services": services,
        "quotes_count": len(quotes),
        "vk_app_id": settings.vk_app_id,
    }


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
    card = await build_music_card(query)
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


@app.get("/api/mini/vk-url")
async def mini_vk_url(
    chat_id: int | None = None,
    user: MiniAppUser = Depends(require_mini_owner),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if not settings.vk_app_id:
        return {"ok": False, "detail": "VK_APP_ID не задан в .env"}
    config = await get_or_create_app_config(session, settings)
    base = (config.service_base_url or settings.profile_public_url or "").strip().rstrip("/")
    # service_base_url may point at /profile.php; reduce to scheme+host.
    if base:
        split = urlsplit(base)
        if split.scheme and split.netloc:
            base = f"{split.scheme}://{split.netloc}"
    if not base:
        base = "https://redvps.site"
    app_id = int(settings.vk_app_id)
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
    upd = await asyncio.to_thread(get_update_status, settings)
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
    user: MiniAppUser = Depends(require_mini_owner),
) -> dict[str, Any]:
    result = await asyncio.to_thread(run_update, settings)
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
        "error": result.error,
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
