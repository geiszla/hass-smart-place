"""Expected state: what a device should report right after a command.

Home Assistant speaks in targets ("turn on"), the vendor protocol mostly
in toggles. A caller therefore has to decide every command against the
state the device will have once the commands already sent took effect —
and it has to show that state at once, without waiting for the server.

:class:`ExpectedState` holds that one value. While it is pending:

- a server value equal to it confirms it (the expectation is dropped and
  the server value rules again);
- a different server value is ignored. The server rebroadcasts unchanged
  values two to three times per second, so a stale frame arriving between
  the command and its echo would otherwise make the entity flicker;
- after ``timeout`` seconds without confirmation it is dropped and the
  server value is shown again (the command was lost, refused, or changed
  nothing).

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


@dataclass(slots=True)
class ExpectedState(Generic[_T]):
    """The value a device is expected to report, until confirmed or timed out."""

    timeout: float = EXPECTED_STATE_TIMEOUT
    clock: Callable[[], float] = time.monotonic
    _value: _T | None = None
    _deadline: float | None = None

    def expect(self, value: _T) -> None:
        """Start (or restart) expecting ``value``."""
        self._value = value
        self._deadline = self.clock() + self.timeout

    def clear(self) -> None:
        """Drop the expectation."""
        self._value = None
        self._deadline = None

    def resolve(self, server_value: _T | None) -> _T | None:
        """Return the effective value given the latest server value.

        The expected value while one is pending, else ``server_value``.
        A matching server value or a passed deadline ends the expectation.
        """
        if self._deadline is None:
            return server_value
        if server_value == self._value or self.clock() >= self._deadline:
            self.clear()
            return server_value
        return self._value
