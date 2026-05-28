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
  other STAND series surface as ``stand<N>`` attributes.
- One package-box sensor per ``PACKETBOX<N>`` index. The state is
  the package's unlock code while a package is waiting in the box,
  and ``None`` (HA renders as ``unknown`` → effectively "free")
  while the box is empty (``PACKETBOX<N>:Frei``).
- One indoor temperature sensor per ``TEMPIST<N>`` where the
  matching ``Klimas<N>`` climate zone exists in the snapshot — the
  zone's room name labels the sensor. Sensor IDs without a matching
  zone are skipped (we can't surface a meaningful name for them).
- Aggregated rollups computed from the snapshot — ``Active package
  box code`` (first non-Frei code across all boxes) and ``Weather
  alarm`` (single line summarising which alarms are currently on).
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
from homeassistant.const import UnitOfEnergy, UnitOfSpeed, UnitOfTemperature, UnitOfVolume
from homeassistant.helpers.device_registry import DeviceInfo

from . import SmartPlaceData
from .const import DOMAIN

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
    if state.package_boxes:
        entities.append(SmartPlaceActivePackageCodeSensor(entry, data))
        entities.extend(SmartPlacePackageBoxSensor(entry, data, box_id) for box_id in sorted(state.package_boxes))
    if state.rain_alarm is not None or state.hail_alarm is not None or state.wind_alarms:
        entities.append(SmartPlaceWeatherAlarmSensor(entry, data))
    # Only expose temp sensors whose climate-zone room name we know;
    # the SPA pairs ``TEMPIST<N>`` with ``Klimas<N>`` 1:1.
    entities.extend(
        SmartPlaceIndoorTemperatureSensor(entry, data, sensor_id)
        for sensor_id in sorted(state.indoor_temperatures)
        if sensor_id in state.climate_zones
    )
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
    """Wind speed reading from the WINDGESCHWINDIGKEIT broadcast."""

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

    The display name comes from the matching ``Klimas<N>`` climate
    zone (``state.climate_zones[N]``) and is read via the ``name``
    property on every state write — so a late-arriving
    ``ClimateConfig`` updates the entity name without needing an
    HA reload. Sensors with no matching zone are dropped at
    platform setup time.
    """

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(
        self,
        entry: ConfigEntry,
        data: SmartPlaceData,
        sensor_id: int,
    ) -> None:
        """Wire the per-sensor unique_id."""
        super().__init__(entry, data)
        self._sensor_id = sensor_id
        self._attr_unique_id = f"{entry.entry_id}_indoor_temperature_{sensor_id}"

    @property
    def name(self) -> str:
        """Return ``<room> temperature`` from the current snapshot."""
        room = self._data.state.climate_zones.get(self._sensor_id) or "Indoor"
        return f"{room} temperature"

    @property
    def native_value(self) -> float | None:
        """Return the latest reading for our TEMPIST<N> sensor."""
        return self._data.state.indoor_temperatures.get(self._sensor_id)


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
    def extra_state_attributes(self) -> dict[str, str]:
        """Expose the non-daily STAND series as ``stand<N>`` attributes."""
        chart = self._data.state.charts.get(self._chart_id)
        if chart is None:
            return {}
        return {f"stand{series}": value for series, value in sorted(chart.stands.items()) if series != _DAILY_SERIES}


class SmartPlacePackageBoxSensor(_SmartPlaceSensorBase):
    """One ``PACKETBOX<N>`` broadcast: shows the package's unlock code.

    The Smart Place SPA pushes ``PACKETBOX<N>:Frei`` (German for
    "free") while the box is empty, and ``PACKETBOX<N>:<code>`` when
    a package is waiting — ``<code>`` is the same string the SPA
    shows the user on the main panel to open the box. We surface the
    code as the sensor's state, mapping ``Frei`` to ``None`` so HA
    leaves the state "unknown" until a package actually arrives.

    No polling needed: ``PACKETBOX<N>`` is a broadcast (no read
    command exists), so the entity only updates when the server
    pushes a fresh value.
    """

    _attr_icon = "mdi:package-variant-closed"

    def __init__(
        self,
        entry: ConfigEntry,
        data: SmartPlaceData,
        box_id: int,
    ) -> None:
        """Wire the per-box name + unique_id."""
        super().__init__(entry, data)
        self._box_id = box_id
        self._attr_name = f"Package box {box_id} code"
        self._attr_unique_id = f"{entry.entry_id}_package_box_{box_id}_code"

    @property
    def native_value(self) -> str | None:
        """Return the package unlock code, or None while the box is free."""
        raw = self._data.state.package_boxes.get(self._box_id)
        if raw is None or raw == _PACKAGE_BOX_FREE:
            return None
        return raw


_PACKAGE_BOX_FREE: str = "Frei"


class SmartPlaceActivePackageCodeSensor(_SmartPlaceSensorBase):
    """Aggregated view: the unlock code of the first occupied package box.

    Iterates the per-box snapshot in id order and returns the first
    non-``Frei`` value. ``None`` while every box is free. The matching
    box id is exposed as a ``box`` attribute so dashboards can render
    "Box 2: ABCD-1234" without subscribing to N entities.
    """

    _attr_name = "Active package box code"
    _attr_icon = "mdi:package-variant"

    def __init__(self, entry: ConfigEntry, data: SmartPlaceData) -> None:
        """Wire the entry-scoped unique_id."""
        super().__init__(entry, data)
        self._attr_unique_id = f"{entry.entry_id}_active_package_code"

    def _first_occupied(self) -> tuple[int, str] | None:
        for box_id in sorted(self._data.state.package_boxes):
            raw = self._data.state.package_boxes[box_id]
            if raw and raw != _PACKAGE_BOX_FREE:
                return box_id, raw
        return None

    @property
    def native_value(self) -> str | None:
        """Return the first occupied box's code, or None when all boxes are free."""
        active = self._first_occupied()
        return active[1] if active else None

    @property
    def extra_state_attributes(self) -> dict[str, int | str]:
        """Return ``{"box": <id>}`` while occupied; empty when all boxes are free."""
        active = self._first_occupied()
        return {"box": active[0]} if active else {}


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


def _safe_float(raw: str | None) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return None
