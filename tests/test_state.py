"""Tests for the HA-side :class:`SmartPlaceState` snapshot.

These exercise the pure-Python fold of inbound frames into the
dataclass. No HA imports — the module is intentionally HA-free for
exactly this kind of test.
"""

from __future__ import annotations

import pytest

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
    raw = "Elektro XX00-00-00;Area;Zeit;Verbrauch in Kw/h;Elektro=49=rgba(206,0,105,0.9)=rgba(206, 0, 105, 0.2);===;===;===;;CHF;0.1788;0.1249;0000000;Elektro;kWh;smartPLACE_Elektro;63;XX00-00-00"
    state = SmartPlaceState()
    state.apply(NamedValue(name="ChartDefinition", value=raw, index=49))
    chart = state.charts[49]
    assert chart.label == "Electricity"
    assert chart.category == "Elektro"
    assert chart.unit == "kWh"


def test_apply_chart_definition_translates_german_kaltwasser() -> None:
    """``Kaltwasser I`` translates to ``Cold water I`` (suffix preserved)."""
    raw = "Kaltwasser I XX00-00-00;Area;Zeit;Verbrauch in l;Kaltwasser I=335=rgba=rgba;===;===;===;;CHF;0.002;;0000000;Wasser;l;smartPLACE_Wasser;91;XX00-00-00"
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
    raw = "SingelDiagramm144:WÃ¤rme XX00-00-00;Area;Zeit;Verbrauch in Kw/h;WÃ¤rme=144=rgba=rgba;===;===;===;;CHF;0.9;;0000000;Energie;kWh;smartPLACE_Energie_W;53;XX00-00-00"
    state = SmartPlaceState()
    state.apply(parse_frame(raw))
    assert state.charts[144].label == "Heating"
    assert state.charts[144].unit == "kWh"


def test_apply_chart_definition_marks_summe_charts() -> None:
    """SUMME charts carry their constituent meter ids and drop the "total" tag."""
    raw = "SUMME Kaltwasser XX00-00-00;Area;Zeit;Verbrauch in l;SUMME Kaltwasser=337=rgba=rgba;===;===;===;;CHF;0.002;;0000000;Summe;l;>SummeForDiagramm-336>SummeForDiagramm-335>;250;SUMME Kaltwasser"
    state = SmartPlaceState()
    state.apply(NamedValue(name="ChartDefinition", value=raw, index=337))
    assert state.charts[337].category == "Summe"
    assert state.charts[337].label == "Cold water"
    assert state.charts[337].summed_chart_ids == (336, 335)


def test_apply_chart_definition_non_summe_has_no_constituents() -> None:
    """Per-meter charts carry no ``SummeForDiagramm`` references."""
    raw = "Kaltwasser I XX00-00-00;Area;Zeit;Verbrauch in l;Kaltwasser I=335=rgba=rgba;===;===;===;;CHF;0.002;;0000000;Wasser;l;smartPLACE_Wasser;91;XX00-00-00"
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
        "GlobalGsa>YEALINKOFF>192.0.2.1>60"
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
    raw = "GlobalGsa>YEALINKOFF>192.0.2.1>60>LEER>1^Seiteneingang>70>"
    state = SmartPlaceState()
    state.apply(parse_frame(raw))
    assert state.gsa_cameras == {}
    # Unknown German label has no translation entry → kept verbatim.
    assert state.gsa_door_labels == {1: "Seiteneingang"}


def test_apply_gsa_config_tolerates_truncated_frame() -> None:
    """A GlobalGsa frame cut off before the camera field leaves the maps empty."""
    state = SmartPlaceState()
    state.apply(parse_frame("GlobalGsa>YEALINKOFF>192.0.2.1>60>"))
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
    """An invalid first reading is dropped — the zone isn't surfaced."""
    state = SmartPlaceState()
    assert state.apply(NamedValue(name="Humidity", value="--", index=3)) is False
    assert state.humidities == {}


