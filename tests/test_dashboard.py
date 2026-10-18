"""Dashboard: aggregation queries, Persian formatting, sign-in, pages, CSV and the read-only role."""

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from app.agent.fa import TEHRAN
from app.config import get_settings
from app.dashboard import formatting, queries
from app.dashboard import main as dash
from app.dashboard.auth import hash_password, verify_password
from app.db import demo
from app.db.models import AgentRun, AuditEvent, Intent, LlmCall, RunFeedback, RunStatus, User
from app.db.readonly_role import ensure_readonly_role
from tests.conftest import NOW  # Sunday 2026-10-18 10:00 Tehran

PASSWORD = "correct horse battery"


# --- fixtures -------------------------------------------------------------------------------------


def _run(session, user, *, status, model="deepseek-chat", when=NOW - dt.timedelta(hours=1), action=True,
         validation_errors=0, steps=2, latency_ms=2000, calls=((1000, 100, 1500, "intent"),)) -> AgentRun:
    run = AgentRun(user_id=user.id, intent=Intent.email, status=status, model=model, steps=steps,
                   action_json={"type": "email", "to": "a@example.com", "subject": "س", "body": "متن کامل ایمیل"}
                   if action else None,
                   validation_errors=validation_errors, latency_ms=latency_ms, input_text="به سارا ایمیل بزن",
                   created_at=when)
    session.add(run)
    session.flush()
    for prompt, completion, latency, purpose in calls:
        session.add(LlmCall(run_id=run.id, model=model, purpose=purpose, status="ok", prompt_tokens=prompt,
                            completion_tokens=completion, cost_usd=(prompt + completion) / 1_000_000,
                            latency_ms=latency, created_at=when))
    session.commit()
    return run


@pytest.fixture
def scenario(session, owner):
    """Six runs with known outcomes, all today (Tehran)."""
    runs = {
        "executed1": _run(session, owner, status=RunStatus.executed),
        "executed2": _run(session, owner, status=RunStatus.executed, model="deepseek-reasoner",
                          validation_errors=1, steps=3,
                          calls=((2000, 200, 4000, "intent"), (2500, 300, 6000, "tool_step"))),
        "failed": _run(session, owner, status=RunStatus.failed),
        "cancelled": _run(session, owner, status=RunStatus.rejected),
        "edited": _run(session, owner, status=RunStatus.rejected),
        "question": _run(session, owner, status=RunStatus.clarification, action=False),
    }
    session.add(AuditEvent(event_type="action_cancelled", actor="user:1", payload_json={"run_id": runs["cancelled"].id}))
    session.add(RunFeedback(run_id=runs["executed1"].id, user_id=owner.id, rating=1))
    session.add(RunFeedback(run_id=runs["failed"].id, user_id=owner.id, rating=-1))
    session.commit()
    return runs


@pytest.fixture
def settings(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "dashboard_password_hash", hash_password(PASSWORD, iterations=1000))
    monkeypatch.setattr(s, "dashboard_secret_key", "test-secret")
    monkeypatch.setattr(s, "daily_token_budget", 10_000)
    monkeypatch.setattr(s, "monthly_token_budget", 100_000)
    dash._login_limiter._events.clear()
    return s


@pytest.fixture
def client(settings, session):
    app = dash.create_app()
    app.dependency_overrides[dash.get_db] = lambda: session
    app.dependency_overrides[dash.get_now] = lambda: NOW
    return TestClient(app)


def _sign_in(client) -> None:
    response = client.post("/login", data={"password": PASSWORD, "next": "/"}, follow_redirects=False)
    assert response.status_code == 303


# --- aggregation queries --------------------------------------------------------------------------


def test_kpis(session, scenario) -> None:
    k = queries.kpis(session, NOW)
    # 5 runs x 1100 tokens + 2200 + 2800 for the reasoner run
    assert (k["tokens_today"], k["tokens_7d"], k["tokens_30d"]) == (10_500, 10_500, 10_500)
    assert k["cost_30d"] == pytest.approx(0.0105)
    # latencies [1500 x5, 4000, 6000]: P95 = 4000 + 0.7 * 2000
    assert k["latency_p50"] == pytest.approx(1500) and k["latency_p95"] == pytest.approx(5400)
    assert k["success_rate"] == pytest.approx(2 / 3)  # executed / (executed + failed)
    assert k["confirmation_rate"] == pytest.approx(3 / 5)  # 3 confirmed of 5 decided
    assert k["edit_rate"] == pytest.approx(1 / 5)
    assert k["runs_30d"] == 6


def test_decision_separates_cancel_from_edit(session, scenario) -> None:
    rows, _ = queries.logs(session)
    decision = {r["id"]: r["decision"] for r in rows}
    assert decision[scenario["cancelled"].id] == "cancelled"
    assert decision[scenario["edited"].id] == "edited"
    assert decision[scenario["failed"].id] == "confirmed"
    assert decision[scenario["question"].id] is None


