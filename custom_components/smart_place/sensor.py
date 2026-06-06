"""Sensor platform for Smart Place.

Exposes the read-only metrics surfaced by the WS dispatch:

- Outdoor temperature (TEMPOUT)
- Wind speed (WINDGESCHWINDIGKEIT)
- One consumption sensor per discovered chart, named from
  ``ChartDefinition.label`` (e.g. ``Electricity``, ``Heating``,
  ``Cold water 1``, ``Cold water total``). Both per-meter and the
  aggregated SUMME charts surface — they expose the same data at
  different aggregation levels and consumers can pick whichever
  fits. The sensor state is the daily ``STAND1`` reading; the
  other STAND series surface as ``stand<N>`` attributes, plus
  ``target`` / ``target_percent`` / ``target_status`` (green /
  orange / red — same thresholds the SPA uses) when a
  ``CHARTZIEL<id>`` value has been observed.
- One ``Package delivery PIN`` sensor — the unlock PIN for a waiting
  parcel, extracted from the ``PERSINFO`` delivery banner; ``None``
  (HA ``unknown``) when no delivery is waiting. The SPA carries the
  PIN as free text in PERSINFO, *not* in ``PACKETBOX<N>`` (which only
  ever reported ``Frei`` in practice) — see ``state.py``.
- One indoor temperature sensor + one humidity sensor per climate
  zone, paired under a per-zone HA sub-device
  (``via_device=<entry>``) so they render together in the device
  card. The zone's room name labels the sub-device. Sensor IDs
  without a matching ``Klimas<N>`` zone are skipped.
- One intercom sensor per id seen in ``SPRECHEN<N>`` /
  ``CALLINFO<N>`` — state is the translated caller location while
  ringing, ``None`` while idle. The raw ``ringing`` flag and the
  last-seen ``caller`` surface as attributes.
- One aggregated ``Infoboard`` sensor — state is the first
  non-``Read`` slot body in slot-id order; ``None`` when every
  slot is cleared. Per-slot bodies surface as ``slot<n>``
  attributes.
- One ``Personal info`` text sensor for ``PERSINFO`` (same
  ``Read`` → ``None`` convention).
- An aggregated ``Weather alarm`` rollup — a single line summarising
  which alarms are currently on, computed from the shared snapshot.
  HA's built-in ``group`` integration is for user-defined helpers,
  not integrations: the idiomatic pattern is just a normal sensor
  whose ``native_value`` is computed from the shared snapshot.

Discovery is observation-based: entities are created once during
``async_setup_entry`` from whatever the state snapshot holds at that
moment (see ``__init__.py``'s observation window). A new sensor ID
that first appears later needs an HA reload to surface.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE, UnitOfEnergy, UnitOfSpeed, UnitOfTemperature, UnitOfVolume
from homeassistant.helpers.device_registry import DeviceInfo

from . import SmartPlaceData
from .const import DOMAIN
from .smart_place_client import chart_target_status

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback


# Daily series — STAND1 carries today's consumption so far. State
# resets to 0 at midnight, so ``TOTAL_INCREASING`` is the correct
# state class (HA treats the value-drop as a period reset and
# continues accumulating deltas in the Energy / Water dashboard).
_DAILY_SERIES: int = 1


# Chart units are emitted by the SPA as opaque tokens (``unit-KWh``,
# ``unit-l``, etc.). Mapping table: token -> (device_class, native_unit).
_CHART_UNIT_MAP: dict[str, tuple[SensorDeviceClass, str]] = {
    "KWh": (SensorDeviceClass.ENERGY, UnitOfEnergy.KILO_WATT_HOUR),
    "kWh": (SensorDeviceClass.ENERGY, UnitOfEnergy.KILO_WATT_HOUR),
    "Wh": (SensorDeviceClass.ENERGY, UnitOfEnergy.WATT_HOUR),
    "l": (SensorDeviceClass.WATER, UnitOfVolume.LITERS),
    "L": (SensorDeviceClass.WATER, UnitOfVolume.LITERS),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Build sensor entities from the observation-window snapshot."""
    data: SmartPlaceData = hass.data[DOMAIN][entry.entry_id]
    state = data.state

    entities: list[SensorEntity] = []

    if state.outdoor_temperature is not None:
        entities.append(SmartPlaceOutdoorTemperatureSensor(entry, data))
    if state.wind_speed is not None:
        entities.append(SmartPlaceWindSpeedSensor(entry, data))
    # Always created (not observation-gated): a delivery PIN is
    # transient and rare, so gating on its presence at setup time would
    # mean the sensor almost never exists. As a permanent entity it
    # reads ``unknown`` between deliveries and shows the PIN while a
    # parcel is waiting.
    entities.append(SmartPlacePackageDeliveryPinSensor(entry, data))
    if state.rain_alarm is not None or state.hail_alarm is not None or state.wind_alarms:
        entities.append(SmartPlaceWeatherAlarmSensor(entry, data))
    # Per-zone temperature + humidity, both attached to a per-zone
    # sub-device so they render together in HA's device card.
    # ``TEMPIST<N>`` pairs 1:1 with ``Klimas<N>``; humidity follows
    # the same id convention.
    for zone_id in sorted(state.climate_zones):
        if zone_id in state.indoor_temperatures:
            entities.append(SmartPlaceIndoorTemperatureSensor(entry, data, zone_id))
        if zone_id in state.humidities:
            entities.append(SmartPlaceHumiditySensor(entry, data, zone_id))
    intercom_ids = sorted(set(state.intercom_ringing) | set(state.intercom_callers))
    entities.extend(SmartPlaceIntercomSensor(entry, data, idx) for idx in intercom_ids)
    if state.infoboard_contents:
        entities.append(SmartPlaceInfoboardSensor(entry, data))
    if state.person_info is not None:
        entities.append(SmartPlacePersonInfoSensor(entry, data))
    for chart_id in sorted(state.charts):
        chart = state.charts[chart_id]
        unit = chart.unit or data.client.state.chart_units.get(chart_id, "")
        mapped = _CHART_UNIT_MAP.get(unit)
        if mapped is None:
            continue
        device_class, native_unit = mapped
        entities.append(
            SmartPlaceChartSensor(entry, data, chart_id, device_class, native_unit),
        )

    async_add_entities(entities)


