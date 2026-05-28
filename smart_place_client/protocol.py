"""Smart Place wire-protocol module.

Pure parsing, encoding, and connection-state logic for the Smart Place
WebSocket protocol. No I/O, no aiohttp, no Click. This module is shared
between the standalone client and the Home Assistant integration.

Every message shape below cites the source that motivated it
(live-captured frame or JavaScript source) per DESIGN.md §1.2 — there is
no second public source for this protocol, so we keep a paper trail.

References:
- DESIGN.md §1, §1.1, §6.2.
- `javallg.js` (vendor frontend, served from the routed page).
- Live capture at `tests/fixtures/` (once captured during Phase 3).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
import re
from typing import Final

DISCOVERY_HOST: Final = "spr1.smartplace.ch"
DISCOVERY_PORT: Final = 8770
DISCOVERY_WS_PATH: Final = "/StartAppExt/"
DISCOVERY_ORIGIN: Final = f"https://{DISCOVERY_HOST}:{DISCOVERY_PORT}"

APP_WS_PATH: Final = "/UpdatenLS"

LEGACY_HOST: Final = "spr0.smartplace.ch"
LEGACY_PORT: Final = 8770

GLOBAL_CONFIG_REQUEST: Final = "GiveMeGlobalConfig"
STATUS_LISTE_REQUEST: Final = "GiveStatusListe"


class ProtocolError(Exception):
    """Raised when the server emits something that violates our understanding."""


class SmartPlaceAuthError(ProtocolError):
    """Raised when the token is rejected or the discovery channel refuses to route.

    Token-bearing values are never included in the exception message — the
    log redaction layer assumes nothing here will leak.
    """


@dataclass(frozen=True, slots=True)
class GoToLinkSSL:
    """Discovery routing frame for the modern SSL path.

    Source: live discovery WS frame ``GoToLinkSSL:spr1.smartplace.ch:<port>/Start1:Leer``
    observed 2026-05-28 (see DESIGN.md §1.1). Frame is ``:``-delimited with
    four fields: prefix, host, port-or-port/path, token2.
    """

    host: str
    port: int
    path: str | None
    token2: str

    @property
    def routed_https_url(self) -> str:
        """Return the HTTPS URL the browser would load in its iframe.

        Per DESIGN §6.2 step 3: if ``token2 == "Leer"`` and the route
        contains a path, use that path (observed: ``/Start1``). Otherwise
        use ``/Infoboard1?<token2>``.
        """
        if self.token2 == "Leer" and self.path:
            return f"https://{self.host}:{self.port}{self.path}"
        return f"https://{self.host}:{self.port}/Infoboard1?{self.token2}"

    @property
    def app_ws_url(self) -> str:
        """Return the app-channel WebSocket URL (always ``/UpdatenLS``)."""
        return f"wss://{self.host}:{self.port}{APP_WS_PATH}"

    @property
    def app_ws_origin(self) -> str:
        """Return the ``Origin`` header to use for the app WS handshake."""
        return f"https://{self.host}:{self.port}"


@dataclass(frozen=True, slots=True)
class GoToLinkOldSystem:
    """Discovery routing frame for the legacy server.

    Source: ``GoToLinkOLDSYSTEM:<token2>`` in the Start5 page JavaScript.
    Not yet seen as a live frame but the JS handler exists, so the parser
    handles it. Redirects to ``spr0.smartplace.ch:8770/Start2?<token2>``.
    """

    token2: str

    @property
    def legacy_url(self) -> str:
        """Return the legacy HTTPS URL the browser would load."""
        return f"https://{LEGACY_HOST}:{LEGACY_PORT}/Start2?{self.token2}"


@dataclass(frozen=True, slots=True)
class HostNotOnline:
    """Discovery routing frame: the user's installation is offline.

    Source: ``HostNotOnline`` literal in the Start5 page JavaScript.
    Not yet seen as a live frame. Surfaces as an auth-like failure to HA;
    we keep retrying because the installation may come back online.
    """


@dataclass(frozen=True, slots=True)
class GlobalConfig:
    """Bootstrap-read response for ``GiveMeGlobalConfig``.

    Source: live ``EINSTELLUNGENGLOBAL>...`` frame observed 2026-05-28
    (see DESIGN.md §1.1). ``>``-delimited text frame with six fields:
    language, standby, brightness, screensaver mode, screensaver start,
    screensaver duration. Values are kept as raw strings — we have no
    documentation that pins the enum / unit semantics, so callers parse
    further if they need to.
    """

    language: str
    standby: str
    brightness: str
    screensaver_mode: str
    screensaver_start: str
    screensaver_duration: str


@dataclass(frozen=True, slots=True)
class StatusListe:
    """Bootstrap-read response for ``GiveStatusListe``.

    Source: live ``StatusListe>...`` frame observed 2026-05-28
    (see DESIGN.md §1.1). ``>``-delimited text frame with three fields.
    Field semantics are not documented; kept as a positional tuple of
    strings so callers can inspect / extend the parse without losing data.
    """

    fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Temperature:
    """Current temperature reading from an indoor sensor.

    Source: live capture 2026-05-28, ``TEMPIST<sensor>:<degrees-celsius>``.
    Names map: ``TEMPIST`` = "TEMPeratur IST" (current/actual temperature
    in German). ``sensor`` is the trailing integer (1-indexed); ``value``
    is the wire string (typically a float like ``26.3``) kept untyped per
    our existing GlobalConfig/StatusListe convention.
    """

    sensor: int
    value: str


@dataclass(frozen=True, slots=True)
class OutdoorTemperature:
    """Current outdoor temperature reading.

    Source: live capture 2026-05-28, ``TEMPOUT:<degrees-celsius>``.
    """

    value: str


@dataclass(frozen=True, slots=True)
class WindSpeed:
    """Current wind-speed reading.

    Source: live capture 2026-05-28, ``WINDGESCHWINDIGKEIT:<value>``
    ("Windgeschwindigkeit" = wind speed in German). Unit is presumed
    m/s (typical for European weather stations) but not confirmed.
    """

    value: str


@dataclass(frozen=True, slots=True)
class UnknownFrame:
    """Catch-all for text frames whose shape we don't yet recognise.

    The dispatch layer logs (token-redacted) and moves on. As we capture
    more frames during Phase 3, distinct shapes get promoted to their own
    dataclass per DESIGN.md §9 question 5.
    """

    raw: str


ServerFrame = (
    GoToLinkSSL
    | GoToLinkOldSystem
    | HostNotOnline
    | GlobalConfig
    | StatusListe
    | Temperature
    | OutdoorTemperature
    | WindSpeed
    | UnknownFrame
)
"""Any frame the server may emit, post-parsing."""


def _parse_host_not_online(_text: str) -> HostNotOnline:
    """Return the singleton offline marker."""
    return HostNotOnline()


def _parse_go_to_link_old_system(text: str) -> GoToLinkOldSystem:
    """Parse ``GoToLinkOLDSYSTEM:<token2>``."""
    _, _, token2 = text.partition(":")
    if not token2:
        raise ProtocolError("GoToLinkOLDSYSTEM frame missing token2")
    return GoToLinkOldSystem(token2=token2)


def _parse_go_to_link_ssl(text: str) -> GoToLinkSSL:
    """Parse ``GoToLinkSSL:<host>:<port-or-port/path>:<token2>``.

    Source: DESIGN.md §1 table + §1.1 live observation. The middle field
    can be either a bare port (``8770``) or a port with a trailing path
    (``8770/Start1``); the live route was the latter.
    """
    # maxsplit=3 -> exactly four parts when well-formed.
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
    """Parse ``EINSTELLUNGENGLOBAL>f1>f2>f3>f4>f5>f6``.

    Source: live capture 2026-05-28 returned exactly six fields.
    """
    _, _, payload = text.partition(">")
    fields = payload.split(">")
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


def _parse_status_liste(text: str) -> StatusListe:
    """Parse ``StatusListe>...``.

    Source: live capture 2026-05-28 returned three fields; we keep them
    as a positional tuple because semantics aren't pinned yet.
    """
    _, _, payload = text.partition(">")
    return StatusListe(fields=tuple(payload.split(">")))


_TEMPIST_RE = re.compile(r"^TEMPIST(\d+):(.+)$")


def _parse_temperature(text: str) -> Temperature:
    """Parse ``TEMPIST<sensor>:<value>``.

    Generalises across all sensor indices (TEMPIST1, TEMPIST2, ... seen
    in live captures 2026-05-28) so adding a new sensor doesn't require
    a new registry entry.
    """
    m = _TEMPIST_RE.match(text)
    if m is None:
        raise ProtocolError(f"TEMPIST frame malformed: {text!r}")
    return Temperature(sensor=int(m.group(1)), value=m.group(2))


def _parse_outdoor_temperature(text: str) -> OutdoorTemperature:
    """Parse ``TEMPOUT:<value>``."""
    _, _, value = text.partition(":")
    if not value:
        raise ProtocolError(f"TEMPOUT frame missing value: {text!r}")
    return OutdoorTemperature(value=value)


def _parse_wind_speed(text: str) -> WindSpeed:
    """Parse ``WINDGESCHWINDIGKEIT:<value>``."""
    _, _, value = text.partition(":")
    if not value:
        raise ProtocolError(f"WINDGESCHWINDIGKEIT frame missing value: {text!r}")
    return WindSpeed(value=value)


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


KNOWN_MESSAGES: Final[list[MessageDefinition]] = [
    MessageDefinition(
        name="HostNotOnline",
        description=(
            "Installation is offline; the discovery WS returns this when the user's "
            "home server is unreachable from spr1. Surfaces as a typed error so the "
            "integration can mark entities unavailable."
        ),
        pattern=re.compile(r"^HostNotOnline$"),
        parse=_parse_host_not_online,
        example="HostNotOnline",
    ),
    MessageDefinition(
        name="GoToLinkSSL",
        description=(
            "Modern SSL routing frame. Format: GoToLinkSSL:<host>:<port-or-port/path>:<token2>. "
            "The middle field is a bare port or port-with-path; the routed port is dynamic "
            "per session (DESIGN §10)."
        ),
        pattern=re.compile(r"^GoToLinkSSL:"),
        parse=_parse_go_to_link_ssl,
        example="GoToLinkSSL:spr1.smartplace.ch:38435/Start1:Leer",
    ),
    MessageDefinition(
        name="GoToLinkOldSystem",
        description=(
            "Legacy server routing. Format: GoToLinkOLDSYSTEM:<token2>. Redirects "
            "to spr0.smartplace.ch:8770/Start2?<token2>. Seen only in javallg.js, "
            "not yet as a live frame."
        ),
        pattern=re.compile(r"^GoToLinkOLDSYSTEM:"),
        parse=_parse_go_to_link_old_system,
        example="GoToLinkOLDSYSTEM:legacy-token-xyz",
    ),
    MessageDefinition(
        name="GlobalConfig",
        description=(
            "Response to GiveMeGlobalConfig. Format: EINSTELLUNGENGLOBAL>language>"
            "standby>brightness>screensaver_mode>screensaver_start>screensaver_duration. "
            "Values are wire-typed strings (brightness is 0..1 float-as-string)."
        ),
        pattern=re.compile(r"^EINSTELLUNGENGLOBAL>"),
        parse=_parse_global_config,
        example="EINSTELLUNGENGLOBAL>2>300>0.8>1>300>undefined",
    ),
    MessageDefinition(
        name="StatusListe",
        description=(
            "Response to GiveStatusListe. Format: StatusListe>f1>f2>... — observed "
            "fields look like info-board tab labels (Wetter, Tagesverbrauch), NOT "
            "per-device state (DESIGN §9 Q2)."
        ),
        pattern=re.compile(r"^StatusListe>"),
        parse=_parse_status_liste,
        example="StatusListe>Wetter>Tagesverbrauch>",
    ),
    MessageDefinition(
        name="Temperature",
        description=(
            "Push: current temperature reading from indoor sensor N. "
            "Format: TEMPIST<sensor>:<degrees-celsius>. The regex generalises "
            "across all sensors observed (TEMPIST1, TEMPIST2, TEMPIST3, TEMPIST6, ...) "
            "without a per-sensor registry entry."
        ),
        pattern=re.compile(r"^TEMPIST\d+:"),
        parse=_parse_temperature,
        example="TEMPIST3:27.2",
    ),
    MessageDefinition(
        name="OutdoorTemperature",
        description=("Push: current outdoor temperature reading. Format: TEMPOUT:<degrees-celsius>."),
        pattern=re.compile(r"^TEMPOUT:"),
        parse=_parse_outdoor_temperature,
        example="TEMPOUT:26.6",
    ),
    MessageDefinition(
        name="WindSpeed",
        description=("Push: current wind-speed reading (likely m/s). Format: WINDGESCHWINDIGKEIT:<value>."),
        pattern=re.compile(r"^WINDGESCHWINDIGKEIT:"),
        parse=_parse_wind_speed,
        example="WINDGESCHWINDIGKEIT:7.9",
    ),
]
"""All known server frame shapes, in identification order.

