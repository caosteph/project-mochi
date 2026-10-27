"""CRUD + hybrid recall for Phase 1 memory. recall() implements the hybrid
retrieval sketched in docs/02-architectures.md: vector search + keyword search,
merged and reranked by similarity, keyword match, recency, and confidence.

Note: there's no separate "importance" signal in Phase 1 (no access-tracking
yet) — confidence stands in for it in the rerank formula below. This is a
simplification, not an oversight; revisit if/when real usage data exists.
"""

import math
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text
from sqlmodel import Session, select

from app.config import settings
from app.memory.embeddings import embed_local
from app.memory.models import Fact, Goal, MemoryChange, MessageLog, Task

# The one definition of "a fact she currently holds". Every user-facing read goes through it, so a
# superseded fact can't leak back into recall, the profile card, or /facts by a query forgetting it.
_active = Fact.superseded_by.is_(None)


@dataclass
class RecallHit:
    fact: Fact
    score: float
    similarity: float


def remember_fact(
    session: Session, *, text: str, confidence: float, provenance: str, pinned: bool = False
) -> Fact:
    fact = Fact(text=text, embedding=embed_local(text), confidence=confidence,
                provenance=provenance, pinned=pinned)
    session.add(fact)
    session.commit()
    session.refresh(fact)
    return fact


def pinned_facts(session: Session, *, limit: int | None = None) -> list[Fact]:
    """The always-on profile facts, highest-confidence first — the source of the profile card
    injected into every system prompt (app/agent/profile.py). Bounded so the card can't grow
    unwatched past the prompt budget."""
    limit = settings.profile_card_max_facts if limit is None else limit
    return list(session.exec(
        select(Fact).where(Fact.pinned, _active).order_by(Fact.confidence.desc(), Fact.created_at).limit(limit)
    ))


def list_facts(session: Session, *, limit: int) -> list[Fact]:
    """Facts for the `/facts` view: pinned first (they're the always-on card), then most-recent.
    Bounded by `limit`. Callers must read the returned rows' attributes before the session closes."""
    return list(session.exec(
        select(Fact).where(_active).order_by(Fact.pinned.desc(), Fact.created_at.desc()).limit(limit)
    ))


def count_facts(session: Session) -> int:
    return len(list(session.exec(select(Fact.id).where(_active))))


def set_pinned(session: Session, *, fact_id: int, pinned: bool) -> Fact | None:
    """Pin/unpin one fact by id (the `/pin` `/unpin` commands). Returns the updated row, or None if
    no fact has that id. The caller must invalidate the profile-card cache after a real change."""
    fact = session.get(Fact, fact_id)
    if fact is None:
        return None
    fact.pinned = pinned
    session.add(fact)
    session.commit()
    session.refresh(fact)
    return fact


def add_goal(session: Session, *, text: str, target_date: datetime | None = None) -> Goal:
    goal = Goal(text=text, target_date=target_date)
    session.add(goal)
    session.commit()
    session.refresh(goal)
    return goal


def add_task(
    session: Session, *, text: str, due_date: datetime | None = None, goal_id: int | None = None
) -> Task:
    task = Task(text=text, due_date=due_date, goal_id=goal_id)
    session.add(task)
    session.commit()
    session.refresh(task)
    return task


def log_message(session: Session, *, chat_id: int, role: str, text: str) -> None:
    session.add(MessageLog(chat_id=chat_id, role=role, text=text))
    session.commit()


def _vector_search(session: Session, query_vec: list[float], limit: int, *, include_superseded: bool):
    stmt = select(Fact, Fact.embedding.cosine_distance(query_vec).label("distance"))
    if not include_superseded:
        stmt = stmt.where(_active)
    stmt = stmt.order_by(Fact.embedding.cosine_distance(query_vec)).limit(limit)
    return session.exec(stmt).all()


def _keyword_search(session: Session, query: str, limit: int, *, include_superseded: bool):
    active = "" if include_superseded else "AND superseded_by IS NULL "
    rows = session.exec(
        text(
            "SELECT id, ts_rank(to_tsvector('english', text), plainto_tsquery('english', :q)) AS rank "
            "FROM fact WHERE to_tsvector('english', text) @@ plainto_tsquery('english', :q) "
            f"{active}ORDER BY rank DESC LIMIT :limit"
        ),
        params={"q": query, "limit": limit},
    ).all()
    if not rows:
        return []
    ids = [r.id for r in rows]
    ranks = {r.id: r.rank for r in rows}
    facts = session.exec(select(Fact).where(Fact.id.in_(ids))).all()
    return [(f, ranks[f.id]) for f in facts]


