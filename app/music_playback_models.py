from datetime import datetime
from sqlalchemy import DateTime, Float, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.db import Base
from app.models import utcnow


class MusicPlaybackState(Base):
    __tablename__ = "music_playback_state"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    client_id: Mapped[str] = mapped_column(String(128), default="")
    output_id: Mapped[str] = mapped_column(String(256), default="default")
    volume: Mapped[int] = mapped_column(Integer, default=70)
    queue: Mapped[list] = mapped_column(JSON, default=list)
    repeat_mode: Mapped[str] = mapped_column(String(8), default="off")
    transfer_id: Mapped[str] = mapped_column(String(32), default="")
    queue_command_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)


class MusicTransfer(Base):
    __tablename__ = "music_transfers"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    status: Mapped[str] = mapped_column(String(24), default="waiting")
    source_device: Mapped[str] = mapped_column(String(134))
    source_key: Mapped[str] = mapped_column(String(64))
    target: Mapped[dict] = mapped_column(JSON)
    position: Mapped[float] = mapped_column(Float, default=0)
    stop_command_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    start_command_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MusicRemoteCommand(Base):
    __tablename__ = "music_remote_commands"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_key: Mapped[str] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(16))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    error: Mapped[str] = mapped_column(String(240), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