Each entry uses a regex pattern, which lets one entry cover all
trailing-index variants of an otherwise-uniform shape (e.g. one
``Temperature`` entry handles every ``TEMPIST<N>`` sensor without a
per-sensor row). Per the ``smart-place-prior-art`` memory: every
``MessageDefinition`` should cite its source in the surrounding
parser's docstring.
"""


def parse_frame(text: str) -> ServerFrame:
    """Parse a text frame from the server.

    Walks :data:`KNOWN_MESSAGES` in declaration order and returns the
    first matching frame. Unknown shapes return :class:`UnknownFrame`
    so the dispatch layer can log + skip + record for later analysis
    rather than crashing.
    """
    for defn in KNOWN_MESSAGES:
        if defn.pattern.match(text):
            return defn.parse(text)
    return UnknownFrame(raw=text)


def encode_global_config_request() -> str:
    """Encode the ``GiveMeGlobalConfig`` bootstrap-read.

    Source: ``javallg.js`` ``spsocket2.send("GiveMeGlobalConfig")`` and
    DESIGN §6.2 step 5.
    """
    return GLOBAL_CONFIG_REQUEST


def encode_status_liste_request() -> str:
    """Encode the ``GiveStatusListe`` bootstrap-read.

    Source: ``javallg.js`` ``spsocket2.send("GiveStatusListe")`` and
    DESIGN §6.2 step 6.
    """
    return STATUS_LISTE_REQUEST


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
    ``READY``. ``OFFLINE`` and ``LEGACY`` are terminal-for-this-attempt
    branches that the connection loop handles separately.
    """

    DISCOVERY_OPEN = "discovery_open"
    ROUTED = "routed"
    APP_OPEN = "app_open"
    BOOTSTRAPPED = "bootstrapped"
    READY = "ready"
    OFFLINE = "offline"
    LEGACY = "legacy"
    CLOSED = "closed"


