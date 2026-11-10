"""A blind held by a weather alarm.

While a rain, wind or hail alarm holds a blind, its value frame carries a
word in place of the position (``BlindValue.alarm``). The vendor system
then drives the blind up and refuses to lower it: the blind is blocked.

:class:`BlindAlarm` is what a caller shows of that. It keeps the server's
last report through the gap after a reconnect, in which no value is known
yet. Pure logic, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .devices import BlindValue


@dataclass(slots=True)
class BlindAlarm:
    """Whether a weather alarm blocks a blind, as the server last reported it.

    The server replays every value after a reconnect; until it does, the
    blind's value is unknown. The last report is kept through that gap,
    so the gap reads neither as the end of an alarm nor, once the replay
    is in, as a new one: a consumer only sees a change the server
    reported. Before the first report ever, the blind counts as free.
    """

    _alarm: str | None = None

    def update(self, value: BlindValue | None) -> None:
        """Feed the blind's current value; ``None`` (not reported in this connection) keeps the last report."""
        if value is not None:
            self._alarm = value.alarm

    @property
    def alarm(self) -> str | None:
        """Return ``rain`` / ``wind`` / ``hail`` or the vendor's own word while blocked, else ``None``."""
        return self._alarm

    @property
    def blocked(self) -> bool:
        """Return whether an alarm holds the blind."""
        return self._alarm is not None
