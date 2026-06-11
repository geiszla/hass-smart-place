"""Decoded snapshot of Smart Place state across observed frames.

One :class:`SmartPlaceState` per consumer (HA config entry, CLI session,
notebook). Feed every parsed :class:`ServerFrame` through
:meth:`SmartPlaceState.apply` and the snapshot tracks the latest indoor
temperatures, weather, alarms, and per-chart STAND series.

Pure value-object: no I/O, no HA imports. Lives in the client library
rather than ``custom_components/`` so it's importable from notebooks
and pytest without bringing in ``homeassistant``.

Per the smart-place-observe-only memory: this module never sends
commands. It only translates inbound frames into typed fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import TYPE_CHECKING

from .protocol import NamedFields, NamedValue, Temperature

if TYPE_CHECKING:
    from .protocol import ServerFrame


# Strip the trailing site code the SPA appends to chart titles, e.g.
# "Elektro HH77-14-01" -> "Elektro". Pattern is a hyphenated alnum tail
# (typical: "HH77-14-01", "HHnn-nn-nn"); trimmed defensively, never raises.
_CHART_SITE_SUFFIX_RE = re.compile(r"\s+[A-Z]{2,}\d+-\d+-\d+(?:\s+.*)?$")

# Light German -> English mapping for the chart labels we see. Only
# tokens we've actually observed live; unknown labels pass through
# unchanged so we don't silently mistranslate.
_CHART_LABEL_GERMAN_TO_ENGLISH: dict[str, str] = {
    "Elektro": "Electricity",
    "Wärme": "Heating",
    "Kaltwasser": "Cold water",
    "Warmwasser": "Hot water",
    "PV Anteil Elektro": "PV electricity",
    # SUMME charts drop the "total" qualifier: their per-meter
    # constituents are disabled by default in HA, so the aggregate
    # IS the cold/hot water reading users see — and "total" misreads
    # as "lifetime" next to a daily state.
    "SUMME Kaltwasser": "Cold water",
    "SUMME Warmwasser": "Hot water",
}

# ClimateConfig values look like "Bedroom heating" — for sensor naming
# we want just the room ("Bedroom"). The vendor SPA names climate
# zones with this trailing "heating" / "Heizung" tag.
_CLIMATE_ROOM_TAIL_RE = re.compile(r"\s+(?:heating|Heizung)\s*$", re.IGNORECASE)

# Intercom CALLINFO values are German location labels for the building's
# call points (front door, mailbox, ...). Only tokens we've observed
# live; unknown labels pass through unchanged.
_CALLER_INFO_GERMAN_TO_ENGLISH: dict[str, str] = {
    "Wohnungseingang": "Apartment entrance",
    "Haupteingang": "Main entrance",
    "Hauseingang": "Building entrance",
    "Eingang": "Entrance",
    "Briefkasten": "Mailbox",
    "Garageneingang": "Garage entrance",
    "Garagentor": "Garage door",
    "Garage": "Garage",
    "Tiefgarage": "Underground garage",
}

# SPA chart traffic-light thresholds — taken verbatim from javallg.js
# (~lines 953-973). Below ``ORANGE_THRESHOLD`` (60% of target) the SPA
# paints the value green; below ``RED_THRESHOLD`` (90%) orange; at or
# above the target (100%) red. The SPA actually splits 0-60% across
# six sub-buckets for fill visuals, but the colour switches only at
# these three breakpoints, so we collapse to three statuses.
_CHART_ORANGE_THRESHOLD: float = 0.6
_CHART_RED_THRESHOLD: float = 0.9

# SUMME chart definitions reference the per-meter charts they
# aggregate, e.g. ``>SummeForDiagramm-336>SummeForDiagramm-335>``.
# Extracted so the sensor platform can disable the constituent
# meters by default in favour of the aggregate.
_SUMME_CONSTITUENT_RE = re.compile(r"SummeForDiagramm-(\d+)")

# Parcel-box unlock PIN, extracted from the free-text ``PERSINFO``
# banner. Empirically a real delivery does NOT set ``PACKETBOX<N>:<code>``
# (the box keeps reporting ``Frei``); instead the SPA pushes a German
# banner like "Sie haben eine Lieferung in der Paketbox. Bitte verwenden
# Sie den PIN:4489 um diese rauszuholen." and the digits after ``PIN:``
# are the code that opens the box.
_PERSINFO_PIN_RE = re.compile(r"PIN:\s*(\d+)", re.IGNORECASE)


def _translate_caller_label(raw: str) -> str:
    """Translate a CALLINFO location label to English where known."""
    return _CALLER_INFO_GERMAN_TO_ENGLISH.get(raw, raw)


def chart_target_status(current: float | None, target: float | None) -> str | None:
    """Classify a chart reading against its target — ``green``/``orange``/``red``.

    Mirrors the SPA's own thresholds: <60% green, 60-90% orange, ≥90%
    red. Returns ``None`` if either value is missing or the target is
    non-positive (no meaningful ratio).
    """
    if current is None or target is None or target <= 0:
        return None
    ratio = current / target
    if ratio >= _CHART_RED_THRESHOLD:
        return "red"
    if ratio >= _CHART_ORANGE_THRESHOLD:
        return "orange"
    return "green"


def _clean_chart_label(raw: str) -> str:
    """Return an HA-friendly chart label.

    Strips the site-code suffix and translates known German tokens
    (``Elektro`` -> ``Electricity`` etc.). Roman numeral suffixes
    (``I``, ``II``) are kept; the SPA uses them to distinguish two
    meters of the same kind.
    """
    cleaned = _CHART_SITE_SUFFIX_RE.sub("", raw).strip()
    for german, english in _CHART_LABEL_GERMAN_TO_ENGLISH.items():
        if cleaned.startswith(german):
            return (english + cleaned[len(german) :]).strip()
    return cleaned


def _climate_zone_room(raw: str) -> str:
    """Drop the trailing ``heating`` tag so the name reads as a room."""
    return _CLIMATE_ROOM_TAIL_RE.sub("", raw).strip()


def _alarm_on(raw: str) -> bool:
    """Smart Place encodes ``00`` as off; anything else (``01``, ...) means on."""
    return raw not in ("", "00")


@dataclass(slots=True)
class ChartReading:
    """Latest values for one consumption chart.

    ``unit`` is the raw token from the ``StatusEntry`` reference
    (``KWh``, ``l``, ...); the sensor platform maps it to a proper HA
    unit/device-class. ``stands`` maps the STAND series id to its
    latest reading (e.g. ``99`` for the cumulative total, lower series
    for time-bucket aggregates).

    ``label`` and ``category`` come from the ``ChartDefinition`` frame
    that the server emits during ``GiveMeMainmenu``: ``label`` is the
    cleaned English title (``"Cold water 1"``), ``category`` is the
    raw SPA category (``"Wasser"`` / ``"Energie"`` / ``"Elektro"`` /
    ``"Summe"`` — the last one marks an aggregated SUMME chart).
    Both are ``""`` until ``ChartDefinition`` arrives.
    ``summed_chart_ids`` lists the per-meter charts a SUMME chart
    aggregates (empty for non-SUMME charts) — the sensor platform
    disables those constituents by default.
    """

    unit: str
    stands: dict[int, str] = field(default_factory=dict)
    label: str = ""
    category: str = ""
    summed_chart_ids: tuple[int, ...] = ()


@dataclass(slots=True)
class SmartPlaceState:
    """Per-entry snapshot of everything we've seen on the WS.

    Each field is ``None`` / empty until the first matching frame
    arrives. Discovery is observation-based: a ``WINDALARM<2>`` push
    registers zone 2 in ``wind_alarms``. The integration sets up
    entities after ``wait_for_bootstrap`` plus a brief observation
    window (see ``__init__.py``); IDs that arrive after platforms have
    been forwarded won't materialise as entities until HA reloads the
    entry.

    Indoor temperatures (``TEMPIST<N>``) are only surfaced when a
    matching ``ClimateConfig`` zone exists in ``climate_zones`` — the
    SPA uses a 1:1 convention between ``Klimas<N>`` zones and
    ``TEMPIST<N>`` sensors, so we use the zone's room name for the
    sensor entity label. Sensors with no matching zone are kept in
    the snapshot but the HA layer skips them.
    """

    outdoor_temperature: float | None = None
    wind_speed: float | None = None
    rain_alarm: bool | None = None
    hail_alarm: bool | None = None
    blinds_maintenance: bool | None = None
    wind_alarms: dict[int, bool] = field(default_factory=dict)
    charts: dict[int, ChartReading] = field(default_factory=dict)
    # ``TEMPIST<N>`` indoor temperature in °C, by sensor id.
    indoor_temperatures: dict[int, float] = field(default_factory=dict)
    # ``Klimas<N>`` zone room name (English) from ``ClimateConfig``
    # frames received during ``GiveMeMainmenu``. Used as the HA name
    # for the matching ``TEMPIST<N>`` sensor.
    climate_zones: dict[int, str] = field(default_factory=dict)
    # Scene display name (e.g. ``"Evening"``) from ``SceneConfig``
    # — the first ``,``-field of ``INHALTSZENEN<N>``.
    scenes: dict[int, str] = field(default_factory=dict)
    # Whether scene N is currently active, from ``SZENEN<N>``
    # broadcasts (``"01"`` = active).
    scene_states: dict[int, bool] = field(default_factory=dict)
    # Aggregate ``any blind closed`` flag per ``JALZENTRAL<N>``
    # group. Best-effort: empty / ``"00"`` = none closed, anything
    # else = at least one closed. The SPA's semantics here aren't
    # fully documented; revisit if the user reports false positives.
    blinds_central: dict[int, bool] = field(default_factory=dict)
    # ``FEUCHTEIST<N>`` indoor humidity in % per climate-zone id —
    # paired 1:1 with ``Klimas<N>`` zones the same way as
    # ``indoor_temperatures``.
    humidities: dict[int, float] = field(default_factory=dict)
    # ``SPRECHEN<N>`` door-intercom ring state. ``True`` while the
    # SPA-side value is ``"ring"`` (incoming call); any other value
    # reads as idle.
    intercom_ringing: dict[int, bool] = field(default_factory=dict)
    # ``CALLINFO<N>`` caller location label (translated to English
    # via ``_CALLER_INFO_GERMAN_TO_ENGLISH``).
    intercom_callers: dict[int, str] = field(default_factory=dict)
    # ``INFOBOARD<N>INHALT`` slot content. ``None`` when the SPA sent
    # the literal ``Read`` (= acknowledged / cleared); otherwise the
    # raw text the SPA would render in the slot.
    infoboard_contents: dict[int, str | None] = field(default_factory=dict)
    # ``PERSINFO`` banner text. Same convention as
    # ``infoboard_contents``: ``None`` when ``Read``, otherwise the
    # raw banner text the SPA would display. A parcel delivery rides
    # in here as free text containing the unlock PIN — see the
    # ``package_delivery_pin`` property, which pulls just the digits out.
    person_info: str | None = None
    # ``CHARTZIEL<id>`` daily target value per chart id, parsed to
    # float. Paired with the chart's STAND1 reading to compute the
    # traffic-light status (see :func:`chart_target_status`).
    chart_targets: dict[int, float] = field(default_factory=dict)

    def apply(self, frame: ServerFrame) -> None:
        """Fold one frame into the snapshot — best-effort, never raises."""
        if isinstance(frame, Temperature):
            self.indoor_temperatures[frame.sensor] = frame.value
            return
        if isinstance(frame, NamedValue):
            self._apply_named_value(frame)
            return
        if isinstance(frame, NamedFields):
            self._apply_named_fields(frame)
            return

    def chart_target_status(self, chart_id: int) -> str | None:
        """Return ``green``/``orange``/``red`` for chart ``chart_id``'s STAND1 vs target."""
        chart = self.charts.get(chart_id)
        if chart is None:
            return None
        current = _safe_float(chart.stands.get(1))
        return chart_target_status(current, self.chart_targets.get(chart_id))

    @property
    def package_delivery_pin(self) -> str | None:
        """Return the parcel-box unlock PIN, or ``None`` when no delivery is waiting.

        The PIN is carried as free text in the ``PERSINFO`` banner
        (``person_info``), not in ``PACKETBOX<N>`` — see
        ``_PERSINFO_PIN_RE``. Returns the digits after ``PIN:`` when the
        banner is present and matches; ``None`` once the banner clears
        (``PERSINFO:Read`` sets ``person_info`` back to ``None``) or when
        the banner is some other notice without a PIN.
        """
        if self.person_info is None:
            return None
        match = _PERSINFO_PIN_RE.search(self.person_info)
        return match.group(1) if match else None

    def _apply_named_value(self, frame: NamedValue) -> None:
        name = frame.name
        if name == "OutdoorTemperature":
            self.outdoor_temperature = _safe_float(frame.value)
        elif name == "WindSpeed":
            self.wind_speed = _safe_float(frame.value)
        elif name == "Rain":
            self.rain_alarm = _alarm_on(frame.value)
        elif name == "Hail":
            self.hail_alarm = _alarm_on(frame.value)
        elif name == "BlindsMaintenance":
            self.blinds_maintenance = _alarm_on(frame.value)
        elif name == "WindAlarm" and frame.index is not None:
            self.wind_alarms[frame.index] = _alarm_on(frame.value)
        elif name == "ChartPointUpdate" and frame.index is not None:
            self._apply_chart_point(frame.index, frame.value)
        elif name == "ChartStand":
            self._apply_chart_stand(frame.value)
        elif name == "ChartDefinition" and frame.index is not None:
            self._apply_chart_definition(frame.index, frame.value)
        elif name == "SceneState" and frame.index is not None:
            self.scene_states[frame.index] = _alarm_on(frame.value)
        # ``LightsCentral`` (LEUCHTENZENTRAL<N>) is deliberately NOT
        # folded: it is the state of the SPA's "All" master button,
        # not an any-light-on aggregate (verified live 2026-06-11 —
        # it read 00 while two ``leuchte<n>`` loads were at 255).
        # Per-light ``LightState`` frames are the truthful source;
        # fold those instead when light support lands.
        elif name == "BlindsCentral" and frame.index is not None:
            self.blinds_central[frame.index] = _alarm_on(frame.value)
        elif name == "Humidity" and frame.index is not None:
            value = _safe_float(frame.value)
            if value is not None:
                self.humidities[frame.index] = value
        elif name == "DoorIntercom" and frame.index is not None:
            self.intercom_ringing[frame.index] = frame.value == "ring"
        elif name == "CallInfo" and frame.index is not None:
            self.intercom_callers[frame.index] = _translate_caller_label(frame.value)
        elif name == "InfoboardContent" and frame.index is not None:
            self.infoboard_contents[frame.index] = None if frame.value == "Read" else frame.value
        elif name == "PersonInfo":
            self.person_info = None if frame.value == "Read" else frame.value
        elif name == "ChartTarget" and frame.index is not None:
            value = _safe_float(frame.value)
            if value is not None:
                self.chart_targets[frame.index] = value

    def _apply_named_fields(self, frame: NamedFields) -> None:
        if frame.name == "ClimateConfig" and frame.index is not None and frame.fields:
            self.climate_zones[frame.index] = _climate_zone_room(frame.fields[0])
        elif frame.name == "SceneConfig" and frame.index is not None and frame.fields:
            self.scenes[frame.index] = frame.fields[0]

    def _apply_chart_point(self, chart_id: int, payload: str) -> None:
        """Fold a ``StandsSingelChartUpdate<id>:STAND<series>:<reading>`` payload."""
        series_raw, _, reading = payload.partition(":")
        series = _parse_stand_series(series_raw)
        if series is None:
            return
        self.charts.setdefault(chart_id, ChartReading(unit="")).stands[series] = reading

    def _apply_chart_stand(self, payload: str) -> None:
        """Fold a ``CHART<id>STAND<series>:<reading>`` STAND1 snapshot from GiveMeMainmenu."""
        chart_raw, _, rest = payload.partition("STAND")
        if not chart_raw or not rest:
            return
        try:
            chart_id = int(chart_raw)
        except ValueError:
            return
        series_raw, _, reading = rest.partition(":")
        try:
            series = int(series_raw)
        except ValueError:
            return
        self.charts.setdefault(chart_id, ChartReading(unit="")).stands[series] = reading

    def _apply_chart_definition(self, chart_id: int, payload: str) -> None:
        """Fold ``SingelDiagramm<id>:<title>;<...>;<category>;<unit>;...``.

        Sets ``label`` (cleaned English) and ``category`` (raw SPA tag)
        on the chart. The 14th and 15th ``;``-fields hold category and
        unit when present; we tolerate shorter payloads silently
        because the server occasionally truncates these. SUMME charts
        carry ``SummeForDiagramm-<id>`` references to their constituent
        meters — extracted into ``summed_chart_ids``.
        """
        fields = payload.split(";")
        if not fields or not fields[0]:
            return
        chart = self.charts.setdefault(chart_id, ChartReading(unit=""))
        chart.label = _clean_chart_label(fields[0])
        if len(fields) > 13:
            chart.category = fields[13]
        if len(fields) > 14 and fields[14] and not chart.unit:
            chart.unit = fields[14]
        chart.summed_chart_ids = tuple(int(m) for m in _SUMME_CONSTITUENT_RE.findall(payload))


def _parse_stand_series(series_raw: str) -> int | None:
    if not series_raw.startswith("STAND"):
        return None
    try:
        return int(series_raw.removeprefix("STAND"))
    except ValueError:
        return None


def _safe_float(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None
