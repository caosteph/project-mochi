"""Phase 3A reminder engine — deterministic, offline, no phone, no model.
Drives the real logic against the scratch DB + a recording mock bot.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text
from sqlmodel import Session

from app.memory.db import init_db
from app.memory.models import Purchase, Reminder, ReminderStatus
from app.proactive import jobs, reminders

UTC = timezone.utc


@pytest.fixture(autouse=True)
def _no_calendar_mirror(monkeypatch):
    # These tests exercise the engine, not Google — keep create_* from mirroring
    # (which would hit the network and trip the no-network guard).
    monkeypatch.setattr("app.config.settings.calendar_mirror_enabled", False)


# --- schema evolution (the create_all-won't-alter bug) ---------------------

def test_init_db_adds_new_reminder_columns(engine):
    # Simulate the Phase-1-era reminder table by dropping the Phase-3 columns.
    with engine.begin() as conn:
        for col in ("recurrence", "kind", "purchase_id", "calendar_event_id", "sent_at"):
            conn.execute(text(f"ALTER TABLE reminder DROP COLUMN IF EXISTS {col}"))
    init_db(engine)  # must ALTER them back (create_all alone would not)
    with engine.begin() as conn:
        cols = {r[0] for r in conn.execute(text(
            "select column_name from information_schema.columns where table_name='reminder'"
        ))}
    assert {"recurrence", "kind", "purchase_id", "calendar_event_id", "sent_at"} <= cols


# --- natural-language parsing ----------------------------------------------

@pytest.mark.parametrize("phrase,expect_rec", [
    ("tomorrow at 3pm", None),
    ("in 2 hours", None),
    ("next Friday at 10am", None),
    ("every Sunday at 9am", "weekly"),
    ("every morning", "daily"),
    ("daily at 8am", "daily"),
])
def test_parse_when_resolves_future(phrase, expect_rec):
    now = datetime(2026, 7, 12, 14, 0, tzinfo=UTC)
    due, rec = reminders.parse_when(phrase, now=now)
    assert due > now
    assert rec == expect_rec


@pytest.mark.parametrize("phrase", ["yesterday", "asdf gibberish", "last week"])
def test_parse_when_rejects_bad_times(phrase):
    with pytest.raises(reminders.ReminderParseError):
        reminders.parse_when(phrase, now=datetime(2026, 7, 12, 14, 0, tzinfo=UTC))


# --- recurrence advancement (catch-up without spam) ------------------------

def test_next_occurrence_skips_missed_slots():
    anchor = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)  # weekly, but a month ago
    now = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
    nxt = reminders.next_occurrence(anchor, "weekly", now)
    assert nxt > now and (nxt - now) <= timedelta(weeks=1)  # exactly the next future slot


# --- quiet hours -----------------------------------------------------------

@pytest.mark.parametrize("hour,expected", [(22, True), (3, True), (8, False), (12, False), (20, False)])
def test_quiet_hours_wraps_midnight(hour, expected):
    local = datetime.now().astimezone().replace(hour=hour, minute=0)
    assert reminders.in_quiet_hours(local) is expected  # default window 21:00–08:00


# --- return reminder -------------------------------------------------------

def test_create_return_reminder_lead_dedup_and_none(engine):
    now = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    with Session(engine) as s:
        p = Purchase(vendor="REI", item="jacket", return_by=datetime(2026, 7, 20, 12, 0, tzinfo=UTC))
        s.add(p)
        s.commit()
        s.refresh(p)
        r = reminders.create_return_reminder(s, p, now=now)
        assert r is not None and "jacket" in r.text
        assert r.due_at == datetime(2026, 7, 17, 12, 0, tzinfo=UTC)  # 3 days before return_by
        assert reminders.create_return_reminder(s, p, now=now) is None  # dedup

        p2 = Purchase(vendor="X", item="thing", return_by=None)
        s.add(p2)
        s.commit()
        s.refresh(p2)
        assert reminders.create_return_reminder(s, p2, now=now) is None  # no return_by


# --- the tick (recording mock bot) -----------------------------------------

class RecordingBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        self.sent.append({"text": text, "buttons": reply_markup is not None})


def _seed(s, text_, due_at, status=ReminderStatus.PENDING.value, recurrence=None):
    r = Reminder(text=text_, due_at=due_at, status=status, recurrence=recurrence)
    s.add(r)
    s.commit()
    s.refresh(r)
    return r


def test_tick_sends_due_dedups_and_reschedules_recurring(engine, monkeypatch):
    monkeypatch.setattr("app.config.settings.quiet_hours_start", 0)
    monkeypatch.setattr("app.config.settings.quiet_hours_end", 0)  # disable quiet hours
    jobs.set_enabled(True)
    now = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    with Session(engine) as s:
        due = _seed(s, "one-off due", now - timedelta(minutes=1))
        _seed(s, "not yet", now + timedelta(hours=2))
        rec = _seed(s, "daily thing", now - timedelta(minutes=1), recurrence="daily")

        bot = RecordingBot()
        n = asyncio.run(jobs.run_reminder_tick(bot, s, chat_id=1, now=now))
        assert n == 2  # the one-off + the recurring; NOT the future one
        assert all(m["buttons"] for m in bot.sent)  # Done/Snooze buttons

        s.refresh(due)
        s.refresh(rec)
        assert due.status == ReminderStatus.SENT.value       # one-off consumed
        assert rec.status == ReminderStatus.PENDING.value    # recurring stays pending
        assert rec.due_at > now                              # …advanced to next slot

        # Second tick right away sends nothing new (dedup / exactly-once).
        bot2 = RecordingBot()
        assert asyncio.run(jobs.run_reminder_tick(bot2, s, chat_id=1, now=now)) == 0


def test_tick_respects_quiet_hours_and_pause(engine, monkeypatch):
    now_quiet = datetime.now().astimezone().replace(hour=23, minute=0).astimezone(UTC)
    with Session(engine) as s:
        _seed(s, "due but quiet", now_quiet - timedelta(minutes=1))
        jobs.set_enabled(True)
        assert asyncio.run(jobs.run_reminder_tick(RecordingBot(), s, 1, now=now_quiet)) == 0  # quiet

    now_ok = datetime.now().astimezone().replace(hour=12, minute=0).astimezone(UTC)
    with Session(engine) as s:
        _seed(s, "due, paused", now_ok - timedelta(minutes=1))
        jobs.set_enabled(False)  # kill-switch
        assert asyncio.run(jobs.run_reminder_tick(RecordingBot(), s, 1, now=now_ok)) == 0
        jobs.set_enabled(True)


def test_tick_isolates_a_failing_reminder(engine, monkeypatch):
    monkeypatch.setattr("app.config.settings.quiet_hours_start", 0)
    monkeypatch.setattr("app.config.settings.quiet_hours_end", 0)
    jobs.set_enabled(True)
    now = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    with Session(engine) as s:
        _seed(s, "good one", now - timedelta(minutes=1))

        calls = {"n": 0}

        async def flaky_send(chat_id, text, reply_markup=None, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("telegram hiccup")  # first reminder fails

        _seed(s, "another good one", now - timedelta(minutes=1))
        bot = SimpleNamespace(send_message=flaky_send)
        # Must not raise, and must still attempt both reminders.
        asyncio.run(jobs.run_reminder_tick(bot, s, 1, now=now))
        assert calls["n"] == 2  # the failing one didn't abort the tick


# --- callbacks -------------------------------------------------------------

def test_done_and_snooze(engine):
    now = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    with Session(engine) as s:
        r = _seed(s, "thing", now, status=ReminderStatus.SENT.value)
        reminders.mark_done(s, r.id)
        s.refresh(r)
        assert r.status == ReminderStatus.DONE.value

        r2 = _seed(s, "other", now, status=ReminderStatus.SENT.value)
        reminders.snooze(s, r2.id, now=now)
        s.refresh(r2)
        assert r2.status == ReminderStatus.PENDING.value and r2.due_at > now and r2.sent_at is None


# --- calendar mirror -------------------------------------------------------

def test_mirror_reminder_creates_event_once(engine):
    with Session(engine) as s:
        r = _seed(s, "mirror me", datetime(2026, 7, 20, 12, 0, tzinfo=UTC))
        service = MagicMock()
        service.events().insert().execute.return_value = {"id": "evt_1"}
        eid = reminders.mirror_reminder(s, r, service=service)
        assert eid == "evt_1"
        s.refresh(r)
        assert r.calendar_event_id == "evt_1"
        # Idempotent: a second mirror doesn't create another event.
        service.events().insert.reset_mock()
        reminders.mirror_reminder(s, r, service=service)
        service.events().insert.assert_not_called()


def test_mirror_reminder_duration(engine):
    with Session(engine) as s:
        r = _seed(s, "2-hour meeting", datetime(2026, 7, 20, 12, 0, tzinfo=UTC))
        service = MagicMock()
        service.events().insert().execute.return_value = {"id": "e"}
        reminders.mirror_reminder(s, r, duration_minutes=120, service=service)
        _, kwargs = service.events().insert.call_args
        start = datetime.fromisoformat(kwargs["body"]["start"]["dateTime"])
        end = datetime.fromisoformat(kwargs["body"]["end"]["dateTime"])
        assert (end - start) == timedelta(minutes=120)  # honors the estimated duration

    with Session(engine) as s:
        r = _seed(s, "call mom", datetime(2026, 7, 20, 12, 0, tzinfo=UTC))
        service = MagicMock()
        service.events().insert().execute.return_value = {"id": "e2"}
        reminders.mirror_reminder(s, r, service=service)  # no duration → short default
        _, kwargs = service.events().insert.call_args
        start = datetime.fromisoformat(kwargs["body"]["start"]["dateTime"])
        end = datetime.fromisoformat(kwargs["body"]["end"]["dateTime"])
        assert (end - start) == timedelta(minutes=15)  # settings.reminder_event_default_minutes
