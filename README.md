# personal-agent

A private, local-first personal AI agent you message from your phone. Telegram → LangGraph →
a local open-weight model (Ollama), with durable Postgres state. Long-term memory, Gmail/
Calendar/Drive, goal tracking, and proactivity are added phase by phase.

> **Design docs & roadmap live in [`docs/`](./docs).** Start with [`docs/00-plan.md`](./docs/00-plan.md).
> **Working in this repo (human or AI)?** Read [`CLAUDE.md`](./CLAUDE.md) first — it has the
> non-negotiable privacy/safety rules.

## Status

**Phase 3A** — proactive reminders: on top of durable memory (P1) and Google Calendar/Gmail with a
human-in-the-loop approval gate (P2), Mochi is now **proactive** — set any reminder by talking to her
("call mom every Sunday"), and she pushes it to you unprompted (with Done/Snooze), quiet-hours-aware,
mirrored into Google Calendar. Email receipt auto-extraction is next (3B). See the per-phase guides in
[`docs/`](./docs).

## Quickstart

Full setup (Ollama, Postgres, Telegram bot) is in [`docs/03-phase0-build.md`](./docs/03-phase0-build.md).
Once prerequisites are in place:

```bash
cp .env.example .env      # then fill in TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
uv sync                   # install dependencies
uv run python -m app.main
```

Message your bot on Telegram — you should get a reply generated entirely on your Mac.

## Safety in one line

The agent proposes; you dispose. Private data stays local, Gmail can draft but never send,
and every real-world action needs your explicit approval. Details in `CLAUDE.md`.