def test_apply_humidity_invalid_clears_known_zone() -> None:
    """A later invalid payload clears a known zone to None (not stale)."""
    state = SmartPlaceState()
    assert state.apply(NamedValue(name="Humidity", value="42.5", index=3)) is True
    # ``--`` is the SPA's no-reading marker: the prior 42.5 must not linger.
    assert state.apply(NamedValue(name="Humidity", value="--", index=3)) is True
    assert state.humidities == {3: None}
    # Re-sending the invalid marker is now a no-op (already None).
    assert state.apply(NamedValue(name="Humidity", value="--", index=3)) is False


def test_apply_door_intercom_only_ring_means_on() -> None:
    """``SPRECHEN<N>:ring`` flips the per-intercom ringing flag; other values are off."""
    state = SmartPlaceState()
    assert state.apply(NamedValue(name="DoorIntercom", value="ring", index=1)) is True
    assert state.apply(NamedValue(name="DoorIntercom", value="idle", index=2)) is True
    assert state.intercom_ringing == {1: True, 2: False}
    # A re-sent identical value is a no-op for the fan-out gate.
    assert state.apply(NamedValue(name="DoorIntercom", value="ring", index=1)) is False


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


def test_apply_returns_true_on_change_false_on_resend() -> None:
    """``apply`` reports whether the snapshot changed — the fan-out gate.

    The server re-broadcasts unchanged values ~2-3x/s; the HA layer skips
    the per-entity fan-out when ``apply`` returns ``False``.
    """
    state = SmartPlaceState()
    # First sighting of a value is a change.
    assert state.apply(NamedValue(name="WindSpeed", value="7.9")) is True
    # Re-sending the same value is a no-op.
    assert state.apply(NamedValue(name="WindSpeed", value="7.9")) is False
    # A new value is a change again.
    assert state.apply(NamedValue(name="WindSpeed", value="8.0")) is True


def test_apply_change_detection_across_frame_kinds() -> None:
    """Indexed dicts, indoor temps, charts, and unknowns all gate correctly."""
    state = SmartPlaceState()
    # Indexed alarm dict.
    assert state.apply(NamedValue(name="WindAlarm", value="01", index=2)) is True
    assert state.apply(NamedValue(name="WindAlarm", value="01", index=2)) is False
    # Indoor temperature (Temperature frame).
    assert state.apply(Temperature(sensor=3, value=22.4)) is True
    assert state.apply(Temperature(sensor=3, value=22.4)) is False
    assert state.apply(Temperature(sensor=3, value=22.5)) is True
    # Chart STAND series.
    assert state.apply(NamedValue(name="ChartStand", value="49STAND1:5")) is True
    assert state.apply(NamedValue(name="ChartStand", value="49STAND1:5")) is False
    # Unknown / unhandled frames never count as a change.
    assert state.apply(UnknownFrame(raw="ZZZ:unmapped")) is False
    assert state.apply(NamedValue(name="LightsCentral", value="01", index=1)) is False


# Real reply to ``SingelStandUpdate:49:<von>:<bis>`` (2026-07-13 probe), in arrival order.
_RANGE_REPLY = (
    "SingelStandUpdate49:98:63.357::::",
    "SingelStandUpdate49:97:41.0819999999999::::",
    "SingelStandUpdate49:96:22.2750000000001::::",
    "SingelStand2Update49:99:4744.52",
    "SingelStand3Update49:99:4807.877",
)


def test_apply_chart_range_reply_publishes_buckets_in_one_step() -> None:
    """Range buckets become visible with the reply's last frame, not one by one.

    Series 97 = HT, 96 = NT, 98 = total, with '::::'-padding after the
    reading. Publishing per frame showed the new day's HT bucket next to
    the old day's NT bucket right after midnight.
    """
    state = SmartPlaceState()
    for text in _RANGE_REPLY[:-1]:
        assert state.apply(parse_frame(text)) is False
        assert state.chart_today_buckets == {}
    assert state.apply(parse_frame(_RANGE_REPLY[-1])) is True
    assert state.chart_today_buckets[49] == {98: 63.357, 97: 41.0819999999999, 96: 22.2750000000001}
    # Re-sending the same reply is a no-op (fan-out gate).
    assert [state.apply(parse_frame(text)) for text in _RANGE_REPLY] == [False] * 5


