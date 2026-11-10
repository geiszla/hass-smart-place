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
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import SupportsResponse, callback
from homeassistant.helpers import (
    area_registry as ar,
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
    service,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.typing import UNDEFINED, UndefinedType
import probatio

from .blueprint_install import install_blueprints
from .const import (
    CAPTURE_PATH,
    CHART_POLL_INTERVAL,
    CONF_PROTECTED_SOCKETS,
    CONF_TOKEN,
    CONF_ZONE_AREAS_SET,
    DOMAIN,
    DRY_RUN,
    SETUP_BOOTSTRAP_TIMEOUT,
    SETUP_OBSERVATION_WINDOW,
    VALUE_BURST_TIMEOUT,
)
from .plan_view import async_build_plan_config
from .smart_place_client import (
    BLIND_KIND_AWNING,
    BLIND_KIND_FABRIC,
    BLIND_KIND_SLATTED,
    DEVICE_LIGHT,
    DISCOVERY_ORIGIN,
    LIGHT_KIND_DIMMER,
    LIGHT_KIND_LED,
    LIGHT_KIND_SOCKET,
    LIGHT_KIND_SWITCH,
    ServerFrame,
    SessionPhase,
    SmartPlaceClient,
    SmartPlaceState,
    blind_centre,
    install_token_redaction_filter,
    light_centre,
    suggest_zone,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from homeassistant.core import CALLBACK_TYPE, HomeAssistant, ServiceCall, ServiceResponse
    from homeassistant.helpers.typing import ConfigType

    from .dashboard import SmartPlaceDashboard
    from .smart_place_client import BlindConfig, CommandDefinition, LevelGroup, LightConfig
    from .smart_place_client.plan import Point

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SERVICE_ADJUST_SETPOINT = "adjust_setpoint"
SERVICE_GET_PLAN_CONFIG = "get_plan_config"
ATTR_STEPS = "steps"
ATTR_CONFIG_ENTRY_ID = "config_entry_id"

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.CLIMATE,
    Platform.COVER,
    Platform.FAN,
    Platform.IMAGE,
    Platform.LIGHT,
    Platform.SCENE,
    Platform.SENSOR,
    Platform.SWITCH,
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

MANUFACTURER = "smart PLACE AG"

# Category devices by category (the identifier suffix, lower-cased) and
# what each is called. The names are singular, like the titles of Home
# Assistant's own cards, and carry no "Smart Place": Home Assistant puts
# the device name in front of every entity name.
_CATEGORY_DEVICE_NAMES: dict[str, str] = {
    "Weather": "Weather",
    "Energy": "Energy",
    "Doors": "Door",
    "Cameras": "Camera",
    "Scenes": "Scene",
}

_LIGHT_MODELS: dict[str, str] = {
    LIGHT_KIND_SWITCH: "Light",
    LIGHT_KIND_LED: "LED light",
    LIGHT_KIND_DIMMER: "Dimmer",
    LIGHT_KIND_SOCKET: "Socket",
}
_BLIND_MODELS: dict[str, str] = {
    BLIND_KIND_SLATTED: "Slatted blind",
    BLIND_KIND_FABRIC: "Fabric blind",
    BLIND_KIND_AWNING: "Awning",
}

type SmartPlaceConfigEntry = ConfigEntry[SmartPlaceData]


@dataclass(slots=True)
class SmartPlaceData:
    """Per-config-entry runtime data, stored on ``entry.runtime_data``."""

    hass: HomeAssistant
    entry: SmartPlaceConfigEntry
    client: SmartPlaceClient
    state: SmartPlaceState
    # Registry id of the main device; every sub-device links to it with
    # ``via_device_id``.
    main_device_id: str
    listeners: list[Callable[[], None]] = field(default_factory=list)
    # Called with every command an entity is about to send (``entity.py``).
    command_listeners: list[Callable[[CommandDefinition, tuple[object, ...]], None]] = field(default_factory=list)
    # Identifiers of the devices this run created entities for. A device
    # outside this set is no longer provided and may be deleted by the user.
    device_identifiers: set[tuple[str, str]] = field(default_factory=set)
    # The sidebar dashboard, ``None`` if it could not be set up.
    dashboard: SmartPlaceDashboard | None = None
    # Scenes that became switches and level groups (by their level-1 scene
    # id) that became fans in this run; see ``entity.async_classify_scenes``.
    scene_switch_ids: set[int] = field(default_factory=set)
    level_group_ids: set[int] = field(default_factory=set)
    # True once the value burst that follows the first main-menu reply had
    # ``SETUP_OBSERVATION_WINDOW`` to arrive, or did not come within
    # ``VALUE_BURST_TIMEOUT``. Until then no entity is available and no
    # entity that depends on other frames is created; see ``devices_listed``.
    first_reply_observed: bool = False

    @property
    def is_healthy(self) -> bool:
        """Return True iff the WS is open AND the session is in a usable phase.

        Both legs are needed. ``client.connected`` flips False the moment
        the WS drops; the phase check covers the brief window while a
        new WS is open but bootstrap hasn't completed yet.
        """
        return self.client.connected and self.client.state.phase in HEALTHY_PHASES

    @property
    def devices_listed(self) -> bool:
        """Return True once this connection delivered its complete main-menu reply.

        The platforms create the control entities then, not per frame:
        room suggestions need every heating zone and scene grouping needs
        every scene. After the first reply it stays False until the value
        burst is in (``first_reply_observed``): a name that depends on a
        count (wind alarms, intercoms, doorbell mutes) and a state built
        from several frames (an intercom's caller) need the whole burst,
        and it follows the reply up to some seconds later.
        """
        return self.first_reply_observed and self.client.state.main_menu_loaded

    @property
    def protected(self) -> dict[str, frozenset[int]]:
        """Return the vendor ids no command may be sent for, by device family.

        Read from the entry options whenever it is asked for, so a changed
        option counts for the next command. Only sockets can be protected
        today; another family would be one more entry here.
        """
        return {DEVICE_LIGHT: frozenset(self.entry.options.get(CONF_PROTECTED_SOCKETS, ()))}

    @callback
    def async_notify_listeners(self) -> None:
        """Let every entity look at the state again."""
        # Isolate each entity: a raised listener (e.g. a bad native_value)
        # must not stop the remaining entities from seeing this update.
        for listener in list(self.listeners):
            try:
                listener()
            except Exception:
                _LOGGER.exception("Smart Place entity update listener raised; continuing")

    @callback
    def async_plan_changed(self) -> None:
        """Note that entities or plan images changed: the dashboard may look different now."""
        if self.dashboard is not None:
            self.dashboard.async_schedule_refresh()


def main_device_info(entry: SmartPlaceConfigEntry) -> DeviceInfo:
    """Reference to the main Smart Place device, shared by all platforms.

    Identifiers only: ``async_setup_entry`` registers the device itself
    (name, manufacturer, configuration URL) before any platform loads.
    """
    return DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})


