"""Smart Place wire-protocol module.

Pure parsing, encoding, and connection-state logic for the Smart Place
WebSocket protocol. No I/O, no aiohttp, no Click. This module is shared
between the standalone client and the Home Assistant integration.
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


def _indexed_value_parser(name: str, prefix: str) -> Callable[[str], NamedValue]:
    """Build a parser for ``prefix<index>:<value>`` per-id pushes returning ``NamedValue``.

    Generalises across ids — one entry covers ``leuchte1``,
    ``leuchte13``, etc. ``index`` is the captured trailing-digit
    suffix; ``value`` may be empty.
    """
    pattern = re.compile(rf"^{re.escape(prefix)}(\d+):(.*)$", re.DOTALL)

    def parse(text: str) -> NamedValue:
        m = pattern.match(text)
        if m is None:
            raise ProtocolError(f"{prefix}<N>: frame malformed: {text!r}")
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


KNOWN_MESSAGES: Final[list[MessageDefinition]] = [
    # -- Discovery + routing -----------------------------------------------
    MessageDefinition(
        name="GoToLinkSSL",
        description=(
            "Discovery routing frame. Format: GoToLinkSSL:<host>:<port-or-port/path>:<token2>. "
            "The middle field is a bare port or port-with-path; the routed port is dynamic "
            "per session."
        ),
        pattern=re.compile(r"^GoToLinkSSL:"),
        parse=_parse_go_to_link_ssl,
        example="GoToLinkSSL:spr1.smartplace.ch:38435/Start1:Leer",
    ),
    # -- Bootstrap responses (singletons, sent once per session) -----------
    MessageDefinition(
        name="GlobalConfig",
        description=(
            "Response to GiveMeGlobalConfig — SPA display config. "
            "Format: EINSTELLUNGENGLOBAL>language>standby>brightness>screensaver_mode>"
            "screensaver_start>screensaver_duration. Brightness is a 0..1 float-as-string "
            "and screensaver_duration may be the literal 'undefined'."
        ),
        pattern=re.compile(r"^EINSTELLUNGENGLOBAL>"),
        parse=_parse_global_config,
        example="EINSTELLUNGENGLOBAL>2>300>0.8>1>300>undefined",
    ),
    MessageDefinition(
        name="InfoboardWidgets",
        description=(
            "Response to GiveStatusListe — ordered list of info-board widget labels "
            "the user has enabled (e.g. 'Wetter' for weather, 'Tagesverbrauch' for "
            "daily energy consumption). Does NOT enumerate devices; per-device "
            "pushes arrive separately."
        ),
        pattern=re.compile(r"^StatusListe(?:>|$)"),
        parse=_named_fields_parser("InfoboardWidgets", "StatusListe"),
        example="StatusListe>Wetter>Tagesverbrauch>",
    ),
    MessageDefinition(
        name="InfoboardEntry",
        description=(
            "Per-row info-board content entry. Format: "
            "StatusInhaltListe_<level>_<row>_SPtext<id>>... — internal "
            "structure mixes '_', '>' and '~' delimiters and is opaque "
            "here; value preserves the trailing payload verbatim."
        ),
        pattern=re.compile(r"^StatusInhaltListe_"),
        parse=_raw_value_parser("InfoboardEntry", "StatusInhaltListe_"),
        example="StatusInhaltListe_1_1_SPtext390>TEMPOUT~SPDB-REM>unit-C~>LinkOff",
    ),
    MessageDefinition(
        name="BasicInfos",
        description=(
            "Response to GiveMeBasicInfos — installation metadata "
            "(';'-delimited). Value contains the customer's SPID, creator "
            "email, initials, installation tag, creation date, and "
            "autofill setting — treat as PII; do not log raw."
        ),
        pattern=re.compile(r"^BasicInfos:"),
        parse=_named_value_parser("BasicInfos", "BasicInfos"),
        example="BasicInfos:0000000;creator@example.com;CRE;XX00-00-00;2020-01-01;AutoFillOn",
    ),
    MessageDefinition(
        name="LanguageOptions",
        description=(
            "Response: list of UI language options. Wire prefix conflicts with the "
            "dataclass name GlobalConfig — registry uses a distinct name. Format: "
            "GlobalConfig>1-deutsch<2-english<3-...<6-polski (note '<' delimiter "
            "inside the single field)."
        ),
        pattern=re.compile(r"^GlobalConfig>"),
        parse=_named_fields_parser("LanguageOptions", "GlobalConfig"),
        example="GlobalConfig>1-deutsch<2-english<3-francaise",
    ),
    MessageDefinition(
        name="GsaConfig",
        description=(
            "Response to GiveMeGlobalGsa — Yealink/SIP gateway, LAN IP, "
            "and additional config. Format: GlobalGsa>YEALINK_FLAG>IP>...>. "
            "Contains internal-network info; treat as sensitive."
        ),
        pattern=re.compile(r"^GlobalGsa>"),
        parse=_named_fields_parser("GsaConfig", "GlobalGsa"),
        example="GlobalGsa>YEALINKOFF>10.0.0.1>60>1^/linkmap1>",
    ),
    MessageDefinition(
        name="AllItems",
        description=(
            "Big bootstrap dump of all controllable items for a non-admin user. "
            "Wire prefix includes a literal space: 'AllItemsOhneAdmin :' followed "
            "by ';'-delimited per-type lists separated by ':' (Klimas:Leuchten:"
            "Jalousien:Szenen:...). Value preserves the raw trailing payload."
        ),
        pattern=re.compile(r"^AllItemsOhneAdmin :"),
        parse=_raw_value_parser("AllItems", "AllItemsOhneAdmin :"),
        example="AllItemsOhneAdmin :Klimas1=Name,10px,20px,...",
    ),
    # -- Counters (singletons) ---------------------------------------------
    MessageDefinition(
        name="InvoicesPendingCount",
        description="Count of invoices awaiting closure. Format: RechnungenAbschliessenCount:<n>.",
        pattern=re.compile(r"^RechnungenAbschliessenCount:"),
        parse=_named_value_parser("InvoicesPendingCount", "RechnungenAbschliessenCount"),
        example="RechnungenAbschliessenCount:0",
    ),
    MessageDefinition(
        name="OffersCount",
        description="Count of pending offers/quotations. Format: AngebotCount:<n>.",
        pattern=re.compile(r"^AngebotCount:"),
        parse=_named_value_parser("OffersCount", "AngebotCount"),
        example="AngebotCount:1",
    ),
    MessageDefinition(
        name="InvoicesCount",
        description="Count of invoices. Format: RechnungCount:<n>.",
        pattern=re.compile(r"^RechnungCount:"),
        parse=_named_value_parser("InvoicesCount", "RechnungCount"),
        example="RechnungCount:1",
    ),
    # -- Lifecycle markers --------------------------------------------------
    MessageDefinition(
        name="PongOK",
        description="Heartbeat reply to client-sent 'Ping'. Wire is just the prefix; fields=().",
        pattern=re.compile(r"^PongOK$"),
        parse=_named_fields_parser("PongOK", "PongOK"),
        example="PongOK",
    ),
    MessageDefinition(
        name="SocketConnectedFinished",
        description=(
            "Sent once after the client emits 'SocketConnected:<n>'. Payload is "
            "an internal Mojo transaction handle (perl backend), captured "
            "verbatim. Format: SocketConnectedFinished>Mojo::Transaction::"
            "WebSocket=HASH(0x...)."
        ),
        pattern=re.compile(r"^SocketConnectedFinished(?:>|$)"),
        parse=_named_fields_parser("SocketConnectedFinished", "SocketConnectedFinished"),
        example="SocketConnectedFinished>Mojo::Transaction::WebSocket=HASH(0x0)",
    ),
    MessageDefinition(
        name="MainMenuFinished",
        description="Marker: GiveMeMainmenu reply complete. Triggers next bootstrap step in the SPA.",
        pattern=re.compile(r"^GiveMeMainMenuFinished$"),
        parse=_named_fields_parser("MainMenuFinished", "GiveMeMainMenuFinished"),
        example="GiveMeMainMenuFinished",
    ),
    MessageDefinition(
        name="InfoboardContentFinished",
        description="Marker: StatusInhaltListe stream complete. Triggers GiveMeMainmenu in the SPA.",
        pattern=re.compile(r"^StatusInhaltFinishedListe$"),
        parse=_named_fields_parser("InfoboardContentFinished", "StatusInhaltFinishedListe"),
        example="StatusInhaltFinishedListe",
    ),
    # -- Weather + environment (singletons) --------------------------------
    MessageDefinition(
        name="OutdoorTemperature",
        description="Push: current outdoor temperature (degrees Celsius). Format: TEMPOUT:<value>.",
        pattern=re.compile(r"^TEMPOUT:"),
        parse=_named_value_parser("OutdoorTemperature", "TEMPOUT"),
        example="TEMPOUT:26.6",
    ),
    MessageDefinition(
        name="WindSpeed",
        description=(
            "Push: current wind speed (unit presumed km/h or m/s, not yet confirmed). "
            "Format: WINDGESCHWINDIGKEIT:<value>. 'Windgeschwindigkeit' = wind speed in German."
        ),
        pattern=re.compile(r"^WINDGESCHWINDIGKEIT:"),
        parse=_named_value_parser("WindSpeed", "WINDGESCHWINDIGKEIT"),
        example="WINDGESCHWINDIGKEIT:7.9",
    ),
    MessageDefinition(
        name="Rain",
        description="Push: rain alarm flag. Format: REGEN:<code> (00 = off).",
        pattern=re.compile(r"^REGEN:"),
        parse=_named_value_parser("Rain", "REGEN"),
        example="REGEN:00",
    ),
    MessageDefinition(
        name="Hail",
        description="Push: hail alarm flag. Format: HAGEL:<code> (00 = off).",
        pattern=re.compile(r"^HAGEL:"),
        parse=_named_value_parser("Hail", "HAGEL"),
        example="HAGEL:00",
    ),
    MessageDefinition(
        name="BlindsMaintenance",
        description=(
            "Push: blinds-maintenance flag. Format: JALWARTUNG:<code> (00 = off). "
            "'Jalousie-Wartung' = blinds maintenance in German."
        ),
        pattern=re.compile(r"^JALWARTUNG:"),
        parse=_named_value_parser("BlindsMaintenance", "JALWARTUNG"),
        example="JALWARTUNG:00",
    ),
    # -- Other singletons --------------------------------------------------
    MessageDefinition(
        name="PersonInfo",
        description="Push: presence/person info flag. Format: PERSINFO:<state>.",
        pattern=re.compile(r"^PERSINFO:"),
        parse=_named_value_parser("PersonInfo", "PERSINFO"),
        example="PERSINFO:Read",
    ),
    MessageDefinition(
        name="ApiToken",
        description=(
            "Push response to GiveMeToken — an opaque third-party API token. "
            "Format: TOKEN:<value>. Treat as a secret; do not log raw."
        ),
        pattern=re.compile(r"^TOKEN:"),
        parse=_named_value_parser("ApiToken", "TOKEN"),
        example="TOKEN:redacted",
    ),
    MessageDefinition(
        name="SpotifyToken",
        description=(
            "Push response containing Spotify auth context. Format: SPOTIFYTOKEN<...< "
            "(uses '<' as inner delimiter). Treat as a secret; do not log raw."
        ),
        pattern=re.compile(r"^SPOTIFYTOKEN<"),
        parse=_raw_value_parser("SpotifyToken", "SPOTIFYTOKEN<"),
        example="SPOTIFYTOKEN<GLOBAL<",
    ),
    MessageDefinition(
        name="MediacenterUpdate",
        description=(
            "Push: media-centre asset update (icon/picture changes). Format: MediacenterUpdateInfos>Type>Index>Asset."
        ),
        pattern=re.compile(r"^MediacenterUpdateInfos(?:>|$)"),
        parse=_named_fields_parser("MediacenterUpdate", "MediacenterUpdateInfos"),
        example="MediacenterUpdateInfos>Pic>81>graphics/transparent.png",
    ),
    # -- Per-id sensor pushes (single value) -------------------------------
    MessageDefinition(
        name="Temperature",
        description=(
            "Push: current indoor temperature reading from sensor N (°C as float). "
            "Format: TEMPIST<sensor>:<value>. TEMPIST = 'TEMPeratur IST' (actual)."
        ),
        pattern=re.compile(r"^TEMPIST\d+:"),
        parse=_parse_temperature,
        example="TEMPIST3:27.2",
    ),
    MessageDefinition(
        name="TemperatureSetpoint",
        description=(
            "Push: temperature setpoint for room/zone N (°C as float-as-string). "
            "Format: TEMPSOLL<n>:<value>. TEMPSOLL = 'TEMPeratur SOLL' (target)."
        ),
        pattern=re.compile(r"^TEMPSOLL\d+:"),
        parse=_indexed_value_parser("TemperatureSetpoint", "TEMPSOLL"),
        example="TEMPSOLL5:18",
    ),
    MessageDefinition(
        name="Humidity",
        description=(
            "Push: current humidity reading from sensor N (% as float-as-string). "
            "Format: FEUCHTEIST<n>:<value>. 'Feuchte IST' = actual humidity."
        ),
        pattern=re.compile(r"^FEUCHTEIST\d+:"),
        parse=_indexed_value_parser("Humidity", "FEUCHTEIST"),
        example="FEUCHTEIST5:0.0",
    ),
    MessageDefinition(
        name="ClimateInfo",
        description=(
            "Push: climate-zone N status payload (HVAC mode/fan-coil info). "
            "Format: KLIMASINFO<n>:<value> (value 'null' when unconfigured)."
        ),
        pattern=re.compile(r"^KLIMASINFO\d+:"),
        parse=_indexed_value_parser("ClimateInfo", "KLIMASINFO"),
        example="KLIMASINFO5:null",
    ),
    MessageDefinition(
        name="SceneState",
        description="Push: state of scene N. Format: SZENEN<n>:<code> (e.g. '00', '01').",
        pattern=re.compile(r"^SZENEN\d+:"),
        parse=_indexed_value_parser("SceneState", "SZENEN"),
        example="SZENEN10:00",
    ),
    MessageDefinition(
        name="LightState",
        description=(
            "Push: state/level of light N. Format: leuchte<n>:<value>. Value is "
            "an 8-bit dim level 0..255 for dimmers, or a state code (e.g. 'on'/"
            "'off') for switches."
        ),
        pattern=re.compile(r"^leuchte\d+:"),
        parse=_indexed_value_parser("LightState", "leuchte"),
        example="leuchte13:255",
    ),
    MessageDefinition(
        name="BlindState",
        description=(
            "Push: state/icon code for blind N. Format: JALICO<n>:<code> (e.g. '0-01'). 'JALICO' = Jalousie-Icon."
        ),
        pattern=re.compile(r"^JALICO\d+:"),
        parse=_indexed_value_parser("BlindState", "JALICO"),
        example="JALICO8:0-01",
    ),
    MessageDefinition(
        name="Volume",
        description="Push: volume level for speaker N (0..100). Format: Vol<n>:<value>.",
        pattern=re.compile(r"^Vol\d+:"),
        parse=_indexed_value_parser("Volume", "Vol"),
        example="Vol1:0",
    ),
    MessageDefinition(
        name="InfoboardSlot",
        description="Push: info-board slot N state (icon/visibility code). Format: INFOBOARD<n>:<code>.",
        pattern=re.compile(r"^INFOBOARD\d+:"),
        parse=_indexed_value_parser("InfoboardSlot", "INFOBOARD"),
        example="INFOBOARD1:01",
    ),
    MessageDefinition(
        name="PackageBox",
        description="Push: package-box N occupancy. Format: PACKETBOX<n>:<state> ('Frei' = free).",
        pattern=re.compile(r"^PACKETBOX\d+:"),
        parse=_indexed_value_parser("PackageBox", "PACKETBOX"),
        example="PACKETBOX1:Frei",
    ),
    MessageDefinition(
        name="ChartTarget",
        description="Push: target/goal value for chart N. Format: CHARTZIEL<n>:<value>.",
        pattern=re.compile(r"^CHARTZIEL\d+:"),
        parse=_indexed_value_parser("ChartTarget", "CHARTZIEL"),
        example="CHARTZIEL337:300",
    ),
    MessageDefinition(
        name="WindAlarm",
        description="Push: wind-alarm zone N flag. Format: WINDALARM<n>:<code> (00 = off).",
        pattern=re.compile(r"^WINDALARM\d+:"),
        parse=_indexed_value_parser("WindAlarm", "WINDALARM"),
        example="WINDALARM1:00",
    ),
    MessageDefinition(
        name="LightsCentral",
        description="Push: aggregate state for light group N. Format: LEUCHTENZENTRAL<n>:<code>.",
        pattern=re.compile(r"^LEUCHTENZENTRAL\d+:"),
        parse=_indexed_value_parser("LightsCentral", "LEUCHTENZENTRAL"),
        example="LEUCHTENZENTRAL1:00",
    ),
    MessageDefinition(
        name="BlindsCentral",
        description="Push: aggregate state for blind group N. Format: JALZENTRAL<n>:<code> (may be empty).",
        pattern=re.compile(r"^JALZENTRAL\d+:"),
        parse=_indexed_value_parser("BlindsCentral", "JALZENTRAL"),
        example="JALZENTRAL1:",
    ),
    MessageDefinition(
        name="SpeakersCentral",
        description="Push: aggregate state for speaker group N. Format: LAUTSPRECHERZENTRAL<n>:<code>.",
        pattern=re.compile(r"^LAUTSPRECHERZENTRAL\d+:"),
        parse=_indexed_value_parser("SpeakersCentral", "LAUTSPRECHERZENTRAL"),
        example="LAUTSPRECHERZENTRAL1:00",
    ),
    MessageDefinition(
        name="Mute",
        description="Push: mute state for audio zone N. Format: MUTE<n>:<code>.",
        pattern=re.compile(r"^MUTE\d+:"),
        parse=_indexed_value_parser("Mute", "MUTE"),
        example="MUTE1:00",
    ),
    MessageDefinition(
        name="DoorIntercom",
        description=(
            "Push: door intercom N state. Format: SPRECHEN<n>:<state> "
            "('ring' on incoming call). 'Sprechen' = to speak/intercom."
        ),
        pattern=re.compile(r"^SPRECHEN\d+:"),
        parse=_indexed_value_parser("DoorIntercom", "SPRECHEN"),
        example="SPRECHEN1:ring",
    ),
    MessageDefinition(
        name="CallInfo",
        description="Push: caller location/info for intercom N. Format: CALLINFO<n>:<label>.",
        pattern=re.compile(r"^CALLINFO\d+:"),
        parse=_indexed_value_parser("CallInfo", "CALLINFO"),
        example="CALLINFO1:FrontDoor",
    ),
    # -- Per-id entity configuration (comma-delimited) ---------------------
    MessageDefinition(
        name="LightConfig",
        description=(
            "Light entity definition N. Format: INHALTLeuchten<n>:name,x,y,kind,"
            "?,dim-curve,view,?,memory-flag. Sent once per light during bootstrap."
        ),
        pattern=re.compile(r"^INHALTLeuchten\d+:"),
        parse=_indexed_fields_parser("LightConfig", "INHALTLeuchten"),
        example="INHALTLeuchten13:Corridor lights,289px,330px,schalter,,dimmkurve1,Uebersicht1,,MemoryOFF",
    ),
    MessageDefinition(
        name="BlindConfig",
        description=(
            "Blind/shutter entity definition N. Format: INHALTJalousien<n>:name,x,y,"
            "kind,?,speed,view. Sent once per blind during bootstrap."
        ),
        pattern=re.compile(r"^INHALTJalousien\d+:"),
        parse=_indexed_fields_parser("BlindConfig", "INHALTJalousien"),
        example="INHALTJalousien8:Office blinds,674px,405px,jalousie,,60,Uebersicht1",
    ),
    MessageDefinition(
        name="ClimateConfig",
        description=(
            "Climate-zone definition N. Format: INHALTKlimas<n>:name,x,y,mode,?,"
            "color,view,fancoil-flag. Sent once per zone during bootstrap."
        ),
        pattern=re.compile(r"^INHALTKlimas\d+:"),
        parse=_indexed_fields_parser("ClimateConfig", "INHALTKlimas"),
        example="INHALTKlimas5:Bathroom heating,108px,569px,Heizen,,rgb(0- 0- 0),Uebersicht1,FanCoilOff",
    ),
    MessageDefinition(
        name="SceneConfig",
        description=(
            "Scene definition N. Format: INHALTSZENEN<n>:name,x,y,icon,onstate,"
            "removable,?,weekdays,trigger-time,sunrise-flag,sunset-flag,...,form."
        ),
        pattern=re.compile(r"^INHALTSZENEN\d+:"),
        parse=_indexed_fields_parser("SceneConfig", "INHALTSZENEN"),
        example="INHALTSZENEN10:Evening,250px,535px,0.png,OnOn,Remove,,mo-di,19-45,...,SzenenForm1",
    ),
    MessageDefinition(
        name="MediacenterConfig",
        description="Media-centre definition N. Format: INHALTMediacenter<n>:name,x,y,?,?,view,color.",
        pattern=re.compile(r"^INHALTMediacenter\d+:"),
        parse=_indexed_fields_parser("MediacenterConfig", "INHALTMediacenter"),
        example="INHALTMediacenter1:Mediacenter,459px,434px,,,Uebersicht1,rgba(255-164-65-0.9)",
    ),
    MessageDefinition(
        name="MediaPanelConfig",
        description="Media-panel definition N. Format: INHALTMediaPanel<n>:name,x,y,removable,onstate,view.",
        pattern=re.compile(r"^INHALTMediaPanel\d+:"),
        parse=_indexed_fields_parser("MediaPanelConfig", "INHALTMediaPanel"),
        example="INHALTMediaPanel1:Main panel,369px,429px,NoRemove,SollMediaOn,Uebersicht1",
    ),
    MessageDefinition(
        name="VolumeConfig",
        description=(
            "Volume-control definition N (which speaker zones the slider drives). "
            "Format: INHALTVol<n>:name,view-positions (positions use '?' between "
            "alternatives)."
        ),
        pattern=re.compile(r"^INHALTVol\d+:"),
        parse=_indexed_fields_parser("VolumeConfig", "INHALTVol"),
        example="INHALTVol1:Main Wohnen,Uebersicht2-327px-508px?Uebersicht1-213px-129px",
    ),
    MessageDefinition(
        name="LightSubMenu",
        description="Light sub-menu definition N. Format: UnterMenuLeuchten<n>:name,x,y,kind,onstate,group-msg,view.",
        pattern=re.compile(r"^UnterMenuLeuchten\d+:"),
        parse=_indexed_fields_parser("LightSubMenu", "UnterMenuLeuchten"),
        example="UnterMenuLeuchten1:All,70px,10px,Leuchten,OnOn,LEUCHTENZENTRAL1,Uebersicht1",
    ),
    MessageDefinition(
        name="BlindSubMenu",
        description="Blind sub-menu definition N. Format: UnterMenuJalousien<n>:name,x,y,kind,onstate,group-msg,view.",
        pattern=re.compile(r"^UnterMenuJalousien\d+:"),
        parse=_indexed_fields_parser("BlindSubMenu", "UnterMenuJalousien"),
        example="UnterMenuJalousien1:All,70px,10px,Jalousien,OffOff,JALZENTRAL1,Uebersicht1",
    ),
    MessageDefinition(
        name="SpeakerSubMenu",
        description="Speaker sub-menu definition N. Format: UnterMenuLautsprecher<n>:name,x,y,kind,onstate,group-msg,?,view.",
        pattern=re.compile(r"^UnterMenuLautsprecher\d+:"),
        parse=_indexed_fields_parser("SpeakerSubMenu", "UnterMenuLautsprecher"),
        example="UnterMenuLautsprecher1:All,70px,820px,Lautsprecher,OnOn,LAUTSPRECHERZENTRAL1,,Uebersicht1",
    ),
    MessageDefinition(
        name="QuickStartTile",
        description=("Quick-start tile N for the home screen. Format: IndividuellStart<n>:label,kind,target-id,view."),
        pattern=re.compile(r"^IndividuellStart\d+:"),
        parse=_indexed_fields_parser("QuickStartTile", "IndividuellStart"),
        example="IndividuellStart1:Szenen,Szenen,1,Start1",
    ),
    MessageDefinition(
        name="Floorplan",
        description="Floor-plan view definition N. Format: Floorplan<n>:label,x,y,plan-name.",
        pattern=re.compile(r"^Floorplan\d+:"),
        parse=_indexed_fields_parser("Floorplan", "Floorplan"),
        example="Floorplan1:HH77-14-01,70px,10px,Leuchten-Jalousien-Klima",
    ),
    # -- Charts (per-id, opaque values) ------------------------------------
    MessageDefinition(
        name="ChartPointUpdate",
        description=(
            "Push: single data point for chart N. Format: "
            "StandsSingelChartUpdate<n>:STAND<series>:<value>. The "
            "':STAND<series>:<value>' tail is opaque; value preserves it verbatim."
        ),
        pattern=re.compile(r"^StandsSingelChartUpdate\d+:"),
        parse=_indexed_value_parser("ChartPointUpdate", "StandsSingelChartUpdate"),
        example="StandsSingelChartUpdate336:STAND99:188968",
    ),
    MessageDefinition(
        name="ChartDefinition",
        description=(
            "Chart definition N (axis labels, series, units, decimals, ...). "
            "Format: SingelDiagramm<n>:<';'-delimited descriptor>. Internal "
            "format opaque; value preserves the trailing payload verbatim."
        ),
        pattern=re.compile(r"^SingelDiagramm\d+:"),
        parse=_indexed_value_parser("ChartDefinition", "SingelDiagramm"),
        example="SingelDiagramm336:Kaltwasser;Area;Zeit;Verbrauch;...",
    ),
    MessageDefinition(
        name="ChartStand",
        description=(
            "Chart-stand value for chart N, series M. Format: "
            "CHART<chartId>STAND<seriesId>:<value>. The chartId is the "
            "leading-digit suffix to 'CHART'; value preserves the seriesId "
            "and reading verbatim ('STAND<m>:<value>' shape lost)."
        ),
        pattern=re.compile(r"^CHART\d+STAND\d+:"),
        parse=_raw_value_parser("ChartStand", "CHART"),
        example="CHART337STAND1:52",
    ),
    MessageDefinition(
        name="ChartSumResponse",
        description=(
            "Reply to GiveMeChartSummeWasGenau<id> — which datasource a chart "
            "sums. Format: GiveMeChartSummeWasGenauBack>category>chartId."
        ),
        pattern=re.compile(r"^GiveMeChartSummeWasGenauBack(?:>|$)"),
        parse=_named_fields_parser("ChartSumResponse", "GiveMeChartSummeWasGenauBack"),
        example="GiveMeChartSummeWasGenauBack>Wasser>337",
    ),
    # -- One-off shapes ----------------------------------------------------
    MessageDefinition(
        name="PlaySlot",
        description=(
            "Media-player track update for slot N. Format: PLAYSLOT-<n><timestamp><label><image><url> "
            "(uses '<' as delimiter)."
        ),
        pattern=re.compile(r"^PLAYSLOT-\d+<"),
        parse=_raw_value_parser("PlaySlot", "PLAYSLOT-"),
        example="PLAYSLOT-1<0<Stream<icon.png<http://example/stream.mp3",
    ),
]
"""All known server frame shapes, in identification order.

``parse_frame`` returns the first definition whose ``pattern``
matches, so more-specific patterns appear before more-general ones.
Per-id entries use ``\\d+`` in the pattern (e.g. ``^TEMPIST\\d+:``)
so one entry generalises across all ids of a uniform shape.
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
