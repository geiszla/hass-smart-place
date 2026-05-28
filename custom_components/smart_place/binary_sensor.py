"""WebSocket connection health binary_sensor for Smart Place."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory

from smart_place_client import SessionPhase

from . import SmartPlaceData
from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback


_HEALTHY_PHASES = frozenset(
    {
        SessionPhase.APP_OPEN,
        SessionPhase.BOOTSTRAPPED,
        SessionPhase.READY,
    },
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the connectivity binary_sensor."""
    data: SmartPlaceData = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([SmartPlaceConnectionSensor(entry, data)])


class SmartPlaceConnectionSensor(BinarySensorEntity):
    """Reports whether the Smart Place WebSocket is in a healthy phase."""

    _attr_has_entity_name = True
    _attr_name = "Connection"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, data: SmartPlaceData) -> None:
        """Initialise the binary_sensor wired to the live client state."""
        self._data = data
        self._attr_unique_id = f"{entry.entry_id}_connection"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Smart Place",
            manufacturer="smart PLACE AG",
        )

    @property
    def is_on(self) -> bool:
        """Return True iff the session is in a healthy phase."""
        return self._data.client.state.phase in _HEALTHY_PHASES

    async def async_added_to_hass(self) -> None:
        """Subscribe to client frame events so state pushes to HA."""
        self._data.listeners.append(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        """Drop our state-push subscription."""
        with contextlib.suppress(ValueError):
            self._data.listeners.remove(self.async_write_ha_state)
