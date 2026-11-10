"""Climate platform for Smart Place — one entity per heating zone.

``INHALTKlimas<id>`` describes a zone, ``TEMPIST<id>`` carries the
measured temperature, ``TEMPSOLL<id>`` the setpoint (whole degrees 18..26)
and ``KLIMASINFO<id>`` whether the zone calls for heat. The one command is
the absolute setpoint ``TEMPSOLL<id>:<value>``.

The vendor app labels setpoint 18 "forced off". Here 18 is HVAC mode Off
and 19..26 are the target temperatures of mode Heat. Switching back to
Heat restores the setpoint the zone had before it was switched off.

The entity deliberately does not declare the turn-on / turn-off features:
a broad "turn off" (an area, a voice command for "everything") must not
switch the heating off.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any

from homeassistant.components.climate import ATTR_HVAC_MODE, ClimateEntity, ClimateEntityFeature, HVACAction, HVACMode
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity

from . import SmartPlaceConfigEntry, SmartPlaceData, zone_device_info
from .const import DOMAIN
from .entity import SmartPlacePushEntity, async_add_discovered
from .smart_place_client import (
    SETPOINT_DEFAULT,
    SETPOINT_MIN,
    Commands,
    ExpectedState,
    heating_is_off,
    setpoint_for_temperature,
    step_setpoint,
    target_temperature,
)
from .smart_place_client.commands import SETPOINT_MAX, SETPOINT_OFF

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity import Entity
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

PARALLEL_UPDATES = 0

# The server sends 0.0 for a sensor the building does not have (see sensor.py).
_PLACEHOLDER_READING = 0.0

_HVAC_ACTIONS: dict[str | None, HVACAction] = {
    "heizen": HVACAction.HEATING,
    "kuehlen": HVACAction.COOLING,
    None: HVACAction.IDLE,
}


@dataclass
class _StoredSetpoint(ExtraStoredData):
    """The setpoint to restore when heating is switched on again."""

    setpoint: int

    def as_dict(self) -> dict[str, Any]:
        """Return the stored data as a dict."""
        return {"setpoint": self.setpoint}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartPlaceConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a climate entity for every zone of the main-menu reply."""
    data = entry.runtime_data

    def collect() -> dict[Hashable, Callable[[], Entity]]:
        if not data.devices_listed:
            return {}
        return {zone_id: partial(SmartPlaceClimate, entry, data, zone_id) for zone_id in data.state.climate}

    async_add_discovered(entry, async_add_entities, collect)


class SmartPlaceClimate(SmartPlacePushEntity, ClimateEntity, RestoreEntity):
    """A heating zone."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE
    _attr_min_temp = SETPOINT_MIN
    _attr_max_temp = SETPOINT_MAX
    _attr_target_temperature_step = 1.0

    def __init__(self, entry: SmartPlaceConfigEntry, data: SmartPlaceData, zone_id: int) -> None:
        """Wire the zone's id, unique_id and device."""
        self._data = data
        self._zone_id = zone_id
        self._attr_unique_id = f"{entry.entry_id}_climate_{zone_id}"
        self._attr_device_info = zone_device_info(entry, data, zone_id)
        self._attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF]
        self._expected_setpoint: ExpectedState[int] = ExpectedState()
        # Last heating setpoint (19..26) seen from the server or sent from here.
        self._heating_setpoint = SETPOINT_DEFAULT

    def _setpoint(self) -> int | None:
        """Return the effective vendor setpoint, ``None`` if not received yet."""
        return self._expected_setpoint.resolve(self._data.state.setpoints.get(self._zone_id))

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Return Off for vendor setpoint 18, else Heat."""
        setpoint = self._setpoint()
        if setpoint is None:
            return None
        return HVACMode.OFF if heating_is_off(setpoint) else HVACMode.HEAT

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return whether the zone calls for heat right now."""
        setpoint = self._setpoint()
        info = self._data.state.climate_info
        if setpoint is None or self._zone_id not in info:
            return None
        if heating_is_off(setpoint):
            return HVACAction.OFF
        return _HVAC_ACTIONS.get(info[self._zone_id])

    @property
    def native_current_temperature(self) -> float | None:
        """Return the measured temperature, folding the 0.0 placeholder to unknown."""
        value = self._data.state.indoor_temperatures.get(self._zone_id)
        return None if value == _PLACEHOLDER_READING else value

    @property
    def native_target_temperature(self) -> float | None:
        """Return the target temperature, ``None`` while off."""
        setpoint = self._setpoint()
        return None if setpoint is None else target_temperature(setpoint)

    @property
    def extra_restore_state_data(self) -> _StoredSetpoint:
        """Keep the heating setpoint across restarts."""
        return _StoredSetpoint(self._heating_setpoint)

    @callback
    def _handle_frame_update(self) -> None:
        """Remember the server's heating setpoint, then write if anything changed."""
        setpoint = self._data.state.setpoints.get(self._zone_id)
        if setpoint is not None and not heating_is_off(setpoint):
            self._heating_setpoint = setpoint
        super()._handle_frame_update()

    async def async_added_to_hass(self) -> None:
        """Restore the heating setpoint remembered before the restart."""
        await super().async_added_to_hass()
        if (stored := await self.async_get_last_extra_data()) is not None:
            setpoint = stored.as_dict().get("setpoint")
            if isinstance(setpoint, int) and SETPOINT_MIN <= setpoint <= SETPOINT_MAX:
                self._heating_setpoint = setpoint
        # The server's current value is newer than anything stored.
        self._handle_frame_update()

    @callback
    def async_entity_id_changed(self, old_entity_id: str) -> None:
        """Opt in to in-place entity_id changes: nothing here is keyed on the entity_id."""
        super().async_entity_id_changed(old_entity_id)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set the target temperature, rounded to a whole degree.

        An ``hvac_mode`` given in the same call wins for the mode: with
        Off the temperature is only remembered for the next switch to Heat.
        """
        temperature = kwargs.get(ATTR_TEMPERATURE)
        setpoint = None if temperature is None else setpoint_for_temperature(temperature)
        if kwargs.get(ATTR_HVAC_MODE) == HVACMode.OFF:
            if setpoint is not None:
                self._heating_setpoint = setpoint
            await self._async_send_setpoint(SETPOINT_OFF)
        elif setpoint is not None:
            await self._async_send_setpoint(setpoint)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Switch heating off (vendor setpoint 18) or back on at the remembered setpoint."""
        if hvac_mode == HVACMode.OFF:
            await self._async_send_setpoint(SETPOINT_OFF)
            return
        setpoint = self._setpoint()
        if setpoint is None:
            # Without the current setpoint "heat" could overwrite a different one.
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="state_unknown")
        if heating_is_off(setpoint):
            await self._async_send_setpoint(self._heating_setpoint)

    async def async_adjust_setpoint(self, steps: int) -> None:
        """Move the setpoint ``steps`` along the vendor scale 18..26 (18 = Off)."""
        setpoint = self._setpoint()
        if setpoint is None:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="state_unknown")
        await self._async_send_setpoint(step_setpoint(setpoint, steps))

    async def _async_send_setpoint(self, setpoint: int) -> None:
        """Send a vendor setpoint unless it already is the effective one."""
        if setpoint == self._setpoint():
            return
        await self._async_issue(Commands.Setpoint, self._zone_id, setpoint)
        if not heating_is_off(setpoint):
            self._heating_setpoint = setpoint
        self._async_expect(self._expected_setpoint, setpoint)
