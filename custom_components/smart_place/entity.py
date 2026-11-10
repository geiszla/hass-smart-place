"""Shared base for Smart Place push entities.

The Smart Place server re-broadcasts the full sensor set roughly twice a
second even when nothing has changed, and ``__init__.py``'s
``async_notify_listeners`` fans every parsed frame out to *all* entities. Writing
on every callback therefore re-reported each entity ~2x/s. Those writes fire
``state_reported`` events that flood the event bus and feed duplicate samples
into downstream consumers — most visibly a Statistics helper, whose rolling
buffer fills from the broadcast cadence instead of from real value changes.

``SmartPlacePushEntity`` subscribes to the same fan-out but compares what HA
would actually write and skips the write when it is unchanged, so
``last_reported`` only advances on a genuine state or availability change.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Hashable
import contextlib
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.const import Platform
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.event import async_call_later

from .const import DOMAIN
from .smart_place_client import ExpectedState, SmartPlaceNotConnectedError, classify_scenes, command_is_protected

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.core import CALLBACK_TYPE
    from homeassistant.helpers.entity import CalculatedState
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import SmartPlaceConfigEntry, SmartPlaceData
    from .smart_place_client import CommandDefinition, SceneEntities

_LOGGER = logging.getLogger(__name__)

# The timer may fire a moment before the expectation's own deadline.
_EXPECTATION_TIMER_MARGIN = 0.1


@callback
def async_add_discovered(
    entry: SmartPlaceConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    collect: Callable[[], dict[Hashable, Callable[[], Entity]]],
) -> None:
    """Add an entity for everything ``collect`` finds, now and whenever more appears.

    ``collect`` maps a key per discoverable thing to a factory for its
    entity. It runs on every listener fan-out; a key seen before is
    skipped, so each thing gets its entity once. Nothing is ever removed.
    """
    data = entry.runtime_data
    known: set[Hashable] = set()

    @callback
    def discover() -> None:
        found = collect()
        new = [factory() for key, factory in found.items() if key not in known]
        if new:
            known.update(found)
            async_add_entities(new)
            data.async_plan_changed()

    discover()
    data.listeners.append(discover)
    entry.async_on_unload(lambda: data.listeners.remove(discover))


@callback
def async_classify_scenes(entry: SmartPlaceConfigEntry, data: SmartPlaceData) -> SceneEntities:
    """Decide which scene becomes a scene entity, a switch or a level of a fan.

    The scene, switch and fan platforms all ask here, so they agree.
    What exists decides first, so nothing is ever converted: a scene that
    already is a switch in the entity registry stays one, and a level
    group that already is a fan stays a fan, whatever the scene flags say
    now. What was decided in this run counts the same way: an entity is
    only registered a moment after it was created.
    """
    registry = er.async_get(data.hass)
    scenes = data.state.scene_configs

    def registered(platform: Platform, kind: str) -> set[int]:
        return {
            scene_id
            for scene_id in scenes
            if registry.async_get_entity_id(platform, DOMAIN, f"{entry.entry_id}_{kind}_{scene_id}")
        }

    entities = classify_scenes(
        scenes,
        data.state.scene_states,
        keep_as_switch=registered(Platform.SWITCH, "scene_switch") | data.scene_switch_ids,
        keep_as_group=registered(Platform.FAN, "level_group") | data.level_group_ids,
    )
    # Before the main-menu reply is complete the scene list can be partial; nothing is decided from it.
    if data.devices_listed:
        data.scene_switch_ids.update(entities.switches)
        data.level_group_ids.update(group.scene_ids[0] for group in entities.level_groups)
    return entities


class SmartPlacePushEntity(Entity):
    """Frame-driven entity that only writes when its reported state changes."""

    _attr_should_poll = False
    _data: SmartPlaceData
    _sp_last_written: CalculatedState | None = None
    _sp_expectation_timer: CALLBACK_TYPE | None = None

    @property
    def available(self) -> bool:
        """Unavailable while the Smart Place WebSocket is down."""
        return self._data.is_healthy

    @callback
    def _handle_frame_update(self) -> None:
        """Write to HA only when the reported state actually changed.

        Compares the state and the full attribute set HA would write (so
        brightness, position, target temperature and a late-arriving name
        count as changes). The comparison value is recorded *after* a
        successful write: if ``async_write_ha_state`` raises (caught +
        logged by the fan-out in ``__init__.py``), the next notification
        retries instead of skipping the value forever.
        """
        calculated = self._async_calculate_state()
        if calculated == self._sp_last_written:
            return
        self.async_write_ha_state()
        self._sp_last_written = calculated

    def _command_refused(self, command: CommandDefinition, args: tuple[object, ...]) -> bool:
        """Return True, with one line in the log, for a command for a protected device."""
        if not command_is_protected(command, args, self._data.protected):
            return False
        _LOGGER.info("%s is protected: %s not sent", self.entity_id, command.name)
        return True

    async def _async_issue(self, command: CommandDefinition, *args: object) -> None:
        """Issue a command; raise a translated error if it cannot be sent.

        Every command of every entity passes here, so this is where a
        command for a protected device is dropped: without an error for
        the caller, so that a call for many entities reaches the others.
        """
        if self._command_refused(command, args):
            return
        try:
            await self._data.client.issue(command, *args)
        except SmartPlaceNotConnectedError as err:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="not_connected") from err

    async def _async_issue_expecting(
        self,
        command: CommandDefinition,
        args: tuple[object, ...],
        *expectations: tuple[ExpectedState[Any], object, object],
    ) -> None:
        """Issue a command and show the state it leads to at once.

        Each expectation is ``(holder, expected value, current server
        value)``. They are set before the send is awaited, so a call that
        arrives meanwhile is decided against them, and dropped again if the
        send fails. No frame arrives when an expectation times out, so a
        timer writes the state then.
        """
        # Nothing is sent for a protected device, so nothing is expected of it.
        if self._command_refused(command, args):
            return
        for expected, value, server_value in expectations:
            expected.expect(value, server_value)
        if self._sp_expectation_timer is not None:
            self._sp_expectation_timer()
        self._sp_expectation_timer = async_call_later(
            self.hass,
            max(expected.timeout for expected, _, _ in expectations) + _EXPECTATION_TIMER_MARGIN,
            self._async_expectation_ended,
        )
        self._handle_frame_update()
        try:
            await self._async_issue(command, *args)
        except HomeAssistantError:
            for expected, _, _ in expectations:
                expected.clear()
            self._handle_frame_update()
            raise

    @callback
    def _async_expectation_ended(self, _now: datetime) -> None:
        self._sp_expectation_timer = None
        self._handle_frame_update()

    async def async_added_to_hass(self) -> None:
        """Subscribe to the frame fan-out (HA writes the initial state itself)."""
        await super().async_added_to_hass()
        self._sp_last_written = self._async_calculate_state()
        self._data.listeners.append(self._handle_frame_update)

    async def async_will_remove_from_hass(self) -> None:
        """Drop the frame subscription on removal."""
        await super().async_will_remove_from_hass()
        with contextlib.suppress(ValueError):
            self._data.listeners.remove(self._handle_frame_update)
        if self._sp_expectation_timer is not None:
            self._sp_expectation_timer()
            self._sp_expectation_timer = None

    @callback
    def async_entity_id_changed(self, old_entity_id: str) -> None:
        """Opt in to in-place entity_id changes: nothing here is keyed on the entity_id."""
        super().async_entity_id_changed(old_entity_id)


class SmartPlaceToggleEntity(SmartPlacePushEntity):
    """On/off entity whose only vendor command is a toggle.

    Home Assistant asks for a target ("turn on"), the vendor only flips.
    Every call is therefore decided against the *effective* state: the
    state expected from the commands already sent, else the last server
    value. Without a server value (after a reconnect, until the server
    replays it) the state is unknown and an expectation is dropped. Mix
    in before the platform's entity class.
    """

    def __init__(self) -> None:
        """Set up the expected on/off state."""
        self._expected_on: ExpectedState[bool] = ExpectedState()

    def _server_is_on(self) -> bool | None:
        """Return the last on/off state from the server, ``None`` if not received."""
        raise NotImplementedError

    def _toggle_command(self) -> tuple[CommandDefinition, int]:
        """Return the toggle command and the vendor id it takes."""
        raise NotImplementedError

    @property
    def is_on(self) -> bool | None:
        """Return the effective on/off state."""
        return self._expected_on.resolve(self._server_is_on())

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on: toggle unless already on."""
        await self._async_switch(on=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off: toggle unless already off."""
        await self._async_switch(on=False)

    async def _async_switch(self, *, on: bool) -> None:
        command, vendor_id = self._toggle_command()
        # Nothing is ever sent for a protected device: there is nothing to
        # wait for, and a state that is not known is no error.
        if self._command_refused(command, (vendor_id,)):
            return
        # A scene or group command issued a moment ago is about to change
        # values: a toggle decided now could undo what it switches.
        if (delay := self._data.client.settle.remaining()) > 0:
            await asyncio.sleep(delay)
        current = self.is_on
        if current is None:
            # A toggle against an unknown state could switch the wrong way.
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="state_unknown")
        if current is on:
            return
        await self._async_issue_expecting(command, (vendor_id,), (self._expected_on, on, self._server_is_on()))
