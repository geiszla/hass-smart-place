"""Binary sensor platform for Smart Place.

Two families of entities live here:

- ``SmartPlaceConnectionSensor`` — diagnostic; tracks WS health.
- Alarm sensors derived from broadcast pushes (``Rain``, ``Hail``,
  ``BlindsMaintenance``, ``WindAlarm<N>``). All carry
  ``device_class=PROBLEM``: they are the building's weather alarms
  (the ones that retract the blinds), not ambient weather readings,
  so a uniform Problem/OK reading fits all of them — including
  rain, which previously used ``MOISTURE`` and stuck out.

Scenes are not binary sensors: a trigger scene's ``SZENEN<N>`` flag
latches once it ran (verified live 2026-06-11 — six of twelve on
simultaneously), so on/off has no usable meaning there. They are scene,
switch and fan entities now (``scene.py`` / ``switch.py`` / ``fan.py``);
``async_setup_entry`` in ``__init__.py`` removes the binary sensors
registered for them before 2026-06.

``Any light on`` (``LEUCHTENZENTRAL<N>``) and ``Any blind closed``
(``JALZENTRAL<N>``) were likewise dropped: each flag is the state of
the SPA's per-type "All" master button, not an aggregate (verified
live 2026-06-11 — LEUCHTENZENTRAL1 read 00 while two lights were on;
the blinds menu wires JALZENTRAL1 to the same button mechanism, and
no JALZENTRAL broadcast has ever been observed). The truthful sources
are the per-device light and cover entities.

Parcel deliveries are exposed as a ``sensor`` entity (not binary):
the useful state is the unlock PIN (text), surfaced as the ``Package
delivery PIN`` sensor — see ``sensor.py``. The PIN rides in the
``PERSINFO`` banner, not in ``PACKETBOX<N>`` (which only ever reported
``Frei`` in practice). Intercoms are likewise exposed as ``sensor``
entities because the useful state is the caller location (text) — the
ringing flag surfaces as an attribute.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable
from functools import partial
from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory

from . import SmartPlaceConfigEntry, SmartPlaceData, category_device_info, main_device_info
from .entity import SmartPlacePushEntity, async_add_discovered

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
    """Add the Connection sensor and an alarm sensor per alarm the server reports."""
    data = entry.runtime_data
    state = data.state

    def collect() -> dict[Hashable, Callable[[], Entity]]:
        found: dict[Hashable, Callable[[], Entity]] = {"connection": partial(SmartPlaceConnectionSensor, entry, data)}
        if state.rain_alarm is not None:
            found["rain"] = partial(SmartPlaceRainSensor, entry, data)
        if state.hail_alarm is not None:
            found["hail"] = partial(SmartPlaceHailSensor, entry, data)
        if state.blinds_maintenance is not None:
            found["blinds_maintenance"] = partial(SmartPlaceBlindsMaintenanceSensor, entry, data)
        # The name depends on how many zones there are; from half a
        # bootstrap it would be wrong for good.
        if data.devices_listed:
            for zone_id in sorted(state.wind_alarms):
                found["wind_alarm", zone_id] = partial(SmartPlaceWindAlarmSensor, entry, data, zone_id)
        return found

    async_add_discovered(entry, async_add_entities, collect)


class _SmartPlaceBinarySensorBase(SmartPlacePushEntity, BinarySensorEntity):
    """Common wiring: shared device + push-update subscription.

    Availability + the change-gated frame subscription live in
    ``SmartPlacePushEntity``. Subclasses set ``_category`` to land on a
    category sub-device (its own dashboard card); the default ``None`` keeps
    the entity on the main Smart Place device.
    """

    _attr_has_entity_name = True
    _category: str | None = None

    def __init__(self, entry: SmartPlaceConfigEntry, data: SmartPlaceData) -> None:
        """Wire the shared per-entry device + state-listener machinery."""
        self._data = data
        self._attr_device_info = (
            category_device_info(entry, data, self._category) if self._category is not None else main_device_info(entry)
        )


class SmartPlaceConnectionSensor(_SmartPlaceBinarySensorBase):
    """Reports whether the Smart Place WebSocket is in a healthy phase."""

    _attr_name = "Connection"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, entry: SmartPlaceConfigEntry, data: SmartPlaceData) -> None:
        """Wire the entry-scoped unique_id."""
        super().__init__(entry, data)
        self._attr_unique_id = f"{entry.entry_id}_connection"

    @property
    def available(self) -> bool:
        """Always available — this entity's whole purpose is reporting WS health."""
        return True

    @property
    def is_on(self) -> bool:
        """Return True iff the session is in a healthy phase."""
        return self._data.is_healthy

    @property
    def extra_state_attributes(self) -> dict[str, bool] | None:
        """Flag dry run, so tooling can check it before calling a service."""
        return {"dry_run": True} if self._data.client.dry_run else None


