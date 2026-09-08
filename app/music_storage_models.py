"""Credential-bound replicas and resumable transfers, separate from track metadata."""
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import utcnow


class MusicStorageCopy(Base):
    __tablename__ = "music_storage_copies"
    __table_args__ = (UniqueConstraint("track_id", "credential_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    track_id: Mapped[int] = mapped_column(Integer, index=True)
    credential_id: Mapped[int] = mapped_column(Integer, index=True)
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(BigInteger)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MusicStorageJob(Base):
    __tablename__ = "music_storage_jobs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    track_id: Mapped[int] = mapped_column(Integer, index=True)
    credential_id: Mapped[int] = mapped_column(Integer, index=True)
    operation: Mapped[str] = mapped_column(String(16))
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(BigInteger)
    offset: Mapped[int] = mapped_column(BigInteger, default=0)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    error_code: Mapped[str] = mapped_column(String(40), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MusicImportRun(Base):
    __tablename__ = "music_import_runs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    credential_id: Mapped[int] = mapped_column(Integer, index=True)
    root: Mapped[str] = mapped_column(String(32))
    relative_path: Mapped[str] = mapped_column(String(1024), default="")
    status: Mapped[str] = mapped_column(String(16), default="pending")
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MusicImportFile(Base):
    __tablename__ = "music_import_files"
    __table_args__ = (UniqueConstraint("credential_id", "path_key", "sha256"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(32), index=True)
    credential_id: Mapped[int] = mapped_column(Integer, index=True)
    path_key: Mapped[str] = mapped_column(String(64))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    size: Mapped[int] = mapped_column(BigInteger)
    filename: Mapped[str] = mapped_column(String(255))
    ordinal: Mapped[int] = mapped_column(Integer)
    offset: Mapped[int] = mapped_column(BigInteger, default=0)
    track_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class MusicProviderCheck(Base):
    __tablename__ = "music_provider_checks"
    provider: Mapped[str] = mapped_column(String(20), primary_key=True)
    token_fingerprint: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MusicContentLock(Base):
    __tablename__ = "music_content_locks"
    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
