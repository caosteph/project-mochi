"""Pure time/recurrence logic for reminders — no DB, no Google, no LLM.

Split out of `reminders.py` (which had grown to 579 LOC across six concerns) so this — the part
that's genuinely pure and the most worth unit-testing — stands alone. Everything here is a function
of its arguments plus `settings`; nothing touches a session or the network.

Named `reminder_time`, not `scheduling`, to avoid colliding with `jobs.py` (the APScheduler tick).
`in_quiet_hours` is proactive-general — `jobs` uses it for email-signal asks too, not just reminders
— but it lives here as a wall-clock time predicate.
"""

import re
from datetime import UTC, datetime

import dateparser
from dateutil.relativedelta import relativedelta
from tzlocal import get_localzone

from app.config import settings
from app.memory.models import Recurrence

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


def same_time_of_day(a: datetime, b: datetime, window_seconds: int) -> bool:
    """True if two instants land at (about) the same local clock time, wrapping midnight —
    so 08:00 today and 08:00 tomorrow match, but 09:00 and 21:00 don't. Used by reminder dedup."""
    tz = get_localzone()
    la, lb = a.astimezone(tz), b.astimezone(tz)
    secs = lambda d: d.hour * 3600 + d.minute * 60 + d.second  # noqa: E731
    diff = abs(secs(la) - secs(lb))
    return min(diff, 86_400 - diff) <= window_seconds
