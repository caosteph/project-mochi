"""Phase 4A #4 — consult_expert now previews (require_approval) before anything leaves, exactly like
web_search: approval pauses the graph, a reject consults nothing, an approve consults once. Mirrors
the approval-gate tests in test_web_search.py. Offline — hosted is faked, nothing hits the network
(conftest also blocks non-local sockets).
"""

import uuid

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.types import Command
from sqlmodel import Session, select

from app.agent import router
from app.agent.tools import expert_tools
from app.agent.tools.expert_tools import consult_expert
from app.channels.render import render_proposal
from app.memory.models import HostedConsult
from tests.support import FakeModel


@pytest.fixture(autouse=True)
def _use_test_engine(engine, monkeypatch):
    """Point the tool's audit writes (get_engine) at the test DB — guarantees a HostedConsult row is
    never written to a real database."""
    monkeypatch.setattr(expert_tools, "get_engine", lambda: engine)


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(router, "hosted_available", lambda: True)
    monkeypatch.setattr("app.config.settings.redact_terms", "")


def _counting_model(monkeypatch, calls, answer="the answer"):
    def _cm(*a, **k):
        calls["n"] += 1
        return FakeModel(answer)

    monkeypatch.setattr(router, "chat_model", _cm)


def _graph():
    def inject(state):
        return {"messages": [AIMessage(
            "", tool_calls=[{"name": "consult_expert", "id": "c1", "type": "tool_call",
                             "args": {"question": "explain gradient descent simply"}}]
        )]}

    g = StateGraph(MessagesState)
    g.add_node("agent", inject)
    g.add_node("tools", ToolNode([consult_expert]))
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", tools_condition, {"tools": "tools", END: END})
    g.add_edge("tools", END)
    return g.compile(checkpointer=MemorySaver())


def test_consult_pauses_for_approval(enabled, engine, monkeypatch):
    calls = {"n": 0}
    _counting_model(monkeypatch, calls)
    app = _graph()
    cfg = {"configurable": {"thread_id": str(uuid.uuid4())}}
    result = app.invoke({"messages": []}, cfg)

    assert "__interrupt__" in result
    payload = result["__interrupt__"][0].value
    assert payload["action"] == "consult_expert" and payload["details"]["question"]
    assert calls["n"] == 0  # nothing consulted before approval
    with Session(engine) as s:
        assert list(s.exec(select(HostedConsult))) == []


def test_reject_consults_nothing(enabled, engine, monkeypatch):
    calls = {"n": 0}
    _counting_model(monkeypatch, calls)
    app = _graph()
    cfg = {"configurable": {"thread_id": str(uuid.uuid4())}}
    app.invoke({"messages": []}, cfg)
    result = app.invoke(Command(resume={"approved": False}), cfg)

    assert calls["n"] == 0
    assert "cancelled" in result["messages"][-1].content.lower()
    with Session(engine) as s:
        assert list(s.exec(select(HostedConsult))) == []


def test_approve_consults_once(enabled, engine, monkeypatch):
    calls = {"n": 0}
    _counting_model(monkeypatch, calls)
    app = _graph()
    cfg = {"configurable": {"thread_id": str(uuid.uuid4())}}
    app.invoke({"messages": []}, cfg)
    result = app.invoke(Command(resume={"approved": True}), cfg)

    assert calls["n"] == 1  # consulted exactly once (no double-count from the interrupt re-run)
    assert "the answer" in result["messages"][-1].content.lower()
    with Session(engine) as s:
        assert len(list(s.exec(select(HostedConsult)))) == 1


def test_render_proposal_consult_expert():
    text = render_proposal("consult_expert", {"question": "how do tides work"})
    assert "how do tides work" in text and "leave your machine" in text
