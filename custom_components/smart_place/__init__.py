"""Smart Place Home Assistant integration entry point.

Thin wrapper over :mod:`smart_place_client`. Lifecycle per DESIGN §6.1:
``async_setup_entry`` constructs a :class:`SmartPlaceClient` and starts
its ``run()`` coroutine via ``entry.async_create_background_task`` so HA
owns the task lifetime. ``async_unload_entry`` calls ``aclose`` which
cancels the task on the next iteration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from homeassistant.const import Platform
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from smart_place_client import ServerFrame, SmartPlaceClient, install_token_redaction_filter

from .const import CONF_TOKEN, DOMAIN

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR]


@dataclass(slots=True)
class SmartPlaceData:
    """Per-config-entry runtime data stored on hass.data[DOMAIN][entry_id]."""

    client: SmartPlaceClient
    listeners: list[Callable[[], None]] = field(default_factory=list)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Smart Place from a config entry."""
    install_token_redaction_filter()

    token: str = entry.data[CONF_TOKEN]
    session = async_get_clientsession(hass)

    async def trigger_reauth() -> None:
        entry.async_start_reauth(hass)

    client = SmartPlaceClient.live(
        token=token,
        session=session,
        on_reauth=trigger_reauth,
    )

    data = SmartPlaceData(client=client)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = data

    async def _notify_listeners(_frame: ServerFrame) -> None:
        for listener in list(data.listeners):
            listener()

    client.subscribe(_notify_listeners)

    entry.async_create_background_task(
        hass=hass,
        target=client.run(),
        name=f"smart_place_ws_{entry.entry_id}",
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Smart Place config entry — clean up the client and platforms."""
    data: SmartPlaceData | None = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if data is not None:
        await data.client.aclose()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
