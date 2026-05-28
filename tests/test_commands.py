"""Tests for the client→server command registry.

Mirror of ``test_protocol.py``'s ``test_known_messages_*`` block: every
``Commands`` entry must encode to the documented example, and the
``KNOWN_COMMANDS`` tuple must enumerate the full namespace.
"""

from __future__ import annotations

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


def test_static_commands_encode_matches_example() -> None:
    """Static commands take no args and their encode() output is the example."""
    static_names = {
        "InfoboardWidgets",
        "InfoboardContent",
        "Mainmenu",
        "OpenGroundFloorEntrance",
        "OpenMailbox",
        "OpenGarageEntrance",
        "OpenFrontDoor",
    }
    for defn in KNOWN_COMMANDS:
        if defn.name in static_names:
            assert defn.encode() == defn.example, f"{defn.name} encode() != example"


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
