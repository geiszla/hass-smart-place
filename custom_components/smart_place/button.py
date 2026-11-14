"""Button platform for Smart Place — door openers, "all lights", "all heating" and the blinds group buttons.

The four ``OEFFNER<n>`` commands map to physical doors at the
property: front door, ground-floor entrance, garage, mailbox. They're
write-only: the Smart Place SPA fires them as text frames over the
same app WebSocket. Per the ``smart-place-observe-only`` memory, the
client library never sends them unprompted — pressing the HA button
is the explicit, user-driven trigger.

"All lights off" and "All lights on" switch every light and socket
through its own entity, one after the other. The vendor app's lights
sub-menu has a group message for this, but it is not used: what the
server does with it is not known, and a protected socket could not be
kept out of it.

"All heating off" switches off every heating zone that heats, also
through the zones' own entities, and remembers which zones it switched.
"Heating back on" switches exactly those on again; each zone returns to
the target it had (``climate.py``). A zone that was off before stays off.

The vendor app's blinds sub-menus add group buttons: the one that covers
its whole plan gets "all up" and "all down" for the blinds of that plan,
which move every blind through its own entity as well. The vendor's
group command for this is not used: it leaves out blinds, and which ones
is set inside the vendor system. Any other blinds sub-menu gets one
button that sends its own group message (what the server does with it is
untested, so that button is disabled by default).
"""

from __future__ import annotations

from collections.abc import Callable, Hashable
import contextlib
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any

from homeassistant.components.button import ButtonEntity
from homeassistant.components.climate import ATTR_HVAC_MODE, DOMAIN as CLIMATE_DOMAIN, SERVICE_SET_HVAC_MODE, HVACMode
from homeassistant.components.cover import ATTR_CURRENT_POSITION, CoverState
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_CLOSE_COVER,
    SERVICE_OPEN_COVER,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    Platform,
)
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.restore_state import ExtraStoredData

from . import SmartPlaceConfigEntry, SmartPlaceData, category_device_info, main_device_info
from .const import DOMAIN
from .entity import SmartPlacePushEntity, async_add_discovered
from .smart_place_client import DOORS, LIGHT_KIND_SOCKET, CommandDefinition, Commands, Door, blinds_menu_plan

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
    """Create one button per known door, the "all lights" and "all heating" buttons and the blinds group buttons."""
    data = entry.runtime_data
    state = data.state

    def collect() -> dict[Hashable, Callable[[], Entity]]:
        found: dict[Hashable, Callable[[], Entity]] = {
            ("door", door.key): partial(SmartPlaceDoorButton, entry, data, door) for door in DOORS
        }
        if not data.devices_listed:
            return found
        if state.lights:
            for on in (False, True):
                found["all_lights", on] = partial(SmartPlaceAllLightsButton, entry, data, on=on)
        if state.climate:
            for on in (False, True):
                found["all_heating", on] = partial(SmartPlaceAllHeatingButton, entry, data, on=on)
        for menu in state.blind_sub_menus.values():
            if (plan_id := blinds_menu_plan(menu)) is not None:
                for direction in ("up", "down"):
                    found[f"blinds_group_{direction}", menu.id] = partial(
                        SmartPlaceAllBlindsButton,
                        entry,
                        data,
                        name=f"{menu.name} blinds {direction}",
                        unique_id=f"blinds_group_{direction}_{menu.id}",
                        translation_key=f"blinds_group_{direction}",
                        plan_id=plan_id,
                        up=direction == "up",
                    )
                continue
            # Any other sub-menu must not move the whole plan: it sends its own
            # message, verbatim, so only a well-formed one gets a button.
            with contextlib.suppress(ValueError):
                Commands.BlindsGroupMessage.encode(menu.group_message)
                found["blinds_group", menu.id] = partial(
                    SmartPlaceGroupButton,
                    entry,
                    data,
                    name=f"{menu.name} blinds",
                    unique_id=f"blinds_group_{menu.id}",
                    translation_key="blinds_group",
                    command=Commands.BlindsGroupMessage,
                    argument=menu.group_message,
                    enabled_default=False,
                )
        return found

    async_add_discovered(entry, async_add_entities, collect)


