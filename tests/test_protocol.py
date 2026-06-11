"""Pure-function tests for :mod:`smart_place_client.protocol`.

Per DESIGN §5.1 these tests:

- Cover happy path, malformed frames, the offline / legacy redirects,
  bootstrap message encoding, and SessionState transitions.
- Run with no async, no I/O, no network — instant.
"""

from __future__ import annotations

import pytest

from smart_place_client.messages import KNOWN_MESSAGES, parse_frame
from smart_place_client.protocol import (
    APP_WS_PATH,
    DISCOVERY_HOST,
    DISCOVERY_PORT,
    DISCOVERY_WS_PATH,
    GlobalConfig,
    GoToLinkSSL,
    HostNotOnline,
    MessageDefinition,
    NamedFields,
    NamedValue,
    ProtocolError,
    SessionPhase,
    SessionState,
    Temperature,
    UnknownFrame,
    _parse_fields_after_prefix,
    discovery_ws_url,
    encode_frame,
    parse_chart_references,
    parse_unit_hints,
    repair_mojibake,
)

# ---------------------------- parse_frame ----------------------------


def test_parse_go_to_link_ssl_with_path() -> None:
    """Live-observed 2026-05-28 route: port-with-path, token2='Leer'."""
    frame = parse_frame("GoToLinkSSL:spr1.smartplace.ch:8770/Start1:Leer")
    assert frame == GoToLinkSSL(host="spr1.smartplace.ch", port=8770, path="/Start1", token2="Leer")


def test_parse_go_to_link_ssl_bare_port() -> None:
    """JS source also supports the no-path variant with a real token2."""
    frame = parse_frame("GoToLinkSSL:host.example.com:9000:abc123")
    assert frame == GoToLinkSSL(host="host.example.com", port=9000, path=None, token2="abc123")


@pytest.mark.parametrize(
    "bad",
    [
        "GoToLinkSSL:onlytwo",
        "GoToLinkSSL:host:8770",
        "GoToLinkSSL::8770:token",
        "GoToLinkSSL:host::token",
        "GoToLinkSSL:host:8770:",
        "GoToLinkSSL:host:notaport:token",
    ],
)
def test_parse_go_to_link_ssl_malformed_raises(bad: str) -> None:
    with pytest.raises(ProtocolError):
        parse_frame(bad)


def test_parse_global_config() -> None:
    """Synthetic 6-field frame (older shape used in unit tests)."""
    frame = parse_frame("EINSTELLUNGENGLOBAL>de>0>80>1>23:00>30")
    assert frame == GlobalConfig(
        language="de",
        standby="0",
        brightness="80",
        screensaver_mode="1",
        screensaver_start="23:00",
        screensaver_duration="30",
    )


def test_parse_global_config_phase3_observed_shape() -> None:
    """Phase 3 live capture 2026-05-28: numeric fields, brightness as 0..1, 'undefined' literal."""
    frame = parse_frame("EINSTELLUNGENGLOBAL>2>300>0.8>1>300>undefined")
    assert frame == GlobalConfig(
        language="2",
        standby="300",
        brightness="0.8",
        screensaver_mode="1",
        screensaver_start="300",
        screensaver_duration="undefined",
    )


def test_parse_go_to_link_ssl_dynamic_routed_port() -> None:
    """Phase 3 capture confirmed routed port is dynamic, not always 8770."""
    frame = parse_frame("GoToLinkSSL:spr1.smartplace.ch:38435/Start1:Leer")
    assert frame == GoToLinkSSL(host="spr1.smartplace.ch", port=38435, path="/Start1", token2="Leer")


def test_parse_host_not_online_yields_typed_frame() -> None:
    """``HostNotOnline`` parses to the typed frame the client maps to ``SmartPlaceOfflineError``."""
    assert parse_frame("HostNotOnline") == HostNotOnline()


@pytest.mark.parametrize(
    ("text", "sensor", "value"),
    [
        ("TEMPIST1:26.6", 1, 26.6),
        ("TEMPIST3:27.2", 3, 27.2),
        ("TEMPIST6:25.8", 6, 25.8),
        ("TEMPIST12:21.0", 12, 21.0),
    ],
)
def test_parse_temperature_generalises_across_sensors(text: str, sensor: int, value: float) -> None:
    """One Temperature entry handles TEMPIST<N> for any N; value parses as float."""
    frame = parse_frame(text)
    assert frame == Temperature(sensor=sensor, value=value)


