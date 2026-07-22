"""Cross-cutting regression tests — the bugs that actually reached Stephanie, exercised
across module seams. Component-level cases live with their modules (test_reminders.py,
test_email_signals.py, test_tool_select.py, test_edge_cases.py); this file covers the
*integration* seams where those units meet — which is exactly where the duplicate-reminder
and orphaned-calendar-event failures actually happened.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from sqlmodel import Session
from tzlocal import get_localzone

from app.memory.models import ReminderStatus
from app.proactive import briefing, jobs, reminders
from tests.support import FakeBot

UTC = UTC


def test_reminder_lifecycle_create_fire_once_cancel_cleans_mirror(engine, monkeypatch):
    """The full reminder lifecycle in one flow — the seam where the real bugs lived: a
    reminder mirrors to Calendar on create, fires EXACTLY once at its due time, and
    cancelling deletes the mirrored event (no orphaned '⏰ …' event left behind — the
    pollution Stephanie hit when cancelling piled-up reminders)."""
    from app.integrations import google_calendar

    created, deleted = [], []
    monkeypatch.setattr(google_calendar, "create_event", lambda **k: created.append(k) or {"id": "evt_1"})
    monkeypatch.setattr(google_calendar, "delete_event", lambda eid, **k: deleted.append(eid))
    monkeypatch.setattr("app.config.settings.calendar_mirror_enabled", True)
    monkeypatch.setattr("app.config.settings.quiet_hours_start", 0)
    monkeypatch.setattr("app.config.settings.quiet_hours_end", 0)  # never quiet
    jobs.set_enabled(True)

    now = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    with Session(engine) as s:
        r = reminders.create_reminder(s, text="return jacket", when="in 2 hours", now=now)
        assert r.calendar_event_id == "evt_1" and len(created) == 1  # mirrored on create

        later = now + timedelta(hours=2, minutes=1)
        bot = FakeBot()
        assert asyncio.run(jobs.run_reminder_tick(bot, s, chat_id=1, now=later)) == 1  # fires once
        assert len(bot.texts) == 1 and "return jacket" in bot.texts[0]
        s.refresh(r)
        assert r.status == ReminderStatus.SENT.value  # one-off consumed

        assert asyncio.run(jobs.run_reminder_tick(FakeBot(), s, 1, now=later)) == 0  # exactly-once

        cancelled = reminders.cancel_reminder(s, "jacket")
        assert cancelled.status == ReminderStatus.CANCELLED.value
        assert deleted == ["evt_1"] and cancelled.calendar_event_id is None  # no orphan


def test_reminder_created_today_flows_into_the_briefing(engine, monkeypatch):
    """Integration across the parser → briefing seam: a reminder set for later today
    (parsed by dateparser, not the model) shows up in the morning digest."""
    monkeypatch.setattr("app.config.settings.calendar_mirror_enabled", False)
    now = datetime.now(get_localzone()).replace(hour=8, minute=0, second=0, microsecond=0)
    svc = MagicMock()
    svc.events().list().execute.return_value = {"items": []}
    with Session(engine) as s:
        reminders.create_reminder(s, text="call the dentist", when="today at 4pm", now=now)
        out = briefing.build_briefing(s, now=now, service=svc)
    assert "call the dentist" in out


# --- the duplicate-reminder spam she actually hit ---------------------------
# Production data (2026-07-20): "Perplexity prep" ×8, "submit health insurance claims" ×10
# across two casings, "yoga class" ×7 — and 26 of 34 reminders hand-cancelled. She wrote
# "STOP!!!!!" and "I DONT Wng these reminders". Root cause: dedup only matched reminders
# whose due_at was within ±60 min, so the SAME task recreated for the next day never collapsed.

def _pending(session) -> list:
    return reminders.list_pending(session)


def test_same_task_recreated_next_day_does_not_duplicate(engine, monkeypatch):
    """The 'Perplexity prep' ×8 bug: asked again on a later day, at the same hour, it must
    return the existing reminder instead of stacking another one."""
    monkeypatch.setattr("app.config.settings.calendar_mirror_enabled", False)
    now = datetime.now(get_localzone()).replace(hour=6, minute=0, second=0, microsecond=0)

    with Session(engine) as s:
        first = reminders.create_reminder(s, text="Perplexity prep", when="today at 8am", now=now)
        again = reminders.create_reminder(s, text="Perplexity prep", when="tomorrow at 8am", now=now)
        assert again.id == first.id, "same task at the same hour on a later day must not duplicate"
        assert len(_pending(s)) == 1


def test_dedup_ignores_casing(engine, monkeypatch):
    """'Yoga class' and 'yoga class' are the same task (they became separate rows in prod)."""
    monkeypatch.setattr("app.config.settings.calendar_mirror_enabled", False)
    now = datetime.now(get_localzone()).replace(hour=6, minute=0, second=0, microsecond=0)

    with Session(engine) as s:
        first = reminders.create_reminder(s, text="Yoga class", when="today at 7pm", now=now)
        again = reminders.create_reminder(s, text="yoga class", when="today at 7pm", now=now)
        assert again.id == first.id
        assert len(_pending(s)) == 1


def test_same_text_at_a_genuinely_different_time_stays_separate(engine, monkeypatch):
    """Guard against over-merging: twice-a-day is a real thing. 9am and 9pm are two reminders,
    even though the text is identical — otherwise the dedup fix would swallow intent."""
    monkeypatch.setattr("app.config.settings.calendar_mirror_enabled", False)
    now = datetime.now(get_localzone()).replace(hour=6, minute=0, second=0, microsecond=0)

    with Session(engine) as s:
        morning = reminders.create_reminder(s, text="take medicine", when="today at 9am", now=now)
        evening = reminders.create_reminder(s, text="take medicine", when="today at 9pm", now=now)
        assert morning.id != evening.id, "different times of day are different reminders"
        assert len(_pending(s)) == 2
