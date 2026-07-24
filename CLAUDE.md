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
- `docs/04-constitution.md` — the auditable rule list (soft `[prompt]` vs hard `[code]` tiers).
- `docs/05-phase1-build.md` — memory core: schema, embeddings, hybrid recall, tool-calling loop.
- `docs/06-phase2-build.md` — Google (direct API): OAuth, calendar/gmail tools, the approval gate.
- `docs/07-phase3a-build.md` — proactive reminder engine: scheduler, add/list/cancel, calendar mirror.
- `docs/08-phase3b-build.md` — quarantined reader (dual-LLM) + general email-signal pipeline.
- `docs/09-phase4a-build.md` — sensitivity router + de-identified hosted delegation.
- `docs/10-phase4b-build.md` — the builder: sandboxed web-app + document generation.
- `docs/11-phase6-build.md` — daily briefing (deterministic morning digest) + testing hardening.
- `docs/12-read-email-build.md` — read a specific email on demand (quarantined summarizer + `read_email`).
- `docs/13-web-search-build.md` — web search (scrubbed + approved + audited; pluggable Tavily/DuckDuckGo).
- `docs/14-future-work.md` — the self-contained future-work list (problem → why → effort).

## How to work here (Stephanie's standing guidance)

Explicit, always-on expectations for any AI session in this repo — read this each session and hold to it:
- **Validate every claim — never assert what you haven't checked.** Run it, measure it, show the output.
  "It works" / "it's done" requires proof: a passing test, a green verify run, a real round-trip.
- **Test everything.** Add/extend `tests/` + the phase `scripts/verify_*.py`, driving the *real* code and
  model (not just plumbing). **After ANY persona/tool/graph change, re-run the tool-firing verifies
  (`verify_phase1/2/3` + `verify_dynamic_tools` + `verify_scenarios`) BEFORE claiming success — they
  regress silently.** `verify_scenarios` is the behavioral gate (right tool fires + no JSON dump). Never
  report a model metric from a single run (re-run to rule out variance; the 7B is stochastic).
  `scripts/verify_all.sh` runs the whole regression **sequentially** (Ollama serializes — parallel runs
  give misleading results). To attribute a suspected regression, use **`scripts/verify_firing.py
  --baseline <tools>`** — it stashes your changes, measures HEAD in a fresh process, restores, and
  prints a HEAD-vs-working firing diff (the automated version of the manual bisection).
  **Never gate on a single model sample** — use `_verify_lib.sample_check(name, probe, samples=, need=)`,
  which retries only when needed (early-exits, so a healthy check still costs one call) and always
  prints `hits/attempts` so a scrape-by is visible. Pick `need` by meaning: a *capability* check
  ("can it do this at all") uses `need=1`; a **must-not** check ("this must never happen") uses
  **`need=samples`** — retrying a must-not until it passes launders the violation, which is the one
  way this helper can be used to make the gate lie.
  **Test that tools EXECUTE, not just that they fire.** Every `verify_*` script breaks before the
  tool node runs (deliberately — so measuring tool *choice* never creates a draft or hits the
  network), which means a tool can be selected perfectly and raise on every call. `cancel_reminder`
  shipped that way. `tests/test_tools_execute.py` invokes each DB-backed tool for real; a new tool
  must be given test args or declared external. **And test multi-turn**: real conversations answer
  "yes", and every behavioural check used to be single-turn, which is how a broken confirmation path
  reached her. **Never assert an ORM attribute after its session closes** — `commit()` expires the
  instance, so the write lands and the confirmation crashes; `scripts/audit_session_scope.py` gates
  this and mocked-session unit tests structurally cannot.
- **Definition of done:** offline `pytest` + relevant `verify_*` green, no regressions, docs/CLAUDE.md
  updated — *then* it's done, not before.
- **Problem-solve through obstacles.** When something blocks (e.g. the 7B tool-count wall), diagnose the
  root cause and engineer a real fix — don't just route around it or declare it a blocker.
- **Research genuine knowledge gaps**, and prefer an on-machine empirical check over an assumption.
- **Plan first for non-trivial work**; surface design-influencing questions; recommend, don't option-dump.
  Ask sparingly (she gets click-fatigue) — proceed with a sensible default when you can.