def test_apply_chart_range_reply_is_stamped_with_the_requested_day() -> None:
    """A complete reply records which day its buckets cover."""
    state = SmartPlaceState()
    state.chart_range_request = 1783893600
    for text in _RANGE_REPLY:
        state.apply(parse_frame(text))
    assert state.chart_today_starts == {49: 1783893600}
    # The next day's request does not relabel yesterday's buckets ...
    state.chart_range_request = 1783980000
    assert state.chart_today_starts == {49: 1783893600}
    for text in ("SingelStandUpdate49:97:0::::", "SingelStandUpdate49:96:0.02::::"):
        assert state.apply(parse_frame(text)) is False
    assert state.chart_today_buckets[49][96] == 22.2750000000001
    # ... its own complete reply does, together with the new values.
    assert state.apply(parse_frame("SingelStand3Update49:99:4807.9")) is True
    assert state.chart_today_starts == {49: 1783980000}
    assert state.chart_today_buckets[49] == {97: 0.0, 96: 0.02}


def test_apply_chart_range_reply_for_a_new_day_replaces_the_buckets() -> None:
    """A series missing in the first reply after midnight is unknown, not yesterday's value."""
    state = SmartPlaceState()
    state.chart_range_request = 1783893600
    for text in _RANGE_REPLY:
        state.apply(parse_frame(text))
    state.chart_range_request = 1783980000
    for text in (
        "SingelStandUpdate49:98:0.02::::",
        "SingelStandUpdate49:97:Keine Werte::::",
        "SingelStandUpdate49:96:0.02::::",
        "SingelStand3Update49:99:4807.9",
    ):
        state.apply(parse_frame(text))
    assert state.chart_today_buckets[49] == {98: 0.02, 96: 0.02}
    assert state.chart_today_starts == {49: 1783980000}


def test_apply_chart_range_reply_for_the_same_day_keeps_a_series_it_lacks() -> None:
    """Within a day a poll without a number for one series must not drop that series."""
    state = SmartPlaceState()
    state.chart_range_request = 1783893600
    for text in _RANGE_REPLY:
        state.apply(parse_frame(text))
    for text in (
        "SingelStandUpdate49:98:64.0::::",
        "SingelStandUpdate49:97:Keine Werte::::",
        "SingelStandUpdate49:96:22.9::::",
        "SingelStand3Update49:99:4808.5",
    ):
        state.apply(parse_frame(text))
    assert state.chart_today_buckets[49] == {98: 64.0, 97: 41.0819999999999, 96: 22.9}


def test_apply_chart_range_stand_ignores_non_numeric_readings() -> None:
    """'Keine Werte' (the dead PV chart) must not clear, restamp or crash anything."""
    state = SmartPlaceState()
    state.chart_range_request = 100
    state.apply(parse_frame("SingelStandUpdate904:98:5.0::::"))
    state.apply(parse_frame("SingelStand3Update904:99:1.0"))
    state.chart_range_request = 200
    assert state.apply(parse_frame("SingelStandUpdate904:98:Keine Werte::::")) is False
    assert state.apply(parse_frame("SingelStand3Update904:99:Keine Werte")) is False
    assert state.chart_today_buckets[904][98] == 5.0
    assert state.chart_today_starts == {904: 100}


def test_clear_control_values_drops_a_half_received_range_reply() -> None:
    """A reply cut off by a disconnect is not completed by the next connection."""
    state = SmartPlaceState()
    state.apply(parse_frame("SingelStandUpdate49:97:41.0::::"))
    state.clear_control_values()
    assert state.apply(parse_frame("SingelStand3Update49:99:4807.877")) is False
    assert state.chart_today_buckets == {}


def test_chart_range_registers_parse_but_do_not_fold() -> None:
    """Range start/end register frames parse cleanly and change nothing."""
    state = SmartPlaceState()
    assert state.apply(parse_frame("SingelStand2Update49:99:4744.52")) is False
    assert state.apply(parse_frame("SingelStand3Update49:99:4807.877")) is False
    assert state.apply(parse_frame("FinishSingelChartUpdate49")) is False
    assert not state.chart_today_buckets


# ------------------------- control devices ---------------------------
#
# Wire strings below are frames from the 2026-11-09 capture.


