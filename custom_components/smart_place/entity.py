"""Shared base for Smart Place push entities.

The Smart Place server re-broadcasts the full sensor set roughly twice a
second even when nothing has changed, and ``__init__.py``'s
``_notify_listeners`` fans every parsed frame out to *all* entities. Writing
on every callback therefore re-reported each entity ~2x/s. Those writes fire
``state_reported`` events that flood the event bus and feed duplicate samples
into downstream consumers — most visibly a Statistics helper, whose rolling
buffer fills from the broadcast cadence instead of from real value changes.

``SmartPlacePushEntity`` subscribes to the same fan-out but compares a
signature of what HA would actually write and skips the write when it is
unchanged, so ``last_reported`` only advances on a genuine state or
availability change.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from homeassistant.core import callback
from homeassistant.helpers.entity import Entity

if TYPE_CHECKING:
    from . import SmartPlaceData

_UNSET: Any = object()


class SmartPlacePushEntity(Entity):
    """Frame-driven entity that only writes when its reported state changes."""

    _attr_should_poll = False
    _data: SmartPlaceData
    _sp_last_signature: Any = _UNSET

    @property
    def available(self) -> bool:
        """Unavailable while the Smart Place WebSocket is down."""
        return self._data.is_healthy

    def _report_signature(self) -> Any:
        """Snapshot of what HA would write, so unchanged frames are skipped.

        ``None`` stands for the unavailable state (HA writes ``unavailable``
        and drops the normal attributes then). When available, the resolved
        state plus the entity-supplied attributes capture every value or
        attribute change the subclasses expose. ``name`` is included because
        a few entities rename dynamically (e.g. a chart sensor when its
        ``ChartDefinition`` label arrives late) without any state change.
        """
        if not self.available:
            return None
        return (self.name, self.state, repr(self.extra_state_attributes))

    @callback
    def _handle_frame_update(self) -> None:
        """Write to HA only when the reported state actually changed.

        The signature is recorded *after* a successful write: if
        ``async_write_ha_state`` raises (caught + logged by the fan-out in
        ``__init__.py``), the unchanged signature means the next
        notification retries instead of skipping the value forever.
        """
        signature = self._report_signature()
        if signature == self._sp_last_signature:
            return
        self.async_write_ha_state()
        self._sp_last_signature = signature

    async def async_added_to_hass(self) -> None:
        """Subscribe to the frame fan-out (HA writes the initial state itself)."""
        await super().async_added_to_hass()
        self._sp_last_signature = self._report_signature()
        self._data.listeners.append(self._handle_frame_update)

    async def async_will_remove_from_hass(self) -> None:
        """Drop the frame subscription on removal."""
        await super().async_will_remove_from_hass()
        with contextlib.suppress(ValueError):
            self._data.listeners.remove(self._handle_frame_update)
