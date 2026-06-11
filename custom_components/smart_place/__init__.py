"""Smart Place Home Assistant integration entry point.

Thin wrapper over :mod:`smart_place_client`. Lifecycle per DESIGN §6.1:
``async_setup_entry`` constructs a :class:`SmartPlaceClient` and starts
its ``run()`` coroutine via ``entry.async_create_background_task`` so HA
owns the task lifetime. ``async_unload_entry`` calls ``aclose`` which
cancels the task on the next iteration.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from homeassistant.const import Platform
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo

from .const import CHART_POLL_INTERVAL, CONF_TOKEN, DOMAIN, SETUP_OBSERVATION_WINDOW
from .smart_place_client import (
    DISCOVERY_ORIGIN,
    ServerFrame,
    SessionPhase,
    SmartPlaceClient,
    SmartPlaceState,
    install_token_redaction_filter,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.SENSOR,
]

# Session phases in which the WS is connected far enough that entity
# state is meaningful. APP_OPEN covers the brief window between the
# server's GoToLinkSSL and the bootstrap reply; BOOTSTRAPPED and READY
# are the steady-state phases.
HEALTHY_PHASES: frozenset[SessionPhase] = frozenset(
    {
        SessionPhase.APP_OPEN,
        SessionPhase.BOOTSTRAPPED,
        SessionPhase.READY,
    },
)


def main_device_info(entry: ConfigEntry) -> DeviceInfo:
    """Registry info for the main Smart Place device, shared by all platforms.

    ``configuration_url`` deep-links to the vendor SPA with the entry's
    current token so automations can template it via
    ``device_attr(..., 'configuration_url')`` instead of hardcoding the
    token — a re-auth refreshes the URL on the post-reauth reload.
    """
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="Smart Place",
        manufacturer="smart PLACE AG",
        configuration_url=f"{DISCOVERY_ORIGIN}/Start5?{entry.data[CONF_TOKEN]}",
    )


@dataclass(slots=True)
class SmartPlaceData:
    """Per-config-entry runtime data stored on hass.data[DOMAIN][entry_id]."""

    client: SmartPlaceClient
    state: SmartPlaceState
    listeners: list[Callable[[], None]] = field(default_factory=list)

    @property
    def is_healthy(self) -> bool:
        """Return True iff the WS is open AND the session is in a usable phase.

        Both legs are needed. ``client.connected`` flips False the moment
        the WS drops; the phase check covers the brief window while a
        new WS is open but bootstrap hasn't completed yet.
        """
        return self.client.connected and self.client.state.phase in HEALTHY_PHASES


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Smart Place from a config entry."""
    install_token_redaction_filter()

    token: str = entry.data[CONF_TOKEN]
    session = async_get_clientsession(hass)

    async def trigger_reauth() -> None:
        entry.async_start_reauth(hass)

    # ``data`` is bound below but the closures only fire after
    # ``client.run()`` starts — which happens after the binding —
    # so Python's call-time free-variable lookup resolves cleanly.
    async def trigger_disconnect_notify() -> None:
        # Push the WS drop to every entity so ``available`` flips False
        # for the whole reconnect-backoff gap (otherwise listeners only
        # wake on the next frame, which doesn't arrive until reconnect).
        for listener in list(data.listeners):
            listener()

    client = SmartPlaceClient.live(
        token=token,
        session=session,
        chart_poll_interval=CHART_POLL_INTERVAL,
        on_reauth=trigger_reauth,
        on_disconnect=trigger_disconnect_notify,
    )

    data = SmartPlaceData(client=client, state=SmartPlaceState())
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = data

    async def _on_frame(frame: ServerFrame) -> None:
        data.state.apply(frame)
        for listener in list(data.listeners):
            listener()

    client.subscribe(_on_frame)

    entry.async_create_background_task(
        hass=hass,
        target=client.run(),
        name=f"smart_place_ws_{entry.entry_id}",
    )

    # Entity families exposed until 2026-06 and then dropped — scenes
    # (the server reports several "active" at once, so on/off carries
    # no usable meaning) and the LEUCHTENZENTRAL / JALZENTRAL rollups
    # (actually the SPA's "All" master-button states, not aggregates);
    # see binary_sensor.py for both. Their registry entries would
    # otherwise linger forever as dead "restored" entities, so prune
    # them here.
    retired_unique_id_prefixes = (
        f"{entry.entry_id}_scene_",
        f"{entry.entry_id}_lights_central_",
        f"{entry.entry_id}_blinds_central_",
    )
    registry = er.async_get(hass)
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if reg_entry.unique_id.startswith(retired_unique_id_prefixes):
            registry.async_remove(reg_entry.entity_id)

    # Wait for the bootstrap + a brief observation window so the initial
    # broadcast pushes (TEMPIST<N>, PACKETBOX<N>, REGEN, ...) and the
    # chart-stand replies have a chance to land before platforms
    # enumerate entities. Failing to bootstrap doesn't abort setup —
    # the connection background task keeps retrying, and the
    # connectivity binary_sensor will report the WS as down.
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(SETUP_OBSERVATION_WINDOW + 10):
            await client.wait_for_bootstrap()
            await asyncio.sleep(SETUP_OBSERVATION_WINDOW)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Smart Place config entry — clean up the client and platforms."""
    data: SmartPlaceData | None = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if data is not None:
        await data.client.aclose()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
