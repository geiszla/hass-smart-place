"""Tests for the shipped automation blueprints and their installer.

The blueprints are read as plain YAML: no Home Assistant. That they hold
no private data is checked in ``test_fixtures.py``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest
import yaml

_INTEGRATION = Path(__file__).parent.parent / "custom_components" / "smart_place"
# Loaded by its path: importing the package would import Home Assistant.
_SPEC = importlib.util.spec_from_file_location("blueprint_install", _INTEGRATION / "blueprint_install.py")
assert _SPEC is not None
assert _SPEC.loader is not None
blueprint_install = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(blueprint_install)

_FILES = sorted(blueprint_install.BLUEPRINTS.glob("*.yaml"))
_NAMES = [path.name for path in _FILES]
# The form Home Assistant's importer turns into a raw GitHub address ("Re-import blueprint").
_SOURCE_URL = (
    "https://github.com/geiszla/hass-smart-place/blob/main/custom_components/smart_place/blueprints/automation/"
)
# These notify the phones chosen in the form; the other blueprints notify every phone that has the app.
_WITH_PHONES_INPUT = {"consumption_warning.yaml", "high_winds.yaml"}


class _Input(str):
    """The name behind an ``!input`` tag."""

    __slots__ = ()


class _Loader(yaml.SafeLoader):
    """Safe loader that knows Home Assistant's ``!input`` tag."""


def _construct_input(loader: yaml.SafeLoader, node: yaml.ScalarNode) -> _Input:
    return _Input(loader.construct_scalar(node))


_Loader.add_constructor("!input", _construct_input)


def _load(path: Path) -> dict[str, Any]:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=_Loader)


def _used_inputs(node: object) -> set[str]:
    if isinstance(node, _Input):
        return {str(node)}
    if isinstance(node, dict):
        node = list(node.values())
    if isinstance(node, list):
        return {name for item in node for name in _used_inputs(item)}
    return set()


def _declared_inputs(blueprint: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The inputs of a blueprint by name, those of a section included."""
    declared: dict[str, dict[str, Any]] = {}
    for key, value in blueprint["blueprint"]["input"].items():
        declared.update(value.get("input", {key: value}))
    return declared


def test_the_expected_blueprints_are_shipped() -> None:
    assert _NAMES == [
        "consumption_warning.yaml",
        "high_winds.yaml",
        "info_board_entry.yaml",
        "intercom_ringing.yaml",
        "package_delivered.yaml",
    ]


@pytest.mark.parametrize("path", _FILES, ids=_NAMES)
def test_blueprint_metadata(path: Path) -> None:
    metadata = _load(path)["blueprint"]
    assert metadata["name"].startswith("Smart Place: ")
    assert metadata["domain"] == "automation"
    assert metadata["description"].strip()
    assert metadata["source_url"] == _SOURCE_URL + path.name


@pytest.mark.parametrize("path", _FILES, ids=_NAMES)
def test_blueprint_declares_exactly_the_inputs_it_uses(path: Path) -> None:
    blueprint = _load(path)
    assert _used_inputs(blueprint) == set(_declared_inputs(blueprint))


@pytest.mark.parametrize("path", _FILES, ids=_NAMES)
def test_blueprint_is_an_automation_with_named_inputs(path: Path) -> None:
    blueprint = _load(path)
    assert blueprint["triggers"]
    assert blueprint["actions"]
    for name, definition in _declared_inputs(blueprint).items():
        assert definition["name"], name
        assert len(definition["selector"]) == 1, name


@pytest.mark.parametrize("path", _FILES, ids=_NAMES)
def test_phones_are_chosen_by_the_user_or_not_an_input(path: Path) -> None:
    """A blueprint either has a required ``phones`` input for the companion app's devices, or none."""
    inputs = _declared_inputs(_load(path))
    if path.name not in _WITH_PHONES_INPUT:
        assert "phones" not in inputs
        return
    phones = inputs["phones"]
    assert "default" not in phones
    assert phones["selector"] == {"device": {"multiple": True, "filter": [{"integration": "mobile_app"}]}}


@pytest.mark.parametrize("path", _FILES, ids=_NAMES)
def test_entity_inputs_offer_smart_place_entities(path: Path) -> None:
    """Every entity input is limited to this integration, except the wind sensor, which may be a helper."""
    for name, definition in _declared_inputs(_load(path)).items():
        if "entity" not in definition["selector"]:
            continue
        filters = definition["selector"]["entity"]["filter"]
        if name == "wind_sensor":
            assert filters == [{"domain": "sensor", "device_class": "wind_speed"}]
        else:
            assert [entity_filter["integration"] for entity_filter in filters] == ["smart_place"], name


def test_consumption_warning_covers_both_levels_with_a_title_each() -> None:
    blueprint = _load(blueprint_install.BLUEPRINTS / "consumption_warning.yaml")
    inputs = _declared_inputs(blueprint)
    levels = inputs["levels"]["selector"]["select"]
    assert levels["multiple"] is True
    assert [option["value"] for option in levels["options"]] == ["orange", "red"]
    assert inputs["levels"]["default"] == ["orange", "red"]
    # One title per level, each with a default; nothing to type for either.
    assert {name for name in inputs if "title" in name} == {"title_orange", "title_red"}
    assert all(inputs[name]["default"] for name in ("title_orange", "title_red"))
    [trigger] = blueprint["triggers"]
    assert trigger["to"] == "levels"
    assert trigger["not_from"] == ["unknown", "unavailable"]


def test_installer_copies_every_blueprint_into_a_new_folder(tmp_path: Path) -> None:
    target = tmp_path / "blueprints" / "automation" / "smart_place"
    assert blueprint_install.install_blueprints(target) == _NAMES
    for path in _FILES:
        assert (target / path.name).read_bytes() == path.read_bytes()


def test_installer_copies_only_what_is_missing_and_overwrites_nothing(tmp_path: Path) -> None:
    blueprint_install.install_blueprints(tmp_path)
    changed, removed = tmp_path / _NAMES[0], tmp_path / _NAMES[1]
    changed.write_text("changed by the user", encoding="utf-8")
    removed.unlink()
    other = tmp_path / "my_own.yaml"
    other.write_text("not ours", encoding="utf-8")

    assert blueprint_install.install_blueprints(tmp_path) == [removed.name]
    assert changed.read_text(encoding="utf-8") == "changed by the user"
    assert removed.read_bytes() == _FILES[1].read_bytes()
    assert other.read_text(encoding="utf-8") == "not ours"
    assert blueprint_install.install_blueprints(tmp_path) == []


@pytest.mark.parametrize("path", _FILES, ids=_NAMES)
def test_a_tap_on_the_notification_opens_something_on_both_phone_systems(path: Path) -> None:
    """Without a target a tap opens the default dashboard. Android reads ``clickAction``, iOS ``url``."""
    text = path.read_text(encoding="utf-8")
    assert text.count("'clickAction': tap_target, 'url': tap_target") == 1


@pytest.mark.parametrize(
    ("name", "sensor"),
    [
        ("consumption_warning.yaml", "use_sensor"),
        ("high_winds.yaml", "wind_sensor"),
        ("package_delivered.yaml", "pin_sensor"),
    ],
)
def test_sensor_notifications_open_the_sensors_details(name: str, sensor: str) -> None:
    text = (_FILES[_NAMES.index(name)]).read_text(encoding="utf-8")
    assert f'tap_target: "/?more-info-entity-id={{{{ {sensor} }}}}"' in text
