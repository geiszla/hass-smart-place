"""Sensor platform for Smart Place.

Exposes the read-only metrics surfaced by the WS dispatch:

- Outdoor temperature (TEMPOUT)
- Wind speed (WINDGESCHWINDIGKEIT)
- One consumption sensor per discovered chart, named from
  ``ChartDefinition.label`` plus a ``(today)`` suffix (e.g.
  ``Electricity (today)``, ``Cold water (today)``). Per-meter charts
  that roll up into a SUMME aggregate (``Cold water I`` / ``II``)
  are registered disabled by default — the aggregate carries the
  reading users care about. The sensor state is the daily ``STAND1``
  reading; the other STAND series surface as ``this_week`` /
  ``this_month`` / ``this_year`` / ``lifetime`` attributes, plus
  ``target`` / ``target_percent`` / ``target_status`` (green /
  orange / red — same thresholds the SPA uses) when a
  ``CHARTZIEL<id>`` value has been observed.
- For each chart with a daily target, two companion entities surface
  the SPA's traffic-light system as first-class state: ``<Chart>
  target use`` (today's consumption as % of target — feeds gauge
  cards with severity thresholds 60/90 and ``numeric_state``
  triggers) and ``<Chart> target status`` (a ``green`` / ``orange``
  / ``red`` enum for state-trigger automations and logbook history).
  Both follow the parent chart's enabled-by-default rule.
- One ``Package delivery PIN`` sensor — the unlock PIN for a waiting
  parcel, extracted from the ``PERSINFO`` delivery banner; ``None``
  (HA ``unknown``) when no delivery is waiting. The SPA carries the
  PIN as free text in PERSINFO, *not* in ``PACKETBOX<N>`` (which only
  ever reported ``Frei`` in practice) — see ``state.py``.
- One indoor temperature sensor + one humidity sensor per climate
  zone, paired under a per-zone HA sub-device
  (``via_device=<entry>``) so they render together in the device
  card. The zone's room name labels the sub-device. Sensor IDs
  without a matching ``Klimas<N>`` zone are skipped. Humidity is
  disabled by default — this building reports a constant 0.0 —
  and both fold the 0.0 placeholder to ``unknown``.
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
- An aggregated ``Weather alarm`` enum rollup — ``none`` / ``rain``
  / ``hail`` / ``wind`` / ``multiple``, computed from the shared
  snapshot. HA's built-in ``group`` integration is for user-defined
  helpers, not integrations: the idiomatic pattern is just a normal
  sensor whose ``native_value`` is computed from the shared snapshot.

Discovery is observation-based: entities are created once during
``async_setup_entry`` from whatever the state snapshot holds at that
moment (see ``__init__.py``'s observation window). A new sensor ID
that first appears later needs an HA reload to surface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE, UnitOfEnergy, UnitOfSpeed, UnitOfTemperature, UnitOfVolume
from homeassistant.helpers.device_registry import DeviceInfo

from . import SmartPlaceData, category_device_info, main_device_info
from .const import DOMAIN
from .entity import SmartPlacePushEntity
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

# Friendly attribute names for the non-daily STAND series. Semantics
# are empirical (live diff 2026-06-11): the week/month/year buckets fit
# every water, heating, and electricity chart (e.g. Wärme reads
# 0/0/1309 in June — heating off all summer, year carries the winter),
# and 99 is unambiguously the lifetime meter reading. Series not
# listed here (79/89/96-98, populated only for electricity) keep their
# raw ``stand<N>`` key until their meaning is confirmed.
_STAND_SERIES_NAMES: dict[int, str] = {
    2: "this_week",
    3: "this_month",
    4: "this_year",
    99: "lifetime",
}

# The server pushes a constant ``0.0`` for sensors the building does
# not actually have (verified live 2026-06-11: every FEUCHTEIST zone
# reads 0.0 around the clock). Indoor readings of exactly 0.0 are
# physically implausible in an occupied building, so both the
# humidity and indoor-temperature sensors fold 0.0 to ``None`` (HA
# ``unknown``) rather than display a fake measurement. Outdoor
# temperature and wind speed are exempt — 0.0 is a legitimate reading
# there.
_PLACEHOLDER_READING: float = 0.0


# Chart units are emitted by the SPA as opaque tokens (``unit-KWh``,
# ``unit-l``, etc.). Mapping table: token -> (device_class, native_unit).
_CHART_UNIT_MAP: dict[str, tuple[SensorDeviceClass, str]] = {
    "KWh": (SensorDeviceClass.ENERGY, UnitOfEnergy.KILO_WATT_HOUR),
    "kWh": (SensorDeviceClass.ENERGY, UnitOfEnergy.KILO_WATT_HOUR),
    "Wh": (SensorDeviceClass.ENERGY, UnitOfEnergy.WATT_HOUR),
    "l": (SensorDeviceClass.WATER, UnitOfVolume.LITERS),
    "L": (SensorDeviceClass.WATER, UnitOfVolume.LITERS),
}

# Unit tokens from the unit-bearing StatusEntry rows (``unit-°C`` on
# TEMPOUT, ``unit-km/h`` on WINDGESCHWINDIGKEIT — see
# ``parse_unit_hints``), mapped to proper HA units. HA validates
# ``native_unit_of_measurement`` against the device class, so the
# server token can't pass through verbatim; unknown or absent tokens
# fall back to the units this installation is known to use.
_TEMPERATURE_UNIT_MAP: dict[str, str] = {
    "°C": UnitOfTemperature.CELSIUS,
    "C": UnitOfTemperature.CELSIUS,
    "°F": UnitOfTemperature.FAHRENHEIT,
    "F": UnitOfTemperature.FAHRENHEIT,
}
_WIND_SPEED_UNIT_MAP: dict[str, str] = {
    "km/h": UnitOfSpeed.KILOMETERS_PER_HOUR,
    "m/s": UnitOfSpeed.METERS_PER_SECOND,
    "mph": UnitOfSpeed.MILES_PER_HOUR,
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
    # Always created (not observation-gated): the idle banner is
    # ``PERSINFO:Read`` which folds to ``None``, so gating on presence
    # would usually skip the sensor at setup and a later banner could
    # never surface (discovery is one-shot). Reads ``unknown`` when clear.
    entities.append(SmartPlacePersonInfoSensor(entry, data))
    # Per-meter charts that roll up into a SUMME aggregate (e.g.
    # ``Cold water I`` / ``II`` under ``Cold water``) are registered
    # disabled — the aggregate is the reading users care about, and
    # the constituents stay one UI toggle away.
    summed_chart_ids = {cid for chart in state.charts.values() for cid in chart.summed_chart_ids}
    for chart_id in sorted(state.charts):
        chart = state.charts[chart_id]
        unit = chart.unit or data.client.state.chart_units.get(chart_id, "")
        mapped = _CHART_UNIT_MAP.get(unit)
        if mapped is None:
            continue
        device_class, native_unit = mapped
        enabled_default = chart_id not in summed_chart_ids
        entities.append(
            SmartPlaceChartSensor(
                entry,
                data,
                chart_id,
                device_class,
                native_unit,
                enabled_default=enabled_default,
            ),
        )
        # Target companions only exist where the SPA sets a daily goal
        # (verified live 2026-06-11: targets on the four user-facing
        # charts, none on PV). CHARTZIEL arrives in the same bootstrap
        # as the chart definitions, so gating at setup time is safe.
        if chart_id in state.chart_targets:
            entities.append(SmartPlaceChartTargetPercentSensor(entry, data, chart_id, enabled_default=enabled_default))
            entities.append(SmartPlaceChartTargetStatusSensor(entry, data, chart_id, enabled_default=enabled_default))

    async_add_entities(entities)


def _climate_zone_device_info(entry: ConfigEntry, room: str, zone_id: int) -> DeviceInfo:
    """Return a per-zone sub-device that pairs temp + humidity under one card.

    The device is named after the room alone — ``has_entity_name``
    composes friendly names as "<device> <entity>", so a "climate"
    qualifier in the device name produced clunky names like
    "Bedroom climate Temperature". "Bedroom Temperature" reads better
    and the model field carries the climate-zone context instead.
    """
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_zone_{zone_id}")},
        name=room,
        manufacturer="smart PLACE AG",
        model="Climate zone",
        via_device=(DOMAIN, entry.entry_id),
    )


class _SmartPlaceSensorBase(SmartPlacePushEntity, SensorEntity):
    """Common wiring: device_info, push-update subscription.

    Availability + the change-gated frame subscription live in
    ``SmartPlacePushEntity``. Subclasses set ``_category`` to land on a
    category sub-device (its own dashboard card); the default ``None`` keeps
    the entity on the main Smart Place device. Subclasses that need a bespoke
    device (the per-zone climate sensors) override ``_attr_device_info`` after
    ``super().__init__``.
    """

    _attr_has_entity_name = True
    _category: str | None = None

    def __init__(self, entry: ConfigEntry, data: SmartPlaceData) -> None:
        """Wire the shared per-entry device + state-listener machinery."""
        self._data = data
        self._attr_device_info = (
            category_device_info(entry, self._category) if self._category is not None else main_device_info(entry)
        )


class SmartPlaceOutdoorTemperatureSensor(_SmartPlaceSensorBase):
    """Outdoor temperature reading from the TEMPOUT broadcast.

    The unit comes from the bootstrap's TEMPOUT status-row hint
    (``unit-°C`` on this installation), falling back to Celsius when
    the hint is missing or unrecognised.
    """

    _attr_name = "Outdoor temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    # Round the *displayed* value — including the 5-minute statistics
    # mean HA shows in the more-info detail view. HA's recorder averages
    # the raw float states, and summing IEEE doubles (e.g. 21.3 stored as
    # 21.299999999999997) leaks a long tail like ``21.300000000000004``
    # into that aggregate. ``suggested_display_precision`` is the only
    # lever that reaches the statistics display; rounding ``native_value``
    # doesn't, because HA re-averages the rounded states. See the chart
    # target-use sensor for the originally reported case.
    _attr_suggested_display_precision = 1
    _category = "Weather"

    def __init__(self, entry: ConfigEntry, data: SmartPlaceData) -> None:
        """Wire the entry-scoped unique_id + server-hinted unit."""
        super().__init__(entry, data)
        self._attr_unique_id = f"{entry.entry_id}_outdoor_temperature"
        hint = data.state.unit_hints.get("TEMPOUT", "")
        self._attr_native_unit_of_measurement = _TEMPERATURE_UNIT_MAP.get(hint, UnitOfTemperature.CELSIUS)

    @property
    def native_value(self) -> float | None:
        """Return the latest outdoor temperature, or None if never observed."""
        return self._data.state.outdoor_temperature


class SmartPlaceWindSpeedSensor(_SmartPlaceSensorBase):
    """Wind speed reading from the WINDGESCHWINDIGKEIT broadcast.

    The unit comes from the bootstrap's WINDGESCHWINDIGKEIT status-row
    hint — ``StatusInhaltListe_1_4_SPtext393>WINDGESCHWINDIGKEIT~
    SPDB-REM>unit-km/h~>LinkOff`` (captured 2026-05-28, re-verified
    2026-06-11) — falling back to ``km/h`` (what the SPA's web UI
    renders) when the hint is missing or unrecognised.
    """

    _attr_name = "Wind speed"
    _attr_device_class = SensorDeviceClass.WIND_SPEED
    _attr_state_class = SensorStateClass.MEASUREMENT
    # Keep the statistics mean clean — see SmartPlaceOutdoorTemperatureSensor.
    _attr_suggested_display_precision = 1
    _category = "Weather"

    def __init__(self, entry: ConfigEntry, data: SmartPlaceData) -> None:
        """Wire the entry-scoped unique_id + server-hinted unit."""
        super().__init__(entry, data)
        self._attr_unique_id = f"{entry.entry_id}_wind_speed"
        hint = data.state.unit_hints.get("WINDGESCHWINDIGKEIT", "")
        self._attr_native_unit_of_measurement = _WIND_SPEED_UNIT_MAP.get(hint, UnitOfSpeed.KILOMETERS_PER_HOUR)

    @property
    def native_value(self) -> float | None:
        """Return the latest wind speed, or None if never observed."""
        return self._data.state.wind_speed


class SmartPlaceIndoorTemperatureSensor(_SmartPlaceSensorBase):
    """Indoor temperature reading from a ``TEMPIST<N>`` broadcast.

    Lives under the per-zone room sub-device so it pairs with the
    humidity sensor of the same zone in HA's device card. The
    display name (``Temperature``) inherits the room name via
    ``has_entity_name`` (e.g. "Bedroom Temperature").
    """

    _attr_translation_key = "temperature"
    _attr_name = "Temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    # Keep the statistics mean clean — see SmartPlaceOutdoorTemperatureSensor.
    _attr_suggested_display_precision = 1

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
        """Return the latest reading, folding the 0.0 placeholder to unknown."""
        value = self._data.state.indoor_temperatures.get(self._sensor_id)
        return None if value == _PLACEHOLDER_READING else value


class SmartPlaceHumiditySensor(_SmartPlaceSensorBase):
    """Indoor humidity reading from a ``FEUCHTEIST<N>`` broadcast.

    Shares the per-zone sub-device with the matching temperature
    sensor (1:1 by ``Klimas<N>`` id) so HA's device card renders
    both side by side.

    Disabled by default: this building has no humidity measurement —
    the server pushes a constant 0.0 for every zone (verified live
    2026-06-11). The entity stays available to enable manually in
    case a future installation does report real values; 0.0 folds to
    ``unknown`` either way so a placeholder never reads as bone-dry air.
    """

    _attr_name = "Humidity"
    _attr_device_class = SensorDeviceClass.HUMIDITY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    # Whole-percent display; also keeps the statistics mean clean — see
    # SmartPlaceOutdoorTemperatureSensor.
    _attr_suggested_display_precision = 0
    _attr_entity_registry_enabled_default = False

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
        """Return the latest reading, folding the 0.0 placeholder to unknown."""
        value = self._data.state.humidities.get(self._zone_id)
        return None if value == _PLACEHOLDER_READING else value


class _SmartPlaceChartScopedSensor(_SmartPlaceSensorBase):
    """Shared wiring for chart-scoped sensors: label, daily reading, target.

    The display name is read from ``ChartDefinition.label`` via the
    ``name`` property on each state write, so a late-arriving
    ``ChartDefinition`` swaps the fallback label for the real one
    (e.g. ``Electricity``) without an HA reload. Subclasses set
    ``_name_suffix`` to distinguish themselves under the same label.
    """

    _name_suffix: str = ""
    _category = "Energy"

    def __init__(
        self,
        entry: ConfigEntry,
        data: SmartPlaceData,
        chart_id: int,
        *,
        enabled_default: bool = True,
    ) -> None:
        """Wire the per-chart id, fallback label, and enabled-default flag."""
        super().__init__(entry, data)
        self._chart_id = chart_id
        self._attr_entity_registry_enabled_default = enabled_default
        self._fallback_name = f"Chart {chart_id}"

    @property
    def name(self) -> str:
        """Return the chart's English label plus this sensor's suffix."""
        chart = self._data.state.charts.get(self._chart_id)
        label = chart.label if chart and chart.label else self._fallback_name
        return f"{label} {self._name_suffix}"

    def _daily_reading(self) -> float | None:
        """Return today's STAND1 reading, or None if not yet seen."""
        chart = self._data.state.charts.get(self._chart_id)
        if chart is None:
            return None
        return _safe_float(chart.stands.get(_DAILY_SERIES))

    def _target(self) -> float | None:
        """Return the latest CHARTZIEL daily target, or None if never pushed."""
        return self._data.state.chart_targets.get(self._chart_id)

    def _target_percent(self) -> float | None:
        """Return today's reading as a percentage of the daily target."""
        target = self._target()
        current = self._daily_reading()
        if not target or current is None:
            return None
        return round(current / target * 100, 1)


