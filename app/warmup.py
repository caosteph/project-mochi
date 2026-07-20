"""Keep the local model resident.

Ollama unloads a model after ~5 min idle, so the first message after a quiet
stretch pays a cold-reload penalty (a big chunk of the latency Stephanie feels).
A background daemon pings Ollama's *native* /api/generate with keep_alive=-1 —
the native endpoint honors keep_alive (the OpenAI-compatible one ignores it) — on
an interval shorter than the idle timeout, so the model stays warm. An empty
prompt makes it a pure load/keep-alive request, not a generation.
"""

import logging
import threading
import time

import httpx

from app.config import settings

log = logging.getLogger(__name__)


def _native_url() -> str:
    return settings.ollama_base_url.removesuffix("/v1") + "/api/generate"


def warm_now() -> None:
    """Load the model and pin it resident. Best-effort — never raises."""
    try:
        httpx.post(
            _native_url(),
            json={"model": settings.local_model, "keep_alive": -1, "stream": False},
            timeout=60.0,
        )
    except Exception as exc:  # a warmup failure must never take the app down
        log.warning("Keep-warm ping failed: %s", exc)


def _warm_tool_vectors() -> None:
    """Pre-compute the tool-selection embedding cache (measured ~1.1s cold vs ~55ms warm), so
    the first message after a restart doesn't pay it. Imported lazily to avoid an import cycle;
    best-effort — never raises."""
    try:
        from app.agent.tool_select import warm_tool_vectors
        from app.agent.tools import ALL_TOOLS

        warm_tool_vectors(ALL_TOOLS)
        log.info("Tool-vector cache warmed (%d tools).", len(ALL_TOOLS))
    except Exception as exc:
        log.warning("Tool-vector warm failed: %s", exc)


def start_keep_warm() -> None:
    """Warm the model once now, then keep it warm on a daemon thread. Also warms the
    tool-selection embedding cache once, off the startup path."""
    if settings.keep_warm_interval_seconds <= 0:
        return

    def loop() -> None:
        first = True
        while True:
            warm_now()
            if first:  # after the model is resident, so the embed call isn't queued behind a load
                _warm_tool_vectors()
                first = False
            time.sleep(settings.keep_warm_interval_seconds)

    threading.Thread(target=loop, daemon=True).start()
    log.info("Keep-warm started (every %ss).", settings.keep_warm_interval_seconds)
