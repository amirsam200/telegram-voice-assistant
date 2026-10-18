"""Contact management: command parsing, previews, Confirm-only execution and the agent's propose_contact."""

import pytest
from sqlalchemy import select

from app.agent import contacts
from app.agent.actions import AddContactAction, DeleteContactAction, intent_of
from app.agent.agent import run_agent
from app.agent.executor import cancel, confirm_and_execute, preview
from app.db.models import AgentRun, Contact, Intent, RunStatus, ToolCall
from tests.test_agent import ScriptedProvider


def _emails(session) -> dict[str, str]:
    return {c.name: c.email for c in session.scalars(select(Contact))}


# --- /addcontact ---------------------------------------------------------------------------------


def test_parse_add_takes_last_word_as_email_and_lowercases_it() -> None:
    action = contacts.parse_add(["رضا", "احمدی", "Reza.Ahmadi@Example.com"])
    assert (action.name, action.email) == ("رضا احمدی", "reza.ahmadi@example.com")


@pytest.mark.parametrize("args", [[], ["رضا"], ["reza@example.com"]])
def test_parse_add_needs_name_and_email(args) -> None:
    with pytest.raises(contacts.CommandError, match="/addcontact"):
        contacts.parse_add(args)


@pytest.mark.parametrize("bad", ["reza", "reza@", "reza@example", "@example.com", "reza example.com"])
def test_parse_add_rejects_invalid_email(bad) -> None:
    with pytest.raises(contacts.CommandError, match="ایمیل معتبر نیست"):
        contacts.parse_add(["رضا", bad])


def test_add_runs_only_after_confirm(session, owner, ctx) -> None:
    proposal = contacts.propose(session, owner.id, contacts.parse_add(["رضا", "reza@example.com"]), "/addcontact")

    assert "افزودن مخاطب" in proposal.preview and "reza@example.com" in proposal.preview
    run = session.get(AgentRun, proposal.run_id)
    assert (run.status, run.intent) == (RunStatus.pending_confirmation, Intent.contact)
    assert "رضا" not in _emails(session)  # nothing saved before Confirm

    result = confirm_and_execute(session, proposal.run_id, owner.id, ctx)
    again = confirm_and_execute(session, proposal.run_id, owner.id, ctx)
    assert result.ok and "ذخیره شد" in result.message
    assert not again.ok
    assert _emails(session)["رضا"] == "reza@example.com"
    assert session.get(AgentRun, proposal.run_id).status == RunStatus.executed
    assert [c.tool_name for c in session.scalars(select(ToolCall))] == ["add_contact"]


def test_cancelled_add_saves_nothing(session, owner, ctx) -> None:
    proposal = contacts.propose(session, owner.id, contacts.parse_add(["رضا", "reza@example.com"]), "/addcontact")
    assert cancel(session, proposal.run_id, owner.id)
    assert not confirm_and_execute(session, proposal.run_id, owner.id, ctx).ok
    assert "رضا" not in _emails(session)


def test_adding_an_existing_name_updates_its_email(session, owner, ctx) -> None:
    action = contacts.parse_add(["سارا", "محمدی", "sara.new@example.com"])
    proposal = contacts.propose(session, owner.id, action, "/addcontact")

    assert "تغییر ایمیل مخاطب" in proposal.preview
    assert "sara.mohammadi@example.com" in proposal.preview  # current address shown
    result = confirm_and_execute(session, proposal.run_id, owner.id, ctx)
    assert result.ok and "تغییر کرد" in result.message
    assert _emails(session)["سارا محمدی"] == "sara.new@example.com"
    assert session.scalar(select(Contact).where(Contact.name == "سارا محمدی")) is not None
    assert len(_emails(session)) == 2  # updated, not duplicated


# --- /delcontact ---------------------------------------------------------------------------------


@pytest.mark.parametrize("args", [["علی", "رضایی"], ["Ali", "Rezaei"], ["ali.rezaei@example.com"], ["رضایی"]])
def test_resolve_delete_by_name_alias_email_or_unique_part(session, owner, args) -> None:
    action = contacts.resolve_delete(session, owner.id, args)
    assert (action.name, action.email) == ("علی رضایی", "ali.rezaei@example.com")


def test_resolve_delete_unknown_and_ambiguous(session, owner) -> None:
    with pytest.raises(contacts.CommandError, match="پیدا نشد"):
        contacts.resolve_delete(session, owner.id, ["رضا", "احمدی"])
    session.add(Contact(user_id=owner.id, name="علی کریمی", aliases=[], email="ali.karimi@example.com"))
    session.commit()
    with pytest.raises(contacts.CommandError, match="چند مخاطب") as exc:
        contacts.resolve_delete(session, owner.id, ["عل"])
    assert "ali.karimi@example.com" in str(exc.value) and "ali.rezaei@example.com" in str(exc.value)
    with pytest.raises(contacts.CommandError, match="/delcontact"):
        contacts.resolve_delete(session, owner.id, [])


