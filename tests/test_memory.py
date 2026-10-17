"""Memory: small per-message context, and facts saved through tools after an executed action."""

import datetime as dt

import pytest
from sqlalchemy import select

from app.agent.actions import EmailAction, ScheduleAction
from app.agent.agent import run_agent
from app.agent.context import build_context
from app.agent.executor import confirm_and_execute
from app.config import get_settings
from app.db.models import AgentRun, Contact, Intent, MemoryFact, RunStatus, ToolCall
from tests.conftest import NOW
from tests.test_agent import ScriptedProvider


def _executed(session, owner, ctx, action) -> AgentRun:
    run = AgentRun(user_id=owner.id, intent=Intent(action.type), action_json=action.model_dump(mode="json"),
                   status=RunStatus.pending_confirmation)
    session.add(run)
    session.commit()
    assert confirm_and_execute(session, run.id, owner.id, ctx).ok
    return run


def test_context_has_preferences_and_only_last_five_messages(session, owner) -> None:
    session.add(MemoryFact(user_id=owner.id, key="usual_meeting_length_min", value="45", source="agent"))
    for i in range(7):
        session.add(AgentRun(user_id=owner.id, intent=Intent.unknown, status=RunStatus.clarification,
                             input_text=f"پیام شماره {i}", reply_text=f"پاسخ {i}"))
    session.commit()

    text = build_context(session, owner, "سلام", NOW).text
    assert '"usual_meeting_length_min": "45"' in text
    assert "پیام شماره 0" not in text and "پیام شماره 1" not in text
    assert all(f"پیام شماره {i}" in text for i in range(2, 7))


def test_new_contact_is_saved_after_email_is_sent(session, owner, ctx) -> None:
    action = EmailAction(to="reza@example.com", subject="فایل‌ها", body="سلام رضا",
                         new_contacts=[{"name": "رضا کریمی", "email": "reza@example.com"}])
    run = _executed(session, owner, ctx, action)

    contact = session.scalar(select(Contact).where(Contact.email == "reza@example.com"))
    assert contact.name == "رضا کریمی"
    tools = session.scalars(select(ToolCall.tool_name).where(ToolCall.run_id == run.id)).all()
    assert tools == ["send_email", "save_contact"]


def test_usual_meeting_length_is_learned(session, owner, ctx) -> None:
    for day, minutes in [(19, 60), (20, 30), (21, 30)]:
        _executed(session, owner, ctx, ScheduleAction(
            date=dt.date(2026, 10, day), time=dt.time(10, 0), duration_min=minutes, title="جلسه"))
    fact = session.scalar(select(MemoryFact).where(MemoryFact.key == "usual_meeting_length_min"))
    assert fact.value == "30"


def test_email_from_an_earlier_message_counts_as_the_users_words(session, owner, ctx) -> None:
    session.add(AgentRun(user_id=owner.id, intent=Intent.unknown, status=RunStatus.clarification,
                         input_text="ایمیلش reza@example.com هست", reply_text="باشه"))
    session.commit()
    provider = ScriptedProvider([("propose_email", {"to": "reza@example.com", "subject": "قرارداد",
                                                    "body": "قرارداد آماده است."})])
    result = run_agent(session, owner, "حالا به رضا بگو قرارداد آماده است", provider, ctx)
    assert isinstance(result.action, EmailAction)


@pytest.mark.llm
@pytest.mark.skipif(not get_settings().deepseek_api_key, reason="DEEPSEEK_API_KEY not set")
def test_usual_meeting_length_is_used_as_default(session, owner, ctx) -> None:
    from app.agent.provider import DeepSeekProvider

    session.add(MemoryFact(user_id=owner.id, key="usual_meeting_length_min", value="45", source="agent"))
    session.commit()
    result = run_agent(session, owner, "فردا ساعت نه صبح با علی جلسه بذار", DeepSeekProvider(), ctx)
    assert isinstance(result.action, ScheduleAction), result.question
    assert result.action.duration_min == 45
