"""Logging of every LLM request to llm_calls, done inside the provider so callers do not change.

The run comes from the run_id ContextVar (app.logging_setup); the purpose is inferred from the
conversation: the first call of a run decides the intent, later calls are tool steps.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.logging_setup import run_id_var

logger = logging.getLogger(__name__)

PURPOSES = ("stt_fix", "intent", "tool_step", "review")


@dataclass
class LlmCallRecord:
    model: str
    purpose: str
    status: str  # ok, retry, error
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    latency_ms: int | None = None
    error: str | None = None
    run_id: int | None = None
    cost_usd: float | None = None


def infer_purpose(messages: list[dict[str, Any]]) -> str:
    """intent for the first call of a run; tool_step once the model has answered before."""
    for message in messages:
        if message.get("role") == "tool" or (message.get("role") == "assistant" and message.get("tool_calls")):
            return "tool_step"
    return "intent"


def current_run_id() -> int | None:
    value = run_id_var.get()
    return int(value) if value and value.isdigit() else None


def cached_tokens_of(usage: Any) -> int:
    """DeepSeek reports prompt_cache_hit_tokens; OpenAI-style APIs prompt_tokens_details.cached_tokens."""
    if usage is None:
        return 0
    hit = getattr(usage, "prompt_cache_hit_tokens", None)
    if hit is None:
        details = getattr(usage, "prompt_tokens_details", None)
        hit = getattr(details, "cached_tokens", None) if details is not None else None
    return int(hit or 0)


def cost_usd(model: str, prompt_tokens: int, completion_tokens: int, cached_tokens: int = 0) -> float | None:
    """Cost from the per-model prices in config (USD per 1M tokens); None for unknown models."""
    prices = get_settings().llm_prices.get(model)
    if prices is None:
        return None
    cached = min(cached_tokens, prompt_tokens)
    total = (
        (prompt_tokens - cached) * prices.get("input", 0.0)
        + cached * prices.get("cached_input", prices.get("input", 0.0))
        + completion_tokens * prices.get("output", 0.0)
    )
    return round(total / 1_000_000, 6)


Recorder = Callable[[LlmCallRecord], None]


class DbRecorder:
    """Writes each record in its own short transaction, so it is visible at once."""

    def __init__(self, session_factory: Callable | None = None,
                 after: list[Callable[[LlmCallRecord], None]] | None = None) -> None:
        self._session_factory = session_factory
        self.after = after if after is not None else []

    def __call__(self, record: LlmCallRecord) -> None:
        from app.db.models import LlmCall

        try:
            if self._session_factory is None:
                from app.db.session import SessionLocal

                self._session_factory = SessionLocal
            if record.cost_usd is None and record.status == "ok":
                record.cost_usd = cost_usd(record.model, record.prompt_tokens, record.completion_tokens,
                                           record.cached_tokens)
            for run_id in dict.fromkeys([record.run_id, None]):  # without the run if it no longer exists
                try:
                    with self._session_factory() as session:
                        session.add(LlmCall(
                            run_id=run_id, model=record.model, purpose=record.purpose,
                            prompt_tokens=record.prompt_tokens, completion_tokens=record.completion_tokens,
                            cached_tokens=record.cached_tokens, cost_usd=record.cost_usd,
                            latency_ms=record.latency_ms, status=record.status, error=record.error,
                        ))
                        session.commit()
                    break
                except IntegrityError:
                    if run_id is None:
                        raise
                    logger.warning("LLM call refers to missing run %s; recorded without it", run_id)
        except Exception:  # logging must never break the request itself
            logger.exception("Could not record LLM call")
            return
        for hook in self.after:
            try:
                hook(record)
            except Exception:
                logger.exception("LLM call hook failed")


_recorder: Recorder | None = None


def get_recorder() -> Recorder:
    global _recorder
    if _recorder is None:
        _recorder = DbRecorder()
    return _recorder


def set_recorder(recorder: Recorder | None) -> None:
    global _recorder
    _recorder = recorder
