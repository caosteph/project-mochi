"""The safety property that matters most this phase: create_draft NEVER writes to
Gmail without explicit approval, and only writes after approval. Tested against the
real tool + confirm gate + interrupt/resume, with Gmail mocked (no network, no creds).
Deterministic — the tool call is injected, not chosen by the model.
"""

import uuid

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.types import Command

from app.agent.tools.google_tools import create_draft
from app.integrations import google_gmail

DRAFT_ARGS = {"to": "sam@example.com", "subject": "Hi", "body": "Hello Sam"}


def _build_graph(args: dict | None = None):
    args = args or DRAFT_ARGS

    def inject_tool_call(state: MessagesState):
        return {
            "messages": [
                AIMessage(
                    "",
                    tool_calls=[
                        {"name": "create_draft", "id": "c1", "type": "tool_call", "args": args}
                    ],
                )
            ]
        }

    g = StateGraph(MessagesState)
    g.add_node("agent", inject_tool_call)
    g.add_node("tools", ToolNode([create_draft]))
    g.add_edge(START, "agent")
    # After the tool runs, go straight to END (don't loop back into the injector).
    g.add_conditional_edges("agent", tools_condition, {"tools": "tools", END: END})
    g.add_edge("tools", END)
    return g.compile(checkpointer=MemorySaver())


@pytest.fixture
def spy_create_draft(monkeypatch):
    calls = []

    def _spy(to, subject, body, **kwargs):
        calls.append({"to": to, "subject": subject, "body": body})
        return {"id": "draft_123"}

    monkeypatch.setattr(google_gmail, "create_draft", _spy)
    return calls


def test_draft_pauses_for_approval_with_full_proposal(spy_create_draft):
    app = _build_graph()
    cfg = {"configurable": {"thread_id": str(uuid.uuid4())}}
    result = app.invoke({"messages": []}, cfg)

    assert "__interrupt__" in result, "create_draft must pause for approval"
    payload = result["__interrupt__"][0].value
    assert payload["action"] == "create_draft"
    assert payload["details"] == DRAFT_ARGS  # the human sees the real to/subject/body
    assert spy_create_draft == [], "must not write to Gmail before approval"


def test_reject_writes_nothing(spy_create_draft):
    app = _build_graph()
    cfg = {"configurable": {"thread_id": str(uuid.uuid4())}}
    app.invoke({"messages": []}, cfg)
    result = app.invoke(Command(resume={"approved": False}), cfg)

    assert spy_create_draft == [], "rejected draft must never touch Gmail"
    assert "cancelled" in result["messages"][-1].content.lower()


def test_approve_writes_once(spy_create_draft):
    app = _build_graph()
    cfg = {"configurable": {"thread_id": str(uuid.uuid4())}}
    app.invoke({"messages": []}, cfg)
    result = app.invoke(Command(resume={"approved": True}), cfg)

    assert spy_create_draft == [DRAFT_ARGS], "approved draft must be created exactly once"
    assert "draft_123" in result["messages"][-1].content


def test_self_recipient_resolves_to_own_address(spy_create_draft, monkeypatch):
    # "to=me" (or myself/self/empty) is resolved to her real address, and the
    # proposal she approves shows the resolved address — not the literal "me".
    monkeypatch.setattr(google_gmail, "get_own_address", lambda **k: "steph@example.com")
    app = _build_graph({"to": "me", "subject": "Hi", "body": "note to self"})
    cfg = {"configurable": {"thread_id": str(uuid.uuid4())}}

    result = app.invoke({"messages": []}, cfg)
    assert result["__interrupt__"][0].value["details"]["to"] == "steph@example.com"

    app.invoke(Command(resume={"approved": True}), cfg)
    assert spy_create_draft == [{"to": "steph@example.com", "subject": "Hi", "body": "note to self"}]