class SmartPlaceRainSensor(_SmartPlaceBinarySensorBase):
    """Rain alarm — on iff the SPA reports a non-zero REGEN code."""

    _attr_name = "Rain alarm"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    # Hidden by default: the per-source alarms roll up into the
    # ``Weather alarm`` enum, which stays visible — these stay one UI
    # toggle away for anyone who wants the individual flags.
    _attr_entity_registry_visible_default = False
    _category = "Weather"

    def __init__(self, entry: SmartPlaceConfigEntry, data: SmartPlaceData) -> None:
        """Wire the entry-scoped unique_id."""
        super().__init__(entry, data)
        self._attr_unique_id = f"{entry.entry_id}_rain"

    @property
    def is_on(self) -> bool | None:
        """Return True iff a rain alarm is currently active."""
        return self._data.state.rain_alarm


class SmartPlaceHailSensor(_SmartPlaceBinarySensorBase):
    """Hail alarm — on iff the SPA reports a non-zero HAGEL code."""

    _attr_name = "Hail alarm"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    # Hidden by default — rolled up into the visible ``Weather alarm`` enum.
    _attr_entity_registry_visible_default = False
    _category = "Weather"

    def __init__(self, entry: SmartPlaceConfigEntry, data: SmartPlaceData) -> None:
        """Wire the entry-scoped unique_id."""
        super().__init__(entry, data)
        self._attr_unique_id = f"{entry.entry_id}_hail"

    @property
    def is_on(self) -> bool | None:
        """Return True iff a hail alarm is currently active."""
        return self._data.state.hail_alarm


class SmartPlaceBlindsMaintenanceSensor(_SmartPlaceBinarySensorBase):
    """Blinds-maintenance flag — on iff the SPA reports JALWARTUNG != 00."""

    _attr_name = "Blinds maintenance"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _category = "Weather"

    def __init__(self, entry: SmartPlaceConfigEntry, data: SmartPlaceData) -> None:
        """Wire the entry-scoped unique_id."""
        super().__init__(entry, data)
        self._attr_unique_id = f"{entry.entry_id}_blinds_maintenance"

    @property
    def is_on(self) -> bool | None:
        """Return True iff blinds maintenance mode is currently active."""
        return self._data.state.blinds_maintenance


class SmartPlaceWindAlarmSensor(_SmartPlaceBinarySensorBase):
    """Per-zone wind alarm — on iff WINDALARM<N> reports a non-zero code.

    The zone suffix only appears when more than one zone is known when
    the entity is created — with a single zone, "Wind alarm 1" is just
    noise.
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    # Hidden by default — rolled up into the visible ``Weather alarm`` enum.
    _attr_entity_registry_visible_default = False
    _category = "Weather"

    def __init__(self, entry: SmartPlaceConfigEntry, data: SmartPlaceData, zone_id: int) -> None:
        """Wire the per-zone name + unique_id."""
        super().__init__(entry, data)
        self._zone_id = zone_id
        suffix = f" {zone_id}" if len(data.state.wind_alarms) > 1 else ""
        self._attr_name = f"Wind alarm{suffix}"
        self._attr_unique_id = f"{entry.entry_id}_wind_alarm_{zone_id}"

    @property
    def is_on(self) -> bool | None:
        """Return True iff the zone's wind alarm is currently active."""
        return self._data.state.wind_alarms.get(self._zone_id)
