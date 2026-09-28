"""Persistent PC transcription queue: one job per track, leased to one worker."""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import utcnow


class TranscriptionJob(Base):
    __tablename__ = "transcription_jobs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Dedupe: a track has at most one job ever; a finished result is reused forever.
    track_id: Mapped[int] = mapped_column(Integer, unique=True, index=True, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    duration: Mapped[float] = mapped_column(Float, default=0)
    language: Mapped[str] = mapped_column(String(8), default="ru")
    # queued -> assigned (offered to one worker) -> running -> done | failed
    state: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    worker_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    failed_workers: Mapped[list] = mapped_column(JSON, default=list)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    progress: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(String(200), default="")
    requested_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TranscriptionWorker(Base):
    __tablename__ = "transcription_workers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # One worker per individually paired agent credential.
    credential_id: Mapped[int] = mapped_column(Integer, unique=True, index=True, nullable=False)
    source_name: Mapped[str] = mapped_column(String(128), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    state: Mapped[str] = mapped_column(String(16), default="disabled")
    detail: Mapped[str] = mapped_column(String(300), default="")
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict)
    load: Mapped[dict] = mapped_column(JSON, default=dict)
    # First-time setup progress while state == "installing": {stage, percent}.
    setup: Mapped[dict] = mapped_column(JSON, default=dict)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
