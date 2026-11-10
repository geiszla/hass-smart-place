"""Tests for :mod:`smart_place_client.intercom`: camera-to-door matching and the Intercom view."""

from __future__ import annotations

from typing import Any

import pytest

from smart_place_client import (
    DOORS,
    Commands,
    DashboardEntities,
    NamedEntity,
    build_dashboard,
    camera_door,
    intercom_view,
)

_DOOR_NAMES = [door.name for door in DOORS]

_DOOR_ENTITIES = [NamedEntity(f"button.{door.key}", door.name) for door in DOORS]
_CAMERA_ENTITIES = [
    NamedEntity("camera.ground_floor_entrance", "Ground floor entrance camera"),
    NamedEntity("camera.garage", "Garage camera"),
    NamedEntity("camera.mailbox", "Mailbox camera"),
    NamedEntity("camera.garage_entrance", "Garage entrance camera"),
]


def _types(section: dict[str, Any]) -> list[str]:
    return [card["type"] for card in section["cards"]]


def _headings(view: dict[str, Any]) -> list[list[str]]:
    return [[card["heading"] for card in section["cards"] if card["type"] == "heading"] for section in view["sections"]]


def test_doors_are_the_four_openers() -> None:
    assert [(door.key, door.name, door.command) for door in DOORS] == [
        ("front_door", "Front door", Commands.OpenFrontDoor),
        ("ground_floor_entrance", "Ground floor entrance", Commands.OpenGroundFloorEntrance),
        ("garage_entrance", "Garage entrance", Commands.OpenGarageEntrance),
        ("mailbox", "Mailbox", Commands.OpenMailbox),
    ]


@pytest.mark.parametrize(
    ("camera", "door"),
    [
        ("Ground floor entrance camera", "Ground floor entrance"),
        ("Garage camera", "Garage entrance"),
        ("Garage entrance camera", "Garage entrance"),
        ("Mailbox camera", "Mailbox"),
        ("mailbox CAMERA", "Mailbox"),
        # Without the word "camera" the whole name is the place.
        ("Mailbox", "Mailbox"),
        # The camera's name may be the longer one.
        ("Front door inside camera", "Front door"),
        ("Courtyard camera", None),
        # A name without a place (the fallback "Camera <id>") fits no door.
        ("Camera 3", None),
        ("camera", None),
        # Only the start counts: "entrance" alone names no door.
        ("Entrance camera", None),
        ("Floor camera", None),
    ],
)
def test_camera_door_goes_by_the_start_of_the_names(camera: str, door: str | None) -> None:
    found = camera_door(camera, _DOOR_NAMES)
    assert (None if found is None else _DOOR_NAMES[found]) == door


def test_camera_door_prefers_the_closest_name() -> None:
    doors = ["Garage", "Garage entrance", "Garage entrance north"]
    assert camera_door("Garage camera", doors) == 0
    assert camera_door("Garage entrance camera", doors) == 1
    assert camera_door("Garage entrance north camera", doors) == 2
    # Two doors fit equally well: the first.
    assert camera_door("Garage camera", ["Garage north", "Garage south"]) == 0
    assert camera_door("Garage camera", []) is None


def test_view_for_the_reference_installation() -> None:
    """Front door (no camera) shares the first column; three columns in all."""
    view = intercom_view(_DOOR_ENTITIES, _CAMERA_ENTITIES)
    assert view is not None
    assert {key: value for key, value in view.items() if key != "sections"} == {
        "title": "Intercom",
        "path": "intercom",
        "icon": "mdi:doorbell-video",
        "type": "sections",
        "max_columns": 3,
    }
    assert all(section["type"] == "grid" for section in view["sections"])
    assert _headings(view) == [["Front door", "Ground floor entrance"], ["Garage entrance"], ["Mailbox"]]
    first, garage, mailbox = view["sections"]
    assert _types(first) == ["heading", "button", "heading", "picture-entity", "button"]
    assert _types(garage) == ["heading", "picture-entity", "picture-entity", "button"]
    assert _types(mailbox) == ["heading", "picture-entity", "button"]
    # The camera named like the door comes first.
    assert [card["entity"] for card in garage["cards"] if card["type"] == "picture-entity"] == [
        "camera.garage_entrance",
        "camera.garage",
    ]
    assert [card["icon"] for section in view["sections"] for card in section["cards"] if card["type"] == "heading"] == [
        "mdi:door",
        "mdi:office-building",
        "mdi:garage",
        "mdi:mailbox-open",
    ]


