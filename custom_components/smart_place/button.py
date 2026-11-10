"""Button platform for Smart Place — door openers, "all lights" and the blinds group buttons.

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

The vendor app's blinds sub-menus add group buttons: the one that covers
its whole plan gets "all up" and "all down" for the blinds of that plan;
any other blinds sub-menu gets one button that sends its own group
message (what the server does with it is untested, so that button is
disabled by default).
"""

from __future__ import annotations

from collections.abc import Callable, Hashable
import contextlib
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Final

from homeassistant.components.button import ButtonEntity
from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_OFF, SERVICE_TURN_ON, STATE_OFF, STATE_ON, Platform
from homeassistant.helpers import entity_registry as er

from . import SmartPlaceConfigEntry, SmartPlaceData, category_device_info, main_device_info
from .const import DOMAIN
from .entity import SmartPlacePushEntity, async_add_discovered
from .smart_place_client import LIGHT_KIND_SOCKET, CommandDefinition, Commands, blinds_menu_plan

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity import Entity
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

PARALLEL_UPDATES = 0


@dataclass(frozen=True, slots=True)
class _DoorButton:
    """Static definition of one door-opener button."""

    key: str
    name: str
    command: CommandDefinition


_DOORS: Final[tuple[_DoorButton, ...]] = (
    _DoorButton(key="front_door", name="Front door", command=Commands.OpenFrontDoor),
    _DoorButton(
        key="ground_floor_entrance",
        name="Ground floor entrance",
        command=Commands.OpenGroundFloorEntrance,
    ),
    _DoorButton(key="garage_entrance", name="Garage entrance", command=Commands.OpenGarageEntrance),
    _DoorButton(key="mailbox", name="Mailbox", command=Commands.OpenMailbox),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartPlaceConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create one button per known door, the two "all lights" buttons and the blinds group buttons."""
    data = entry.runtime_data
    state = data.state

    def collect() -> dict[Hashable, Callable[[], Entity]]:
        found: dict[Hashable, Callable[[], Entity]] = {
            ("door", door.key): partial(SmartPlaceDoorButton, entry, data, door) for door in _DOORS
        }
        if not data.devices_listed:
            return found
        if state.lights:
            for on in (False, True):
                found["all_lights", on] = partial(SmartPlaceAllLightsButton, entry, data, on=on)
        for menu in state.blind_sub_menus.values():
            if (plan_id := blinds_menu_plan(menu)) is not None:
                for direction, command in (("up", Commands.BlindsGroupUp), ("down", Commands.BlindsGroupDown)):
                    found[f"blinds_group_{direction}", menu.id] = partial(
                        SmartPlaceGroupButton,
                        entry,
                        data,
                        name=f"{menu.name} blinds {direction}",
                        unique_id=f"blinds_group_{direction}_{menu.id}",
                        translation_key=f"blinds_group_{direction}",
                        command=command,
                        argument=plan_id,
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

    def __init__(self, entry: SmartPlaceConfigEntry, data: SmartPlaceData, door: _DoorButton) -> None:
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


class SmartPlaceGroupButton(SmartPlacePushEntity, ButtonEntity):
    """A blinds sub-menu group button: all blinds up, all blinds down, a group of blinds."""

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
