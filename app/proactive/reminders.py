"""Reminder engine — pure logic, deterministic, no LLM.

Every function takes an explicit SQLModel `Session` (and `now`, injectable for
tests) so the whole engine runs against a scratch DB with no phone and no model.
Natural-language time parsing is done by `dateparser` here, NOT by the flaky 7B —
the model just decides *to* set a reminder and hands over the phrase.
"""

import logging
import re
from datetime import UTC, datetime, timedelta

import dateparser
from dateutil.relativedelta import relativedelta
from sqlmodel import Session, select
from tzlocal import get_localzone

from app.config import settings
from app.memory.models import (
    DEADLINE_SIGNAL_TYPES,
    EmailSignal,
    Purchase,
    Recurrence,
    Reminder,
    ReminderKind,
    ReminderStatus,
    SignalStatus,
    SignalType,
)
from app.proactive import text_match

log = logging.getLogger(__name__)

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
    now = now or datetime.now(UTC)
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
    dt = dt.astimezone(UTC)

    if rec:
        # Anchor a recurring reminder to its next occurrence strictly in the future.
        dt = next_occurrence(dt, rec, now) if dt <= now else dt
    elif dt <= now:
        raise ReminderParseError(f"{when!r} looks like it's in the past — give me a future time")
    return dt, rec


def next_occurrence(due_at: datetime, recurrence: str, now: datetime) -> datetime:
    """The next occurrence strictly after `now` — advancing by whole periods and
    SKIPPING any missed slots (so downtime yields one nudge, not a catch-up burst).

    Computed in the local IANA zone (DST-aware) so a "daily 8am" reminder stays 8am
    *local* across a DST change, instead of drifting an hour (which a fixed UTC delta
    would cause). `relativedelta` on a zone-aware datetime preserves wall-clock; the
    zone supplies the right offset for the new date."""
    step = {
        Recurrence.DAILY.value: relativedelta(days=1),
        Recurrence.WEEKLY.value: relativedelta(weeks=1),
        Recurrence.MONTHLY.value: relativedelta(months=1),
    }[recurrence]
    local = due_at.astimezone(get_localzone())
    while local.astimezone(UTC) <= now:
        local = local + step
    return local.astimezone(UTC)


# --- creation --------------------------------------------------------------

def _maybe_mirror(
    session: Session, reminder: Reminder, duration_minutes: int | None, mirror: bool | None
) -> None:
    """Mirror to Google Calendar if enabled — best-effort (a mirror failure must
    never lose the reminder itself). Both creation paths call this, so user and
    return reminders behave the same."""
    do_mirror = settings.calendar_mirror_enabled if mirror is None else mirror
    if not do_mirror:
        return
    try:
        mirror_reminder(session, reminder, duration_minutes=duration_minutes)
    except Exception:  # the reminder stands even if the calendar write fails — but log it
        log.warning("Calendar mirror failed for reminder %s (reminder still active)", reminder.id, exc_info=True)


def _same_time_of_day(a: datetime, b: datetime, window_seconds: int) -> bool:
    """True if two instants land at (about) the same local clock time, wrapping midnight —
    so 08:00 today and 08:00 tomorrow match, but 09:00 and 21:00 don't."""
    tz = get_localzone()
    la, lb = a.astimezone(tz), b.astimezone(tz)
    secs = lambda d: d.hour * 3600 + d.minute * 60 + d.second  # noqa: E731
    diff = abs(secs(la) - secs(lb))
    return min(diff, 86_400 - diff) <= window_seconds


def _is_same_reminder(a_text: str, a_due: datetime, b_text: str, b_due: datetime) -> bool:
    """Whether two reminders are the same task, for dedup purposes.

    Same task (`text_match.same_thing`) AND either due at nearly the same instant, OR — the case
    that actually bit Stephanie — already pending at the SAME time of day within the horizon.
    The ±window-only rule caught same-day re-asks but not a task recreated on later days, so
    "Perplexity prep" accumulated 8 rows and she hand-cancelled 26 reminders. Different times of
    day stay distinct, so a real twice-a-day reminder still works.
    """
    if not text_match.same_thing(a_text, b_text):
        return False
    window = settings.reminder_dedup_window_minutes * 60
    delta = abs((a_due - b_due).total_seconds())
    if delta <= window:
        return True
    horizon = settings.reminder_dedup_horizon_days * 86_400
    return delta <= horizon and _same_time_of_day(a_due, b_due, window)


