"""Tool-firing rate harness — the automated version of the manual HEAD-vs-working bisection
used repeatedly this project to catch persona/tool regressions (see docs/12, docs/13, and the
"persona edits are high-variance" lesson). Drives the REAL agent (`build_agent`) and streams
each prompt, breaking BEFORE the tool executes (no side effects, no network).

Usage:
    # Measure firing rates on the CURRENT working tree:
    DATABASE_URL=postgresql://localhost/personal_agent_test PYTHONPATH=. \\
        uv run python scripts/verify_firing.py add_reminder,create_draft,web_search

    # Compare working tree vs HEAD (git-stashes, measures HEAD in a FRESH process, restores):
    ...  uv run python scripts/verify_firing.py --baseline add_reminder,create_draft,web_search

Options: --n N (samples per prompt, default 4), --json (machine-readable).

Run with the bot stopped — its keep-warm pings contend with these calls on Ollama (which
serializes), giving misleading rates.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

from scripts._verify_lib import bootstrap_env, fires, require_scratch_db

require_scratch_db()
bootstrap_env()

# Representative prompts per tool (accumulated from real-model measurement this project).
PROMPTS: dict[str, list[str]] = {
    "add_reminder": [
        "remind me to call mom every Sunday",
        "remind me to submit the form tomorrow at 3pm",
        "remind me to take out the trash tonight",
    ],
    "create_draft": [
        "draft a quick note to myself reminding me to call mom",
        "draft a thank-you note to Sam for dinner last night",
        "draft a note to my friend Sam saying hello",
    ],
    "web_search": [
        "what's the weather in Paris right now?",
        "is Trader Joe's open on Sundays?",
        "what's the current price of bitcoin?",
    ],
    "read_email": [
        "what did the landlord's email say?",
        "what does the email from my doctor say?",
    ],
    "calendar_list_events": ["what's on my calendar today?", "am I free tomorrow afternoon?"],
    "build_web_app": ["build me a landing page for my bakery", "make a website about my cat"],
}


def measure(tools: list[str], n: int) -> dict:
    """{tool: {prompt: hits_out_of_n}} on the current on-disk code."""
    from app.agent.graph import build_agent
    agent = build_agent()
    out: dict[str, dict[str, int]] = {}
    for tool in tools:
        prompts = PROMPTS.get(tool)
        if not prompts:
            print(f"  (no prompts registered for {tool!r} — add to PROMPTS)", file=sys.stderr)
            continue
        out[tool] = {p: sum(fires(agent, p, tool) for _ in range(n)) for p in prompts}
    return out


def _print_table(label: str, results: dict, n: int) -> None:
    print(f"\n### {label} (N={n})")
    for tool, per in results.items():
        total = sum(per.values())
        print(f"  {tool}: {total}/{len(per) * n} total")
        for prompt, hits in per.items():
            print(f"    {hits}/{n}  {prompt!r}")


def _print_diff(head: dict, working: dict, n: int) -> None:
    print(f"\n### HEAD vs WORKING (N={n}) — ⚠️ marks a drop")
    regressed = False
    for tool in working:
        print(f"  {tool}:")
        for prompt, w in working[tool].items():
            h = head.get(tool, {}).get(prompt, 0)
            flag = " ⚠️ REGRESSED" if w < h else (" ✅ improved" if w > h else "")
            if w < h:
                regressed = True
            print(f"    HEAD {h}/{n}  ->  WORKING {w}/{n}  {prompt!r}{flag}")
    print("\n" + ("⚠️  Some prompts fire LESS than HEAD — investigate before shipping."
                  if regressed else "✅ No tool fires less than HEAD."))
    sys.exit(1 if regressed else 0)


def _git_dirty() -> bool:
    r = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
    return bool(r.stdout.strip())


def _subprocess_measure(tools: list[str], n: int, script: str) -> dict:
    """Measure in a FRESH process on the currently-checked-out (stashed=HEAD) code, using a
    copy of this script placed OUTSIDE the repo (so `git stash -u` can't remove it)."""
    env = dict(os.environ, PYTHONPATH=os.getcwd())
    r = subprocess.run(
        [sys.executable, script, ",".join(tools), "--n", str(n), "--json"],
        capture_output=True, text=True, env=env, cwd=os.getcwd(),
    )
    if r.returncode != 0:
        raise RuntimeError(f"HEAD measurement failed:\n{r.stderr[-1500:]}")
    return json.loads(r.stdout.strip().splitlines()[-1])


def _run_baseline(tools: list[str], n: int) -> None:
    print("Measuring WORKING tree…", file=sys.stderr)
    working = measure(tools, n)
    if not _git_dirty():
        print("Working tree is clean — nothing to diff against HEAD.")
        _print_table("working", working, n)
        return
    # Copy THIS script out of the repo BEFORE stashing — it's untracked, so `git stash -u`
    # would delete it out from under us.
    tmp_script = os.path.join(tempfile.gettempdir(), "verify_firing_baseline.py")
    shutil.copy(os.path.abspath(__file__), tmp_script)
    print("Stashing (incl. untracked) and measuring HEAD in a fresh process…", file=sys.stderr)
    subprocess.run(["git", "stash", "push", "-u", "-m", "verify_firing-baseline"], check=True)
    try:
        head = _subprocess_measure(tools, n, tmp_script)
    finally:
        subprocess.run(["git", "stash", "pop"], check=True)  # ALWAYS restore
        print("Restored working tree.", file=sys.stderr)
    _print_diff(head, working, n)


def main() -> None:
    ap = argparse.ArgumentParser(description="Tool-firing rate harness / HEAD-vs-working bisection.")
    ap.add_argument("tools", help="comma-separated tool names (e.g. add_reminder,create_draft,web_search)")
    ap.add_argument("--n", type=int, default=4, help="samples per prompt (default 4)")
    ap.add_argument("--baseline", action="store_true", help="diff the working tree against HEAD")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()
    tools = [t.strip() for t in args.tools.split(",") if t.strip()]

    if args.baseline:
        _run_baseline(tools, args.n)
    else:
        results = measure(tools, args.n)
        if args.json:
            print(json.dumps(results))
        else:
            _print_table("working", results, args.n)


if __name__ == "__main__":
    main()
