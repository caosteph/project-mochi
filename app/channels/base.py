"""The Channel seam.

Every messaging surface (Telegram now; iMessage/BlueBubbles later) implements this
interface, so swapping the channel never touches the agent. Keeping this boundary
thin is what makes the Phase 9 iMessage move a drop-in.
"""

from abc import ABC, abstractmethod


class Channel(ABC):
    @abstractmethod
    def run(self) -> None:
        """Start receiving messages and dispatching them to the agent (blocking)."""
        raise NotImplementedError
