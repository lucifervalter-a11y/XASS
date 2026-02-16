from functools import lru_cache
from typing import Annotated, Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _parse_csv_int(value: Any) -> list[int]:
    if value is None:
        return []
    if isinstance(value, list):
        return [int(item) for item in value if str(item).strip()]
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",")]
        return [int(item) for item in items if item]
    return []


def _parse_csv_str(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    bot_token: str = ""
    owner_user_id: int = 0
    authorized_user_ids: Annotated[list[int], NoDecode] = []
    admin_user_ids: Annotated[list[int], NoDecode] = []

    database_url: str = "sqlite+aiosqlite:///./data/serverredus.db"
    media_root: str = "./data/media"
    export_root: str = "./data/exports"
    profile_json_path: str = "./data/profile.json"
    profile_backups_dir: str = "./data/backups"
    profile_audit_log_path: str = "./data/audit.log"
    profile_public_url: str = ""
    profile_avatars_dir: str = "./data/avatars"
    now_playing_source_default: str = "pc_agent"
    iphone_now_playing_api_key: str = ""
    iphone_now_playing_stale_minutes: int = 180
    vk_user_id: int | None = None
    vk_app_id: int | None = None
    vk_access_token: str = ""
    vk_api_version: str = "5.199"
    vk_now_playing_refresh_minutes: int = 2

    telegram_webhook_path: str = "change-me-webhook-path"
    telegram_secret_token: str = ""
    setup_api_key: str = "change-me-setup-key"
    agent_api_key: str = "change-me-agent-key"
    agent_pair_code_ttl_minutes: int = 15
    agent_pair_code_length: int = 8

    notify_chat_id: int | None = None
    monitored_services: Annotated[list[str], NoDecode] = []
    heartbeat_check_interval_sec: int = 30
    use_polling: bool = False
    polling_request_timeout_sec: int = 25
    polling_retry_delay_sec: int = 2
    polling_drop_pending_updates: bool = False
    timezone: str = "UTC"
    top_processes_limit: int = 5

    @field_validator("authorized_user_ids", "admin_user_ids", mode="before")
    @classmethod
    def _normalize_int_lists(cls, value: Any) -> list[int]:
        return _parse_csv_int(value)

    @field_validator("owner_user_id", mode="before")
    @classmethod
    def _normalize_owner_user_id(cls, value: Any) -> int:
        if value is None:
            return 0
        if isinstance(value, str) and not value.strip():
            return 0
        return int(value)

    @field_validator("notify_chat_id", mode="before")
    @classmethod
    def _normalize_optional_notify_chat(cls, value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return int(value)

    @field_validator("vk_user_id", mode="before")
    @classmethod
    def _normalize_optional_vk_user_id(cls, value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return int(value)

    @field_validator("vk_app_id", mode="before")
    @classmethod
    def _normalize_optional_vk_app_id(cls, value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return int(value)

    @field_validator("monitored_services", mode="before")
    @classmethod
    def _normalize_str_lists(cls, value: Any) -> list[str]:
        return _parse_csv_str(value)

    @field_validator("timezone", mode="before")
    @classmethod
    def _normalize_timezone(cls, value: Any) -> str:
        if value is None:
            return "UTC"
        text = str(value).strip()
        return text or "UTC"

    @field_validator("now_playing_source_default", mode="before")
    @classmethod
    def _normalize_now_playing_source_default(cls, value: Any) -> str:
        allowed = {"pc_agent", "iphone", "vk"}
        text = str(value or "").strip().lower()
        return text if text in allowed else "pc_agent"

    @field_validator("iphone_now_playing_stale_minutes", mode="before")
    @classmethod
    def _normalize_iphone_stale_minutes(cls, value: Any) -> int:
        if value is None:
            return 180
        parsed = int(value)
        return max(5, min(parsed, 1440))

    @field_validator("vk_now_playing_refresh_minutes", mode="before")
    @classmethod
    def _normalize_vk_refresh_minutes(cls, value: Any) -> int:
        if value is None:
            return 2
        parsed = int(value)
        return max(1, min(parsed, 120))

    @field_validator("agent_pair_code_ttl_minutes", mode="before")
    @classmethod
    def _normalize_pair_ttl(cls, value: Any) -> int:
        if value is None:
            return 15
        parsed = int(value)
        return max(1, min(parsed, 240))

    @field_validator("agent_pair_code_length", mode="before")
    @classmethod
    def _normalize_pair_code_length(cls, value: Any) -> int:
        if value is None:
            return 8
        parsed = int(value)
        return max(6, min(parsed, 24))

    @property
    def all_authorized_user_ids(self) -> set[int]:
        user_ids = {user_id for user_id in self.authorized_user_ids}
        user_ids.update(self.admin_user_ids)
        if self.owner_user_id:
            user_ids.add(self.owner_user_id)
        return user_ids


@lru_cache
def get_settings() -> Settings:
    return Settings()
