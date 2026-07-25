"""The graph's repair node: what happens to a tool call the model wrote as text.

Decision table (drawn from the 2026-07-24 failure): a text tool-call for a tool that was BOUND this
turn is a mis-format -> execute it; for a tool that was NOT bound it's topic bleed -> strip it and keep
the prose. A per-turn budget stops repair loops. Hermetic: each case keeps prose around the JSON so the
strip path never falls back to the (model-invoking) plain-reply branch.
"""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.graph import _repair_node

_CALL = 'Sure, doing that. ```json\n{"name": "build_web_app", "arguments": {"description": "x"}}\n```'


def _state(content, *, bound, repair_count=0, msg=None):
    last = msg if msg is not None else AIMessage(content=content, id="m1")
    return {"messages": [HumanMessage("hi"), last], "bound_tools": bound, "repair_count": repair_count}


def test_bound_text_call_is_executed():
    out = _repair_node(_state(_CALL, bound=["build_web_app"]))
    msg = out["messages"][0]
    assert msg.tool_calls and msg.tool_calls[0]["name"] == "build_web_app"
    assert msg.tool_calls[0]["args"] == {"description": "x"}
    assert msg.content == "Sure, doing that."  # JSON stripped from the visible content
    assert out["repair_count"] == 1


def test_unbound_text_call_is_stripped_not_executed():
    out = _repair_node(_state(_CALL, bound=["recall", "add_reminder"]))
    msg = out["messages"][0]
    assert not getattr(msg, "tool_calls", None)   # NOT executed (topic bleed)
    assert msg.content == "Sure, doing that."     # she sees only the prose


def test_repair_budget_prevents_loops():
    # Already repaired once this turn -> even a bound call is stripped, not executed again.
    out = _repair_node(_state(_CALL, bound=["build_web_app"], repair_count=1))
    msg = out["messages"][0]
    assert not getattr(msg, "tool_calls", None)
    assert msg.content == "Sure, doing that."


def test_unknown_tool_is_stripped():
    call = 'Ok. ```json\n{"name": "not_a_real_tool", "arguments": {}}\n```'
    out = _repair_node(_state(call, bound=["not_a_real_tool"]))
    assert not getattr(out["messages"][0], "tool_calls", None)
    assert out["messages"][0].content == "Ok."


def test_plain_prose_is_a_noop():
    assert _repair_node(_state("Your trip is on August 15th.", bound=["recall"])) == {}


def test_real_tool_call_is_left_alone():
    ai = AIMessage(content="", id="m1", tool_calls=[{"name": "recall", "args": {}, "id": "t1"}])
    assert _repair_node(_state("", bound=["recall"], msg=ai)) == {}


def test_non_ai_last_message_is_a_noop():
    state = {"messages": [HumanMessage("when is my trip?")], "bound_tools": [], "repair_count": 0}
    assert _repair_node(state) == {}
    tool_last = {"messages": [ToolMessage(content="done", tool_call_id="t1")],
                 "bound_tools": [], "repair_count": 0}
    assert _repair_node(tool_last) == {}
