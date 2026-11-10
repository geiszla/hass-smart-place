"""Fan platform for Smart Place — one entity per level group of scenes.

On/off scenes named alike except for a trailing number 1..N (N >= 3), such
as "Lüftung Stufe 1/2/3", are the levels of one thing: the server keeps
one of them active. Each group is one fan with N speeds; selecting level k
sends ``SZENEN<id of level k>``.

There is no off: the fan declares neither turn-off nor a zero speed.
Whether sending the active level's scene again switches it off is
untested, so the active level is never sent again.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable
from functools import partial
import math
from typing import TYPE_CHECKING, Any

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util.percentage import percentage_to_ranged_value, ranged_value_to_percentage

from . import SmartPlaceConfigEntry, SmartPlaceData, level_group_device_info
from .const import DOMAIN
from .entity import SmartPlacePushEntity, async_add_discovered, async_classify_scenes
from .smart_place_client import Commands, ExpectedState, LevelGroup

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
    """Add a fan for every level group among the scenes of the main-menu reply."""
    data = entry.runtime_data

    def collect() -> dict[Hashable, Callable[[], Entity]]:
        if not data.devices_listed:
            return {}
        return {
            group.scene_ids[0]: partial(SmartPlaceLevelFan, entry, data, group)
            for group in async_classify_scenes(entry, data).level_groups
        }

    async_add_discovered(entry, async_add_entities, collect)


class SmartPlaceLevelFan(SmartPlacePushEntity, FanEntity):
    """A level group: N on/off scenes of which the server keeps one active."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_features = FanEntityFeature.SET_SPEED | FanEntityFeature.TURN_ON

    def __init__(self, entry: SmartPlaceConfigEntry, data: SmartPlaceData, group: LevelGroup) -> None:
        """Wire the group's scenes, unique_id and device.

        The group gets a device of its own named after it; the entity
        takes that name.
        """
        self._data = data
        self._scene_ids = group.scene_ids
        self._attr_speed_count = len(group.scene_ids)
        self._attr_unique_id = f"{entry.entry_id}_level_group_{group.scene_ids[0]}"
        self._attr_device_info = level_group_device_info(entry, data, group)
        self._expected_level: ExpectedState[int] = ExpectedState()

    def _server_level(self) -> int | None:
        """Return the highest active level, 0 if none is active, ``None`` if unknown."""
        flags = [self._data.state.scene_states.get(scene_id) for scene_id in self._scene_ids]
        if all(flag is None for flag in flags):
            return None
        return max((level for level, on in enumerate(flags, start=1) if on), default=0)

    def _level(self) -> int | None:
        return self._expected_level.resolve(self._server_level())

    @property
    def is_on(self) -> bool | None:
        """Return whether a level is active, ``None`` while unknown."""
        level = self._level()
        return None if level is None else level > 0

    @property
    def percentage(self) -> int | None:
        """Return the active level as a percentage."""
        level = self._level()
        if not level:
            return level
        return ranged_value_to_percentage((1, self.speed_count), level)

    async def async_set_percentage(self, percentage: int) -> None:
        """Select the level that matches ``percentage``; zero is refused."""
        if percentage == 0:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_off_level")
        await self._async_select(math.ceil(percentage_to_ranged_value((1, self.speed_count), percentage)))

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Select the asked level; without one, level 1 if no level is active."""
        if percentage is not None:
            await self.async_set_percentage(percentage)
        elif self._known_level() == 0:
            await self._async_select(1)

    def _known_level(self) -> int:
        level = self._level()
        if level is None:
            # Sending the scene of the level that is already active may switch it off.
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="state_unknown")
        return level

    async def _async_select(self, level: int) -> None:
        if level == self._known_level():
            return
        await self._async_issue(Commands.Scene, self._scene_ids[level - 1])
        self._async_expect(self._expected_level, level)
