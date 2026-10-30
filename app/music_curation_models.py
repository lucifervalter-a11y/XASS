"""Additive, durable catalog-edit journal. Audio originals are never rewritten."""
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import utcnow


class MusicCurationBatch(Base):
    __tablename__ = "music_curation_batches"
    __table_args__ = (UniqueConstraint("owner_id", "request_key", name="uq_music_curation_owner_request"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    request_key: Mapped[str] = mapped_column(String(64))
    request_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16), default="applied")
    entries: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    undone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