def test_apply_light_config_and_level() -> None:
    """``INHALTLeuchten<n>`` describes the light, ``leuchte<n>`` carries its level."""
    state = SmartPlaceState()
    assert (
        state.apply(
            parse_frame("INHALTLeuchten14:Big bathroom lights,126px,544px,led,,dimmkurve1,Uebersicht1,,MemoryOFF"),
        )
        is True
    )
    assert state.lights[14].name == "Big bathroom lights"
    assert (state.lights[14].top, state.lights[14].left) == (126, 544)
    assert state.apply(parse_frame("leuchte14:250")) is True
    assert state.apply(parse_frame("leuchte13:0")) is True
    assert state.light_levels == {14: 250, 13: 0}
    # Rebroadcasts of unchanged frames do not wake the entities.
    assert state.apply(parse_frame("leuchte14:250")) is False
    assert (
        state.apply(
            parse_frame("INHALTLeuchten14:Big bathroom lights,126px,544px,led,,dimmkurve1,Uebersicht1,,MemoryOFF"),
        )
        is False
    )


def test_apply_light_level_ignores_non_numeric_value() -> None:
    state = SmartPlaceState()
    assert state.apply(parse_frame("leuchte14:on")) is False
    assert state.light_levels == {}


def test_apply_blind_config_and_value() -> None:
    """``JALICO<n>`` folds to a position or a weather alarm."""
    state = SmartPlaceState()
    assert state.apply(parse_frame("INHALTJalousien1:Bath blinds,127px,778px,jalousie,,60,Uebersicht1")) is True
    assert state.blinds[1].kind == "jalousie"
    assert state.apply(parse_frame("JALICO1:48-00")) is True
    assert state.apply(parse_frame("JALICO2:regenalarm-00")) is True
    assert state.blind_values[1].position == 48
    assert state.blind_values[2].position is None
    assert state.blind_values[2].alarm == "rain"
    assert state.apply(parse_frame("JALICO1:48-00")) is False
    assert state.apply(parse_frame("JALICO1:50-00")) is True
    # An alarm word that is not known here still means "no position".
    assert state.apply(parse_frame("JALICO1:sturmalarm-00")) is True
    assert (state.blind_values[1].position, state.blind_values[1].alarm) == (None, "sturmalarm")


def test_apply_climate_config_keeps_room_names_for_the_sensors() -> None:
    """The typed zone and the plain room-name map come from the same frame."""
    state = SmartPlaceState()
    frame = parse_frame("INHALTKlimas5:Large bathroom heating,108px,569px,Heizen,,rgb(0- 0- 0),Uebersicht1,FanCoilOff")
    assert state.apply(frame) is True
    assert state.climate_zones == {5: "Large bathroom"}
    assert state.climate[5].room == "Large bathroom"
    assert (state.climate[5].top, state.climate[5].left) == (108, 569)
    assert state.apply(frame) is False


def test_apply_setpoint_and_climate_info() -> None:
    state = SmartPlaceState()
    assert state.apply(parse_frame("TEMPSOLL5:21")) is True
    assert state.apply(parse_frame("TEMPSOLL6:18")) is True
    assert state.apply(parse_frame("KLIMASINFO5:heizen")) is True
    assert state.apply(parse_frame("KLIMASINFO6:null")) is True
    assert state.setpoints == {5: 21, 6: 18}
    assert state.climate_info == {5: "heizen", 6: None}
    assert state.apply(parse_frame("TEMPSOLL5:21")) is False
    assert state.apply(parse_frame("KLIMASINFO6:null")) is False


def test_apply_scene_config_keeps_names_and_adds_typed_config() -> None:
    state = SmartPlaceState()
    frame = parse_frame(
        "INHALTSZENEN5:LÃ¼ftung Stufe 3,655px,745px,25.png,OnOff,NoRemove,,,00-00,,00-00,NoPw,"
        "SzenenSonnenAufOFF,SzenenSonnenUnterOFF,SzenenSonnenAufOFF,SzenenSonnenUnterOFF,SollDisplayON,SzenenForm2",
    )
    assert state.apply(frame) is True
    assert state.scenes == {5: "Lüftung Stufe 3"}
    assert state.scene_configs[5].name == "Lüftung Stufe 3"
    assert state.scene_configs[5].kind == "OnOff"
    assert state.scene_configs[5].pin_protected is False
    assert state.apply(frame) is False


