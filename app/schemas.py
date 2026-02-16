from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.enums import SourceType


class HeartbeatPayload(BaseModel):
    source_name: str = Field(min_length=1, max_length=128)
    source_type: SourceType = SourceType.PC_AGENT
    metrics: dict[str, Any] = Field(default_factory=dict)
    now_playing: str | None = None
    active_app: str | None = None
    activity: dict[str, Any] = Field(default_factory=dict)
    processes: list[dict[str, Any]] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class HeartbeatResponse(BaseModel):
    ok: bool = True
    source_name: str
    recovered: bool = False
    new_source: bool = False
    server_time: datetime


class AgentPairClaimPayload(BaseModel):
    pair_code: str = Field(min_length=4, max_length=64)
    source_name: str | None = Field(default=None, min_length=1, max_length=128)
    source_type: SourceType = SourceType.PC_AGENT


class AgentPairClaimResponse(BaseModel):
    ok: bool = True
    source_name: str
    source_type: SourceType
    agent_api_key: str
    issued_at: datetime


class ExternalNowPlayingPayload(BaseModel):
    text: str = Field(default="", max_length=512)
    source: str = Field(default="iphone", min_length=1, max_length=32)
    artist: str = Field(default="", max_length=256)
    title: str = Field(default="", max_length=256)
    track: str = Field(default="", max_length=256)
