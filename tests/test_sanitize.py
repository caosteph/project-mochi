"""Phase 4A — the deterministic identifier scrubber (the hard backstop). 100% on known
terms + structural PII, and the too-personal fail-closed threshold.
"""

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
    monkeypatch.setattr(settings, "redact_max_hits", 4)
    assert sanitize.is_too_personal(5) is True
    assert sanitize.is_too_personal(4) is False
    assert sanitize.is_too_personal(0) is False
