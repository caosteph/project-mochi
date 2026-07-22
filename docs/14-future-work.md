# Future work — the consolidated, self-contained list

Everything that's next, in **priority order** (value × feasibility under the real constraints: $0
budget, 16GB M2 Air, solo project, daily personal use). Each item covers the goal, roughly how, why
it's worth it, and rough effort. Resolved work is kept at the bottom because the lessons still apply.

Detailed phase plan: [`00-plan.md`](./00-plan.md). Shipped status: [`CLAUDE.md`](../CLAUDE.md).

> **Picking anything up:** if it touches the persona, tools, or graph, validate with
> `scripts/verify_firing.py --baseline <tools>` before shipping — the 7B regresses silently.
>
> **Cross-reference items by NAME, never by number.** Reordering renumbers everything, and two
> `#N` references have already rotted into pointing at unrelated entries.

---

### 1. Automated backups of the memory DB
Everything the project exists for — 34 reminders, memory, goals, message history — lives in one
Postgres with **zero backups**, on a machine that already crashed this week. A `pg_dump` to a
git-ignored `backups/` dir, rotated, scheduled daily via launchd (mirroring the agent plist), and
restored once into a scratch DB to prove it actually works. Best risk-to-effort ratio on this list:
a disk failure or a bad migration currently erases the whole point of the project. **Small (~20 lines).**

### 2. Let a task be retired
Nothing can mark a task dead. She was rejected from Perplexity and wrote "I ALRWADY GOT REJEXTED FROM
PERPLECITH NO NEED TO KEEP REMINDING" — and "Perplexity prep" kept being recreated (8 rows), still
pending for the next morning until it was cancelled by hand. Cancelling one instance doesn't stop the
next recreation, because nothing records that the *underlying thing* is over. Add an explicit
"that's done / that's dead" path — a `retire_task` tool plus a tombstone the reminder and
email-signal paths consult before creating — so obsolescence is stated once and respected everywhere.
This, not dedup, is the real fix for what made her stop using it. **Medium.**

**Promoted (2026-07-21):** this now sits above the email signal scanner. Re-enabling proactive
detection before there's a way to say "that's over" would recreate exactly the loop that ended her
usage — she'd get new auto-created reminders with no mechanism to kill the underlying thing.

### 3. Make memory real (capture first, then structure)
The premise is "holds long-term memory about her life" — the production DB has **1 fact from 89
messages**. Two stages, deliberately one item: there is no point structuring memory that isn't being
captured, and splitting them invites building (b) first.

**(a) Fix capture.** Instrument the post-turn extraction sweep (`app/memory/extract.py`) to report
candidates found vs stored vs deduped, replay recent `MessageLog` history through it offline to get a
real capture rate, then fix the weak link (likely making the sweep primary and the flaky
`remember_fact` tool a bonus). Add `/facts` so what she's remembered is visible.

**(b) Then add structure** (the old Phase 5): lightweight typed records (person / preference / routine
/ project) with confidence and recency, surfaced as a compact profile block in the system prompt
rather than raw recall hits, and fed into the briefing and replies. Mind the prompt budget — the
persona already uses ~3,600 of the 8k window.

Without (a) this is a capable chatbot with tools, not a personal agent; with both it's *hers*. It
compounds: briefing, replies, and proactivity all improve when memory is real. **Medium, diagnosis-led.**

### 4. Liveness heartbeat + `/status`
launchd's `KeepAlive` restarts a process that *exits*, but not one that's wedged (hung poll, dead DB
connection, unloaded model). Add a self-check that verifies the essentials — polling alive, Postgres
reachable, Ollama responding, last tick recent — and self-heals or pings the chat on failure, plus a
`/status` command reporting uptime, model, last briefing, pending reminders, and dependency health.
An assistant you trust with reminders is worse than useless when it's quietly dead, and right now
silence looks identical to "nothing to say." **Small.**

**Promoted (2026-07-21):** she has already lived through a silently-dead bot, and until this exists
"Mochi has nothing to say" and "Mochi is down" look identical from her phone. Small effort, direct
trust impact.

### 5. Re-enable the email signal scanner (the flagship)
The headline feature — spot a purchase, remind her before the return window closes — is switched
**off** because early scans were noisy. It now has dedup, a require-due-date filter, per-scan caps,
go-forward-only baselining, an approval ask, and the context fix that improved extraction. Turn it on
in shadow mode first (log detections without pushing), hand-check precision for a few days, tighten,
then enable the proactive ask with `/pause` as the kill switch. It's the differentiator that motivated
the project, currently dormant, and the only feature that creates value with zero effort from her.
**Depends on *Let a task be retired*** — turning noisy proactivity back on before obsolescence can
be stated once and respected everywhere repeats the failure that made her stop. A demotion by
dependency, not by value. **Medium — mostly tuning and judgement, not new code.**

