# Future work — the consolidated, self-contained list

Everything that's next, in **priority order** (value × feasibility under the real constraints: $0
budget, 16GB M2 Air, solo project, daily personal use). Each item covers the goal, roughly how, why
it's worth it, and rough effort. Resolved work is kept at the bottom because the lessons still apply.

Detailed phase plan: [`00-plan.md`](./00-plan.md). Shipped status: [`CLAUDE.md`](../CLAUDE.md).

> **Picking anything up:** if it touches the persona, tools, or graph, validate with
> `scripts/verify_firing.py --baseline <tools>` before shipping — the 7B regresses silently.

---

### 1. Automated backups of the memory DB
Everything the project exists for — 34 reminders, memory, goals, message history — lives in one
Postgres with **zero backups**, on a machine that already crashed this week. A `pg_dump` to a
git-ignored `backups/` dir, rotated, scheduled daily via launchd (mirroring the agent plist), and
restored once into a scratch DB to prove it actually works. Best risk-to-effort ratio on this list:
a disk failure or a bad migration currently erases the whole point of the project. **Small (~20 lines).**

### 2. Make long-term memory actually accumulate
The premise is "holds long-term memory about her life" — the production DB has **1 fact from 89
messages**. Diagnose before building: instrument the post-turn extraction sweep (`app/memory/extract.py`)
to report candidates found vs stored vs deduped, replay recent `MessageLog` history through it offline
to get a real capture rate, then fix the weak link (likely making the sweep primary and the flaky
`remember_fact` tool a bonus). Add `/facts` so it's visible what she's remembered. Without this it's a
capable chatbot with tools, not a personal agent — and it compounds: briefing, replies, and proactivity
all improve when memory is real. **Medium, diagnosis-led.**

### 3. Re-enable the email signal scanner (the flagship)
The headline feature — spot a purchase, remind her before the return window closes — is switched
**off** because early scans were noisy. It now has dedup, a require-due-date filter, per-scan caps,
go-forward-only baselining, an approval ask, and the context fix that improved extraction. Turn it on
in shadow mode first (log detections without pushing), hand-check precision for a few days, tighten,
then enable the proactive ask with `/pause` as the kill switch. It's the differentiator that motivated
the project, currently dormant, and the only feature that creates value with zero effort from her.
**Medium — mostly tuning and judgement, not new code.**

### 4. Let a task be retired
Nothing can mark a task dead. She was rejected from Perplexity and wrote "I ALRWADY GOT REJEXTED FROM
PERPLECITH NO NEED TO KEEP REMINDING" — and "Perplexity prep" kept being recreated (8 rows), still
pending for the next morning until it was cancelled by hand. Cancelling one instance doesn't stop the
next recreation, because nothing records that the *underlying thing* is over. Add an explicit
"that's done / that's dead" path — a `retire_task` tool plus a tombstone the reminder and
email-signal paths consult before creating — so obsolescence is stated once and respected everywhere.
This, not dedup, is the real fix for what made her stop using it. **Medium.**

### 5. Voice messages (local transcription)
Handle Telegram voice notes: download the audio, transcribe locally with `whisper.cpp`/`faster-whisper`
(base model is plenty for short notes), feed the text through the normal turn path. Strictly local —
audio is personal data, so it never leaves the machine. This is the biggest everyday UX upgrade on a
phone: capturing a reminder while walking is exactly where typing loses, and it's what makes an
assistant habitual rather than occasional. **Medium, $0.**

### 6. Try a newer same-size local model
The 2026-07 finding was that *context*, not model capability, was the real bottleneck — so the model
choice deserves a fair re-test. Build 8k variants of one or two modern 7–8B candidates, A/B them with
`verify_firing.py` + `verify_scenarios.py` at equal context, and compare firing, coherence, and
tokens/sec; adopt only on measured improvement (`LOCAL_MODEL` is a one-line switch). Potentially a
large quality gain for **$0**, and it directly tests whether the ~$1.4k Mac mini is still needed.
**Small — a few hours of measurement.**

### 7. Secret scanning in CI (a hard rule currently enforced by habit)
Rule 5 — *secrets never leave the machine and never get committed* — is the **only hard-tier rule not
enforced by code**. Today it's `.gitignore` plus a human remembering to check that `.env` isn't staged
before each push. On a **public** repo the failure mode is severe and unrecoverable: a leaked bot token
or hosted API key stays in git history after any deletion, so the fix is key rotation, not a revert.
Add `gitleaks` (or `trufflehog`) as a third CI job on every push, and optionally a pre-commit hook so
it fails *before* the commit exists rather than after it's public. Low probability × irreversible
outcome × ~20 lines is exactly the shape of thing CLAUDE.md says belongs in the hard tier rather than
in someone's memory. Distinct from *Secrets at rest* below, which is about how `.env` is **stored**;
this is about it **escaping into git history**. **Small.**

