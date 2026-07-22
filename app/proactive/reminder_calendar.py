"""Mirroring reminders into Google Calendar — the one integration concern pulled out of
`reminders.py` so the reminder domain stays free of Google I/O.

A timed reminder is mirrored to a Calendar event so the popup still fires if the app is down;
cancelling deletes that event so no orphaned "⏰ …" entry is left behind. Both operations are
best-effort and touch only events Mochi created (by stored id) — never a real user event. Imports
`google_calendar` lazily so this module's import is network-free.
"""

import logging
from datetime import timedelta

from sqlmodel import Session

from app.config import settings
from app.memory.models import Reminder

log = logging.getLogger(__name__)


def mirror_reminder(
    session: Session, reminder: Reminder, *, duration_minutes: int | None = None, service=None
) -> str | None:
    """Create a Google Calendar event for a timed reminder so it fires even if the
    app is down. Idempotent (skips if already mirrored). `service` is injectable for
    tests; google_calendar is imported lazily to keep this module's core network-free.

    The event's length is `duration_minutes` (the model estimates it from the task
    when it implies one, e.g. a 2-hour meeting) or a short default — it's cosmetic:
    the popup fires at the start regardless, and a reminder is a moment, not a block."""
    if reminder.calendar_event_id:
        return reminder.calendar_event_id
    from app.integrations import google_calendar

    minutes = duration_minutes or settings.reminder_event_default_minutes
    start = reminder.due_at
    end = start + timedelta(minutes=minutes)
    event = google_calendar.create_event(
        summary=f"⏰ {reminder.text}",
        start_iso=start.isoformat(),
        end_iso=end.isoformat(),
        popup_minutes=0,
        service=service,
    )
    reminder.calendar_event_id = event.get("id")
    session.add(reminder)
    session.commit()
    return reminder.calendar_event_id


def delete_mirror(reminder: Reminder) -> None:
    """Delete a reminder's mirrored Google Calendar event, if any. Cancelling a reminder
    must not leave an orphaned '⏰ …' event cluttering the calendar (a real bug — cancelled
    duplicates left their events behind). Best-effort; only deletes the event Mochi created
    (by its stored id), never a real user event."""
    if not reminder.calendar_event_id:
        return
    try:
        from app.integrations import google_calendar

        google_calendar.delete_event(reminder.calendar_event_id)
    except Exception:
        log.warning("failed to delete mirrored event for reminder %s", reminder.id, exc_info=True)
    reminder.calendar_event_id = None