### 6. Voice messages (local transcription)
Handle Telegram voice notes: download the audio, transcribe locally with `whisper.cpp`/`faster-whisper`
(base model is plenty for short notes), feed the text through the normal turn path. Strictly local —
audio is personal data, so it never leaves the machine. This is the biggest everyday UX upgrade on a
phone: capturing a reminder while walking is exactly where typing loses, and it's what makes an
assistant habitual rather than occasional. **Medium, $0.**

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

### 9. Try a newer same-size local model
The 2026-07 finding was that *context*, not model capability, was the real bottleneck — so the model
choice deserves a fair re-test. Build 8k variants of one or two modern 7–8B candidates, A/B them with
`verify_firing.py` + `verify_scenarios.py` at equal context, and compare firing, coherence, and
tokens/sec; adopt only on measured improvement (`LOCAL_MODEL` is a one-line switch). Potentially a
large quality gain for **$0**, and it directly tests whether the ~$1.4k Mac mini is still needed.
**Small — a few hours of measurement.**

### 10. Measure answer *quality*, not just tool firing
Every gate we have asks "did the right tool fire?" and "is the reply free of JSON?" — nothing asks
"was the answer any good?". SOTA practice is eval-driven: a golden set of conversations scored by an
LLM judge, gating changes on the score. Cheap here: reuse the existing free hosted model
(`router`/`consult_expert` path) as the judge, keep a small fixture set of real turns with expected
properties (correct, grounded in the tool result, no invention, right tone), and add it to
`verify_all.sh`. Without it, a change can quietly make replies worse while every gate stays green —
the exact failure class that reached Stephanie before. **Medium.**

### 11. Nightly review pass (her own request)
She asked for this directly: *"Can you run a nightly dream pass that checks for any mistakes or
corrections that I had to make for you and then in the morning propose durable fixes."* Mine the day's
`messagelog` for correction signals — "no", "stop", "I didn't ask for", a reminder cancelled shortly
after creation — summarise them on the local model, and surface a short morning list of proposed
changes she can approve. It converts her frustration into durable fixes instead of repeated
corrections, and it's the only item here that improves the system without her having to report a bug.
**Medium.**

### 12. Generalizable per-action approval layer
Today the gate is ad-hoc: `create_draft` and `web_search` call `require_approval` directly and
`render.render_proposal` switches per action. Promote it to a declared policy — a config map of
action → {always ask / ask once then remember / never} with a renderer registry, so gating a new
action is a table entry rather than bespoke code — and extend it to currently-ungated external writes
like calendar-event mirroring. Stephanie explicitly asked for "ask permission when doing stuff", and
predictability is what makes a permission model trustworthy. **Medium.**

*Advanced (2026-07-21):* the interrupt/resume spine is now generalized beyond approve/reject — see
*Buttons for any yes/no or pick-one decision* in Resolved. `confirm.ask_choice` + the `choice`
interrupt payload + the `ans:` callback are the reusable half; what's left here is the *policy* layer
(remember "don't ask again", a registry) rather than the mechanism.

### 13. Google Drive (read, quarantined)
Mirror the Gmail pattern exactly: least-privilege read-only scope, a search/read tool pair, bodies
routed through the **quarantined reader** (never into the privileged agent), results
`frame_untrusted`-wrapped, no write capability. The last major personal data source, and it
strengthens the receipt/return flows. Now unblocked — tool count is no longer a constraint (~95
prompt tokens per tool, ~3,200 of headroom). **Medium (new OAuth scope).**

### 14. Email in the daily briefing
Fold email signals into the morning digest once *Re-enable the email signal scanner* is proven quiet. Currently excluded on purpose
because the scanner was the noisy part. **Small.**

### 15. A coverage floor in CI
`pytest-cov` already runs in CI but nothing fails on a drop, so coverage can erode silently — it
sat at 78% before the last two passes moved it to ~82%. Add a `--cov-fail-under` threshold set just
below current, and optionally run the one embedding-semantic test file too. Five lines of workflow
config, blocked on nothing. (Split out of the old "self-hosted CI runner + coverage gate": bundling
the free half with a half that needs ~$1.4k of hardware kept it artificially low on this list.)
**Small.**

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

