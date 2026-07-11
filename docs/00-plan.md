# Personal AI Agent — Full-System Architecture & Roadmap

> **This folder (`~/personal-agent-docs/`) is the single source of truth.** This file is the canonical plan. A copy at `~/.claude/plans/reactive-gliding-lightning.md` is stale — ignore it.

## Context

A personal AI agent you message from your phone (Telegram first, iMessage later) that holds
long-term memory about your life, connects to Gmail / Calendar / Drive, builds web apps +
mobile apps + PDFs + plans, tracks your goals, and is **proactive** — e.g. noticing a
purchase in your email and reminding you to return it before the window closes.

**This plan covers the entire system end-to-end.** The MVP (Phases 0–3) is the first
deliverable, not the scope limit — every capability you described is planned through Phase 10.

Decisions locked in this planning session:
- **Channel:** Telegram first (swappable adapter; iMessage via BlueBubbles on the Mac mini).
- **Model:** open-weight only.
- **Privacy AND cost are both primary.** → **local-first, sensitivity-aware routing** (below).
- **Hosting:** prototype on the MacBook Air now (Telegram long-polling → no public endpoint);
  Mac mini becomes the always-on, fully-local home.
- **Email scope:** Gmail **read + draft** (drafts for your approval, never auto-send).
- **Build targets:** web + PDFs/docs first, then mobile — all in scope.
- **Runtime framework:** LangGraph (durable, resumable, graph-structured workflows).
- **Data store:** Postgres + pgvector (semantic memory + relational data + LangGraph checkpointer).
- **Integrations:** off-the-shelf MCP servers (Gmail/Calendar/Drive/filesystem) via `langchain-mcp-adapters`.

## Hardware

- **Now — MacBook Air M2, 16GB, macOS 15.5, ~1.1TB free.** Runs a local 7–8B model
  (Llama 3.1 8B / Qwen2.5 7B, quantized ≈ 5–6GB) alongside Postgres + the app. Enough for the
  **private data path**; heavy coding leans on opt-in hosted (non-sensitive only) until the mini.
- **Target — Mac mini M4 Pro, 64GB, ≥512GB SSD (1TB preferred).** ~273 GB/s bandwidth ≈ 2×
  token speed of base M4; 64GB holds a 32B-class coding model + Postgres + OS. Budget fallback:
  M4 base / 32GB (runs ~14B-class, bandwidth-limited). This becomes the always-on, fully-local home.

## Guiding principles

1. **Privacy-first routing, tagged by data *origin* (deterministic).** Work is classified by
   where its data came from — in code, not by an LLM's judgment — and the router **fails closed**
   (defaults to local):
   - *Sensitive* (anything from a Gmail/Calendar/Drive/memory tool, or your personal data)
     → **local model only**, **local embeddings only**. Never leaves the machine.
   - *Non-sensitive* (generic coding, public info, planning with no personal data) → may use an
     **opt-in hosted open-weight** endpoint for quality/speed. A master **`LOCAL_ONLY`** switch
     forces everything local.
   - Embeddings are always local so memory vectors never leave, regardless of the switch.
   - **Honest boundary:** Gmail/Calendar/Drive MCP servers still call Google's cloud (your mail
     already lives there). This principle protects against adding *new* exposure to third-party
     *inference* providers — it is not full air-gapping.
2. **Untrusted content is data, never instructions (dual-LLM / CaMeL pattern).** Untrusted email/
   web/Drive content is parsed by a **quarantined model with no tool access** that emits only
   validated structured data; the **privileged** agent never ingests raw untrusted text. Every
   side-effectful action (send, delete, share, purchase, external write) is **gated behind your
   explicit confirmation**. Tool scopes are minimal; the code sandbox (below) has no access to
   `data/` secrets. This out-of-model policy layer holds even if the model is fooled.
3. **Lean on deterministic plumbing.** ~70% of the system (memory, reminders, scheduling,
   integrations, return-window logic) barely depends on model quality — build it solidly so the
   weaker local model only handles what truly needs an LLM.
