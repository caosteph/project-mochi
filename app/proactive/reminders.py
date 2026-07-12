"""Reminder engine — pure logic, deterministic, no LLM.

Every function takes an explicit SQLModel `Session` (and `now`, injectable for
tests) so the whole engine runs against a scratch DB with no phone and no model.
Natural-language time parsing is done by `dateparser` here, NOT by the flaky 7B —
the model just decides *to* set a reminder and hands over the phrase.
"""

import re
from datetime import datetime, timedelta, timezone

import dateparser
from dateutil.relativedelta import relativedelta
from sqlmodel import Session, select

from app.config import settings
from app.memory.models import Purchase, Recurrence, Reminder, ReminderKind, ReminderStatus

_RECURRENCES = {r.value for r in Recurrence}
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


class ReminderParseError(ValueError):
    """Raised when a natural-language time can't be resolved — surfaced to the
    user as a request for a clearer time, never a crash or a wrong reminder."""


# --- quiet hours -----------------------------------------------------------

def in_quiet_hours(now_local: datetime) -> bool:
    """True if the LOCAL wall-clock hour is inside [quiet_start, quiet_end),
    handling a window that wraps past midnight (e.g. 21:00–08:00)."""
    start, end = settings.quiet_hours_start, settings.quiet_hours_end
    if start == end:
        return False
    h = now_local.hour
    if start < end:
        return start <= h < end
    return h >= start or h < end  # wraps midnight


# --- natural-language time parsing -----------------------------------------

_TIME_OF_DAY = {
    "morning": "8am", "afternoon": "2pm", "evening": "6pm", "tonight": "9pm",
    "night": "9pm", "noon": "12pm", "midnight": "12am",
}


def _infer_recurrence(when: str) -> str | None:
    w = when.lower()
    if "every day" in w or "each day" in w or "everyday" in w or "daily" in w or "every morning" in w or "every night" in w or "every evening" in w:
        return Recurrence.DAILY.value
    if "every month" in w or "monthly" in w:
        return Recurrence.MONTHLY.value
    if "every week" in w or "weekly" in w or any(f"every {d}" in w for d in _WEEKDAYS):
        return Recurrence.WEEKLY.value
    return None


def _normalize_when(when: str) -> str:
    """Turn a reminder phrase into something dateparser reliably handles: drop the
    recurrence lead-ins ('every', 'daily', …), map 'next Friday'→'Friday' (future
    preference picks the next one anyway — 'next X' returns None otherwise), and
    map bare times-of-day ('morning') to concrete times when no clock time is given."""
    w = when.lower().strip()
    # Drop recurrence lead-ins (order matters: multi-word first).
    for kw in ("every day", "each day", "everyday", "every week", "every month",
               "daily", "weekly", "monthly", "every"):
        w = w.replace(kw, " ")
    for d in _WEEKDAYS:
        w = w.replace(f"next {d}", d)  # 'next friday' -> 'friday'
    if not re.search(r"\d", w):  # no explicit clock time → map a time-of-day word in
        for word, t in _TIME_OF_DAY.items():
            if re.search(rf"\b{word}\b", w):  # word-boundary so 'night' ≠ 'tonight'/'midnight'
                w = re.sub(rf"\b{word}\b", t, w)
                break
    w = re.sub(r"\bat\b", " ", w)  # a dangling 'at' (e.g. '  at 8am') confuses the parser
    return " ".join(w.split()).strip()


def parse_when(when: str, recurrence: str | None = None, *, now: datetime | None = None) -> tuple[datetime, str | None]:
    """Return (due_at UTC, recurrence-or-None). Raises ReminderParseError if the
    phrase can't be resolved to a sensible future time."""
    now = now or datetime.now(timezone.utc)
    rec = recurrence or _infer_recurrence(when)
    if rec is not None and rec not in _RECURRENCES:
        raise ReminderParseError(f"unknown recurrence {recurrence!r}")

    cleaned = _normalize_when(when)
    dt = dateparser.parse(
        cleaned or when,
        settings={
            "RELATIVE_BASE": now.astimezone(),  # resolve "tomorrow" relative to now, in local tz
            "PREFER_DATES_FROM": "future",
            "RETURN_AS_TIMEZONE_AWARE": True,
        },
    )
    if dt is None:
        raise ReminderParseError(f"couldn't understand the time {when!r}")
    if dt.tzinfo is None:
        dt = dt.astimezone()
    dt = dt.astimezone(timezone.utc)

    if rec:
        # Anchor a recurring reminder to its next occurrence strictly in the future.
        dt = next_occurrence(dt, rec, now) if dt <= now else dt
    elif dt <= now:
        raise ReminderParseError(f"{when!r} looks like it's in the past — give me a future time")
    return dt, rec


