"""Demo mode (docs/demo-mode.md): the fixture-backed provider, the seed script,
and the console channel — all offline, no model, no network, no real data.

The provider tests prove the demo seam serves the same shapes as the real
Google modules; the seed tests prove the demo database loads (and refuses to
touch a non-demo DB); the console tests prove the demo transport drives the
real proactive pipeline end to end (scan → ask → approve → reminder).
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, select

from app.agent.quarantine import ExtractedSignal
from app.config import settings
from app.integrations import demo as demo_provider
from app.integrations import google_calendar, google_gmail
from app.memory.models import EmailSignal, Fact, Goal, IngestState, Reminder, SignalStatus
from app.proactive import email_signals, sender_trust
from scripts import seed_demo


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture(autouse=True)
def demo_on(monkeypatch):
    """Demo mode on for every test here; fixtures reloaded fresh each time."""
    monkeypatch.setattr(settings, "demo_mode", True)
    demo_provider.reset()
    yield
    monkeypatch.setattr(settings, "demo_mode", False)
    demo_provider.reset()


# --- the fixture provider ---------------------------------------------------

def test_fixtures_load_five_messages_newest_first():
    metas = demo_provider.list_recent_metadata()
    assert len(metas) == 5
    # Newest first: the injection email (today 11:02) before the dinner invite.
    assert metas[0]["subject"].startswith("URGENT: Security notice")
    assert metas[1]["subject"] == "Dinner Thursday?"
    assert all(m["from"] and m["subject"] and m["date"] for m in metas)


def test_return_email_resolves_return_by_date():
    body = demo_provider.get_message_body("demo-msg-1")["body_text"]
    due = (datetime.now(UTC) + timedelta(days=2)).strftime("%B %-d, %Y")
    assert f"Returns must be started by {due}" in body
    assert "#NW-88412" in body and "$189.00" in body


def test_search_newer_than_returns_all_fixtures():
    ids = demo_provider.search_message_ids("newer_than:3d -category:promotions -category:social")
    assert len(ids) == 5  # every fixture message is ≤2 days old


def test_search_term_matches_injection_email():
    assert demo_provider.search_message_ids("security") == ["demo-msg-2"]
    assert demo_provider.search_message_ids("jacket") == ["demo-msg-1"]


def test_injection_email_is_suspicious_and_return_is_trusted(monkeypatch):
    monkeypatch.setattr(settings, "trusted_sender_domains", "northwindoutfitters.example")
    injection = demo_provider.get_message_body("demo-msg-2")
    assert sender_trust.classify(injection["from"], injection["auth_results"]) == "suspicious"
    ret = demo_provider.get_message_body("demo-msg-1")
    assert sender_trust.classify(ret["from"], ret["auth_results"]) == "trusted"


def test_get_own_address_is_the_demo_user():
    assert demo_provider.get_own_address() == "alex.rivera@example.com"


def test_draft_round_trip():
    created = demo_provider.create_draft("jordan.lee@example.com", "Dinner Thursday", "See you at 7!")
    assert created["id"].startswith("demo-draft-")
    drafts = demo_provider.list_drafts()
    assert len(drafts) == 1 and drafts[0]["subject"] == "Dinner Thursday"
    demo_provider.delete_draft(created["id"])
    assert demo_provider.list_drafts() == []


def test_calendar_fixtures_and_created_events():
    events = demo_provider.list_events()
    assert [e["summary"] for e in events] == [
        "Dentist appointment", "Dinner with Jordan", "Team standup",
    ]
    created = demo_provider.create_event(
        "Return the jacket", "2030-01-01T10:00:00+00:00", "2030-01-01T10:30:00+00:00"
    )
    assert created["id"].startswith("demo-event-")
    assert len(demo_provider.list_created_events()) == 1


def test_unknown_message_id_raises():
    with pytest.raises(KeyError):
        demo_provider.get_message_body("demo-msg-nope")


# --- the dispatch seam (Google modules → demo provider) ---------------------

def test_gmail_dispatch_uses_demo_when_no_service():
    metas = google_gmail.list_recent_metadata()
    assert len(metas) == 5
    assert google_gmail.get_own_address() == "alex.rivera@example.com"
    assert google_gmail.search_message_ids("security") == ["demo-msg-2"]


def test_explicit_service_wins_over_demo(monkeypatch):
    """The offline-test seam is untouched: a passed service is always used,
    even with demo mode on."""

    # A fake that records it was used instead of raising, for the metadata path.
    calls = {"n": 0}

    class _Meta:
        def list(self, **kw):
            calls["n"] += 1
            return self

        def execute(self):
            return {"messages": []}

    class _Users:
        def messages(self):
            return _Meta()

    class _Svc:
        def users(self):
            return _Users()

    assert google_gmail.list_recent_metadata(service=_Svc()) == []
    assert calls["n"] == 1


def test_calendar_dispatch_uses_demo():
    events = google_calendar.list_events()
    assert [e["summary"] for e in events] == [
        "Dentist appointment", "Dinner with Jordan", "Team standup",
    ]


# --- the seed script --------------------------------------------------------

@pytest.fixture
def _mock_embeddings(monkeypatch):
    """Seed facts need embeddings; stand in for Ollama (offline)."""
    monkeypatch.setattr(
        "app.memory.store.embed_local", lambda text: [0.0] * 768
    )


def test_seed_loads_profile_and_initializes_scan(session, _mock_embeddings):
    seed_demo.main()
    pinned = [f for f in session.exec(select(Fact)) if f.pinned]
    assert len(pinned) == 3
    assert len(list(session.exec(select(Fact)))) == 6  # 3 pinned + 3 facts
    assert len(list(session.exec(select(Goal)))) == 1
    state = session.exec(select(IngestState)).first()
    assert state is not None
    assert state.initialized_at <= datetime.now(UTC) - timedelta(days=6)


def test_seed_is_idempotent(session, _mock_embeddings):
    seed_demo.main()
    seed_demo.main()
    assert len(list(session.exec(select(Fact)))) == 6
    assert len(list(session.exec(select(Goal)))) == 1


def test_seed_refuses_without_demo_mode(monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", False)
    with pytest.raises(SystemExit):
        seed_demo.main()


def test_seed_refuses_non_demo_database(monkeypatch):
    monkeypatch.setattr(settings, "database_url", "postgresql://postgres@localhost/personal_agent")
    with pytest.raises(SystemExit):
        seed_demo.main()


# --- the console channel ----------------------------------------------------

@pytest.fixture
def channel(monkeypatch):
    """A ConsoleChannel with the real agent swapped for a stub (no model)."""
    import app.channels.console as console_mod

    monkeypatch.setattr(console_mod, "build_agent", lambda: object())
    monkeypatch.setattr(settings, "telegram_chat_id", 1)
    return console_mod.ConsoleChannel()


def test_console_dispatch_routes_commands(channel):
    assert channel._dispatch("/help") is True
    assert channel._dispatch("/bogus") is True
    assert channel._dispatch("just chatting") is False


def test_console_approve_creates_reminder(session, channel, capsys):
    signal = EmailSignal(
        source="gmail:demo-msg-1",
        signal_type="return", title="Trailhead Rain Jacket from Northwind Outfitters",
        due_date=datetime.now(UTC) + timedelta(days=2),
        status=SignalStatus.DETECTED.value,
    )
    session.add(signal)
    session.commit()
    channel._cmd_approve(str(signal.id))
    assert "I'll remind you" in capsys.readouterr().out
    reminder = session.exec(select(Reminder)).first()
    assert reminder is not None and "jacket" in reminder.text.lower()
    session.refresh(signal)
    assert signal.status == SignalStatus.CONFIRMED.value


def test_console_reject_dismisses(session, channel, capsys):
    signal = EmailSignal(
        source="gmail:demo-msg-1",
        signal_type="return", title="Trailhead Rain Jacket from Northwind Outfitters",
        due_date=datetime.now(UTC) + timedelta(days=2),
        status=SignalStatus.DETECTED.value,
    )
    session.add(signal)
    session.commit()
    channel._cmd_reject(str(signal.id))
    assert "Skipped" in capsys.readouterr().out
    session.refresh(signal)
    assert signal.status == SignalStatus.DISMISSED.value


def test_console_scan_end_to_end(session, channel, monkeypatch, capsys):
    """The flagship moment, offline: demo inbox → real ingest → a return signal
    is asked about. The extractor is faked (its real-model behavior is covered
    by scripts/verify_phase3b.py); everything else is the real pipeline."""
    monkeypatch.setattr(settings, "signal_mode", "live")
    # Quiet hours would silently swallow the ask if the test runs at night.
    monkeypatch.setattr(settings, "quiet_hours_start", 0)
    monkeypatch.setattr(settings, "quiet_hours_end", 0)
    email_signals._mark_initialized(session, datetime.now(UTC) - timedelta(days=7))

    def fake_extract(email):
        subject = email["subject"]
        if "shipped" in subject:
            due = (datetime.now(UTC) + timedelta(days=2)).date().isoformat()
            return ExtractedSignal(
                is_actionable=True, signal_type="return",
                title="Trailhead Rain Jacket from Northwind Outfitters", due_date=due,
            )
        return ExtractedSignal(is_actionable=False, signal_type="other", title="")

    monkeypatch.setattr("app.agent.quarantine.extract_signal", fake_extract)
    channel._cmd_scan()
    out = capsys.readouterr().out
    assert "Trailhead Rain Jacket" in out
    signals = list(session.exec(select(EmailSignal)))
    assert len(signals) == 1 and signals[0].signal_type == "return"
    # The scan also pushed the approval ask, flipping it to ASKED.
    assert signals[0].status == SignalStatus.ASKED.value
    # The injection email was read but produced no signal — the defense held.
    assert len(list(session.exec(select(EmailSignal).where(
        EmailSignal.title.contains("Security"))))) == 0


def test_console_turn_replies(session, channel, monkeypatch, capsys):
    """A chat turn through the console channel: the fake agent's reply is
    printed and the turn is logged to the message log."""
    from langchain_core.messages import AIMessage

    from app.memory.models import MessageLog

    class _FakeAgent:
        def stream(self, graph_input, config, stream_mode=None):
            yield {"agent": {"messages": [AIMessage(content="Hi Alex — dentist tomorrow at 10.")]}}

    channel.agent = _FakeAgent()
    monkeypatch.setattr(settings, "fact_sweep_enabled", False)
    channel._turn("what's tomorrow?")
    out = capsys.readouterr().out
    assert "Hi Alex — dentist tomorrow at 10." in out
    logs = list(session.exec(select(MessageLog)))
    assert [row.role for row in logs] == ["user", "assistant"]


def test_console_turn_interrupt_approve(session, channel, monkeypatch, capsys):
    """An approval interrupt pauses the turn, the console asks, and a 'y'
    resumes the graph with approved=True."""
    from types import SimpleNamespace

    from langchain_core.messages import AIMessage
    from langgraph.types import Command

    calls = {"resumed": None}

    class _FakeAgent:
        def stream(self, graph_input, config, stream_mode=None):
            if isinstance(graph_input, Command):
                calls["resumed"] = graph_input.resume
                yield {"agent": {"messages": [AIMessage(content="Draft saved.")]}}
            else:
                yield {"__interrupt__": [SimpleNamespace(value={
                    "type": "approval_request", "action": "create_draft",
                    "details": {"to": "jordan.lee@example.com"},
                })]}

    channel.agent = _FakeAgent()
    monkeypatch.setattr(settings, "fact_sweep_enabled", False)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    channel._turn("draft an email to Jordan")
    out = capsys.readouterr().out
    assert calls["resumed"] == {"approved": True}
    assert "Draft saved." in out
    assert "Draft to approve" in out  # render_proposal of the interrupt payload


def test_console_events_lists_demo_calendar(channel, capsys):
    channel._cmd_events()
    out = capsys.readouterr().out
    assert "Dentist appointment" in out and "Dinner with Jordan" in out


def test_console_drafts_empty_then_created(channel, capsys):
    channel._cmd_drafts()
    assert "No drafts" in capsys.readouterr().out
    demo_provider.create_draft("jordan.lee@example.com", "Dinner Thursday", "See you at 7!")
    channel._cmd_drafts()
    assert "Dinner Thursday" in capsys.readouterr().out


# --- briefing greeting ------------------------------------------------------

def test_demo_briefing_greets_demo_user_not_stephanie(session):
    """The /briefing greeting must address the demo persona (Alex), not the real user."""
    from app.proactive import briefing

    out = briefing.build_briefing(session, now=datetime.now(UTC))
    assert "Morning, Alex" in out
    assert "Stephanie" not in out