def test_door_block_cards() -> None:
    view = intercom_view(_DOOR_ENTITIES, _CAMERA_ENTITIES)
    assert view is not None
    heading, camera, button = view["sections"][2]["cards"]
    assert heading == {"type": "heading", "heading": "Mailbox", "heading_style": "title", "icon": "mdi:mailbox-open"}
    assert camera == {
        "type": "picture-entity",
        "entity": "camera.mailbox",
        "camera_image": "camera.mailbox",
        "camera_view": "auto",
        "fit_mode": "cover",
        "show_name": False,
        "show_state": False,
    }
    assert button == {
        "type": "button",
        "entity": "button.mailbox",
        "icon": "mdi:lock-open-variant",
        "show_name": False,
        "show_icon": True,
        "show_state": False,
        "grid_options": {"columns": "full", "rows": 2},
        "tap_action": {
            "action": "perform-action",
            "perform_action": "button.press",
            "target": {"entity_id": "button.mailbox"},
        },
    }


def test_cameras_that_fit_no_door_get_their_own_block() -> None:
    cameras = [
        *_CAMERA_ENTITIES,
        NamedEntity("camera.yard", "Courtyard camera"),
        NamedEntity("camera.five", "Camera 5"),
    ]
    view = intercom_view(_DOOR_ENTITIES, cameras)
    assert view is not None
    assert _headings(view)[-1] == ["Cameras"]
    last = view["sections"][-1]["cards"]
    assert last[0] == {"type": "heading", "heading": "Cameras", "heading_style": "title", "icon": "mdi:cctv"}
    assert [card["entity"] for card in last[1:]] == ["camera.yard", "camera.five"]
    assert _types(view["sections"][-1]) == ["heading", "picture-entity", "picture-entity"]


def test_doors_without_cameras_have_heading_and_button_only() -> None:
    """No camera at all: nothing to share a column with, so one block per column."""
    view = intercom_view(_DOOR_ENTITIES, [])
    assert view is not None
    assert _headings(view) == [["Front door"], ["Ground floor entrance"], ["Garage entrance"], ["Mailbox"]]
    assert all(_types(section) == ["heading", "button"] for section in view["sections"])


def test_a_door_without_a_camera_after_the_last_camera_gets_its_own_column() -> None:
    doors = [*_DOOR_ENTITIES[1:], _DOOR_ENTITIES[0]]
    view = intercom_view(doors, _CAMERA_ENTITIES)
    assert view is not None
    assert _headings(view) == [["Ground floor entrance"], ["Garage entrance"], ["Mailbox"], ["Front door"]]


def test_cameras_without_door_buttons() -> None:
    """Every door button disabled: the cameras are still shown."""
    view = intercom_view([], _CAMERA_ENTITIES)
    assert view is not None
    assert _headings(view) == [["Cameras"]]
    assert len(view["sections"][0]["cards"]) == 5


def test_no_view_without_doors_and_cameras() -> None:
    assert intercom_view([], []) is None


def _dashboard(entities: DashboardEntities) -> dict[str, Any]:
    return build_dashboard(
        plans={}, images={}, lights={}, blinds={}, zones={}, entities=entities, temperature_unit="°C"
    )


def test_dashboard_has_the_intercom_view_after_the_scenes() -> None:
    config = _dashboard(
        DashboardEntities(scenes=["scene.night"], doors=_DOOR_ENTITIES, cameras=_CAMERA_ENTITIES),
    )
    assert [view["path"] for view in config["views"]] == ["scenes", "intercom"]
    assert config["views"][1] == intercom_view(_DOOR_ENTITIES, _CAMERA_ENTITIES)


def test_dashboard_with_door_buttons_only_has_no_placeholder_view() -> None:
    config = _dashboard(DashboardEntities(doors=_DOOR_ENTITIES))
    assert [view["path"] for view in config["views"]] == ["intercom"]
