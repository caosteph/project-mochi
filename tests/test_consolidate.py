"""Memory consolidation, step C (shadow) — the deterministic half, with a fake judge.

The judge's *accuracy* is real-model behavior (scripts/verify_consolidation.py). Everything around it is
code, and it's what must hold unattended:
  - candidates: only current facts, at/above the threshold, most similar first, capped
  - code — not the model — decides which fact survives (duplicate: pinned > confidence > newer;
    contradiction: newer), and a pinned fact is only ever flagged, never queued
  - a judge failure or nonsense answer means "keep both"
  - no chains: a fact slated to go isn't judged again in the same run
  - it stops the moment she messages (Ollama serializes; her reply must not wait)
  - SHADOW WRITES NOTHING to memory
  - it runs at most once a day, and only while she's idle; a yielded/failed run retries

Hermetic: real Postgres + pgvector with hand-made vectors; no Ollama.
"""

import asyncio
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pytest
from sqlmodel import Session, select

from app.config import settings
from app.memory import consolidate, store
from app.memory.consolidate import FactView, PairVerdict
from app.memory.models import ConsolidationRun, Fact, MemoryChange, MessageLog, Provenance
from app.proactive import jobs

DIM = 768
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _unit(*w) -> list[float]:
    v = np.zeros(DIM)
    v[: len(w)] = w
    return list(v / np.linalg.norm(v))


def _add(session, text, vec, *, days=0, pinned=False, conf=0.8) -> int:
    f = Fact(text=text, embedding=vec, confidence=conf, provenance=Provenance.USER_STATED.value,
             pinned=pinned, created_at=T0 + timedelta(days=days))
    session.add(f)
    session.commit()
    return f.id


class Judge:
    """Answers from a {frozenset(texts): relation} table; records every pair it was asked about."""

    def __init__(self, table=None, default="distinct", on_call=None):
        self.table, self.default, self.on_call, self.asked = table or {}, default, on_call, []

    def invoke(self, messages):
        body = messages[-1].content
        a, b = body.removeprefix("Fact A: ").split("\nFact B: ")
        self.asked.append((a, b))
        if self.on_call:
            self.on_call()
        return PairVerdict(relation=self.table.get(frozenset((a, b)), self.default), reason="test")


def _fv(i, *, days=0, pinned=False, conf=0.8):
    return FactView(i, f"f{i}", pinned, conf, T0 + timedelta(days=days))


# --- candidates -------------------------------------------------------------------------------


def test_candidates_are_current_above_threshold_and_most_similar_first(engine):
    with Session(engine) as s:
        a = _add(s, "a", _unit(1, 0, 0))
        b = _add(s, "b", _unit(0.95, 0.31, 0))       # ~0.95 to a
        c = _add(s, "c", _unit(0.8, 0.6, 0))         # 0.8 to a
        _add(s, "far", _unit(0, 0, 1))
        gone = _add(s, "gone", _unit(1, 0.01, 0))    # ~1.0 to a, but superseded
        store.supersede(s, old_id=gone, new_id=c, run_id="x")
        pairs = consolidate.candidate_pairs(s, threshold=0.75, limit=10)
        got = [({p[0].id, p[1].id}, round(p[2], 2)) for p in pairs]
        assert got[0] == ({a, b}, 0.95)
        assert {a, c} in [g[0] for g in got] and all(gone not in g[0] for g in got)
        assert all(sim >= 0.75 for _, sim in got)
        assert len(consolidate.candidate_pairs(s, threshold=0.75, limit=1)) == 1


# --- code decides who survives ------------------------------------------------------------------


def test_duplicate_keeps_pinned_then_confidence_then_newer():
    v = PairVerdict(relation="duplicate")
    assert consolidate.decide(_fv(1, pinned=True), _fv(2, days=9, conf=0.99), v, 0.9).new.id == 1
    assert consolidate.decide(_fv(1, conf=0.95), _fv(2, days=9, conf=0.7), v, 0.9).new.id == 1
    assert consolidate.decide(_fv(1), _fv(2, days=9), v, 0.9).new.id == 2


def test_contradiction_always_keeps_the_newer_fact():
    p = consolidate.decide(_fv(1, conf=0.99), _fv(2, days=30, conf=0.5), PairVerdict(relation="contradiction"), 0.8)
    assert (p.old.id, p.new.id, p.blocked) == (1, 2, None)


def test_superseding_a_pinned_fact_is_only_ever_flagged():
    p = consolidate.decide(_fv(1, pinned=True), _fv(2, days=30), PairVerdict(relation="contradiction"), 0.8)
    assert p.old.id == 1 and p.blocked == "pinned"


