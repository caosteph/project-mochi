"""scripts/compare_models.py — the model A/B harness.

The verdict is the part that decides whether Mochi's model gets swapped, so it's pinned here
case by case. The Ollama plumbing (pull/create, the speed probe's field names, unloading) runs
against a fake Ollama HTTP server and a fake `ollama` CLI on PATH; the two wrapped scripts
(scorecard / verify_scenarios) are canned, since they need the real model.
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest

from scripts import compare_models as cm


def _row(model, *, rates=None, firing=None, scenarios=True, gb=4.9, error=None):
    if error:
        return {"base": model, "error": error}
    metrics = {"tool_firing_pct": firing or {"add_reminder": 80.0, "create_draft": 60.0},
               "no_json_dump_pct": 100.0, "honors_no_reminders_pct": 100.0,
               "honors_across_summary_pct": 0.0, "answers_not_narrates_pct": 50.0}
    metrics.update(rates or {})
    return {"base": model, "model": model, "speed": {"resident_gb": gb},
            "scorecard": {"header": {"local_model": model}, "metrics": metrics},
            "scenarios_passed": scenarios, "scenarios": "16/16 checks passed."}


BASE = _row("qwen2.5:7b-8k")
KW = {"tolerance": 10.0, "max_resident_gb": 7.0}


def test_derived_names_and_modelfile():
    assert cm.derived_name("qwen3:8b") == "qwen3:8b-8k"
    assert cm.derived_name("llama3.1") == "llama3.1:8k"
    assert cm.modelfile("qwen3:8b") == "FROM qwen3:8b\nPARAMETER num_ctx 8192\n"


def test_verdict_adopt_needs_a_real_gain():
    cand = _row("c", rates={"honors_across_summary_pct": 67.0})
    v = cm.verdict(BASE, cand, **KW)
    assert v.startswith("ADOPT?") and "honors_across_summary_pct 0→67" in v


def test_verdict_tie_when_within_noise():
    cand = _row("c", rates={"answers_not_narrates_pct": 55.0})
    assert cm.verdict(BASE, cand, **KW).startswith("TIE")


def test_verdict_any_regression_vetoes_a_gain():
    cand = _row("c", rates={"honors_across_summary_pct": 100.0},
                firing={"add_reminder": 40.0, "create_draft": 60.0})
    v = cm.verdict(BASE, cand, **KW)
    assert v.startswith("NO — regressed") and "fires add_reminder 80→40" in v


def test_verdict_failed_gate_or_memory_or_error_is_no():
    assert "verify_scenarios" in cm.verdict(BASE, _row("c", scenarios=False), **KW)
    assert "GB resident" in cm.verdict(BASE, _row("c", gb=9.5), **KW)
    assert "run failed" in cm.verdict(BASE, _row("c", error="boom"), **KW)


def test_verdict_ignores_probes_skipped_in_quick_mode():
    base = _row("b", rates={"honors_across_summary_pct": None})
    cand = _row("c", rates={"honors_across_summary_pct": None, "answers_not_narrates_pct": 90.0})
    assert cm.verdict(base, cand, **KW).startswith("ADOPT?")


# --- plumbing against a fake Ollama ------------------------------------------------


class _FakeOllama(BaseHTTPRequestHandler):
    calls: list  # set per test by the fake_ollama fixture

    def log_message(self, *a):
        pass

    def _send(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/tags":
            self._send({"models": [{"name": "qwen3:8b"}]})
        elif self.path == "/api/ps":
            self._send({"models": [{"name": "qwen3:8b-8k", "model": "qwen3:8b-8k", "size": 5_600_000_000}]})

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).calls.append(req)
        self._send({"prompt_eval_count": 4000, "prompt_eval_duration": 20 * 10**9,
                    "eval_count": 128, "eval_duration": 8 * 10**9, "total_duration": 29 * 10**9})


@pytest.fixture
def fake_ollama(monkeypatch, tmp_path):
    _FakeOllama.calls = []
    server = HTTPServer(("127.0.0.1", 0), _FakeOllama)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    from app.config import settings

    monkeypatch.setattr(settings, "ollama_base_url", f"http://127.0.0.1:{server.server_port}/v1")
    log = tmp_path / "ollama.log"
    cli = tmp_path / "ollama"
    cli.write_text(f'#!/bin/sh\necho "$@" >> {log}\n[ "$1" = create ] && cat "$4" >> {log}\nexit 0\n')
    cli.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    yield log
    server.shutdown()


def test_evaluate_end_to_end_against_fake_ollama(fake_ollama, monkeypatch):
    def fake_script(name, model, *args):
        if name == "scorecard.py":
            out = "progress line\n" + json.dumps(_row(model)["scorecard"])
            return SimpleNamespace(returncode=0, stdout=out, stderr="")
        return SimpleNamespace(returncode=0, stdout="16/16 checks passed.\n", stderr="")

    monkeypatch.setattr(cm, "_script", fake_script)
    row = cm.evaluate("qwen3:8b", n=1, quick=True)

    assert "error" not in row, row.get("error")
    assert row["model"] == "qwen3:8b-8k"
    assert row["speed"] == {"prompt_tokens": 4000, "prompt_tok_s": 200.0, "decode_tok_s": 16.0,
                            "turn_s": 29.0, "resident_gb": 5.6}
    assert row["scenarios_passed"] and row["scenarios"] == "16/16 checks passed."
    cli_log = fake_ollama.read_text()
    assert "pull" not in cli_log  # already installed → no download
    assert "create qwen3:8b-8k" in cli_log and "PARAMETER num_ctx 8192" in cli_log
    assert _FakeOllama.calls[-1] == {"model": "qwen3:8b-8k", "keep_alive": 0}  # unloaded after
    timed = [c["prompt"] for c in _FakeOllama.calls if "prompt" in c]
    assert len(timed) == 2 and timed[0] != timed[1]  # regression: identical prompts hit the prefix cache
    assert timed[0].split("\n", 1)[1] == timed[1].split("\n", 1)[1] == cm._SPEED_PROMPT


def test_evaluate_pulls_a_missing_model_and_records_failures(fake_ollama, monkeypatch):
    monkeypatch.setattr(cm, "_script", lambda *a: SimpleNamespace(returncode=1, stdout="", stderr="no model"))
    row = cm.evaluate("llama3.1:8b", n=1, quick=True)
    assert "pull llama3.1:8b" in fake_ollama.read_text()
    assert "scorecard failed" in row["error"]


def test_scorecard_running_the_wrong_model_is_an_error(monkeypatch):
    out = json.dumps(_row("qwen2.5:7b")["scorecard"])
    monkeypatch.setattr(cm, "_script", lambda *a: SimpleNamespace(returncode=0, stdout=out, stderr=""))
    with pytest.raises(RuntimeError, match="not 'qwen3:8b-8k'"):
        cm.run_scorecard("qwen3:8b-8k", 1, True)


def test_report_prints_a_verdict_per_candidate(capsys):
    rows = [BASE, _row("c1", rates={"honors_across_summary_pct": 67.0}), _row("c2", error="pull failed")]
    for r in rows[:2]:
        r["speed"] = {"prompt_tok_s": 200.0, "decode_tok_s": 16.0, "turn_s": 29.0, "resident_gb": 4.9}
    cm.print_report(rows, **KW)
    out = capsys.readouterr().out
    assert "c1: ADOPT?" in out and "c2: NO — run failed" in out
