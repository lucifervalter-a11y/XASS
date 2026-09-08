"""Private music library; file bytes stay outside the public web tree."""
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import utcnow


class MusicTrack(Base):
    __tablename__ = "music_tracks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(240))
    artist: Mapped[str] = mapped_column(String(240), default="")
    album: Mapped[str] = mapped_column(String(240), default="")
    filename: Mapped[str] = mapped_column(String(255))
    storage_name: Mapped[str] = mapped_column(String(96), unique=True)
    mime: Mapped[str] = mapped_column(String(80))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    size: Mapped[int] = mapped_column(BigInteger)
    duration: Mapped[float] = mapped_column(Float, default=0)
    favorite: Mapped[bool] = mapped_column(Boolean, default=False)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MusicUpload(Base):
    __tablename__ = "music_uploads"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    size: Mapped[int] = mapped_column(BigInteger)
    offset: Mapped[int] = mapped_column(BigInteger, default=0)
    owner_id: Mapped[int] = mapped_column(BigInteger)
    track_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MusicPlaylist(Base):
    __tablename__ = "music_playlists"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    track_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MusicUploadReceipt(Base):
    """Stable response for a completed multi-track upload; safe finish retries."""
    __tablename__ = "music_upload_receipts"
    upload_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    result: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MusicSession(Base):
    __tablename__ = "music_sessions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    track_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    device: Mapped[str] = mapped_column(String(134), default="local")
    state: Mapped[str] = mapped_column(String(24), default="idle")
    position: Mapped[float] = mapped_column(Float, default=0)
    share_site: Mapped[bool] = mapped_column(Boolean, default=False)
    share_discord: Mapped[bool] = mapped_column(Boolean, default=False)
    session_key: Mapped[str] = mapped_column(String(64), default="")
    previous_source: Mapped[str] = mapped_column(String(64), default="pc_agent")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
