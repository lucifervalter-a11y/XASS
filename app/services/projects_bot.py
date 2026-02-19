from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from app.bot_api import TelegramBotClient
from app.config import Settings
from app.services.projects_store import (
    PROJECT_STATUS_VALUES,
    append_audit_log as append_projects_audit_log,
    backup_json_file as backup_projects_json,
    create_project_id,
    ensure_projects_exists,
    ensure_site_config_exists,
    find_project,
    save_projects,
    save_site_config,
    set_featured,
)

logger = logging.getLogger(__name__)

SafeSendFn = Callable[..., Awaitable[None]]
SafeEditFn = Callable[..., Awaitable[None]]
PathToUrlFn = Callable[[Path], str]


class ProjectsBotService:
    def __init__(
        self,
        *,
        settings: Settings,
        bot_client: TelegramBotClient | None,
        safe_send: SafeSendFn,
        safe_edit_or_send: SafeEditFn,
        path_to_url: PathToUrlFn,
    ):
        self.settings = settings
        self.bot_client = bot_client
        self.safe_send = safe_send
        self.safe_edit_or_send = safe_edit_or_send
        self.path_to_url = path_to_url
        self.dialogs: dict[int, dict[str, Any]] = {}
        self.upload_ctx: dict[int, dict[str, Any]] = {}

    def _paths(self) -> tuple[Path, Path, Path, Path]:
        return (
            Path(self.settings.projects_json_path),
            Path(self.settings.site_config_json_path),
            Path(self.settings.projects_backups_dir),
            Path(self.settings.projects_audit_log_path),
        )

    def _assets_dirs(self) -> tuple[Path, Path]:
        return (Path(self.settings.projects_assets_dir), Path(self.settings.backgrounds_assets_dir))

    def _save_projects(self, projects: list[dict[str, Any]], user_id: int, action: str, payload: dict[str, Any]) -> Path | None:
        projects_path, _, backups_dir, audit_path = self._paths()
        backup = backup_projects_json(projects_path, backups_dir, "projects")
        save_projects(projects_path, projects)
        data = dict(payload)
        data["backup_path"] = str(backup) if backup else None
        append_projects_audit_log(audit_path, user_id, action, data)
        return backup

    def _save_site_config(self, config: dict[str, Any], user_id: int, action: str, payload: dict[str, Any]) -> Path | None:
        _, site_cfg_path, backups_dir, audit_path = self._paths()
        backup = backup_projects_json(site_cfg_path, backups_dir, "site_config")
        save_site_config(site_cfg_path, config)
        data = dict(payload)
        data["backup_path"] = str(backup) if backup else None
        append_projects_audit_log(audit_path, user_id, action, data)
        return backup

    def _project_text(self, item: dict[str, Any]) -> str:
        years = item.get("years") if isinstance(item.get("years"), dict) else {}
        tags = item.get("tags") if isinstance(item.get("tags"), list) else []
        cover = item.get("cover") if isinstance(item.get("cover"), dict) else {}
        subtitle = str(item.get("subtitle") or "").strip() or "-"
        description = str(item.get("description") or "").strip() or "-"
        if len(description) > 220:
            description = f"{description[:217]}..."
        return (
            f"Название: {item.get('title') or '-'}\n"
            f"ID: {item.get('id') or '-'}\n"
            f"Статус: {item.get('status') or '-'}\n"
            f"Годы: {years.get('from') or '-'}-{years.get('to') or '-'}\n"
            f"Подзаголовок: {subtitle}\n"
            f"Описание: {description}\n"
            f"Ссылка: {item.get('url') or 'нет'}\n"
            f"Теги: {', '.join(str(v) for v in tags) if tags else '-'}\n"
            f"Cover: {cover.get('type') or 'image'} | {cover.get('src') or '-'}\n"
            f"Обновлено: {item.get('updated_at') or '-'}"
        )

    def _parse_url(self, raw: str) -> str:
        text = (raw or "").strip()
        if text in {"", "-"}:
            return ""
        if text.startswith("http://") or text.startswith("https://"):
            return text
        raise ValueError("Ссылка должна начинаться с http:// или https://")

    def _parse_status(self, raw: str) -> str:
        status = (raw or "").strip().lower()
        if status not in PROJECT_STATUS_VALUES:
            raise ValueError("Статус: working/testing/dev/unstable/archived/stable")
        return status

    def _parse_years(self, raw: str) -> dict[str, int]:
        text = (raw or "").strip().replace("—", "-").replace("–", "-")
        if "-" in text:
            left, right = [x.strip() for x in text.split("-", maxsplit=1)]
            if not left.isdigit() or not right.isdigit():
                raise ValueError("Годы должны быть числами")
            y1, y2 = int(left), int(right)
        else:
            if not text.isdigit():
                raise ValueError("Год должен быть числом")
            y1 = int(text)
            y2 = y1
        if y1 < 1970 or y2 > 2100:
            raise ValueError("Годы должны быть в диапазоне 1970..2100")
        if y2 < y1:
            y2 = y1
        return {"from": y1, "to": y2}

    def _parse_tags(self, raw: str) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        source = (raw or "").strip()
        if not source:
            return []
        # Accept: "python, fastapi", "python fastapi", "python;fastapi"
        for part in re.split(r"[,\s;]+", source):
            tag = part.strip()
            if not tag:
                continue
            key = tag.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(tag)
        return out

    def _parse_cover(self, raw: str) -> dict[str, str]:
        text = (raw or "").strip()
        if text in {"", "-"}:
            return {"type": "image", "src": ""}
        lower = text.lower()
        if lower.startswith("image "):
            return {"type": "image", "src": self._parse_url(text[6:])}
        if lower.startswith("video "):
            return {"type": "video", "src": self._parse_url(text[6:])}
        return {"type": "image", "src": self._parse_url(text)}

    def _list_keyboard(self, projects: list[dict[str, Any]], page: int, pages: int, per_page: int = 6) -> dict[str, Any]:
        rows: list[list[dict[str, str]]] = []
        start = page * per_page
        end = min(len(projects), start + per_page)
        for item in projects[start:end]:
            title = str(item.get("title") or "-")
            if len(title) > 28:
                title = f"{title[:25]}..."
            rows.append([{"text": title, "callback_data": f"projects:view:{item.get('id')}"}])
        nav: list[dict[str, str]] = []
        if page > 0:
            nav.append({"text": "◀️ Пред", "callback_data": f"projects:list:{page - 1}"})
        if page < pages - 1:
            nav.append({"text": "След ▶️", "callback_data": f"projects:list:{page + 1}"})
        if nav:
            rows.append(nav)
        rows.append([{"text": "➕ Добавить проект", "callback_data": "projects:add"}, {"text": "🖼 Фон проектов", "callback_data": "projects:bg"}])
        rows.append([{"text": "⬅️ Назад", "callback_data": "panel:home"}])
        return {"inline_keyboard": rows}

    def _project_keyboard(self, project_id: str) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [{"text": "✏️ Редактировать", "callback_data": f"projects:edit:{project_id}"}, {"text": "⭐ Featured", "callback_data": f"projects:featured:{project_id}"}],
                [{"text": "🔎 Проверить URL", "callback_data": f"projects:check:{project_id}"}, {"text": "🗑 Удалить", "callback_data": f"projects:delask:{project_id}"}],
                [{"text": "⬆️ Выше", "callback_data": f"projects:up:{project_id}"}, {"text": "⬇️ Ниже", "callback_data": f"projects:down:{project_id}"}],
                [{"text": "🖼 Cover image", "callback_data": f"projects:coverimg:{project_id}"}, {"text": "🎬 Cover video", "callback_data": f"projects:covervid:{project_id}"}],
                [{"text": "⬅️ К списку", "callback_data": "projects:list:0"}],
            ]
        }

    async def show_panel(self, *, chat_id: int | None, message_id: int | None, page: int = 0) -> None:
        if chat_id is None:
            return
        projects_path, site_cfg_path, _, _ = self._paths()
        projects = ensure_projects_exists(projects_path)
        ensure_site_config_exists(site_cfg_path)
        total = len(projects)
        per_page = 6
        pages = max(1, (total + per_page - 1) // per_page)
        p = max(0, min(page, pages - 1))
        featured = next((x for x in projects if bool(x.get("featured"))), None)
        lines = ["Проекты", "-------", f"Страница: {p + 1}/{pages}", f"Всего: {total}", f"Featured: {(featured or {}).get('title') if isinstance(featured, dict) else '-'}", ""]
        start = p * per_page
        end = min(total, start + per_page)
        for idx, item in enumerate(projects[start:end], start=start + 1):
            years = item.get("years") if isinstance(item.get("years"), dict) else {}
            lines.append(f"{idx}. {item.get('title') or '-'} | {item.get('status') or '-'} | {years.get('from') or '-'}-{years.get('to') or '-'}")
        if not projects:
            lines.append("Список пуст. Нажмите «Добавить проект».")
        await self.safe_edit_or_send(chat_id, message_id, "\n".join(lines), self._list_keyboard(projects, p, pages))

    async def show_project(self, *, chat_id: int | None, message_id: int | None, project_id: str) -> None:
        if chat_id is None:
            return
        projects_path, _, _, _ = self._paths()
        projects = ensure_projects_exists(projects_path)
        item = find_project(projects, project_id)
        if item is None:
            await self.safe_edit_or_send(chat_id, message_id, "Проект не найден.", {"inline_keyboard": [[{"text": "⬅️ К списку", "callback_data": "projects:list:0"}]]})
            return
        await self.safe_edit_or_send(chat_id, message_id, self._project_text(item), self._project_keyboard(project_id))

    async def show_bg(self, *, chat_id: int | None, message_id: int | None) -> None:
        if chat_id is None:
            return
        _, site_cfg_path, _, _ = self._paths()
        cfg = ensure_site_config_exists(site_cfg_path)
        bg = cfg.get("projects_background") if isinstance(cfg.get("projects_background"), dict) else {}
        text = f"Фон проектов\n------------\nТип: {bg.get('type') or 'image'}\nsrc: {bg.get('src') or '-'}"
        kb = {"inline_keyboard": [[{"text": "🖼 Upload image", "callback_data": "projects:bgimg"}, {"text": "🎬 Upload video", "callback_data": "projects:bgvid"}], [{"text": "🔗 URL", "callback_data": "projects:bgurl"}, {"text": "🧹 Очистить", "callback_data": "projects:bgclear"}], [{"text": "⬅️ К проектам", "callback_data": "projects:list:0"}]]}
        await self.safe_edit_or_send(chat_id, message_id, text, kb)
    async def handle_callback(self, *, chat_id: int | None, message_id: int | None, user_id: int, data: str) -> bool:
        if chat_id is None:
            return False
        if data in {"projects:panel", "projects:list", "projects:list:0"}:
            await self.show_panel(chat_id=chat_id, message_id=message_id, page=0)
            return True
        if data.startswith("projects:list:"):
            try:
                page = int(data.split(":", maxsplit=2)[2])
            except ValueError:
                page = 0
            await self.show_panel(chat_id=chat_id, message_id=message_id, page=page)
            return True
        if data.startswith("projects:view:"):
            await self.show_project(chat_id=chat_id, message_id=message_id, project_id=data.split(":", maxsplit=2)[2])
            return True
        if data == "projects:add":
            self.dialogs[user_id] = {"chat_id": chat_id, "mode": "add", "step": "title", "draft": {}}
            await self.safe_send(chat_id, "Добавление проекта: шаг 1/8\nВведите название. Для отмены: /cancel")
            return True
        if data == "projects:addconfirm":
            await self._confirm_add(user_id=user_id, chat_id=chat_id)
            return True
        if data == "projects:addcancel":
            self.dialogs.pop(user_id, None)
            await self.show_panel(chat_id=chat_id, message_id=message_id, page=0)
            return True
        if data.startswith("projects:edit:"):
            project_id = data.split(":", maxsplit=2)[2]
            kb = {"inline_keyboard": [[{"text": "Название", "callback_data": f"projects:field:{project_id}:title"}, {"text": "Подзаголовок", "callback_data": f"projects:field:{project_id}:subtitle"}], [{"text": "Описание", "callback_data": f"projects:field:{project_id}:description"}, {"text": "Статус", "callback_data": f"projects:field:{project_id}:status"}], [{"text": "Годы", "callback_data": f"projects:field:{project_id}:years"}, {"text": "Теги", "callback_data": f"projects:field:{project_id}:tags"}], [{"text": "URL", "callback_data": f"projects:field:{project_id}:url"}, {"text": "Cover URL", "callback_data": f"projects:field:{project_id}:cover"}], [{"text": "⬅️ К проекту", "callback_data": f"projects:view:{project_id}"}]]}
            await self.safe_edit_or_send(chat_id, message_id, "Выберите поле для редактирования.", kb)
            return True
        if data.startswith("projects:field:"):
            parts = data.split(":", maxsplit=3)
            if len(parts) < 4:
                return True
            self.dialogs[user_id] = {"chat_id": chat_id, "mode": "edit", "step": "value", "project_id": parts[2], "field": parts[3]}
            await self.safe_send(chat_id, f"Введите новое значение для {parts[3]}. Для отмены: /cancel")
            return True
        if data == "projects:editconfirm":
            await self._confirm_edit(user_id=user_id, chat_id=chat_id)
            return True
        if data == "projects:editcancel":
            state = self.dialogs.get(user_id) or {}
            self.dialogs.pop(user_id, None)
            await self.show_project(chat_id=chat_id, message_id=message_id, project_id=str(state.get("project_id") or ""))
            return True
        if data.startswith("projects:delask:"):
            pid = data.split(":", maxsplit=2)[2]
            kb = {"inline_keyboard": [[{"text": "✅ Удалить", "callback_data": f"projects:delrun:{pid}"}, {"text": "✖️ Отмена", "callback_data": f"projects:view:{pid}"}]]}
            await self.safe_edit_or_send(chat_id, message_id, "Удалить проект?", kb)
            return True
        if data.startswith("projects:delrun:"):
            await self._delete_project(user_id=user_id, chat_id=chat_id, message_id=message_id, project_id=data.split(":", maxsplit=2)[2])
            return True
        if data.startswith("projects:featured:"):
            await self._set_featured(user_id=user_id, chat_id=chat_id, message_id=message_id, project_id=data.split(":", maxsplit=2)[2])
            return True
        if data.startswith("projects:up:") or data.startswith("projects:down:"):
            await self._move_project(user_id=user_id, chat_id=chat_id, message_id=message_id, project_id=data.split(":", maxsplit=2)[2], up=data.startswith("projects:up:"))
            return True
        if data.startswith("projects:check:"):
            await self._check_project(chat_id=chat_id, project_id=data.split(":", maxsplit=2)[2])
            return True
        if data.startswith("projects:coverimg:") or data.startswith("projects:covervid:"):
            self.upload_ctx[user_id] = {"chat_id": chat_id, "kind": "cover", "project_id": data.split(":", maxsplit=2)[2], "media_type": "image" if data.startswith("projects:coverimg:") else "video", "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10)}
            await self.safe_send(chat_id, "Отправьте файл для cover. Для отмены: /cancel")
            return True
        if data == "projects:bg":
            await self.show_bg(chat_id=chat_id, message_id=message_id)
            return True
        if data == "projects:bgclear":
            _, site_cfg_path, _, _ = self._paths()
            cfg = ensure_site_config_exists(site_cfg_path)
            cfg["projects_background"] = {"type": "image", "src": ""}
            self._save_site_config(cfg, user_id, "projects_bg_clear", {})
            await self.show_bg(chat_id=chat_id, message_id=message_id)
            return True
        if data == "projects:bgurl":
            self.dialogs[user_id] = {"chat_id": chat_id, "mode": "bgurl", "step": "value"}
            await self.safe_send(chat_id, "Введите URL фона (или '-'). Для отмены: /cancel")
            return True
        if data in {"projects:bgimg", "projects:bgvid"}:
            self.upload_ctx[user_id] = {"chat_id": chat_id, "kind": "bg", "media_type": "image" if data == "projects:bgimg" else "video", "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10)}
            await self.safe_send(chat_id, "Отправьте файл для фона проектов. Для отмены: /cancel")
            return True
        return False

    async def maybe_handle_dialog_input(self, message: dict[str, Any], *, user_id: int | None) -> bool:
        if user_id is None:
            return False
        state = self.dialogs.get(user_id)
        if not state:
            return False
        chat_id = (message.get("chat") or {}).get("id")
        text = (message.get("text") or "").strip()
        if chat_id is None or state.get("chat_id") != chat_id:
            return False
        if text.lower() in {"/cancel", "cancel", "отмена"}:
            self.dialogs.pop(user_id, None)
            await self.safe_send(chat_id, "Операция отменена.")
            return True
        try:
            mode = str(state.get("mode") or "")
            if mode == "add":
                return await self._add_step(user_id=user_id, chat_id=chat_id, text=text)
            if mode == "edit":
                return await self._edit_step(user_id=user_id, chat_id=chat_id, text=text)
            if mode == "bgurl":
                url = self._parse_url(text)
                _, site_cfg_path, _, _ = self._paths()
                cfg = ensure_site_config_exists(site_cfg_path)
                if not url:
                    cfg["projects_background"] = {"type": "image", "src": ""}
                else:
                    cfg["projects_background"] = {"type": "video" if url.lower().endswith((".mp4", ".webm", ".ogg")) else "image", "src": url}
                self._save_site_config(cfg, user_id, "projects_bg_url", {"src": url})
                self.dialogs.pop(user_id, None)
                await self.show_bg(chat_id=chat_id, message_id=None)
                return True
        except ValueError as exc:
            await self.safe_send(chat_id, f"Ошибка: {exc}")
            return True
        return False

    async def maybe_handle_upload(self, message: dict[str, Any], *, user_id: int | None) -> bool:
        if user_id is None or self.bot_client is None:
            return False
        ctx = self.upload_ctx.get(user_id)
        if not isinstance(ctx, dict):
            return False
        chat_id = (message.get("chat") or {}).get("id")
        if chat_id is None or ctx.get("chat_id") != chat_id:
            return False
        if datetime.now(timezone.utc) > ctx.get("expires_at", datetime.now(timezone.utc)):
            self.upload_ctx.pop(user_id, None)
            return False
        text = (message.get("text") or "").strip().lower()
        if text in {"/cancel", "cancel", "отмена"}:
            self.upload_ctx.pop(user_id, None)
            await self.safe_send(chat_id, "Загрузка отменена.")
            return True
        meta = self._extract_media(message, str(ctx.get("media_type") or "image"))
        if meta is None:
            await self.safe_send(chat_id, "Нужен файл нужного типа.")
            return True
        file_id, ext = meta
        try:
            file_meta = await self.bot_client.get_file(file_id)
            tg_path = str(file_meta.get("file_path") or "")
            if not tg_path:
                raise RuntimeError("file_path empty")
            assets_dir, bg_dir = self._assets_dirs()
            assets_dir.mkdir(parents=True, exist_ok=True)
            bg_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
            if ctx.get("kind") == "bg":
                local_path = bg_dir / f"projects_bg_{stamp}{ext}"
            else:
                local_path = assets_dir / f"{ctx.get('project_id')}_{stamp}{ext}"
            await self.bot_client.download_file(tg_path, local_path)
            rel = self.path_to_url(local_path)
            self.upload_ctx.pop(user_id, None)
            if ctx.get("kind") == "bg":
                _, site_cfg_path, _, _ = self._paths()
                cfg = ensure_site_config_exists(site_cfg_path)
                cfg["projects_background"] = {"type": str(ctx.get("media_type") or "image"), "src": rel}
                self._save_site_config(cfg, user_id, "projects_bg_upload", {"src": rel})
                await self.safe_send(chat_id, f"Фон обновлен: {rel}")
                return True
            await self._apply_cover_upload(user_id=user_id, chat_id=chat_id, project_id=str(ctx.get("project_id") or ""), media_type=str(ctx.get("media_type") or "image"), src=rel)
            return True
        except Exception:
            logger.exception("projects upload failed")
            await self.safe_send(chat_id, "Не удалось обработать файл.")
            return True
    async def _add_step(self, *, user_id: int, chat_id: int, text: str) -> bool:
        state = self.dialogs.get(user_id) or {}
        draft = state.get("draft")
        if not isinstance(draft, dict):
            self.dialogs.pop(user_id, None)
            return True
        step = str(state.get("step") or "title")
        if step == "title":
            draft["title"] = text
            state["step"] = "subtitle"
            await self.safe_send(chat_id, "Шаг 2/8: подзаголовок (или '-')")
            return True
        if step == "subtitle":
            draft["subtitle"] = "" if text == "-" else text
            state["step"] = "description"
            await self.safe_send(chat_id, "Шаг 3/8: описание")
            return True
        if step == "description":
            draft["description"] = text
            state["step"] = "status"
            await self.safe_send(chat_id, "Шаг 4/8: статус")
            return True
        if step == "status":
            draft["status"] = self._parse_status(text)
            state["step"] = "years"
            await self.safe_send(chat_id, "Шаг 5/8: годы (2025 или 2023-2025)")
            return True
        if step == "years":
            draft["years"] = self._parse_years(text)
            state["step"] = "tags"
            await self.safe_send(chat_id, "Шаг 6/8: теги через запятую")
            return True
        if step == "tags":
            draft["tags"] = self._parse_tags(text)
            state["step"] = "url"
            await self.safe_send(chat_id, "Шаг 7/8: URL (или '-')")
            return True
        if step == "url":
            draft["url"] = self._parse_url(text)
            state["step"] = "cover"
            await self.safe_send(chat_id, "Шаг 8/8: cover URL ('-' | image https://... | video https://...)")
            return True
        if step == "cover":
            draft["cover"] = self._parse_cover(text)
            state["step"] = "confirm"
            await self.safe_send(chat_id, f"Проверьте данные:\n\n{self._project_text(draft)}", reply_markup={"inline_keyboard": [[{"text": "✅ Сохранить", "callback_data": "projects:addconfirm"}, {"text": "✖️ Отмена", "callback_data": "projects:addcancel"}]]})
            return True
        return True

    async def _confirm_add(self, *, user_id: int, chat_id: int) -> None:
        state = self.dialogs.get(user_id) or {}
        if state.get("mode") != "add" or state.get("step") != "confirm":
            await self.safe_send(chat_id, "Нет проекта для сохранения.")
            return
        draft = state.get("draft")
        if not isinstance(draft, dict):
            self.dialogs.pop(user_id, None)
            return
        projects_path, _, _, _ = self._paths()
        projects = ensure_projects_exists(projects_path)
        existing_ids = {str(x.get("id") or "") for x in projects}
        draft = dict(draft)
        draft["id"] = create_project_id(str(draft.get("title") or "project"), existing_ids)
        draft["featured"] = not any(bool(x.get("featured")) for x in projects)
        draft["sort"] = max([int(x.get("sort") or 0) for x in projects] or [90]) + 10
        draft["updated_at"] = datetime.now(timezone.utc).isoformat()
        projects.append(draft)
        self._save_projects(projects, user_id, "projects_add", {"project_id": draft["id"]})
        self.dialogs.pop(user_id, None)
        await self.safe_send(chat_id, f"Проект добавлен: {draft['title']} ({draft['id']})")
        await self.show_project(chat_id=chat_id, message_id=None, project_id=draft["id"])

    async def _edit_step(self, *, user_id: int, chat_id: int, text: str) -> bool:
        state = self.dialogs.get(user_id) or {}
        if str(state.get("step") or "") != "value":
            return True
        pid = str(state.get("project_id") or "")
        field = str(state.get("field") or "")
        projects_path, _, _, _ = self._paths()
        projects = ensure_projects_exists(projects_path)
        item = find_project(projects, pid)
        if item is None:
            self.dialogs.pop(user_id, None)
            await self.safe_send(chat_id, "Проект не найден.")
            return True
        candidate = dict(item)
        candidate["years"] = dict(item.get("years") or {})
        candidate["cover"] = dict(item.get("cover") or {"type": "image", "src": ""})
        if field == "title":
            candidate["title"] = text
        elif field == "subtitle":
            candidate["subtitle"] = "" if text == "-" else text
        elif field == "description":
            candidate["description"] = text
        elif field == "status":
            candidate["status"] = self._parse_status(text)
        elif field == "years":
            candidate["years"] = self._parse_years(text)
        elif field == "tags":
            candidate["tags"] = self._parse_tags(text)
        elif field == "url":
            candidate["url"] = self._parse_url(text)
        elif field == "cover":
            candidate["cover"] = self._parse_cover(text)
        else:
            raise ValueError("Неизвестное поле")
        candidate["updated_at"] = datetime.now(timezone.utc).isoformat()
        state["candidate"] = candidate
        state["step"] = "confirm"
        await self.safe_send(chat_id, f"Подтвердите изменение:\n\n{self._project_text(candidate)}", reply_markup={"inline_keyboard": [[{"text": "✅ Сохранить", "callback_data": "projects:editconfirm"}, {"text": "✖️ Отмена", "callback_data": "projects:editcancel"}]]})
        return True

    async def _confirm_edit(self, *, user_id: int, chat_id: int) -> None:
        state = self.dialogs.get(user_id) or {}
        if state.get("mode") != "edit" or state.get("step") != "confirm":
            await self.safe_send(chat_id, "Нет изменений для сохранения.")
            return
        pid = str(state.get("project_id") or "")
        candidate = state.get("candidate")
        if not isinstance(candidate, dict):
            self.dialogs.pop(user_id, None)
            return
        projects_path, _, _, _ = self._paths()
        projects = ensure_projects_exists(projects_path)
        item = find_project(projects, pid)
        if item is None:
            self.dialogs.pop(user_id, None)
            await self.safe_send(chat_id, "Проект не найден.")
            return
        for k, v in candidate.items():
            item[k] = v
        self._save_projects(projects, user_id, "projects_edit", {"project_id": pid, "field": state.get("field")})
        self.dialogs.pop(user_id, None)
        await self.safe_send(chat_id, "Проект обновлен.")
        await self.show_project(chat_id=chat_id, message_id=None, project_id=pid)

    async def _delete_project(self, *, user_id: int, chat_id: int, message_id: int | None, project_id: str) -> None:
        projects_path, _, _, _ = self._paths()
        projects = ensure_projects_exists(projects_path)
        projects = [x for x in projects if str(x.get("id")) != project_id]
        self._save_projects(projects, user_id, "projects_delete", {"project_id": project_id})
        await self.safe_edit_or_send(chat_id, message_id, "Проект удален.", {"inline_keyboard": [[{"text": "⬅️ К списку", "callback_data": "projects:list:0"}]]})

    async def _set_featured(self, *, user_id: int, chat_id: int, message_id: int | None, project_id: str) -> None:
        projects_path, _, _, _ = self._paths()
        projects = ensure_projects_exists(projects_path)
        projects = set_featured(projects, project_id)
        self._save_projects(projects, user_id, "projects_featured", {"project_id": project_id})
        await self.show_project(chat_id=chat_id, message_id=message_id, project_id=project_id)

    async def _move_project(self, *, user_id: int, chat_id: int, message_id: int | None, project_id: str, up: bool) -> None:
        projects_path, _, _, _ = self._paths()
        projects = ensure_projects_exists(projects_path)
        idx = next((i for i, x in enumerate(projects) if str(x.get("id")) == project_id), -1)
        if idx >= 0:
            if up and idx > 0:
                projects[idx], projects[idx - 1] = projects[idx - 1], projects[idx]
            if (not up) and idx < len(projects) - 1:
                projects[idx], projects[idx + 1] = projects[idx + 1], projects[idx]
            for i, item in enumerate(projects):
                item["sort"] = 100 + i * 10
                item["updated_at"] = datetime.now(timezone.utc).isoformat()
            self._save_projects(projects, user_id, "projects_sort", {"project_id": project_id, "direction": "up" if up else "down"})
        await self.show_project(chat_id=chat_id, message_id=message_id, project_id=project_id)

    async def _check_project(self, *, chat_id: int, project_id: str) -> None:
        projects_path, _, _, _ = self._paths()
        projects = ensure_projects_exists(projects_path)
        item = find_project(projects, project_id)
        if item is None:
            await self.safe_send(chat_id, "Проект не найден.")
            return
        url = str(item.get("url") or "").strip()
        if not url:
            await self.safe_send(chat_id, "Ссылка у проекта не задана.")
            return
        try:
            async with httpx.AsyncClient(timeout=12, follow_redirects=True, trust_env=False) as client:
                response = await client.get(url, headers={"User-Agent": "serverredus-project-check/1.0"})
            ok = 200 <= response.status_code < 400
            await self.safe_send(chat_id, f"Проверка URL: {'доступен' if ok else 'недоступен'}\nHTTP: {response.status_code}\n{response.url}")
        except Exception as exc:
            await self.safe_send(chat_id, f"Проверка URL: ошибка\n{exc}")

    def _extract_media(self, message: dict[str, Any], media_type: str) -> tuple[str, str] | None:
        if media_type == "image":
            photos = message.get("photo") or []
            if isinstance(photos, list) and photos:
                fid = str((photos[-1] or {}).get("file_id") or "")
                if fid:
                    return fid, ".jpg"
            doc = message.get("document")
            if isinstance(doc, dict) and str(doc.get("mime_type") or "").lower().startswith("image/"):
                fid = str(doc.get("file_id") or "")
                name = str(doc.get("file_name") or "")
                ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ".jpg"
                if fid:
                    return fid, ext
        else:
            video = message.get("video")
            if isinstance(video, dict):
                fid = str(video.get("file_id") or "")
                if fid:
                    return fid, ".mp4"
            doc = message.get("document")
            if isinstance(doc, dict) and str(doc.get("mime_type") or "").lower().startswith("video/"):
                fid = str(doc.get("file_id") or "")
                name = str(doc.get("file_name") or "")
                ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ".mp4"
                if fid:
                    return fid, ext
        return None

    async def _apply_cover_upload(self, *, user_id: int, chat_id: int, project_id: str, media_type: str, src: str) -> None:
        projects_path, _, _, _ = self._paths()
        projects = ensure_projects_exists(projects_path)
        item = find_project(projects, project_id)
        if item is None:
            await self.safe_send(chat_id, "Проект не найден.")
            return
        item["cover"] = {"type": media_type, "src": src}
        item["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._save_projects(projects, user_id, "projects_cover_upload", {"project_id": project_id, "src": src, "type": media_type})
        await self.safe_send(chat_id, f"Cover обновлен: {src}")
