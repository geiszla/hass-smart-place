"""Switch platform for Smart Place — sockets, on/off scenes, the doorbell mute and a setting per blind.

- A light of kind ``steckdose`` is an outlet switch on its own device, so
  it stays out of light groups and "all lights off".
- An ``OnOff`` scene has a real state (``SZENEN<id>:00|01``) and becomes a
  switch on the "Scene" device (see ``scene.py``) — unless it is one level
  of a level group, which is a fan (``fan.py``).
- ``MUTE<n>`` is the doorbell mute, on the main device.

These three only have a toggle command, so on/off is decided against the
effective state — see ``SmartPlaceToggleEntity``.

- Every blind gets the configuration switch "Lower after weather alarm".
  It switches nothing on the vendor side: it is a setting of this
  integration, kept in the config entry options. The Blinds page of the
  dashboard has one card with these switches; they are on no device.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable
from functools import partial
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later

from . import SmartPlaceConfigEntry, SmartPlaceData, category_device_info, light_device_info, main_device_info
from .const import CONF_LOWER_AFTER_ALARM, CONF_LOWER_DELAY_MINUTES
from .entity import (
    SmartPlacePushEntity,
    SmartPlaceToggleEntity,
    async_add_discovered,
    async_classify_scenes,
    async_keep_spoken_name,
)
from .smart_place_client import (
    DEVICE_LIGHT,
    LIGHT_KIND_SOCKET,
    BlindAlarm,
    BlindConfig,
    BlindMotion,
    BlindRaised,
    BlindValue,
    CommandDefinition,
    Commands,
    LightConfig,
    LowerAfterAlarm,
    SceneConfig,
    blind_can_be_lowered,
    command_raises_blind,
    cover_position,
    light_is_on,
    weather_alarm_icon,
)

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.core import CALLBACK_TYPE, HomeAssistant
    from homeassistant.helpers.entity import Entity
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

# The timer may fire a moment before the rule's own deadline.
_WAIT_TIMER_MARGIN = 0.1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartPlaceConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a switch per socket, per on/off scene outside a level group, per doorbell mute and per blind."""
    data = entry.runtime_data
    state = data.state

    def collect() -> dict[Hashable, Callable[[], Entity]]:
        if not data.devices_listed:
            return {}
        # The mute's name depends on how many mutes there are, so it waits like the rest.
        found: dict[Hashable, Callable[[], Entity]] = {
            ("mute", mute_id): partial(SmartPlaceMuteSwitch, entry, data, mute_id) for mute_id in state.mutes
        }
        for light in state.lights.values():
            if light.kind == LIGHT_KIND_SOCKET:
                found["socket", light.id] = partial(SmartPlaceSocket, entry, data, light)
        for scene_id in async_classify_scenes(entry, data).switches:
            found["scene", scene_id] = partial(SmartPlaceSceneSwitch, entry, data, state.scene_configs[scene_id])
        for blind in state.blinds.values():
            found["lower_after_alarm", blind.id] = partial(SmartPlaceLowerAfterAlarmSwitch, entry, data, blind)
        return found

    async_add_discovered(entry, async_add_entities, collect)


class SmartPlaceSocket(SmartPlaceToggleEntity, SwitchEntity):
    """A switched socket.

    While it is protected (integration options) no command is sent for it
    — see ``SmartPlacePushEntity._async_issue`` — it shows a lock, and it
    is hidden in Home Assistant: a hidden entity is left out of calls for
    an area, a device, a floor or a label, and out of the automatic
    dashboards.
    """

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
        # The protection the hidden flag was last set for; ``None`` before the first time.
        self._hidden_for: bool | None = None

    def _protected(self) -> bool:
        return self._light_id in self._data.protected[DEVICE_LIGHT]

    @callback
    def _async_follow_protection(self) -> None:
        """Hide the switch when the socket becomes protected; show it again if that hid it.

        Done when the entity is added and when the option changes, not on
        every update: the user may show a protected socket again. A flag
        the user set is never cleared.
        """
        protected = self._protected()
        if protected == self._hidden_for:
            return
        self._hidden_for = protected
        entry = self.registry_entry
        assert entry is not None
        if protected and entry.hidden_by is None:
            hidden_by = er.RegistryEntryHider.INTEGRATION
        elif not protected and entry.hidden_by is er.RegistryEntryHider.INTEGRATION:
            hidden_by = None
        else:
            return
        er.async_get(self.hass).async_update_entity(self.entity_id, hidden_by=hidden_by)

    @callback
    def _handle_frame_update(self) -> None:
        """Follow a changed protection (the options changed), then write if anything changed."""
        self._async_follow_protection()
        super()._handle_frame_update()

    async def async_added_to_hass(self) -> None:
        """Apply the protection the options hold now."""
        await super().async_added_to_hass()
        self._async_follow_protection()

    @callback
    def async_entity_id_changed(self, old_entity_id: str) -> None:
        """Opt in to in-place entity_id changes: nothing here is keyed on the entity_id."""
        super().async_entity_id_changed(old_entity_id)

    @property
    def icon(self) -> str | None:
        """Return a lock while protected, else nothing (the device class has an icon)."""
        return "mdi:lock" if self._protected() else None

    @property
    def extra_state_attributes(self) -> dict[str, bool]:
        """Expose whether the socket is protected."""
        return {"protected": self._protected()}

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
        """Wire the scene's id, name, unique_id and device."""
        super().__init__()
        self._data = data
        self._scene_id = scene.id
        self._attr_name = scene.name
        self._attr_unique_id = f"{entry.entry_id}_scene_switch_{scene.id}"
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
        self._attr_device_info = main_device_info(entry)

    def _server_is_on(self) -> bool | None:
        return self._data.state.mutes.get(self._mute_id)

    def _toggle_command(self) -> tuple[CommandDefinition, int]:
        return Commands.MuteToggle, self._mute_id