def test_delete_runs_only_after_confirm(session, owner, ctx) -> None:
    proposal = contacts.propose(session, owner.id, contacts.resolve_delete(session, owner.id, ["علی"]), "/delcontact")

    assert "حذف مخاطب" in proposal.preview and "ali.rezaei@example.com" in proposal.preview
    assert "علی رضایی" in _emails(session)
    result = confirm_and_execute(session, proposal.run_id, owner.id, ctx)
    assert result.ok and "حذف شد" in result.message
    assert "علی رضایی" not in _emails(session)


def test_delete_of_an_already_deleted_contact_fails_in_persian(session, owner, ctx) -> None:
    first = contacts.propose(session, owner.id, contacts.resolve_delete(session, owner.id, ["علی"]), "/delcontact")
    second = contacts.propose(session, owner.id, contacts.resolve_delete(session, owner.id, ["علی"]), "/delcontact")
    assert confirm_and_execute(session, first.run_id, owner.id, ctx).ok
    result = confirm_and_execute(session, second.run_id, owner.id, ctx)
    assert not result.ok and "پیدا نشد" in result.message
    assert session.get(AgentRun, second.run_id).status == RunStatus.failed


def test_other_users_cannot_confirm_a_contact_run(session, owner, ctx) -> None:
    proposal = contacts.propose(session, owner.id, contacts.resolve_delete(session, owner.id, ["سارا"]), "/delcontact")
    assert not confirm_and_execute(session, proposal.run_id, owner.id + 1, ctx).ok
    assert "سارا محمدی" in _emails(session)


# --- /contacts -----------------------------------------------------------------------------------


def test_list_text(session, owner) -> None:
    text = contacts.list_text(session, owner.id)
    assert "مخاطبان (۲)" in text
    assert "• سارا محمدی — sara.mohammadi@example.com" in text
    assert contacts.list_text(session, owner.id + 1) == contacts.NO_CONTACTS


# --- voice / text request ------------------------------------------------------------------------


def test_agent_proposes_a_spoken_contact(session, owner, ctx) -> None:
    provider = ScriptedProvider([("propose_contact", {"name": "رضا احمدی", "email": "Reza.Ahmadi@gmail.com"})])
    result = run_agent(session, owner, "رضا احمدی را با ایمیل رضا نقطه احمدی ات جیمیل دات کام اضافه کن",
                       provider, ctx)

    assert isinstance(result.action, AddContactAction)
    assert result.action.email == "reza.ahmadi@gmail.com"
    run = session.get(AgentRun, result.run_id)
    assert (run.status, run.intent) == (RunStatus.pending_confirmation, Intent.contact)
    assert "رضا احمدی" not in _emails(session)  # only proposed
    assert confirm_and_execute(session, run.id, owner.id, ctx).ok
    assert _emails(session)["رضا احمدی"] == "reza.ahmadi@gmail.com"


def test_agent_rejects_an_invalid_email_and_asks(session, owner, ctx) -> None:
    provider = ScriptedProvider(
        [("propose_contact", {"name": "رضا", "email": "reza-at-gmail"})],
        [("ask_clarification", {"question": "لطفاً ایمیل رضا را دوباره و حرف به حرف بگویید."})],
    )
    result = run_agent(session, owner, "رضا رو اضافه کن", provider, ctx)
    assert result.action is None and "ایمیل" in result.question
    tool_reply = provider.seen_messages[1][-1]["content"]
    assert "invalid propose_contact" in tool_reply


def test_agent_contact_for_an_existing_name_previews_the_change(session, owner, ctx) -> None:
    provider = ScriptedProvider([("propose_contact", {"name": "علی رضایی", "email": "ali@company.com"})])
    result = run_agent(session, owner, "ایمیل علی رضایی را عوض کن", provider, ctx)
    assert result.action.previous_email == "ali.rezaei@example.com"
    assert "ایمیل فعلی: ali.rezaei@example.com" in preview(result.action)


def test_model_cannot_see_or_set_previous_email() -> None:
    from app.agent.agent import tool_schemas

    schema = next(t for t in tool_schemas() if t["function"]["name"] == "propose_contact")
    assert set(schema["function"]["parameters"]["properties"]) == {"name", "email"}


def test_contact_actions_map_to_contact_intent() -> None:
    assert intent_of(AddContactAction(name="رضا", email="r@example.com")) == "contact"
    assert intent_of(DeleteContactAction(contact_id=1, name="رضا")) == "contact"