def category_device_info(entry: SmartPlaceConfigEntry, data: SmartPlaceData, category: str) -> DeviceInfo:
    """Sub-device grouping one family of entities under its own dashboard card.

    ``category`` is a key of ``_CATEGORY_DEVICE_NAMES`` and, lower-cased,
    the identifier suffix. Hung off the main device via ``via_device_id``
    so HA's auto dashboard renders it as a separate card while the Devices
    page still nests it under Smart Place — the same pattern the per-room
    climate sub-devices use. Splitting the entities across these keeps the
    integration from rendering as one giant 40-entity card. No area on
    purpose: an area-wide call must not reach scenes or door openers.
    """
    identifier = (DOMAIN, f"{entry.entry_id}_cat_{category.lower()}")
    data.device_identifiers.add(identifier)
    return DeviceInfo(
        identifiers={identifier},
        name=_CATEGORY_DEVICE_NAMES[category],
        manufacturer=MANUFACTURER,
        via_device_id=data.main_device_id,
    )


def level_group_device_info(entry: SmartPlaceConfigEntry, data: SmartPlaceData, group: LevelGroup) -> DeviceInfo:
    """Return the device of one level group, named after the group alone.

    No "Smart Place" prefix: the fan takes the device name, and the voice
    assistant matches on that name ("Ventilation").
    """
    identifier = (DOMAIN, f"{entry.entry_id}_level_group_{group.scene_ids[0]}")
    data.device_identifiers.add(identifier)
    return DeviceInfo(
        identifiers={identifier},
        name=group.name,
        manufacturer=MANUFACTURER,
        via_device_id=data.main_device_id,
    )


