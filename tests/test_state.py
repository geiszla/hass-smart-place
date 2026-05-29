"""Tests for the HA-side :class:`SmartPlaceState` snapshot.

These exercise the pure-Python fold of inbound frames into the
dataclass. No HA imports — the module is intentionally HA-free for
exactly this kind of test.
"""

from __future__ import annotations

from smart_place_client import NamedFields, NamedValue, SmartPlaceState, Temperature, UnknownFrame, chart_target_status


def test_apply_outdoor_temperature_parses_float() -> None:
    state = SmartPlaceState()
    state.apply(NamedValue(name="OutdoorTemperature", value="14.5"))
    assert state.outdoor_temperature == 14.5


def test_apply_outdoor_temperature_invalid_float_keeps_none() -> None:
    """Non-numeric values don't crash — they leave the field as ``None``."""
    state = SmartPlaceState()
    state.apply(NamedValue(name="OutdoorTemperature", value="--"))
    assert state.outdoor_temperature is None


def test_apply_alarms_use_00_as_off() -> None:
    """The SPA encodes ``00`` as off; anything else (``01``, label) is on."""
    state = SmartPlaceState()
    state.apply(NamedValue(name="Rain", value="00"))
    state.apply(NamedValue(name="Hail", value="01"))
    state.apply(NamedValue(name="BlindsMaintenance", value=""))
    assert state.rain_alarm is False
    assert state.hail_alarm is True
    # Empty payload reads as off (no maintenance flag raised).
    assert state.blinds_maintenance is False


def test_apply_wind_alarm_indexed() -> None:
    """Per-zone wind alarms fold by index."""
    state = SmartPlaceState()
    state.apply(NamedValue(name="WindAlarm", value="00", index=1))
    state.apply(NamedValue(name="WindAlarm", value="01", index=2))
    assert state.wind_alarms == {1: False, 2: True}


def test_apply_package_box_stores_raw_label() -> None:
    """Package boxes store the raw label so the entity can decide free vs occupied."""
    state = SmartPlaceState()
    state.apply(NamedValue(name="PackageBox", value="Frei", index=1))
    state.apply(NamedValue(name="PackageBox", value="DHL-7842", index=2))
    assert state.package_boxes == {1: "Frei", 2: "DHL-7842"}


def test_apply_chart_point_update_collects_stand_series() -> None:
    """``ChartPointUpdate`` frames fold the STAND<series>:<reading> tail."""
    state = SmartPlaceState()
    state.apply(NamedValue(name="ChartPointUpdate", value="STAND99:6062.018", index=49))
    state.apply(NamedValue(name="ChartPointUpdate", value="STAND1:7.616", index=49))
    state.apply(NamedValue(name="ChartPointUpdate", value="STAND99:223329", index=337))
    assert state.charts[49].stands == {99: "6062.018", 1: "7.616"}
    assert state.charts[337].stands == {99: "223329"}


def test_apply_chart_point_update_ignores_malformed_value() -> None:
    """A non-STAND payload is dropped silently."""
    state = SmartPlaceState()
    state.apply(NamedValue(name="ChartPointUpdate", value="garbage", index=49))
    assert state.charts == {}


def test_apply_temperature_records_indoor_value() -> None:
    """``TEMPIST<N>`` frames fold into ``indoor_temperatures`` keyed by sensor."""
    state = SmartPlaceState()
    state.apply(Temperature(sensor=3, value=22.4))
    state.apply(Temperature(sensor=4, value=18.7))
    assert state.indoor_temperatures == {3: 22.4, 4: 18.7}


def test_apply_climate_config_strips_heating_tail() -> None:
    """``ClimateConfig`` name folds into ``climate_zones`` minus the 'heating' tag."""
    state = SmartPlaceState()
    state.apply(
        NamedFields(
            name="ClimateConfig",
            fields=("Bedroom heating", "254px", "419px", "Heizen", "", "rgb(0- 0- 0)", "Uebersicht1", "FanCoilOff"),
            index=1,
        ),
    )
    state.apply(
        NamedFields(
            name="ClimateConfig",
            fields=("Office Heizung", "x", "y", "Heizen", "", "rgb", "Uebersicht1", "FanCoilOff"),
            index=2,
        ),
    )
    assert state.climate_zones == {1: "Bedroom", 2: "Office"}


