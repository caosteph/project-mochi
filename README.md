# Mochi — a private, local-first personal AI agent

Mochi is a personal AI assistant you message from your phone like a friend. It remembers your
life, watches your inbox and calendar, sets reminders, builds little web apps and documents — and
it does this **proactively** (its flagship trick: notice a purchase in your email and remind you to
return it before the window closes).

The point that shapes every design decision: **it runs on your own machine, on open-weight models,
and your private data never leaves.** No personal data is sent to a cloud LLM. Mochi *proposes*;
you *dispose* — every action that touches the outside world waits for your explicit approval.

> **Status:** actively built, phase by phase. Currently at **Phase 8** — durable memory, Google
> Calendar/Gmail, proactive reminders, safe email reading, a daily briefing, web search, and a
> sandboxed app/document builder are all working. See [Current status](#current-status).

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
- **Dynamic tool selection** solves a real limit: the local 7B collapses when bound with too many
  tools, so each turn binds only a small, relevant subset chosen from your message.

## Tech stack

| Area | Choice |
|------|--------|
| Language / tooling | **Python 3.12**, [`uv`](https://github.com/astral-sh/uv) |
| Agent runtime | **LangGraph** — stateful graph, tool nodes, `interrupt()`, durable Postgres checkpointer |
| Models (local) | **Ollama** — `qwen2.5:7b` (chat) + `nomic-embed-text` (embeddings), via an OpenAI-compatible API so local↔hosted is a base-URL swap |
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

## Roadmap & future work

The detailed phase plan lives in [`docs/00-plan.md`](./docs/00-plan.md); this is the consolidated
list of what's next, grouped.

**More capabilities**
- **Google Drive** (read files — receipts, docs) via the quarantined reader.
- **Deeper long-term memory & preferences** — a richer profile so Mochi knows you better over time.
- **Voice-message transcription** · **quick lists/notes**.
- **Email in the daily briefing** — once the Phase 3B scanner is proven quiet and re-enabled.
- **Deep-read a web result** — fetch a full result page through the quarantined reader (today's web
  search is snippets only).

**Search**
- Add/switch providers — **SearXNG** (self-hosted, fully local routing) and **Brave**; smarter
  result ranking. (Switching providers is already a one-line config change.)

**Safety & permissions**
- A **generalizable per-action approval layer** — a config-driven policy of *which* actions require
  your Approve/Reject (today: drafts + web search; extend to calendar-event writes, etc.).

**Platform & quality**
- Move to a **Mac mini running a larger local model** — the single biggest quality lever. Most rough
  edges (tool-firing reliability, the occasional derail) are the 7B's limits, not the design's.

## Quickstart

Prerequisites (full setup in [`docs/03-phase0-build.md`](./docs/03-phase0-build.md)): Ollama running
with `qwen2.5:7b` + `nomic-embed-text` pulled, Postgres with a `personal_agent` DB and the `vector`
extension, and a Telegram bot token.

```bash
cp .env.example .env      # fill in TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (+ Google creds for P2+)
uv sync                   # install dependencies
uv run python -m app.main
```

Message your bot on Telegram — the reply is generated entirely on your own machine.

## Repository layout

```
app/
  agent/        LangGraph graph, persona, tools, router, quarantined reader, tool selection
  channels/     Telegram adapter (+ a Channel interface for future transports)
  integrations/ Google auth / Calendar / Gmail
  memory/       SQLModel models, embeddings, hybrid recall, fact extraction
  proactive/    reminders, email-signal scanning, the daily briefing, the job scheduler
  builder/      sandboxed web-app + document generation and LAN serving
docs/           the roadmap, primers, and a build guide per phase (single source of truth)
scripts/        verify_*.py — real-model checks that drive the actual agent end-to-end
tests/          offline pytest suite (mocks the model + Google)
CLAUDE.md       orientation + the non-negotiable safety rules — read this first
```

## Testing philosophy

Two layers, because they catch different things:

- **`tests/`** — a fast offline `pytest` suite that mocks the model + Google. Proves the plumbing.
- **`scripts/verify_*.py`** — real-model checks that drive the *actual* agent (`build_agent()`) and
  the real 7B end-to-end, asserting *behavior* (the right tool fires, no raw-JSON dumps, injection
  is refused). The local model is stochastic, so these use soft floors and are re-run to rule out
  variance. `scripts/verify_all.sh` runs the whole regression sequentially.

```bash
uv run pytest tests/ -q
uv run ruff check app/ tests/ scripts/
./scripts/verify_all.sh      # full real-model regression (needs Ollama + a scratch DB)
```

## License

Personal project, shared for reference. No license granted for reuse at this time.
