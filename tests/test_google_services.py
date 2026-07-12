"""Unit tests for the Google service wrappers against a mocked API `service` —
offline, no credentials. Locks in two things that matter: correct API arg mapping,
and that email reads surface metadata ONLY (no body/snippet — safety rule #4).
"""

from unittest.mock import MagicMock

from app.integrations import google_calendar, google_gmail


def test_list_events_maps_fields_and_is_readonly():
    service = MagicMock()
    service.events().list().execute.return_value = {
        "items": [
            {
                "summary": "Dentist",
                "start": {"dateTime": "2026-07-12T09:00:00Z"},
                "end": {"dateTime": "2026-07-12T10:00:00Z"},
                "location": "123 Main St",
            },
            {"start": {"date": "2026-07-13"}, "end": {"date": "2026-07-14"}},  # all-day, no title
        ]
    }
    events = google_calendar.list_events("2026-07-12T00:00:00Z", "2026-07-14T00:00:00Z", service=service)

    assert events[0] == {
        "summary": "Dentist",
        "start": "2026-07-12T09:00:00Z",
        "end": "2026-07-12T10:00:00Z",
        "location": "123 Main St",
    }
    assert events[1]["summary"] == "(no title)"
    assert events[1]["start"] == "2026-07-13"  # falls back to all-day date


def test_gmail_metadata_has_no_body_or_snippet():
    service = MagicMock()
    service.users().messages().list().execute.return_value = {"messages": [{"id": "m1"}]}
    service.users().messages().get().execute.return_value = {
        "snippet": "SECRET body preview that must not leak",
        "payload": {
            "headers": [
                {"name": "From", "value": "landlord@example.com"},
                {"name": "Subject", "value": "Rent"},
                {"name": "Date", "value": "Mon, 6 Jul 2026 10:00:00 -0400"},
                {"name": "To", "value": "stephanie@example.com"},
            ]
        },
    }
    msgs = google_gmail.list_recent_metadata(max_results=5, service=service)

    assert len(msgs) == 1
    assert set(msgs[0].keys()) == {"from", "subject", "date"}  # exactly these, nothing more
    assert msgs[0]["from"] == "landlord@example.com"
    assert "SECRET" not in str(msgs[0])  # snippet/body never surfaces


def test_gmail_requests_metadata_format_only():
    # Belt-and-suspenders: assert we ask Gmail for `format=metadata`, not full/raw,
    # so a body isn't even fetched over the wire.
    service = MagicMock()
    service.users().messages().list().execute.return_value = {"messages": [{"id": "m1"}]}
    service.users().messages().get().execute.return_value = {"payload": {"headers": []}}
    google_gmail.list_recent_metadata(service=service)

    _, kwargs = service.users().messages().get.call_args
    assert kwargs["format"] == "metadata"


def test_create_draft_builds_message_and_returns_resource():
    service = MagicMock()
    service.users().drafts().create().execute.return_value = {"id": "draft_999"}
    result = google_gmail.create_draft("a@b.com", "Subj", "Body text", service=service)

    assert result == {"id": "draft_999"}
    _, kwargs = service.users().drafts().create.call_args
    assert kwargs["userId"] == "me"
    assert "raw" in kwargs["body"]["message"]  # base64url-encoded MIME


def test_get_own_address_reads_profile(monkeypatch):
    monkeypatch.setattr(google_gmail, "_own_address", None)  # reset the cache
    service = MagicMock()
    service.users().getProfile().execute.return_value = {"emailAddress": "steph@example.com"}
    assert google_gmail.get_own_address(service=service) == "steph@example.com"
