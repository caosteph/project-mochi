"""Email → actionable-signal ingestion — the pipeline on top of the quarantined
reader. Deterministic and offline-testable: `service` (Gmail) and `extractor`
(the reader) are injectable, so the whole thing runs against a mock inbox and a
fake reader with no network and no model.

Flow per scan: search recent mail → skip ids already processed → on the very first
run, baseline-skip everything (go-forward-only, no backfill) → for up to
`signal_max_per_scan` NEW messages, read the body, run the quarantined reader, and
if it's an actionable signal, store an EmailSignal(status="detected"). The proactive
approval ask + reminder creation happen later (jobs.py + telegram.py) — this module
only detects and records.

Cost is bounded by capping *reader invocations + body fetches* per scan (not just
the resulting signals): running the local 7B on every email would be minutes of
compute, so we process at most N new messages and let the rest wait for the next scan.
"""

import logging
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

import dateparser
from sqlmodel import Session, select
from tzlocal import get_localzone

from app.agent import quarantine
from app.config import settings
from app.integrations import google_gmail
from app.memory.models import EmailSignal, IngestState, ProcessedEmail, SignalStatus, SignalType
from app.proactive import reminders, text_match

log = logging.getLogger(__name__)

_SEARCH_MAX = 100  # ids fetched per scan; reader work is separately capped below


def _scan_query() -> str:
    """Broad-but-bounded: recent, and only the Primary/Updates categories (skip the
    promotions/social noise where actionable mail almost never lives). A general
    extractor can't filter on 'receipt' keywords, so we filter on recency + category
    and let the reader decide what's actionable."""
    return f"newer_than:{settings.signal_scan_window_days}d -category:promotions -category:social"


# --- due-date resolution ----------------------------------------------------

def _parse_due(raw: str) -> datetime | None:
    """Parse an extracted date string into tz-aware UTC. A date-only value (no clock
    time) is given a sensible local hour (10am) so a reminder never fires at midnight
    UTC; a naive datetime is assumed local. Returns None if unparseable."""
    raw = raw.strip()
    dt = None
    date_only = False
    try:
        dt = datetime.fromisoformat(raw)
        date_only = len(raw) <= 10  # "YYYY-MM-DD" and shorter carry no time
    except ValueError:
        dt = dateparser.parse(raw, settings={"PREFER_DATES_FROM": "future"})
        if dt is None:
            return None
        date_only = dt.hour == 0 and dt.minute == 0 and dt.second == 0
    if date_only:
        dt = dt.replace(hour=10, minute=0, second=0, microsecond=0)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=get_localzone())
    return dt.astimezone(UTC)


def resolve_due_date(
    signal_type: str, extracted_due: str | None, received_at: datetime | None, *, now: datetime | None = None
) -> datetime | None:
    """The date the signal is 'due': the extracted date if the reader found one;
    otherwise, for a `return` with no stated date, a simple default window from when
    the email arrived; otherwise None (no date → the reminder is still offered, just
    without a firm time — jobs.py handles the no-date case)."""
    now = now or datetime.now(UTC)
    if extracted_due:
        parsed = _parse_due(extracted_due)
        if parsed is not None:
            return parsed
    if signal_type == SignalType.RETURN.value:
        base = received_at or now
        return base + timedelta(days=settings.signal_default_return_days)
    return None


def _received_at(email: dict) -> datetime | None:
    raw = email.get("date")
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if dt is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


# --- dedup + first-run state ------------------------------------------------

def _state(session: Session) -> IngestState:
    st = session.get(IngestState, 1)
    if st is None:
        st = IngestState(id=1, initialized_at=None)
        session.add(st)
        session.commit()
        session.refresh(st)
    return st


def _is_initialized(session: Session) -> bool:
    return _state(session).initialized_at is not None


def _mark_initialized(session: Session, now: datetime) -> None:
    st = _state(session)
    st.initialized_at = now
    session.add(st)
    session.commit()


def _already_processed(session: Session, message_id: str) -> bool:
    return session.exec(
        select(ProcessedEmail).where(ProcessedEmail.message_id == message_id)
    ).first() is not None


def _mark_processed(session: Session, message_id: str, outcome: str) -> None:
    session.add(ProcessedEmail(message_id=message_id, outcome=outcome))
    session.commit()


# --- the scan ---------------------------------------------------------------

def _is_duplicate_signal(session: Session, title: str, now: datetime, days: int = 14) -> bool:
    """True if a recent signal (any status) is about the same thing — so multiple emails on
    one topic (e.g. six 'Palantir Offer …' threads) produce a single offer, not a burst."""
    cutoff = now - timedelta(days=days)
    recent = session.exec(select(EmailSignal).where(EmailSignal.created_at >= cutoff)).all()
    return any(text_match.same_thing(title, s.title) for s in recent)


def _already_on_calendar(title: str, due: datetime, *, service=None) -> bool:
    """True if a matching event is already on her calendar around `due` — because then a reminder
    is redundant: the calendar event IS the reminder (and a Mochi reminder would even mirror back a
    duplicate event). Matches a ±1-day window by fuzzy title (`text_match.same_thing`).

    Fail-OPEN: any calendar error → False (surface the signal). A calendar hiccup must never silently
    swallow a real return-window/bill reminder. `google_calendar` is imported lazily to keep this
    module's import network-free."""
    from app.integrations import google_calendar

    try:
        start = (due - timedelta(days=1)).isoformat()
        end = (due + timedelta(days=1)).isoformat()
        events = google_calendar.list_events(start, end, max_results=25, service=service)
    except Exception:
        log.warning("calendar check failed for %r; surfacing the signal (fail-open)", title, exc_info=True)
        return False
    return any(text_match.same_thing(title, e.get("summary") or "") for e in events)


