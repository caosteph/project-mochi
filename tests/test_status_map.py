"""The status breadcrumb map covers every registered tool and falls back safely; and the
per-action approval renderer shows the right proposal for each gated action."""

from app.agent.tools import ALL_TOOLS
from app.channels.telegram import _render_proposal, status_for_tool


def test_every_tool_has_a_specific_status():
    for tool in ALL_TOOLS:
        status = status_for_tool(tool.name)
        assert status and status != "⏳ Working on it…", (
            f"tool {tool.name!r} has no specific status line — add one to _TOOL_STATUS"
        )


def test_unknown_tool_falls_back():
    assert status_for_tool("some_future_tool") == "⏳ Working on it…"


def test_render_proposal_web_search_shows_scrubbed_query():
    out = _render_proposal("web_search", {"query": "weather in Paris"})
    assert "weather in Paris" in out and "search" in out.lower()
    # No draft fields leak into a web-search proposal.
    assert "To:" not in out and "Subject:" not in out


def test_render_proposal_draft_shows_recipient_and_body():
    out = _render_proposal("create_draft", {"to": "sam@example.com", "subject": "Hi", "body": "Hello Sam"})
    assert "sam@example.com" in out and "Hi" in out and "Hello Sam" in out


def test_render_proposal_unknown_action_defaults_to_draft_shape():
    # Fail safe: an unrecognized action renders the draft shape (never a bare/blank proposal).
    out = _render_proposal("some_future_action", {"to": "x", "subject": "y", "body": "z"})
    assert "approve" in out.lower()
