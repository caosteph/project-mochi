"""The general choice-button primitive: ask_user → interrupt → tap → resume → chosen option.

She asked ~five times for tappable yes/no buttons and got prose she had to type "yes" at. This
covers the mechanism that replaces that: the `ask_user` tool pauses the graph with a choice, the
channel renders a button per option, her tap resumes with the index, and the tool hands the chosen
option back to the model.

Deterministic and offline: the "graph" is faked at the interrupt boundary, and the "bot" records
what it was told to send/edit. No model, no network.
"""

import asyncio
from types import SimpleNamespace

import pytest

from app.agent.confirm import ask_choice
from app.agent.tools.interaction_tools import ask_user
from app.channels import render, telegram

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

class Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        self.sent.append(SimpleNamespace(text=text, reply_markup=reply_markup))


def test_deliver_renders_one_button_per_option():
    chan = telegram.TelegramChannel.__new__(telegram.TelegramChannel)
    bot = Bot()
    payload = {"type": "choice", "question": "Which reminder?", "options": ["dentist", "mom"]}
    asyncio.run(chan._deliver(1, SimpleNamespace(bot=bot), payload, None))
    msg = bot.sent[-1]
    assert msg.text == "Which reminder?"
    buttons = [b for row in msg.reply_markup.inline_keyboard for b in row]
    assert [b.text for b in buttons] == ["dentist", "mom"]
    assert [b.callback_data for b in buttons] == ["ans:0", "ans:1"]


class Query:
    def __init__(self, data, buttons):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        self.data = data
        markup = InlineKeyboardMarkup([[InlineKeyboardButton(b, callback_data=f"ans:{i}")]
                                       for i, b in enumerate(buttons)])
        self.message = SimpleNamespace(text="Which reminder?", reply_markup=markup, message_id=7)
        self.toast = None
        self.edited = None

    async def answer(self, text=None, **kw):
        self.toast = text

    async def edit_message_text(self, text, **kw):
        self.edited = text

    async def edit_message_reply_markup(self, reply_markup=None):
        pass


def test_tapping_a_choice_resumes_with_the_index_and_shows_the_pick(monkeypatch):
    chan = telegram.TelegramChannel.__new__(telegram.TelegramChannel)
    monkeypatch.setattr(chan, "_authorized", lambda _u: True)

    resumed = {}

    async def fake_run(chat_id, ctx, graph_input, announce_thinking):
        resumed["command"] = graph_input
        return None, "ok", None

    async def fake_deliver(*a, **k):
        resumed["delivered"] = True

    monkeypatch.setattr(chan, "_run_with_status", fake_run)
    monkeypatch.setattr(chan, "_deliver", fake_deliver)

    query = Query("ans:1", ["dentist", "mom"])
    update = SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=1))
    asyncio.run(chan._on_callback(update, SimpleNamespace(bot=Bot())))

    # resumed with the tapped index
    assert resumed["command"].resume == {"choice": 1}
    assert resumed["delivered"]
    # native polish: toast named the pick, and the question was rewritten to its resolved state
    assert "mom" in (query.toast or "")
    assert query.edited == "Which reminder? → ✅ mom"


def test_choice_callback_from_an_unauthorized_chat_does_nothing(monkeypatch):
    chan = telegram.TelegramChannel.__new__(telegram.TelegramChannel)
    monkeypatch.setattr(chan, "_authorized", lambda _u: False)
    called = {"n": 0}

    async def boom(*a, **k):
        called["n"] += 1

    monkeypatch.setattr(chan, "_run_with_status", boom)
    query = Query("ans:0", ["a", "b"])
    update = SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=999))
    asyncio.run(chan._on_callback(update, SimpleNamespace(bot=Bot())))
    assert called["n"] == 0