def test_parse_temperature_missing_value_raises() -> None:
    # Matches the registry prefix (TEMPIST3:) so it's routed to the parser,
    # but the parser's stricter regex requires at least one character after `:`.
    with pytest.raises(ProtocolError):
        parse_frame("TEMPIST3:")


def test_parse_temperature_non_float_value_raises() -> None:
    """Bad numeric values surface as ProtocolError, not raw ValueError."""
    with pytest.raises(ProtocolError):
        parse_frame("TEMPIST1:not-a-float")


def test_parse_outdoor_temperature() -> None:
    """TEMPOUT:<value> parses as NamedValue(name='OutdoorTemperature', value=...)."""
    frame = parse_frame("TEMPOUT:26.6")
    assert frame == NamedValue(name="OutdoorTemperature", value="26.6")


def test_parse_outdoor_temperature_missing_value_raises() -> None:
    with pytest.raises(ProtocolError):
        parse_frame("TEMPOUT:")


def test_parse_wind_speed() -> None:
    """WINDGESCHWINDIGKEIT:<value> parses as NamedValue(name='WindSpeed', value=...)."""
    frame = parse_frame("WINDGESCHWINDIGKEIT:7.9")
    assert frame == NamedValue(name="WindSpeed", value="7.9")


def test_parse_wind_speed_missing_value_raises() -> None:
    with pytest.raises(ProtocolError):
        parse_frame("WINDGESCHWINDIGKEIT:")


@pytest.mark.parametrize(
    "bad",
    [
        "EINSTELLUNGENGLOBAL>de",
        "EINSTELLUNGENGLOBAL>de>0>80>1>23:00",
        "EINSTELLUNGENGLOBAL>de>0>80>1>23:00>30>extra",
    ],
)
def test_parse_global_config_wrong_field_count(bad: str) -> None:
    with pytest.raises(ProtocolError):
        parse_frame(bad)


def test_parse_unknown_frame() -> None:
    """Unknown shapes don't crash — they propagate as UnknownFrame."""
    frame = parse_frame("UnregisteredPrefix:opaque")
    assert frame == UnknownFrame(raw="UnregisteredPrefix:opaque")


# ------------------ per-id pushes (indexed parsers) -------------------


def test_parse_indexed_value_extracts_index_and_value() -> None:
    """`prefix<N>:<value>` lands as NamedValue with `index` populated."""
    frame = parse_frame("leuchte13:255")
    assert frame == NamedValue(name="LightState", value="255", index=13)


def test_parse_indexed_value_allows_empty_value() -> None:
    """`prefix<N>:` with no payload still parses (e.g. JALZENTRAL1:)."""
    frame = parse_frame("JALZENTRAL1:")
    assert frame == NamedValue(name="BlindsCentral", value="", index=1)


def test_parse_indexed_fields_splits_on_comma() -> None:
    """Comma-delimited per-id configs land as NamedFields with index + fields."""
    frame = parse_frame("UnterMenuLeuchten1:All,70px,10px,Leuchten,OnOn,LEUCHTENZENTRAL1,Uebersicht1")
    assert frame == NamedFields(
        name="LightSubMenu",
        fields=("All", "70px", "10px", "Leuchten", "OnOn", "LEUCHTENZENTRAL1", "Uebersicht1"),
        index=1,
    )


def test_parse_marker_frame_has_empty_fields() -> None:
    """Marker frames (PongOK, GiveMeMainMenuFinished) decode as NamedFields with fields=()."""
    frame = parse_frame("PongOK")
    assert frame == NamedFields(name="PongOK", fields=())


def test_named_value_singleton_has_no_index() -> None:
    """Singleton NamedValue frames leave `index` as None."""
    frame = parse_frame("TEMPOUT:24.5")
    assert frame == NamedValue(name="OutdoorTemperature", value="24.5", index=None)


# ----------------- _parse_fields_after_prefix helper -----------------


def test_parse_fields_after_prefix_bare_prefix() -> None:
    assert _parse_fields_after_prefix("FOO", "FOO") == ()


def test_parse_fields_after_prefix_single_separator() -> None:
    assert _parse_fields_after_prefix("FOO>", "FOO") == ("",)


