"""Native-device public keys and single-use, purpose-bound authorizations."""
from datetime import datetime
from sqlalchemy import BigInteger, Boolean, DateTime, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.db import Base
from app.models import utcnow


class NativeDevice(Base):
    __tablename__ = "native_devices"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    public_key: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(100), default="iPhone")
    generation: Mapped[int] = mapped_column(Integer)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class NativeChallenge(Base):
    __tablename__ = "native_challenges"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    kind: Mapped[str] = mapped_column(String(16))
    device_id: Mapped[str] = mapped_column(String(32), default="")
    public_key: Mapped[str] = mapped_column(String(128), default="")
    pair_hash: Mapped[str] = mapped_column(String(64), default="")
    purpose: Mapped[str] = mapped_column(String(256), default="")
    binding_hash: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)
    generation: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used: Mapped[bool] = mapped_column(Boolean, default=False)


class NativeProof(Base):
    __tablename__ = "native_action_proofs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[int] = mapped_column(BigInteger)
    device_id: Mapped[str] = mapped_column(String(32))
    purpose: Mapped[str] = mapped_column(String(256))
    binding_hash: Mapped[str] = mapped_column(String(64))
    generation: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used: Mapped[bool] = mapped_column(Boolean, default=False)