def _climate_zone_device_info(entry: ConfigEntry, room: str, zone_id: int) -> DeviceInfo:
    """Return a per-zone sub-device that pairs temp + humidity under one card."""
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_zone_{zone_id}")},
        name=f"{room} climate",
        manufacturer="smart PLACE AG",
        via_device=(DOMAIN, entry.entry_id),
    )


class _SmartPlaceSensorBase(SensorEntity):
    """Common wiring: device_info, push-update subscription."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, data: SmartPlaceData) -> None:
        """Wire the shared per-entry device + state-listener machinery."""
        self._data = data
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Smart Place",
            manufacturer="smart PLACE AG",
        )

    @property
    def available(self) -> bool:
        """Mark the entity unavailable while the WS is disconnected."""
        return self._data.is_healthy

    async def async_added_to_hass(self) -> None:
        """Push state updates to HA on every parsed WS frame."""
        self._data.listeners.append(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        """Drop the push-update subscription on entity removal."""
        with contextlib.suppress(ValueError):
            self._data.listeners.remove(self.async_write_ha_state)


class SmartPlaceOutdoorTemperatureSensor(_SmartPlaceSensorBase):
    """Outdoor temperature reading from the TEMPOUT broadcast."""

    _attr_name = "Outdoor temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, entry: ConfigEntry, data: SmartPlaceData) -> None:
        """Wire the entry-scoped unique_id."""
        super().__init__(entry, data)
        self._attr_unique_id = f"{entry.entry_id}_outdoor_temperature"

    @property
    def native_value(self) -> float | None:
        """Return the latest outdoor temperature, or None if never observed."""
        return self._data.state.outdoor_temperature


class SmartPlaceWindSpeedSensor(_SmartPlaceSensorBase):
    """Wind speed reading from the WINDGESCHWINDIGKEIT broadcast.

    Unit confirmed two ways: the SPA's web UI renders it as ``km/h``,
    and the bootstrap StatusEntry for this frame embeds the hint
    explicitly — ``StatusInhaltListe_1_4_SPtext393>WINDGESCHWINDIGKEIT~
    SPDB-REM>unit-km/h~>LinkOff`` (captured 2026-05-28). We could pull
    that hint dynamically the way ``parse_chart_references`` does for
    charts, but with only two unit-bearing singletons (wind + outdoor
    temperature, both stable across installations) hard-coding stays
    simpler.
    """

    _attr_name = "Wind speed"
    _attr_device_class = SensorDeviceClass.WIND_SPEED
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfSpeed.KILOMETERS_PER_HOUR

    def __init__(self, entry: ConfigEntry, data: SmartPlaceData) -> None:
        """Wire the entry-scoped unique_id."""
        super().__init__(entry, data)
        self._attr_unique_id = f"{entry.entry_id}_wind_speed"

    @property
    def native_value(self) -> float | None:
        """Return the latest wind speed, or None if never observed."""
        return self._data.state.wind_speed


class SmartPlaceIndoorTemperatureSensor(_SmartPlaceSensorBase):
    """Indoor temperature reading from a ``TEMPIST<N>`` broadcast.

    Lives under the per-zone ``<room> climate`` sub-device so it
    pairs with the humidity sensor of the same zone in HA's device
    card. The display name (``Temperature``) inherits the zone name
    via ``has_entity_name``.
    """

    _attr_translation_key = "temperature"
    _attr_name = "Temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(
        self,
        entry: ConfigEntry,
        data: SmartPlaceData,
        sensor_id: int,
    ) -> None:
        """Wire the per-zone unique_id + sub-device."""
        super().__init__(entry, data)
        self._sensor_id = sensor_id
        self._attr_unique_id = f"{entry.entry_id}_indoor_temperature_{sensor_id}"
        room = data.state.climate_zones.get(sensor_id) or f"Zone {sensor_id}"
        self._attr_device_info = _climate_zone_device_info(entry, room, sensor_id)

    @property
    def native_value(self) -> float | None:
        """Return the latest reading for our TEMPIST<N> sensor."""
        return self._data.state.indoor_temperatures.get(self._sensor_id)


class SmartPlaceHumiditySensor(_SmartPlaceSensorBase):
    """Indoor humidity reading from a ``FEUCHTEIST<N>`` broadcast.

    Shares the per-zone sub-device with the matching temperature
    sensor (1:1 by ``Klimas<N>`` id) so HA's device card renders
    both side by side.
    """

    _attr_name = "Humidity"
    _attr_device_class = SensorDeviceClass.HUMIDITY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(
        self,
        entry: ConfigEntry,
        data: SmartPlaceData,
        zone_id: int,
    ) -> None:
        """Wire the per-zone unique_id + sub-device."""
        super().__init__(entry, data)
        self._zone_id = zone_id
        self._attr_unique_id = f"{entry.entry_id}_humidity_{zone_id}"
        room = data.state.climate_zones.get(zone_id) or f"Zone {zone_id}"
        self._attr_device_info = _climate_zone_device_info(entry, room, zone_id)

    @property
    def native_value(self) -> float | None:
        """Return the latest humidity reading for our zone."""
        return self._data.state.humidities.get(self._zone_id)


class SmartPlaceChartSensor(_SmartPlaceSensorBase):
    """Daily consumption (electricity, heating, water) refreshed by polling.

    The state is the daily ``STAND1`` reading (today's consumption
    so far). Resets to 0 at midnight, which HA treats as a period
    boundary under ``TOTAL_INCREASING`` and accumulates deltas
    correctly in the Energy / Water dashboards. Other STAND series
    (week / month / year) surface as ``stand<N>`` attributes.

    The display name is read from ``ChartDefinition.label`` via the
    ``name`` property on each state write, so a late-arriving
    ``ChartDefinition`` swaps the fallback ``Electricity chart 49``
    for ``Electricity`` without an HA reload.
    """

    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(
        self,
        entry: ConfigEntry,
        data: SmartPlaceData,
        chart_id: int,
        device_class: SensorDeviceClass,
        native_unit: str,
    ) -> None:
        """Wire the per-chart device class, unit, fallback label, and unique_id."""
        super().__init__(entry, data)
        self._chart_id = chart_id
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = native_unit
        self._attr_unique_id = f"{entry.entry_id}_chart_{chart_id}"
        kind = "Electricity" if device_class is SensorDeviceClass.ENERGY else "Water"
        self._fallback_name = f"{kind} chart {chart_id}"

    @property
    def name(self) -> str:
        """Return the chart's English label, or the generic fallback."""
        chart = self._data.state.charts.get(self._chart_id)
        if chart and chart.label:
            return chart.label
        return self._fallback_name

    @property
    def native_value(self) -> float | None:
        """Return the daily STAND1 reading, or None if not yet seen."""
        chart = self._data.state.charts.get(self._chart_id)
        if chart is None:
            return None
        return _safe_float(chart.stands.get(_DAILY_SERIES))

    @property
    def extra_state_attributes(self) -> dict[str, str | float | None]:
        """Expose non-daily STAND series + chart-target attributes.

        ``stand<N>`` carries each non-daily STAND series verbatim.
        ``target`` is the latest ``CHARTZIEL`` value for the chart;
        ``target_percent`` is current / target as a percentage; and
        ``target_status`` (``green`` / ``orange`` / ``red``) mirrors
        the SPA's own traffic-light thresholds (<60% / 60-90% /
        ≥90%). All three are ``None`` until both the STAND1
        reading and the target value have been observed.
        """
        chart = self._data.state.charts.get(self._chart_id)
        if chart is None:
            return {}
        attrs: dict[str, str | float | None] = {
            f"stand{series}": value for series, value in sorted(chart.stands.items()) if series != _DAILY_SERIES
        }
        target = self._data.state.chart_targets.get(self._chart_id)
        current = _safe_float(chart.stands.get(_DAILY_SERIES))
        attrs["target"] = target
        attrs["target_percent"] = round(current / target * 100, 1) if (target and current is not None) else None
        attrs["target_status"] = chart_target_status(current, target)
        return attrs


