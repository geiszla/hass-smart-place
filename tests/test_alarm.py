"""Tests for :mod:`smart_place_client.alarm`."""

from __future__ import annotations

from smart_place_client import BlindAlarm
from smart_place_client.devices import BlindValue, parse_blind_value


def _value(raw: str) -> BlindValue:
    value = parse_blind_value(raw)
    assert value is not None
    return value


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
