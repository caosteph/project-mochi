# 04 — Mochi's Constitution (the rule list)

This is the single, auditable catalog of the rules Mochi operates under. Its job is to make
one distinction impossible to miss: **which rules are *guaranteed* vs which are merely
*encouraged*.**

## The two-tier rule model

Rules split into two tiers with very different enforcement — and **"always" only holds for one
of them:**

- **Soft tier — `[prompt]`.** Personality/voice and behavioral defaults, expressed in
  `app/agent/persona.md` and fed to the model as a system prompt. The local 7–8B model *usually*
  follows these, but a weak model or a cleverly worded (or prompt-injected) message can talk it out
  of prompt text. Good enough for *style*; **not** something to rely on for safety.
- **Hard tier — `[code]`.** Safety and privacy invariants enforced in deterministic code *outside*
  the model — tool allow-lists, OAuth scopes, the chat_id whitelist, the `interrupt()` gate, the
  sensitivity router. These hold **regardless** of what the model "decides" or what any incoming
  content says. These are the ones that are *actually* always followed.

Design corollaries:
- **Personality applies to the privileged agent only.** The Phase 3 quarantined reader that parses
  untrusted email/web/Drive content stays persona-free and tool-free — a voice on the
  untrusted-content parser would just be another injection surface.
- **Immutable rules are not learnable.** Phase 5 procedural memory may adapt *preferences*; it must
  never be able to edit a hard rule or this constitution.
- When a rule *must* hold, it belongs in the hard tier. If it's only in the prompt, treat it as a
  strong default, not a guarantee.

## The rules