### 18. Structured run records (turns *and* gate runs)
There is no way to answer "why did it do that last Tuesday?", and no way to answer "is the gate
getting worse?". Both are the same missing thing — a structured row per run — so build one writer with
two surfaces:

- **Per production turn:** which tools were in the bound subset, which fired, what came back, latency,
  token counts. `MessageLog` stores the text but not the trajectory. Builds on the latency logging
  already shipped; SOTA equivalent is OpenTelemetry/Langfuse-style tracing.
- **Per gate run:** commit sha, timestamp, and each check's verdict + `hits`/`attempts`, appended to a
  git-ignored JSONL with a one-line diff against the previous run. This matters *because* of
  `sample_check`: a behavioural check can now stay green while quietly needing three attempts where it
  once needed one, and nothing but human memory compares runs.

Makes regressions diagnosable after the fact instead of only reproducible live. **Small-medium.**

### 19. More search providers
Add SearXNG (self-hosted → fully local query routing, the privacy ideal) and Brave behind the existing
seam; smarter result ranking. Switching is already one config value (`WEB_SEARCH_PROVIDER`). **Small each.**

### 20. Constrained decoding + validated retry for tool calls
The quarantined reader already uses `json_schema` structured output; the main agent's tool calls
don't — a malformed call just fails the turn. Small models benefit disproportionately from
constrained decoding plus a single validate-and-retry. Cheap reliability that doesn't depend on
getting a better model. **Small.**

### 21. Interruptibility (cancel a running turn)
A turn can't be stopped once it starts — if Mochi misreads a message and begins building the wrong
thing, Stephanie waits it out. A `/stop` command (plus ignoring superseded turns) is standard
assistant UX and cheap here. **Small.**

### 22. Alembic migrations
`init_db` is `create_all` + hand-written `ALTER`s, and `create_all` won't alter existing tables, so new
columns are added by hand. Fine at this size, fragile as the schema grows — and *Make memory real*
and *Measure answer quality* will both grow it.
**Medium, mostly one-time.**

### 23. Checkpoint pruning
`PostgresSaver` writes a row per turn and nothing prunes it, so it grows unbounded. A periodic
retention job (keep last N per thread / last M days). **Small.**

### 24. Docker sandbox for generated code
`SubprocessSandbox` is best-effort — scrubbed env, cwd jail, best-effort `sandbox-exec` — not real
isolation, and the builder executes model-generated code. The `DockerSandbox` drop-in was always the
plan (better on the mini). **Medium.**

### 25. Secrets at rest
`.env` holds the bot token and hosted API key in plaintext. Keychain was always the plan. **Small-medium.**

### 26. Doc bloat and drift
`docs/` is ~3,900 lines (`05-phase1-build.md` alone is 1,312). More importantly, this session found
**three confidently-written conclusions that were wrong**, all downstream of one unmeasured config. Do
a periodic "does this still match reality?" pass, and prefer linking measurements over restating them.
**Small, recurring.**

### 27. Mac mini + a larger local model (and the self-hosted gate runner)
Still the best raw quality lever — reliability, memory headroom, and it's the only way to run the
**full real-model gate in CI**: `verify_all.sh` can't run on GitHub (no Ollama, by design), so a
self-hosted runner on the mini is the only path to gating model behavior automatically rather than by
remembering to run it locally. Ranked last because it's ~$1.4k against a $0 budget and the context fix
already delivered much of what it promised. Revisit after *Try a newer same-size local model* says
whether a free model swap gets there. **Hardware + a migration pass.**

---

## ✅ Resolved (kept — the lessons still apply)

### Buttons for any yes/no or pick-one decision (2026-07-21)
She asked ~five times for tappable buttons — *"please make it yes or no buttons that i can click"*,
*"if it's a question with multiple concrete options then it should be a button or some sort of
selector"* — and never got one. Two causes: the model had no way to *emit* a button-backed question
(so it wrote prose she typed "yes" at, into the void), and it asked permission for reads that need
none (the calendar).

Reviewed the full Telegram-native surface first (inline keyboards / reply keyboards / polls /
force-reply). **Inline keyboards win**, and the more "native-looking" options are worse: a reply
keyboard's tap arrives as an ordinary *text message*, which reintroduces the exact "model re-derives
intent from words" failure being removed; a poll is survey UI wrong for a 1:1 yes/no.

Generalized the existing approval spine from approve/reject to arbitrary options:
- `confirm.ask_choice(question, options) -> int` + a `{"type":"choice",...}` interrupt payload
  (alongside `approval_request`);
