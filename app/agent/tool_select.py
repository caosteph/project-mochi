"""Dynamic per-turn tool selection ("tool RAG"): bind a small, relevant subset per turn — an
always-on memory core + keyword-matched tools + the embedding-nearest tools, capped small.

HISTORY / CORRECTION (2026-07-20). This was built for a measured "tool-count wall" (11 tools
fire, 13–15 → ~0). That wall turned out to be **context exhaustion, not a model limit**: each
bound tool costs ~95 prompt tokens, and the model was running at Ollama's default num_ctx 4096
where the base prompt was already ~3,600 — so ~11 tools is exactly where the prompt crossed the
window. With the 8k-context model, **all 17 tools bind and fire 3/3** (prompt ~4,998 tokens).

So this module is no longer load-bearing for correctness — but it's kept because it's still
better: it saves ~665 prompt tokens/turn vs binding everything (faster prefill, more generation
headroom) and routing is accurate (measured: the right tool was in the subset 15/15, and the
selected subsets run 7–9 tools, below the cap). Adding new tools is now safe — budget ~95
tokens each against the context. See docs/14-future-work.md.
"""

import logging
import math
import time

from app.config import settings
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


def warm_tool_vectors(all_tools: list) -> None:
    """Pre-compute the per-tool embedding cache at startup. Measured: the FIRST select_tools
    call costs ~1.1s (it embeds every tool description) vs ~55ms once warm — so without this,
    Stephanie's first message after every restart pays a full extra second. Pure cache warming:
    identical values, computed earlier. Best-effort; never raises."""
    for tool in all_tools:
        try:
            _tool_vec(tool)
        except Exception:
            log.warning("tool-vector warm failed; first turn will pay the cold cost", exc_info=True)
            return


def select_tools(message: str, all_tools: list, *, k: int = 6, cap: int = 10) -> list:
    """Pick a small relevant tool subset for this message: CORE + embedding-nearest top-k +
    keyword matches, deduped (CORE first, then by relevance) and capped. Fails safe: if
    embedding is unavailable, returns CORE + keyword matches (never the whole set)."""
    by_name = {t.name: t for t in all_tools}
    ordered: list[str] = [n for n in CORE if n in by_name]

    # Embedding-nearest (the precise relevance signal) — prioritized after CORE.
    t0 = time.perf_counter()
    try:
        if message.strip():
            mv = embed_local(message)
            ranked = sorted(((_cos(mv, _tool_vec(t)), t.name) for t in all_tools), reverse=True)
            ordered += [name for _, name in ranked[:k]]
    except Exception:  # Ollama hiccup → fall back to CORE + keywords, never all tools
        log.warning("tool-select embedding failed; using keyword routing only", exc_info=True)
    if settings.latency_log:
        log.info("latency: tool_select embedding %.0fms", (time.perf_counter() - t0) * 1000)

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
