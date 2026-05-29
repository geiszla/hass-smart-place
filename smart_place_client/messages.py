"""Smart Place message registry + dispatch.

One :class:`MessageDefinition` per known wire-frame shape, plus the
:func:`parse_frame` dispatcher that walks the registry. Kept separate
from :mod:`smart_place_client.protocol` so this list can grow without
bloating the type/parser module.

Adding a new shape: append a ``MessageDefinition`` here using one of
the helper parsers in :mod:`~smart_place_client.protocol`
(``_named_value_parser``, ``_named_fields_parser``,
``_indexed_value_parser``, ``_indexed_fields_parser``,
``_raw_value_parser``). The example string must match the entry's
own pattern — :mod:`tests.test_protocol` enforces this.
"""

from __future__ import annotations

import re
from typing import Final

from smart_place_client.protocol import (
    MessageDefinition,
    ServerFrame,
    UnknownFrame,
    _indexed_fields_parser,
    _indexed_value_parser,
    _named_fields_parser,
    _named_value_parser,
    _parse_global_config,
    _parse_go_to_link_ssl,
    _parse_temperature,
    _raw_value_parser,
)

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
        description=(
            "Marker: end of the InfoboardEntry response stream "
            "(reply to ``Commands.InfoboardContent``). Triggers "
            "``GiveMeMainmenu`` in the SPA."
        ),
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
        # Must precede InfoboardSlot — both start with INFOBOARD\d+ but this
        # one has the literal 'INHALT' between digits and colon.
        name="InfoboardContent",
        description=(
            "Push: rendered HTML/text content for info-board slot N. Format: "
            "INFOBOARD<n>INHALT:<body> ('Read' = clear/acknowledge marker)."
        ),
        pattern=re.compile(r"^INFOBOARD\d+INHALT:"),
        parse=_indexed_value_parser("InfoboardContent", "INFOBOARD", separator="INHALT:"),
        example="INFOBOARD2INHALT:Read",
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

:func:`parse_frame` returns the first definition whose ``pattern``
matches, so more-specific patterns appear before more-general ones.
Per-id entries use ``\\d+`` in the pattern (e.g. ``^TEMPIST\\d+:``)
so one entry generalises across all ids of a uniform shape.
"""


def parse_frame(text: str) -> ServerFrame:
    """Parse a text frame from the server.

    Walks :data:`KNOWN_MESSAGES` in declaration order and returns the
    first matching frame. Unknown shapes return
    :class:`~smart_place_client.protocol.UnknownFrame` so the dispatch
    layer can log + skip + record for later analysis rather than
    crashing.
    """
    for defn in KNOWN_MESSAGES:
        if defn.pattern.match(text):
            return defn.parse(text)
    return UnknownFrame(raw=text)