def zone_device_identifier(entry: SmartPlaceConfigEntry, zone_id: int) -> tuple[str, str]:
    """Device identifier of a climate zone."""
    return (DOMAIN, f"{entry.entry_id}_zone_{zone_id}")


def zone_device_info(entry: SmartPlaceConfigEntry, data: SmartPlaceData, zone_id: int) -> DeviceInfo:
    """Return the per-zone sub-device: climate, temperature and humidity on one card.

    The device is named after the room alone — ``has_entity_name``
    composes friendly names as "<device> <entity>", so a "climate"
    qualifier in the device name produced clunky names like
    "Bedroom climate Temperature". "Bedroom Temperature" reads better
    and the model field carries the climate-zone context instead.
    """
    room = data.state.climate_zones.get(zone_id) or f"Zone {zone_id}"
    identifier = zone_device_identifier(entry, zone_id)
    data.device_identifiers.add(identifier)
    return DeviceInfo(
        identifiers={identifier},
        name=room,
        manufacturer=MANUFACTURER,
        model="Climate zone",
        suggested_area=room,
        via_device_id=data.main_device_id,
    )


def _suggested_area(
    entry: SmartPlaceConfigEntry,
    data: SmartPlaceData,
    name: str,
    centre: tuple[float, float] | None,
    plan_id: int | None,
) -> str | None:
    """Return the area to suggest for a light, socket or blind, if any.

    The area currently assigned to the device of the matching heating
    zone (so a renamed area is followed), else the zone's room name.
    """
    zone_id = suggest_zone(name, centre, plan_id, data.state.climate.values())
    if zone_id is None:
        return None
    hass = data.hass
    zone_device = dr.async_get(hass).async_get_device_by_identifier(
        zone_device_identifier(entry, zone_id), entry.entry_id
    )
    if (
        zone_device is not None
        and zone_device.area_id is not None
        and (area := ar.async_get(hass).async_get_area(zone_device.area_id)) is not None
    ):
        return area.name
    return data.state.climate[zone_id].room


def _light_device(entry: SmartPlaceConfigEntry, light: LightConfig) -> tuple[tuple[str, str], Point | None]:
    """Return the device identifier of a light or socket and, if it is placed, its marker centre on the plan."""
    centre = light_centre(light.left, light.top) if light.left is not None and light.top is not None else None
    return (DOMAIN, f"{entry.entry_id}_light_{light.id}"), centre


def _blind_device(entry: SmartPlaceConfigEntry, blind: BlindConfig) -> tuple[tuple[str, str], Point | None]:
    """Return the device identifier of a blind and, if it is placed, its marker centre on the plan."""
    centre = blind_centre(blind.left, blind.top) if blind.left is not None and blind.top is not None else None
    return (DOMAIN, f"{entry.entry_id}_blind_{blind.id}"), centre


def light_device_info(entry: SmartPlaceConfigEntry, data: SmartPlaceData, light: LightConfig) -> DeviceInfo:
    """Return the device of one light or socket.

    One device per light, named with the vendor name; its single entity
    takes the device name. The area is only a suggestion: HA applies it
    when it creates the device. Afterwards only a rename in the vendor
    system can move the device (``_async_follow_renames``).
    """
    identifier, centre = _light_device(entry, light)
    data.device_identifiers.add(identifier)
    return DeviceInfo(
        identifiers={identifier},
        name=light.name,
        manufacturer=MANUFACTURER,
        model=_LIGHT_MODELS.get(light.kind),
        suggested_area=_suggested_area(entry, data, light.name, centre, light.plan_id),
        via_device_id=data.main_device_id,
    )


def blind_device_info(entry: SmartPlaceConfigEntry, data: SmartPlaceData, blind: BlindConfig) -> DeviceInfo:
    """Return the device of one blind (see ``light_device_info``)."""
    identifier, centre = _blind_device(entry, blind)
    data.device_identifiers.add(identifier)
    return DeviceInfo(
        identifiers={identifier},
        name=blind.name,
        manufacturer=MANUFACTURER,
        model=_BLIND_MODELS.get(blind.kind),
        suggested_area=_suggested_area(entry, data, blind.name, centre, blind.plan_id),
        via_device_id=data.main_device_id,
    )


