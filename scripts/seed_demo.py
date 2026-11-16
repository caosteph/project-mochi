"""Seed the demo database (docs/demo-mode.md).

Loads `demo/fixtures/profile.json` into the memory store (pinned facts, facts,
goals) and marks the email scan as already initialized so the first `/scan`
processes the fixture inbox instead of baseline-skipping it.

Idempotent: re-running deletes previously seeded demo rows first, so a fresh
demo is one command away.

Safety: refuses to run unless DEMO_MODE=true AND the DATABASE_URL looks like a
demo/test database — demo data must never land in a real database.

Usage (from the repo root, or as the `seed` service in docker-compose.yml):
    DEMO_MODE=true DATABASE_URL=postgresql://... uv run python scripts/seed_demo.py
"""

import json
import logging
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlmodel import Session, select

from app.config import settings
from app.memory import store
from app.memory.db import get_engine, init_db
from app.memory.embeddings import EmbeddingError
from app.memory.models import Fact, Goal, IngestState, Provenance

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_FIXTURES = _REPO_ROOT / "demo" / "fixtures"
_EMBED_ATTEMPTS = 10
_EMBED_RETRY_SECONDS = 15


def _fail(msg: str) -> "SystemExit":
    print(f"seed_demo: refusing to run: {msg}", file=sys.stderr)
    return SystemExit(2)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not settings.demo_mode:
        raise _fail("DEMO_MODE is not true — demo data must never seed a real database.")
    url = (settings.database_url or "").lower()
    if "demo" not in url and "test" not in url:
        raise _fail(
            f"DATABASE_URL {settings.database_url!r} does not look like a demo/test database."
        )

    profile = json.loads((_FIXTURES / "profile.json").read_text(encoding="utf-8"))
    pinned = profile.get("pinned_facts", [])
    facts = profile.get("facts", [])
    goals = profile.get("goals", [])
    all_texts = pinned + facts

    init_db()
    with Session(get_engine()) as session:
        # Idempotency: clear previously seeded demo rows (matched by exact text).
        for row in session.exec(select(Fact).where(Fact.text.in_(all_texts))):
            session.delete(row)
        for row in session.exec(select(Goal).where(Goal.text.in_(goals))):
            session.delete(row)
        session.commit()

        # Facts need embeddings (Ollama must be up with nomic-embed-text); retry
        # while the model is still pulling, then fail loudly instead of seeding
        # half a profile.
        for attempt in range(1, _EMBED_ATTEMPTS + 1):
            try:
                for text in pinned:
                    store.remember_fact(
                        session, text=text, confidence=1.0,
                        provenance=Provenance.IMPORTED, pinned=True,
                    )
                for text in facts:
                    store.remember_fact(
                        session, text=text, confidence=1.0,
                        provenance=Provenance.IMPORTED,
                    )
                break
            except EmbeddingError as exc:
                if attempt == _EMBED_ATTEMPTS:
                    raise SystemExit(f"seed_demo: embeddings unavailable after {attempt} tries: {exc}") from exc
                log.info("embeddings not ready yet (%s); retry %d/%d in %ds",
                         exc, attempt, _EMBED_ATTEMPTS, _EMBED_RETRY_SECONDS)
                time.sleep(_EMBED_RETRY_SECONDS)

        for text in goals:
            store.add_goal(session, text=text)

        # The scan baseline-skips everything on its first run unless initialized_at
        # is already set — backdate it a week so the fixture inbox (≤2 days old)
        # is treated as new mail on the first /scan. IngestState is a single row
        # (id=1); only initialized_at matters for the go-forward check.
        state = session.get(IngestState, 1) or IngestState(id=1)
        state.initialized_at = datetime.now(UTC) - timedelta(days=7)
        session.add(state)
        session.commit()

    print(
        f"seed_demo: seeded {len(pinned)} pinned fact(s), {len(facts)} fact(s), "
        f"{len(goals)} goal(s); email scan initialized to 7 days ago."
    )


if __name__ == "__main__":
    main()
