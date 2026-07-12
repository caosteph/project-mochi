# Phase 3A — Proactive Reminder Engine (Build Steps)

**Goal of this phase:** make Mochi *proactive* — she can now message Stephanie **unprompted** (the
channel has only ever *responded* before). And she does it generally: Stephanie sets up **any**
reminder by talking to Mochi — one-off ("remind me to submit the form tomorrow at 3") or recurring
("call mom every Sunday", "ping me every morning to journal"). The roadmap's return-window flow is
just *one auto-created kind* of reminder on top of this engine.

**Split note:** Phase 3 is split. **3A (this doc)** is the reminder engine + proactive push,
delivering the milestone by *seeding* a purchase. **3B (next)** adds Gmail receipt ingestion + the
**quarantined reader** (dual-LLM) that safely parses untrusted email *bodies* into a `Purchase` — the
new email-body-reading safety surface, isolated on purpose.

**Milestone (definition of done):** a seeded `Purchase` with a near return date →
`create_return_reminder` → the reminder-tick fires **exactly one** unprompted "return X by <date>"
nudge (with Done/Snooze), and a second tick fires nothing. Plus: Mochi reliably sets reminders from
natural language, and timed reminders appear as real Google Calendar events. **All verified without a
phone** (`scripts/verify_phase3.py` + `tests/test_reminders.py`).

---

## Overview

```
Telegram ─▶ agent (10 tools; +add_reminder/list/cancel) ─▶ reminders.create_reminder()
                                                              │ dateparser (NOT the model) parses "when"
                                                              ▼
                                                     Postgres: Reminder (one-off | recurring)
                                                              │
JobQueue (APScheduler on the bot loop) ─every 60s─▶ reminder_tick ─▶ due? quiet-hours? enabled?
                                                              │
                              🔔 nudge to the whitelisted chat + [✅ Done][⏰ Snooze]
                              recurring → advance to next slot;  one-off → SENT (dedup)
                                                              │
                          timed reminder ─▶ google_calendar.create_event (mirror, calendar.events scope)
```

---

## Step 1 — Dependencies

