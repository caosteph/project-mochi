"""The general choice-button primitive: ask_user → interrupt → tap → resume → chosen option.

She asked ~five times for tappable yes/no buttons and got prose she had to type "yes" at. This
covers the mechanism that replaces that: the `ask_user` tool pauses the graph with a choice, the
channel renders a button per option, her tap resumes with the index, and the tool hands the chosen
option back to the model.

Deterministic and offline: the "graph" is faked at the interrupt boundary, and the shared FakeBot /
FakeQuery record what the channel was told to send/edit (see tests/support).
"""

import asyncio

import pytest

from app.agent.confirm import ask_choice
from app.agent.tools.interaction_tools import ask_user
from app.channels import render
from tests.support import FakeQuery, inline_markup, make_update

# --- the tool + confirm helper, at the interrupt boundary -------------------


def test_ask_choice_returns_the_index_the_channel_resumes_with(monkeypatch):
    # interrupt() returns whatever Command(resume=...) carried — simulate a tap on option 1.
    monkeypatch.setattr("app.agent.confirm.interrupt", lambda payload: {"choice": 1})
    assert ask_choice("Which one?", ["a", "b", "c"]) == 1


def test_ask_choice_is_out_of_range_safe(monkeypatch):
    monkeypatch.setattr("app.agent.confirm.interrupt", lambda payload: {"choice": 9})
    assert ask_choice("Which one?", ["a", "b"]) == -1


def test_ask_choice_payload_shape_is_what_the_channel_expects(monkeypatch):
    seen = {}
    monkeypatch.setattr("app.agent.confirm.interrupt", lambda payload: seen.update(payload) or {"choice": 0})
    ask_choice("Cancel which?", ["x", "y"])
    assert seen["type"] == "choice"
    assert seen["question"] == "Cancel which?"
    assert seen["options"] == ["x", "y"]


def test_ask_user_tool_returns_the_chosen_option_string(monkeypatch):
    monkeypatch.setattr("app.agent.confirm.interrupt", lambda payload: {"choice": 2})
    assert ask_user.invoke({"question": "Pick", "options": ["red", "green", "blue"]}) == "blue"


def test_ask_user_refuses_fewer_than_two_options(monkeypatch):
    # A one-button question is a dead-end; the tool should reject it so the model rephrases.
    monkeypatch.setattr("app.agent.confirm.interrupt", lambda payload: {"choice": 0})
    with pytest.raises(ValueError, match="two concrete options"):
        ask_user.invoke({"question": "Only one?", "options": ["yes"]})


# --- rendering --------------------------------------------------------------


def test_render_choice_is_just_the_question():
    assert render.render_choice("Which reminder?") == "Which reminder?"


def test_render_resolved_choice_shows_the_pick():
    assert render.render_resolved_choice("Which reminder?", "dentist") == "Which reminder? → ✅ dentist"


# --- the channel side: buttons out, tap in ----------------------------------


def test_deliver_renders_one_button_per_option(channel, ctx, fake_bot):
    payload = {"type": "choice", "question": "Which reminder?", "options": ["dentist", "mom"]}
    asyncio.run(channel._deliver(1, ctx, payload, None))
    assert fake_bot.last.text == "Which reminder?"
    assert fake_bot.last.button_labels == ["dentist", "mom"]
    assert fake_bot.last.callback_data == ["ans:0", "ans:1"]


def test_tapping_a_choice_resumes_with_the_index_and_shows_the_pick(channel, ctx, monkeypatch):
    resumed = {}

    async def fake_run(chat_id, ctx_, graph_input, announce_thinking):
        resumed["command"] = graph_input
        return None, "ok", None

    async def fake_deliver(*a, **k):
        resumed["delivered"] = True

    monkeypatch.setattr(channel, "_run_with_status", fake_run)
    monkeypatch.setattr(channel, "_deliver", fake_deliver)

    query = FakeQuery("ans:1", message=_choice_message(["dentist", "mom"]))
    asyncio.run(channel._on_callback(make_update(callback_query=query), ctx))

    assert resumed["command"].resume == {"choice": 1}
    assert resumed["delivered"]
    # native polish: toast named the pick, and the question was rewritten to its resolved state
    assert "mom" in (query.toast or "")
    assert query.edited == "Which reminder? → ✅ mom"


def test_choice_callback_from_an_unauthorized_chat_does_nothing(channel, ctx, monkeypatch):
    monkeypatch.setattr(channel, "_authorized", lambda _u: False)
    called = {"n": 0}

    async def boom(*a, **k):
        called["n"] += 1

    monkeypatch.setattr(channel, "_run_with_status", boom)
    query = FakeQuery("ans:0", message=_choice_message(["a", "b"]))
    asyncio.run(channel._on_callback(make_update(callback_query=query, chat_id=999), ctx))
    assert called["n"] == 0


def _choice_message(labels):
    from types import SimpleNamespace

    markup = inline_markup([(lbl, f"ans:{i}") for i, lbl in enumerate(labels)])
    return SimpleNamespace(text="Which reminder?", reply_markup=markup, message_id=7)