4. **One config swap for model location.** All models are OpenAI-compatible endpoints
   (Ollama local ↔ hosted), selected by the router — hosted↔local is configuration, not a rewrite.

## Control & authority model (you stay in charge)

"The agent proposes; you dispose." Layered so "act as you" is prevented at multiple independent
levels — the outer layers hold even if the model is hijacked by a malicious email. Built in from
Phase 2 (when external accounts connect), not deferred.

1. **Credential scoping (hardest limit).** OAuth grants minimized so tokens *cannot* act as you:
   Gmail `readonly` + `gmail.compose` (drafts) — **never `gmail.send`**; no social/posting write
   scopes granted. Enforced by the platform, not the prompt → survives prompt injection.
2. **No outbound tools by default.** No send/post/share/delete tools registered. The agent drafts;
   you press send.
3. **Human-in-the-loop gate.** Side-effectful actions use LangGraph `interrupt()` — the graph
   pauses (state in the checkpointer), the proposal (recipient/subject/body) is pushed to Telegram
   with Approve/Reject buttons, and resumes only on your explicit yes. Default: approve *all* external writes.
4. **Recipient/destination allowlist.** If sending is ever enabled, restrict to pre-approved
   recipients/domains so injected addresses are rejected.
5. **Kill switch & modes.** `/pause` (disable actions), `/kill` (halt), global `DRY_RUN`; plus
   stopping the host service.
6. **Audit log + daily digest.** Every proposed and executed action logged immutably; a daily
   "what I did / drafted" recap so nothing is silent.
7. **Rate limits / caps.** Max outbound actions per hour + hard cap + anomaly halt.

**Hosting note:** a cloud host would put your mail, tokens, and memory on someone else's machine —
reintroducing the privacy exposure you want to avoid, and 24/7 GPU rental is costly. The Mac mini
keeps everything home and is the recommended target; the control model above is identical either way.

## Tech stack

- **Language:** Python 3.12.
- **Messaging:** `python-telegram-bot` (long-polling); a `Channel` interface for later iMessage/BlueBubbles.
- **Agent runtime:** **LangGraph** — stateful graph with tool nodes; `langchain-openai`
  `ChatOpenAI` per endpoint. A **router node** picks local vs hosted per task sensitivity.
- **Local inference:** **Ollama** (M2 now: 7–8B; Mac mini: 32B-class). **Local embeddings**
  (`nomic-embed-text`) from day one.
- **Data store:** **Postgres + pgvector** — relational tables (SQLModel) + `pgvector` semantic
  recall + **LangGraph Postgres checkpointer**. Postgres.app locally; `pg_dump` backups from day one.
- **Memory (LangMem on LangGraph):** three scopes — **semantic** (facts w/ provenance +
  confidence), **episodic** (timestamped events), **procedural** (learned preferences the agent
  appends to its own heuristics). Small always-in-context **core blocks** (user persona, current
  task, <1k tokens) + summarized working buffer. **Hybrid retrieval** (vector + keyword +
  recency/importance rerank), not pure top-k. Optional: `valid_from/valid_to/supersedes` on facts
  for temporal validity (lightweight alternative to a Zep/Graphiti knowledge graph).
- **Integrations:** off-the-shelf **MCP servers** (Gmail/Calendar/Drive/filesystem) via
  **`langchain-mcp-adapters`**. Gmail scope: read + `gmail.compose` (drafts only).
- **Scheduler / proactivity:** APScheduler background jobs invoking graph runs.
- **Doc/PDF:** `python-docx`, `reportlab` / markdown→PDF. **Mobile (later):** Expo / React Native.
- **Secrets:** `.env` (git-ignored) now; macOS Keychain + encrypted token store on the mini.

## Repository layout (to create)

Root: `~/personal-agent/`

