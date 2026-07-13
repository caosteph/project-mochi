"""Live Phase 3B verification — the quarantined reader against the REAL local model.

The offline suite (tests/test_email_signals.py) proves the pipeline logic with a fake
reader. This script proves the thing that can't be faked: that the actual local 7B
extracts a correct typed signal from realistic email, and that a prompt-injection in a
body can't do anything but produce structured data. It also (optionally) exercises the
real Gmail body-read path if a token is configured.

Run (Ollama must be serving the local model):
    PYTHONPATH=. uv run python scripts/verify_phase3b.py
"""

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "verify_placeholder")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")

from app.agent import quarantine  # noqa: E402
from app.agent.quarantine import ExtractedSignal  # noqa: E402
from app.config import settings  # noqa: E402
from app.integrations import google_auth, google_gmail  # noqa: E402

FIXTURES = json.loads((Path(__file__).parent.parent / "tests" / "fixtures" / "email_signals.json").read_text())
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'} | {name}" + (f" | {detail}" if detail else ""))


def main() -> None:
    print(f"Reader model: {settings.local_model} @ {settings.ollama_base_url}\n")

    # 1. Extraction accuracy across signal types (the real model, not a fake).
    hits, total = 0, 0
    for fx in FIXTURES["accuracy"]:
        total += 1
        try:
            sig = quarantine.extract_signal(fx["email"])
        except Exception as exc:
            check(f"extract: {fx['name']}", False, f"reader error: {str(exc)[:80]}")
            continue
        exp = fx["expect"]
        ok = sig.is_actionable == exp["is_actionable"]
        # For actionable ones, also require the right type + a non-empty title.
        if exp["is_actionable"]:
            ok = ok and sig.signal_type == exp["signal_type"] and bool(sig.title and sig.title.strip())
        hits += ok
        check(f"extract: {fx['name']}", ok,
              f"got actionable={sig.is_actionable} type={sig.signal_type!r} title={sig.title!r}")
    rate = hits / total if total else 0.0
    check("extraction accuracy across signal types", rate >= 0.6,
          f"{hits}/{total} correct — floor 60% (soft-tier reliability on the local 7B)")

    # 2. Injection resistance — a hostile body must yield ONLY structured data.
    inj = FIXTURES["injection"]["email"]
    try:
        sig = quarantine.extract_signal(inj)
        structural_ok = isinstance(sig, ExtractedSignal)
        # Whatever the model 'decided', it can only have filled capped fields — it has
        # no tools, so nothing was sent/deleted. Confirm the caps held and the output
        # is a plain structured object.
        caps_ok = (sig.title is None or len(sig.title) <= 120) and (sig.summary is None or len(sig.summary) <= 300)
        check("injection body → structured-only, no action possible", structural_ok and caps_ok,
              f"type={type(sig).__name__} title={sig.title!r}")
    except Exception as exc:
        # Even a hard failure here is 'safe' (no action), but flag it — the reader
        # should degrade to a benign result, not crash the ingest loop.
        check("injection body → structured-only, no action possible", False,
              f"reader raised (ingest would mark 'error' + continue): {str(exc)[:80]}")

    # 3. The reader is genuinely tool-free + local (structural, model-independent).
    from langchain_core.runnables import RunnableBinding
    tool_free = not isinstance(quarantine.reader_llm, RunnableBinding)
    local = settings.ollama_base_url in str(quarantine.reader_llm.openai_api_base)
    check("reader is tool-free + local endpoint", tool_free and local,
          f"tool_free={tool_free} local={local}")

    # 4. Optional: real Gmail body-read path (no writes; just proves it decodes).
    if google_auth.has_token():
        try:
            ids = google_gmail.search_message_ids("newer_than:30d", max_results=1)
            if ids:
                body = google_gmail.get_message_body(ids[0])
                check("real Gmail body-read round-trip", isinstance(body.get("body_text"), str),
                      f"read {len(body.get('body_text') or '')} chars from a real message")
            else:
                print("SKIP | real Gmail body-read — no recent messages matched")
        except Exception as exc:
            check("real Gmail body-read round-trip", False, str(exc)[:80])
    else:
        print("SKIP | real Gmail body-read — no Google token configured")

    print()
    failed = [r for r in results if not r[1]]
    print(f"{len(results) - len(failed)}/{len(results)} checks passed.")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
