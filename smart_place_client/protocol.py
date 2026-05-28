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
class GoToLinkOldSystem:
    """Discovery routing frame redirecting to the legacy server."""

    token2: str

    @property
    def legacy_url(self) -> str:
        """HTTPS URL the browser would load on the legacy server."""
        return f"https://{LEGACY_HOST}:{LEGACY_PORT}/Start2?{self.token2}"


@dataclass(frozen=True, slots=True)
class HostNotOnline:
    """Discovery-WS error: the user's installation is offline."""


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
    """Per-sensor indoor temperature push (TEMPIST<sensor>:<value>)."""

    sensor: int
    value: str


@dataclass(frozen=True, slots=True)
class MediacenterUpdateInfos:
    """Media/multiroom status update; raw text until field semantics are confirmed."""

    raw: str


@dataclass(frozen=True, slots=True)
class Marker:
    """Generic placeholder for a no-payload server frame.

    Used for the many ``*Finished`` / list-terminator frames whose entire
    semantic is "this transmission completed" — the wire shape carries no
    data, so a typed class per shape would be 25 lines of boilerplate for
    no field. ``name`` distinguishes them; the registry's
    :class:`MessageDefinition` carries the description / source.

    Dispatchers can branch on ``isinstance(frame, Marker) and frame.name == "..."``
    or by mapping ``frame.name`` to a handler table.
    """

    name: str


@dataclass(frozen=True, slots=True)
class NamedValue:
    """Generic ``prefix:<value>`` server frame, identified by ``name``.

    Used for sensor / status pushes that carry a single string value.
    The wire shape is uniform (``prefix:value``) so one class plus a
    ``name`` discriminator covers any number of registry entries —
    OutdoorTemperature, WindSpeed, and future similar shapes — without
    a dedicated dataclass per shape.
    """

    name: str
    value: str


@dataclass(frozen=True, slots=True)
class NamedFields:
    """Generic ``prefix>f1>f2>...`` server frame, identified by ``name``.

    Used for SPA replies whose payload is a ``>``-delimited list of
    string fields. The wire shape is uniform (``prefix>fields``) so
    one class plus a ``name`` discriminator covers any number of
    registry entries — InfoboardWidgets, InfoboardContent, etc. —
    without a dedicated dataclass per shape.
    """

    name: str
    fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class UnknownFrame:
    """Catch-all for text frames the registry doesn't recognise yet.

    The dispatch layer logs (token-redacted) and moves on; in live mode
    the raw frame is also appended to ``output/unknown_frames.ndjson``
    for later analysis.
    """

    raw: str


ServerFrame = (
    GoToLinkSSL
    | GoToLinkOldSystem
    | HostNotOnline
    | GlobalConfig
    | Temperature
    | MediacenterUpdateInfos
    | Marker
    | NamedValue
    | NamedFields
    | UnknownFrame
)
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


def _parse_go_to_link_old_system(text: str) -> GoToLinkOldSystem:
    """Parse ``GoToLinkOLDSYSTEM:<token2>``."""
    _, _, token2 = text.partition(":")
    if not token2:
        raise ProtocolError("GoToLinkOLDSYSTEM frame missing token2")
    return GoToLinkOldSystem(token2=token2)


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
    return Temperature(sensor=int(m.group(1)), value=m.group(2))


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


def _marker_parser(name: str) -> Callable[[str], Marker]:
    """Build a parser for a marker frame whose only data is its identifier."""
    return lambda _text: Marker(name=name)


def _named_value_parser(name: str, prefix: str) -> Callable[[str], NamedValue]:
    """Build a parser for ``prefix:<value>`` frames returning ``NamedValue(name, value)``."""

    def parse(text: str) -> NamedValue:
        if not text.startswith(prefix + ":"):
            raise ProtocolError(f"frame missing prefix {prefix!r}: {text!r}")
        value = text[len(prefix) + 1 :]
        if not value:
            raise ProtocolError(f"{prefix} frame missing value: {text!r}")
        return NamedValue(name=name, value=value)

    return parse


