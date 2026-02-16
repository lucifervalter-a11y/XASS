from app.config import Settings


def is_authorized(user_id: int | None, settings: Settings) -> bool:
    if user_id is None:
        return False
    return user_id in settings.all_authorized_user_ids


def is_owner(user_id: int | None, settings: Settings) -> bool:
    return bool(user_id and settings.owner_user_id and user_id == settings.owner_user_id)

