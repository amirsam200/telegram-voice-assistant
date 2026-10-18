"""llm_calls logging from the provider, quality signals captured per run, feedback and demo data."""

import contextlib

import pytest
from sqlalchemy import func, select

from app.agent import llm_log
from app.agent.agent import run_agent
from app.agent.llm_log import cached_tokens_of, cost_usd, infer_purpose
from app.bot import main as bot
from app.db import demo
from app.db.models import AgentRun, AuditEvent, Intent, LlmCall, RunFeedback, RunStatus, User
from app.logging_setup import log_run
from tests.conftest import NOW
from tests.test_agent import ScriptedProvider
from tests.test_provider import _provider


def _calls(session) -> list[LlmCall]:
    session.expire_all()
    return session.scalars(select(LlmCall).order_by(LlmCall.id)).all()


# --- provider -> llm_calls ------------------------------------------------------------------------


def test_successful_call_is_recorded_with_run_purpose_tokens_and_cost(session, owner) -> None:
    run = AgentRun(user_id=owner.id, intent=Intent.unknown, status=RunStatus.running)
    session.add(run)
    session.commit()
    p, _ = _provider(failures=0)
    with log_run(run.id):
        p.chat([{"role": "user", "content": "سلام"}], tools=[])

    [call] = _calls(session)
    assert (call.run_id, call.model, call.purpose, call.status) == (run.id, "deepseek-chat", "intent", "ok")
    assert (call.prompt_tokens, call.completion_tokens, call.cached_tokens) == (10, 3, 0)
    assert float(call.cost_usd) == pytest.approx(cost_usd("deepseek-chat", 10, 3))
    assert call.latency_ms is not None and call.created_at is not None


def test_retries_and_final_failure_are_recorded(session) -> None:
    p, _ = _provider(failures=2)
    p.chat([{"role": "user", "content": "hi"}], tools=[])
    assert [c.status for c in _calls(session)] == ["retry", "retry", "ok"]
    assert "APITimeoutError" in _calls(session)[0].error

    from app.agent.provider import LLMError

    p, _ = _provider(failures=10, max_retries=1)
    with pytest.raises(LLMError):
        p.chat([{"role": "user", "content": "hi"}], tools=[])
    assert [c.status for c in _calls(session)][-2:] == ["retry", "error"]


def test_calls_outside_a_run_have_no_run_id(session) -> None:
    p, _ = _provider(failures=0)
    with log_run("mcp-abc"):
        p.chat([{"role": "user", "content": "hi"}], tools=[])
    assert _calls(session)[0].run_id is None


def test_recording_failure_never_breaks_the_call() -> None:
    def broken_factory():
        raise RuntimeError("database down")

    p, _ = _provider(failures=0)
    p._recorder = llm_log.DbRecorder(broken_factory)
    assert p.chat([{"role": "user", "content": "hi"}], tools=[]).content == "سلام"


