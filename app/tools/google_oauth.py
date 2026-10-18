"""OAuth credentials for the owner's Google account (refresh token from env).

One refresh token covers both scopes; run scripts/google_auth.py again when the scopes change.
"""

from app.config import get_settings

SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.send",
]
TOKEN_URI = "https://oauth2.googleapis.com/token"


def is_configured() -> bool:
    s = get_settings()
    return bool(s.google_client_id and s.google_client_secret and s.google_refresh_token)


def credentials():
    from google.oauth2.credentials import Credentials

    s = get_settings()
    # No scopes on refresh: Google returns a token for everything the refresh token was granted.
    return Credentials(
        token=None,
        refresh_token=s.google_refresh_token,
        client_id=s.google_client_id,
        client_secret=s.google_client_secret,
        token_uri=TOKEN_URI,
    )
