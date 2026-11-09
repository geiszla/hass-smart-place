"""Tests for :mod:`smart_place_client.conversions`."""

from __future__ import annotations

from smart_place_client import (
    SETPOINT_DEFAULT,
    SETPOINT_MIN,
    cover_position,
    heating_is_off,
    light_is_on,
    setpoint_for_temperature,
    step_setpoint,
    target_temperature,
)


def test_cover_position_is_the_vendor_position_reversed() -> None:
    """Vendor 0 = fully up = Home Assistant 100 (open)."""
    assert cover_position(0) == 100
    assert cover_position(100) == 0
    assert cover_position(33) == 67


def test_light_is_on_for_any_level_above_zero() -> None:
    assert light_is_on(0) is False
    assert light_is_on(250) is True
    assert light_is_on(255) is True
    assert light_is_on(1) is True


def test_setpoint_18_means_off() -> None:
    assert heating_is_off(18) is True
    assert heating_is_off(19) is False
    assert target_temperature(18) is None
    assert target_temperature(21) == 21
    assert SETPOINT_MIN == 19
    assert SETPOINT_DEFAULT == 21


def test_setpoint_for_temperature_rounds_and_stays_in_heating_range() -> None:
    assert setpoint_for_temperature(21.4) == 21
    assert setpoint_for_temperature(21.6) == 22
    assert setpoint_for_temperature(10) == 19
    assert setpoint_for_temperature(40) == 26


def test_step_setpoint_moves_along_the_vendor_scale() -> None:
    """18 (Off) is the bottom of the scale, 26 the top; both ends clamp."""
    assert step_setpoint(21, 1) == 22
    assert step_setpoint(21, -1) == 20
    assert step_setpoint(19, -1) == 18
    assert step_setpoint(18, -1) == 18
    assert step_setpoint(18, 1) == 19
    assert step_setpoint(26, 1) == 26
    assert step_setpoint(20, 10) == 26
    assert step_setpoint(20, -10) == 18
