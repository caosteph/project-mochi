# Future work — the consolidated, self-contained list

The single place that gathers what's next and *why*, grouped by theme. Each item is
**problem → why → rough effort**. The detailed phase plan is `docs/00-plan.md`; the shipped
status is in `CLAUDE.md`. Ordered within each group by leverage.

> **How to pick up an item:** anything touching the persona/tools/graph must be validated with
> `scripts/verify_firing.py --baseline <tools>` (HEAD-vs-working firing diff) before shipping —
> the 7B regresses silently. See CLAUDE.md's testing guidance.

---

## Latency & the 7B reliability ceiling

**⭐ Raise the local model's context window (likely the single highest-leverage fix).**
- **Problem:** measured — the model loads at **`num_ctx` = 4096** (Ollama's default; the app never
  sets it), but the persona alone is **~3,600 tokens**. Add the bound tools' schemas, the rolling
  summary, and recent turns and a turn **overflows 4096 and truncates the front — the persona/system
  instructions** (the "call this tool immediately" rules). This is a strong candidate root cause of
  the tool-firing flakiness we keep fighting (create_draft/remember_fact/add_reminder wobble).
- **Why it's not an app change:** Ollama's OpenAI-compatible endpoint **ignores per-request
  `num_ctx`** (verified). Raising it is operational: set **`OLLAMA_CONTEXT_LENGTH=8192`** on the
  Ollama server (simplest) or bake a Modelfile model (`PARAMETER num_ctx 8192`).
- **The catch (why it's a decision, not a default):** a bigger KV cache costs **more memory** on the
  16GB Air (already tight). It's a memory-for-reliability trade to make deliberately.
- **Effort:** ~15 min to try + validate. **Experiment:** set `OLLAMA_CONTEXT_LENGTH=8192`, restart
  Ollama, `scripts/verify_firing.py add_reminder,create_draft,web_search` vs the 4096 baseline. If
  firing improves, it's the cheapest quality win on the board.

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

**Process supervision + unclean-shutdown recovery. ⭐ (observed twice, for real)**
- **Problem:** the bot is a single long-polling process with no auto-restart, and nothing recovers
  the stack after a hard shutdown. Both happened during development: (1) the bot died silently
  mid-session; (2) the machine died, and afterwards **Postgres refused to start** because a stale
  `postmaster.pid` (referencing a since-recycled PID) survived the unclean shutdown — `brew services`
  just flapped into `error` state. Both left Mochi silently dead: no reminders, no briefing, no replies.
- **Why:** an assistant you rely on for reminders is worthless if it's quietly down. Neither failure
  announces itself; you find out by missing something.
- **Fix:** a macOS **launchd** plist with `KeepAlive` for the bot; a small **startup preflight** that
  checks Postgres + Ollama, clears a stale `postmaster.pid` when no postmaster is actually running,
  and starts what's missing; optionally a heartbeat ping so silence is detectable.
- **Effort:** small (a plist + a ~30-line preflight script). High value for reliability.

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
