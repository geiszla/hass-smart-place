"""Tests for :mod:`smart_place_client.expected`, with an injected clock.

The tests call the helper in the order the entities do: decide against
``resolve``, ``expect`` with the server value of that moment, then read
the state again with ``resolve`` right away (the entity writes its state
after every command).
"""

from __future__ import annotations

from smart_place_client import EXPECTED_STATE_TIMEOUT, GROUP_SETTLE_TIME, ExpectedState, SettleTime, step_setpoint


class _Clock:
    """Hand-driven monotonic clock."""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class _Light:
    """A toggle entity in miniature: the server value, the expectation and the toggles sent."""

    def __init__(self, *, on: bool, clock: _Clock | None = None) -> None:
        self.server: bool | None = on
        self.expected: ExpectedState[bool] = ExpectedState(clock=clock or _Clock())
        self.toggles = 0

    @property
    def is_on(self) -> bool | None:
        return self.expected.resolve(self.server)

    def switch(self, *, on: bool) -> None:
        """What ``SmartPlaceToggleEntity`` does for turn on / turn off."""
        if self.is_on is on:
            return
        self.toggles += 1
        self.expected.expect(on, self.server)
        # The entity writes its state right after the command.
        assert self.is_on is on

    def receive(self, on: bool | None) -> bool | None:
        """A value frame arrives; return what the entity shows then."""
        self.server = on
        return self.is_on


def test_without_expectation_the_server_value_rules() -> None:
    expected: ExpectedState[bool] = ExpectedState(clock=_Clock())
    assert expected.resolve(True) is True
    assert expected.resolve(None) is None


def test_expected_value_is_shown_at_once() -> None:
    """Right after a command the entity shows the target, not the old server value."""
    light = _Light(on=False)
    light.switch(on=True)
    assert light.toggles == 1
    assert light.is_on is True


def test_matching_server_value_confirms() -> None:
    light = _Light(on=False)
    light.switch(on=True)
    assert light.receive(True) is True
    # Confirmed: a later real change is followed at once.
    assert light.receive(False) is False


def test_contrary_server_value_is_ignored_while_pending() -> None:
    """A stale rebroadcast between command and echo must not flicker the state."""
    clock = _Clock()
    light = _Light(on=False, clock=clock)
    light.switch(on=True)
    clock.now += EXPECTED_STATE_TIMEOUT - 0.1
    assert light.receive(False) is True


def test_unconfirmed_expectation_times_out() -> None:
    """Without an echo the server value is shown again after the timeout."""
    clock = _Clock()
    light = _Light(on=False, clock=clock)
    light.switch(on=True)
    clock.now += EXPECTED_STATE_TIMEOUT
    assert light.is_on is False
    assert light.is_on is False


def test_new_command_restarts_the_timeout() -> None:
    clock = _Clock()
    expected: ExpectedState[bool] = ExpectedState(timeout=3.0, clock=clock)
    expected.expect(False, True)
    clock.now += 2.0
    expected.expect(True, True)
    clock.now += 2.0
    assert expected.resolve(False) is True
    clock.now += 1.0
    assert expected.resolve(False) is False


def test_clear_drops_the_expectation() -> None:
    expected: ExpectedState[int] = ExpectedState(clock=_Clock())
    expected.expect(22, 21)
    expected.clear()
    assert expected.resolve(21) == 21


def test_unknown_server_value_ends_the_expectation() -> None:
    """The connection dropped and the values were cleared: the command may never have arrived.

    The entity then shows unknown, and it keeps showing the server value
    when the server replays the old value after a quick reconnect.
    """
    light = _Light(on=False)
    light.switch(on=True)
    assert light.receive(None) is None
    assert light.receive(False) is False


def test_two_quick_setpoint_steps_add_up() -> None:
    """The second step starts from the expected setpoint, not the stale server one."""
    expected: ExpectedState[int] = ExpectedState(clock=_Clock())
    server = 21
    sent = []
    for _ in range(2):
        current = expected.resolve(server)
        assert current is not None
        target = step_setpoint(current, 1)
        sent.append(target)
        expected.expect(target, server)
        assert expected.resolve(server) == target
    assert sent == [22, 23]
    # The echo of the first step is not the newest expectation; the second one's confirms.
    assert expected.resolve(22) == 23
    assert expected.resolve(23) == 23
    assert expected.resolve(24) == 24


def test_a_step_up_and_back_ends_on_the_first_setpoint() -> None:
    """21 → 22 → 21 before any echo: the old 21 does not confirm, the echoes 22, 21 do."""
    expected: ExpectedState[int] = ExpectedState(clock=_Clock())
    expected.expect(22, 21)
    assert expected.resolve(21) == 22
    expected.expect(21, 21)
    assert expected.resolve(21) == 21
    assert expected.resolve(22) == 21
    assert expected.resolve(21) == 21
    # Confirmed by the second echo: a change made elsewhere is followed.
    assert expected.resolve(24) == 24


def test_on_then_off_before_the_first_echo_ends_off() -> None:
    """Two toggles go out; the echoes (on, off) are not shown as a flicker and need no third toggle.

    Regression: the server still said "off" when "off" became the
    expectation, so that old value confirmed it at once. The echoes were
    then shown as on, off, and a "turn off" between them sent a third
    toggle that left the light on.
    """
    light = _Light(on=False)
    light.switch(on=True)
    light.switch(on=True)
    light.switch(on=False)
    assert light.toggles == 2
    # An unchanged rebroadcast of the old value is not the echo.
    assert light.receive(False) is False
    # First echo: the light went on. Still shown off, and "turn off" sends nothing.
    assert light.receive(True) is False
    light.switch(on=False)
    assert light.toggles == 2
    # Second echo: off. Now the expectation is confirmed.
    assert light.receive(False) is False
    assert light.receive(True) is True


def test_an_expectation_the_server_already_meets_still_times_out() -> None:
    """Without the echoes (the toggles were lost) the server value rules again after the timeout."""
    clock = _Clock()
    light = _Light(on=False, clock=clock)
    light.switch(on=True)
    light.switch(on=False)
    clock.now += EXPECTED_STATE_TIMEOUT
    assert light.is_on is False
    assert light.receive(True) is True


def test_fan_level_follows_the_same_rules() -> None:
    """Level 3 active, level 1 selected: the server's level is the highest active one."""
    expected: ExpectedState[int] = ExpectedState(clock=_Clock())
    assert expected.resolve(3) == 3
    expected.expect(1, 3)
    assert expected.resolve(3) == 1
    # Echo part one: level 1 on (3 is still on, so the level reads 3). Part two: level 3 off.
    assert expected.resolve(3) == 1
    assert expected.resolve(1) == 1
    assert expected.resolve(2) == 2


def test_settle_time_runs_for_its_duration_after_a_start() -> None:
    clock = _Clock()
    settle = SettleTime(clock=clock)
    assert settle.remaining() == 0.0
    settle.start()
    assert settle.remaining() == GROUP_SETTLE_TIME
    clock.now += 0.25
    assert settle.remaining() == GROUP_SETTLE_TIME - 0.25
    clock.now += GROUP_SETTLE_TIME
    assert settle.remaining() == 0.0


def test_settle_time_restarts_with_every_start() -> None:
    clock = _Clock()
    settle = SettleTime(duration=1.0, clock=clock)
    settle.start()
    clock.now += 0.75
    settle.start()
    assert settle.remaining() == 1.0
