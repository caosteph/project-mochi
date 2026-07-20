"""Web search (Phase 8) — offline. Two layers:
  - logic tests (approval stubbed True): scrub / refuse / available / no-results / framing /
    audit / rate-limit. The load-bearing one is `test_query_is_scrubbed_before_it_leaves`:
    only a PII-scrubbed query may ever reach the provider.
  - approval-gate tests through a REAL graph (like test_confirm_gate): the search pauses for
    Approve/Reject, a reject searches nothing, an approve searches exactly once.
Nothing hits the network (the provider is monkeypatched; conftest also blocks non-local sockets).
"""

import uuid

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.types import Command
from sqlmodel import Session, select

from app.agent.tools import web_tools
from app.agent.tools.web_tools import web_search
from app.integrations import web_search as search_api
from app.integrations.web_search import SearchResult
from app.memory.models import WebSearch


def _results(n=2):
    return [SearchResult(title=f"T{i}", url=f"https://ex/{i}", snippet=f"snippet {i}") for i in range(n)]


@pytest.fixture(autouse=True)
def _use_test_engine(engine, monkeypatch):
    """Point the tool's audit writes (get_engine) at the test DB — makes the suite hermetic
    w.r.t. DATABASE_URL and guarantees a WebSearch row is never written to a real database."""
    monkeypatch.setattr(web_tools, "get_engine", lambda: engine)


@pytest.fixture
def enabled(monkeypatch):
    # duckduckgo → available() is True without a key; a term to prove name-scrubbing works.
    monkeypatch.setattr("app.config.settings.web_search_enabled", True)
    monkeypatch.setattr("app.config.settings.web_search_provider", "duckduckgo")
    monkeypatch.setattr("app.config.settings.redact_terms", "Stephanie,Cao")


def _approve(monkeypatch, ok=True):
    monkeypatch.setattr(web_tools, "require_approval", lambda *a, **k: ok)


# --- logic (approval stubbed) -----------------------------------------------

def test_query_is_scrubbed_before_it_leaves(enabled, engine, monkeypatch):
    """The whole privacy point: PII in the query is redacted before the provider (and the
    audit) ever sees it."""
    _approve(monkeypatch)
    captured = {}
    monkeypatch.setattr(search_api, "search", lambda q, **k: captured.update(query=q) or _results())

    web_search.invoke({"query": "email jane@corp.com about Stephanie's 555-123-4567 appointment"})

    q = captured["query"]
    assert "jane@corp.com" not in q and "Stephanie" not in q and "555-123-4567" not in q
    assert "[redacted]" in q
    with Session(engine) as s:
        rows = list(s.exec(select(WebSearch)))
    assert len(rows) == 1 and "jane@corp.com" not in rows[0].query and rows[0].n_results == 2


def test_pii_dense_query_is_refused_and_nothing_leaves(enabled, engine, monkeypatch):
    _approve(monkeypatch)
    called = {"n": 0}
    monkeypatch.setattr(search_api, "search", lambda q, **k: called.update(n=called["n"] + 1) or _results())
    monkeypatch.setattr("app.config.settings.redact_max_hits", 1)  # low bar → this counts as dense

    out = web_search.invoke({"query": "Stephanie Cao ssn 123-45-6789 card 4111 1111 1111 1111"})

    assert "too personal" in out.lower()
    assert called["n"] == 0
    with Session(engine) as s:
        assert list(s.exec(select(WebSearch))) == []


def test_unavailable_answers_locally(monkeypatch):
    monkeypatch.setattr("app.config.settings.web_search_enabled", False)
    out = web_search.invoke({"query": "weather in Paris"})
    assert "your own knowledge" in out.lower()


def test_no_results_message(enabled, monkeypatch):
    _approve(monkeypatch)
    monkeypatch.setattr(search_api, "search", lambda q, **k: [])
    out = web_search.invoke({"query": "asdfqwer nonsense"})
    assert "empty" in out.lower()


def test_results_are_framed_as_untrusted(enabled, monkeypatch):
    _approve(monkeypatch)
    monkeypatch.setattr(search_api, "search", lambda q, **k: _results())
    out = web_search.invoke({"query": "weather in Paris"})
    assert "External content from your web search" in out  # frame_untrusted wrapper
    assert "https://ex/0" in out and "snippet 1" in out