### 8. A fast smoke tier for the gate
The standing rule is to re-run the tool-firing verifies after *any* persona/tool/graph change — but a
full `verify_all.sh` measured **21 minutes** wall-clock (2026-07-21). That cost makes it the first
thing skipped under time pressure, and a skipped gate is precisely how earlier regressions reached
Stephanie before any test caught them. Add a smoke tier (`verify_smoke.sh`, or a `--fast` flag): ruff
+ the offline suite + `verify_scenarios` + firing on the core tools, targeting ~3 minutes, with the
full sequential run reserved for pre-push. It doesn't add coverage — it makes the coverage we already
have cheap enough that the rule is actually followed mid-change instead of only at the end. **Small.**

### 9. Measure answer *quality*, not just tool firing
Every gate we have asks "did the right tool fire?" and "is the reply free of JSON?" — nothing asks
"was the answer any good?". SOTA practice is eval-driven: a golden set of conversations scored by an
LLM judge, gating changes on the score. Cheap here: reuse the existing free hosted model
(`router`/`consult_expert` path) as the judge, keep a small fixture set of real turns with expected
properties (correct, grounded in the tool result, no invention, right tone), and add it to
`verify_all.sh`. Without it, a change can quietly make replies worse while every gate stays green —
the exact failure class that reached Stephanie before. **Medium.**

### 10. Liveness heartbeat + `/status`
launchd's `KeepAlive` restarts a process that *exits*, but not one that's wedged (hung poll, dead DB
connection, unloaded model). Add a self-check that verifies the essentials — polling alive, Postgres
reachable, Ollama responding, last tick recent — and self-heals or pings the chat on failure, plus a
`/status` command reporting uptime, model, last briefing, pending reminders, and dependency health.
An assistant you trust with reminders is worse than useless when it's quietly dead, and right now
silence looks identical to "nothing to say." **Small.**

### 11. Nightly review pass (her own request)
She asked for this directly: *"Can you run a nightly dream pass that checks for any mistakes or
corrections that I had to make for you and then in the morning propose durable fixes."* Mine the day's
`messagelog` for correction signals — "no", "stop", "I didn't ask for", a reminder cancelled shortly
after creation — summarise them on the local model, and surface a short morning list of proposed
changes she can approve. It converts her frustration into durable fixes instead of repeated
corrections, and it's the only item here that improves the system without her having to report a bug.
**Medium.**

### 12. Deeper memory & preferences (Phase 5)
Build on #2 — there's no point structuring memory that isn't being captured. Add lightweight typed
structure (person / preference / routine / project) with confidence and recency, surface it as a
compact profile block in the system prompt rather than raw recall hits, and feed it into the briefing
and replies. Mind the prompt budget: the persona already uses ~3,600 of the 8k window. This is the
difference between a generic assistant and *hers*. **Medium.**

### 13. Generalizable per-action approval layer
Today the gate is ad-hoc: `create_draft` and `web_search` call `require_approval` directly and
`render.render_proposal` switches per action. Promote it to a declared policy — a config map of
action → {always ask / ask once then remember / never} with a renderer registry, so gating a new
action is a table entry rather than bespoke code — and extend it to currently-ungated external writes
like calendar-event mirroring. Stephanie explicitly asked for "ask permission when doing stuff", and
predictability is what makes a permission model trustworthy. **Medium.**

### 14. Google Drive (read, quarantined)
Mirror the Gmail pattern exactly: least-privilege read-only scope, a search/read tool pair, bodies
routed through the **quarantined reader** (never into the privileged agent), results
`frame_untrusted`-wrapped, no write capability. The last major personal data source, and it
strengthens the receipt/return flows. Now unblocked — tool count is no longer a constraint (~95
prompt tokens per tool, ~3,200 of headroom). **Medium (new OAuth scope).**

### 15. Email in the daily briefing
Fold email signals into the morning digest once #3 is proven quiet. Currently excluded on purpose
because the scanner was the noisy part. **Small.**

### 16. Speak MCP (Model Context Protocol)
The original plan called for off-the-shelf MCP servers via `langchain-mcp-adapters`; we went direct
instead (right call at the time — fewer moving parts). But MCP is now the ecosystem standard, and an
MCP client would let Mochi use maintained servers (Drive, Notion, Slack, filesystem, …) instead of a
bespoke integration each time. Constraint to respect: each bound tool costs ~95 prompt tokens, so
adopt MCP *behind* the existing `tool_select` filter rather than binding whole servers. Biggest
leverage-per-effort for capability breadth. **Medium.**

