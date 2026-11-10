"""A blind held by a weather alarm.

While a rain, wind or hail alarm holds a blind, its value frame carries a
word in place of the position (``BlindValue.alarm``). The vendor system
then drives the blind up and refuses to lower it: the blind is blocked.

:class:`BlindAlarm` is what a caller shows of that. It keeps the server's
last report through the gap after a reconnect, in which no value is known
yet.

:class:`LowerAfterAlarm` decides when to lower a blind because its alarm
ended. That is a command nobody asked for at that moment, so its rule is
strict where :class:`BlindAlarm` is lenient: it only counts an alarm end
that was seen in the current connection, and it forgets everything when
the connection drops.

Pure logic with an injectable clock, no I/O.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import time
from typing import TYPE_CHECKING, Final

from .commands import DEVICE_BLIND, CommandDefinition, Commands

if TYPE_CHECKING:
    from .devices import BlindValue

# Vendor position of a blind that is fully down.
_FULLY_DOWN: Final = 100

# Which blinds a group button moves is decided inside the vendor system.
_BLINDS_GROUP_COMMANDS: Final = (Commands.BlindsGroupUp, Commands.BlindsGroupDown, Commands.BlindsGroupMessage)


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


def blind_can_be_lowered(value: BlindValue | None) -> bool:
    """Return whether a down command has a point: the position is known (so no alarm holds the blind) and not fully down."""
    return value is not None and value.position is not None and value.position != _FULLY_DOWN


def command_moves_blind(command: CommandDefinition, args: Sequence[object], blind_id: int) -> bool:
    """Return whether ``command`` with ``args`` is for blind ``blind_id``: its own up, down or vent, or a blinds group button."""
    if command.device == DEVICE_BLIND:
        return args[0] == blind_id
    return command in _BLINDS_GROUP_COMMANDS


@dataclass(slots=True)
class LowerAfterAlarm:
    """Decide when to lower one blind because its weather alarm ended.

    Feed every value of the blind through :meth:`update`. The rule:

    - A value with an alarm sets the note "alarm seen". A missing value
      (``None``: the connection dropped and the values were cleared)
      drops it.
    - A position that arrives while the note is set is the end of the
      alarm, and starts the wait. The server replays every value after a
      connect; the note is gone by then, so a replay never counts: an
      alarm end that was not seen live lowers nothing.
    - The wait (it may be zero) is cancelled when the alarm is back, the
      value goes missing, the function is switched off, the caller sends
      a command for the blind (:meth:`cancel`), or the blind reports
      another position than at the end of the alarm (somebody moves it).
    - When the wait is over, :meth:`due` says once whether to lower: yes,
      unless the alarm is back or the value is missing. It never says so
      twice for one alarm: a command that could not be sent is not
      repeated.

    The caller schedules a callback for the end of the wait, because no
    frame arrives then.
    """

    clock: Callable[[], float] = time.monotonic
    _alarm_seen: bool = False
    _deadline: float | None = None
    # Position at the end of the alarm; the wait only runs while it stays.
    _position: int | None = None

    def update(self, value: BlindValue | None, *, enabled: bool, delay: float) -> bool:
        """Feed the blind's current value and whether the function is switched on.

        Repeats of a value change nothing. Returns ``True`` when this
        value started the wait: the caller should ask :meth:`due`
        ``delay`` seconds from now (at once for a ``delay`` of zero).
        """
        if value is None or value.position is None:
            self._alarm_seen = value is not None
            self._deadline = None
            return False
        alarm_ended = self._alarm_seen
        self._alarm_seen = False
        if not enabled:
            self._deadline = None
        elif alarm_ended:
            self._deadline = self.clock() + delay
            self._position = value.position
            return True
        elif value.position != self._position:
            self._deadline = None
        return False

    def cancel(self) -> None:
        """Stop a running wait; the next alarm starts a new one."""
        self._deadline = None

    @property
    def waiting(self) -> bool:
        """Return whether a wait is running."""
        return self._deadline is not None

    def due(self, value: BlindValue | None, *, enabled: bool, healthy: bool) -> bool:
        """Return ``True`` once, when the wait is over and the blind is to be lowered now.

        ``healthy`` is whether a command can reach the server. With the
        wait over the answer is final, also when it is ``False``.
        """
        if self._deadline is None or self.clock() < self._deadline:
            return False
        self._deadline = None
        # The position is not consulted: right after an alarm the server may
        # still report the one from before it, while the blind is up.
        return enabled and healthy and value is not None and value.position is not None
