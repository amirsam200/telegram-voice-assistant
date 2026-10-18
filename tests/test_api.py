"""Admin API: auth, filters, pagination, errors, retry and health (no network)."""

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.agent.actions import EmailAction
from app.api import main as api
from app.api.health import HealthChecker
from app.config import get_settings
from app.db.models import (
    AgentRun,
    AuditEvent,
    CalendarEvent,
    Contact,
    Email,
    Intent,
    RunStatus,
    ToolCall,
    VoiceMessage,
)

KEY = "test-admin-key"
AUTH = {"X-API-Key": KEY}


@pytest.fixture
def settings(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "admin_api_key", KEY)
    monkeypatch.setattr(s, "allowed_user_ids", "1001")
    monkeypatch.setattr(s, "debug", False)
    return s


@pytest.fixture
def client(settings, session, ctx):
    app = api.create_app()
    app.dependency_overrides[api.get_db] = lambda: session
    app.dependency_overrides[api.get_tool_context] = lambda: ctx
    app.dependency_overrides[api.get_health_checker] = lambda: HealthChecker(
        {"database": lambda: None, "telegram": lambda: None, "llm": lambda: None}
    )
    return TestClient(app)


def _run(session, owner, **kw) -> AgentRun:
    values = {"user_id": owner.id, "intent": Intent.email, "status": RunStatus.executed, "input_text": "متن"}
    values.update(kw)
    run = AgentRun(**values)
    session.add(run)
    session.commit()
    return run


# --- auth -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/runs", "/runs/1", "/events", "/emails", "/contacts"])
@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}])
def test_admin_endpoints_need_the_key(client, path, headers) -> None:
    assert client.get(path, headers=headers).status_code == 401


def test_no_configured_key_locks_the_api(client, settings, monkeypatch) -> None:
    monkeypatch.setattr(settings, "admin_api_key", "")
    assert client.get("/runs", headers={"X-API-Key": ""}).status_code == 401
    assert client.get("/runs", headers=AUTH).status_code == 401


def test_post_endpoints_need_the_key(client) -> None:
    assert client.post("/contacts", json={"name": "رضا", "email": "r@example.com"}).status_code == 401
    assert client.post("/runs/1/retry").status_code == 401


def test_docs_only_in_debug(settings, monkeypatch) -> None:
    assert TestClient(api.create_app()).get("/docs").status_code == 404
    assert TestClient(api.create_app()).get("/openapi.json").status_code == 404
    monkeypatch.setattr(settings, "debug", True)
    assert TestClient(api.create_app()).get("/openapi.json").status_code == 200


# --- health ---------------------------------------------------------------------------------------


def test_health_ok(client) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"database": "ok", "telegram": "ok", "llm": "ok"}}


def test_health_reports_failures_without_details(client) -> None:
    def telegram_down():
        raise ConnectionError("https://api.telegram.org/bot123:SECRET/getMe failed")

    client.app.dependency_overrides[api.get_health_checker] = lambda: HealthChecker(
        {"database": lambda: None, "telegram": telegram_down, "llm": lambda: None}
    )
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["checks"]["telegram"] == "error: ConnectionError"
    assert "SECRET" not in response.text


def test_health_results_are_cached() -> None:
    calls = []
    now = [0.0]
    checker = HealthChecker({"db": lambda: calls.append(1)}, cache_s=30, clock=lambda: now[0])
    checker.run()
    now[0] = 10
    checker.run()
    assert len(calls) == 1
    now[0] = 31
    checker.run()
    assert len(calls) == 2


# --- runs -----------------------------------------------------------------------------------------


def test_runs_filters_and_pagination(client, session, owner) -> None:
    for i in range(5):
        _run(session, owner, status=RunStatus.failed if i % 2 else RunStatus.executed)
    _run(session, owner, intent=Intent.schedule)

    body = client.get("/runs", params={"limit": 2}, headers=AUTH).json()
    assert (body["total"], body["limit"], body["offset"], len(body["items"])) == (6, 2, 0, 2)
    assert body["items"][0]["id"] > body["items"][1]["id"]  # newest first

    failed = client.get("/runs", params={"status": "failed"}, headers=AUTH).json()
    assert failed["total"] == 2 and {r["status"] for r in failed["items"]} == {"failed"}
    schedule = client.get("/runs", params={"intent": "schedule"}, headers=AUTH).json()
    assert schedule["total"] == 1 and schedule["items"][0]["intent"] == "schedule"
    page3 = client.get("/runs", params={"limit": 2, "offset": 4}, headers=AUTH).json()
    assert len(page3["items"]) == 2


def test_runs_time_filters(client, session, owner) -> None:
    run = _run(session, owner)
    created = run.created_at
    later = (created + dt.timedelta(minutes=1)).isoformat()
    earlier = (created - dt.timedelta(minutes=1)).isoformat()
    assert client.get("/runs", params={"since": later}, headers=AUTH).json()["total"] == 0
    assert client.get("/runs", params={"since": earlier, "until": later}, headers=AUTH).json()["total"] == 1


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"offset": -1}, {"status": "nope"},
                                    {"since": "not-a-date"}])
def test_runs_invalid_params_are_422(client, params) -> None:
    assert client.get("/runs", params=params, headers=AUTH).status_code == 422


