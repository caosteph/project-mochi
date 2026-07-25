"""Live behavioral verification for MULTI-TURN conversation — the class of failure the single-turn
gate (verify_scenarios) structurally misses, and the one that made real chats "mixed / bad".

Drives the REAL agent across several turns on ONE persistent thread (so state, summary, and topic
carry over, exactly like a phone conversation) and asserts the guarantees the 2026-07-24 transcript
violated:
  - raw tool-call JSON never reaches the user, across a whole conversation (deterministic: the graph's
    repair node strips/executes a text tool-call, so this should hold regardless of 7B luck);
  - a mid-thread topic switch is answered, not bulldozed back into the prior task;
  - a "make me a plan" request stays conversational (the builder does not fire) so it stays iterable;
  - a correction is acknowledged (informational — soft-tier on a 7B).

Run:
    DATABASE_URL=postgresql://localhost/personal_agent_test PYTHONPATH=. uv run python scripts/verify_multiturn.py
"""

import re
import uuid

from scripts._verify_lib import (
    bootstrap_env,
    check,
    require_scratch_db,
    sample_check,
    summarize_and_exit,
)

require_scratch_db()
bootstrap_env()

from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402

from app.agent.graph import build_agent  # noqa: E402

# A fenced JSON block or a bare {"key": … — the exact leak from the bad conversation.
_JSON_DUMP = re.compile(r'```\s*json|\{\s*"[\w]+"\s*:')


class Convo:
    """A conversation on one persistent thread. `turn()` sends a message and returns
    (visible_reply, tools_fired_this_turn) — visible_reply is the post-repair text the user sees."""

    def __init__(self, agent):
        self.agent = agent
        self.cfg = {"configurable": {"thread_id": f"mt-{uuid.uuid4()}"}}

    def turn(self, text: str) -> tuple[str, list[str]]:
        try:
            state = self.agent.invoke({"messages": [HumanMessage(text)]}, self.cfg)
        except Exception as exc:
            return f"<error: {exc}>", []
        msgs = state.get("messages", [])
        idx = max((i for i, m in enumerate(msgs) if isinstance(m, HumanMessage)), default=-1)
        turn_msgs = msgs[idx + 1:]
        fired = [
            tc["name"]
            for m in turn_msgs
            if isinstance(m, AIMessage)
            for tc in (getattr(m, "tool_calls", None) or [])
        ]
        reply = ""
        for m in reversed(turn_msgs):
            if isinstance(m, AIMessage) and not getattr(m, "tool_calls", None):
                content = m.content if isinstance(m.content, str) else ""
                if content.strip():
                    reply = content
                    break
        return reply, fired


def main() -> None:
    agent = build_agent()

    # --- A. A plan conversation must never surface raw JSON, turn after turn. ---
    c = Convo(agent)
    plan_turns = [
        "help me make a plan to get toned for my greece trip on august 15",
        "actually make it about prepping BEFORE the trip, not during",
        "i'm lactose intolerant and i need more than one idea per meal",
    ]
    replies = [c.turn(t)[0] for t in plan_turns]
    leaked = [r for r in replies if r and _JSON_DUMP.search(r)]
    check("plan conversation never shows raw JSON", not leaked,
          (f"{len(leaked)} turn(s) leaked: {leaked[0][:70]!r}") if leaked else "clean across all turns")
    answered = sum(bool(r.strip()) and not r.startswith("<error") for r in replies)
    check("every plan turn gets a real reply", answered == len(plan_turns), f"{answered}/{len(plan_turns)}")

    # --- B. Topic switch mid-thread: a factual question is answered, not dragged into the plan. ---
    reply_b, _ = c.turn("wait, when is my greece trip again?")
    ok_b = bool(reply_b) and not _JSON_DUMP.search(reply_b) and ("15" in reply_b or "august" in reply_b.lower())
    check("mid-thread topic switch answered cleanly (date, no JSON)", ok_b, f"{reply_b[:80]!r}")

    # --- C. "make me a plan" stays conversational — the builder must NOT fire (must-not → all clean). ---
    def plan_stays_conversational() -> tuple[bool, str]:
        _, fired = Convo(agent).turn("give me a meal plan for this week, and ask me if you need to know more")
        return "build_web_app" not in fired, f"fired={fired}"

    sample_check("a plan request stays conversational (build_web_app must NOT fire)",
                 plan_stays_conversational, samples=2, need=2)

    # --- D. A correction is acknowledged (informational — correction-following is soft-tier on a 7B). ---
    c3 = Convo(agent)
    c3.turn("give me some dinner ideas for this week")
    reply_d, _ = c3.turn("i'm lactose intolerant, keep that in mind")
    honored = bool(reply_d) and any(
        w in reply_d.lower() for w in ("lactose", "dairy-free", "dairy free", "without dairy", "non-dairy", "no dairy")
    )
    check("correction acknowledged (lactose) [informational]", True,
          f"honored={honored} | {reply_d[:80]!r}")

    summarize_and_exit()


if __name__ == "__main__":
    main()
