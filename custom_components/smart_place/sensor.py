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
  zone, on the per-zone HA sub-device (``zone_device_info``) so they
  render together with the zone's climate entity in the device card. The zone's room name labels the sub-device. Sensor IDs
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

Discovery is observation-based: an entity is created when the state
snapshot first holds its value — normally all at once at setup, after
``__init__.py``'s observation window, and later for anything the server
only delivers then. An entity that takes a unit, a name or a default from
other frames when it is created (outdoor readings, intercoms, charts and
what hangs on them) waits for the complete main-menu reply.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable
from datetime import timedelta
from functools import partial
import logging
from typing import TYPE_CHECKING

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE, UnitOfEnergy, UnitOfSpeed, UnitOfTemperature, UnitOfVolume
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_point_in_time, async_track_time_interval
from homeassistant.util import dt as dt_util

from . import SmartPlaceConfigEntry, SmartPlaceData, category_device_info, main_device_info, zone_device_info
from .const import DOMAIN
from .entity import SmartPlacePushEntity, async_add_discovered
from .smart_place_client import (
    CHART_RANGE_SERIES_HIGH_TARIFF,
    CHART_RANGE_SERIES_LOW_TARIFF,
    CHART_RANGE_SERIES_TOTAL,
    chart_target_status,
    energy_cost_chf,
    is_high_tariff,
    next_tariff_boundary,
    rates_for,
)
from .smart_place_client.tariff import STATISTICS_SETTLE, TARIFF_TZ, MonthToDateCost, local_month_start

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity import Entity
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .smart_place_client import SmartPlaceState

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

# How often the month-to-date cost sensor re-reads its history prefix from
# recorder statistics. The prefix only changes when the range buckets move
# to a new day; that case triggers a read of its own, so this is the retry
# and the pick-up for statistics corrected after the fact (a backfill).
_COST_PREFIX_REFRESH = timedelta(minutes=15)

# A prefix read prompted by a new day that did not deliver the prefix (the
# recorder failed, or is slow) is repeated on a later update, this far
# apart at least.
_COST_PREFIX_RETRY = timedelta(minutes=1)


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

