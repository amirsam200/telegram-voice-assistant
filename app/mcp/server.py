"""MCP server exposing the agent's tools to MCP clients (Claude Desktop, IDEs).

One implementation, two interfaces: every tool goes through app.tools.registry.execute, exactly
like the agent, so arguments are validated and every call is logged in tool_calls.

Write tools (create_calendar_event, send_email) do nothing unless confirm=true; without it they
return a preview. Every write attempt, refused or executed, is recorded in audit_events.
The tools act for the owner (the first ID in ALLOWED_USER_IDS).
"""

import json
import uuid
from collections.abc import Callable
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent import fa
from app.config import get_settings
from app.db.models import AuditEvent, User
from app.tools.google_calendar import CalendarClient
from app.tools.registry import EmailSender, ToolContext, ToolError, execute, upcoming_events

INSTRUCTIONS = (
    "Tools of a Persian voice assistant for its owner: contacts, Google Calendar (Asia/Tehran) and email. "
    "create_calendar_event and send_email only act with confirm=true: call them first without it, show the "
    "preview to the user, and repeat the call with confirm=true only after the user agrees."
)
UPCOMING_URI = "calendar://upcoming"


class McpToolError(RuntimeError):
    """Shown to the MCP client as a tool error."""


def create_server(
    session_factory: Callable[[], Session] | None = None,
    calendar: CalendarClient | None = None,
    send_email: EmailSender | None = None,
    now: Callable | None = None,
    owner_telegram_id: int | None = None,
) -> FastMCP:
    """Build the server. Tests pass fakes; by default it uses the real database, calendar and email."""
    if session_factory is None:
        from app.db.session import SessionLocal

        session_factory = SessionLocal
    clock = now or fa.now_tehran
    server = FastMCP("voice-assistant", instructions=INSTRUCTIONS)

    def _context(session: Session) -> ToolContext:
        owner_tg = owner_telegram_id if owner_telegram_id is not None else get_settings().owner_telegram_id
        owner = session.scalar(select(User).where(User.telegram_user_id == owner_tg)) if owner_tg else None
        if owner is None:
            raise McpToolError("No owner user: set ALLOWED_USER_IDS and run the seed.")
        return ToolContext(session=session, user_id=owner.id, now=clock, calendar=calendar, send_email=send_email)

    def _audit(session: Session, event: str, payload: dict[str, Any]) -> None:
        session.add(AuditEvent(event_type=event, actor="mcp", payload_json=payload))
        session.commit()

    def _read(name: str, args: dict[str, Any]) -> dict:
        with session_factory() as session:
            try:
                return execute(_context(session), name, args)
            except ToolError as exc:
                raise McpToolError(str(exc)) from exc

    def _write(name: str, args: dict[str, Any], confirm: bool, request_id: str | None) -> dict:
        with session_factory() as session:
            ctx = _context(session)
            if not confirm:
                _audit(session, "mcp_write_refused", {"tool": name, "arguments": args})
                return {
                    "executed": False,
                    "preview": {"tool": name, "arguments": args},
                    "message": "Nothing was done. Show this preview to the user and call again with "
                               "confirm=true only if they agree.",
                }
            # Same request_id = same call: a retry returns the stored result instead of acting twice.
            key = f"mcp:{name}:{request_id or uuid.uuid4().hex}"
            try:
                result = execute(ctx, name, args, idempotency_key=key)
            except ToolError as exc:
                _audit(session, "mcp_write_failed", {"tool": name, "arguments": args, "error": str(exc)})
                raise McpToolError(str(exc)) from exc
            _audit(session, "mcp_write_executed",
                   {"tool": name, "arguments": args, "idempotency_key": key, "result": result})
            return {"executed": True, "result": result}

    Confirm = Annotated[bool, Field(description="Must be true to act. Without it the call only returns a preview.")]
    RequestId = Annotated[str | None, Field(description="Optional id; repeating it never acts twice.")]

    @server.tool()
    def find_contact(name: Annotated[str, Field(description="Name or part of a name, Persian or English.")]) -> dict:
        """Find the owner's saved contacts by name or alias."""
        return _read("find_contact", {"name": name})

    @server.tool()
    def list_calendar_events(date: Annotated[str, Field(description="YYYY-MM-DD (Gregorian or Jalali).")]) -> dict:
        """List the owner's Google Calendar events on one day (Asia/Tehran)."""
        return _read("list_calendar_events", {"date": date})

    @server.tool()
    def create_calendar_event(
        date: Annotated[str, Field(description="YYYY-MM-DD (Gregorian or Jalali).")],
        time: Annotated[str, Field(description="Start time HH:MM, 24h, Asia/Tehran.")],
        duration_min: Annotated[int, Field(ge=5, le=720)],
        title: str,
        description: str = "",
        guests: Annotated[list[str] | None, Field(description="Guest email addresses.")] = None,
        confirm: Confirm = False,
        request_id: RequestId = None,
    ) -> dict:
        """Create an event in the owner's Google Calendar and invite the guests. Needs confirm=true."""
        args = {"date": date, "time": time, "duration_min": duration_min, "title": title,
                "description": description, "guests": guests or []}
        return _write("create_calendar_event", args, confirm, request_id)

    # Named send_email for clients; the Python name must not shadow the send_email parameter above.
    @server.tool(name="send_email")
    def send_email_tool(
        to: Annotated[str, Field(description="One recipient email address.")],
        subject: str,
        body: str,
        confirm: Confirm = False,
        request_id: RequestId = None,
    ) -> dict:
        """Send one email from the owner. Needs confirm=true."""
        return _write("send_email", {"to": to, "subject": subject, "body": body}, confirm, request_id)

    @server.resource(UPCOMING_URI, name="upcoming_events", mime_type="application/json",
                     description="The owner's calendar events for the next 7 days.")
    def upcoming() -> str:
        with session_factory() as session:
            ctx = _context(session)
            try:
                events = upcoming_events(ctx, days=7)
            except Exception as exc:
                raise McpToolError(f"could not read the calendar: {exc}") from exc
            return json.dumps({"from": ctx.now().isoformat(timespec="minutes"), "events": events},
                              ensure_ascii=False)

    return server
