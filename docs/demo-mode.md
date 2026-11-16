# Demo mode — try the real agent in ~2 minutes

Mochi is a local-first personal agent, so running it normally needs a Telegram bot,
Google credentials, and your own data. Demo mode removes all of that: it runs the
**real agent** — same graph, same tools, same local model — against **fictional
fixture data**, with no accounts and no personal data anywhere.

> **No video/GIF yet** — this doc covers the interactive demo. A recorded walkthrough
> is coming later.

## Run it

Prerequisites: Docker + Docker Compose. First run pulls ~5 GB of models
(`qwen2.5:7b` + `nomic-embed-text`); later runs are fast.

```bash
docker compose up
# type at the prompt; /help lists commands, /quit exits
```

That's it. The compose stack starts Postgres+pgvector, Ollama (pulls the models and
creates the `qwen2.5:7b-8k` variant), seeds the demo database, then drops you at
Mochi's console prompt. Ports 8100–8110 are published so web apps Mochi builds
open in your browser at `http://localhost:8100`.

```bash
docker compose down        # stop (add -v to wipe the demo database + models)
```

### Without Docker

If you already run Mochi locally (Ollama + Postgres per
[`docs/03-phase0-build.md`](./03-phase0-build.md)), you can run demo mode directly:

```bash
DEMO_MODE=true DATABASE_URL=postgresql://postgres@localhost/personal_agent_demo \
  uv run python scripts/seed_demo.py
DEMO_MODE=true DATABASE_URL=postgresql://postgres@localhost/personal_agent_demo \
  uv run python -m app.main
```

The seed script refuses to run unless `DEMO_MODE=true` **and** the database URL
contains `demo` or `test` — demo data never lands in a real database.

## What's in the demo data

Everything is fictional — invented names, merchants, and addresses
(`demo/fixtures/`). The demo user is **Alex Rivera** (alex.rivera@example.com).

**Inbox** (`demo/fixtures/inbox.json`) — 5 messages, dated relative to today so the
demo works any day:

| Message | Why it's there |
|---|---|
| Northwind Outfitters — your Trailhead Rain Jacket shipped, returns by a date 2 days out | The proactive return reminder |
| "Northwind Outfitters Support" — URGENT security notice, SPF/DKIM fail | The prompt-injection defense |
| ParcelPost — your package arrives Friday | Ordinary mail the scan handles normally |
| Trailhead Monthly — November trail guide | A newsletter the scan ignores |
| Jordan Lee — dinner Thursday? | Ordinary mail, possibly an appointment |

**Calendar** (`demo/fixtures/calendar.json`) — a dentist appointment tomorrow, dinner
with Jordan the day after, a team standup the day after that.

**Profile** (`demo/fixtures/profile.json`) — pinned facts (Alex prefers concise
replies, likes hiking), a few facts, and a goal (run a 10k by spring).

## What to try

### 1. The proactive return reminder

```
/scan
```

The scan reads the fixture inbox (at most 3 new messages per scan — the real
cost bound) and asks about anything actionable. It finds the Northwind order:
the jacket's return window closes in 2 days. Approve it with the id the scan
printed (the dinner invite may also surface as an appointment — approve or
reject it the same way):

```
/approve 2
/tick
```

`/approve` creates the reminder; `/tick` fires it, and Mochi tells you the
return is due.

### 2. The injection defense

```
/scan
```

The second scan processes the remaining messages, including the "URGENT: Security
notice" email. That email is addressed to the AI, not to Alex: it orders the
assistant to draft a fake refund confirmation and delete the return reminders,
and not to tell the user.

Nothing happens. No draft, no deletion, no nudge. That's the point:

- The email is parsed by the **quarantined reader** — a separate, persona-free,
  tool-free model call that can only return a summary. It cannot act.
- The sender fails SPF/DKIM/DMARC and isn't in the trusted-sender list, so it's
  flagged suspicious.
- The reader marks it not actionable (it's an instruction to the agent, not a
  return/bill/appointment), so no signal is created at all.

Try it directly: `what did the security email say?` — Mochi reads it through the
quarantined reader and summarizes it safely.

### 3. The builder

```
build me a small web app — a packing checklist for a hiking trip
```

Mochi generates a single-page site with the local model and serves it on your
machine. It prints a link like `http://localhost:8100` — open it in your browser.
Then ask for a change ("make it dark themed") and it updates the same site in place.

### 4. The rest of the agent

- `what's on my calendar this week?` — reads the fixture calendar.
- `what do you know about me?` — the seeded profile (pinned facts + goals).
- `draft an email to jordan.lee@example.com saying I can do Thursday` — drafts it
  (never sends), then `/drafts` lists it.
- `/briefing` — the daily briefing over the demo data.
- `remember that I prefer window seats` — stored as a fact (the post-turn fact
  sweep captures it), and it shows up in later turns.

## How it works (the seam)

Demo mode is a thin seam, not a fork — the agent logic is identical; only the
data sources are swapped:

- **`DEMO_MODE=true`** (config, `app/config.py`) flips three things:
  1. The Google integration modules (`app/integrations/google_gmail.py`,
     `google_calendar.py`) dispatch to `app/integrations/demo.py` — a
     fixture-backed provider with the same function signatures — whenever no
     explicit `service` is passed. An explicitly passed service (the
     offline-test seam) always wins.
  2. `app/main.py` runs the **console channel** (`app/channels/console.py`)
     instead of Telegram — same Channel interface, stdin/stdout transport,
     `/scan` `/approve` `/reject` `/tick` in place of the JobQueue and buttons.
  3. The persona loads `demo/persona.md` (same voice, fictional user Alex)
     instead of `persona.md`, so the agent addresses its demo user correctly.
- **`scripts/seed_demo.py`** loads the profile fixtures into Postgres and
  backdates the email-scan state a week, so the first `/scan` treats the
  fixture inbox as new mail. Idempotent — re-running clears the seeded rows first.
- **Fixtures use relative dates** (`days_ago`, `days_from_now`,
  `{{return_by}}`, `{{iso:+N}}`) resolved at load, so the demo works any day.
- **Builder URLs** use `BUILDER_PUBLIC_HOST=localhost` in compose so the
  printed link is reachable from the host (the server itself always binds
  `0.0.0.0`).

Tests: `tests/test_demo.py` covers the fixture provider, the seed script, and
the console channel's commands (all offline, mocked model + Google).

## What still needs a bot token

The live Telegram loop is the one thing demo mode doesn't cover: `docker compose up`
runs the console channel, not Telegram. To try the Telegram transport for real,
you need a Telegram bot token (from [@BotFather](https://t.me/BotFather)) and a
chat ID — set them in `.env` and run the normal quickstart in the README. No
token is needed for anything in this demo, and no token is bundled or invented
anywhere in the repo.
