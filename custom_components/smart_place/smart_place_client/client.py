"""Smart Place I/O client.

Owns all I/O for the Smart Place protocol — both the live WebSocket path
and the offline replay path. Pure parsing / state lives in
:mod:`smart_place_client.protocol`.

Both ``SmartPlaceClient.live(...)`` and ``SmartPlaceClient.replay(...)``
return the same class and use the same dispatch loop. The only thing
that differs is where incoming frames come from. Per the
``test-fixture-divergence-smell`` memory: if the live/replay branches
ever start growing extra parsing or state logic, that's a warning sign
that the replay path is no longer exercising production code, and the
fix is to move the difference back to the I/O boundary, not to add more
branches.

DESIGN.md §2, §5, §6 are the design references.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import logging
import os
from pathlib import Path
import random
import re
import sys
from types import TracebackType
from typing import Any, Final, Literal, Self

import aiohttp

from .commands import Commands
from .messages import parse_frame
from .protocol import (
    APP_WS_PATH,
    DISCOVERY_ORIGIN,
    GoToLinkSSL,
    HostNotOnline,
    NamedFields,
    NamedValue,
    ProtocolError,
    ServerFrame,
    SessionPhase,
    SessionState,
    SmartPlaceAuthError,
    SmartPlaceOfflineError,
    UnknownFrame,
    discovery_ws_url,
    encode_frame,
    parse_chart_references,
)

_LOGGER = logging.getLogger("smart_place_client")

# Per-step timeout for the discovery → routed-page → app-WS chain. A
# server that accepts the TCP connection but stalls without sending the
# first frame would otherwise hang the reconnect loop indefinitely
# (``aiohttp.WSMessage.receive()`` has no built-in deadline). 30 s is
# generous for the slowest legitimate handshake observed in captures
# (~1 s) but short enough that a stalled session recovers in under a
# minute.
DISCOVERY_STEP_TIMEOUT: Final[float] = 30.0

# Browser-like UA so the server treats us the same as the SPA.
# Source: matches what a current desktop browser would send to the SPA.
USER_AGENT: str = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
)

# Token-shaped strings the redacting filter scrubs.
# Smart Place tokens are typically opaque ~100-char URL-safe strings;
# we redact anything that follows ``TOKEN=`` or ``?<long-string>`` in
# URLs. The path alternation covers both the discovery / routed-page
# paths (``/Start<N>?``) and the post-route iframe (``/Infoboard<N>?``)
# — both carry token-bearing query strings (token2 in the latter).
_TOKEN_QUERY = re.compile(r"(TOKEN=)[^&\s\"']+", re.IGNORECASE)
_URL_TOKEN_TRAILING = re.compile(r"(/(?:Start|Infoboard)[0-9]?\?)[^\s\"']+")


def _interpret_discovery_frame(frame: ServerFrame) -> GoToLinkSSL:
    """Validate the first discovery WS reply and return the route, or raise.

    Split out of ``_run_live_once`` so the disposition order is unit-
    testable without mocking ``aiohttp.ClientSession``: the offline
    branch must fire *before* anything that only accepts ``GoToLinkSSL``
    (otherwise ``HostNotOnline`` would surface as a generic
    ``ProtocolError`` and the reconnect / config-flow code that
    catches ``SmartPlaceOfflineError`` would never see it).
    """
    if isinstance(frame, HostNotOnline):
        raise SmartPlaceOfflineError("Smart Place installation is offline")
    if not isinstance(frame, GoToLinkSSL):
        raise ProtocolError(
            f"discovery returned non-routing frame {type(frame).__name__}",
        )
    return frame


def install_token_redaction_filter(logger: logging.Logger = _LOGGER) -> None:
    """Install a log filter that scrubs URL tokens from emitted records.

    Safe to call multiple times; subsequent calls are no-ops.
    """
    for existing in logger.filters:
        if isinstance(existing, _TokenRedactionFilter):
            return
    logger.addFilter(_TokenRedactionFilter())


class _TokenRedactionFilter(logging.Filter):
    """Log filter that replaces token query strings with ``<REDACTED>``."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        scrubbed = _URL_TOKEN_TRAILING.sub(r"\1<REDACTED>", _TOKEN_QUERY.sub(r"\1<REDACTED>", msg))
        if scrubbed != msg:
            record.msg = scrubbed
            record.args = None
        return True


