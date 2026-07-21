"""Daily briefing — a deterministic morning digest.

Deliberately **no LLM**: the briefing is assembled purely in code from trusted
sources (today's Google Calendar, reminders due today, active goals/open tasks),
so it can't go incoherent or dump raw JSON — the exact failure mode that made the
model-driven replies untrustworthy. Everything is injectable (`service`, `now`) so
the whole thing runs offline against a scratch DB + a mock calendar.

Email is intentionally excluded for now — the email scanner has been the noisy
source; it can be folded in once it's proven quiet.
"""

import logging
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, select
from tzlocal import get_localzone

from app.integrations import google_calendar
from app.memory.models import Goal, GoalStatus, Reminder, Task, TaskStatus
from app.proactive import reminders

log = logging.getLogger(__name__)

_MAX_GOALS = 3
_MAX_TASKS = 5


# --- today's window (local) -------------------------------------------------

def _today_bounds(now: datetime) -> tuple[str, str]:
    """RFC3339 [start, end) of the local day containing `now` — computed in code
    (not by the 7B, which is unreliable at date math)."""
    local = now.astimezone(get_localzone())
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1) - timedelta(seconds=1)
    return start.isoformat(), end.isoformat()


def due_today(session: Session, now: datetime) -> list[Reminder]:
    """Pending/sent reminders whose due_at falls on today (local wall-clock)."""
    tz = get_localzone()
    local = now.astimezone(tz)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return [r for r in reminders.list_pending(session) if start <= r.due_at.astimezone(tz) < end]


# --- sections (each returns [] when empty, so it's simply omitted) ----------

def _calendar_section(now: datetime, service) -> list[str]:
    try:
        start_iso, end_iso = _today_bounds(now)
        events = google_calendar.list_events(start_iso, end_iso, max_results=20, service=service)
    except Exception:  # a calendar hiccup must not sink the whole briefing
        log.warning("briefing: calendar fetch failed; omitting that section", exc_info=True)
        return []
    if not events:
        return []
    return ["📅 On your calendar today:", *(google_calendar.format_event(e, with_date=False) for e in events)]


def _reminders_section(session: Session, now: datetime) -> list[str]:
    rems = due_today(session, now)
    if not rems:
        return []
    tz = get_localzone()
    return [
        "⏰ Reminders due today:",
        *(f"• {r.due_at.astimezone(tz):%-I:%M %p} — {r.text}" for r in rems),
    ]


def _goals_section(session: Session) -> list[str]:
    goals = session.exec(
        select(Goal)
        .where(Goal.status == GoalStatus.ACTIVE.value)
        .order_by(Goal.created_at.desc())
        .limit(_MAX_GOALS)
    ).all()
    tasks = session.exec(
        select(Task)
        .where(Task.status == TaskStatus.OPEN.value)
        .order_by(Task.created_at.desc())
        .limit(_MAX_TASKS)
    ).all()
    lines: list[str] = []
    if goals:
        lines += ["🎯 Goals you're working on:", *(f"• {g.text}" for g in goals)]
    if tasks:
        lines += ["✅ Open tasks:", *(f"• {t.text}" for t in tasks)]
    return lines


# --- assembly ---------------------------------------------------------------

def build_briefing(session: Session, *, now: datetime | None = None, service=None) -> str:
    """The morning digest as one plain-text message — deterministic, no model.
    Empty sections are omitted; a genuinely empty day gets a short, warm line."""
    now = now or datetime.now(UTC)
    header = f"☀️ Morning, Stephanie — {now.astimezone(get_localzone()):%A, %b %-d}."

    blocks = [
        _calendar_section(now, service),
        _reminders_section(session, now),
        _goals_section(session),
    ]
    body: list[str] = []
    for block in blocks:
        if not block:
            continue
        if body:
            body.append("")  # blank line between populated sections
        body += block

    if not body:
        return f"{header}\n\nClear day ahead — nothing on the calendar, no reminders due. Enjoy it. 🌤️"
    return header + "\n\n" + "\n".join(body)
