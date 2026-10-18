"""Tools: JSON schemas for the LLM, Pydantic validation, and a logged executor.

Every call goes through `execute`, which writes a tool_calls row (arguments, result, status,
latency) and honours an idempotency key so side-effecting tools never run twice.
"""

import datetime as dt
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, EmailStr, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent import fa
from app.db.models import Contact, MemoryFact, ToolCall
from app.tools.google_calendar import CalendarClient, CalendarError, event_id_for
from app.tools.mailer import EmailError, get_sender

logger = logging.getLogger(__name__)


class ToolError(RuntimeError):
    """A tool failed; the message is safe to show to the model."""


class ToolInProgress(ToolError):
    pass


EmailSender = Callable[..., str]  # same signature as the providers in app.tools.mailer


@dataclass
class ToolContext:
    session: Session
    user_id: int
    run_id: int | None = None
    now: Callable[[], dt.datetime] = fa.now_tehran
    calendar: CalendarClient | None = None
    send_email: EmailSender | None = None
    # Emails the agent has seen from trusted sources during this run (contact lookups).
    seen_emails: set[str] = field(default_factory=set)


# --- argument models -------------------------------------------------------------------------


class NoArgs(BaseModel):
    pass


class FindContactArgs(BaseModel):
    name: str = Field(min_length=1, max_length=200, description="Name or part of a name, Persian or English.")


