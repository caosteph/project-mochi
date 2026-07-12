"""Google tools: calendar read, email metadata, gated draft creation.

Upholds Phase 2 scoping (docs/06-phase2-build.md): calendar is read-only; email is
metadata-only (no bodies reach the privileged agent); creating a draft is a write to
Google and so passes through the human-in-the-loop approval gate before anything is
written. There is deliberately no send tool anywhere.
"""

from langchain_core.tools import tool

from app.agent.confirm import require_approval
from app.integrations import google_calendar, google_gmail


@tool
def calendar_list_events(start_iso: str | None = None, end_iso: str | None = None) -> str:
    """List upcoming Google Calendar events. start_iso/end_iso are optional RFC3339
    timestamps (e.g. 2026-07-12T00:00:00Z); default is the next 7 days. Use the
    current date/time provided in your system context to build ranges for 'today',
    'tomorrow', etc."""
    events = google_calendar.list_events(start_iso, end_iso)
    if not events:
        return "No events found in that window."
    lines = []
    for e in events:
        loc = f" @ {e['location']}" if e.get("location") else ""
        lines.append(f"- {e['start']}: {e['summary']}{loc}")
    return "\n".join(lines)


@tool
def gmail_list_recent(max_results: int = 10) -> str:
    """List recent emails as metadata only — sender, subject, date. You cannot read
    message bodies yet (that arrives in a later phase); use this for triage like
    'any unread from my landlord?'."""
    msgs = google_gmail.list_recent_metadata(max_results=max_results)
    if not msgs:
        return "No recent messages found."
    return "\n".join(f"- {m['date']} | {m['from']} | {m['subject']}" for m in msgs)


# Recipients that mean "Stephanie herself" — resolved to her own address so
# "draft an email to me" works without the model needing to know her address.
_SELF_RECIPIENTS = {"me", "myself", "self", "yourself", "stephanie", ""}


@tool
def create_draft(to: str, subject: str, body: str) -> str:
    """Create a Gmail draft (never sends it). Requires Stephanie's explicit approval:
    this call pauses and asks her to Approve/Reject before anything is written. Compose
    the full email body yourself from her instructions. For a draft to herself, pass
    to="me" (it's resolved to her own address)."""
    if to.strip().lower() in _SELF_RECIPIENTS:
        to = google_gmail.get_own_address()
    if not require_approval("create_draft", {"to": to, "subject": subject, "body": body}):
        return "Draft cancelled — nothing was created."
    draft = google_gmail.create_draft(to, subject, body)
    return (
        f"Draft created (id {draft.get('id')}). It's saved in your Gmail, unsent — "
        "review and press send when you're ready."
    )


GOOGLE_TOOLS = [calendar_list_events, gmail_list_recent, create_draft]
