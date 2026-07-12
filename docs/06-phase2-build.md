# Phase 2 — Google (Calendar + Gmail) & the Approval Gate (Build Steps)

**Goal of this phase:** connect Mochi to Stephanie's real Google account — read her Calendar,
triage her inbox by metadata, and **draft** (never send) emails — and stand up the
**human-in-the-loop approval gate** that pauses every external write for her explicit yes/no. This
is the phase where the agent first touches the outside world, so the safety machinery matters as
much as the features.

**Two decisions shape this phase (see `docs/00-plan.md` history):**
1. **Direct Google Python API, not MCP.** The roadmap said "off-the-shelf MCP servers," but the
   real 2026 landscape is: Google's official MCP servers are *remote/hosted* (not the local stdio
   servers the plan assumed) and possibly Developer-Preview-gated; community stdio servers require
   trusting third-party code with your OAuth tokens. For a security-sensitive personal agent, a
   thin direct `google-api-python-client` integration gives the most control, the most local
   posture, precise minimal scopes, and local token storage — no Node, no remote-MCP dependency.
   MCP remains the plan for Phase 7 (Drive) when breadth justifies it.
2. **Email = metadata + fresh drafts only.** Safety rule #4 (the privileged agent never ingests
   raw untrusted email *bodies*) is upheld by *not reading bodies* this phase. Mochi sees
   sender/subject/date for triage and composes fresh drafts from Stephanie's instructions. The
   quarantined-reader / dual-LLM pattern that safely parses bodies lands in **Phase 3** (receipt
   parsing), where it's already core.

**Milestone (definition of done):**
1. Ask "what's on my calendar tomorrow?" → Mochi answers from the real Calendar, all local.
2. Ask "any recent email from X?" → Mochi answers from metadata (sender/subject/date); it can't
   quote bodies and says so.
3. Ask it to draft an email → the proposal appears in Telegram with **Approve / Reject** buttons;
   Approve creates a real Gmail draft (unsent); Reject creates nothing.
