"""Binary sensor platform for Smart Place.

Three families of entities live here:

- ``SmartPlaceConnectionSensor`` — diagnostic; tracks WS health.
- Alarm sensors derived from broadcast pushes (``Rain``, ``Hail``,
  ``BlindsMaintenance``, ``WindAlarm<N>``).
- Activity sensors derived from the ``GiveMeMainmenu`` state
  snapshot: per-scene "currently active" indicators (paired by
  ``SceneConfig`` name + ``SZENEN<N>`` value), plus per-group
  ``Any light on`` / ``Any blind closed`` rollups
  (``LEUCHTENZENTRAL<N>`` / ``JALZENTRAL<N>``).

Package boxes are exposed as ``sensor`` entities (not binary) since
the SPA's ``PACKETBOX<N>`` payload carries the package's unlock code
while occupied — see ``sensor.py``.
"""

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
    """Build binary sensors from the observation-window snapshot + diagnostics."""
    data: SmartPlaceData = hass.data[DOMAIN][entry.entry_id]
    state = data.state

    entities: list[BinarySensorEntity] = [SmartPlaceConnectionSensor(entry, data)]

    if state.rain_alarm is not None:
        entities.append(SmartPlaceRainSensor(entry, data))
    if state.hail_alarm is not None:
        entities.append(SmartPlaceHailSensor(entry, data))
    if state.blinds_maintenance is not None:
        entities.append(SmartPlaceBlindsMaintenanceSensor(entry, data))
    entities.extend(SmartPlaceWindAlarmSensor(entry, data, zone_id) for zone_id in sorted(state.wind_alarms))
    # Scenes only surface when both the name (SceneConfig) and a state
    # (SZENEN<N>) are present — without a name we'd ship an unlabeled
    # "Scene 9" entity, which isn't useful.
    entities.extend(
        SmartPlaceSceneSensor(entry, data, scene_id)
        for scene_id in sorted(state.scene_states)
        if scene_id in state.scenes
    )
    entities.extend(SmartPlaceAnyLightOnSensor(entry, data, group_id) for group_id in sorted(state.lights_central))
    entities.extend(SmartPlaceAnyBlindClosedSensor(entry, data, group_id) for group_id in sorted(state.blinds_central))

    async_add_entities(entities)


class _SmartPlaceBinarySensorBase(BinarySensorEntity):
    """Common wiring: shared device + push-update subscription."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, data: SmartPlaceData) -> None:
        """Wire the shared per-entry device + state-listener machinery."""
        self._data = data
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Smart Place",
            manufacturer="smart PLACE AG",
        )

    async def async_added_to_hass(self) -> None:
        """Push state updates to HA on every parsed WS frame."""
        self._data.listeners.append(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        """Drop the push-update subscription on entity removal."""
        with contextlib.suppress(ValueError):
            self._data.listeners.remove(self.async_write_ha_state)


class SmartPlaceConnectionSensor(_SmartPlaceBinarySensorBase):
    """Reports whether the Smart Place WebSocket is in a healthy phase."""

    _attr_name = "Connection"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, entry: ConfigEntry, data: SmartPlaceData) -> None:
        """Wire the entry-scoped unique_id."""
        super().__init__(entry, data)
        self._attr_unique_id = f"{entry.entry_id}_connection"

    @property
    def is_on(self) -> bool:
        """Return True iff the session is in a healthy phase."""
        return self._data.client.state.phase in _HEALTHY_PHASES


class SmartPlaceRainSensor(_SmartPlaceBinarySensorBase):
    """Rain alarm — on iff the SPA reports a non-zero REGEN code."""

    _attr_name = "Rain"
    _attr_device_class = BinarySensorDeviceClass.MOISTURE

    def __init__(self, entry: ConfigEntry, data: SmartPlaceData) -> None:
        """Wire the entry-scoped unique_id."""
        super().__init__(entry, data)
        self._attr_unique_id = f"{entry.entry_id}_rain"

    @property
    def is_on(self) -> bool | None:
        """Return True iff a rain alarm is currently active."""
        return self._data.state.rain_alarm


class SmartPlaceHailSensor(_SmartPlaceBinarySensorBase):
    """Hail alarm — on iff the SPA reports a non-zero HAGEL code."""

    _attr_name = "Hail"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, entry: ConfigEntry, data: SmartPlaceData) -> None:
        """Wire the entry-scoped unique_id."""
        super().__init__(entry, data)
        self._attr_unique_id = f"{entry.entry_id}_hail"

    @property
    def is_on(self) -> bool | None:
        """Return True iff a hail alarm is currently active."""
        return self._data.state.hail_alarm


class SmartPlaceBlindsMaintenanceSensor(_SmartPlaceBinarySensorBase):
    """Blinds-maintenance flag — on iff the SPA reports JALWARTUNG != 00."""

    _attr_name = "Blinds maintenance"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry, data: SmartPlaceData) -> None:
        """Wire the entry-scoped unique_id."""
        super().__init__(entry, data)
        self._attr_unique_id = f"{entry.entry_id}_blinds_maintenance"

    @property
    def is_on(self) -> bool | None:
        """Return True iff blinds maintenance mode is currently active."""
        return self._data.state.blinds_maintenance


class SmartPlaceWindAlarmSensor(_SmartPlaceBinarySensorBase):
    """Per-zone wind alarm — on iff WINDALARM<N> reports a non-zero code."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, entry: ConfigEntry, data: SmartPlaceData, zone_id: int) -> None:
        """Wire the per-zone name + unique_id."""
        super().__init__(entry, data)
        self._zone_id = zone_id
        self._attr_name = f"Wind alarm {zone_id}"
        self._attr_unique_id = f"{entry.entry_id}_wind_alarm_{zone_id}"

    @property
    def is_on(self) -> bool | None:
        """Return True iff the zone's wind alarm is currently active."""
        return self._data.state.wind_alarms.get(self._zone_id)


