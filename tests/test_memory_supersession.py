"""Soft supersession — the data layer memory consolidation will write through.

What has to hold, because consolidation will run unattended at night:
  - a superseded fact vanishes from EVERY user-facing read (recall, the profile card, /facts,
    the count) — one forgotten query and an outdated fact keeps steering replies
  - it stays visible to capture-time dedup, so re-stating it in an old message can't resurrect it
  - the guards refuse (and write nothing) on: self-supersession, missing or already-superseded
    facts, and ANY pinned fact — the always-on card is proposed to her, never auto-changed
  - undo restores exactly the link the change made, and is idempotent

Hermetic: facts carry hand-made vectors and `embed_local` is patched, so the real pgvector and
full-text queries run with no Ollama.
"""

import json

import pytest
from sqlmodel import Session, select

from app.memory import extract, store
from app.memory.models import Fact, MemoryChange, Provenance
from scripts import import_profile

DIM = 768


def _vec(i: int) -> list[float]:
    v = [0.0] * DIM
    v[i] = 1.0
    return v


VECS = {"lives in Queens": _vec(0), "moved to Brooklyn": _vec(1), "likes hiking": _vec(2)}


@pytest.fixture(autouse=True)
def fake_embed(monkeypatch):
    """Known texts map to their vector; anything else to an unrelated one."""
    embed = lambda text: VECS.get(text, _vec(DIM - 1))  # noqa: E731
    monkeypatch.setattr(store, "embed_local", embed)
    return embed


def _fact(session, text, *, pinned=False) -> int:
    f = Fact(text=text, embedding=VECS[text], confidence=0.9, provenance=Provenance.USER_STATED.value,
             pinned=pinned)
    session.add(f)
    session.commit()
    return f.id


@pytest.fixture
def moved(engine):
    """'lives in Queens' superseded by 'moved to Brooklyn'; 'likes hiking' untouched."""
    with Session(engine) as s:
        queens, brooklyn, hiking = (_fact(s, t) for t in ("lives in Queens", "moved to Brooklyn", "likes hiking"))
        change = store.supersede(s, old_id=queens, new_id=brooklyn, run_id="r1", reason="moved")
        return {"queens": queens, "brooklyn": brooklyn, "hiking": hiking, "change": change.id}


def _texts(facts) -> set[str]:
    return {f.text for f in facts}


def test_supersede_logs_a_reversible_change(engine, moved):
    with Session(engine) as s:
        change = s.get(MemoryChange, moved["change"])
        old = s.get(Fact, moved["queens"])
        assert (change.kind, change.old_fact_id, change.new_fact_id) == ("supersede", moved["queens"], moved["brooklyn"])
        assert (change.before_text, change.after_text, change.reason) == ("lives in Queens", "moved to Brooklyn", "moved")
        assert old.superseded_by == moved["brooklyn"] and old.superseded_at is not None


def test_a_superseded_fact_vanishes_from_every_user_facing_read(engine, moved):
    with Session(engine) as s:
        assert "lives in Queens" not in _texts(store.list_facts(s, limit=50))
        assert store.count_facts(s) == 2
        assert "lives in Queens" not in {h.fact.text for h in store.recall(s, query="lives in Queens", k=5)}
        assert "lives in Queens" not in {h.fact.text for h in store.recall(s, query="Queens", k=5)}  # keyword path


def test_a_superseded_pinned_fact_drops_off_the_profile_card(engine):
    """Only reachable by a manual supersede of something unpinned that was pinned later — but the
    card's query must hold on its own, not rely on the guard."""
    with Session(engine) as s:
        queens, brooklyn = _fact(s, "lives in Queens"), _fact(s, "moved to Brooklyn")
        store.supersede(s, old_id=queens, new_id=brooklyn, run_id="r")
        store.set_pinned(s, fact_id=queens, pinned=True)
        assert "lives in Queens" not in _texts(store.pinned_facts(s))


def test_capture_dedup_still_sees_superseded_facts(engine, moved):
    with Session(engine) as s:
        hits = store.recall(s, query="lives in Queens", k=1, include_superseded=True)
        assert hits[0].fact.text == "lives in Queens" and hits[0].similarity == pytest.approx(1.0)


def test_the_fact_sweep_does_not_resurrect_a_superseded_fact(engine, moved):
    class Extractor:
        def invoke(self, _messages):
            return extract.ExtractedFacts(facts=["lives in Queens"])

    with Session(engine) as s:
        stored = extract.sweep_and_store(s, "ugh, back when I lived in Queens…", extractor=Extractor())
        assert stored == []
        assert len(list(s.exec(select(Fact).where(Fact.text == "lives in Queens")))) == 1


def test_the_importer_neither_restores_nor_repins_a_superseded_fact(engine, moved, tmp_path, monkeypatch):
    profile = tmp_path / "p.json"
    profile.write_text(json.dumps({"facts": [
        {"text": "lives in Queens", "category": "communication", "confidence": 0.9},  # a pinned category
    ]}))
    monkeypatch.setattr("sys.argv", ["import_profile.py", "--input", str(profile), "--commit"])
    import_profile.main()
    with Session(engine) as s:
        rows = list(s.exec(select(Fact).where(Fact.text == "lives in Queens")))
        assert len(rows) == 1 and rows[0].pinned is False and rows[0].superseded_by == moved["brooklyn"]


# --- guards: refuse and write nothing ---------------------------------------------------


@pytest.mark.parametrize("case", ["self", "missing", "old_already", "new_already", "pinned"])
def test_guards_refuse_without_writing(engine, case):
    with Session(engine) as s:
        a, b = _fact(s, "lives in Queens", pinned=(case == "pinned")), _fact(s, "moved to Brooklyn")
        c = _fact(s, "likes hiking")
        if case in ("old_already", "new_already"):
            store.supersede(s, old_id=(a if case == "old_already" else b), new_id=c, run_id="setup")
        before = s.exec(select(MemoryChange)).all().__len__()
        old_id, new_id = {"self": (a, a), "missing": (a, 9999)}.get(case, (a, b))
        with pytest.raises(store.SupersedeError):
            store.supersede(s, old_id=old_id, new_id=new_id, run_id="r")
        assert len(s.exec(select(MemoryChange)).all()) == before
        s.expire_all()
        if case in ("self", "missing", "pinned"):
            assert s.get(Fact, a).superseded_by is None


# --- undo ----------------------------------------------------------------------------


def test_undo_restores_the_fact_everywhere(engine, moved):
    with Session(engine) as s:
        assert store.undo_change(s, moved["change"]) is True
        assert "lives in Queens" in _texts(store.list_facts(s, limit=50))
        assert store.count_facts(s) == 3
        assert s.get(MemoryChange, moved["change"]).undone_at is not None


def test_undo_is_idempotent_and_ignores_unknown_ids(engine, moved):
    with Session(engine) as s:
        assert store.undo_change(s, moved["change"]) is True
        assert store.undo_change(s, moved["change"]) is False
        assert store.undo_change(s, 424242) is False


def test_undo_only_removes_the_link_it_made(engine, moved):
    """If the old fact was later re-pointed (undo, then superseded by something else), undoing the
    first change must not clear the second link."""
    with Session(engine) as s:
        store.undo_change(s, moved["change"])
        second = store.supersede(s, old_id=moved["queens"], new_id=moved["hiking"], run_id="r2")
        s.get(MemoryChange, moved["change"]).undone_at = None  # simulate a stale first change
        s.commit()
        assert store.undo_change(s, moved["change"]) is False
        assert s.get(Fact, moved["queens"]).superseded_by == second.new_fact_id
