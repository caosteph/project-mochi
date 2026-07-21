"""Thin Gmail wrapper: metadata-only reads, draft creation, and (Phase 3B) a
single body-reading function reserved for the quarantined reader. Never sends.

Decision (Phase 2, docs/06-phase2-build.md): the *privileged agent* must NOT ingest
raw email bodies (safety rule #4 — untrusted content is data, never instructions).
So the agent-facing tools (list_recent_metadata) return only From/Subject/Date —
never the body, never the Gmail `snippet`. Phase 3B adds `get_message_body`, which
reads the full body, but it is used **solely by the quarantined reader**
(app/agent/quarantine.py → app/proactive/email_signals.py) — never wired into an
agent tool. The reader has no tools and emits only validated structured data, so a
malicious body can't act. Drafts are created via gmail.compose scope; there is no
send function anywhere by construction.

Functions take an optional pre-built `service` for offline unit testing against a mock.
"""

import base64
from email.message import EmailMessage
from html.parser import HTMLParser

from googleapiclient.discovery import build

from app.integrations.google_auth import get_credentials

_BODY_MAX_CHARS = 20_000  # bound the reader's input regardless of email size


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


# --- Phase 3B: body reading for the quarantined reader ONLY -----------------

class _TextExtractor(HTMLParser):
    """Collect visible text from HTML, dropping <script>/<style> content. Stdlib
    only — no BeautifulSoup dependency. Good enough to feed a parser model; we
    don't need perfect rendering, just the words."""

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, _attrs) -> None:  # signature fixed by HTMLParser
        if tag in ("script", "style", "head"):
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "head") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip and data.strip():
            self._parts.append(data.strip())

    def text(self) -> str:
        return " ".join(self._parts)


def _html_to_text(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:
        return html  # malformed markup → fall back to the raw string
    return parser.text()


def search_message_ids(query: str, max_results: int = 25, *, service=None) -> list[str]:
    """Return the message-ids matching a Gmail search query, newest first."""
    service = service or _service()
    resp = (
        service.users().messages().list(userId="me", maxResults=max_results, q=query).execute()
    )
    return [m["id"] for m in resp.get("messages", [])]


def _decode_part(part: dict) -> str:
    data = part.get("body", {}).get("data")
    if not data:
        return ""
    return base64.urlsafe_b64decode(data.encode()).decode("utf-8", errors="replace")


def _walk_for_body(payload: dict) -> str:
    """Prefer text/plain; fall back to text/html (stripped). Recurses MIME parts."""
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        return _decode_part(payload)
    if mime == "text/html":
        return _html_to_text(_decode_part(payload))
    plain, html = "", ""
    for part in payload.get("parts", []) or []:
        got = _walk_for_body(part)
        if not got:
            continue
        if part.get("mimeType") == "text/plain":
            plain = plain or got
        else:
            html = html or got
    return plain or html


def get_message_body(message_id: str, *, service=None) -> dict:
    """Read one message's headers + plain-text body. RESERVED FOR THE QUARANTINED
    READER — never wire this into an agent tool (see module docstring / rule #4).
    Returns {from, subject, date, body_text}; body_text is length-bounded."""
    service = service or _service()
    msg = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    payload = msg.get("payload", {})
    headers = {h["name"]: h["value"] for h in payload.get("headers", [])}
    body = _walk_for_body(payload)[:_BODY_MAX_CHARS]
    return {
        "from": headers.get("From"),
        "subject": headers.get("Subject"),
        "date": headers.get("Date"),
        "body_text": body,
    }