class SmartPlaceChartSensor(_SmartPlaceChartScopedSensor):
    """Daily consumption (electricity, heating, water) refreshed by polling.

    The state is the daily ``STAND1`` reading (today's consumption
    so far). Resets to 0 at midnight, which HA treats as a period
    boundary under ``TOTAL_INCREASING`` and accumulates deltas
    correctly in the Energy / Water dashboards. The other STAND
    series surface as named attributes (``this_week`` / ``this_month``
    / ``this_year`` / ``lifetime`` — see ``_STAND_SERIES_NAMES``).

    ``(today)`` is appended to the name so nobody mistakes the daily
    reading for a lifetime total.
    """

    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _name_suffix = "(today)"

    def __init__(
        self,
        entry: ConfigEntry,
        data: SmartPlaceData,
        chart_id: int,
        device_class: SensorDeviceClass,
        native_unit: str,
        *,
        enabled_default: bool = True,
    ) -> None:
        """Wire the per-chart device class, unit, fallback label, and unique_id."""
        super().__init__(entry, data, chart_id, enabled_default=enabled_default)
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = native_unit
        # Clean up the displayed consumption + its statistics deltas (see
        # SmartPlaceOutdoorTemperatureSensor). Water meters report whole
        # litres; energy (kWh) carries sub-watt-hour readings (PV runs as
        # low as ~0.005 kWh/day), so it keeps three decimals.
        self._attr_suggested_display_precision = 3 if device_class is SensorDeviceClass.ENERGY else 0
        self._attr_unique_id = f"{entry.entry_id}_chart_{chart_id}"
        kind = "Electricity" if device_class is SensorDeviceClass.ENERGY else "Water"
        self._fallback_name = f"{kind} chart {chart_id}"

    @property
    def native_value(self) -> float | None:
        """Return the daily STAND1 reading, or None if not yet seen."""
        return self._daily_reading()

    @property
    def extra_state_attributes(self) -> dict[str, str | float | None]:
        """Expose non-daily STAND series + chart-target attributes.

        Each non-daily STAND series surfaces under its friendly name
        (``this_week`` / ``this_month`` / ``this_year`` /
        ``lifetime``), falling back to the raw ``stand<N>`` key for
        series whose meaning is unconfirmed. Series the server sends
        empty are omitted entirely. ``target`` is the latest
        ``CHARTZIEL`` value for the chart (a daily target, matching
        the daily state); ``target_percent`` is current / target as a
        percentage; and ``target_status`` (``green`` / ``orange`` /
        ``red``) mirrors the SPA's own traffic-light thresholds
        (<60% / 60-90% / ≥90%). All three are ``None`` until both the
        STAND1 reading and the target value have been observed. The
        latter two are also first-class entities (see
        ``SmartPlaceChartTargetPercentSensor`` /
        ``SmartPlaceChartTargetStatusSensor``); the attributes stay
        for template users.
        """
        chart = self._data.state.charts.get(self._chart_id)
        if chart is None:
            return {}
        attrs: dict[str, str | float | None] = {
            _STAND_SERIES_NAMES.get(series, f"stand{series}"): value
            for series, value in sorted(chart.stands.items())
            if series != _DAILY_SERIES and value != ""
        }
        attrs["target"] = self._target()
        attrs["target_percent"] = self._target_percent()
        attrs["target_status"] = chart_target_status(self._daily_reading(), self._target())
        return attrs


