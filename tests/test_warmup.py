"""app/warmup.py — keeping the local model resident and pre-warming the tool-vector cache.

This module was 0% covered while carrying real logic that Stephanie feels directly: without the
keep-warm ping the first message after an idle stretch pays a model reload, and without the
tool-vector warming her first message after a restart paid ~1.1s of embedding. Both paths are
best-effort by design (a warmup failure must never take the app down), so the tests pin that too.
"""

import app.warmup as warmup
from app.agent import tool_select
from app.agent.tools import ALL_TOOLS


def test_warm_now_pings_native_endpoint_with_keep_alive(monkeypatch):
    """Must hit Ollama's NATIVE /api/generate — the OpenAI-compatible endpoint ignores
    keep_alive, which is the whole point of the ping."""
    sent = {}

    def fake_post(url, json=None, timeout=None):
        sent["url"], sent["json"] = url, json

    monkeypatch.setattr(warmup.httpx, "post", fake_post)
    warmup.warm_now()

    assert sent["url"].endswith("/api/generate") and "/v1" not in sent["url"]
    assert sent["json"]["keep_alive"] == -1  # pin resident, not just load
    assert sent["json"]["model"] == warmup.settings.local_model


def test_warm_now_swallows_failures(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("ollama down")

    monkeypatch.setattr(warmup.httpx, "post", boom)
    warmup.warm_now()  # must not raise — a warmup failure can't take the bot down


def test_warm_tool_vectors_populates_the_cache(monkeypatch):
    monkeypatch.setattr(tool_select, "embed_local", lambda _text: [0.1, 0.2, 0.3])
    monkeypatch.setattr(tool_select, "_tool_vecs", {})

    warmup._warm_tool_vectors()

    # Every tool pre-embedded, so the first real turn doesn't pay the ~1.1s cold cost.
    assert set(tool_select._tool_vecs) == {t.name for t in ALL_TOOLS}


def test_warm_tool_vectors_never_raises_when_embedding_is_down(monkeypatch):
    def boom(_text):
        raise RuntimeError("embeddings down")

    monkeypatch.setattr(tool_select, "embed_local", boom)
    monkeypatch.setattr(tool_select, "_tool_vecs", {})

    warmup._warm_tool_vectors()  # best-effort: degrades to a cold first turn, never a crash


def test_start_keep_warm_is_a_noop_when_disabled(monkeypatch):
    """interval <= 0 disables warming entirely — no thread, no ping."""
    started = []
    monkeypatch.setattr("app.config.settings.keep_warm_interval_seconds", 0)
    monkeypatch.setattr(warmup.threading, "Thread", lambda *a, **k: started.append(1))

    warmup.start_keep_warm()

    assert started == []
