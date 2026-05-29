"""End-to-end tests for :mod:`smart_place_client.client` in replay mode.

DESIGN §5.1: both `sp-cli --replay` and these tests drive the same
``SmartPlaceClient.replay(...)`` code path, so the dispatch is
exercised end-to-end without any network or mock server.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest

from smart_place_client import (
    CapturedFrame,
    GlobalConfig,
    GoToLinkSSL,
    NamedFields,
    NamedValue,
    ProtocolError,
    ServerFrame,
    SessionPhase,
    SmartPlaceAuthError,
    SmartPlaceClient,
    SmartPlaceOfflineError,
    UnknownFrame,
    install_token_redaction_filter,
)
from smart_place_client.client import _LOGGER, ExponentialBackoff, _interpret_discovery_frame, _scrub_token
from smart_place_client.protocol import HostNotOnline

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------- replay end-to-end --------------------------


async def test_replay_walks_dispatch_to_ready_state() -> None:
    """One bootstrap fixture through the dispatch loop populates state."""
    collected: list[ServerFrame] = []

    async def collector(frame: ServerFrame) -> None:
        collected.append(frame)

    client = SmartPlaceClient.replay(
        path=FIXTURES / "bootstrap.ndjson",
        handlers=[collector],
    )
    async with client:
        await client.run()

    # Server-direction frames only — client-direction lines are skipped
    # so commands are never re-issued during replay. Typed classes use
    # their dataclass name; NamedFields/NamedValue use the registry .name.
    seen_labels = [f.name if isinstance(f, NamedFields | NamedValue) else type(f).__name__ for f in collected]
    assert seen_labels == [
        "GoToLinkSSL",
        "GlobalConfig",
        "MainMenuFinished",
        "LightState",
        "UnknownFrame",
    ]

    # State machine walked through the happy path.
    assert isinstance(collected[0], GoToLinkSSL)
    assert isinstance(client.state.route, GoToLinkSSL)
    assert client.state.global_config == GlobalConfig(
        language="de",
        standby="0",
        brightness="80",
        screensaver_mode="1",
        screensaver_start="23:00",
        screensaver_duration="30",
    )
    assert client.state.main_menu_loaded is True
    # The bootstrap event fires when MainMenuFinished arrives.
    assert client._bootstrap_done.is_set()


async def test_replay_send_is_logged_not_transmitted() -> None:
    """Per smart-place-observe-only: replay sends never hit the network."""
    client = SmartPlaceClient.replay(path=FIXTURES / "bootstrap.ndjson")
    async with client:
        await client.send("leuchte1:50")
        await client.send("GiveStatusListe")
        await client.run()
    sent_texts = [c.text for c in client.sent_log]
    assert sent_texts == ["leuchte1:50", "GiveStatusListe"]
    assert all(c.direction == "client" for c in client.sent_log)


async def test_replay_capture_round_trips(tmp_path: Path) -> None:
    """A capture pass over a replay should produce a re-replayable file."""
    out = tmp_path / "capture.ndjson"
    client = SmartPlaceClient.replay(path=FIXTURES / "bootstrap.ndjson", capture=out)
    async with client:
        await client.run()

    lines = [line for line in out.read_text(encoding="utf-8").splitlines() if line]
    # Capture sees server frames (replay only dispatches server-direction).
    parsed = [CapturedFrame.from_json(line) for line in lines]
    assert {c.direction for c in parsed} == {"server"}
    assert parsed[0].text == "GoToLinkSSL:spr1.smartplace.ch:8770/Start1:Leer"
    # And the round-tripped file is itself replayable.
    second = SmartPlaceClient.replay(path=out)
    collected: list[ServerFrame] = []

    async def collector(frame: ServerFrame) -> None:
        collected.append(frame)

    second.subscribe(collector)
    async with second:
        await second.run()
    assert isinstance(collected[0], GoToLinkSSL)


async def test_handler_exception_does_not_crash_dispatch(caplog: pytest.LogCaptureFixture) -> None:
    """A misbehaving handler logs and the dispatch loop keeps going."""

    async def bad(_: ServerFrame) -> None:
        raise RuntimeError("boom")

    seen: list[ServerFrame] = []

    async def good(frame: ServerFrame) -> None:
        seen.append(frame)

    client = SmartPlaceClient.replay(
        path=FIXTURES / "bootstrap.ndjson",
        handlers=[bad, good],
    )
    with caplog.at_level(logging.ERROR, logger="smart_place_client"):
        async with client:
            await client.run()

    assert len(seen) == 5
    assert any("frame handler raised" in rec.message for rec in caplog.records)


async def test_live_logs_unknown_frame_to_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each unknown frame seen in live mode is appended to the unknown-log file."""

    async def _iter(items: list[str]):
        for item in items:
            yield item

    out = tmp_path / "unknown.ndjson"
    client = SmartPlaceClient.live(token="dummy", unknown_log=out)

    async def fake_once(self_: SmartPlaceClient) -> None:
        await self_._dispatch_loop(
            _iter(
                [
                    "GoToLinkSSL:h:8770/Start1:Leer",
                    "ZZZ_UNKNOWN_PUSH:hello",
                    "MOLNICA:1.7",
                    "ZZZ_UNKNOWN_PUSH:world",
                ],
            ),
        )
        self_._closing = True

    async def fake_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("smart_place_client.client.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(SmartPlaceClient, "_run_live_once", fake_once)
    await client.run()

    lines = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line]
    assert [entry["raw"] for entry in lines] == [
        "ZZZ_UNKNOWN_PUSH:hello",
        "MOLNICA:1.7",
        "ZZZ_UNKNOWN_PUSH:world",
    ]
    # Every entry has both a numeric and an ISO timestamp.
    for entry in lines:
        assert isinstance(entry["ts"], float)
        assert entry["iso_ts"].startswith("20")


