"""The always-on profile card: store.pinned_facts (selection) + profile.render_card (formatting).

Real embeddings, scratch DB — remember_fact embeds locally, so it can't be mocked away.
"""

from sqlmodel import Session

from app.agent import profile
from app.memory import store
from app.memory.models import Provenance


def _pin(session, text, *, confidence=0.9):
    return store.remember_fact(
        session, text=text, confidence=confidence, provenance=Provenance.IMPORTED.value, pinned=True
    )


def test_pinned_facts_filters_orders_and_caps(engine):
    with Session(engine) as s:
        store.remember_fact(s, text="an unpinned fact", confidence=1.0,
                            provenance=Provenance.IMPORTED.value, pinned=False)
        _pin(s, "low confidence rule", confidence=0.5)
        _pin(s, "top confidence rule", confidence=0.99)
        _pin(s, "middle confidence rule", confidence=0.8)

    with Session(engine) as s:
        top2 = store.pinned_facts(s, limit=2)
        # pinned only, highest confidence first, capped at the limit
        assert [f.text for f in top2] == ["top confidence rule", "middle confidence rule"]
        every = [f.text for f in store.pinned_facts(s, limit=50)]
        assert "an unpinned fact" not in every  # unpinned never enters the card
        assert len(every) == 3


def test_render_card_leads_with_a_directive_and_lists_facts(engine):
    with Session(engine) as s:
        _pin(s, "Stephanie never wants em dashes.")
        _pin(s, "Stephanie wants concise replies that lead with the answer.")

    with Session(engine) as s:
        card = profile.render_card(store.pinned_facts(s))
    assert card.startswith("\n\n---")                     # sits as its own block in the prefix
    assert "follow them in every reply" in card           # framed as standing instructions
    assert "- Stephanie never wants em dashes." in card   # each fact is a bullet


def test_render_card_is_empty_without_pins():
    # A fresh DB / CI (no pinned facts) must leave the system prompt untouched.
    assert profile.render_card([]) == ""