def test_apply_sub_menus_and_plan() -> None:
    state = SmartPlaceState()
    assert (
        state.apply(parse_frame("UnterMenuLeuchten1:All,70px,10px,Leuchten,OnOn,LEUCHTENZENTRAL1,Uebersicht1")) is True
    )
    assert (
        state.apply(parse_frame("UnterMenuJalousien1:All,70px,10px,Jalousien,OffOff,JALZENTRAL1,Uebersicht1")) is True
    )
    assert state.apply(parse_frame("Floorplan1:XX00-00-00,70px,10px,Leuchten-Jalousien-Klima")) is True
    assert state.light_sub_menus[1].group_message == "LEUCHTENZENTRAL1"
    assert state.blind_sub_menus[1].plan_id == 1
    assert state.plans[1].functions == ("Leuchten", "Jalousien", "Klima")
    assert state.apply(parse_frame("Floorplan1:XX00-00-00,70px,10px,Leuchten-Jalousien-Klima")) is False


def test_apply_mute() -> None:
    state = SmartPlaceState()
    assert state.apply(parse_frame("MUTE1:00")) is True
    assert state.mutes == {1: False}
    assert state.apply(parse_frame("MUTE1:01")) is True
    assert state.mutes == {1: True}
    assert state.apply(parse_frame("MUTE1:01")) is False


@pytest.mark.parametrize("code", ["", "02", "1", "0", "on", "true", "001"])
def test_scene_and_mute_flags_other_than_00_and_01_mean_unknown(code: str) -> None:
    """Such a value must not read as "on" (or "off"): it would decide a toggle."""
    state = SmartPlaceState()
    # Never stored when it is the first value ...
    assert state.apply(parse_frame(f"SZENEN3:{code}")) is False
    assert state.apply(parse_frame(f"MUTE1:{code}")) is False
    assert state.scene_states == {}
    assert state.mutes == {}
    # ... and it removes a value that was known.
    state.apply(parse_frame("SZENEN3:01"))
    state.apply(parse_frame("SZENEN4:00"))
    state.apply(parse_frame("MUTE1:00"))
    assert state.apply(parse_frame(f"SZENEN3:{code}")) is True
    assert state.apply(parse_frame(f"MUTE1:{code}")) is True
    assert state.scene_states == {4: False}
    assert state.mutes == {}
    assert state.apply(parse_frame(f"SZENEN3:{code}")) is False


def test_credential_frame_changes_nothing() -> None:
    """The remote-access password frames parse to an empty value and fold to nothing."""
    state = SmartPlaceState()
    frame = parse_frame("144Update:not-the-real-password")
    assert "not-the-real-password" not in repr(frame)
    assert state.apply(frame) is False


def test_clear_control_values_forgets_values_but_keeps_devices() -> None:
    """After a disconnect nothing old may decide a toggle; the devices stay known."""
    state = SmartPlaceState()
    for text in (
        "INHALTLeuchten13:Corridor lights,289px,330px,schalter,,dimmkurve1,Uebersicht1,,MemoryOFF",
        "leuchte13:255",
        "JALICO1:48-00",
        "TEMPSOLL5:21",
        "KLIMASINFO5:heizen",
        "SZENEN11:01",
        "MUTE1:01",
        "TEMPIST5:20.7",
    ):
        state.apply(parse_frame(text))
    assert state.clear_control_values() is True
    assert state.light_levels == {}
    assert state.blind_values == {}
    assert state.setpoints == {}
    assert state.climate_info == {}
    assert state.scene_states == {}
    assert state.mutes == {}
    # Device descriptions and plain readings are not control values.
    assert 13 in state.lights
    assert state.indoor_temperatures == {5: 20.7}
    # Nothing left to clear: no listener needs to wake.
    assert state.clear_control_values() is False
    # The server replays the values after the reconnect; that is a change again.
    assert state.apply(parse_frame("leuchte13:255")) is True
