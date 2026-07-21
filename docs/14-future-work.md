# Future work — the consolidated, self-contained list

The single place that gathers what's next and *why*. The **top 10** are ranked by value × feasibility
under the real constraints ($0 budget, 16GB M2 Air, solo project, daily personal use) and each has a
**Goal / Plan / Benefit**. Smaller items are tracked below, and resolved work is kept at the bottom
because the lessons still matter.

The detailed phase plan is [`00-plan.md`](./00-plan.md); shipped status is in [`CLAUDE.md`](../CLAUDE.md).

> **How to pick up an item:** anything touching the persona/tools/graph must be validated with
> `scripts/verify_firing.py --baseline <tools>` (HEAD-vs-working firing diff) before shipping — the
> 7B regresses silently. See CLAUDE.md's testing guidance.

| # | Item | One-line why |
|---|------|--------------|
| [1](#1-automated-backups-of-the-memory-db) | Automated backups of the memory DB | Everything the project is *for* lives in one un-backed-up Postgres, on a machine that already crashed. |
| [2](#2-make-long-term-memory-actually-accumulate) | Make long-term memory actually accumulate | **1 fact from 89 messages** — the core premise isn't working. |
| [3](#3-re-enable-the-email-signal-scanner-the-flagship) | Re-enable the email signal scanner | The flagship proactive feature is switched **off**. |
| [4](#4-voice-messages-local-transcription) | Voice messages (local transcription) | Biggest daily UX win on a phone. |
| [5](#5-try-a-newer-same-size-local-model) | Try a newer same-size local model | Possible Mac-mini-class gain for **$0**. |
| [6](#6-liveness-heartbeat--status) | Liveness heartbeat + `/status` | Silence currently looks identical to "nothing to say". |
| [7](#7-deeper-memory--preferences-phase-5) | Deeper memory & preferences | What makes it *hers* rather than generic. |
| [8](#8-generalizable-per-action-approval-layer) | Generalizable approval layer | Requested; consistency is what makes "ask first" trustworthy. |
| [9](#9-shared-verify-library--split-telegrampy) | Shared verify lib + split `telegram.py` | Measured duplication; pays back every future phase. |
| [10](#10-google-drive-read-quarantined) | Google Drive (read, quarantined) | The last big personal data source. |

---

## 1. Automated backups of the memory DB
**Goal:** never lose the accumulated memory (facts, reminders, goals, message history, checkpoints).
**Plan:** a `scripts/backup.sh` running `pg_dump` of `personal_agent` to a timestamped, gzipped file
under a git-ignored `backups/` dir; keep the last N (rotate); schedule daily via a launchd agent
(mirroring `launchd/com.mochi.agent.plist`); verify by actually restoring into a scratch DB once —
an unverified backup isn't a backup. Optionally copy off-machine (iCloud/external) since a disk
failure takes both otherwise.
**Benefit:** the highest risk-to-effort ratio on this list. There are already 34 reminders and the
memory DB in a single Postgres with **zero** backups, on a machine that crashed once this week. A
disk failure, a bad migration, or a `DROP` in the wrong terminal erases the entire point of the project.
**Effort:** small (~20 lines + a plist).

## 2. Make long-term memory actually accumulate
**Goal:** Mochi should genuinely know things about Stephanie's life over time.
**Plan:** diagnose first — the production DB has **1 `Fact` row against 89 logged messages**, so
either `remember_fact` isn't firing (measured ~40%, and `verify_phase1` treats it as informational)
or the post-turn extraction sweep (`app/memory/extract.py`) isn't capturing/persisting. Instrument
the sweep (how many candidates found vs stored vs deduped), replay recent `MessageLog` history
through it offline to measure real capture rate, then fix the weak link — likely making the sweep the
primary path and the tool a bonus. Re-check with the now-fixed 8k context, which may already help.
Add a `/facts` command so it's visible what she's remembered.
**Benefit:** this is the project's premise. Without it, Mochi is a capable chatbot with tools — the
"personal agent that holds long-term memory about her life" doesn't exist yet. It also compounds:
every other feature (briefing, replies, proactivity) gets better when memory is real.
**Effort:** medium (diagnosis-led).

## 3. Re-enable the email signal scanner (the flagship)
**Goal:** turn the headline capability back on — notice a purchase in email and remind her to return
it before the window closes (plus bills/appointments/deadlines/deliveries).
**Plan:** `signal_scanning_enabled` is `False` because early scans were noisy. It now has dedup
(`text_match.same_thing`), a require-due-date filter, per-scan reader caps, go-forward-only baselining,
and an approval ask before anything is created — plus the context fix that improved extraction. Turn
it on behind a short trial: run scans against recent mail with results logged but *not* pushed, review
precision by hand for a few days, tighten filters, then enable the proactive ask. Keep `/pause` as the
kill switch.
**Benefit:** this is the differentiator that motivated the whole project, currently dormant. It's also
the only feature that creates value with zero user effort.
**Effort:** medium (mostly tuning + judgement, not new code).

## 4. Voice messages (local transcription)
**Goal:** send Mochi a voice note and have it work like a typed message.
**Plan:** handle Telegram `voice`/`audio` updates in the channel, download the OGG, transcribe locally
with `whisper.cpp` or `faster-whisper` (small/base model is plenty for short notes), then feed the text
through the normal turn path. Keep it strictly local — audio is personal data, so it never leaves the
machine, consistent with rule #1.
**Benefit:** the biggest everyday UX upgrade on a phone. Capturing a reminder while walking is exactly
the moment typing loses — and it's the interaction pattern that makes an assistant habitual.
**Effort:** medium; $0 (local model).

## 5. Try a newer same-size local model
**Goal:** better tool-calling and coherence without new hardware.
**Plan:** the 2026-07 finding was that *context*, not model capability, was the real bottleneck — so
re-evaluate the model choice on a level playing field. Build 8k-context variants of one or two modern
7–8B candidates (e.g. a newer Qwen or Llama), A/B them with `scripts/verify_firing.py` plus
`verify_scenarios.py`, and compare firing rates, coherence, and tokens/sec at equal context. Adopt only
on measured improvement; `LOCAL_MODEL` makes it a one-line switch.
**Benefit:** potentially a large quality gain for **$0**, and it directly tests whether the ~$1.4k Mac
mini is still needed. Cheap to run now that the harness exists.
**Effort:** small (a few hours of measurement).

## 6. Liveness heartbeat + `/status`
**Goal:** never be silently down again.
**Plan:** launchd `KeepAlive` restarts a process that *exits*, but not one that is wedged (hung poll,
dead DB connection, Ollama unloaded). Add a lightweight self-check job that verifies the essentials
(Telegram polling alive, Postgres reachable, Ollama responding, last tick recent) and, on failure,
either self-heals or pings the chat; plus a `/status` command reporting uptime, model, last briefing,
pending reminders, and dependency health.
**Benefit:** an assistant you rely on for reminders is worse than useless if it's quietly dead —
you only find out by missing something. This closes the gap supervision doesn't cover.
**Effort:** small.

## 7. Deeper memory & preferences (Phase 5)
**Goal:** move from isolated facts to a usable profile — people, preferences, routines, ongoing situations.
**Plan:** build on #2 (there's no point structuring memory that isn't being captured). Add lightweight
typed structure (person / preference / routine / project) with confidence + recency, surface it in the
system prompt as a compact profile block rather than raw recall hits, and feed it into the briefing and
replies. Watch the prompt budget — the persona is already ~3,600 tokens of the 8k window.
**Benefit:** the difference between a generic assistant and *hers*. It's what makes replies feel like
they come from someone who knows her, and it compounds with every other capability.
**Effort:** medium.

## 8. Generalizable per-action approval layer
**Goal:** one consistent, configurable answer to "which actions require my approval?".
**Plan:** today the gate is applied ad-hoc — `create_draft` and `web_search` call `require_approval`
directly and `telegram._render_proposal` switches per action. Promote it to a declared policy: a
config map of action → {always ask, ask once then remember, never} with a registry of renderers, so
adding a gated action is a table entry rather than bespoke code. Extend coverage to currently-ungated
external writes (e.g. calendar-event mirroring). Keep the hard-tier guarantees in code.
**Benefit:** Stephanie explicitly asked for "ask permission when doing stuff." Predictability is what
makes the permission model trustworthy — ad-hoc gating means she can't reason about what will and
won't act on its own.
**Effort:** medium.

## 9. Shared verify library + split `telegram.py`
**Goal:** reduce the maintenance drag before it compounds.
**Plan:** (a) extract `scripts/_verify_lib.py` with `check()`, `fires()`, `rate()`, the scratch-DB
guard, and env bootstrap — measured duplication: `check()` reimplemented in **9** scripts, `fires()` in
**4**, guard in **7**, env placeholders in **10**, across 1,289 lines. (b) split `telegram.py`
(**705 LOC, 62% coverage** — transport + ~10 commands + buttons + rendering + approval + audit) into
`channels/telegram/{app,commands,streaming,render}.py`. Do them separately, tests green between.
**Benefit:** every future phase touches both files; consistent verify output makes regressions easier
to read, and splitting the god-module makes the untested 38% reachable.
**Effort:** small (a); medium (b).

## 10. Google Drive (read, quarantined)
**Goal:** let Mochi pull up receipts, docs, and spreadsheets on request.
**Plan:** mirror the Gmail pattern exactly — a least-privilege read-only scope, a `drive_search` /
`read_document` tool pair, bodies routed through the **quarantined reader** (never into the privileged
agent), results `frame_untrusted`-wrapped, no new write capability. Reuse `google_auth`; add keywords
to `tool_select`.
**Benefit:** the last major personal data source, and it makes the return-window/receipt flows much
stronger. Now unblocked: tool count is no longer a constraint (~95 prompt tokens per tool, ~3,200 of
headroom).
**Effort:** medium (new OAuth scope + integration).

### Honorable mention — Mac mini + a larger local model
Still the best raw quality lever (reliability, memory headroom, and it unlocks a self-hosted CI runner
for the real-model gate). Ranked below the ten because it's ~$1.4k against a $0 budget, and the 2026-07
context fix already delivered much of what it promised. Revisit after #5.

---

## Also tracked (smaller or later)

- **Email in the daily briefing** — once #3 is proven quiet. *Small.*
- **Deep-read a web result page** via the quarantined reader — closes the one framing-only injection
  residual (web snippets are soft-tier "data not instructions"). Pairs with fetching full pages. *Medium.*
- **More search providers** behind the existing seam — SearXNG (self-hosted → fully local query
  routing, the privacy ideal) and Brave; smarter ranking. Switching is one config value. *Small each.*
- **Alembic migrations** — `init_db` is `create_all` + hand-written `ALTER`s; `create_all` won't alter
  existing tables, so new columns are added manually. Fragile as the schema grows. *Medium, one-time.*
- **Checkpoint pruning** — `PostgresSaver` writes a row per turn and nothing prunes it. *Small.*
- **Docker sandbox** — `SubprocessSandbox` is best-effort (scrubbed env + cwd jail + best-effort
  `sandbox-exec`), not real isolation, and the builder runs generated code. *Medium.*
- **Secrets at rest** — `.env` holds the bot token + hosted key in plaintext; Keychain was always the
  plan for the mini. *Small-medium.*
- **Self-hosted CI runner** — run the full real-model `verify_all.sh` on the mini; model behavior isn't
  gated on GitHub today. *Medium.*
- **Coverage gate** — `pytest-cov` runs in CI; add a threshold once a baseline is trusted. *Small.*
- **Optional Ollama+nomic in CI** — to also run the one embedding-semantic test file. *Small.*

## Cleanup & tech debt (measured 2026-07-20, whole-repo pass)

The repo is in good shape overall: **no TODO/FIXME debt, no unused dependencies, ruff clean, 78%
coverage**. The real opportunities (item #9 covers the top two):

- **Verify-script boilerplate** — the biggest duplication; see #9.
- **`telegram.py` god-module** — 705 LOC at 62%; see #9.
- **Duplicated `_fmt_event`** in `app/agent/tools/google_tools.py` and `app/proactive/briefing.py` —
  near-identical (start parsing, all-day handling, location), differing only in date-vs-time format and
  bullet glyph → one helper with a `style` flag. *Small.*
- **Dead code** — `app/proactive/jobs.py:is_enabled()` is defined but never called (the module reads
  `_enabled` directly) → delete it or use it consistently. *Trivial.*
- **Coverage holes** — `app/warmup.py` **0%** (and it now carries real logic: keep-warm + tool-vector
  warming), `reminder_tools.py` **34%**, `memory_tools.py` **40%**, `builder/serve.py` **35%**. The tool
  wrappers are thin, but they're the layer the model actually calls. *Small each.*
- **Doc bloat / drift** — `docs/` is 3,876 lines (`05-phase1-build.md` alone is 1,312). More importantly,
  this session found **three confidently-written conclusions that were wrong**, all downstream of one
  unmeasured config → do a periodic "does this still match reality?" pass, and prefer linking
  measurements over restating them. *Small, recurring.*

---

## ✅ Resolved (kept — the lessons still apply)

### The context window was starving generation (2026-07-20)
Ollama's default `num_ctx` is **4096**, but a turn's prompt measured **~3,800–4,050 tokens**. `num_ctx`
covers **prompt + generation**, so only **~75 tokens** remained to produce a reply — forcing llama.cpp
**context-shifting mid-generation**, which evicts the front of the prompt: the persona's "call this tool
immediately" instructions. *Not* prompt-eval truncation — token counts are identical at 4096 and 8192,
so the prompt always fit; the damage happened while generating.

**Fix:** `ollama/Modelfile.qwen2.5-7b-8k` (`FROM qwen2.5:7b` + `PARAMETER num_ctx 8192`) +
`LOCAL_MODEL=qwen2.5:7b-8k`. Surgical by necessity — Ollama's OpenAI endpoint ignores per-request
`num_ctx`, and `OLLAMA_CONTEXT_LENGTH` is global (it would also inflate nomic-embed).

| prompt → tool | 4096 | 8192 |
|---|---|---|
| "ping me in 2 hours to stretch" → `add_reminder` | 0/8 | **6/6** |
| "draft an email to me saying hi" → `create_draft` | 0/4 | **4/4** |
| "draft a note to alex@example.com …" → `create_draft` | 0/8 | **4/4** |
| "read me the email from Chase …" → `read_email` | 0/3 | **4/4** |

Cost: +0.3 GB resident (4.6 → 4.9 GB), zero swap. **Corrected misdiagnoses:** "the 7B derails on the
imperative *read me the email*", "`create_draft` is tool-count-diluted" (which had me lower a verify
floor), and it explains **why net-additive persona edits tanked firing** — they ate the last of the
generation headroom.

**Measurement lesson:** the first firing comparison read 33/36 → 33/36 and looked like a null result,
because the prompt set was **saturated** (8 of 9 already 4/4). Gains only show on prompts that were
*failing* — always include known-failing canaries in a before/after.

### The "tool-count wall" was the same root cause
The documented wall (11 tools fire, 13–15 → ~0) was also context exhaustion: each bound tool costs
**~95 prompt tokens**, so at 4096 with a ~3,600-token base prompt, ~11 tools is exactly where the prompt
crossed the window.

| tools bound | prompt_tokens | `add_reminder` | `create_draft` |
|---|---|---|---|
| 10 | 4,333 | 3/3 | 3/3 |
| 13 | 4,644 | 3/3 | 3/3 |
| **17 (all)** | **4,998** | **3/3** | **3/3** |

**Consequence: adding tools is no longer dangerous** (~95 tokens each, ~3,200 headroom ≈ room for ~30
more). The ≤10 cap in `tool_select` was deliberately left alone — routing never excluded the right tool
(15/15) and its subsets are only 7–9 tools, so the cap isn't binding, while per-turn selection still
saves ~665 prompt tokens/turn. It's an optimization now, not a workaround.

### Process supervision + unclean-shutdown recovery
Both failures were observed for real: the bot died silently mid-session, and after the machine crashed
Postgres refused to start because a stale `postmaster.pid` survived (`brew services` just flapped into
`error`), leaving Mochi silently dead. Shipped:
- `launchd/com.mochi.agent.plist` — `RunAtLoad` + `KeepAlive`, `ThrottleInterval` 30s, logs to `data/mochi.log`.
- `scripts/preflight.sh` — repairs Postgres (removing a stale lock **only** after confirming no
  postmaster is running — the recorded PID had been recycled to an unrelated app), starts Ollama, and
  creates the 8k model if missing; exits non-zero so launchd retries.
- `scripts/run_mochi.sh` — preflight, then `exec` so launchd supervises the real process.
- **Verified** by killing the bot: restarted automatically in ~15s. Remaining gap → item #6.

### Latency instrumentation (and one fix)
`LATENCY_LOG=true` logs per-turn tool-select embedding ms, time-to-first-token, tool round-trips, total.
First measurements: the per-turn embedding round-trip is **~55ms warm** — so the once-planned
"keyword fast-path" was **ruled out** (~1–2% of a turn, not worth touching the fragile selection path).
But the *first* call cost **~1.1s** (it embeds every tool description), so the cache is now pre-warmed at
startup (`warmup._warm_tool_vectors`) — pure cache warming, no behavior change.
