"""Live Phase 3A verification — the proactive reminder engine, end to end,
without a phone. Drives the real graph + real tick logic against a scratch DB and
a recording bot; touches Google only for the optional calendar round-trip.

Run:
    DATABASE_URL=postgresql://localhost/personal_agent_test \
        PYTHONPATH=. uv run python scripts/verify_phase3.py
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from scripts._verify_lib import (
    bootstrap_env,
    check,
    rate,
    require_scratch_db,
    skip,
    summarize_and_exit,
)

require_scratch_db()
bootstrap_env()

from sqlmodel import Session  # noqa: E402

from app.agent.graph import build_agent  # noqa: E402
from app.integrations import google_auth, google_calendar  # noqa: E402
from app.memory.db import get_engine, init_db  # noqa: E402
from app.memory.models import Purchase  # noqa: E402
from app.proactive import jobs, reminders  # noqa: E402

UTC = UTC


class RecordingBot:
    def __init__(self):
        self.sent = []

    # Parameter NAMES are load-bearing: jobs.py calls this with chat_id=/reply_markup= keywords,
    # so they can't be underscore-prefixed to silence ARG002 the way a positional stub could.
    async def send_message(self, chat_id, text, reply_markup=None, **kw):  # noqa: ARG002
        self.sent.append(text)


def main() -> None:
    init_db()
    jobs.set_enabled(True)

    # 1. THE MILESTONE: seeded purchase -> exactly one return nudge; second tick -> zero.
    now = datetime.now(UTC)
    with Session(get_engine()) as s:
        p = Purchase(vendor="REI", item="rain jacket", return_by=now + timedelta(days=2))
        s.add(p)
        s.commit()
        s.refresh(p)
        # mirror=False so this milestone check doesn't leave a real calendar event
        # behind; the calendar create/delete round-trip is exercised separately below.
        reminders.create_return_reminder(s, p, mirror=False, now=now)
        bot = RecordingBot()
        # Disable quiet hours for the check so it isn't time-of-day-dependent.
        from app.config import settings
        settings.quiet_hours_start = settings.quiet_hours_end = 0
        n1 = asyncio.run(jobs.run_reminder_tick(bot, s, chat_id=1, now=now))
        n2 = asyncio.run(jobs.run_reminder_tick(RecordingBot(), s, chat_id=1, now=now))
    check("seeded purchase → exactly one return nudge", n1 == 1 and n2 == 0,
          f"first tick sent {n1}, second sent {n2}; text={bot.sent[0][:60] if bot.sent else '—'!r}")

    # 2. Model actually fires add_reminder (soft-tier reliability, like Phase 2).
    agent = build_agent()

    prompts = [
        "remind me to call mom every Sunday",
        "remind me to submit the form tomorrow at 3pm",
        "ping me in 2 hours to stretch",
    ]
    hits = rate(agent, prompts, "add_reminder")
    check("model fires add_reminder (rate)", hits / len(prompts) >= 0.6,
          f"{hits}/{len(prompts)} — soft-tier (prompt) reliability on a 7B, floor 60%")

    # 3. Real calendar event round-trip (needs calendar.events scope → re-consent).
    if google_auth.has_token():
        try:
            start = (now + timedelta(days=1)).isoformat()
            end = (now + timedelta(days=1, hours=1)).isoformat()
            ev = google_calendar.create_event(
                f"[mochi-verify] safe to ignore {uuid.uuid4().hex[:6]}", start, end, popup_minutes=0
            )
            eid = ev.get("id")
            google_calendar.delete_event(eid)
            check("calendar event create+delete round-trip", bool(eid), f"id={eid}")
        except Exception as exc:
            msg = str(exc)
            hint = " (re-consent needed for calendar.events scope?)" if "insufficient" in msg.lower() or "scope" in msg.lower() else ""
            check("calendar event create+delete round-trip", False, f"{msg[:80]}{hint}")
    else:
        skip("calendar round-trip", "no Google token configured")

    summarize_and_exit()


if __name__ == "__main__":
    main()