### 17. Deep-read a web result page (closes an injection residual)
Web-search snippets are `frame_untrusted`-wrapped — soft-tier "data, not instructions" — rather than
passed through the dual-LLM boundary. Route fetched pages through `quarantine` like email bodies. This
matters more the moment we fetch *full* pages for richer answers; today the residual is bounded by
no-send / gated-writes / local-only. **Medium.**

### 18. Decision tracing / observability
There is no way to answer "why did it do that last Tuesday?". `MessageLog` stores text but not the
trajectory: which tools were in the bound subset, which fired, what came back, how long each took.
SOTA is structured tracing (OpenTelemetry / Langfuse-style). A local-first version — one row per turn
capturing the tool subset, calls, token counts and latency — makes regressions diagnosable after the
fact instead of only reproducible live. Builds on the latency logging already shipped. **Small-medium.**

### 19. Gate-result history (so a slow decline is visible)
Every `verify_all.sh` run is judged in isolation, so a *gradual* decline is invisible. Sampling makes
this sharper: with `sample_check` a behavioural check can stay green while quietly needing three
attempts where it once needed one, and nothing but human memory compares runs. Append one JSONL row
per run — commit sha, timestamp, per-check name/verdict/`hits`/`attempts` — to a git-ignored
`data/gate-history.jsonl`, and print a one-line diff against the previous run at the end. Turns "is
this getting worse?" into a question answerable from data rather than recollection. Distinct from
*Decision tracing / observability* below, which records production turns, not gate results. **Small.**

### 20. More search providers
Add SearXNG (self-hosted → fully local query routing, the privacy ideal) and Brave behind the existing
seam; smarter result ranking. Switching is already one config value (`WEB_SEARCH_PROVIDER`). **Small each.**

### 21. Constrained decoding + validated retry for tool calls
The quarantined reader already uses `json_schema` structured output; the main agent's tool calls
don't — a malformed call just fails the turn. Small models benefit disproportionately from
constrained decoding plus a single validate-and-retry. Cheap reliability that doesn't depend on
getting a better model. **Small.**

### 22. Interruptibility (cancel a running turn)
A turn can't be stopped once it starts — if Mochi misreads a message and begins building the wrong
thing, Stephanie waits it out. A `/stop` command (plus ignoring superseded turns) is standard
assistant UX and cheap here. **Small.**

### 23. Alembic migrations
`init_db` is `create_all` + hand-written `ALTER`s, and `create_all` won't alter existing tables, so new
columns are added by hand. Fine at this size, fragile as the schema grows — and #2/#7 will grow it.
**Medium, mostly one-time.**

### 24. Checkpoint pruning
`PostgresSaver` writes a row per turn and nothing prunes it, so it grows unbounded. A periodic
retention job (keep last N per thread / last M days). **Small.**

### 25. Docker sandbox for generated code
`SubprocessSandbox` is best-effort — scrubbed env, cwd jail, best-effort `sandbox-exec` — not real
isolation, and the builder executes model-generated code. The `DockerSandbox` drop-in was always the
plan (better on the mini). **Medium.**

### 26. Secrets at rest
`.env` holds the bot token and hosted API key in plaintext. Keychain was always the plan. **Small-medium.**

### 27. Self-hosted CI runner + coverage gate
Model behavior isn't gated on GitHub today (no Ollama in CI, by design). A self-hosted runner on the
mini could run the full `verify_all.sh`; separately, add a coverage threshold now that `pytest-cov`
runs in CI, and optionally run the one embedding-semantic test file there too. **Medium / small.**

### 28. Doc bloat and drift
`docs/` is ~3,900 lines (`05-phase1-build.md` alone is 1,312). More importantly, this session found
**three confidently-written conclusions that were wrong**, all downstream of one unmeasured config. Do
a periodic "does this still match reality?" pass, and prefer linking measurements over restating them.
**Small, recurring.**

### 29. Mac mini + a larger local model
Still the best raw quality lever — reliability, memory headroom, and it unlocks the self-hosted CI
runner. Ranked last because it's ~$1.4k against a $0 budget and the context fix already delivered much
of what it promised. Revisit after #5 says whether a free model swap gets there. **Hardware + a migration pass.**

---

## ✅ Resolved (kept — the lessons still apply)

