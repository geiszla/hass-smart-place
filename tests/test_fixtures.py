"""Tests on the capture fixtures themselves.

The repository is public and the fixtures come from a real flat, so the
first block guards what must never be in them, nor in the test modules,
the registry examples and the shipped blueprints. The second block
replays the redacted 2026-11-09 captures.
"""

from __future__ import annotations

import ast
from collections import Counter
from collections.abc import Callable
import json
from pathlib import Path
import re

import pytest

from smart_place_client import (
    KNOWN_COMMANDS,
    KNOWN_MESSAGES,
    ServerFrame,
    SessionPhase,
    SmartPlaceClient,
    SmartPlaceState,
    UnknownFrame,
    cover_position,
)

TESTS = Path(__file__).parent
FIXTURES = TESTS / "fixtures"
BLUEPRINTS = TESTS.parent / "custom_components" / "smart_place" / "blueprints"

# Frames that carry the installation's remote-access passwords.
_CREDENTIAL_FRAME = re.compile(r"\b\d+Update:")
# Any IPv4 address except the documentation range 192.0.2.0/24 (covers LAN addresses).
_IPV4 = re.compile(r"\b(?!192\.0\.2\.)\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
# Installation tags look like two capitals and three number pairs; the placeholder is all zeros.
_INSTALLATION_TAG = re.compile(r"\b(?!XX00-00-00)[A-Z]{2}\d{2}-\d{2}-\d{2}\b")
_URL_TOKEN = re.compile(r"TOKEN=(?!<REDACTED>)|/(?:Start|Infoboard)\d?\?(?!<REDACTED>)")
# Free text that can carry a parcel PIN or an info-board link: only the "cleared" marker is allowed.
_FREE_TEXT = re.compile(r"^(?:PERSINFO|INFOBOARD\d+INHALT):(?!Read$)")
_PIN = re.compile(r"PIN:\s*\d", re.IGNORECASE)
# The routing frame's last field is a second token unless it is the placeholder ``Leer``.
_ROUTE_TOKEN = re.compile(r"^GoToLinkSSL:[^:]*:[^:]*:(?!Leer$|<REDACTED>$)")
# Chart definitions (both frames share the layout) carry the customer number in their 13th field.
_CHART_DEFINITION = re.compile(r"(?:CreateChartSingelDiagramm|SingelDiagramm)\d+:([^\n\"']*)")
_CUSTOMER_NUMBER_FIELD = 12

_FORBIDDEN: dict[str, re.Pattern[str]] = {
    "credential frame": _CREDENTIAL_FRAME,
    "network address": _IPV4,
    "installation tag": _INSTALLATION_TAG,
    "URL token": _URL_TOKEN,
    "second token": _ROUTE_TOKEN,
    "free text": _FREE_TEXT,
    "parcel PIN": _PIN,
}
# What can be told from the shape alone, in any text: a frame, a line of a test module.
_FORBIDDEN_SHAPES = {kind: _FORBIDDEN[kind] for kind in ("network address", "installation tag")}
# A blueprint takes every entity and phone as an input. An entity id in it, or the notify action of a
# phone, would be one of some home. The two actions that a blueprint names in full are not entities.
_ENTITY_ID = re.compile(
    r"\b(?:automation|binary_sensor|button|camera|climate|cover|device_tracker|fan|image|light|notify|person"
    r"|scene|script|sensor|switch)\.(?!press\b|send_message\b)[a-z0-9_]+"
)
_FORBIDDEN_IN_BLUEPRINTS = {
    **_FORBIDDEN_SHAPES,
    "URL token": _URL_TOKEN,
    "parcel PIN": _PIN,
    "entity id": _ENTITY_ID,
}


def _has_customer_number(text: str) -> bool:
    """Return whether a chart definition in ``text`` has a customer number that is not all zeros."""
    return any(
        len(fields := match.group(1).split(";")) > _CUSTOMER_NUMBER_FIELD
        and bool(fields[_CUSTOMER_NUMBER_FIELD].strip("0"))
        for match in _CHART_DEFINITION.finditer(text)
    )


def _private_data(text: str) -> list[str]:
    """Return what kinds of private data a frame text contains."""
    found = [kind for kind, pattern in _FORBIDDEN.items() if pattern.search(text)]
    if _has_customer_number(text):
        found.append("customer number")
    if text.startswith(("BasicInfos:", "TOKEN:", "SPOTIFYTOKEN<")):
        found.append("account data")
    return found


def _private_shapes(text: str) -> list[str]:
    """Return the private data that shows by its shape in a text that is not a single frame."""
    found = [kind for kind, pattern in _FORBIDDEN_SHAPES.items() if pattern.search(text)]
    if _has_customer_number(text):
        found.append("customer number")
    return found


def _fixture_texts() -> list[tuple[str, int, str]]:
    rows = []
    for path in sorted(FIXTURES.glob("*.ndjson")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            rows.append((path.name, number, json.loads(line)["text"]))
    return rows


def test_fixtures_contain_no_private_data() -> None:
    """No credential frame, address, installation tag, token, PIN or account data in any fixture."""
    leaks = [(name, number, kinds) for name, number, text in _fixture_texts() if (kinds := _private_data(text))]
    assert leaks == []


def _python_lines() -> list[tuple[str, int, str]]:
    """Every line of the test modules, apart from the guard's own examples below."""
    rows = []
    for path in sorted(TESTS.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        skipped: set[int] = set()
        if path == Path(__file__):
            for node in ast.parse(source).body:
                if (
                    isinstance(node, ast.AnnAssign)
                    and isinstance(node.target, ast.Name)
                    and node.target.id in ("_GUARD_EXAMPLES", "_GUARD_LINE_EXAMPLES")
                ):
                    assert node.end_lineno is not None
                    skipped.update(range(node.lineno, node.end_lineno + 1))
        rows += [
            (path.name, number, line)
            for number, line in enumerate(source.splitlines(), start=1)
            if number not in skipped
        ]
    return rows


def _python_strings() -> list[tuple[str, int, str]]:
    """Every string literal of the test modules, adjacent literals joined as Python joins them."""
    return [
        (path.name, node.lineno, node.value)
        for path in sorted(TESTS.glob("*.py"))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def test_test_modules_contain_no_private_data() -> None:
    """No installation tag, no address outside 192.0.2.0/24 and no customer number in ``tests/*.py``.

    Tags and addresses are looked for line by line (comments included).
    A chart definition can be split over several literals, so customer
    numbers are looked for in the string literals.
    """
    leaks = [
        (name, number, kind)
        for name, number, line in _python_lines()
        for kind, pattern in _FORBIDDEN_SHAPES.items()
        if pattern.search(line)
    ]
    guard_examples = {text for text, _ in (*_GUARD_EXAMPLES, *_GUARD_LINE_EXAMPLES)}
    leaks += [
        (name, number, "customer number")
        for name, number, text in _python_strings()
        if text not in guard_examples and _has_customer_number(text)
    ]
    assert leaks == []


def test_registry_examples_contain_no_private_data() -> None:
    """The message and command examples are public too."""
    examples = [(defn.name, defn.example) for defn in (*KNOWN_MESSAGES, *KNOWN_COMMANDS)]
    assert [(name, kinds) for name, example in examples if (kinds := _private_shapes(example))] == []


def _private_blueprint_data(line: str) -> list[str]:
    """Return what kinds of private data a line of a blueprint contains."""
    return [kind for kind, pattern in _FORBIDDEN_IN_BLUEPRINTS.items() if pattern.search(line)]


def test_blueprints_contain_no_private_data() -> None:
    """No address, installation tag, token, PIN, entity id or phone in any shipped blueprint."""
    paths = sorted(BLUEPRINTS.rglob("*.yaml"))
    assert paths
    leaks = [
        (path.name, number, kinds)
        for path in paths
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if (kinds := _private_blueprint_data(line))
    ]
    assert leaks == []


# The guard's own examples: each must be recognised as the kind named. Private-looking on
# purpose (all invented), so ``test_test_modules_contain_no_private_data`` skips this list.
_GUARD_EXAMPLES: list[tuple[str, str]] = [
    ("144Update:secret", "credential frame"),
    ("145Update:", "credential frame"),
    ("GlobalGsa>YEALINKOFF>172.16.0.10>60>LEER>LEER>70>", "network address"),
    ("GlobalGsa>YEALINKOFF>192.168.1.20>60>LEER>LEER>70>", "network address"),
    ("GlobalGsa>YEALINKOFF>10.1.2.3>60>LEER>LEER>70>", "network address"),
    ("Floorplan1:AB12-34-56,70px,10px,Leuchten", "installation tag"),
    ("SingelDiagramm49:Elektro AB12-34-56;Area", "installation tag"),
    ("opening wss://h:8770/StartAppExt/?TOKEN=abc", "URL token"),
    ("GoToLinkSSL:spr1.smartplace.ch:38435:abc123", "second token"),
    ("GoToLinkSSL:spr1.smartplace.ch:38435/Start1:Leer2", "second token"),
    ("PERSINFO:Sie haben eine Lieferung. Bitte verwenden Sie den PIN:1234", "parcel PIN"),
    ("PERSINFO:something else", "free text"),
    ("INFOBOARD2INHALT:<a href='x'>link</a>", "free text"),
    ("SingelDiagramm49:Elektro;a;b;c;d;e;f;g;;CHF;0.1;0.1;1234567;Elektro;kWh", "customer number"),
    ("CreateChartSingelDiagramm49:Elektro;a;b;c;d;e;f;g;;CHF;0.1;0.1;1234567;Elektro;kWh", "customer number"),
    ("BasicInfos:1234567;someone@example.com", "account data"),
]


@pytest.mark.parametrize(("text", "kind"), _GUARD_EXAMPLES)
def test_private_data_guard_recognises(text: str, kind: str) -> None:
    """The guard itself must flag each kind of private data."""
    assert kind in _private_data(text)


# The same for lines of a test module.
_GUARD_LINE_EXAMPLES: list[tuple[str, str]] = [
    ('    state.apply(parse_frame("GlobalGsa>YEALINKOFF>10.0.0.1>60>"))', "network address"),
    ("# the tag AB12-34-56 of the installation", "installation tag"),
    ('raw = "SingelDiagramm49:Elektro;a;b;c;d;e;f;g;;CHF;0.1;0.1;1234567;Elektro;kWh"', "customer number"),
]


@pytest.mark.parametrize(("line", "kind"), _GUARD_LINE_EXAMPLES)
def test_private_data_guard_recognises_shapes_in_source_lines(line: str, kind: str) -> None:
    assert kind in _private_shapes(line)


# The same for lines of a blueprint (all invented).
@pytest.mark.parametrize(
    ("line", "kind"),
    [
        ("    entity_id: sensor.garden_shed_temperature", "entity id"),
        ("        - action: notify.mobile_app_some_phone", "entity id"),
        ("      default: button.side_gate", "entity id"),
        ("            clickAction: https://spr1.smartplace.ch:8770/Start5?abc", "URL token"),
        ('      default: "PIN: 1234"', "parcel PIN"),
    ],
)
def test_private_data_guard_recognises_private_blueprint_lines(line: str, kind: str) -> None:
    assert kind in _private_blueprint_data(line)


def test_private_data_guard_accepts_what_a_blueprint_needs() -> None:
    for line in (
        "          - action: button.press",
        "        # notify.send_message cannot carry data.",
        "        - action: \"notify.{{ ('mobile_app_' ~ device_attr(repeat.item, 'name')) | slugify }}\"",
        "        {{ integration_entities('mobile_app') | select('match', 'notify\\.') | list }}",
        '      default: "PIN: {{ value }}"',
        '      value: "{{ trigger.to_state.state }}"',
        "    min_version: 2026.11.0",
    ):
        assert _private_blueprint_data(line) == []


def test_private_data_guard_accepts_redacted_frames() -> None:
    for text in (
        "GlobalGsa>YEALINKOFF>192.0.2.1>60>1^/linkmap1>1^Eingang EG>70>",
        "Floorplan1:XX00-00-00,70px,10px,Leuchten-Jalousien-Klima",
        "PERSINFO:Read",
        "INFOBOARD2INHALT:Read",
        "INFOBOARD2:01",
        "SingelStandUpdate49:97:41.0819999999999::::",
        "SingelDiagramm49:Elektro XX00-00-00;a;b;c;d;e;f;g;;CHF;0.1;0.1;0000000;Elektro;kWh",
        "CreateChartSingelDiagramm49:Elektro XX00-00-00;a;b;c;d;e;f;g;;CHF;0.1;0.1;0000000;Elektro;kWh",
        "GoToLinkSSL:spr1.smartplace.ch:38435/Start1:Leer",
        "GoToLinkSSL:spr1.smartplace.ch:38435:<REDACTED>",
    ):
        assert _private_data(text) == []


# --------------------------- replay ---------------------------------


async def test_control_bootstrap_replays_without_unknown_frames() -> None:
    """Every frame of a full bootstrap of the reference flat is a known shape."""
    seen: list[ServerFrame] = []

    async def collect(frame: ServerFrame) -> None:
        seen.append(frame)

    client = SmartPlaceClient.replay(path=FIXTURES / "control-bootstrap.ndjson", handlers=[collect])
    await client.run()
    assert [frame for frame in seen if isinstance(frame, UnknownFrame)] == []
    assert client.state.main_menu_loaded is True
    assert client.state.phase is SessionPhase.READY
    assert client.state.plan_ids == {1}


def test_reference_flat_inventory(flat_state: SmartPlaceState) -> None:
    """The bootstrap describes 15 lights, 10 blinds, 6 zones, 12 scenes and one plan."""
    assert Counter(light.kind for light in flat_state.lights.values()) == {"schalter": 11, "steckdose": 3, "led": 1}
    assert Counter(blind.kind for blind in flat_state.blinds.values()) == {"jalousie": 5, "jalousiestoff": 5}
    assert {blind.runtime_s for blind in flat_state.blinds.values()} == {29, 39, 60}
    assert sorted(zone.room for zone in flat_state.climate.values()) == [
        "Bedroom",
        "Corridor/laundry room",
        "Large bathroom",
        "Living room/dining room/kitchen",
        "Office",
        "Small bathroom",
    ]
    assert Counter(scene.kind for scene in flat_state.scene_configs.values()) == {"OnOn": 7, "OnOff": 5}
    assert not any(scene.pin_protected for scene in flat_state.scene_configs.values())
    assert list(flat_state.plans) == [1]
    assert flat_state.plans[1].functions == ("Leuchten", "Jalousien", "Klima")
    assert flat_state.light_sub_menus[1].group_message == "LEUCHTENZENTRAL1"
    assert flat_state.blind_sub_menus[1].plan_id == 1
    assert flat_state.mutes == {1: False}


def test_reference_flat_values_arrive_with_the_bootstrap(flat_state: SmartPlaceState) -> None:
    """Every device has a value after the burst that follows ``SocketConnected:1``."""
    assert set(flat_state.light_levels) == set(flat_state.lights)
    assert set(flat_state.blind_values) == set(flat_state.blinds)
    assert set(flat_state.setpoints) == set(flat_state.climate)
    assert set(flat_state.climate_info) == set(flat_state.climate)
    assert set(flat_state.scene_states) == set(flat_state.scene_configs)
    # 250 and 255 both mean on.
    assert set(flat_state.light_levels.values()) == {0, 250, 255}
    assert {value.alarm for value in flat_state.blind_values.values()} == {None, "rain"}
    assert flat_state.setpoints[6] == 18
    assert set(flat_state.climate_info.values()) == {None, "heizen"}


def test_scene_echo_switches_lights_and_closes_two_blinds(
    flat_state: SmartPlaceState,
    fold_fixture: Callable[[SmartPlaceState, str], None],
) -> None:
    """The recorded "Night" scene: its flag, five lights, then two blinds running down."""
    night = next(scene.id for scene in flat_state.scene_configs.values() if scene.name == "Night")
    assert flat_state.scene_states[night] is False
    assert flat_state.light_levels[13] == 255
    assert cover_position(flat_state.blind_values[1].position or 0) == 100

    fold_fixture(flat_state, "scene-echo.ndjson")

    assert flat_state.scene_states[night] is True
    assert {light_id: flat_state.light_levels[light_id] for light_id in (13, 2, 3, 5, 14)} == {
        13: 0,
        2: 0,
        3: 0,
        5: 0,
        14: 250,
    }
    for blind_id in (1, 10):
        value = flat_state.blind_values[blind_id]
        assert value.position == 100
        assert cover_position(value.position) == 0
