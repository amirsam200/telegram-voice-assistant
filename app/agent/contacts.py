"""Contact management from Telegram: parse commands, build actions and propose them.

Adding or deleting a contact goes through the same pending run + Confirm flow as every other action.
"""

from dataclasses import dataclass

from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent import fa
from app.agent.actions import AddContactAction, ContactAction, DeleteContactAction
from app.agent.executor import preview
from app.db.models import AgentRun, Contact, Intent, RunStatus
from app.tools.registry import contact_by_name

ADD_USAGE = "روش استفاده: /addcontact نام ایمیل\nمثال: /addcontact رضا احمدی reza@example.com"
DEL_USAGE = "روش استفاده: /delcontact نام (یا ایمیل مخاطب)\nمثال: /delcontact رضا احمدی"
BAD_EMAIL = "«{}» یک ایمیل معتبر نیست. لطفاً نشانی را درست بنویسید، مثلاً name@example.com"
NO_CONTACTS = "هنوز مخاطبی ذخیره نشده است. با /addcontact نام ایمیل یکی اضافه کنید."
NOT_FOUND = "مخاطبی با نام «{}» پیدا نشد. فهرست مخاطبان: /contacts"
AMBIGUOUS = "چند مخاطب با «{}» پیدا شد. لطفاً نام کامل یا ایمیل را بنویسید:\n{}"

_EMAIL = TypeAdapter(EmailStr)


class CommandError(ValueError):
    """The command is malformed; the message is Persian and shown to the user."""


def valid_email(text: str) -> str:
    try:
        return str(_EMAIL.validate_python(text.strip())).lower()
    except ValidationError as exc:
        raise CommandError(BAD_EMAIL.format(text.strip())) from exc


def parse_add(args: list[str]) -> AddContactAction:
    """/addcontact <name...> <email>: the last word is the email, the rest is the name."""
    words = [w for w in args if w.strip()]
    if len(words) < 2:
        raise CommandError(ADD_USAGE)
    email = valid_email(words[-1])
    return AddContactAction(name=" ".join(words[:-1]), email=email)


def with_previous_email(session: Session, user_id: int, action: AddContactAction) -> AddContactAction:
    """Note the current email when this name is already saved, so the preview says it will change."""
    existing = contact_by_name(session, user_id, action.name)
    previous = existing.email if existing is not None else None
    return action.model_copy(update={"previous_email": previous})


def _line(contact: Contact) -> str:
    return f"• {contact.name} — {contact.email or 'بدون ایمیل'}"


def resolve_delete(session: Session, user_id: int, args: list[str]) -> DeleteContactAction:
    """/delcontact <name or email>: exact name, alias or email first, then a unique partial match."""
    query = " ".join(w for w in args if w.strip()).strip()
    if not query:
        raise CommandError(DEL_USAGE)
    wanted = fa.normalize(query)
    contacts = session.scalars(select(Contact).where(Contact.user_id == user_id).order_by(Contact.id)).all()
    exact = [
        c for c in contacts
        if wanted in {fa.normalize(n) for n in [c.name, *c.aliases] if n} or (c.email or "").lower() == wanted
    ]
    matches = exact or [c for c in contacts if wanted in fa.normalize(c.name)]
    if not matches:
        raise CommandError(NOT_FOUND.format(query))
    if len(matches) > 1:
        raise CommandError(AMBIGUOUS.format(query, "\n".join(_line(c) for c in matches)))
    contact = matches[0]
    return DeleteContactAction(contact_id=contact.id, name=contact.name, email=contact.email)


def list_text(session: Session, user_id: int) -> str:
    contacts = session.scalars(select(Contact).where(Contact.user_id == user_id).order_by(Contact.name)).all()
    if not contacts:
        return NO_CONTACTS
    return f"👥 مخاطبان ({fa.to_fa_digits(len(contacts))}):\n" + "\n".join(_line(c) for c in contacts)


@dataclass
class Proposal:
    run_id: int
    preview: str


def propose(session: Session, user_id: int, action: ContactAction, input_text: str) -> Proposal:
    """Store the action as a run waiting for Confirm, like an agent proposal (but without the LLM)."""
    if isinstance(action, AddContactAction):
        action = with_previous_email(session, user_id, action)
    text = preview(action)
    run = AgentRun(
        user_id=user_id,
        input_text=input_text,
        intent=Intent.contact,
        action_json=action.model_dump(mode="json"),
        status=RunStatus.pending_confirmation,
        reply_text=text,
        steps=0,
    )
    session.add(run)
    session.commit()
    return Proposal(run_id=run.id, preview=text)