class SmartPlaceChartTargetPercentSensor(_SmartPlaceChartScopedSensor):
    """Today's consumption as a percentage of the chart's daily target.

    Backs "colour meter" dashboards: a gauge card with ``severity``
    thresholds at 60 (orange) and 90 (red) reproduces the SPA's
    traffic light exactly, and ``numeric_state`` automation triggers
    can fire at any custom level (e.g. warn at 75%, before the status
    sensor flips to red). Values above 100 are real — consumption past
    the target — so no upper clamp is applied.

    Resets to 0 with the STAND1 reading at midnight; ``None`` (HA
    ``unknown``) until both today's reading and the ``CHARTZIEL``
    target have been observed.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_icon = "mdi:gauge"
    # The originally reported case: the more-info view showed a 5-minute
    # mean like ``4.7000000001%``. ``native_value`` is already rounded to
    # one decimal, but HA averages those states for the statistics graph
    # and the average leaks a float tail; this rounds that display too.
    # See SmartPlaceOutdoorTemperatureSensor.
    _attr_suggested_display_precision = 1
    _name_suffix = "target use"

    def __init__(
        self,
        entry: ConfigEntry,
        data: SmartPlaceData,
        chart_id: int,
        *,
        enabled_default: bool = True,
    ) -> None:
        """Wire the per-chart unique_id."""
        super().__init__(entry, data, chart_id, enabled_default=enabled_default)
        self._attr_unique_id = f"{entry.entry_id}_chart_{chart_id}_target_percent"

    @property
    def native_value(self) -> float | None:
        """Return today's reading as % of target, or None before both are seen."""
        return self._target_percent()


