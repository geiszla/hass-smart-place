"""Image platform for Smart Place — the floor plan images.

The client fetches each plan's PNG after every main-menu reply and again
when the server reports a rotation. One entity per plan serves the cached
bytes; its timestamp only moves when the bytes change. The plan dashboard
uses the entity as the background of its picture cards.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable
from functools import partial
from typing import TYPE_CHECKING

from homeassistant.components.image import ImageEntity
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.util import dt as dt_util

from . import SmartPlaceConfigEntry, SmartPlaceData, main_device_info
from .entity import SmartPlacePushEntity, async_add_discovered

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
    """Add an image for every floor plan of the main-menu reply."""
    data = entry.runtime_data

    def collect() -> dict[Hashable, Callable[[], Entity]]:
        if not data.devices_listed:
            return {}
        return {plan_id: partial(SmartPlacePlanImage, hass, entry, data, plan_id) for plan_id in data.state.plans}

    async_add_discovered(entry, async_add_entities, collect)


class SmartPlacePlanImage(SmartPlacePushEntity, ImageEntity):
    """One floor plan image.

    Suffixed by id only when more than one plan is known when the entity
    is created. The plan label is not used as a name: it can be an
    installation tag.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_content_type = "image/png"
    _attr_translation_key = "floor_plan"

    def __init__(self, hass: HomeAssistant, entry: SmartPlaceConfigEntry, data: SmartPlaceData, plan_id: int) -> None:
        """Wire the plan's id, name and unique_id."""
        ImageEntity.__init__(self, hass)
        self._data = data
        self._plan_id = plan_id
        self._digest: str | None = None
        suffix = f" {plan_id}" if len(data.state.plans) > 1 else ""
        self._attr_name = f"Floor plan{suffix}"
        self._attr_unique_id = f"{entry.entry_id}_floorplan_{plan_id}"
        self._attr_device_info = main_device_info(entry)
        self._async_adopt_image()

    @property
    def available(self) -> bool:
        """Available once the image was fetched; it does not need the connection after that."""
        return self._plan_id in self._data.client.plan_images

    async def async_image(self) -> bytes | None:
        """Return the cached PNG."""
        image = self._data.client.plan_images.get(self._plan_id)
        return image.content if image is not None else None

    @callback
    def _async_adopt_image(self) -> None:
        """Move the timestamp when the client holds different bytes than last time."""
        image = self._data.client.plan_images.get(self._plan_id)
        if image is not None and image.digest != self._digest:
            self._digest = image.digest
            self._attr_image_last_updated = dt_util.utcnow()

    @callback
    def _handle_frame_update(self) -> None:
        self._async_adopt_image()
        super()._handle_frame_update()