class SaveContactArgs(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: EmailStr


class AddContactArgs(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: EmailStr


class DeleteContactArgs(BaseModel):
    contact_id: int


class RememberArgs(BaseModel):
    key: str = Field(min_length=1, max_length=200, description="Short snake_case key.")
    value: str = Field(min_length=1, max_length=2000)


class DateArgs(BaseModel):
    date: dt.date = Field(description="Gregorian YYYY-MM-DD.")

    @field_validator("date", mode="before")
    @classmethod
    def _date(cls, v):
        return fa.parse_date(v) if isinstance(v, str) else v


class CreateEventArgs(BaseModel):
    date: dt.date
    time: dt.time
    duration_min: int = Field(ge=5, le=720)
    title: str = Field(min_length=1, max_length=300)
    description: str = ""
    guests: list[EmailStr] = Field(default_factory=list, max_length=20)

    @field_validator("date", mode="before")
    @classmethod
    def _date(cls, v):
        return fa.parse_date(v) if isinstance(v, str) else v

    @field_validator("time", mode="before")
    @classmethod
    def _time(cls, v):
        return fa.parse_time(v) if isinstance(v, str) else v


class SendEmailArgs(BaseModel):
    to: EmailStr
    subject: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=10000)


# --- implementations -------------------------------------------------------------------------


def get_current_datetime(ctx: ToolContext, args: NoArgs) -> dict:
    now = ctx.now()
    return {
        "datetime": now.isoformat(timespec="minutes"),
        "date": now.date().isoformat(),
        "time": now.strftime("%H:%M"),
        "weekday_fa": fa.weekday_fa(now.date()),
        "jalali_date": fa.jalali_iso(now.date()),
        "timezone": "Asia/Tehran",
    }


def _contact_dict(c: Contact) -> dict:
    return {"name": c.name, "email": c.email, "aliases": c.aliases}


def match_contacts(session: Session, user_id: int, text: str) -> list[Contact]:
    """Contacts whose name or an alias matches the query (either contains the other)."""
    query = fa.normalize(text)
    if not query:
        return []
    found = []
    for contact in session.scalars(select(Contact).where(Contact.user_id == user_id)):
        names = [fa.normalize(n) for n in [contact.name, *contact.aliases] if n]
        if any(n and (n in query or query in n) for n in names):
            found.append(contact)
    return found


def find_contact(ctx: ToolContext, args: FindContactArgs) -> dict:
    contacts = match_contacts(ctx.session, ctx.user_id, args.name)
    for c in contacts:
        if c.email:
            ctx.seen_emails.add(c.email.lower())
    return {"contacts": [_contact_dict(c) for c in contacts]}


def save_contact(ctx: ToolContext, args: SaveContactArgs) -> dict:
    email = str(args.email).lower()
    contact = ctx.session.scalar(
        select(Contact).where(Contact.user_id == ctx.user_id, Contact.email == email)
    )
    if contact is None:
        contact = Contact(user_id=ctx.user_id, name=args.name, email=email, aliases=[])
        ctx.session.add(contact)
        created = True
    else:
        if args.name not in [contact.name, *contact.aliases]:
            contact.aliases = [*contact.aliases, args.name]
        created = False
    ctx.session.flush()
    return {"created": created, "contact": _contact_dict(contact)}


def contact_by_name(session: Session, user_id: int, name: str) -> Contact | None:
    """The contact whose name is exactly this one (after Persian normalisation)."""
    wanted = fa.normalize(name)
    for contact in session.scalars(select(Contact).where(Contact.user_id == user_id).order_by(Contact.id)):
        if fa.normalize(contact.name) == wanted:
            return contact
    return None


def add_contact(ctx: ToolContext, args: AddContactArgs, idempotency_key: str) -> dict:
    """Confirmed by the user: save a contact, or replace the email of the contact with this name."""
    email = str(args.email).lower()
    contact = contact_by_name(ctx.session, ctx.user_id, args.name)
    created = contact is None
    previous = None if created else contact.email
    if created:
        contact = Contact(user_id=ctx.user_id, name=args.name, email=email, aliases=[])
        ctx.session.add(contact)
    else:
        contact.email = email
    ctx.session.flush()
    return {"created": created, "previous_email": previous, "contact": _contact_dict(contact)}


def delete_contact(ctx: ToolContext, args: DeleteContactArgs, idempotency_key: str) -> dict:
    contact = ctx.session.get(Contact, args.contact_id)
    if contact is None or contact.user_id != ctx.user_id:
        raise ToolError("contact not found (it may already be deleted)")
    deleted = _contact_dict(contact)
    ctx.session.delete(contact)
    ctx.session.flush()
    return {"deleted": deleted}


def remember(ctx: ToolContext, args: RememberArgs) -> dict:
    fact = ctx.session.scalar(
        select(MemoryFact).where(MemoryFact.user_id == ctx.user_id, MemoryFact.key == args.key)
    )
    if fact is None:
        fact = MemoryFact(user_id=ctx.user_id, key=args.key, value=args.value, source="agent")
        ctx.session.add(fact)
    else:
        fact.value = args.value
        fact.source = "agent"
    ctx.session.flush()
    return {"key": args.key, "value": args.value}


def _calendar(ctx: ToolContext) -> CalendarClient:
    if ctx.calendar is None:
        from app.tools.google_calendar import GoogleCalendarClient

        ctx.calendar = GoogleCalendarClient()
    return ctx.calendar


def list_calendar_events(ctx: ToolContext, args: DateArgs) -> dict:
    start = dt.datetime.combine(args.date, dt.time(0, 0), tzinfo=fa.TEHRAN)
    events = _calendar(ctx).list_events(start, start + dt.timedelta(days=1))
    return {"date": args.date.isoformat(), "events": events}


def create_calendar_event(ctx: ToolContext, args: CreateEventArgs, idempotency_key: str) -> dict:
    start = dt.datetime.combine(args.date, args.time, tzinfo=fa.TEHRAN)
    return _calendar(ctx).create_event(
        event_id=event_id_for(idempotency_key),
        start=start,
        end=start + dt.timedelta(minutes=args.duration_min),
        title=args.title,
        description=args.description,
        guests=[str(g) for g in args.guests],
    )


def send_email(ctx: ToolContext, args: SendEmailArgs, idempotency_key: str) -> dict:
    sender = ctx.send_email
    if sender is None:
        sender = get_sender()
    html = '<div dir="rtl" style="font-family:Tahoma,sans-serif;white-space:pre-wrap">{}</div>'.format(
        args.body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    message_id = sender(
        to=str(args.to), subject=args.subject, text=args.body, html=html, idempotency_key=idempotency_key
    )
    return {"message_id": message_id}


# --- registry --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    func: Callable[..., dict]
    side_effect: bool = False  # needs user confirmation and an idempotency key

    def schema(self) -> dict:
        params = self.args_model.model_json_schema()
        params.pop("title", None)
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": params},
        }


TOOLS: dict[str, Tool] = {
    t.name: t
    for t in [
        Tool("get_current_datetime", "Current date and time in Asia/Tehran, with the Jalali date.",
             NoArgs, get_current_datetime),
        Tool("find_contact", "Find the user's saved contacts by name or alias (Persian or English).",
             FindContactArgs, find_contact),
        Tool("save_contact", "Save a contact's name and email, or add the name as an alias.",
             SaveContactArgs, save_contact),
        Tool("add_contact", "Save a contact the user confirmed, or replace the email of the contact with that name.",
             AddContactArgs, add_contact, side_effect=True),
        Tool("delete_contact", "Delete a saved contact the user confirmed.",
             DeleteContactArgs, delete_contact, side_effect=True),
        Tool("remember", "Store a preference or fact about the user.", RememberArgs, remember),
        Tool("list_calendar_events", "List the user's calendar events on one day.",
             DateArgs, list_calendar_events),
        Tool("create_calendar_event", "Create a Google Calendar event (Asia/Tehran) and invite guests.",
             CreateEventArgs, create_calendar_event, side_effect=True),
        Tool("send_email", "Send an email from the user (Gmail or Resend, per EMAIL_PROVIDER).",
             SendEmailArgs, send_email, side_effect=True),
    ]
}


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str, ensure_ascii=False))


