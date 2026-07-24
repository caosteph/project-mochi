"""scripts/import_profile.py — seed Mochi's memory from an external agent's profile export.

Drives the real importer end-to-end against the scratch DB with real local embeddings (recall-based
dedup is the whole point, so it can't be mocked away). Each test writes a profile file, runs `main()`
with patched argv, and asserts DB state.
"""

import json

import pytest
from sqlmodel import Session, select

from app.memory.models import Fact, Goal, GoalStatus, Provenance
from scripts import import_profile


def _run(tmp_path, monkeypatch, profile_text: str, *, commit: bool):
    """Write `profile_text` to a temp file and run the importer over it. Returns the exit code."""
    p = tmp_path / "profile.json"
    p.write_text(profile_text, encoding="utf-8")
    argv = ["import_profile.py", "--input", str(p)]
    if commit:
        argv.append("--commit")
    monkeypatch.setattr("sys.argv", argv)
    return import_profile.main()


def _facts(engine) -> list[Fact]:
    with Session(engine) as s:
        return list(s.exec(select(Fact)))


# --- 1. commit stores facts with imported provenance ------------------------

def test_commit_stores_facts_with_imported_provenance(tmp_path, monkeypatch, engine):
    profile = json.dumps({"facts": [
        {"text": "Stephanie's sister Lilian is a senior at Cornell", "category": "relationships", "confidence": 0.95},
        {"text": "Stephanie is allergic to shellfish", "category": "health", "confidence": 0.9},
    ]})
    rc = _run(tmp_path, monkeypatch, profile, commit=True)
    assert rc == 0
    facts = _facts(engine)
    assert len(facts) == 2
    assert all(f.provenance == Provenance.IMPORTED.value for f in facts)
    assert {f.text for f in facts} == {
        "Stephanie's sister Lilian is a senior at Cornell",
        "Stephanie is allergic to shellfish",
    }
    # confidence carried through from the source
    conf = {f.text: f.confidence for f in facts}
    assert conf["Stephanie is allergic to shellfish"] == pytest.approx(0.9)


# --- 2. a near-duplicate of an existing fact is deduped, not stored ----------

def test_dedupes_against_existing_memory(tmp_path, monkeypatch, engine, seed):
    seed.fact("Stephanie's sister Lilian is a senior at Cornell")  # already known
    profile = json.dumps({"facts": [
        {"text": "Stephanie's sister Lilian is a senior at Cornell", "category": "relationships"},  # dup
        {"text": "Stephanie's favorite season is fall", "category": "preferences"},                 # new
    ]})
    _run(tmp_path, monkeypatch, profile, commit=True)
    facts = _facts(engine)
    # the pre-seeded one + the one genuinely new fact = 2 total (the duplicate was skipped)
    assert len(facts) == 2
    assert any(f.text == "Stephanie's favorite season is fall" for f in facts)
    # nothing imported over the top of the pre-existing (user_stated) row
    imported = [f for f in facts if f.provenance == Provenance.IMPORTED.value]
    assert {f.text for f in imported} == {"Stephanie's favorite season is fall"}


# --- 3. goals become active Goal rows ---------------------------------------

def test_goals_are_stored(tmp_path, monkeypatch, engine):
    profile = json.dumps({
        "facts": [{"text": "Stephanie runs to stay in shape", "category": "health"}],
        "goals": [{"text": "Get in shape before the Greece trip", "target_date": "2026-08-15"}],
    })
    _run(tmp_path, monkeypatch, profile, commit=True)
    with Session(engine) as s:
        goals = list(s.exec(select(Goal)))
    assert len(goals) == 1
    assert goals[0].text == "Get in shape before the Greece trip"
    assert goals[0].status == GoalStatus.ACTIVE.value
    assert goals[0].target_date is not None and goals[0].target_date.year == 2026


# --- 3b. goal dedup is semantic + high-bar: distinct goals survive, re-runs don't -----

def test_goal_dedup_keeps_distinct_related_goals(tmp_path, monkeypatch, engine):
    """Regression: same_thing word-overlap wrongly merged "Book Greece accommodations" into "Get in
    shape for Greece" because both say "Greece". Semantic dedup at a high bar keeps them distinct."""
    profile = json.dumps({"goals": [
        {"text": "Get in shape for Greece trip (flatter stomach, consistent movement)"},
        {"text": "Book and finalize Greece trip accommodations in Athens and Corfu with Ben"},
        {"text": "Build a consistent fitness routine through group classes"},
    ]})
    _run(tmp_path, monkeypatch, profile, commit=True)
    with Session(engine) as s:
        texts = {g.text for g in s.exec(select(Goal))}
    assert len(texts) == 3  # all three distinct objectives survived


def test_goal_dedup_skips_a_true_rerun(tmp_path, monkeypatch, engine):
    """Re-importing the same file is idempotent for goals (an exact goal is not duplicated)."""
    profile = json.dumps({"goals": [{"text": "Explore applying to Stanford GSB"}]})
    _run(tmp_path, monkeypatch, profile, commit=True)
    _run(tmp_path, monkeypatch, profile, commit=True)  # second run
    with Session(engine) as s:
        assert len(list(s.exec(select(Goal)))) == 1  # not duplicated


# --- 4. dry run (the default) writes nothing ---------------------------------

def test_dry_run_stores_nothing(tmp_path, monkeypatch, engine):
    profile = json.dumps({
        "facts": [{"text": "Stephanie is allergic to shellfish", "category": "health"}],
        "goals": [{"text": "Learn to surf"}],
    })
    rc = _run(tmp_path, monkeypatch, profile, commit=False)  # no --commit
    assert rc == 0
    assert _facts(engine) == []
    with Session(engine) as s:
        assert list(s.exec(select(Goal))) == []


# --- 5. parse tolerates a ```json fence + surrounding prose ------------------

def test_parse_tolerates_markdown_fence_and_prose(tmp_path, monkeypatch, engine):
    profile = (
        "Sure! Here's the profile you asked for:\n\n"
        "```json\n"
        '{"facts": [{"text": "Stephanie lives in New York City", "category": "identity"}]}\n'
        "```\n"
        "Let me know if you'd like more detail."
    )
    rc = _run(tmp_path, monkeypatch, profile, commit=True)
    assert rc == 0
    facts = _facts(engine)
    assert len(facts) == 1 and facts[0].text == "Stephanie lives in New York City"


# --- 6. unit: confidence is clamped and defaulted ---------------------------

def test_confidence_clamped_and_defaulted():
    assert import_profile._clamp_confidence(2.0) == 1.0
    assert import_profile._clamp_confidence(-0.5) == 0.0
    assert import_profile._clamp_confidence(None) == 0.8   # missing → default
    assert import_profile._clamp_confidence("junk") == 0.8
