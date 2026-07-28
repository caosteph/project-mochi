"""Deterministic identifier scrubber — the HARD backstop for de-identified hosted
delegation (Phase 4A). The local model is asked to phrase a generic, de-identified
question before it ever reaches `consult_expert`; this module then guarantees, in code,
that known identifiers can't slip through regardless of what the model produced.

It is deliberately conservative and deterministic (no model): known terms from
`settings.redact_terms` (name/email/aliases) plus structural PII regexes. A query that
needs *many* redactions is treated as inherently personal → not safe to delegate
(`is_too_personal` → answer locally). This is a best-effort de-identification aid, not a
guarantee that all *semantically* sensitive content is removed — see docs/04-constitution.md.
"""

import re

from app.config import settings

_PLACEHOLDER = "[redacted]"

# Structural PII. Order matters: SSN and card patterns run before the looser phone
# pattern so they aren't partially eaten by it.
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CARD = re.compile(r"\b(?:\d[ -]?){13,16}\b")
_PHONE = re.compile(r"(?<![\w.])\+?\d(?:[\d\s().-]{7,})\d(?![\w.])")
_PII_REGEXES = (_EMAIL, _SSN, _CARD, _PHONE)

# High-risk / catastrophic identifiers (SSN, credit card): a SINGLE one is enough to refuse
# egress regardless of the total redaction count — PII risk is categorical, not linear (one SSN
# outweighs five first names). Detected per-category in has_high_risk_pii() below (SSN by format,
# card by Luhn). Extension point for bank/account + passport once low-false-positive regexes exist.


def _terms() -> list[str]:
    return [t.strip() for t in settings.redact_terms.split(",") if t.strip()]


def redact(text: str) -> tuple[str, int]:
    """Return (scrubbed_text, number_of_redactions). Replaces every configured term
    (case-insensitive) and every structural-PII match with a placeholder."""
    out = text
    hits = 0
    # Longest terms first so a longer identifier isn't partially clobbered by a shorter one.
    for term in sorted(_terms(), key=len, reverse=True):
        out, n = re.compile(re.escape(term), re.IGNORECASE).subn(_PLACEHOLDER, out)
        hits += n
    for rx in _PII_REGEXES:
        out, n = rx.subn(_PLACEHOLDER, out)
        hits += n
    return out, hits


def _luhn(digits: str) -> bool:
    """Luhn checksum. A real credit-card number passes by construction, so gating the card
    pattern on this only ever removes FALSE positives (random 13-16 digit runs like order or
    tracking numbers) — it never drops a genuine card, so the egress floor is unweakened."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return bool(digits) and total % 10 == 0


def has_high_risk_pii(text: str) -> bool:
    """True if `text` contains any high-risk identifier: an SSN (by format), or a credit-card
    number whose digits pass the Luhn checksum. Pure and deterministic — no model. Run on the
    ORIGINAL text (before `redact`), since a scrubbed copy has already had these replaced with
    the placeholder (which is exactly why callers must pass the original — see is_too_personal)."""
    if _SSN.search(text):
        return True
    return any(_luhn(re.sub(r"\D", "", m.group())) for m in _CARD.finditer(text))


def is_too_personal(text: str, n_hits: int) -> bool:
    """True if a query is too sensitive to delegate to the hosted model — answer locally
    instead (fails closed). Two independent triggers, either one refuses:

    - a single HIGH-RISK identifier (SSN/card) is present, regardless of count — PII risk is
      categorical, not linear; and
    - the flat redaction count is high enough that the query is clearly personal.

    `text` is the original (pre-redaction) query; `n_hits` is `redact()`'s count."""
    return has_high_risk_pii(text) or n_hits > settings.redact_max_hits
