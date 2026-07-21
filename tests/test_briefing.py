"""Phase 6 daily briefing — deterministic, offline. Drives the real assembly logic
against the scratch DB + a mock calendar; no phone, no model."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from sqlmodel import Session
from tzlocal import get_localzone

from app.memory.models import (
    EmailSignal,
    Goal,
    GoalStatus,
    Reminder,
    ReminderStatus,
    SignalStatus,
    SignalType,
    Task,
    TaskStatus,
)
from app.proactive import briefing, jobs

UTC = UTC
TZ = get_localzone()


def _today(hour: int) -> datetime:
    return datetime.now(TZ).replace(hour=hour, minute=0, second=0, microsecond=0)


def _cal_service(events: list[tuple]) -> MagicMock:
    """A MagicMock Google service whose events().list().execute() yields `events`
    (each a (summary, start_iso, [location]) tuple)."""
    svc = MagicMock()
    svc.events().list().execute.return_value = {
        "items": [
            {
                "summary": e[0],
                "start": {"dateTime": e[1]},
                "end": {"dateTime": e[1]},
                "location": e[2] if len(e) > 2 else None,
            }
            for e in events
        ]
    }
    return svc


class RecordingBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        self.sent.append(text)


def _seed_reminder(s: Session, text_: str, due_at: datetime) -> Reminder:
    r = Reminder(text=text_, due_at=due_at, status=ReminderStatus.PENDING.value)
    s.add(r)
    s.commit()
    s.refresh(r)
    return r


def test_briefing_includes_calendar_reminders_and_goals(engine):
    now = _today(8)
    svc = _cal_service([("Dentist", _today(15).isoformat(), "Downtown")])
    with Session(engine) as s:
        _seed_reminder(s, "call mom", _today(14))              # today → included
        _seed_reminder(s, "pay rent", now + timedelta(days=2))  # not today → excluded
        s.add(Goal(text="run a 10k", status=GoalStatus.ACTIVE.value))
        s.add(Task(text="buy running shoes", status=TaskStatus.OPEN.value))
        s.commit()
        out = briefing.build_briefing(s, now=now, service=svc)

    assert "Morning, Stephanie" in out
    assert "Dentist" in out and "Downtown" in out
    assert "call mom" in out
    assert "pay rent" not in out          # due in 2 days, not today
    assert "run a 10k" in out
    assert "buy running shoes" in out


def test_briefing_empty_day_is_warm_not_blank(engine):
    now = _today(8)
    with Session(engine) as s:
        out = briefing.build_briefing(s, now=now, service=_cal_service([]))
    assert "Clear day ahead" in out
    assert "Morning, Stephanie" in out


def test_briefing_excludes_email_signals(engine):
    """Email is deliberately kept out of the briefing (it's been the noisy source)."""
    now = _today(8)
    with Session(engine) as s:
        s.add(EmailSignal(
            source="gmail:x", signal_type=SignalType.BILL.value,
            title="SECRET BILL", status=SignalStatus.DETECTED.value,
        ))
        s.commit()
        out = briefing.build_briefing(s, now=now, service=_cal_service([]))
    assert "SECRET BILL" not in out


def test_briefing_survives_calendar_failure(engine):
    """A calendar hiccup must not sink the whole briefing — the section is just omitted."""
    now = _today(8)
    broken = MagicMock()
    broken.events().list().execute.side_effect = RuntimeError("calendar down")
    with Session(engine) as s:
        _seed_reminder(s, "still here", _today(14))
        out = briefing.build_briefing(s, now=now, service=broken)
    assert "still here" in out  # reminders section survives the calendar failure


def test_due_today_filters_by_local_day(engine):
    now = _today(8)
    with Session(engine) as s:
        _seed_reminder(s, "today", _today(20))
        _seed_reminder(s, "tomorrow", now + timedelta(days=1))
        _seed_reminder(s, "yesterday", now - timedelta(days=1))
        due = briefing.due_today(s, now)
    assert {r.text for r in due} == {"today"}


def test_run_daily_briefing_respects_pause_and_flag(engine, monkeypatch):
    now = _today(8)
    svc = _cal_service([])
    with Session(engine) as s:
        # paused (kill-switch) → nothing sent
        jobs.set_enabled(False)
        bot = RecordingBot()
        assert asyncio.run(jobs.run_daily_briefing(bot, s, 1, now=now, service=svc)) is False
        assert bot.sent == []

        # enabled but briefing_enabled off → nothing sent
        jobs.set_enabled(True)
        monkeypatch.setattr("app.config.settings.briefing_enabled", False)
        bot = RecordingBot()
        assert asyncio.run(jobs.run_daily_briefing(bot, s, 1, now=now, service=svc)) is False
        assert bot.sent == []

        # enabled + flag on → exactly one message
        monkeypatch.setattr("app.config.settings.briefing_enabled", True)
        bot = RecordingBot()
        assert asyncio.run(jobs.run_daily_briefing(bot, s, 1, now=now, service=svc)) is True
        assert len(bot.sent) == 1 and "Morning, Stephanie" in bot.sent[0]
