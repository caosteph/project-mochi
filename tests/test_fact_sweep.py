"""Phase 4A.2 — the post-turn fact-capture sweep. Offline: the extractor is faked and
store.recall/remember_fact are mocked, so no model, embeddings, or DB are touched.
"""

import asyncio
from types import SimpleNamespace

from app.memory import extract, store
from app.memory.models import Provenance


class FakeExtractor:
    def __init__(self, facts):
        self._facts = facts

    def invoke(self, messages):
        return extract.ExtractedFacts(facts=self._facts)


def test_extract_facts_dedups_strips_and_caps():
    out = extract.extract_facts("x", extractor=FakeExtractor(["  fall  ", "fall", "FALL", "", "  ", "peanuts"]))
    assert out == ["fall", "peanuts"]  # whitespace-stripped, case-insensitive dedup, blanks dropped


def test_new_fact_is_stored(monkeypatch):
    monkeypatch.setattr(store, "recall", lambda session, **kw: [])
    calls = []
    monkeypatch.setattr(store, "remember_fact", lambda session, **kw: calls.append(kw))
    stored = extract.sweep_and_store(None, "I'm allergic to peanuts", extractor=FakeExtractor(["allergic to peanuts"]))
    assert stored == ["allergic to peanuts"]
    assert len(calls) == 1
    assert calls[0]["text"] == "allergic to peanuts"
    assert calls[0]["provenance"] == Provenance.INFERRED.value


def test_duplicate_fact_is_skipped(monkeypatch):
    monkeypatch.setattr(store, "recall", lambda session, **kw: [SimpleNamespace(similarity=0.95)])
    calls = []
    monkeypatch.setattr(store, "remember_fact", lambda session, **kw: calls.append(kw))
    stored = extract.sweep_and_store(None, "peanuts again", extractor=FakeExtractor(["allergic to peanuts"]))
    assert stored == [] and calls == []  # near-duplicate (0.95 ≥ threshold) → not re-stored


def test_low_similarity_still_stores(monkeypatch):
    monkeypatch.setattr(store, "recall", lambda session, **kw: [SimpleNamespace(similarity=0.30)])
    calls = []
    monkeypatch.setattr(store, "remember_fact", lambda session, **kw: calls.append(kw))
    stored = extract.sweep_and_store(None, "new thing", extractor=FakeExtractor(["favorite season is fall"]))
    assert stored == ["favorite season is fall"] and len(calls) == 1


def test_no_facts_stores_nothing(monkeypatch):
    monkeypatch.setattr(store, "recall", lambda session, **kw: [])
    calls = []
    monkeypatch.setattr(store, "remember_fact", lambda session, **kw: calls.append(kw))
    assert extract.sweep_and_store(None, "what's the weather?", extractor=FakeExtractor([])) == []
    assert calls == []


def test_channel_fact_sweep_swallows_errors(channel, monkeypatch):
    from app.channels import telegram

    monkeypatch.setattr(telegram, "get_engine", lambda: (_ for _ in ()).throw(RuntimeError("no db")))
    # Must not raise even though the sweep blows up — the turn is already delivered.
    asyncio.run(channel._fact_sweep("I'm allergic to peanuts"))
