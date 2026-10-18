"""Send email from the owner's own Gmail with the Gmail API (scope gmail.send).

Gmail has no idempotency key; the tool registry already runs each confirmed send at most once.
The key is still written into the Message-ID so a send can be traced back to its run.
"""

import base64
import hashlib
from email.message import EmailMessage
from email.utils import formataddr

from app.config import get_settings
from app.tools import google_oauth
from app.tools.mailer import EmailError

SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"


class GmailError(EmailError):
    pass


def build_message(
    to: str | list[str],
    subject: str,
    text: str,
    html: str | None = None,
    idempotency_key: str | None = None,
    sender: str | None = None,
) -> EmailMessage:
    message = EmailMessage()
    message["To"] = to if isinstance(to, str) else ", ".join(to)
    if sender:
        message["From"] = sender
    message["Subject"] = subject
    if idempotency_key:
        digest = hashlib.sha1(idempotency_key.encode()).hexdigest()
        message["Message-ID"] = f"<va-{digest}@voice-assistant.local>"
    message.set_content(text)
    if html:
        message.add_alternative(html, subtype="html")
    return message


def send_email(
    to: str | list[str],
    subject: str,
    text: str,
    html: str | None = None,
    idempotency_key: str | None = None,
) -> str:
    """Send one email from the authorised Gmail account and return Gmail's message id."""
    if not google_oauth.is_configured():
        raise GmailError(
            "Gmail is not configured: set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and "
            "GOOGLE_REFRESH_TOKEN (run scripts/google_auth.py)"
        )
    from google.auth.exceptions import GoogleAuthError
    from google.auth.transport.requests import AuthorizedSession
    from requests import RequestException

    s = get_settings()
    # Gmail sets the address of the signed-in account; only the display name is ours to choose.
    sender = formataddr((s.owner_name, s.owner_email)) if s.owner_name and s.owner_email else None
    message = build_message(to, subject, text, html, idempotency_key, sender)
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

    try:
        response = AuthorizedSession(google_oauth.credentials()).post(SEND_URL, json={"raw": raw}, timeout=20)
    except GoogleAuthError as exc:
        raise GmailError(f"Gmail authorisation failed (run scripts/google_auth.py again): {exc}") from exc
    except RequestException as exc:
        raise GmailError(f"could not reach Gmail: {exc.__class__.__name__}") from exc

    if response.status_code >= 400:
        try:
            detail = response.json().get("error", {}).get("message", response.text)
        except ValueError:
            detail = response.text
        if response.status_code == 403 and "scope" in str(detail).lower():
            detail += " (the Google sign-in has no gmail.send scope: run scripts/google_auth.py again)"
        raise GmailError(f"Gmail returned {response.status_code}: {str(detail)[:300]}")

    return response.json()["id"]
