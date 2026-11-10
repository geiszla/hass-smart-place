"""Cover platform for Smart Place — one entity per blind the server lists.

``INHALTJalousien<id>`` describes a blind, ``JALICO<id>:<a>-<b>`` carries
its position (vendor percent, 0 = fully up) or a weather alarm. The vendor
commands are single shots: up (``JALUP``), down (``JALDOW``) and, for
slatted blinds only, vent (``JALLUE``). There is no stop and no
go-to-position command, so the entity offers open, close and — on slatted
blinds — tilt-open for vent.

The position is the server's own time-based estimate, hence
``assumed_state``: Home Assistant then keeps both direction buttons
enabled whatever the reported position.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable
from functools import partial
import time
from typing import TYPE_CHECKING, Any

from homeassistant.components.cover import CoverDeviceClass, CoverEntity, CoverEntityFeature
from homeassistant.core import callback
from homeassistant.helpers.event import async_call_later

from . import SmartPlaceConfigEntry, SmartPlaceData, blind_device_info
from .entity import SmartPlacePushEntity, async_add_discovered
from .smart_place_client import (
    BLIND_KIND_AWNING,
    BLIND_KIND_FABRIC,
    BLIND_KIND_SLATTED,
    BLIND_MOTION_TIMEOUT,
    BlindConfig,
    BlindMotion,
    BlindValue,
    Commands,
    cover_position,
)

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.core import CALLBACK_TYPE, HomeAssistant
    from homeassistant.helpers.entity import Entity
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

PARALLEL_UPDATES = 0

_DEVICE_CLASSES: dict[str, CoverDeviceClass] = {
    BLIND_KIND_SLATTED: CoverDeviceClass.BLIND,
    BLIND_KIND_FABRIC: CoverDeviceClass.SHADE,
    BLIND_KIND_AWNING: CoverDeviceClass.AWNING,
}

# HA's scene restore calls open_cover and then open_cover_tilt for a cover
# stored as open (cover/reproduce_state.py). Sent to the vendor that would
# be "up" followed by "vent", which stops the blind at the vent position.
# A tilt-open this soon after an open is that second call and is ignored.
_OPEN_THEN_TILT_WINDOW = 2.0

# The timer may fire a moment before the motion tracker's own deadline.
_MOTION_TIMER_MARGIN = 0.1

_FULLY_CLOSED = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartPlaceConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a cover for every blind of the main-menu reply."""
    data = entry.runtime_data

    def collect() -> dict[Hashable, Callable[[], Entity]]:
        if not data.devices_listed:
            return {}
        return {blind.id: partial(SmartPlaceCover, entry, data, blind) for blind in data.state.blinds.values()}

    async_add_discovered(entry, async_add_entities, collect)


class SmartPlaceCover(SmartPlacePushEntity, CoverEntity):
    """A blind: slatted, fabric or awning."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_assumed_state = True

    def __init__(self, entry: SmartPlaceConfigEntry, data: SmartPlaceData, blind: BlindConfig) -> None:
        """Wire the blind's id, unique_id, device and features."""
        self._data = data
        self._blind_id = blind.id
        self._attr_unique_id = f"{entry.entry_id}_blind_{blind.id}"
        self._attr_device_info = blind_device_info(entry, data, blind)
        self._attr_device_class = _DEVICE_CLASSES.get(blind.kind)
        # Set explicitly: HA would otherwise add stop and set-position
        # because a position is reported.
        features = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE
        if blind.kind == BLIND_KIND_SLATTED:
            features |= CoverEntityFeature.OPEN_TILT
        self._attr_supported_features = features
        self._motion = BlindMotion()
        self._motion_timer: CALLBACK_TYPE | None = None
        self._opened_at: float | None = None

    def _value(self) -> BlindValue | None:
        return self._data.state.blind_values.get(self._blind_id)

    @property
    def current_cover_position(self) -> int | None:
        """Return the position (0 = closed), ``None`` if unknown or held by a weather alarm."""
        value = self._value()
        if value is None or value.position is None:
            return None
        return cover_position(value.position)

    @property
    def is_closed(self) -> bool | None:
        """Return whether the blind is fully down, ``None`` without a position."""
        position = self.current_cover_position
        return None if position is None else position == _FULLY_CLOSED

    @property
    def is_opening(self) -> bool:
        """Return whether the last positions moved up."""
        return self._motion.direction == "opening"

    @property
    def is_closing(self) -> bool:
        """Return whether the last positions moved down."""
        return self._motion.direction == "closing"

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        """Expose the weather alarm (``rain`` / ``wind`` / ``hail``) holding the blind, if any."""
        value = self._value()
        return {"weather_alarm": value.alarm if value is not None else None}

    @callback
    def _handle_frame_update(self) -> None:
        """Feed the position to the motion tracker, then write if anything changed."""
        if self._motion.update(self.current_cover_position):
            # The server says nothing when a blind stops; look again after the timeout.
            self._async_cancel_motion_timer()
            self._motion_timer = async_call_later(
                self.hass,
                BLIND_MOTION_TIMEOUT + _MOTION_TIMER_MARGIN,
                self._async_motion_timed_out,
            )
        super()._handle_frame_update()

    @callback
    def _async_motion_timed_out(self, _now: datetime) -> None:
        self._motion_timer = None
        self._handle_frame_update()

    @callback
    def _async_cancel_motion_timer(self) -> None:
        if self._motion_timer is not None:
            self._motion_timer()
            self._motion_timer = None

    async def async_added_to_hass(self) -> None:
        """Seed the motion tracker so the first position change already has a direction."""
        await super().async_added_to_hass()
        self._motion.update(self.current_cover_position)

    async def async_will_remove_from_hass(self) -> None:
        """Cancel the motion timer alongside the frame subscription."""
        await super().async_will_remove_from_hass()
        self._async_cancel_motion_timer()

    @callback
    def async_entity_id_changed(self, old_entity_id: str) -> None:
        """Opt in to in-place entity_id changes: the timer is not keyed on the entity_id."""
        super().async_entity_id_changed(old_entity_id)

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Move the blind up."""
        await self._async_issue(Commands.BlindUp, self._blind_id)
        self._opened_at = time.monotonic()

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Move the blind down."""
        await self._async_issue(Commands.BlindDown, self._blind_id)

    async def async_open_cover_tilt(self, **kwargs: Any) -> None:
        """Move a slatted blind to its vent position."""
        if self._opened_at is not None and time.monotonic() - self._opened_at < _OPEN_THEN_TILT_WINDOW:
            return
        await self._async_issue(Commands.BlindVent, self._blind_id)
