"""Tests for :mod:`smart_place_client.alarm`, with an injected clock."""

from __future__ import annotations

import pytest

from smart_place_client import (
    KNOWN_COMMANDS,
    BlindAlarm,
    Commands,
    LowerAfterAlarm,
    blind_can_be_lowered,
    command_moves_blind,
)
from smart_place_client.devices import BlindValue, parse_blind_value

_WAIT = 600.0


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _value(raw: str) -> BlindValue:
    value = parse_blind_value(raw)
    assert value is not None
    return value


_ALARM = _value("regenalarm-00")
_UP = _value("0-00")
_DOWN = _value("100-00")


def test_a_blind_is_free_before_the_first_report() -> None:
    alarm = BlindAlarm()
    assert alarm.blocked is False
    assert alarm.alarm is None
    alarm.update(None)
    assert alarm.blocked is False


def test_an_alarm_word_blocks_and_a_position_frees() -> None:
    alarm = BlindAlarm()
    alarm.update(_value("regenalarm-00"))
    assert alarm.blocked is True
    assert alarm.alarm == "rain"
    alarm.update(_value("windalarm-00"))
    assert alarm.alarm == "wind"
    alarm.update(_value("0-00"))
    assert alarm.blocked is False
    assert alarm.alarm is None


def test_an_unknown_word_blocks_too() -> None:
    alarm = BlindAlarm()
    alarm.update(_value("sturmalarm-01"))
    assert alarm.blocked is True
    assert alarm.alarm == "sturmalarm"


def test_the_last_report_is_kept_while_no_value_is_known() -> None:
    """After a reconnect the value is unknown until the server replays it."""
    alarm = BlindAlarm()
    alarm.update(_value("hagelalarm-00"))
    alarm.update(None)
    assert alarm.blocked is True
    assert alarm.alarm == "hail"
    # The replay confirms it: nothing changed for a consumer.
    alarm.update(_value("hagelalarm-00"))
    assert alarm.alarm == "hail"

    free = BlindAlarm()
    free.update(_value("40-00"))
    free.update(None)
    assert free.blocked is False


def test_an_alarm_that_ended_during_the_gap_ends_with_the_fresh_value() -> None:
    alarm = BlindAlarm()
    alarm.update(_value("regenalarm-00"))
    alarm.update(None)
    assert alarm.blocked is True
    alarm.update(_value("0-00"))
    assert alarm.blocked is False


# ----------------------------- LowerAfterAlarm -----------------------------


def _feed(rule: LowerAfterAlarm, *values: BlindValue | None, enabled: bool = True, delay: float = 0.0) -> list[bool]:
    return [rule.update(value, enabled=enabled, delay=delay) for value in values]


def _due(rule: LowerAfterAlarm, value: BlindValue | None, *, enabled: bool = True, healthy: bool = True) -> bool:
    return rule.due(value, enabled=enabled, healthy=healthy)


def test_values_replayed_after_a_connect_do_not_trigger() -> None:
    """The server replays every value on each connect: a first position is no alarm end."""
    rule = LowerAfterAlarm(clock=_Clock())
    assert _feed(rule, None, _UP, _UP, _value("0-01")) == [False, False, False, False]
    assert rule.waiting is False
    assert _due(rule, _UP) is False


def test_alarm_end_triggers_once() -> None:
    rule = LowerAfterAlarm(clock=_Clock())
    assert _feed(rule, None, _ALARM, _ALARM) == [False, False, False]
    assert _due(rule, _ALARM) is False
    # Without a waiting time the blind is lowered at once.
    assert _feed(rule, _UP) == [True]
    assert _due(rule, _UP) is True
    # Once: neither a second look nor the rebroadcast of the position lowers again.
    assert _due(rule, _UP) is False
    assert _feed(rule, _UP, _UP) == [False, False]
    assert _due(rule, _UP) is False


def test_alarm_end_is_due_when_the_waiting_time_is_over() -> None:
    clock = _Clock()
    rule = LowerAfterAlarm(clock=clock)
    assert _feed(rule, _ALARM, _UP, delay=_WAIT) == [False, True]
    assert rule.waiting is True
    clock.now += _WAIT - 1
    assert _due(rule, _UP) is False
    # The rebroadcast of the same position does not restart or stop the wait.
    assert _feed(rule, _UP, _value("0-01"), delay=_WAIT) == [False, False]
    clock.now += 1
    assert _due(rule, _UP) is True
    assert rule.waiting is False
    assert _due(rule, _UP) is False


def test_an_unknown_alarm_word_counts_as_an_alarm() -> None:
    rule = LowerAfterAlarm(clock=_Clock())
    assert _feed(rule, _value("sturmalarm-00"), _UP) == [False, True]
    assert _due(rule, _UP) is True


def test_alarm_again_within_the_waiting_time_cancels() -> None:
    """Alarm, free, alarm: nothing is lowered; the next alarm end starts a new wait."""
    clock = _Clock()
    rule = LowerAfterAlarm(clock=clock)
    assert _feed(rule, _ALARM, _UP, delay=_WAIT) == [False, True]
    clock.now += _WAIT / 2
    assert _feed(rule, _ALARM, delay=_WAIT) == [False]
    assert rule.waiting is False
    clock.now += _WAIT
    assert _due(rule, _ALARM) is False
    # The second alarm ends: a full wait from then on.
    assert _feed(rule, _UP, delay=_WAIT) == [True]
    clock.now += _WAIT - 1
    assert _due(rule, _UP) is False
    clock.now += 1
    assert _due(rule, _UP) is True


