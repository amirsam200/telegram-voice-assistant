"""The per-message context the agent sees. Small and targeted: never the whole history."""

import datetime as dt
import json
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent import fa
from app.db.models import AgentRun, Contact, User


@dataclass
class AgentContext:
    text: str  # the context message for the model
    trusted_emails: set[str] = field(default_factory=set)
    default_duration_min: int = 60


def _contacts_for(session: Session, user_id: int, transcript: str) -> list[dict]:
    """Contacts whose full name or an alias appears as whole words in the transcript."""
    padded = f" {fa.normalize(transcript)} "
    found = []
    for contact in session.scalars(select(Contact).where(Contact.user_id == user_id)):
        names = [fa.normalize(n) for n in [contact.name, *contact.aliases]]
        if any(len(n) >= 2 and f" {n} " in padded for n in names):
            found.append({"name": contact.name, "aliases": contact.aliases, "email": contact.email})
    return found


def build_context(
    session: Session,
    user: User,
    transcript: str,
    now: dt.datetime,
    edit_of: AgentRun | None = None,
) -> AgentContext:
    today = now.date()
    contacts = _contacts_for(session, user.id, transcript)
    trusted = {c["email"].lower() for c in contacts if c["email"]}

    parts = [
        "## Current date and time (Asia/Tehran)",
        f"{now.strftime('%Y-%m-%d %H:%M')}, {fa.weekday_fa(today)}, Jalali {fa.jalali_iso(today)}",
        "",
        "## Coming days (Gregorian | Jalali | weekday)",
        fa.calendar_table(today),
        "",
        "## User",
        json.dumps({"name": user.name, "timezone": user.timezone}, ensure_ascii=False),
        "",
        "## Contacts mentioned in the transcript",
        json.dumps(contacts, ensure_ascii=False) if contacts else "(none matched; use find_contact)",
    ]
    if edit_of is not None and edit_of.action_json:
        parts += [
            "",
            "## The user wants to EDIT this earlier proposal",
            "Apply the requested change and propose the full updated action.",
            json.dumps(edit_of.action_json, ensure_ascii=False),
        ]
        trusted |= {e.lower() for e in _emails_in_action(edit_of.action_json)}
    return AgentContext(text="\n".join(parts), trusted_emails=trusted)


def _emails_in_action(action: dict) -> list[str]:
    emails = [action.get("to")] if action.get("to") else []
    emails += action.get("guests", [])
    emails += [c.get("email") for c in action.get("new_contacts", []) if c.get("email")]
    return [e for e in emails if e]
