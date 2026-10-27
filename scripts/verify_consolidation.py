"""Live check: memory consolidation's judge on the REAL local model + real embeddings (step C).

The hermetic tests (tests/test_consolidate.py) prove the plumbing with a fake judge; this proves the
part that's actually stochastic — whether the 7B tells a duplicate from a contradiction from two facts
that are merely related. Seeds a scratch DB with known pairs, runs the production `consolidate()` in
shadow (nothing is written), and checks:

  capability (need 2 of 3):  a reworded duplicate is proposed as a duplicate
                             a moved city is proposed as a contradiction, keeping the NEWER fact
  must-not (need 3 of 3):    two related-but-both-true facts are never proposed
                             a pinned fact is never proposed for automatic supersession

The threshold is forced low here so every seeded pair reaches the judge — this measures the judge,
not the threshold (that's scripts/calibrate_consolidation.py's job).

    DATABASE_URL=postgresql://localhost/personal_agent_test PYTHONPATH=. uv run python scripts/verify_consolidation.py
"""

from datetime import UTC, datetime, timedelta

from scripts._verify_lib import bootstrap_env, require_scratch_db, sample_check, summarize_and_exit

require_scratch_db()
bootstrap_env()

from sqlalchemy import text  # noqa: E402
from sqlmodel import Session  # noqa: E402

from app.agent.graph import consolidation_judge  # noqa: E402
from app.memory import consolidate, store  # noqa: E402
from app.memory.db import get_engine, init_db  # noqa: E402

OLD = datetime.now(UTC) - timedelta(days=200)
SEED = [  # (text, created_at, pinned)
    ("allergic to peanuts", OLD, False),
    ("has a peanut allergy", OLD + timedelta(days=30), False),
    ("lives in Queens", OLD, False),
    ("moved to Brooklyn in October", OLD + timedelta(days=150), False),
    ("sister Lilian is a senior at Cornell", OLD, False),
    ("brother Sam lives in Austin", OLD, False),
    ("prefers short, direct replies", OLD, True),
    ("likes replies to be brief and to the point", OLD + timedelta(days=10), False),
]


def _seed(session) -> dict[str, int]:
    session.exec(text("TRUNCATE fact, memorychange RESTART IDENTITY CASCADE"))
    session.commit()
    ids = {}
    for t, created, pinned in SEED:
        f = store.remember_fact(session, text=t, confidence=0.9, provenance="user_stated", pinned=pinned)
        f.created_at = created
        session.add(f)
        session.commit()
        ids[t] = f.id
    return ids


def _run(session):
    return consolidate.consolidate(session, judge=consolidation_judge(), threshold=0.3, max_pairs=60)


def main() -> None:
    engine = get_engine()
    init_db(engine)
    with Session(engine) as session:
        ids = _seed(session)

        def proposes(relation, drop, keep):
            def probe():
                result = _run(session)
                hit = [p for p in result.proposals
                       if (p.old.id, p.new.id) == (ids[drop], ids[keep]) and p.relation == relation]
                return bool(hit), f"{len(result.proposals)} proposals"
            return probe

        def never(pred, what):
            def probe():
                bad = [p for p in _run(session).proposals if pred(p)]
                return not bad, (f"{what}: {[(p.relation, p.old.text, p.new.text) for p in bad]}" if bad else "")
            return probe

        sample_check("reworded duplicate is proposed as a duplicate",
                     proposes("duplicate", "allergic to peanuts", "has a peanut allergy"), samples=3, need=2)
        sample_check("moved city is a contradiction that keeps the NEWER fact",
                     proposes("contradiction", "lives in Queens", "moved to Brooklyn in October"), samples=3, need=2)
        siblings = {ids["sister Lilian is a senior at Cornell"], ids["brother Sam lives in Austin"]}
        sample_check("MUST-NOT: related-but-both-true facts are never proposed",
                     never(lambda p: {p.old.id, p.new.id} == siblings, "proposed"), samples=3, need=3)
        sample_check("MUST-NOT: a pinned fact is never queued for automatic supersession",
                     never(lambda p: p.old.pinned and p.blocked != "pinned", "unblocked pinned drop"),
                     samples=3, need=3)
        changed = session.exec(text("SELECT count(*) FROM memorychange")).one()[0]
        superseded = session.exec(text("SELECT count(*) FROM fact WHERE superseded_by IS NOT NULL")).one()[0]
        sample_check("shadow wrote nothing", lambda: (changed == 0 and superseded == 0,
                                                      f"{changed} changes, {superseded} superseded"), samples=1, need=1)
        session.exec(text("TRUNCATE fact, memorychange RESTART IDENTITY CASCADE"))
        session.commit()
    summarize_and_exit()


if __name__ == "__main__":
    main()
