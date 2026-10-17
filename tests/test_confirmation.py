"""Confirmation: only Confirm executes, and a run executes at most once."""

import datetime as dt
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.agent.executor import cancel, confirm_and_execute, preview
from app.agent.actions import EmailAction, ScheduleAction
from app.db.models import AgentRun, CalendarEvent, Email, Intent, RunStatus
from app.tools.registry import ToolContext
from tests.conftest import NOW

EMAIL = EmailAction(to="sara.mohammadi@example.com", subject="گزارش", body="سلام سارا")
MEETING = ScheduleAction(
    date=dt.date(2026, 10, 19), time=dt.time(10, 0), duration_min=45,
    title="جلسه بودجه", guests=["ali.rezaei@example.com"],
)


def _pending(session, owner, action) -> AgentRun:
    run = AgentRun(user_id=owner.id, intent=Intent(action.type), action_json=action.model_dump(mode="json"),
                   status=RunStatus.pending_confirmation)
    session.add(run)
    session.commit()
    return run


def test_confirm_sends_email_once(session, owner, ctx, email) -> None:
    run = _pending(session, owner, EMAIL)
    first = confirm_and_execute(session, run.id, owner.id, ctx)
    second = confirm_and_execute(session, run.id, owner.id, ctx)

    assert first.ok and "فرستاده شد" in first.message
    assert not second.ok and "قبلاً انجام شده" in second.message
    assert len(email.sent) == 1
    row = session.scalar(select(Email))
    assert (row.status, row.provider_message_id) == ("sent", "msg-1")
    assert session.get(AgentRun, run.id).status == RunStatus.executed


def test_confirm_creates_calendar_event(session, owner, ctx, calendar) -> None:
    run = _pending(session, owner, MEETING)
    result = confirm_and_execute(session, run.id, owner.id, ctx)
    assert result.ok
    (event,) = calendar.created.values()
    assert event["guests"] == ["ali.rezaei@example.com"]
    stored = session.scalar(select(CalendarEvent))
    assert (stored.duration_min, stored.title) == (45, "جلسه بودجه")


def test_double_tap_in_parallel_executes_once(engine, session, owner, calendar, email) -> None:
    run = _pending(session, owner, EMAIL)
    make_session = sessionmaker(bind=engine, expire_on_commit=False)

    def tap(_):
        s = make_session()
        try:
            ctx = ToolContext(session=s, user_id=owner.id, now=lambda: NOW, calendar=calendar, send_email=email)
            return confirm_and_execute(s, run.id, owner.id, ctx).ok
        finally:
            s.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(tap, range(4)))
    assert results.count(True) == 1
    assert len(email.sent) == 1


def test_cancelled_run_never_executes(session, owner, ctx, email) -> None:
    run = _pending(session, owner, EMAIL)
    assert cancel(session, run.id, owner.id)
    result = confirm_and_execute(session, run.id, owner.id, ctx)
    assert not result.ok
    assert email.sent == []


def test_other_users_cannot_confirm(session, owner, ctx, email) -> None:
    run = _pending(session, owner, EMAIL)
    result = confirm_and_execute(session, run.id, owner.id + 1, ctx)
    assert not result.ok
    assert email.sent == []


def test_failed_send_is_reported_in_persian(session, owner, ctx) -> None:
    from app.tools.resend_client import ResendError

    def broken(**kwargs):
        raise ResendError("Resend returned 500")

    ctx.send_email = broken
    run = _pending(session, owner, EMAIL)
    result = confirm_and_execute(session, run.id, owner.id, ctx)
    assert not result.ok and "ناموفق" in result.message
    assert session.get(AgentRun, run.id).status == RunStatus.failed
    assert session.scalar(select(Email)).status == "failed"


def test_preview_is_persian_with_jalali_date() -> None:
    text = preview(MEETING)
    assert "دوشنبه ۲۷ مهر ۱۴۰۵" in text
    assert "۱۰:۰۰ تا ۱۰:۴۵" in text
    assert "ali.rezaei@example.com" in text
