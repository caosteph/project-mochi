"""A cross-turn cap on side-effectful agent actions — a runaway / prompt-injection
loop guard. In-memory rolling one-hour window per action type; resets on restart
(fine for this purpose). Note: LangGraph's per-turn `recursion_limit` already bounds
tool calls *within* a turn; this adds a cross-turn ceiling.
"""

import time
from collections import defaultdict, deque

from app.config import settings

_events: dict[str, deque] = defaultdict(deque)
_WINDOW_SECONDS = 3600


def allow(action: str) -> bool:
    """Record an attempt of `action`; return False if it would exceed
    settings.max_actions_per_hour within the last hour."""
    now = time.time()
    window = _events[action]
    cutoff = now - _WINDOW_SECONDS
    while window and window[0] < cutoff:
        window.popleft()
    if len(window) >= settings.max_actions_per_hour:
        return False
    window.append(now)
    return True


def reset() -> None:
    """Clear all counters — used by tests."""
    _events.clear()
