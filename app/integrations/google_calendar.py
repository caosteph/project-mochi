"""Thin Google Calendar read wrapper. Read-only in Phase 2 (the token has no
calendar-write scope). Functions take an optional pre-built `service` so they're
unit-testable against a mock without touching the network or real credentials.
"""

from datetime import datetime, timedelta, timezone

from googleapiclient.discovery import build

from app.integrations.google_auth import get_credentials


_service_cache = None


def _service():
    # Cache the built service — build() re-fetches the API discovery doc each call,
    # so rebuilding per request adds needless latency. google-auth refreshes the
    # cached credentials' access token in-memory when it expires, so this stays valid.
    global _service_cache
    if _service_cache is None:
        _service_cache = build("calendar", "v3", credentials=get_credentials(), cache_discovery=False)
    return _service_cache


def reset_service_cache() -> None:
    """Drop the cached service — call after an OAuth re-consent (new credentials)."""
    global _service_cache
    _service_cache = None


def list_events(
    start_iso: str | None = None,
    end_iso: str | None = None,
    max_results: int = 10,
    *,
    service=None,
) -> list[dict]:
    """Return upcoming primary-calendar events between start_iso and end_iso
    (RFC3339). Defaults to the next 7 days. Each event is a small dict — no raw
    description text is surfaced, only summary/time/location.
    """
    service = service or _service()
    now = datetime.now(timezone.utc)
    start = start_iso or now.isoformat()
    end = end_iso or (now + timedelta(days=7)).isoformat()

    resp = (
        service.events()
        .list(
            calendarId="primary",
            timeMin=start,
            timeMax=end,
            maxResults=max_results,
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )
    events = []
    for e in resp.get("items", []):
        start_field = e.get("start", {})
        end_field = e.get("end", {})
        events.append(
            {
                "summary": e.get("summary", "(no title)"),
                "start": start_field.get("dateTime") or start_field.get("date"),
                "end": end_field.get("dateTime") or end_field.get("date"),
                "location": e.get("location"),
            }
        )
    return events


def create_event(
    summary: str, start_iso: str, end_iso: str, *, popup_minutes: int = 0, service=None
) -> dict:
    """Create a primary-calendar event with a popup reminder. Used only by the
    reminder engine to mirror reminders (no agent-facing update/delete tool exists)."""
    service = service or _service()
    body = {
        "summary": summary,
        "start": {"dateTime": start_iso},
        "end": {"dateTime": end_iso},
        "reminders": {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": popup_minutes}],
        },
    }
    return service.events().insert(calendarId="primary", body=body).execute()


def delete_event(event_id: str, *, service=None) -> None:
    """Delete an event — used by verify_phase3.py to clean up its test event."""
    service = service or _service()
    service.events().delete(calendarId="primary", eventId=event_id).execute()
