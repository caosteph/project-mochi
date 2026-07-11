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
| Private data (Gmail/Cal/Drive/memory) → local model + local embeddings only | code | sensitivity router, fail-closed + `LOCAL_ONLY`; embeddings always local | ▢ planned (P4); embeddings ✅ done (P1, `app/memory/embeddings.py`) |
| Never send email — draft only | code | Gmail OAuth scope (`readonly` + `compose`, no `gmail.send`); no send tool registered | ▢ planned (P2) |
| Confirm before any side-effectful / external action | code | LangGraph `interrupt()` human-in-the-loop gate | ▢ planned (P2–3) |
| Untrusted content is data, not instructions | code + prompt | quarantined reader (no tools, structured output) + prompt reminder | ▢ planned (P3) |
| No destructive deletes / permission or setting changes | code | no such tools registered | ◐ ongoing |
| Rate limits + hard cap on outbound actions | code | limiter + anomaly halt | ▢ planned |
| Immutable rules are not learnable | code | procedural memory cannot edit this constitution | ▢ design note (P5) |
| Personality / voice + soft operating principles | prompt | `app/agent/persona.md` | ◐ this change |

Status key: ✅ done · ◐ ongoing/partial · ▢ planned (phase noted).

## How to change a rule

- **Soft (voice/behavior):** edit `app/agent/persona.md` and commit. It's versioned so vibe changes
  are reviewable, and it stays consistent across channels (Telegram now, iMessage later).
- **Hard (a guarantee):** it changes only by editing the enforcing code *and* this table, in the
  same change, with Stephanie's explicit say-so. A hard rule is never weakened silently or by the
  model.