def test_usage_by_day_splits_model_and_purpose_on_tehran_days(session, owner) -> None:
    # 00:30 Tehran on the 18th is still the 17th in UTC: it must count on the 18th.
    _run(session, owner, status=RunStatus.executed, when=dt.datetime(2026, 10, 18, 0, 30, tzinfo=TEHRAN))
    _run(session, owner, status=RunStatus.executed, when=dt.datetime(2026, 10, 17, 23, 30, tzinfo=TEHRAN),
         model="deepseek-reasoner", calls=((500, 50, 900, "intent"), (700, 70, 900, "tool_step")))
    data = queries.usage_by_day(session, NOW, "model", count=3)
    assert data["days"] == ["2026-10-16", "2026-10-17", "2026-10-18"]
    assert data["series"]["deepseek-chat"]["tokens"] == [0, 0, 1100]
    assert data["series"]["deepseek-reasoner"]["tokens"] == [0, 1320, 0]
    purposes = queries.usage_by_day(session, NOW, "purpose", count=3)["series"]
    assert purposes["tool_step"]["tokens"] == [0, 770, 0]
    with pytest.raises(ValueError):
        queries.usage_by_day(session, NOW, "user")


def test_latency_and_rates_by_day(session, scenario) -> None:
    latency = queries.latency_by_day(session, NOW, count=2)
    assert latency["p50"] == [None, 1500] and latency["p95"][1] == 5400
    rates = queries.rates_by_day(session, NOW, count=2)
    assert rates["success"] == [None, pytest.approx(0.6667, abs=1e-4)]
    assert rates["edit"] == [None, 0.2]


def test_quality_by_model(session, scenario) -> None:
    quality = {q["model"]: q for q in queries.quality_by_model(session, NOW)}
    chat, reasoner = quality["deepseek-chat"], quality["deepseek-reasoner"]
    assert chat["runs"] == 5 and reasoner["runs"] == 1
    assert chat["success_rate"] == pytest.approx(0.5)  # 1 executed, 1 failed
    assert reasoner["json_valid_rate"] == 0.0 and chat["json_valid_rate"] == 1.0
    assert chat["edit_rate"] == pytest.approx(1 / 4)
    assert reasoner["avg_steps"] == 3.0
    assert reasoner["avg_cost"] == pytest.approx(0.005)
    assert chat["thumbs_up_share"] == 0.5 and chat["feedback"] == 2


def test_expensive_runs_and_filters(session, scenario) -> None:
    top = queries.expensive_runs(session, NOW, limit=2)
    assert top[0]["id"] == scenario["executed2"].id and top[0]["tokens"] == 5000
    rows, total = queries.logs(session, model="deepseek-chat", status="rejected")
    assert total == 2 and {r["id"] for r in rows} == {scenario["cancelled"].id, scenario["edited"].id}
    assert queries.logs(session, intent="schedule")[1] == 0
    assert queries.logs(session, date_from=NOW.date() + dt.timedelta(days=1))[1] == 0
    assert queries.logs(session, date_from=NOW.date(), date_to=NOW.date())[1] == 6
    page, total = queries.logs(session, limit=4, offset=4)
    assert total == 6 and len(page) == 2


def test_demo_data_can_be_excluded(session, owner, scenario) -> None:
    demo.seed(session, days=3, now=NOW)
    with_demo = queries.kpis(session, NOW)["tokens_30d"]
    without = queries.kpis(session, NOW, include_demo=False)["tokens_30d"]
    assert without == 10_500 and with_demo > without
    assert queries.logs(session, include_demo=False)[1] == 6


def test_run_detail_has_everything(session, scenario) -> None:
    detail = queries.run_detail(session, scenario["executed2"].id)
    assert detail["run"]["total_tokens"] == 5000 and detail["run"]["decision"] == "confirmed"
    assert [c["purpose"] for c in detail["llm_calls"]] == ["intent", "tool_step"]
    assert queries.run_detail(session, 99999) is None


# --- formatting -----------------------------------------------------------------------------------


def test_persian_number_formatting() -> None:
    assert formatting.fa_number(1234567) == "۱٬۲۳۴٬۵۶۷"
    assert formatting.fa_number(3.14159, 2) == "۳٫۱۴"
    assert formatting.fa_percent(0.6667) == "۶۶٫۷٪"
    assert formatting.fa_compact(2_500_000) == "۲٫۵ میلیون"
    assert formatting.fa_usd(0.0105) == "۰٫۰۱۰۵ $"
    assert formatting.fa_ms(1500) == "۱٫۵۰ ث" and formatting.fa_ms(850) == "۸۵۰ م‌ث"
    assert formatting.fa_number(None) == formatting.fa_percent(None) == "—"


