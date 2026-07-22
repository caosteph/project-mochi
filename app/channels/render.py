"""Presentation for the Telegram channel — the *stateless* half.

Split out of `telegram.py` (which had grown to ~700 LOC of transport + commands + buttons +
rendering) so the formatting rules are pure functions: no bot, no network, no `self`. That makes
them directly unit-testable, which the rest of the channel isn't.

Nothing here performs I/O. `telegram.py` decides what to *send*; this decides how it *looks*.
"""

import logging

import telegramify_markdown

log = logging.getLogger(__name__)

TG_LIMIT = 4096  # Telegram's max message length
CHUNK_SIZE = 4000  # plain-text fallback chunk (headroom under the limit)

# Friendly, present-tense status shown when Mochi starts using a tool. Kept short —
# these are breadcrumbs, not sentences.
TOOL_STATUS = {
    "calendar_list_events": "📅 Checking your calendar…",
    "gmail_list_recent": "📬 Looking through your inbox…",
    "read_email": "📖 Reading that email…",
    "create_draft": "✉️ Drafting that email…",
    "recall": "🧠 Checking what I remember…",
    "remember_fact": "🧠 Noting that down…",
    "add_goal": "🎯 Adding that goal…",
    "add_task": "✅ Adding that task…",
    "add_reminder": "⏰ Setting that reminder…",
    "list_reminders": "📋 Checking your reminders…",
    "cancel_reminder": "🗑️ Cancelling that reminder…",
    "retire_task": "✔️ Marking that done for good…",
    "consult_expert": "🧭 Consulting a bigger model…",
    "web_search": "🔎 Searching the web…",
    "build_web_app": "🛠️ Building that…",
    "make_document": "📄 Putting that document together…",
    "serve_project": "🌐 Serving that up…",
    "list_projects": "📁 Checking what I've built…",
    "ask_user": "🤔 Just need a quick answer…",
}

_FALLBACK_STATUS = "⏳ Working on it…"


def status_for_tool(name: str) -> str:
    return TOOL_STATUS.get(name, _FALLBACK_STATUS)


def render_proposal(action: str, details: dict) -> str:
    """The human-readable proposal shown with Approve/Reject, per action type. Each
    side-effectful action that routes through the confirm gate gets a rendering here."""
    if action == "web_search":
        return (
            "🔎 Search the web for (this scrubbed query will leave your machine):\n\n"
            f"{details.get('query')}"
        )
    # Default / create_draft: a draft to approve (never auto-sent).
    return (
        "📝 Draft to approve (it will not be sent):\n\n"
        f"To: {details.get('to')}\n"
        f"Subject: {details.get('subject')}\n\n"
        f"{details.get('body')}"
    )


def render_choice(question: str) -> str:
    """The prompt shown above a row of choice buttons. The options are the button labels
    themselves (rendered by the channel), so this is just the question — kept plain so a long or
    markdown-ish question can't break the send."""
    return (question or "Which one?").strip()


def render_resolved_choice(question: str, chosen: str) -> str:
    """What the question message is rewritten to after she taps, so the chat keeps a clean record
    instead of a dangling question with dead buttons: 'Which one? → ✅ dentist appointment'."""
    q = (question or "").strip()
    return f"{q} → ✅ {chosen}" if q else f"✅ {chosen}"


def balance_markdown(text: str) -> str:
    """Close markers left dangling mid-sentence so a *partial* stream still converts.

    While a reply streams in, the buffer is routinely mid-token — `**bol`, an opened code fence,
    a half-written italic. Telegram rejects that outright, which is why streaming used to be sent
    as plain text and only formatted once at the end (formatting would "pop in" at the finish).
    Closing the open markers on a copy makes almost every intermediate frame valid; anything this
    doesn't catch still falls back to plain via `to_markdown_v2` returning None.
    """
    if not text:
        return text
    out = text
    if out.count("```") % 2 == 1:  # fences first — they contain backticks the pass below counts
        out += "\n```"
    for marker in ("**", "__", "`", "*", "_"):
        if out.count(marker) % 2 == 1:
            out += marker
    return out


def to_markdown_v2(text: str) -> str | None:
    """Convert model text to Telegram MarkdownV2, or None if it can't be used.

    Returns None when the converter fails OR the result exceeds Telegram's limit — in both
    cases the caller should fall back to plain text. Returning None (rather than raising or
    returning the original) keeps the "never fail to deliver" rule explicit at the call site.
    """
    try:
        formatted = telegramify_markdown.markdownify(text, latex_escape=False)
    except Exception:  # converter hiccup → treat as unformatted
        log.warning("MarkdownV2 conversion failed; falling back to plain text", exc_info=True)
        return None
    return formatted if formatted and len(formatted) <= TG_LIMIT else None


def chunk(text: str, size: int = CHUNK_SIZE) -> list[str]:
    """Split text into Telegram-sized pieces so a long reply is never dropped."""
    if not text:
        return []
    return [text[i : i + size] for i in range(0, len(text), size)]