def next_occurrence(due_at: datetime, recurrence: str, now: datetime) -> datetime:
    """The next occurrence strictly after `now` — advancing by whole periods and
    SKIPPING any missed slots (so downtime yields one nudge, not a catch-up burst)."""
    nxt = due_at
    if recurrence == Recurrence.MONTHLY.value:
        while nxt <= now:
            nxt = nxt + relativedelta(months=1)
        return nxt
    step = timedelta(days=1) if recurrence == Recurrence.DAILY.value else timedelta(weeks=1)
    while nxt <= now:
        nxt = nxt + step
    return nxt


# --- creation --------------------------------------------------------------

def create_reminder(
    session: Session,
    *,
    text: str,
    when: str,
    recurrence: str | None = None,
    now: datetime | None = None,
) -> Reminder:
    """Create a user reminder from a natural-language time. Raises
    ReminderParseError on an unparseable/past time (nothing is created)."""
    due_at, rec = parse_when(when, recurrence, now=now)
    reminder = Reminder(
        text=text, due_at=due_at, recurrence=rec, kind=ReminderKind.GENERIC.value,
        status=ReminderStatus.PENDING.value,
    )
    session.add(reminder)
    session.commit()
    session.refresh(reminder)
    return reminder


def create_return_reminder(
    session: Session, purchase: Purchase, *, now: datetime | None = None
) -> Reminder | None:
    """Create a one-off return reminder from a Purchase — `reminder_lead_days`
    before the window closes (clamped to now). Deduped per purchase; returns None
    if there's no return_by or a reminder already exists for this purchase."""
    if purchase.return_by is None:
        return None
    now = now or datetime.now(timezone.utc)
    existing = session.exec(
        select(Reminder).where(Reminder.purchase_id == purchase.id)
    ).first()
    if existing is not None:
        return None

    due_at = purchase.return_by - timedelta(days=settings.reminder_lead_days)
    if due_at < now:
        due_at = now
    reminder = Reminder(
        text=f"Return {purchase.item} to {purchase.vendor} by {purchase.return_by:%b %d}",
        due_at=due_at, kind=ReminderKind.RETURN_WINDOW.value, purchase_id=purchase.id,
        status=ReminderStatus.PENDING.value,
    )
    session.add(reminder)
    session.commit()
    session.refresh(reminder)
    return reminder


# --- queries + state transitions -------------------------------------------

def due_reminders(session: Session, now: datetime) -> list[Reminder]:
    return list(
        session.exec(
            select(Reminder).where(
                Reminder.status == ReminderStatus.PENDING.value, Reminder.due_at <= now
            )
        ).all()
    )


def list_pending(session: Session) -> list[Reminder]:
    return list(
        session.exec(
            select(Reminder)
            .where(Reminder.status.in_([ReminderStatus.PENDING.value, ReminderStatus.SENT.value]))
            .order_by(Reminder.due_at)
        ).all()
    )


def mark_fired(session: Session, reminder: Reminder, now: datetime) -> None:
    """After a nudge is sent: a recurring reminder advances to its next occurrence
    and stays PENDING; a one-off becomes SENT."""
    reminder.sent_at = now
    if reminder.recurrence:
        reminder.due_at = next_occurrence(reminder.due_at, reminder.recurrence, now)
        reminder.status = ReminderStatus.PENDING.value
    else:
        reminder.status = ReminderStatus.SENT.value
    session.add(reminder)
    session.commit()


def mark_done(session: Session, reminder_id: int) -> Reminder | None:
    reminder = session.get(Reminder, reminder_id)
    if reminder is None:
        return None
    reminder.status = ReminderStatus.DONE.value
    session.add(reminder)
    session.commit()
    return reminder


def snooze(session: Session, reminder_id: int, *, now: datetime | None = None) -> Reminder | None:
    reminder = session.get(Reminder, reminder_id)
    if reminder is None:
        return None
    now = now or datetime.now(timezone.utc)
    reminder.due_at = now + timedelta(days=settings.reminder_snooze_days)
    reminder.status = ReminderStatus.PENDING.value
    reminder.sent_at = None
    session.add(reminder)
    session.commit()
    return reminder


def mirror_reminder(session: Session, reminder: Reminder, *, service=None) -> str | None:
    """Create a Google Calendar event for a timed reminder so it fires even if the
    app is down. Idempotent (skips if already mirrored). `service` is injectable for
    tests; google_calendar is imported lazily to keep this module's core network-free."""
    if reminder.calendar_event_id:
        return reminder.calendar_event_id
    from app.integrations import google_calendar

    start = reminder.due_at
    end = start + timedelta(hours=1)
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


def cancel_reminder(session: Session, query: str) -> Reminder | None:
    """Cancel by numeric id or a case-insensitive substring of the text."""
    reminder = None
    if query.strip().isdigit():
        reminder = session.get(Reminder, int(query.strip()))
    if reminder is None:
        candidates = session.exec(
            select(Reminder).where(
                Reminder.status.in_([ReminderStatus.PENDING.value, ReminderStatus.SENT.value])
            )
        ).all()
        q = query.lower()
        reminder = next((r for r in candidates if q in r.text.lower()), None)
    if reminder is None:
        return None
    reminder.status = ReminderStatus.CANCELLED.value
    session.add(reminder)
    session.commit()
    return reminder
