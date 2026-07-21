"""The wiring guard for the split channel.

`TelegramChannel` is composed from three mixins (`telegram_stream`, `telegram_commands`,
`telegram_buttons`). The failure mode that split introduces is silent: a handler that no
longer gets registered, or a mixin method that quietly stops existing, shows up only as
Mochi ignoring a command in real use — no test, no exception, no log line.

So this file asserts the seams themselves: every command is registered to a real callable,
the background jobs are scheduled, and every member of `ChannelContract` is present on the
composed class. None of it needs a bot, a database, or a model.
"""

from types import SimpleNamespace

import pytest

from app.channels import telegram
from app.channels.base import ChannelContract


class FakeJobQueue:
    def __init__(self):
        self.repeating = []
        self.daily = []

    def run_repeating(self, cb, interval, first):
        self.repeating.append((cb.__name__, interval, first))

    def run_daily(self, cb, time):
        self.daily.append((cb.__name__, time))


class FakeApp:
    def __init__(self):
        self.handlers = []
        self.job_queue = FakeJobQueue()
        self.polled = False

    def add_handler(self, handler):
        self.handlers.append(handler)

    def run_polling(self):
        self.polled = True


class FakeBuilder:
    def __init__(self, app):
        self._app = app

    def token(self, _token):
        return self

    def build(self):
        return self._app


@pytest.fixture
def started(monkeypatch):
    """Run the real `run()` against a fake python-telegram-bot Application."""
    app = FakeApp()
    monkeypatch.setattr(
        telegram, "Application", SimpleNamespace(builder=lambda: FakeBuilder(app))
    )
    chan = telegram.TelegramChannel.__new__(telegram.TelegramChannel)  # skip build_agent()
    chan.run()
    return chan, app


def test_every_declared_command_resolves_to_a_real_handler():
    """COMMANDS is data, so it can name a method that doesn't exist — which `run()` would
    only discover at startup, after launchd had already reported success."""
    chan = telegram.TelegramChannel.__new__(telegram.TelegramChannel)
    for command, handler_name in telegram.COMMANDS:
        handler = getattr(chan, handler_name, None)
        assert callable(handler), f"/{command} -> {handler_name} is missing or not callable"


def test_run_registers_every_command(started):
    _chan, app = started
    registered = {
        c for h in app.handlers for c in getattr(h, "commands", None) or ()
    }
    assert registered == {command for command, _ in telegram.COMMANDS}


def test_run_registers_the_callback_and_message_handlers(started):
    _chan, app = started
    kinds = {type(h).__name__ for h in app.handlers}
    assert "CallbackQueryHandler" in kinds, "inline buttons (approve/reject) would be dead"
    assert "MessageHandler" in kinds, "plain chat messages would be ignored"


def test_run_schedules_the_background_jobs(started):
    """The proactive half of the agent lives entirely in these three jobs."""
    _chan, app = started
    assert {name for name, _, _ in app.job_queue.repeating} == {
        "reminder_tick_job",
        "signal_ingest_job",
    }
    assert [name for name, _ in app.job_queue.daily] == ["daily_briefing_job"]


def test_composed_instance_satisfies_the_mixin_contract(monkeypatch):
    """Each mixin calls the others through `self`; ChannelContract is where that coupling
    is declared. If a member is renamed in one module and not the contract, catch it here
    rather than at runtime.

    Checked on a real instance, not the class: `agent` is assigned in `__init__`, so a
    class-level hasattr would miss it — and `agent` is the one contract member the mixins
    can't function without.
    """
    monkeypatch.setattr(telegram, "build_agent", lambda: object())
    chan = telegram.TelegramChannel()
    missing = [name for name in ChannelContract.__protocol_attrs__ if not hasattr(chan, name)]
    assert not missing, f"TelegramChannel is missing contract members: {missing}"


def test_the_split_modules_each_contribute_their_handlers():
    """Guards against a mixin being dropped from the bases (which Python accepts silently
    until the missing method is called)."""
    from app.channels.telegram_buttons import ButtonsMixin
    from app.channels.telegram_commands import CommandsMixin
    from app.channels.telegram_stream import StreamingMixin

    bases = telegram.TelegramChannel.__mro__
    assert StreamingMixin in bases and CommandsMixin in bases and ButtonsMixin in bases