def test_purpose_is_inferred_from_the_conversation() -> None:
    assert infer_purpose([{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]) == "intent"
    assert infer_purpose([{"role": "user", "content": "u"},
                          {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
                          {"role": "tool", "content": "{}"}]) == "tool_step"


def test_cost_uses_cached_price_and_unknown_models_have_none(monkeypatch) -> None:
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "llm_prices", {"m": {"input": 1.0, "cached_input": 0.1, "output": 2.0}})
    # 600k uncached * $1 + 400k cached * $0.1 + 100k output * $2, per 1M tokens
    assert cost_usd("m", 1_000_000, 100_000, cached_tokens=400_000) == pytest.approx(0.6 + 0.04 + 0.2)
    assert cost_usd("other", 1000, 1000) is None


def test_cached_tokens_from_either_usage_format() -> None:
    from types import SimpleNamespace as NS

    assert cached_tokens_of(NS(prompt_cache_hit_tokens=120)) == 120
    assert cached_tokens_of(NS(prompt_tokens_details=NS(cached_tokens=80))) == 80
    assert cached_tokens_of(NS(prompt_tokens=10)) == 0
    assert cached_tokens_of(None) == 0


# --- quality signals ------------------------------------------------------------------------------


def test_validation_failures_are_counted_per_run(session, owner, ctx) -> None:
    provider = ScriptedProvider(
        [("propose_email", {"to": "x@invented.com", "subject": "گزارش", "body": "سلام"})],
        [("propose_email", {"to": "sara.mohammadi@example.com", "subject": "گزارش", "body": "not persian"})],
        [("propose_email", {"to": "sara.mohammadi@example.com", "subject": "گزارش", "body": "سلام سارا"})],
    )
    result = run_agent(session, owner, "به سارا ایمیل بزن", provider, ctx)
    run = session.get(AgentRun, result.run_id)
    assert (run.validation_errors, run.steps, run.status) == (2, 3, RunStatus.pending_confirmation)


@contextlib.contextmanager
def _test_scope(session):
    yield session
    session.commit()


def test_feedback_is_stored_once_per_run_and_only_for_own_runs(session, owner, monkeypatch) -> None:
    monkeypatch.setattr(bot, "session_scope", lambda: _test_scope(session))
    run = AgentRun(user_id=owner.id, intent=Intent.email, status=RunStatus.executed)
    session.add(run)
    session.commit()

    assert bot._save_feedback(run.id, owner.id, 1)
    assert bot._save_feedback(run.id, owner.id, -1)  # changing the vote updates the same row
    assert not bot._save_feedback(run.id, owner.id + 1, 1)
    assert not bot._save_feedback(999, owner.id, 1)
    rows = session.scalars(select(RunFeedback)).all()
    assert [(r.run_id, r.rating) for r in rows] == [(run.id, -1)]


# --- demo data ------------------------------------------------------------------------------------


def test_demo_seed_is_realistic_flagged_and_idempotent(session) -> None:
    runs = demo.seed(session, days=7, now=NOW)
    assert runs > 20
    assert demo.seed(session, days=7, now=NOW) == 0  # already there

    user = session.scalar(select(User).where(User.telegram_user_id == demo.DEMO_TELEGRAM_ID))
    calls = session.scalars(select(LlmCall)).all()
    assert calls and all(c.demo for c in calls)
    assert {c.model for c in calls} == {"deepseek-chat", "deepseek-reasoner"}
    assert {c.purpose for c in calls} >= {"intent", "tool_step"}
    statuses = set(session.scalars(select(AgentRun.status).where(AgentRun.user_id == user.id)))
    assert {RunStatus.executed, RunStatus.rejected} <= statuses
    assert session.scalar(select(func.count()).select_from(RunFeedback)) > 0
    assert session.scalar(select(func.count()).select_from(AuditEvent)
                          .where(AuditEvent.event_type == "action_edit_requested")) > 0
    newest = session.scalar(select(func.max(AgentRun.created_at)))
    assert newest <= NOW


def test_demo_remove_only_deletes_demo_data(session, owner) -> None:
    real = AgentRun(user_id=owner.id, intent=Intent.email, status=RunStatus.executed)
    session.add(real)
    session.add(LlmCall(model="deepseek-chat", purpose="intent", status="ok", prompt_tokens=5))
    session.commit()
    demo.seed(session, days=3, now=NOW)

    assert demo.remove(session) > 0
    assert session.scalar(select(func.count()).select_from(AgentRun)) == 1
    assert [c.demo for c in session.scalars(select(LlmCall))] == [False]
    assert session.scalar(select(User).where(User.telegram_user_id == demo.DEMO_TELEGRAM_ID)) is None
    assert demo.remove(session) == 0


def test_api_refuses_to_retry_demo_runs(session, owner, ctx, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from app.api import main as api
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "admin_api_key", "k")
    demo.seed(session, days=10, now=NOW)
    failed = session.scalar(select(AgentRun).where(AgentRun.status == RunStatus.failed))
    assert failed is not None and failed.action_json is not None
    app = api.create_app()
    app.dependency_overrides[api.get_db] = lambda: session
    app.dependency_overrides[api.get_tool_context] = lambda: ctx
    response = TestClient(app).post(f"/runs/{failed.id}/retry", headers={"X-API-Key": "k"})
    assert response.status_code == 409 and "demo" in response.text


def test_a_missing_run_does_not_lose_the_row(session, llm_recorder) -> None:
    llm_recorder(llm_log.LlmCallRecord(model="deepseek-chat", purpose="intent", status="ok",
                                       prompt_tokens=7, run_id=99999))
    [call] = _calls(session)
    assert (call.run_id, call.prompt_tokens) == (None, 7)