def test_reconnect_during_an_alarm_does_not_trigger_on_the_replay() -> None:
    """The alarm ended while the connection was down: that end was not seen, nothing is lowered."""
    rule = LowerAfterAlarm(clock=_Clock())
    assert _feed(rule, _ALARM) == [False]
    # The connection drops (the values are cleared), the new one replays a position.
    assert _feed(rule, None, _UP, _UP) == [False, False, False]
    assert rule.waiting is False
    assert _due(rule, _UP) is False


def test_alarm_end_seen_in_the_new_connection_triggers() -> None:
    """Reconnect during an alarm: the replay says alarm, and its later end is seen live."""
    rule = LowerAfterAlarm(clock=_Clock())
    assert _feed(rule, _ALARM, None, _ALARM) == [False, False, False]
    assert _feed(rule, _UP) == [True]
    assert _due(rule, _UP) is True


def test_a_dropped_connection_cancels_the_wait() -> None:
    clock = _Clock()
    rule = LowerAfterAlarm(clock=clock)
    assert _feed(rule, _ALARM, _UP, delay=_WAIT) == [False, True]
    assert _feed(rule, None, delay=_WAIT) == [False]
    assert rule.waiting is False
    assert _feed(rule, _UP, delay=_WAIT) == [False]
    clock.now += _WAIT
    assert _due(rule, _UP) is False


def test_switching_off_cancels_the_wait() -> None:
    clock = _Clock()
    rule = LowerAfterAlarm(clock=clock)
    assert _feed(rule, _ALARM, _UP, delay=_WAIT) == [False, True]
    assert _feed(rule, _UP, enabled=False, delay=_WAIT) == [False]
    assert rule.waiting is False
    # Switched on again: the old alarm end does not come back.
    assert _feed(rule, _UP, delay=_WAIT) == [False]
    clock.now += _WAIT
    assert _due(rule, _UP) is False


def test_switched_off_at_the_end_of_the_alarm_nothing_starts() -> None:
    rule = LowerAfterAlarm(clock=_Clock())
    assert _feed(rule, _ALARM, _UP, enabled=False) == [False, False]
    assert rule.waiting is False
    # Switching on later does not replay the missed end either.
    assert _feed(rule, _UP) == [False]
    assert _due(rule, _UP) is False


def test_switched_on_during_the_alarm_its_end_triggers() -> None:
    """The alarm is noted whether the function is on or off."""
    rule = LowerAfterAlarm(clock=_Clock())
    assert _feed(rule, _ALARM, enabled=False) == [False]
    assert _feed(rule, _ALARM, _UP) == [False, True]
    assert _due(rule, _UP) is True


def test_switched_off_when_the_wait_is_over_nothing_is_lowered() -> None:
    clock = _Clock()
    rule = LowerAfterAlarm(clock=clock)
    assert _feed(rule, _ALARM, _UP, delay=_WAIT) == [False, True]
    clock.now += _WAIT
    assert _due(rule, _UP, enabled=False) is False
    assert _due(rule, _UP) is False


def test_a_manual_command_cancels_the_wait() -> None:
    clock = _Clock()
    rule = LowerAfterAlarm(clock=clock)
    assert _feed(rule, _ALARM, _UP, delay=_WAIT) == [False, True]
    rule.cancel()
    assert rule.waiting is False
    clock.now += _WAIT
    assert _due(rule, _UP) is False
    # The next alarm is a new case.
    assert _feed(rule, _ALARM, _UP, delay=_WAIT) == [False, True]


def test_a_position_change_during_the_wait_cancels() -> None:
    """Somebody moves the blind (wall button, vendor app, a scene): the wait is over."""
    clock = _Clock()
    rule = LowerAfterAlarm(clock=clock)
    assert _feed(rule, _ALARM, _UP, delay=_WAIT) == [False, True]
    assert _feed(rule, _value("12-00"), delay=_WAIT) == [False]
    assert rule.waiting is False
    clock.now += _WAIT
    assert _due(rule, _value("12-00")) is False


def test_already_fully_down_sends_nothing() -> None:
    rule = LowerAfterAlarm(clock=_Clock())
    assert _feed(rule, _ALARM, _DOWN) == [False, True]
    assert _due(rule, _DOWN) is False
    assert rule.waiting is False


def test_not_connected_when_the_wait_is_over_nothing_is_lowered_and_nothing_is_retried() -> None:
    clock = _Clock()
    rule = LowerAfterAlarm(clock=clock)
    assert _feed(rule, _ALARM, _UP, delay=_WAIT) == [False, True]
    clock.now += _WAIT
    assert _due(rule, _UP, healthy=False) is False
    assert _due(rule, _UP) is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("0-00", True), ("55-01", True), ("100-00", False), ("regenalarm-00", False), ("sturmalarm-00", False)],
)
def test_blind_can_be_lowered_with_a_known_position_that_is_not_fully_down(raw: str, expected: bool) -> None:
    assert blind_can_be_lowered(_value(raw)) is expected


def test_blind_without_a_value_cannot_be_lowered() -> None:
    assert blind_can_be_lowered(None) is False


def test_commands_for_a_blind_are_its_own_and_the_blinds_group_buttons() -> None:
    assert {defn.name for defn in KNOWN_COMMANDS if command_moves_blind(defn, defn.example_args, 8)} == {
        "BlindUp",
        "BlindDown",
        "BlindVent",
        "BlindsGroupUp",
        "BlindsGroupDown",
        "BlindsGroupMessage",
    }
    for command in (Commands.BlindUp, Commands.BlindDown, Commands.BlindVent):
        assert not command_moves_blind(command, (7,), 8)
    # Light 8 is not blind 8.
    assert not command_moves_blind(Commands.LightToggle, (8,), 8)