class SmartPlaceDoorButton(SmartPlacePushEntity, ButtonEntity):
    """One-shot button that sends an OEFFNER<n> command to the SPA.

    Availability (``is_healthy``) and its push subscription come from
    ``SmartPlacePushEntity`` — without subscribing, the button would never
    learn the WS dropped (or recovered) and could show stale availability.
    The dedup collapses the broadcast stream to connect/disconnect edges,
    since a button has no frame-driven state of its own.
    """

    _attr_has_entity_name = True

    def __init__(self, entry: SmartPlaceConfigEntry, data: SmartPlaceData, door: Door) -> None:
        """Wire the door's name, unique_id, and write command."""
        self._data = data
        self._door = door
        self._attr_name = door.name
        self._attr_unique_id = f"{entry.entry_id}_door_{door.key}"
        self._attr_device_info = category_device_info(entry, data, "Doors")

    async def async_press(self) -> None:
        """Send the door's OEFFNER command on the live WS.

        Issued via ``client.issue`` so the actuation lands in the audit
        trail (door openers are ``audit=True``). A press that cannot be
        sent raises, so the caller does not take it for an opened door.
        """
        await self._async_issue(self._door.command)


class SmartPlaceAllLightsButton(SmartPlacePushEntity, ButtonEntity):
    """Switch every light and socket of the entry off, or on.

    Each light and socket entity that is not in the wanted state yet gets
    its own turn-off (or turn-on) call, one after the other. So the
    toggle rule, the expected state and the protection of a socket apply
    as for a single press, and a protected socket is never switched.
    """

    _attr_has_entity_name = True

    def __init__(self, entry: SmartPlaceConfigEntry, data: SmartPlaceData, *, on: bool) -> None:
        """Wire the direction, the unique_id and the translated name."""
        self._data = data
        self._entry_id = entry.entry_id
        self._on = on
        self._attr_translation_key = "all_lights_on" if on else "all_lights_off"
        self._attr_unique_id = f"{entry.entry_id}_{self._attr_translation_key}"
        self._attr_device_info = main_device_info(entry)

    async def async_press(self) -> None:
        """Call turn-off (or turn-on) for every light and socket in the other state."""
        registry = er.async_get(self.hass)
        service, other_state = (SERVICE_TURN_ON, STATE_OFF) if self._on else (SERVICE_TURN_OFF, STATE_ON)
        for light in self._data.state.lights.values():
            domain, kind = (Platform.SWITCH, "socket") if light.kind == LIGHT_KIND_SOCKET else (Platform.LIGHT, "light")
            entity_id = registry.async_get_entity_id(domain, DOMAIN, f"{self._entry_id}_{kind}_{light.id}")
            # A disabled entity has no state; an unknown or unavailable one is left alone.
            if entity_id is None or (state := self.hass.states.get(entity_id)) is None or state.state != other_state:
                continue
            await self.hass.services.async_call(
                domain, service, {ATTR_ENTITY_ID: entity_id}, blocking=True, context=self._context
            )


@dataclass
class _SwitchedOffZones(ExtraStoredData):
    """The heating zones "All heating off" switched off and nothing switched on again."""

    zones: list[int]

    def as_dict(self) -> dict[str, Any]:
        """Return the stored data as a dict."""
        return {"zones": self.zones}


class SmartPlaceAllHeatingButton(SmartPlacePushEntity, ButtonEntity):
    """Switch every heating zone off, or switch those zones on again.

    "Off" sets every zone that heats to Off with its own service call and
    remembers the zone. "On" sets exactly the remembered zones back to
    Heat, where each returns to the target it had. Without a preceding
    "off" it does nothing. A remembered zone that is unavailable at the
    press stays remembered. The "off" button keeps the list across
    restarts; both buttons work on ``SmartPlaceData.heating_switched_off``.
    """

    _attr_has_entity_name = True

    def __init__(self, entry: SmartPlaceConfigEntry, data: SmartPlaceData, *, on: bool) -> None:
        """Wire the direction, the unique_id and the translated name."""
        self._data = data
        self._entry_id = entry.entry_id
        self._on = on
        self._attr_translation_key = "heating_back_on" if on else "all_heating_off"
        self._attr_unique_id = f"{entry.entry_id}_{self._attr_translation_key}"
        self._attr_device_info = main_device_info(entry)

    @property
    def extra_restore_state_data(self) -> _SwitchedOffZones | None:
        """Keep the switched-off zones across restarts."""
        return None if self._on else _SwitchedOffZones(sorted(self._data.heating_switched_off))

    async def async_added_to_hass(self) -> None:
        """Restore the zones switched off before the restart."""
        await super().async_added_to_hass()
        if not self._on and (stored := await self.async_get_last_extra_data()) is not None:
            self._data.heating_switched_off.update(stored.as_dict()["zones"])

    @callback
    def async_entity_id_changed(self, old_entity_id: str) -> None:
        """Opt in to in-place entity_id changes: nothing here is keyed on the entity_id."""
        super().async_entity_id_changed(old_entity_id)

    async def async_press(self) -> None:
        """Switch off every zone that heats, or switch the remembered zones on again."""
        registry = er.async_get(self.hass)
        remembered = self._data.heating_switched_off
        mode, other_mode = (HVACMode.HEAT, HVACMode.OFF) if self._on else (HVACMode.OFF, HVACMode.HEAT)
        for zone_id in sorted(remembered if self._on else self._data.state.climate):
            entity_id = registry.async_get_entity_id(Platform.CLIMATE, DOMAIN, f"{self._entry_id}_climate_{zone_id}")
            state = None if entity_id is None else self.hass.states.get(entity_id)
            if state is None:
                # A disabled or removed entity: nothing to switch, now or later.
                remembered.discard(zone_id)
                continue
            if state.state == other_mode:
                await self.hass.services.async_call(
                    CLIMATE_DOMAIN,
                    SERVICE_SET_HVAC_MODE,
                    {ATTR_ENTITY_ID: entity_id, ATTR_HVAC_MODE: mode},
                    blocking=True,
                    context=self._context,
                )
                if not self._on:
                    remembered.add(zone_id)
            # Switched on by this press or before it by the user: nothing left to
            # restore. An unavailable zone stays remembered for a later press.
            if self._on and state.state in (mode, other_mode):
                remembered.discard(zone_id)