### The context window was starving generation (2026-07-20)
Ollama's default `num_ctx` is **4096**, but a turn's prompt measured **~3,800–4,050 tokens**. `num_ctx`
covers **prompt + generation**, so only **~75 tokens** remained to reply — forcing llama.cpp
**context-shifting mid-generation**, evicting the front of the prompt: the persona's "call this tool
immediately" instructions. *Not* prompt-eval truncation — token counts are identical at 4096 and 8192,
so the prompt always fit; the damage happened while generating.

Fixed with `ollama/Modelfile.qwen2.5-7b-8k` (`FROM qwen2.5:7b` + `PARAMETER num_ctx 8192`) +
`LOCAL_MODEL=qwen2.5:7b-8k` — surgical because Ollama's OpenAI endpoint ignores per-request `num_ctx`
and `OLLAMA_CONTEXT_LENGTH` is global (it would also inflate nomic-embed). Every previously
"known-hard" prompt: "ping me in 2 hours to stretch" **0/8 → 6/6**, "draft an email to me saying hi"
**0/4 → 4/4**, "draft a note to alex@example.com …" **0/8 → 4/4**, "read me the email from Chase …"
**0/3 → 4/4**. Cost: +0.3 GB resident, zero swap.

This corrected three earlier misdiagnoses ("the 7B derails on imperatives", "`create_draft` is
tool-count-diluted" — which had me lower a verify floor) and explains **why net-additive persona edits
tanked firing**: they ate the last of the generation headroom.

**Measurement lesson:** the first before/after read 33/36 → 33/36 and looked like a null result, because
the prompt set was **saturated** (8 of 9 already 4/4). Gains only show on prompts that were *failing* —
always include known-failing canaries in a before/after.

### The "tool-count wall" was the same root cause
The documented wall (11 tools fire, 13–15 → ~0) was also context exhaustion: each bound tool costs
**~95 prompt tokens**, so at 4096 with a ~3,600-token base prompt, ~11 tools is exactly where the
prompt crossed the window. On the 8k model: 10 tools → 4,333 tok, 13 → 4,644, **17 (all) → 4,998, and
`add_reminder`/`create_draft` fire 3/3 at every count**. So adding tools is no longer dangerous
(~3,200 tokens of headroom ≈ room for ~30 more). The ≤10 cap in `tool_select` was left alone
deliberately — routing never excluded the right tool (15/15) and its subsets are only 7–9 tools, so the
cap isn't binding, while per-turn selection still saves ~665 prompt tokens/turn. It's an optimization
now, not a workaround.

### Process supervision + unclean-shutdown recovery
Both failures were observed for real: the bot died silently mid-session, and after the machine crashed
Postgres refused to start because a stale `postmaster.pid` survived (`brew services` flapped into
`error`), leaving Mochi silently dead. Shipped `launchd/com.mochi.agent.plist` (`RunAtLoad` +
`KeepAlive`, 30s throttle, logs to `data/mochi.log`), `scripts/preflight.sh` (repairs Postgres —
removing a stale lock **only** after confirming no postmaster is running, since the recorded PID had
been recycled to an unrelated app — starts Ollama, creates the 8k model if missing), and
`scripts/run_mochi.sh` (preflight, then `exec` so launchd supervises the real process). Verified by
killing the bot: restarted automatically in ~15s. Remaining gap → *Liveness heartbeat + `/status`*
(a **wedged** process still isn't restarted — `KeepAlive` only catches one that exits). Referred to by
name, not number: this line previously pointed at "item #6", which renumbering had silently turned
into an unrelated entry.

### Tech debt paydown (2026-07-20)
Measured findings from the whole-repo pass, fixed:
- **`scripts/_verify_lib.py`** — the verify scripts had drifted into copy-paste: `check()` in **9**
  scripts (three different signatures), `fires()` in **4**, the scratch-DB guard in **7**, env
  placeholders in **10**. All ten now share one library; duplication is 0 and total verify LOC went
  **1,289 → 1,182** *including* the new file. Consolidating also upgraded every script to the best
  variant — failures are now listed by name (previously only `verify_phase1` did that).
- **`google_calendar.format_event(e, with_date=…)`** replaces the duplicated `_fmt_event` in
  `google_tools.py` and `briefing.py` (same parsing, two presentations). It lives in
  `google_calendar` because both callers already import it — no new dependency, no cycle.
- **Dead code removed**: `jobs.is_enabled()` (never called).
- **`app/channels/render.py`** — the stateless presentation layer (status breadcrumbs, per-action
  approval proposal, MarkdownV2 conversion, chunking) split out of `telegram.py` and now **100%
  covered**; `telegram.py` 705 → 663 LOC.
- **Coverage**: `warmup.py` **0% → 65%**, `memory_tools` **40% → 88%**, `reminder_tools` **34% → 86%**;
  suite **164 → 183 tests**, total **78% → 81%**.

**Deliberately not done — the full `telegram.py` class split.** Moving the 9 command handlers and the
155-line `_run_with_status` into mixins would mostly relocate code: it adds indirection without fixing
a defect, and it refactors the live path of a bot in daily use. The part with real value (the pure
render layer) is done. Revisit only when there's a concrete reason — e.g. a second channel (iMessage)
actually needing to share the streaming engine.

### What the real transcripts exposed (2026-07-20)
Reading the production `messagelog` + `reminder` tables instead of trusting invented benchmarks:
**26 of 34 reminders had been hand-cancelled**, `Perplexity prep` existed ×8, and usage collapsed
(23 messages on 07-12 → 2 on 07-17 → silence). Fixed:
- **Duplicate reminders.** `_find_duplicate` only matched reminders due within ±60 min, so the same
  task recreated on a later day never collapsed. It now also matches a pending same-task reminder at
  the **same local time of day** within `reminder_dedup_horizon_days` (7) — while 9am vs 9pm stays
  two reminders, so a genuine twice-daily one still works. `add_reminder` now replies *"that's already
  set … I didn't add a second one"* instead of implying it created something.
- **Duplicate replies.** Two processes polling one bot token both answer every message ("you also just
  duplicated the message that you just sent me"). `app/main.py` now takes an exclusive `flock`; a
  second instance exits. This most likely also explains the late-night verbosity — a stale process
  still held the old 4096-context model after the switch.
- **Formatting during streaming.** `render.balance_markdown()` closes the markers a half-streamed
  buffer leaves dangling, so MarkdownV2 applies live instead of popping in at the end.
- **Benchmarks re-grounded in her words.** `verify_scenarios` gained the class of check the suite
  never had — **must-NOT-fire** ("don't set any reminders" → `add_reminder` stays silent), plus
  "answers instead of promising to check", greeting restraint, and no markdown-dumping. All 12 pass.

### The gate cried wolf: single-sample checks on a stochastic model (2026-07-21)
The very next gate run failed on `add_goal wrote a row | 0 -> 0` and `add_task wrote a row | 0 -> 0`.
Re-probing the same prompt at N=3 fired `['add_goal','add_task']` **3/3** — single-sample variance on
a 7B, not a regression. That's the worst place for a false alarm: a gate that goes red at random stops
being believed, and the next *real* regression gets waved through.

The suite was inconsistent about this — `verify_phase2`'s reliability rates and `verify_phase1`'s
fact-capture sweep already sampled and applied soft floors, but a handful of *gating behavioural*
checks invoked the model exactly once. Added `_verify_lib.sample_check(name, probe, samples=, need=)`:
it runs a probe up to `samples` times, passes at `need` hits, and **early-exits as soon as the outcome
is decided**, so a healthy check still costs one model call (it prints `1/1`, not `1/3`) and only a
wobbling one pays for retries. Applied to `verify_phase1` (both Biscuit recall checks, `need=1 of 3`;
`add_goal`/`add_task` driven from **one** retry loop so a two-tool prompt isn't invoked twice) and
`verify_scenarios` (calendar-answers and greeting-length at `need=1 of 2`).

**The knob that matters is `need`, and it encodes the check's meaning.** A *capability* check ("can it
do this at all") uses `need=1` — retry semantics. A **must-not** check ("this must never happen") uses
**`need=samples`**, so every sample has to be clean: retrying a must-not until it passes would launder
the violation. "A bare greeting triggers no tool" is therefore `2 of 2`, not `1 of 2`.

Honest tradeoff: retries cut false alarms but could mask a *slow* decline. Mitigations — the
`hits/attempts` tally is always printed so a scrape-by is visible, and `scripts/verify_firing.py`
remains the tool for measuring an actual rate rather than a pass/fail. `tests/test_verify_lib.py`
(11 offline tests, fake probes) pins the verdict *and* the call count for every branch, including that
a retry can't launder a must-not violation. Post-change: `verify_phase1` 9/9, `verify_scenarios` 12/12.

### Latency instrumentation (and one fix)
`LATENCY_LOG=true` logs per-turn tool-select embedding ms, time-to-first-token, tool round-trips, and
total. First measurements: the per-turn embedding round-trip is **~55ms warm**, so the once-planned
"keyword fast-path" was **ruled out** (~1–2% of a turn, not worth touching the fragile selection path).
But the *first* call cost **~1.1s** (it embeds every tool description), so the cache is now pre-warmed
at startup (`warmup._warm_tool_vectors`) — pure cache warming, no behavior change.
