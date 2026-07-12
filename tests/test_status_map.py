"""The status breadcrumb map covers every registered tool and falls back safely."""

from app.agent.tools import ALL_TOOLS
from app.channels.telegram import status_for_tool


def test_every_tool_has_a_specific_status():
    for tool in ALL_TOOLS:
        status = status_for_tool(tool.name)
        assert status and status != "⏳ Working on it…", (
            f"tool {tool.name!r} has no specific status line — add one to _TOOL_STATUS"
        )


def test_unknown_tool_falls_back():
    assert status_for_tool("some_future_tool") == "⏳ Working on it…"