(Prompt-tier guidance — a strong default the model usually follows, not a hard guarantee. When something
*must* hold, it belongs in code, per the two-tier model below.)

## Current status

**Memory, made real: seeded + an always-on profile card.** Prod memory was 3 facts from 68 user
messages. Diagnosis (replaying her real history through the live extractor): the extractor is precise
but only ~7% of messages carry an explicit self-fact, so organic capture is inherently slow. Fixed by
seeding from an agent that already knows her: `docs/profile-extraction-prompt.md` (a copy-paste prompt
that exports a JSON profile) + `scripts/import_profile.py` (stores each fact `provenance="imported"`,
deduped via the real `store.recall` at `fact_dedup_similarity`; goals dedup semantically at 0.90, not
word-overlap; dry-run by default, `--commit` writes; all local, input in git-ignored `data/`). Imported
90 facts + 9 goals. Then, because facts only reached the model when it chose to call `recall`, added an
**always-on profile card**: a curated subset of pinned facts (`Fact.pinned`, `store.pinned_facts`,
`app/agent/profile.py`) injected into the stable system prefix every turn (lazily built + process-cached
so Ollama's prefix KV-cache survives). The importer pins categories `{communication, dislikes}` minus a
`PIN_EXCLUDE` set. That exclusion was **measured, not guessed**: pinning "ask follow-up questions when
writing in her voice" collapsed `create_draft` firing 7/12 to 1/12 (an always-on "ask, don't assume"
fights the drafting tools), so action-directing / code-enforced / non-behavioral facts are excluded and
stay in recall. Final card is 14 pure-style rules (~630 tokens). Gated: `verify_scenarios` 16/16 (no
JSON-dump), core tools (add_reminder/web_search/build_web_app) unregressed, `create_draft` improved on
concrete prompts. Known soft-tier limit: create_draft is less reliable on very vague draft phrasings
("just say hello") with the card, a graceful prose miss (not a JSON dump). Also enabled **FileVault**
(2026-07-23) since the seed includes meds/comp/financials. See `docs/14-future-work.md` #1.

**Email signal scanner — re-enabled in SHADOW mode (the flagship, cautiously).** The proactive
return/bill/appointment scanner (Phase 3B) was off since it was too noisy. Re-enabling is now safe
because retire-task lets the detector skip topics she's marked done. Replaced the on/off
`signal_scanning_enabled` with a 3-state **`signal_mode`** (off / shadow / live, `app/config.py`,
opt-in via `SIGNAL_MODE` in `.env`; public default off). **shadow** = scan real mail every ~6h and
**log** each detection (`SHADOW-SIGNAL …` → `scripts/review_signals.sh`) but store nothing and never
message her — deliberately log-only so a shadow detection can't suppress a later real ask via the
14-day dedup, and going live has no backlog. **live** = the existing approve/reject ask. The scanner also **skips any signal already on her
calendar** (`signal_skip_calendared`, `_already_on_calendar` + `text_match`) — the calendar event
already reminds her, and a Mochi reminder would mirror back a duplicate. `jobs` branches on the
mode; `ingest_signals(shadow=…)` is the seam. Her instance is running shadow now; the
live flip is a one-line `.env` change after a few days of precision review. See `docs/14-future-work.md` #3.

**Let a task be retired — the staleness root-fix.** Her transcript's loudest pain was Mochi nagging
about things she'd already done ("I ALREADY GOT REJECTED FROM PERPLEXITY NO NEED TO KEEP REMINDING").
Root cause: reminders were modeled as *instances* with per-row statuses; nothing recorded that the
*underlying topic* was over, so cancelling one never stopped the next email/re-add (classic alert
fatigue — the topic-level "mute" mature alerting always has). New `RetiredTopic` tombstone
(`app/memory/models.py`) consulted at **both** creation seams: `reminders.retire_topic` records it,
cancels matching pending reminders, dismisses matching pending signals, marks matching tasks done (all
via `text_match.same_thing`); `create_or_get_reminder` raises `RetiredTopicError`; the email detector
skips retired topics — which is why re-enabling the scanner depended on this. A `retire_task` tool
(keyword+regex-boosted so it routes even with embeddings down) + a one-line persona nudge. Verified:
real-DB tests per seam (fail-on-old-code), full gate ALL GREEN, HEAD-vs-working bisection clean.
**Also reclassified the gate's sample-checks** (Stephanie: personality may evolve, don't gate voice):
capability checks firmed to `need=2 of 3` after measuring each at 8/8; the greeting-length check is now
informational. See `docs/14-future-work.md` (Resolved).

