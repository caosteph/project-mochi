"""Fixture-backed Google stand-ins for demo mode (docs/demo-mode.md).

Implements the same function signatures as `app.integrations.google_gmail` and
`app.integrations.google_calendar`, but serves fictional data from
`demo/fixtures/` instead of calling Google. No network, no credentials, no real
personal data — every name, merchant, and address here is invented.

Fixtures use RELATIVE dates so the demo works any day it is run:
- inbox messages carry `days_ago` (+ `time`) for the Date header;
- `{{return_by}}` in a body resolves to today + `return_by_days`, formatted
  "Month D, YYYY" (so the quarantined reader can parse it as a due date);
- `{{iso:+N}}` resolves to the ISO date N days from now;
- calendar events carry `days_from_now` + start/end times.

State (created drafts / created events) lives in memory for the process and
resets on restart — the demo always starts from the same fixtures. `reset()`
reloads the fixtures (used by tests and the seed script).
"""

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

from app.config import settings

log = logging.getLogger(__name__)

_RETURN_BY_RE = re.compile(r"\{\{return_by\}\}")
_ISO_RE = re.compile(r"\{\{iso:([+-]\d+)\}\}")
_NEWER_THAN_RE = re.compile(r"newer_than:(\d+)d")


class _Store:
    """Loaded fixtures + in-memory writes, resolved to absolute dates at load."""

    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.events: list[dict] = []
        self.drafts: list[dict] = []
        self.created_events: list[dict] = []
        self.user: dict = {}
        self._next_draft = 1
        self._next_event = 1

    def load(self, fixtures_dir: str | Path) -> None:
        base = Path(fixtures_dir)
        now = datetime.now(UTC)
        inbox = json.loads((base / "inbox.json").read_text(encoding="utf-8"))
        self.user = inbox.get("user", {})
        self.messages = [self._message(m, now) for m in inbox["messages"]]
        self.messages.sort(key=lambda m: m["_dt"], reverse=True)  # newest first
        calendar = json.loads((base / "calendar.json").read_text(encoding="utf-8"))
        self.events = [self._event(e, now) for e in calendar["events"]]
        self.events.sort(key=lambda e: e["start"])
        self.drafts = []
        self.created_events = []
        self._next_draft = 1
        self._next_event = 1

    @staticmethod
    def _resolve_body(body: str, now: datetime, return_by_days: int | None) -> str:
        if return_by_days is not None:
            due = now + timedelta(days=return_by_days)
            body = _RETURN_BY_RE.sub(f"{due:%B %-d, %Y}", body)
        return _ISO_RE.sub(lambda m: (now + timedelta(days=int(m.group(1)))).date().isoformat(), body)

    def _message(self, raw: dict, now: datetime) -> dict:
        days_ago = raw.get("days_ago", 0)
        hh, mm = (raw.get("time") or "09:00").split(":")
        dt = (now - timedelta(days=days_ago)).replace(
            hour=int(hh), minute=int(mm), second=0, microsecond=0
        )
        return {
            "id": raw["id"],
            "from": raw["from"],
            "subject": raw["subject"],
            "date": format_datetime(dt),
            "auth_results": raw.get("auth_results"),
            "body_text": self._resolve_body(raw.get("body", ""), now, raw.get("return_by_days")),
            "_dt": dt,
        }

    @staticmethod
    def _event(raw: dict, now: datetime) -> dict:
        day = (now + timedelta(days=raw.get("days_from_now", 0))).date()
        sh, sm = raw["start"].split(":")
        eh, em = raw["end"].split(":")
        start = datetime(day.year, day.month, day.day, int(sh), int(sm), tzinfo=UTC)
        end = datetime(day.year, day.month, day.day, int(eh), int(em), tzinfo=UTC)
        return {
            "summary": raw["summary"],
            "start": start.isoformat(),
            "end": end.isoformat(),
            "location": raw.get("location"),
        }


_STORE: _Store | None = None


def _store() -> _Store:
    global _STORE
    if _STORE is None:
        _STORE = _Store()
        _STORE.load(settings.demo_fixtures_dir)
    return _STORE


