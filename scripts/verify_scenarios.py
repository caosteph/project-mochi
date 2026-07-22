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

from scripts._verify_lib import (
    bootstrap_env,
    check,
    fires,
    rate,
    require_scratch_db,
    sample_check,
    summarize_and_exit,
    tool_calls,
)

require_scratch_db()
bootstrap_env()

from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402

from app.agent.graph import build_agent  # noqa: E402

# A code-fenced JSON block, or a raw {"key": … — the exact leak from the bad conversation.
_JSON_DUMP = re.compile(r'```json|\{\s*"[\w]+"\s*:')

# Narrating an intention instead of acting on it. From her transcripts: "### Checking Your
# Calendar: I'll check..." — which is why she asked "why is it always stale".
_NARRATES = re.compile(r"i'?ll check|let'?s start by checking|let me check|###\s*checking", re.I)

# Markdown-header / numbered-list dumping. The persona says lead with the answer and don't dump
# lists; the real transcripts are full of "### Checking Your Calendar:" followed by 1. 2. 3.
_DUMPS = re.compile(r"^###\s|\n###\s|\n\s*1\.\s.*\n\s*2\.\s", re.S)


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


def bound_tools_after(turns: list[str]) -> set[str]:
    """Which tools would be BOUND on the last of `turns`, given the ones before it.

    Deterministic (no model): tool selection is pure. This exists because the gate only ever
    tested single-turn prompts, and the whole class of failure it missed is a follow-up — the
    tool can't fire if it was never bound, no matter how good the model is.
    """
    from app.agent import tool_select
    from app.agent.graph import TOOL_SELECT_TURNS
    from app.agent.tools import ALL_TOOLS

    text = "\n".join(turns[-TOOL_SELECT_TURNS:])
    return {t.name for t in tool_select.select_tools(text, ALL_TOOLS)}


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

    # 3. Regressions taken verbatim from her real Telegram history (see docs/14-future-work.md).
    #    Everything above asks "did the right tool fire"; these ask the questions her complaints
    #    actually raise — does it act, does it stay quiet, does it shut up.

    #    "where are you getting your information from and why is it always stale"
    #    Sampled: "can it answer rather than narrate" is a capability, so one wobble is variance.
    cal_replies: list[str] = []

    def calendar_answers() -> tuple[bool, str]:
        text = reply_text(agent, "what's on my calendar today?")
        cal_replies.append(text)
        return not _NARRATES.search(text), f"{text[:70]!r}"

    sample_check("answers the calendar instead of promising to check it",
                 calendar_answers, samples=2, need=1)

    #    "STOP!!!!!" · "I DONT Wng these reminders" · "never gave permission for this"
    #    The suite had NO must-not-fire check, yet unwanted creation was her loudest complaint.
    unwanted = sum(
        fires(agent, "don't set any reminders, just tell me what I already have", "add_reminder")
        for _ in range(2)
    )
    check("respects 'don't set any reminders' — add_reminder must NOT fire", unwanted == 0, f"fired {unwanted}/2")

    #    Her bare "hello" got an unsolicited offer to draft an email.
    #    "triggers no tool" is a MUST-NOT check, so it needs EVERY sample clean (need=samples):
    #    retrying until a violation happens not to repeat would launder it.
    def greeting_fires_nothing() -> tuple[bool, str]:
        names = tool_calls(agent, "hello")
        return not names, f"fired={names}"

    sample_check("a bare greeting triggers no tool", greeting_fires_nothing, samples=2, need=2)

    greet_replies: list[str] = []

    def greeting_is_short() -> tuple[bool, str]:
        text = reply_text(agent, "hello")
        greet_replies.append(text)
        return len(text) <= 400, f"{len(text)} chars"

    sample_check("a bare greeting gets a short answer", greeting_is_short, samples=2, need=1)

    # 4. Follow-ups: the intent is in an EARLIER turn and the latest message is a bare
    #    confirmation. Taken from her 2026-07-21 conversation, where she asked to cancel a
    #    reminder, was asked to confirm, said "yes" — and got a raw JSON tool call pasted as
    #    text plus a false "the reminder has been removed", because "yes" routes to nothing and
    #    cancel_reminder was never bound. Deterministic, so it costs no model calls.
    followups = [
        (["I already did the health insurance claims, remove that reminder", "yes"], "cancel_reminder"),
        (["cancel my reminder about the dentist", "yes please"], "cancel_reminder"),
        (["remind me to call mom on sunday", "yes"], "add_reminder"),
        (["remove the outdated reminder", "do you understand?"], "cancel_reminder"),
    ]
    missed = [(t[-1], w) for t, w in followups if w not in bound_tools_after(t)]
    check("a bare confirmation still binds the tool the conversation is about",
          not missed, f"{len(followups) - len(missed)}/{len(followups)}"
          + (f" | missing {missed[0][1]} after {missed[0][0]!r}" if missed else ""))

    #    Persona: lead with the answer, no dumping. Transcripts: "### Checking Your Calendar:" + 1/2/3.
    dumpy = [t for t in (*replies, *cal_replies, *greet_replies) if t and _DUMPS.search(t)]
    check("no markdown-header / numbered-list dumping", not dumpy,
          f"{len(dumpy)} dumped | {dumpy[0][:60]!r}" if dumpy else "clean")

    # 5. Buttons for decisions (she asked ~5x for "yes or no buttons that i can click").
    #    Deterministic anchor: an AMBIGUOUS cancel must surface a `choice` interrupt (buttons),
    #    not prose — this rides cancel_reminder's own ask_choice, so it doesn't depend on the 7B
    #    picking a tool. Seed two matching reminders, drive the real agent, look for the interrupt.
    from datetime import UTC, datetime, timedelta

    from sqlmodel import Session as _Session
    from sqlmodel import select as _select

    from app.memory.db import get_engine as _get_engine
    from app.memory.models import Reminder as _Reminder
    from app.memory.models import ReminderStatus as _RS

    def _seed_two_dentist() -> None:
        with _Session(_get_engine()) as s:
            for r in s.exec(_select(_Reminder).where(_Reminder.text.ilike("%dentist%"))):
                s.delete(r)
            s.commit()
            for i, t in enumerate(["dentist appointment", "dentist cleaning follow-up"]):
                s.add(_Reminder(text=t, due_at=datetime.now(UTC) + timedelta(days=i + 1),
                                status=_RS.PENDING.value))
            s.commit()

    def ambiguous_cancel_shows_buttons() -> tuple[bool, str]:
        _seed_two_dentist()
        cfg = {"configurable": {"thread_id": f"scn-choice-{uuid.uuid4()}"}}
        payload = None
        try:
            for upd in agent.stream({"messages": [HumanMessage("cancel my dentist reminder")]},
                                    cfg, stream_mode="updates"):
                if "__interrupt__" in upd:
                    payload = upd["__interrupt__"][0].value
        except Exception as exc:
            return False, f"stream error: {type(exc).__name__}"
        ok = bool(payload) and payload.get("type") == "choice" and len(payload.get("options", [])) == 2
        return ok, (f"choice with {payload.get('options')}" if payload else "no interrupt (asked in prose)")

    sample_check("an ambiguous cancel offers buttons, not a typed question",
                 ambiguous_cancel_shows_buttons, samples=3, need=1)

    #    Best-effort: the model reaches for ask_user when a question genuinely has discrete options.
    #    Soft-tier (depends on the 7B choosing the tool), so need=1 of 3 and it's informational.
    ask_user_prompts = [
        "should I remind you about the dentist tomorrow morning or the evening?",
        "do you want the report as a PDF or a Word doc?",
    ]
    au = sum(fires(agent, p, "ask_user") for p in ask_user_prompts)
    check("model can reach for ask_user on a discrete-option question [informational]",
          True, f"{au}/{len(ask_user_prompts)} fired — best-effort soft-tier, ask_user is always bound (CORE)")

    summarize_and_exit()


if __name__ == "__main__":
    main()
