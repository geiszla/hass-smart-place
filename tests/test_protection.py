"""Tests for :mod:`smart_place_client.protection`."""

from __future__ import annotations

from smart_place_client import DEVICE_BLIND, DEVICE_LIGHT, KNOWN_COMMANDS, Commands, command_is_protected

_PROTECTED = {DEVICE_LIGHT: {9}}


def test_every_command_of_a_protected_light_is_refused() -> None:
    assert command_is_protected(Commands.LightToggle, (9,), _PROTECTED)
    assert command_is_protected(Commands.LightDim, (9, 128), _PROTECTED)


def test_other_lights_are_not_affected() -> None:
    assert not command_is_protected(Commands.LightToggle, (5,), _PROTECTED)
    assert not command_is_protected(Commands.LightDim, (5, 128), _PROTECTED)
    assert not command_is_protected(Commands.LightToggle, (9,), {})
    assert not command_is_protected(Commands.LightToggle, (9,), {DEVICE_LIGHT: set()})


def test_the_same_id_of_another_family_is_another_device() -> None:
    """Light 9 and blind 9 share nothing but the number."""
    assert not command_is_protected(Commands.BlindDown, (9,), _PROTECTED)
    assert not command_is_protected(Commands.Setpoint, (9, 21), _PROTECTED)
    assert not command_is_protected(Commands.Scene, (9,), _PROTECTED)
    assert not command_is_protected(Commands.MuteToggle, (9,), _PROTECTED)


def test_another_family_can_be_protected_the_same_way() -> None:
    protected = {DEVICE_BLIND: {8}}
    for command in (Commands.BlindUp, Commands.BlindDown, Commands.BlindVent):
        assert command_is_protected(command, (8,), protected)
        assert not command_is_protected(command, (7,), protected)
    assert not command_is_protected(Commands.LightToggle, (8,), protected)


def test_commands_that_name_no_device_are_never_refused() -> None:
    """Scenes, group buttons, doors and the reads take no device id (or none at all)."""
    protected = {DEVICE_LIGHT: {1, 9, 49}, DEVICE_BLIND: {1, 9, 49}}
    for command in KNOWN_COMMANDS:
        if command.device is None:
            assert not command_is_protected(command, command.example_args, protected), command.name
