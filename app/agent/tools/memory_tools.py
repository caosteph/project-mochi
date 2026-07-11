"""Agent-callable memory tools. Errors raise plain ValueError/RuntimeError —
ToolNode's default error handling catches these and returns an error
ToolMessage to the model instead of crashing the graph, so the model can
self-correct (e.g. retry with a valid date format).
"""

from datetime import date, datetime, timezone

from langchain_core.tools import tool
from sqlmodel import Session

from app.memory import store
from app.memory.db import get_engine
from app.memory.models import Provenance


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.combine(date.fromisoformat(value), datetime.min.time(), tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError(f"Invalid date {value!r}; expected YYYY-MM-DD") from exc


@tool
def remember_fact(text: str, confidence: float = 0.8, provenance: str = Provenance.USER_STATED.value) -> str:
    """Store a durable fact about Stephanie for later recall — preferences, biographical
    details, ongoing situations. provenance is "user_stated" when she said it directly,
    "inferred" when deduced from context. confidence is 0-1."""
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("confidence must be between 0 and 1")
    if provenance not in {p.value for p in Provenance}:
        raise ValueError(f"provenance must be one of {[p.value for p in Provenance]}")
    with Session(get_engine()) as session:
        fact = store.remember_fact(session, text=text, confidence=confidence, provenance=provenance)
        return f"Remembered (id={fact.id}): {fact.text}"


@tool
def recall(query: str, k: int = 8) -> str:
    """Search remembered facts relevant to query. Returns up to k, ranked by relevance,
    recency, and confidence."""
    with Session(get_engine()) as session:
        hits = store.recall(session, query=query, k=k)
    if not hits:
        return "No relevant facts found."
    return "\n".join(
        f"- {h.fact.text} (confidence={h.fact.confidence:.2f}, recorded {h.fact.created_at:%Y-%m-%d})"
        for h in hits
    )


@tool
def add_goal(text: str, target_date: str | None = None) -> str:
    """Record a goal Stephanie wants to work toward. target_date is optional, YYYY-MM-DD."""
    with Session(get_engine()) as session:
        goal = store.add_goal(session, text=text, target_date=_parse_date(target_date))
        return f"Goal added (id={goal.id}): {goal.text}"


@tool
def add_task(text: str, due_date: str | None = None, goal_id: int | None = None) -> str:
    """Record an actionable task. due_date is optional, YYYY-MM-DD. goal_id optionally
    links this task to a goal returned by add_goal."""
    with Session(get_engine()) as session:
        task = store.add_task(session, text=text, due_date=_parse_date(due_date), goal_id=goal_id)
        return f"Task added (id={task.id}): {task.text}"


MEMORY_TOOLS = [remember_fact, recall, add_goal, add_task]
