"""Voice notes: transcribe on-device, show what was heard, then the same turn as typed text.

What matters, and what each test pins:
  - a voice note becomes EXACTLY the turn a typed message would (same graph input, same
    swipe-reply-to-/ask routing, same fact sweep) — no second, divergent code path
  - the audio never outlives the handler: the temp file is deleted on success AND on failure
  - she sees the transcript, so a mis-hearing is visible before Mochi acts on it
  - long / silent / failed notes get a plain answer, never a crash or a silent drop

The decoder test runs real PyAV on a real OGG/Opus file (Telegram's voice format). The whisper
model itself is stubbed — it needs a one-time weight download; scripts/verify_voice.py drives
the real model on the Mac.
"""

import asyncio
import math
import os
from types import SimpleNamespace

import numpy as np
import pytest

from app.config import settings
from app.integrations import transcribe
from tests.support import FakeMessage, make_update


class FakeVoiceFile:
    def __init__(self, payload=b"OggS-fake"):
        self.payload = payload
        self.saved_to = None

    async def download_to_drive(self, path):
        self.saved_to = path
        with open(path, "wb") as fh:
            fh.write(self.payload)


def _voice_update(duration=5, reply_to=None):
    file = FakeVoiceFile()

    async def get_file():
        return file

    msg = FakeMessage(text=None, reply_to_message=reply_to)
    msg.voice = SimpleNamespace(duration=duration, get_file=get_file)
    return make_update(message=msg), file


@pytest.fixture
def turns(channel, monkeypatch):
    """Record what reaches the shared turn path instead of running the model."""
    seen = []

    async def handle_text(update, ctx, text):
        seen.append(text)

    monkeypatch.setattr(channel, "_handle_text", handle_text)
    return seen


def test_a_voice_note_becomes_a_turn_with_the_transcript(channel, ctx, turns, monkeypatch):
    seen_paths = []

    def fake_transcribe(path):
        seen_paths.append(path)
        assert os.path.exists(path)  # downloaded before transcription
        return "remind me to call mom tomorrow at 3"

    monkeypatch.setattr(transcribe, "transcribe", fake_transcribe)
    update, file = _voice_update()
    asyncio.run(channel._on_voice(update, ctx))

    assert turns == ["remind me to call mom tomorrow at 3"]
    assert update.message.replies == ["🎙️ “remind me to call mom tomorrow at 3”"]
    assert seen_paths == [file.saved_to]
    assert not os.path.exists(file.saved_to)  # audio never outlives the handler


def test_the_audio_is_deleted_even_when_transcription_fails(channel, ctx, turns, monkeypatch):
    errors = []

    async def report_error(chat_id, ctx_, exc):
        errors.append(exc)

    def boom(path):
        raise RuntimeError("model failed to load")

    monkeypatch.setattr(channel, "_report_error", report_error)
    monkeypatch.setattr(transcribe, "transcribe", boom)
    update, file = _voice_update()
    asyncio.run(channel._on_voice(update, ctx))

    assert turns == [] and len(errors) == 1
    assert not os.path.exists(file.saved_to)


def test_silence_gets_a_plain_answer_not_a_turn(channel, ctx, turns, monkeypatch):
    monkeypatch.setattr(transcribe, "transcribe", lambda path: "")
    update, _ = _voice_update()
    asyncio.run(channel._on_voice(update, ctx))
    assert turns == [] and "couldn't make out" in update.message.replies[0]


def test_an_overlong_note_is_declined_before_downloading(channel, ctx, turns, monkeypatch):
    monkeypatch.setattr(transcribe, "transcribe", lambda path: pytest.fail("should not transcribe"))
    update, file = _voice_update(duration=settings.voice_max_seconds + 1)
    asyncio.run(channel._on_voice(update, ctx))
    assert turns == [] and file.saved_to is None
    assert "split it up" in update.message.replies[0]


def test_voice_can_be_turned_off(channel, ctx, turns, monkeypatch):
    monkeypatch.setattr(settings, "voice_enabled", False)
    update, file = _voice_update()
    asyncio.run(channel._on_voice(update, ctx))
    assert turns == [] and file.saved_to is None and "turned off" in update.message.replies[0]


