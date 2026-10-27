"""Liveness: watchdog, dependency checks, outage alerts, and /status.

The failure this guards against is silence: a wedged or half-dead Mochi looks exactly like a
quiet day from Stephanie's phone. So the tests lean on the transitions that decide whether she
hears about it — alerted once (not every 5 minutes), held through quiet hours, told when it's
back, and restarted only when a restart can actually fix it. And /status has to answer when the
database is the thing that's down.

Offline: Postgres is the real scratch DB; Ollama and Telegram are stubbed.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlmodel import Session

from app import health
from app.config import settings
from app.health import Check, Monitor
from app.memory.models import MessageLog, Reminder
from tests.support import FakeMessage, make_update

OK = [Check("Postgres", True), Check("Ollama", True)]
PG_DOWN = [Check("Postgres", False, "unreachable", restartable=True), Check("Ollama", True)]
MODEL_MISSING = [Check("Postgres", True), Check("chat model x", False, "not pulled/created")]


# --- Monitor: the alerting state machine --------------------------------------


def test_healthy_says_nothing():
    assert Monitor().update(OK, quiet=False) == health.Decision()


def test_an_outage_is_announced_once_not_every_check():
    m = Monitor(restart_after=0)
    first = m.update(PG_DOWN, quiet=False)
    assert first.message and "Postgres" in first.message
    assert m.update(PG_DOWN, quiet=False).message is None
    assert m.update(PG_DOWN, quiet=False).message is None


def test_a_new_component_breaking_mid_outage_is_announced():
    m = Monitor(restart_after=0)
    m.update(PG_DOWN, quiet=False)
    both = [Check("Postgres", False, restartable=True), Check("Ollama", False, restartable=True)]
    msg = m.update(both, quiet=False).message
    assert msg and "Ollama" in msg


def test_recovery_is_announced_once():
    m = Monitor(restart_after=0)
    m.update(PG_DOWN, quiet=False)
    assert "healthy again" in m.update(OK, quiet=False).message
    assert m.update(OK, quiet=False).message is None


def test_quiet_hours_hold_the_alert_until_morning():
    m = Monitor(restart_after=0)
    assert m.update(PG_DOWN, quiet=True).message is None
    assert m.update(PG_DOWN, quiet=False).message  # still down at 8am → now she hears


def test_quiet_hours_hold_the_recovery_message_too():
    """Otherwise an outage announced at 8:55pm that clears at 11pm leaves her believing it's
    still broken."""
    m = Monitor(restart_after=0)
    m.update(PG_DOWN, quiet=False)
    assert m.update(OK, quiet=True).message is None
    assert "healthy again" in m.update(OK, quiet=False).message


def test_an_outage_that_clears_during_quiet_hours_is_never_announced():
    m = Monitor(restart_after=0)
    m.update(PG_DOWN, quiet=True)
    assert m.update(OK, quiet=True).message is None
    assert m.update(OK, quiet=False).message is None


def test_restart_only_after_n_consecutive_restartable_failures():
    m = Monitor(restart_after=3)
    assert not m.update(PG_DOWN, quiet=False).restart
    assert not m.update(PG_DOWN, quiet=False).restart
    third = m.update(PG_DOWN, quiet=False)
    assert third.restart and "Restarting" in third.message


def test_restart_still_happens_during_quiet_hours_just_silently():
    m = Monitor(restart_after=1)
    d = m.update(PG_DOWN, quiet=True)
    assert d.restart and d.message is None


def test_a_recovery_resets_the_restart_count():
    m = Monitor(restart_after=3)
    m.update(PG_DOWN, quiet=False)
    m.update(PG_DOWN, quiet=False)
    m.update(OK, quiet=False)
    assert not m.update(PG_DOWN, quiet=False).restart


def test_a_missing_model_alerts_but_never_restarts():
    """A restart can't pull a model, so restarting on it would just loop forever."""
    m = Monitor(restart_after=1)
    d1 = m.update(MODEL_MISSING, quiet=False)
    assert d1.message and not d1.restart
    assert not m.update(MODEL_MISSING, quiet=False).restart


def test_restart_after_zero_disables_restarts():
    m = Monitor(restart_after=0)
    for _ in range(5):
        assert not m.update(PG_DOWN, quiet=False).restart


# --- the checks themselves ----------------------------------------------------


def test_postgres_check_passes_against_the_real_db(engine):
    assert health.check_postgres().ok


def test_postgres_check_fails_fast_and_restartable_when_down(monkeypatch):
    monkeypatch.setattr(settings, "database_url", "postgresql://localhost:9/personal_agent_test")
    c = health.check_postgres()
    assert not c.ok and c.restartable


def test_ollama_check_reports_each_model(monkeypatch):
    monkeypatch.setattr(settings, "local_model", "qwen2.5:7b-8k")
    monkeypatch.setattr(settings, "embedding_model", "nomic-embed-text")
    monkeypatch.setattr(health, "_ollama_tags", lambda: ["qwen2.5:7b-8k", "nomic-embed-text:latest"])
    checks = health.check_ollama()
    assert [c.ok for c in checks] == [True, True, True]


def test_ollama_check_flags_a_missing_model_as_not_restartable(monkeypatch):
    monkeypatch.setattr(settings, "local_model", "qwen2.5:7b-8k")
    monkeypatch.setattr(health, "_ollama_tags", lambda: ["qwen2.5:7b", "nomic-embed-text:latest"])
    chat = next(c for c in health.check_ollama() if "chat model" in c.name)
    assert not chat.ok and not chat.restartable