def execute(
    ctx: ToolContext,
    name: str,
    arguments: dict[str, Any] | str,
    idempotency_key: str | None = None,
) -> dict:
    """Validate, run and log one tool call. Raises ToolError with a model-safe message."""
    tool = TOOLS.get(name)
    if tool is None:
        raise ToolError(f"unknown tool {name!r}")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except json.JSONDecodeError as exc:
            raise ToolError(f"arguments are not valid JSON: {exc}") from exc
    if tool.side_effect and not idempotency_key:
        raise ToolError(f"{name} needs an idempotency key")

    record: ToolCall | None = None
    if idempotency_key:
        record = ctx.session.scalar(select(ToolCall).where(ToolCall.idempotency_key == idempotency_key))
        if record is not None:
            if record.status == "success":
                logger.info("Tool %s already ran for key %s; returning stored result", name, idempotency_key)
                return record.result_json or {}
            if record.status == "running":
                raise ToolInProgress(f"{name} is already running")
    if record is None:
        record = ToolCall(run_id=ctx.run_id, tool_name=name, idempotency_key=idempotency_key)
        ctx.session.add(record)
    record.arguments_json = _jsonable(arguments)
    record.status = "running"
    ctx.session.commit()

    started = time.monotonic()
    try:
        args = tool.args_model.model_validate(arguments)
        if tool.side_effect:
            result = tool.func(ctx, args, idempotency_key)
        else:
            result = tool.func(ctx, args)
    except ValidationError as exc:
        error = f"invalid arguments: {exc.errors(include_url=False)}"
    except (CalendarError, EmailError, ToolError, ValueError) as exc:
        error = str(exc)
    else:
        record.result_json = _jsonable(result)
        record.status = "success"
        record.latency_ms = int((time.monotonic() - started) * 1000)
        ctx.session.commit()
        return result

    ctx.session.rollback()
    record = ctx.session.merge(record)
    record.result_json = {"error": error}
    record.status = "error"
    record.latency_ms = int((time.monotonic() - started) * 1000)
    ctx.session.commit()
    logger.warning("Tool %s failed: %s", name, error)
    raise ToolError(error)