async def test_live_chases_chart_ids_after_status_content_finished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the StatusEntry burst, fetch each referenced chart's stands.

    Mimics the SPA flow: StatusEntry rows embed CHART<id>STAND<series>
    references; once the StatusContentFinished marker arrives, one
    GiveMeChartStandsManuell<id> per unique chart-id is issued so the
    server emits CHART<id>STAND<n>:<value> push frames. The chase also
    populates ``state.chart_units`` from the embedded ``unit-...`` tag
    so the HA layer knows which sensor device class to use.
    """

    async def _iter(items: list[str]):
        for item in items:
            yield item

    client = SmartPlaceClient.live(token="dummy", unknown_log=None)

    sent: list[str] = []

    class _FakeWS:
        closed = False

        async def send_str(self, text: str) -> None:
            sent.append(text)

    client._ws = _FakeWS()  # type: ignore[assignment]

    async def fake_once(self_: SmartPlaceClient) -> None:
        await self_._dispatch_loop(
            _iter(
                [
                    "GoToLinkSSL:h:8770/Start1:Leer",
                    "StatusInhaltListe_2_1_SPtext>CHART337STAND1~SPDB-CHARTSSTANDS>unit-l~>LinkOff",
                    "StatusInhaltListe_2_2_SPtext>CHART49STAND1~SPDB-CHARTSSTANDS>unit-KWh~>LinkOff",
                    "StatusInhaltListe_2_3_SPtext>CHART337STAND2~SPDB-CHARTSSTANDS>unit-l~>LinkOff",
                    "StatusInhaltFinishedListe",
                ],
            ),
        )
        self_._closing = True

    async def fake_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("smart_place_client.client.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(SmartPlaceClient, "_run_live_once", fake_once)
    await client.run()

    # Two unique chart ids (49, 337), sent in sorted order; STAND2 in the
    # third row is the same chart 337 — deduped via the set.
    assert sent == ["GiveMeChartStandsManuell49", "GiveMeChartStandsManuell337"]
    # The chase also captures unit hints for the HA sensor mapping.
    assert client.state.chart_ids == {49, 337}
    assert client.state.chart_units == {49: "KWh", 337: "l"}


async def test_poll_charts_refires_known_chart_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_poll_charts`` periodically re-sends ``GiveMeChartStandsManuell<id>``."""

    sent: list[str] = []

    class _FakeWS:
        closed = False

        async def send_str(self, text: str) -> None:
            sent.append(text)

    client = SmartPlaceClient.live(token="dummy", chart_poll_interval=1.0)
    client._ws = _FakeWS()  # type: ignore[assignment]
    client.state.chart_ids = {49, 337}

    sleep_count = 0

    async def fake_sleep(_: float) -> None:
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count >= 2:
            # Stop the loop after two ticks so the test terminates.
            client._closing = True

    monkeypatch.setattr("smart_place_client.client.asyncio.sleep", fake_sleep)
    await client._poll_charts()

    # First tick fires both charts; second tick sets _closing before sending.
    assert sent == ["GiveMeChartStandsManuell49", "GiveMeChartStandsManuell337"]


