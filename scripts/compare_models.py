"""A/B local models on one command: is a newer same-size model actually better for Mochi?

docs/14 "Try a newer same-size local model": the 2026-07 finding was that context, not model
capability, was the bottleneck — so the model choice deserves a fair re-test at equal context,
and the answer decides whether the Mac mini (#29) is still needed. This automates the recipe:

  for each model (the FIRST is the baseline — normally today's qwen2.5:7b):
    1. pull it if missing, derive an 8k-context variant (same Modelfile as production, so the
       comparison is at equal context — the variable we already proved matters)
    2. measure speed + resident memory on a ~4k-token prompt (a real turn's size; on a 16GB Air
       prompt processing, not decode, is most of the latency she feels)
    3. run scripts/scorecard.py --json (tool firing, no JSON dump, negative constraints, answers
       vs narrates) and scripts/verify_scenarios.py (the behavioral gate) against it
    4. unload it, so two models never sit in memory at once
  then print a side-by-side table and a verdict per candidate against the baseline.

Verdict rule (deliberately conservative — swapping the model under a working agent is the
riskiest config change there is): ADOPT only if the scenarios gate passes, no scorecard rate
drops by more than --tolerance points, at least one rises by more than it, and it fits
--max-resident-gb. Rates are stochastic; a lone ADOPT is a reason to re-run, not to switch.

Run with the bot STOPPED (this refuses otherwise — keep-warm pings contend on Ollama and skew
the rates, and it holds the single-instance lock so launchd can't start the bot mid-run):

    launchctl bootout gui/$(id -u)/com.mochi.agent
    DATABASE_URL=postgresql://localhost/personal_agent_test PYTHONPATH=. \\
        uv run python scripts/compare_models.py qwen2.5:7b qwen3:8b llama3.1:8b
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.mochi.agent.plist

Slow by nature (each model runs the full scorecard; budget ~30-60 min per model on the Air).
--quick trims the scorecard to one prompt per probe for a first pass. Results are written to
data/model-compare-<timestamp>.json (git-ignored). Report, not a gate — kept out of verify_all.sh.
Adopting a winner is then: point LOCAL_MODEL at its -8k variant, mirror the Modelfile into
ollama/ and MODEL in scripts/preflight.sh, and re-run the full gate.
"""

import argparse
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
NUM_CTX = 8192  # production's context (ollama/Modelfile.qwen2.5-7b-8k)

# The scalar scorecard rates compared between models (tool-firing rates are expanded per tool).
_RATES = ("no_json_dump_pct", "honors_no_reminders_pct", "honors_across_summary_pct",
          "answers_not_narrates_pct")


def derived_name(base: str) -> str:
    """The 8k variant's tag: qwen3:8b → qwen3:8b-8k; an untagged base gets an :8k tag."""
    return f"{base}-8k" if ":" in base else f"{base}:8k"


def modelfile(base: str) -> str:
    return f"FROM {base}\nPARAMETER num_ctx {NUM_CTX}\n"


def flat_rates(scorecard: dict) -> dict[str, float | None]:
    """One flat name → percent map out of a scorecard --json report."""
    m = scorecard["metrics"]
    out = {f"fires {tool}": pct for tool, pct in m["tool_firing_pct"].items()}
    out.update({k: m.get(k) for k in _RATES})
    return out


def verdict(base: dict, cand: dict, *, tolerance: float, max_resident_gb: float) -> str:
    """Compare one candidate's result row against the baseline's. Pure, so it's unit-tested."""
    if cand.get("error"):
        return f"NO — run failed: {cand['error']}"
    if not cand["scenarios_passed"]:
        return "NO — verify_scenarios gate failed"
    gb = cand["speed"].get("resident_gb")
    if gb is not None and gb > max_resident_gb:
        return f"NO — {gb:.1f} GB resident exceeds {max_resident_gb:.1f} GB"
    b, c = flat_rates(base["scorecard"]), flat_rates(cand["scorecard"])
    shared = [k for k in b if b[k] is not None and c.get(k) is not None]
    drops = [f"{k} {b[k]:.0f}→{c[k]:.0f}" for k in shared if c[k] < b[k] - tolerance]
    if drops:
        return "NO — regressed: " + ", ".join(drops)
    gains = [f"{k} {b[k]:.0f}→{c[k]:.0f}" for k in shared if c[k] > b[k] + tolerance]
    if not gains:
        return "TIE — no gain beyond noise; keep the baseline"
    return "ADOPT? — better on: " + ", ".join(gains) + " (re-run to confirm before switching)"


