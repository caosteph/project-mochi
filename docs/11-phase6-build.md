# Phase 6 — the daily briefing (+ testing hardening)

Builds on Phases 3A/3B. Mochi gains a **daily morning briefing**: one deterministic
digest of the day — today's calendar, reminders due today, and active goals/tasks —
pushed once each morning and available on demand via `/briefing`. This phase also
**hardens the test suite** after a run of bugs that reached Stephanie before any test
caught them.

## Why deterministic (no LLM)

Every recent failure — a raw-JSON dump into chat, an off-topic reply, wrong calendar
dates — was the **local 7B going off the rails**. A briefing is a place where we have
all the facts already (calendar events, reminder rows, goal rows), so there's no reason
to route them through a stochastic model and risk incoherence. `build_briefing` assembles
the message **in code**. It cannot dump JSON, wander off-topic, or hallucinate an event.
That's the whole design: the briefing is trustworthy precisely because the model isn't in
the loop.

Email is deliberately **excluded** for now — the email scanner has been the noisy source
(Phase 3B, currently paused). It can be folded into the briefing once it's proven quiet.

## What ships

`app/proactive/briefing.py`
- `build_briefing(session, *, now=None, service=None) -> str` — the digest as one
  plain-text message. Sections are omitted when empty; a genuinely empty day gets a short,
  warm line instead of a blank message. `now`/`service` are injectable for offline tests.
- Sections, each returning `[]` when empty (so it's simply skipped):
  - **Calendar** — `google_calendar.list_events` over today's local bounds (`_today_bounds`,
    computed in code — the 7B is unreliable at date math). Time-only lines; the header
    carries the date. A calendar hiccup is caught and the section omitted, never fatal.
  - **Reminders due today** — `due_today(session, now)` filters `reminders.list_pending`
    to the local day.
  - **Goals / tasks** — active `Goal`s + open `Task`s, capped (`_MAX_GOALS`/`_MAX_TASKS`).

`app/proactive/jobs.py`
- `run_daily_briefing(bot, session, chat_id, *, now, service)` — testable core; sends ONE
  message. Gated by the `/pause` kill-switch (`jobs.is_enabled()`) **and** `briefing_enabled`.
- `daily_briefing_job(context)` — the PTB callback, wrapped in a top-level try/except so a
  failure never stops tomorrow's run.

`app/channels/telegram.py`
- `/briefing` command (`_on_briefing`) — the digest on demand. Works even when proactivity
  is paused, since she explicitly asked for it. Built off the loop (calendar I/O).
- `app.job_queue.run_daily(daily_briefing_job, time=time(hour=briefing_hour, tz=local))` —
  the morning push. `run_daily` fires once/day, so no "already sent today" bookkeeping is
  needed.

`app/config.py`
- `briefing_enabled: bool = True` (the morning push; `/briefing` always works),
  `briefing_hour: int = 8` (local, after quiet hours ends at 8).

## Testing hardening

The root cause of the recent pain: the unit suite **mocks the model + Google**, so
integration/behavioral regressions only surfaced in the slow verify scripts or when
Stephanie hit them live. Two additions close that gap:

- **`scripts/verify_scenarios.py`** (real model, added to `verify_all.sh`) — drives the
  **real agent** through conversations and asserts *behavior*: the right tool fires, and —
  the flagship — the ambiguous prompt that broke ("setting what reminder?") produces **plain
  prose, no JSON dump**, on-topic. Soft floors (7B variance). This is the class of check the
  mocked tests can't do.
- **`tests/test_regressions.py`** — cross-cutting *integration* tests where the real bugs
  lived: the full reminder lifecycle (create → mirror → fire exactly once → cancel deletes
  the mirror, no orphaned `⏰ …` event) and the parser→briefing seam. Component-level cases
  stay with their modules; this file covers the seams between them.
- **`tests/test_briefing.py`** — `build_briefing` sections, empty-day line, email-excluded,
  calendar-failure resilience, `due_today` day-filtering, and `run_daily_briefing` pause/flag
  gating — all offline against a scratch DB + a mock calendar.
- Plus the previously-untested **require-due-date noise filter** (`tests/test_email_signals.py`).

## Verifying

```bash
# offline
DATABASE_URL=postgresql://localhost/personal_agent_test uv run pytest tests/ -q
uv run ruff check app/ tests/ scripts/

# real model (slow; Ollama up + scratch DB)
DATABASE_URL=postgresql://localhost/personal_agent_test uv run python scripts/verify_scenarios.py
./scripts/verify_all.sh            # the full sequential regression, now incl. scenarios
```

Live check (the transport, not correctness): send `/briefing` from the phone → today's
digest; confirm the scheduled push arrives once at `briefing_hour`.

## Deferred

- **Model-phrased briefing** — a warmer, model-written intro line. Reliability first; the
  deterministic version has to earn trust before we let the 7B near it.
- **Email in the briefing** — once the Phase 3B scanner is proven quiet and re-enabled.
- **Urgency scoring / smarter ordering** — the roadmap's fuller Phase 6 (relevance-ranked
  multi-source). This is the useful, low-noise core of it.
