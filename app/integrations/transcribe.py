"""Local speech-to-text for voice notes — audio never leaves the machine.

Voice is personal data (rule 1), so this runs `faster-whisper` on-device: no hosted STT, no
audio persisted (the channel deletes the temp file in a `finally`). The model is loaded lazily
on the first voice note and cached for the life of the process (base/int8 is ~150 MB resident).
Its weights are downloaded once from Hugging Face on that first use — model weights, not data.

Decoding is done here with PyAV rather than `faster_whisper.decode_audio`: faster-whisper 1.2.1
calls `av.open(..., metadata_errors=...)`, which PyAV >= 17 no longer accepts (verified — it
raises TypeError), and its `av` requirement has no upper bound, so a fresh install breaks. Our
own decoder hands the model a ready 16 kHz mono array and doesn't care which PyAV is installed.
"""

import contextlib
import logging
import os
import tempfile
import threading
from pathlib import Path

import numpy as np

from app.config import settings

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000  # what Whisper expects

# Voice notes are written here (not the system temp dir) only for the decoder, and deleted in the
# handler's `finally`. A `finally` doesn't run on `os._exit` (the watchdog / health restart), so the
# directory is also emptied on every startup — a note interrupted mid-transcription can't linger.
# Under data/: git-ignored, and on the FileVault-encrypted disk.
SCRATCH_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "voice_tmp"

_model = None
_model_lock = threading.Lock()


def decode(path: str) -> np.ndarray:
    """Any container/codec PyAV can read (Telegram voice is OGG/Opus) → 16 kHz mono float32."""
    import av

    resampler = av.AudioResampler(format="flt", layout="mono", rate=SAMPLE_RATE)
    chunks: list[np.ndarray] = []
    with av.open(path) as container:
        for frame in container.decode(audio=0):
            chunks.extend(f.to_ndarray().reshape(-1) for f in resampler.resample(frame))
        chunks.extend(f.to_ndarray().reshape(-1) for f in resampler.resample(None))  # flush
    return np.concatenate(chunks).astype(np.float32) if chunks else np.zeros(0, np.float32)


def scratch_file() -> str:
    """A fresh, private (0600) file in SCRATCH_DIR for one downloaded note."""
    SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    fd, path = tempfile.mkstemp(suffix=".oga", dir=SCRATCH_DIR)
    os.close(fd)
    return path


def purge_scratch() -> int:
    """Delete any leftover notes (from a process that died mid-transcription). Returns how many."""
    removed = 0
    for leftover in SCRATCH_DIR.glob("*") if SCRATCH_DIR.exists() else ():
        with contextlib.suppress(OSError):
            leftover.unlink()
            removed += 1
    return removed


def _get_model():
    global _model
    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel

            log.info("Loading whisper model %r (first voice note)…", settings.whisper_model)
            _model = WhisperModel(settings.whisper_model, device="cpu", compute_type="int8")
        return _model


def transcribe(path: str) -> str:
    """The spoken text, stripped ('' for silence). Blocking — call it off the event loop."""
    audio = decode(path)
    if audio.size == 0:
        return ""
    # vad_filter drops silence (a pocket-dial note shouldn't become a hallucinated sentence).
    segments, _info = _get_model().transcribe(
        audio, language=settings.whisper_language or None, vad_filter=True, beam_size=5
    )
    return " ".join(s.text.strip() for s in segments).strip()