def test_rate_limit_blocks_after_cap(enabled, monkeypatch):
    _approve(monkeypatch)
    monkeypatch.setattr(search_api, "search", lambda q, **k: _results())
    monkeypatch.setattr("app.config.settings.max_actions_per_hour", 2)
    web_search.invoke({"query": "q1"})
    web_search.invoke({"query": "q2"})
    out = web_search.invoke({"query": "q3"})
    assert "limit" in out.lower()


# --- provider seam (parsing + switching) ------------------------------------

def test_tavily_parsing_with_injected_client():
    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"results": [
                {"title": "A", "url": "http://a", "content": "body a"},
                {"title": "B", "url": "http://b", "content": "body b"},
            ]}

    class FakeClient:
        def post(self, url, json):
            self.sent = json
            return FakeResp()

    c = FakeClient()
    res = search_api.search("q", provider="tavily", api_key="k", max_results=5, client=c)
    assert [r.title for r in res] == ["A", "B"]
    assert res[0].url == "http://a" and res[0].snippet == "body a"
    assert c.sent["query"] == "q" and c.sent["api_key"] == "k"


def test_duckduckgo_parsing(monkeypatch):
    class FakeDDGS:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def text(self, query, max_results):
            return [{"title": "T", "href": "http://h", "body": "b"}]

    monkeypatch.setattr("ddgs.DDGS", FakeDDGS)
    res = search_api.search("q", provider="duckduckgo", max_results=3)
    assert len(res) == 1 and res[0].url == "http://h" and res[0].snippet == "b"


def test_unknown_provider_raises():
    with pytest.raises(ValueError):
        search_api.search("q", provider="bing")


def test_tavily_requires_key():
    with pytest.raises(ValueError):
        search_api.search("q", provider="tavily", api_key=None)


# --- the approval gate, through a real graph --------------------------------

def _graph():
    def inject(state):
        return {"messages": [AIMessage(
            "", tool_calls=[{"name": "web_search", "id": "w1", "type": "tool_call", "args": {"query": "weather in Paris"}}]
        )]}

    g = StateGraph(MessagesState)
    g.add_node("agent", inject)
    g.add_node("tools", ToolNode([web_search]))
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", tools_condition, {"tools": "tools", END: END})
    g.add_edge("tools", END)
    return g.compile(checkpointer=MemorySaver())


def test_web_search_pauses_for_approval(enabled, engine, monkeypatch):
    monkeypatch.setattr(search_api, "search", lambda q, **k: _results())
    app = _graph()
    cfg = {"configurable": {"thread_id": str(uuid.uuid4())}}
    result = app.invoke({"messages": []}, cfg)

    assert "__interrupt__" in result
    payload = result["__interrupt__"][0].value
    assert payload["action"] == "web_search" and payload["details"]["query"] == "weather in Paris"
    with Session(engine) as s:  # nothing searched/audited before approval
        assert list(s.exec(select(WebSearch))) == []


def test_reject_searches_nothing(enabled, engine, monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(search_api, "search", lambda q, **k: calls.update(n=calls["n"] + 1) or _results())
    app = _graph()
    cfg = {"configurable": {"thread_id": str(uuid.uuid4())}}
    app.invoke({"messages": []}, cfg)
    result = app.invoke(Command(resume={"approved": False}), cfg)

    assert calls["n"] == 0
    assert "cancelled" in result["messages"][-1].content.lower()
    with Session(engine) as s:
        assert list(s.exec(select(WebSearch))) == []


def test_approve_searches_once(enabled, engine, monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(search_api, "search", lambda q, **k: calls.update(n=calls["n"] + 1) or _results())
    app = _graph()
    cfg = {"configurable": {"thread_id": str(uuid.uuid4())}}
    app.invoke({"messages": []}, cfg)
    result = app.invoke(Command(resume={"approved": True}), cfg)

    assert calls["n"] == 1
    assert "External content from your web search" in result["messages"][-1].content
    with Session(engine) as s:
        assert len(list(s.exec(select(WebSearch)))) == 1
