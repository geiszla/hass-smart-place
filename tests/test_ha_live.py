"""Tests for the safety guards of ``scripts/ha_live.py``.

The script talks to a real Home Assistant box that controls a real
building. These tests run its guards against a scripted API object: no
network, no Home Assistant.
"""

from __future__ import annotations

from collections import deque
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any, Self

import aiohttp
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
    {"entity_id": "climate.bedroom", "config_entry_id": _ENTRY_ID, "unique_id": "entry1_climate_1"},
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


async def test_call_of_an_integration_action_goes_through() -> None:
    """A ``smart_place`` action may be called on an entity of another domain."""
    api = _FakeApi(dry_run=True)
    await ha_live._call_in_dry_run(api, "smart_place.adjust_setpoint", "climate.bedroom", {"steps": -1})
    assert api.service_calls == [
        {
            "domain": "smart_place",
            "service": "adjust_setpoint",
            "service_data": {"steps": -1},
            "target": {"entity_id": "climate.bedroom"},
        },
    ]


@pytest.mark.parametrize("service", ["homeassistant.turn_off", "switch.turn_on", "script.turn_on", "scene.apply"])
async def test_call_is_refused_for_a_service_of_another_domain(service: str) -> None:
    """A service of another domain could act on more than the checked entity."""
    api = _FakeApi(dry_run=True)
    with pytest.raises(ha_live.ApiError, match="not a service of the light or the smart_place domain"):
        await ha_live._call_in_dry_run(api, service, "light.kitchen_lights", {})
    assert api.commands == []


@pytest.mark.parametrize("key", ["entity_id", "device_id", "area_id", "floor_id", "label_id"])
async def test_call_is_refused_when_the_data_names_further_targets(key: str) -> None:
    """Home Assistant honours these keys as targets; only the named entity is checked."""
    api = _FakeApi(dry_run=True)
    with pytest.raises(ha_live.ApiError, match=f"must not name targets \\({key}\\)"):
        await ha_live._call_in_dry_run(api, "light.turn_on", "light.kitchen_lights", {key: "other", "brightness": 1})
    assert api.commands == []


def test_call_command_refuses_further_targets_given_as_arguments() -> None:
    """The same refusal through the command line, where the data is typed as key=value."""
    result = CliRunner().invoke(
        ha_live.cli,
        ["--token", "t", "--url", "http://192.0.2.1:1", "call", "light.turn_off", "light.kitchen_lights", "area_id=x"],
    )
    assert result.exit_code == 2
    assert "must not name targets (area_id)" in result.output


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


class _FakeSocket:
    """Scripted Home Assistant WebSocket: accepts the login, answers every command, records what was sent."""

    closed = False

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self._replies: deque[dict[str, Any]] = deque([{"type": "auth_required"}])

    async def send_json(self, message: dict[str, Any]) -> None:
        self.sent.append(message)
        if message["type"] == "auth":
            self._replies.append({"type": "auth_ok"})
        else:
            self._replies.append({"id": message.get("id"), "type": "result", "success": True, "result": "done"})

    async def receive(self) -> SimpleNamespace:
        return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(self._replies.popleft()))


@pytest.fixture
def fake_socket(monkeypatch: pytest.MonkeyPatch) -> _FakeSocket:
    """Make every session the script opens connect to one fake socket."""
    socket = _FakeSocket()

    class _FakeSession:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def ws_connect(self, _url: str, **_kwargs: object) -> _FakeSocket:
            return socket

    monkeypatch.setattr(ha_live.aiohttp, "ClientSession", _FakeSession)
    return socket


def _invoke_ws(*args: str) -> Any:
    return CliRunner().invoke(ha_live.cli, ["--token", "t", "--url", "http://192.0.2.1:1", "ws", *args])


def test_ws_hatch_sends_an_allowed_command_with_its_payload(fake_socket: _FakeSocket) -> None:
    result = _invoke_ws("config_entries/get", '{"domain": "smart_place"}')
    assert result.exit_code == 0, result.output
    assert fake_socket.sent == [
        {"type": "auth", "access_token": "t"},
        {"id": 1, "type": "config_entries/get", "domain": "smart_place"},
    ]


