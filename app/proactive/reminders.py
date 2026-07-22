"""Reminder engine — pure logic, deterministic, no LLM.

Every function takes an explicit SQLModel `Session` (and `now`, injectable for
tests) so the whole engine runs against a scratch DB with no phone and no model.
Pure time parsing lives in `reminder_time`, Calendar mirroring in `reminder_calendar`; this module
is the reminder *domain* — create/dedup/cancel/find/retire/lifecycle. The model just decides *to*
set a reminder and hands over the phrase (parsed by dateparser in `reminder_time`, not the 7B).
"""

import logging
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, select

from app.config import settings
from app.memory.models import (
    DEADLINE_SIGNAL_TYPES,
    EmailSignal,
    Purchase,
    Reminder,
    ReminderKind,
    ReminderStatus,
    RetiredTopic,
    SignalStatus,
    SignalType,
    Task,
    TaskStatus,
)
from app.proactive import reminder_calendar, reminder_time, text_match
from app.proactive.reminder_time import (
    ReminderParseError,  # noqa: F401 — re-export: reminders.ReminderParseError is caught by name in reminder_tools + tests
)

log = logging.getLogger(__name__)


class RetiredTopicError(ValueError):
    """Raised when something tries to create a reminder for a topic Stephanie has retired.
    Consistent with ReminderParseError — the caller turns it into a friendly reply."""


def is_retired(session: Session, text: str) -> bool:
    """True if `text` fuzzy-matches a retired topic (text_match.same_thing — the matcher de-dup and
    cancel already use). Read-only; safe to call on every create."""
    return any(text_match.same_thing(text, t.text) for t in session.exec(select(RetiredTopic)))


def retire_topic(session: Session, text: str) -> tuple[str, int]:
    """Record a tombstone for `text` and clear everything already outstanding for it: cancel every
    pending/sent reminder that matches, dismiss any pending EmailSignal that matches (so an
    already-detected topic can't slip through the approve button), and mark matching open tasks
    done. Returns (text, number of reminders cancelled). Idempotent-ish: a second retire of the
    same topic just adds another tombstone row (harmless) and finds nothing left to clear.

    Returns plain values (never an ORM instance), so a caller reading the result after the session
    closes can't hit DetachedInstanceError — the bug class that bit cancel/snooze three times."""
    session.add(RetiredTopic(text=text))

    cancelled = 0
    for match in find_pending_matches(session, text):
        if cancel_reminder_by_id(session, match.id) is not None:
            cancelled += 1

    for signal in session.exec(
        select(EmailSignal).where(
            EmailSignal.status.in_([SignalStatus.DETECTED.value, SignalStatus.ASKED.value])
        )
    ):
        if text_match.same_thing(text, signal.title):
            signal.status = SignalStatus.DISMISSED.value
            session.add(signal)

    for task in session.exec(select(Task).where(Task.status == TaskStatus.OPEN.value)):
        if text_match.same_thing(text, task.text):
            task.status = TaskStatus.DONE.value
            task.completed_at = datetime.now(UTC)
            session.add(task)

    session.commit()
    return text, cancelled


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
        reminder_calendar.mirror_reminder(session, reminder, duration_minutes=duration_minutes)
    except Exception:  # the reminder stands even if the calendar write fails — but log it
        log.warning("Calendar mirror failed for reminder %s (reminder still active)", reminder.id, exc_info=True)


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
    return delta <= horizon and reminder_time.same_time_of_day(a_due, b_due, window)


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
                reminder_calendar.delete_mirror(r)  # also remove its orphaned calendar event
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
    Raises ReminderParseError on an unparseable/past time, or RetiredTopicError if she's told me
    this topic is over (nothing is created in either case).
    """
    if is_retired(session, text):
        raise RetiredTopicError(text)
    due_at, rec = reminder_time.parse_when(when, recurrence, now=now)
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
        reminder.due_at = reminder_time.next_occurrence(reminder.due_at, reminder.recurrence, now)
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
    reminder_calendar.delete_mirror(reminder)
    reminder.status = ReminderStatus.CANCELLED.value
    session.add(reminder)
    session.commit()
    return reminder


def cancel_reminder(session: Session, query: str) -> Reminder | None:
    """Cancel the best match for `query` (or None if nothing matches). Convenience wrapper over
    find_pending_matches + cancel_reminder_by_id; the tool layer handles ambiguity via a choice."""
    matches = find_pending_matches(session, query)
    return cancel_reminder_by_id(session, matches[0].id) if matches else None
