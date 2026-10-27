"""scripts/calibrate_consolidation.py — sets consolidation's grouping threshold from her real facts.

It runs against the LIVE database, so the property that matters most is pinned first: Postgres
itself refuses any write on its connection. Then the analysis: pairs/similarities, the stratified
sample (must reach the low-similarity bands, where a wordless contradiction hides), the threshold
table, and the recommendation's refusals (too few labels; nothing reaches the target; over budget).

Hermetic: real Postgres with hand-made vectors; no Ollama.
"""

import json

import numpy as np
import psycopg
import pytest
from sqlmodel import Session

from app.config import settings
from app.memory import store
from app.memory.models import Fact, Provenance
from scripts import calibrate_consolidation as cc

DIM = 768


def _unit(*weights) -> list[float]:
    v = np.zeros(DIM)
    v[: len(weights)] = weights
    return list(v / np.linalg.norm(v))


def _facts(n_pairs_by_sim: dict[float, int]) -> list[cc.Pair]:
    """Synthetic pairs at chosen similarities (the analysis only reads .sim and ids)."""
    out, i = [], 0
    for sim, n in n_pairs_by_sim.items():
        for _ in range(n):
            out.append(cc.Pair(cc.FactRow(i, f"f{i}", False), cc.FactRow(i + 1, f"f{i+1}", False), sim))
            i += 2
    return sorted(out, key=lambda p: p.sim, reverse=True)


# --- the safety property --------------------------------------------------------------------


def test_the_connection_cannot_write(engine):
    with cc.connect_read_only(settings.database_url) as conn, pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        conn.execute("INSERT INTO fact (text, confidence, provenance, pinned, created_at) "
                     "VALUES ('x', 0.5, 'inferred', false, now())")


def test_load_facts_reads_current_facts_only(engine):
    with Session(engine) as s:
        rows = [Fact(text=t, embedding=_unit(*w), confidence=0.9, provenance=Provenance.USER_STATED.value)
                for t, w in [("lives in Queens", (1, 0)), ("moved to Brooklyn", (0.6, 0.8)), ("likes hiking", (0, 1))]]
        s.add_all(rows)
        s.commit()
        ids = [r.id for r in rows]
        store.supersede(s, old_id=ids[0], new_id=ids[1], run_id="r")
    with cc.connect_read_only(settings.database_url) as conn:
        facts, vecs = cc.load_facts(conn)
    assert [f.text for f in facts] == ["moved to Brooklyn", "likes hiking"]
    assert np.allclose(np.linalg.norm(vecs, axis=1), 1.0)
    (pair,) = cc.all_pairs(facts, vecs)
    assert pair.sim == pytest.approx(0.8)


# --- pure analysis -----------------------------------------------------------------------------


def test_all_pairs_are_sorted_most_similar_first():
    facts = [cc.FactRow(i, str(i), False) for i in range(3)]
    vecs = np.array([[1, 0], [0.8, 0.6], [0, 1]], dtype=float)
    pairs = cc.all_pairs(facts, vecs)
    assert [round(p.sim, 2) for p in pairs] == [0.8, 0.6, 0.0]
    assert pairs[0].key == "0-1"


def test_stratified_sample_reaches_the_low_bands_and_skips_labelled():
    pairs = _facts({0.95: 30, 0.75: 30, 0.55: 30, 0.2: 30})
    labelled = {pairs[0].key}
    sample = cc.stratified_sample(pairs, 20, labelled, seed=1)
    assert len(sample) == 20 and pairs[0].key not in {p.key for p in sample}
    assert any(p.sim < 0.5 for p in sample), "the band where wordless contradictions hide must be sampled"
    assert sample == cc.stratified_sample(pairs, 20, labelled, seed=1)  # deterministic


def test_a_thin_band_donates_its_share():
    pairs = _facts({0.95: 1, 0.2: 50})
    assert len(cc.stratified_sample(pairs, 10, set())) == 10


def test_threshold_table_counts_calls_and_related_caught():
    pairs = _facts({0.9: 2, 0.6: 2, 0.3: 2})
    labels = {pairs[0].key: True, pairs[2].key: True, pairs[4].key: True, pairs[5].key: False}
    rows = {r.threshold: r for r in cc.threshold_table(pairs, labels)}
    assert (rows[0.85].candidates, rows[0.85].related_caught, rows[0.85].related_total) == (2, 1, 3)
    assert (rows[0.55].candidates, rows[0.55].related_caught) == (4, 2)


def test_recommend_picks_the_highest_threshold_meeting_recall():
    pairs = _facts({0.9: 5, 0.6: 5, 0.3: 20})
    labels = {p.key: True for p in pairs if p.sim >= 0.6}
    best, why = cc.recommend(cc.threshold_table(pairs, labels), target_recall=0.9, max_candidates=100)
    assert best.threshold == 0.6 and best.candidates == 10 and "use 0.60" in why


@pytest.mark.parametrize(("labels_sims", "budget", "expect"), [
    ([0.9, 0.9], 100, "label more"),                         # too few related labels to trust
    ([0.2] * 6, 100, "can't group them"),                    # related facts embed far apart
    ([0.45] * 6, 5, "raise the budget"),                     # reachable, but over the call budget
])
def test_recommend_refuses_when_it_cannot_honestly_answer(labels_sims, budget, expect):
    pairs = _facts({0.9: 10, 0.45: 6, 0.2: 6})
    pick = {s: [p for p in pairs if p.sim == s] for s in set(labels_sims)}
    labels = {pick[s].pop().key: True for s in labels_sims}
    _best, why = cc.recommend(cc.threshold_table(pairs, labels), target_recall=0.9, max_candidates=budget)
    assert expect in why


# --- labelling + the whole script --------------------------------------------------------------


def test_labelling_saves_after_every_answer_and_quit_keeps_progress(tmp_path):
    path = tmp_path / "labels.json"
    sample = _facts({0.9: 1, 0.5: 1, 0.3: 1})
    answers = iter(["y", "huh", "n", "q"])
    labels: dict[str, bool] = {}
    assert cc.label_interactively(sample, labels, path, ask=lambda _p: next(answers)) == 2
    assert json.loads(path.read_text()) == {sample[0].key: True, sample[1].key: False}


def test_main_end_to_end_against_the_db(engine, tmp_path, monkeypatch, capsys):
    with Session(engine) as s:
        for t, w in [("a", (1, 0, 0)), ("b", (0.9, 0.1, 0)), ("c", (0, 0, 1))]:
            s.add(Fact(text=t, embedding=_unit(*w), confidence=0.9, provenance=Provenance.INFERRED.value))
        s.commit()
    answers = iter(["y", "n", "n"])
    monkeypatch.setattr("builtins.input", lambda _p: next(answers))
    assert cc.main(["--label", "3", "--labels", str(tmp_path / "l.json")]) == 0
    out = capsys.readouterr().out
    assert "3 fact pairs" in out and "recommendation:" in out and "label more" in out
    assert len(json.loads((tmp_path / "l.json").read_text())) == 3