def test_other_chats_are_ignored(channel, ctx, turns, monkeypatch):
    monkeypatch.setattr(channel, "_authorized", lambda _u: False)
    update, file = _voice_update()
    asyncio.run(channel._on_voice(update, ctx))
    assert turns == [] and file.saved_to is None and update.message.replies == []


def test_typed_and_spoken_share_one_turn_path(channel, ctx, monkeypatch):
    """The graph receives the same input either way — a spoken note isn't a second-class turn."""
    inputs = []

    async def run_with_status(chat_id, ctx_, graph_input, announce_thinking):
        inputs.append(graph_input["messages"][0].content)
        return None, "ok", None

    async def deliver(*a, **k):
        pass

    monkeypatch.setattr(channel, "_run_with_status", run_with_status)
    monkeypatch.setattr(channel, "_deliver", deliver)
    monkeypatch.setattr(settings, "fact_sweep_enabled", False)
    monkeypatch.setattr(transcribe, "transcribe", lambda path: "what's on my calendar today")

    asyncio.run(channel._on_message(make_update(message=FakeMessage("what's on my calendar today")), ctx))
    update, _ = _voice_update()
    asyncio.run(channel._on_voice(update, ctx))
    assert inputs == ["what's on my calendar today"] * 2


def test_a_spoken_swipe_reply_to_an_ask_answer_continues_that_thread(channel, ctx, monkeypatch):
    followups = []

    async def on_followup(chat_id, ctx_, history, text):
        followups.append((history, text))

    channel._ask_threads = {7: ["prior history"]}
    monkeypatch.setattr(channel, "_on_ask_followup", on_followup)
    monkeypatch.setattr(transcribe, "transcribe", lambda path: "can you expand on that")
    update, _ = _voice_update(reply_to=SimpleNamespace(message_id=7))
    asyncio.run(channel._on_voice(update, ctx))
    assert followups == [(["prior history"], "can you expand on that")]


# --- the decoder, for real -----------------------------------------------------------


@pytest.fixture
def ogg_opus(tmp_path):
    """A real 2-second 440 Hz OGG/Opus file — Telegram's voice-note format."""
    import av

    path = str(tmp_path / "note.oga")
    sr = 48_000
    pcm = (0.3 * np.sin(2 * math.pi * 440 * np.arange(sr * 2) / sr)).astype(np.float32)
    with av.open(path, "w", format="ogg") as out:
        stream = out.add_stream("libopus", rate=sr)
        stream.layout = "mono"
        frame = av.AudioFrame.from_ndarray(pcm.reshape(1, -1), format="flt", layout="mono")
        frame.sample_rate = sr
        for packet in [*stream.encode(frame), *stream.encode(None)]:
            out.mux(packet)
    return path


def test_decode_reads_telegram_voice_format_to_16k_mono(ogg_opus):
    audio = transcribe.decode(ogg_opus)
    assert audio.dtype == np.float32 and audio.ndim == 1
    assert abs(audio.size / transcribe.SAMPLE_RATE - 2.0) < 0.05
    assert 0.2 < float(np.abs(audio).max()) < 0.4  # the 0.3-amplitude tone survived


def test_transcribe_skips_the_model_for_empty_audio(monkeypatch):
    monkeypatch.setattr(transcribe, "decode", lambda path: np.zeros(0, np.float32))
    monkeypatch.setattr(transcribe, "_get_model", lambda: pytest.fail("model should not load"))
    assert transcribe.transcribe("x") == ""


def test_transcribe_joins_segments_and_pins_language(monkeypatch):
    calls = {}

    class Model:
        def transcribe(self, audio, **kw):
            calls.update(kw)
            return iter([SimpleNamespace(text=" remind me "), SimpleNamespace(text="to stretch ")]), None

    monkeypatch.setattr(transcribe, "decode", lambda path: np.ones(16000, np.float32))
    monkeypatch.setattr(transcribe, "_get_model", Model)
    assert transcribe.transcribe("x") == "remind me to stretch"
    assert calls["language"] == settings.whisper_language and calls["vad_filter"] is True
