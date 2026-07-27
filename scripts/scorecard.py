"""Behavioral scorecard — RATES (not pass/fail) for the current LOCAL_MODEL + model profile, so a
model swap (docs/14 #9, a free same-size model; or #29, the Mac mini) is evidence-driven rather than
vibes. A REPORT, not a gate: deliberately kept OUT of verify_all.sh's fail loop because real-model
probes are slow and stochastic (the gates already timed out twice recently).

How this differs from verify_firing.py: that measures tool-FIRING rates and does a HEAD-vs-working
*git* A/B. This adds the conversation-level behaviors a bigger model is supposed to fix and that
firing rates can't see:
  - no_json_dump           — an ambiguous prompt yields prose, never a raw tool-call JSON leak
  - honors_no_reminders    — a single-turn negative constraint is obeyed (add_reminder must NOT fire)
  - honors_across_summary  — the SAME constraint is obeyed after the conversation has been through a
                             rolling summary (the real multi-turn failure; verify_multiturn is only
                             3 turns so it never summarizes — this is the point of this probe)
  - answers_not_narrates   — "what's on my calendar" is answered, not "let me check…"
It reuses verify_firing.PROMPTS + _verify_lib primitives; build QUALITY is intentionally absent —
codegen quality depends on the HOSTED model, not LOCAL_MODEL, and true visual quality needs the
deferred vision loop (docs/14 #30), so a structural-validator score here would only mislead. Use
scripts/verify_builder.py for the build path.

Run (bot STOPPED — keep-warm pings contend on Ollama and skew rates):
    DATABASE_URL=postgresql://localhost/personal_agent_test PYTHONPATH=. \
        uv run python scripts/scorecard.py                 # full
    ...  uv run python scripts/scorecard.py --quick        # fast subset (skips the slow summary probe)
    ...  uv run python scripts/scorecard.py --json         # one machine-readable row

A/B recipe: run once and note the header; change LOCAL_MODEL (or, once item 2 lands, a scaffolding
flag) and run again; diff the two --json rows. The first run on the current 7B IS the baseline —
record it before any swap or the later numbers have nothing to compare against.
"""

import argparse
import json
import re
import sys
import uuid

from scripts._verify_lib import bootstrap_env, fires, require_scratch_db

require_scratch_db()
bootstrap_env()

from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402

from app.agent.graph import build_agent  # noqa: E402
from app.config import settings  # noqa: E402
from scripts.verify_firing import PROMPTS  # noqa: E402

# The exact leak form and the narrate-instead-of-act form, copied from verify_scenarios.py so this
# stays a standalone report (importing that module would drag its build_agent import + gate wiring).
_JSON_DUMP = re.compile(r'```json|\{\s*"[\w]+"\s*:')
_NARRATES = re.compile(r"i'?ll check|let'?s start by checking|let me check|###\s*checking", re.I)

# Tools whose firing rate we report (a representative core, not every tool).
_FIRING_TOOLS = ["add_reminder", "create_draft", "web_search", "read_email",
                 "calendar_list_events", "build_web_app"]

# The ambiguous prompt that broke and leaked JSON ("setting what reminder?").
_AMBIGUOUS = ["setting what reminder?", "what reminder are you talking about?", "huh, which reminder?"]

# A negative constraint + the request that would otherwise trip add_reminder.
_NO_REMINDERS = "don't set any reminders, just tell me what I already have"
_CONSTRAINT = "One rule from now on: never create reminders for me, no matter what I ask you to."
_TRIGGER = "I need to remember to call the dentist tomorrow at 3pm."

# Long, side-effect-free filler to push the conversation past the summarizer's thresholds
# (working_buffer_keep_recent messages AND working_buffer_max_tokens). Declarative so it doesn't
# trip tools; asks only for an acknowledgement.
_FILLER = [
    "Some background so you have context, just acknowledge briefly. "
    + ("I have a lot going on across work and personal life this month and next. " * 55),
    "More context, please just say ok. "
    + ("There are projects, errands, appointments, and travel plans in the mix. " * 55),
    "Even more background, a short acknowledgement is fine. "
    + ("I am thinking through priorities, deadlines, and what matters most right now. " * 55),
    "Last of the brain dump, just acknowledge. "
    + ("Wrapping up with reflections on the week ahead and how I want to spend it. " * 55),
]


def _reply_text(agent, prompt: str) -> str:
    """Run one turn to completion on a fresh thread; return the final plain-text reply ('' if none)."""
    cfg = {"configurable": {"thread_id": f"sc-{uuid.uuid4()}"}}
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


def _tool_calls_on_thread(agent, prompt: str, thread: str) -> list[str]:
    """Tool names chosen at the FIRST agent step of `prompt`, on an EXISTING thread (so a rolling
    summary already in that thread is in context). Breaks before execution — no side effects."""
    cfg = {"configurable": {"thread_id": thread}}
    for update in agent.stream({"messages": [HumanMessage(prompt)]}, cfg, stream_mode="updates"):
        step = update.get("agent")
        if step and step.get("messages"):
            return [tc["name"] for tc in (getattr(step["messages"][-1], "tool_calls", None) or [])]
    return []


def _pct(hits: int, total: int) -> float:
    return round(100.0 * hits / total, 1) if total else 0.0


def _firing_rates(agent, n: int, quick: bool) -> dict:
    out = {}
    for tool in _FIRING_TOOLS:
        prompts = PROMPTS.get(tool, [])
        if quick:
            prompts = prompts[:1]
        if not prompts:
            continue
        hits = sum(fires(agent, p, tool) for p in prompts for _ in range(n))
        out[tool] = _pct(hits, len(prompts) * n)
    return out


