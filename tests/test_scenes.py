"""Tests for :mod:`smart_place_client.scenes`."""

from __future__ import annotations

import pytest

from smart_place_client import (
    LevelGroup,
    SceneConfig,
    SmartPlaceState,
    classify_scenes,
    find_level_groups,
    is_level_group,
)


def _scene(scene_id: int, name: str, kind: str = "OnOff", *, pin: bool = False) -> SceneConfig:
    return SceneConfig(id=scene_id, name=name, kind=kind, top=None, left=None, pin_protected=pin, displayed=True)


def _scenes(*scenes: SceneConfig) -> dict[int, SceneConfig]:
    return {scene.id: scene for scene in scenes}


def test_three_numbered_on_off_scenes_form_a_level_group() -> None:
    scenes = _scenes(_scene(7, "Pump 2"), _scene(5, "Pump 1"), _scene(9, "Pump 3"))
    assert find_level_groups(scenes) == (LevelGroup(name="Pump", scene_ids=(5, 7, 9)),)


def test_vendor_default_name_is_translated() -> None:
    scenes = _scenes(_scene(3, "Lüftung Stufe 1"), _scene(4, "Lüftung Stufe 2"), _scene(5, "Lüftung Stufe 3"))
    assert find_level_groups(scenes) == (LevelGroup(name="Ventilation", scene_ids=(3, 4, 5)),)


def test_names_are_compared_ignoring_case() -> None:
    scenes = _scenes(_scene(1, "Fan 1"), _scene(2, "FAN 2"), _scene(3, "fan 3"))
    assert find_level_groups(scenes) == (LevelGroup(name="Fan", scene_ids=(1, 2, 3)),)


def test_two_levels_stay_switches() -> None:
    scenes = _scenes(_scene(1, "Fan 1"), _scene(2, "Fan 2"))
    assert find_level_groups(scenes) == ()
    assert classify_scenes(scenes, {1: True, 2: False}).switches == (1, 2)


def test_levels_must_be_exactly_one_to_n() -> None:
    assert find_level_groups(_scenes(_scene(1, "Fan 1"), _scene(2, "Fan 2"), _scene(4, "Fan 4"))) == ()
    assert find_level_groups(_scenes(_scene(1, "Fan 0"), _scene(2, "Fan 1"), _scene(3, "Fan 2"))) == ()
    assert find_level_groups(_scenes(_scene(1, "Fan 2"), _scene(2, "Fan 3"), _scene(3, "Fan 4"))) == ()
    # Two scenes claim level 2.
    assert (
        find_level_groups(_scenes(_scene(1, "Fan 1"), _scene(2, "Fan 2"), _scene(3, "Fan 2"), _scene(4, "Fan 3"))) == ()
    )


def test_only_plain_on_off_scenes_can_be_levels() -> None:
    trigger = _scenes(_scene(1, "Fan 1", "OnOn"), _scene(2, "Fan 2", "OnOn"), _scene(3, "Fan 3", "OnOn"))
    assert find_level_groups(trigger) == ()
    with_pin = _scenes(_scene(1, "Fan 1"), _scene(2, "Fan 2", pin=True), _scene(3, "Fan 3"))
    assert find_level_groups(with_pin) == ()


def test_the_level_follows_a_name_after_whitespace() -> None:
    """A bare number is not a name with a level, and neither is a number glued to the name."""
    assert find_level_groups(_scenes(_scene(1, "1"), _scene(2, "2"), _scene(3, "3"))) == ()
    # Not a group named "1" with the levels 1, 2, 3.
    assert find_level_groups(_scenes(_scene(1, "11"), _scene(2, "12"), _scene(3, "13"))) == ()
    assert find_level_groups(_scenes(_scene(1, "Fan1"), _scene(2, "Fan2"), _scene(3, "Fan3"))) == ()
    assert find_level_groups(_scenes(_scene(1, " 1"), _scene(2, " 2"), _scene(3, " 3"))) == ()
    spaced = _scenes(_scene(1, "Zone 2 fan  1"), _scene(2, "Zone 2 fan 2"), _scene(3, "Zone 2 fan\t3"))
    assert find_level_groups(spaced) == (LevelGroup(name="Zone 2 fan", scene_ids=(1, 2, 3)),)


_FAN = LevelGroup(name="Fan", scene_ids=(3, 4, 5))


def test_exactly_one_active_scene_makes_a_level_group() -> None:
    assert is_level_group(_FAN, {3: False, 4: True, 5: False}) is True
    assert is_level_group(_FAN, {3: False, 4: False, 5: True, 9: True}) is True


