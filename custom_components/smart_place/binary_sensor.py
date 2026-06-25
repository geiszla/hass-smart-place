"""Binary sensor platform for Smart Place.

Two families of entities live here:

- ``SmartPlaceConnectionSensor`` — diagnostic; tracks WS health.
- Alarm sensors derived from broadcast pushes (``Rain``, ``Hail``,
  ``BlindsMaintenance``, ``WindAlarm<N>``). All carry
  ``device_class=PROBLEM``: they are the building's weather alarms
  (the ones that retract the blinds), not ambient weather readings,
  so a uniform Problem/OK reading fits all of them — including
  rain, which previously used ``MOISTURE`` and stuck out.

Scenes (``SceneConfig`` + ``SZENEN<N>``) are deliberately *not*
exposed: the server reports several "active" at once (verified live
2026-06-11 — six of twelve on simultaneously), so an on/off entity
has no usable meaning. The client library still parses and tracks
them in ``SmartPlaceState`` for future use; ``async_setup_entry``
in ``__init__.py`` removes the previously-registered scene entities
from the registry.

``Any light on`` (``LEUCHTENZENTRAL<N>``) and ``Any blind closed``
(``JALZENTRAL<N>``) were likewise dropped: each flag is the state of
the SPA's per-type "All" master button, not an aggregate (verified
live 2026-06-11 — LEUCHTENZENTRAL1 read 00 while two lights were on;
the blinds menu wires JALZENTRAL1 to the same button mechanism, and
no JALZENTRAL broadcast has ever been observed). Truthful
replacements should fold the per-device pushes — ``leuchte<n>``
(``LightState``) and ``JALICO<n>`` — which stay in the message
registry for that.

Parcel deliveries are exposed as a ``sensor`` entity (not binary):
the useful state is the unlock PIN (text), surfaced as the ``Package
delivery PIN`` sensor — see ``sensor.py``. The PIN rides in the
``PERSINFO`` banner, not in ``PACKETBOX<N>`` (which only ever reported
``Frei`` in practice). Intercoms are likewise exposed as ``sensor``
entities because the useful state is the caller location (text) — the
ringing flag surfaces as an attribute.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.helpers.entity import EntityCategory

from . import SmartPlaceData, category_device_info, main_device_info
from .const import DOMAIN
from .entity import SmartPlacePushEntity

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Build binary sensors from the observation-window snapshot + diagnostics."""
    data: SmartPlaceData = hass.data[DOMAIN][entry.entry_id]
    state = data.state

    entities: list[BinarySensorEntity] = [SmartPlaceConnectionSensor(entry, data)]

    if state.rain_alarm is not None:
        entities.append(SmartPlaceRainSensor(entry, data))
    if state.hail_alarm is not None:
        entities.append(SmartPlaceHailSensor(entry, data))
    if state.blinds_maintenance is not None:
        entities.append(SmartPlaceBlindsMaintenanceSensor(entry, data))
    entities.extend(SmartPlaceWindAlarmSensor(entry, data, zone_id) for zone_id in sorted(state.wind_alarms))

    async_add_entities(entities)


class _SmartPlaceBinarySensorBase(SmartPlacePushEntity, BinarySensorEntity):
    """Common wiring: shared device + push-update subscription.

    Availability + the change-gated frame subscription live in
    ``SmartPlacePushEntity``. Subclasses set ``_category`` to land on a
    category sub-device (its own dashboard card); the default ``None`` keeps
    the entity on the main Smart Place device.
    """

    _attr_has_entity_name = True
    _category: str | None = None

    def __init__(self, entry: ConfigEntry, data: SmartPlaceData) -> None:
        """Wire the shared per-entry device + state-listener machinery."""
        self._data = data
        self._attr_device_info = (
            category_device_info(entry, self._category) if self._category is not None else main_device_info(entry)
        )


class SmartPlaceConnectionSensor(_SmartPlaceBinarySensorBase):
    """Reports whether the Smart Place WebSocket is in a healthy phase."""

    _attr_name = "Connection"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, entry: ConfigEntry, data: SmartPlaceData) -> None:
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


class SmartPlaceRainSensor(_SmartPlaceBinarySensorBase):
    """Rain alarm — on iff the SPA reports a non-zero REGEN code."""

    _attr_name = "Rain alarm"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _category = "Weather"

    def __init__(self, entry: ConfigEntry, data: SmartPlaceData) -> None:
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
    _category = "Weather"

    def __init__(self, entry: ConfigEntry, data: SmartPlaceData) -> None:
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

    def __init__(self, entry: ConfigEntry, data: SmartPlaceData) -> None:
        """Wire the entry-scoped unique_id."""
        super().__init__(entry, data)
        self._attr_unique_id = f"{entry.entry_id}_blinds_maintenance"

    @property
    def is_on(self) -> bool | None:
        """Return True iff blinds maintenance mode is currently active."""
        return self._data.state.blinds_maintenance


class SmartPlaceWindAlarmSensor(_SmartPlaceBinarySensorBase):
    """Per-zone wind alarm — on iff WINDALARM<N> reports a non-zero code.

    The zone suffix only appears when more than one zone has been
    discovered — with a single zone, "Wind alarm 1" is just noise.
    A second zone surfacing later goes through an HA reload anyway
    (observation-based discovery), which re-derives the names.
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _category = "Weather"

    def __init__(self, entry: ConfigEntry, data: SmartPlaceData, zone_id: int) -> None:
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