def test_run_detail_has_transcript_action_and_tool_calls_but_no_audio(client, session, owner) -> None:
    voice = VoiceMessage(user_id=owner.id, telegram_file_id="tg-file", file_path="/app/data/voice/x.ogg",
                         duration_s=5, transcript="به سارا ایمیل بزن", stt_confidence=0.97)
    session.add(voice)
    session.commit()
    run = _run(session, owner, voice_message_id=voice.id, input_text="به سارا ایمیل بزن",
               action_json={"type": "email", "to": "sara.mohammadi@example.com"})
    session.add(ToolCall(run_id=run.id, tool_name="send_email", arguments_json={"to": "x"},
                         result_json={"message_id": "m1"}, status="success"))
    session.commit()

    response = client.get(f"/runs/{run.id}", headers=AUTH)
    body = response.json()
    assert body["input_text"] == "به سارا ایمیل بزن"
    assert body["action_json"]["to"] == "sara.mohammadi@example.com"
    assert [c["tool_name"] for c in body["tool_calls"]] == ["send_email"]
    assert body["voice"] == {"id": voice.id, "duration_s": 5, "stt_confidence": 0.97,
                             "created_at": body["voice"]["created_at"]}
    assert "file_path" not in response.text and "tg-file" not in response.text


def test_unknown_run_is_404(client) -> None:
    assert client.get("/runs/999", headers=AUTH).status_code == 404


# --- retry ----------------------------------------------------------------------------------------


def test_retry_resends_a_failed_email(client, session, owner, email) -> None:
    action = EmailAction(to="sara.mohammadi@example.com", subject="گزارش", body="سلام سارا")
    run = _run(session, owner, status=RunStatus.failed, error="Gmail returned 500",
               action_json=action.model_dump(mode="json"))

    response = client.post(f"/runs/{run.id}/retry", headers=AUTH)
    body = response.json()
    assert response.status_code == 200
    assert body["ok"] and body["status"] == "executed" and "فرستاده شد" in body["message"]
    assert len(email.sent) == 1
    assert session.scalar(select(Email)).status == "sent"
    events = [e.event_type for e in session.scalars(select(AuditEvent))]
    assert "action_retried" in events
    # A second retry is refused: the run is no longer failed.
    assert client.post(f"/runs/{run.id}/retry", headers=AUTH).status_code == 409
    assert len(email.sent) == 1


@pytest.mark.parametrize("status, action", [(RunStatus.executed, {"type": "email"}), (RunStatus.failed, None),
                                            (RunStatus.pending_confirmation, {"type": "email"})])
def test_retry_only_failed_actions(client, session, owner, status, action) -> None:
    run = _run(session, owner, status=status, action_json=action)
    response = client.post(f"/runs/{run.id}/retry", headers=AUTH)
    assert response.status_code == 409
    assert client.post("/runs/999/retry", headers=AUTH).status_code == 404


# --- events, emails, contacts ---------------------------------------------------------------------


def test_events_date_filters(client, session, owner) -> None:
    run = _run(session, owner, intent=Intent.schedule)
    for day in (19, 20, 25):
        session.add(CalendarEvent(run_id=run.id, date=dt.date(2026, 10, day), time=dt.time(10), duration_min=60,
                                  title=f"جلسه {day}", guests=[]))
    session.commit()
    body = client.get("/events", params={"from": "2026-10-19", "to": "2026-10-20"}, headers=AUTH).json()
    assert body["total"] == 2
    assert [e["date"] for e in body["items"]] == ["2026-10-20", "2026-10-19"]


def test_emails_status_and_recipient_filters(client, session, owner) -> None:
    run = _run(session, owner)
    session.add_all([
        Email(run_id=run.id, to="sara@example.com", subject="الف", body="ب", status="sent"),
        Email(run_id=run.id, to="ali@example.com", subject="الف", body="ب", status="failed"),
    ])
    session.commit()
    assert client.get("/emails", params={"status": "failed"}, headers=AUTH).json()["items"][0]["to"] == "ali@example.com"
    assert client.get("/emails", params={"to": "SARA@example.com"}, headers=AUTH).json()["total"] == 1


def test_contacts_list_and_search(client, owner) -> None:
    body = client.get("/contacts", headers=AUTH).json()
    assert body["total"] == 2
    assert client.get("/contacts", params={"q": "sara"}, headers=AUTH).json()["items"][0]["name"] == "سارا محمدی"
    assert client.get("/contacts", params={"q": "رضایی"}, headers=AUTH).json()["total"] == 1


def test_create_contact_defaults_to_owner_and_is_audited(client, session, owner) -> None:
    response = client.post("/contacts", json={"name": " رضا  احمدی ", "email": "Reza@Example.com"}, headers=AUTH)
    assert response.status_code == 201
    body = response.json()
    assert (body["name"], body["email"], body["user_id"]) == ("رضا احمدی", "reza@example.com", owner.id)
    audit = session.scalar(select(AuditEvent).where(AuditEvent.event_type == "contact_created"))
    assert audit.payload_json["contact_id"] == body["id"]


@pytest.mark.parametrize("payload, code", [
    ({"name": "رضا", "email": "not-an-email"}, 422),
    ({"name": "", "email": "r@example.com"}, 422),
    ({"name": "سارا محمدی", "email": "new@example.com"}, 409),
    ({"name": "سارای دیگر", "email": "sara.mohammadi@example.com"}, 409),
    ({"name": "رضا", "email": "r@example.com", "user_id": 999}, 404),
])
def test_create_contact_errors(client, session, owner, payload, code) -> None:
    assert client.post("/contacts", json=payload, headers=AUTH).status_code == code
    assert session.scalar(select(Contact).where(Contact.name == "رضا")) is None
