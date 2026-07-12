"""Thin Gmail wrapper: metadata-only reads and draft creation. Never sends.

Decision (Phase 2, docs/06-phase2-build.md): the privileged agent must NOT ingest
raw email bodies (safety rule #4 — untrusted content is data, never instructions;
the quarantined reader that safely parses bodies lands in Phase 3). So
list_recent_metadata returns only From/Subject/Date — never the body, and never the
Gmail `snippet` (which is body-derived). Drafts are created via gmail.compose scope;
there is no send function anywhere by construction.

Functions take an optional pre-built `service` for offline unit testing against a mock.
"""

import base64
from email.message import EmailMessage

from googleapiclient.discovery import build

from app.integrations.google_auth import get_credentials


_own_address: str | None = None
_service_cache = None


def _service():
    # Cached — see google_calendar._service() for why (build() re-fetches discovery).
    global _service_cache
    if _service_cache is None:
        _service_cache = build("gmail", "v1", credentials=get_credentials(), cache_discovery=False)
    return _service_cache


def reset_service_cache() -> None:
    """Drop the cached service — call after an OAuth re-consent (new credentials)."""
    global _service_cache, _own_address
    _service_cache = None
    _own_address = None


def get_own_address(*, service=None) -> str:
    """The authenticated account's own email address (cached). Used to resolve
    'me'/'myself' recipients into a real address for self-drafts."""
    global _own_address
    if _own_address is None:
        service = service or _service()
        _own_address = service.users().getProfile(userId="me").execute()["emailAddress"]
    return _own_address


def list_recent_metadata(max_results: int = 10, query: str | None = None, *, service=None) -> list[dict]:
    """Return recent messages as metadata only: from / subject / date. No body,
    no snippet — see the module docstring for why."""
    service = service or _service()
    resp = (
        service.users()
        .messages()
        .list(userId="me", maxResults=max_results, q=query)
        .execute()
    )
    out = []
    for m in resp.get("messages", []):
        msg = (
            service.users()
            .messages()
            .get(
                userId="me",
                id=m["id"],
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            )
            .execute()
        )
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        # Deliberately extract ONLY these three — never msg["snippet"] or any body part.
        out.append(
            {
                "from": headers.get("From"),
                "subject": headers.get("Subject"),
                "date": headers.get("Date"),
            }
        )
    return out


def create_draft(to: str, subject: str, body: str, *, service=None) -> dict:
    """Create a Gmail draft (unsent). Returns the created draft resource (has 'id')."""
    service = service or _service()
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    return (
        service.users()
        .drafts()
        .create(userId="me", body={"message": {"raw": raw}})
        .execute()
    )


def delete_draft(draft_id: str, *, service=None) -> None:
    """Delete a draft by id — used by verify_phase2.py to clean up after a live test."""
    service = service or _service()
    service.users().drafts().delete(userId="me", id=draft_id).execute()
