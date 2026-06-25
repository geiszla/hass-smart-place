"""Tests for the HA-side :class:`SmartPlaceState` snapshot.

These exercise the pure-Python fold of inbound frames into the
dataclass. No HA imports — the module is intentionally HA-free for
exactly this kind of test.
"""

from __future__ import annotations

from smart_place_client import (
    NamedFields,
    NamedValue,
    SmartPlaceState,
    Temperature,
    UnknownFrame,
    chart_target_status,
    parse_frame,
)


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


def test_apply_package_box_is_ignored() -> None:
    """PackageBox frames parse but are intentionally dropped by ``apply``.

    The unlock PIN rides in PERSINFO, not PACKETBOX (which only ever
    reported 'Frei' in practice), so the snapshot no longer tracks
    package boxes. The frame stays in KNOWN_MESSAGES purely so it parses
    cleanly instead of landing in unknown_frames — see messages.py.
    """
    state = SmartPlaceState()
    # Must not raise, and must not invent any package-box state.
    state.apply(NamedValue(name="PackageBox", value="Frei", index=1))
    state.apply(NamedValue(name="PackageBox", value="DHL-7842", index=2))
    assert not hasattr(state, "package_boxes")
    assert state.package_delivery_pin is None


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


def test_apply_status_entry_folds_unit_hints_with_mojibake_repair() -> None:
    """A raw TEMPOUT status row (mojibake ``Â°C`` as sent live) folds to a clean hint."""
    raw = "StatusInhaltListe_1_1_SPtext390>TEMPOUT~SPDB-REM>unit-Â°C~>LinkOff"
    state = SmartPlaceState()
    state.apply(parse_frame(raw))
    assert state.unit_hints == {"TEMPOUT": "°C"}


def test_apply_chart_definition_translates_mojibake_waerme() -> None:
    """A double-encoded ``WÃ¤rme`` wire frame ends up as the English ``Heating``.

    Exercises the full path: ``parse_frame`` repairs the mojibake the
    live server sends (captured 2026-06-11), then the German→English
    chart-label map — which the mojibake used to silently defeat —
    matches ``Wärme``.
    """
    raw = "SingelDiagramm144:WÃ¤rme HH77-14-01;Area;Zeit;Verbrauch in Kw/h;WÃ¤rme=144=rgba=rgba;===;===;===;;CHF;0.9;;2021018;Energie;kWh;smartPLACE_Energie_W;53;HH77-14-01 DG 11U3"
    state = SmartPlaceState()
    state.apply(parse_frame(raw))
    assert state.charts[144].label == "Heating"
    assert state.charts[144].unit == "kWh"


def test_apply_chart_definition_marks_summe_charts() -> None:
    """SUMME charts carry their constituent meter ids and drop the "total" tag."""
    raw = "SUMME Kaltwasser HH77-14-01;Area;Zeit;Verbrauch in l;SUMME Kaltwasser=337=rgba=rgba;===;===;===;;CHF;0.002;;2021018;Summe;l;>SummeForDiagramm-336>SummeForDiagramm-335>;250;SUMME Kaltwasser"
    state = SmartPlaceState()
    state.apply(NamedValue(name="ChartDefinition", value=raw, index=337))
    assert state.charts[337].category == "Summe"
    assert state.charts[337].label == "Cold water"
    assert state.charts[337].summed_chart_ids == (336, 335)


def test_apply_chart_definition_non_summe_has_no_constituents() -> None:
    """Per-meter charts carry no ``SummeForDiagramm`` references."""
    raw = "Kaltwasser I HH77-14-01;Area;Zeit;Verbrauch in l;Kaltwasser I=335=rgba=rgba;===;===;===;;CHF;0.002;;2021018;Wasser;l;smartPLACE_Wasser;91;HH77-14-01"
    state = SmartPlaceState()
    state.apply(NamedValue(name="ChartDefinition", value=raw, index=335))
    assert state.charts[335].summed_chart_ids == ()


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


