"""Button platform for Smart Place — door openers.

The four ``OEFFNER<n>`` commands map to physical doors at the
property: front door, ground-floor entrance, garage, mailbox. They're
write-only: the Smart Place SPA fires them as text frames over the
same app WebSocket. Per the ``smart-place-observe-only`` memory, the
client library never sends them unprompted — pressing the HA button
is the explicit, user-driven trigger.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from homeassistant.components.button import ButtonEntity

from . import SmartPlaceConfigEntry, SmartPlaceData, category_device_info
from .entity import SmartPlacePushEntity
from .smart_place_client import CommandDefinition, Commands

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

PARALLEL_UPDATES = 0


@dataclass(frozen=True, slots=True)
class _DoorButton:
    """Static definition of one door-opener button."""

    key: str
    name: str
    command: CommandDefinition


_DOORS: Final[tuple[_DoorButton, ...]] = (
    _DoorButton(key="front_door", name="Front door", command=Commands.OpenFrontDoor),
    _DoorButton(
        key="ground_floor_entrance",
        name="Ground floor entrance",
        command=Commands.OpenGroundFloorEntrance,
    ),
    _DoorButton(key="garage_entrance", name="Garage entrance", command=Commands.OpenGarageEntrance),
    _DoorButton(key="mailbox", name="Mailbox", command=Commands.OpenMailbox),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartPlaceConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Always create one button per known door — no discovery needed."""
    data = entry.runtime_data
    async_add_entities(SmartPlaceDoorButton(entry, data, door) for door in _DOORS)


class SmartPlaceDoorButton(SmartPlacePushEntity, ButtonEntity):
    """One-shot button that sends an OEFFNER<n> command to the SPA.

    Availability (``is_healthy``) and its push subscription come from
    ``SmartPlacePushEntity`` — without subscribing, the button would never
    learn the WS dropped (or recovered) and could show stale availability.
    The dedup collapses the broadcast stream to connect/disconnect edges,
    since a button has no frame-driven state of its own.
    """

    _attr_has_entity_name = True
    # Disabled by default — pressing one of these opens a real physical
    # door, mailbox, or garage. Users can enable per-button in the HA
    # entity registry once they've confirmed the label matches the
    # door on their property.
    _attr_entity_registry_enabled_default = False

    def __init__(self, entry: SmartPlaceConfigEntry, data: SmartPlaceData, door: _DoorButton) -> None:
        """Wire the door's name, unique_id, and write command."""
        self._data = data
        self._door = door
        self._attr_name = door.name
        self._attr_unique_id = f"{entry.entry_id}_door_{door.key}"
        self._attr_device_info = category_device_info(entry, data, "Doors")

    async def async_press(self) -> None:
        """Send the door's OEFFNER command on the live WS.

        Issued via ``client.issue`` so the actuation lands in the audit
        trail (door openers are ``audit=True``). A press that cannot be
        sent raises, so the caller does not take it for an opened door.
        """
        await self._async_issue(self._door.command)
