"""On-demand email reading — the reactive counterpart to the proactive scanner
(app/proactive/email_signals.py). When Stephanie asks "what did the landlord's email
say?", this searches her inbox, fetches ONE matching body, and runs it through the
**quarantined summarizer** — returning only a validated, length-capped `EmailSummary`.

This module is the single place a body is fetched and immediately handed to the
quarantine boundary (mirroring how email_signals.py owns the scan path). The privileged
agent (google_tools.read_email) never receives `body_text` — only the structured
summary — so constitution rule #4 holds: untrusted content is data, never instructions.

`service` (Gmail) and `reader` (the summarizer) are injectable, so the whole thing runs
offline against a mock inbox + a fake reader with no network and no model.
"""

from app.agent import quarantine
from app.config import settings
from app.integrations import google_gmail


def read_email_summary(
    query: str, *, service=None, reader=None, max_candidates: int | None = None
) -> tuple[quarantine.EmailSummary | None, int]:
    """Find the newest email matching `query` and summarize it behind the quarantine.

    Returns `(summary, n_matches)`:
      - `(None, 0)`  → nothing matched the query.
      - `(None, n)`  with n>0 → matched, but the message has no readable text body.
      - `(summary, n)` → the newest match, summarized.

    Only the newest match's body is fetched (one reader call — bounds latency/cost); the
    match count is reported so the caller can note "newest of N".
    """
    max_candidates = max_candidates if max_candidates is not None else settings.email_read_max_candidates
    ids = google_gmail.search_message_ids(query, max_results=max_candidates, service=service)
    if not ids:
        return None, 0
    email = google_gmail.get_message_body(ids[0], service=service)  # newest first
    if not (email.get("body_text") or "").strip():
        return None, len(ids)
    summary = quarantine.summarize_email(email, reader=reader)
    # Prefer the real headers over the model's echo — exact, not paraphrased (still
    # capped, still framed as untrusted by the caller).
    if email.get("from"):
        summary.sender = email["from"][:200]
    if email.get("subject"):
        summary.subject = email["subject"][:200]
    return summary, len(ids)


def format_summary(summary: quarantine.EmailSummary, n_matches: int) -> str:
    """Render ONLY the structured summary fields (never the raw body) as plain text.
    The caller wraps this in the untrusted-content frame."""
    lines: list[str] = []
    if summary.sender:
        lines.append(f"From: {summary.sender}")
    if summary.subject:
        lines.append(f"Subject: {summary.subject}")
    if lines:
        lines.append("")  # blank line before the prose
    lines.append(summary.summary)
    if summary.action_needed:
        lines.append(f"\n👉 Needs you to: {summary.action_needed}")
    if summary.date:
        lines.append(f"🗓️ Date mentioned: {summary.date}")
    note = f"\n\n(newest of {n_matches} matching emails)" if n_matches > 1 else ""
    return "\n".join(lines) + note