# --- Ollama -----------------------------------------------------------------------


def _native() -> str:
    return os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1").removesuffix("/v1")


def _installed() -> set[str]:
    resp = httpx.get(f"{_native()}/api/tags", timeout=10)
    resp.raise_for_status()
    return {m["name"] for m in resp.json().get("models", [])}


def _has(installed: set[str], name: str) -> bool:
    return name in installed or (":" not in name and f"{name}:latest" in installed)


def prepare(base: str) -> str:
    """Pull `base` if needed and create its 8k variant. Returns the variant's tag."""
    if not _has(_installed(), base):
        print(f"  pulling {base} (one-time download)…", flush=True)
        subprocess.run(["ollama", "pull", base], check=True)
    name = derived_name(base)
    with tempfile.NamedTemporaryFile("w", suffix=".Modelfile", delete=False) as fh:
        fh.write(modelfile(base))
    try:  # always (re)create: cheap (reuses the base layers), and stays right after a re-pull
        subprocess.run(["ollama", "create", name, "-f", fh.name], check=True, capture_output=True)
    finally:
        os.unlink(fh.name)
    return name


def _unload(model: str) -> None:
    with contextlib.suppress(Exception):
        httpx.post(f"{_native()}/api/generate", json={"model": model, "keep_alive": 0}, timeout=30)


# ~4k tokens: the size of a real turn's prompt (persona + tools + profile card + history).
_SPEED_PROMPT = (
    "Here is some context about my week. "
    + "I have meetings, errands, a dentist appointment, a few deadlines, and travel to plan. " * 220
    + "\nIn two sentences, what should I prioritize?"
)


def measure_speed(model: str) -> dict:
    """Prompt-processing and decode speed on a turn-sized prompt, plus resident memory. Runs
    twice and keeps the second, so the one-time model load isn't counted as speed."""
    body = {"model": model, "prompt": _SPEED_PROMPT, "stream": False,
            "options": {"num_predict": 128, "temperature": 0}}
    r: dict = {}
    for _ in range(2):
        resp = httpx.post(f"{_native()}/api/generate", json=body, timeout=900)
        resp.raise_for_status()
        r = resp.json()
    ps = httpx.get(f"{_native()}/api/ps", timeout=10).json().get("models", [])
    size = next((m.get("size") for m in ps if m.get("name") == model or m.get("model") == model), None)

    def per_sec(count: str, dur: str) -> float | None:
        return round(r[count] / (r[dur] / 1e9), 1) if r.get(count) and r.get(dur) else None

    return {
        "prompt_tokens": r.get("prompt_eval_count"),
        "prompt_tok_s": per_sec("prompt_eval_count", "prompt_eval_duration"),
        "decode_tok_s": per_sec("eval_count", "eval_duration"),
        "turn_s": round(r["total_duration"] / 1e9, 1) if r.get("total_duration") else None,
        "resident_gb": round(size / 1e9, 1) if size else None,
    }


# --- the scripts this wraps ---------------------------------------------------------


def _script(name: str, model: str, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "LOCAL_MODEL": model, "PYTHONPATH": str(ROOT)}
    return subprocess.run([sys.executable, f"scripts/{name}", *args], cwd=ROOT, env=env,
                          capture_output=True, text=True)


def run_scorecard(model: str, n: int, quick: bool) -> dict:
    args = ["--json", "--n", str(n)] + (["--quick"] if quick else [])
    proc = _script("scorecard.py", model, *args)
    rows = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
    if proc.returncode != 0 or not rows:
        raise RuntimeError(f"scorecard failed: {(proc.stderr or proc.stdout).strip()[-300:]}")
    report = json.loads(rows[-1])
    # The whole comparison is void if the override didn't take (e.g. something pinned the model).
    if report["header"]["local_model"] != model:
        raise RuntimeError(f"scorecard ran {report['header']['local_model']!r}, not {model!r}")
    return report


def run_scenarios(model: str) -> tuple[bool, str]:
    proc = _script("verify_scenarios.py", model)
    tally = next((ln for ln in reversed(proc.stdout.splitlines()) if "checks passed" in ln), "?")
    return proc.returncode == 0, tally.strip()


