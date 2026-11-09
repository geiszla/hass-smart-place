"""Tests for :mod:`smart_place_client.expected`, with an injected clock."""

from __future__ import annotations

from smart_place_client import EXPECTED_STATE_TIMEOUT, ExpectedState, step_setpoint


class _Clock:
    """Hand-driven monotonic clock."""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_without_expectation_the_server_value_rules() -> None:
    expected: ExpectedState[bool] = ExpectedState(clock=_Clock())
    assert expected.resolve(True) is True
    assert expected.resolve(None) is None


def test_expected_value_is_shown_at_once() -> None:
    """Right after a command the entity shows the target, not the old server value."""
    expected: ExpectedState[bool] = ExpectedState(clock=_Clock())
    expected.expect(True)
    assert expected.resolve(False) is True


def test_matching_server_value_confirms() -> None:
    clock = _Clock()
    expected: ExpectedState[bool] = ExpectedState(clock=clock)
    expected.expect(True)
    assert expected.resolve(True) is True
    # Confirmed: a later real change is followed at once.
    assert expected.resolve(False) is False


def test_contrary_server_value_is_ignored_while_pending() -> None:
    """A stale rebroadcast between command and echo must not flicker the state."""
    clock = _Clock()
    expected: ExpectedState[bool] = ExpectedState(clock=clock)
    expected.expect(True)
    clock.now += EXPECTED_STATE_TIMEOUT - 0.1
    assert expected.resolve(False) is True


def test_unconfirmed_expectation_times_out() -> None:
    """Without an echo the server value is shown again after the timeout."""
    clock = _Clock()
    expected: ExpectedState[bool] = ExpectedState(clock=clock)
    expected.expect(True)
    clock.now += EXPECTED_STATE_TIMEOUT
    assert expected.resolve(False) is False
    assert expected.resolve(False) is False


def test_new_command_restarts_the_timeout() -> None:
    clock = _Clock()
    expected: ExpectedState[bool] = ExpectedState(timeout=3.0, clock=clock)
    expected.expect(True)
    clock.now += 2.0
    expected.expect(False)
    clock.now += 2.0
    assert expected.resolve(True) is False
    clock.now += 1.0
    assert expected.resolve(True) is True


def test_clear_drops_the_expectation() -> None:
    expected: ExpectedState[int] = ExpectedState(clock=_Clock())
    expected.expect(22)
    expected.clear()
    assert expected.resolve(21) == 21


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
        expected.expect(target)
    assert sent == [22, 23]
    assert expected.resolve(server) == 23


def test_toggle_decision_uses_the_effective_state() -> None:
    """On, then off before the echo: two toggles go out and the light ends off."""
    expected: ExpectedState[bool] = ExpectedState(clock=_Clock())
    server_on = False
    toggles = 0
    for want in (True, True, False):
        if expected.resolve(server_on) != want:
            toggles += 1
            expected.expect(want)
    assert toggles == 2
    # The first echo (on) is contrary to the newest expectation and is ignored.
    assert expected.resolve(True) is False
    assert expected.resolve(False) is False
