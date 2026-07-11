"""The summarization trim boundary must never leave a kept sequence starting
with an orphan ToolMessage (a tool response whose AIMessage(tool_calls) was
trimmed). Ollama tolerates that; stricter OpenAI-compatible endpoints — which
this project is designed to swap in — reject it. Pure logic, no LLM/DB needed.
"""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.graph import _trim_boundary


def _tool_call(name: str, call_id: str):
    return {"name": name, "args": {}, "id": call_id, "type": "tool_call"}


def test_boundary_skips_leading_tool_message():
    # keep_recent=6 would naively cut at index 2 — a ToolMessage. The boundary
    # must advance past it so the kept sequence starts on a clean message.
    messages = [
        HumanMessage("my dog is Biscuit", id="m0"),
        AIMessage("", id="m1", tool_calls=[_tool_call("remember_fact", "c1")]),
        ToolMessage("Remembered", id="m2", tool_call_id="c1"),
        AIMessage("Got it!", id="m3"),
        HumanMessage("I like hiking", id="m4"),
        AIMessage("Noted!", id="m5"),
        HumanMessage("what's up", id="m6"),
        AIMessage("Not much!", id="m7"),
    ]
    boundary = _trim_boundary(messages, keep_recent=6)
    kept = messages[boundary:]
    assert not isinstance(kept[0], ToolMessage), (
        f"kept sequence starts with an orphan ToolMessage: {[type(m).__name__ for m in kept]}"
    )


def test_boundary_unchanged_when_first_kept_is_clean():
    # keep_recent=3, first kept message is already a HumanMessage — no shift.
    messages = [
        HumanMessage("a", id="m0"),
        AIMessage("b", id="m1"),
        HumanMessage("c", id="m2"),
        AIMessage("d", id="m3"),
        HumanMessage("e", id="m4"),
    ]
    assert _trim_boundary(messages, keep_recent=3) == 2


def test_boundary_skips_consecutive_tool_messages():
    # Two tool responses in a row at the naive boundary (parallel tool calls).
    messages = [
        HumanMessage("hi", id="m0"),
        AIMessage("", id="m1", tool_calls=[_tool_call("a", "c1"), _tool_call("b", "c2")]),
        ToolMessage("r1", id="m2", tool_call_id="c1"),
        ToolMessage("r2", id="m3", tool_call_id="c2"),
        AIMessage("done", id="m4"),
        HumanMessage("next", id="m5"),
        AIMessage("ok", id="m6"),
    ]
    boundary = _trim_boundary(messages, keep_recent=5)  # naive cut = index 2
    kept = messages[boundary:]
    assert not isinstance(kept[0], ToolMessage), [type(m).__name__ for m in kept]
