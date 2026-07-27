"""Live gate for the builder — the 2026-07-25 failures made concrete:
  - a first build is not bare (uses Tailwind + Alpine), and
  - "make it richer / use tailwind" actually ITERATES (edit_web_app fires and the file changes),
    instead of narrating "I'm enhancing…" without rebuilding.

Drives the real agent on one thread. The quality check needs hosted codegen (gpt-oss-120b); it's
skipped (not failed) when hosted is unavailable, since the local 7B is not expected to match it.

Run:
    DATABASE_URL=postgresql://localhost/personal_agent_test PYTHONPATH=. uv run python scripts/verify_builder.py
"""

import uuid

from scripts._verify_lib import (
    bootstrap_env,
    check,
    require_scratch_db,
    skip,
    summarize_and_exit,
)

require_scratch_db()
bootstrap_env()

from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402

from app.agent import rate_limit, router  # noqa: E402
from app.agent.graph import build_agent  # noqa: E402
from app.builder import workspace  # noqa: E402


class Convo:
    def __init__(self, agent):
        self.agent = agent
        self.cfg = {"configurable": {"thread_id": f"bld-{uuid.uuid4()}"}}

    def turn(self, text: str) -> list[str]:
        """Send a message; return the tools that fired this turn (repaired text-calls count, since the
        repair node attaches real tool_calls to the message)."""
        state = self.agent.invoke({"messages": [HumanMessage(text)]}, self.cfg)
        msgs = state.get("messages", [])
        idx = max((i for i, m in enumerate(msgs) if isinstance(m, HumanMessage)), default=-1)
        return [
            tc["name"]
            for m in msgs[idx + 1:]
            if isinstance(m, AIMessage)
            for tc in (getattr(m, "tool_calls", None) or [])
        ]


def _snapshot() -> dict[str, str]:
    """Every project's index.html content, keyed by name. Robust to the model firing build more than
    once in a turn (which creates >1 project) — we diff by content, not by 'latest'."""
    snap = {}
    for name in workspace.list_projects():
        p = workspace.WORKSPACE_ROOT / name / "index.html"
        if p.exists():
            snap[name] = p.read_text(encoding="utf-8")
    return snap


def main() -> None:
    rate_limit.reset()  # don't let an earlier verify's builds exhaust the hourly budget
    agent = build_agent()
    c = Convo(agent)

    before = set(_snapshot())
    fired1 = c.turn("build me a habit tracker web app")
    check("build ask fires build_web_app", "build_web_app" in fired1, f"fired={fired1}")
    mid = _snapshot()
    new = {k: v for k, v in mid.items() if k not in before}  # what this run built
    check("build produced a new site", bool(new), f"new projects={list(new)}")

    sample = next(iter(new.values()), "")
    if router.hosted_available() and sample:
        low = sample.lower()
        ok = "cdn.tailwindcss.com" in low and "alpine" in low and len(sample) > 1500
        check("built app uses Tailwind + Alpine, not a bare page", ok,
              f"tailwind={'cdn.tailwindcss.com' in low} alpine={'alpine' in low} len={len(sample)}")
    else:
        skip("built app uses Tailwind + Alpine, not a bare page", "hosted codegen unavailable")

    fired2 = c.turn("make it richer and use tailwind")
    check("iteration fires edit_web_app instead of narrating", "edit_web_app" in fired2, f"fired={fired2}")
    after = _snapshot()
    changed = any(after.get(k) != v for k, v in new.items())
    check("the edit actually changed a built file", changed, "changed in place" if changed else "unchanged")

    summarize_and_exit()


if __name__ == "__main__":
    main()