4. `scripts/verify_phase2.py` passes (or prints setup steps if OAuth isn't configured yet), and the
   deterministic gate/service tests are green.

**Est. time:** a focused day, plus ~20 min of one-time Google Cloud console setup.

---

## Overview

```
Telegram ─▶ agent (7 tools) ─▶ tools_condition ─▶ tools (ToolNode)
                                                     │
        calendar_list_events / gmail_list_recent ────┤ (read: run, return)
                                                     │
        create_draft ── require_approval() ── interrupt() ──▶ graph PAUSES
                                                     │              │
   result carries __interrupt__ ◀──────────────────┘              │
        │                                                          │
   Telegram sends [✅ Approve] [❌ Reject] ──▶ Command(resume={approved}) ─▶ resumes here
                                                     │
                            approved? ─ yes ─▶ google_gmail.create_draft() (real, unsent)
                                       └─ no  ─▶ "cancelled", nothing written

app/integrations/google_auth.py ─▶ google-api-python-client ─▶ Google (readonly + compose only)
```

---

## Step 1 — Dependencies

```bash
cd ~/personal-agent
uv add google-api-python-client google-auth-oauthlib
```
(`google-auth` comes transitively.) No Node, no `langchain-mcp-adapters`, no MCP server processes.

---

## Step 2 — Google Cloud setup (one-time, manual — like the BotFather steps in Phase 0)

This is Stephanie's part; it ends with a `client_secret` file in `data/`.

1. **Project:** [console.cloud.google.com](https://console.cloud.google.com) → new project (e.g.
   `mochi-agent`), select it.
2. **Enable APIs:** APIs & Services → Library → enable **Gmail API** and **Google Calendar API**.
3. **OAuth consent screen:** External → app name `Mochi`, your email as support/developer contact.
   Skip adding scopes (requested at runtime). **Add your @gmail.com as a Test user.** Leave
   publishing status **Testing**.
4. **Credential:** Credentials → Create Credentials → OAuth client ID → **Desktop app** → Download
   JSON.
5. **Place it:** save the file to `data/google_client_secret.json` (git-ignored).

The first app run after this opens a browser once for consent (read email/calendar + create
drafts — never send).

> **Honest limitation (documented, not hidden):** an unverified app in "Testing" status with
> *sensitive* Gmail scopes issues refresh tokens that expire ~7 days, so periodic re-consent is
> expected on a personal @gmail.com. App verification is a heavy process not worth it for one user;
> Phase 9 (the always-on Mac mini) revisits this.

---

## Step 3 — OAuth (`app/integrations/google_auth.py`)

Least-privilege scopes are a **constant here, not env-tunable** — the token literally cannot send
email or write the calendar. Standard desktop `InstalledAppFlow`; token stored locally, `chmod 600`.

```python
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",  # drafts only — never gmail.send
    "https://www.googleapis.com/auth/calendar.readonly",
]

def get_credentials() -> Credentials:
    # load data/google_token.json if present; refresh if expired; else run the
    # one-time run_local_server(port=0) browser consent flow. Save with chmod 600.
```
(Full file in the repo.) `has_token()` is a cheap "is OAuth configured?" check used by the verify
script. Calendar **write** is deliberately absent — added in Phase 3 (reminder→event mirroring) via
a re-consent, so we don't hold authority we don't yet use.

---

## Step 4 — Service wrappers (`app/integrations/google_calendar.py`, `google_gmail.py`)

Thin, and — critically — each function takes an optional pre-built `service=` so it's unit-testable
against a mock with no network or credentials.

- `google_calendar.list_events(start_iso, end_iso, max_results, *, service=None)` → list of
  `{summary, start, end, location}`. Read-only.
- `google_gmail.list_recent_metadata(max_results, query, *, service=None)` → list of
  `{from, subject, date}` — **metadata only**. It requests `format="metadata"` from Gmail (so a
  body isn't even fetched) and extracts *only* those three headers — never `msg["snippet"]`, which
  is body-derived. This is how safety decision #2 becomes code.
- `google_gmail.create_draft(to, subject, body, *, service=None)` → creates an unsent draft,
  returns the resource (`{id: ...}`). There is **no send function anywhere** by construction.
- `google_gmail.delete_draft(draft_id, *, service=None)` → used only by the verify script's cleanup.

---

## Step 5 — The approval gate (`app/agent/confirm.py`)

The reusable mechanism every side-effectful tool routes through. Verified empirically before
writing (the exact `__interrupt__` shape, resume round trip, and side-effect timing):

```python
from langgraph.types import interrupt

def require_approval(action: str, details: dict) -> bool:
    decision = interrupt({"type": "approval_request", "action": action, "details": details})
    if isinstance(decision, dict):
        return bool(decision.get("approved"))
    return bool(decision)
```

**Critical correctness rule:** `interrupt()` re-runs the enclosing node *from the top* on resume,
so a tool MUST call `require_approval()` **before** any side effect and write only after it returns
True. `create_draft` is structured exactly this way; `tests/test_confirm_gate.py` locks it in
(reject → Gmail never touched; approve → written exactly once).

---

## Step 6 — Tools (`app/agent/tools/google_tools.py`) + register

`calendar_list_events` and `gmail_list_recent` just call the wrappers. `create_draft` gates first:

```python
@tool
def create_draft(to: str, subject: str, body: str) -> str:
    """Create a Gmail draft (never sends). Requires explicit approval..."""
    if not require_approval("create_draft", {"to": to, "subject": subject, "body": body}):
        return "Draft cancelled — nothing was created."
    draft = google_gmail.create_draft(to, subject, body)
    return f"Draft created (id {draft.get('id')}). It's in your Gmail, unsent — review and send."
```

Register in `app/agent/tools/__init__.py`: `ALL_TOOLS = [*MEMORY_TOOLS, *GOOGLE_TOOLS]`. **No
graph-shape change is needed** — the existing `ToolNode` + Postgres checkpointer already support
`interrupt()`. Verified: the tool call → interrupt → pause works through the real graph unchanged.

---

## Step 7 — Config + current-time context

`app/config.py` gains `google_client_secret_path` and `google_token_path` (paths only; scopes live
in `google_auth.py`). Separately, `app/agent/graph.py`'s `_agent_node` now prepends the **current
date/time** to the system prompt so the model can resolve "today"/"tomorrow" into RFC3339 ranges for
calendar queries:

```python
now = datetime.now().astimezone()
core = f"{SYSTEM_PROMPT}\n\nCurrent date/time: {now:%A, %Y-%m-%d %H:%M %Z}."
```

---

## Step 8 — Telegram approval UI + live status narration (`app/channels/telegram.py`)

The meatiest new async plumbing. Because the local model is slow (~30–60s/turn), the channel
**streams** the graph (`agent.stream(stream_mode="updates")`) instead of a blocking `invoke`, so it
can narrate what Mochi is doing and preserve the approval flow. The stream is a sync generator, so
it runs on a worker thread that hands events to the async handler via a queue.

- **Live status narration (`_run_with_status`):** a single status message, edited in place through
  the observable phases — `💭 Thinking…` → the tool's status (`📅 Checking your calendar…`,
  `✉️ Drafting that email…`, etc.) → `✍️ Composing your reply…` — plus a `typing…` keepalive between
  steps. The status message is left in the chat as a breadcrumb of the last phase (not deleted).
  Telegram's native status line only allows fixed built-in actions (no custom text), so named
  statuses are ordinary messages. `stream_mode="updates"` surfaces an `{"agent": …}` update with
  `tool_calls` *before* the tool runs (so the status is announced up front) and an interrupt as an
  `{"__interrupt__": …}` update (same payload as `invoke`), so the approval flow is preserved.
- **Approval:** on an interrupt, `_deliver` sends the proposal (to/subject/body) with an inline
  `[✅ Approve] [❌ Reject]` keyboard. A `CallbackQueryHandler` strips the buttons (no double-taps)
  and resumes via `Command(resume={"approved": …})`, which also streams (so post-approval work is
  narrated too). `thread_id` is constant per chat, so the resume resolves the right paused turn.
- **Error surfacing:** a stream exception becomes a visible "⚠️ Something went wrong — mind trying
  again?" instead of a silent no-reply (an actual confusion we hit and fixed).
- **Current time in context:** `_agent_node` prepends the current date/time to the system prompt so
  the model can resolve "today"/"tomorrow" into calendar ranges.

(Full handler in the repo.) This is the foundational approval flow every future external action —
sending, sharing, purchasing — will reuse.

---

## Step 9 — Persona + tool-invocation reliability (`app/agent/persona.md`, `google_tools.py`)

Update "what you can do right now" to the honest new truth (read calendar, email *metadata* only,
draft-not-send). The bigger lesson (learned the hard way — see gotchas): binding the tools isn't
enough; the local 7B often *won't call them* without a forceful, example-driven persona push. The
"Using your Google tools" section mirrors the Phase 1 memory discipline and adds two hard-won rules:

- **Always call `calendar_list_events`/`gmail_list_recent` fresh, every turn — never answer schedule
  or inbox questions from earlier results in the conversation, and never invent events.** Without
  this the model reuses stale context and *hallucinates* calendar entries (it did, in testing).
- **`create_draft` immediately; for a self-draft pass `to="me"`.** The `create_draft` tool resolves
  `me`/`myself`/`self`/empty → her own address via `google_gmail.get_own_address()`, so "draft an
  email to me" works without the model needing to know her address (which was why it stalled with a
  clarifying question before).

