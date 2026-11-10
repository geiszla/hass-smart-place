"""Tests for the client→server command registry.

Mirror of ``test_protocol.py``'s ``test_known_messages_*`` block: every
``Commands`` entry must encode to the documented example, and the
``KNOWN_COMMANDS`` tuple must enumerate the full namespace.
"""

from __future__ import annotations

import pytest

from smart_place_client import KNOWN_COMMANDS, CommandDefinition, Commands


def test_known_commands_names_are_unique() -> None:
    """Each registry entry has a unique CamelCase name."""
    names = [defn.name for defn in KNOWN_COMMANDS]
    assert len(names) == len(set(names)), f"duplicate names: {names}"


def test_known_commands_have_descriptions_and_examples() -> None:
    """Every entry is fully populated — description + example + callable encoder."""
    for defn in KNOWN_COMMANDS:
        assert isinstance(defn, CommandDefinition)
        assert defn.name, "name missing"
        assert defn.description, f"{defn.name} missing description"
        assert defn.example, f"{defn.name} missing example"
        assert callable(defn.encode), f"{defn.name} encode not callable"


def test_commands_encode_matches_example() -> None:
    """Every command encodes its ``example_args`` to its documented example."""
    for defn in KNOWN_COMMANDS:
        assert defn.encode(*defn.example_args) == defn.example, f"{defn.name} encode() != example"


def test_ping_command_encodes_to_wire_ping() -> None:
    """The Ping heartbeat command emits the SPA's literal ``Ping`` wire string."""
    assert Commands.Ping.encode() == "Ping"


def test_socket_connected_default_and_override() -> None:
    """``SocketConnected.encode()`` defaults to ``:1`` but accepts any N."""
    assert Commands.SocketConnected.encode() == "SocketConnected:1"
    assert Commands.SocketConnected.encode(2) == "SocketConnected:2"
    assert Commands.SocketConnected.encode(n=99) == "SocketConnected:99"


def test_chart_stands_encode_embeds_id() -> None:
    """``ChartStands`` is the parameterized command — takes the chart id."""
    assert Commands.ChartStands.encode(49) == "GiveMeChartStandsManuell49"
    assert Commands.ChartStands.encode(337) == "GiveMeChartStandsManuell337"


def test_known_commands_matches_namespace_attributes() -> None:
    """Every CommandDefinition declared on Commands surfaces in KNOWN_COMMANDS."""
    declared = {value.name for value in vars(Commands).values() if isinstance(value, CommandDefinition)}
    registered = {defn.name for defn in KNOWN_COMMANDS}
    assert declared == registered


def test_open_doors_use_oeffner_ids() -> None:
    """Door-opener wire commands keep the OEFFNER<n> shape the SPA expects."""
    assert Commands.OpenGroundFloorEntrance.encode() == "OEFFNER1"
    assert Commands.OpenMailbox.encode() == "OEFFNER2"
    assert Commands.OpenGarageEntrance.encode() == "OEFFNER3"
    assert Commands.OpenFrontDoor.encode() == "OEFFNER4"


def test_door_openers_are_audited() -> None:
    """Door openers (control/actuation) land in the audit trail."""
    for defn in (
        Commands.OpenGroundFloorEntrance,
        Commands.OpenMailbox,
        Commands.OpenGarageEntrance,
        Commands.OpenFrontDoor,
    ):
        assert defn.audit is True, f"{defn.name} should be audited"


def test_machinery_commands_are_not_audited() -> None:
    """Bootstrap reads, heartbeat, and chart polling stay out of the trail."""
    for defn in (
        Commands.StatusContent,
        Commands.Mainmenu,
        Commands.GlobalGsa,
        Commands.SocketConnected,
        Commands.Ping,
        Commands.ChartStands,
    ):
        assert defn.audit is False, f"{defn.name} should not be audited"


def test_audit_defaults_to_true() -> None:
    """New commands are audited by default — the trail fails safe to visibility."""
    assert CommandDefinition(name="X", description="d", encode=lambda: "X", example="X").audit is True


# The commands that change something in the building. Listed by hand on
# purpose: a new command must be added here (a write) or to the read list
# below, so nobody can declare a write as a read by accident.
_WRITE_COMMANDS = {
    "OpenGroundFloorEntrance",
    "OpenMailbox",
    "OpenGarageEntrance",
    "OpenFrontDoor",
    "LightToggle",
    "LightDim",
    "BlindUp",
    "BlindDown",
    "BlindVent",
    "BlindsGroupUp",
    "BlindsGroupDown",
    "Setpoint",
    "Scene",
    "GroupMessage",
    "BlindsGroupMessage",
    "MuteToggle",
}
_READ_COMMANDS = {
    "StatusContent",
    "Mainmenu",
    "GlobalGsa",
    "SocketConnected",
    "Ping",
    "ChartStands",
    "ChartStandRange",
}


def test_write_flag_matches_the_explicit_lists() -> None:
    """Every command is declared a write or a read, exactly as listed here."""
    assert {defn.name for defn in KNOWN_COMMANDS if defn.write} == _WRITE_COMMANDS
    assert {defn.name for defn in KNOWN_COMMANDS if not defn.write} == _READ_COMMANDS


