"""Google Calendar API client for the owner's calendar (OAuth refresh token from env)."""

import datetime as dt
import hashlib
from typing import Any, Protocol

from app.config import get_settings
from app.tools import google_oauth

API = "https://www.googleapis.com/calendar/v3"


class CalendarError(RuntimeError):
    pass


class CalendarClient(Protocol):
    def list_events(self, start: dt.datetime, end: dt.datetime) -> list[dict[str, Any]]: ...

    def create_event(
        self,
        event_id: str,
        start: dt.datetime,
        end: dt.datetime,
        title: str,
        description: str,
        guests: list[str],
    ) -> dict[str, Any]: ...


def event_id_for(idempotency_key: str) -> str:
    """Deterministic Google event id (base32hex alphabet), so a retry can never create a duplicate."""
    return "va" + hashlib.sha1(idempotency_key.encode()).hexdigest()


class GoogleCalendarClient:
    def __init__(self) -> None:
        from google.auth.transport.requests import AuthorizedSession

        s = get_settings()
        if not google_oauth.is_configured():
            raise CalendarError(
                "Google Calendar is not configured: set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and "
                "GOOGLE_REFRESH_TOKEN (run scripts/google_auth.py)"
            )
        self._session = AuthorizedSession(google_oauth.credentials())
        self._calendar_id = s.google_calendar_id or "primary"
        self._timezone = s.timezone

    def _request(self, method: str, path: str, **kwargs) -> Any:
        from google.auth.exceptions import GoogleAuthError
        from requests import RequestException

        try:
            response = self._session.request(method, f"{API}{path}", timeout=20, **kwargs)
        except (GoogleAuthError, RequestException) as exc:
            raise CalendarError(f"Google Calendar request failed: {exc.__class__.__name__}: {exc}") from exc
        return response

    def list_events(self, start: dt.datetime, end: dt.datetime) -> list[dict[str, Any]]:
        response = self._request(
            "GET",
            f"/calendars/{self._calendar_id}/events",
            params={
                "timeMin": start.isoformat(),
                "timeMax": end.isoformat(),
                "singleEvents": "true",
                "orderBy": "startTime",
                "timeZone": self._timezone,
                "maxResults": 50,
            },
        )
        if response.status_code != 200:
            raise CalendarError(f"Google Calendar returned {response.status_code}: {response.text[:300]}")
        return [
            {
                "title": item.get("summary", ""),
                "start": item.get("start", {}).get("dateTime") or item.get("start", {}).get("date"),
                "end": item.get("end", {}).get("dateTime") or item.get("end", {}).get("date"),
            }
            for item in response.json().get("items", [])
        ]

    def create_event(
        self,
        event_id: str,
        start: dt.datetime,
        end: dt.datetime,
        title: str,
        description: str,
        guests: list[str],
    ) -> dict[str, Any]:
        body = {
            "id": event_id,
            "summary": title,
            "description": description,
            "start": {"dateTime": start.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": self._timezone},
            "end": {"dateTime": end.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": self._timezone},
            "attendees": [{"email": g} for g in guests],
        }
        path = f"/calendars/{self._calendar_id}/events"
        response = self._request("POST", path, params={"sendUpdates": "all"}, json=body)
        if response.status_code == 409:
            # Same deterministic id already exists: the event was created by an earlier attempt.
            response = self._request("GET", f"{path}/{event_id}")
        if response.status_code not in (200, 201):
            raise CalendarError(f"Google Calendar returned {response.status_code}: {response.text[:300]}")
        item = response.json()
        return {"event_id": item["id"], "html_link": item.get("htmlLink")}