def _named_fields_parser(name: str, prefix: str) -> Callable[[str], NamedFields]:
    """Build a parser for ``prefix[>f1>f2>...]`` frames returning ``NamedFields(name, fields)``."""
    return lambda text: NamedFields(name=name, fields=_parse_fields_after_prefix(text, prefix))


KNOWN_MESSAGES: Final[list[MessageDefinition]] = [
    MessageDefinition(
        name="HostNotOnline",
        description=(
            "Discovery-WS error: the user's installation is offline (spr1 cannot "
            "reach their home server). Surfaces as SmartPlaceAuthError so the HA "
            "integration can mark entities unavailable. Seen in the Start5 page "
            "JavaScript; not yet observed as a live frame."
        ),
        pattern=re.compile(r"^HostNotOnline$"),
        parse=lambda _text: HostNotOnline(),
        example="HostNotOnline",
    ),
    MessageDefinition(
        name="GoToLinkSSL",
        description=(
            "Modern SSL routing frame. Format: GoToLinkSSL:<host>:<port-or-port/path>:<token2>. "
            "The middle field is a bare port or port-with-path; the routed port is dynamic "
            "per session (DESIGN §10). Live capture 2026-05-28 saw "
            "GoToLinkSSL:spr1.smartplace.ch:<port>/Start1:Leer."
        ),
        pattern=re.compile(r"^GoToLinkSSL:"),
        parse=_parse_go_to_link_ssl,
        example="GoToLinkSSL:spr1.smartplace.ch:38435/Start1:Leer",
    ),
    MessageDefinition(
        name="GoToLinkOldSystem",
        description=(
            "Legacy server routing. Format: GoToLinkOLDSYSTEM:<token2>. Redirects "
            "to spr0.smartplace.ch:8770/Start2?<token2>. Seen in javallg.js "
            "but not yet observed as a live frame."
        ),
        pattern=re.compile(r"^GoToLinkOLDSYSTEM:"),
        parse=_parse_go_to_link_old_system,
        example="GoToLinkOLDSYSTEM:legacy-token-xyz",
    ),
    MessageDefinition(
        name="GlobalConfig",
        description=(
            "Bootstrap-read response to GiveMeGlobalConfig — SPA display config. "
            "Format: EINSTELLUNGENGLOBAL>language>standby>brightness>screensaver_mode>"
            "screensaver_start>screensaver_duration. Values are wire-typed strings; "
            "brightness is a 0..1 float-as-string and screensaver_duration may be the "
            "literal 'undefined' (live capture 2026-05-28)."
        ),
        pattern=re.compile(r"^EINSTELLUNGENGLOBAL>"),
        parse=_parse_global_config,
        example="EINSTELLUNGENGLOBAL>2>300>0.8>1>300>undefined",
    ),
    MessageDefinition(
        name="InfoboardWidgets",
        description=(
            "Bootstrap-read response to GiveStatusListe (wire prefix: StatusListe). "
            "Ordered list of info-board widget labels the user has enabled in the "
            "SPA — e.g. 'Wetter' for the weather widget, 'Tagesverbrauch' for daily "
            "energy consumption. Sent once per session after the routed page loads; "
            "the field count reflects the user's configured widget set (three "
            "fields seen in the Phase 3 capture). Does NOT enumerate devices — "
            "per-device pushes (Temperature, OutdoorTemperature, ...) arrive "
            "separately on the same WS."
        ),
        pattern=re.compile(r"^StatusListe(?:>|$)"),
        parse=_named_fields_parser("InfoboardWidgets", "StatusListe"),
        example="StatusListe>Wetter>Tagesverbrauch>",
    ),
    MessageDefinition(
        name="InfoboardContent",
        description=(
            "Content rows for one info-board widget (wire prefix: StatusInhaltListe). "
            "Format: StatusInhaltListe[>f1>f2>...]. Per-field semantics "
            "unconfirmed. Not yet live-captured."
        ),
        pattern=re.compile(r"^StatusInhaltListe(?:>|$)"),
        parse=_named_fields_parser("InfoboardContent", "StatusInhaltListe"),
        example="StatusInhaltListe>1>2",
    ),
    MessageDefinition(
        name="StatusInhaltFinishedListe",
        description=("Marker: full status dashboard content list has been sent. Not yet live-captured."),
        pattern=re.compile(r"^StatusInhaltFinishedListe$"),
        parse=_marker_parser("StatusInhaltFinishedListe"),
        example="StatusInhaltFinishedListe",
    ),
    MessageDefinition(
        name="StatusLinkInhaltFinishedListe",
        description=("Marker: full status-link/detail content for one tile has been sent. Not yet live-captured."),
        pattern=re.compile(r"^StatusLinkInhaltFinishedListe$"),
        parse=_marker_parser("StatusLinkInhaltFinishedListe"),
        example="StatusLinkInhaltFinishedListe",
    ),
    MessageDefinition(
        name="AdminMainmenuFinished",
        description=("Marker: admin main-menu / device inventory has finished loading. Not yet live-captured."),
        pattern=re.compile(r"^GiveMeAdminMainmenuFinished$"),
        parse=_marker_parser("AdminMainmenuFinished"),
        example="GiveMeAdminMainmenuFinished",
    ),
    MessageDefinition(
        name="CheckLeuchtenValuesFinished",
        description=("Marker: light/output state check batch has finished. Not yet live-captured."),
        pattern=re.compile(r"^CheckLeuchtenValuesFinished$"),
        parse=_marker_parser("CheckLeuchtenValuesFinished"),
        example="CheckLeuchtenValuesFinished",
    ),
    MessageDefinition(
        name="CheckJalousienValuesFinished",
        description=("Marker: cover/blind state check batch has finished. Not yet live-captured."),
        pattern=re.compile(r"^CheckJalousienValuesFinished$"),
        parse=_marker_parser("CheckJalousienValuesFinished"),
        example="CheckJalousienValuesFinished",
    ),
    MessageDefinition(
        name="CheckKlimasValuesFinished",
        description=("Marker: climate state check batch has finished. Not yet live-captured."),
        pattern=re.compile(r"^CheckKlimasValuesFinished$"),
        parse=_marker_parser("CheckKlimasValuesFinished"),
        example="CheckKlimasValuesFinished",
    ),
    MessageDefinition(
        name="CheckLautsprecherValuesFinished",
        description=("Marker: speaker/audio state check batch has finished. Not yet live-captured."),
        pattern=re.compile(r"^CheckLautsprecherValuesFinished$"),
        parse=_marker_parser("CheckLautsprecherValuesFinished"),
        example="CheckLautsprecherValuesFinished",
    ),
    MessageDefinition(
        name="ReloadSensorFinished",
        description=("Marker: sensor metadata/state reload has finished. Not yet live-captured."),
        pattern=re.compile(r"^ReloadSensorFinished$"),
        parse=_marker_parser("ReloadSensorFinished"),
        example="ReloadSensorFinished",
    ),
    MessageDefinition(
        name="SzenenReloadFinished",
        description=("Marker: scene metadata/state reload has finished. Not yet live-captured."),
        pattern=re.compile(r"^SzenenReloadFinished$"),
        parse=_marker_parser("SzenenReloadFinished"),
        example="SzenenReloadFinished",
    ),
    MessageDefinition(
        name="GlobalAnbindungenBack",
        description=(
            "Configured integration/connection payload with opaque >-delimited fields. "
            "Payload may contain private integration details — kept opaque. Not yet live-captured."
        ),
        pattern=re.compile(r"^GlobalAnbindungenBack(?:>|$)"),
        parse=_named_fields_parser("GlobalAnbindungenBack", "GlobalAnbindungenBack"),
        example="GlobalAnbindungenBack>1>name",
    ),
    MessageDefinition(
        name="GlobalAnbindungenBackFinish",
        description=("Marker: configured integration/connection list has finished loading. Not yet live-captured."),
        pattern=re.compile(r"^GlobalAnbindungenBackFinish$"),
        parse=_marker_parser("GlobalAnbindungenBackFinish"),
        example="GlobalAnbindungenBackFinish",
    ),
    MessageDefinition(
        name="GiveMeAPIFuerBack",
        description=(
            "API-detail payload for one configured integration. "
            "Payload may contain private integration details — kept opaque. Not yet live-captured."
        ),
        pattern=re.compile(r"^GiveMeAPIFuerBack(?:>|$)"),
        parse=_named_fields_parser("GiveMeAPIFuerBack", "GiveMeAPIFuerBack"),
        example="GiveMeAPIFuerBack>1>value",
    ),
    MessageDefinition(
        name="GiveMeAPIAnbindungsInfosBack",
        description=(
            "API binding-info payload for one Smart Place Manager integration. "
            "Payload may contain private integration details — kept opaque. Not yet live-captured."
        ),
        pattern=re.compile(r"^GiveMeAPIAnbindungsInfosBack(?:>|$)"),
        parse=_named_fields_parser(
            "GiveMeAPIAnbindungsInfosBack",
            "GiveMeAPIAnbindungsInfosBack",
        ),
        example="GiveMeAPIAnbindungsInfosBack>1>value",
    ),
    MessageDefinition(
        name="MediacenterUpdateInfos",
        description=(
            "Media/multiroom playback or service-state update. Wire-frame "
            "delimiter and per-field semantics unconfirmed — kept as raw text. "
            "Not yet live-captured."
        ),
        pattern=re.compile(r"^MediacenterUpdateInfos"),
        parse=lambda text: MediacenterUpdateInfos(raw=text),
        example="MediacenterUpdateInfos>1>playing",
    ),
    MessageDefinition(
        name="Temperature",
        description=(
            "Push: current temperature reading from indoor sensor N (degrees "
            "Celsius as a float-as-string). Format: TEMPIST<sensor>:<value>. "
            "TEMPIST = TEMPeratur IST (current/actual temperature in German). "
            "One registry entry handles all sensors observed (TEMPIST1, "
            "TEMPIST2, TEMPIST3, TEMPIST6, ...)."
        ),
        pattern=re.compile(r"^TEMPIST\d+:"),
        parse=_parse_temperature,
        example="TEMPIST3:27.2",
    ),
    MessageDefinition(
        name="OutdoorTemperature",
        description=("Push: current outdoor temperature reading (degrees Celsius). Format: TEMPOUT:<value>."),
        pattern=re.compile(r"^TEMPOUT:"),
        parse=_named_value_parser("OutdoorTemperature", "TEMPOUT"),
        example="TEMPOUT:26.6",
    ),
    MessageDefinition(
        name="WindSpeed",
        description=(
            "Push: current wind-speed reading (unit presumed km/h or m/s, not "
            "yet confirmed). Format: WINDGESCHWINDIGKEIT:<value>. "
            "'Windgeschwindigkeit' = wind speed in German."
        ),
        pattern=re.compile(r"^WINDGESCHWINDIGKEIT:"),
        parse=_named_value_parser("WindSpeed", "WINDGESCHWINDIGKEIT"),
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
    """Encode the ``GiveMeGlobalConfig`` bootstrap-read."""
    return GLOBAL_CONFIG_REQUEST


def encode_status_liste_request() -> str:
    """Encode the ``GiveStatusListe`` bootstrap-read."""
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
    - ``global_config`` and ``infoboard_widgets`` are populated by the
      bootstrap reads.
    - The state machine doesn't own the WebSocket; the I/O layer in
      ``client.py`` does. This object is plain data so it is trivial to
      test from ``test_protocol.py``.
    """

    phase: SessionPhase = SessionPhase.DISCOVERY_OPEN
    route: GoToLinkSSL | None = None
    global_config: GlobalConfig | None = None
    infoboard_widgets: NamedFields | None = None  # name == "InfoboardWidgets"

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
        elif isinstance(frame, NamedFields) and frame.name == "InfoboardWidgets":
            self.infoboard_widgets = frame

        if self.phase is SessionPhase.APP_OPEN and self.global_config and self.infoboard_widgets:
            self.phase = SessionPhase.BOOTSTRAPPED
        elif self.phase is SessionPhase.BOOTSTRAPPED and self.global_config and self.infoboard_widgets:
            self.phase = SessionPhase.READY
        return self.phase

    def close(self) -> None:
        """Mark the session as closed; idempotent."""
        self.phase = SessionPhase.CLOSED
