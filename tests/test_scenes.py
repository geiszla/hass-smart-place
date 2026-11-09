"""Tests for :mod:`smart_place_client.scenes`."""

from __future__ import annotations

from smart_place_client import LevelGroup, SceneConfig, SmartPlaceState, classify_scenes, find_level_groups


def _scene(scene_id: int, name: str, kind: str = "OnOff", *, pin: bool = False) -> SceneConfig:
    return SceneConfig(id=scene_id, name=name, kind=kind, top=None, left=None, pin_protected=pin)


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
    assert classify_scenes(scenes).switches == (1, 2)


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
    # A bare number is not a name with a level.
    assert find_level_groups(_scenes(_scene(1, "1"), _scene(2, "2"), _scene(3, "3"))) == ()


def test_classify_sorts_scenes_into_entity_kinds() -> None:
    scenes = _scenes(
        _scene(1, "Bye-Bye", "OnOn"),
        _scene(6, "Blinds"),
        _scene(3, "Fan 1"),
        _scene(4, "Fan 2"),
        _scene(5, "Fan 3"),
        _scene(8, "Party", "OnOn", pin=True),
        _scene(9, "Safe", pin=True),
        _scene(10, "Odd", "Unknown"),
    )
    entities = classify_scenes(scenes)
    assert entities.triggers == (1,)
    assert entities.switches == (6,)
    assert entities.level_groups == (LevelGroup(name="Fan", scene_ids=(3, 4, 5)),)


def test_existing_switches_are_never_converted_to_a_level_group() -> None:
    """ "Fan 3" appears after "Fan 1" and "Fan 2" became switches: all three stay switches."""
    scenes = _scenes(_scene(1, "Fan 1"), _scene(2, "Fan 2"), _scene(3, "Fan 3"))
    entities = classify_scenes(scenes, keep_as_switch={1, 2})
    assert entities.level_groups == ()
    assert entities.switches == (1, 2, 3)


def test_reference_flat_scenes(flat_state: SmartPlaceState) -> None:
    """Seven trigger scenes, two switches and the three ventilation levels as one group."""
    entities = classify_scenes(flat_state.scene_configs)
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