# --- sign-in and access ---------------------------------------------------------------------------


def test_password_hash_roundtrip() -> None:
    stored = hash_password("s3cret-password", iterations=1000)
    assert stored.startswith("pbkdf2_sha256:1000:") and "$" not in stored
    assert verify_password("s3cret-password", stored)
    assert not verify_password("wrong", stored)
    assert not verify_password("s3cret-password", "garbage")


@pytest.mark.parametrize("path", ["/", "/logs", "/runs/1"])
def test_unauthenticated_pages_redirect_to_login(client, path) -> None:
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith("/login")


def test_unauthenticated_csv_is_refused(client) -> None:
    assert client.get("/logs.csv", follow_redirects=False).status_code == 401


def test_wrong_password_is_refused_and_rate_limited(client) -> None:
    for _ in range(5):
        assert client.post("/login", data={"password": "nope"}).status_code == 401
    blocked = client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert blocked.status_code == 401 and "تلاش‌های ورود زیاد" in blocked.text


def test_no_password_configured_means_nobody_signs_in(client, settings, monkeypatch) -> None:
    monkeypatch.setattr(settings, "dashboard_password_hash", "")
    response = client.post("/login", data={"password": ""})
    assert response.status_code in (401, 422)
    assert client.get("/", follow_redirects=False).status_code == 303


def test_sign_in_redirect_stays_on_site(client) -> None:
    response = client.post("/login", data={"password": PASSWORD, "next": "//evil.example"}, follow_redirects=False)
    assert response.headers["location"] == "/"


def test_logout_ends_the_session(client) -> None:
    _sign_in(client)
    assert client.get("/", follow_redirects=False).status_code == 200
    client.post("/logout")
    assert client.get("/", follow_redirects=False).status_code == 303


def test_healthz_is_public(client) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


# --- pages ----------------------------------------------------------------------------------------


def test_pages_render_with_demo_data(client, session, scenario) -> None:
    demo.seed(session, days=30, now=NOW)
    _sign_in(client)

    home = client.get("/")
    assert home.status_code == 200
    for label in ("Tokens today", "Latency P50 / P95", "Confirmation rate", "Quality per model", "deepseek-reasoner",
                  "بودجهٔ توکن", "chart.umd.min.js"):
        assert label in home.text
    assert "۱۰٬۵۰۰ از ۱۰٬۰۰۰ توکن" in home.text  # real usage only, demo excluded from the budget

    logs = client.get("/logs", params={"status": "failed"})
    assert logs.status_code == 200 and "ناموفق" in logs.text
    assert "متن کامل ایمیل" not in logs.text  # no email bodies in the list

    run = client.get(f"/runs/{scenario['executed2'].id}")
    assert run.status_code == 200 and "متن کامل ایمیل" in run.text and "tool_step" in run.text
    assert client.get("/runs/999999").status_code == 404


def test_csv_export_uses_the_filters(client, scenario) -> None:
    _sign_in(client)
    response = client.get("/logs.csv", params={"status": "rejected"})
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/csv")
    lines = response.content.decode("utf-8-sig").strip().splitlines()
    assert lines[0].split(",") == queries.LOG_COLUMNS
    assert len(lines) == 3
    assert "متن کامل ایمیل" not in response.text


# --- read-only database role ----------------------------------------------------------------------


def test_readonly_role_can_select_but_not_write(engine, session, owner) -> None:
    role, password = "dashboard_ro_test", "ro-test-password"
    ensure_readonly_role(engine, role, password)
    ensure_readonly_role(engine, role, password)  # idempotent
    url = make_url(str(engine.url.render_as_string(hide_password=False))).set(username=role, password=password)
    ro = create_engine(url)
    try:
        with ro.connect() as conn:
            assert conn.scalar(text("SELECT count(*) FROM users")) == 1
        with pytest.raises(DBAPIError):
            with ro.begin() as conn:
                conn.execute(text("INSERT INTO users (telegram_user_id, name) VALUES (5, 'x')"))
        with pytest.raises(DBAPIError):
            with ro.begin() as conn:
                conn.execute(text("SET TRANSACTION READ WRITE"))
                conn.execute(text("DELETE FROM users"))
    finally:
        ro.dispose()
    assert session.scalar(text("SELECT count(*) FROM users")) == 1
    with pytest.raises(ValueError):
        ensure_readonly_role(engine, "bad; DROP TABLE users", "x")


def test_user_model_is_untouched(session, owner) -> None:
    assert session.get(User, owner.id).name == "مالک"


def test_pretty_json_keeps_persian_and_is_escaped_in_html() -> None:
    assert formatting.pretty_json({"title": "جلسه <b>"}) == '{\n  "title": "جلسه <b>"\n}'
    template = dash.templates.env.from_string("{{ v | pretty_json }}")
    assert "&lt;b&gt;" in template.render(v={"title": "<b>"})
