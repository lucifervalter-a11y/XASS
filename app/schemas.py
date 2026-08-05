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
    discord: dict[str, Any] | None = None
    agent_version: str = Field(default="0.0.0", max_length=32)
    agent_revision: str = Field(default="", max_length=128)
    agent_distribution: str = Field(default="source", max_length=32)
    command_results: list[dict[str, Any]] = Field(default_factory=list, max_length=50)


class HeartbeatResponse(BaseModel):
    ok: bool = True
    source_name: str
    recovered: bool = False
    new_source: bool = False
    server_time: datetime
    server_version: str = "0.7.0"
    update: dict[str, Any] | None = None
    installer_update: dict[str, Any] | None = None
    commands: list[dict[str, Any]] = Field(default_factory=list)


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