@dataclass(slots=True)
class SessionState:
    """In-memory state machine for one connection lifetime.

    Mutating methods return the new phase (and update self.phase) so
    callers can both drive control flow and assert on transitions.
    Invariants:
    - ``route`` is populated once we've parsed a ``GoToLinkSSL`` frame.
    - ``global_config`` and ``status_liste`` are populated by the
      bootstrap reads.
    - The state machine doesn't own the WebSocket; the I/O layer in
      ``client.py`` does. This object is plain data so it is trivial to
      test from ``test_protocol.py``.
    """

    phase: SessionPhase = SessionPhase.DISCOVERY_OPEN
    route: GoToLinkSSL | None = None
    global_config: GlobalConfig | None = None
    status_liste: StatusListe | None = None

    def on_discovery_frame(self, frame: ServerFrame) -> SessionPhase:
        """Apply a frame received on the discovery WS.

        Returns the new phase. Raises :class:`ProtocolError` if the frame
        is not one of the expected discovery responses.
        """
        if isinstance(frame, GoToLinkSSL):
            self.route = frame
            self.phase = SessionPhase.ROUTED
            return self.phase
        if isinstance(frame, GoToLinkOldSystem):
            self.phase = SessionPhase.LEGACY
            return self.phase
        if isinstance(frame, HostNotOnline):
            self.phase = SessionPhase.OFFLINE
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

        Bootstrap frames advance the phase; anything else leaves the
        phase alone so per-entity dispatch can pick it up.
        """
        if isinstance(frame, GlobalConfig):
            self.global_config = frame
        elif isinstance(frame, StatusListe):
            self.status_liste = frame

        if self.phase is SessionPhase.APP_OPEN and self.global_config and self.status_liste:
            self.phase = SessionPhase.BOOTSTRAPPED
        elif self.phase is SessionPhase.BOOTSTRAPPED and self.global_config and self.status_liste:
            self.phase = SessionPhase.READY
        return self.phase

    def close(self) -> None:
        """Mark the session as closed; idempotent."""
        self.phase = SessionPhase.CLOSED