def test_write_defaults_to_true() -> None:
    """A new command is a write unless declared a read — dry run fails safe."""
    assert CommandDefinition(name="X", description="d", encode=lambda: "X", example="X").write is True


def test_write_commands_are_audited() -> None:
    """Every command that changes the building lands in the audit trail."""
    assert all(defn.audit for defn in KNOWN_COMMANDS if defn.write)


def test_commands_that_change_other_devices_are_flagged() -> None:
    """Scenes and group buttons start the settle time; a command for one device does not.

    Listed by hand like the writes: a new scene or group command belongs here.
    """
    assert {defn.name for defn in KNOWN_COMMANDS if defn.changes_others} == {
        "Scene",
        "GroupMessage",
        "BlindsGroupMessage",
        "BlindsGroupUp",
        "BlindsGroupDown",
    }
    assert CommandDefinition(name="X", description="d", encode=lambda: "X", example="X").changes_others is False


def test_control_commands_encode_vendor_wire_strings() -> None:
    """The control commands produce the frames the vendor app sends."""
    assert Commands.LightToggle.encode(13) == "leuchte13"
    assert Commands.LightDim.encode(4, 0) == "DIMleuchte4:0"
    assert Commands.LightDim.encode(4, 255) == "DIMleuchte4:255"
    assert Commands.BlindUp.encode(1) == "JALUP1"
    assert Commands.BlindDown.encode(10) == "JALDOW10"
    assert Commands.BlindVent.encode(9) == "JALLUE9"
    assert Commands.BlindsGroupUp.encode(1) == "JALUPZENTRAL1"
    assert Commands.BlindsGroupDown.encode(2) == "JALDOWZENTRAL2"
    assert Commands.Setpoint.encode(6, 18) == "TEMPSOLL6:18"
    assert Commands.Setpoint.encode(6, 26) == "TEMPSOLL6:26"
    assert Commands.Scene.encode(11) == "SZENEN11"
    assert Commands.GroupMessage.encode("LEUCHTENZENTRAL1") == "LEUCHTENZENTRAL1"
    assert Commands.BlindsGroupMessage.encode("JALZENTRAL2") == "JALZENTRAL2"
    assert Commands.BlindsGroupMessage.encode("JALGRUPPE12") == "JALGRUPPE12"
    assert Commands.MuteToggle.encode(1) == "MUTE1"


@pytest.mark.parametrize(
    ("command", "args"),
    [
        (Commands.LightToggle, (0,)),
        (Commands.LightToggle, (-1,)),
        (Commands.LightToggle, ("13",)),
        (Commands.LightToggle, (True,)),
        (Commands.LightDim, (13, -1)),
        (Commands.LightDim, (13, 256)),
        (Commands.LightDim, (13, 12.5)),
        (Commands.BlindUp, (0,)),
        (Commands.BlindDown, (1.0,)),
        (Commands.BlindVent, (None,)),
        (Commands.BlindsGroupUp, (0,)),
        (Commands.BlindsGroupDown, ("1",)),
        (Commands.Setpoint, (5, 17)),
        (Commands.Setpoint, (5, 27)),
        (Commands.Setpoint, (5, 21.5)),
        (Commands.Setpoint, (0, 21)),
        (Commands.Scene, (0,)),
        (Commands.MuteToggle, (0,)),
        # A group message comes from the server; only the lights shape may go back out.
        (Commands.GroupMessage, ("OEFFNER1",)),
        (Commands.GroupMessage, ("LEUCHTENZENTRAL1\nOEFFNER1",)),
        (Commands.GroupMessage, ("LEUCHTENZENTRAL1\n",)),
        (Commands.GroupMessage, ("JALZENTRAL1",)),
        (Commands.GroupMessage, ("",)),
        (Commands.GroupMessage, (1,)),
        # The same for a blinds sub-menu: only a frame name of the blinds family.
        (Commands.BlindsGroupMessage, ("OEFFNER1",)),
        (Commands.BlindsGroupMessage, ("LEUCHTENZENTRAL1",)),
        (Commands.BlindsGroupMessage, ("SZENEN7",)),
        (Commands.BlindsGroupMessage, ("leuchte13",)),
        (Commands.BlindsGroupMessage, ("PwReset1",)),
        (Commands.BlindsGroupMessage, ("JALZENTRAL2\nOEFFNER1",)),
        (Commands.BlindsGroupMessage, ("JALZENTRAL2:01",)),
        (Commands.BlindsGroupMessage, ("JALZENTRAL",)),
        (Commands.BlindsGroupMessage, ("TEMPSOLL5:18",)),
        (Commands.BlindsGroupMessage, ("",)),
        (Commands.BlindsGroupMessage, (2,)),
    ],
)
def test_control_commands_reject_bad_arguments(command: CommandDefinition, args: tuple[object, ...]) -> None:
    """Out-of-range or wrongly typed arguments raise instead of reaching the wire."""
    with pytest.raises(ValueError, match=r"must be|not a lights group message|not a blinds group message"):
        command.encode(*args)
