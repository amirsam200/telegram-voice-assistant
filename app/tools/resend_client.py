"""Minimal client for the Resend email API (https://resend.com/docs/api-reference)."""

import httpx

from app.config import get_settings

RESEND_URL = "https://api.resend.com/emails"


class ResendError(RuntimeError):
    pass


def send_email(
    to: str | list[str],
    subject: str,
    text: str,
    html: str | None = None,
    idempotency_key: str | None = None,
) -> str:
    """Send one email and return Resend's message id."""
    settings = get_settings()
    if not settings.resend_api_key:
        raise ResendError("RESEND_API_KEY is not set")

    payload: dict = {
        "from": settings.email_from,
        "to": [to] if isinstance(to, str) else to,
        "subject": subject,
        "text": text,
    }
    if html:
        payload["html"] = html

    headers = {"Authorization": f"Bearer {settings.resend_api_key}"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key

    try:
        response = httpx.post(RESEND_URL, json=payload, headers=headers, timeout=20)
    except httpx.HTTPError as exc:
        raise ResendError(f"could not reach Resend: {exc.__class__.__name__}") from exc

    if response.status_code >= 400:
        try:
            detail = response.json().get("message", response.text)
        except ValueError:
            detail = response.text
        raise ResendError(f"Resend returned {response.status_code}: {detail}")

    return response.json()["id"]
