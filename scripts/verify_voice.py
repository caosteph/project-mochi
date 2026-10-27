"""Voice notes, for real: speech → Telegram's audio format → on-device whisper → the real agent.

The hermetic suite (tests/test_voice.py) proves the channel plumbing and the decoder, but stubs the
whisper model (it needs a one-time weight download). This drives the real thing on the Mac:

  1. macOS `say` speaks a phrase to AIFF (deterministic synthetic speech — no mic, no personal audio)
  2. re-encoded to OGG/Opus, the exact container + codec Telegram delivers voice notes in
  3. app.integrations.transcribe.transcribe — the production function, real faster-whisper model
  4. the transcript goes to the real agent (build_agent), which must fire add_reminder — proving a
     spoken reminder lands as a reminder, not just as text

The first run downloads the whisper weights (~150 MB for "base") from Hugging Face — model weights,
not data; audio never leaves the machine. Skips cleanly off macOS (no `say`).

Run (bot STOPPED, Ollama up):
    DATABASE_URL=postgresql://localhost/personal_agent_test PYTHONPATH=. uv run python scripts/verify_voice.py
"""

import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from scripts._verify_lib import (
    bootstrap_env,
    check,
    fires,
    require_scratch_db,
    sample_check,
    skip,
    summarize_and_exit,
)

require_scratch_db()
bootstrap_env()

from app.integrations import transcribe  # noqa: E402

PHRASES = {
    "remind me to call mom tomorrow at 3pm": ("call", "mom"),
    "remind me to take out the trash tonight": ("trash", "tonight"),
}


def to_ogg_opus(src: str, dst: str) -> None:
    """Re-encode to OGG/Opus 48 kHz mono — what Telegram hands the bot for a voice note."""
    import av

    with av.open(src) as inp, av.open(dst, "w", format="ogg") as out:
        stream = out.add_stream("libopus", rate=48_000)
        stream.layout = "mono"
        resampler = av.AudioResampler(format="s16", layout="mono", rate=48_000)
        for frame in inp.decode(audio=0):
            for f in resampler.resample(frame):
                for packet in stream.encode(f):
                    out.mux(packet)
        for f in resampler.resample(None):
            for packet in stream.encode(f):
                out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)


def main() -> None:
    if not shutil.which("say"):
        skip("voice round-trip", "needs macOS `say` to synthesize speech")
        summarize_and_exit()
        return

    from app.agent.graph import build_agent

    agent = build_agent()
    with tempfile.TemporaryDirectory() as tmp:
        for i, (phrase, words) in enumerate(PHRASES.items()):
            aiff, ogg = str(Path(tmp) / f"{i}.aiff"), str(Path(tmp) / f"{i}.oga")
            subprocess.run(["say", "-o", aiff, phrase], check=True)
            to_ogg_opus(aiff, ogg)

            started = time.monotonic()
            heard = transcribe.transcribe(ogg)
            secs = time.monotonic() - started
            lowered = heard.lower()
            check(f"transcribes {phrase!r}", all(w in lowered for w in words),
                  f"heard {heard!r} in {secs:.1f}s")
            if heard:
                sample_check(f"spoken note fires add_reminder ({phrase!r})",
                             lambda h=heard: (fires(agent, h, "add_reminder"), ""), samples=3, need=2)

    summarize_and_exit()


if __name__ == "__main__":
    main()