def test_apply_chart_definition_extracts_label_category_unit() -> None:
    """``ChartDefinition`` fills ``label`` / ``category`` / ``unit`` on the chart."""
    raw = "Elektro HH77-14-01;Area;Zeit;Verbrauch in Kw/h;Elektro=49=rgba(206,0,105,0.9)=rgba(206, 0, 105, 0.2);===;===;===;;CHF;0.1788;0.1249;2021018;Elektro;kWh;smartPLACE_Elektro;63;HH77-14-01 UG 11U2"
    state = SmartPlaceState()
    state.apply(NamedValue(name="ChartDefinition", value=raw, index=49))
    chart = state.charts[49]
    assert chart.label == "Electricity"
    assert chart.category == "Elektro"
    assert chart.unit == "kWh"


def test_apply_chart_definition_translates_german_kaltwasser() -> None:
    """``Kaltwasser I`` translates to ``Cold water I`` (suffix preserved)."""
    raw = "Kaltwasser I HH77-14-01;Area;Zeit;Verbrauch in l;Kaltwasser I=335=rgba=rgba;===;===;===;;CHF;0.002;;2021018;Wasser;l;smartPLACE_Wasser;91;HH77-14-01"
    state = SmartPlaceState()
    state.apply(NamedValue(name="ChartDefinition", value=raw, index=335))
    assert state.charts[335].label == "Cold water I"
    assert state.charts[335].category == "Wasser"


def test_apply_chart_definition_marks_summe_charts() -> None:
    """SUMME charts get ``category == 'Summe'`` so the platform can filter them."""
    raw = "SUMME Kaltwasser HH77-14-01;Area;Zeit;Verbrauch in l;SUMME Kaltwasser=337=rgba=rgba;===;===;===;;CHF;0.002;;2021018;Summe;l;>SummeForDiagramm-336>SummeForDiagramm-335>;250;SUMME Kaltwasser"
    state = SmartPlaceState()
    state.apply(NamedValue(name="ChartDefinition", value=raw, index=337))
    assert state.charts[337].category == "Summe"
    assert state.charts[337].label == "Cold water total"


def test_apply_chart_stand_snapshot_from_main_menu() -> None:
    """``ChartStand:<id>STAND<series>:<reading>`` frames fold into ``charts[id].stands``."""
    state = SmartPlaceState()
    state.apply(NamedValue(name="ChartStand", value="49STAND1:7.655"))
    state.apply(NamedValue(name="ChartStand", value="337STAND1:76"))
    assert state.charts[49].stands == {1: "7.655"}
    assert state.charts[337].stands == {1: "76"}


def test_apply_scene_state_and_config_fold_separately() -> None:
    """``SceneState`` flips the active flag; ``SceneConfig`` populates the name."""
    state = SmartPlaceState()
    state.apply(NamedValue(name="SceneState", value="01", index=9))
    state.apply(NamedValue(name="SceneState", value="00", index=10))
    state.apply(
        NamedFields(
            name="SceneConfig",
            fields=("Afternoon sun", "520px", "535px"),
            index=9,
        ),
    )
    state.apply(
        NamedFields(name="SceneConfig", fields=("Evening", "x", "y"), index=10),
    )
    assert state.scene_states == {9: True, 10: False}
    assert state.scenes == {9: "Afternoon sun", 10: "Evening"}


def test_apply_lights_central_and_blinds_central_track_groups() -> None:
    """LEUCHTENZENTRAL/JALZENTRAL fold into per-group booleans (00 / "" = off)."""
    state = SmartPlaceState()
    state.apply(NamedValue(name="LightsCentral", value="00", index=1))
    state.apply(NamedValue(name="LightsCentral", value="01", index=2))
    state.apply(NamedValue(name="BlindsCentral", value="", index=1))
    state.apply(NamedValue(name="BlindsCentral", value="03", index=2))
    assert state.lights_central == {1: False, 2: True}
    assert state.blinds_central == {1: False, 2: True}


def test_apply_humidity_records_per_zone_value() -> None:
    """``FEUCHTEIST<N>`` folds into ``humidities`` keyed by zone id."""
    state = SmartPlaceState()
    state.apply(NamedValue(name="Humidity", value="42.5", index=3))
    state.apply(NamedValue(name="Humidity", value="0.0", index=4))
    assert state.humidities == {3: 42.5, 4: 0.0}


