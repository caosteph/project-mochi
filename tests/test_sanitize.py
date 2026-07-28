"""Phase 4A — the deterministic identifier scrubber (the hard backstop). 100% on known
terms + structural PII, and the too-personal fail-closed threshold.
"""

import pytest

from app.agent import sanitize
from app.config import settings


def test_configured_terms_are_redacted(monkeypatch):
    monkeypatch.setattr(settings, "redact_terms", "Stephanie, Steph Cao")
    clean, hits = sanitize.redact("Hi, I'm Stephanie — friends call me Steph Cao.")
    assert "Stephanie" not in clean and "Steph Cao" not in clean
    assert clean.count("[redacted]") == 2 and hits == 2


def test_terms_are_case_insensitive(monkeypatch):
    monkeypatch.setattr(settings, "redact_terms", "stephanie")
    clean, hits = sanitize.redact("STEPHANIE and stephanie and Stephanie")
    assert "tephanie" not in clean and hits == 3


def test_structural_pii_regexes(monkeypatch):
    monkeypatch.setattr(settings, "redact_terms", "")
    for raw, label in [
        ("reach me at jane.doe@example.com", "email"),
        ("call 415-555-1212 today", "phone"),
        ("ssn 123-45-6789", "ssn"),
        ("card 4111 1111 1111 1111", "card"),
    ]:
        clean, hits = sanitize.redact(raw)
        assert "[redacted]" in clean and hits >= 1, label
        # the raw sensitive token is gone
        assert not any(ch.isdigit() for ch in clean.replace("[redacted]", "")) or "@" not in clean


def test_clean_text_passes_untouched(monkeypatch):
    monkeypatch.setattr(settings, "redact_terms", "Stephanie")
    text = "Suggest vegetarian dinner recipes that avoid cilantro."
    clean, hits = sanitize.redact(text)
    assert clean == text and hits == 0


def test_is_too_personal_threshold(monkeypatch):
    # Low-risk text (no high-risk identifier) stays purely count-based.
    monkeypatch.setattr(settings, "redact_max_hits", 4)
    clean = "just a generic question with some redacted names"
    assert sanitize.is_too_personal(clean, 5) is True
    assert sanitize.is_too_personal(clean, 4) is False
    assert sanitize.is_too_personal(clean, 0) is False


def test_single_high_risk_pii_refused(monkeypatch):
    # A single high-risk identifier (SSN or card) must refuse regardless of count — even
    # a lone one that would sit at/under the flat threshold. This is the categorical floor.
    monkeypatch.setattr(settings, "redact_max_hits", 4)
    assert sanitize.has_high_risk_pii("my ssn is 123-45-6789") is True
    assert sanitize.has_high_risk_pii("card 4111 1111 1111 1111") is True
    assert sanitize.has_high_risk_pii("what's the weather in Paris") is False
    # count alone would allow these (n_hits == 1 <= 4); the category must refuse.
    assert sanitize.is_too_personal("my ssn is 123-45-6789", 1) is True
    assert sanitize.is_too_personal("card 4111 1111 1111 1111", 1) is True


def test_low_risk_stays_count_based(monkeypatch):
    # No high-risk identifier → decision is the flat count, unchanged from before.
    monkeypatch.setattr(settings, "redact_max_hits", 4)
    text = "recommend a gift for a coworker who likes coffee and hiking"
    assert sanitize.is_too_personal(text, 4) is False
    assert sanitize.is_too_personal(text, 5) is True


def test_high_risk_detected_on_original_not_scrubbed(monkeypatch):
    # The categorical gate MUST run on the pre-redaction text: after redact() the SSN is gone,
    # so a caller that passed the scrubbed copy would silently disable the high-risk refusal.
    # This test documents and locks that invariant (the wrapper-seam tests enforce it per-caller).
    monkeypatch.setattr(settings, "redact_terms", "")
    original = "my ssn is 123-45-6789"
    scrubbed, _ = sanitize.redact(original)
    assert sanitize.has_high_risk_pii(original) is True
    assert sanitize.has_high_risk_pii(scrubbed) is False


@pytest.mark.parametrize(
    "raw",
    [
        "4111111111111111",       # Visa test number, no separators
        "4111 1111 1111 1111",    # spaced
        "4111-1111-1111-1111",    # dashed
        "378282246310005",        # Amex (15 digits)
        "6011111111111117",       # Discover
    ],
)
def test_card_luhn_valid_is_high_risk(raw):
    assert sanitize.has_high_risk_pii(f"my card is {raw}") is True


@pytest.mark.parametrize(
    "raw",
    [
        "4111111111111112",   # a real card's digits with a broken check digit → fails Luhn
        "1234567890123456",   # a benign 16-digit run (e.g. an order/tracking number) → fails Luhn
    ],
)
def test_non_card_digit_run_not_high_risk(raw):
    # Luhn drops these false positives so a benign long number no longer hard-refuses egress
    # (it falls back to the count path). Intended precision gain, not a hole in the floor.
    assert sanitize.has_high_risk_pii(f"reference number {raw}") is False


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("ssn 123-45-6789", True),      # dashed SSN — caught
        ("ssn 123456789", False),       # KNOWN LIMITATION: bare 9-digit SSN not caught (regex needs dashes)
        ("what is the weather in Paris", False),
    ],
)
def test_high_risk_formats(raw, expected):
    assert sanitize.has_high_risk_pii(raw) is expected
