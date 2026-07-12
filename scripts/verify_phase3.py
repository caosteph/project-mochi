"""Live Phase 3A verification — the proactive reminder engine, end to end,
without a phone. Drives the real graph + real tick logic against a scratch DB and
a recording bot; touches Google only for the optional calendar round-trip.

Run:
    DATABASE_URL=postgresql://localhost/personal_agent_test \
        PYTHONPATH=. uv run python scripts/verify_phase3.py
"""

import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

if "personal_agent_test" not in os.environ.get("DATABASE_URL", "") and "verify" not in os.environ.get(
    "DATABASE_URL", ""
):
    print(f"Refusing to run: DATABASE_URL must point at a scratch DB (got {os.environ.get('DATABASE_URL')!r}).")
    sys.exit(1)

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "verify_placeholder")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")

from langchain_core.messages import HumanMessage  # noqa: E402
from sqlmodel import Session  # noqa: E402

from app.agent.graph import build_agent  # noqa: E402
from app.integrations import google_auth, google_calendar  # noqa: E402
from app.memory.db import get_engine, init_db  # noqa: E402
from app.memory.models import Purchase  # noqa: E402
from app.proactive import jobs, reminders  # noqa: E402

UTC = timezone.utc
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'} | {name}" + (f" | {detail}" if detail else ""))


class RecordingBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
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

    def fires(prompt: str) -> bool:
        cfg = {"configurable": {"thread_id": f"verify-rem-{uuid.uuid4()}"}}
        for update in agent.stream({"messages": [HumanMessage(prompt)]}, cfg, stream_mode="updates"):
            ap = update.get("agent")
            if ap and ap.get("messages"):
                if any(tc["name"] == "add_reminder" for tc in (getattr(ap["messages"][-1], "tool_calls", None) or [])):
                    return True
        return False

    prompts = [
        "remind me to call mom every Sunday",
        "remind me to submit the form tomorrow at 3pm",
        "ping me in 2 hours to stretch",
    ]
    hits = sum(fires(p) for p in prompts)
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
        print("SKIP | calendar round-trip — no Google token configured")

    print()
    failed = [r for r in results if not r[1]]
    print(f"{len(results) - len(failed)}/{len(results)} checks passed.")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
