"""The Channel seam.

Every messaging surface (Telegram now; iMessage/BlueBubbles later) implements this
interface, so swapping the channel never touches the agent. Keeping this boundary
thin is what makes the Phase 9 iMessage move a drop-in.
"""

from abc import ABC, abstractmethod
from typing import Any, Protocol


class Channel(ABC):
    @abstractmethod
    def run(self) -> None:
        """Start receiving messages and dispatching them to the agent (blocking)."""
        raise NotImplementedError


class ChannelContract(Protocol):
    """What the Telegram mixins may assume about the class they're mixed into.

    The mixins in `telegram_stream` / `telegram_commands` / `telegram_buttons` are not
    standalone classes — they call each other's methods through `self`. Without this, that
    coupling is invisible: you only find out a mixin depended on `_run_with_status` by
    running it. Declaring the contract makes the seam checkable and tells anyone splitting
    the class further exactly what has to keep existing.
    """

    agent: Any

    def _authorized(self, update: Any) -> bool: ...
    def _config(self, chat_id: int) -> dict: ...
    def _log_one(self, chat_id: int, role: str, text: str) -> None: ...
    async def _log_turn(self, chat_id: int, user_text: str | None, assistant_text: str | None) -> None: ...
    async def _send_rich(self, bot: Any, chat_id: int, text: str) -> Any: ...
    async def _report_error(self, chat_id: int, ctx: Any, error: Exception) -> None: ...
    async def _run_with_status(
        self, chat_id: int, ctx: Any, graph_input: Any, announce_thinking: bool
    ) -> tuple[dict | None, str | None, Exception | None]: ...
    async def _deliver(
        self, chat_id: int, ctx: Any, interrupt_payload: dict | None,
        reply: str | None, user_text: str | None = None,
    ) -> None: ...