class SmartPlaceChartTargetStatusSensor(_SmartPlaceChartScopedSensor):
    """The SPA's traffic-light rating of today's consumption.

    An enum over ``green`` / ``orange`` / ``red`` using the SPA's own
    breakpoints: <60% of the daily target is green, 60-90% orange,
    ≥90% red — note red starts *before* the target is crossed. As
    first-class state it gives clean automation triggers ("changed to
    red → notify") and a logbook trail of every transition, and keeps
    the thresholds inside the integration (``chart_target_status``)
    instead of hardcoded in user automations.

    Falls back to green at midnight when STAND1 resets, which re-arms
    a "changed to red" notification daily for free. ``None`` (HA
    ``unknown``) until both the reading and the target are observed.
    """

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_translation_key = "chart_target_status"
    _attr_icon = "mdi:traffic-light"
    # Hidden by default: the traffic-light status is a derived convenience
    # for automations; the percent + daily-reading sensors carry the data
    # users watch, so it stays out of the default UI.
    _attr_entity_registry_visible_default = False
    _name_suffix = "target status"

    def __init__(
        self,
        entry: ConfigEntry,
        data: SmartPlaceData,
        chart_id: int,
        *,
        enabled_default: bool = True,
    ) -> None:
        """Wire the per-chart unique_id + enum options."""
        super().__init__(entry, data, chart_id, enabled_default=enabled_default)
        self._attr_unique_id = f"{entry.entry_id}_chart_{chart_id}_target_status"
        self._attr_options = ["green", "orange", "red"]

    @property
    def native_value(self) -> str | None:
        """Return green/orange/red, or None before reading + target are seen."""
        return chart_target_status(self._daily_reading(), self._target())


