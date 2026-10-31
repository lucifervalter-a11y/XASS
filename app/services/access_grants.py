from __future__ import annotations

import logging
from typing import Any
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import AccessGrant, BusinessConnection, MessageLog
from app.services.auth import is_owner

logger = logging.getLogger(__name__)


async def grant_user_access(
    session: AsyncSession,
    *,
    user_id: int,
    granted_by: int | None = None,
    username: str | None = None,
    first_name: str | None = None,
    can_business: bool = True,
    can_view_deleted: bool = True,
    can_music: bool = False,
    note: str | None = None,
) -> AccessGrant:
    grant = await session.scalar(select(AccessGrant).where(AccessGrant.user_id == user_id))
    if grant is None:
        grant = AccessGrant(
            user_id=user_id,
            username=username.lstrip("@") if username else None,
            first_name=first_name,
            granted_by=granted_by,
            can_business=can_business,
            can_view_deleted=can_view_deleted,
            can_music=can_music,
            is_active=True,
            note=note,
        )
        session.add(grant)
    else:
        grant.is_active = True
        grant.can_business = can_business
        grant.can_view_deleted = can_view_deleted
        grant.can_music = can_music
        if username:
            grant.username = username.lstrip("@")
        if first_name:
            grant.first_name = first_name
        if granted_by:
            grant.granted_by = granted_by
        if note:
            grant.note = note
    await session.commit()
    logger.info("Granted access to user %s (business=%s, deleted=%s, music=%s)", user_id, can_business, can_view_deleted, can_music)
    return grant


async def revoke_user_access(session: AsyncSession, user_id: int) -> bool:
    grant = await session.scalar(select(AccessGrant).where(AccessGrant.user_id == user_id))
    if grant is None:
        return False
    grant.is_active = False
    connections = list(await session.scalars(select(BusinessConnection).where(BusinessConnection.user_id == user_id)))
    for conn in connections:
        conn.is_enabled = False
    await session.commit()
    logger.info("Revoked access and disabled connections for user %s", user_id)
    return True


async def get_user_grant(session: AsyncSession | None, user_id: int) -> AccessGrant | None:
    if session is None:
        return None
    return await session.scalar(select(AccessGrant).where(AccessGrant.user_id == user_id, AccessGrant.is_active.is_(True)))


async def has_active_grant(session: AsyncSession | None, user_id: int | None) -> bool:
    if session is None or user_id is None:
        return False
    grant = await get_user_grant(session, user_id)
    return grant is not None and grant.is_active


async def is_user_permitted(
    session: AsyncSession,
    user_id: int | None,
    permission: str,
    settings: Settings,
) -> bool:
    if user_id is None:
        return False
    if is_owner(user_id, settings) or user_id in settings.admin_user_ids:
        return True
    if user_id in settings.all_authorized_user_ids:
        return True
    if session is None:
        return False
    grant = await get_user_grant(session, user_id)
    if not grant or not grant.is_active:
        return False
    if permission in ("any", "login", "start"):
        return True
    if permission == "business":
        return grant.can_business
    if permission in ("deleted", "view_deleted"):
        return grant.can_view_deleted
    if permission == "music":
        return grant.can_music
    return False


async def list_all_grants(session: AsyncSession) -> list[AccessGrant]:
    return list(await session.scalars(select(AccessGrant).order_by(AccessGrant.id.desc())))


async def upsert_business_connection(
    session: AsyncSession,
    *,
    connection_id: str,
    user_id: int,
    user_chat_id: int | None = None,
    can_reply: bool = False,
    is_enabled: bool = True,
) -> BusinessConnection:
    conn = await session.scalar(select(BusinessConnection).where(BusinessConnection.connection_id == connection_id))
    if conn is None:
        conn = BusinessConnection(
            connection_id=connection_id,
            user_id=user_id,
            user_chat_id=user_chat_id or user_id,
            can_reply=can_reply,
            is_enabled=is_enabled,
        )
        session.add(conn)
    else:
        conn.user_id = user_id
        if user_chat_id:
            conn.user_chat_id = user_chat_id
        conn.can_reply = can_reply
        conn.is_enabled = is_enabled
    await session.commit()
    logger.info("Upserted business connection %s for user %s (enabled=%s)", connection_id, user_id, is_enabled)
    return conn


async def get_business_connection(session: AsyncSession | None, connection_id: str) -> BusinessConnection | None:
    if session is None:
        return None
    return await session.scalar(select(BusinessConnection).where(BusinessConnection.connection_id == connection_id))


async def get_user_connection_ids(session: AsyncSession | None, user_id: int) -> list[str]:
    if session is None:
        return []
    rows = await session.scalars(
        select(BusinessConnection.connection_id).where(
            BusinessConnection.user_id == user_id,
            BusinessConnection.is_enabled.is_(True),
        )
    )
    return list(rows)


async def find_user_by_username(session: AsyncSession, username: str) -> int | None:
    clean = username.lstrip("@").lower().strip()
    if not clean:
        return None
    grant = await session.scalar(select(AccessGrant.user_id).where(func.lower(AccessGrant.username) == clean))
    if grant:
        return int(grant)
    log = await session.scalar(
        select(MessageLog.from_user_id)
        .where(func.lower(MessageLog.from_username) == clean, MessageLog.from_user_id.isnot(None))
        .order_by(MessageLog.id.desc())
        .limit(1)
    )
    if log:
        return int(log)
    return None
