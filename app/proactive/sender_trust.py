"""Sender-trust classification (Phase #2) — a deterministic input to how a proactive nudge is
PHRASED, kept separate from whether a signal is DETECTED.

The threat isn't prompt injection (the quarantined reader already defends the agent). It's the
agent being used as a high-trust delivery channel to Stephanie: a phishing "bill" that Mochi
surfaces as "💸 … want a reminder to pay it?" borrows the assistant's credibility. So before a
bill/deadline/return nudge is phrased as an action prompt, we classify the sender:

  - "trusted"    — the from-domain (or a subdomain) is on the configured allowlist AND auth passes;
  - "suspicious" — a spoof signal: the display name name-drops a listed brand from a DIFFERENT
                   domain, or SPF/DKIM/DMARC failed;
  - "unknown"    — everyone else (a normal, unlisted sender).

All inputs are ENVELOPE metadata (From header, Gmail's verified Authentication-Results), never body
content, so this is a trust signal a malicious body can't forge. Pure + deterministic + no model.
"""

from email.utils import parseaddr

from app.config import settings


def _trusted_domains() -> list[str]:
    return [d.strip().lower() for d in settings.trusted_sender_domains.split(",") if d.strip()]


def _domain_of(from_header: str | None) -> str:
    """The lowercased domain of a From header, via stdlib parseaddr. '' if unparseable."""
    _name, addr = parseaddr(from_header or "")
    return addr.rsplit("@", 1)[-1].lower() if "@" in addr else ""


def _display_name(from_header: str | None) -> str:
    name, _addr = parseaddr(from_header or "")
    return name.lower()


def _domain_matches(domain: str, allowed: str) -> bool:
    """True if `domain` is `allowed` or a subdomain of it (mail.chase.com matches chase.com)."""
    return bool(domain) and (domain == allowed or domain.endswith("." + allowed))


def _auth_failed(auth_results: str | None) -> bool:
    """True if Gmail's Authentication-Results shows an explicit SPF/DKIM/DMARC failure — a spoof
    indicator. A missing header is NOT a failure (many legit senders omit some methods)."""
    if not auth_results:
        return False
    text = auth_results.lower()
    return any(f"{method}=fail" in text for method in ("spf", "dkim", "dmarc"))


def _impersonates_brand(from_header: str | None) -> bool:
    """True if the display name name-drops a trusted brand (e.g. "Chase") but the actual from-domain
    is NOT that brand's trusted domain — the classic display-name spoof."""
    name = _display_name(from_header)
    domain = _domain_of(from_header)
    if not name:
        return False
    for d in _trusted_domains():
        brand = d.split(".")[0]  # "chase" from "chase.com"
        if brand and brand in name and not _domain_matches(domain, d):
            return True
    return False


def classify(from_header: str | None, auth_results: str | None) -> str:
    """Return the sender trust tier: "trusted" | "unknown" | "suspicious". Spoof signals win over
    allowlisting (a From that name-drops a brand from the wrong domain is suspicious even if some
    other check would pass)."""
    if _impersonates_brand(from_header) or _auth_failed(auth_results):
        return "suspicious"
    domain = _domain_of(from_header)
    if any(_domain_matches(domain, d) for d in _trusted_domains()):
        return "trusted"
    return "unknown"
