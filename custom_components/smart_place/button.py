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
import logging
from typing import TYPE_CHECKING, Final

from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.device_registry import DeviceInfo

from . import SmartPlaceData
from .const import DOMAIN
from .smart_place_client import CommandDefinition, Commands

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback


_LOGGER = logging.getLogger(__name__)


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
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Always create one button per known door — no discovery needed."""
    data: SmartPlaceData = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(SmartPlaceDoorButton(entry, data, door) for door in _DOORS)


class SmartPlaceDoorButton(ButtonEntity):
    """One-shot button that sends an OEFFNER<n> command to the SPA."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    # Disabled by default — pressing one of these opens a real physical
    # door, mailbox, or garage. Users can enable per-button in the HA
    # entity registry once they've confirmed the label matches the
    # door on their property.
    _attr_entity_registry_enabled_default = False

    def __init__(self, entry: ConfigEntry, data: SmartPlaceData, door: _DoorButton) -> None:
        """Wire the door's name, unique_id, and write command."""
        self._data = data
        self._door = door
        self._attr_name = door.name
        self._attr_unique_id = f"{entry.entry_id}_door_{door.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Smart Place",
            manufacturer="smart PLACE AG",
        )

    @property
    def available(self) -> bool:
        """Gray out the button while the WS isn't connected (press would no-op)."""
        return self._data.is_healthy

    async def async_press(self) -> None:
        """Send the door's OEFFNER command on the live WS."""
        try:
            await self._data.client.send(self._door.command.encode())
        except RuntimeError:
            # send() raises RuntimeError before the WS opens — log it
            # so the user sees the press happened but the connection
            # wasn't ready, instead of failing silently.
            _LOGGER.warning(
                "%s pressed before Smart Place WS was open; press ignored",
                self._door.name,
            )
