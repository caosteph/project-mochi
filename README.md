# Mochi — a private, local-first personal AI agent

[![Tests](https://github.com/caosteph/project-mochi/actions/workflows/tests.yml/badge.svg)](https://github.com/caosteph/project-mochi/actions/workflows/tests.yml)
[![Ruff](https://github.com/caosteph/project-mochi/actions/workflows/ruff.yml/badge.svg)](https://github.com/caosteph/project-mochi/actions/workflows/ruff.yml)

Mochi is a personal AI assistant you message from your phone like a friend. It remembers your
life, watches your inbox and calendar, sets reminders, builds little web apps and documents — and
it does this **proactively** (its flagship trick: notice a purchase in your email and remind you to
return it before the window closes).

The point that shapes every design decision: **it runs on your own machine, on open-weight models,
and your private data never leaves.** No personal data is sent to a cloud LLM. Mochi *proposes*;
you *dispose* — every action that touches the outside world waits for your explicit approval.

> **Status:** actively built, phase by phase. Durable memory, Google Calendar/Gmail, proactive
> reminders, safe email reading, a daily briefing, web search, and a sandboxed app/document builder
> all work. It runs supervised (launchd restarts it) and is exercised by CI plus a real-model
> regression gate. See [Current status](#current-status).

> This is a personal project built in the open as a learning exercise. It is **not** a packaged
> product — expect rough edges, and read [`CLAUDE.md`](./CLAUDE.md) + [`docs/`](./docs) before
> running it.

---

## Why this exists

Most "AI assistants" send everything you say to a cloud model. For an assistant that holds your
email, your calendar, your habits, and your relationships, that's the wrong default. Mochi is an
experiment in the opposite: a genuinely *personal* agent where

- **privacy is structural, not a promise** — anything sourced from your data runs on a local model
  with local embeddings, enforced by a deterministic router in code, not by a prompt;
- **you stay in control** — it can draft an email but never send one; every external write pauses
  for your approval;
- **untrusted content can't hijack it** — email/web text is read by a separate, tool-less model and
  reduced to validated data before the main agent ever sees it.

## How it works

```
     Telegram (your phone)
            │  long-polling, chat-id whitelisted
            ▼
   ┌─────────────────────────────────────────────┐
   │  LangGraph agent  (local model via Ollama)   │
   │  • dynamic per-turn tool selection           │
   │  • human-in-the-loop interrupt() for writes  │
   │  • rolling summary + context trimming        │
   └───────┬─────────────────────────┬────────────┘
           │ tools                    │ untrusted email/web bodies
           ▼                          ▼
   memory · reminders · google   ┌──────────────────────────┐
   · builder · expert-consult    │  Quarantined reader       │
           │                     │  (separate, TOOL-FREE      │
           ▼                     │   local model → validated  │
   Postgres + pgvector           │   structured data only)    │
   (relational + semantic        └──────────────────────────┘
    memory + LangGraph
    checkpointer)
```

- **Deterministic sensitivity router** picks local vs. hosted models *by data origin* (in code,
  never by an LLM), and fails closed. Personal data → always local. A hosted model is used only for
  opt-in, de-identified, PII-scrubbed, audited questions — raw personal data never leaves.
- **Dynamic tool selection** binds only a small, relevant subset of tools per turn, chosen from your
  message. It was built for an apparent "tool-count wall", which later measurement showed was really
  context exhaustion (~95 prompt tokens per bound tool against a 4,096 window). With that fixed all
  tools bind fine, so this is now an optimization — it saves ~665 prompt tokens a turn — rather than
  a workaround.

## Tech stack

| Area | Choice |
|------|--------|
| Language / tooling | **Python 3.12**, [`uv`](https://github.com/astral-sh/uv) |
| Agent runtime | **LangGraph** — stateful graph, tool nodes, `interrupt()`, durable Postgres checkpointer |
| Models (local) | **Ollama** — `qwen2.5:7b-8k` (chat, a Modelfile variant — see the quickstart note) + `nomic-embed-text` (embeddings), via an OpenAI-compatible API so local↔hosted is a base-URL swap |
| Data | **Postgres + pgvector** — one store for relational tables (SQLModel), semantic recall, and the checkpointer |
| Channel | **python-telegram-bot** (long-polling); a `Channel` interface keeps iMessage a drop-in later |
| Integrations | **Google API** (Calendar + Gmail, least-privilege scopes — `gmail.readonly` + `gmail.compose`, never `send`) |
| Builder | **reportlab** / **python-docx** for documents; a subprocess sandbox for generated web apps |

## Safety model (the non-negotiables)

These are enforced in **code**, outside the model, so they hold even if the model is fooled or
prompt-injected. Full auditable list: [`docs/04-constitution.md`](./docs/04-constitution.md).

1. **Privacy by data origin.** Your data → local model + local embeddings, always. The router fails
   closed; `LOCAL_ONLY=true` forces everything local (the default).
2. **Never send email.** Gmail scope is read + *compose drafts only*. Mochi drafts; you press send.
3. **Every external write is human-gated** via a LangGraph `interrupt()` → Telegram Approve/Reject.
4. **Untrusted content is data, never instructions** — parsed by a quarantined, tool-free reader
   that emits only validated structured data (the dual-LLM / CaMeL pattern).
5. **Secrets never leave the machine and never get committed** (`.env` is git-ignored; the code
   sandbox can't see `data/` or tokens).

## Current status

Everything below works today. Each capability's step-by-step build history lives in [`docs/`](./docs).

- **Remembers your life.** Durable memory in Postgres with local embeddings and hybrid recall. It was
  seeded with a profile so it starts out knowing you, and a curated set of your own style rules is
  pinned into every reply, so it follows your voice without having to look them up first.
- **Watches your inbox, safely.** It reads Gmail and Calendar with least-privilege access: it can
  draft an email but never send one. Untrusted email is parsed by a separate, tool-free reader that
  only emits validated data, so a malicious message can't hijack it. From that it spots things worth
  acting on (a return window, a bill, an appointment, a deadline, a delivery) and asks you first before
  making a reminder. It can also summarize a specific email on request ("what did the landlord's email
  say?").
- **Reminds you, and knows when to stop.** Natural-language reminders, one-off or recurring, pushed
  with quiet hours and Done/Snooze buttons and mirrored to Google Calendar. When you say you've already
  done something, it retires the whole topic, cancelling outstanding reminders and blocking a later
  email from re-creating them, instead of nagging about something that's finished.
- **Briefs you each morning.** One digest of today's calendar, reminders, and goals, assembled without
  the model (so it can't wander or dump raw data), pushed each morning or on demand with `/briefing`.
- **Looks things up online.** Weather, hours, prices, "is X open." Only a scrubbed, PII-free query
  leaves the machine, and you approve it before it goes; results are pulled together locally and every
  search is logged.
- **Builds things.** Small web apps and PDF or Word documents, generated and run in a sandbox, then
  served to your phone over your home network.
- **Asks a stronger model when it helps.** A de-identified, scrubbed, audited `/ask` can consult a
  bigger hosted model for generic questions while your raw personal data stays local.
- **Talks in buttons.** Any yes/no or pick-one decision shows up as tappable inline buttons rather than
  a question you have to type "yes" at.
- **Runs unattended.** Supervised by launchd (restarts on crash, repairs its own dependencies first), a
  single-instance lock so it can't answer you twice, a local model tuned with enough context that
  replies don't run dry mid-sentence, and text that formats as it streams.

## Roadmap & future work

The full, self-contained list (problem → why → effort) is **[`docs/14-future-work.md`](./docs/14-future-work.md)**;
the detailed phase plan is [`docs/00-plan.md`](./docs/00-plan.md). Highlights:

- **Widen memory capture** — memory is now seeded and shaped into every reply (see Current status),
  but *organic* capture is still coverage-limited (~7% of messages carry a self-fact); the lever is
  mining her existing signal (reminders, calendar), not tuning the extractor.
- **Flip the email signal scanner to live** — the flagship proactive feature currently runs in
  **shadow mode** (scans real mail and logs detections, stores nothing, messages nothing) pending a
  few days of precision review; going live is a one-line `.env` change.
- **Try a newer same-size local model** — context, not model capability, turned out to be the
  bottleneck, so the model choice deserves a fair re-test. Free.
- **Reliability/ops:** checkpoint pruning, Alembic migrations, a Docker sandbox.
- **Capabilities:** Google Drive, deeper memory, voice notes, email-in-briefing, a generalizable
  per-action approval layer.
- **Search:** SearXNG (fully-local) / Brave providers behind the existing one-line-swap seam.

## Quickstart

Prerequisites (full setup in [`docs/03-phase0-build.md`](./docs/03-phase0-build.md)): Ollama running
with `qwen2.5:7b` + `nomic-embed-text` pulled, Postgres with a `personal_agent` DB and the `vector`
extension, and a Telegram bot token.

```bash
# Build the 8k-context model (REQUIRED — see note below), then:
ollama create qwen2.5:7b-8k -f ollama/Modelfile.qwen2.5-7b-8k

cp .env.example .env      # fill in TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (+ Google creds for P2+)
uv sync                   # install dependencies
uv run python -m app.main
```

> **Why the custom model:** Ollama's default context is 4,096 tokens, but a turn's prompt here is
> already ~4,000 (persona + tool schemas + history) — leaving ~75 tokens to generate a reply, which
> forces context-shifting that silently evicts the system prompt and breaks tool-calling. Measured:
> several prompts went **0/4 → 4/4** from this change alone. Details in
> [`docs/14-future-work.md`](./docs/14-future-work.md).

Message your bot on Telegram — the reply is generated entirely on your own machine.

## Repository layout

```
app/
  agent/        LangGraph graph, persona, tools, router, quarantined reader, tool selection,
                the always-on profile card (pinned memory injected every turn)
  channels/     Telegram adapter — core + streaming/commands/buttons mixins, rendering
                (and a Channel interface + contract for future transports)
  integrations/ Google auth / Calendar / Gmail
  memory/       SQLModel models, embeddings, hybrid recall, fact extraction
  proactive/    reminders (+ reminder_time parsing, reminder_calendar mirroring), email-signal
                scanning, the daily briefing, the job scheduler
  builder/      sandboxed web-app + document generation and LAN serving
docs/           the roadmap, primers, and a build guide per phase (single source of truth)
scripts/        verify_*.py real-model checks + preflight/run wrappers (_verify_lib.py is shared)
tests/          offline pytest suite (mocks the model + Google)
launchd/        the agent plist (starts at login, restarts on exit) + a daily DB-backup plist
backups/        local rotated pg_dumps of the memory DB (git-ignored; prove-restore via restore_check.sh)
ollama/         Modelfile for the 8k-context model variant
.github/        CI: ruff + the hermetic test suite on every push
CLAUDE.md       orientation + the non-negotiable safety rules — read this first
```

## Testing philosophy

Two layers, because they catch different things:

- **`tests/`** — a fast offline `pytest` suite (a few hundred tests, ~30s) that mocks the model + Google.
  Proves the plumbing. It earns its keep: writing the channel-button tests is what surfaced a live
  bug where pressing "Snooze" saved the change but crashed before confirming it.
- **`scripts/verify_*.py`** — real-model checks that drive the *actual* agent (`build_agent()`) and
  the real 7B end-to-end, asserting *behavior* (the right tool fires, no raw-JSON dumps, injection
  is refused). The local model is stochastic, so these use soft floors, and a behavioural check that
  wobbles is decided from several samples (`sample_check`) rather than one — while a *must-not* check
  requires **every** sample to be clean, so a retry can't launder a violation. Each check prints its
  `hits/attempts`, so scraping by never looks like a clean pass. `scripts/verify_all.sh` runs the
  whole regression sequentially.

A **pre-push hook** (`.githooks/pre-push`, opt in with `./scripts/install_hooks.sh`) reproduces CI
locally before every push: it runs `ruff` and the hermetic suite **with the embedding endpoint pointed
at a dead host**, so a test that embeds without the `needs_ollama` marker — which passes locally but
fails in CI, since CI has no Ollama — is caught here instead. Bypass with `git push --no-verify`.

```bash
./scripts/install_hooks.sh   # one-time: enable the pre-push CI-parity gate
uv run pytest tests/ -q
uv run ruff check app/ tests/ scripts/
./scripts/verify_all.sh      # full real-model regression (needs Ollama + a scratch DB)
```

## License

Personal project, shared for reference. No license granted for reuse at this time.
