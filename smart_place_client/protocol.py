"""Smart Place wire-protocol module.

Pure parsing, encoding, and connection-state logic for the Smart Place
WebSocket protocol. No I/O, no aiohttp, no Click. This module is shared
between the standalone client and the Home Assistant integration.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from enum import Enum
import re
from typing import Final

DISCOVERY_HOST: Final = "spr1.smartplace.ch"
DISCOVERY_PORT: Final = 8770
DISCOVERY_WS_PATH: Final = "/StartAppExt/"
DISCOVERY_ORIGIN: Final = f"https://{DISCOVERY_HOST}:{DISCOVERY_PORT}"

APP_WS_PATH: Final = "/UpdatenLS"


class ProtocolError(Exception):
    """Raised when the server emits something that violates our understanding."""


class SmartPlaceAuthError(ProtocolError):
    """Raised when the token is rejected or the discovery channel refuses to route.

    Token-bearing values are never included in the exception message — the
    log redaction layer assumes nothing here will leak.
    """


@dataclass(frozen=True, slots=True)
class GoToLinkSSL:
    """Discovery routing frame: host/port/path/token to use for the app channel."""

    host: str
    port: int
    path: str | None
    token2: str

    @property
    def routed_https_url(self) -> str:
        """HTTPS URL the browser would load in its iframe after discovery."""
        if self.token2 == "Leer" and self.path:
            return f"https://{self.host}:{self.port}{self.path}"
        return f"https://{self.host}:{self.port}/Infoboard1?{self.token2}"

    @property
    def app_ws_url(self) -> str:
        """App-channel WebSocket URL (always ``/UpdatenLS``)."""
        return f"wss://{self.host}:{self.port}{APP_WS_PATH}"

    @property
    def app_ws_origin(self) -> str:
        """``Origin`` header to use for the app WS handshake."""
        return f"https://{self.host}:{self.port}"


@dataclass(frozen=True, slots=True)
class GlobalConfig:
    """Bootstrap-read response: SPA display config (language, dimming, screensaver, ...)."""

    language: str
    standby: str
    brightness: str
    screensaver_mode: str
    screensaver_start: str
    screensaver_duration: str


@dataclass(frozen=True, slots=True)
class Temperature:
    """Per-sensor indoor temperature push (TEMPIST<sensor>:<value>); value in °C."""

    sensor: int
    value: float


@dataclass(frozen=True, slots=True)
class NamedValue:
    """Generic single-string-value server frame, identified by ``name``.

    Covers both singletons (TEMPOUT, WindSpeed, BasicInfos: wire is
    ``prefix:value``) and per-id pushes (leuchte13, SZENEN10, Vol1:
    wire is ``prefix<index>:value``). For per-id shapes, ``index`` is
    the trailing-digit suffix; for singletons it is ``None``. ``value``
    is the raw string after the separator and may be empty.
    """

    name: str
    value: str
    index: int | None = None


@dataclass(frozen=True, slots=True)
class NamedFields:
    """Generic multi-field server frame, identified by ``name``.

    Covers ``prefix>f1>f2>...`` replies (InfoboardWidgets, GlobalGsa)
    and ``prefix<index>:f1,f2,...`` per-id configs (INHALTLeuchten,
    UnterMenuJalousien, Floorplan). For per-id shapes, ``index`` is
    the trailing-digit suffix; for singletons it is ``None``.
    ``fields`` is empty for marker frames (PongOK,
    GiveMeMainMenuFinished) where the wire is just the prefix with
    no payload.
    """

    name: str
    fields: tuple[str, ...]
    index: int | None = None


@dataclass(frozen=True, slots=True)
class UnknownFrame:
    """Catch-all for text frames the registry doesn't recognise yet.

    The dispatch layer logs (token-redacted) and moves on; in live mode
    the raw frame is also appended to ``output/unknown_frames.ndjson``
    for later analysis.
    """

    raw: str


ServerFrame = GoToLinkSSL | GlobalConfig | Temperature | NamedValue | NamedFields | UnknownFrame
"""Any frame the server may emit, post-parsing."""


def _parse_fields_after_prefix(text: str, prefix: str) -> tuple[str, ...]:
    """Return the ``>``-delimited fields trailing ``prefix`` in ``text``.

    - ``prefix`` alone (no payload) returns ``()``.
    - ``prefix>`` returns ``("",)`` (one empty field).
    - ``prefix>a>b>c`` returns ``("a", "b", "c")``.
    - Trailing ``>`` preserves a trailing empty field: ``prefix>a>`` is
      ``("a", "")`` — matches what the SPA wire format emits.

    Raises :class:`ProtocolError` if ``prefix`` is absent or if a
    non-``>`` character follows the prefix.
    """
    if not text.startswith(prefix):
        raise ProtocolError(f"frame missing prefix {prefix!r}: {text!r}")
    rest = text[len(prefix) :]
    if not rest:
        return ()
    if not rest.startswith(">"):
        raise ProtocolError(
            f"frame {prefix!r}: expected '>' after prefix, got {rest[:1]!r}",
        )
    return tuple(rest[1:].split(">"))


def _parse_go_to_link_ssl(text: str) -> GoToLinkSSL:
    """Parse ``GoToLinkSSL:<host>:<port-or-port/path>:<token2>``.

    The middle field is either a bare port (``8770``) or a port with a
    trailing path (``8770/Start1``).
    """
    parts = text.split(":", maxsplit=3)
    if len(parts) != 4:
        raise ProtocolError(f"GoToLinkSSL frame malformed: expected 4 fields, got {len(parts)}")

    _, host, port_or_port_path, token2 = parts
    if not host or not port_or_port_path or not token2:
        raise ProtocolError("GoToLinkSSL frame has empty field")

    port_str, sep, path = port_or_port_path.partition("/")
    try:
        port = int(port_str)
    except ValueError as err:
        raise ProtocolError(f"GoToLinkSSL port not an int: {port_str!r}") from err

    return GoToLinkSSL(
        host=host,
        port=port,
        path=f"/{path}" if sep else None,
        token2=token2,
    )


def _parse_global_config(text: str) -> GlobalConfig:
    """Parse ``EINSTELLUNGENGLOBAL>f1>f2>f3>f4>f5>f6``."""
    fields = _parse_fields_after_prefix(text, "EINSTELLUNGENGLOBAL")
    if len(fields) != 6:
        raise ProtocolError(
            f"EINSTELLUNGENGLOBAL frame: expected 6 fields, got {len(fields)}",
        )
    return GlobalConfig(
        language=fields[0],
        standby=fields[1],
        brightness=fields[2],
        screensaver_mode=fields[3],
        screensaver_start=fields[4],
        screensaver_duration=fields[5],
    )


_TEMPIST_RE = re.compile(r"^TEMPIST(\d+):(.+)$")


def _parse_temperature(text: str) -> Temperature:
    """Parse ``TEMPIST<sensor>:<value>``; one entry generalises across sensors."""
    m = _TEMPIST_RE.match(text)
    if m is None:
        raise ProtocolError(f"TEMPIST frame malformed: {text!r}")
    try:
        value = float(m.group(2))
    except ValueError as err:
        raise ProtocolError(f"TEMPIST value not a float: {m.group(2)!r}") from err
    return Temperature(sensor=int(m.group(1)), value=value)


_INFOBOARD_CHART_REF_RE = re.compile(
    r"CHART(\d+)STAND(\d+)~SPDB-CHARTSSTANDS>unit-([^~]+)",
)


def parse_chart_references(infoboard_entry_value: str) -> Iterator[tuple[int, int, str]]:
    """Yield ``(chart_id, series, unit)`` tuples from an InfoboardEntry value.

    InfoboardEntry rows that bind a label to a consumption chart embed
    one or more ``CHART<id>STAND<series>~SPDB-CHARTSSTANDS>unit-<unit>``
    references (e.g. ``unit-l`` for water, ``unit-KWh`` for electricity).
    Use this to populate :attr:`SessionState.chart_units` and feed the
    HA layer the device-class/unit hints it needs to expose the chart
    as a sensor.
    """
    for m in _INFOBOARD_CHART_REF_RE.finditer(infoboard_entry_value):
        yield int(m.group(1)), int(m.group(2)), m.group(3)


@dataclass(frozen=True, slots=True)
class MessageDefinition:
    r"""Declarative entry in :data:`KNOWN_MESSAGES`.

    One per known wire-frame shape. ``parse_frame()`` walks
    :data:`KNOWN_MESSAGES` in order; the first definition whose
    ``pattern`` matches takes the frame. Adding a new known frame:
    write a parser, append a ``MessageDefinition`` (with a wire
    ``example``), add a unit test.

    Attributes:
        name: CamelCase identifier; matches the dataclass name returned
            by :attr:`parse`.
        description: Plain-English meaning + when the frame appears.
            Read by future-us when debugging.
        pattern: Compiled regex used for identification. Use ``^...$``
            for exact matches; use ``\d+`` etc. to generalise across
            indexed variants like ``TEMPIST<N>`` so we don't need one
            entry per sensor.
        parse: Callable that takes the full frame text and returns the
            parsed dataclass. Must raise :class:`ProtocolError` on
            malformed frames.
        example: Concrete wire string used by the smoke test in
            :func:`test_known_messages_drive_parse_frame` and as
            documentation of the shape.
    """

    name: str
    description: str
    pattern: re.Pattern[str]
    parse: Callable[[str], ServerFrame]
    example: str


def _named_value_parser(name: str, prefix: str, *, allow_empty: bool = False) -> Callable[[str], NamedValue]:
    """Build a parser for ``prefix:<value>`` singletons returning ``NamedValue(name, value)``."""

    def parse(text: str) -> NamedValue:
        if not text.startswith(prefix + ":"):
            raise ProtocolError(f"frame missing prefix {prefix!r}: {text!r}")
        value = text[len(prefix) + 1 :]
        if not value and not allow_empty:
            raise ProtocolError(f"{prefix} frame missing value: {text!r}")
        return NamedValue(name=name, value=value)

    return parse


def _named_fields_parser(name: str, prefix: str) -> Callable[[str], NamedFields]:
    """Build a parser for ``prefix[>f1>f2>...]`` frames returning ``NamedFields(name, fields)``."""
    return lambda text: NamedFields(name=name, fields=_parse_fields_after_prefix(text, prefix))


def _raw_value_parser(name: str, prefix: str) -> Callable[[str], NamedValue]:
    """Build a parser that stores everything after ``prefix`` as ``value`` verbatim.

    Use for one-off shapes where the separator isn't ``:`` or ``>``
    (``SPOTIFYTOKEN<...``, ``PLAYSLOT-1<...``,
    ``StatusInhaltListe_1_1_...``). ``value`` may be empty.
    """

    def parse(text: str) -> NamedValue:
        if not text.startswith(prefix):
            raise ProtocolError(f"frame missing prefix {prefix!r}: {text!r}")
        return NamedValue(name=name, value=text[len(prefix) :])

    return parse


def _indexed_value_parser(
    name: str,
    prefix: str,
    *,
    separator: str = ":",
) -> Callable[[str], NamedValue]:
    """Build a parser for ``prefix<index><separator><value>`` per-id pushes.

    Generalises across ids — one entry covers ``leuchte1``,
    ``leuchte13``, etc. ``index`` is the captured trailing-digit
    suffix; ``value`` may be empty. ``separator`` is normally ``":"``
    but may be a longer literal (e.g. ``"INHALT:"`` for
    ``INFOBOARD<n>INHALT:<value>``) when a per-id family interleaves
    text between the index and the colon.
    """
    pattern = re.compile(rf"^{re.escape(prefix)}(\d+){re.escape(separator)}(.*)$", re.DOTALL)

    def parse(text: str) -> NamedValue:
        m = pattern.match(text)
        if m is None:
            raise ProtocolError(f"{prefix}<N>{separator} frame malformed: {text!r}")
        return NamedValue(name=name, value=m.group(2), index=int(m.group(1)))

    return parse


def _indexed_fields_parser(name: str, prefix: str) -> Callable[[str], NamedFields]:
    """Build a parser for ``prefix<index>:<f1>,<f2>,...`` per-id configs (comma-delimited)."""
    pattern = re.compile(rf"^{re.escape(prefix)}(\d+):(.*)$", re.DOTALL)

    def parse(text: str) -> NamedFields:
        m = pattern.match(text)
        if m is None:
            raise ProtocolError(f"{prefix}<N>: frame malformed: {text!r}")
        payload = m.group(2)
        fields: tuple[str, ...] = tuple(payload.split(",")) if payload else ()
        return NamedFields(name=name, fields=fields, index=int(m.group(1)))

    return parse


def encode_frame(message: str) -> str:
    """Encode an arbitrary text message for the app WS.

    The protocol uses plain text on the wire (vendor frontend uses
    ``spsocket2.send(text)``). This function is a thin pass-through so the
    encoding boundary is explicit and any future framing (length prefix,
    base64, etc.) lands here without scattering changes.
    """
    if "\n" in message or "\r" in message:
        raise ProtocolError("Smart Place text frame must not contain newlines")
    return message


def discovery_ws_url(token: str) -> str:
    """Build the discovery WS URL for the given token.

    The token is the only secret; keep all URL construction here so a
    misuse stands out in review. Callers must not log the returned URL —
    `client.py` ships a token-redacting log filter for that.
    """
    return f"wss://{DISCOVERY_HOST}:{DISCOVERY_PORT}{DISCOVERY_WS_PATH}?TOKEN={token}"


class SessionPhase(Enum):
    """Phases of a single Smart Place connection lifetime.

    The state machine is linear in the happy path:
    ``DISCOVERY_OPEN`` → ``ROUTED`` → ``APP_OPEN`` → ``BOOTSTRAPPED`` →
    ``READY``. ``CLOSED`` is the terminal phase set by :meth:`SessionState.close`.
    """

    DISCOVERY_OPEN = "discovery_open"
    ROUTED = "routed"
    APP_OPEN = "app_open"
    BOOTSTRAPPED = "bootstrapped"
    READY = "ready"
    CLOSED = "closed"


@dataclass(slots=True)
class SessionState:
    """In-memory state machine for one connection lifetime.

    Mutating methods return the new phase (and update self.phase) so
    callers can both drive control flow and assert on transitions.
    Invariants:
    - ``route`` is populated once we've parsed a ``GoToLinkSSL`` frame.
    - ``infoboard_widgets`` is populated by the
      ``Commands.InfoboardWidgets`` reply (wire ``GiveStatusListe`` →
      ``StatusListe>...``) and signals BOOTSTRAPPED.
    - ``global_config`` is populated opportunistically if the client
      chose to send ``GiveMeGlobalConfig`` (the bootstrap doesn't
      require it; see ``client.py`` notes).
    - The state machine doesn't own the WebSocket; the I/O layer in
      ``client.py`` does. This object is plain data so it is trivial to
      test from ``test_protocol.py``.
    """

    phase: SessionPhase = SessionPhase.DISCOVERY_OPEN
    route: GoToLinkSSL | None = None
    global_config: GlobalConfig | None = None
    infoboard_widgets: NamedFields | None = None  # name == "InfoboardWidgets"
    chart_ids: set[int] = field(default_factory=set)
    chart_units: dict[int, str] = field(default_factory=dict)

    def on_discovery_frame(self, frame: ServerFrame) -> SessionPhase:
        """Apply a frame received on the discovery WS.

        Returns the new phase. Raises :class:`ProtocolError` if the frame
        is not the expected routing response.
        """
        if isinstance(frame, GoToLinkSSL):
            self.route = frame
            self.phase = SessionPhase.ROUTED
            return self.phase
        raise ProtocolError(
            f"Unexpected discovery frame: {type(frame).__name__}",
        )

    def on_app_open(self) -> SessionPhase:
        """Mark that the app WS handshake has completed."""
        if self.phase is not SessionPhase.ROUTED:
            raise ProtocolError(
                f"app WS opened from unexpected phase: {self.phase.value}",
            )
        self.phase = SessionPhase.APP_OPEN
        return self.phase

    def on_app_frame(self, frame: ServerFrame) -> SessionPhase:
        """Apply a frame received on the app WS.

        InfoboardWidgets advances the phase; anything else (including
        the opportunistic GlobalConfig) is stashed but does not gate
        progress.
        """
        if isinstance(frame, GlobalConfig):
            self.global_config = frame
        elif isinstance(frame, NamedFields) and frame.name == "InfoboardWidgets":
            self.infoboard_widgets = frame

        if self.phase is SessionPhase.APP_OPEN and self.infoboard_widgets:
            self.phase = SessionPhase.BOOTSTRAPPED
        elif self.phase is SessionPhase.BOOTSTRAPPED and self.infoboard_widgets:
            self.phase = SessionPhase.READY
        return self.phase

    def close(self) -> None:
        """Mark the session as closed; idempotent."""
        self.phase = SessionPhase.CLOSED
