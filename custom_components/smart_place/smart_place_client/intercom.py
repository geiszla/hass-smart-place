"""The doors of the intercom and the dashboard view built from them.

The four ``OEFFNER<n>`` commands open doors of the building. The view
shows one block per door: a heading, the cameras that belong to the door
and a wide button that opens it. Which camera belongs to which door is
not sent by the server, so it is decided from the names: a camera belongs
to the door whose name starts with the camera's name, or the other way
round ("Garage camera" and "Garage entrance camera" both show the door
"Garage entrance").

Pure functions, no HA imports: the caller supplies entity ids and names
and gets plain dicts back.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from .commands import CommandDefinition, Commands


@dataclass(frozen=True, slots=True)
class Door:
    """One door the installation can open."""

    key: str
    name: str
    command: CommandDefinition


# In the order the dashboard shows them.
DOORS: Final[tuple[Door, ...]] = (
    Door(key="front_door", name="Front door", command=Commands.OpenFrontDoor),
    Door(key="ground_floor_entrance", name="Ground floor entrance", command=Commands.OpenGroundFloorEntrance),
    Door(key="garage_entrance", name="Garage entrance", command=Commands.OpenGarageEntrance),
    Door(key="mailbox", name="Mailbox", command=Commands.OpenMailbox),
)


@dataclass(frozen=True, slots=True)
class NamedEntity:
    """An entity and the name the integration gave it (without its device's name)."""

    entity_id: str
    name: str


# A camera entity is named "<place> camera".
_CAMERA_WORD: Final = "camera"

# Heading icon by a word of the door's name; the first match wins.
_DOOR_ICONS: Final[tuple[tuple[str, str], ...]] = (
    ("garage", "mdi:garage"),
    ("mailbox", "mdi:mailbox-open"),
    ("entrance", "mdi:office-building"),
)
_DOOR_ICON: Final = "mdi:door"
_CAMERAS_ICON: Final = "mdi:cctv"
_OPEN_ICON: Final = "mdi:lock-open-variant"

_MAX_COLUMNS: Final = 3


def _words(name: str) -> list[str]:
    return name.lower().split()


def _place(camera_name: str) -> list[str]:
    """Return the words of a camera's name without the word "camera" at its end."""
    words = _words(camera_name)
    return words[:-1] if words[-1:] == [_CAMERA_WORD] else words


def _shared_start(first: Sequence[str], second: Sequence[str]) -> int:
    """Return how many words the shorter of two names has, if the longer starts with them; else 0."""
    shorter, longer = sorted((first, second), key=len)
    return len(shorter) if longer[: len(shorter)] == shorter else 0


def camera_door(camera_name: str, door_names: Sequence[str]) -> int | None:
    """Return the index of the door a camera belongs to, ``None`` if its name fits no door.

    Of several doors that fit, the one that shares the most words wins,
    then the one whose name is nearest in length, then the first.
    """
    place = _place(camera_name)
    best: tuple[int, int] | None = None
    found = None
    for index, door_name in enumerate(door_names):
        door = _words(door_name)
        shared = _shared_start(place, door)
        if not shared:
            continue
        rank = (shared, -abs(len(door) - len(place)))
        if best is None or rank > best:
            best, found = rank, index
    return found


def _heading(text: str, icon: str) -> dict[str, Any]:
    return {"type": "heading", "heading": text, "heading_style": "title", "icon": icon}


def _camera_card(camera: NamedEntity) -> dict[str, Any]:
    return {
        "type": "picture-entity",
        "entity": camera.entity_id,
        "camera_image": camera.entity_id,
        "camera_view": "auto",
        "fit_mode": "cover",
        "show_name": False,
        "show_state": False,
    }


def _open_card(door: NamedEntity) -> dict[str, Any]:
    return {
        "type": "button",
        "entity": door.entity_id,
        "icon": _OPEN_ICON,
        "show_name": False,
        "show_icon": True,
        "show_state": False,
        "grid_options": {"columns": "full", "rows": 2},
        "tap_action": {
            "action": "perform-action",
            "perform_action": "button.press",
            "target": {"entity_id": door.entity_id},
        },
    }


def _door_icon(name: str) -> str:
    words = _words(name)
    return next((icon for word, icon in _DOOR_ICONS if word in words), _DOOR_ICON)


def intercom_view(doors: Sequence[NamedEntity], cameras: Sequence[NamedEntity]) -> dict[str, Any] | None:
    """Build the Intercom view; ``None`` without any door button and camera.

    One block per door, in the given order: heading, its cameras (the one
    named like the door first), the button that opens it. A door without a
    camera is a short block: it shares a column with the next door that
    has one. Cameras that fit no door come last, under "Cameras".
    """
    if not doors and not cameras:
        return None
    door_names = [door.name for door in doors]
    by_door: dict[int | None, list[NamedEntity]] = {}
    for camera in cameras:
        by_door.setdefault(camera_door(camera.name, door_names), []).append(camera)

    sections: list[list[dict[str, Any]]] = []
    # Blocks of doors without a camera, waiting for the next door that has one.
    short: list[list[dict[str, Any]]] = []
    for index, door in enumerate(doors):
        own = sorted(by_door.get(index, ()), key=lambda camera: _place(camera.name) != _words(door.name))
        block = [
            _heading(door.name, _door_icon(door.name)),
            *(_camera_card(camera) for camera in own),
            _open_card(door),
        ]
        if own:
            sections.append([card for held in short for card in held] + block)
            short = []
        else:
            short.append(block)
    sections.extend(short)
    if None in by_door:
        sections.append([_heading("Cameras", _CAMERAS_ICON), *(_camera_card(camera) for camera in by_door[None])])
    return {
        "title": "Intercom",
        "path": "intercom",
        "icon": "mdi:doorbell-video",
        "type": "sections",
        "max_columns": _MAX_COLUMNS,
        "sections": [{"type": "grid", "cards": cards} for cards in sections],
    }