@dataclass(slots=True)
class CapturedFrame:
    """One ndjson line in a capture / replay fixture.

    Captures both directions so a developer can later inspect what was
    sent during a live session as well as what was received. The
    ``ts`` field is wall-clock UTC; replay uses it to optionally
    re-pace frames.
    """

    direction: Literal["server", "client"]
    ts: float
    text: str

    def to_json(self) -> str:
        """Return a single ndjson line (no trailing newline).

        Includes a human-readable ``iso_ts`` alongside the machine-readable
        unix ``ts`` so the file is scannable by eye. Replay ignores
        ``iso_ts``; only ``ts`` is used for re-pacing.
        """
        return json.dumps(
            {
                "direction": self.direction,
                "ts": self.ts,
                "iso_ts": datetime.fromtimestamp(self.ts, tz=UTC).isoformat(timespec="milliseconds"),
                "text": self.text,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, line: str) -> CapturedFrame:
        """Parse one ndjson line into a CapturedFrame."""
        payload = json.loads(line)
        direction = payload["direction"]
        if direction not in ("server", "client"):
            raise ValueError(f"capture line has bad direction {direction!r}")
        return cls(direction=direction, ts=float(payload["ts"]), text=str(payload["text"]))


FrameHandler = Callable[[ServerFrame], Awaitable[None]]
ReauthCallback = Callable[[], Awaitable[None]]
DisconnectCallback = Callable[[], Awaitable[None]]


@dataclass(slots=True)
class ExponentialBackoff:
    """Exponential backoff with full-additive jitter.

    Sequence (no jitter): 1, 2, 4, 8, 16, 32, 60, 60, ... seconds.
    With jitter=0.3 each delay is multiplied by a random factor in
    [1.0, 1.3) to spread retries out and avoid thundering herds when
    many clients reconnect after a server hiccup.

    DESIGN.md §6.2 specifies base=2.0, cap=60.0, jitter=0.3.
    """

    base: float = 2.0
    cap: float = 60.0
    jitter: float = 0.3
    initial: float = 1.0
    _next: float = 0.0

    def __post_init__(self) -> None:
        """Seed the next-delay state from ``initial``."""
        self._next = self.initial

    def reset(self) -> None:
        """Restart the schedule at ``initial`` — call after a clean run."""
        self._next = self.initial

    def peek(self) -> float:
        """Return the delay :meth:`next` would yield, without advancing."""
        return self._apply_jitter(self._next)

    def next(self) -> float:
        """Return the next delay seconds and advance the schedule."""
        delay = self._apply_jitter(self._next)
        self._next = min(self._next * self.base, self.cap)
        return delay

    def _apply_jitter(self, value: float) -> float:
        if self.jitter <= 0:
            return value
        return value * (1.0 + random.uniform(0.0, self.jitter))


@dataclass(slots=True)
class _LiveSources:
    """Resources held only by a live-mode client.

    ``session`` is None until the first call to :meth:`SmartPlaceClient.run`
    so callers can construct a live client outside of any event loop —
    aiohttp >=3.10 requires a running loop to construct a ClientSession.
    """

    token: str
    session: aiohttp.ClientSession | None
    own_session: bool
    heartbeat: float
    chart_poll_interval: float
    ping_interval: float
    ping_timeout: float


@dataclass(slots=True)
class _ReplaySources:
    """Resources held only by a replay-mode client."""

    path: Path
    realtime: bool
    sent_log: list[CapturedFrame] = field(default_factory=list)


@dataclass(slots=True)
class SmartPlaceClient:
    """Smart Place WebSocket client (live or replay).

    Instantiate via the classmethod constructors :meth:`live` or
    :meth:`replay`. Both return the same class; the dispatch loop is
    identical. Behavioural differences (where frames come from, where
    sends go) live entirely behind the ``_live`` / ``_replay``
    attributes.

    DESIGN.md §2 architecture and §6.2 connection lifecycle.
    """

    state: SessionState
    handlers: list[FrameHandler]
    capture_path: Path | None
    unknown_log_path: Path | None = None
    on_reauth: ReauthCallback | None = None
    on_disconnect: DisconnectCallback | None = None
    backoff: ExponentialBackoff = field(default_factory=ExponentialBackoff)
    _capture_handle: object | None = None
    _unknown_handle: object | None = None
    _ws: aiohttp.ClientWebSocketResponse | None = None
    _live: _LiveSources | None = None
    _replay: _ReplaySources | None = None
    _closing: bool = False
    _bootstrap_done: asyncio.Event = field(default_factory=asyncio.Event)
    _chart_poll_task: asyncio.Task[None] | None = None
    _heartbeat_task: asyncio.Task[None] | None = None
    _last_pong: float = 0.0

    # ------------------------- constructors -------------------------

    @classmethod
    def live(
        cls,
        token: str,
        *,
        session: aiohttp.ClientSession | None = None,
        capture: Path | str | None = None,
        unknown_log: Path | str | None = None,
        heartbeat: float = 30.0,
        chart_poll_interval: float = 60.0,
        ping_interval: float = 60.0,
        ping_timeout: float = 30.0,
        handlers: list[FrameHandler] | None = None,
        on_reauth: ReauthCallback | None = None,
        on_disconnect: DisconnectCallback | None = None,
        backoff: ExponentialBackoff | None = None,
    ) -> Self:
        """Build a live client that connects to the real smartplace.ch.

        Args:
            token: opaque URL token (the only secret). Never logged.
            session: optional pre-existing aiohttp session (HA passes
                its own pooled session; the CLI lets us create one).
            capture: optional path to tee every frame (both directions)
                as ndjson — tokens are redacted on the way in.
            unknown_log: optional path to record every frame that the
                parser doesn't recognise. Each occurrence is appended
                (not deduped) so we can compare instances of the same
                shape later. Tokens redacted defensively.
            heartbeat: aiohttp WS ping interval, in seconds.
            chart_poll_interval: how often (seconds) to refresh
                consumption charts via ``GiveMeChartStandsManuell<id>``.
                Chart values don't push; this loop keeps them current.
                Set <=0 to disable polling.
            ping_interval: how often (seconds) to send the
                application-level ``Ping`` heartbeat. Mirrors the
                SPA's ``StartWebsocketTestMain`` cadence (60s). Set
                <=0 to disable the heartbeat (aiohttp's WS-level
                ping still runs via ``heartbeat`` above).
            ping_timeout: deadline (seconds) for a ``PongOK`` reply
                after the last sent ``Ping``. If the deadline lapses
                the WS is closed so the reconnect loop fires.
            handlers: optional initial frame handlers.
            on_reauth: coroutine invoked once if the token is rejected;
                HA wires this to its reauth flow. After it returns,
                :meth:`run` exits (no further reconnect attempts).
            on_disconnect: coroutine invoked after every WS lifetime
                ends (drop, error, or graceful close — before the
                reconnect-backoff sleep). HA wires this to fan
                listeners out so entity ``available`` flips False
                during the gap, instead of showing stale values.
            backoff: optional reconnect schedule override (mainly for
                tests).
        """
        install_token_redaction_filter()
        live = _LiveSources(
            token=token,
            session=session,
            own_session=session is None,
            heartbeat=heartbeat,
            chart_poll_interval=chart_poll_interval,
            ping_interval=ping_interval,
            ping_timeout=ping_timeout,
        )
        return cls(
            state=SessionState(),
            handlers=list(handlers or []),
            unknown_log_path=Path(unknown_log) if unknown_log else None,
            capture_path=Path(capture) if capture else None,
            on_reauth=on_reauth,
            on_disconnect=on_disconnect,
            backoff=backoff or ExponentialBackoff(),
            _live=live,
        )

    @classmethod
    def replay(
        cls,
        path: Path | str,
        *,
        capture: Path | str | None = None,
        realtime: bool = False,
        handlers: list[FrameHandler] | None = None,
    ) -> Self:
        """Build a replay client that iterates an ndjson capture.

        Args:
            path: path to a capture fixture (``CapturedFrame`` per line).
            capture: optional path to re-tee the replayed stream into a
                new file (useful when transforming fixtures).
            realtime: if True, sleep between server frames so wall-clock
                gaps from the original capture are preserved. Default
                False — tests run as fast as possible.
            handlers: optional initial frame handlers.
        """
        install_token_redaction_filter()
        return cls(
            state=SessionState(),
            handlers=list(handlers or []),
            capture_path=Path(capture) if capture else None,
            _replay=_ReplaySources(path=Path(path), realtime=realtime),
        )

    # --------------------------- public API --------------------------

    def subscribe(self, handler: FrameHandler) -> None:
        """Register a coroutine handler invoked on every server frame."""
        self.handlers.append(handler)

    async def run(self) -> None:
        """Run until the client is closed or an unrecoverable error.

        - Replay mode runs the fixture to completion exactly once.
        - Live mode wraps the per-connection lifetime in a reconnect
          loop with exponential backoff + jitter (DESIGN §6.2). The
          loop exits on ``SmartPlaceAuthError`` (after invoking
          ``on_reauth``), on ``CancelledError`` (re-raised), or when
          :meth:`aclose` flips ``_closing``.
        """
        try:
            if self._live is not None:
                await self._run_live_with_reconnect()
            elif self._replay is not None:
                await self._run_replay()
            else:
                raise RuntimeError("SmartPlaceClient missing both live and replay sources")
        finally:
            if self.capture_path is not None and self._capture_handle is not None:
                self._capture_handle.close()  # type: ignore[attr-defined]
                self._capture_handle = None
            if self._unknown_handle is not None:
                self._unknown_handle.close()  # type: ignore[attr-defined]
                self._unknown_handle = None

    async def _run_live_with_reconnect(self) -> None:
        """Live-mode outer loop: reconnect on errors, stop on auth/cancel."""
        while not self._closing:
            try:
                await self._run_live_once()
                self.backoff.reset()
            except asyncio.CancelledError:
                raise
            except SmartPlaceAuthError as err:
                _LOGGER.error("auth error, stopping reconnect: %s", err)
                if self.on_reauth is not None:
                    try:
                        await self.on_reauth()
                    except Exception:
                        _LOGGER.exception("on_reauth callback raised")
                return
            except SmartPlaceOfflineError:
                # Installation offline — still transient; retry. Logged
                # separately so the user sees the actual cause rather
                # than a generic ``WS dropped``.
                delay = self.backoff.next()
                _LOGGER.info("Smart Place installation offline; retrying in %.1fs", delay)
                if self._closing:
                    return
                await asyncio.sleep(delay)
            except Exception as err:  # noqa: BLE001
                delay = self.backoff.next()
                _LOGGER.warning("WS dropped: %s; retrying in %.1fs", err, delay)
                if self._closing:
                    return
                await asyncio.sleep(delay)

    async def send(self, text: str) -> None:
        """Send a text frame to the server (live) or log it (replay).

        Per the ``smart-place-observe-only`` memory: live mode has no
        software gate. If you don't want to issue commands, don't call
        ``send()``. Replay mode appends to an in-memory log instead of
        contacting the network.
        """
        frame = encode_frame(text)
        captured = CapturedFrame(
            direction="client",
            ts=_now_ts(),
            text=frame,
        )
        self._write_capture(captured)
        if self._live is not None:
            if self._ws is None or self._ws.closed:
                raise RuntimeError("send() called before the app WS is open")
            await self._ws.send_str(frame)
            _LOGGER.debug("sent: %s", frame)
        elif self._replay is not None:
            self._replay.sent_log.append(captured)
            _LOGGER.debug("replay: send recorded (not transmitted): %s", frame)
        else:
            raise RuntimeError("SmartPlaceClient has no I/O source")

    async def aclose(self) -> None:
        """Close the WS and any owned aiohttp session. Idempotent."""
        self._closing = True
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        if self._live is not None and self._live.own_session and self._live.session is not None:
            await self._live.session.close()
        self.state.close()

    async def __aenter__(self) -> Self:
        """Enter the async context — returns self for `async with` syntax."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Exit the async context — always calls :meth:`aclose`."""
        await self.aclose()

    @property
    def sent_log(self) -> list[CapturedFrame]:
        """In-memory log of sends — populated only in replay mode."""
        return self._replay.sent_log if self._replay else []

    @property
    def connected(self) -> bool:
        """True iff an app/discovery WS is currently open.

        ``SessionState.phase`` alone is not enough to gate entity
        availability: when the WS drops, ``_run_live_once`` clears
        ``self._ws`` but the phase stays at ``READY`` / ``BOOTSTRAPPED``
        until the next handshake's ``on_app_open`` fires, which can
        be many backoff seconds away. Pair this with the phase check
        in the HA layer so entities flip ``available=False`` for the
        whole gap, not just up to the disconnect.
        """
        return self._ws is not None and not self._ws.closed

    async def wait_for_bootstrap(self) -> None:
        """Wait until both bootstrap reads have completed.

        Wrap with ``async with asyncio.timeout(seconds): ...`` at the
        caller if you want a deadline.
        """
        await self._bootstrap_done.wait()

    async def connect_and_bootstrap(self) -> None:
        """One-shot discovery → app WS → bootstrap, with no retries.

        For config-flow validation. ``run()`` swallows
        :class:`SmartPlaceAuthError` (to trigger the HA reauth flow
        and exit) and retries :class:`SmartPlaceOfflineError` (so the
        background task survives transient outages) — both behaviours
        leave the bootstrap event un-set, so a caller awaiting
        ``wait_for_bootstrap`` only learns about the failure when its
        own timeout trips and surfaces it as ``cannot_connect``.

        This method runs exactly one ``_run_live_once`` attempt,
        races it against the bootstrap event, and propagates the
        original exception so the caller can map it to the right
        config-flow error key.
        """
        runner = asyncio.create_task(self._run_live_once(), name="smart_place_validate")
        bootstrap_wait = asyncio.create_task(
            self._bootstrap_done.wait(),
            name="smart_place_validate_wait",
        )
        try:
            done, pending = await asyncio.wait(
                {runner, bootstrap_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            if bootstrap_wait in done:
                return  # success — caller closes the client
            # Runner exited first — re-raise whatever it threw, or
            # surface the unexpected clean exit as a protocol error.
            exc = runner.exception()
            if exc is not None:
                raise exc
            raise ProtocolError("connection closed before bootstrap completed")
        finally:
            if not runner.done():
                runner.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await runner

    # ----------------------- live-mode internals ----------------------

    async def _run_live_once(self) -> None:
        assert self._live is not None
        if self._live.session is None:
            self._live.session = aiohttp.ClientSession()
        session: aiohttp.ClientSession = self._live.session

        ws_url = discovery_ws_url(self._live.token)
        _LOGGER.info("opening discovery WS: %s", ws_url)
        discovery_headers = {"User-Agent": USER_AGENT, "Origin": DISCOVERY_ORIGIN}

        # Each step gets its own deadline so a stalled server (TCP
        # accepted but no application frame) doesn't hang the reconnect
        # loop.
        async with (
            asyncio.timeout(DISCOVERY_STEP_TIMEOUT),
            session.ws_connect(
                ws_url,
                headers=discovery_headers,
                heartbeat=self._live.heartbeat,
            ) as discovery_ws,
        ):
            self._ws = discovery_ws
            first_msg = await discovery_ws.receive()
            if first_msg.type is not aiohttp.WSMsgType.TEXT:
                raise ProtocolError(
                    f"discovery WS first frame had unexpected type {first_msg.type!r}",
                )
            text = str(first_msg.data)
            self._write_capture(CapturedFrame("server", _now_ts(), text))
            frame = parse_frame(text)

            # Classify FIRST — ``on_discovery_frame`` only accepts
            # ``GoToLinkSSL`` and raises ``ProtocolError`` otherwise,
            # which would mask the typed ``SmartPlaceOfflineError``
            # the reconnect / config-flow paths catch.
            route = _interpret_discovery_frame(frame)
            self.state.on_discovery_frame(route)

        self._ws = None
        _LOGGER.info("discovery routed to %s:%s%s", route.host, route.port, route.path or "")

        # Step 3: fetch routed page as the browser iframe would.
        routed_url = route.routed_https_url
        async with (
            asyncio.timeout(DISCOVERY_STEP_TIMEOUT),
            session.get(
                routed_url,
                headers={"User-Agent": USER_AGENT, "Referer": DISCOVERY_ORIGIN},
            ) as resp,
        ):
            if resp.status != 200:
                raise ProtocolError(
                    f"routed page GET returned {resp.status} for {routed_url}",
                )
            await resp.read()  # drain; we don't parse the page body in v1
            _LOGGER.debug("routed page %s -> %d", routed_url, resp.status)

        # Step 4: open the app WS at /UpdatenLS.
        app_ws_url = route.app_ws_url
        app_headers = {"User-Agent": USER_AGENT, "Origin": route.app_ws_origin}
        # Deadline only the handshake — the dispatch loop body below
        # runs for the life of the connection and must not be wrapped.
        async with asyncio.timeout(DISCOVERY_STEP_TIMEOUT):
            app_ws = await session.ws_connect(
                app_ws_url,
                headers=app_headers,
                heartbeat=self._live.heartbeat,
            )
        try:
            self._ws = app_ws
            self.state.on_app_open()
            _LOGGER.info("app WS open at %s%s", route.host, APP_WS_PATH)

            # Bootstrap reads (DESIGN §10):
            #   1. Commands.StatusContent -> StatusEntry rows +
            #      chart-id hints, terminated by
            #      StatusContentFinished. The chart-chase below
            #      then issues Commands.ChartStands(<id>) per chart
            #      id referenced in the rows.
            #   2. Commands.Mainmenu -> Big config + state dump:
            #      ChartDefinition (chart labels/categories/units),
            #      ClimateConfig (room names — used to label TEMPIST
            #      sensors), LightConfig/BlindConfig/etc., plus the
            #      initial state snapshot (ChartStand STAND1 readings,
            #      group switch states, volumes, scenes). Terminated
            #      by MainMenuFinished — used as the bootstrap-done
            #      signal because by then every config frame an HA
            #      entity might need has arrived.
            #   3. Commands.SocketConnected -> Tells the server we're
            #      ready for the broadcast stream. Server then pushes
            #      the full state snapshot: PACKETBOX<N>, REGEN, HAGEL,
            #      TEMPOUT, WINDGESCHWINDIGKEIT, WINDALARM<N>,
            #      JALWARTUNG, LEUCHTENZENTRAL<N>, INFOBOARD<N> /
            #      INFOBOARD<N>INHALT, Vol<N>, PERSINFO, MUTE,
            #      KLIMASINFO<N>, SPRECHEN/CALLINFO, SceneState etc.
            #      Without this command the server stays quiet on
            #      those broadcast frames (verified 2026-05-29 via
            #      HAR audit + live probe).
            # TEMPIST<N> (indoor temps) does push spontaneously
            # independent of SocketConnected.
            # GiveStatusListe is sent first by the SPA but the
            # server doesn't require it — verified 2026-05-29.
            # GiveMeGlobalConfig is sent first by the SPA but the
            # server doesn't require it — verified 2026-05-28.
            await self.send(Commands.StatusContent.encode())
            await self.send(Commands.Mainmenu.encode())
            await self.send(Commands.SocketConnected.encode())

            self._chart_poll_task = asyncio.create_task(
                self._poll_charts(),
                name="smart_place_chart_poll",
            )
            # Seed the heartbeat clock so the first deadline doesn't fire
            # immediately on a freshly-opened socket — we treat connection
            # open as a fresh liveness signal.
            loop = asyncio.get_running_loop()
            self._last_pong = loop.time()
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat(),
                name="smart_place_heartbeat",
            )
            try:
                # Dispatch loop.
                await self._dispatch_loop(_aiter_ws_text(app_ws))
            finally:
                for task_attr in ("_chart_poll_task", "_heartbeat_task"):
                    task: asyncio.Task[None] | None = getattr(self, task_attr)
                    if task is not None:
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await task
                        setattr(self, task_attr, None)
        finally:
            # Replaces the auto-cleanup the original ``async with`` did.
            # Suppressed because we don't care about close-time errors
            # during reconnect — the loop will reopen a fresh WS.
            with contextlib.suppress(Exception):
                await app_ws.close()
            self._ws = None
            # Fan the disconnect out so HA flips entity ``available``
            # immediately — without this, listeners only wake on the
            # next frame, which doesn't arrive until reconnect.
            if self.on_disconnect is not None:
                try:
                    await self.on_disconnect()
                except Exception:
                    _LOGGER.exception("on_disconnect callback raised")

    # ---------------------- replay-mode internals ---------------------

    async def _run_replay(self) -> None:
        assert self._replay is not None
        _LOGGER.info("replaying %s", self._replay.path)
        # Synthesise the discovery → routed → app-open phase from the
        # fixture: the first server frame is parsed as discovery, then
        # any embedded "GoToLinkSSL" route becomes the in-memory state.
        # This is the SAME dispatch the live path uses, just with a
        # frame iterator instead of an aiohttp WS.
        await self._dispatch_loop(_aiter_capture(self._replay))

    # ------------------------ shared dispatch -------------------------

    async def _chase_chart_ids(self, frame: ServerFrame) -> None:
        """SPA-mirroring chart-fetch chase.

        Two discovery paths:

        - ``StatusEntry`` frames embed ``CHART<id>STAND<series>~
          SPDB-CHARTSSTANDS>unit-<unit>`` references — collected from
          the ``Commands.StatusContent`` reply.
        - ``ChartDefinition`` frames (one per chart in the
          installation) arrive during the ``Commands.Mainmenu``
          response — these surface charts the status list doesn't
          reference (e.g. per-meter sub-charts when only the SUMME is
          on the panel) and carry the authoritative unit.

        Both feed :attr:`SessionState.chart_ids` /
        ``chart_units``; after either ``StatusContentFinished`` or
        ``MainMenuFinished``, one ``GiveMeChartStandsManuell<id>`` is
        sent per known chart so the full STAND fan-out arrives. Live
        mode only — replay has no live WS to send on.
        """
        if self._live is None or self._ws is None:
            return
        if isinstance(frame, NamedValue) and frame.name == "StatusEntry":
            for chart_id, _series, unit in parse_chart_references(frame.value):
                self.state.chart_ids.add(chart_id)
                self.state.chart_units[chart_id] = unit
        elif isinstance(frame, NamedValue) and frame.name == "ChartDefinition" and frame.index is not None:
            self.state.chart_ids.add(frame.index)
            fields = frame.value.split(";")
            if len(fields) > 14 and fields[14]:
                self.state.chart_units[frame.index] = fields[14]
        elif isinstance(frame, NamedFields) and frame.name in ("StatusContentFinished", "MainMenuFinished"):
            for cid in sorted(self.state.chart_ids):
                await self.send(Commands.ChartStands.encode(cid))

    async def _heartbeat(self) -> None:
        """Application-level Ping/PongOK liveness probe.

        Sends ``Ping`` every ``ping_interval`` seconds; if no
        ``PongOK`` has arrived within ``ping_timeout`` seconds since
        the last one (or since the connection opened), closes the
        WebSocket so the reconnect loop in
        :meth:`_run_live_with_reconnect` fires. Mirrors the SPA's
        ``StartWebsocketTestMain`` cadence — the aiohttp WS-level
        ``heartbeat`` covers TCP-keepalive but doesn't catch an
        application that's gone deaf above the socket layer.
        """
        assert self._live is not None
        interval = self._live.ping_interval
        timeout = self._live.ping_timeout
        if interval <= 0:
            return
        loop = asyncio.get_running_loop()
        while not self._closing:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return
            if self._closing or self._ws is None or self._ws.closed:
                return
            if loop.time() - self._last_pong > interval + timeout:
                _LOGGER.warning(
                    "no PongOK in %.0fs; closing WS to force reconnect",
                    loop.time() - self._last_pong,
                )
                with contextlib.suppress(Exception):
                    await self._ws.close()
                return
            try:
                await self.send(Commands.Ping.encode())
            except (RuntimeError, ConnectionError) as err:
                _LOGGER.debug("heartbeat send failed (%s); stopping", err)
                return

    async def _poll_charts(self) -> None:
        """Periodically re-issue ``GiveMeChartStandsManuell<id>`` for known charts.

        Chart values don't push; without a poll we'd only see the
        initial bootstrap reading. Cancelled when the connection is
        torn down or :meth:`aclose` flips ``_closing``.
        """
        assert self._live is not None
        interval = self._live.chart_poll_interval
        if interval <= 0:
            return
        while not self._closing:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return
            if self._closing or self._ws is None or self._ws.closed:
                return
            for cid in sorted(self.state.chart_ids):
                if self._closing or self._ws is None or self._ws.closed:
                    return
                try:
                    await self.send(Commands.ChartStands.encode(cid))
                except (RuntimeError, ConnectionError) as err:
                    _LOGGER.debug("chart poll send failed (%s); stopping", err)
                    return

    async def _dispatch_loop(self, source: AsyncIterator[str]) -> None:
        async for text in source:
            self._write_capture(CapturedFrame("server", _now_ts(), text))
            try:
                frame = parse_frame(text)
            except ProtocolError as err:
                _LOGGER.warning("parse error %s on frame %r — skipping", err, text)
                continue

            if isinstance(frame, UnknownFrame):
                self._log_unknown(frame)

            current_phase = self.state.phase
            if current_phase in (SessionPhase.DISCOVERY_OPEN, SessionPhase.ROUTED):
                # In replay we may walk through the discovery frame here.
                if isinstance(frame, GoToLinkSSL):
                    self.state.on_discovery_frame(frame)
                else:
                    # Tolerate an out-of-order app frame in replay fixtures.
                    self.state.phase = SessionPhase.APP_OPEN
                    self.state.on_app_frame(frame)
            else:
                self.state.on_app_frame(frame)

            if isinstance(frame, NamedFields) and frame.name == "MainMenuFinished":
                if not self._bootstrap_done.is_set():
                    self._bootstrap_done.set()

            if isinstance(frame, NamedFields) and frame.name == "PongOK":
                self._last_pong = asyncio.get_running_loop().time()

            await self._chase_chart_ids(frame)

            for handler in self.handlers:
                try:
                    await handler(frame)
                except Exception:
                    _LOGGER.exception("frame handler raised")

            if self._closing:
                break

    # --------------------------- capture I/O --------------------------

    def _write_capture(self, frame: CapturedFrame) -> None:
        if self.capture_path is None:
            return
        if self._capture_handle is None:
            self.capture_path.parent.mkdir(parents=True, exist_ok=True)
            self._capture_handle = self.capture_path.open("a", encoding="utf-8")
        scrubbed_text = _scrub_token(frame.text)
        sanitized = CapturedFrame(
            direction=frame.direction,
            ts=frame.ts,
            text=scrubbed_text,
        )
        self._capture_handle.write(sanitized.to_json() + "\n")  # type: ignore[attr-defined]
        self._capture_handle.flush()  # type: ignore[attr-defined]

    def _log_unknown(self, frame: UnknownFrame) -> None:
        """Append an unknown frame to the unknown-log file.

        Only fires in live mode and when ``unknown_log_path`` is set —
        replay-mode unknowns would just re-log the same frames we
        already recorded the first time we saw them live. Every
        occurrence is appended (not deduped) so we can compare the
        payloads of repeated shapes when figuring out what they mean.
        """
        if self._live is None or self.unknown_log_path is None:
            return
        if self._unknown_handle is None:
            self.unknown_log_path.parent.mkdir(parents=True, exist_ok=True)
            self._unknown_handle = self.unknown_log_path.open("a", encoding="utf-8")
            _LOGGER.info("logging unknown frames to %s", self.unknown_log_path)
        ts = _now_ts()
        entry = {
            "ts": ts,
            "iso_ts": datetime.fromtimestamp(ts, tz=UTC).isoformat(timespec="milliseconds"),
            "raw": _scrub_token(frame.raw),
        }
        self._unknown_handle.write(json.dumps(entry, ensure_ascii=False) + "\n")  # type: ignore[attr-defined]
        self._unknown_handle.flush()  # type: ignore[attr-defined]


# -------------------------- module helpers ---------------------------


def _now_ts() -> float:
    """UTC wall-clock seconds since epoch — capture timestamps."""
    return datetime.now(tz=UTC).timestamp()


def _scrub_token(text: str) -> str:
    """Apply the same redaction the log filter does, for capture lines."""
    return _URL_TOKEN_TRAILING.sub(r"\1<REDACTED>", _TOKEN_QUERY.sub(r"\1<REDACTED>", text))


async def _aiter_ws_text(ws: aiohttp.ClientWebSocketResponse) -> AsyncIterator[str]:
    async for msg in ws:
        if msg.type is aiohttp.WSMsgType.TEXT:
            yield str(msg.data)
        elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
            return
        elif msg.type is aiohttp.WSMsgType.ERROR:
            raise ProtocolError(f"WS error: {ws.exception()!r}")


async def _aiter_capture(replay: _ReplaySources) -> AsyncIterator[str]:
    """Iterate server-direction text frames from a capture fixture.

    Skips client-direction frames so replay never re-issues commands
    against any server. If ``realtime`` is set, sleeps between frames to
    preserve the original wall-clock pacing.
    """
    last_ts: float | None = None
    with replay.path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                cap = CapturedFrame.from_json(line)
            except (ValueError, KeyError, json.JSONDecodeError) as err:
                _LOGGER.warning("skipping malformed capture line: %s", err)
                continue
            if cap.direction != "server":
                continue
            if replay.realtime and last_ts is not None:
                gap = max(0.0, cap.ts - last_ts)
                if gap > 0:
                    await asyncio.sleep(gap)
            last_ts = cap.ts
            yield cap.text


# ---------------------- CLI (importable but lazy) --------------------


def _cli_entry() -> None:
    """Console-script entry point for ``sp-cli`` — imports Click lazily.

    The HA integration never imports this function, so Click stays out
    of HA's import graph.
    """
    import click  # noqa: PLC0415 — intentional lazy import

    _build_cli(click)(standalone_mode=True)


def _build_cli(click: Any) -> Callable[..., None]:
    """Build the Click command. Kept out of module top level so HA never imports Click."""

    @click.command(name="sp-cli")
    @click.option(
        "--live",
        "live_mode",
        is_flag=True,
        default=False,
        help="Connect to the real smartplace.ch server (uses $SMART_PLACE_TOKEN).",
    )
    @click.option(
        "--replay",
        "replay_path",
        type=click.Path(exists=True, dir_okay=False, path_type=Path),
        default=None,
        help="Replay an ndjson capture fixture; the live server is not contacted.",
    )
    @click.option(
        "--capture",
        "capture_path",
        type=click.Path(dir_okay=False, path_type=Path),
        default=None,
        help="Tee every frame (both directions, token-redacted) to this file.",
    )
    @click.option(
        "--log-level",
        default="INFO",
        type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"]),
        help="Logging verbosity for the client library.",
    )
    def cli(
        live_mode: bool,
        replay_path: Path | None,
        capture_path: Path | None,
        log_level: str,
    ) -> None:
        """Smart Place CLI — observe and (carefully) interact with the live system.

        Exactly one of --live and --replay <file> is required. Either
        mode is bidirectional: server frames print to stdout, lines
        typed on stdin are sent. There is no software gate against
        accidental sends — see DESIGN.md §5.3.

        In --live mode, frames the parser doesn't recognise are always
        appended to ``output/unknown_frames.ndjson`` (gitignored) so
        they can be inspected later. Capture files (``--capture``) can
        be placed anywhere; ``output/`` is a good default home.
        """
        if live_mode == (replay_path is not None):
            raise click.UsageError("exactly one of --live and --replay <file> is required")
        logging.basicConfig(level=log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        install_token_redaction_filter()

        asyncio.run(_run_cli(live_mode, replay_path, capture_path))

    return cli


# CLI-default output directory. User-supplied --capture paths can target
# anywhere; only the always-on unknown-frame log defaults under here.
# Gitignored so captures don't get committed by accident.
_CLI_OUTPUT_DIR = Path("output")
_CLI_UNKNOWN_LOG = _CLI_OUTPUT_DIR / "unknown_frames.ndjson"


async def _run_cli(
    live_mode: bool,
    replay_path: Path | None,
    capture_path: Path | None,
) -> None:
    """Async body of the CLI: print server frames, send stdin lines."""

    async def printer(frame: ServerFrame) -> None:
        ts = datetime.now(tz=UTC).isoformat(timespec="milliseconds")
        sys.stdout.write(f"[{ts}] <- {frame!r}\n")
        sys.stdout.flush()

    # Auto-load .env so users don't have to export SMART_PLACE_TOKEN in
    # every terminal. python-dotenv walks up from cwd; existing env vars
    # win. Lazy import keeps it out of HA's import graph (HA never enters
    # the CLI).
    from dotenv import load_dotenv  # noqa: PLC0415

    load_dotenv()

    if live_mode:
        token = os.environ.get("SMART_PLACE_TOKEN")
        if not token:
            raise SystemExit(
                "SMART_PLACE_TOKEN is required for --live. Put it in .env "
                "(SMART_PLACE_TOKEN=...) or export it in your shell."
            )
        client = SmartPlaceClient.live(
            token=token,
            capture=capture_path,
            unknown_log=_CLI_UNKNOWN_LOG,
            handlers=[printer],
        )
    else:
        assert replay_path is not None
        client = SmartPlaceClient.replay(path=replay_path, capture=capture_path, handlers=[printer])

    async with client:
        runner = asyncio.create_task(client.run(), name="sp-cli-run")
        sender = asyncio.create_task(_stdin_pump(client), name="sp-cli-stdin")
        try:
            # The runner is the primary task. If stdin closes (EOF) the sender
            # exits early and that's fine — the user explicitly wanted
            # observe-only. The CLI should only stop when the runner finishes
            # or the user interrupts.
            await runner
        finally:
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await sender
        exc = runner.exception()
        if exc is not None and not isinstance(exc, asyncio.CancelledError):
            raise exc


async def _stdin_pump(client: SmartPlaceClient) -> None:
    """Read lines from stdin and feed them to ``client.send``.

    Each line is a single text frame. Empty lines are ignored. Closing
    stdin (Ctrl-D) ends the pump and causes the CLI to exit.
    """
    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if line == "":
            return
        line = line.rstrip("\n")
        if not line:
            continue
        try:
            await client.send(line)
        except Exception as err:  # noqa: BLE001
            sys.stderr.write(f"send failed: {err}\n")


if __name__ == "__main__":
    _cli_entry()
