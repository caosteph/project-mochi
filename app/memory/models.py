"""SQLModel tables for Phase 1 memory. Purchase and Relationship are deliberately
absent here — see docs/05-phase1-build.md's scope-trim note: Purchase only means
something once Phase 3's Gmail pipeline can populate it, and Relationship is Phase 5.
"""

from datetime import datetime, timezone
from enum import Enum

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, Column, DateTime, String
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# All datetime columns are explicitly TIMESTAMPTZ. SQLModel's default mapping
# for a plain `datetime` field creates a timezone-naive Postgres column, which
# silently drops the tzinfo on write — any later `now - created_at` comparison
# then raises "can't subtract offset-naive and offset-aware datetimes".
def _tz_column(*, nullable: bool = False) -> Column:
    return Column(DateTime(timezone=True), nullable=nullable)


class Provenance(str, Enum):
    USER_STATED = "user_stated"
    INFERRED = "inferred"
    IMPORTED = "imported"  # reserved for Phase 2+ (Gmail/Calendar-derived facts)


class GoalStatus(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class TaskStatus(str, Enum):
    OPEN = "open"
    DONE = "done"
    CANCELLED = "cancelled"


class ReminderStatus(str, Enum):
    PENDING = "pending"
    SENT = "sent"
    DONE = "done"
    SNOOZED = "snoozed"
    CANCELLED = "cancelled"


class ReminderKind(str, Enum):
    GENERIC = "generic"          # user-created via add_reminder
    RETURN_WINDOW = "return_window"  # auto-created from a Purchase


class Recurrence(str, Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class Fact(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    text: str
    embedding: list[float] = Field(sa_column=Column(Vector(768)))
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    provenance: str = Field(default=Provenance.USER_STATED.value, sa_column=Column(String, index=True))
    created_at: datetime = Field(default_factory=_utcnow, sa_column=_tz_column())


class Goal(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    text: str
    status: str = Field(default=GoalStatus.ACTIVE.value, sa_column=Column(String, index=True))
    target_date: datetime | None = Field(default=None, sa_column=_tz_column(nullable=True))
    created_at: datetime = Field(default_factory=_utcnow, sa_column=_tz_column())
    completed_at: datetime | None = Field(default=None, sa_column=_tz_column(nullable=True))


class Task(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    text: str
    status: str = Field(default=TaskStatus.OPEN.value, sa_column=Column(String, index=True))
    due_date: datetime | None = Field(default=None, sa_column=_tz_column(nullable=True))
    goal_id: int | None = Field(default=None, foreign_key="goal.id")
    created_at: datetime = Field(default_factory=_utcnow, sa_column=_tz_column())
    completed_at: datetime | None = Field(default=None, sa_column=_tz_column(nullable=True))


class Purchase(SQLModel, table=True):
    """A purchase with a return window. Seeded by hand in Phase 3A; auto-created
    from Gmail receipts (via the quarantined reader) in Phase 3B."""
    id: int | None = Field(default=None, primary_key=True)
    vendor: str
    item: str
    amount: float | None = None
    currency: str = "USD"
    order_date: datetime | None = Field(default=None, sa_column=_tz_column(nullable=True))
    return_by: datetime | None = Field(default=None, sa_column=_tz_column(nullable=True))
    source: str = "seeded"  # "seeded" | "gmail:<message-id>"
    created_at: datetime = Field(default_factory=_utcnow, sa_column=_tz_column())


class Reminder(SQLModel, table=True):
    """A proactive reminder — one-off or recurring. Written by the reminder
    engine (add_reminder tool + the return-window flow). The reminder-tick job
    fires PENDING reminders whose due_at has passed.

    NOTE: this table already existed (Phase 1, empty). The columns below `task_id`
    are added by init_db()'s idempotent ALTER TABLE — create_all() does NOT alter
    an existing table, so new columns need the ALTER (see app/memory/db.py)."""
    id: int | None = Field(default=None, primary_key=True)
    text: str
    due_at: datetime = Field(sa_column=_tz_column())
    status: str = Field(default=ReminderStatus.PENDING.value, sa_column=Column(String, index=True))
    task_id: int | None = Field(default=None, foreign_key="task.id")
    created_at: datetime = Field(default_factory=_utcnow, sa_column=_tz_column())
    # Phase 3A additions:
    recurrence: str | None = None  # None = one-off; else "daily"/"weekly"/"monthly"
    kind: str = Field(default=ReminderKind.GENERIC.value, sa_column=Column(String, index=True))
    purchase_id: int | None = Field(default=None, foreign_key="purchase.id")
    calendar_event_id: str | None = None  # the mirrored Google Calendar event, if any
    sent_at: datetime | None = Field(default=None, sa_column=_tz_column(nullable=True))


class Event(SQLModel, table=True):
    """Episodic memory. Schema-ready this phase (same dormant-table treatment
    as Reminder); populated starting Phase 2/3."""
    id: int | None = Field(default=None, primary_key=True)
    text: str
    embedding: list[float] | None = Field(default=None, sa_column=Column(Vector(768), nullable=True))
    occurred_at: datetime = Field(default_factory=_utcnow, sa_column=_tz_column())
    created_at: datetime = Field(default_factory=_utcnow, sa_column=_tz_column())


class MessageLog(SQLModel, table=True):
    """Populated by deterministic code (a call from channels/telegram.py after
    each turn), not by an LLM tool — separate from the checkpointer's internal
    state so message history is directly queryable."""
    id: int | None = Field(default=None, primary_key=True)
    # Telegram chat_ids can exceed 32-bit INTEGER range (e.g. 8736433076) —
    # SQLModel's default int mapping is INTEGER, not BIGINT. Caught live,
    # against a real Telegram chat_id, not from reading the code.
    chat_id: int = Field(sa_column=Column(BigInteger, index=True))
    role: str  # "user" | "assistant"
    text: str
    created_at: datetime = Field(default_factory=_utcnow, sa_column=_tz_column())