def test_parse_fields_after_prefix_fields_and_trailing_empty() -> None:
    assert _parse_fields_after_prefix("FOO>a>b", "FOO") == ("a", "b")
    assert _parse_fields_after_prefix("FOO>a>b>", "FOO") == ("a", "b", "")


def test_parse_fields_after_prefix_missing_prefix_raises() -> None:
    with pytest.raises(ProtocolError):
        _parse_fields_after_prefix("BAR>a", "FOO")


def test_parse_fields_after_prefix_non_separator_after_prefix_raises() -> None:
    with pytest.raises(ProtocolError):
        _parse_fields_after_prefix("FOOX>a", "FOO")


# --------------------------- routed URLs ----------------------------


def test_go_to_link_ssl_routed_url_leer_with_path() -> None:
    frame = GoToLinkSSL(host="h", port=8770, path="/Start1", token2="Leer")
    assert frame.routed_https_url == "https://h:8770/Start1"


def test_go_to_link_ssl_routed_url_real_token_uses_infoboard1() -> None:
    frame = GoToLinkSSL(host="h", port=8770, path=None, token2="abc")
    assert frame.routed_https_url == "https://h:8770/Infoboard1?abc"


def test_go_to_link_ssl_app_ws_url_and_origin() -> None:
    frame = GoToLinkSSL(host="example", port=8770, path="/Start1", token2="Leer")
    assert frame.app_ws_url == f"wss://example:8770{APP_WS_PATH}"
    assert frame.app_ws_origin == "https://example:8770"


# ----------------------------- encoders ------------------------------


def test_encode_frame_rejects_newlines() -> None:
    with pytest.raises(ProtocolError):
        encode_frame("bad\nframe")
    with pytest.raises(ProtocolError):
        encode_frame("bad\rframe")


def test_encode_frame_passes_through_arbitrary_text() -> None:
    assert encode_frame("leuchte12:75") == "leuchte12:75"


def test_discovery_ws_url_uses_constants() -> None:
    url = discovery_ws_url("tok123")
    assert url == f"wss://{DISCOVERY_HOST}:{DISCOVERY_PORT}{DISCOVERY_WS_PATH}?TOKEN=tok123"


# -------------------------- SessionState ----------------------------


def test_session_state_starts_in_discovery_open() -> None:
    state = SessionState()
    assert state.phase is SessionPhase.DISCOVERY_OPEN
    assert state.route is None
    assert state.global_config is None
    assert state.main_menu_loaded is False


def test_session_state_routing_transitions_to_routed() -> None:
    state = SessionState()
    state.on_discovery_frame(GoToLinkSSL("h", 8770, "/Start1", "Leer"))
    assert state.phase is SessionPhase.ROUTED
    assert state.route is not None


def test_session_state_unexpected_discovery_frame_raises() -> None:
    state = SessionState()
    with pytest.raises(ProtocolError):
        state.on_discovery_frame(GlobalConfig("de", "0", "0", "0", "0", "0"))


def test_session_state_full_happy_path() -> None:
    state = SessionState()
    state.on_discovery_frame(GoToLinkSSL("h", 8770, "/Start1", "Leer"))
    state.on_app_open()
    assert state.phase is SessionPhase.APP_OPEN
    state.on_app_frame(GlobalConfig("de", "0", "0", "0", "0", "0"))
    # Still APP_OPEN: needs the MainMenuFinished marker before BOOTSTRAPPED.
    assert state.phase is SessionPhase.APP_OPEN
    state.on_app_frame(NamedFields(name="MainMenuFinished", fields=()))
    assert state.phase is SessionPhase.BOOTSTRAPPED
    assert state.main_menu_loaded is True


def test_session_state_close_is_terminal() -> None:
    state = SessionState()
    state.on_discovery_frame(GoToLinkSSL("h", 8770, None, "tok"))
    state.close()
    assert state.phase is SessionPhase.CLOSED


def test_session_state_app_open_without_route_raises() -> None:
    state = SessionState()
    with pytest.raises(ProtocolError):
        state.on_app_open()


# --------------------------- KNOWN_MESSAGES --------------------------


def test_known_messages_registry_names_are_unique() -> None:
    """Each registry entry has a unique CamelCase name (the dispatch key)."""
    names = [defn.name for defn in KNOWN_MESSAGES]
    assert len(names) == len(set(names)), f"duplicate names: {names}"


