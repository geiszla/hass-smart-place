"""Scene platform for Smart Place — one entity per trigger scene.

An ``OnOn`` scene only triggers: activating it sends ``SZENEN<id>``. Its
``SZENEN<id>`` flag latches at ``01`` once the scene ran, so it is not a
state and is not exposed. On/off scenes are switches (``switch.py``) or
levels of a fan (``fan.py``); scenes protected by a PIN get no entity.
A scene the vendor app does not display gets its entity disabled by
default.

All scenes sit on the device "Scene", so Home Assistant's automatic
dashboard shows them on one card. The full name is then "<device> <scene>"
("Scene Night"); the voice assistant still gets the scene's own name as
the alias (``entity.async_keep_spoken_name``).
"""

from __future__ import annotations

from collections.abc import Callable, Hashable
from functools import partial
from typing import TYPE_CHECKING, Any

from homeassistant.components.scene import Scene
from homeassistant.core import callback

from . import SmartPlaceConfigEntry, SmartPlaceData, category_device_info
from .entity import SmartPlacePushEntity, async_add_discovered, async_classify_scenes, async_keep_spoken_name
from .smart_place_client import Commands, SceneConfig

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
    """Add a scene for every trigger scene of the main-menu reply."""
    data = entry.runtime_data

    def collect() -> dict[Hashable, Callable[[], Entity]]:
        if not data.devices_listed:
            return {}
        return {
            scene_id: partial(SmartPlaceScene, entry, data, data.state.scene_configs[scene_id])
            for scene_id in async_classify_scenes(entry, data).triggers
        }

    async_add_discovered(entry, async_add_entities, collect)


class SmartPlaceScene(SmartPlacePushEntity, Scene):
    """A trigger scene."""

    _attr_has_entity_name = True

    def __init__(self, entry: SmartPlaceConfigEntry, data: SmartPlaceData, scene: SceneConfig) -> None:
        """Wire the scene's id, name, unique_id and device."""
        self._data = data
        self._scene_id = scene.id
        self._attr_name = scene.name
        self._attr_unique_id = f"{entry.entry_id}_scene_{scene.id}"
        self._attr_device_info = category_device_info(entry, data, "Scenes")
        # A scene the vendor app does not show is not meant for daily use.
        self._attr_entity_registry_enabled_default = scene.displayed

    async def async_added_to_hass(self) -> None:
        """Keep the scene's own name as what the voice assistant listens for."""
        await super().async_added_to_hass()
        async_keep_spoken_name(self, self._data.state.scene_configs[self._scene_id].name)

    @callback
    def async_entity_id_changed(self, old_entity_id: str) -> None:
        """Opt in to in-place entity_id changes: nothing here is keyed on the entity_id."""
        super().async_entity_id_changed(old_entity_id)

    async def async_activate(self, **kwargs: Any) -> None:
        """Trigger the scene."""
        await self._async_issue(Commands.Scene, self._scene_id)