def test_distinct_proposes_nothing():
    assert consolidate.decide(_fv(1), _fv(2), PairVerdict(relation="distinct"), 0.99) is None


@pytest.mark.parametrize("answer", [RuntimeError("timeout"), {"relation": "merge-them"}, {"nope": 1}])
def test_a_failed_or_nonsense_verdict_means_keep_both(answer):
    class Bad:
        def invoke(self, _m):
            if isinstance(answer, Exception):
                raise answer
            return answer

    assert consolidate.judge_pair(_fv(1), _fv(2), judge=Bad()).relation == "distinct"


def test_a_plain_dict_verdict_is_validated():
    class DictJudge:
        def invoke(self, _m):
            return {"relation": "duplicate", "reason": "same"}

    assert consolidate.judge_pair(_fv(1), _fv(2), judge=DictJudge()).relation == "duplicate"


# --- a whole pass ------------------------------------------------------------------------------------


@pytest.fixture
def trio(engine):
    """x≈y≈z all similar (y–z most, then x–y, then x–z); the tests make x/y the duplicates."""
    with Session(engine) as s:
        return {t: _add(s, t, _unit(*w), days=d) for t, w, d in
                [("x", (1, 0), 0), ("y", (0.98, 0.2), 5), ("z", (0.95, 0.31), 9)]}


def test_no_chains_a_fact_slated_to_go_is_not_judged_again(engine, trio):
    judge = Judge({frozenset(("x", "y")): "duplicate"})
    with Session(engine) as s:
        result = consolidate.consolidate(s, judge=judge, threshold=0.5, max_pairs=10)
    (p,) = result.proposals
    assert (p.old.text, p.new.text) == ("x", "y")
    # 3 candidates (y–z, x–y, x–z); once x is slated to go, x–z is skipped rather than judged
    assert result.candidates == 3 and result.judged == 2
    assert {frozenset(pair) for pair in judge.asked} == {frozenset(("y", "z")), frozenset(("x", "y"))}


def test_a_blocked_pinned_proposal_does_not_retire_the_fact(engine):
    with Session(engine) as s:
        _add(s, "pinned rule", _unit(1, 0), pinned=True)
        _add(s, "newer rule", _unit(0.99, 0.1), days=3)
        _add(s, "newest rule", _unit(0.98, 0.2), days=6)
        judge = Judge(default="contradiction")
        result = consolidate.consolidate(s, judge=judge, threshold=0.5, max_pairs=10)
    blocked = [p for p in result.proposals if p.blocked]
    assert blocked and all(p.old.text == "pinned rule" for p in blocked)


def test_it_stops_the_moment_she_messages(engine, trio):
    calls = []
    with Session(engine) as s:
        result = consolidate.consolidate(
            s, judge=Judge(), threshold=0.5, max_pairs=10, should_yield=lambda: len(calls) > 0 or calls.append(1)
        )
    assert result.yielded and result.judged == 1 and result.candidates == 3


def test_the_call_budget_caps_the_pass(engine, trio):
    with Session(engine) as s:
        result = consolidate.consolidate(s, judge=Judge(), threshold=0.5, max_pairs=2)
    assert result.candidates == 2 and result.judged == 2


def test_shadow_writes_nothing(engine, trio):
    with Session(engine) as s:
        result = consolidate.consolidate(s, judge=Judge(default="duplicate"), threshold=0.5, max_pairs=10)
        assert result.proposals
        assert s.exec(select(MemoryChange)).all() == []
        assert s.exec(select(Fact).where(Fact.superseded_by.is_not(None))).all() == []


# --- the daily job ------------------------------------------------------------------------------------


@pytest.fixture
def shadow(monkeypatch):
    monkeypatch.setattr(settings, "consolidation_mode", "shadow")
    monkeypatch.setattr(settings, "consolidation_idle_minutes", 60)
    monkeypatch.setattr(settings, "consolidation_min_interval_hours", 20)
    monkeypatch.setattr(settings, "consolidation_similarity_threshold", 0.5)


NOW = T0 + timedelta(days=100)


def _say(s, minutes_ago, role="user"):
    s.add(MessageLog(chat_id=1, role=role, text="hi", created_at=NOW - timedelta(minutes=minutes_ago)))
    s.commit()


def _run(s, minutes_ago, outcome):
    s.add(ConsolidationRun(run_id=outcome, mode="shadow", started_at=NOW - timedelta(minutes=minutes_ago),
                           outcome=outcome))
    s.commit()


