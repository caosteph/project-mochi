"""Every database-backed tool must actually RUN, not just get chosen.

This is the gap that let `cancel_reminder` ship broken. Every `scripts/verify_*.py` check
deliberately breaks *before* the tool node executes — that's what makes measuring tool choice
safe (it never creates a draft or hits the network). The unintended consequence is that the
entire gate answers "did the model pick the right tool?" and nothing answers "does the tool
work?". `cancel_reminder` was picked correctly and raised DetachedInstanceError on every call.

So: invoke each DB-only tool for real, against the scratch database, and assert it returns a
string instead of raising. Deliberately shallow — this is a smoke test for the "it explodes on
contact" class, not a substitute for each tool's own behavioural tests.

Tools needing Google, the network, a hosted model, or the builder sandbox are listed in
NEEDS_EXTERNAL below so that adding a tool without covering it is a visible decision rather
than an oversight — the count is asserted.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session

from app.agent.tools import ALL_TOOLS
from app.memory.models import Reminder, ReminderStatus

# Tools that cannot run in an offline test: Google APIs, outbound HTTP, a hosted model, or the
# builder sandbox. Each is covered by its own phase verify script instead.
NEEDS_EXTERNAL = {
    "calendar_list_events", "gmail_list_recent", "read_email", "create_draft",
    "web_search", "consult_expert", "build_web_app", "make_document", "serve_project",
}

# Safe arguments for the DB-only tools.
ARGS = {
    "recall": {"query": "anything"},
    "remember_fact": {"text": "the sky is blue", "confidence": 0.5, "provenance": "inferred"},
    "add_goal": {"text": "run a 10k"},
    "add_task": {"text": "buy running shoes"},
    "list_reminders": {},
    "cancel_reminder": {"query": "a reminder that does not exist"},
    "add_reminder": {"text": "water the plants", "when": "tomorrow at 3pm"},
    "list_projects": {},
}


@pytest.fixture(autouse=True)
def _seed(engine, monkeypatch):
    # Calendar mirroring is best-effort and already swallowed on failure, but switching it off
    # keeps this test about the tool rather than about Google being unreachable.
    from app.config import settings

    monkeypatch.setattr(settings, "calendar_mirror_enabled", False)
    with Session(engine) as s:
        s.add(Reminder(text="dentist appointment",
                       due_at=datetime.now(UTC) + timedelta(days=1),
                       status=ReminderStatus.PENDING.value))
        s.commit()


def _db_tools():
    return [t for t in ALL_TOOLS if t.name not in NEEDS_EXTERNAL]


@pytest.mark.parametrize("tool", _db_tools(), ids=lambda t: t.name)
def test_tool_executes_without_raising(tool):
    """The check the gate structurally cannot make: run it, don't just select it."""
    if tool.name not in ARGS:
        pytest.fail(
            f"{tool.name} is neither in NEEDS_EXTERNAL nor given test arguments — "
            "decide which, so new tools can't silently escape execution coverage"
        )
    result = tool.invoke(ARGS[tool.name])
    assert isinstance(result, str) and result.strip(), (
        f"{tool.name} returned {result!r}; tools must return a non-empty string for the model"
    )


def test_cancel_reminder_executes_on_a_real_match():
    """The exact call that raised in production: a query that DOES match, so the helper commits
    and expires the instance before the tool formats its confirmation."""
    from app.agent.tools.reminder_tools import cancel_reminder

    out = cancel_reminder.invoke({"query": "the dentist reminder"})
    assert "Cancelled" in out and "dentist" in out


def test_every_tool_is_accounted_for():
    """Adding a tool without either covering it here or declaring it external should fail."""
    names = {t.name for t in ALL_TOOLS}
    uncovered = names - NEEDS_EXTERNAL - set(ARGS)
    assert not uncovered, (
        f"new tool(s) {sorted(uncovered)} have no execution coverage — add test args, "
        "or add them to NEEDS_EXTERNAL with a phase verify script that exercises them"
    )
