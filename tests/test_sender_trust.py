"""Phase #2 — the deterministic sender-trust classifier. Pure (no model, no network): the From header
and Gmail's Authentication-Results are envelope metadata a malicious body can't forge, and they decide
how confidently a bill/deadline/return nudge is phrased.
"""

import pytest

from app.config import settings
from app.proactive import sender_trust


@pytest.fixture(autouse=True)
def _allowlist(monkeypatch):
    monkeypatch.setattr(settings, "trusted_sender_domains", "chase.com, pge.com")


PASS = "spf=pass dkim=pass dmarc=pass"


def test_allowlisted_domain_is_trusted():
    assert sender_trust.classify("Chase <alerts@chase.com>", PASS) == "trusted"


def test_subdomain_of_allowlisted_is_trusted():
    assert sender_trust.classify("Chase <no-reply@email.chase.com>", PASS) == "trusted"


def test_missing_auth_header_does_not_downgrade_allowlisted():
    # Many legit senders omit some auth methods; a missing header is not a failure.
    assert sender_trust.classify("PG&E <billing@pge.com>", None) == "trusted"


def test_display_name_impersonation_is_suspicious():
    # Name-drops "Chase" but the real domain is evil.com — the classic display-name spoof.
    assert sender_trust.classify("Chase Support <no-reply@evil.com>", PASS) == "suspicious"


def test_auth_failure_is_suspicious():
    assert sender_trust.classify("Billing <billing@randomvendor.com>", "spf=fail dkim=none") == "suspicious"


def test_spoof_beats_allowlist():
    # An auth failure from an allowlisted-looking domain is still suspicious (spoof wins).
    assert sender_trust.classify("Chase <alerts@chase.com>", "dmarc=fail") == "suspicious"


def test_normal_unlisted_sender_is_unknown():
    assert sender_trust.classify("A Friend <friend@gmail.com>", PASS) == "unknown"


def test_unparseable_sender_is_unknown():
    assert sender_trust.classify(None, None) == "unknown"
    assert sender_trust.classify("", None) == "unknown"
