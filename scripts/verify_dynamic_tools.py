"""Live verification that dynamic per-turn tool binding makes the builder work
CONVERSATIONALLY without breaking the core tools — the whole point of the change.

Drives the real graph (build_agent) and checks which tool the model *fires* for each
message (streaming the agent step and inspecting tool_calls, before execution — so nothing
is actually built/served). Also prints the per-turn selected-subset sizes.

Run:
    DATABASE_URL=postgresql://localhost/personal_agent_test PYTHONPATH=. uv run python scripts/verify_dynamic_tools.py
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

from app.agent import tool_select  # noqa: E402
from app.agent.graph import build_agent  # noqa: E402
from app.agent.tools import ALL_TOOLS  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'} | {name}" + (f" | {detail}" if detail else ""))


def main() -> None:
    # 1. Subset sizes stay small (under the ~13-tool wall).
    sizes = {m: len(tool_select.select_tools(m, ALL_TOOLS)) for m in
             ["build me a landing page", "remind me to call mom", "what's on my calendar", "make a pdf plan"]}
    check("per-turn subsets stay small (<=10)", all(s <= 10 for s in sizes.values()), str(sizes))

    agent = build_agent()

    def fires(prompt: str, tool: str) -> bool:
        cfg = {"configurable": {"thread_id": f"verify-dyn-{uuid.uuid4()}"}}
        for update in agent.stream({"messages": [HumanMessage(prompt)]}, cfg, stream_mode="updates"):
            ap = update.get("agent")
            if ap and ap.get("messages"):
                names = [tc["name"] for tc in (getattr(ap["messages"][-1], "tool_calls", None) or [])]
                if tool in names:
                    return True
                break  # stop before the tool executes (don't actually build/serve)
        return False

    def rate(prompts, tool):
        return sum(fires(p, tool) for p in prompts)

    reminders = ["remind me to call mom every Sunday", "remind me to submit the form tomorrow at 3pm", "ping me in 2 hours to stretch"]
    builds = ["build me a landing page for my bakery", "make a simple website about my cat", "build a portfolio page for me"]
    docs = ["make me a pdf plan for my week", "write up a one-page summary as a pdf"]

    r = rate(reminders, "add_reminder")
    check("add_reminder still fires (core not broken)", r / len(reminders) >= 0.6, f"{r}/{len(reminders)}")
    b = rate(builds, "build_web_app")
    check("build_web_app fires CONVERSATIONALLY", b / len(builds) >= 0.6, f"{b}/{len(builds)}")
    d = rate(docs, "make_document")
    check("make_document fires conversationally", d / len(docs) >= 0.5, f"{d}/{len(docs)}")

    print()
    failed = [x for x in results if not x[1]]
    print(f"{len(results) - len(failed)}/{len(results)} checks passed.")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