def recall(
    session: Session, *, query: str, k: int | None = None, include_superseded: bool = False
) -> list[RecallHit]:
    """Hybrid recall over the facts she currently holds. `include_superseded=True` is ONLY for
    capture-time dedup (the fact sweep, the profile importer): a fact consolidation replaced must
    still count as "already known", or re-stating it in an old message would resurrect it."""
    k = settings.recall_default_k if k is None else k
    query_vec = embed_local(query)
    vec_hits = _vector_search(session, query_vec, settings.recall_candidate_limit,
                              include_superseded=include_superseded)
    kw_hits = _keyword_search(session, query, settings.recall_candidate_limit,
                              include_superseded=include_superseded)

    by_id: dict[int, dict] = {}
    max_kw_rank = max((r for _, r in kw_hits), default=0.0) or 1.0
    for fact, distance in vec_hits:
        by_id.setdefault(fact.id, {"fact": fact})["similarity"] = 1 - distance
    for fact, rank in kw_hits:
        by_id.setdefault(fact.id, {"fact": fact})["keyword"] = rank / max_kw_rank

    now = datetime.now(UTC)
    scored = []
    for entry in by_id.values():
        fact = entry["fact"]
        similarity = entry.get("similarity", 0.0)
        keyword = entry.get("keyword", 0.0)
        age_days = (now - fact.created_at).total_seconds() / 86400
        recency = math.exp(-age_days / settings.recall_recency_half_life_days)
        score = (
            settings.recall_similarity_weight * similarity
            + settings.recall_keyword_weight * keyword
            + settings.recall_recency_weight * recency
            + settings.recall_confidence_weight * fact.confidence
        )
        scored.append(RecallHit(fact=fact, score=score, similarity=similarity))

    scored.sort(key=lambda h: h.score, reverse=True)
    return scored[:k]


# --- soft supersession (memory consolidation) ------------------------------------------


class SupersedeError(ValueError):
    """A supersession the guards refuse — never applied, never partially applied."""


def supersede(
    session: Session, *, old_id: int, new_id: int, run_id: str, reason: str = ""
) -> MemoryChange:
    """Mark `old_id` as replaced by `new_id`, logging a reversible MemoryChange. Refuses (raises
    SupersedeError, writes nothing): a fact superseding itself, a missing or already-superseded fact
    on either side, and — always — a PINNED fact: the always-on card shapes every reply, so changing
    it is proposed to her, never done automatically."""
    if old_id == new_id:
        raise SupersedeError("a fact can't supersede itself")
    old, new = session.get(Fact, old_id), session.get(Fact, new_id)
    if old is None or new is None:
        raise SupersedeError(f"no such fact: {old_id if old is None else new_id}")
    if old.superseded_by is not None or new.superseded_by is not None:
        raise SupersedeError("both facts must be current")
    if old.pinned:
        raise SupersedeError(f"fact {old_id} is pinned (always-on card) — propose, don't auto-change")
    now = datetime.now(UTC)
    old.superseded_by, old.superseded_at = new_id, now
    change = MemoryChange(run_id=run_id, kind="supersede", old_fact_id=old_id, new_fact_id=new_id,
                          before_text=old.text, after_text=new.text, reason=reason, created_at=now)
    session.add(old)
    session.add(change)
    session.commit()
    session.refresh(change)
    return change


def undo_change(session: Session, change_id: int) -> bool:
    """Revert one supersession: the old fact is current again. Idempotent — False if the change
    doesn't exist, was already undone, or the old fact has since been re-pointed elsewhere (only
    the link this change made is ever removed)."""
    change = session.get(MemoryChange, change_id)
    if change is None or change.undone_at is not None or change.kind != "supersede":
        return False
    old = session.get(Fact, change.old_fact_id)
    if old is None or old.superseded_by != change.new_fact_id:
        return False
    old.superseded_by = old.superseded_at = None
    change.undone_at = datetime.now(UTC)
    session.add(old)
    session.add(change)
    session.commit()
    return True