```bash
uv add "python-telegram-bot[job-queue]" dateparser
```
`[job-queue]` pulls APScheduler (the plan's scheduler — integrated with the bot loop so jobs get
`context.bot`). `dateparser` parses natural-language times reliably (the 7B is not trustworthy at
date math — proven in Phase 2's calendar work).

---

## Step 2 — Schema (`app/memory/models.py`, `app/memory/db.py`)

New `Purchase` (vendor/item/amount/order_date/return_by/source). `Reminder` (already existed, empty,
from Phase 1) gains `recurrence` (None=one-off, else daily/weekly/monthly), `kind`, `purchase_id`,
`calendar_event_id`, `sent_at`.

**The schema-evolution gotcha (a real bug caught in review):** `SQLModel.metadata.create_all()`
creates the *new* `Purchase` table but **does not ALTER the existing `reminder` table** — so its new
columns would silently be missing and the first insert would crash `column "purchase_id" does not
exist`. `init_db()` therefore runs idempotent `ALTER TABLE reminder ADD COLUMN IF NOT EXISTS ...`
(the same raw-idempotent-SQL pattern already used for indexes). Verified by a test that drops the
columns, runs `init_db()`, and asserts they're re-added.

---

## Step 3 — The engine (`app/proactive/reminders.py`) — pure, testable

All functions take an explicit `Session` (+ `now`) so the whole engine runs against a scratch DB
with no phone, no model. Highlights:

- **`parse_when(when, recurrence, now)`** → `(due_at UTC, recurrence)`. Normalizes the phrase for
  dateparser: strips recurrence lead-ins ("every"/"daily"), rewrites "next Friday"→"Friday" (future
  preference picks the next; "next X" returns None raw), maps bare times-of-day ("morning"→8am) with
  word boundaries (so "night" ≠ "tonight"). Raises `ReminderParseError` for unparseable/past times.
  This was tuned **empirically** against a phrase suite (that's a test now).
- **`next_occurrence(due_at, recurrence, now)`** advances by whole periods to the next slot *strictly
  after now* — skipping missed ones (downtime → one nudge, not a burst).
- **`create_reminder` / `create_return_reminder`** (return: lead-days before window, clamp-to-now,
  dedup per purchase, None if no `return_by`).
- **`due_reminders` / `mark_fired`** (recurring → reschedule + stay PENDING; one-off → SENT) **/
  `mark_done` / `snooze` / `cancel_reminder` / `mirror_reminder`.**

---

## Step 4 — The tick (`app/proactive/jobs.py`) — the proactive push

`run_reminder_tick(bot, session, chat_id, now)` (testable core) and `reminder_tick_job(context)` (the
JobQueue callback). Each due reminder is sent in its **own try/except** — one bad row can't wedge all
future proactivity — and the nudge is **sent then marked** (bias to never-lost over never-duplicated).
A runtime `_enabled` flag (seeded from config, toggled by `/pause` `/resume`) and a local quiet-hours
check gate it. Registered in `TelegramChannel.run()` via `app.job_queue.run_repeating(...)`.

---

## Step 5 — Tools + persona (`app/agent/tools/reminder_tools.py`, `persona.md`)

`add_reminder(text, when, recurrence=None)`, `list_reminders()`, `cancel_reminder(query)` — registered
into `ALL_TOOLS` (now 10). The persona gets a forceful, example-driven "using your reminder tools"
section (the same discipline that lifted tool-firing to ~80%): call `add_reminder` immediately, pass
the time phrase verbatim (the tool parses it — don't compute dates yourself). Tool-firing rate is
**measured** in verify (3/3 in practice), not assumed.

---

## Step 6 — Calendar mirroring (`google_calendar.py`, `google_auth.py`)

Add `calendar.events` write scope (upgrade from `calendar.readonly` → a one-time OAuth **re-consent**;
drive it by deleting `data/google_token.json` and re-running the consent flow). New `create_event`
(and `delete_event` for verify cleanup) — only `create_event` is ever called, only by the engine; no
update/delete tool is exposed to the model. `mirror_reminder` creates an event for a timed reminder
and stores its id (idempotent — never double-creates).

**Safety scoping (documented in `docs/04-constitution.md`):** writing an event to *her own* calendar
for *her own* reminder is not the "act as her toward a third party / destructive / outbound" case the
approval gate exists for, and it runs in a background job where `interrupt()` doesn't apply — so it's
**not per-event approval-gated**; it's gated by the `calendar_mirror_enabled` opt-in, create-only,
her calendar. A deliberate, written scoping of the hard rule — not a silent weakening. (The `events`
scope does grant delete power at the token level; noted as the least-privilege tradeoff accepted by
choosing mirroring.)

---

## Step 7 — Channel wiring + control (`app/channels/telegram.py`, `app/config.py`)

`_on_callback` now **dispatches by prefix**: `rem:done:<id>` / `rem:snooze:<id>` → the reminder
handler; `approve`/`reject` → the Phase-2 draft path (a regression-guarded split). `/pause` `/resume`
flip the runtime kill-switch. Config adds the reminder/quiet-hours/proactivity/mirror settings
(defaults: lead 3d, quiet 21–08, snooze 1d, tick 60s, mirror on).

---

## Step 8 — Verify (no phone)

- **`uv run pytest tests/test_reminders.py -v`** (22 tests): schema-evolution ALTER; NL-parse suite;
  recurrence skip-missed; quiet-hours wraparound; return-reminder lead/clamp/dedup/None; the tick with
  a recording bot (due/not-due/already-sent/recurring/quiet/paused, second-tick-sends-nothing);
  per-reminder error isolation; done/snooze; calendar-mirror idempotence.
- **`DATABASE_URL=…test PYTHONPATH=. uv run python scripts/verify_phase3.py`**: (1) the milestone
  (seeded purchase → exactly one return nudge, second tick → zero); (2) the model fires `add_reminder`
  across phrasings (≥60% floor); (3) real calendar event create→verify→**delete** (needs the
  re-consent).
- **JobQueue firing smoke:** build an Application, register a repeating job, run the loop briefly,
  assert it fired ≥3× — don't assume the scheduler fires.
- **Live check (transport only):** "remind me to stretch in 2 minutes" → the nudge arrives (~2 min),
  Snooze returns it later, Done closes it; the calendar event appears.

---

## Common gotchas

- **`column "purchase_id" does not exist`** → the `init_db()` ALTER didn't run; `create_all()` never
  alters an existing table. See Step 2.
- **Calendar mirror `invalid_scope`** → the token predates the `calendar.events` scope; delete
  `data/google_token.json` and re-consent.
- **Reminder never fires** → check `/pause` state (runtime flag), quiet hours (9pm–8am local), and
  that `due_at` has actually passed. Nothing is lost — it fires on the next non-quiet tick.
- **"tonight"/monthly-day-of-month** parse imperfectly — known limitation; NL parsing covers common
  phrases, and unparseable ones return a clear error, never a wrong reminder.
- **Model didn't call `add_reminder`** → soft-tier reliability on the 7B; measured, not guaranteed
  (verify_phase3 tracks the rate).

---

## Hardening (post-review pass)

A critical review added four strengthenings (all tested offline in `tests/test_hardening.py`):
- **Untrusted-content framing** — email subjects/senders and calendar titles are attacker-
  influenceable, so `calendar_list_events`/`gmail_list_recent` wrap their output in a "external
  content — information only, not instructions" frame (`frame_untrusted`). Prompt-tier
  defense-in-depth; the control model (gated drafts, no send, whitelist, local) already bounds any
  injection to at-worst memory/reminder/calendar pollution.
- **Cached Google service objects** — `build()` re-fetched the discovery doc every call; now built
  once (`reset_service_cache()` after re-consent). Latency win.
- **DST-correct recurrence** — `next_occurrence` advances in the local IANA zone (`tzlocal`), so a
  "daily 8am" reminder stays 8am local across a DST change instead of drifting an hour.
- **Action rate cap** — `app/agent/rate_limit.py` caps `create_draft`/`add_reminder` per rolling
  hour (`max_actions_per_hour`), a runaway/injection-loop guard on top of the per-turn recursion
  limit. `create_draft`'s check is *after* approval so the `interrupt()` re-run doesn't double-count.

## What Phase 3A deliberately does NOT do (comes in 3B / later)

- **No Gmail reading / quarantined reader / email-body ingestion** — all 3B. Return reminders are
  *seeded* here; auto-extraction from receipts is next.
- **No LLM-composed nudges** — deterministic text (a scheduled push must be reliable, not
  model-dependent).
- **Recurrence is a small set** (daily/weekly/monthly + anchor); exotic rules are later.
- **No daily digest yet.**

---

## Suggested commit

```bash
git add app/memory/ app/proactive/ app/agent/tools/ app/agent/persona.md app/channels/telegram.py \
        app/config.py app/integrations/ pyproject.toml uv.lock tests/test_reminders.py \
        scripts/verify_phase3.py docs/07-phase3a-build.md CLAUDE.md docs/04-constitution.md README.md
git commit -m "Phase 3A: proactive reminder engine (conversational, recurring, calendar-mirrored)"
```