def _no_json_dump_rate(agent, n: int, quick: bool) -> float:
    prompts = _AMBIGUOUS[:1] if quick else _AMBIGUOUS
    clean = sum(
        bool(t := _reply_text(agent, p)) and not _JSON_DUMP.search(t)
        for p in prompts for _ in range(n)
    )
    return _pct(clean, len(prompts) * n)


def _honors_no_reminders_rate(agent, n: int) -> float:
    """Single-turn must-not: add_reminder should NOT fire when told not to."""
    obeyed = sum(not fires(agent, _NO_REMINDERS, "add_reminder") for _ in range(n))
    return _pct(obeyed, n)


def _answers_not_narrates_rate(agent, n: int) -> float:
    answered = sum(not _NARRATES.search(_reply_text(agent, "what's on my calendar today?") or "") for _ in range(n))
    return _pct(answered, n)


def _honors_across_summary(agent, n: int) -> tuple[float, int]:
    """Multi-turn must-not that crosses the summarization boundary: build a conversation long enough
    to trigger the rolling summary, confirm it formed, then check the negative constraint still holds.
    Returns (obey_rate, runs_that_actually_summarized) — the second value keeps the metric honest: if
    nothing summarized, the rate says nothing about the boundary."""
    obeyed = summarized = 0
    for _ in range(n):
        thread = f"sc-corr-{uuid.uuid4()}"
        cfg = {"configurable": {"thread_id": thread}}
        try:
            for f in _FILLER:
                agent.invoke({"messages": [HumanMessage(f)]}, cfg)
            if (agent.get_state(cfg).values or {}).get("summary"):
                summarized += 1
            agent.invoke({"messages": [HumanMessage(_CONSTRAINT)]}, cfg)
            fired = "add_reminder" in _tool_calls_on_thread(agent, _TRIGGER, thread)
            obeyed += not fired
        except Exception as exc:  # a wobble on one run shouldn't abort the whole scorecard
            print(f"  (summary probe run errored: {type(exc).__name__})", file=sys.stderr)
    return _pct(obeyed, n), summarized


def _header() -> dict:
    from app.agent import router
    return {
        "local_model": settings.local_model,
        "model_temperature": settings.model_temperature,
        "model_max_output_tokens": settings.model_max_output_tokens,
        "hosted_available": router.hosted_available(),
        # Item 2 (scaffolding flags) is deferred; when it lands, record the flag state here so runs
        # remain comparable across flag A/Bs.
        "scaffolding_flags": "n/a (docs/14 item 2 deferred)",
    }


def run(n: int, quick: bool) -> dict:
    agent = build_agent()
    metrics: dict = {
        "tool_firing_pct": _firing_rates(agent, n, quick),
        "no_json_dump_pct": _no_json_dump_rate(agent, n, quick),
        "honors_no_reminders_pct": _honors_no_reminders_rate(agent, n),
        "answers_not_narrates_pct": _answers_not_narrates_rate(agent, n),
    }
    if quick:
        metrics["honors_across_summary_pct"] = None  # slow probe skipped
        metrics["summary_probe_summarized_runs"] = None
    else:
        rate, summarized = _honors_across_summary(agent, n)
        metrics["honors_across_summary_pct"] = rate
        metrics["summary_probe_summarized_runs"] = f"{summarized}/{n}"
    return {"header": _header(), "n": n, "quick": quick, "metrics": metrics}


def _print_report(report: dict) -> None:
    h, m = report["header"], report["metrics"]
    print("\n=== Behavioral scorecard ===")
    print(f"model={h['local_model']}  temp={h['model_temperature']}  "
          f"max_out={h['model_max_output_tokens']}  hosted={h['hosted_available']}")
    print(f"flags: {h['scaffolding_flags']}   |   N={report['n']} per prompt"
          + ("  (quick)" if report["quick"] else ""))
    print("\ntool firing:")
    for tool, pct in m["tool_firing_pct"].items():
        print(f"  {pct:5.1f}%  {tool}")
    print("\nbehavior:")
    print(f"  {m['no_json_dump_pct']:5.1f}%  no JSON dump on ambiguous prompts")
    print(f"  {m['honors_no_reminders_pct']:5.1f}%  honors 'don't set any reminders' (single-turn)")
    across = m["honors_across_summary_pct"]
    if across is None:
        print("      -   honors constraint across a rolling summary (skipped in --quick)")
    else:
        print(f"  {across:5.1f}%  honors constraint across a rolling summary "
              f"(summarized {m['summary_probe_summarized_runs']} runs)")
    print(f"  {m['answers_not_narrates_pct']:5.1f}%  answers calendar instead of narrating")
    print("\n(Report, not a gate. Higher is better; compare two runs, don't read a single number as"
          " a threshold.)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Behavioral scorecard (rates) for the current LOCAL_MODEL.")
    ap.add_argument("--n", type=int, default=3, help="samples per prompt (default 3)")
    ap.add_argument("--quick", action="store_true", help="fewer prompts; skip the slow summary probe")
    ap.add_argument("--json", action="store_true", help="emit one machine-readable row (for A/B diffs)")
    args = ap.parse_args()

    report = run(args.n, args.quick)
    if args.json:
        print(json.dumps(report))
    else:
        _print_report(report)


if __name__ == "__main__":
    main()
