import datetime as dt
import enum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    Boolean,
    Numeric,
    SmallInteger,
    MetaData,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Intent(str, enum.Enum):
    schedule = "schedule"
    email = "email"
    contact = "contact"  # add or delete a saved contact
    unknown = "unknown"


class RunStatus(str, enum.Enum):
    running = "running"
    clarification = "clarification"  # the agent asked the user a question instead of proposing
    pending_confirmation = "pending_confirmation"
    confirmed = "confirmed"
    executed = "executed"
    rejected = "rejected"
    failed = "failed"


def _enum(cls: type[enum.Enum], name: str) -> Enum:
    return Enum(cls, name=name, values_callable=lambda e: [m.value for m in e])


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(320))
    timezone: Mapped[str] = mapped_column(
        String(64), default="Asia/Tehran", server_default="Asia/Tehran", nullable=False
    )

    contacts: Mapped[list["Contact"]] = relationship(back_populates="user")


class Contact(TimestampMixin, Base):
    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # Persian and English spellings, e.g. ["علی", "Ali Rezaei"].
    aliases: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default="[]", nullable=False
    )
    email: Mapped[str | None] = mapped_column(String(320))

    user: Mapped[User] = relationship(back_populates="contacts")


class VoiceMessage(TimestampMixin, Base):
    __tablename__ = "voice_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    telegram_file_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # Local path of the downloaded audio; cleared when the audio is deleted after transcription.
    file_path: Mapped[str | None] = mapped_column(String(500))
    duration_s: Mapped[int | None] = mapped_column(Integer)
    transcript: Mapped[str | None] = mapped_column(Text)
    stt_confidence: Mapped[float | None] = mapped_column(Float)


class AgentRun(TimestampMixin, Base):
    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    voice_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("voice_messages.id", ondelete="SET NULL"), index=True
    )
    # The transcript or typed text the run started from, and what the bot replied.
    input_text: Mapped[str | None] = mapped_column(Text)
    reply_text: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    intent: Mapped[Intent] = mapped_column(_enum(Intent, "agent_intent"), nullable=False)
    action_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[RunStatus] = mapped_column(
        _enum(RunStatus, "agent_run_status"),
        default=RunStatus.pending_confirmation,
        server_default=RunStatus.pending_confirmation.value,
        nullable=False,
    )
    model: Mapped[str | None] = mapped_column(String(100))
    tokens: Mapped[int | None] = mapped_column(Integer)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    steps: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    # Final answers the model gave that failed validation (bad JSON, schema, invented email, past date).
    validation_errors: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)


class ToolCall(TimestampMixin, Base):
    __tablename__ = "tool_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True
    )
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    arguments_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(
        String(32), default="pending", server_default="pending", nullable=False
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer)


class CalendarEvent(TimestampMixin, Base):
    __tablename__ = "calendar_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True)
    provider_event_id: Mapped[str | None] = mapped_column(String(255))
    date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    time: Mapped[dt.time] = mapped_column(Time, nullable=False)
    duration_min: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    guests: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default="[]", nullable=False
    )


class Email(TimestampMixin, Base):
    __tablename__ = "emails"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True)
    to: Mapped[str] = mapped_column(String(320), nullable=False)
    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    provider_message_id: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(
        String(32), default="pending", server_default="pending", nullable=False
    )


class MemoryFact(TimestampMixin, Base):
    __tablename__ = "memory_facts"
    __table_args__ = (UniqueConstraint("user_id", "key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(200), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str | None] = mapped_column(String(100))


class AuditEvent(TimestampMixin, Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    actor: Mapped[str | None] = mapped_column(String(200))
    payload_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class LlmCall(Base):
    """One request to the LLM provider (written by the provider, one row per attempt)."""

    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("agent_runs.id", ondelete="SET NULL"), index=True)
    model: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    # stt_fix, intent (first call of a run), tool_step (later calls), review
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # ok, retry, error
    error: Mapped[str | None] = mapped_column(Text)
    # Rows from the demo seed script; shown on the dashboard but never counted against budgets.
    demo: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )


class RunFeedback(TimestampMixin, Base):
    """Thumbs up (+1) or down (-1) the user gave under a bot reply; one per run."""

    __tablename__ = "run_feedback"
    __table_args__ = (UniqueConstraint("run_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    rating: Mapped[int] = mapped_column(SmallInteger, nullable=False)


class BudgetAlert(Base):
    """A budget alert that was sent, so each threshold alerts once per day or month."""

    __tablename__ = "budget_alerts"
    __table_args__ = (UniqueConstraint("period", "threshold", name="uq_budget_alerts_period_threshold"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    period: Mapped[str] = mapped_column(String(32), nullable=False)  # "day:2026-10-18" or "month:2026-10"
    threshold: Mapped[int] = mapped_column(Integer, nullable=False)  # percent: 80 or 100
    used_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    budget_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