def _find_duplicate(session: Session, text: str, due_at: datetime) -> Reminder | None:
    """An existing PENDING reminder for the same task — so repeated asks, double tool-calls, and
    day-after-day recreation don't pile up. See `_is_same_reminder` for the rule."""
    candidates = session.exec(
        select(Reminder).where(Reminder.status == ReminderStatus.PENDING.value)
    ).all()
    for r in candidates:
        if _is_same_reminder(r.text, r.due_at, text, due_at):
            return r
    return None


def dedupe_pending_reminders(session: Session, *, dry_run: bool = False) -> list[int]:
    """One-time cleanup: cancel already-accumulated duplicate PENDING reminders, keeping the
    earliest of each same-task group (same due-window). The create-time dedup stops NEW dupes;
    this clears the pre-existing backlog. Reversible (status→cancelled). Returns cancelled ids."""
    pending = session.exec(
        select(Reminder).where(Reminder.status == ReminderStatus.PENDING.value).order_by(Reminder.id)
    ).all()
    kept: list[tuple[str, datetime]] = []
    cancelled: list[int] = []
    for r in pending:
        is_dup = any(_is_same_reminder(kt, kd, r.text, r.due_at) for kt, kd in kept)
        if is_dup:
            cancelled.append(r.id)
            if not dry_run:
                _delete_mirror(r)  # also remove its orphaned calendar event
                r.status = ReminderStatus.CANCELLED.value
                session.add(r)
        else:
            kept.append((r.text, r.due_at))
    if not dry_run:
        session.commit()
    return cancelled


def create_or_get_reminder(
    session: Session,
    *,
    text: str,
    when: str,
    recurrence: str | None = None,
    duration_minutes: int | None = None,
    mirror: bool | None = None,
    now: datetime | None = None,
) -> tuple[Reminder, bool]:
    """Create a reminder, or return the existing one it duplicates.

    Returns `(reminder, created)`. The flag matters for what the agent *says*: silently
    reporting "done, I'll remind you" when nothing new was created is how Stephanie ended up
    believing she had reminders she didn't, and being surprised by ones she did.
    Raises ReminderParseError on an unparseable/past time (nothing is created).
    """
    due_at, rec = parse_when(when, recurrence, now=now)
    duplicate = _find_duplicate(session, text, due_at)
    if duplicate is not None:
        return duplicate, False
    reminder = Reminder(
        text=text, due_at=due_at, recurrence=rec, kind=ReminderKind.GENERIC.value,
        status=ReminderStatus.PENDING.value,
    )
    session.add(reminder)
    session.commit()
    session.refresh(reminder)
    _maybe_mirror(session, reminder, duration_minutes, mirror)
    return reminder, True


def create_reminder(
    session: Session,
    *,
    text: str,
    when: str,
    recurrence: str | None = None,
    duration_minutes: int | None = None,
    mirror: bool | None = None,
    now: datetime | None = None,
) -> Reminder:
    """Create a user reminder from a natural-language time, and (if mirroring is on) a matching
    calendar event. A near-duplicate of an existing pending reminder is returned instead of
    re-created. Use `create_or_get_reminder` when you need to know which happened."""
    reminder, _ = create_or_get_reminder(
        session, text=text, when=when, recurrence=recurrence,
        duration_minutes=duration_minutes, mirror=mirror, now=now,
    )
    return reminder


def create_return_reminder(
    session: Session, purchase: Purchase, *, mirror: bool | None = None, now: datetime | None = None
) -> Reminder | None:
    """Create a one-off return reminder from a Purchase — `reminder_lead_days`
    before the window closes (clamped to now) — and mirror it to the calendar like
    any other timed reminder. Deduped per purchase; returns None if there's no
    return_by or a reminder already exists for this purchase."""
    if purchase.return_by is None:
        return None
    now = now or datetime.now(UTC)
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
    _maybe_mirror(session, reminder, None, mirror)
    return reminder


_SIGNAL_REMINDER_TEXT = {
    SignalType.RETURN.value: "Return {t}",
    SignalType.BILL.value: "Pay {t}",
    SignalType.DELIVERY.value: "Look out for {t}",
}


def _signal_reminder_text(signal: EmailSignal) -> str:
    return _SIGNAL_REMINDER_TEXT.get(signal.signal_type, "{t}").format(t=signal.title)