@callback
def _async_follow_renames(hass: HomeAssistant, entry: SmartPlaceConfigEntry, data: SmartPlaceData) -> None:
    """Give every light, socket and blind that was renamed in the vendor system its new name.

    Run after each complete main-menu reply. The device's area follows to
    the one suggested for the new name only while the device is still in
    the one suggested for the old name ("no suggestion" matches "no
    area"): an area the user chose is kept. The name a user gave the
    device in Home Assistant and the entity ids are not touched.
    """
    devices = dr.async_get(hass)
    areas = ar.async_get(hass)

    def in_area(device: dr.DeviceEntry, area_name: str | None) -> bool:
        if area_name is None:
            return device.area_id is None
        area = areas.async_get_area_by_name(area_name)
        return area is not None and device.area_id == area.id

    for (identifier, centre), listed in (
        *((_light_device(entry, light), light) for light in data.state.lights.values()),
        *((_blind_device(entry, blind), blind) for blind in data.state.blinds.values()),
    ):
        device = devices.async_get_device_by_identifier(identifier, entry.entry_id)
        if device is None or device.name == listed.name:
            continue
        assert device.name is not None
        old_area = _suggested_area(entry, data, device.name, centre, listed.plan_id)
        new_area = _suggested_area(entry, data, listed.name, centre, listed.plan_id)
        area_id: str | UndefinedType | None = UNDEFINED
        if new_area != old_area and in_area(device, old_area):
            area_id = None if new_area is None else areas.async_get_or_create(new_area).id
        _LOGGER.info("%s was renamed to %s in Smart Place", device.name, listed.name)
        devices.async_update_device(device.id, name=listed.name, area_id=area_id)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration's actions."""
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_ADJUST_SETPOINT,
        entity_domain=Platform.CLIMATE,
        schema={probatio.Required(ATTR_STEPS): int},
        func="async_adjust_setpoint",
    )

    async def get_plan_config(call: ServiceCall) -> ServiceResponse:
        entry = service.async_get_config_entry(call.hass, DOMAIN, call.data.get(ATTR_CONFIG_ENTRY_ID))
        return async_build_plan_config(call.hass, entry)

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_PLAN_CONFIG,
        get_plan_config,
        schema=probatio.Schema({probatio.Optional(ATTR_CONFIG_ENTRY_ID): cv.string}),
        supports_response=SupportsResponse.ONLY,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: SmartPlaceConfigEntry) -> bool:
    """Set up Smart Place from a config entry."""
    install_token_redaction_filter()
    if DRY_RUN:
        _LOGGER.warning("Smart Place dry run is on: commands are logged, not sent to the Smart Place server")

    token: str = entry.data[CONF_TOKEN]
    session = async_get_clientsession(hass)

    async def trigger_reauth() -> None:
        entry.async_start_reauth(hass)

    # Last (health, main menu received) pair propagated to entities. Drives
    # the extra fan-out in ``_on_frame``: the server re-broadcasts unchanged
    # values, so the first frames after a reconnect can be no-ops that the
    # fan-out gate would skip — leaving entities stuck "unavailable" — and
    # the end of a main-menu reply changes no value at all, yet it is the
    # moment the platforms look for new devices.
    notified_status = (False, False)

    # ``data`` is bound below but the closures only fire after
    # ``client.run()`` starts — which happens after the binding —
    # so Python's call-time free-variable lookup resolves cleanly.
    async def trigger_disconnect_notify() -> None:
        # Push the WS drop to every entity so ``available`` flips False
        # for the whole reconnect-backoff gap (otherwise listeners only
        # wake on the next frame, which doesn't arrive until reconnect).
        # Control values of the lost connection must not decide a toggle
        # after the reconnect; the server replays them then.
        nonlocal notified_status
        data.state.clear_control_values()
        notified_status = (False, False)
        # The next connection's value burst starts the window again.
        stop_observation()
        data.async_notify_listeners()

    async def plan_image_changed() -> None:
        # No frame arrives with a fetched image; wake the image entities.
        data.async_notify_listeners()
        data.async_plan_changed()

    client = SmartPlaceClient.live(
        token=token,
        session=session,
        capture=CAPTURE_PATH,
        chart_poll_interval=CHART_POLL_INTERVAL,
        on_reauth=trigger_reauth,
        on_disconnect=trigger_disconnect_notify,
        on_plan_image=plan_image_changed,
        dry_run=DRY_RUN,
    )

    # Registered before any platform so sub-devices can link to it by id
    # (the identifier-based ``via_device`` is deprecated and raises when HA
    # cannot attribute the call to an integration). ``configuration_url``
    # deep-links to the vendor SPA with the entry's current token so
    # automations can template it via ``device_attr(..., 'configuration_url')``
    # instead of hardcoding the token — a re-auth refreshes the URL on the
    # post-reauth reload.
    main_device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name="Smart Place",
        manufacturer=MANUFACTURER,
        configuration_url=f"{DISCOVERY_ORIGIN}/Start5?{token}",
    )

    data = SmartPlaceData(
        hass=hass,
        entry=entry,
        client=client,
        state=SmartPlaceState(),
        main_device_id=main_device.id,
    )
    data.device_identifiers.add((DOMAIN, entry.entry_id))
    entry.runtime_data = data

    # The first main-menu reply is followed by the value burst (leuchte<N>,
    # JALICO<N>, TEMPSOLL<N>, ..., WINDALARM<N>, SPRECHEN<N>, MUTE<N>,
    # CALLINFO<N>, PERSINFO, SZENEN<N>, ...): at once, or up to 5 s later
    # (captures of 2026-11-10). Entities are enumerated once that burst had
    # ``SETUP_OBSERVATION_WINDOW`` to land — at setup below, or later when
    # the first bootstrap is slower than the setup waits. A window started
    # by the reply itself ended before a late burst: the intercom sensor
    # was then created from the ring flag alone and got its caller two
    # frames later, which looked like a ring.
    observed = asyncio.Event()
    cancel_observation: CALLBACK_TYPE | None = None
    # Whether the running timer is the window after the burst, not the wait for the burst.
    burst_seen = False

    @callback
    def observation_over(_now: datetime) -> None:
        nonlocal cancel_observation
        cancel_observation = None
        data.first_reply_observed = True
        observed.set()
        data.async_notify_listeners()

    @callback
    def burst_missing(now: datetime) -> None:
        _LOGGER.warning(
            "Smart Place did not send the values of all listed devices within %.0f s after its device list; "
            "the entities are created without them and take each value when it arrives",
            VALUE_BURST_TIMEOUT,
        )
        observation_over(now)

    @callback
    def stop_observation() -> None:
        nonlocal cancel_observation, burst_seen
        if cancel_observation is not None:
            cancel_observation()
            cancel_observation = None
        burst_seen = False

    entry.async_on_unload(stop_observation)

    async def _on_frame(frame: ServerFrame) -> None:
        nonlocal notified_status, cancel_observation, burst_seen
        # Chart range replies do not say which day they cover; the fold
        # stamps them with the range the client asked for.
        data.state.chart_range_request = client.state.chart_range_request
        changed = data.state.apply(frame)
        # Skip the per-entity fan-out when the snapshot didn't change — the
        # server re-broadcasts the full sensor set ~2-3x/s even when nothing
        # changes.
        status = (data.is_healthy, client.state.main_menu_loaded)
        if status[1] and not notified_status[1]:
            # This connection's list of devices is complete.
            _async_follow_renames(hass, entry, data)
        if changed or status != notified_status:
            data.async_notify_listeners()
        notified_status = status
        if client.state.main_menu_loaded and not data.first_reply_observed:
            if not burst_seen and data.state.control_values_received:
                stop_observation()
                burst_seen = True
                cancel_observation = async_call_later(hass, SETUP_OBSERVATION_WINDOW, observation_over)
            elif cancel_observation is None:
                # No burst (yet): the devices must not be withheld for good.
                cancel_observation = async_call_later(hass, VALUE_BURST_TIMEOUT, burst_missing)

    client.subscribe(_on_frame)

    entry.async_create_background_task(
        hass=hass,
        target=client.run(),
        name=f"smart_place_ws_{entry.entry_id}",
    )

    # Binary sensors exposed until 2026-06 and then dropped — scenes
    # (trigger scenes latch their flag, so on/off carried no usable
    # meaning) and the LEUCHTENZENTRAL / JALZENTRAL rollups (actually the
    # SPA's "All" master-button states, not aggregates). Their registry
    # entries would otherwise linger forever as dead "restored" entities,
    # so prune them here. Only in the binary_sensor domain: today's scene
    # entities use the same unique-id prefix.
    retired_unique_id_prefixes = (
        f"{entry.entry_id}_scene_",
        f"{entry.entry_id}_lights_central_",
        f"{entry.entry_id}_blinds_central_",
    )
    registry = er.async_get(hass)
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if reg_entry.domain == Platform.BINARY_SENSOR and reg_entry.unique_id.startswith(retired_unique_id_prefixes):
            registry.async_remove(reg_entry.entity_id)

    # Wait for the bootstrap, its value burst and the observation window so
    # everything normally appears at once. Failing to bootstrap doesn't abort
    # setup — the connection background task keeps retrying, the connectivity
    # binary_sensor reports the WS as down, and the platforms add the
    # entities when the server delivers them.
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(SETUP_BOOTSTRAP_TIMEOUT + SETUP_OBSERVATION_WINDOW):
            await observed.wait()

    _async_assign_zone_areas(hass, entry, data)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _async_setup_dashboard(hass, entry, data)
    await _async_install_blueprints(hass)
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
    return True


