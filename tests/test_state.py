"""Tests for the HA-side :class:`SmartPlaceState` snapshot.

These exercise the pure-Python fold of inbound frames into the
dataclass. No HA imports — the module is intentionally HA-free for
exactly this kind of test.
"""

from __future__ import annotations

from smart_place_client import NamedFields, NamedValue, SmartPlaceState, Temperature, UnknownFrame


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


def test_apply_unknown_frame_does_not_raise() -> None:
    """The fold is best-effort — unknown frames are ignored."""
    state = SmartPlaceState()
    state.apply(UnknownFrame(raw="ZZZ:unmapped"))
    state.apply(NamedFields(name="MainMenuFinished", fields=()))
    # Nothing observed yet; the snapshot stays at its defaults.
    assert state.outdoor_temperature is None
    assert state.charts == {}
