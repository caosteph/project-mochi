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


def is_too_personal(n_hits: int) -> bool:
    """True if a query required so many redactions it's clearly personal — a signal to
    NOT delegate it to the hosted model and answer locally instead (fails closed)."""
    return n_hits > settings.redact_max_hits
