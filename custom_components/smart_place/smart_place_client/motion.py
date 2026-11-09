"""Direction of a moving blind, derived from its reported positions.

The server reports a blind's position about once per second while it
moves and says nothing when it stops. :class:`BlindMotion` turns that
stream into "opening" / "closing" / not moving: the direction is the sign
of the change between two distinct successive positions, and it ends at
either end stop, on a value without a position (weather alarm, unknown)
and after ``timeout`` seconds without a new position.

Positions are Home Assistant cover positions (0 = closed, 100 = open).
Pure logic with an injectable clock; the Home Assistant entity schedules a
callback at the timeout to write its state, because no frame arrives then.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import time
from typing import Final, Literal

BLIND_MOTION_TIMEOUT: Final = 3.0

_FULLY_CLOSED: Final = 0
_FULLY_OPEN: Final = 100

Direction = Literal["opening", "closing"]


@dataclass(slots=True)
class BlindMotion:
    """Track whether a blind is opening, closing or at rest."""

    timeout: float = BLIND_MOTION_TIMEOUT
    clock: Callable[[], float] = time.monotonic
    _position: int | None = None
    _direction: Direction | None = None
    _deadline: float = 0.0

    def update(self, position: int | None) -> None:
        """Feed the latest position (``None`` when the server reports none)."""
        previous, self._position = self._position, position
        if position is None or position in (_FULLY_CLOSED, _FULLY_OPEN):
            self._direction = None
        elif previous is not None and position != previous:
            self._direction = "opening" if position > previous else "closing"
            self._deadline = self.clock() + self.timeout

    @property
    def direction(self) -> Direction | None:
        """Return the current direction, ``None`` when at rest."""
        if self._direction is not None and self.clock() >= self._deadline:
            self._direction = None
        return self._direction
