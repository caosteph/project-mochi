"""Agent-callable reminder tools — this is what lets Stephanie set up any proactive
reminder by talking to Mochi (not just the hardcoded return-window flow). Time
parsing is done by the reminder engine (dateparser), not the model.
"""

from langchain_core.tools import tool
from sqlmodel import Session

from app.agent import rate_limit
from app.agent.confirm import ask_choice
from app.memory.db import get_engine
from app.proactive import reminders


@tool
def add_reminder(
    text: str, when: str, recurrence: str | None = None, duration_minutes: int | None = None
) -> str:
    """Set a proactive reminder that Mochi will send Stephanie at the right time.
    `text` is what to remind her of ("call mom", "submit the form"). `when` is a
    natural-language time ("tomorrow at 3pm", "in 2 hours", "next Friday at 10am",
    "every Sunday at 9am"). `recurrence` is optional — "daily", "weekly", or
    "monthly" — for repeating reminders (or just say "every ..." in `when`).
    `duration_minutes` is optional: set it ONLY when the task clearly implies a
    length (a 2-hour meeting → 120, an hour at the gym → 60); omit it for ordinary
    reminders (they become a short calendar marker)."""
    if not rate_limit.allow("add_reminder"):
        return "I've hit my safety limit on reminders for the hour — paused. Try again a bit later."
    with Session(get_engine()) as session:
        try:
            reminder, created = reminders.create_or_get_reminder(
                session, text=text, when=when, recurrence=recurrence, duration_minutes=duration_minutes
            )
        except reminders.ReminderParseError as exc:
            return f"I couldn't pin down when — {exc}. Give me a specific time like 'tomorrow at 3pm'."
        rec = f", repeating {reminder.recurrence}" if reminder.recurrence else ""
        when_str = f"{reminder.due_at.astimezone():%a %b %-d at %-I:%M %p}"
        if not created:
            # Say so rather than implying a new one was made — silently "confirming" a duplicate
            # is how she ended up with 8 copies of the same reminder.
            return f"That's already set — {reminder.text}{rec}, {when_str}. I didn't add a second one."
        return f"Done — I'll remind you to {reminder.text}{rec}. First: {when_str}."


@tool
def list_reminders() -> str:
    """List Stephanie's upcoming (pending) reminders."""
    # Read every attribute INSIDE the session. Touching a model object after the `with` block
    # works only until something commits and expires it — see cancel_reminder below, where
    # exactly that shipped broken.
    with Session(get_engine()) as session:
        lines = [
            f"- {r.text} — {r.due_at.astimezone():%a %b %-d, %-I:%M %p}"
            + (f" (every {r.recurrence})" if r.recurrence else "")
            for r in reminders.list_pending(session)
        ]
    return "\n".join(lines) if lines else "You have no upcoming reminders."


@tool
def cancel_reminder(query: str) -> str:
    """Cancel a reminder by a description of it (e.g. 'the mom reminder') or its number.

    One match → cancels it. Several matches → shows Stephanie buttons to pick which (she chose
    this: no friction when it's clear, a tap when it's genuinely ambiguous). None → says so.
    """
    # Read-only lookup first (safe to re-run, which the choice interrupt does). All DB reads
    # capture plain values inside the session — reading an attribute after commit raised
    # DetachedInstanceError once, which cancelled the row then crashed the confirmation.
    with Session(get_engine()) as session:
        matches = reminders.find_pending_matches(session, query)
        labels = [r.text for r in matches]
        ids = [r.id for r in matches]

    if not matches:
        return f"I couldn't find a reminder matching {query!r}."

    if len(matches) == 1:
        target_id, target_text = ids[0], labels[0]
    else:
        # Ambiguous → let her tap which one. ask_choice pauses the graph and resumes with her
        # index; on resume this tool re-runs from the top, so the lookup above repeats and the
        # indices still line up.
        idx = ask_choice("Which reminder should I cancel?", labels)
        if idx < 0:
            return "Okay, I didn't cancel anything."
        target_id, target_text = ids[idx], labels[idx]

    with Session(get_engine()) as session:
        cancelled = reminders.cancel_reminder_by_id(session, target_id)
    if cancelled is None:
        return f"That reminder ({target_text}) was already gone."
    return f"Cancelled: {target_text}."


REMINDER_TOOLS = [add_reminder, list_reminders, cancel_reminder]