async def _async_entry_updated(hass: HomeAssistant, entry: SmartPlaceConfigEntry) -> None:
    """Show changed options at once.

    Nothing is reloaded: the entities read the options when they need
    them. Only what they show, and the plan dashboard, has to follow.
    """
    data = entry.runtime_data
    data.async_notify_listeners()
    data.async_plan_changed()


async def _async_install_blueprints(hass: HomeAssistant) -> None:
    """Copy the shipped automation blueprints; a failure costs the blueprints only."""
    target = Path(hass.config.path("blueprints", "automation", DOMAIN))
    try:
        await hass.async_add_executor_job(install_blueprints, target)
    except OSError as err:
        _LOGGER.warning(
            "The Smart Place automation blueprints could not be copied to %s (%s); everything else works", target, err
        )


@callback
def _async_setup_dashboard(hass: HomeAssistant, entry: SmartPlaceConfigEntry, data: SmartPlaceData) -> None:
    """Add the sidebar dashboard; a failure costs the dashboard only.

    It is built on Home Assistant internals (see ``dashboard.py``), so
    both the import and the registration are allowed to fail.
    """
    try:
        from .dashboard import async_register_dashboard  # noqa: PLC0415

        data.dashboard = async_register_dashboard(hass, entry)
    except Exception:  # HA internals; whatever fails, only the dashboard is lost
        _LOGGER.warning(
            "The Smart Place dashboard could not be set up; everything else works. "
            "The smart_place.get_plan_config action still returns its configuration",
            exc_info=True,
        )


