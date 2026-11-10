"""Button platform for Smart Place — door openers and group buttons.

The four ``OEFFNER<n>`` commands map to physical doors at the
property: front door, ground-floor entrance, garage, mailbox. They're
write-only: the Smart Place SPA fires them as text frames over the
same app WebSocket. Per the ``smart-place-observe-only`` memory, the
client library never sends them unprompted — pressing the HA button
is the explicit, user-driven trigger.

The vendor app's sub-menus add group buttons: per lights sub-menu one
button that sends the sub-menu's group message (what the server does
with it is untested, so the button is disabled by default). The blinds
sub-menu that covers its whole plan gets "all up" and "all down" for the
blinds of that plan; any other blinds sub-menu gets one button that
sends its own group message, like the lights one and disabled by default
for the same reason.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable
import contextlib
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Final

from homeassistant.components.button import ButtonEntity

from . import SmartPlaceConfigEntry, SmartPlaceData, category_device_info, main_device_info
from .entity import SmartPlacePushEntity, async_add_discovered
from .smart_place_client import CommandDefinition, Commands, blinds_menu_plan

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
    """Create one button per known door and the group buttons of the sub-menus."""
    data = entry.runtime_data
    state = data.state

    def collect() -> dict[Hashable, Callable[[], Entity]]:
        found: dict[Hashable, Callable[[], Entity]] = {
            ("door", door.key): partial(SmartPlaceDoorButton, entry, data, door) for door in _DOORS
        }
        if not data.devices_listed:
            return found
        for menu in state.light_sub_menus.values():
            # The message is sent back verbatim, so only a well-formed one gets a button.
            with contextlib.suppress(ValueError):
                Commands.GroupMessage.encode(menu.group_message)
                found["lights_group", menu.id] = partial(
                    SmartPlaceGroupButton,
                    entry,
                    data,
                    name=f"{menu.name} lights",
                    unique_id=f"lights_group_{menu.id}",
                    translation_key="lights_group",
                    command=Commands.GroupMessage,
                    argument=menu.group_message,
                    enabled_default=False,
                )
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
            # Any other sub-menu must not move the whole plan: it sends its own message.
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


class SmartPlaceGroupButton(SmartPlacePushEntity, ButtonEntity):
    """A sub-menu group button: lights, all blinds up, all blinds down, a group of blinds."""

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
