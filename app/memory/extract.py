"""Post-turn fact-capture sweep — the reliability fix for memory (an early, narrow slice
of the roadmap's Phase 5 consolidation).

The `remember_fact` tool fires only ~40% on the local 7B now that it competes with ~11
other tools for the model's attention. Rather than depend on the model choosing it
mid-turn, this runs a **dedicated, single-purpose local extraction** after each turn:
one focused prompt, structured output, no competing tools — the same reason the 3B
receipt reader is reliable. Facts are personal → this stays on the LOCAL model
(constitution). Extracted facts are de-duplicated against memory before storing.
"""

import logging

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field
from sqlmodel import Session

from app.config import settings
from app.memory import store
from app.memory.models import Provenance

log = logging.getLogger(__name__)


class ExtractedFacts(BaseModel):
    facts: list[str] = Field(
        default_factory=list,
        description="Short third-person fact strings for each durable personal fact the user stated.",
    )


_SYSTEM = SystemMessage(
    "You read ONE message from the user and extract any DURABLE personal facts they state about "
    "themselves or their life — preferences, relationships, important dates, ongoing situations, "
    'biographical details. Return each as a short third-person string (e.g. "allergic to peanuts", '
    '"brother Sam lives in Austin", "favorite season is fall"). Extract ONLY facts the user actually '
    "stated — never infer or invent. Questions, requests, greetings, and small talk contain no facts: "
    "return an empty list for those."
)


def extract_facts(user_message: str, *, extractor) -> list[str]:
    """Extract durable personal facts from one user message.

    `extractor` is injected (dependency inversion): the model that reads the message is an
    agent-layer concern (`app.agent.graph.fact_extractor` builds the SENSITIVE local model), so
    this module — a `app.memory` leaf — never imports up into `app.agent`. Tests pass a fake."""
    result = extractor.invoke([_SYSTEM, HumanMessage(user_message)])
    seen, out = set(), []
    for f in result.facts:
        f = (f or "").strip()
        if f and f.lower() not in seen:
            seen.add(f.lower())
            out.append(f[:300])
    return out[:10]  # bound per message


def sweep_and_store(session: Session, user_message: str, *, extractor) -> list[str]:
    """Extract facts from a user message and store the new ones (deduped against memory).
    Returns the list of newly-stored facts. Safe to call every turn. `extractor` is injected —
    see extract_facts."""
    stored = []
    for fact in extract_facts(user_message, extractor=extractor):
        hits = store.recall(session, query=fact, k=1)
        if hits and hits[0].similarity >= settings.fact_dedup_similarity:
            continue  # already know this (near-duplicate) — don't re-store
        store.remember_fact(session, text=fact, confidence=0.7, provenance=Provenance.INFERRED.value)
        stored.append(fact)
    return stored