class SmartPlaceLowerAfterAlarmSwitch(SmartPlacePushEntity, SwitchEntity):
    """Per blind: lower it when its weather alarm has ended.

    A weather alarm drives a blind up and holds it there. While this
    switch is on, the integration sends the blind's down command when it
    sees the alarm end: the one command it sends without a user action at
    that moment. The rule is ``smart_place_client.alarm.LowerAfterAlarm``.
    This class feeds it the blind's values, keeps the timer for the
    waiting time and sends the command the way every entity does
    (``_async_issue``: dry run, audit log, protected devices).

    Moving the blind up switches this off: Home Assistant's own up command
    at once, any other move up when the blind reports it
    (``smart_place_client.alarm.BlindRaised``).

    Turning it on lowers a blind that is not down. No down command is
    sent, at the end of an alarm or when it is turned on, for a blind
    that is already on its way down: the vendor system takes a second
    down command as "stop" (``smart_place_client.motion.BlindMotion``).

    The switch state is kept in the config entry options: restored entity
    state is written too rarely for a setting.

    It is on no device (user decision, 2026-11-11): it is switched on the
    dashboard's Blinds page, and on the blind's device page it was a
    second place for the same thing. Its name therefore carries the
    blind's name.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "lower_after_alarm"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: SmartPlaceConfigEntry, data: SmartPlaceData, blind: BlindConfig) -> None:
        """Wire the blind's id and unique_id."""
        self._data = data
        self._blind_id = blind.id
        self._attr_unique_id = f"{entry.entry_id}_lower_after_alarm_{blind.id}"
        self._lowering = LowerAfterAlarm()
        self._raised = BlindRaised()
        self._motion = BlindMotion(run_time=blind.runtime_s)
        # Seeded here: the icon is part of the state that is recorded as written when the entity is added.
        self._alarm = BlindAlarm()
        self._alarm.update(self._value())
        self._wait_timer: CALLBACK_TYPE | None = None

    def _value(self) -> BlindValue | None:
        return self._data.state.blind_values.get(self._blind_id)

    def _position(self) -> int | None:
        """Return the blind's position on Home Assistant's scale (100 is fully up), ``None`` without one."""
        value = self._value()
        return None if value is None or value.position is None else cover_position(value.position)

    def _blind_name(self) -> str:
        """Return the blind's current vendor name."""
        return self._data.state.blinds[self._blind_id].name

    @property
    def name(self) -> str:
        """Return the blind's name and the function: there is no device to take a name from."""
        return f"{self._blind_name()} Lower after weather alarm"

    @property
    def is_on(self) -> bool:
        """Return whether the blind is lowered after its weather alarm."""
        return self._blind_id in self._data.entry.options.get(CONF_LOWER_AFTER_ALARM, ())

    @property
    def icon(self) -> str | None:
        """Return the alarm's icon while off and a weather alarm holds the blind, else nothing (the lock of ``icons.json``)."""
        alarm = self._alarm.alarm
        return None if alarm is None or self.is_on else weather_alarm_icon(alarm)

    @callback
    def _handle_frame_update(self) -> None:
        """Feed the blind's value to the rule, then write if the switch itself changed."""
        self._async_follow_blind()
        super()._handle_frame_update()

    @callback
    def _async_follow_blind(self) -> None:
        value = self._value()
        self._alarm.update(value)
        self._motion.update(self._position())
        if self._raised.update(value, weather_alarm=self._data.state.weather_alarm_on) and self.is_on:
            _LOGGER.info(
                "%s (blind %d) was moved up: 'Lower after weather alarm' is turned off",
                self._blind_name(),
                self._blind_id,
            )
            # Storing follows the blind again, with the function off.
            self._async_store(on=False)
            return
        delay = self._data.entry.options.get(CONF_LOWER_DELAY_MINUTES, 0) * 60
        if self._lowering.update(value, enabled=self.is_on, delay=delay):
            self._async_cancel_wait_timer()
            if delay:
                # No frame arrives when the wait is over.
                self._wait_timer = async_call_later(self.hass, delay + _WAIT_TIMER_MARGIN, self._async_wait_over)
            else:
                self._async_lower_if_due()
        elif not self._lowering.waiting:
            self._async_cancel_wait_timer()

    @callback
    def _async_wait_over(self, _now: datetime) -> None:
        self._wait_timer = None
        self._async_lower_if_due()

    @callback
    def _async_cancel_wait_timer(self) -> None:
        if self._wait_timer is not None:
            self._wait_timer()
            self._wait_timer = None

    @callback
    def _async_lower_if_due(self) -> None:
        if self._lowering.due(self._value(), enabled=self.is_on, healthy=self._data.is_healthy):
            self._data.entry.async_create_task(self.hass, self._async_lower_after_alarm())

    def _moving_down(self) -> bool:
        """Return True, with one line in the log, for a blind on its way down: a second down command would stop it."""
        if self._motion.direction != "closing":
            return False
        _LOGGER.info(
            "%s (blind %d) is already moving down: no down command is sent", self._blind_name(), self._blind_id
        )
        return True

    async def _async_lower_after_alarm(self) -> None:
        """Send the down command. Nobody called for it, so a failure is logged, not raised."""
        # An automation of the user may have closed the blind at the same alarm end.
        if self._moving_down():
            return
        _LOGGER.info("Lowering %s (blind %d): its weather alarm ended", self._blind_name(), self._blind_id)
        try:
            await self._async_issue(Commands.BlindDown, self._blind_id)
        except HomeAssistantError as err:
            _LOGGER.warning(
                "%s (blind %d) was not lowered after its weather alarm: %s", self._blind_name(), self._blind_id, err
            )

    @callback
    def _async_command_issued(self, command: CommandDefinition, args: tuple[object, ...]) -> None:
        """Follow a command Home Assistant sends for this blind: it ends a running wait, and its up command switches this off."""
        if self._motion.command_sent(command, args, self._blind_id, self._position()):
            self._lowering.cancel()
            self._async_cancel_wait_timer()
        if self.is_on and command_raises_blind(command, args, self._blind_id):
            _LOGGER.info(
                "%s (blind %d) is moved up: 'Lower after weather alarm' is turned off",
                self._blind_name(),
                self._blind_id,
            )
            self._async_store(on=False)

    @callback
    def _async_store(self, *, on: bool) -> None:
        entry = self._data.entry
        blinds = set(entry.options.get(CONF_LOWER_AFTER_ALARM, ())) - {self._blind_id}
        if on:
            blinds.add(self._blind_id)
        self.hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_LOWER_AFTER_ALARM: sorted(blinds)}
        )
        # The entry's update listener runs a moment later; show it now.
        self._handle_frame_update()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Switch the function on; a blind that is not down is lowered at once.

        A blind that is blocked now is lowered when its alarm ends. A
        blind that is already moving down gets no command: a second down
        command would stop it.
        """
        if self.is_on:
            return
        self._async_store(on=True)
        if blind_can_be_lowered(self._value()) and not self._moving_down():
            _LOGGER.info(
                "Lowering %s (blind %d): 'Lower after weather alarm' was turned on and the blind is not down",
                self._blind_name(),
                self._blind_id,
            )
            await self._async_issue(Commands.BlindDown, self._blind_id)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Switch the function off; a running wait ends and nothing is sent."""
        self._async_store(on=False)

    async def async_added_to_hass(self) -> None:
        """Note an alarm that already holds the blind, and listen for commands for the blind."""
        await super().async_added_to_hass()
        self._async_follow_blind()
        self._data.command_listeners.append(self._async_command_issued)

    async def async_will_remove_from_hass(self) -> None:
        """Stop the wait timer and the command subscription alongside the frame subscription."""
        await super().async_will_remove_from_hass()
        self._data.command_listeners.remove(self._async_command_issued)
        self._async_cancel_wait_timer()

    @callback
    def async_entity_id_changed(self, old_entity_id: str) -> None:
        """Opt in to in-place entity_id changes: nothing here is keyed on the entity_id."""
        super().async_entity_id_changed(old_entity_id)