def create_from_signal(
    session: Session, signal: EmailSignal, *, mirror: bool | None = None, now: datetime | None = None
) -> Reminder:
    """Turn an approved EmailSignal into a reminder (and a mirrored calendar event).
    This is the general path — a return is just one `signal_type`. Lead-time is by
    type: deadline-style signals (return/bill/deadline) fire `reminder_lead_days`
    BEFORE the due date (clamped to now), while appointment/delivery fire AT it. A
    signal with no due date defaults to a next-day nudge. Idempotent: re-approving a
    signal returns its existing reminder (linked via signal.reminder_id)."""
    now = now or datetime.now(UTC)
    if signal.reminder_id is not None:
        existing = session.get(Reminder, signal.reminder_id)
        if existing is not None:
            return existing

    due = signal.due_date
    if due is None:
        due = now + timedelta(days=1)  # no date extracted → a gentle next-day nudge
    elif signal.signal_type in DEADLINE_SIGNAL_TYPES:
        due = due - timedelta(days=settings.reminder_lead_days)
        if due < now:
            due = now

    kind = (
        ReminderKind.RETURN_WINDOW.value
        if signal.signal_type == SignalType.RETURN.value
        else ReminderKind.GENERIC.value
    )
    reminder = Reminder(
        text=_signal_reminder_text(signal), due_at=due, kind=kind,
        status=ReminderStatus.PENDING.value,
    )
    session.add(reminder)
    session.commit()
    session.refresh(reminder)

    signal.reminder_id = reminder.id
    signal.status = SignalStatus.CONFIRMED.value
    session.add(signal)
    session.commit()

    _maybe_mirror(session, reminder, None, mirror)
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
    now = now or datetime.now(UTC)
    reminder.due_at = now + timedelta(days=settings.reminder_snooze_days)
    reminder.status = ReminderStatus.PENDING.value
    reminder.sent_at = None
    session.add(reminder)
    session.commit()
    return reminder


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


def _delete_mirror(reminder: Reminder) -> None:
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


def find_pending_matches(session: Session, query: str) -> list[Reminder]:
    """Pending/sent reminders matching `query`, best match first — READ-ONLY.

    Split out so a caller can see whether the query is ambiguous (more than one match) and offer
    a choice, rather than silently cancelling one guess. Being read-only, it's safe to re-run,
    which the interrupt/resume flow does.

    Matching is deliberately forgiving — substring alone was too strict, since the model passes
    back whatever phrasing she used, so "the dentist reminder" has to match the stored "dentist
    appointment" (it didn't, and even the docstring example 'the mom reminder' failed). Falls
    back to `text_match.same_thing`, the fuzzy matcher this project already uses for de-dup.
    Ordering: exact substring matches first, then fuzzy, each ranked by content-word overlap and
    then soonest due — so `matches[0]` is the best single guess.
    """
    if query.strip().isdigit():
        one = session.get(Reminder, int(query.strip()))
        return [one] if one and one.status in (ReminderStatus.PENDING.value, ReminderStatus.SENT.value) else []

    candidates = session.exec(
        select(Reminder).where(
            Reminder.status.in_([ReminderStatus.PENDING.value, ReminderStatus.SENT.value])
        )
    ).all()
    q = query.lower()
    q_words = text_match.content_words(query)

    def rank(r: Reminder) -> tuple:
        return (len(q_words & text_match.content_words(r.text)), -r.due_at.timestamp())

    exact = sorted((r for r in candidates if q in r.text.lower()), key=rank, reverse=True)
    fuzzy = sorted(
        (r for r in candidates if q not in r.text.lower() and text_match.same_thing(query, r.text)),
        key=rank,
        reverse=True,
    )
    return exact + fuzzy


def cancel_reminder_by_id(session: Session, reminder_id: int) -> Reminder | None:
    """Cancel a specific reminder and remove its mirrored calendar event. Returns None if it's
    gone or already cancelled (so a stale button tap is harmless)."""
    reminder = session.get(Reminder, reminder_id)
    if reminder is None or reminder.status == ReminderStatus.CANCELLED.value:
        return None
    _delete_mirror(reminder)
    reminder.status = ReminderStatus.CANCELLED.value
    session.add(reminder)
    session.commit()
    return reminder


def cancel_reminder(session: Session, query: str) -> Reminder | None:
    """Cancel the best match for `query` (or None if nothing matches). Convenience wrapper over
    find_pending_matches + cancel_reminder_by_id; the tool layer handles ambiguity via a choice."""
    matches = find_pending_matches(session, query)
    return cancel_reminder_by_id(session, matches[0].id) if matches else None
