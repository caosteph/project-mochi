"""Live Phase 8 verification — web search against the REAL local 7B, plus (if configured)
a real provider round-trip. The offline suite (tests/test_web_search.py) proves the
scrub/approval/audit logic with mocks; this proves the two things that can't be faked: the
7B decides to call web_search for outside-world questions, and a real provider returns
parseable results.

Run (Ollama serving the local model):
    DATABASE_URL=postgresql://localhost/personal_agent_test PYTHONPATH=. uv run python scripts/verify_web_search.py
"""

import os
import sys
import uuid

if "personal_agent_test" not in os.environ.get("DATABASE_URL", "") and "verify" not in os.environ.get("DATABASE_URL", ""):
    print(f"Refusing to run: DATABASE_URL must be a scratch DB (got {os.environ.get('DATABASE_URL')!r}).")
    sys.exit(1)
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "verify_placeholder")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")

from langchain_core.messages import HumanMessage  # noqa: E402

from app.agent.graph import build_agent  # noqa: E402
from app.agent.tools.web_tools import web_search_available  # noqa: E402
from app.config import settings  # noqa: E402
from app.integrations import web_search as search_api  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'} | {name}" + (f" | {detail}" if detail else ""))


def fires(agent, prompt: str) -> bool:
    """True if the model fires web_search for `prompt`. Breaks BEFORE execution (so no
    approval/network), like the other verify scripts."""
    cfg = {"configurable": {"thread_id": f"ws-{uuid.uuid4()}"}}
    for u in agent.stream({"messages": [HumanMessage(prompt)]}, cfg, stream_mode="updates"):
        ap = u.get("agent")
        if ap and ap.get("messages"):
            return "web_search" in [tc["name"] for tc in (getattr(ap["messages"][-1], "tool_calls", None) or [])]
    return False


def main() -> None:
    # 1. Real-model firing: outside-world questions should route to web_search.
    agent = build_agent()
    prompts = [
        "what's the weather in Paris right now?",
        "is Trader Joe's open on Sundays?",
        "what's the current price of bitcoin?",
    ]
    hits = sum(fires(agent, p) for p in prompts)
    check("model fires web_search (rate)", hits >= 2, f"{hits}/{len(prompts)} — floor 2/3 (soft-tier reliability on a 7B)")

    # 2. Live provider round-trip (best-effort; only if configured). Proves the real API
    #    parses into SearchResults. Skipped when web search isn't set up.
    if web_search_available():
        try:
            res = search_api.search("weather in Tokyo today")
            ok = len(res) >= 1 and bool(res[0].url)
            check(f"live {settings.web_search_provider} round-trip", ok,
                  f"{len(res)} results; first url={res[0].url[:60] if res else None}")
        except Exception as exc:
            check(f"live {settings.web_search_provider} round-trip", False, str(exc)[:100])
    else:
        print(f"SKIP | live provider round-trip — web search not configured "
              f"(provider={settings.web_search_provider!r}, key set={bool(settings.web_search_api_key)})")

    print()
    failed = [r for r in results if not r[1]]
    print(f"{len(results) - len(failed)}/{len(results)} checks passed.")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
