"""Token budgets: usage windows (Tehran days and months), alerts once per threshold, hard stop."""

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anyio
import pytest
from sqlalchemy import func, select

from app import budget
from app.agent.fa import TEHRAN
from app.bot import main as bot
from app.config import get_settings
from app.db.models import BudgetAlert, LlmCall
from tests.conftest import NOW  # Sunday 2026-10-18 10:00 Tehran


@pytest.fixture
def limits(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "daily_token_budget", 1000)
    monkeypatch.setattr(s, "monthly_token_budget", 10_000)
    monkeypatch.setattr(s, "budget_hard_stop", False)
    return s


def _call(session, tokens: int, when: dt.datetime, demo: bool = False, status: str = "ok") -> None:
    session.add(LlmCall(model="deepseek-chat", purpose="intent", status=status, prompt_tokens=tokens - tokens // 4,
                        completion_tokens=tokens // 4, demo=demo, created_at=when))
    session.commit()


def _alerts(session) -> list[tuple[str, int]]:
    return [(a.period, a.threshold) for a in session.scalars(select(BudgetAlert).order_by(BudgetAlert.id))]


def test_usage_counts_tehran_day_and_month_without_demo_rows(session, limits) -> None:
    _call(session, 300, NOW - dt.timedelta(hours=2))                       # today 08:00
    _call(session, 200, dt.datetime(2026, 10, 18, 0, 5, tzinfo=TEHRAN))    # today, just after midnight
    _call(session, 400, dt.datetime(2026, 10, 17, 23, 55, tzinfo=TEHRAN))  # yesterday, same month
    _call(session, 900, dt.datetime(2026, 9, 30, 12, 0, tzinfo=TEHRAN))    # last month
    _call(session, 5000, NOW - dt.timedelta(minutes=5), demo=True)         # demo: never counted

    day, month = budget.statuses(session, NOW)
    assert (day.key, day.used, day.budget) == ("day:2026-10-18", 500, 1000)
    assert (month.key, month.used) == ("month:2026-10", 900)
    assert day.percent == pytest.approx(50.0)


def test_disabled_budget_is_left_out(session, limits, monkeypatch) -> None:
    monkeypatch.setattr(limits, "daily_token_budget", 0)
    assert [b.period for b in budget.statuses(session, NOW)] == ["month"]


def test_alerts_once_at_80_and_100_percent(session, limits) -> None:
    sent: list[str] = []
    _call(session, 700, NOW - dt.timedelta(hours=1))
    assert budget.check_and_alert(session, NOW, sent.append) == []

    _call(session, 150, NOW - dt.timedelta(minutes=30))  # 850 / 1000
    assert budget.check_and_alert(session, NOW, sent.append) == [("day:2026-10-18", 80)]
    assert budget.check_and_alert(session, NOW, sent.append) == []  # not again

    _call(session, 200, NOW - dt.timedelta(minutes=10))  # 1050 / 1000
    assert budget.check_and_alert(session, NOW, sent.append) == [("day:2026-10-18", 100)]
    assert len(sent) == 2
    assert "۸۰٪" in sent[0] and "۱٬۰۰۰" in sent[0]
    assert "۱۰۰٪" in sent[1] and "توقف خودکار خاموش" in sent[1]

    tomorrow = NOW + dt.timedelta(days=1)
    _call(session, 900, tomorrow - dt.timedelta(minutes=1))
    assert budget.check_and_alert(session, tomorrow, sent.append) == [("day:2026-10-19", 80)]


def test_jumping_past_both_thresholds_sends_both(session, limits) -> None:
    sent: list[str] = []
    _call(session, 1200, NOW - dt.timedelta(minutes=1))
    assert budget.check_and_alert(session, NOW, sent.append) == [("day:2026-10-18", 80), ("day:2026-10-18", 100)]


def test_failed_send_is_retried_on_the_next_call(session, limits) -> None:
    _call(session, 850, NOW - dt.timedelta(minutes=1))

    def offline(text):
        raise RuntimeError("Telegram sendMessage returned 502")

    assert budget.check_and_alert(session, NOW, offline) == []
    assert _alerts(session) == []  # claim released
    sent: list[str] = []
    assert budget.check_and_alert(session, NOW, sent.append) == [("day:2026-10-18", 80)]


def test_monthly_budget_alerts_independently(session, limits, monkeypatch) -> None:
    monkeypatch.setattr(limits, "daily_token_budget", 0)
    for day in range(1, 10):
        _call(session, 1000, dt.datetime(2026, 10, day, 12, tzinfo=TEHRAN))  # 9000 / 10000
    assert budget.check_and_alert(session, NOW, lambda t: None) == [("month:2026-10", 80)]


def test_hard_stop_only_when_enabled_and_exhausted(session, limits, monkeypatch) -> None:
    _call(session, 1000, NOW - dt.timedelta(minutes=1))
    assert not budget.is_blocked(session, NOW)  # hard stop off
    monkeypatch.setattr(limits, "budget_hard_stop", True)
    assert budget.is_blocked(session, NOW)
    assert "ربات درخواست تازه نمی‌پذیرد" in budget.alert_text(budget.statuses(session, NOW)[0], 100)
    monkeypatch.setattr(limits, "daily_token_budget", 5000)
    assert not budget.is_blocked(session, NOW)


def test_bot_refuses_new_requests_after_hard_stop(limits, monkeypatch) -> None:
    monkeypatch.setattr(limits, "budget_hard_stop", True)
    monkeypatch.setattr(bot, "_budget_blocked", lambda: True)
    message = SimpleNamespace(reply_text=AsyncMock())
    assert anyio.run(bot._refuse_if_over_budget, message)
    assert "بودجه" in message.reply_text.call_args.args[0]

    monkeypatch.setattr(limits, "budget_hard_stop", False)
    assert not anyio.run(bot._refuse_if_over_budget, SimpleNamespace(reply_text=AsyncMock()))


def test_recorder_hook_runs_the_budget_check(session, limits, engine, monkeypatch) -> None:
    from sqlalchemy.orm import sessionmaker

    from app.agent import llm_log

    sent: list[str] = []
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def hook(record):
        if record.status == "ok":
            with factory() as s:
                budget.check_and_alert(s, send=sent.append)

    recorder = llm_log.DbRecorder(factory, after=[hook])
    recorder(llm_log.LlmCallRecord(model="deepseek-chat", purpose="intent", status="ok",
                                   prompt_tokens=700, completion_tokens=200))
    assert len(sent) == 1 and session.scalar(select(func.count()).select_from(BudgetAlert)) == 1


def test_alert_message_never_contains_the_token_url(limits, monkeypatch) -> None:
    import httpx

    monkeypatch.setattr(limits, "telegram_bot_token", "123:SECRET")
    monkeypatch.setattr(limits, "allowed_user_ids", "1001")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: SimpleNamespace(status_code=403))
    with pytest.raises(RuntimeError) as exc:
        budget.send_telegram("x")
    assert "SECRET" not in str(exc.value)
