"""`/facts`, `/pin`, `/unpin` — viewing memory and curating the always-on profile card.

The behaviour that matters: pinning/unpinning a fact must (1) persist and (2) invalidate the cached
profile card, so the change lands on the very next reply instead of after a restart (the card cache
was the one piece of state a naive implementation would forget). Hermetic: facts are inserted
directly with a zero vector, so no Ollama is needed and this runs in CI.
"""

import asyncio

import pytest
from sqlmodel import Session, select

from app.agent import graph
from app.memory.models import Fact, Provenance
from tests.support import FakeMessage, make_update


def _cmd(text=""):
    return make_update(message=FakeMessage(text))


def _fact(engine, text, *, pinned=False, confidence=0.8) -> int:
    """Insert a Fact directly (dummy embedding) and return its id — no embedding model involved."""
    with Session(engine) as s:
        f = Fact(text=text, embedding=[0.0] * 768, confidence=confidence,
                 provenance=Provenance.USER_STATED.value, pinned=pinned)
        s.add(f)
        s.commit()
        s.refresh(f)
        return f.id


def _pinned_ids(engine) -> set[int]:
    with Session(engine) as s:
        return {f.id for f in s.exec(select(Fact).where(Fact.pinned))}


@pytest.fixture(autouse=True)
def _reset_card_cache():
    """The card cache is a module global; keep tests from leaking a value into each other."""
    yield
    graph._profile_card_cache = None


# --- /facts -----------------------------------------------------------------


def test_facts_lists_with_pinned_marked_and_ids(channel, ctx, engine):
    fid_pin = _fact(engine, "Stephanie never wants em dashes.", pinned=True)
    fid_plain = _fact(engine, "Stephanie lives in New York City.")
    update = _cmd("/facts")
    asyncio.run(channel._on_facts(update, ctx))
    out = update.message.replies[0]
    assert f"* {fid_pin}." in out                 # pinned fact is marked
    assert f"{fid_plain}." in out                 # every fact carries its id
    assert "em dashes" in out
    assert "/pin" in out and "/unpin" in out       # tells her how to change what's always-on


def test_facts_when_empty_says_so(channel, ctx, engine):
    update = _cmd("/facts")
    asyncio.run(channel._on_facts(update, ctx))
    assert "haven't stored any facts" in update.message.replies[0]


# --- /pin and /unpin --------------------------------------------------------


def test_pin_persists_and_invalidates_the_card(channel, ctx, engine):
    fid = _fact(engine, "Stephanie prefers concise replies that lead with the answer.")
    graph._profile_card_cache = "STALE CARD"       # pretend a card is already cached this process
    update = _cmd(f"/pin {fid}")
    asyncio.run(channel._on_pin(update, ctx))
    assert fid in _pinned_ids(engine)              # (1) persisted
    assert graph._profile_card_cache is None       # (2) cache dropped -> rebuilds next reply
    assert "every reply" in update.message.replies[0].lower()


def test_unpin_persists_and_invalidates_the_card(channel, ctx, engine):
    fid = _fact(engine, "Stephanie prefers concise replies.", pinned=True)
    graph._profile_card_cache = "STALE CARD"
    update = _cmd(f"/unpin {fid}")
    asyncio.run(channel._on_unpin(update, ctx))
    assert fid not in _pinned_ids(engine)
    assert graph._profile_card_cache is None
    assert "no longer" in update.message.replies[0].lower()


def test_pin_without_a_number_explains_itself_and_changes_nothing(channel, ctx, engine):
    _fact(engine, "some fact")
    update = _cmd("/pin")
    asyncio.run(channel._on_pin(update, ctx))
    assert "Usage" in update.message.replies[0]
    assert _pinned_ids(engine) == set()


def test_pin_unknown_id_says_so_and_does_not_bust_the_cache(channel, ctx, engine):
    graph._profile_card_cache = "STALE CARD"       # a no-op must NOT invalidate the cache
    update = _cmd("/pin 9999")
    asyncio.run(channel._on_pin(update, ctx))
    assert "No fact #9999" in update.message.replies[0]
    assert graph._profile_card_cache == "STALE CARD"