class SmartPlacePackageDeliveryPinSensor(_SmartPlaceSensorBase):
    """Parcel-box unlock PIN, extracted from the ``PERSINFO`` banner.

    A real delivery does *not* populate ``PACKETBOX<N>:<code>`` (the box
    keeps reporting ``Frei``). Instead the SPA pushes a free-text
    ``PERSINFO`` banner that contains the PIN, e.g. "Sie haben eine
    Lieferung in der Paketbox. Bitte verwenden Sie den PIN:4489 um diese
    rauszuholen." This sensor surfaces just the digits as a clean code,
    or ``None`` (HA ``unknown``) when no delivery is waiting. The full
    banner text is also available verbatim on the separate
    ``Personal info`` sensor — duplication is intentional.

    No polling needed: ``PERSINFO`` is a broadcast, so the entity only
    updates when the server pushes a fresh banner.
    """

    _attr_icon = "mdi:lock-open-variant"

    def __init__(self, entry: ConfigEntry, data: SmartPlaceData) -> None:
        """Wire the entry-scoped name + unique_id."""
        super().__init__(entry, data)
        self._attr_name = "Package delivery PIN"
        self._attr_unique_id = f"{entry.entry_id}_package_delivery_pin"

    @property
    def native_value(self) -> str | None:
        """Return the parcel unlock PIN, or None when no delivery is waiting."""
        return self._data.state.package_delivery_pin