def reset(fixtures_dir: str | Path | None = None) -> None:
    """Reload fixtures from disk (fresh state). Tests and the seed script use this."""
    global _STORE
    _STORE = _Store()
    _STORE.load(fixtures_dir or settings.demo_fixtures_dir)


def _public_message(m: dict) -> dict:
    return {
        "from": m["from"],
        "subject": m["subject"],
        "date": m["date"],
        "auth_results": m["auth_results"],
        "body_text": m["body_text"],
    }


# --- Gmail-shaped functions (mirror app.integrations.google_gmail) ----------

def get_own_address() -> str:
    return _store().user.get("email", "alex.rivera@example.com")


def list_recent_metadata(max_results: int = 10, query: str | None = None) -> list[dict]:
    msgs = _store().messages
    if query:
        q = query.lower()
        msgs = [m for m in msgs if q in m["from"].lower() or q in m["subject"].lower()]
    return [
        {"from": m["from"], "subject": m["subject"], "date": m["date"]}
        for m in msgs[:max_results]
    ]


def search_message_ids(query: str, max_results: int = 25) -> list[str]:
    """Approximate Gmail search over the fixtures: `newer_than:Nd` filters by
    recency; any other terms substring-match from/subject/body. Newest first."""
    now = datetime.now(UTC)
    msgs = _store().messages
    newer = _NEWER_THAN_RE.search(query or "")
    if newer:
        cutoff = now - timedelta(days=int(newer.group(1)))
        msgs = [m for m in msgs if m["_dt"] >= cutoff]
    terms = re.sub(r"newer_than:\d+d|-category:\w+", "", query or "").strip().lower()
    if terms:
        msgs = [
            m for m in msgs
            if terms in m["from"].lower() or terms in m["subject"].lower()
            or terms in m["body_text"].lower()
        ]
    return [m["id"] for m in msgs[:max_results]]


def get_message_body(message_id: str) -> dict:
    for m in _store().messages:
        if m["id"] == message_id:
            return _public_message(m)
    raise KeyError(f"no demo message {message_id!r}")


def create_draft(to: str, subject: str, body: str) -> dict:
    store = _store()
    draft_id = f"demo-draft-{store._next_draft}"
    store._next_draft += 1
    store.drafts.append({"id": draft_id, "to": to, "subject": subject, "body": body})
    log.info("demo draft created: %s to %s", draft_id, to)
    return {"id": draft_id}


def delete_draft(draft_id: str) -> None:
    store = _store()
    store.drafts = [d for d in store.drafts if d["id"] != draft_id]


def list_drafts() -> list[dict]:
    """Demo-only helper: drafts created this session (for /drafts + tests)."""
    return list(_store().drafts)


# --- Calendar-shaped functions (mirror app.integrations.google_calendar) ----

def list_events(
    start_iso: str | None = None,
    end_iso: str | None = None,
    max_results: int = 10,
) -> list[dict]:
    now = datetime.now(UTC)
    start = datetime.fromisoformat(start_iso) if start_iso else now
    end = datetime.fromisoformat(end_iso) if end_iso else now + timedelta(days=7)
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    store = _store()
    all_events = [
        {"summary": e["summary"], "start": e["start"], "end": e["end"], "location": e["location"]}
        for e in store.events + store.created_events
    ]
    out = []
    for e in all_events:
        s = datetime.fromisoformat(e["start"])
        if start <= s <= end:
            out.append(e)
    out.sort(key=lambda e: e["start"])
    return out[:max_results]


def create_event(summary: str, start_iso: str, end_iso: str) -> dict:
    store = _store()
    event_id = f"demo-event-{store._next_event}"
    store._next_event += 1
    store.created_events.append(
        {"id": event_id, "summary": summary, "start": start_iso, "end": end_iso, "location": None}
    )
    log.info("demo event created: %s %s", event_id, summary)
    return {"id": event_id}


def delete_event(event_id: str) -> None:
    store = _store()
    store.created_events = [
        e for e in store.created_events if e.get("id") != event_id
    ]


def list_created_events() -> list[dict]:
    """Demo-only helper: events created this session (for /events + tests)."""
    return list(_store().created_events)
