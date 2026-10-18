"""Picks the email provider from EMAIL_PROVIDER (gmail or resend).

Every provider has the same signature:
    send_email(to, subject, text, html=None, idempotency_key=None) -> message id
and raises EmailError (model-safe message) on failure.
"""

from collections.abc import Callable

from app.config import get_settings

PROVIDERS = ("gmail", "resend")


class EmailError(RuntimeError):
    pass


def get_sender() -> Callable[..., str]:
    provider = get_settings().email_provider.strip().lower()
    if provider == "gmail":
        from app.tools.gmail_client import send_email
    elif provider == "resend":
        from app.tools.resend_client import send_email
    else:
        raise EmailError(f"EMAIL_PROVIDER must be one of {', '.join(PROVIDERS)}, not {provider!r}")
    return send_email