class SmartPlaceWeatherAlarmSensor(_SmartPlaceSensorBase):
    """Aggregated view: which weather alarms are currently on.

    Compacts ``Rain`` / ``Hail`` / per-zone ``WindAlarm<N>`` into a
    single state line — empty when all clear. Per-alarm booleans
    surface as attributes for finer-grained automations.
    """

    _attr_name = "Weather alarm"
    _attr_icon = "mdi:weather-cloudy-alert"

    def __init__(self, entry: ConfigEntry, data: SmartPlaceData) -> None:
        """Wire the entry-scoped unique_id."""
        super().__init__(entry, data)
        self._attr_unique_id = f"{entry.entry_id}_weather_alarm"

    def _active_labels(self) -> list[str]:
        state = self._data.state
        labels: list[str] = []
        if state.hail_alarm:
            labels.append("Hail")
        if state.rain_alarm:
            labels.append("Rain")
        labels.extend(f"Wind alarm zone {zone}" for zone in sorted(state.wind_alarms) if state.wind_alarms[zone])
        return labels

    @property
    def native_value(self) -> str | None:
        """Return a comma-joined alarm list, or None when nothing is active."""
        labels = self._active_labels()
        return ", ".join(labels) if labels else None

    @property
    def extra_state_attributes(self) -> dict[str, bool | list[int]]:
        """Per-source breakdown so automations can target a specific alarm."""
        state = self._data.state
        return {
            "rain": bool(state.rain_alarm),
            "hail": bool(state.hail_alarm),
            "wind_alarm_zones": sorted(zone for zone, on in state.wind_alarms.items() if on),
        }