def test_ollama_check_unreachable_is_restartable(monkeypatch):
    """Through the real HTTP path, against a port nothing listens on."""
    monkeypatch.setattr(settings, "ollama_base_url", "http://127.0.0.1:9/v1")
    checks = health.check_ollama()
    assert len(checks) == 1 and not checks[0].ok and checks[0].restartable


def test_polling_check():
    running = SimpleNamespace(updater=SimpleNamespace(running=True))
    stopped = SimpleNamespace(updater=SimpleNamespace(running=False))
    assert health.check_polling(running).ok
    assert health.check_polling(None).ok  # no application to inspect (e.g. tests)
    c = health.check_polling(stopped)
    assert not c.ok and c.restartable


# --- watchdog -----------------------------------------------------------------


def test_watchdog_expires_only_past_the_threshold():
    assert not health.watchdog_expired(now=100.0, last_beat=50.0, stale_after=60)
    assert health.watchdog_expired(now=200.0, last_beat=50.0, stale_after=60)
    assert not health.watchdog_expired(now=10_000.0, last_beat=0.0, stale_after=0)  # disabled


def test_heartbeat_job_refreshes_the_beat(monkeypatch):
    monkeypatch.setattr(health, "_last_beat", 0.0)
    asyncio.run(health.heartbeat_job(None))
    assert health._last_beat > 0.0


# --- health_job: checks → alert → restart, end to end -------------------------


@pytest.fixture
def fresh_monitor(monkeypatch):
    monkeypatch.setattr(health, "_monitor", Monitor())
    monkeypatch.setattr("app.proactive.reminder_time.in_quiet_hours", lambda _now: False)
    restarts = []
    monkeypatch.setattr(health, "_exit_for_restart", restarts.append)
    return restarts


def test_health_job_alerts_her_and_restarts_after_repeated_failures(monkeypatch, fake_bot, fresh_monitor):
    monkeypatch.setattr(settings, "health_restart_after_failures", 2)
    monkeypatch.setattr(health, "run_checks", lambda _app=None: PG_DOWN)
    context = SimpleNamespace(bot=fake_bot, application=None)
    asyncio.run(health.health_job(context))
    assert len(fake_bot.messages) == 1 and "Postgres" in fake_bot.messages[0].text
    assert fake_bot.messages[0].chat_id == settings.telegram_chat_id
    assert fresh_monitor == []
    asyncio.run(health.health_job(context))
    assert len(fresh_monitor) == 1 and "Postgres" in fresh_monitor[0]


def test_health_job_is_silent_when_healthy(monkeypatch, fake_bot, fresh_monitor):
    monkeypatch.setattr(health, "run_checks", lambda _app=None: OK)
    asyncio.run(health.health_job(SimpleNamespace(bot=fake_bot, application=None)))
    assert fake_bot.messages == [] and fresh_monitor == []


def test_health_job_never_raises(monkeypatch, fake_bot, fresh_monitor):
    def boom(_app=None):
        raise RuntimeError("checks exploded")

    monkeypatch.setattr(health, "run_checks", boom)
    asyncio.run(health.health_job(SimpleNamespace(bot=fake_bot, application=None)))


def test_reminder_tick_job_records_the_tick(monkeypatch, fake_bot, engine):
    from app.proactive import jobs

    monkeypatch.setattr(health, "_last_tick_at", None)
    asyncio.run(jobs.reminder_tick_job(SimpleNamespace(bot=fake_bot)))
    assert health._last_tick_at is not None


# --- /status ------------------------------------------------------------------


def test_status_reports_health_reminders_and_last_briefing(engine):
    now = datetime.now(UTC)
    with Session(engine) as s:
        s.add(Reminder(text="call mom", due_at=now + timedelta(days=1)))
        s.add(Reminder(text="pay rent", due_at=now + timedelta(days=3)))
        s.add(MessageLog(chat_id=1, role="assistant", text="[briefing] daily digest",
                         created_at=now - timedelta(hours=3)))
        s.commit()
    out = health.build_status(checks=OK, proactivity_on=True, engine=engine, now=now)
    assert "all good" in out
    assert "2 pending" in out and "call mom" in out  # soonest first
    assert "Last briefing: 3h ago" in out
    assert settings.local_model in out


def test_status_shows_failures_and_pause(engine):
    out = health.build_status(checks=PG_DOWN, proactivity_on=False, engine=engine)
    assert "something is wrong" in out and "❌ Postgres" in out
    assert "PAUSED" in out
    assert "none pending" in out and "Last briefing: not yet" in out


def test_status_still_answers_when_the_db_is_down():
    from app.memory.db import get_engine

    dead = get_engine("postgresql://localhost:9/personal_agent_test")
    out = health.build_status(checks=PG_DOWN, proactivity_on=True, engine=dead)
    assert "unavailable" in out and "❌ Postgres" in out


def test_status_command_replies(channel, ctx, monkeypatch):
    monkeypatch.setattr(health, "run_checks", lambda _app=None: OK)
    update = make_update(message=FakeMessage("/status"))
    asyncio.run(channel._on_status(update, ctx))
    assert "Mochi" in update.message.replies[0]


def test_status_command_ignores_other_chats(channel, ctx, monkeypatch):
    monkeypatch.setattr(channel, "_authorized", lambda _u: False)
    update = make_update(message=FakeMessage("/status"))
    asyncio.run(channel._on_status(update, ctx))
    assert update.message.replies == []
