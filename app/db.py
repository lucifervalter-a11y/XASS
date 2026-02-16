from collections.abc import AsyncIterator

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

settings = get_settings()
engine = create_async_engine(settings.database_url, echo=False)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    pass


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


async def init_db() -> None:
    import app.models  # noqa: F401

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.run_sync(_apply_runtime_migrations)


def _apply_runtime_migrations(connection) -> None:
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    if "app_config" not in tables:
        return

    columns = {item["name"] for item in inspector.get_columns("app_config")}
    migration_sql: list[str] = []

    if "away_mode_enabled" not in columns:
        migration_sql.append("ALTER TABLE app_config ADD COLUMN away_mode_enabled BOOLEAN NOT NULL DEFAULT FALSE")
    if "away_mode_message" not in columns:
        migration_sql.append("ALTER TABLE app_config ADD COLUMN away_mode_message TEXT")
    if "quiet_hours_start_minute" not in columns:
        migration_sql.append("ALTER TABLE app_config ADD COLUMN quiet_hours_start_minute INTEGER")
    if "quiet_hours_end_minute" not in columns:
        migration_sql.append("ALTER TABLE app_config ADD COLUMN quiet_hours_end_minute INTEGER")
    if "away_until_at" not in columns:
        migration_sql.append("ALTER TABLE app_config ADD COLUMN away_until_at DATETIME")
    if "away_schedule_enabled" not in columns:
        migration_sql.append("ALTER TABLE app_config ADD COLUMN away_schedule_enabled BOOLEAN NOT NULL DEFAULT FALSE")
    if "away_schedule_start_minute" not in columns:
        migration_sql.append("ALTER TABLE app_config ADD COLUMN away_schedule_start_minute INTEGER")
    if "away_schedule_end_minute" not in columns:
        migration_sql.append("ALTER TABLE app_config ADD COLUMN away_schedule_end_minute INTEGER")
    if "away_bypass_user_ids" not in columns:
        migration_sql.append("ALTER TABLE app_config ADD COLUMN away_bypass_user_ids TEXT")
    if "service_base_url" not in columns:
        migration_sql.append("ALTER TABLE app_config ADD COLUMN service_base_url TEXT")
    if "iphone_shortcut_url" not in columns:
        migration_sql.append("ALTER TABLE app_config ADD COLUMN iphone_shortcut_url TEXT")

    for statement in migration_sql:
        connection.execute(text(statement))
