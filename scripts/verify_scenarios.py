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

import re
import uuid

from scripts._verify_lib import bootstrap_env, check, rate, require_scratch_db, summarize_and_exit

require_scratch_db()
bootstrap_env()

from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402

from app.agent.graph import build_agent  # noqa: E402

# A code-fenced JSON block, or a raw {"key": … — the exact leak from the bad conversation.
_JSON_DUMP = re.compile(r'```json|\{\s*"[\w]+"\s*:')


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
    w = rate(agent, ["what's the weather in Paris right now?", "is Trader Joe's open on Sundays?"], "web_search")
    check("outside-world ask → web_search", w >= 1, f"{w}/2")

    # 2. Coherence (the flagship): the ambiguous prompt that broke ("setting what
    #    reminder?") must produce PLAIN PROSE — never a raw JSON dump — and stay on-topic.
    coh = ["setting what reminder?", "what reminder are you talking about?", "huh, which reminder?"]
    replies = [reply_text(agent, p) for p in coh]
    clean = sum(bool(t) and not _JSON_DUMP.search(t) for t in replies)
    check("replies are plain prose, no JSON dump", clean >= 2, f"{clean}/{len(coh)} clean | {replies[0][:70]!r}")
    on_topic = sum("reminder" in t.lower() for t in replies if t)
    check("stays on-topic (talks about reminders)", on_topic >= 1, f"{on_topic}/{len(coh)}")

    summarize_and_exit()


if __name__ == "__main__":
    main()