class SmartPlacePackageDeliveryPinSensor(_SmartPlaceSensorBase):
    """Parcel-box unlock PIN, extracted from the ``PERSINFO`` banner.

    A real delivery does *not* populate ``PACKETBOX<N>:<code>`` (the box
    keeps reporting ``Frei``). Instead the SPA pushes a free-text
    ``PERSINFO`` banner that contains the PIN, e.g. "Sie haben eine
    Lieferung in der Paketbox. Bitte verwenden Sie den PIN:4489 um diese
    rauszuholen." This sensor surfaces just the digits as a clean code,
    or ``None`` (HA ``unknown``) when no delivery is waiting — HA has
    no presentable "empty" state for free-text sensors, so the
    standard ``unknown`` it is. The full banner text is also available
    verbatim on the separate ``Personal info`` sensor — duplication is
    intentional.

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
    """Aggregated view: which weather alarm is currently on.

    An enum over ``Rain`` / ``Hail`` / per-zone ``WindAlarm<N>``:
    ``none`` when all clear (instead of an ``unknown`` that reads
    like a failure), the alarm kind when exactly one is active, and
    ``multiple`` when more than one fires at once — the per-alarm
    attributes carry the breakdown for that case.
    """

    _attr_name = "Weather alarm"
    _attr_icon = "mdi:weather-cloudy-alert"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_translation_key = "weather_alarm"
    _category = "Weather"

    def __init__(self, entry: ConfigEntry, data: SmartPlaceData) -> None:
        """Wire the entry-scoped unique_id."""
        super().__init__(entry, data)
        self._attr_unique_id = f"{entry.entry_id}_weather_alarm"
        self._attr_options = ["none", "rain", "hail", "wind", "multiple"]

    def _active_kinds(self) -> list[str]:
        state = self._data.state
        kinds: list[str] = []
        if state.rain_alarm:
            kinds.append("rain")
        if state.hail_alarm:
            kinds.append("hail")
        if any(state.wind_alarms.values()):
            kinds.append("wind")
        return kinds

    @property
    def native_value(self) -> str:
        """Return ``none``, the single active alarm kind, or ``multiple``."""
        kinds = self._active_kinds()
        if not kinds:
            return "none"
        if len(kinds) == 1:
            return kinds[0]
        return "multiple"

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
    "rang from X" from "idle". KNOWN LIMITATION: ``SPRECHEN<n>:ring`` is
    a sticky server latch — it never clears on the WS (the live ring +
    clear are SIP-only; see IMPLEMENT.md), so this can read "ringing"
    long after the call. Suffixed by id only when more than one intercom
    is discovered — with a single unit, "Intercom 1" is just noise (same
    rule as the wind alarms).
    """

    _attr_icon = "mdi:phone-incoming"

    def __init__(self, entry: ConfigEntry, data: SmartPlaceData, intercom_id: int) -> None:
        """Wire the per-intercom name + unique_id."""
        super().__init__(entry, data)
        self._intercom_id = intercom_id
        intercom_count = len(set(data.state.intercom_ringing) | set(data.state.intercom_callers))
        suffix = f" {intercom_id}" if intercom_count > 1 else ""
        self._attr_name = f"Intercom{suffix}"
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
    order (so users see "any active message"); ``None`` (HA
    ``unknown``) when every slot is acknowledged. Per-slot bodies
    surface as ``slot<n>`` attributes so finer-grained dashboards can
    still address them individually.
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
    # Hidden by default: a diagnostic raw-banner feed that's ``unknown``
    # most of the time. Always registered (see async_setup_entry) so a
    # late banner can surface, but kept out of the default UI.
    _attr_entity_registry_visible_default = False

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
