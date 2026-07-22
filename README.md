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

Built in phases; each has a build doc in [`docs/`](./docs).

- **P1 — memory core:** durable Postgres memory, local embeddings + hybrid recall, the tool-calling loop.
- **P2 — Google + approval gate:** Calendar/Gmail (least-privilege), human-in-the-loop draft approval.
- **P3A — proactive reminders:** natural-language reminders (one-off/recurring), pushed with quiet
  hours + Done/Snooze, mirrored to Google Calendar.
- **P3B — safe email reading:** the quarantined reader + a general signal pipeline (return / bill /
  appointment / deadline / delivery) that asks before acting.
- **P4A — sensitivity router + de-identified hosted consult** (`/ask`): raw data stays local; only
  scrubbed, audited derivatives can reach an opt-in hosted model.
- **P4B — the builder:** generates web apps + PDFs/Word docs, runs them in a sandbox, serves static
  sites on your LAN. Solved the tool-count wall with dynamic per-turn tool binding.
- **P6 — daily briefing:** one deterministic (no-LLM) morning digest of today's calendar, reminders,
  and goals — pushed each morning and on demand via `/briefing`.
- **P7 — read email on demand:** "what did the landlord's email say?" → a safe summary via the same
  quarantined reader (raw body never reaches the main agent).
- **P8 — web search:** "what's the weather / is X open / current price of Y" → the local model looks
  it up online. Only a **PII-scrubbed** query leaves (you approve it first), results are synthesized
  locally, every query is logged to `/sent`. Pluggable provider (Tavily or keyless DuckDuckGo).
- **Reliability pass:** the local model runs at an 8k context — at Ollama's default 4,096 a turn's
  prompt (~4,000 tokens) left almost no room to generate, which silently degraded tool-calling;
  fixing it took several previously-dead prompts from 0/23 to 18/18. Plus **launchd supervision**
  with a dependency preflight, a **single-instance lock** (two pollers on one bot token answer every
  message twice), reminder de-duplication, and formatting that renders *while* a reply streams.
- **Tappable decisions:** when Mochi needs a yes/no or pick-one answer it shows **inline buttons**
  rather than a question you have to type "yes" at (e.g. cancelling a reminder that matches more than
  one → a picker). Built on the same human-in-the-loop `interrupt()` spine as draft approval.
- **Tasks can be retired:** tell Mochi you've already done something ("I submitted the claims, stop
  reminding me") and it records the *topic* as done — cancelling outstanding reminders and blocking any
  re-creation from a later email — instead of just clearing one reminder that comes back.

## Roadmap & future work

The full, self-contained list (problem → why → effort) is **[`docs/14-future-work.md`](./docs/14-future-work.md)**;
the detailed phase plan is [`docs/00-plan.md`](./docs/00-plan.md). Highlights:

- **⭐ Back up the memory database** — everything the project is *for* lives in one Postgres with no
  backups. Highest risk-to-effort item on the list.
- **Make long-term memory actually accumulate** — the premise is a durable memory; in practice it
  captures far less than it should, which is the gap between "agent" and "chatbot with tools".
- **Re-enable the email signal scanner** — the flagship proactive feature, currently off because
  early scans were too noisy.
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
  agent/        LangGraph graph, persona, tools, router, quarantined reader, tool selection
  channels/     Telegram adapter — core + streaming/commands/buttons mixins, rendering
                (and a Channel interface + contract for future transports)
  integrations/ Google auth / Calendar / Gmail
  memory/       SQLModel models, embeddings, hybrid recall, fact extraction
  proactive/    reminders, email-signal scanning, the daily briefing, the job scheduler
  builder/      sandboxed web-app + document generation and LAN serving
docs/           the roadmap, primers, and a build guide per phase (single source of truth)
scripts/        verify_*.py real-model checks + preflight/run wrappers (_verify_lib.py is shared)
tests/          offline pytest suite (mocks the model + Google)
launchd/        the agent plist — starts at login, restarts on exit
ollama/         Modelfile for the 8k-context model variant
.github/        CI: ruff + the hermetic test suite on every push
CLAUDE.md       orientation + the non-negotiable safety rules — read this first
```

## Testing philosophy

Two layers, because they catch different things:

- **`tests/`** — a fast offline `pytest` suite (232 tests, ~16s) that mocks the model + Google.
  Proves the plumbing. It earns its keep: writing the channel-button tests is what surfaced a live
  bug where pressing "Snooze" saved the change but crashed before confirming it.
- **`scripts/verify_*.py`** — real-model checks that drive the *actual* agent (`build_agent()`) and
  the real 7B end-to-end, asserting *behavior* (the right tool fires, no raw-JSON dumps, injection
  is refused). The local model is stochastic, so these use soft floors, and a behavioural check that
  wobbles is decided from several samples (`sample_check`) rather than one — while a *must-not* check
  requires **every** sample to be clean, so a retry can't launder a violation. Each check prints its
  `hits/attempts`, so scraping by never looks like a clean pass. `scripts/verify_all.sh` runs the
  whole regression sequentially.

```bash
uv run pytest tests/ -q
uv run ruff check app/ tests/ scripts/
./scripts/verify_all.sh      # full real-model regression (needs Ollama + a scratch DB)
```

## License

Personal project, shared for reference. No license granted for reuse at this time.
