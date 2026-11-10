"""Floor plan geometry and the dashboard built from it.

The vendor app draws the plan image unscaled at page position left 10,
top 54 and places every device at the page position from its config
frame. The helpers here turn those page positions into marker centres in
image pixels (offsets measured in the vendor app, 2026-11-09) and build
the configuration of Home Assistant's ``picture-elements`` card from them:
one view per function (lights, blinds, climate), as in the vendor app,
because markers of different kinds overlap, plus a view with the scenes
and the Intercom view (``intercom.py``).

The card is one column wide (at most 500 px), so the whole plan fits a
desktop window and fills a phone's width. It shows only the part of the
image that holds the drawing and the markers. A marker is a rigid group
of elements around one anchor, sized in CSS pixels and drawn on a
backdrop: the plan under it can be white or dark.

Pure functions, no HA imports: the caller supplies entity ids and image
sizes and gets plain dicts back.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
import math
import struct
from typing import Any, Final, TypeVar

from .devices import BLIND_KIND_SLATTED, BlindConfig, ClimateZoneConfig, LightConfig, PlanConfig
from .intercom import NamedEntity, intercom_view

Point = tuple[float, float]

_Device = TypeVar("_Device", LightConfig, BlindConfig, ClimateZoneConfig)

_PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
_PNG_HEADER_LENGTH: Final = 24

# Marker sizes are CSS pixels on a screen at least this wide. On a
# narrower one (a phone) they shrink with the screen, as the plan does, so
# markers that do not touch at this width do not touch at any width.
_FULL_SIZE_VIEWPORT: Final = 412.0

# Kept around the drawing and the outermost marker centres, in image pixels.
_WINDOW_PADDING: Final = 12
# Kept between a marker and the edge of the card.
_EDGE: Final = 2.0

_ICON: Final = 24.0
_LABEL_FONT: Final = 13.0
_LABEL_LINE: Final = 16.0
# The frontend's own padding around the text of a state label; it does not shrink.
_LABEL_PADDING: Final = 8.0
_ROW: Final = _LABEL_LINE + 2 * _LABEL_PADDING
_CORNER: Final = _ROW / 2
# A light's icon is the frontend's state badge, which has this fixed size.
_BADGE: Final = 40.0

# Blind marker, one row: an icon for the position, then Down / Vent / Up as
# in the vendor app. The position icon is set off by a wider gap.
_BLIND_START: Final = 6.0
_BLIND_STATE: Final = _BLIND_START + _ICON + 6.0
_BLIND_BUTTON_PITCH: Final = _ICON + 2.0
_BLIND_END: Final = 4.0
# Wide enough for "Unavailable".
_STATE_WIDTH: Final = 92.0

# Climate marker, two rows: measured temperature above the target, the
# − / + buttons left and right of both.
_CLIMATE_WIDTH: Final = 112.0
_CLIMATE_BUTTON_DX: Final = _CLIMATE_WIDTH / 2 - 2.0 - _ICON / 2
# The icon that says a zone heats (or cools) right now, left of the target.
_ACTION_ICON: Final = 16.0
_ACTION_ICON_DX: Final = -18.0
_HVAC_ACTIONS: Final[dict[str, tuple[str, str, str]]] = {
    "heating": ("mdi:fire", "Heating", "var(--state-climate-heat-color)"),
    "cooling": ("mdi:snowflake", "Cooling", "var(--state-climate-cool-color)"),
}

_UNAVAILABLE_STATES: Final = ["unknown", "unavailable"]

# A blind's position as a gauge: the outline of a window with a shutter
# box, and drawn on its opening how far the blind is down. The stock icons
# for "partly" and "down" look alike at this size. The opening is 8 x 11 of
# the icon's 24 units, at (8, 9).
_POSITION_ICON: Final = "mdi:window-shutter-open"
_OPENING_LEFT: Final = 8.0
_OPENING_TOP: Final = 9.0
_OPENING_WIDTH: Final = 8.0
# By the cover's ``current_position`` (100 is fully up): the limits of the
# position, the rows of the opening drawn as closed, tooltip.
_POSITIONS: Final[tuple[tuple[dict[str, int], float, str], ...]] = (
    ({"above": 99}, 0.0, "Fully up"),
    ({"above": 0, "below": 100}, 6.0, "Partly down"),
    ({"below": 1}, 11.0, "Fully down"),
)

# Shown in place of a blind's position while a weather alarm blocks it,
# by the cover's ``weather_alarm`` attribute: icon and tooltip. Any other
# word gets the last pair.
_ALARM_ICONS: Final[dict[str, tuple[str, str]]] = {
    "rain": ("mdi:weather-pouring", "Rain alarm"),
    "wind": ("mdi:weather-windy", "Wind alarm"),
    "hail": ("mdi:weather-hail", "Hail alarm"),
}
_OTHER_ALARM_ICON: Final = ("mdi:alert", "Weather alarm")
_ALARM_COLOUR: Final = "var(--warning-color)"
# Sets the position gauge apart from the buttons beside it.
_POSITION_COLOUR: Final = "var(--primary-color)"
_BLOCKED_OPACITY: Final = "0.3"

# Shown while no entity is known yet (the server has not listed the devices).
_WAITING_VIEW: Final[dict[str, Any]] = {
    "title": "Smart Place",
    "path": "waiting",
    "icon": "mdi:timer-sand",
    "cards": [
        {
            "type": "markdown",
            "content": (
                "No Smart Place devices are known yet. They appear here once the Smart Place server has listed them."
            ),
        },
    ],
}


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
    """The image entity that shows a plan, and the image's pixel size.

    ``content`` is the box ``(left, top, right, bottom)`` around what the
    image depicts, when the rest of it is empty (transparent); ``None``
    when that is not known.
    """

    entity_id: str
    width: int
    height: int
    content: tuple[int, int, int, int] | None = None


@dataclass(frozen=True, slots=True)
class _Window:
    """The part of a plan image the card shows, in image pixels."""

    left: float
    top: float
    width: float
    height: float


@dataclass(frozen=True, slots=True)
class DashboardEntities:
    """Entity ids the dashboard may use; anything missing is left out.

    The three mappings are keyed by vendor id (a socket's switch entity
    sits in ``lights`` next to the light entities). The three
    ``..._buttons`` are the buttons that act on all lights, all blinds
    and all heating zones. ``doors`` are the door buttons, in the order
    to show them; ``cameras`` the intercom cameras.
    """

    lights: Mapping[int, str] = field(default_factory=dict)
    blinds: Mapping[int, str] = field(default_factory=dict)
    climates: Mapping[int, str] = field(default_factory=dict)
    scenes: Sequence[str] = ()
    switches: Sequence[str] = ()
    fans: Sequence[str] = ()
    light_buttons: Sequence[str] = ()
    blind_buttons: Sequence[str] = ()
    climate_buttons: Sequence[str] = ()
    doors: Sequence[NamedEntity] = ()
    cameras: Sequence[NamedEntity] = ()


def _inside(point: Point, image: PlanBackground) -> bool:
    return 0 <= point[0] <= image.width and 0 <= point[1] <= image.height


def _window(image: PlanBackground, centres: Iterable[Point]) -> _Window:
    """Return the part of the image to show: the drawing and every marker centre, with a margin.

    The card can cut only one axis, by the same amount on both sides (it
    centres the image in a box of another shape), so the window is the
    smaller of the two that are possible. Without a known content box it
    is the whole image.
    """
    if image.content is None:
        return _Window(0, 0, image.width, image.height)
    left, top, right, bottom = image.content
    for x, y in centres:
        left, right = min(left, x), max(right, x)
        top, bottom = min(top, y), max(bottom, y)
    half_width = min(max(image.width / 2 - left, right - image.width / 2) + _WINDOW_PADDING, image.width / 2)
    half_height = min(max(image.height / 2 - top, bottom - image.height / 2) + _WINDOW_PADDING, image.height / 2)
    if half_width / image.width <= half_height / image.height:
        width = math.ceil(2 * half_width)
        return _Window((image.width - width) / 2, 0, width, image.height)
    height = math.ceil(2 * half_height)
    return _Window(0, (image.height - height) / 2, image.width, height)


def _px(size: float, *, fixed: bool = False) -> str:
    """Return ``size`` CSS pixels, fewer on a screen narrower than ``_FULL_SIZE_VIEWPORT``."""
    if fixed:
        return f"{size:g}px"
    return f"min({size:g}px, {round(size * 100 / _FULL_SIZE_VIEWPORT, 2):g}vw)"


def _anchor(
    point: Point, window: _Window, half_width: float, half_height: float, *, fixed: bool = False
) -> dict[str, str]:
    """Position a marker's centre in percent of the window, the whole marker kept inside the card.

    ``half_width`` and ``half_height`` are half the marker's size. Every
    element of one marker gets the same anchor, so the browser moves them
    together when the marker is at the edge.
    """
    left = round((point[0] - window.left) / window.width * 100, 2)
    top = round((point[1] - window.top) / window.height * 100, 2)
    x_margin = _px(half_width + _EDGE, fixed=fixed)
    y_margin = _px(half_height + _EDGE, fixed=fixed)
    return {
        "left": f"clamp({x_margin}, {left:g}%, calc(100% - {x_margin}))",
        "top": f"clamp({y_margin}, {top:g}%, calc(100% - {y_margin}))",
    }


def _shift(dx: float = 0.0, dy: float = 0.0) -> str:
    """Return the transform that puts an element's centre ``dx``, ``dy`` pixels from its anchor."""

    def axis(offset: float) -> str:
        if not offset:
            return "-50%"
        return f"calc(-50% {'+' if offset > 0 else '-'} {_px(abs(offset))})"

    return f"translate({axis(dx)}, {axis(dy)})"


