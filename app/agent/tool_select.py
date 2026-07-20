"""Dynamic per-turn tool selection ("tool RAG"). The local 7B collapses when bound with
too many tools (measured: 11 fire reliably, 13–15 → ~0), so instead of binding all tools
every turn we bind a small, relevant subset chosen from the user's message: an always-on
memory core + keyword-matched tools + the embedding-nearest tools, capped small.

Validated on qwen2.5:7b: a ~6-tool subset fires the target tool reliably, and embedding
routing puts the right tool in the top few for non-memory intents (memory is the always-on
core). See docs/10-phase4b-build.md.
"""

import logging
import math

from app.memory.embeddings import embed_local

log = logging.getLogger(__name__)

# Always bound — broadly needed and hard to route by embedding (generic descriptions).
CORE = ("recall", "remember_fact")

# High-signal keyword → tool boosts. Union'd in (false positives only add a capped tool;
# they never remove the right one), so we can be generous.
KEYWORDS: dict[str, tuple[str, ...]] = {
    "build_web_app": ("build", "website", "web page", "webpage", "web app", "landing page", "web site", "html page"),
    "make_document": ("pdf", "document", " doc", "word doc", "write-up", "write up", "one-pager", "one page", "report"),
    "add_reminder": ("remind", "reminder", "ping me", "nudge me", "don't let me forget"),
    "list_reminders": ("my reminders", "upcoming reminders", "what reminders"),
    "cancel_reminder": ("cancel", "delete the reminder", "remove the reminder"),
    "calendar_list_events": ("calendar", "schedule", "am i free", "are we free", "meeting", "appointment", "what's on my"),
    "gmail_list_recent": ("inbox", "recent email", "any email", "check my email", "unread", "new emails"),
    "read_email": ("what did", "what does the email", "what's in the email", "read the email", "read me the email", "read my email", "the email from", "summarize the email", "the email say", "what did it say", "gist of the email", "what does it say"),
    "create_draft": ("draft", "email to", "reply to", "compose", "send an email", "write an email"),
    "add_goal": ("goal", "i want to"),
    "add_task": ("add a task", "to-do", "todo", "to do list"),
    "consult_expert": ("explain", "how do i", "how does", "help me understand", "write code", "debug", "algorithm"),
    "web_search": ("search", "look up", "google", "weather", "how much is", "price of", "who won", "who is", "what is a", "latest", "right now", "near me", "open on", "hours", "what time does", "current"),
    "serve_project": ("make it public", "make it live", "serve", "share the site"),
    "list_projects": ("my projects", "what have you built", "list projects"),
}

_tool_vecs: dict[str, list[float]] = {}


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb + 1e-9)


def _tool_vec(tool) -> list[float]:
    if tool.name not in _tool_vecs:
        first_line = (tool.description or "").splitlines()[0] if tool.description else ""
        _tool_vecs[tool.name] = embed_local(f"{tool.name}: {first_line}")
    return _tool_vecs[tool.name]


def select_tools(message: str, all_tools: list, *, k: int = 6, cap: int = 10) -> list:
    """Pick a small relevant tool subset for this message: CORE + embedding-nearest top-k +
    keyword matches, deduped (CORE first, then by relevance) and capped. Fails safe: if
    embedding is unavailable, returns CORE + keyword matches (never the whole set)."""
    by_name = {t.name: t for t in all_tools}
    ordered: list[str] = [n for n in CORE if n in by_name]

    # Embedding-nearest (the precise relevance signal) — prioritized after CORE.
    try:
        if message.strip():
            mv = embed_local(message)
            ranked = sorted(((_cos(mv, _tool_vec(t)), t.name) for t in all_tools), reverse=True)
            ordered += [name for _, name in ranked[:k]]
    except Exception:  # Ollama hiccup → fall back to CORE + keywords, never all tools
        log.warning("tool-select embedding failed; using keyword routing only", exc_info=True)

    # Keyword boosts fill any remaining slots.
    low = message.lower()
    ordered += [name for name, kws in KEYWORDS.items() if name in by_name and any(kw in low for kw in kws)]

    seen: set[str] = set()
    chosen: list = []
    for name in ordered:
        if name in by_name and name not in seen:
            seen.add(name)
            chosen.append(by_name[name])
        if len(chosen) >= cap:
            break
    return chosen
