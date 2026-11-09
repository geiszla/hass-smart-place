"""Typed device descriptions folded from Smart Place config and value frames.

One frozen dataclass per controllable family (lights and sockets, blinds,
climate zones, scenes, sub-menus, floor plans) plus the parsers that build
them from the comma-delimited config frames of the ``GiveMeMainmenu``
reply. Pure data: no I/O, no HA imports.

Field order on the wire is ``name,top,left,...`` (vertical position first),
verified in the 2026-11-09 captures and the vendor JavaScript. Positions are
page pixels (``289px``); a field that does not read as ``<n>px`` becomes
``None`` so consumers can treat the device as "not placed on the plan".
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from .protocol import NamedFields

LIGHT_KIND_SWITCH: Final = "schalter"
LIGHT_KIND_DIMMER: Final = "dimmer"
LIGHT_KIND_SOCKET: Final = "steckdose"
LIGHT_KIND_LED: Final = "led"

BLIND_KIND_SLATTED: Final = "jalousie"
BLIND_KIND_FABRIC: Final = "jalousiestoff"
BLIND_KIND_AWNING: Final = "markise"

# ``OnOn`` scenes only trigger; ``OnOff`` scenes have a real on/off state.
SCENE_KIND_TRIGGER: Final = "OnOn"
SCENE_KIND_SWITCH: Final = "OnOff"

_SCENE_PIN_FIELD: Final = 11
_SCENE_PIN_REQUIRED: Final = "Pw"

# ``JALICO<n>:<a>-<b>`` reports one of these instead of a position while a
# weather alarm holds the blind.
_BLIND_ALARMS: Final[dict[str, str]] = {
    "regenalarm": "rain",
    "windalarm": "wind",
    "hagelalarm": "hail",
}

_PX_RE = re.compile(r"^(-?\d+)px$")
_PLAN_RE = re.compile(r"^Uebersicht(\d+)$")

# ClimateConfig names look like "Bedroom heating"; the room is the name
# without the trailing "heating" / "Heizung" tag the vendor app appends.
_CLIMATE_ROOM_TAIL_RE = re.compile(r"\s+(?:heating|Heizung)\s*$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class LightConfig:
    """One ``INHALTLeuchten<id>`` light or socket."""

    id: int
    name: str
    kind: str
    top: int | None
    left: int | None
    plan_id: int | None


@dataclass(frozen=True, slots=True)
class BlindConfig:
    """One ``INHALTJalousien<id>`` blind."""

    id: int
    name: str
    kind: str
    top: int | None
    left: int | None
    plan_id: int | None
    runtime_s: int | None


@dataclass(frozen=True, slots=True)
class ClimateZoneConfig:
    """One ``INHALTKlimas<id>`` climate zone.

    ``room`` is ``name`` without the trailing "heating" tag; a zone that
    serves several rooms joins them with ``/``.
    """

    id: int
    name: str
    room: str
    mode: str
    top: int | None
    left: int | None
    plan_id: int | None
    fan_coil: bool


@dataclass(frozen=True, slots=True)
class SceneConfig:
    """One ``INHALTSZENEN<id>`` scene."""

    id: int
    name: str
    kind: str
    top: int | None
    left: int | None
    pin_protected: bool


@dataclass(frozen=True, slots=True)
class SubMenuConfig:
    """One ``UnterMenuLeuchten<id>`` / ``UnterMenuJalousien<id>`` group button."""

    id: int
    name: str
    top: int | None
    left: int | None
    group_message: str
    plan_id: int | None


@dataclass(frozen=True, slots=True)
class PlanConfig:
    """One ``Floorplan<id>`` floor plan."""

    id: int
    label: str
    top: int | None
    left: int | None
    functions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BlindValue:
    """One ``JALICO<id>`` value.

    ``position`` is the vendor percentage (0 = fully up, 100 = fully down),
    ``None`` while ``alarm`` (``rain`` / ``wind`` / ``hail``) is set.
    ``flag`` is the raw second part (``00`` / ``01``), kept unparsed.
    """

    position: int | None
    alarm: str | None
    flag: str


def _px(raw: str) -> int | None:
    match = _PX_RE.match(raw)
    return int(match.group(1)) if match else None


def _plan_id(raw: str) -> int | None:
    match = _PLAN_RE.match(raw)
    return int(match.group(1)) if match else None


def _field(fields: tuple[str, ...], index: int) -> str:
    return fields[index] if len(fields) > index else ""


def climate_zone_room(name: str) -> str:
    """Drop the trailing ``heating`` tag so the name reads as a room."""
    return _CLIMATE_ROOM_TAIL_RE.sub("", name).strip()


def parse_light_config(frame: NamedFields) -> LightConfig | None:
    """Build a :class:`LightConfig` from a ``LightConfig`` frame, or ``None`` if unnamed."""
    fields = frame.fields
    if frame.index is None or not _field(fields, 0):
        return None
    return LightConfig(
        id=frame.index,
        name=fields[0],
        kind=_field(fields, 3),
        top=_px(_field(fields, 1)),
        left=_px(_field(fields, 2)),
        plan_id=_plan_id(_field(fields, 6)),
    )


def parse_blind_config(frame: NamedFields) -> BlindConfig | None:
    """Build a :class:`BlindConfig` from a ``BlindConfig`` frame, or ``None`` if unnamed."""
    fields = frame.fields
    if frame.index is None or not _field(fields, 0):
        return None
    runtime = _field(fields, 5)
    return BlindConfig(
        id=frame.index,
        name=fields[0],
        kind=_field(fields, 3),
        top=_px(_field(fields, 1)),
        left=_px(_field(fields, 2)),
        plan_id=_plan_id(_field(fields, 6)),
        runtime_s=int(runtime) if runtime.isdigit() else None,
    )


def parse_climate_config(frame: NamedFields) -> ClimateZoneConfig | None:
    """Build a :class:`ClimateZoneConfig` from a ``ClimateConfig`` frame, or ``None`` if unnamed."""
    fields = frame.fields
    if frame.index is None or not _field(fields, 0):
        return None
    return ClimateZoneConfig(
        id=frame.index,
        name=fields[0],
        room=climate_zone_room(fields[0]),
        mode=_field(fields, 3),
        top=_px(_field(fields, 1)),
        left=_px(_field(fields, 2)),
        plan_id=_plan_id(_field(fields, 6)),
        fan_coil=_field(fields, 7) == "FanCoilOn",
    )


def parse_scene_config(frame: NamedFields) -> SceneConfig | None:
    """Build a :class:`SceneConfig` from a ``SceneConfig`` frame.

    Returns ``None`` for a frame too short to carry the type and PIN
    fields: without them the scene can neither be classified nor be known
    to be safe to trigger.
    """
    fields = frame.fields
    if frame.index is None or len(fields) <= _SCENE_PIN_FIELD or not fields[0]:
        return None
    return SceneConfig(
        id=frame.index,
        name=fields[0],
        kind=fields[4],
        top=_px(fields[1]),
        left=_px(fields[2]),
        pin_protected=fields[_SCENE_PIN_FIELD] == _SCENE_PIN_REQUIRED,
    )


def parse_sub_menu(frame: NamedFields) -> SubMenuConfig | None:
    """Build a :class:`SubMenuConfig` from a light or blind sub-menu frame."""
    fields = frame.fields
    if frame.index is None or not _field(fields, 0):
        return None
    return SubMenuConfig(
        id=frame.index,
        name=fields[0],
        top=_px(_field(fields, 1)),
        left=_px(_field(fields, 2)),
        group_message=_field(fields, 5),
        plan_id=_plan_id(_field(fields, 6)),
    )


def parse_plan_config(frame: NamedFields) -> PlanConfig | None:
    """Build a :class:`PlanConfig` from a ``Floorplan`` frame."""
    fields = frame.fields
    if frame.index is None or not fields:
        return None
    functions = _field(fields, 3)
    return PlanConfig(
        id=frame.index,
        label=fields[0],
        top=_px(_field(fields, 1)),
        left=_px(_field(fields, 2)),
        functions=tuple(functions.split("-")) if functions else (),
    )


def parse_blind_value(raw: str) -> BlindValue | None:
    """Parse the ``<a>-<b>`` payload of a ``JALICO<id>`` frame."""
    head, sep, flag = raw.rpartition("-")
    if not sep:
        return None
    if head in _BLIND_ALARMS:
        return BlindValue(position=None, alarm=_BLIND_ALARMS[head], flag=flag)
    if not head.isdigit():
        return None
    return BlindValue(position=int(head), alarm=None, flag=flag)