def ingest_signals(session: Session, *, service=None, extractor=None, now: datetime | None = None,
                   shadow: bool = False) -> list[EmailSignal]:
    """Scan recent mail and record any new actionable signals. Returns the newly
    created EmailSignals (status='detected'). On the first-ever run, baseline-skips
    the existing inbox (go-forward-only) and returns [].

    `shadow=True` is the observe-without-touching-her mode: a detection is LOGGED and the message
    marked processed, but **no EmailSignal row is stored and nothing is asked** — so a shadow
    detection can't later suppress a real ask via dedup, and flipping to live has no backlog. Used
    to hand-check precision for a few days before any ask reaches her. Returns [] in shadow."""
    now = now or datetime.now(UTC)
    extractor = extractor or quarantine.extract_signal

    ids = google_gmail.search_message_ids(_scan_query(), max_results=_SEARCH_MAX, service=service)

    if not _is_initialized(session):
        # First run: establish the go-forward line — mark everything present now as
        # seen WITHOUT extracting, so we only ever act on mail that arrives later.
        for mid in ids:
            if not _already_processed(session, mid):
                _mark_processed(session, mid, "baseline")
        _mark_initialized(session, now)
        return []

    created: list[EmailSignal] = []
    reads = 0
    for mid in ids:
        if reads >= settings.signal_max_per_scan:
            break  # cost bound: at most N bodies fetched + reader calls per scan
        if _already_processed(session, mid):
            continue
        reads += 1
        try:
            email = google_gmail.get_message_body(mid, service=service)
            sig = extractor(email)
            due = resolve_due_date(sig.signal_type, sig.due_date, _received_at(email), now=now)
            # Skip: not actionable, no title, or (noise filter) no concrete date. A dateless
            # "FYI"/"discussion"/marketing item is exactly the noise Stephanie flagged.
            if (
                not sig.is_actionable
                or not (sig.title and sig.title.strip())
                or (settings.signal_require_due_date and due is None)
            ):
                _mark_processed(session, mid, "skipped")
                continue
            # Dedup: don't offer the same thing twice — six "Palantir Offer …" emails → one offer.
            if _is_duplicate_signal(session, sig.title, now):
                _mark_processed(session, mid, "duplicate")
                continue
            # Never nag about a topic she's retired ("I already did that"). This is why
            # re-enabling the scanner depends on task-retirement existing.
            if reminders.is_retired(session, sig.title):
                _mark_processed(session, mid, "retired")
                continue
            # Already on her calendar? Then the event IS the reminder — surfacing one is redundant
            # (and would mirror back a duplicate event). Her rule; only for dated signals.
            if settings.signal_skip_calendared and due and _already_on_calendar(sig.title, due):
                if shadow:
                    log.info("SHADOW-SKIP reason=on_calendar src=gmail:%s title=%r", mid, sig.title.strip())
                _mark_processed(session, mid, "on_calendar")
                session.commit()
                continue
            if shadow:
                # Observe only: log what we WOULD surface, mark processed so it isn't re-scanned,
                # store nothing and ask nothing. This is the precision-review record.
                log.info(
                    "SHADOW-SIGNAL type=%s due=%s src=gmail:%s title=%r",
                    sig.signal_type, (due.date().isoformat() if due else "none"), mid, sig.title.strip(),
                )
                _mark_processed(session, mid, "shadow")
                session.commit()
                continue
            row = EmailSignal(
                source=f"gmail:{mid}",
                signal_type=sig.signal_type,
                title=sig.title.strip(),
                summary=sig.summary,
                due_date=due,
                amount=sig.amount,
                currency=sig.currency,
                status=SignalStatus.DETECTED.value,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            _mark_processed(session, mid, "signal")
            created.append(row)
        except Exception:  # one malformed body / bad extraction can't wedge the scan
            log.exception("signal ingest failed for message %s; skipping", mid)
            _mark_processed(session, mid, "error")
    return created


# --- proactive phrasing (deterministic — never model-generated) -------------

def _when(signal: EmailSignal) -> str:
    return f" by {signal.due_date.astimezone():%b %-d}" if signal.due_date else ""


def suggest_text(signal: EmailSignal) -> str:
    """The approval ask, composed ONLY from validated, length-capped fields — never
    the raw email body and never model-generated, so untrusted content can't smuggle
    instructions into a message Mochi sends."""
    t = signal.title
    when = _when(signal)
    templates = {
        SignalType.RETURN.value: f"🛍️ Looks like you got {t}. Want me to remind you to return it{when}?",
        SignalType.BILL.value: f"💸 {t} looks due{when}. Want a reminder to pay it?",
        SignalType.APPOINTMENT.value: f"📅 {t}{when}. Want a reminder?",
        SignalType.DEADLINE.value: f"⏳ Deadline — {t}{when}. Want a reminder?",
        SignalType.DELIVERY.value: f"📦 {t} is on the way{when}. Want a reminder to look out for it?",
    }
    return templates.get(signal.signal_type, f"📌 {t}{when}. Want a reminder?")
