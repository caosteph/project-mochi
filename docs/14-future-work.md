# Future work — the consolidated, self-contained list

The single place that gathers what's next and *why*, grouped by theme. Each item is
**problem → why → rough effort**. The detailed phase plan is `docs/00-plan.md`; the shipped
status is in `CLAUDE.md`. Ordered within each group by leverage.

> **How to pick up an item:** anything touching the persona/tools/graph must be validated with
> `scripts/verify_firing.py --baseline <tools>` (HEAD-vs-working firing diff) before shipping —
> the 7B regresses silently. See CLAUDE.md's testing guidance.

---

## ✅ RESOLVED — the context window was starving generation (2026-07)

Kept here because it reframes several older "7B limitations" as misdiagnoses.

- **What was wrong:** Ollama's default `num_ctx` is **4096**, but a normal turn's prompt measures
  **~3,800–4,050 tokens** (persona ~3,600 + bound tool schemas + rolling summary + history).
  `num_ctx` covers **prompt + generation**, so only **~75 tokens** remained to produce a reply —
  forcing llama.cpp **context-shifting mid-generation**, which evicts the front of the prompt: the
  persona's "call this tool immediately" instructions.
- **Not a prompt-eval truncation.** Prompt token counts are *identical* at 4096 and 8192 (3902,
  4021, 3954 …), so the prompt always fit. The damage happened while generating.
- **Fix:** a derived model, `ollama/Modelfile.qwen2.5-7b-8k` (`FROM qwen2.5:7b` +
  `PARAMETER num_ctx 8192`) + `LOCAL_MODEL=qwen2.5:7b-8k`. Surgical: Ollama's OpenAI endpoint ignores
  per-request `num_ctx`, and `OLLAMA_CONTEXT_LENGTH` is global (would also inflate nomic-embed).
- **Measured effect** — every previously "known-hard" prompt, N as shown:

  | prompt → tool | 4096 | 8192 |
  |---|---|---|
  | "ping me in 2 hours to stretch" → `add_reminder` | 0/8 | **6/6** |
  | "draft an email to me saying hi" → `create_draft` | 0/4 | **4/4** |
  | "draft a note to alex@example.com …" → `create_draft` | 0/8 | **4/4** |
  | "read me the email from Chase …" → `read_email` | 0/3 | **4/4** |

  Already-reliable prompts stayed 4/4 (a saturated set shows no delta — that's why the first
  `verify_firing` comparison read 33/36 → 33/36 and looked like a null result).
- **Cost:** +0.3 GB resident (4.6 → 4.9 GB); memory pressure unchanged, **zero swap** on the 16GB Air.
- **Misdiagnoses this corrects:** "the imperative *read me the email* derails the 7B" (docs/12);
  "the model won't draft to `alex@example.com`"; "`create_draft` is tool-count-diluted" (which had me
  lower a verify floor); and it **mechanistically explains why net-additive persona edits tanked
  firing** — extra persona tokens ate the last of the generation headroom.
- **Follow-up — TESTED, and it was the same root cause.** The documented **"tool-count wall"**
  (~11 tools fire, 13–15 → ~0) was *also* context exhaustion. Each bound tool costs **~95 prompt
  tokens**; at `num_ctx` 4096 with a ~3,600-token base prompt, ~11 tools is exactly where the prompt
  crossed the window. Measured on the 8k model:

  | tools bound | prompt_tokens | `add_reminder` | `create_draft` |
  |---|---|---|---|
  | 10 | 4,333 | 3/3 | 3/3 |
  | 13 | 4,644 | 3/3 | 3/3 |
  | **17 (all)** | **4,998** | **3/3** | **3/3** |

  Every one of those exceeds 4,096 — i.e. they'd have overflowed the old window. **Consequence:
  adding new tools is no longer dangerous** (budget ~95 tokens each; ~3,200 tokens of headroom
  remain, so roughly 30 more tools before pressure).
  **The ≤10 cap was left alone deliberately:** routing never excluded the right tool (15/15) and the
  subsets it picks are only 7–9 tools, so the cap isn't even binding — and per-turn selection still
  saves ~665 prompt tokens/turn (faster prefill, more generation headroom). It's now an
  optimization, not a workaround.

## Latency & the 7B reliability ceiling

**Mac mini + a larger local model (the root fix).**
- **Problem:** the 7B on a 16GB M2 Air is the ceiling for both quality (tool-firing reliability, the
  occasional derail) and headroom (macOS + 4.6GB model + Postgres + app). **Why:** a 14B–32B model on
  a 64GB mini would lift reliability at the source and end the memory pressure; it also unlocks a
  self-hosted CI runner (below). **Effort:** hardware (~$1.4k M4 Pro/64GB) + a migration pass. Biggest
  lever overall.

**Instrument-then-optimize the turn (instrumentation shipped).**
- `LATENCY_LOG=true` logs per-turn `tool_select` embedding ms, time-to-first-token, tool round-trips,
  total. Use it to decide anything below — don't pre-optimize.
- ~~**tool-select keyword fast-path**~~ — **ruled out by measurement.** The per-turn embedding round-trip
  costs **~55ms warm**, i.e. ~1–2% of a multi-second turn. Skipping it would risk the fragile selection
  path for no meaningful gain. Revisit only if a future profile says otherwise.
