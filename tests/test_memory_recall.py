import json
from pathlib import Path

import pytest
from sqlmodel import Session

from app.memory import store

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "memory_recall.json").read_text())


@pytest.fixture
def seeded_session(engine):
    with Session(engine) as session:
        for f in FIXTURES["facts"]:
            store.remember_fact(session, text=f["text"], confidence=f["confidence"], provenance=f["provenance"])
        yield session


@pytest.mark.parametrize("case", FIXTURES["queries"], ids=[c["query"] for c in FIXTURES["queries"]])
def test_recall_accuracy(seeded_session, case):
    hits = store.recall(seeded_session, query=case["query"], k=case["k"])
    texts = [h.fact.text for h in hits]
    assert any(case["expect_substring"] in t for t in texts), (
        f"query {case['query']!r} missed expected fact {case['expect_substring']!r}; got {texts}"
    )
