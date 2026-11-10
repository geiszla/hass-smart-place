"""Expected state: what a device should report right after a command.

Home Assistant speaks in targets ("turn on"), the vendor protocol mostly
in toggles. A caller therefore has to decide every command against the
state the device will have once the commands already sent took effect —
and it has to show that state at once, without waiting for the server.

:class:`ExpectedState` holds that one value. While it is pending:

- a server value equal to it confirms it (the expectation is dropped and
  the server value rules again) — unless the server already reported that
  value when the expectation was set: then an equal value is old, not the
  echo, and only confirms after a different value was seen in between
  (on, then off again before the first echo: the echoes are on, off);
- a different server value is ignored. The server rebroadcasts unchanged
  values two to three times per second, so a stale frame arriving between
  the command and its echo would otherwise make the entity flicker;
- an unknown server value (``None``: the connection dropped and the values
  were cleared) ends it: the command may never have reached the server;
- after ``timeout`` seconds without confirmation it is dropped and the
  server value is shown again (the command was lost, refused, or changed
  nothing).

:class:`SettleTime` covers the commands that change other devices (a
scene, a group button): for a moment after one of them the server values
are about to change, so a toggle must not be decided against them yet.

Pure logic with an injectable clock; the Home Assistant entity only
schedules a callback at the timeout to write its state, because no frame
arrives at that moment.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import time
from typing import Final, Generic, TypeVar

_T = TypeVar("_T")

EXPECTED_STATE_TIMEOUT: Final = 3.0

# The echoes of the devices a scene changes arrive about 0.2 s after the
# scene command (2026-11-09 capture: scene flag, then the lights).
GROUP_SETTLE_TIME: Final = 1.0


@dataclass(slots=True)
class ExpectedState(Generic[_T]):
    """The value a device is expected to report, until confirmed or timed out."""

    timeout: float = EXPECTED_STATE_TIMEOUT
    clock: Callable[[], float] = time.monotonic
    _value: _T | None = None
    _deadline: float | None = None
    # The server reported the expected value already when it was set, and
    # no other value since: an equal server value is not the echo yet.
    _awaits_change: bool = False

    def expect(self, value: _T, server_value: _T | None) -> None:
        """Start (or restart) expecting ``value``; ``server_value`` is what the server reports now."""
        self._value = value
        self._deadline = self.clock() + self.timeout
        self._awaits_change = server_value == value

    def clear(self) -> None:
        """Drop the expectation."""
        self._value = None
        self._deadline = None
        self._awaits_change = False

    def resolve(self, server_value: _T | None) -> _T | None:
        """Return the effective value given the latest server value.

        The expected value while one is pending, else ``server_value``.
        A confirming or unknown server value and a passed deadline end
        the expectation.
        """
        if self._deadline is None:
            return server_value
        if server_value is None or self.clock() >= self._deadline:
            self.clear()
            return server_value
        if server_value != self._value:
            self._awaits_change = False
            return self._value
        if not self._awaits_change:
            self.clear()
        return server_value


@dataclass(slots=True)
class SettleTime:
    """The wait after a command that changes other devices.

    :meth:`start` when such a command was issued; :meth:`remaining` is how
    long a toggle decision still has to wait for the echoes.
    """

    duration: float = GROUP_SETTLE_TIME
    clock: Callable[[], float] = time.monotonic
    _until: float | None = None

    def start(self) -> None:
        """Note that a command that changes other devices was issued now."""
        self._until = self.clock() + self.duration

    def remaining(self) -> float:
        """Return the seconds left to wait, ``0.0`` when there is nothing to wait for."""
        if self._until is None:
            return 0.0
        return max(0.0, self._until - self.clock())