This is soft-tier (`[prompt]`) — it lifts reliability (measured create_draft firing from ~1/3 to
~3/3 on varied phrasings), it does **not** guarantee it; a 7B stays probabilistic, and the residual
weak spot is "remind me *again*"-style re-asks that reuse in-context data. The Mac mini's bigger
model is the real fix. `scripts/verify_phase2.py` measures this rate directly (below) so a
regression is caught without a phone.

---

## Step 10 — Verify

Per the standing convention (`CLAUDE.md`): deterministic offline tests + a live driver.

**Offline (`uv run pytest tests/ -v`, under the no-network guard):**
- `test_confirm_gate.py` — the real `create_draft` in a minimal graph with Gmail mocked: interrupt
  fires with the full proposal; reject writes nothing; approve writes exactly once; `to="me"`
  resolves to her real address in the proposal she approves.
- `test_google_services.py` — wrappers against a mocked `service`: field mapping, `format=metadata`
  is requested, email reads expose exactly `{from, subject, date}` (a planted `snippet` never
  leaks), and `get_own_address` reads the profile.
- `test_status_map.py` — every registered tool has a status breadcrumb; unknown tools fall back.

**Live (`scripts/verify_phase2.py`, gated on `data/google_token.json`):** if unconfigured, prints
the Step 2 setup steps and exits 0. If configured: real calendar read, real gmail metadata read
(asserts no body keys), a real draft round-trip (create to *yourself* → confirm in Gmail → delete),
**and a model-driven reliability measurement** — it drives the real graph with several phrasings of
calendar and draft asks and checks the tools actually *fire* (rate-based, ≥60% floor). This last
part is the discipline that was missing the first time: verifying the model's *behavior*, not just
the plumbing. Run:
```bash
DATABASE_URL=postgresql://localhost/personal_agent_test PYTHONPATH=. uv run python scripts/verify_phase2.py
```

