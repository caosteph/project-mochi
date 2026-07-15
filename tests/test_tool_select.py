"""Dynamic tool selection — deterministic logic (CORE + keyword + cap + fail-safe). The
embedding path (which needs Ollama) is exercised in scripts/verify_dynamic_tools.py; here
we stub the embedder so the tests are hermetic.
"""

from app.agent import tool_select
from app.agent.tools import ALL_TOOLS


def _fail_embed(monkeypatch):
    def _raise(_):
        raise RuntimeError("no ollama")

    monkeypatch.setattr(tool_select, "embed_local", _raise)


def _names(msg):
    return [t.name for t in tool_select.select_tools(msg, ALL_TOOLS)]


def test_core_always_present(monkeypatch):
    _fail_embed(monkeypatch)
    names = _names("hey how are you")
    assert "recall" in names and "remember_fact" in names


def test_keyword_routing_includes_the_right_tool(monkeypatch):
    _fail_embed(monkeypatch)
    assert "build_web_app" in _names("build me a website for my shop")
    assert "add_reminder" in _names("remind me to call mom on sunday")
    assert "make_document" in _names("make me a pdf of my plan")
    assert "create_draft" in _names("draft an email to bob")
    assert "calendar_list_events" in _names("what's on my calendar tomorrow")
    assert "gmail_list_recent" in _names("any recent email from my landlord")


def test_cap_is_respected(monkeypatch):
    _fail_embed(monkeypatch)
    msg = "build a website, remind me, make a pdf, draft an email, check my calendar and my inbox"
    assert len(_names(msg)) <= 10


def test_failsafe_returns_core_plus_keywords(monkeypatch):
    _fail_embed(monkeypatch)  # embedding down
    names = _names("build me a website")
    assert "recall" in names and "build_web_app" in names  # no crash, still routed


def test_embedding_path_runs_and_caps(monkeypatch):
    # embedder available (constant vector) → path executes, subset still capped + has core
    monkeypatch.setattr(tool_select, "embed_local", lambda _: [1.0, 0.0, 0.0])
    tool_select._tool_vecs.clear()
    names = [t.name for t in tool_select.select_tools("anything at all", ALL_TOOLS)]
    assert "recall" in names and len(names) <= 10
    tool_select._tool_vecs.clear()