```
personal-agent/
  pyproject.toml
  .env.example
  app/
    config.py            # endpoints, LOCAL_ONLY switch, whitelisted chat_id, paths
    main.py              # entrypoint: bot + scheduler
    agent/
      graph.py           # LangGraph graph, system prompt, tool nodes, checkpointer
      router.py          # deterministic origin-based sensitivity tag → local vs hosted endpoint
      confirm.py         # confirmation-gating for side-effectful tool calls (send/delete/share)
      tools/             # memory, reminder, builder tools; MCP tools loaded dynamically
    channels/
      base.py            # Channel interface
      telegram.py        # long-polling adapter + chat_id whitelist
    memory/
      db.py              # SQLModel engine (Postgres) + pgvector
      models.py          # ProfileFact, Goal, Task, Reminder, Purchase, Relationship, MessageLog
      store.py           # read/write + local-embedding semantic recall
    integrations/
      mcp_client.py      # langchain-mcp-adapters: Gmail/Calendar/Drive/fs servers
      google_auth.py     # OAuth + encrypted token storage
    proactive/
      scheduler.py       # APScheduler
      jobs.py            # ingestion, extraction, reminder-tick, briefings, quiet-hours + dedup
    builder/
      workspace.py       # isolated project dir; code runs in a container/restricted user, no secret access
      serve.py           # serve built web apps on localhost + tunnel link to view on phone
      docs.py            # PDF/docx generation
  data/                  # postgres data dir ref, oauth token (git-ignored)
  workspace/             # agent-built artifacts (git-ignored)
```

## Roadmap (full system)

### Part A — MVP (Phases 0–3)

**Phase 0 — Scaffolding & message loop.** Config with **whitelisted chat_id**; local Postgres +
pgvector; Ollama running a local 7–8B model; minimal LangGraph graph + Postgres checkpointer;
Telegram long-polling. *Milestone:* chat from your phone; state survives restart.

**Phase 1 — Memory core (LangMem).** Postgres schema (facts w/ provenance+confidence, goals,
tasks, reminders, purchases, episodic events, message log); **local** embedding pipeline; **hybrid
retrieval** (vector + keyword + recency/importance rerank); core blocks + working-buffer
summarization; tools `remember_fact`/`recall`/`add_goal`/`add_task`. **Start the eval fixture set
here** (recall accuracy, no-network check). *Milestone:* remembers facts across restarts; evals green.

**Phase 2 — Google via MCP.** One-time OAuth (Calendar read/write, Gmail read + compose);
`mcp_client.py` connects Gmail/Calendar/Drive MCP servers; **email/calendar content is tagged
sensitive → routed to the local model.** *Milestone:* calendar Q&A + a Gmail draft appear, all local.

**Phase 3 — Proactivity MVP.** APScheduler jobs; **receipt→return-window flow** (tight Gmail
filters → **quarantined-reader** extraction into a strict validated schema → `Purchase`+`Reminder`;
deterministic vendor parsers as fallback); **reminder-tick** with quiet-hours, dedup, done/snooze.
Critical reminders are **mirrored into a real Google Calendar event** so they survive agent
downtime. *Milestone:* seeded purchase → exactly one unprompted "return X by <date>" nudge.

### Part B — Core capabilities (Phases 4–6)

**Phase 4 — Building: web + docs.** Isolated `workspace/` (code runs in a container/restricted
user, no secret access); agent scaffolds/writes web apps and serves them on localhost + a tunnel
so you can view on your phone; PDF/docx/plan generation. Generic coding is *non-sensitive* → may
use opt-in hosted for quality. *Milestone:* "build a landing page" → runnable, viewable on phone; PDF plan renders.

**Phase 5 — Deep memory & life-model.** Richer schema (relationships, temporal validity);
**procedural memory** — the agent learns your preferences/workflows and appends to its own
heuristics; consolidation job that de-dupes/summarizes and reconciles conflicting facts by
confidence; a weekly **self-review**. *Milestone:* it recalls nuanced life context, adapts to your
preferences, and updates beliefs as things change.