def test_known_messages_entries_have_descriptions_and_examples() -> None:
    for defn in KNOWN_MESSAGES:
        assert isinstance(defn, MessageDefinition)
        assert defn.name
        assert defn.description, f"{defn.name} missing description"
        assert defn.pattern, f"{defn.name} missing pattern"
        assert defn.example, f"{defn.name} missing example"
        assert callable(defn.parse), f"{defn.name} parse not callable"
        assert defn.pattern.match(defn.example), f"{defn.name} example {defn.example!r} does not match its own pattern"


def test_parse_chart_references_extracts_id_series_and_unit() -> None:
    """Real-world StatusEntry value yields one ``(chart_id, series, unit)`` tuple."""
    raw = "SPtext397>CHART49STAND1~SPDB-CHARTSSTANDS>unit-KWh~>LinkOff"
    assert list(parse_chart_references(raw)) == [(49, 1, "KWh")]


def test_parse_chart_references_handles_multiple_units() -> None:
    """Water (``unit-l``) and electricity (``unit-KWh``) both extract."""
    raw = "SPtext>CHART337STAND1~SPDB-CHARTSSTANDS>unit-l~>LinkOff<SPtext>CHART49STAND2~SPDB-CHARTSSTANDS>unit-KWh~>LinkOff"
    refs = list(parse_chart_references(raw))
    assert refs == [(337, 1, "l"), (49, 2, "KWh")]


def test_parse_chart_references_returns_empty_when_no_chart() -> None:
    """A non-chart row (TEMPOUT label) yields nothing."""
    raw = "SPtext390>TEMPOUT~SPDB-REM>unit-C~>LinkOff"
    assert list(parse_chart_references(raw)) == []


def test_parse_unit_hints_extracts_singleton_units() -> None:
    """Unit-bearing rows yield ``(signal_name, unit)`` pairs (live shapes, 2026-06-11)."""
    assert list(parse_unit_hints("1_1_SPtext390>TEMPOUT~SPDB-REM>unit-°C~>LinkOff")) == [("TEMPOUT", "°C")]
    assert list(parse_unit_hints("1_4_SPtext393>WINDGESCHWINDIGKEIT~SPDB-REM>unit-km/h~>LinkOff")) == [
        ("WINDGESCHWINDIGKEIT", "km/h"),
    ]


def test_parse_unit_hints_skips_icon_rows() -> None:
    """Icon-bearing rows (REGEN / HAGEL) carry no unit and yield nothing."""
    raw = "1_2_SPtext391>REGEN~SPDB-REM>icon-regen~>LinkOff<SPtext402>HAGEL~SPDB-REM>icon-hagel~>LinkOff"
    assert list(parse_unit_hints(raw)) == []


def test_repair_mojibake_undoes_double_encoded_utf8() -> None:
    """The server's double-encoded German labels decode back to the intended text."""
    assert repair_mojibake("WÃ¤rme HH77-14-01") == "Wärme HH77-14-01"
    assert repair_mojibake("LÃ¼ftung Stufe 1") == "Lüftung Stufe 1"


def test_repair_mojibake_passes_through_ascii_and_clean_utf8() -> None:
    """ASCII and correctly-encoded text are returned unchanged."""
    assert repair_mojibake("TEMPIST1:24.2") == "TEMPIST1:24.2"
    assert repair_mojibake("Wärme") == "Wärme"


def test_parse_frame_repairs_mojibake_in_payload() -> None:
    """parse_frame feeds repaired text to the matched parser."""
    frame = parse_frame("SingelDiagramm144:WÃ¤rme HH77-14-01;Area;Zeit")
    assert isinstance(frame, NamedValue)
    assert frame.name == "ChartDefinition"
    assert frame.index == 144
    assert frame.value.startswith("Wärme ")


def test_known_messages_examples_round_trip_through_parse_frame() -> None:
    """parse_frame(defn.example) yields the named shape declared by the entry.

    Typed classes are identified by their dataclass name;
    NamedValue / NamedFields entries are identified by the parsed
    frame's ``name`` attribute (the dataclass itself is shared across
    many registry rows).
    """
    for defn in KNOWN_MESSAGES:
        frame = parse_frame(defn.example)
        if isinstance(frame, NamedValue | NamedFields):
            actual = frame.name
        else:
            actual = type(frame).__name__
        assert actual == defn.name, f"{defn.name} example {defn.example!r} parsed as {actual!r}"
