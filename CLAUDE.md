# CLAUDE.md — personal-agent

Orientation for any AI session (or human) working in this repo. Read this first.

## What this is

A **private, local-first personal AI agent** that Stephanie messages from her phone. It holds
long-term memory about her life, connects to Gmail/Calendar/Drive, tracks goals, builds
web apps/PDFs, and is **proactive** (flagship example: notice a purchase in email → remind her
to return it before the window closes).

The full plan, learning docs, and per-phase build guides live in this repo's **`docs/`** folder
— the **single source of truth** for design decisions:
- `docs/00-plan.md` — full end-to-end roadmap (Phases 0–10).
- `docs/01-primer.md` — beginner explanation of agent concepts.
- `docs/02-architectures.md` — technical guide (named frameworks, diagrams).
- `docs/03-phase0-build.md` — the step-by-step build guide this repo implements.

## Current status

**Phase 0 — scaffolding & message loop.** This repo currently implements only Phase 0:
Telegram long-polling → a single-node LangGraph agent → a local Ollama model, with a durable
Postgres checkpointer so conversation state survives restarts. No memory, no Gmail, no
proactivity yet — those are Phases 1–3.

## Non-negotiable safety rules (these outlive any single task)

These are the reason the project exists as *local-first with a human in charge*. Do not
weaken them without Stephanie's explicit say-so.

1. **Privacy — data origin decides the model, deterministically.** Anything sourced from
   Gmail/Calendar/Drive/memory or Stephanie's personal data is **sensitive → local model +
   local embeddings only**. Only generic/public work may use a hosted model, and only when the
   router (Phase 4) is opt-in. The router **fails closed** (unknown → local). `LOCAL_ONLY=true`
   forces everything local; it is the current default. **Embeddings are always local.**
2. **Never grant `gmail.send`.** Gmail scope is `readonly` + `gmail.compose` (drafts only).
   The agent drafts; Stephanie presses send. No send/post/share/delete tools are registered by
   default.
3. **Every side-effectful action is human-gated.** External writes pause via LangGraph
   `interrupt()` and require an explicit Telegram approval before executing.
4. **Untrusted content is data, never instructions.** Email/web/Drive content is parsed by a
   quarantined reader model with **no tools** that emits only validated structured data. The
   privileged agent never ingests raw untrusted text. (Dual-LLM / CaMeL pattern.)
5. **Secrets never leave the machine and never get committed.** `.env` is git-ignored. The
   code sandbox (Phase 4) has no access to `data/` or OAuth tokens.

If a task seems to require breaking one of these, stop and confirm with Stephanie first.

## Architecture (locked decisions)

- **Language:** Python 3.12. **Package/venv manager:** `uv` (falls back to stdlib venv + pip).
- **Runtime:** LangGraph — stateful graph, tool nodes, `interrupt()` for human-in-the-loop,
  durable Postgres checkpointer.
- **Models:** open-weight only. Local via **Ollama** (Qwen 2.5 class); OpenAI-compatible
  endpoints so local↔hosted is a base-URL swap, chosen per-task by a deterministic router (later).
- **Data:** one **Postgres + pgvector** instance = relational tables (SQLModel) + semantic
  recall + the LangGraph checkpointer.
- **Channel:** Telegram now (`python-telegram-bot`, long-polling); a `Channel` interface keeps
  iMessage/BlueBubbles a drop-in for Phase 9.
- **Integrations:** off-the-shelf MCP servers via `langchain-mcp-adapters` (Phase 2+).

## Repo layout

```
personal-agent/
  pyproject.toml        # deps; treated as an application (tool.uv package = false)
  .env.example          # template — copy to .env, fill in, never commit .env
  CLAUDE.md             # this file
  README.md             # human quickstart
  app/
    config.py           # pydantic-settings; LOCAL_ONLY switch; whitelisted chat_id
    main.py             # entrypoint: python -m app.main
    agent/graph.py      # LangGraph graph + Postgres checkpointer + local model
    channels/base.py    # Channel interface
    channels/telegram.py# long-polling adapter + chat_id whitelist
  data/                 # local state / tokens (git-ignored)
  workspace/            # agent-built artifacts (git-ignored)
```

Later phases add `app/memory/`, `app/integrations/`, `app/proactive/`, `app/builder/`,
`app/agent/router.py`, and `app/agent/confirm.py`. Create these when their phase starts —
don't stub them out empty. See `00-plan.md` for the target tree.

## Running it

Prereqs (see `03-phase0-build.md` for full setup): Ollama running with `qwen2.5:7b` pulled,
Postgres.app with a `personal_agent` DB (+ `vector` extension), and a `.env` filled from
`.env.example` (bot token + your chat_id).

```bash
cd ~/personal-agent
uv run python -m app.main        # or: source .venv/bin/activate && python -m app.main
```

## Conventions

- Keep Phase 0 lean: no speculative abstractions, no half-finished later-phase code.
- All config flows through `app/config.py` — don't read env vars directly elsewhere.
- Prefer editing existing files; keep the `Channel` seam and the safety layers intact.
- When you finish a phase, update the "Current status" section above.
