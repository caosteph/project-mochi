"""The Telegram presentation layer (app/channels/render.py) — pure functions, so unlike the
rest of the channel these are directly testable: no bot, no network, no event loop.

Covers the status breadcrumbs, the per-action approval proposal, and the "never fail to
deliver" rules (MarkdownV2 conversion + plain-text chunking).
"""

from app.agent.tools import ALL_TOOLS
from app.channels.render import (
    balance_markdown,
    CHUNK_SIZE,
    TG_LIMIT,
    chunk,
    render_proposal,
    status_for_tool,
    to_markdown_v2,
)


# --- status breadcrumbs -----------------------------------------------------

def test_every_tool_has_a_specific_status():
    for tool in ALL_TOOLS:
        status = status_for_tool(tool.name)
        assert status and status != "⏳ Working on it…", (
            f"tool {tool.name!r} has no specific status line — add one to render.TOOL_STATUS"
        )


def test_unknown_tool_falls_back():
    assert status_for_tool("some_future_tool") == "⏳ Working on it…"


# --- approval proposals (per action) ---------------------------------------

def test_render_proposal_web_search_shows_scrubbed_query():
    out = render_proposal("web_search", {"query": "weather in Paris"})
    assert "weather in Paris" in out and "search" in out.lower()
    assert "To:" not in out and "Subject:" not in out  # no draft fields leak in


def test_render_proposal_draft_shows_recipient_and_body():
    out = render_proposal("create_draft", {"to": "sam@example.com", "subject": "Hi", "body": "Hello Sam"})
    assert "sam@example.com" in out and "Hi" in out and "Hello Sam" in out


def test_render_proposal_unknown_action_defaults_to_draft_shape():
    # Fail safe: an unrecognized action still renders something approvable, never a blank.
    out = render_proposal("some_future_action", {"to": "x", "subject": "y", "body": "z"})
    assert "approve" in out.lower()


# --- MarkdownV2 conversion (None == "use plain text") ----------------------

def test_markdown_converts_simple_text():
    out = to_markdown_v2("hello **world**")
    assert out and "world" in out


def test_markdown_returns_none_when_over_telegram_limit():
    # Too long to send as one formatted message → caller must fall back + chunk.
    assert to_markdown_v2("x" * (TG_LIMIT + 100)) is None


def test_markdown_never_raises_on_bad_input(monkeypatch):
    import app.channels.render as render

    monkeypatch.setattr(render.telegramify_markdown, "markdownify",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))
    assert to_markdown_v2("anything") is None  # swallowed → plain-text path


# --- chunking (a long reply is never dropped) ------------------------------

def test_chunk_splits_on_the_boundary():
    pieces = chunk("y" * (CHUNK_SIZE * 2 + 5))
    assert len(pieces) == 3
    assert [len(p) for p in pieces] == [CHUNK_SIZE, CHUNK_SIZE, 5]
    assert "".join(pieces) == "y" * (CHUNK_SIZE * 2 + 5)  # lossless


def test_chunk_short_text_is_one_piece_and_empty_is_none():
    assert chunk("short") == ["short"]
    assert chunk("") == []


# --- mid-stream formatting --------------------------------------------------
# Streaming used to be plain text with formatting only applied on the final edit, so the
# reply visibly "popped" into shape when it finished. balance_markdown closes the markers a
# half-streamed buffer leaves dangling so intermediate frames are valid MarkdownV2.

def test_balance_closes_unclosed_bold_and_italic():
    assert balance_markdown("**bol") == "**bol**"
    assert balance_markdown("_ital") == "_ital_"
    assert balance_markdown("here is `cod") == "here is `cod`"


def test_balance_closes_an_open_code_fence():
    out = balance_markdown("```python\nprint(1)")
    assert out.count("```") == 2 and out.rstrip().endswith("```")


def test_balance_leaves_already_valid_text_alone():
    for good in ("plain sentence", "**bold**", "a `snippet` inline", "```py\nx=1\n```"):
        assert balance_markdown(good) == good


def test_balance_handles_empty_and_survives_conversion():
    assert balance_markdown("") == ""
    # the whole point: a partial buffer should now convert instead of being rejected
    assert to_markdown_v2(balance_markdown("**partially wri")) is not None