def test_apply_humidity_ignores_invalid_float() -> None:
    """Non-numeric humidity values don't crash — they're dropped silently."""
    state = SmartPlaceState()
    state.apply(NamedValue(name="Humidity", value="--", index=3))
    assert state.humidities == {}


def test_apply_door_intercom_only_ring_means_on() -> None:
    """``SPRECHEN<N>:ring`` flips the per-intercom ringing flag; other values are off."""
    state = SmartPlaceState()
    state.apply(NamedValue(name="DoorIntercom", value="ring", index=1))
    state.apply(NamedValue(name="DoorIntercom", value="idle", index=2))
    assert state.intercom_ringing == {1: True, 2: False}


def test_apply_call_info_translates_known_german_labels() -> None:
    """``CALLINFO<N>`` known German labels translate to English; unknowns pass through."""
    state = SmartPlaceState()
    state.apply(NamedValue(name="CallInfo", value="Wohnungseingang", index=1))
    state.apply(NamedValue(name="CallInfo", value="Mystery Location", index=2))
    assert state.intercom_callers == {1: "Apartment entrance", 2: "Mystery Location"}


def test_apply_infoboard_content_read_becomes_none() -> None:
    """``INFOBOARD<n>INHALT:Read`` reads as ``None``; other text passes through."""
    state = SmartPlaceState()
    state.apply(NamedValue(name="InfoboardContent", value="Read", index=2))
    state.apply(NamedValue(name="InfoboardContent", value="New parcel waiting", index=1))
    assert state.infoboard_contents == {2: None, 1: "New parcel waiting"}


def test_apply_person_info_read_becomes_none() -> None:
    """``PERSINFO:Read`` clears the banner; other text is kept verbatim."""
    state = SmartPlaceState()
    state.apply(NamedValue(name="PersonInfo", value="Read"))
    assert state.person_info is None
    state.apply(NamedValue(name="PersonInfo", value="Family bonus available"))
    assert state.person_info == "Family bonus available"


def test_apply_chart_target_parses_float() -> None:
    """``CHARTZIEL<id>`` folds into ``chart_targets`` parsed as float."""
    state = SmartPlaceState()
    state.apply(NamedValue(name="ChartTarget", value="300", index=337))
    state.apply(NamedValue(name="ChartTarget", value="25.5", index=144))
    state.apply(NamedValue(name="ChartTarget", value="not-a-number", index=49))
    assert state.chart_targets == {337: 300.0, 144: 25.5}


def test_chart_target_status_thresholds_match_spa_traffic_light() -> None:
    """``chart_target_status`` mirrors the SPA's <60% / 60-90% / ≥90% breakpoints."""
    assert chart_target_status(0, 100) == "green"
    assert chart_target_status(59.0, 100) == "green"
    assert chart_target_status(60.0, 100) == "orange"
    assert chart_target_status(89.0, 100) == "orange"
    assert chart_target_status(90.0, 100) == "red"
    assert chart_target_status(150.0, 100) == "red"


def test_chart_target_status_returns_none_when_inputs_missing() -> None:
    """Without both current and a positive target, the status is unknown (None)."""
    assert chart_target_status(None, 100) is None
    assert chart_target_status(50.0, None) is None
    assert chart_target_status(50.0, 0) is None
    assert chart_target_status(50.0, -1) is None


def test_state_chart_target_status_method_pairs_stand1_with_target() -> None:
    """``SmartPlaceState.chart_target_status`` reads STAND1 + the target snapshot."""
    state = SmartPlaceState()
    state.apply(NamedValue(name="ChartStand", value="49STAND1:5"))
    state.apply(NamedValue(name="ChartTarget", value="10", index=49))
    assert state.chart_target_status(49) == "green"
    state.apply(NamedValue(name="ChartPointUpdate", value="STAND1:7.5", index=49))
    assert state.chart_target_status(49) == "orange"
    state.apply(NamedValue(name="ChartPointUpdate", value="STAND1:9.5", index=49))
    assert state.chart_target_status(49) == "red"


def test_apply_unknown_frame_does_not_raise() -> None:
    """The fold is best-effort — unknown frames are ignored."""
    state = SmartPlaceState()
    state.apply(UnknownFrame(raw="ZZZ:unmapped"))
    state.apply(NamedFields(name="MainMenuFinished", fields=()))
    # Nothing observed yet; the snapshot stays at its defaults.
    assert state.outdoor_temperature is None
    assert state.charts == {}
