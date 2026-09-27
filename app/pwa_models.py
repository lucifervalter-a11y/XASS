"""Single-use browser authorizations. Never store the bearer token itself."""
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class PwaActionProof(Base):
    __tablename__ = "pwa_action_proofs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[int] = mapped_column(BigInteger)
    credential_id: Mapped[int] = mapped_column(Integer)
    purpose: Mapped[str] = mapped_column(String(300))
    binding_hash: Mapped[str] = mapped_column(String(64))
    generation: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used: Mapped[bool] = mapped_column(Boolean, default=False)