# The server sends a range bucket with the float noise of its own
# subtraction (``2.35699999999997``). Rounded only where a bucket is shown:
# the cost is computed from the raw buckets, because rounding them first
# can move it by 0.0001 CHF.
_BUCKET_DECIMALS: int = 6

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
    entry: SmartPlaceConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a sensor for every reading the state snapshot holds, now and later."""
    data = entry.runtime_data
    async_add_discovered(entry, async_add_entities, partial(_collect, entry, data))


def _collect(entry: SmartPlaceConfigEntry, data: SmartPlaceData) -> dict[Hashable, Callable[[], Entity]]:
    """Map every sensor the current snapshot supports to a factory for it."""
    state = data.state
    found: dict[Hashable, Callable[[], Entity]] = {}

    # Always created (not observation-gated): a delivery PIN is
    # transient and rare, so gating on its presence would mean the
    # sensor almost never exists. As a permanent entity it reads
    # ``unknown`` between deliveries and shows the PIN while a parcel
    # is waiting.
    found["package_delivery_pin"] = partial(SmartPlacePackageDeliveryPinSensor, entry, data)
    if state.rain_alarm is not None or state.hail_alarm is not None or state.wind_alarms:
        found["weather_alarm"] = partial(SmartPlaceWeatherAlarmSensor, entry, data)
    # Per-zone temperature + humidity, both attached to a per-zone
    # sub-device so they render together in HA's device card.
    # ``TEMPIST<N>`` pairs 1:1 with ``Klimas<N>``; humidity follows
    # the same id convention.
    for zone_id in sorted(state.climate_zones):
        if zone_id in state.indoor_temperatures:
            found["temperature", zone_id] = partial(SmartPlaceIndoorTemperatureSensor, entry, data, zone_id)
        if zone_id in state.humidities:
            found["humidity", zone_id] = partial(SmartPlaceHumiditySensor, entry, data, zone_id)
    if state.infoboard_contents:
        found["infoboard"] = partial(SmartPlaceInfoboardSensor, entry, data)
    # Always created (not observation-gated): the idle banner is
    # ``PERSINFO:Read`` which folds to ``None``, so gating on presence
    # would usually skip the sensor. Reads ``unknown`` when clear.
    found["person_info"] = partial(SmartPlacePersonInfoSensor, entry, data)

    # Everything below takes something from other frames when it is
    # created: a unit, a name that depends on how many there are, a chart's
    # label (it becomes the entity id) or whether a chart is part of a sum.
    # Created from half a bootstrap it would get that wrong for good.
    if not data.devices_listed:
        return found

    if state.outdoor_temperature is not None:
        found["outdoor_temperature"] = partial(SmartPlaceOutdoorTemperatureSensor, entry, data)
    if state.wind_speed is not None:
        found["wind_speed"] = partial(SmartPlaceWindSpeedSensor, entry, data)
    for intercom_id in sorted(set(state.intercom_ringing) | set(state.intercom_callers)):
        found["intercom", intercom_id] = partial(SmartPlaceIntercomSensor, entry, data, intercom_id)
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
        found["chart", chart_id] = partial(
            SmartPlaceChartSensor,
            entry,
            data,
            chart_id,
            device_class,
            native_unit,
            enabled_default=enabled_default,
        )
        # Target companions only exist where the SPA sets a daily goal
        # (verified live 2026-06-11: targets on the four user-facing
        # charts, none on PV). CHARTZIEL arrives in the same bootstrap
        # as the chart definitions.
        if chart_id in state.chart_targets:
            found["chart_target_percent", chart_id] = partial(
                SmartPlaceChartTargetPercentSensor, entry, data, chart_id, enabled_default=enabled_default
            )
            found["chart_target_status", chart_id] = partial(
                SmartPlaceChartTargetStatusSensor, entry, data, chart_id, enabled_default=enabled_default
            )

    # Electricity price + cost entities, attached to the grid meter chart
    # (label "Electricity" — excludes the PV allocation chart, whose
    # server-side history is empty, and any SUMME aggregate).
    electricity_chart_id = _find_electricity_chart_id(state)
    if electricity_chart_id is not None:
        found["electricity_price"] = partial(_electricity_price_sensor, entry, data)
        found["electricity_cost_today"] = partial(
            SmartPlaceElectricityCostTodaySensor, entry, data, electricity_chart_id
        )
        found["electricity_cost_month"] = partial(
            SmartPlaceElectricityCostMonthSensor, entry, data, electricity_chart_id
        )
        found["electricity_high_tariff"] = partial(
            SmartPlaceElectricityTariffEnergySensor, entry, data, electricity_chart_id, high=True
        )
        found["electricity_low_tariff"] = partial(
            SmartPlaceElectricityTariffEnergySensor, entry, data, electricity_chart_id, high=False
        )
    return found


def _electricity_price_sensor(entry: SmartPlaceConfigEntry, data: SmartPlaceData) -> SmartPlaceElectricityPriceSensor:
    """Create the price sensor; warn once if this year's rate table is missing."""
    _, stale = rates_for(dt_util.now())
    if stale:
        _LOGGER.warning(
            "No EWZ tariff table for the current year - electricity price/cost "
            "sensors are using the latest known year's rates. Add the new year "
            "to smart_place_client/tariff.py (published by ewz each September)."
        )
    return SmartPlaceElectricityPriceSensor(entry, data)