class SmartPlaceSceneSensor(_SmartPlaceBinarySensorBase):
    """Per-scene "currently active" indicator.

    The display name is read from ``SceneConfig`` via the ``name``
    property on each state write — so a late-arriving ``SceneConfig``
    swaps in the proper label without an HA reload.
    """

    _attr_icon = "mdi:palette"

    def __init__(self, entry: ConfigEntry, data: SmartPlaceData, scene_id: int) -> None:
        """Wire the per-scene unique_id; name is computed dynamically."""
        super().__init__(entry, data)
        self._scene_id = scene_id
        self._attr_unique_id = f"{entry.entry_id}_scene_{scene_id}"

    @property
    def name(self) -> str:
        """Return the scene's display name from ``SceneConfig``."""
        return self._data.state.scenes.get(self._scene_id) or f"Scene {self._scene_id}"

    @property
    def is_on(self) -> bool | None:
        """Return True iff the scene is currently active (``SZENEN<N>:01``)."""
        return self._data.state.scene_states.get(self._scene_id)


class SmartPlaceAnyLightOnSensor(_SmartPlaceBinarySensorBase):
    """Aggregate group flag: any light on for ``LEUCHTENZENTRAL<N>``."""

    _attr_icon = "mdi:lightbulb-group"

    def __init__(self, entry: ConfigEntry, data: SmartPlaceData, group_id: int) -> None:
        """Wire the per-group name + unique_id."""
        super().__init__(entry, data)
        self._group_id = group_id
        suffix = f" (group {group_id})" if group_id > 1 else ""
        self._attr_name = f"Any light on{suffix}"
        self._attr_unique_id = f"{entry.entry_id}_lights_central_{group_id}"

    @property
    def is_on(self) -> bool | None:
        """Return True iff at least one light in the group is on."""
        return self._data.state.lights_central.get(self._group_id)


class SmartPlaceAnyBlindClosedSensor(_SmartPlaceBinarySensorBase):
    """Aggregate group flag: any blind closed for ``JALZENTRAL<N>``.

    Best-effort: the SPA's ``JALZENTRAL<N>`` payload is often empty
    or ``"00"`` when no blinds are closed, and something else when
    at least one is. The exact semantics aren't fully documented;
    treat the ``True`` side as "blinds appear to be in a non-neutral
    state" rather than a strict closed/not-closed reading.
    """

    _attr_icon = "mdi:blinds-horizontal-closed"

    def __init__(self, entry: ConfigEntry, data: SmartPlaceData, group_id: int) -> None:
        """Wire the per-group name + unique_id."""
        super().__init__(entry, data)
        self._group_id = group_id
        suffix = f" (group {group_id})" if group_id > 1 else ""
        self._attr_name = f"Any blind closed{suffix}"
        self._attr_unique_id = f"{entry.entry_id}_blinds_central_{group_id}"

    @property
    def is_on(self) -> bool | None:
        """Return True iff at least one blind in the group appears closed."""
        return self._data.state.blinds_central.get(self._group_id)