- ~~**Cold tool-vector cache**~~ — **fixed.** The *first* `select_tools` call cost **~1.1s** (it embeds
  every tool description); the cache is now pre-warmed at startup (`warmup._warm_tool_vectors`), so the
  first message after a restart no longer pays it. Pure cache warming — no behavior change.
- **Persona trim:** the ~3,600-token persona is both a latency cost (prefill) and a dilution risk.
  **Why:** shorter prompt → faster prefill + likely better tool-firing. **But:** persona edits are
  high-variance (a net-additive edit tanked firing this project) — treat as a measured experiment
  gated by `verify_firing.py`, trimming net-neutral-or-shorter. **Effort:** small-medium, high-care.
- **A small fast model for tool-less chat turns:** route pure conversation to a 3B while the 7B
  handles tool turns. **Why:** snappier chat. **But:** adds routing complexity + a second resident
  model (memory). **Effort:** medium. Reconsider after the mini.

## Reliability & operations

**✅ DONE — Process supervision + unclean-shutdown recovery** (both failures were observed for real:
the bot died silently mid-session, and after the machine died Postgres refused to start because a
stale `postmaster.pid` survived — `brew services` just flapped into `error`, leaving Mochi silently
dead). Shipped:
- `launchd/com.mochi.agent.plist` — `RunAtLoad` + `KeepAlive` (starts at login, restarts on any exit),
  `ThrottleInterval` 30s so a failing start doesn't hot-loop; logs to `data/mochi.log`.
- `scripts/preflight.sh` — verifies/repairs Postgres (removes a stale `postmaster.pid` **only** after
  confirming no postmaster is actually running — a recorded PID can be recycled to an unrelated
  process, which is exactly what we hit), starts Ollama, and creates the 8k model if missing. Exits
  non-zero so launchd retries rather than starting a bot that will fail every turn.
- `scripts/run_mochi.sh` — preflight, then `exec` the bot so launchd supervises the real process.
- **Verified** by killing the bot: launchd restarted it automatically (~15s).
- *Still open (small):* a heartbeat/liveness ping so a *silently wedged* (not crashed) bot is
  detectable — KeepAlive only catches exits.

**PostgresSaver checkpoint growth.**
- **Problem:** the LangGraph checkpointer writes a row per turn and nothing prunes it — unbounded
  growth. **Why:** disk/scan cost creeps over months. **Fix:** a periodic retention job (keep last N
  per thread / last M days). **Effort:** small.

**Schema migrations.**
- **Problem:** `init_db` = `create_all` + hand-written `ALTER`s; `create_all` won't alter existing
  tables, so new columns are added manually. **Why:** fragile + error-prone as the schema evolves.
  **Fix:** adopt **Alembic**. **Effort:** medium (mostly one-time).

**Full isolation for the code sandbox.**
- **Problem:** `SubprocessSandbox` is best-effort (scrubbed env + cwd jail + best-effort
  `sandbox-exec`), not real isolation. **Why:** the builder runs generated code. **Fix:** the
  `DockerSandbox` drop-in (better on the mini). **Effort:** medium.

## Security

**Deep-read web results via the quarantined reader.**
- **Problem:** web-search snippets are `frame_untrusted`-wrapped (soft-tier "data not instructions"),
  not passed through the dual-LLM boundary — a determined injection in a result is only bounded by
  no-send/gated-writes/local. **Why:** the moment we fetch *full* result pages (richer answers), that
  residual grows. **Fix:** route fetched pages through `quarantine` like email bodies. **Effort:**
  medium. Pairs with the "fetch full page" capability.

## Capabilities

- **Google Drive (read):** pull up receipts/docs, quarantined like email. New OAuth scope. Medium.
- **Deeper long-term memory / preferences:** a richer profile so replies + briefing get more
  personal over time (Phase 5). Medium.
- **Email in the daily briefing:** once the Phase 3B scanner is proven quiet and re-enabled. Small.
- **Voice-message transcription; quick lists/notes.** Small each.
- **Generalizable per-action approval layer:** a config-driven policy of *which* actions need
  Approve/Reject (today: drafts + web search via `_render_proposal`; extend to calendar-event writes,
  etc.). **Why:** Stephanie asked for broader "ask permission when doing stuff." Medium.

## Search

- **More providers behind the existing seam:** **SearXNG** (self-hosted → fully local query routing,
  the privacy ideal) and **Brave**; smarter result ranking. Switching is already one config value
  (`WEB_SEARCH_PROVIDER`). Small-medium each.

## Testing

- **Self-hosted CI runner (Mac mini):** run the full real-model `scripts/verify_all.sh` in CI once
  the mini exists — closes the gap that model-behavior isn't gated on GitHub today. Medium.
- **Coverage gate:** `pytest-cov` runs in CI now; add a threshold once a baseline is known. Small.
- **Optional Ollama+nomic in CI:** to also run the 2 embedding-semantic tests (`test_memory_recall`)
  in CI rather than only locally. Small, if fuller coverage is wanted.
