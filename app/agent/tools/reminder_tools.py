"""Agent-callable reminder tools — this is what lets Stephanie set up any proactive
reminder by talking to Mochi (not just the hardcoded return-window flow). Time
parsing is done by the reminder engine (dateparser), not the model.
"""

from langchain_core.tools import tool
from sqlmodel import Session

from app.config import settings
from app.memory.db import get_engine
from app.proactive import reminders


@tool
def add_reminder(text: str, when: str, recurrence: str | None = None) -> str:
    """Set a proactive reminder that Mochi will send Stephanie at the right time.
    `text` is what to remind her of ("call mom", "submit the form"). `when` is a
    natural-language time ("tomorrow at 3pm", "in 2 hours", "next Friday at 10am",
    "every Sunday at 9am"). `recurrence` is optional — "daily", "weekly", or
    "monthly" — for repeating reminders (or just say "every ..." in `when`)."""
    with Session(get_engine()) as session:
        try:
            reminder = reminders.create_reminder(
                session, text=text, when=when, recurrence=recurrence
            )
        except reminders.ReminderParseError as exc:
            return f"I couldn't pin down when — {exc}. Give me a specific time like 'tomorrow at 3pm'."
        if settings.calendar_mirror_enabled:
            try:
                reminders.mirror_reminder(session, reminder)
            except Exception:  # mirroring is best-effort; the reminder itself still stands
                pass
        rec = f", repeating {reminder.recurrence}" if reminder.recurrence else ""
        return (
            f"Done — I'll remind you to {reminder.text}{rec}. "
            f"First: {reminder.due_at.astimezone():%a %b %-d at %-I:%M %p}."
        )


@tool
def list_reminders() -> str:
    """List Stephanie's upcoming (pending) reminders."""
    with Session(get_engine()) as session:
        pending = reminders.list_pending(session)
    if not pending:
        return "You have no upcoming reminders."
    lines = []
    for r in pending:
        rec = f" (every {r.recurrence})" if r.recurrence else ""
        lines.append(f"- {r.text} — {r.due_at.astimezone():%a %b %-d, %-I:%M %p}{rec}")
    return "\n".join(lines)


@tool
def cancel_reminder(query: str) -> str:
    """Cancel a reminder by a description of it (e.g. 'the mom reminder') or its number."""
    with Session(get_engine()) as session:
        cancelled = reminders.cancel_reminder(session, query)
    if cancelled is None:
        return f"I couldn't find a reminder matching {query!r}."
    return f"Cancelled: {cancelled.text}."


REMINDER_TOOLS = [add_reminder, list_reminders, cancel_reminder]