class SmartPlaceAllBlindsButton(SmartPlacePushEntity, ButtonEntity):
    """Move every blind of a plan up, or down.

    Each blind that is not reported at that end, or that moves away from
    it, gets its own open (or close) call, one after the other. A blind
    held by a weather alarm is left alone, and the cover sends nothing to
    a blind that already moves that way. The vendor's group command
    (``JALUPZENTRAL<plan>`` / ``JALDOWZENTRAL<plan>``) is not used: here
    it leaves out the two blinds at the outer edge of the balconies
    (2026-11-14).
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        entry: SmartPlaceConfigEntry,
        data: SmartPlaceData,
        *,
        name: str,
        unique_id: str,
        translation_key: str,
        plan_id: int,
        up: bool,
    ) -> None:
        """Wire the plan, the direction, the button's name and unique_id."""
        self._data = data
        self._entry_id = entry.entry_id
        self._plan_id = plan_id
        self._service, self._end, self._away = (
            (SERVICE_OPEN_COVER, 100, CoverState.CLOSING) if up else (SERVICE_CLOSE_COVER, 0, CoverState.OPENING)
        )
        self._attr_name = name
        self._attr_translation_key = translation_key
        self._attr_unique_id = f"{entry.entry_id}_{unique_id}"
        self._attr_device_info = main_device_info(entry)

    async def async_press(self) -> None:
        """Call open (or close) for every blind of the plan that is not at rest at that end."""
        registry = er.async_get(self.hass)
        for blind in self._data.state.blinds.values():
            if blind.plan_id != self._plan_id:
                continue
            entity_id = registry.async_get_entity_id(Platform.COVER, DOMAIN, f"{self._entry_id}_blind_{blind.id}")
            if entity_id is None or (state := self.hass.states.get(entity_id)) is None:
                continue
            position = state.attributes.get(ATTR_CURRENT_POSITION)
            # No position: the cover is unavailable, or a weather alarm holds the blind. A blind
            # that was just sent the other way is still reported at this end.
            if position is None or (position == self._end and state.state != self._away):
                continue
            await self.hass.services.async_call(
                Platform.COVER, self._service, {ATTR_ENTITY_ID: entity_id}, blocking=True, context=self._context
            )


class SmartPlaceGroupButton(SmartPlacePushEntity, ButtonEntity):
    """The button of a blinds sub-menu that does not cover its whole plan: it sends the sub-menu's own group message."""

    _attr_has_entity_name = True

    def __init__(
        self,
        entry: SmartPlaceConfigEntry,
        data: SmartPlaceData,
        *,
        name: str,
        unique_id: str,
        translation_key: str,
        command: CommandDefinition,
        argument: str | int,
        enabled_default: bool = True,
    ) -> None:
        """Wire the button's name, unique_id and command."""
        self._data = data
        self._command = command
        self._argument = argument
        self._attr_name = name
        self._attr_translation_key = translation_key
        self._attr_unique_id = f"{entry.entry_id}_{unique_id}"
        self._attr_entity_registry_enabled_default = enabled_default
        self._attr_device_info = main_device_info(entry)

    async def async_press(self) -> None:
        """Send the group command."""
        await self._async_issue(self._command, self._argument)