@pytest.mark.parametrize(
    "flags",
    [
        {3: False, 4: False, 5: False},
        {3: True, 4: True, 5: False},
        {3: True, 4: True, 5: True},
    ],
)
def test_none_or_several_active_scenes_are_independent_scenes(flags: dict[int, bool]) -> None:
    assert is_level_group(_FAN, flags) is False


@pytest.mark.parametrize("flags", [{}, {3: True}, {3: False, 4: True}, {4: False, 5: False}])
def test_the_decision_waits_for_the_flags_of_all_members(flags: dict[int, bool]) -> None:
    """Also when the known flags already show one, or several, active scenes."""
    assert is_level_group(_FAN, flags) is None


def _mixed_scenes() -> dict[int, SceneConfig]:
    return _scenes(
        _scene(1, "Bye-Bye", "OnOn"),
        _scene(6, "Blinds"),
        _scene(3, "Fan 1"),
        _scene(4, "Fan 2"),
        _scene(5, "Fan 3"),
        _scene(8, "Party", "OnOn", pin=True),
        _scene(9, "Safe", pin=True),
        _scene(10, "Odd", "Unknown"),
    )


def test_classify_sorts_scenes_into_entity_kinds() -> None:
    entities = classify_scenes(_mixed_scenes(), {3: False, 4: False, 5: True})
    assert entities.triggers == (1,)
    assert entities.switches == (6,)
    assert entities.level_groups == (_FAN,)


def test_scenes_named_like_levels_are_switches_when_not_exactly_one_is_active() -> None:
    for flags in ({3: False, 4: False, 5: False}, {3: True, 4: False, 5: True}):
        entities = classify_scenes(_mixed_scenes(), flags)
        assert entities.switches == (3, 4, 5, 6)
        assert entities.level_groups == ()


def test_scenes_named_like_levels_get_no_entity_until_all_their_flags_are_known() -> None:
    """Neither the fan nor switches; the other scenes are not held back."""
    for flags in ({}, {3: False, 4: True}, {3: True, 4: True, 6: False}):
        entities = classify_scenes(_mixed_scenes(), flags)
        assert entities.triggers == (1,)
        assert entities.switches == (6,)
        assert entities.level_groups == ()


def test_existing_switches_are_never_converted_to_a_level_group() -> None:
    """ "Fan 3" appears after "Fan 1" and "Fan 2" became switches: all three stay switches."""
    scenes = _scenes(_scene(1, "Fan 1"), _scene(2, "Fan 2"), _scene(3, "Fan 3"))
    for flags in ({1: True, 2: False, 3: False}, {}):
        entities = classify_scenes(scenes, flags, keep_as_switch={1, 2})
        assert entities.level_groups == ()
        assert entities.switches == (1, 2, 3)


def test_an_existing_level_group_stays_one_whatever_the_flags_say() -> None:
    """The fan exists already: unknown flags (a reconnect) or two active levels do not turn it into switches."""
    for flags in ({}, {3: True, 4: True, 5: False}, {3: False, 4: False, 5: False}):
        entities = classify_scenes(_mixed_scenes(), flags, keep_as_group={3})
        assert entities.level_groups == (_FAN,)
        assert entities.switches == (6,)


def test_an_existing_switch_decides_before_an_existing_level_group() -> None:
    entities = classify_scenes(_mixed_scenes(), {3: False, 4: True, 5: False}, keep_as_switch={4}, keep_as_group={3})
    assert entities.level_groups == ()
    assert entities.switches == (3, 4, 5, 6)


def test_reference_flat_scenes(flat_state: SmartPlaceState) -> None:
    """Seven trigger scenes, two switches and the three ventilation levels as one group."""
    entities = classify_scenes(flat_state.scene_configs, flat_state.scene_states)
    names = {scene.id: scene.name for scene in flat_state.scene_configs.values()}
    assert {names[sid] for sid in entities.triggers} == {
        "Bye-Bye",
        "Welcome",
        "Set heating",
        "Afternoon sun",
        "Evening",
        "Night",
        "Movie (night)",
    }
    assert {names[sid] for sid in entities.switches} == {"Blinds", "Movie (day)"}
    assert entities.level_groups == (LevelGroup(name="Ventilation", scene_ids=(3, 4, 5)),)
    # Exactly one ventilation level is active in the capture.
    assert [flat_state.scene_states[sid] for sid in (3, 4, 5)].count(True) == 1
