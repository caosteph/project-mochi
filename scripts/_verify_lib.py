"""Shared helpers for the `scripts/verify_*.py` live checks.

These scripts had drifted into copy-paste: `check()` existed in 9 of them (in three different
signatures), `fires()` in 4, the scratch-DB guard in 7, and the Telegram env placeholders in 10.
Same semantics, slightly different wording each time — so output and floors read inconsistently.

**Import-order matters.** `app.config` reads the environment at import time, so a script must call
`require_scratch_db()` and `bootstrap_env()` BEFORE importing anything from `app.*`:

    from scripts._verify_lib import bootstrap_env, check, fires, require_scratch_db, summarize_and_exit

    require_scratch_db()
    bootstrap_env()

    from app.agent.graph import build_agent  # noqa: E402

Resolves as a namespace package because the scripts run with `PYTHONPATH=.` (repo root) — including
`verify_firing.py --baseline`, which runs a copy of itself from a temp dir with `cwd=<repo>`.
"""

import os
import sys
import uuid

# (name, passed, detail) — shared across every check in the running script.
results: list[tuple[str, bool, str]] = []


def require_scratch_db() -> None:
    """Refuse to run against anything but a scratch database. These scripts write rows and drive
    the real agent; pointing them at the live DB would pollute Stephanie's actual memory."""
    url = os.environ.get("DATABASE_URL", "")
    if "personal_agent_test" not in url and "verify" not in url:
        print(f"Refusing to run: DATABASE_URL must point at a scratch DB (got {url!r}).")
        sys.exit(1)


def bootstrap_env() -> None:
    """Placeholder Telegram creds so importing app.config succeeds outside the real app."""
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "verify_placeholder")
    os.environ.setdefault("TELEGRAM_CHAT_ID", "1")


def check(name: str, ok: bool, detail: str = "") -> None:
    """Record + print one PASS/FAIL line."""
    results.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'} | {name}" + (f" | {detail}" if detail else ""))


def skip(name: str, why: str = "") -> None:
    """A check that couldn't run (missing creds/config). Printed, never counted as failure."""
    print(f"SKIP | {name}" + (f" — {why}" if why else ""))


def summarize_and_exit() -> None:
    """Print the tally (and *which* checks failed) and exit non-zero if anything did, so
    `verify_all.sh` flags it. Listing the failures was verify_phase1's behaviour — the best
    of the copy-pasted variants, so consolidating gives it to every script."""
    print()
    failed = [r for r in results if not r[1]]
    print(f"{len(results) - len(failed)}/{len(results)} checks passed.")
    if failed:
        print("FAILED:")
        for name, _, detail in failed:
            print(f"  - {name}" + (f" ({detail})" if detail else ""))
        sys.exit(1)


def tool_calls(agent, prompt: str) -> list[str]:
    """Tool names the model chooses at the FIRST agent step, before anything executes.

    Returning the list (rather than a bool) is what makes "must NOT fire" and "should fire
    nothing at all" checks possible — the class of check the suite was missing.
    """
    from langchain_core.messages import HumanMessage  # lazy: keep import order flexible

    cfg = {"configurable": {"thread_id": f"vfy-{uuid.uuid4()}"}}
    for update in agent.stream({"messages": [HumanMessage(prompt)]}, cfg, stream_mode="updates"):
        agent_step = update.get("agent")
        if agent_step and agent_step.get("messages"):
            return [tc["name"] for tc in (getattr(agent_step["messages"][-1], "tool_calls", None) or [])]
    return []


def fires(agent, prompt: str, tool: str) -> bool:
    """True if the model FIRES `tool` for `prompt`. Breaks BEFORE the tool executes, so
    measuring tool choice never creates a draft, builds a site, or hits the network."""
    return tool in tool_calls(agent, prompt)


def rate(agent, prompts: list[str], tool: str) -> int:
    """How many of `prompts` fire `tool` (one sample each)."""
    return sum(fires(agent, p, tool) for p in prompts)
