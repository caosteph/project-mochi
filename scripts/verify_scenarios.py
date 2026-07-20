"""Live behavioral verification — drives the REAL agent (build_agent) through realistic
conversations and asserts BEHAVIOR, not just that a tool fired. This is the class of check
the mocked unit tests can't do: every recent failure (an incoherent raw-JSON dump, an
off-topic reply that wandered into drafting emails) was real-model behavior that only
shows up when the actual 7B runs end-to-end.

Soft floors (the local 7B has run-to-run variance), like the other verify scripts. Runs
against a scratch DB; the coherence turns complete through local tools only (memory /
reminder-list) — never Google — so no network/creds are needed.

Run:
    DATABASE_URL=postgresql://localhost/personal_agent_test PYTHONPATH=. uv run python scripts/verify_scenarios.py
"""

import os
import re
import sys
import uuid

if "personal_agent_test" not in os.environ.get("DATABASE_URL", "") and "verify" not in os.environ.get("DATABASE_URL", ""):
    print(f"Refusing to run: DATABASE_URL must be a scratch DB (got {os.environ.get('DATABASE_URL')!r}).")
    sys.exit(1)
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "verify_placeholder")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")

from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402

from app.agent.graph import build_agent  # noqa: E402

results: list[tuple[str, bool, str]] = []

# A code-fenced JSON block, or a raw {"key": … — the exact leak from the bad conversation.
_JSON_DUMP = re.compile(r'```json|\{\s*"[\w]+"\s*:')


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'} | {name}" + (f" | {detail}" if detail else ""))


def fires(agent, prompt: str, tool: str) -> bool:
    """True if the model FIRES `tool` for `prompt`. Streams and breaks BEFORE the tool
    executes, so nothing is actually built/served/written."""
    cfg = {"configurable": {"thread_id": f"scn-{uuid.uuid4()}"}}
    for update in agent.stream({"messages": [HumanMessage(prompt)]}, cfg, stream_mode="updates"):
        ap = update.get("agent")
        if ap and ap.get("messages"):
            names = [tc["name"] for tc in (getattr(ap["messages"][-1], "tool_calls", None) or [])]
            return tool in names
    return False


def reply_text(agent, prompt: str) -> str:
    """Run one turn to completion; return the final plain-text assistant reply ('' if the
    model produced no clean reply — e.g. it wandered off into a tool call instead)."""
    cfg = {"configurable": {"thread_id": f"scn-{uuid.uuid4()}"}}
    try:
        state = agent.invoke({"messages": [HumanMessage(prompt)]}, cfg)
    except Exception:
        return ""
    for m in reversed(state.get("messages", [])):
        if isinstance(m, AIMessage) and not getattr(m, "tool_calls", None):
            content = m.content if isinstance(m.content, str) else ""
            if content.strip():
                return content
    return ""


def rate(agent, prompts, tool) -> int:
    return sum(fires(agent, p, tool) for p in prompts)


def main() -> None:
    agent = build_agent()

    # 1. The obvious asks fire the right tool (break before execution → no side effects).
    r = rate(agent, ["remind me to call mom every Sunday", "ping me in 2 hours to stretch"], "add_reminder")
    check("reminder ask → add_reminder", r >= 1, f"{r}/2")
    c = rate(agent, ["what's on my calendar today?", "am I free this afternoon?"], "calendar_list_events")
    check("calendar ask → calendar_list_events", c >= 1, f"{c}/2")
    b = rate(agent, ["build me a landing page for my bakery", "make a website about my cat"], "build_web_app")
    check("build ask → build_web_app", b >= 1, f"{b}/2")
    e = rate(agent, ["what did the landlord's email say?", "what does the email from my doctor say?"], "read_email")
    check("read-content ask → read_email", e >= 1, f"{e}/2")

    # 2. Coherence (the flagship): the ambiguous prompt that broke ("setting what
    #    reminder?") must produce PLAIN PROSE — never a raw JSON dump — and stay on-topic.
    coh = ["setting what reminder?", "what reminder are you talking about?", "huh, which reminder?"]
    replies = [reply_text(agent, p) for p in coh]
    clean = sum(bool(t) and not _JSON_DUMP.search(t) for t in replies)
    check("replies are plain prose, no JSON dump", clean >= 2, f"{clean}/{len(coh)} clean | {replies[0][:70]!r}")
    on_topic = sum("reminder" in t.lower() for t in replies if t)
    check("stays on-topic (talks about reminders)", on_topic >= 1, f"{on_topic}/{len(coh)}")

    print()
    failed = [x for x in results if not x[1]]
    print(f"{len(results) - len(failed)}/{len(results)} checks passed.")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
