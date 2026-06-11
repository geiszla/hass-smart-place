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
pass a callable that returns the wire string.

Per the ``smart-place-observe-only`` memory: declaring a command
here does not auto-issue it. The library only sends when a caller
explicitly invokes ``client.send(Commands.X.encode(...))``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final


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
    """

    name: str
    description: str
    encode: Callable[..., str]
    example: str


def _static(wire: str) -> Callable[[], str]:
    """Return a no-arg encoder that always emits ``wire`` verbatim."""

    def encode() -> str:
        return wire

    return encode


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


def _collect_known_commands() -> tuple[CommandDefinition, ...]:
    """Walk :class:`Commands` and return every declared definition.

    Iteration order matches declaration order (Python preserves
    class attribute order since 3.7), so the tuple is stable.
    """
    return tuple(value for value in vars(Commands).values() if isinstance(value, CommandDefinition))


KNOWN_COMMANDS: Final[tuple[CommandDefinition, ...]] = _collect_known_commands()
"""Flat tuple of every command on :class:`Commands` — for iteration / tests / docs."""
