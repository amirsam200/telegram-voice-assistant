"""Response and request models for the admin API. Nothing here carries secrets or audio."""

import datetime as dt
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.db.models import Intent, RunStatus

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class _Orm(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class RunSummary(_Orm):
    id: int
    user_id: int | None
    intent: Intent
    status: RunStatus
    input_text: str | None = Field(description="Transcript or typed text.")
    model: str | None
    tokens: int | None
    steps: int | None
    latency_ms: int | None
    error: str | None
    created_at: dt.datetime


class ToolCallOut(_Orm):
    id: int
    tool_name: str
    arguments_json: dict[str, Any] | None
    result_json: dict[str, Any] | None
    status: str
    latency_ms: int | None
    created_at: dt.datetime


class VoiceOut(_Orm):
    """Metadata only: the audio itself is deleted after transcription and never served."""

    id: int
    duration_s: int | None
    stt_confidence: float | None
    created_at: dt.datetime


class RunDetail(RunSummary):
    reply_text: str | None
    action_json: dict[str, Any] | None
    prompt_version: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    voice: VoiceOut | None = None
    tool_calls: list[ToolCallOut] = []


class EventOut(_Orm):
    id: int
    run_id: int
    provider_event_id: str | None
    date: dt.date
    time: dt.time
    duration_min: int
    title: str
    description: str | None
    guests: list[str]
    created_at: dt.datetime


class EmailOut(_Orm):
    id: int
    run_id: int
    to: str
    subject: str
    body: str
    provider_message_id: str | None
    status: str
    created_at: dt.datetime


class ContactOut(_Orm):
    id: int
    user_id: int
    name: str
    email: str | None
    aliases: list[str]
    created_at: dt.datetime


class ContactIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: EmailStr
    aliases: list[str] = Field(default_factory=list, max_length=20)
    user_id: int | None = Field(default=None, description="Defaults to the owner.")

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("name must not be empty")
        return v


class RetryOut(BaseModel):
    run_id: int
    ok: bool
    status: str
    message: str = Field(description="The Persian result message, as the bot would show it.")


class HealthOut(BaseModel):
    status: str
    checks: dict[str, str]