@pytest.mark.parametrize(
    "payload",
    [
        '{"type": "call_service", "domain": "light", "service": "turn_on", "target": {"entity_id": "light.x"}}',
        '{"type": "ping"}',
        '{"id": 7}',
        '{"domain": "light", "id": 1, "type": "fire_event"}',
    ],
)
def test_ws_hatch_refuses_a_payload_that_replaces_the_command(fake_socket: _FakeSocket, payload: str) -> None:
    """``ws ping '{"type": "call_service", ...}'`` must not reach the box as a service call."""
    result = _invoke_ws("ping", payload)
    assert result.exit_code == 2
    assert "must not contain the key 'type' or 'id'" in result.output
    assert fake_socket.sent == []


@pytest.mark.parametrize("payload", ["[1, 2]", '"type"', "{not json"])
def test_ws_hatch_refuses_a_payload_that_is_not_a_json_object(fake_socket: _FakeSocket, payload: str) -> None:
    result = _invoke_ws("ping", payload)
    assert result.exit_code == 2
    assert fake_socket.sent == []


# ------------------------------ update ------------------------------


class _FakeUpdateApi:
    """Stand-in for ``HassApi`` with the Marketplace's update entity; records the service calls."""

    calls: list[tuple[str, dict[str, Any]]]
    refusal: str | None

    def __init__(self, *_args: object) -> None:
        pass

    async def get(self, path: str) -> Any:
        assert path == "/api/states/update.smart_place_update"
        return {"state": "on", "attributes": {"installed_version": "aaa1111", "latest_version": "bbb2222"}}

    async def ws_command(self, command_type: str, **payload: Any) -> Any:
        self.calls.append((command_type, payload))
        if self.refusal is not None:
            raise ha_live.ApiError(f"WS command 'call_service' failed: home_assistant_error: {self.refusal}")


@pytest.fixture
def update_api(monkeypatch: pytest.MonkeyPatch) -> type[_FakeUpdateApi]:
    _FakeUpdateApi.calls = []
    _FakeUpdateApi.refusal = None
    monkeypatch.setattr(ha_live, "HassApi", _FakeUpdateApi)
    return _FakeUpdateApi


def _invoke_update(*args: str) -> Any:
    return CliRunner().invoke(ha_live.cli, ["--token", "t", "--url", "http://192.0.2.1:1", "update", *args])


def test_update_installs_through_the_marketplace_update_entity(update_api: type[_FakeUpdateApi]) -> None:
    """``update`` calls ``update.install`` on the Marketplace's update entity of the integration, nothing else."""
    assert ha_live.MARKETPLACE_UPDATE_ENTITY == "update.smart_place_update"
    result = _invoke_update("--version", "ccc3333")
    assert result.exit_code == 0, result.output
    assert update_api.calls == [
        (
            "call_service",
            {
                "max_wait": ha_live.UPDATE_TIMEOUT,
                "domain": "update",
                "service": "install",
                "service_data": {"version": "ccc3333"},
                "target": {"entity_id": "update.smart_place_update"},
            },
        ),
    ]
    assert "Installing ccc3333 (installed: aaa1111)" in result.output


def test_update_without_a_version_installs_what_the_marketplace_offers(update_api: type[_FakeUpdateApi]) -> None:
    result = _invoke_update()
    assert result.exit_code == 0, result.output
    assert [payload["service_data"] for _, payload in update_api.calls] == [{}]
    assert "Installing bbb2222" in result.output


def test_update_reports_a_refusal_of_the_marketplace(update_api: type[_FakeUpdateApi]) -> None:
    """Until the Marketplace warning was accepted in the UI, Home Assistant refuses the install."""
    update_api.refusal = "Open the Marketplace and read the warning first"
    result = _invoke_update()
    assert result.exit_code == 2
    assert "Open the Marketplace and read the warning first" in result.output


def test_update_help_names_the_marketplace_and_its_warning() -> None:
    result = CliRunner().invoke(ha_live.cli, ["update", "--help"])
    assert result.exit_code == 0
    assert "Marketplace" in result.output
    assert "warning not accepted" in result.output
    assert "HACS" not in result.output