def _find_electricity_chart_id(state: SmartPlaceState) -> int | None:
    """Return the grid electricity meter's chart id, or None.

    Matches the cleaned ``ChartDefinition`` label exactly: this
    installation has one grid meter chart labelled ``Electricity``
    (raw ``Elektro <site>``). The PV chart cleans to ``PV electricity``
    and water/heating to other labels, so an exact match cannot pick
    the wrong chart; if a future installation reports several
    (``Electricity I`` / ``II``), no price entities are created rather
    than guessing.
    """
    for chart_id, chart in sorted(state.charts.items()):
        if chart.label == "Electricity":
            return chart_id
    return None


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

    def __init__(self, entry: SmartPlaceConfigEntry, data: SmartPlaceData) -> None:
        """Wire the shared per-entry device + state-listener machinery."""
        self._data = data
        self._attr_device_info = (
            category_device_info(entry, data, self._category) if self._category is not None else main_device_info(entry)
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

    def __init__(self, entry: SmartPlaceConfigEntry, data: SmartPlaceData) -> None:
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

    def __init__(self, entry: SmartPlaceConfigEntry, data: SmartPlaceData) -> None:
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
        entry: SmartPlaceConfigEntry,
        data: SmartPlaceData,
        sensor_id: int,
    ) -> None:
        """Wire the per-zone unique_id + sub-device."""
        super().__init__(entry, data)
        self._sensor_id = sensor_id
        self._attr_unique_id = f"{entry.entry_id}_indoor_temperature_{sensor_id}"
        self._attr_device_info = zone_device_info(entry, data, sensor_id)

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
        entry: SmartPlaceConfigEntry,
        data: SmartPlaceData,
        zone_id: int,
    ) -> None:
        """Wire the per-zone unique_id + sub-device."""
        super().__init__(entry, data)
        self._zone_id = zone_id
        self._attr_unique_id = f"{entry.entry_id}_humidity_{zone_id}"
        self._attr_device_info = zone_device_info(entry, data, zone_id)

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
        entry: SmartPlaceConfigEntry,
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
        entry: SmartPlaceConfigEntry,
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
    _attr_translation_key = "chart_target_use"
    # The originally reported case: the more-info view showed a 5-minute
    # mean like ``4.7000000001%``. ``native_value`` is already rounded to
    # one decimal, but HA averages those states for the statistics graph
    # and the average leaks a float tail; this rounds that display too.
    # See SmartPlaceOutdoorTemperatureSensor.
    _attr_suggested_display_precision = 1
    _name_suffix = "target use"

    def __init__(
        self,
        entry: SmartPlaceConfigEntry,
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
    # Hidden by default: the traffic-light status is a derived convenience
    # for automations; the percent + daily-reading sensors carry the data
    # users watch, so it stays out of the default UI.
    _attr_entity_registry_visible_default = False
    _name_suffix = "target status"

    def __init__(
        self,
        entry: SmartPlaceConfigEntry,
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


class SmartPlaceElectricityPriceSensor(_SmartPlaceSensorBase):
    """Current all-in price of one grid kWh (EWZ tariff clock).

    A pure wall-clock function — high tariff Mon-Sat 06:00-22:00 local,
    low tariff otherwise, at the calendar year's EWZ rates (see
    ``smart_place_client.tariff`` for provenance and the yearly manual
    update). Always available: it needs no server data, so it keeps
    working through WebSocket drops and can feed HA's Energy dashboard
    cost tracking ("Use an entity with current price") alongside the
    ``Electricity (today)`` consumption sensor.

    Frame-driven writes would lag a tariff flip by whatever the next
    state change is, so the sensor also schedules an exact refresh at
    every boundary (06:00 / 22:00 / midnight).
    """

    _category = "Energy"
    _attr_name = "Electricity price"
    _attr_native_unit_of_measurement = "CHF/kWh"
    _attr_translation_key = "electricity_price"
    _attr_suggested_display_precision = 4
    # Hidden by default: the price is plumbing for the Energy dashboard's
    # "entity with current price" cost tracking (hidden entities still
    # work there), not a reading to watch on the auto-generated Overview
    # — the tariff window + rates stay inspectable via its attributes.
    _attr_entity_registry_visible_default = False

    def __init__(self, entry: SmartPlaceConfigEntry, data: SmartPlaceData) -> None:
        """Wire the unique_id; no chart data is consumed."""
        super().__init__(entry, data)
        self._attr_unique_id = f"{entry.entry_id}_electricity_price"
        self._boundary_unsub = None

    @property
    def available(self) -> bool:
        """Always available — the price is a clock, not a server value."""
        return True

    @property
    def native_value(self) -> float:
        """Return the all-in CHF/kWh for a grid kWh drawn right now."""
        now = dt_util.now()
        tariff, _ = rates_for(now)
        return tariff.total_ht if is_high_tariff(now) else tariff.total_nt

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Expose the tariff window, rate table components, and staleness."""
        now = dt_util.now()
        tariff, stale = rates_for(now)
        return {
            "tariff_window": "high" if is_high_tariff(now) else "low",
            "high_tariff_chf_kwh": tariff.total_ht,
            "low_tariff_chf_kwh": tariff.total_nt,
            "energy_chf_kwh": tariff.energy_ht if is_high_tariff(now) else tariff.energy_nt,
            "grid_chf_kwh": tariff.grid_ht if is_high_tariff(now) else tariff.grid_nt,
            "levies_chf_kwh": tariff.levies,
            "pv_chf_kwh": tariff.pv_chf_kwh,
            "fixed_chf_month": tariff.fixed_chf_month,
            "rates_year": tariff.year,
            "rates_stale": stale,
            "rates_source": tariff.source,
        }

    async def async_added_to_hass(self) -> None:
        """Subscribe to frames + schedule the exact boundary refresh."""
        await super().async_added_to_hass()
        self._schedule_boundary_refresh()

    async def async_will_remove_from_hass(self) -> None:
        """Cancel the boundary timer alongside the frame subscription."""
        await super().async_will_remove_from_hass()
        if self._boundary_unsub is not None:
            self._boundary_unsub()
            self._boundary_unsub = None

    @callback
    def async_entity_id_changed(self, old_entity_id: str) -> None:
        """Opt in to in-place entity_id changes: the timer is not keyed on the entity_id."""
        super().async_entity_id_changed(old_entity_id)

    def _schedule_boundary_refresh(self) -> None:
        boundary = next_tariff_boundary(dt_util.now())
        self._boundary_unsub = async_track_point_in_time(self.hass, self._on_tariff_boundary, boundary)

    @callback
    def _on_tariff_boundary(self, _now: datetime) -> None:
        """Write the flipped price and re-arm for the next boundary."""
        self._handle_frame_update()
        self._schedule_boundary_refresh()


class _SmartPlaceElectricityCostSensor(_SmartPlaceChartScopedSensor):
    """Shared wiring for the cost sensors: the range buckets and the day they cover."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "CHF"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 2

    def _buckets(self) -> dict[int, float]:
        return self._data.state.chart_today_buckets.get(self._chart_id, {})

    def _bucket_day(self) -> datetime | None:
        """Return the local midnight of the day the buckets cover.

        Not the wall clock: after midnight the buckets still hold the old
        day until the first poll of the new day was answered.
        """
        start = self._data.state.chart_today_starts.get(self._chart_id)
        return None if start is None else dt_util.utc_from_timestamp(start).astimezone(TARIFF_TZ)

    def _today_cost(self) -> float | None:
        """Return the cost of the buckets at their day's rates, None before the first range poll."""
        buckets = self._buckets()
        ht = buckets.get(CHART_RANGE_SERIES_HIGH_TARIFF)
        nt = buckets.get(CHART_RANGE_SERIES_LOW_TARIFF)
        if ht is None and nt is None:
            return None
        tariff, _ = rates_for(self._bucket_day() or dt_util.now())
        return energy_cost_chf(ht or 0.0, nt or 0.0, tariff)


class SmartPlaceElectricityCostTodaySensor(_SmartPlaceElectricityCostSensor):
    """Today's grid electricity cost in CHF, from server-side tariff buckets.

    The client polls ``Commands.ChartStandRange`` with the current local
    day every ``CHART_POLL_INTERVAL``; the server answers with its own
    high/low-tariff consumption split (the same bucketing the EWZ
    invoices bill, verified against five months of invoices — see
    DESIGN.md). State = HT kWh x HT rate + NT kWh x NT rate at the
    rates of the buckets' year.

    ``TOTAL`` + ``last_reset`` (midnight of the day the buckets cover)
    lets HA's long-term statistics accumulate daily costs into
    weekly/monthly/yearly sums correctly across the midnight reset AND
    across a 1 January rate change (each day accrues at its own year's
    rates). ``last_reset`` moves together with the values: until the
    first reply of the new day arrives the sensor still reports the old
    day's cost under the old day's ``last_reset``.

    Known bias, documented in ``smart_place_client.tariff``: the meter
    buckets include the ZEV solar allocation that EWZ bills ~20% cheaper;
    against the 2025 invoices this sensor over-reads by +0.3..+2.3%
    (~0.4 CHF/month). The monthly fixed fee (``fixed_chf_month``
    attribute) is not part of the state either.
    """

    _name_suffix = "cost (today)"

    def __init__(self, entry: SmartPlaceConfigEntry, data: SmartPlaceData, chart_id: int) -> None:
        """Wire the per-chart unique_id."""
        super().__init__(entry, data, chart_id)
        self._attr_unique_id = f"{entry.entry_id}_chart_{chart_id}_cost_today"

    @property
    def last_reset(self) -> datetime | None:
        """Return the midnight of the day the buckets cover."""
        return self._bucket_day()

    @property
    def native_value(self) -> float | None:
        """Return today's variable cost, or None before the first range poll."""
        return self._today_cost()

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Expose the kWh split, the rates applied, and the fixed-fee context."""
        buckets = self._buckets()
        tariff, stale = rates_for(self._bucket_day() or dt_util.now())
        return {
            "high_tariff_kwh": _shown_bucket(buckets.get(CHART_RANGE_SERIES_HIGH_TARIFF)),
            "low_tariff_kwh": _shown_bucket(buckets.get(CHART_RANGE_SERIES_LOW_TARIFF)),
            "total_kwh": _shown_bucket(buckets.get(CHART_RANGE_SERIES_TOTAL)),
            "high_tariff_chf_kwh": tariff.total_ht,
            "low_tariff_chf_kwh": tariff.total_nt,
            "fixed_chf_month": tariff.fixed_chf_month,
            "rates_year": tariff.year,
            "rates_stale": stale,
        }


class SmartPlaceElectricityCostMonthSensor(_SmartPlaceElectricityCostSensor):
    """Month-to-date grid electricity cost in CHF.

    State = history prefix + live today: the prefix is the long-term
    statistics change of the daily cost sensor from the 1st of the month
    up to the midnight of the day the range buckets cover (read from HA's
    recorder — the same statistics the Energy dashboard charts, including
    any backfilled history), and the today part comes live from the
    server's tariff buckets. Reading the prefix from statistics keeps this
    sensor consistent with the Energy dashboard and makes it restart-proof
    without any extra bookkeeping.

    The two parts roll over to a new day at different moments, so they are
    only added when they belong to the same day (``MonthToDateCost``): the
    value stands still for a moment after midnight instead of dropping by
    the cost of the day that just ended. The prefix is read once the
    statistics of that day are complete (again every ``_COST_PREFIX_RETRY``
    until that read succeeds) and re-read every ``_COST_PREFIX_REFRESH``.
    ``None`` (unknown) until both the first statistics read and the first
    range poll have landed.
    """

    _name_suffix = "cost (this month)"

    def __init__(self, entry: SmartPlaceConfigEntry, data: SmartPlaceData, chart_id: int) -> None:
        """Wire the per-chart unique_id and the month-to-date sum."""
        super().__init__(entry, data, chart_id)
        self._attr_unique_id = f"{entry.entry_id}_chart_{chart_id}_cost_month"
        self._cost_unique_id = f"{entry.entry_id}_chart_{chart_id}_cost_today"
        self._month = MonthToDateCost()
        # When a prefix read was last started because the buckets moved to a new day.
        self._prefix_prompted_at: datetime | None = None
        self._refresh_unsub = None

    @property
    def last_reset(self) -> datetime | None:
        """Return the 1st of the month of the day the value covers."""
        return None if self._month.value_day is None else local_month_start(self._month.value_day)

    @property
    def native_value(self) -> float | None:
        """Return history prefix + live today, or None until both are known."""
        return self._month.value

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Expose the prefix/live split so the composition is inspectable."""
        prefix_day = self._month.prefix_day
        return {
            "history_chf": self._month.prefix_chf,
            "history_through": None if prefix_day is None else prefix_day.date().isoformat(),
            "today_chf": self._today_cost(),
        }

    @callback
    def _handle_frame_update(self) -> None:
        """Fold the running day's cost; fetch the prefix of a new day once it can be read."""
        day = self._bucket_day()
        self._month.update(day, self._today_cost())
        now = dt_util.utcnow()
        if (
            day is not None
            and day != self._month.prefix_day
            and now >= day + STATISTICS_SETTLE
            and (self._prefix_prompted_at is None or now - self._prefix_prompted_at >= _COST_PREFIX_RETRY)
        ):
            # Until the read delivers this day's prefix it is prompted
            # again, but not by every update: one read at a time.
            self._prefix_prompted_at = now
            self.hass.async_create_task(self._async_refresh_prefix())
        super()._handle_frame_update()

    async def async_added_to_hass(self) -> None:
        """Read the prefix and start the refresh timer."""
        await super().async_added_to_hass()
        await self._async_refresh_prefix()
        self._refresh_unsub = async_track_time_interval(self.hass, self._async_refresh_prefix, _COST_PREFIX_REFRESH)

    async def async_will_remove_from_hass(self) -> None:
        """Cancel the prefix refresh timer alongside the frame subscription."""
        await super().async_will_remove_from_hass()
        if self._refresh_unsub is not None:
            self._refresh_unsub()
            self._refresh_unsub = None

    @callback
    def async_entity_id_changed(self, old_entity_id: str) -> None:
        """Opt in to in-place entity_id changes: the timer is not keyed on the entity_id."""
        super().async_entity_id_changed(old_entity_id)

    async def _async_refresh_prefix(self, _now: datetime | None = None) -> None:
        """Re-read the cost of the month's days before the buckets' day from recorder statistics."""
        day = self._bucket_day()
        # Looked up on every read: the daily sensor's entity_id (its statistic id) can be renamed.
        statistic_id = er.async_get(self.hass).async_get_entity_id("sensor", DOMAIN, self._cost_unique_id)
        if day is None or statistic_id is None:
            return
        start = local_month_start(day)
        if start >= day:
            prefix = 0.0  # first day of the month: no history part yet
        elif dt_util.utcnow() < day + STATISTICS_SETTLE:
            return
        else:
            try:
                # Period "hour", not "day": for "day" the recorder extends the end to
                # the end of the day that contains it, which pulled the running day in.
                stats = await get_instance(self.hass).async_add_executor_job(
                    statistics_during_period,
                    self.hass,
                    start,
                    day,
                    {statistic_id},
                    "hour",
                    None,
                    {"change"},
                )
            except Exception:  # noqa: BLE001 - recorder may not be ready yet; retry next tick
                _LOGGER.debug("month-cost prefix query failed; keeping previous value", exc_info=True)
                return
            prefix = round(sum(row.get("change") or 0.0 for row in stats.get(statistic_id, ())), 6)
        if (day, prefix) != (self._month.prefix_day, self._month.prefix_chf):
            self._month.set_prefix(day, prefix)
            self._handle_frame_update()


class SmartPlaceElectricityTariffEnergySensor(_SmartPlaceChartScopedSensor):
    """Today's high- or low-tariff share of grid consumption (kWh).

    Sourced from the same server-side range buckets as the cost sensor.
    Mirrors ``SmartPlaceChartSensor``'s daily ``TOTAL_INCREASING``
    convention (resets to 0 at midnight), so each share can feed the
    Energy dashboard or utility meters independently — e.g. to track
    how much load actually runs in the cheap window.
    """

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 3
    # Hidden by default (same rationale as the price sensor): these are
    # the Energy dashboard's grid-flow inputs, not Overview readings —
    # the dashboard and recorder use hidden entities just fine.
    _attr_entity_registry_visible_default = False

    def __init__(self, entry: SmartPlaceConfigEntry, data: SmartPlaceData, chart_id: int, *, high: bool) -> None:
        """Wire the tariff window (high/low) this share tracks."""
        super().__init__(entry, data, chart_id)
        self._series = CHART_RANGE_SERIES_HIGH_TARIFF if high else CHART_RANGE_SERIES_LOW_TARIFF
        window = "ht" if high else "nt"
        self._attr_unique_id = f"{entry.entry_id}_chart_{chart_id}_{window}_today"
        self._name_suffix = f"{'high' if high else 'low'} tariff (today)"

    @property
    def native_value(self) -> float | None:
        """Return today's share in kWh, or None before the first range poll."""
        return _shown_bucket(self._data.state.chart_today_buckets.get(self._chart_id, {}).get(self._series))


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

    _attr_translation_key = "package_delivery_pin"

    def __init__(self, entry: SmartPlaceConfigEntry, data: SmartPlaceData) -> None:
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
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_translation_key = "weather_alarm"
    _category = "Weather"

    def __init__(self, entry: SmartPlaceConfigEntry, data: SmartPlaceData) -> None:
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

    _attr_translation_key = "intercom"

    def __init__(self, entry: SmartPlaceConfigEntry, data: SmartPlaceData, intercom_id: int) -> None:
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
    _attr_translation_key = "infoboard"

    def __init__(self, entry: SmartPlaceConfigEntry, data: SmartPlaceData) -> None:
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
    _attr_translation_key = "person_info"
    # Hidden by default: a diagnostic raw-banner feed that's ``unknown``
    # most of the time. Always registered (see async_setup_entry) so a
    # late banner can surface, but kept out of the default UI.
    _attr_entity_registry_visible_default = False

    def __init__(self, entry: SmartPlaceConfigEntry, data: SmartPlaceData) -> None:
        """Wire the entry-scoped unique_id."""
        super().__init__(entry, data)
        self._attr_unique_id = f"{entry.entry_id}_person_info"

    @property
    def native_value(self) -> str | None:
        """Return the latest PERSINFO text, or None when cleared."""
        return self._data.state.person_info


def _shown_bucket(value: float | None) -> float | None:
    return None if value is None else round(value, _BUCKET_DECIMALS)


def _safe_float(raw: str | None) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return None
