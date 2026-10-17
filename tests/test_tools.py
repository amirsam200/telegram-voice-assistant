import pytest
from sqlalchemy import select

from app.db.models import Contact, MemoryFact, ToolCall
from app.tools.registry import TOOLS, ToolError, execute


def test_every_tool_has_a_json_schema() -> None:
    for tool in TOOLS.values():
        schema = tool.schema()
        assert schema["function"]["name"] == tool.name
        assert schema["function"]["parameters"]["type"] == "object"


def test_current_datetime_includes_jalali(ctx) -> None:
    result = execute(ctx, "get_current_datetime", {})
    assert result["date"] == "2026-10-18"
    assert result["jalali_date"] == "1405-07-26"
    assert result["weekday_fa"] == "یکشنبه"


@pytest.mark.parametrize("query", ["علی", "Ali Rezaei", "رضایی", "علي"])
def test_find_contact_by_persian_and_english_names(ctx, query) -> None:
    result = execute(ctx, "find_contact", {"name": query})
    assert [c["email"] for c in result["contacts"]] == ["ali.rezaei@example.com"]


def test_find_unknown_contact_returns_nothing(ctx) -> None:
    assert execute(ctx, "find_contact", {"name": "رضا احمدی"})["contacts"] == []


def test_save_contact_creates_then_adds_alias(ctx, session) -> None:
    assert execute(ctx, "save_contact", {"name": "رضا", "email": "reza@example.com"})["created"] is True
    assert execute(ctx, "save_contact", {"name": "Reza", "email": "reza@example.com"})["created"] is False
    contact = session.scalar(select(Contact).where(Contact.email == "reza@example.com"))
    assert contact.aliases == ["Reza"]


def test_remember_upserts(ctx, session) -> None:
    execute(ctx, "remember", {"key": "usual_meeting_length_min", "value": "30"})
    execute(ctx, "remember", {"key": "usual_meeting_length_min", "value": "45"})
    facts = session.scalars(select(MemoryFact)).all()
    assert [(f.key, f.value) for f in facts] == [("usual_meeting_length_min", "45")]


def test_invalid_arguments_are_rejected_and_logged(ctx, session) -> None:
    with pytest.raises(ToolError):
        execute(ctx, "send_email", {"to": "not-an-email", "subject": "س", "body": "ب"}, idempotency_key="k1")
    call = session.scalar(select(ToolCall))
    assert call.status == "error"
    assert call.latency_ms is not None


def test_side_effect_tools_need_an_idempotency_key(ctx) -> None:
    with pytest.raises(ToolError):
        execute(ctx, "send_email", {"to": "a@example.com", "subject": "س", "body": "ب"})


def test_send_email_runs_once_per_key(ctx, email, session) -> None:
    args = {"to": "sara.mohammadi@example.com", "subject": "گزارش", "body": "سلام"}
    first = execute(ctx, "send_email", args, idempotency_key="run:1:send_email")
    second = execute(ctx, "send_email", args, idempotency_key="run:1:send_email")
    assert first == second
    assert len(email.sent) == 1
    assert email.sent[0]["key"] == "run:1:send_email"
    assert session.scalar(select(ToolCall)).status == "success"


def test_create_event_uses_tehran_time_and_lists_it(ctx, calendar) -> None:
    result = execute(
        ctx,
        "create_calendar_event",
        {"date": "1405-07-27", "time": "۱۰:۳۰", "duration_min": 45, "title": "جلسه بودجه",
         "description": "", "guests": ["ali.rezaei@example.com"]},
        idempotency_key="run:2:create_calendar_event",
    )
    event = calendar.created[result["event_id"]]
    assert event["start"].isoformat() == "2026-10-19T10:30:00+03:30"
    assert (event["end"] - event["start"]).seconds == 45 * 60
    listed = execute(ctx, "list_calendar_events", {"date": "2026-10-19"})
    assert [e["title"] for e in listed["events"]] == ["جلسه بودجه"]