| Rule | Tier | Enforced where | Status |
|------|------|----------------|--------|
| Only respond to the whitelisted `chat_id` | code | `channels/telegram.py` whitelist | ✅ done (P0) |
| Private data (Gmail/Cal/Drive/memory) → local model + local embeddings only | code | deterministic sensitivity router (`app/agent/router.py`): SENSITIVE→local always, fails closed, `LOCAL_ONLY` overrides; embeddings always local. **Scoped (P4A, with Stephanie's say-so):** only *de-identified, PII-scrubbed, audited* derivatives may reach an opt-in **free** hosted model (`consult_expert`/`/ask`) or search provider (a scrubbed query for `web_search`, P8) — raw personal data never leaves; see the scoped-modification notes below | ◐ done (P4A router + P8 web search live); embeddings ✅ done (P1, `app/memory/embeddings.py`) |
| Never send email — draft only | code | Gmail OAuth scope (`readonly` + `compose`, no `gmail.send` — `app/integrations/google_auth.py`); no send tool registered | ✅ done (P2) |
| Confirm before any side-effectful / external action | code | LangGraph `interrupt()` gate — `app/agent/confirm.py`, wired through Telegram Approve/Reject (renderer is per-action, `_render_proposal`) | ✅ done (P2, gating draft writes; P8 also gates `web_search` queries). **Scoped exception (P3A):** mirroring a reminder into an event on *her own* calendar is create-only, background (no `interrupt()` applies), and not third-party/destructive/outbound — so it is opt-in via `calendar_mirror_enabled`, not per-event gated. A deliberate written scoping, not a silent weakening. |
| Untrusted content is data, not instructions | code + prompt | quarantined reader for email *bodies* (`app/agent/quarantine.py`: separate local model, **no tools**, no persona, `json_schema` structured output, length-capped fields, raw body never persisted/logged or seen by the privileged agent); plus P3A framing of subjects/senders + calendar titles (`frame_untrusted`) | ✅ done for email bodies (P3B); framing ◐ ongoing for other surfaces (web/Drive later) |
| Proactive messages bounded (quiet hours, dedup, kill-switch) | code | `app/proactive/` — quiet-hours skip, status-based dedup (exactly-once), `/pause` `/resume` runtime flag; proactive sends only to the whitelisted chat | ✅ done (P3A) |
| No destructive deletes / permission or setting changes | code | no such tools registered (note: `calendar.events` scope *can* delete, but only `create_event` is ever called, by the engine, not the agent) | ◐ ongoing |
| Agent-generated code runs sandboxed, no secret access | code | `app/builder/sandbox.py` `SubprocessSandbox`: **scrubbed env** (hard — no `.env`/DB/token/keys in the child), cwd jailed to `workspace/`, timeout, best-effort `sandbox-exec` **deny** of `data/`+`.env` reads (verified). Honest residual: network open + non-denied files otherwise readable until the `DockerSandbox` drop-in (Mac mini) | ◐ done (P4B step 1; full isolation via Docker later) |
| Rate limits + hard cap on outbound actions | code | per-action rolling hourly cap (`app/agent/rate_limit.py`), gating `create_draft`/`add_reminder`; per-turn calls also bounded by LangGraph `recursion_limit` | ✅ done (P3A, `max_actions_per_hour`) |
| Immutable rules are not learnable | code | procedural memory cannot edit this constitution | ▢ design note (P5) |
| Personality / voice + soft operating principles | prompt | `app/agent/persona.md` | ◐ this change |

Status key: ✅ done · ◐ ongoing/partial · ▢ planned (phase noted).

## Scoped modification — de-identified hosted delegation (P4A)

The "private data → local only" rule was **deliberately scoped** in Phase 4A, with Stephanie's
explicit say-so (the required process for a hard-tier change: edit the enforcing code *and* this
table together). The scope:

- **Still guaranteed in code:** *raw* personal data never leaves the machine. A deterministic scrubber
  (`app/agent/sanitize.py`) hard-redacts known identifiers (name/email/phone + PII regexes) from
  everything sent to the opt-in hosted model; `LOCAL_ONLY` or hosted-off means nothing is sent at all;
  a PII-dense question is refused and answered locally (fails closed); the hosted model has no tools;
  every hosted call is audited (`HostedConsult` → `/sent`), so nothing is silent.
- **Best-effort, explicitly not guaranteed:** the local model producing a genuinely de-identified
  question. A non-PII-but-sensitive phrase it fails to generalize could reach the hosted provider.
  This is the residual risk Stephanie accepted in choosing the hybrid; it is bounded by the scrubber
  and surfaced by the audit log, and its rate is *measured* (`scripts/verify_phase4a.py`), not assumed.
- **Default is unchanged:** hosting is opt-in and off (`LOCAL_ONLY=true`); with it off, the system is
  exactly as local as before.

This is a written, auditable scoping — not a silent weakening. Reverting is a one-line switch
(`HOSTED_ENABLED=false` / `LOCAL_ONLY=true`).

## Scoped modification — web search (P8)

Web search (`web_search`, `app/agent/tools/web_tools.py`) sends a query to a search provider. It
follows the **same** scrub/refuse/audit spine as the hosted consult, plus a human approval:

- **Still guaranteed in code:** only a **scrubbed** query leaves — `sanitize.redact` strips known
  identifiers + PII before the provider call; a PII-dense query is refused and answered locally (fails
  closed); the query is **approved by Stephanie** (`require_approval("web_search", …)`) before it runs,
  so she previews exactly what leaves; every query is audited (`WebSearch` → `/sent`); results are
  **untrusted web content**, framed as data and read/synthesized by the **local** model.
- **Deliberately independent of `LOCAL_ONLY`:** that flag governs the hosted *LLM* for personal data.
  A scrubbed, generic search query is a smaller, separate externality, so web search has its own opt-in
  (`WEB_SEARCH_ENABLED` + a provider/key). This is the one place the "LOCAL_ONLY forces *everything*
  local" phrasing is scoped — written here, with Stephanie's say-so, not silently.
- **Best-effort, not guaranteed:** as with the hosted consult, a non-PII-but-sensitive phrase the
  scrubber doesn't catch could reach the provider. Bounded by the scrubber + approval + audit; the
  provider is pluggable (Tavily, or keyless DuckDuckGo, or a self-hosted SearXNG later for full-local).

Reverting is a one-line switch (`WEB_SEARCH_ENABLED=false`).

## How to change a rule

- **Soft (voice/behavior):** edit `app/agent/persona.md` and commit. It's versioned so vibe changes
  are reviewable, and it stays consistent across channels (Telegram now, iMessage later).
- **Hard (a guarantee):** it changes only by editing the enforcing code *and* this table, in the
  same change, with Stephanie's explicit say-so. A hard rule is never weakened silently or by the
  model.
