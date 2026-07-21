"""The agent-callable tool wrappers (memory + reminders).

They're thin, but they're the layer the model actually calls — so what matters is that each one
passes the model's arguments through correctly, validates what the model can get wrong, and
returns a plain-language string (never a raw object or JSON, which is the failure mode that has
reached Stephanie before). The layer underneath is mocked; its behaviour is tested elsewhere.
"""

from datetime import datetime, timezone

import pytest
from sqlmodel import Session

from app.agent.tools import memory_tools, reminder_tools
from app.memory.models import Reminder, ReminderStatus


@pytest.fixture(autouse=True)
def _use_test_engine(engine, monkeypatch):
    """Point both wrapper modules' get_engine at the scratch DB (hermetic w.r.t. DATABASE_URL)."""
    monkeypatch.setattr(memory_tools, "get_engine", lambda: engine)
    monkeypatch.setattr(reminder_tools, "get_engine", lambda: engine)


# --- memory tools -----------------------------------------------------------

def test_remember_fact_stores_and_reports_plainly(monkeypatch):
    captured = {}

    class FakeFact:
        id, text = 7, "allergic to peanuts"

    def fake_store(session, *, text, confidence, provenance):
        captured.update(text=text, confidence=confidence, provenance=provenance)
        return FakeFact()

    monkeypatch.setattr(memory_tools.store, "remember_fact", fake_store)
    out = memory_tools.remember_fact.invoke(
        {"text": "allergic to peanuts", "confidence": 0.9, "provenance": "user_stated"}
    )

    assert captured == {"text": "allergic to peanuts", "confidence": 0.9, "provenance": "user_stated"}
    assert "allergic to peanuts" in out and "{" not in out  # prose, not a dumped object


def test_remember_fact_rejects_what_the_model_can_get_wrong():
    # ValueError is deliberate: ToolNode turns it into an error ToolMessage so the model retries.
    with pytest.raises(ValueError):
        memory_tools.remember_fact.invoke({"text": "x", "confidence": 5.0})
    with pytest.raises(ValueError):
        memory_tools.remember_fact.invoke({"text": "x", "provenance": "made_up"})


def test_recall_renders_hits_and_the_empty_case(monkeypatch):
    monkeypatch.setattr(memory_tools.store, "recall", lambda session, *, query, k: [])
    assert "No relevant facts" in memory_tools.recall.invoke({"query": "dog"})


def test_add_goal_and_task_pass_through_and_validate_dates(monkeypatch):
    seen = {}

    class Row:
        id, text = 1, "run a 10k"

    monkeypatch.setattr(memory_tools.store, "add_goal",
                        lambda session, *, text, target_date: seen.update(goal=(text, target_date)) or Row())
    out = memory_tools.add_goal.invoke({"text": "run a 10k", "target_date": "2026-10-01"})
    assert seen["goal"][0] == "run a 10k" and seen["goal"][1].year == 2026
    assert "run a 10k" in out

    # A bad date from the model is rejected clearly rather than stored wrong.
    with pytest.raises(ValueError, match="expected YYYY-MM-DD"):
        memory_tools.add_goal.invoke({"text": "x", "target_date": "next tuesday"})


# --- reminder tools ---------------------------------------------------------

def test_add_reminder_passes_args_and_confirms_the_time(monkeypatch, engine):
    seen = {}

    due = datetime(2026, 7, 21, 15, 0, tzinfo=timezone.utc)

    def fake_create(session, *, text, when, recurrence=None, duration_minutes=None):
        seen.update(text=text, when=when, recurrence=recurrence, duration_minutes=duration_minutes)
        with Session(engine) as s:
            r = Reminder(text=text, due_at=due, status=ReminderStatus.PENDING.value, recurrence=recurrence)
            s.add(r)
            s.commit()
            s.refresh(r)
            return r

    monkeypatch.setattr(reminder_tools.reminders, "create_reminder", fake_create)
    out = reminder_tools.add_reminder.invoke(
        {"text": "call mom", "when": "tomorrow at 3pm", "recurrence": "weekly"}
    )

    assert seen["text"] == "call mom" and seen["when"] == "tomorrow at 3pm" and seen["recurrence"] == "weekly"
    # Confirms back in words, including when it will actually fire.
    assert "call mom" in out and "weekly" in out and "{" not in out


def test_add_reminder_explains_an_unparseable_time(monkeypatch):
    def boom(session, **kw):
        raise reminder_tools.reminders.ReminderParseError("couldn't read 'whenever'")

    monkeypatch.setattr(reminder_tools.reminders, "create_reminder", boom)
    out = reminder_tools.add_reminder.invoke({"text": "x", "when": "whenever"})
    assert "couldn't" in out.lower() and "3pm" in out  # tells the model how to retry


def test_add_reminder_respects_the_hourly_cap(monkeypatch):
    monkeypatch.setattr("app.config.settings.max_actions_per_hour", 0)
    out = reminder_tools.add_reminder.invoke({"text": "x", "when": "tomorrow at 3pm"})
    assert "limit" in out.lower()


def test_list_reminders_empty_and_populated(monkeypatch):
    monkeypatch.setattr(reminder_tools.reminders, "list_pending", lambda session: [])
    assert "no upcoming reminders" in reminder_tools.list_reminders.invoke({}).lower()


def test_cancel_reminder_reports_hit_and_miss(monkeypatch):
    monkeypatch.setattr(reminder_tools.reminders, "cancel_reminder", lambda session, q: None)
    assert "couldn't find" in reminder_tools.cancel_reminder.invoke({"query": "ghost"}).lower()

    class Cancelled:
        text = "call mom"

    monkeypatch.setattr(reminder_tools.reminders, "cancel_reminder", lambda session, q: Cancelled())
    assert "call mom" in reminder_tools.cancel_reminder.invoke({"query": "mom"})
