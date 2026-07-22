"""Tool selection must survive a bare confirmation.

The bug this pins, from Stephanie's 2026-07-21 conversation: she asked Mochi to cancel a
reminder, Mochi asked her to confirm, she said "yes" — and tool selection, which read only the
newest message, routed "yes" to nothing. `cancel_reminder` was never bound, so the model could
not call it. It printed the call as text (```json {"name": "cancel_reminder", ...}```) and then
said "The reminder has been removed." It had not been. She repeated herself eight times.

Two things are asserted here, because either alone would let it back in:
  1. the selector picks the right tool when given the conversation, and
  2. the graph actually FEEDS it the conversation rather than one message.

Deterministic — selection is pure, so this needs no model and runs in CI.
"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.agent import tool_select
from app.agent.graph import TOOL_SELECT_TURNS
from app.agent.tools import ALL_TOOLS


def selected(text: str) -> set[str]:
    return {t.name for t in tool_select.select_tools(text, ALL_TOOLS)}


def selected_for_turns(turns: list[str]) -> set[str]:
    return selected("\n".join(turns[-TOOL_SELECT_TURNS:]))


CONFIRMATIONS = ["yes", "yes please", "do it", "YES IVE ALREADY SAID SO", "do you understand?"]


def test_a_confirmation_alone_is_an_unreliable_routing_signal():
    """The premise of the bug, stated as what's actually true rather than more.

    A bare confirmation *sometimes* lands on the right tool by embedding luck — "do it" and
    "YES IVE ALREADY SAID SO" happen to. That's the trap: it works often enough to look fine and
    fails often enough to enrage. So the assertion is on the rate, not on any single phrase, and
    it's why the fix is "give the selector the conversation" rather than "add yes to KEYWORDS".
    """
    routed = [c for c in CONFIRMATIONS if "cancel_reminder" in selected(c)]
    assert len(routed) < len(CONFIRMATIONS), (
        "if every confirmation routed correctly on its own there'd be no bug to fix — "
        "re-check whether this fix is still needed"
    )


@pytest.mark.parametrize(
    "turns,want",
    [
        (["I already did the health insurance claims, remove that reminder", "yes"], "cancel_reminder"),
        (["cancel my reminder about the dentist", "yes please"], "cancel_reminder"),
        (["remind me to call mom on sunday", "yes"], "add_reminder"),
        (["remove the outdated reminder", "do you understand?"], "cancel_reminder"),
        (["what's on my calendar today?", "yes"], "calendar_list_events"),
    ],
)
def test_the_conversation_keeps_the_tool_bound_through_a_confirmation(turns, want):
    assert want in selected_for_turns(turns), (
        f"{want} must stay bound when the user answers {turns[-1]!r}; "
        f"got {sorted(selected_for_turns(turns))}"
    )


def test_single_turn_prompts_are_unaffected():
    """The fix must not cost accuracy on the ordinary case."""
    for prompt, want in [
        ("remind me to call mom every Sunday", "add_reminder"),
        ("what's on my calendar today?", "calendar_list_events"),
        ("build me a landing page for my bakery", "build_web_app"),
        ("what did the landlord's email say?", "read_email"),
        ("what's the weather in Paris right now?", "web_search"),
        ("draft an email to alex saying hi", "create_draft"),
    ]:
        assert want in selected_for_turns([prompt]), f"{want} for {prompt!r}"


def test_the_graph_feeds_recent_turns_into_selection_not_just_the_last():
    """Guards the wiring, not the selector. A refactor could quietly go back to passing one
    message and every test above would still pass."""
    captured = {}

    def spy(text, tools, **kw):
        captured["text"] = text
        return tool_select.select_tools(text, tools, **kw)

    from app.agent import graph

    state = {
        "messages": [
            HumanMessage("cancel my reminder about the dentist"),
            AIMessage("Do you want me to cancel it?"),
            HumanMessage("yes"),
        ],
        "summary": "",
    }
    original = graph.tool_select.select_tools
    graph.tool_select.select_tools = spy
    try:
        graph._agent_node(state)
    except Exception:
        pass  # the model call may fail offline; we only care what selection was handed
    finally:
        graph.tool_select.select_tools = original

    assert "dentist" in captured.get("text", ""), (
        "selection must see the earlier turn that carries the intent, not only 'yes'"
    )


def test_routing_survives_the_embedding_backend_being_down():
    """The degraded path, which CI runs every time (no Ollama) and production hits whenever the
    embedding call fails. `select_tools` already falls back to keyword routing — but the keyword
    list matched fixed strings, so "remove THAT reminder" and "remove the OUTDATED reminder" both
    fell through and cancelling silently stopped working. REGEX_BOOSTS covers the phrasings.
    """
    from unittest.mock import patch

    cases = [
        (["I already did the health insurance claims, remove that reminder", "yes"], "cancel_reminder"),
        (["cancel my reminder about the dentist", "yes please"], "cancel_reminder"),
        (["remove the outdated reminder", "do you understand?"], "cancel_reminder"),
        (["remind me to call mom on sunday", "yes"], "add_reminder"),
        (["what's on my calendar today?", "yes"], "calendar_list_events"),
    ]
    with patch.object(tool_select, "embed_local", side_effect=RuntimeError("ollama down")):
        for turns, want in cases:
            names = {t.name for t in tool_select.select_tools("\n".join(turns), ALL_TOOLS)}
            assert want in names, f"{want} unbound without embeddings for {turns[-1]!r}: {sorted(names)}"


def test_retire_task_binds_on_her_phrasings_including_without_embeddings():
    """The staleness fix is worthless if the tool isn't bound when she says the trigger phrases.
    Checked both with embeddings and on the keyword/regex-only fallback (CI / Ollama down)."""
    from unittest.mock import patch

    phrasings = [
        "I already did the health insurance claims, stop reminding me",
        "I got rejected from perplexity, no need to keep reminding me about it",
        "I already submitted the form",
        "I don't need that reminder anymore",
    ]
    for p in phrasings:  # with embeddings
        assert "retire_task" in selected(p), f"retire_task not bound for {p!r}"
    with patch.object(tool_select, "embed_local", side_effect=RuntimeError("no ollama")):
        for p in phrasings:  # keyword/regex only
            assert "retire_task" in selected(p), f"retire_task not bound (no embeddings) for {p!r}"
