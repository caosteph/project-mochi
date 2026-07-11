"""Standalone Phase 1 verification — drives the real agent graph directly
(no Telegram) against a scratch database, so correctness can be checked
without Stephanie's live participation. Telegram itself (the whitelist, bot
token wiring) was already verified in Phase 0 and doesn't need re-checking
here — this script is about the memory system, not the transport.

IMPORTANT: DATABASE_URL must point at a scratch DB and must be set BEFORE
`app.agent.graph` (or anything under `app.memory`) is imported, since the
engine and the Postgres checkpointer connection are both resolved from
`settings.database_url` at import/build time. Run via:

    DATABASE_URL=postgresql://localhost/personal_agent_test \
        uv run python scripts/verify_phase1.py

Exits non-zero if any check fails, so it can gate "this works" claims rather
than just being read as reassuring output.
"""

import os
import sys
import uuid

if "personal_agent_test" not in os.environ.get("DATABASE_URL", "") and "verify" not in os.environ.get(
    "DATABASE_URL", ""
):
    print(
        "Refusing to run: DATABASE_URL must point at a scratch DB "
        "(expected 'personal_agent_test' or 'verify' in the name), got: "
        f"{os.environ.get('DATABASE_URL')!r}. This script writes real rows."
    )
    sys.exit(1)

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "verify_placeholder")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")

from langchain_core.messages import HumanMessage  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app.agent.graph import build_agent  # noqa: E402
from app.memory.db import get_engine  # noqa: E402
from app.memory.models import Goal, Task  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    results.append((name, passed, detail))
    print(f"{'PASS' if passed else 'FAIL'} | {name}" + (f" | {detail}" if detail else ""))


def fresh_thread() -> dict:
    return {"configurable": {"thread_id": f"verify-{uuid.uuid4()}"}}


def tool_calls_in(result) -> list[str]:
    calls: list[str] = []
    for m in result["messages"]:
        calls += [tc["name"] for tc in (getattr(m, "tool_calls", None) or [])]
    return calls


def main() -> None:
    agent = build_agent()

    # --- 1. Tool-invocation reliability: rate across several natural phrasings.
    # Not a single boolean — tool-calling on a 7B local model is probabilistic.
    # See docs/05-phase1-build.md's "tool-invocation reliability" gotcha: an
    # earlier version of this persona measured 0-40% here before a prompt fix.
    phrasings = [
        "my dog's name is Biscuit",
        "quick note: I'm allergic to shellfish",
        "just so you know, I'm training for a half marathon in October",
        "FYI my favorite coffee order is an oat milk cortado",
        "my best friend is named Maya",
    ]
    hits = 0
    for text in phrasings:
        result = agent.invoke({"messages": [HumanMessage(text)]}, fresh_thread())
        if "remember_fact" in tool_calls_in(result):
            hits += 1
    rate = hits / len(phrasings)
    check(
        "tool-invocation reliability (remember_fact fires on stated facts)",
        rate >= 0.6,
        f"{hits}/{len(phrasings)} ({rate:.0%}) — informational floor is 60%, not 100%; a 7B local "
        "model won't be perfectly reliable, this just catches regressions to ~0%",
    )

    # --- 2. Recall from a brand-new thread — proves Postgres retrieval, not
    # checkpointer replay (a fresh thread_id has zero prior message history).
    result = agent.invoke(
        {"messages": [HumanMessage("what is my dog's name?")]}, fresh_thread()
    )
    reply = result["messages"][-1].content
    check("recall from fresh thread finds 'Biscuit'", "Biscuit" in reply, reply[:120])

    # --- 3. add_goal / add_task actually write rows.
    with Session(get_engine()) as session:
        goals_before = len(session.exec(select(Goal)).all())
        tasks_before = len(session.exec(select(Task)).all())
    agent.invoke(
        {"messages": [HumanMessage("add a goal to run a 10k, and a task to buy running shoes")]},
        fresh_thread(),
    )
    with Session(get_engine()) as session:
        goals_after = len(session.exec(select(Goal)).all())
        tasks_after = len(session.exec(select(Task)).all())
    check("add_goal wrote a row", goals_after > goals_before, f"{goals_before} -> {goals_after}")
    check("add_task wrote a row", tasks_after > tasks_before, f"{tasks_before} -> {tasks_after}")

    # --- 4. Context-window management: exceed the buffer, confirm trimming
    # + a populated summary, via a lowered threshold so this doesn't need a
    # genuinely long conversation to trigger.
    from app.config import settings

    original_max_tokens = settings.working_buffer_max_tokens
    settings.working_buffer_max_tokens = 50  # force the trigger quickly
    try:
        cfg = fresh_thread()
        long_text = "Here is a fairly long message to help exceed the token budget quickly. " * 5
        for i in range(4):
            agent.invoke({"messages": [HumanMessage(f"{long_text} (turn {i})")]}, cfg)
        state = agent.get_state(cfg)
        summary = state.values.get("summary")
        msg_count = len(state.values["messages"])
        check(
            "context-window management populated a summary",
            bool(summary),
            f"summary={'<empty>' if not summary else summary[:80]!r}",
        )
        check(
            "context-window management trimmed old messages",
            msg_count <= settings.working_buffer_keep_recent + 2,  # some slack for the last turn
            f"{msg_count} messages remain (keep_recent={settings.working_buffer_keep_recent})",
        )
    finally:
        settings.working_buffer_max_tokens = original_max_tokens

    # --- 5. Restart-durability equivalent: an independent second build_agent()
    # instance (no shared in-memory state) recalls what the first one stored.
    agent2 = build_agent()
    result = agent2.invoke(
        {"messages": [HumanMessage("what is my dog's name?")]}, fresh_thread()
    )
    reply2 = result["messages"][-1].content
    check(
        "second independent build_agent() instance recalls the same fact",
        "Biscuit" in reply2,
        reply2[:120],
    )

    # --- 6. No-network guard sanity: embed_local only ever hits localhost.
    from app.config import settings as s

    check(
        "embedding endpoint is localhost-only by construction",
        "localhost" in s.ollama_base_url or "127.0.0.1" in s.ollama_base_url,
        s.ollama_base_url,
    )

    print()
    failed = [r for r in results if not r[1]]
    print(f"{len(results) - len(failed)}/{len(results)} checks passed.")
    if failed:
        print("FAILED:")
        for name, _, detail in failed:
            print(f"  - {name} ({detail})")
        sys.exit(1)


if __name__ == "__main__":
    main()
