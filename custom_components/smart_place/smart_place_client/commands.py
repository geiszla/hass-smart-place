"""Smart Place client→server command registry.

Mirror of :mod:`smart_place_client.messages` for the outgoing
direction. One :class:`CommandDefinition` per known wire command,
grouped under the :class:`Commands` namespace so the full surface
is greppable from a single place. Each definition pairs a
human-readable description with the encoder that produces the
wire string.

Adding a new command: append a class attribute on :class:`Commands`
with a :class:`CommandDefinition`. For static payloads use
:func:`_static`; for parameterized ones (e.g. an embedded id),
pass a typed encoder function that checks its arguments and returns
the wire string, plus ``example_args`` that produce ``example``.

Per the ``smart-place-observe-only`` memory: declaring a command
here does not auto-issue it. The library only sends when a caller
explicitly invokes ``client.issue(Commands.X, ...)``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import re
from typing import Final

# Vendor ranges, from the vendor JavaScript: dim level is one byte, the
# heating setpoint slider runs 18..26 (18 = forced off, 26 = forced on).
LIGHT_LEVEL_MAX: Final = 255
SETPOINT_OFF: Final = 18
SETPOINT_MAX: Final = 26

# Device families a command can name (``CommandDefinition.device``).
DEVICE_LIGHT: Final = "light"
DEVICE_BLIND: Final = "blind"

# The lights sub-menu names its own command (``UnterMenuLeuchten<n>:...,
# LEUCHTENZENTRAL<n>,...``). It comes from the server, so only this shape
# is ever sent back.
_LIGHTS_GROUP_MESSAGE_RE = re.compile(r"LEUCHTENZENTRAL\d+")

# A blinds sub-menu other than the plan-wide one names its own command the
# same way. No such sub-menu exists in the reference installation, so its
# exact shape is not known: any frame name of the blinds family may go back.
_BLINDS_GROUP_MESSAGE_RE = re.compile(r"JAL[A-Z]*\d+")


@dataclass(frozen=True, slots=True)
class CommandDefinition:
    """One outgoing wire command.

    Attributes:
        name: CamelCase identifier; matches the attribute name on
            :class:`Commands`.
        description: Plain-English meaning + when it's sent + what
            response it triggers, if any. Read by future-us when
            debugging the wire protocol.
        encode: Callable that returns the wire string. Takes no
            args for static payloads, takes the parameters for
            parameterized commands (e.g. ``encode(chart_id)``).
        example: Concrete wire string used as documentation +
            verified by :func:`test_commands_encode_matches_example`.
        example_args: Arguments for which ``encode`` returns ``example``.
        write: Whether the command changes something in the building
            (lights, blinds, heating, scenes, doors). Defaults to ``True``
            so a new command is treated as a write unless declared a
            read; ``client.issue`` withholds writes in dry-run mode.
        changes_others: Whether the command changes devices that have
            entities of their own (a scene, a group button). Their values
            follow a moment later, so ``client.issue`` starts the settle
            time during which no toggle is decided (``expected.SettleTime``).
        device: The device family whose vendor id is the first argument
            (``DEVICE_LIGHT`` covers lights and sockets, ``DEVICE_BLIND``
            blinds); ``None`` for a command that names no single device
            of such a family. Lets a caller tell which device a command
            is for (``protection.command_is_protected``).
        audit: Whether issuing this command is logged at INFO as an
            audit-trail line (``client.issue``). Defaults to ``True`` so
            new commands are recorded by default — the audit log should
            fail safe to *more* visibility. The high-frequency machinery
            (bootstrap reads, heartbeat ``Ping``, chart polling) sets it
            ``False`` so the trail isn't drowned out; control/actuation
            commands (e.g. the door openers) keep the default.
    """

    name: str
    description: str
    encode: Callable[..., str]
    example: str
    audit: bool = True
    example_args: tuple[object, ...] = ()
    write: bool = True
    changes_others: bool = False
    device: str | None = None


def _static(wire: str) -> Callable[[], str]:
    """Return a no-arg encoder that always emits ``wire`` verbatim."""

    def encode() -> str:
        return wire

    return encode


def _device_id(value: int, what: str) -> int:
    """Return ``value`` if it is a usable vendor id, else raise ``ValueError``."""
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{what} must be a positive whole number, got {value!r}")
    return value


def _in_range(value: int, low: int, high: int, what: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ValueError(f"{what} must be a whole number {low}..{high}, got {value!r}")
    return value


def _encode_light_toggle(light_id: int) -> str:
    return f"leuchte{_device_id(light_id, 'light id')}"


def _encode_light_dim(light_id: int, level: int) -> str:
    return f"DIMleuchte{_device_id(light_id, 'light id')}:{_in_range(level, 0, LIGHT_LEVEL_MAX, 'dim level')}"


def _encode_blind_up(blind_id: int) -> str:
    return f"JALUP{_device_id(blind_id, 'blind id')}"


def _encode_blind_down(blind_id: int) -> str:
    return f"JALDOW{_device_id(blind_id, 'blind id')}"


def _encode_blind_vent(blind_id: int) -> str:
    return f"JALLUE{_device_id(blind_id, 'blind id')}"


def _encode_blinds_group_up(plan_id: int) -> str:
    return f"JALUPZENTRAL{_device_id(plan_id, 'plan id')}"


def _encode_blinds_group_down(plan_id: int) -> str:
    return f"JALDOWZENTRAL{_device_id(plan_id, 'plan id')}"


def _encode_setpoint(zone_id: int, value: int) -> str:
    return f"TEMPSOLL{_device_id(zone_id, 'zone id')}:{_in_range(value, SETPOINT_OFF, SETPOINT_MAX, 'setpoint')}"


def _encode_scene(scene_id: int) -> str:
    return f"SZENEN{_device_id(scene_id, 'scene id')}"


def _encode_group_message(message: str) -> str:
    if not isinstance(message, str) or not _LIGHTS_GROUP_MESSAGE_RE.fullmatch(message):
        raise ValueError(f"not a lights group message: {message!r}")
    return message


def _encode_blinds_group_message(message: str) -> str:
    if not isinstance(message, str) or not _BLINDS_GROUP_MESSAGE_RE.fullmatch(message):
        raise ValueError(f"not a blinds group message: {message!r}")
    return message


def _encode_mute_toggle(mute_id: int) -> str:
    return f"MUTE{_device_id(mute_id, 'mute id')}"


class Commands:
    """All client→server commands, grouped in one namespace.

    Use ``Commands.<Name>.encode(...)`` at call sites — the
    indirection through :class:`CommandDefinition` keeps the wire
    payload, its description, and its example colocated.
    """

    # -- Bootstrap reads ---------------------------------------------------
    StatusContent = CommandDefinition(
        name="StatusContent",
        description=(
            "Bootstrap read (wire: ``StatusInhaltListe``): ask for the "
            "status-list schema. Server replies with one ``StatusEntry`` "
            "frame per row (each binding a label to a push frame such as "
            "``TEMPOUT``, ``REGEN``, ``CHART<id>STAND<n>``...), terminated "
            "by ``StatusContentFinished`` (wire: ``StatusInhaltFinishedListe``)."
        ),
        encode=_static("StatusInhaltListe"),
        example="StatusInhaltListe",
        audit=False,
        write=False,
    )
    Mainmenu = CommandDefinition(
        name="Mainmenu",
        description=(
            "Bootstrap read: ask for the full main-menu config + state "
            "dump (``ChartDefinition``, ``ClimateConfig``, ``LightConfig`` "
            "etc., plus initial ``ChartStand``/weather/scene values). "
            "Terminated by ``GiveMeMainMenuFinished``."
        ),
        encode=_static("GiveMeMainmenu"),
        example="GiveMeMainmenu",
        audit=False,
        write=False,
    )
    GlobalGsa = CommandDefinition(
        name="GlobalGsa",
        description=(
            "Bootstrap read (wire: ``GiveMeGlobalGsa``): ask for the "
            "door-intercom (GSA) config. Server replies with one "
            "``GsaConfig`` frame (wire: ``GlobalGsa>...``) carrying the "
            "SIP/Yealink gateway flag, LAN IP, volumes, the intercom "
            "camera array (``id^/linkmap<n>``) and the door-opener labels. "
            "The SPA issues it after ``GiveMeMainMenuFinished``; the camera "
            "platform needs the camera array to enumerate entities."
        ),
        encode=_static("GiveMeGlobalGsa"),
        example="GiveMeGlobalGsa",
        audit=False,
        write=False,
    )
    SocketConnected = CommandDefinition(
        name="SocketConnected",
        description=(
            "Connection handshake the SPA sends right after opening "
            "``/UpdatenLS`` for connection ``N``. Triggers the server's "
            "full current-state broadcast — ``PACKETBOX<N>``, ``REGEN``, "
            "``HAGEL``, ``TEMPOUT``, ``WINDGESCHWINDIGKEIT``, ``WINDALARM<N>``, "
            "``JALWARTUNG``, ``LEUCHTENZENTRAL<N>``, ``INFOBOARD<N>`` / "
            "``INFOBOARD<N>INHALT``, ``Vol<N>``, ``PERSINFO``, ``MUTE``, "
            "``SPRECHEN`` / ``CALLINFO``, ``KLIMASINFO<N>``, ``SceneState`` "
            "etc. Without this the server stays quiet on those broadcast "
            "frames. Also elicits ``SocketConnectedFinished>...``."
        ),
        encode=lambda n=1: f"SocketConnected:{n}",
        example="SocketConnected:1",
        audit=False,
        write=False,
    )
    # -- Heartbeat ---------------------------------------------------------
    Ping = CommandDefinition(
        name="Ping",
        description=(
            "Application-level heartbeat. Server replies with the "
            "``PongOK`` marker message. The SPA sends ``Ping`` every "
            "60s and reconnects if no ``PongOK`` arrives in time "
            "(see ``StartWebsocketTestMain`` in ``javallg.js``); the "
            "client mirrors that pattern in :meth:`SmartPlaceClient._heartbeat`."
        ),
        encode=_static("Ping"),
        example="Ping",
        audit=False,
        write=False,
    )
    # -- Per-id chart fetch (parameterized) --------------------------------
    ChartStands = CommandDefinition(
        name="ChartStands",
        description=(
            "Fetch every STAND<series>:<value> reading for chart <id>. "
            "Server replies with one ``CHART<id>STAND<series>:<value>`` "
            "frame per series. Chart ids come from ``StatusEntry`` "
            "references and ``ChartDefinition`` frames."
        ),
        encode=lambda chart_id: f"GiveMeChartStandsManuell{chart_id}",
        example="GiveMeChartStandsManuell49",
        audit=False,
        example_args=(49,),
        write=False,
    )
    ChartStandRange = CommandDefinition(
        name="ChartStandRange",
        description=(
            "Read chart <id>'s consumption between two epoch-second bounds "
            "(wire: ``SingelStandUpdate:<id>:<von>:<bis>``). This is the "
            "read-only fetch the SPA's chart-detail page issues; the server "
            "replies with one ``SingelStandUpdate<id>:<series>:<kWh>::::`` "
            "frame per series — 98 = range total, 97 = high-tariff (HT) "
            "share, 96 = low-tariff (NT) share — plus the lifetime register "
            "at the bounds (``SingelStand2Update``/``SingelStand3Update``). "
            "The HT/NT buckets are the server's own tariff bucketing (the "
            "same split the EWZ invoices bill), verified 2026-07-13 against "
            "a Sunday range (HT=0) and five months of invoices. The client "
            "polls this with today's bounds to drive the electricity cost "
            "sensors."
        ),
        encode=lambda chart_id, von, bis: f"SingelStandUpdate:{chart_id}:{von}:{bis}",
        example="SingelStandUpdate:49:1757887200:1757973600",
        audit=False,
        example_args=(49, 1757887200, 1757973600),
        write=False,
    )
    # -- Door openers (write; observe-only by default — caller must opt in)
    OpenGroundFloorEntrance = CommandDefinition(
        name="OpenGroundFloorEntrance",
        description=(
            "Door opener: ``OEFFNER1`` opens the ground-floor entrance. "
            "Never auto-issued; the HA integration surfaces this as a "
            "user-pressable Button entity."
        ),
        encode=_static("OEFFNER1"),
        example="OEFFNER1",
    )
    OpenMailbox = CommandDefinition(
        name="OpenMailbox",
        description="Door opener: ``OEFFNER2`` opens the mailbox.",
        encode=_static("OEFFNER2"),
        example="OEFFNER2",
    )
    OpenGarageEntrance = CommandDefinition(
        name="OpenGarageEntrance",
        description="Door opener: ``OEFFNER3`` opens the garage entrance.",
        encode=_static("OEFFNER3"),
        example="OEFFNER3",
    )
    OpenFrontDoor = CommandDefinition(
        name="OpenFrontDoor",
        description="Door opener: ``OEFFNER4`` opens the front door.",
        encode=_static("OEFFNER4"),
        example="OEFFNER4",
    )
    # -- Lights and sockets (write) ---------------------------------------
    LightToggle = CommandDefinition(
        name="LightToggle",
        description=(
            "Toggle light or socket <id> (wire: ``leuchte<id>``, no value). "
            "The only on/off command for every kind (schalter, led, steckdose, "
            "dimmer): the server flips the state and echoes ``leuchte<id>:<level>``. "
            "There is no absolute on or off, so the caller must know the current "
            "state before sending."
        ),
        encode=_encode_light_toggle,
        example="leuchte13",
        example_args=(13,),
        device=DEVICE_LIGHT,
    )
    LightDim = CommandDefinition(
        name="LightDim",
        description=(
            "Set dimmer <id> to an absolute level 0..255 (wire: "
            "``DIMleuchte<id>:<level>``). The vendor app only sends it for kind "
            "``dimmer``; whether the server accepts it for a switch-type light is "
            "untested."
        ),
        encode=_encode_light_dim,
        example="DIMleuchte13:128",
        example_args=(13, 128),
        device=DEVICE_LIGHT,
    )
    # -- Blinds (write) -----------------------------------------------------
    BlindUp = CommandDefinition(
        name="BlindUp",
        description=(
            "Move blind <id> fully up (wire: ``JALUP<id>``, single shot). The "
            "server reports the movement as ``JALICO<id>`` positions. There is no "
            "stop and no go-to-position command; sent to a blind that already moves "
            "up, it stops the blind."
        ),
        encode=_encode_blind_up,
        example="JALUP8",
        example_args=(8,),
        device=DEVICE_BLIND,
    )
    BlindDown = CommandDefinition(
        name="BlindDown",
        description=(
            "Move blind <id> fully down (wire: ``JALDOW<id>``, single shot). Sent to "
            "a blind that already moves down, it stops the blind."
        ),
        encode=_encode_blind_down,
        example="JALDOW8",
        example_args=(8,),
        device=DEVICE_BLIND,
    )
    BlindVent = CommandDefinition(
        name="BlindVent",
        description=(
            "Move slatted blind <id> to its vent position (wire: ``JALLUE<id>``). "
            "The vendor app offers it only for kind ``jalousie``."
        ),
        encode=_encode_blind_vent,
        example="JALLUE8",
        example_args=(8,),
        device=DEVICE_BLIND,
    )
    BlindsGroupUp = CommandDefinition(
        name="BlindsGroupUp",
        description=(
            "Move every blind of floor plan <plan> up (wire: ``JALUPZENTRAL<plan>``), "
            "the 'All up' button of the plan-wide blinds sub-menu. The number is the "
            "plan id. The vendor app offers it for that sub-menu only; any other "
            "blinds sub-menu sends its own group message (``BlindsGroupMessage``). "
            "Which blinds follow is set inside the vendor system; in the reference "
            "installation two do not. The integration does not issue it: its 'all "
            "blinds' buttons move each blind through its own entity."
        ),
        encode=_encode_blinds_group_up,
        example="JALUPZENTRAL1",
        example_args=(1,),
        changes_others=True,
    )
    BlindsGroupDown = CommandDefinition(
        name="BlindsGroupDown",
        description=(
            "Move every blind of floor plan <plan> down (wire: ``JALDOWZENTRAL<plan>``). "
            "As ``BlindsGroupUp``: not every blind follows, and the integration does not issue it."
        ),
        encode=_encode_blinds_group_down,
        example="JALDOWZENTRAL1",
        example_args=(1,),
        changes_others=True,
    )
    # -- Heating (write) ----------------------------------------------------
    Setpoint = CommandDefinition(
        name="Setpoint",
        description=(
            "Set the heating setpoint of climate zone <id> (wire: "
            "``TEMPSOLL<id>:<value>``), absolute, whole degrees 18..26. The vendor "
            "app labels 18 'forced off' and 26 'forced on'."
        ),
        encode=_encode_setpoint,
        example="TEMPSOLL5:21",
        example_args=(5, 21),
    )
    # -- Scenes (write) -----------------------------------------------------
    Scene = CommandDefinition(
        name="Scene",
        description=(
            "Trigger an ``OnOn`` scene or toggle an ``OnOff`` scene (wire: "
            "``SZENEN<id>``, no value). A PIN-protected scene needs a separate PIN "
            "exchange, which is not implemented; do not send this for one."
        ),
        encode=_encode_scene,
        example="SZENEN11",
        example_args=(11,),
        changes_others=True,
    )
    # -- Group and intercom buttons (write) ---------------------------------
    GroupMessage = CommandDefinition(
        name="GroupMessage",
        description=(
            "Press a lights sub-menu button by sending its group message verbatim "
            "(wire: ``LEUCHTENZENTRAL<n>``, taken from the ``LightSubMenu`` frame). "
            "What the server does with it (all off, all on, toggle) is untested. "
            'The integration does not issue it: its "all lights" buttons switch '
            "each light through its own entity."
        ),
        encode=_encode_group_message,
        example="LEUCHTENZENTRAL1",
        example_args=("LEUCHTENZENTRAL1",),
        changes_others=True,
    )
    BlindsGroupMessage = CommandDefinition(
        name="BlindsGroupMessage",
        description=(
            "Press the button of a blinds sub-menu that is not the plan-wide one by "
            "sending its group message verbatim (taken from the ``BlindSubMenu`` "
            "frame), as the vendor app does. No such sub-menu exists in the reference "
            "installation: the shape of the message and what the server does with it "
            "are untested."
        ),
        encode=_encode_blinds_group_message,
        example="JALZENTRAL2",
        example_args=("JALZENTRAL2",),
        changes_others=True,
    )
    MuteToggle = CommandDefinition(
        name="MuteToggle",
        description="Toggle doorbell mute <n> (wire: ``MUTE<n>``, no value).",
        encode=_encode_mute_toggle,
        example="MUTE1",
        example_args=(1,),
    )


def _collect_known_commands() -> tuple[CommandDefinition, ...]:
    """Walk :class:`Commands` and return every declared definition.

    Iteration order matches declaration order (Python preserves
    class attribute order since 3.7), so the tuple is stable.
    """
    return tuple(value for value in vars(Commands).values() if isinstance(value, CommandDefinition))


KNOWN_COMMANDS: Final[tuple[CommandDefinition, ...]] = _collect_known_commands()
"""Flat tuple of every command on :class:`Commands` — for iteration / tests / docs."""
