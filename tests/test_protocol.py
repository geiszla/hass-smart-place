"""Pure-function tests for :mod:`smart_place_client.protocol`.

Per DESIGN §5.1 these tests:

- Cover happy path, malformed frames, the offline / legacy redirects,
  bootstrap message encoding, and SessionState transitions.
- Run with no async, no I/O, no network — instant.
"""

from __future__ import annotations

import pytest

from smart_place_client.protocol import (
    APP_WS_PATH,
    DISCOVERY_HOST,
    DISCOVERY_PORT,
    DISCOVERY_WS_PATH,
    GLOBAL_CONFIG_REQUEST,
    LEGACY_HOST,
    LEGACY_PORT,
    STATUS_LISTE_REQUEST,
    GlobalConfig,
    GoToLinkOldSystem,
    GoToLinkSSL,
    HostNotOnline,
    ProtocolError,
    SessionPhase,
    SessionState,
    StatusListe,
    UnknownFrame,
    discovery_ws_url,
    encode_frame,
    encode_global_config_request,
    encode_status_liste_request,
    parse_frame,
)

# ---------------------------- parse_frame ----------------------------


def test_parse_host_not_online() -> None:
    assert parse_frame("HostNotOnline") == HostNotOnline()


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


def test_parse_go_to_link_old_system() -> None:
    frame = parse_frame("GoToLinkOLDSYSTEM:legacy-token-xyz")
    assert frame == GoToLinkOldSystem(token2="legacy-token-xyz")


def test_parse_go_to_link_old_system_empty_token() -> None:
    with pytest.raises(ProtocolError):
        parse_frame("GoToLinkOLDSYSTEM:")


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


def test_parse_status_liste_phase3_observed_shape() -> None:
    """Phase 3 live capture 2026-05-28: info-board tab labels with trailing empty field."""
    frame = parse_frame("StatusListe>Wetter>Tagesverbrauch>")
    assert frame == StatusListe(fields=("Wetter", "Tagesverbrauch", ""))


def test_parse_go_to_link_ssl_dynamic_routed_port() -> None:
    """Phase 3 capture confirmed routed port is dynamic, not always 8770."""
    frame = parse_frame("GoToLinkSSL:spr1.smartplace.ch:38435/Start1:Leer")
    assert frame == GoToLinkSSL(host="spr1.smartplace.ch", port=38435, path="/Start1", token2="Leer")


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


def test_parse_status_liste() -> None:
    """Live-observed 2026-05-28 3-field frame; kept as a tuple."""
    frame = parse_frame("StatusListe>1>2>3")
    assert frame == StatusListe(fields=("1", "2", "3"))


def test_parse_unknown_frame() -> None:
    """Unknown shapes don't crash — they propagate as UnknownFrame."""
    frame = parse_frame("leuchte12:75")
    assert frame == UnknownFrame(raw="leuchte12:75")


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


def test_legacy_url() -> None:
    frame = GoToLinkOldSystem(token2="legacytok")
    assert frame.legacy_url == f"https://{LEGACY_HOST}:{LEGACY_PORT}/Start2?legacytok"


# ----------------------------- encoders ------------------------------


def test_encoders_round_trip_to_known_strings() -> None:
    assert encode_global_config_request() == GLOBAL_CONFIG_REQUEST
    assert encode_status_liste_request() == STATUS_LISTE_REQUEST


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
    assert state.status_liste is None


def test_session_state_routing_transitions_to_routed() -> None:
    state = SessionState()
    state.on_discovery_frame(GoToLinkSSL("h", 8770, "/Start1", "Leer"))
    assert state.phase is SessionPhase.ROUTED
    assert state.route is not None


def test_session_state_host_not_online_branch() -> None:
    state = SessionState()
    state.on_discovery_frame(HostNotOnline())
    assert state.phase is SessionPhase.OFFLINE


def test_session_state_legacy_branch() -> None:
    state = SessionState()
    state.on_discovery_frame(GoToLinkOldSystem(token2="x"))
    assert state.phase is SessionPhase.LEGACY


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
    # Still APP_OPEN: need both bootstrap reads.
    assert state.phase is SessionPhase.APP_OPEN
    state.on_app_frame(StatusListe(fields=("a", "b", "c")))
    assert state.phase is SessionPhase.BOOTSTRAPPED


def test_session_state_close_is_terminal() -> None:
    state = SessionState()
    state.on_discovery_frame(GoToLinkSSL("h", 8770, None, "tok"))
    state.close()
    assert state.phase is SessionPhase.CLOSED


def test_session_state_app_open_without_route_raises() -> None:
    state = SessionState()
    with pytest.raises(ProtocolError):
        state.on_app_open()
