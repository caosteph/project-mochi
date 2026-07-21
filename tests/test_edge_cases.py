"""Edge-case hardening across the recent work (3B signals, 4A router/sanitizer,
4A.1 /ask UX, 4A.2 fact sweep). Offline + deterministic — fakes for model/bot, no network.
"""

import asyncio
from types import SimpleNamespace

from app.agent import router, sanitize
from app.agent.router import Sensitivity
from app.channels import telegram
from app.config import settings
from app.memory import extract
from app.proactive import email_signals


class _FakeExtractor:
    def __init__(self, facts):
        self._facts = facts

    def invoke(self, messages):
        return extract.ExtractedFacts(facts=self._facts)


class _Bot:
    def __init__(self):
        self.calls = []

    async def send_message(self, chat_id, text, parse_mode=None, **k):
        self.calls.append((text, parse_mode))
        return SimpleNamespace(message_id=len(self.calls))


def _chan():
    c = telegram.TelegramChannel.__new__(telegram.TelegramChannel)
    c._ask_threads = {}
    return c


# --- router: empty-string config must fail closed (not just None) --------------

def test_hosted_available_fails_closed_on_empty_config(monkeypatch):
    monkeypatch.setattr(settings, "hosted_enabled", True)
    monkeypatch.setattr(settings, "local_only", False)
    monkeypatch.setattr(settings, "hosted_model", "m")
    monkeypatch.setattr(settings, "hosted_api_key", "k")
    monkeypatch.setattr(settings, "hosted_base_url", "")  # empty string, not None
    assert router.hosted_available() is False
    # and a non-sensitive route still lands local
    m = router.chat_model(Sensitivity.NON_SENSITIVE)
    assert str(m.openai_api_base) == settings.ollama_base_url


# --- sanitizer edge cases ------------------------------------------------------

def test_redact_longest_term_first_no_partial_leak(monkeypatch):
    monkeypatch.setattr(settings, "redact_terms", "Ann, Anna")
    clean, hits = sanitize.redact("Anna went with Ann")
    # "Anna" must be redacted whole — not left as "a" by a premature "Ann" match
    assert clean == "[redacted] went with [redacted]" and hits == 2


def test_redact_counts_multiple_same_type_pii(monkeypatch):
    monkeypatch.setattr(settings, "redact_terms", "")
    clean, hits = sanitize.redact("emails a@b.com, c@d.com and e@f.com")
    assert hits == 3 and "@" not in clean


def test_redact_ignores_blank_terms(monkeypatch):
    monkeypatch.setattr(settings, "redact_terms", " , ,Bob, ")
    clean, hits = sanitize.redact("Bob is here")
    assert "Bob" not in clean and hits == 1  # blanks skipped, only Bob redacted


# --- fact extraction bounds ----------------------------------------------------

def test_extract_facts_caps_at_ten():
    out = extract.extract_facts("x", extractor=_FakeExtractor([f"fact {i}" for i in range(15)]))
    assert len(out) == 10


def test_extract_facts_truncates_long_fact():
    out = extract.extract_facts("x", extractor=_FakeExtractor(["a" * 500]))
    assert out == ["a" * 300]


# --- /ask UX edge cases --------------------------------------------------------

def test_send_rich_handles_none_text():
    bot = _Bot()
    asyncio.run(_chan()._send_rich(bot, 1, None))
    assert bot.calls and "…" in bot.calls[0][0]


def test_send_rich_chunks_over_length():
    bot = _Bot()
    long_text = "word " * 1200  # ~6000 chars → exceeds the 4096 limit
    last = asyncio.run(_chan()._send_rich(bot, 1, long_text))
    assert len(bot.calls) >= 2  # split into multiple messages
    assert last.message_id == len(bot.calls)  # returns the last sent message
    assert all(len(text) <= 4096 for text, _ in bot.calls)


def test_remember_ask_cap_keeps_recent_drops_oldest():
    chan = _chan()
    for i in range(60):
        chan._remember_ask(SimpleNamespace(message_id=i), [i])
    assert len(chan._ask_threads) == 50
    assert set(chan._ask_threads) == set(range(10, 60))  # oldest 10 evicted
    assert 0 not in chan._ask_threads


def test_remember_ask_ignores_none_message():
    chan = _chan()
    chan._remember_ask(None, ["x"])  # must not raise
    assert chan._ask_threads == {}


# --- email-signal edge cases ---------------------------------------------------

def test_resolve_due_date_unparseable_is_safe():
    # garbage extracted date must not crash: non-return → None; return → default window
    assert email_signals.resolve_due_date("bill", "not a date at all", None) is None
    due = email_signals.resolve_due_date("return", "still garbage", None)
    assert due is not None  # falls back to the default return window


def test_html_to_text_handles_malformed_and_nested():
    html = "<div><p>Hi <b>there</p><script>evil()</script> <span>more</div> tail"
    out = email_signals.google_gmail._html_to_text(html)
    assert "Hi" in out and "there" in out and "more" in out and "tail" in out
    assert "evil" not in out and "<" not in out


# --- calendar date resolution (A1: code-resolved, not model-computed) -----------

def test_calendar_resolve_when_uses_real_dates():
    from datetime import datetime

    from tzlocal import get_localzone

    from app.agent.tools.google_tools import resolve_when
    now = datetime(2026, 7, 15, 14, 0, tzinfo=get_localzone())  # Wed Jul 15

    def label(w):
        return resolve_when(w, now=now)[2]

    assert "Jul 15" in label("today")
    assert "Jul 16" in label("tomorrow")
    assert "Jul 17" in label("next Friday")  # the dateparser "next X" trap → must NOT be today
    s, e, _ = resolve_when("today", now=now)
    assert s[:10] == "2026-07-15" and e[:10] == "2026-07-15"  # a single-day window