# Under a marker: the card's colour, so the theme's text colour is readable
# on it, and a thin outline where the marker lies on the card itself.
_BACKDROP_COLOUR: Final = "color-mix(in srgb, var(--card-background-color) 85%, transparent)"
_OUTLINE_COLOUR: Final = "var(--divider-color)"
_BACKDROP: Final[dict[str, str]] = {
    "background": _BACKDROP_COLOUR,
    "border-radius": _px(_CORNER),
    "box-shadow": f"0 0 0 1px {_OUTLINE_COLOUR}",
}
# The same as a disc inside the state badge's square: its size cannot be
# set, and discs as large as the badge touch where lights are close.
_BADGE_BACKDROP: Final[dict[str, str]] = {
    "background": (
        f"radial-gradient(closest-side, {_BACKDROP_COLOUR} 86%, {_OUTLINE_COLOUR} 86% 91%, transparent 93%)"
    ),
}
_TEXT: Final[dict[str, str]] = {"font-size": _px(_LABEL_FONT), "line-height": _px(_LABEL_LINE)}
# Without the line height the element's box is taller than the icon and the icon sits above its centre.
_GLYPH: Final[dict[str, str]] = {"--mdc-icon-size": _px(_ICON), "line-height": "0"}


def _perform(action: str, entity_id: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    config: dict[str, Any] = {"action": "perform-action", "perform_action": action, "target": {"entity_id": entity_id}}
    if data:
        config["data"] = data
    return config


def _button(icon: str, title: str, action: dict[str, Any], style: dict[str, str]) -> dict[str, Any]:
    # Without an entity the default hold action (more-info) shows an error toast.
    return {
        "type": "icon",
        "icon": icon,
        "title": title,
        "tap_action": action,
        "hold_action": {"action": "none"},
        "style": {**style, **_GLYPH},
    }


def _light_elements(entity_id: str, centre: Point, window: _Window, *, protected: bool) -> list[dict[str, Any]]:
    element: dict[str, Any] = {
        "type": "state-icon",
        "entity": entity_id,
        "tap_action": {"action": "toggle"},
        "hold_action": {"action": "more-info"},
        "style": {**_anchor(centre, window, _BADGE / 2, _BADGE / 2, fixed=True), **_BADGE_BACKDROP},
    }
    if protected:
        # No command is sent for a protected device: say so, and offer no
        # tap that looks like a toggle. A long press still opens the details.
        element["icon"] = "mdi:lock"
        element["tap_action"] = {"action": "none"}
    return [element]


def _blind_elements(entity_id: str, blind: BlindConfig, centre: Point, window: _Window) -> list[dict[str, Any]]:
    """Build a blind's marker: its position as an icon, or the alarm that blocks it, then the direction buttons.

    The icon's element is as wide as the whole marker and carries its
    backdrop; the buttons lie on its right part. The buttons exist twice,
    switched by the cover's ``blocked`` attribute: with their action, and
    greyed without one while a weather alarm blocks the blind (the
    frontend compares attributes as text).
    """
    more_info = {"action": "more-info"}
    buttons = [("mdi:arrow-down", "Down", "cover.close_cover")]
    if blind.kind == BLIND_KIND_SLATTED:
        buttons.append(("mdi:blinds-horizontal", "Vent", "cover.open_cover_tilt"))
    buttons.append(("mdi:arrow-up", "Up", "cover.open_cover"))
    width = _BLIND_STATE + len(buttons) * _BLIND_BUTTON_PITCH - (_BLIND_BUTTON_PITCH - _ICON) + _BLIND_END
    anchor = _anchor(centre, window, width / 2, _ROW / 2)
    side = (_ROW - _ICON) / 2
    state_style = {
        **anchor,
        **_BACKDROP,
        **_GLYPH,
        "box-sizing": "border-box",
        "min-width": _px(width),
        "padding": f"{_px(side)} 0 {_px(side)} {_px(_BLIND_START)}",
        "text-align": "left",
    }
    button_styles = [
        {**anchor, "transform": _shift(_BLIND_STATE + _ICON / 2 + index * _BLIND_BUTTON_PITCH - width / 2)}
        for index in range(len(buttons))
    ]

    def attribute(name: str, **match: str | list[str]) -> dict[str, Any]:
        return {"condition": "state", "entity": entity_id, "attribute": name, **match}

    def gauge(rows: float) -> dict[str, str]:
        """Draw ``rows`` of the position icon's opening as closed, over the backdrop."""
        if not rows:
            return {}
        left, top = _px(_BLIND_START + _OPENING_LEFT), _px(side + _OPENING_TOP)
        size = f"{_px(_OPENING_WIDTH)} {_px(rows)}"
        return {
            "background": f"linear-gradient(currentColor, currentColor) {left} {top} / {size} no-repeat, {_BACKDROP_COLOUR}"
        }

    def state_icon(icon: str, title: str, conditions: list[dict[str, Any]], **style: str) -> dict[str, Any]:
        return {
            "type": "conditional",
            "conditions": conditions,
            "elements": [
                {
                    "type": "icon",
                    "icon": icon,
                    "title": title,
                    "entity": entity_id,
                    "tap_action": more_info,
                    "hold_action": more_info,
                    "style": {**state_style, **style},
                },
            ],
        }

    return [
        # The position attribute is missing during a weather alarm and while unavailable.
        *(
            state_icon(
                _POSITION_ICON,
                title,
                [{"condition": "numeric_state", "entity": entity_id, "attribute": "current_position", **limits}],
                color=_POSITION_COLOUR,
                **gauge(rows),
            )
            for limits, rows, title in _POSITIONS
        ),
        _state_text(
            entity_id, centre, window, [{"condition": "state", "entity": entity_id, "state": _UNAVAILABLE_STATES}]
        ),
        *(
            state_icon(icon, title, [attribute("weather_alarm", state=alarm)], color=_ALARM_COLOUR)
            for alarm, (icon, title) in _ALARM_ICONS.items()
        ),
        state_icon(
            *_OTHER_ALARM_ICON,
            [attribute("blocked", state="true"), attribute("weather_alarm", state_not=list(_ALARM_ICONS))],
            color=_ALARM_COLOUR,
        ),
        {
            "type": "conditional",
            "conditions": [
                attribute("blocked", state_not="true"),
                {"condition": "state", "entity": entity_id, "state_not": _UNAVAILABLE_STATES},
            ],
            "elements": [
                _button(icon, title, _perform(action, entity_id), button_style)
                for (icon, title, action), button_style in zip(buttons, button_styles, strict=True)
            ],
        },
        {
            "type": "conditional",
            "conditions": [attribute("blocked", state="true")],
            "elements": [
                _button(
                    icon,
                    f"{title} (blocked by a weather alarm)",
                    {"action": "none"},
                    {**button_style, "opacity": _BLOCKED_OPACITY},
                )
                for (icon, title, _), button_style in zip(buttons, button_styles, strict=True)
            ],
        },
    ]


def _state_text(entity_id: str, centre: Point, window: _Window, conditions: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the marker of an entity that has nothing to show but its state ("Unavailable")."""
    return {
        "type": "conditional",
        "conditions": conditions,
        "elements": [
            {
                "type": "state-label",
                "entity": entity_id,
                "tap_action": {"action": "more-info"},
                "style": {**_anchor(centre, window, _STATE_WIDTH / 2, _ROW / 2), **_BACKDROP, **_TEXT},
            },
        ],
    }


def _climate_elements(entity_id: str, centre: Point, window: _Window, unit: str) -> list[dict[str, Any]]:
    """Build a heating zone's marker: measured temperature, target below it, − and + at the sides.

    The measured temperature's element is as large as the whole marker
    and carries its backdrop. While the zone calls for heat (the climate
    entity's ``hvac_action``) a flame stands in place of the arrow before
    the target, and both have the theme's heating colour.
    """
    more_info = {"action": "more-info"}
    anchor = _anchor(centre, window, _CLIMATE_WIDTH / 2, _ROW / 2 + _LABEL_LINE / 2)
    second_row = {**anchor, **_TEXT, "transform": _shift(dy=_LABEL_LINE / 2)}

    def state(**match: str | list[str]) -> list[dict[str, Any]]:
        return [{"condition": "state", "entity": entity_id, **match}]

    def action(**match: str | list[str]) -> dict[str, Any]:
        return {"condition": "state", "entity": entity_id, "attribute": "hvac_action", **match}

    def target(prefix: str, **style: str) -> dict[str, Any]:
        return {
            "type": "state-label",
            "entity": entity_id,
            "attribute": "temperature",
            "prefix": prefix,
            "suffix": f" {unit}",
            "tap_action": more_info,
            "style": {**second_row, **style},
        }

    return [
        {
            "type": "conditional",
            "conditions": state(state_not="unavailable"),
            "elements": [
                {
                    "type": "state-label",
                    "entity": entity_id,
                    "attribute": "current_temperature",
                    "suffix": f" {unit}",
                    "tap_action": more_info,
                    "style": {
                        **anchor,
                        **_BACKDROP,
                        **_TEXT,
                        "box-sizing": "border-box",
                        "min-width": _px(_CLIMATE_WIDTH),
                        "padding-bottom": _px(_LABEL_LINE),
                        "text-align": "center",
                    },
                },
                _button(
                    "mdi:minus",
                    "Lower",
                    _perform("smart_place.adjust_setpoint", entity_id, {"steps": -1}),
                    {**anchor, "transform": _shift(-_CLIMATE_BUTTON_DX)},
                ),
                _button(
                    "mdi:plus",
                    "Raise",
                    _perform("smart_place.adjust_setpoint", entity_id, {"steps": 1}),
                    {**anchor, "transform": _shift(_CLIMATE_BUTTON_DX)},
                ),
            ],
        },
        {
            "type": "conditional",
            "conditions": [*state(state="heat"), action(state_not=list(_HVAC_ACTIONS))],
            "elements": [target("→ ")],
        },
        *(
            {
                "type": "conditional",
                "conditions": [*state(state="heat"), action(state=name)],
                "elements": [
                    target("", color=colour, **{"padding-left": _px(_ACTION_ICON)}),
                    {
                        "type": "icon",
                        "icon": icon,
                        "title": title,
                        "entity": entity_id,
                        "tap_action": more_info,
                        "hold_action": more_info,
                        "style": {
                            **anchor,
                            "transform": _shift(_ACTION_ICON_DX, _LABEL_LINE / 2),
                            "--mdc-icon-size": _px(_ACTION_ICON),
                            "line-height": "0",
                            "color": colour,
                        },
                    },
                ],
            }
            for name, (icon, title, colour) in _HVAC_ACTIONS.items()
        ),
        # Off has no target temperature; show the mode instead.
        {
            "type": "conditional",
            "conditions": state(state_not=["heat", "unavailable"]),
            "elements": [{"type": "state-label", "entity": entity_id, "tap_action": more_info, "style": second_row}],
        },
        _state_text(entity_id, centre, window, state(state="unavailable")),
    ]


def _view(
    title: str,
    path: str,
    icon: str,
    image: PlanBackground | None,
    window: _Window | None,
    elements: list[dict[str, Any]],
    unplaced: list[str],
    below: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build one plan view: the plan with its markers, then the cards ``below``, then the unplaced entities.

    One column: the card is then at most 500 px wide, so the plan fits a
    desktop window without scrolling, and as wide as a phone's screen.
    """
    cards: list[dict[str, Any]] = []
    if image is not None and window is not None and elements:
        cards.append(
            {
                "type": "picture-elements",
                "image_entity": image.entity_id,
                # The card shows the middle of the image in a box of this shape.
                "aspect_ratio": f"{window.width:g}:{window.height:g}",
                "elements": elements,
                "grid_options": {"columns": "full"},
            }
        )
    cards.extend(below)
    if unplaced:
        cards.append({"type": "entities", "entities": unplaced, "grid_options": {"columns": "full"}})
    return {
        "title": title,
        "path": path,
        "icon": icon,
        "type": "sections",
        "max_columns": 1,
        "sections": [{"type": "grid", "cards": cards}],
    }


def _named_rows(entity_ids: Sequence[str]) -> list[dict[str, Any]]:
    """Build rows that show the entity's own name, without its device's name in front.

    The entities here share a device ("Scene", the main device); the
    default row name would repeat it on every line ("Scene Night").
    """
    return [{"entity": entity_id, "name": {"type": "entity"}} for entity_id in entity_ids]


def _press_tile(entity_id: str) -> dict[str, Any]:
    """Build a half-width tile that presses a button entity on a tap."""
    press = _perform("button.press", entity_id)
    return {
        "type": "tile",
        "entity": entity_id,
        # The entity's own name; its state is only when it was last pressed.
        "name": {"type": "entity"},
        "hide_state": True,
        "tap_action": press,
        "icon_tap_action": press,
        "grid_options": {"columns": 6},
    }


def _fan_tile(entity_id: str) -> dict[str, Any]:
    return {
        "type": "tile",
        "entity": entity_id,
        "features": [{"type": "fan-speed"}],
        "grid_options": {"columns": "full"},
    }


def _scenes_view(entities: DashboardEntities) -> dict[str, Any] | None:
    cards: list[dict[str, Any]] = []
    if entities.scenes:
        cards.append({"type": "entities", "title": "Scenes", "entities": _named_rows(entities.scenes)})
    if entities.switches:
        cards.append({"type": "entities", "title": "Switches", "entities": _named_rows(entities.switches)})
    if not cards:
        return None
    return {"title": "Scenes", "path": "scenes", "icon": "mdi:palette", "cards": cards}


def _markers(
    devices: Mapping[int, _Device],
    entity_ids: Mapping[int, str],
    centre_of: Callable[[int, int], Point],
    make: Callable[[str, _Device, Point, _Window], list[dict[str, Any]]],
    plan_id: int | None,
    image: PlanBackground | None,
    window: _Window | None,
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
        if on_plan and image is not None and window is not None and centre is not None and _inside(centre, image):
            elements.extend(make(entity_id, device, centre, window))
        else:
            unplaced.append(entity_id)
    return elements, unplaced


def _centres(
    plan_id: int | None,
    image: PlanBackground,
    lights: Mapping[int, LightConfig],
    blinds: Mapping[int, BlindConfig],
    zones: Mapping[int, ClimateZoneConfig],
) -> list[Point]:
    """Return the marker centres of a plan's devices that lie inside its image."""
    kinds: tuple[tuple[Mapping[int, LightConfig | BlindConfig | ClimateZoneConfig], Callable[[int, int], Point]], ...]
    kinds = ((lights, light_centre), (blinds, blind_centre), (zones, climate_centre))
    centres = [
        centre_of(device.left, device.top)
        for devices, centre_of in kinds
        for device in devices.values()
        if device.plan_id == plan_id and device.left is not None and device.top is not None
    ]
    return [centre for centre in centres if _inside(centre, image)]


def build_dashboard(
    *,
    plans: Mapping[int, PlanConfig],
    images: Mapping[int, PlanBackground],
    lights: Mapping[int, LightConfig],
    blinds: Mapping[int, BlindConfig],
    zones: Mapping[int, ClimateZoneConfig],
    entities: DashboardEntities,
    temperature_unit: str,
    protected_lights: Collection[int] = (),
) -> dict[str, Any]:
    """Build the dashboard configuration for the given devices and entities.

    A device appears on the plan named in its config when that plan's
    image is known and the device's marker centre lies inside it. Every
    other device with an entity (no or unreadable position, no image yet,
    centre outside the image) is listed below the plan with a built-in
    card, so it stays reachable; devices of an unknown plan are listed in
    the first plan's views. With several plans the views repeat per plan
    with the plan label in the title; with one plan the label is not shown.
    Below the plan of the first plan's views are the buttons for all
    lights, all blinds and all heating zones, and on the Climate view the
    ventilation. The Scenes view and the Intercom view follow the plan views.
    Without any entity the dashboard has one view that says so. A light or
    socket whose vendor id is in ``protected_lights`` shows a lock and has
    no tap action; a long press opens its details.
    """
    plan_ids: list[int | None] = [*sorted(plans)] or [None]
    several_plans = len(plan_ids) > 1
    views: list[dict[str, Any]] = []
    for plan_id in plan_ids:
        image = images.get(plan_id) if plan_id is not None else None
        label = f" {plans[plan_id].label}" if several_plans and plan_id is not None else ""
        path = f"-{plan_id}" if several_plans else ""
        strays = plan_id == plan_ids[0]
        # One window for the plan's three views: the plan keeps its size from tab to tab.
        window = _window(image, _centres(plan_id, image, lights, blinds, zones)) if image is not None else None
        # The cards that are not tied to a plan are on the first plan's views only.
        kinds = (
            (
                "Lights",
                "lights",
                "mdi:lightbulb-group",
                [_press_tile(button) for button in entities.light_buttons] if strays else [],
                _markers(
                    lights,
                    entities.lights,
                    light_centre,
                    lambda entity_id, light, centre, window: _light_elements(
                        entity_id, centre, window, protected=light.id in protected_lights
                    ),
                    plan_id,
                    image,
                    window,
                    take_strays=strays,
                    plans=plans,
                ),
            ),
            (
                "Blinds",
                "blinds",
                "mdi:blinds",
                [_press_tile(button) for button in entities.blind_buttons] if strays else [],
                _markers(
                    blinds,
                    entities.blinds,
                    blind_centre,
                    _blind_elements,
                    plan_id,
                    image,
                    window,
                    take_strays=strays,
                    plans=plans,
                ),
            ),
            (
                "Climate",
                "climate",
                "mdi:thermostat",
                [
                    *(_press_tile(button) for button in entities.climate_buttons),
                    *(_fan_tile(fan) for fan in entities.fans),
                ]
                if strays
                else [],
                _markers(
                    zones,
                    entities.climates,
                    climate_centre,
                    lambda entity_id, _zone, centre, window: _climate_elements(
                        entity_id, centre, window, temperature_unit
                    ),
                    plan_id,
                    image,
                    window,
                    take_strays=strays,
                    plans=plans,
                ),
            ),
        )
        views.extend(
            _view(f"{title}{label}", f"{slug}{path}", icon, image, window, elements, unplaced, below)
            for title, slug, icon, below, (elements, unplaced) in kinds
            if elements or unplaced or below
        )

    views.extend(
        view for view in (_scenes_view(entities), intercom_view(entities.doors, entities.cameras)) if view is not None
    )
    if not views:
        # The frontend opens a dashboard without views in edit mode, and this one cannot be edited.
        views.append(_WAITING_VIEW)
    return {"title": "Smart Place", "views": views}
