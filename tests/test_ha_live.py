"""Tests for the safety guards of ``scripts/ha_live.py``.

The script talks to a real Home Assistant box that controls a real
building. These tests run its guards against a scripted API object: no
network, no Home Assistant.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from typing import Any

from click.testing import CliRunner
import pytest

_SPEC = importlib.util.spec_from_file_location(
    "ha_live",
    Path(__file__).parent.parent / "scripts" / "ha_live.py",
)
assert _SPEC is not None
assert _SPEC.loader is not None
ha_live = importlib.util.module_from_spec(_SPEC)
sys.modules["ha_live"] = ha_live
_SPEC.loader.exec_module(ha_live)

_ENTRY_ID = "entry1"
_REGISTRY = [
    {
        "entity_id": "binary_sensor.smart_place_connection",
        "config_entry_id": _ENTRY_ID,
        "unique_id": "entry1_connection",
    },
    {"entity_id": "light.kitchen_lights", "config_entry_id": _ENTRY_ID, "unique_id": "entry1_light_3"},
    {"entity_id": "button.smart_place_front_door", "config_entry_id": _ENTRY_ID, "unique_id": "entry1_door_front_door"},
    {"entity_id": "light.other_lamp", "config_entry_id": "another", "unique_id": "xyz"},
]


class _FakeApi:
    """Scripted stand-in for ``HassApi``; records every WebSocket command."""

    def __init__(self, *, dry_run: bool | None) -> None:
        self.commands: list[tuple[str, dict[str, Any]]] = []
        attributes: dict[str, Any] = {"device_class": "connectivity"}
        if dry_run is not None:
            attributes["dry_run"] = dry_run
        self._states = [
            {"entity_id": "binary_sensor.smart_place_connection", "state": "on", "attributes": attributes},
            {"entity_id": "light.kitchen_lights", "state": "off", "attributes": {}},
        ]

    async def get(self, path: str) -> Any:
        if path == "/api/config/config_entries/entry":
            return [{"entry_id": _ENTRY_ID, "domain": "smart_place", "title": "Smart Place", "state": "loaded"}]
        if path == "/api/states":
            return self._states
        raise AssertionError(f"unexpected GET {path}")

    async def ws_command(self, command_type: str, **payload: Any) -> Any:
        self.commands.append((command_type, payload))
        if command_type == "config/entity_registry/list":
            return _REGISTRY
        return {"context": {}, "response": None}

    @property
    def service_calls(self) -> list[dict[str, Any]]:
        return [payload for command_type, payload in self.commands if command_type == "call_service"]


async def test_call_goes_through_in_dry_run() -> None:
    api = _FakeApi(dry_run=True)
    await ha_live._call_in_dry_run(api, "light.turn_on", "light.kitchen_lights", {"brightness": 128})
    assert api.service_calls == [
        {
            "domain": "light",
            "service": "turn_on",
            "service_data": {"brightness": 128},
            "target": {"entity_id": "light.kitchen_lights"},
        },
    ]


@pytest.mark.parametrize("dry_run", [False, None, "true", 1])
async def test_call_is_refused_without_dry_run(dry_run: Any) -> None:
    """Only the literal attribute ``dry_run: true`` opens the gate."""
    api = _FakeApi(dry_run=dry_run)
    with pytest.raises(ha_live.ApiError, match="not in dry run"):
        await ha_live._call_in_dry_run(api, "light.turn_on", "light.kitchen_lights", {})
    assert api.service_calls == []


async def test_call_is_refused_on_a_door_button_even_in_dry_run() -> None:
    api = _FakeApi(dry_run=True)
    with pytest.raises(ha_live.ApiError, match="door button"):
        await ha_live._call_in_dry_run(api, "button.press", "button.smart_place_front_door", {})
    assert api.service_calls == []


async def test_call_is_refused_on_entities_of_other_integrations() -> None:
    api = _FakeApi(dry_run=True)
    for entity_id in ("light.other_lamp", "light.does_not_exist"):
        with pytest.raises(ha_live.ApiError, match="not an entity of the smart_place integration"):
            await ha_live._call_in_dry_run(api, "light.turn_on", entity_id, {})
    assert api.service_calls == []


async def test_call_needs_a_domain_and_a_service() -> None:
    api = _FakeApi(dry_run=True)
    with pytest.raises(ha_live.ApiError, match=r"<domain>\.<service>"):
        await ha_live._call_in_dry_run(api, "turn_on", "light.kitchen_lights", {})
    assert api.commands == []


def test_service_data_values_are_json_where_possible() -> None:
    assert ha_live._parse_service_data(("steps=-1", "temperature=21.5", "hvac_mode=heat", 'x="1"', "flag=true")) == {
        "steps": -1,
        "temperature": 21.5,
        "hvac_mode": "heat",
        "x": "1",
        "flag": True,
    }
    with pytest.raises(ha_live.ApiError, match="key=value"):
        ha_live._parse_service_data(("steps",))


@pytest.mark.parametrize("command", sorted(ha_live.BLOCKED_WS_COMMANDS))
def test_ws_hatch_refuses_commands_that_act(command: str) -> None:
    """The raw ws hatch never reaches the box with a command that makes HA act."""
    result = CliRunner().invoke(ha_live.cli, ["--token", "t", "--url", "http://192.0.2.1:1", "ws", command, "{}"])
    assert result.exit_code == 2
    assert "not allowed through ws" in result.output


def test_ws_hatch_blocks_the_service_and_event_commands() -> None:
    assert {"call_service", "execute_script", "fire_event"} <= ha_live.BLOCKED_WS_COMMANDS
