"""Shared test doubles for the Telegram channel and the routed model.

Before this, a fake bot was reimplemented 8× (`Bot` + `RecordingBot`), a fake callback `Query`
2×, and a `FakeModel` 2× — each recording what it received slightly differently (a bare string, a
tuple, a dict). One of each lives here, recording *rich* objects so every prior assertion style is
expressible through an accessor (`.texts`, `.buttons`, `.last`).

Nothing here does I/O; these stand in for python-telegram-bot objects and a LangChain model.
"""

from types import SimpleNamespace


class SentMessage:
    """One message the bot was asked to send — with everything a test might assert on."""

    def __init__(self, chat_id, text, reply_markup=None, parse_mode=None, message_id=1, **extra):
        self.chat_id = chat_id
        self.text = text
        self.reply_markup = reply_markup
        self.parse_mode = parse_mode
        self.message_id = message_id
        self.extra = extra

    @property
    def buttons(self):
        """Flat list of InlineKeyboardButtons across every row (empty if there's no keyboard)."""
        if not self.reply_markup:
            return []
        return [b for row in self.reply_markup.inline_keyboard for b in row]

    @property
    def button_labels(self):
        return [b.text for b in self.buttons]

    @property
    def callback_data(self):
        return [b.callback_data for b in self.buttons]


class FakeBot:
    """Records every send/edit/document. Replaces the 4 `Bot` + 4 `RecordingBot` copies.

    `send_message` returns a `SentMessage` with a unique `message_id`, because the channel reads
    `.message_id` off the result (to anchor an /ask reply thread)."""

    def __init__(self, *, fail_markdown=False):
        # fail_markdown=True makes any MarkdownV2 send/edit raise, so a test can exercise the
        # channel's plain-text fallback (the "BadRequest: can't parse entities" path).
        self.fail_markdown = fail_markdown
        self.messages: list[SentMessage] = []  # sent, in order
        self.edits: list[SentMessage] = []
        self.documents: list[tuple] = []  # (chat_id, filename)
        self._next_id = 1

    def _maybe_fail(self, parse_mode):
        if self.fail_markdown and parse_mode == "MarkdownV2":
            raise Exception("Bad Request: can't parse entities")

    async def send_message(self, chat_id=None, text=None, reply_markup=None, parse_mode=None, **kw):
        self._maybe_fail(parse_mode)
        msg = SentMessage(chat_id, text, reply_markup, parse_mode, self._next_id, **kw)
        self._next_id += 1
        self.messages.append(msg)
        return msg

    async def edit_message_text(self, text=None, chat_id=None, message_id=None,
                                reply_markup=None, parse_mode=None, **kw):
        self._maybe_fail(parse_mode)
        self.edits.append(SentMessage(chat_id, text, reply_markup, parse_mode, message_id, **kw))

    async def edit_message_reply_markup(self, chat_id=None, message_id=None, reply_markup=None, **kw):
        pass

    async def send_chat_action(self, *a, **k):
        pass

    async def send_document(self, chat_id=None, document=None, filename=None, **kw):
        self.documents.append((chat_id, filename))

    # --- accessors covering the three old recording styles ---
    @property
    def texts(self) -> list[str]:
        return [m.text for m in self.messages]

    @property
    def last(self) -> SentMessage | None:
        return self.messages[-1] if self.messages else None


class FakeQuery:
    """A tapped inline button (`telegram.CallbackQuery`). Captures the toast, the message edit, and
    whether the keyboard was cleared — the things the callback handlers do on a tap."""

    def __init__(self, data, *, message=None):
        self.data = data
        self.message = message
        self.answered = False
        self.toast = None
        self.markup_cleared = False
        self.edited = None

    async def answer(self, text=None, **kw):
        self.answered = True
        self.toast = text

    async def edit_message_text(self, text, **kw):
        self.edited = text

    async def edit_message_reply_markup(self, reply_markup=None):
        self.markup_cleared = reply_markup is None


class FakeMessage:
    """An incoming message with a `reply_text` that records replies (for command-handler tests)."""

    def __init__(self, text="", reply_to_message=None):
        self.text = text
        self.reply_to_message = reply_to_message
        self.replies: list[str] = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)


class FakeModel:
    """A LangChain-style chat model: records the messages it was invoked with, returns a set answer."""

    def __init__(self, answer="a generic answer"):
        self.answer = answer
        self.received = None

    def invoke(self, messages):
        self.received = messages
        return SimpleNamespace(content=self.answer)


def make_update(*, message=None, callback_query=None, chat_id=1):
    """A minimal `telegram.Update` — enough for `_authorized` + a handler to run."""
    return SimpleNamespace(
        message=message,
        callback_query=callback_query,
        effective_chat=SimpleNamespace(id=chat_id),
    )


def inline_markup(labels_and_data):
    """Build an InlineKeyboardMarkup from [(label, callback_data), …] — for FakeQuery.message."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup([[InlineKeyboardButton(lbl, callback_data=cd)]
                                 for lbl, cd in labels_and_data])