def test_apply_gsa_config_extracts_cameras_and_labels() -> None:
    """The full GlobalGsa wire frame folds into cameras + English door labels.

    Exercises the whole path: ``parse_frame`` classifies the
    ``GlobalGsa>...`` text as a ``GsaConfig`` ``NamedFields``, then the
    fold splits field [3] (camera id^link) and field [4] (opener
    id^label) and translates the labels. Wire is the 2026-05-28 capture
    with the LAN IP swapped for a placeholder.
    """
    raw = (
        "GlobalGsa>YEALINKOFF>10.0.0.1>60"
        ">1^/linkmap1<2^/linkmap2<3^/linkmap3<4^/linkmap4"
        ">1^Eingang EG<2^Briefkasten<3^Garage<4^Eingang WHG>70>"
    )
    state = SmartPlaceState()
    state.apply(parse_frame(raw))
    assert state.gsa_cameras == {1: "/linkmap1", 2: "/linkmap2", 3: "/linkmap3", 4: "/linkmap4"}
    assert state.gsa_door_labels == {
        1: "Ground floor entrance",
        2: "Mailbox",
        3: "Garage",
        4: "Front door",
    }


def test_apply_gsa_config_tolerates_leer_and_unknown_labels() -> None:
    """``LEER`` cameras yield no entities; unknown opener labels pass through raw."""
    raw = "GlobalGsa>YEALINKOFF>10.0.0.1>60>LEER>1^Seiteneingang>70>"
    state = SmartPlaceState()
    state.apply(parse_frame(raw))
    assert state.gsa_cameras == {}
    # Unknown German label has no translation entry → kept verbatim.
    assert state.gsa_door_labels == {1: "Seiteneingang"}


def test_apply_gsa_config_tolerates_truncated_frame() -> None:
    """A GlobalGsa frame cut off before the camera field leaves the maps empty."""
    state = SmartPlaceState()
    state.apply(parse_frame("GlobalGsa>YEALINKOFF>10.0.0.1>60>"))
    assert state.gsa_cameras == {}
    assert state.gsa_door_labels == {}


def test_apply_central_master_button_frames_are_ignored() -> None:
    """LEUCHTENZENTRAL / JALZENTRAL are deliberately not folded.

    Each is the SPA's per-type "All" master-button state, not an
    aggregate (verified live 2026-06-11), so the snapshot drops both —
    the frames must still apply without error.
    """
    state = SmartPlaceState()
    state.apply(NamedValue(name="LightsCentral", value="01", index=1))
    state.apply(NamedValue(name="BlindsCentral", value="03", index=1))
    assert not hasattr(state, "lights_central")
    assert not hasattr(state, "blinds_central")


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
    assert state.intercom_callers == {1: "Front door", 2: "Mystery Location"}


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


def test_package_delivery_pin_extracted_from_persinfo_banner() -> None:
    """The parcel PIN is pulled out of the German PERSINFO delivery banner."""
    state = SmartPlaceState()
    state.apply(
        NamedValue(
            name="PersonInfo",
            value=("Sie haben eine Lieferung in der Paketbox. Bitte verwenden Sie den PIN:4489 um diese rauszuholen."),
        ),
    )
    assert state.package_delivery_pin == "4489"


def test_package_delivery_pin_none_when_banner_cleared() -> None:
    """``PERSINFO:Read`` clears the banner, so the PIN reads None again."""
    state = SmartPlaceState()
    state.apply(NamedValue(name="PersonInfo", value="Bitte verwenden Sie den PIN:4489"))
    assert state.package_delivery_pin == "4489"
    state.apply(NamedValue(name="PersonInfo", value="Read"))
    assert state.package_delivery_pin is None


def test_package_delivery_pin_none_when_banner_has_no_pin() -> None:
    """A non-delivery PERSINFO banner yields no PIN."""
    state = SmartPlaceState()
    state.apply(NamedValue(name="PersonInfo", value="Family bonus available"))
    assert state.package_delivery_pin is None


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
