# personal-agent

A private, local-first personal AI agent you message from your phone. Telegram → LangGraph →
a local open-weight model (Ollama), with durable Postgres state. Long-term memory, Gmail/
Calendar/Drive, goal tracking, and proactivity are added phase by phase.

> **Design docs & roadmap live in `~/personal-agent-docs/`.** Start with `00-plan.md`.
> **Working in this repo (human or AI)?** Read [`CLAUDE.md`](./CLAUDE.md) first — it has the
> non-negotiable privacy/safety rules.

## Status

**Phase 0** — scaffolding & message loop: text the bot from your phone, get a local-model
reply, and the conversation survives a restart. No memory / Gmail / proactivity yet.

## Quickstart

Full setup (Ollama, Postgres, Telegram bot) is in `~/personal-agent-docs/03-phase0-build.md`.
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