- the channel renders one inline button per option (`callback_data="ans:<idx>"`), and on tap gives a
  toast and rewrites the question to its resolved state ("Which reminder? → ✅ dentist") instead of
  leaving dead buttons; `_on_callback` resumes with the tapped index.

**Two tiers, matching the reliability lesson.** *Deterministic:* `cancel_reminder` with >1 match now
shows a picker and cancels exactly the tapped one — proven end-to-end through the real graph, never
depends on the 7B. *Best-effort:* a general `ask_user(question, options)` tool (always bound — it's in
`CORE`) the model calls instead of writing a discrete-choice question; measured at ~0/2 firing in free
conversation, i.e. a **soft-tier** capability, not a guarantee — the deterministic tier is where the
reliable value is. The calendar-permission complaint is fixed separately, by a persona edit that says
never ask permission to *read* calendar/inbox/memory.

Verified: offline mechanism + tap-through (`tests/test_ask_user.py`, `tests/test_cancel_reminder_choice.py`),
a real-graph interrupt→resume round-trip, and a `verify_scenarios` anchor ("an ambiguous cancel offers
buttons, not a typed question"). Persona changed → full firing gate re-run. See the *Generalizable
per-action approval layer* item, which this advances (mechanism done, policy layer remains).

### The gate was green while the product was broken (2026-07-21)

The full real-model gate passed **twice** on a day when Mochi could not cancel a reminder at all.
Stephanie asked eight times in one conversation, got raw JSON pasted into the chat, was told
"The reminder has been removed" when it had not been, and finally wrote "JESUS" and "STOP IT".
Four distinct defects, none of which any check could see:

**1. Tool selection read only the newest message.** `select_tools` keyed off the last user turn.
She said "yes" — which routes to nothing — so `cancel_reminder` was never bound, the model
physically could not call it, and it emitted the call as *text* (```` ```json {"name": …} ```` )
then asserted success. Every following "yes" re-rolled a fresh irrelevant subset. Measured on her
real phrasings: last-message-only bound the needed tool **1/5**; selecting from the last three
turns is **5/5**, with single-turn accuracy unchanged (6/6) and *fewer* tools bound on average
(7.7 vs 8.0). Fixed in `graph.py` (`TOOL_SELECT_TURNS`).

**2. `cancel_reminder` raised on success.** It committed (expiring the instance), closed the
session, then formatted `cancelled.text` → `DetachedInstanceError`. The write landed and the
confirmation crashed.

**3. `cancel_reminder` couldn't match her phrasing.** Matching was a bare substring test, so "the
dentist reminder" missed the stored "dentist appointment" — the tool's **own docstring example**
failed. It now uses `text_match.same_thing`, the fuzzy matcher this repo already uses for dedup;
ambiguity picks the best content-word overlap and the reply names what it cancelled.

**4. The test suite wrote to the production database.** Tests patched `get_engine` *in the module
under test*; when the `/ask` handlers moved to `telegram_commands` the patch followed, but
`_log_turn`/`_log_one` stayed in `telegram` and resolved the real `.env` value. 72 fixture rows
landed in her live `messagelog` (removed, backed up first).

**Why nothing caught any of it — the part worth keeping:**

- **The gate measures tool *choice*, never tool *execution*.** Every `verify_*` script breaks
  before the tool node runs, deliberately, so that measuring choice can't create a draft or hit
  the network. The unintended consequence is that a tool can be selected perfectly and raise on
  every call forever. → `tests/test_tools_execute.py` now invokes every DB-backed tool for real
  and asserts a non-empty string back; a new tool must be given args or declared external, so it
  cannot silently escape.
- **Every behavioural check was single-turn.** Real use is conversational, and the failure lived
  entirely in turn 2. → `verify_scenarios` gained a deterministic follow-up check (13 checks now),
  plus `tests/test_tool_select_followup.py`.
- **Mocked sessions cannot expire, so unit tests are structurally blind to `DetachedInstanceError`.**
  → `scripts/audit_session_scope.py` finds it statically (both escape routes: assigned-then-used,
  and returned-out-of-block including from a nested helper), runs in the suite and in
  `verify_all.sh`, and was validated by running it against the pre-fix tree, where it flags all
  three historical instances.
- **DB isolation depended on remembering to patch.** → `conftest` now repoints the application's
  default engine before any `app.*` import, with `tests/test_db_isolation.py` asserting it.

**The meta-lesson, which is the same one as the saturated prompt set:** a green gate means "the
things I thought to check still work". All three of this week's escapes — the context window, the
tool-count wall, and this — were cases where the *measurement* was the defect. When something
reaches her that the gate passed, the fix is not only the bug; it's the missing class of check.

### Repo-wide cleanup: lint config, the channel split, and a bug it exposed (2026-07-21)

**Lint was running on ruff's defaults** (`E4,E7,E9,F`) — no import sorting, no dead arguments, no
modernization. Committed a real rule set (`I,UP,B,SIM,C4,PERF,PIE,RET,ARG,RUF`, minus the ambiguous-
unicode rules that would flag the deliberate en dashes) and fixed all **76** findings, including a
duplicate `"your"` in the reminder-dedup stopword set, 28 `datetime.timezone.utc` → `datetime.UTC`,
and 9 `(str, Enum)` → `StrEnum`. The enum change touches values persisted to Postgres, so it was
verified by round-tripping `Fact`/`Goal`/`Task`/`Reminder` and reading the **raw** column values back:
unchanged.

**Method lesson — don't disable a noisy rule, look at where it fires.** The first plan dropped `ARG`
entirely on a count of 180 hits. Grouping by directory showed **172 were in `tests/`** (stub
signatures that must match what they replace) and only 8 in real code, all worth fixing. The answer
was a `per-file-ignores` entry, not a global exclusion. Related: one "fix" — renaming an unused
`chat_id` arg to `_chat_id` in a verify stub — would have **broken it at runtime**, because the
caller passes that parameter by keyword. Underscore-renaming is only safe for positional args.

**`telegram.py` was split** into `telegram_stream` / `telegram_commands` / `telegram_buttons` mixins
over a slimmed core, reversing the earlier "deliberately not done" call (Stephanie's decision). What
made it more than relocation: a `ChannelContract` Protocol in `channels/base.py` now states what the
mixins may assume about each other, the three `[value]` single-element-list mutable cells in
`_run_with_status` became `nonlocal`, `COMMANDS` became data so `run()` can't drift from what's
implemented, and the duplicated user/assistant logging pair collapsed into `_log_turn`.

**The split immediately paid for itself by exposing a live bug.** `telegram_buttons.py` came out at
**21%** coverage — invisible while it shared a 670-line file with the well-tested streaming engine.
Writing tests for it found that **pressing "⏰ Snooze" was broken**: the handler returned an ORM object
from a closed session and then formatted `reminder.due_at`, raising `DetachedInstanceError`. The
snooze was *written* and the confirmation *crashed*, so from her phone it looked like nothing
happened. Confirmed pre-existing (code byte-identical to HEAD; reproduced through untouched modules).
Fixed by reading the value inside the session — the pattern `_on_signal_button` already used.

Coverage of `app/channels/` went **64% → 91%** (whole app **82% → 88%**); the suite went **205 → 232**
tests. The split is what made that reachable: as one 670-line file it was a single 61% number, and the
per-concern breakdown showed exactly where to aim (`telegram_buttons` 21%, `telegram_commands` 59%).

**One more isolation bug fell out of it.** A new test asserting `/sent` reports "nothing has left the
machine" failed against a real `HostedConsult` row — written by `verify_phase4b` minutes earlier.
`conftest.clean_tables` truncated only *after* each test, so the first test of a run inherited whatever
the verify scripts left in the shared scratch DB. Now truncates before and after.

**Docs corrected where they contradicted the code:** `CLAUDE.md`'s repo-layout section was still a
Phase 0 snapshot telling the next session to *create* `app/memory/`, `app/proactive/`, `app/builder/`
et al — all shipped — and its "locked decisions" still claimed integrations go through MCP servers,
when there is no MCP code in `app/` and Phase 2 deliberately went direct. A comment in the streaming
code still said formatting only happened on the final edit, which stopped being true when mid-stream
formatting shipped.

**Future-work restructure:** merged the two memory items (one was explicitly a prerequisite of the
other) and the two tracing items; split the CI item so the free coverage floor stopped inheriting the
priority of a half that needs $1.4k of hardware; and reordered around **trust and habit** rather than
capability, since usage collapsed 23 messages/day → 2 → silence. Task-retirement and the liveness
heartbeat moved up; the email scanner moved *below* task-retirement, because re-enabling noisy
proactivity before "that's over" exists would repeat the failure that ended her usage.
**Every cross-reference is now by name** — two more `#N` references had already rotted into pointing
at unrelated entries.

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
