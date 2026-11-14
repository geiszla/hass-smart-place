"""Direction of a moving blind, derived from its reported positions and from the caller's own commands.

The server reports a blind's position about once per second while it
moves, with gaps of up to four seconds, and says nothing when it stops. :class:`BlindMotion` turns that
stream into "opening" / "closing" / not moving: the direction is the sign
of the change between two distinct successive positions, and it ends at
the end the blind moves to, on a value without a position (weather alarm,
unknown) and after ``timeout`` seconds without a new position.

The caller's own commands count as well (:meth:`BlindMotion.command_sent`),
because the vendor system takes the command for the direction a blind
already moves in as "stop" (seen by the user, 2026-11-12). A caller that
asks :attr:`BlindMotion.direction` first can therefore

- leave out an up or down command for a blind that already moves that
  way, also in the second before its first position arrives;
- stop a moving blind, by sending the command for its direction.

Positions are Home Assistant cover positions (0 = closed, 100 = open).
Pure logic with an injectable clock; the Home Assistant entity schedules a
callback at the timeout to write its state, because no frame arrives then.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import time
from typing import Final, Literal

from .alarm import command_moves_blind
from .commands import DEVICE_BLIND, CommandDefinition, Commands

# Longer than the longest gap between two position frames of a moving
# blind (4.0 s in the 2026-11-09 capture): a shorter timeout reports "at
# rest" in the middle of a run.
BLIND_MOTION_TIMEOUT: Final = 6.0

# A position that arrives this soon after a command may have been on its
# way before the command took effect.
BLIND_COMMAND_SETTLE: Final = 1.0

_FULLY_CLOSED: Final = 0
_FULLY_OPEN: Final = 100

Direction = Literal["opening", "closing"]

_END: Final[dict[Direction, int]] = {"opening": _FULLY_OPEN, "closing": _FULLY_CLOSED}
# Vent and a sub-menu's own group message are missing: where they move a blind is not known.
_COMMAND_DIRECTIONS: Final[dict[CommandDefinition, Direction]] = {
    Commands.BlindUp: "opening",
    Commands.BlindsGroupUp: "opening",
    Commands.BlindDown: "closing",
    Commands.BlindsGroupDown: "closing",
}


@dataclass(slots=True)
class BlindMotion:
    """Track whether a blind is opening, closing or at rest.

    What a command of the caller does (:meth:`command_sent`):

    - The blind's own up or down command for the direction it already
      moves in stops it: at rest.
    - Any other up or down command, also a blinds group button, starts
      that direction for ``timeout`` seconds; positions then keep it
      going. The opposite command is taken to turn the blind round, and a
      group button is taken not to stop a blind (neither was tried).
      Nothing starts at the end the command moves to, or without a
      position. The reported end can be wrong (2026-11-13): the blind
      then moves unseen, and a second command stops it.
    - Vent and a sub-menu's own group message: at rest until positions
      say otherwise.
    - For ``settle`` seconds after a command, a position that disagrees
      with the above changes nothing: it may have been on its way.
    """

    timeout: float = BLIND_MOTION_TIMEOUT
    settle: float = BLIND_COMMAND_SETTLE
    clock: Callable[[], float] = time.monotonic
    _position: int | None = None
    _direction: Direction | None = None
    _deadline: float = 0.0
    _settled_at: float = 0.0

    def update(self, position: int | None) -> bool:
        """Feed the latest position (``None`` when the server reports none).

        Returns ``True`` when this position (re)started the timeout, i.e.
        the caller should look at :attr:`direction` again ``timeout``
        seconds from now.
        """
        previous, self._position = self._position, position
        if position is None:
            self._direction = None
            return False
        if previous is None or position == previous:
            return False
        moved: Direction = "opening" if position > previous else "closing"
        if moved != self.direction and self.clock() < self._settled_at:
            return False
        if position == _END[moved]:
            self._direction = None
            return False
        self._direction = moved
        self._deadline = self.clock() + self.timeout
        return True

    def command_sent(
        self, command: CommandDefinition, args: Sequence[object], blind_id: int, position: int | None
    ) -> bool:
        """Note a command the caller is sending, with the blind's latest position.

        The position comes along because a command can be sent while the
        caller's entities are told about a frame, before this tracker was
        fed the frame's value: at the end of a weather alarm the old
        value has no position, and nothing would start.

        Returns ``True`` when the command is for blind ``blind_id`` (its
        own up, down or vent, or a blinds group button): the caller should
        look at :attr:`direction` again now and ``timeout`` seconds from
        now.
        """
        if not command_moves_blind(command, args, blind_id):
            return False
        self.update(position)
        now = self.clock()
        towards = _COMMAND_DIRECTIONS.get(command)
        if (
            towards is None
            or self._position in (None, _END[towards])
            or (command.device == DEVICE_BLIND and self.direction == towards)
        ):
            self._direction = None
        else:
            self._direction = towards
            self._deadline = now + self.timeout
        self._settled_at = now + self.settle
        return True

    @property
    def direction(self) -> Direction | None:
        """Return the current direction, ``None`` when at rest."""
        if self._direction is not None and self.clock() >= self._deadline:
            self._direction = None
        return self._direction