**Phase 6 — Advanced proactivity.** Multi-source ingestion (calendar changes, flights/appointments,
Drive) → **relevance/urgency scoring** → a daily/needed **briefing**; goal check-ins ("you set X,
here's where it stands"). *Milestone:* proactive briefings that are useful, not noisy.

### Part C — Expansion (Phases 7–8)

**Phase 7 — Google Drive + broader integrations.** Drive read/organize via MCP (sensitive → local);
optional adapters (maps/notes) as MCP servers appear. *Milestone:* "find/summarize my doc about X."

**Phase 8 — Mobile app building.** Expo/React Native toolchain in the sandbox; scaffold → build →
preview via Expo Go. *Milestone:* "build a simple mobile app" → runs on your phone via Expo.

### Part D — Home base & scale (Phases 9–10)

**Phase 9 — Mac mini: fully local + always-on + iMessage.** Move to the mini; Ollama runs a
32B-class model → **hosted becomes optional/off** (privacy end-state); BlueBubbles **iMessage**
adapter; launchd service; Postgres as a service; scheduled `pg_dump` backups. *Milestone:* 24/7,
fully local, reachable over iMessage.

**Phase 10 — Reliability & observability.** Structured logging + traces (LangSmith-compatible or
local), token/cost budget guardrails, health checks + auto-restart, encrypted off-device backups,
per-tool error handling. *Milestone:* it runs unattended for weeks and you can debug when it doesn't.

## Verification (per phase)
1. **P0:** run `python -m app.main`, message from phone, get a local-model reply; non-whitelisted chat ignored.
2. **P1:** tell it a fact, restart, ask it back; confirm embeddings were computed locally (no network).
3. **P2:** OAuth once; ask tomorrow's calendar; request a draft → appears in Gmail unsent; verify email content hit only the local model.
4. **P3:** seed a purchase with a near return date → exactly one proactive reminder; quiet hours respected.
5. **P4:** build a web page + a PDF; open artifacts from `workspace/`.
6. **P5–P10:** life-model recall test; a useful briefing; a Drive lookup; an Expo app on device; kill/restart survives; backup restores.

## SOTA alignment & deliberate non-goals (personal scale)

- **Already SOTA-aligned:** LangGraph durable checkpointer, MCP tools, human-in-the-loop
  interrupts, least-privilege policy enforced *outside* the model (CaMeL philosophy), start-simple MVP.
- **Adopted from SOTA:** three-scope memory via LangMem, fact provenance/confidence, hybrid
  retrieval, core-block context budgeting, dual-LLM/quarantined reader, early eval fixtures.
- **Deliberately skipped as enterprise overkill for one user:** multi-agent crews / A2A protocols;
  distributed/sharded vector DBs (pgvector suffices); MLOps (model registry, model CI/CD, canary,
  k8s, autoscaling); RBAC/multi-tenant/SSO; heavyweight guardrail platforms; data warehouse/analytics.

## Risks / things to watch
- **Prompt injection via ingested email/web/Drive** — untrusted content could try to hijack tools.
  Mitigate: content is data-only, side-effectful actions are confirmation-gated, tool scopes minimal.
- **Code-execution safety** — agent-generated code runs isolated (container/restricted user) with
  no access to `data/` secrets or OAuth tokens.
- **16GB memory pressure on the Air** — macOS + 8B model + Postgres + app has little headroom;
  see the open decision below on the Air-phase private-data strategy. Benchmark Qwen-class early.
- **Local inference latency** — an 8B model on M2 is ~10–20 tok/s; interactive replies may feel
  slow. Proactive/background jobs are unaffected. Resolved by the Mac mini.
- **Receipt extraction** is the most model-dependent MVP step — tight filters + strict schema +
  deterministic vendor parsers; add repeatable eval fixtures.
- **Proactivity noise** — quiet hours + dedup + urgency scoring + snooze/done.
- **iMessage/BlueBubbles fragility** — against Apple ToS; the Channel adapter contains the risk.
- **Real cost is capex, not tokens** — the "open-source/local" route's true cost is the Mac mini
  (~$1,400 for M4 Pro/64GB); hosted non-sensitive calls are pennies.
- **Secrets** — OAuth token + keys git-ignored and encrypted; Keychain on the mini.