**Interaction — buttons for any yes/no or pick-one decision.** Mochi can now put a decision to
Stephanie as **tappable inline buttons** instead of a prose question she has to type "yes" at (which
she asked for ~5×, and which was the path that broke — a typed "yes" carries no routing signal). The
approval spine is generalized from approve/reject to arbitrary options: `confirm.ask_choice(question,
options)` + a `{"type":"choice"}` interrupt payload, the channel renders one button per option
(`callback_data="ans:<idx>"`) with a tap toast and a message-edit to the resolved state, and
`_on_callback` resumes with the tapped index. **Two tiers:** *deterministic* — `cancel_reminder` with
>1 match shows a picker and cancels exactly the tapped one (proven through the real graph, never
depends on the 7B); *best-effort* — a general `ask_user(question, options)` tool (always bound, in
`CORE`) the model calls instead of writing a discrete-choice question, measured ~0/2 firing in free
conversation so it's a **soft-tier** capability, not a guarantee. The separate calendar-permission
complaint ("you don't have to ask permission for reading calendar events") is fixed by a persona edit:
never ask permission to *read* calendar/inbox/memory. See `docs/14-future-work.md` (Resolved).

**Phase 8.1 — the context fix (a root-cause win, and a correction to earlier conclusions).**
Measured that a turn's prompt is **~3,800–4,050 tokens** while Ollama's default `num_ctx` is **4096**
— and `num_ctx` covers *prompt + generation*, so only ~75 tokens remained to reply, forcing
context-shifting that evicted the persona mid-generation. Fixed with a derived 8k model
(`ollama/Modelfile.qwen2.5-7b-8k` + `LOCAL_MODEL=qwen2.5:7b-8k`; the OpenAI endpoint ignores
per-request `num_ctx`, and `OLLAMA_CONTEXT_LENGTH` is global). Every previously "known-hard" prompt
went **0/23 → 18/18** (e.g. "ping me in 2 hours to stretch" 0/8→6/6; "read me the email from Chase"
0/3→4/4). Cost +0.3GB resident, zero swap. **This corrects several earlier misdiagnoses** ("the 7B
derails on imperatives", "create_draft is tool-count-diluted") and explains *why* net-additive persona
edits tanked firing — they ate the last of the generation headroom. Prompt token counts are identical
at 4096 and 8192, so the prompt always *fit*; the damage was during generation. **The "tool-count
wall" was the same root cause** — tested: each bound tool costs ~95 prompt tokens, so at 4096 the
prompt crossed the window at ~11 tools; on the 8k model **all 17 bind and fire 3/3** (prompt 4,998).
Adding tools is therefore no longer dangerous (~95 tokens each, ~3,200 headroom). Per-turn tool
selection is kept as an *optimization* (saves ~665 tok/turn), not a workaround. **Correction
(2026-07-21): the "routing picked the right tool 15/15" figure was measured only on single-turn
prompts, and that blind spot broke a real conversation** — selection read just the newest message,
so a bare "yes" bound nothing relevant and the model pasted a JSON tool call into the chat instead
of calling it. Selection now reads the last `TOOL_SELECT_TURNS` (3) user turns: follow-ups went
1/5 → 5/5 with single-turn unchanged. See `docs/14-future-work.md`.

**Phase 8 — web search (scrubbed + approved + audited).** Mochi can now **look things up online**
(weather, prices, hours, "is X open", news). New `web_search` tool (`app/agent/tools/web_tools.py`)
reuses the `consult_expert` privacy spine: `sanitize.redact` scrubs the query, `is_too_personal`
refuses PII-dense ones, **`require_approval("web_search", …)` gates it** (Stephanie approves the
scrubbed query before it leaves — the "ask permission when doing stuff" she asked for), `rate_limit`
caps it, a `WebSearch` audit row logs it (`/sent`), and results are `frame_untrusted` + synthesized
**locally**. Provider is pluggable (`app/integrations/web_search.py`): **Tavily** (default, free key)
or **DuckDuckGo** (no key, `ddgs`) — switching is one config value (`WEB_SEARCH_PROVIDER`).
Deliberately **independent of `LOCAL_ONLY`** (scoped decision, `docs/04-constitution.md`): only a
scrubbed generic query leaves. The Telegram approval renderer is now per-action (`_render_proposal`)
— the seed of a future generalizable approval layer. **No persona edit** (no false "can't search"
claim existed) — `web_search` fires from its description + keywords, confirmed by a HEAD-vs-mine
tool-firing bisection (add_reminder/create_draft unregressed). See `docs/13-web-search-build.md`.

**Phase 7 — read a specific email on demand.** Mochi can now read what a *specific* email **says**,
on demand ("what did the landlord's email say?" → a safe summary), reusing the Phase 3B dual-LLM
boundary. New `read_email` tool (`app/agent/tools/google_tools.py`) → `app/agent/email_read.py`
orchestrator → a new **quarantined summarizer** (`quarantine.summarize_email` → `EmailSummary`, same
tool-free/persona-free local `reader_llm`); the tool returns **only** the validated, length-capped
summary — the privileged agent never ingests the raw body (rule #4 holds). No new OAuth scope
(`gmail.readonly` already reads bodies), no approval gate (read-only). Fires reliably on the question
forms ("what did/does X's email say", "summarize the … email"); the bare imperative "read me the email
from X about Y" derails the 7B (documented soft-tier limit). **Persona lesson, measured again and
sharper:** the read_email tool fires 4/4 on its *tool description alone* — a first, net-**additive**
persona edit (routing clause + worked example) silently dropped `add_reminder`/`create_draft` on
unrelated prompts 4/4→0/4; the fix was a **net-neutral, correctness-only** persona edit (verified by
HEAD-vs-mine 4-sample bisection). See `docs/12-read-email-build.md`.

**Phase 6 — the daily briefing (+ testing hardening).** Mochi now sends a **daily morning briefing**:
one *deterministic* digest (no LLM → it can't dump JSON or wander) of today's calendar, reminders due
today, and active goals/tasks — pushed once each morning via `run_daily` at `briefing_hour` (8am, after
quiet hours) and on demand via **`/briefing`**. Gated by the `/pause` kill-switch. Email is deliberately
excluded (the Phase 3B scanner is paused/noisy). Assembled in `app/proactive/briefing.py`. This phase also
**hardened testing** after a run of bugs that reached Stephanie before any test caught them: the unit
suite mocks the model+Google, so behavioral regressions were invisible until live. New:
`scripts/verify_scenarios.py` (real-model — right tool fires + **no JSON dump** + on-topic),
`tests/test_regressions.py` (cross-cutting integration seams: full reminder lifecycle + parser→briefing),
`tests/test_briefing.py`, and the previously-untested require-due-date filter. See `docs/11-phase6-build.md`.

**Phase 4B — the builder (step 1 shipped).** Mochi can now **build things**: `app/builder/` scaffolds/
writes web apps and generates **PDFs/Word docs**, runs them in a **sandbox** (`SubprocessSandbox`:
scrubbed env — no secrets in the child — cwd-jailed to `workspace/`, timeout, best-effort `sandbox-exec`
deny of `data/`+`.env`; a `DockerSandbox` is a later drop-in), and **serves static sites on the LAN** so
Stephanie opens them on her phone. Heavy code-gen routes to the hosted **gpt-oss-120b** (via the 4A
router, scrubbed + audited). **Works conversationally** ("build me a bakery page") — solved a measured
**tool-count wall** (binding all ~15 tools collapses the 7B; 11 fire, 13–15 → 0) with **dynamic per-turn
tool binding** (`app/agent/tool_select.py`): each turn binds only a small relevant subset (memory core +
keyword + embedding-nearest, ≤10) selected from the message; `ToolNode` keeps all tools for execution.
End-to-end: `build_web_app` 3/3, `make_document` 2/2, `add_reminder` 3/3, `create_draft` 3/3 — the
builder works AND all other tools are preserved/improved. `make_document(description)` generates its own
content on the *local* model (personal stays local). `/build` + `/doc` remain as explicit shortcuts. **Also shipped this cycle — 4A.2 reliable fact capture:** a post-turn *local* extraction
sweep (`app/memory/extract.py`) that captures facts 5/5 where the flaky `remember_fact` tool got 1–2/5,
deduped + stored in the background. Step 1 verified offline (`tests/test_builder.py`) + real
(`scripts/verify_phase4b.py` 7/7: sandbox runs node/python, scrubs secrets, `sandbox-exec` denies `.env`,
real HTTP 200, real PDF, real Groq code-gen). Steps 2 (React/Vite dev serving) + 3 (cloudflared tunnel +
code-gen quality/retry) are next. See `docs/10-phase4b-build.md`.

**Phase 4A — sensitivity router + de-identified hosted delegation.** The project's #1 privacy
principle is now real code. `app/agent/router.py` deterministically picks local vs hosted **by data
origin** (tagged in code, never by an LLM): SENSITIVE → local always; NON_SENSITIVE → an opt-in
**free** hosted model only when enabled+configured and `LOCAL_ONLY` is off — else local (fails closed).
The main agent + quarantined reader are the SENSITIVE path (always local); `graph.py` now builds its
models through the router so that's enforced in one place. First live consumer + the capability
Stephanie asked for: a **de-identified hybrid** — the local agent asks a stronger model a *generic,
de-identified* question via the `consult_expert` tool (11 tools now), a **deterministic scrubber**
(`app/agent/sanitize.py`) hard-redacts known identifiers + PII before anything leaves, the hosted model
(no tools) answers, and the local agent re-personalizes. Every hosted call is **audited**
(`HostedConsult` → `/sent`), it **refuses PII-dense questions** (fails closed), and `/ask` is a
pure-generic path touching no memory/Google. This is a **deliberate, Stephanie-authorized scoped
modification** of the "personal data → local only" hard rule (see `docs/04-constitution.md`): raw
personal data never leaves (guaranteed); the local model's de-identification is best-effort (measured,
not assumed) and backstopped + audited. Off by default (`LOCAL_ONLY=true`). Verified offline (15 tests)
+ against the real 7B (`scripts/verify_phase4a.py`: scrub 100%, de-id 5/5, local `/ask` round-trip).
See `docs/09-phase4a-build.md`. **4A.1 UX:** `/ask` answers render as Telegram MarkdownV2 (tables as
aligned monospace; plain-text fallback), and follow-ups work by **swipe-replying** to an `/ask` answer
(or `/ask` while replying) — the quoted context is scrubbed + audited like everything hosted, and a
reply to a normal message still stays local.

**Phase 3B — safe email reading (quarantined reader + general signal pipeline).** Mochi now reads
untrusted email *bodies* — the project's most dangerous surface — via the **dual-LLM / quarantined
reader** (`app/agent/quarantine.py`): a *separate, tool-free, persona-free* local model that parses an
email into a validated, length-capped structured object; the privileged agent never sees the raw body,
which is never persisted or logged. It's a **general** pipeline (`app/proactive/email_signals.py`): a
~6h background job scans recent mail (dedup + go-forward-only first run), the reader extracts a typed
**actionable signal** (return / bill / appointment / deadline / delivery), and Mochi proactively **asks
first** (`sig:approve/reject` buttons) before `create_from_signal` makes a reminder (per-type lead-time
+ calendar mirror). A return is just one `signal_type` — the flagship return-window flow is now
automatic, but so are bills/appointments/etc. Cost is bounded by a per-scan cap on reader calls;
`signal_scanning_enabled` is a kill-switch. No new agent tool (reading stays off the tool list) and no
new OAuth scope. Verified offline (`tests/test_email_signals.py`, 20 tests) **and against the real 7B**
(`scripts/verify_phase3b.py`: 5/5 extraction across types, injection refused). See
`docs/08-phase3b-build.md`.

**Phase 3A — proactive reminder engine.** Builds on Phase 2. Mochi can now be *proactive* — the
channel pushes unprompted reminders, not just responses. 10 tools: memory + Google + reminders
(`add_reminder`/`list_reminders`/`cancel_reminder`). She sets one-off and recurring reminders from
natural language ("call mom every Sunday") — time parsing by `dateparser`, not the model. A JobQueue
tick (`app/proactive/`) fires due reminders to the whitelisted chat with Done/Snooze buttons, with
quiet-hours (9pm–8am), status-dedup, catch-up-without-spam, per-reminder error isolation, and a
`/pause` `/resume` kill-switch. The return-window flagship is one auto-created *kind* of reminder
(seedable now; Gmail auto-extraction via the quarantined reader is 3B). Timed reminders mirror into
Google Calendar events (added `calendar.events` write scope). See `docs/07-phase3a-build.md`.

**Phase 2 — Google (Calendar + Gmail) & the approval gate.** Direct `google-api-python-client`
integration (not MCP — see `docs/06-phase2-build.md`), least-privilege scopes (`gmail.readonly` +
`gmail.compose`; calendar upgraded to `calendar.events` write in 3A for reminder mirroring). The
human-in-the-loop approval gate (`app/agent/confirm.py` → `interrupt()` → Telegram Approve/Reject)
pauses external writes (draft creation). Email is metadata-only — the privileged agent never ingests
raw bodies (quarantined reader is Phase 3B). Live status narration + token streaming + keep-warm +
prompt-cache latency fix. Everything local (`LOCAL_ONLY`).

**Phase 1 — memory core.** Durable Postgres memory (`Fact`/`Goal`/`Task`/`Reminder`/`Event`/
`MessageLog`), local `nomic-embed-text` embeddings + hybrid recall (pgvector + keyword + rerank),
the tool-calling loop, and context-window management (rolling summary + trimming). Documented
tool-invocation-reliability tradeoff (~80%, not 100%, on the local 7B — tracked by the verify
scripts). See `docs/05-phase1-build.md`.

## Non-negotiable safety rules (these outlive any single task)

These are the reason the project exists as *local-first with a human in charge*. Do not
weaken them without Stephanie's explicit say-so.

1. **Privacy — data origin decides the model, deterministically.** Anything sourced from
   Gmail/Calendar/Drive/memory or Stephanie's personal data is **sensitive → local model +
   local embeddings only**. The deterministic router (`app/agent/router.py`, Phase 4A) enforces this:
   SENSITIVE → local always; hosted only for opt-in non-sensitive work; it **fails closed**
   (unknown/misconfigured → local). `LOCAL_ONLY=true` forces everything local; it is the current
   default. **Embeddings are always local.** **Scoped (P4A, with Stephanie's explicit say-so):** the
   `consult_expert`/`/ask` de-identified hybrid may send *de-identified, deterministically-PII-scrubbed,
   audited* derivatives to an opt-in free hosted model — **raw** personal data still never leaves. The
   scrubber + fail-closed + audit log are the hard part; the local model's de-identification is
   best-effort (measured). See `docs/04-constitution.md`'s scoped-modification note.
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

**Two-tier rule model.** These invariants are the **hard tier** — enforced in deterministic code
outside the model, so they hold even if the model is fooled or prompt-injected. Separately, the
**soft tier** is Mochi's personality/voice + behavioral defaults, which live in
`app/agent/persona.md` (privileged agent only — never the quarantined reader) and are only
*prompt*-enforced (the local model usually follows them but can drift). The canonical, auditable
list of every rule — tagged `[prompt]`/`[code]`, with where it's enforced and its status — is
[`docs/04-constitution.md`](./docs/04-constitution.md). When something *must* hold, it belongs in
the hard tier, not the persona.

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
- **Integrations:** **direct API clients**, not MCP. The original plan called for off-the-shelf MCP
  servers via `langchain-mcp-adapters`; Phase 2 went direct with `google-api-python-client` instead
  (fewer moving parts, least-privilege scopes we control — reasoned in `docs/06-phase2-build.md`).
  There is no MCP code in `app/`. Adopting an MCP *client* later is future work, not current fact.

## Repo layout

```
personal-agent/
  pyproject.toml        # deps + ruff/pytest config; an application (tool.uv package = false)
  .env.example          # template — copy to .env, fill in, never commit .env
  app/
    config.py           # pydantic-settings; LOCAL_ONLY switch; whitelisted chat_id
    main.py             # entrypoint (python -m app.main) + the single-instance flock
    agent/              # graph, persona, tools/, router, quarantine, sanitize, tool_select
    channels/           # base (Channel + ChannelContract), render, telegram{,_stream,
                        #   _commands,_buttons}
    integrations/       # google_auth / google_calendar / google_gmail / web_search
    memory/             # models, db, store, embeddings, extract
    proactive/          # reminders (+ reminder_time pure parsing, reminder_calendar mirroring),
                        #   jobs, email_signals, briefing, text_match
    builder/            # sandbox, codegen, docs, serve, workspace
  data/ workspace/      # local state / tokens / built artifacts (git-ignored)
  docs/ scripts/ tests/ launchd/ ollama/ .github/
```

Every one of these exists — Phases 0–8 are shipped. `README.md` carries the same tree with a
one-line gloss per directory; if you're changing structure, update both. `00-plan.md` has the
longer-term target.

## Running it

Prereqs (see `03-phase0-build.md` for full setup): Ollama running with `qwen2.5:7b` pulled,
Postgres.app with a `personal_agent` DB (+ `vector` extension), and a `.env` filled from
`.env.example` (bot token + your chat_id).

**Required: build the 8k-context model.** Ollama's default `num_ctx` is 4096, but a turn's prompt is
already ~4,000 tokens — leaving ~75 tokens to generate, which forces context-shifting and silently
breaks tool-calling (measured: several prompts 0/4 → 4/4 from this alone).
```bash
ollama create qwen2.5:7b-8k -f ollama/Modelfile.qwen2.5-7b-8k   # re-run after re-pulling the base
```
then `LOCAL_MODEL=qwen2.5:7b-8k` in `.env`. Measurements: `docs/14-future-work.md`.

```bash
cd ~/personal-agent
uv run python -m app.main        # or: source .venv/bin/activate && python -m app.main
```

**Supervised (recommended — this is how it runs day to day).** `launchd` starts Mochi at login and
restarts it if it exits; `scripts/preflight.sh` repairs Postgres/Ollama first (including a stale
`postmaster.pid` after an unclean shutdown) and builds the 8k model if missing:
```bash
cp launchd/com.mochi.agent.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.mochi.agent.plist
launchctl print gui/$(id -u)/com.mochi.agent | head   # status
tail -f data/mochi.log                                # logs
launchctl bootout gui/$(id -u)/com.mochi.agent        # stop/uninstall
```
Don't run the manual command *and* the agent at once — two pollers on one bot token conflict.

**Backups (the memory DB — everything the project is *for*).** A daily rotated `pg_dump -Fc` of
Postgres, supervised by its own launchd job. **Local only** by decision: covers a bad migration /
accidental delete / corruption / app bug (restorable in seconds into a scratch DB); disk failure is an
accepted risk, left to the Mac's whole-disk/Time Machine backup. A backup you can't restore is a
guess, so `restore_check.sh` restores the newest dump into a throwaway DB and asserts row-count parity
with prod — **run it after setup**.
```bash
cp launchd/com.mochi.backup.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.mochi.backup.plist  # daily 03:30
launchctl kickstart gui/$(id -u)/com.mochi.backup     # run one now (don't wait for 03:30)
./scripts/restore_check.sh                            # PROVE the newest dump restores (row-count parity)
tail -f data/backup.log                               # logs; dumps land in backups/ (git-ignored, keep last 14)
```
*Note:* FileVault is **on** as of 2026-07-23, so PGDATA, `.env`, and these dumps are encrypted at
rest (recovery key stored in her password manager). This was enabled after seeding memory with a
sensitive personal profile (meds, comp, financial accounts); it protects the whole disk, independent
of backups.

## Conventions

- Keep each phase lean: no speculative abstractions, no half-finished later-phase code.
- All config flows through `app/config.py` — don't read env vars directly elsewhere.
- Prefer editing existing files; keep the `Channel` seam and the safety layers intact.
- When you finish a phase, update the "Current status" section above.
- Every phase's build doc must include a standalone script/test that verifies its milestone by
  driving the real code directly (e.g. `build_agent()`, not through Telegram) against a scratch
  database — so correctness can be checked without Stephanie's live participation. Manual/live
  checks (Telegram, OAuth flows, etc.) confirm the human-facing transport and experience, not
  correctness that could have been checked automatically. (Established in Phase 1 after shipping a
  bug — the model claiming to remember facts without calling the tool — that direct testing found
  in minutes and manual testing had missed; see `scripts/verify_phase1.py`.)
