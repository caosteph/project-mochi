"""Live Phase 8 verification — web search against the REAL local 7B, plus (if configured)
a real provider round-trip. The offline suite (tests/test_web_search.py) proves the
scrub/approval/audit logic with mocks; this proves the two things that can't be faked: the
7B decides to call web_search for outside-world questions, and a real provider returns
parseable results.

Run (Ollama serving the local model):
    DATABASE_URL=postgresql://localhost/personal_agent_test PYTHONPATH=. uv run python scripts/verify_web_search.py
"""

from scripts._verify_lib import (
    bootstrap_env,
    check,
    rate,
    require_scratch_db,
    skip,
    summarize_and_exit,
)

require_scratch_db()
bootstrap_env()

from app.agent.graph import build_agent  # noqa: E402
from app.agent.tools.web_tools import web_search_available  # noqa: E402
from app.config import settings  # noqa: E402
from app.integrations import web_search as search_api  # noqa: E402


def main() -> None:
    # 1. Real-model firing: outside-world questions should route to web_search.
    agent = build_agent()
    prompts = [
        "what's the weather in Paris right now?",
        "is Trader Joe's open on Sundays?",
        "what's the current price of bitcoin?",
    ]
    hits = rate(agent, prompts, "web_search")
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
        skip("live provider round-trip", f"web search not configured (provider="
             f"{settings.web_search_provider!r}, key set={bool(settings.web_search_api_key)})")

    summarize_and_exit()


if __name__ == "__main__":
    main()