def test_due_rules(engine, shadow, monkeypatch):
    with Session(engine) as s:
        assert jobs.consolidation_due(s, NOW) is None                      # never ran, never chatted
        _say(s, 10)
        assert jobs.consolidation_due(s, NOW) == "not idle"
        _say(s, 5, role="assistant")                                      # Mochi talking isn't her activity
    with Session(engine) as s:
        for m in s.exec(select(MessageLog).where(MessageLog.role == "user")).all():
            m.created_at = NOW - timedelta(hours=3)
            s.add(m)
        s.commit()
        assert jobs.consolidation_due(s, NOW) is None
        _run(s, 120, "yielded")
        _run(s, 60, "error: X")
        assert jobs.consolidation_due(s, NOW) is None                     # incomplete runs don't count
        _run(s, 300, "done")
        assert jobs.consolidation_due(s, NOW) == "ran recently"
    monkeypatch.setattr(settings, "consolidation_mode", "off")
    with Session(engine) as s:
        assert jobs.consolidation_due(s, NOW) == "off"


def test_a_pass_is_recorded_and_writes_no_memory(engine, shadow, trio, caplog):
    caplog.set_level("INFO", logger="app.memory.consolidate")
    with Session(engine) as s:
        summary = jobs.run_consolidation_pass(s, judge=Judge({frozenset(("x", "y")): "duplicate"}), now=NOW)
        assert summary["outcome"] == "done" and summary["proposals"] == 1 and summary["judged"] >= 1
        (run,) = s.exec(select(ConsolidationRun)).all()
        assert (run.outcome, run.proposals, run.finished_at is not None) == ("done", 1, True)
        assert s.exec(select(MemoryChange)).all() == []
        assert jobs.run_consolidation_pass(s, judge=Judge(), now=NOW + timedelta(hours=1)) is None  # once a day
    assert "SHADOW-CONSOLIDATE" in caplog.text and "keep_text='y'" in caplog.text


def test_a_message_mid_run_yields_and_a_later_hour_retries(engine, shadow, trio):
    with Session(engine) as s:
        def she_writes():
            s.add(MessageLog(chat_id=1, role="user", text="hey", created_at=NOW + timedelta(seconds=5)))
            s.commit()

        summary = jobs.run_consolidation_pass(s, judge=Judge(on_call=she_writes), now=NOW)
        assert summary["outcome"] == "yielded" and summary["judged"] == 1
        assert jobs.consolidation_due(s, NOW + timedelta(hours=2)) is None   # retried once she's idle again


def test_an_engine_failure_is_recorded_not_raised(engine, shadow, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(consolidate, "consolidate", boom)
    with Session(engine) as s:
        assert jobs.run_consolidation_pass(s, judge=Judge(), now=NOW)["outcome"] == "error: RuntimeError"


def test_the_job_is_a_noop_when_off(monkeypatch):
    monkeypatch.setattr(settings, "consolidation_mode", "off")
    monkeypatch.setattr(jobs, "_consolidation_blocking", lambda: pytest.fail("must not run"))
    asyncio.run(jobs.consolidation_job(SimpleNamespace()))


def test_status_shows_the_last_run(engine, shadow):
    from app import health

    with Session(engine) as s:
        _run(s, 90, "done")
    out = health.build_status(checks=[], proactivity_on=True, engine=engine, now=NOW)
    assert "Memory consolidation (shadow): 1h ago — done" in out


# --- the review script reads what log_shadow writes ---------------------------------------------------


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_review_script_parses_the_shadow_log(tmp_path, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="app.memory.consolidate")
    consolidate.log_shadow("r1", [
        consolidate.Proposal("contradiction", old=_fv(1), new=_fv(2, days=3), similarity=0.81, reason="moved"),
        consolidate.Proposal("duplicate", old=_fv(3, pinned=True), new=_fv(4), similarity=0.93, reason="same",
                             blocked="pinned"),
    ])
    (tmp_path / "scripts").mkdir()
    (tmp_path / "data").mkdir()
    shutil.copy("scripts/review_consolidation.sh", tmp_path / "scripts")
    (tmp_path / "data" / "mochi.log").write_text(
        "\n".join(f"2026-10-27 03:00:00 INFO app.memory.consolidate: {r.getMessage()}" for r in caplog.records)
        + "\n2026-10-27 03:01:00 INFO app.proactive.jobs: consolidation run r1 done: 2 candidates\n"
    )
    out = subprocess.run(["bash", str(tmp_path / "scripts" / "review_consolidation.sh")],
                         capture_output=True, text=True, check=True).stdout
    assert "shadow proposals: 2" in out and "completed runs in the log: 1" in out
    counts = {line.split()[1]: int(line.split()[0]) for line in out.split("by relation:")[1].splitlines()[1:3]}
    assert counts == {"contradiction": 1, "duplicate": 1} and "(1 touch a pinned fact" in out
    assert "KEEP 'f2'" in out and "DROP 'f1'" in out