async def test_poll_charts_disabled_when_interval_non_positive() -> None:
    """An interval of 0 disables polling — the coroutine returns immediately."""
    client = SmartPlaceClient.live(token="dummy", chart_poll_interval=0.0)
    # Should return without ever awaiting sleep — runs to completion in one tick.
    await client._poll_charts()


async def test_live_unknown_log_disabled_by_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """unknown_log=None disables the log even in live mode."""

    async def _iter(items: list[str]):
        for item in items:
            yield item

    sentinel = tmp_path / "should-not-exist.ndjson"
    client = SmartPlaceClient.live(token="dummy", unknown_log=None)

    async def fake_once(self_: SmartPlaceClient) -> None:
        await self_._dispatch_loop(_iter(["ZZZ_UNKNOWN_PUSH:hello"]))
        self_._closing = True

    async def fake_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("smart_place_client.client.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(SmartPlaceClient, "_run_live_once", fake_once)
    await client.run()

    assert not sentinel.exists()
    assert client._unknown_handle is None


async def test_replay_mode_does_not_write_unknown_log(tmp_path: Path) -> None:
    """Replay-mode unknowns are not logged — they're already in the fixture."""
    out = tmp_path / "unknown.ndjson"
    client = SmartPlaceClient.replay(path=FIXTURES / "bootstrap.ndjson")
    # bootstrap.ndjson contains a `MysteryFrameXYZ:` UnknownFrame; replay
    # mode should still skip writing the unknown log because the user
    # already had this frame in the fixture and re-logging would just
    # duplicate work.
    client.unknown_log_path = out
    async with client:
        await client.run()
    assert not out.exists()


async def test_phase3_capture_replays_to_bootstrapped_state() -> None:
    """The committed Phase 3 fixture from a real session replays cleanly.

    Validates the parser against actually-observed wire data: dynamic
    routed port (38435), float brightness (0.8), 'undefined' literal,
    and the MainMenuFinished bootstrap signal.
    """
    client = SmartPlaceClient.replay(path=FIXTURES / "phase3-smoke.ndjson")
    async with client:
        await client.run()
    assert client.state.route is not None
    assert client.state.route.port == 38435
    assert client.state.route.path == "/Start1"
    assert client.state.global_config is not None
    assert client.state.global_config.brightness == "0.8"
    assert client.state.global_config.screensaver_duration == "undefined"
    assert client.state.main_menu_loaded is True


async def test_replay_skips_malformed_lines(tmp_path: Path) -> None:
    """Malformed ndjson lines are skipped with a warning."""
    fixture = tmp_path / "bad.ndjson"
    fixture.write_text(
        "\n".join(
            [
                "not-json-at-all",
                json.dumps({"direction": "server", "ts": 1.0, "text": "EINSTELLUNGENGLOBAL>de>0>80>1>23:00>30"}),
                "",
            ],
        )
        + "\n",
        encoding="utf-8",
    )
    seen: list[ServerFrame] = []

    async def collect(frame: ServerFrame) -> None:
        seen.append(frame)

    client = SmartPlaceClient.replay(path=fixture, handlers=[collect])
    async with client:
        await client.run()

    assert [type(f).__name__ for f in seen] == ["GlobalConfig"]


async def test_replay_realtime_paces_between_server_frames(tmp_path: Path) -> None:
    """realtime=True respects wall-clock gaps from the fixture."""
    fixture = tmp_path / "gap.ndjson"
    fixture.write_text(
        "\n".join(
            [
                json.dumps({"direction": "server", "ts": 100.0, "text": "HostNotOnline"}),
                json.dumps({"direction": "server", "ts": 100.05, "text": "HostNotOnline"}),
            ],
        )
        + "\n",
        encoding="utf-8",
    )
    client = SmartPlaceClient.replay(path=fixture, realtime=True)
    loop = asyncio.get_running_loop()
    started = loop.time()
    async with client:
        await client.run()
    elapsed = loop.time() - started
    assert elapsed >= 0.04


# ------------------ discovery-frame classification --------------


def test_interpret_discovery_frame_offline_raises_typed_error() -> None:
    """``HostNotOnline`` must trip ``SmartPlaceOfflineError`` (not generic ``ProtocolError``).

    Regression test for an ordering bug: an earlier draft applied the
    state-side discovery check first, which raised ``ProtocolError``
    on anything non-``GoToLinkSSL`` — including ``HostNotOnline`` —
    so the reconnect-loop / config-flow paths that catch
    ``SmartPlaceOfflineError`` never fired.
    """
    with pytest.raises(SmartPlaceOfflineError):
        _interpret_discovery_frame(HostNotOnline())


def test_interpret_discovery_frame_passes_route_through() -> None:
    """The happy path returns the same ``GoToLinkSSL`` instance for downstream use."""
    route = GoToLinkSSL(host="h", port=1, path=None, token2="t")
    assert _interpret_discovery_frame(route) is route


def test_interpret_discovery_frame_unexpected_raises_protocol_error() -> None:
    """Anything other than ``HostNotOnline`` / ``GoToLinkSSL`` is a protocol violation."""
    with pytest.raises(ProtocolError):
        _interpret_discovery_frame(UnknownFrame(raw="xyz"))


# --------------------- token redaction --------------------------


def test_log_filter_scrubs_token_query() -> None:
    install_token_redaction_filter()
    record = _LOGGER.makeRecord(
        name="smart_place_client",
        level=logging.INFO,
        fn="x.py",
        lno=1,
        msg="opening wss://spr1.smartplace.ch:8770/StartAppExt/?TOKEN=secrettoken123",
        args=(),
        exc_info=None,
    )
    for filt in _LOGGER.filters:
        assert filt.filter(record) is True
    assert "secrettoken123" not in record.getMessage()
    assert "<REDACTED>" in record.getMessage()


def test_log_filter_scrubs_url_start_query() -> None:
    install_token_redaction_filter()
    record = _LOGGER.makeRecord(
        name="smart_place_client",
        level=logging.INFO,
        fn="x.py",
        lno=1,
        msg="bootstrap: https://spr1.smartplace.ch:8770/Start5?supersecretURLtoken",
        args=(),
        exc_info=None,
    )
    for filt in _LOGGER.filters:
        assert filt.filter(record) is True
    assert "supersecretURLtoken" not in record.getMessage()


def test_log_filter_scrubs_url_infoboard_query() -> None:
    """``/Infoboard<N>?<token2>`` (the routed-iframe URL) must be redacted too."""
    install_token_redaction_filter()
    record = _LOGGER.makeRecord(
        name="smart_place_client",
        level=logging.DEBUG,
        fn="x.py",
        lno=1,
        msg="routed page https://h:38435/Infoboard1?routedtoken99 -> 200",
        args=(),
        exc_info=None,
    )
    for filt in _LOGGER.filters:
        assert filt.filter(record) is True
    assert "routedtoken99" not in record.getMessage()
    assert "<REDACTED>" in record.getMessage()


def test_scrub_token_helper_idempotent() -> None:
    raw = "GET /Start5?abcdef and TOKEN=xyz123"
    once = _scrub_token(raw)
    twice = _scrub_token(once)
    assert once == twice
    assert "abcdef" not in once
    assert "xyz123" not in once


# ------------------ CapturedFrame round-trip --------------------


def test_captured_frame_json_round_trip() -> None:
    frame = CapturedFrame(direction="server", ts=1.5, text="hello>world")
    decoded = CapturedFrame.from_json(frame.to_json())
    assert decoded == frame


def test_captured_frame_rejects_bad_direction() -> None:
    bad = json.dumps({"direction": "neither", "ts": 0.0, "text": "x"})
    with pytest.raises(ValueError, match="direction"):
        CapturedFrame.from_json(bad)


# ----------------------- factory-level checks -------------------


def test_replay_sources_independent_per_client() -> None:
    """Two replay clients don't share `sent_log` state."""
    c1 = SmartPlaceClient.replay(path=FIXTURES / "bootstrap.ndjson")
    c2 = SmartPlaceClient.replay(path=FIXTURES / "bootstrap.ndjson")
    assert c1.sent_log is not c2.sent_log


def test_live_constructor_does_not_open_network() -> None:
    """`live(...)` is callable outside any event loop; no session created yet."""
    client = SmartPlaceClient.live(token="dummy-token")
    assert client._live is not None
    assert client._live.token == "dummy-token"
    # Session and WS are both deferred until run().
    assert client._live.session is None
    assert client._ws is None
    assert client.state.phase is SessionPhase.DISCOVERY_OPEN


def test_exponential_backoff_basic_progression() -> None:
    """Without jitter, the sequence is base^n until cap."""
    backoff = ExponentialBackoff(base=2.0, cap=10.0, jitter=0.0, initial=1.0)
    delays = [backoff.next() for _ in range(6)]
    assert delays == [1.0, 2.0, 4.0, 8.0, 10.0, 10.0]


def test_exponential_backoff_reset_returns_to_initial() -> None:
    backoff = ExponentialBackoff(base=2.0, cap=60.0, jitter=0.0, initial=1.0)
    backoff.next()
    backoff.next()
    assert backoff.peek() > 1.0
    backoff.reset()
    assert backoff.peek() == 1.0


def test_exponential_backoff_jitter_within_range() -> None:
    backoff = ExponentialBackoff(base=2.0, cap=60.0, jitter=0.3, initial=1.0)
    # First delay is in [1.0, 1.3) with jitter 0.3.
    for _ in range(20):
        peek = backoff.peek()
        assert 1.0 <= peek < 1.3
    delay = backoff.next()
    assert 1.0 <= delay < 1.3


async def test_run_live_reconnects_after_transient_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-auth failure triggers backoff sleep, then a successful retry."""
    attempts: list[int] = []
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("smart_place_client.client.asyncio.sleep", fake_sleep)

    client = SmartPlaceClient.live(
        token="t",
        backoff=ExponentialBackoff(base=2.0, cap=10.0, jitter=0.0, initial=1.0),
    )

    async def fake_once(self_: SmartPlaceClient) -> None:
        attempts.append(len(attempts))
        if len(attempts) < 3:
            raise RuntimeError(f"transient failure {len(attempts)}")
        self_._closing = True

    monkeypatch.setattr(SmartPlaceClient, "_run_live_once", fake_once)
    await client.run()
    assert len(attempts) == 3
    assert sleeps == [1.0, 2.0]


async def test_run_live_auth_error_stops_and_calls_on_reauth(monkeypatch: pytest.MonkeyPatch) -> None:
    reauth_called: list[bool] = []

    async def on_reauth() -> None:
        reauth_called.append(True)

    async def fake_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("smart_place_client.client.asyncio.sleep", fake_sleep)

    client = SmartPlaceClient.live(token="t", on_reauth=on_reauth)

    async def fake_once(self_: SmartPlaceClient) -> None:
        raise SmartPlaceAuthError("token rejected")

    monkeypatch.setattr(SmartPlaceClient, "_run_live_once", fake_once)
    await client.run()
    assert reauth_called == [True]


async def test_run_live_auth_error_without_callback_returns_quietly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SmartPlaceClient.live(token="t")

    async def fake_once(_: SmartPlaceClient) -> None:
        raise SmartPlaceAuthError("nope")

    monkeypatch.setattr(SmartPlaceClient, "_run_live_once", fake_once)
    await client.run()
    assert client.on_reauth is None


async def test_run_live_cancelled_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    client = SmartPlaceClient.live(token="t")

    async def fake_once(_: SmartPlaceClient) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(SmartPlaceClient, "_run_live_once", fake_once)
    with pytest.raises(asyncio.CancelledError):
        await client.run()


async def test_run_live_exits_when_closing_after_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """If aclose() flips `_closing` mid-failure, we don't sleep again."""
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("smart_place_client.client.asyncio.sleep", fake_sleep)
    client = SmartPlaceClient.live(token="t")

    async def fake_once(self_: SmartPlaceClient) -> None:
        self_._closing = True
        raise RuntimeError("dropping while shutting down")

    monkeypatch.setattr(SmartPlaceClient, "_run_live_once", fake_once)
    await client.run()
    assert sleeps == []


def test_unknown_frame_dispatched_without_state_advance() -> None:
    """An unknown app frame doesn't break state — it just goes to handlers."""

    async def main() -> list[ServerFrame]:
        seen: list[ServerFrame] = []

        async def handler(frame: ServerFrame) -> None:
            seen.append(frame)

        client = SmartPlaceClient.replay(
            path=FIXTURES / "bootstrap.ndjson",
            handlers=[handler],
        )
        async with client:
            await client.run()
        return seen

    seen = asyncio.run(main())
    unknown = [f for f in seen if isinstance(f, UnknownFrame)]
    assert unknown == [UnknownFrame(raw="MysteryFrameXYZ:opaque")]
