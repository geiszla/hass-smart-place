"""Switch platform for Smart Place — sockets, on/off scenes and the doorbell mute.

- A light of kind ``steckdose`` is an outlet switch on its own device, so
  it stays out of light groups and "all lights off".
- An ``OnOff`` scene has a real state (``SZENEN<id>:00|01``) and becomes a
  switch on the "Scenes" device — unless it is one level of a level group,
  which is a fan (``fan.py``).
- ``MUTE<n>`` is the doorbell mute, on the "Intercom" device.

All three only have a toggle command, so on/off is decided against the
effective state — see ``SmartPlaceToggleEntity``.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable
from functools import partial
from typing import TYPE_CHECKING

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity

from . import SmartPlaceConfigEntry, SmartPlaceData, category_device_info, light_device_info
from .entity import SmartPlaceToggleEntity, async_add_discovered, async_classify_scenes
from .smart_place_client import LIGHT_KIND_SOCKET, CommandDefinition, Commands, LightConfig, SceneConfig, light_is_on

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
    """Add a switch per socket, per on/off scene outside a level group and per doorbell mute."""
    data = entry.runtime_data
    state = data.state

    def collect() -> dict[Hashable, Callable[[], Entity]]:
        found: dict[Hashable, Callable[[], Entity]] = {
            ("mute", mute_id): partial(SmartPlaceMuteSwitch, entry, data, mute_id) for mute_id in state.mutes
        }
        if not data.devices_listed:
            return found
        for light in state.lights.values():
            if light.kind == LIGHT_KIND_SOCKET:
                found["socket", light.id] = partial(SmartPlaceSocket, entry, data, light)
        for scene_id in async_classify_scenes(entry, data).switches:
            found["scene", scene_id] = partial(SmartPlaceSceneSwitch, entry, data, state.scene_configs[scene_id])
        return found

    async_add_discovered(entry, async_add_entities, collect)


class SmartPlaceSocket(SmartPlaceToggleEntity, SwitchEntity):
    """A switched socket."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_device_class = SwitchDeviceClass.OUTLET

    def __init__(self, entry: SmartPlaceConfigEntry, data: SmartPlaceData, light: LightConfig) -> None:
        """Wire the socket's id, unique_id and device."""
        super().__init__()
        self._data = data
        self._light_id = light.id
        self._attr_unique_id = f"{entry.entry_id}_socket_{light.id}"
        self._attr_device_info = light_device_info(entry, data, light)

    def _server_is_on(self) -> bool | None:
        level = self._data.state.light_levels.get(self._light_id)
        return None if level is None else light_is_on(level)

    def _toggle_command(self) -> tuple[CommandDefinition, int]:
        return Commands.LightToggle, self._light_id


class SmartPlaceSceneSwitch(SmartPlaceToggleEntity, SwitchEntity):
    """An on/off scene."""

    _attr_has_entity_name = True
    _attr_translation_key = "scene_switch"

    def __init__(self, entry: SmartPlaceConfigEntry, data: SmartPlaceData, scene: SceneConfig) -> None:
        """Wire the scene's id, name and unique_id."""
        super().__init__()
        self._data = data
        self._scene_id = scene.id
        self._attr_name = scene.name
        self._attr_unique_id = f"{entry.entry_id}_scene_switch_{scene.id}"
        self._attr_device_info = category_device_info(entry, data, "Scenes")

    def _server_is_on(self) -> bool | None:
        return self._data.state.scene_states.get(self._scene_id)

    def _toggle_command(self) -> tuple[CommandDefinition, int]:
        return Commands.Scene, self._scene_id


class SmartPlaceMuteSwitch(SmartPlaceToggleEntity, SwitchEntity):
    """The doorbell mute; on means muted.

    Suffixed by id only when more than one mute is known when the entity
    is created (same rule as the intercom sensors).
    """

    _attr_has_entity_name = True
    _attr_translation_key = "doorbell_mute"

    def __init__(self, entry: SmartPlaceConfigEntry, data: SmartPlaceData, mute_id: int) -> None:
        """Wire the mute's id, name and unique_id."""
        super().__init__()
        self._data = data
        self._mute_id = mute_id
        suffix = f" {mute_id}" if len(data.state.mutes) > 1 else ""
        self._attr_name = f"Doorbell mute{suffix}"
        self._attr_unique_id = f"{entry.entry_id}_mute_{mute_id}"
        self._attr_device_info = category_device_info(entry, data, "Intercom")

    def _server_is_on(self) -> bool | None:
        return self._data.state.mutes.get(self._mute_id)

    def _toggle_command(self) -> tuple[CommandDefinition, int]:
        return Commands.MuteToggle, self._mute_id
