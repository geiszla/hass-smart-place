"""Tests for :mod:`smart_place_client.rooms`."""

from __future__ import annotations

from smart_place_client import ClimateZoneConfig, SmartPlaceState, blind_centre, light_centre, suggest_zone


def _zone(
    zone_id: int, room: str, top: int | None = None, left: int | None = None, plan_id: int | None = 1
) -> ClimateZoneConfig:
    return ClimateZoneConfig(
        id=zone_id,
        name=f"{room} heating",
        room=room,
        mode="Heizen",
        top=top,
        left=left,
        plan_id=plan_id,
        fan_coil=False,
    )


def test_name_match_ignores_case_and_splits_multi_room_zones() -> None:
    zones = [_zone(1, "Bedroom"), _zone(3, "Living room/dining room/kitchen")]
    assert suggest_zone("KITCHEN spot lights", None, 1, zones) == 3
    assert suggest_zone("Dining room lights", None, 1, zones) == 3
    assert suggest_zone("Bedroom balcony blinds", None, 1, zones) == 1


def test_longest_room_name_wins() -> None:
    """ "Small bathroom lights" contains both "bathroom" and "small bathroom"."""
    zones = [_zone(5, "Bathroom"), _zone(6, "Small bathroom")]
    assert suggest_zone("Small bathroom lights", None, 1, zones) == 6
    assert suggest_zone("Bathroom mirror", None, 1, zones) == 5


def test_equal_name_matches_in_two_zones_fall_back_to_position() -> None:
    zones = [_zone(1, "Studio", top=100, left=100), _zone(2, "Studio", top=500, left=500)]
    assert suggest_zone("Studio lights", None, 1, zones) is None
    assert suggest_zone("Studio lights", light_centre(480, 520), 1, zones) == 2


def test_nearest_marker_is_used_when_no_name_matches() -> None:
    zones = [_zone(1, "Bedroom", top=254, left=419), _zone(5, "Large bathroom", top=108, left=569)]
    assert suggest_zone("Big bathroom lights", light_centre(544, 126), 1, zones) == 5


def test_nearest_marker_must_be_unique() -> None:
    zones = [_zone(1, "North", top=100, left=100), _zone(2, "South", top=100, left=300)]
    # x of the device centre is exactly between the two zone markers.
    centre = (100 + 88.0 + 100, 100 - 13.5)
    assert suggest_zone("Lamp", centre, 1, zones) is None


def test_position_needs_the_same_plan_and_placed_zones() -> None:
    zones = [_zone(1, "North", top=100, left=100, plan_id=2), _zone(2, "South")]
    assert suggest_zone("Lamp", (150.0, 90.0), 1, zones) is None
    assert suggest_zone("Lamp", (150.0, 90.0), 2, zones) == 1


def test_no_hint_gives_no_suggestion() -> None:
    assert suggest_zone("Lamp", None, 1, [_zone(1, "Bedroom")]) is None
    assert suggest_zone("Lamp", (10.0, 10.0), 1, []) is None


def test_reference_flat_rooms(flat_state: SmartPlaceState) -> None:
    """22 of the 25 devices match a zone by name; the 3 bathroom ones by position."""
    zones = flat_state.climate.values()
    large_bathroom = next(zone.id for zone in zones if zone.room == "Large bathroom")
    by_name: dict[str, int | None] = {}
    suggested: dict[str, int | None] = {}
    for light in flat_state.lights.values():
        assert light.top is not None
        assert light.left is not None
        by_name[light.name] = suggest_zone(light.name, None, light.plan_id, zones)
        suggested[light.name] = suggest_zone(light.name, light_centre(light.left, light.top), light.plan_id, zones)
    for blind in flat_state.blinds.values():
        assert blind.top is not None
        assert blind.left is not None
        by_name[blind.name] = suggest_zone(blind.name, None, blind.plan_id, zones)
        suggested[blind.name] = suggest_zone(blind.name, blind_centre(blind.left, blind.top), blind.plan_id, zones)

    assert len(suggested) == 25
    unmatched = {name for name, zone_id in by_name.items() if zone_id is None}
    assert unmatched == {"Big bathroom lights", "Bath blinds", "Bathroom window blinds"}
    assert all(zone_id is not None for zone_id in suggested.values())
    assert {suggested[name] for name in unmatched} == {large_bathroom}

    rooms = {name: next(zone.room for zone in zones if zone.id == zone_id) for name, zone_id in suggested.items()}
    assert rooms["Kitchen spot lights"] == "Living room/dining room/kitchen"
    assert rooms["Laundry room lights"] == "Corridor/laundry room"
    assert rooms["Office balcony door blinds"] == "Office"
    assert rooms["Small bathroom lights"] == "Small bathroom"
    assert rooms["Bedroom socket"] == "Bedroom"
