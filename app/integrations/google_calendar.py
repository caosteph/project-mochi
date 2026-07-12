"""Thin Google Calendar read wrapper. Read-only in Phase 2 (the token has no
calendar-write scope). Functions take an optional pre-built `service` so they're
unit-testable against a mock without touching the network or real credentials.
"""

from datetime import datetime, timedelta, timezone

from googleapiclient.discovery import build

from app.integrations.google_auth import get_credentials


def _service():
    return build("calendar", "v3", credentials=get_credentials(), cache_discovery=False)


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
