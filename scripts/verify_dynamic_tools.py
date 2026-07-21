"""Live verification that dynamic per-turn tool binding makes the builder work
CONVERSATIONALLY without breaking the core tools — the whole point of the change.

Drives the real graph (build_agent) and checks which tool the model *fires* for each
message (streaming the agent step and inspecting tool_calls, before execution — so nothing
is actually built/served). Also prints the per-turn selected-subset sizes.

Run:
    DATABASE_URL=postgresql://localhost/personal_agent_test PYTHONPATH=. uv run python scripts/verify_dynamic_tools.py
"""

from scripts._verify_lib import bootstrap_env, check, rate, require_scratch_db, summarize_and_exit

require_scratch_db()
bootstrap_env()

from app.agent import tool_select  # noqa: E402
from app.agent.graph import build_agent  # noqa: E402
from app.agent.tools import ALL_TOOLS  # noqa: E402


def main() -> None:
    # 1. Subset sizes stay small (under the ~13-tool wall).
    sizes = {m: len(tool_select.select_tools(m, ALL_TOOLS)) for m in
             ["build me a landing page", "remind me to call mom", "what's on my calendar", "make a pdf plan"]}
    check("per-turn subsets stay small (<=10)", all(s <= 10 for s in sizes.values()), str(sizes))

    agent = build_agent()

    reminders = ["remind me to call mom every Sunday", "remind me to submit the form tomorrow at 3pm", "ping me in 2 hours to stretch"]
    builds = ["build me a landing page for my bakery", "make a simple website about my cat", "build a portfolio page for me"]
    docs = ["make me a pdf plan for my week", "write up a one-page summary as a pdf"]

    r = rate(agent, reminders, "add_reminder")
    check("add_reminder still fires (core not broken)", r / len(reminders) >= 0.6, f"{r}/{len(reminders)}")
    b = rate(agent, builds, "build_web_app")
    check("build_web_app fires CONVERSATIONALLY", b / len(builds) >= 0.6, f"{b}/{len(builds)}")
    d = rate(agent, docs, "make_document")
    check("make_document fires conversationally", d / len(docs) >= 0.5, f"{d}/{len(docs)}")

    summarize_and_exit()


if __name__ == "__main__":
    main()
