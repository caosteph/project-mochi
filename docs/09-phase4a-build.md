# Phase 4A — Sensitivity router + de-identified hosted delegation (Build Steps)

**Goal of this phase:** stand up the project's #1 principle — **privacy-first routing by data origin**
— as real code, and give it a first live use that lets a stronger (free) hosted model help *without*
Stephanie's raw personal data leaving the machine.

Today the local half exists (everything is hard-wired local). 4A adds the **deterministic router**
(local vs hosted, by origin, fails closed, `LOCAL_ONLY`-overridable) and the **de-identified hybrid**
Stephanie chose: the local agent asks a stronger model a *de-identified* question via `consult_expert`,
a deterministic scrubber hard-blocks known identifiers before anything leaves, the hosted model (no
tools) answers, the local agent re-personalizes — and every hosted call is **audited** (`/sent`).

**Milestone (definition of done):** `route(SENSITIVE)` is always local and `route(NON_SENSITIVE)` is
hosted **only** when hosting is enabled+configured **and** `LOCAL_ONLY` is off (else local — fails
closed); `consult_expert` never sends known identifiers (deterministic scrub), refuses PII-dense
questions, audits what it sends, and returns "answer locally" when hosting is off; `/ask` answers a
generic question with **no** memory/Google touched; the main agent + quarantined reader stay local even
with hosting on. **Verified without a phone** (`tests/test_router|sanitize|expert_tool.py`, 15 tests)
and **against the real 7B** (`scripts/verify_phase4a.py`: routing table, deterministic scrub 100%,
measured de-identification 5/5, local `/ask` round-trip).

---

## The honest safety model (this touches a hard rule)

A **deliberate, Stephanie-authorized, scoped modification** of "personal data → local only" (per the
constitution's change process):
- **Guaranteed (deterministic):** raw personal data never goes to hosted; a scrubber redacts known
  identifiers (name/email/phone/etc. + PII regexes) from everything outbound; `LOCAL_ONLY` or
  hosted-off ⇒ nothing leaves; a PII-dense question is refused (fails closed to local); the hosted
  model has no tools.
- **Best-effort (model-mediated):** the local 7B phrasing a genuinely de-identified question. A
  non-PII-but-sensitive phrase it fails to generalize could reach hosted — bounded by the scrubber and
  made transparent by the audit log. Measured in verify (not assumed).
- **Default posture unchanged:** hosting is opt-in and off (`LOCAL_ONLY=true`); 4A ships fully working
  local, every guarantee testable in that state.

---

## Steps

### Step 1 — Config (`app/config.py`, `.env.example`)
`hosted_enabled` (default False), `hosted_base_url/model/api_key` (None), `redact_terms` (comma-sep
identifiers to always redact), `redact_max_hits` (too-personal threshold). `.env.example` gains a
**free-provider** block (Groq/OpenRouter/Cerebras — open-weight, OpenAI-compatible) + the honest note.

### Step 2 — Router (`app/agent/router.py`)
`Sensitivity` enum; `hosted_available()` (True only if enabled **and** not `local_only` **and** all
three hosted fields set — else False, fails closed); `chat_model(sensitivity, *, temperature, tools)`
— the single authority: NON_SENSITIVE + available → hosted, **everything else → local**.

### Step 3 — Deterministic scrubber (`app/agent/sanitize.py`)
`redact(text)->(clean, n_hits)` — configured terms (case-insensitive, longest-first) + structural PII
regexes (email/phone/SSN/card; SSN & card before the looser phone pattern). `is_too_personal(n_hits)`
— past the threshold ⇒ don't delegate. Pure, deterministic, 100% on known patterns.

### Step 4 — `consult_expert` tool (`app/agent/tools/expert_tools.py`) + `/ask`/`/sent` (`telegram.py`)
Tool gates, in order: `hosted_available()` → `rate_limit.allow` → `redact` → `is_too_personal` →
hosted `invoke` → **audit** (`HostedConsult`) → return prefixed answer. Any gate failing returns a
fail-closed "answer locally" string. Registered in `ALL_TOOLS` (now 11 tools). Persona gains a short
"de-identify first, re-personalize the answer" note. `/ask <q>` is the pure-generic path (no memory/
Google; scrub + audit only when it actually goes hosted); `/sent` lists the audit log.

### Step 5 — Route existing models through the router (`app/agent/graph.py`)
`_llm` / `_summarizer_llm` are now built via `router.chat_model(SENSITIVE, ...)` — same behavior today
(local), but "main agent = local" is enforced in one auditable place. The quarantined reader stays
hard-wired local (an even stronger guarantee).

### Step 6 — Audit table (`app/memory/models.py`, `app/memory/db.py`)
`HostedConsult(sent_text, answer, n_redactions, created_at)` — new table via `create_all`. Added to
the test-suite TRUNCATE list.

### Step 7 — Constitution + CLAUDE.md
The "private data → local" rule rewritten as the scoped modification; router marked done (P4A);
CLAUDE.md status + safety-rule #1 updated (the one hard rule consciously scoped, with her say-so).

---

## Verify (no phone)

- **`PYTHONPATH=. uv run pytest tests/test_router.py tests/test_sanitize.py tests/test_expert_tool.py -v`**
  (15 tests): full routing table incl. `LOCAL_ONLY` override, fails-closed on misconfig, **sensitive +
  reader always local even with hosting on**, tool-binding; scrubber 100% on terms + each PII regex,
  clean text untouched, too-personal threshold; `consult_expert` fail-closed when hosted off (no model
  call, no audit), scrub-before-send + audit when on, refusal of PII-dense questions; `/ask` builds a
  generic-only prompt (asserted: exactly `[system, question]`, no memory).
- **`PYTHONPATH=. uv run python scripts/verify_phase4a.py`** (needs Ollama; hosted optional): routing
  table live, deterministic scrub 100%, **measured** local de-identification rate (floor 60% — 5/5 in
  practice), a real local `/ask` round-trip, and an optional real hosted round-trip + live invariant
  check when opted in.
- **Regression:** full `pytest -q` green (84); `verify_phase1/2/3` + `verify_phase3b` still pass (the
  graph now builds its models via the router — proves no behavior change).
- **Live check (transport only):** `/ask <generic question>` replies; with hosting configured,
  `consult_expert` gets used on a hard question and `/sent` shows exactly what (de-identified) left.

---

## Going live with a free hosted model (user setup)

Fill `.env`: pick a free OpenAI-compatible open-weight endpoint — **Groq** (`https://api.groq.com/
openai/v1`, e.g. `llama-3.3-70b-versatile`), **OpenRouter** (`:free` models), or **Cerebras** — set
`HOSTED_BASE_URL/HOSTED_MODEL/HOSTED_API_KEY`, `HOSTED_ENABLED=true`, `LOCAL_ONLY=false`, and
`REDACT_TERMS=<your name, email, aliases>`. Until then everything runs local.

## What Phase 4A deliberately does NOT do (later)
- No builder / code sandbox (**4B** — the router's next consumer).
- No auto-routing of normal conversation to hosted; no fact-level `shareable` tagging (Phase 5 could
  sharpen the scrubber). No streaming for `/ask`. No `gmail.send` / outbound tools (never by default).

## Suggested commit
```bash
git add app/ tests/ scripts/verify_phase4a.py docs/09-phase4a-build.md docs/04-constitution.md \
        CLAUDE.md .env.example
git commit -m "Phase 4A: sensitivity router + de-identified hosted delegation"
```
