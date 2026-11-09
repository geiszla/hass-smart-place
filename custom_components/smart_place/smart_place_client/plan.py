"""Floor plan geometry and the dashboard built from it.

The vendor app draws the plan image unscaled at page position left 10,
top 54 and places every device at the page position from its config
frame. The helpers here turn those page positions into marker centres in
image pixels (offsets measured in the vendor app, 2026-11-09) and build
the configuration of Home Assistant's ``picture-elements`` card from them:
one view per function (lights, blinds, climate), as in the vendor app,
because markers of different kinds overlap, plus a view with the scenes.

Pure functions, no HA imports: the caller supplies entity ids and image
sizes and gets plain dicts back.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
import struct
from typing import Any, Final, TypeVar

from .devices import BLIND_KIND_SLATTED, BlindConfig, ClimateZoneConfig, LightConfig, PlanConfig

Point = tuple[float, float]

_Device = TypeVar("_Device", LightConfig, BlindConfig, ClimateZoneConfig)

_PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
_PNG_HEADER_LENGTH: Final = 24

# Blind buttons relative to the blind's state icon, in image pixels: the
# vendor app puts Down / Vent / Up in one row below the icon.
_BLIND_DOWN_DX: Final = -60.0
_BLIND_UP_DX: Final = 57.0
_BLIND_BUTTON_DY: Final = 50.0

# Climate marker layout, in image pixels: measured temperature above the
# target, the − / + buttons left and right of both.
_CLIMATE_LINE_DY: Final = 11.0
_CLIMATE_BUTTON_DX: Final = 62.0

_UNAVAILABLE_STATES: Final = ["unknown", "unavailable"]


def light_centre(left: int, top: int) -> Point:
    """Marker centre of a light or socket, in image pixels."""
    return (left + 56.5, top - 74.5)


def blind_centre(left: int, top: int) -> Point:
    """Centre of a blind's state icon, in image pixels."""
    return (left + 10.0, top - 84.0)


def climate_centre(left: int, top: int) -> Point:
    """Marker centre of a climate zone, in image pixels."""
    return (left + 88.0, top - 13.5)


def png_size(data: bytes) -> tuple[int, int] | None:
    """Return ``(width, height)`` from a PNG header, ``None`` if ``data`` is no PNG."""
    if len(data) < _PNG_HEADER_LENGTH or data[:8] != _PNG_SIGNATURE or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return width, height


@dataclass(frozen=True, slots=True)
class PlanBackground:
    """The image entity that shows a plan, and the image's pixel size."""

    entity_id: str
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class DashboardEntities:
    """Entity ids the dashboard may use; anything missing is left out.

    The three mappings are keyed by vendor id (a socket's switch entity
    sits in ``lights`` next to the light entities).
    """

    lights: Mapping[int, str] = field(default_factory=dict)
    blinds: Mapping[int, str] = field(default_factory=dict)
    climates: Mapping[int, str] = field(default_factory=dict)
    scenes: Sequence[str] = ()
    switches: Sequence[str] = ()
    fans: Sequence[str] = ()
    buttons: Sequence[str] = ()


def _style(point: Point, image: PlanBackground) -> dict[str, str]:
    """Position a marker centre in percent of the image size, kept inside the image."""
    left = min(max(point[0] / image.width, 0.0), 1.0)
    top = min(max(point[1] / image.height, 0.0), 1.0)
    return {"left": f"{round(left * 100, 2)}%", "top": f"{round(top * 100, 2)}%"}


def _inside(point: Point, image: PlanBackground) -> bool:
    return 0 <= point[0] <= image.width and 0 <= point[1] <= image.height