**Live human check (last, transport only):** message Mochi "what's on my calendar today?" → see
`💭 Thinking…` → `📅 Checking your calendar…` → `✍️ Composing…` → the *real* events; and "draft an
email to me saying hi" → `✉️ Drafting…` → Approve/Reject → Approve → draft in Gmail (unsent).

---

## Common gotchas

- **`GoogleAuthError: No Google OAuth client secret`** → `data/google_client_secret.json` is
  missing; do Step 2 and confirm the download landed at that exact path.
- **Consent screen "access blocked / app not verified"** → you didn't add your own email as a
  **Test user** on the OAuth consent screen (Step 2.3). Add it.
- **Auth works, then breaks ~a week later** → the ~7-day testing-mode refresh-token expiry. Re-run
  the app to re-consent. Not a bug — a documented limitation of unverified sensitive-scope apps.
- **Model gives a calendar/email answer without calling a tool (and invents events)** → the big one.
  Binding tools doesn't make the model use them; a persistent conversation with earlier calendar
  results makes the 7B reuse/hallucinate instead of re-fetching. Fix is the forceful persona push
  in Step 9 (always call fresh, never invent). It's prompt-tier — measured, not guaranteed; verify
  the rate with `scripts/verify_phase2.py` before assuming a regression. **Lesson: "verified the
  plumbing" ≠ "verified the model's behavior" — measure tool-firing directly, not on the phone.**
- **"Draft an email to me" does nothing / asks "who to?"** → "me" isn't an address the model will
  put in the `to` field, so it stalls. Fixed by `create_draft` resolving `me`/`myself`/`self` →
  `get_own_address()`, plus a persona rule to pass `to="me"` and not ask. If it regresses, check
  both.
- **Turn feels silent / very slow** → the local 7B is genuinely slow (~30–60s), worse on a cold
  model. The live status narration (Step 8) covers the wait; `keep_alive` on Ollama and, ultimately,
  the Mac mini's larger model are the real latency fixes (later).
- **Draft created before approval** → would mean a tool did its side effect before
  `require_approval()`. `interrupt()` re-runs the node from the top, so any pre-interrupt side
  effect runs twice and un-gated. Keep the write strictly after the gate (see Step 5).
- **"OpenAI-compatible endpoint rejects the message sequence"** after an approval on a hosted model
  → unrelated to Phase 2 but note the Phase 1 `_trim_boundary` fix keeps sequences valid across
  endpoint swaps.

---

## What Phase 2 deliberately does NOT do (comes next)

- **No reading email bodies.** Metadata only. The quarantined reader that safely parses bodies is
  **Phase 3** (receipt→return-window). "Draft a *reply* to this email" is intentionally not
  supported yet, because it would require ingesting untrusted body text.
- **No sending, sharing, or deleting anything.** Draft-only; no such tools registered; the token
  lacks the scopes.
- **No calendar writes.** Read-only scope this phase; write arrives in Phase 3 for mirroring
  critical reminders into real Calendar events (via re-consent).
- **No MCP.** Direct API for now; MCP returns in Phase 7 (Drive).
- **No sensitivity router.** `LOCAL_ONLY=true` keeps everything local, so "Google content → local
  model" holds trivially; the deterministic router is Phase 4.
- **No token encryption at rest.** Git-ignored `data/` with `chmod 600` is the Air-phase baseline;
  macOS Keychain is the Phase 9 hardening.

---

## Suggested commit

```bash
cd ~/personal-agent
git add app/integrations/ app/agent/confirm.py app/agent/tools/ app/agent/graph.py \
        app/agent/persona.md app/channels/telegram.py app/config.py pyproject.toml uv.lock \
        tests/ scripts/verify_phase2.py docs/06-phase2-build.md
git commit -m "Phase 2: Google Calendar/Gmail via direct API + human-in-the-loop approval gate"
```
Then flip the relevant `docs/04-constitution.md` rows (Gmail draft-only scope; `interrupt()` gate)
to reflect what's now enforced in code, and update `CLAUDE.md`'s Current status + `README.md`.
