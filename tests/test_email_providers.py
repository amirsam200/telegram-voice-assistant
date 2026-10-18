"""Email providers: EMAIL_PROVIDER picks Gmail (default) or Resend; Gmail builds a proper MIME message."""

import base64
import email
from email import policy

import pytest
from sqlalchemy import select

from app.config import Settings, get_settings
from app.db.models import ToolCall
from app.tools import gmail_client, mailer, registry
from app.tools.registry import ToolError, execute


@pytest.fixture
def settings(monkeypatch):
    s = get_settings()
    for key, value in {
        "email_provider": "gmail",
        "google_client_id": "id",
        "google_client_secret": "secret",
        "google_refresh_token": "refresh",
        "owner_name": "امیرسام",
        "owner_email": "owner@example.com",
    }.items():
        monkeypatch.setattr(s, key, value)
    return s


class FakeResponse:
    def __init__(self, status_code: int, body: dict) -> None:
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self) -> dict:
        return self._body


class FakeSession:
    """Stands in for google.auth AuthorizedSession; records what would be posted to Gmail."""

    response = FakeResponse(200, {"id": "gmail-123"})
    posted: list[dict] = []

    def __init__(self, credentials) -> None:
        self.credentials = credentials

    def post(self, url, json, timeout):
        FakeSession.posted.append({"url": url, "json": json})
        return FakeSession.response


@pytest.fixture
def gmail_api(monkeypatch):
    import google.auth.transport.requests as transport

    FakeSession.posted = []
    FakeSession.response = FakeResponse(200, {"id": "gmail-123"})
    monkeypatch.setattr(transport, "AuthorizedSession", FakeSession)
    return FakeSession


def _decoded(posted: dict) -> email.message.EmailMessage:
    raw = base64.urlsafe_b64decode(posted["json"]["raw"])
    return email.message_from_bytes(raw, policy=policy.default)


def test_gmail_is_the_default_provider() -> None:
    assert Settings.model_fields["email_provider"].default == "gmail"


@pytest.mark.parametrize("provider, module", [("gmail", "app.tools.gmail_client"),
                                              ("resend", "app.tools.resend_client"),
                                              (" Resend ", "app.tools.resend_client")])
def test_email_provider_selects_the_sender(settings, monkeypatch, provider, module) -> None:
    monkeypatch.setattr(settings, "email_provider", provider)
    assert mailer.get_sender().__module__ == module


def test_unknown_provider_is_an_email_error(settings, monkeypatch) -> None:
    monkeypatch.setattr(settings, "email_provider", "smtp")
    with pytest.raises(mailer.EmailError, match="gmail, resend"):
        mailer.get_sender()


def test_both_provider_errors_are_email_errors() -> None:
    from app.tools.resend_client import ResendError

    assert issubclass(ResendError, mailer.EmailError)
    assert issubclass(gmail_client.GmailError, mailer.EmailError)


def test_gmail_message_is_utf8_with_text_and_html() -> None:
    message = gmail_client.build_message(
        "sara@example.com", "گزارش ماهانه", "سلام سارا", html="<p>سلام سارا</p>",
        idempotency_key="run:7:send_email", sender="امیرسام <owner@example.com>",
    )
    parsed = email.message_from_bytes(message.as_bytes(), policy=policy.default)
    assert parsed["To"] == "sara@example.com"
    assert parsed["Subject"] == "گزارش ماهانه"
    assert parsed["From"] == "امیرسام <owner@example.com>"
    assert parsed.get_body(("plain",)).get_content().strip() == "سلام سارا"
    assert parsed.get_body(("html",)).get_content().strip() == "<p>سلام سارا</p>"
    again = gmail_client.build_message("sara@example.com", "s", "t", idempotency_key="run:7:send_email")
    assert parsed["Message-ID"] == again["Message-ID"]


def test_gmail_send_posts_the_raw_message(settings, gmail_api) -> None:
    message_id = gmail_client.send_email("sara@example.com", "گزارش", "سلام", idempotency_key="run:1:send_email")
    assert message_id == "gmail-123"
    [posted] = gmail_api.posted
    assert posted["url"] == gmail_client.SEND_URL
    sent = _decoded(posted)
    assert sent["To"] == "sara@example.com"
    assert sent["Subject"] == "گزارش"
    assert "owner@example.com" in sent["From"]


def test_gmail_without_sign_in_is_not_configured(settings, monkeypatch, gmail_api) -> None:
    monkeypatch.setattr(settings, "google_refresh_token", "")
    with pytest.raises(gmail_client.GmailError, match="not configured"):
        gmail_client.send_email("sara@example.com", "s", "t")
    assert gmail_api.posted == []


def test_gmail_missing_scope_says_to_sign_in_again(settings, gmail_api) -> None:
    gmail_api.response = FakeResponse(403, {"error": {"message": "Request had insufficient authentication scopes."}})
    with pytest.raises(gmail_client.GmailError, match="google_auth.py"):
        gmail_client.send_email("sara@example.com", "s", "t")


def test_send_email_tool_uses_the_configured_provider(ctx, session, monkeypatch) -> None:
    sent = []

    def fake_sender(**kwargs):
        sent.append(kwargs)
        return "gmail-9"

    ctx.send_email = None
    monkeypatch.setattr(registry, "get_sender", lambda: fake_sender)
    result = execute(ctx, "send_email", {"to": "sara.mohammadi@example.com", "subject": "گزارش", "body": "سلام"},
                     idempotency_key="run:3:send_email")
    assert result == {"message_id": "gmail-9"}
    assert sent[0]["to"] == "sara.mohammadi@example.com"
    assert 'dir="rtl"' in sent[0]["html"]


def test_gmail_failure_is_logged_as_tool_error(ctx, session, monkeypatch) -> None:
    def broken(**kwargs):
        raise gmail_client.GmailError("Gmail returned 500: backend error")

    ctx.send_email = broken
    with pytest.raises(ToolError, match="Gmail returned 500"):
        execute(ctx, "send_email", {"to": "sara.mohammadi@example.com", "subject": "س", "body": "ب"},
                idempotency_key="run:4:send_email")
    assert session.scalar(select(ToolCall)).status == "error"
