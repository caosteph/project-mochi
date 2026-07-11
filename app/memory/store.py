"""CRUD + hybrid recall for Phase 1 memory. recall() implements the hybrid
retrieval sketched in docs/02-architectures.md: vector search + keyword search,
merged and reranked by similarity, keyword match, recency, and confidence.

Note: there's no separate "importance" signal in Phase 1 (no access-tracking
yet) — confidence stands in for it in the rerank formula below. This is a
simplification, not an oversight; revisit if/when real usage data exists.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import text
from sqlmodel import Session, select

from app.config import settings
from app.memory.embeddings import embed_local
from app.memory.models import Fact, Goal, MessageLog, Task


@dataclass
class RecallHit:
    fact: Fact
    score: float
    similarity: float


def remember_fact(session: Session, *, text: str, confidence: float, provenance: str) -> Fact:
    fact = Fact(text=text, embedding=embed_local(text), confidence=confidence, provenance=provenance)
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


def _vector_search(session: Session, query_vec: list[float], limit: int):
    stmt = (
        select(Fact, Fact.embedding.cosine_distance(query_vec).label("distance"))
        .order_by(Fact.embedding.cosine_distance(query_vec))
        .limit(limit)
    )
    return session.exec(stmt).all()


def _keyword_search(session: Session, query: str, limit: int):
    rows = session.exec(
        text(
            "SELECT id, ts_rank(to_tsvector('english', text), plainto_tsquery('english', :q)) AS rank "
            "FROM fact WHERE to_tsvector('english', text) @@ plainto_tsquery('english', :q) "
            "ORDER BY rank DESC LIMIT :limit"
        ),
        params={"q": query, "limit": limit},
    ).all()
    if not rows:
        return []
    ids = [r.id for r in rows]
    ranks = {r.id: r.rank for r in rows}
    facts = session.exec(select(Fact).where(Fact.id.in_(ids))).all()
    return [(f, ranks[f.id]) for f in facts]


def recall(session: Session, *, query: str, k: int | None = None) -> list[RecallHit]:
    k = settings.recall_default_k if k is None else k
    query_vec = embed_local(query)
    vec_hits = _vector_search(session, query_vec, settings.recall_candidate_limit)
    kw_hits = _keyword_search(session, query, settings.recall_candidate_limit)

    by_id: dict[int, dict] = {}
    max_kw_rank = max((r for _, r in kw_hits), default=0.0) or 1.0
    for fact, distance in vec_hits:
        by_id.setdefault(fact.id, {"fact": fact})["similarity"] = 1 - distance
    for fact, rank in kw_hits:
        by_id.setdefault(fact.id, {"fact": fact})["keyword"] = rank / max_kw_rank

    now = datetime.now(timezone.utc)
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