def _perform(action: str, entity_id: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    config: dict[str, Any] = {"action": "perform-action", "perform_action": action, "target": {"entity_id": entity_id}}
    if data:
        config["data"] = data
    return config


def _button(icon: str, title: str, action: dict[str, Any], style: dict[str, str]) -> dict[str, Any]:
    return {"type": "icon", "icon": icon, "title": title, "tap_action": action, "style": style}


def _light_elements(entity_id: str, centre: Point, image: PlanBackground) -> list[dict[str, Any]]:
    return [
        {
            "type": "state-icon",
            "entity": entity_id,
            "tap_action": {"action": "toggle"},
            "hold_action": {"action": "more-info"},
            "style": _style(centre, image),
        },
    ]


def _blind_elements(entity_id: str, blind: BlindConfig, centre: Point, image: PlanBackground) -> list[dict[str, Any]]:
    x, y = centre
    style = _style(centre, image)
    more_info = {"action": "more-info"}
    elements: list[dict[str, Any]] = [
        # The position attribute is missing during a weather alarm and while
        # unavailable; a state-label on a missing attribute shows a warning.
        {
            "type": "conditional",
            "conditions": [
                {"condition": "numeric_state", "entity": entity_id, "attribute": "current_position", "above": -1},
            ],
            "elements": [
                {
                    "type": "state-label",
                    "entity": entity_id,
                    "attribute": "current_position",
                    "suffix": "%",
                    "tap_action": more_info,
                    "style": style,
                },
            ],
        },
        {
            "type": "conditional",
            "conditions": [{"condition": "state", "entity": entity_id, "state": _UNAVAILABLE_STATES}],
            "elements": [{"type": "state-label", "entity": entity_id, "tap_action": more_info, "style": style}],
        },
        _button(
            "mdi:arrow-down",
            "Down",
            _perform("cover.close_cover", entity_id),
            _style((x + _BLIND_DOWN_DX, y + _BLIND_BUTTON_DY), image),
        ),
        _button(
            "mdi:arrow-up",
            "Up",
            _perform("cover.open_cover", entity_id),
            _style((x + _BLIND_UP_DX, y + _BLIND_BUTTON_DY), image),
        ),
    ]
    if blind.kind == BLIND_KIND_SLATTED:
        elements.append(
            _button(
                "mdi:blinds-horizontal",
                "Vent",
                _perform("cover.open_cover_tilt", entity_id),
                _style((x, y + _BLIND_BUTTON_DY), image),
            ),
        )
    return elements


def _climate_elements(entity_id: str, centre: Point, image: PlanBackground, unit: str) -> list[dict[str, Any]]:
    x, y = centre
    more_info = {"action": "more-info"}
    target_style = _style((x, y + _CLIMATE_LINE_DY), image)
    return [
        {
            "type": "conditional",
            "conditions": [{"condition": "state", "entity": entity_id, "state_not": "unavailable"}],
            "elements": [
                {
                    "type": "state-label",
                    "entity": entity_id,
                    "attribute": "current_temperature",
                    "suffix": f" {unit}",
                    "tap_action": more_info,
                    "style": _style((x, y - _CLIMATE_LINE_DY), image),
                },
            ],
        },
        {
            "type": "conditional",
            "conditions": [{"condition": "state", "entity": entity_id, "state": "heat"}],
            "elements": [
                {
                    "type": "state-label",
                    "entity": entity_id,
                    "attribute": "temperature",
                    "prefix": "→ ",
                    "suffix": f" {unit}",
                    "tap_action": more_info,
                    "style": target_style,
                },
            ],
        },
        # Off has no target temperature; show the mode instead.
        {
            "type": "conditional",
            "conditions": [{"condition": "state", "entity": entity_id, "state_not": "heat"}],
            "elements": [{"type": "state-label", "entity": entity_id, "tap_action": more_info, "style": target_style}],
        },
        _button(
            "mdi:minus",
            "Lower",
            _perform("smart_place.adjust_setpoint", entity_id, {"steps": -1}),
            _style((x - _CLIMATE_BUTTON_DX, y), image),
        ),
        _button(
            "mdi:plus",
            "Raise",
            _perform("smart_place.adjust_setpoint", entity_id, {"steps": 1}),
            _style((x + _CLIMATE_BUTTON_DX, y), image),
        ),
    ]


def _view(
    title: str,
    path: str,
    icon: str,
    image: PlanBackground | None,
    elements: list[dict[str, Any]],
    unplaced: list[str],
) -> dict[str, Any]:
    """Build one plan view: the plan with its markers, unplaced entities listed below."""
    cards: list[dict[str, Any]] = []
    if image is not None and elements:
        cards.append({"type": "picture-elements", "image_entity": image.entity_id, "elements": elements})
    if unplaced:
        cards.append({"type": "entities", "entities": unplaced})
    card = cards[0] if len(cards) == 1 else {"type": "vertical-stack", "cards": cards}
    return {"title": title, "path": path, "icon": icon, "type": "panel", "cards": [card]}


def _scenes_view(entities: DashboardEntities) -> dict[str, Any] | None:
    cards: list[dict[str, Any]] = []
    if entities.scenes:
        cards.append({"type": "entities", "title": "Scenes", "entities": list(entities.scenes)})
    if entities.switches:
        cards.append({"type": "entities", "title": "Switches", "entities": list(entities.switches)})
    cards.extend({"type": "tile", "entity": fan, "features": [{"type": "fan-speed"}]} for fan in entities.fans)
    if entities.buttons:
        cards.append({"type": "entities", "title": "Groups", "entities": list(entities.buttons)})
    if not cards:
        return None
    return {"title": "Scenes", "path": "scenes", "icon": "mdi:palette", "cards": cards}


def _markers(
    devices: Mapping[int, _Device],
    entity_ids: Mapping[int, str],
    centre_of: Callable[[int, int], Point],
    make: Callable[[str, _Device, Point, PlanBackground], list[dict[str, Any]]],
    plan_id: int | None,
    image: PlanBackground | None,
    *,
    take_strays: bool,
    plans: Mapping[int, PlanConfig],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Split one plan's devices into picture elements and unplaced entity ids.

    ``take_strays`` makes this plan also list the devices whose plan is
    not known, so every entity shows up in exactly one view.
    """
    elements: list[dict[str, Any]] = []
    unplaced: list[str] = []
    for device_id, device in sorted(devices.items()):
        entity_id = entity_ids.get(device_id)
        if entity_id is None:
            continue
        on_plan = device.plan_id == plan_id
        if not on_plan and not (take_strays and device.plan_id not in plans):
            continue
        centre = centre_of(device.left, device.top) if device.left is not None and device.top is not None else None
        if on_plan and image is not None and centre is not None and _inside(centre, image):
            elements.extend(make(entity_id, device, centre, image))
        else:
            unplaced.append(entity_id)
    return elements, unplaced


def build_dashboard(
    *,
    plans: Mapping[int, PlanConfig],
    images: Mapping[int, PlanBackground],
    lights: Mapping[int, LightConfig],
    blinds: Mapping[int, BlindConfig],
    zones: Mapping[int, ClimateZoneConfig],
    entities: DashboardEntities,
    temperature_unit: str,
) -> dict[str, Any]:
    """Build the dashboard configuration for the given devices and entities.

    A device appears on the plan named in its config when that plan's
    image is known and the device's marker centre lies inside it. Every
    other device with an entity (no or unreadable position, no image yet,
    centre outside the image) is listed below the plan with a built-in
    card, so it stays reachable; devices of an unknown plan are listed in
    the first plan's views. With several plans the views repeat per plan
    with the plan label in the title; with one plan the label is not shown.
    """
    plan_ids: list[int | None] = [*sorted(plans)] or [None]
    several_plans = len(plan_ids) > 1
    views: list[dict[str, Any]] = []
    for plan_id in plan_ids:
        image = images.get(plan_id) if plan_id is not None else None
        label = f" {plans[plan_id].label}" if several_plans and plan_id is not None else ""
        path = f"-{plan_id}" if several_plans else ""
        strays = plan_id == plan_ids[0]
        kinds = (
            (
                "Lights",
                "lights",
                "mdi:lightbulb-group",
                _markers(
                    lights,
                    entities.lights,
                    light_centre,
                    lambda entity_id, _light, centre, image: _light_elements(entity_id, centre, image),
                    plan_id,
                    image,
                    take_strays=strays,
                    plans=plans,
                ),
            ),
            (
                "Blinds",
                "blinds",
                "mdi:blinds",
                _markers(
                    blinds,
                    entities.blinds,
                    blind_centre,
                    _blind_elements,
                    plan_id,
                    image,
                    take_strays=strays,
                    plans=plans,
                ),
            ),
            (
                "Climate",
                "climate",
                "mdi:thermostat",
                _markers(
                    zones,
                    entities.climates,
                    climate_centre,
                    lambda entity_id, _zone, centre, image: _climate_elements(
                        entity_id, centre, image, temperature_unit
                    ),
                    plan_id,
                    image,
                    take_strays=strays,
                    plans=plans,
                ),
            ),
        )
        views.extend(
            _view(f"{title}{label}", f"{slug}{path}", icon, image, elements, unplaced)
            for title, slug, icon, (elements, unplaced) in kinds
            if elements or unplaced
        )

    scenes_view = _scenes_view(entities)
    if scenes_view is not None:
        views.append(scenes_view)
    return {"title": "Smart Place", "views": views}