def evaluate(base: str, n: int, quick: bool) -> dict:
    row: dict = {"base": base}
    try:
        model = prepare(base)
        row["model"] = model
        print(f"  {model}: speed…", flush=True)
        row["speed"] = measure_speed(model)
        print(f"  {model}: scorecard (slow)…", flush=True)
        row["scorecard"] = run_scorecard(model, n, quick)
        print(f"  {model}: verify_scenarios…", flush=True)
        row["scenarios_passed"], row["scenarios"] = run_scenarios(model)
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if "model" in row:
            _unload(row["model"])
    return row


# --- report -------------------------------------------------------------------------


def _fmt(v) -> str:
    return "-" if v is None else (f"{v:.0f}" if isinstance(v, float) else str(v))


def print_report(rows: list[dict], *, tolerance: float, max_resident_gb: float) -> None:
    ok = [r for r in rows if not r.get("error")]
    names = [r["model"] for r in ok]
    width = max([28, *(len(n) + 2 for n in names)])
    print("\n=== Model comparison (first = baseline) ===")
    print(f"{'':30}" + "".join(f"{n:>{width}}" for n in names))
    for key, label in (("prompt_tok_s", "prompt tok/s (~4k prompt)"), ("decode_tok_s", "decode tok/s"),
                       ("turn_s", "turn seconds"), ("resident_gb", "resident GB")):
        print(f"{label:30}" + "".join(f"{_fmt(r['speed'].get(key)):>{width}}" for r in ok))
    for key in flat_rates(ok[0]["scorecard"]) if ok else ():
        print(f"{key + ' %':30}" + "".join(f"{_fmt(flat_rates(r['scorecard']).get(key)):>{width}}" for r in ok))
    print(f"{'verify_scenarios':30}" + "".join(f"{r['scenarios']:>{width}}" for r in ok))
    print()
    for r in rows[1:]:
        label = r.get("model", r["base"])
        if not ok or rows[0].get("error"):
            print(f"{label}: no verdict — the baseline run failed ({rows[0].get('error')})")
        else:
            print(f"{label}: {verdict(rows[0], r, tolerance=tolerance, max_resident_gb=max_resident_gb)}")
    for r in rows:
        if r.get("error"):
            print(f"{r['base']}: run failed — {r['error']}")


def main() -> None:
    ap = argparse.ArgumentParser(description="A/B local models for Mochi (first = baseline).")
    ap.add_argument("models", nargs="+", help="Ollama base tags, baseline first (e.g. qwen2.5:7b qwen3:8b)")
    ap.add_argument("--n", type=int, default=3, help="scorecard samples per prompt (default 3)")
    ap.add_argument("--quick", action="store_true", help="scorecard --quick (first pass only)")
    ap.add_argument("--tolerance", type=float, default=10.0, help="points of noise to ignore (default 10)")
    ap.add_argument("--max-resident-gb", type=float, default=7.0,
                    help="memory ceiling on the 16GB Air (default 7; today's 7B-8k is ~4.9)")
    args = ap.parse_args()

    if len(args.models) < 2:
        ap.error("give a baseline and at least one candidate")
    url = os.environ.get("DATABASE_URL", "")
    if "personal_agent_test" not in url:
        sys.exit(f"Refusing to run: DATABASE_URL must point at the scratch DB (got {url!r}).")
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "verify_placeholder")
    os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
    from app.main import acquire_single_instance_lock

    if not acquire_single_instance_lock():
        sys.exit("Mochi is running — stop it first (launchctl bootout gui/$(id -u)/com.mochi.agent).")

    started = time.monotonic()
    rows = []
    for base in args.models:
        print(f"\n--- {base} ---", flush=True)
        rows.append(evaluate(base, args.n, args.quick))

    print_report(rows, tolerance=args.tolerance, max_resident_gb=args.max_resident_gb)
    out = ROOT / "data" / f"model-compare-{datetime.now():%Y%m%d-%H%M}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"args": vars(args), "rows": rows}, indent=2))
    print(f"\nSaved {out.relative_to(ROOT)} · took {(time.monotonic() - started) / 60:.0f} min")


if __name__ == "__main__":
    main()
