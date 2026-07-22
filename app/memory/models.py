"""SQLModel tables. Phase 1 added the memory core (Fact/Goal/Task/Reminder/Event/
MessageLog); Phase 3A added Purchase; Phase 3B added the email-signal pipeline tables
(EmailSignal/ProcessedEmail/IngestState). Relationship is still deliberately absent
(Phase 5) — see docs/05-phase1-build.md's scope-trim note.
"""

from datetime import UTC, datetime
from enum import StrEnum

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, Column, DateTime, String
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(UTC)


# All datetime columns are explicitly TIMESTAMPTZ. SQLModel's default mapping
# for a plain `datetime` field creates a timezone-naive Postgres column, which
# silently drops the tzinfo on write — any later `now - created_at` comparison
# then raises "can't subtract offset-naive and offset-aware datetimes".
def _tz_column(*, nullable: bool = False) -> Column:
    return Column(DateTime(timezone=True), nullable=nullable)


class Provenance(StrEnum):
    USER_STATED = "user_stated"
    INFERRED = "inferred"
    IMPORTED = "imported"  # reserved for Phase 2+ (Gmail/Calendar-derived facts)


class GoalStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class TaskStatus(StrEnum):
    OPEN = "open"
    DONE = "done"
    CANCELLED = "cancelled"


class ReminderStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    DONE = "done"
    SNOOZED = "snoozed"
    CANCELLED = "cancelled"


class ReminderKind(StrEnum):
    GENERIC = "generic"          # user-created via add_reminder
    RETURN_WINDOW = "return_window"  # auto-created from a Purchase


class Recurrence(StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class SignalType(StrEnum):
    """The kind of actionable item the quarantined reader found in an email.
    A return is just one type — the whole point of Phase 3B is that this is a
    general pipeline, not a receipt-specific one. Adding a new type (e.g. a
    flight itinerary) is a new value here + one line of phrasing in suggest_text."""
    RETURN = "return"
    BILL = "bill"
    APPOINTMENT = "appointment"
    DEADLINE = "deadline"
    DELIVERY = "delivery"
    OTHER = "other"


class SignalStatus(StrEnum):
    DETECTED = "detected"      # extracted, approval ask not yet sent
    ASKED = "asked"            # approval ask sent, awaiting her yes/no (never re-asked)
    CONFIRMED = "confirmed"    # approved → a reminder was created
    DISMISSED = "dismissed"    # she said no


# Deadline-style signals fire a reminder BEFORE the due date (lead time); the
# others fire at the date. Consumed by reminders.create_from_signal.
DEADLINE_SIGNAL_TYPES = {SignalType.RETURN.value, SignalType.BILL.value, SignalType.DEADLINE.value}


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


class RetiredTopic(SQLModel, table=True):
    """A tombstone: "this underlying thing is over — never nag about it again."

    Reminders and tasks have per-row statuses, but cancelling one instance never stopped the next
    recreation (an email re-scan, a user re-add), because obsolescence was only ever recorded at
    the instance level. This is the topic-level mute the reminder- and signal-creation paths
    consult before making anything (see reminders.is_retired). `text` is matched fuzzily via
    text_match.same_thing, the same primitive de-dup and cancel use."""

    id: int | None = Field(default=None, primary_key=True)
    text: str
    created_at: datetime = Field(default_factory=_utcnow, sa_column=_tz_column())


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


class EmailSignal(SQLModel, table=True):
    """The general actionable item extracted from one email by the quarantined
    reader (app/agent/quarantine.py). Only these validated, length-capped fields
    are ever stored — never the raw email body (privacy + injection safety, rule
    #4). Lifecycle: detected → (approval) → confirmed | dismissed. On confirm, a
    reminder is created and linked via reminder_id."""
    id: int | None = Field(default=None, primary_key=True)
    source: str  # "gmail:<message-id>"
    signal_type: str = Field(default=SignalType.OTHER.value, sa_column=Column(String, index=True))
    title: str
    summary: str | None = None
    due_date: datetime | None = Field(default=None, sa_column=_tz_column(nullable=True))
    amount: float | None = None
    currency: str | None = None
    status: str = Field(default=SignalStatus.DETECTED.value, sa_column=Column(String, index=True))
    reminder_id: int | None = Field(default=None, foreign_key="reminder.id")
    created_at: datetime = Field(default_factory=_utcnow, sa_column=_tz_column())


class ProcessedEmail(SQLModel, table=True):
    """Dedup log for the receipt/signal scan: EVERY scanned message-id is recorded
    (not just the ones that yielded a signal), so a non-actionable email is never
    re-run through the model on the next scan. Stores only the id + an outcome tag
    — never any email content."""
    id: int | None = Field(default=None, primary_key=True)
    message_id: str = Field(sa_column=Column(String, unique=True, index=True))
    outcome: str  # "signal" | "skipped" | "baseline" | "error"
    processed_at: datetime = Field(default_factory=_utcnow, sa_column=_tz_column())


class IngestState(SQLModel, table=True):
    """Single-row marker for the email scan. `initialized_at` being set means the
    first (baseline) scan has run — implementing go-forward-only ingestion (no
    backfill of the pre-existing inbox) without a fragile 'is ProcessedEmail empty'
    heuristic."""
    id: int | None = Field(default=None, primary_key=True)
    initialized_at: datetime | None = Field(default=None, sa_column=_tz_column(nullable=True))


class HostedConsult(SQLModel, table=True):
    """Audit log of every call to the opt-in hosted model (Phase 4A) — the exact
    (already-scrubbed) text that left the machine, the answer, and how many redactions
    the deterministic scrubber made. This is the transparency half of the de-identified
    hybrid: Stephanie can review precisely what was sent externally (via /sent)."""
    id: int | None = Field(default=None, primary_key=True)
    sent_text: str
    answer: str
    n_redactions: int = 0
    created_at: datetime = Field(default_factory=_utcnow, sa_column=_tz_column())


class WebSearch(SQLModel, table=True):
    """Audit log of every web-search query that left the machine (Phase 8) — the exact
    (already-scrubbed) query, how many redactions the scrubber made, and how many results
    came back. Same transparency principle as HostedConsult: reviewable via /sent."""
    id: int | None = Field(default=None, primary_key=True)
    query: str
    n_redactions: int = 0
    n_results: int = 0
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