class SmartPlaceIntercomSensor(_SmartPlaceSensorBase):
    """Per-intercom combined ringing + caller view.

    State is the translated caller location (e.g. ``Apartment
    entrance``) while ``SPRECHEN<n>`` is ``ring``; ``None`` otherwise.
    The raw ``ringing`` flag and the last-seen ``caller`` (even when
    idle) surface as attributes so automations can distinguish
    "rang from X" from "idle".
    """

    _attr_icon = "mdi:phone-incoming"

    def __init__(self, entry: ConfigEntry, data: SmartPlaceData, intercom_id: int) -> None:
        """Wire the per-intercom name + unique_id."""
        super().__init__(entry, data)
        self._intercom_id = intercom_id
        self._attr_name = f"Intercom {intercom_id}"
        self._attr_unique_id = f"{entry.entry_id}_intercom_{intercom_id}"

    @property
    def native_value(self) -> str | None:
        """Return the translated caller while ringing; None when idle."""
        state = self._data.state
        if not state.intercom_ringing.get(self._intercom_id):
            return None
        return state.intercom_callers.get(self._intercom_id)

    @property
    def extra_state_attributes(self) -> dict[str, str | bool | None]:
        """Expose the raw ringing flag + last-seen caller for automations."""
        state = self._data.state
        return {
            "ringing": bool(state.intercom_ringing.get(self._intercom_id)),
            "caller": state.intercom_callers.get(self._intercom_id),
        }


class SmartPlaceInfoboardSensor(_SmartPlaceSensorBase):
    """Aggregated infoboard view across every ``INFOBOARD<n>INHALT`` slot.

    State is the first non-``Read`` slot's body text in slot-id
    order (so users see "any active message"); ``None`` when every
    slot is acknowledged. Per-slot bodies surface as
    ``slot<n>`` attributes so finer-grained dashboards can still
    address them individually.
    """

    _attr_name = "Infoboard"
    _attr_icon = "mdi:message-text"

    def __init__(self, entry: ConfigEntry, data: SmartPlaceData) -> None:
        """Wire the entry-scoped unique_id."""
        super().__init__(entry, data)
        self._attr_unique_id = f"{entry.entry_id}_infoboard"

    @property
    def native_value(self) -> str | None:
        """Return the first non-``Read`` slot's body, or None when all are cleared."""
        for _slot_id, body in sorted(self._data.state.infoboard_contents.items()):
            if body is not None:
                return body
        return None

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        """Expose every slot's body as ``slot<n>`` so dashboards can target one."""
        return {f"slot{slot_id}": body for slot_id, body in sorted(self._data.state.infoboard_contents.items())}


class SmartPlacePersonInfoSensor(_SmartPlaceSensorBase):
    """``PERSINFO`` banner text — None while the SPA reports ``Read``.

    Surfaced primarily as a diagnostic: the semantics of the
    non-``Read`` payload aren't fully documented yet, and the SPA's
    own JS just renders the raw text. Tagged as diagnostic so it
    doesn't clutter the default device card.
    """

    _attr_name = "Personal info"
    _attr_icon = "mdi:account-alert"

    def __init__(self, entry: ConfigEntry, data: SmartPlaceData) -> None:
        """Wire the entry-scoped unique_id."""
        super().__init__(entry, data)
        self._attr_unique_id = f"{entry.entry_id}_person_info"

    @property
    def native_value(self) -> str | None:
        """Return the latest PERSINFO text, or None when cleared."""
        return self._data.state.person_info


def _safe_float(raw: str | None) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return None
