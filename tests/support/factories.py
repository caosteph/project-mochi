"""Database row factories — replace the scattered `_add` / `_seed` / `_seed_reminder` helpers.

Each takes an open `Session`, commits, and returns the created row (so a test can read its `.id`
inside the session before it detaches). The `seed` conftest fixture wraps these to open sessions on
the test engine, so most tests never touch a `Session` directly: `seed.reminder("dentist")`.
"""

from datetime import UTC, datetime, timedelta

from sqlmodel import Session

from app.memory.models import (
    EmailSignal,
    Fact,
    Provenance,
    Reminder,
    ReminderStatus,
    SignalStatus,
    Task,
    TaskStatus,
)


def make_reminder(session: Session, text: str, *, due_at: datetime | None = None,
                  status: str = ReminderStatus.PENDING.value, recurrence: str | None = None,
                  days: int = 1) -> Reminder:
    reminder = Reminder(
        text=text,
        due_at=due_at or datetime.now(UTC) + timedelta(days=days),
        status=status,
        recurrence=recurrence,
    )
    session.add(reminder)
    session.commit()
    session.refresh(reminder)
    return reminder


def make_signal(session: Session, *, title: str = "Rain jacket from REI", signal_type: str = "return",
                status: str = SignalStatus.ASKED.value, source: str = "gmail:abc123",
                due_date: datetime | None = None, summary: str | None = None) -> EmailSignal:
    signal = EmailSignal(
        source=source,
        signal_type=signal_type,
        title=title,
        summary=summary,
        due_date=due_date or datetime.now(UTC) + timedelta(days=3),
        status=status,
    )
    session.add(signal)
    session.commit()
    session.refresh(signal)
    return signal


def make_fact(session: Session, text: str, *, confidence: float = 0.8,
              provenance: str = Provenance.USER_STATED.value) -> Fact:
    from app.memory import store

    return store.remember_fact(session, text=text, confidence=confidence, provenance=provenance)


def make_task(session: Session, text: str, *, status: str = TaskStatus.OPEN.value) -> Task:
    task = Task(text=text, status=status)
    session.add(task)
    session.commit()
    session.refresh(task)
    return task
