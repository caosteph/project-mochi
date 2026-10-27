"""Liveness: a watchdog for a wedged process, dependency health checks, and `/status`.

WHY: launchd's KeepAlive restarts a process that *exits*, not one that's wedged (hung poll, a
blocking call stuck on the event loop, a dead DB). Stephanie has already lived through a
silently-dead bot, and from her phone "Mochi has nothing to say" and "Mochi is down" looked
identical. Three pieces, all deterministic (no model):

  1. Watchdog. A cheap job beats on the bot's event loop every HEARTBEAT_SECONDS; a daemon
     thread exits the process if the beat goes stale for `watchdog_stale_seconds`. The beat
     comes from the same JobQueue as the reminder tick, so a stale beat covers both a wedged
     loop and a dead scheduler. Exiting is the fix, not a crash: KeepAlive restarts the bot
     and `scripts/preflight.sh` repairs Postgres/Ollama on the way back up.
  2. Health checks every `health_check_interval_seconds`: Postgres, Ollama, both models
     present, Telegram polling alive. `Monitor` turns a series of results into at most one
     alert per outage and one "back" message, and asks for a restart only when a dependency
     preflight can actually repair has stayed down for `health_restart_after_failures`
     checks in a row (a missing model would just restart forever, so it only alerts).
  3. `build_status()` — the `/status` text. Every section degrades on its own, so `/status`
     still answers when the database is the thing that's down.

Time: the watchdog uses `time.monotonic()`, which doesn't advance while the Mac sleeps (nor
does CLOCK_MONOTONIC on Linux), so closing the laptop lid never reads as a stale beat.
"""

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
import psycopg
from sqlmodel import Session, select

from app.config import settings

log = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 30
_CHECK_TIMEOUT = 5.0  # seconds — a health check must never hang the thing it's checking

_started_at = datetime.now(UTC)
_last_beat = time.monotonic()
_last_tick_at: datetime | None = None


def beat() -> None:
    global _last_beat
    _last_beat = time.monotonic()


def record_tick(now: datetime | None = None) -> None:
    """Called by the reminder tick each time it runs, for `/status`."""
    global _last_tick_at
    _last_tick_at = now or datetime.now(UTC)


# --- 1. watchdog --------------------------------------------------------------


def watchdog_expired(now: float, last_beat: float, stale_after: float) -> bool:
    return stale_after > 0 and now - last_beat > stale_after


def _exit_for_restart(reason: str) -> None:
    """Hard exit so launchd's KeepAlive restarts us (and preflight repairs dependencies).
    `os._exit`, not `sys.exit`: from a thread, or from a wedged loop, a SystemExit would be
    swallowed or never reached. The single-instance flock is released by the OS on exit."""
    log.critical("Exiting for a supervised restart: %s", reason)
    logging.shutdown()
    os._exit(1)


def start_watchdog() -> None:
    stale_after = settings.watchdog_stale_seconds
    if stale_after <= 0:
        return
    beat()  # the loop hasn't started yet; count from now

    def loop() -> None:
        while True:
            time.sleep(HEARTBEAT_SECONDS)
            if watchdog_expired(time.monotonic(), _last_beat, stale_after):
                _exit_for_restart(f"no event-loop heartbeat for over {stale_after}s")

    threading.Thread(target=loop, daemon=True, name="mochi-watchdog").start()
    log.info("Watchdog started (restart if the loop is silent for %ss).", stale_after)


async def heartbeat_job(_context) -> None:
    beat()


# --- 2. dependency checks -------------------------------------------------------


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""
    # True when a restart can plausibly fix it: preflight.sh restarts Postgres/Ollama, and a
    # fresh process restarts polling. A missing model or a bad config would just loop.
    restartable: bool = False


def check_postgres() -> Check:
    try:
        with psycopg.connect(settings.database_url, connect_timeout=int(_CHECK_TIMEOUT)) as conn:
            conn.execute("SELECT 1")
        return Check("Postgres", True)
    except Exception as exc:
        return Check("Postgres", False, f"unreachable ({type(exc).__name__})", restartable=True)


def _ollama_tags() -> list[str]:
    url = settings.ollama_base_url.removesuffix("/v1") + "/api/tags"
    resp = httpx.get(url, timeout=_CHECK_TIMEOUT)
    resp.raise_for_status()
    return [m["name"] for m in resp.json().get("models", [])]


def _has_model(names: list[str], model: str) -> bool:
    # Ollama lists an untagged pull as "name:latest".
    return model in names or (":" not in model and f"{model}:latest" in names)


def check_ollama() -> list[Check]:
    try:
        names = _ollama_tags()
    except Exception as exc:
        return [Check("Ollama", False, f"unreachable ({type(exc).__name__})", restartable=True)]
    checks = [Check("Ollama", True)]
    for label, model in (("chat model", settings.local_model), ("embeddings", settings.embedding_model)):
        ok = _has_model(names, model)
        checks.append(Check(f"{label} {model}", ok, "" if ok else "not pulled/created"))
    return checks


def check_polling(application) -> Check:
    updater = getattr(application, "updater", None) if application is not None else None
    if updater is None or updater.running:
        return Check("Telegram polling", True)
    return Check("Telegram polling", False, "stopped", restartable=True)


