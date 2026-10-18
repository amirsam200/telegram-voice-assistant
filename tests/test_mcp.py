"""MCP server through a real MCP client session (in memory) with fake calendar and email."""

import json
from typing import Any

import anyio
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.db.models import AuditEvent, ToolCall
from app.mcp.server import UPCOMING_URI, create_server
from tests.conftest import NOW


@pytest.fixture
def server(engine, owner, calendar, email):
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    return create_server(session_factory=factory, calendar=calendar, send_email=email, now=lambda: NOW,
                         owner_telegram_id=owner.telegram_user_id)


def _client(server, coro_fn):
    """Run coro_fn(client_session) against the server and return its result."""
    async def main():
        async with create_connected_server_and_client_session(server._mcp_server) as client:
            return await coro_fn(client)
    return anyio.run(main)


def call(server, name: str, args: dict[str, Any]):
    result = _client(server, lambda c: c.call_tool(name, args))
    text = result.content[0].text if result.content else ""
    return result.isError, (text if result.isError else json.loads(text))


def _audit(session) -> list[str]:
    session.expire_all()
    return [e.event_type for e in session.scalars(select(AuditEvent).order_by(AuditEvent.id))]


MEETING = {"date": "2026-10-19", "time": "10:30", "duration_min": 45, "title": "جلسه بودجه",
           "guests": ["ali.rezaei@example.com"]}
EMAIL = {"to": "sara.mohammadi@example.com", "subject": "گزارش", "body": "سلام سارا"}


def test_lists_the_four_tools_and_the_resource(server) -> None:
    tools = _client(server, lambda c: c.list_tools()).tools
    assert {t.name for t in tools} == {"find_contact", "list_calendar_events", "create_calendar_event", "send_email"}
    send = next(t for t in tools if t.name == "send_email")
    assert "confirm" in send.inputSchema["properties"]
    resources = _client(server, lambda c: c.list_resources()).resources
    assert [str(r.uri) for r in resources] == [UPCOMING_URI]


def test_find_contact_reuses_the_agent_tool_and_is_logged(server, session) -> None:
    error, body = call(server, "find_contact", {"name": "سارا"})
    assert not error
    assert [c["email"] for c in body["contacts"]] == ["sara.mohammadi@example.com"]
    assert session.scalar(select(ToolCall)).tool_name == "find_contact"


@pytest.mark.parametrize("tool, args", [("create_calendar_event", MEETING), ("send_email", EMAIL)])
def test_write_tools_without_confirm_only_preview(server, session, calendar, email, tool, args) -> None:
    error, body = call(server, tool, args)
    assert not error
    assert body["executed"] is False and body["preview"]["tool"] == tool
    assert calendar.created == {} and email.sent == []
    assert session.scalar(select(ToolCall)) is None
    assert _audit(session) == ["mcp_write_refused"]


def test_send_email_with_confirm_sends_once_and_is_audited(server, session, email) -> None:
    error, body = call(server, "send_email", {**EMAIL, "confirm": True, "request_id": "abc"})
    assert not error and body["executed"] is True and body["result"]["message_id"] == "msg-1"
    again_error, again = call(server, "send_email", {**EMAIL, "confirm": True, "request_id": "abc"})
    assert not again_error and again["result"] == body["result"]
    assert len(email.sent) == 1  # same request_id never sends twice
    assert email.sent[0]["key"] == "mcp:send_email:abc"
    assert _audit(session) == ["mcp_write_executed", "mcp_write_executed"]
    audit = session.scalars(select(AuditEvent)).first()
    assert audit.actor == "mcp" and audit.payload_json["arguments"]["to"] == "sara.mohammadi@example.com"


def test_create_event_with_confirm_uses_tehran_time(server, session, calendar) -> None:
    error, body = call(server, "create_calendar_event", {**MEETING, "confirm": True})
    assert not error and body["executed"]
    event = calendar.created[body["result"]["event_id"]]
    assert event["start"].isoformat() == "2026-10-19T10:30:00+03:30"
    assert event["guests"] == ["ali.rezaei@example.com"]
    error, listed = call(server, "list_calendar_events", {"date": "2026-10-19"})
    assert [e["title"] for e in listed["events"]] == ["جلسه بودجه"]


def test_invalid_write_is_an_error_and_audited(server, session, email) -> None:
    error, text = call(server, "send_email", {**EMAIL, "to": "not-an-email", "confirm": True})
    assert error and "invalid arguments" in text
    assert email.sent == []
    assert _audit(session) == ["mcp_write_failed"]


def test_failed_provider_is_reported(server, session) -> None:
    from app.tools.gmail_client import GmailError

    def broken(**kwargs):
        raise GmailError("Gmail returned 500: backend error")

    srv = create_server(session_factory=sessionmaker(bind=session.get_bind(), expire_on_commit=False),
                        send_email=broken, now=lambda: NOW, owner_telegram_id=1001)
    error, text = call(srv, "send_email", {**EMAIL, "confirm": True})
    assert error and "Gmail returned 500" in text


def test_upcoming_resource_lists_the_next_week(server, calendar) -> None:
    call(server, "create_calendar_event", {**MEETING, "confirm": True})
    call(server, "create_calendar_event", {**MEETING, "date": "2026-11-30", "title": "دور", "confirm": True})
    result = _client(server, lambda c: c.read_resource(UPCOMING_URI))
    body = json.loads(result.contents[0].text)
    assert [e["title"] for e in body["events"]] == ["جلسه بودجه"]


def test_no_owner_is_an_error(engine, session, calendar, email) -> None:
    srv = create_server(session_factory=sessionmaker(bind=engine), calendar=calendar, send_email=email,
                        now=lambda: NOW, owner_telegram_id=424242)
    error, text = call(srv, "find_contact", {"name": "سارا"})
    assert error and "No owner user" in text
