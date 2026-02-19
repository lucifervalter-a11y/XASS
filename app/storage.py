from datetime import datetime
from pathlib import Path

from app.config import get_settings

settings = get_settings()


def ensure_data_dirs() -> None:
    Path(settings.media_root).mkdir(parents=True, exist_ok=True)
    Path(settings.export_root).mkdir(parents=True, exist_ok=True)
    profile_path = Path(settings.profile_json_path)
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    Path(settings.profile_backups_dir).mkdir(parents=True, exist_ok=True)
    Path(settings.profile_audit_log_path).parent.mkdir(parents=True, exist_ok=True)
    Path(settings.profile_avatars_dir).mkdir(parents=True, exist_ok=True)
    projects_path = Path(settings.projects_json_path)
    projects_path.parent.mkdir(parents=True, exist_ok=True)
    site_config_path = Path(settings.site_config_json_path)
    site_config_path.parent.mkdir(parents=True, exist_ok=True)
    Path(settings.projects_backups_dir).mkdir(parents=True, exist_ok=True)
    Path(settings.projects_audit_log_path).parent.mkdir(parents=True, exist_ok=True)
    Path(settings.projects_assets_dir).mkdir(parents=True, exist_ok=True)
    Path(settings.backgrounds_assets_dir).mkdir(parents=True, exist_ok=True)
    Path(settings.update_log_path).parent.mkdir(parents=True, exist_ok=True)
    Path(settings.update_state_path).parent.mkdir(parents=True, exist_ok=True)


def _safe_token(value: str) -> str:
    return "".join(ch for ch in value if ch.isalnum() or ch in ("-", "_"))[:80] or "file"


def build_media_path(chat_id: int, message_id: int, file_id: str, source_file_path: str | None) -> Path:
    now = datetime.utcnow()
    date_dir = Path(settings.media_root) / f"{now.year:04d}" / f"{now.month:02d}" / f"{now.day:02d}" / str(chat_id)
    date_dir.mkdir(parents=True, exist_ok=True)

    extension = ""
    if source_file_path and "." in source_file_path:
        extension = "." + source_file_path.rsplit(".", 1)[-1].lower()

    filename = f"{message_id}_{_safe_token(file_id)}{extension}"
    return date_dir / filename