def run_checks(application=None) -> list[Check]:
    return [check_postgres(), *check_ollama(), check_polling(application)]


@dataclass(frozen=True)
class Decision:
    message: str | None = None
    restart: bool = False


@dataclass
class Monitor:
    """Turns a stream of check results into alerts. Pure — no I/O, no clock — so every
    transition is unit-testable. One alert per outage (re-alerting only if a *new* component
    breaks), held through quiet hours, and one "back to healthy" once it clears."""

    restart_after: int = 3
    consecutive_failures: int = 0
    announced: set[str] = field(default_factory=set)

    def update(self, checks: list[Check], *, quiet: bool) -> Decision:
        failing = [c for c in checks if not c.ok]
        if not failing:
            self.consecutive_failures = 0
            if self.announced and not quiet:
                self.announced.clear()
                return Decision("✅ Mochi is healthy again.")
            return Decision()

        self.consecutive_failures += 1
        restart = (
            self.restart_after > 0
            and self.consecutive_failures >= self.restart_after
            and any(c.restartable for c in failing)
        )
        names = {c.name for c in failing}
        message = None
        if not quiet and (not names <= self.announced or restart):
            lines = [f"⚠️ Mochi health problem — {_summary(failing)}."]
            if restart:
                lines.append("Restarting myself to repair it; I'll be back in a minute.")
            else:
                lines.append("Reminders may not reach you until this clears. /status for details.")
            message = "\n".join(lines)
            self.announced |= names
        return Decision(message, restart)


def _summary(checks: list[Check]) -> str:
    return "; ".join(f"{c.name}: {c.detail}" if c.detail else c.name for c in checks)


_monitor = Monitor()


async def health_job(context) -> None:
    """PTB JobQueue callback. Checks run off the loop (they do blocking I/O with timeouts);
    the alert is sent from the loop. Top-level try/except so a failure never stops the
    scheduler — the watchdog is the backstop for anything this can't catch."""
    from app.proactive import reminder_time  # lazy: proactive imports pull in the DB models

    try:
        checks = await asyncio.to_thread(run_checks, getattr(context, "application", None))
        _monitor.restart_after = settings.health_restart_after_failures
        decision = _monitor.update(checks, quiet=reminder_time.in_quiet_hours(datetime.now().astimezone()))
        if decision.message:
            try:
                await context.bot.send_message(chat_id=settings.telegram_chat_id, text=decision.message)
            except Exception:
                log.exception("health alert failed to send")
        if decision.restart:
            _exit_for_restart(_summary([c for c in checks if not c.ok]))
    except Exception:
        log.exception("health_job crashed; will retry next interval")


# --- 3. /status ---------------------------------------------------------------


def _ago(then: datetime | None, now: datetime) -> str:
    if then is None:
        return "not yet"
    secs = int((now - then).total_seconds())
    if secs < 90:
        return f"{secs}s ago"
    if secs < 90 * 60:
        return f"{secs // 60}m ago"
    if secs < 36 * 3600:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _uptime(now: datetime) -> str:
    secs = int((now - _started_at).total_seconds())
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {rem // 60}m"
    return f"{rem // 60}m"


def _db_lines(engine, now: datetime) -> list[str]:
    from app.memory.models import MessageLog
    from app.proactive import reminders

    with Session(engine) as session:
        pending = reminders.list_pending(session)
        briefing = session.exec(
            select(MessageLog.created_at)
            .where(MessageLog.text.startswith("[briefing]"))
            .order_by(MessageLog.created_at.desc())
            .limit(1)
        ).first()
    lines = [f"Last briefing: {_ago(briefing, now)}"]
    if pending:
        nxt = pending[0]
        snip = nxt.text if len(nxt.text) <= 40 else nxt.text[:39] + "…"
        lines.append(
            f"Reminders: {len(pending)} pending — next “{snip}” {nxt.due_at.astimezone():%a %b %-d %-I:%M %p}"
        )
    else:
        lines.append("Reminders: none pending")
    return lines


def build_status(*, checks: list[Check], proactivity_on: bool, engine=None, now: datetime | None = None) -> str:
    from app.memory.db import get_engine

    now = now or datetime.now(UTC)
    healthy = all(c.ok for c in checks)
    lines = [
        f"🩺 Mochi — {'all good' if healthy else 'something is wrong'}",
        f"Up {_uptime(now)} (since {_started_at.astimezone():%b %-d %-I:%M %p}) · model {settings.local_model}",
        "",
        *(f"{'✅' if c.ok else '❌'} {c.name}{f' — {c.detail}' if c.detail else ''}" for c in checks),
        "",
        f"Reminder tick: {_ago(_last_tick_at, now)} · proactivity {'on' if proactivity_on else 'PAUSED (/resume)'}",
    ]
    try:
        lines += _db_lines(engine or get_engine(), now)
    except Exception as exc:
        lines.append(f"Reminders/briefing: unavailable ({type(exc).__name__})")
    hosted = "off (LOCAL_ONLY)" if settings.local_only else ("on" if settings.hosted_enabled else "off")
    lines.append(f"Email scanner: {settings.signal_mode} · hosted model: {hosted}")
    return "\n".join(lines)
