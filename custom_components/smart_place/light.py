"""Light platform for Smart Place — one entity per light the server lists.

``INHALTLeuchten<id>`` describes a light, ``leuchte<id>:<level>`` carries
its level (0 = off, above 0 = on). Kinds ``schalter`` and ``led`` are plain
on/off lights; ``dimmer`` adds brightness. Kind ``steckdose`` (socket) is a
switch entity instead (``switch.py``), so "all lights off" never cuts socket
power.

The vendor's only on/off command is a toggle (``leuchte<id>``), so turning
on or off is decided against the effective state — see
``SmartPlaceToggleEntity``. A dimmer also takes an absolute level
(``DIMleuchte<id>:<0..255>``). No dimmer exists in the reference
installation: the dimmer entity follows the vendor JavaScript and is
untested against a real device.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable
from functools import partial
from typing import TYPE_CHECKING, Any

from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity

from . import SmartPlaceConfigEntry, SmartPlaceData, light_device_info
from .entity import SmartPlaceToggleEntity, async_add_discovered
from .smart_place_client import (
    LIGHT_KIND_DIMMER,
    LIGHT_KIND_SOCKET,
    CommandDefinition,
    Commands,
    ExpectedState,
    LightConfig,
    light_is_on,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity import Entity
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartPlaceConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a light for every light of the main-menu reply that is not a socket."""
    data = entry.runtime_data

    def collect() -> dict[Hashable, Callable[[], Entity]]:
        if not data.devices_listed:
            return {}
        return {
            light.id: partial(
                SmartPlaceDimmer if light.kind == LIGHT_KIND_DIMMER else SmartPlaceLight,
                entry,
                data,
                light,
            )
            for light in data.state.lights.values()
            if light.kind != LIGHT_KIND_SOCKET
        }

    async_add_discovered(entry, async_add_entities, collect)


class SmartPlaceLight(SmartPlaceToggleEntity, LightEntity):
    """An on/off light."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_color_mode = ColorMode.ONOFF

    def __init__(self, entry: SmartPlaceConfigEntry, data: SmartPlaceData, light: LightConfig) -> None:
        """Wire the light's id, unique_id and device."""
        super().__init__()
        self._data = data
        self._light_id = light.id
        self._attr_supported_color_modes = {ColorMode.ONOFF}
        self._attr_unique_id = f"{entry.entry_id}_light_{light.id}"
        self._attr_device_info = light_device_info(entry, data, light)

    def _server_is_on(self) -> bool | None:
        level = self._data.state.light_levels.get(self._light_id)
        return None if level is None else light_is_on(level)

    def _toggle_command(self) -> tuple[CommandDefinition, int]:
        return Commands.LightToggle, self._light_id


class SmartPlaceDimmer(SmartPlaceLight):
    """A dimmable light (untested: built from the vendor JavaScript)."""

    _attr_color_mode = ColorMode.BRIGHTNESS

    def __init__(self, entry: SmartPlaceConfigEntry, data: SmartPlaceData, light: LightConfig) -> None:
        """Add the expected level to the on/off light."""
        super().__init__(entry, data, light)
        self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
        self._expected_level: ExpectedState[int] = ExpectedState()

    @property
    def brightness(self) -> int | None:
        """Return the effective level; the vendor scale is 0..255 like HA's."""
        return self._expected_level.resolve(self._data.state.light_levels.get(self._light_id))

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Set an absolute level if one is asked for, else toggle on."""
        level = kwargs.get(ATTR_BRIGHTNESS)
        if level is None:
            await super().async_turn_on(**kwargs)
            return
        await self._async_issue_expecting(
            Commands.LightDim,
            (self._light_id, level),
            (self._expected_level, level, self._data.state.light_levels.get(self._light_id)),
            (self._expected_on, True, self._server_is_on()),
        )