@callback
def _async_assign_zone_areas(hass: HomeAssistant, entry: SmartPlaceConfigEntry, data: SmartPlaceData) -> None:
    """Give heating-zone devices without an area the area of their room, once.

    New zone devices get their area through ``suggested_area``, which HA
    only applies when it creates a device. This covers the zone devices
    of an install that predates it. The config-entry flag keeps it from
    ever running again, so an area the user clears later stays cleared.
    """
    if entry.data.get(CONF_ZONE_AREAS_SET) or not data.client.state.main_menu_loaded:
        return
    devices = dr.async_get(hass)
    areas = ar.async_get(hass)
    for zone in data.state.climate.values():
        device = devices.async_get_device_by_identifier(zone_device_identifier(entry, zone.id), entry.entry_id)
        if device is not None and device.area_id is None:
            devices.async_update_device(device.id, area_id=areas.async_get_or_create(zone.room).id)
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_ZONE_AREAS_SET: True})


async def async_unload_entry(hass: HomeAssistant, entry: SmartPlaceConfigEntry) -> bool:
    """Unload a Smart Place config entry — clean up the client, dashboard and platforms."""
    data = entry.runtime_data
    await data.client.aclose()
    if data.dashboard is not None:
        try:
            data.dashboard.async_remove()
        except Exception:  # HA internals; the unload must go on
            _LOGGER.warning("The Smart Place dashboard could not be removed", exc_info=True)
        data.dashboard = None
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    entry: SmartPlaceConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Allow deleting a device the server no longer lists.

    Devices are never removed automatically. A device this run created
    entities for is still provided and cannot be deleted.
    """
    return not device_entry.identifiers & entry.runtime_data.device_identifiers
