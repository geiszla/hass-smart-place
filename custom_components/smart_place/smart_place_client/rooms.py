"""Suggest the room of a light, socket or blind.

No Smart Place frame says which room a device is in. Two hints exist: the
device name usually contains the room ("Kitchen spot lights"), and every
device has a position on the floor plan. The heating zones are the only
things that are named after rooms, so a suggestion is a zone id:

1. Name: split each zone's room name at ``/`` ("Living room/dining
   room/kitchen" serves three rooms) and look for the parts in the device
   name, ignoring case. The zone with the longest part found wins.
2. Position: if no name matches and the device is placed on a plan, the
   zone of that plan whose marker is nearest — only when exactly one zone
   is nearest.
3. Otherwise there is no suggestion.

Pure functions, no HA imports. The caller turns the zone into an area name.
"""

from __future__ import annotations

from collections.abc import Iterable
import math

from .devices import ClimateZoneConfig
from .plan import Point, climate_centre


def _zone_by_name(name: str, zones: Iterable[ClimateZoneConfig]) -> int | None:
    """Return the zone whose longest room name occurs in ``name``, if exactly one does."""
    folded = name.casefold()
    best_length = 0
    best: list[int] = []
    for zone in zones:
        length = max(
            (
                len(part)
                for part in (room.strip().casefold() for room in zone.room.split("/"))
                if part and part in folded
            ),
            default=0,
        )
        if not length or length < best_length:
            continue
        if length > best_length:
            best_length, best = length, []
        best.append(zone.id)
    return best[0] if len(best) == 1 else None


def _zone_by_position(centre: Point, plan_id: int | None, zones: Iterable[ClimateZoneConfig]) -> int | None:
    """Return the zone of ``plan_id`` with the nearest marker, if exactly one is nearest."""
    best_distance = math.inf
    best: list[int] = []
    for zone in zones:
        if zone.plan_id != plan_id or zone.top is None or zone.left is None:
            continue
        distance = math.dist(centre, climate_centre(zone.left, zone.top))
        if distance > best_distance:
            continue
        if distance < best_distance:
            best_distance, best = distance, []
        best.append(zone.id)
    return best[0] if len(best) == 1 else None


def suggest_zone(
    name: str,
    centre: Point | None,
    plan_id: int | None,
    zones: Iterable[ClimateZoneConfig],
) -> int | None:
    """Return the id of the heating zone a device most likely belongs to.

    ``centre`` is the device's marker centre on plan ``plan_id`` (see
    :mod:`.plan`), or ``None`` when the device has no readable position.
    """
    zones = tuple(zones)
    zone_id = _zone_by_name(name, zones)
    if zone_id is None and centre is not None:
        zone_id = _zone_by_position(centre, plan_id, zones)
    return zone_id
