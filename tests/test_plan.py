"""Tests for :mod:`smart_place_client.plan`: geometry and the dashboard builder."""

from __future__ import annotations

import struct
from typing import Any

from smart_place_client import (
    BlindConfig,
    ClimateZoneConfig,
    DashboardEntities,
    LightConfig,
    PlanBackground,
    PlanConfig,
    SmartPlaceState,
    blind_centre,
    build_dashboard,
    climate_centre,
    light_centre,
    png_size,
)

# The reference flat's plan image is 1000 x 710 pixels.
_IMAGE = PlanBackground(entity_id="image.smart_place_floor_plan", width=1000, height=710)


def _png_header(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x06\x00\x00\x00"
    )


def _flat_entities(state: SmartPlaceState) -> DashboardEntities:
    return DashboardEntities(
        lights={
            light.id: f"{'switch' if light.kind == 'steckdose' else 'light'}.device_{light.id}"
            for light in state.lights.values()
        },
        blinds={blind.id: f"cover.blind_{blind.id}" for blind in state.blinds.values()},
        climates={zone.id: f"climate.zone_{zone.id}" for zone in state.climate.values()},
        scenes=["scene.night", "scene.evening"],
        switches=["switch.blinds", "switch.doorbell_mute"],
        fans=["fan.ventilation"],
        buttons=["button.all_blinds_up", "button.all_blinds_down"],
    )


def _flat_dashboard(state: SmartPlaceState, **overrides: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "plans": state.plans,
        "images": {1: _IMAGE},
        "lights": state.lights,
        "blinds": state.blinds,
        "zones": state.climate,
        "entities": _flat_entities(state),
        "temperature_unit": "°C",
    }
    arguments.update(overrides)
    return build_dashboard(**arguments)


def _views(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {view["path"]: view for view in config["views"]}


def _elements_of(view: dict[str, Any], entity_id: str) -> list[dict[str, Any]]:
    """Return the picture elements of ``view`` that concern ``entity_id``, conditionals unwrapped."""
    card = view["cards"][0]
    if card["type"] == "vertical-stack":
        card = card["cards"][0]
    found = []
    for element in card["elements"]:
        inner = element.get("elements", [element])
        owners = {condition.get("entity") for condition in element.get("conditions", [])}
        owners |= {item.get("entity") for item in inner}
        owners.add(element.get("tap_action", {}).get("target", {}).get("entity_id"))
        if entity_id in owners:
            found.extend(inner)
    return found


def _conditions_of(view: dict[str, Any], entity_id: str, title: str) -> list[dict[str, Any]]:
    """Return the conditions under which ``entity_id``'s element titled ``title`` is shown."""
    for element in view["cards"][0]["elements"]:
        conditions = element.get("conditions", [])
        if any(condition.get("entity") == entity_id for condition in conditions) and any(
            item.get("title") == title for item in element["elements"]
        ):
            return conditions
    raise AssertionError(f"no conditional element titled {title} for {entity_id}")


def test_png_size_reads_the_header() -> None:
    assert png_size(_png_header(1000, 710)) == (1000, 710)


def test_png_size_rejects_other_data() -> None:
    assert png_size(b"") is None
    assert png_size(b"<html>not found</html>" * 4) is None
    assert png_size(_png_header(1000, 710)[:20]) is None


def test_marker_centres_use_the_measured_offsets() -> None:
    """Page position (left, top) to image pixels; the image sits at left 10, top 54."""
    assert light_centre(330, 289) == (386.5, 214.5)
    assert blind_centre(405, 674) == (415.0, 590.0)
    assert climate_centre(569, 108) == (657.0, 94.5)


def test_reference_flat_gets_one_view_per_function(flat_state: SmartPlaceState) -> None:
    config = _flat_dashboard(flat_state)
    assert config["title"] == "Smart Place"
    assert [view["path"] for view in config["views"]] == ["lights", "blinds", "climate", "scenes"]
    # One plan: its label (an installation tag) is not shown anywhere.
    assert [view["title"] for view in config["views"]] == ["Lights", "Blinds", "Climate", "Scenes"]
    assert "XX00-00-00" not in repr(config)


def test_lights_view_has_one_toggle_icon_per_light_and_socket(flat_state: SmartPlaceState) -> None:
    view = _views(_flat_dashboard(flat_state))["lights"]
    assert view["type"] == "panel"
    card = view["cards"][0]
    assert card["type"] == "picture-elements"
    assert card["image_entity"] == "image.smart_place_floor_plan"
    assert len(card["elements"]) == 15
    assert {element["type"] for element in card["elements"]} == {"state-icon"}
    assert {element["entity"].split(".")[0] for element in card["elements"]} == {"light", "switch"}

    corridor = next(light for light in flat_state.lights.values() if light.name == "Corridor lights")
    (icon,) = _elements_of(view, f"light.device_{corridor.id}")
    assert icon["tap_action"] == {"action": "toggle"}
    assert icon["hold_action"] == {"action": "more-info"}
    # Centre (386.5, 214.5) of 1000 x 710.
    assert icon["style"] == {"left": "38.65%", "top": "30.21%"}


def test_protected_socket_shows_a_lock_and_cannot_be_toggled(flat_state: SmartPlaceState) -> None:
    """A tap on a protected socket opens its details; the other markers still toggle."""
    office, bedroom = (
        next(light for light in flat_state.lights.values() if light.name == name)
        for name in ("Office socket", "Bedroom socket")
    )
    view = _views(_flat_dashboard(flat_state, protected_lights={office.id}))["lights"]

    (locked,) = _elements_of(view, f"switch.device_{office.id}")
    assert locked["type"] == "state-icon"
    assert locked["icon"] == "mdi:lock"
    assert locked["tap_action"] == {"action": "more-info"}
    assert locked["hold_action"] == {"action": "more-info"}

    (free,) = _elements_of(view, f"switch.device_{bedroom.id}")
    assert "icon" not in free
    assert free["tap_action"] == {"action": "toggle"}
    actions = [element["tap_action"]["action"] for element in view["cards"][0]["elements"]]
    assert actions.count("toggle") == 14


def test_blinds_view_has_position_text_and_direction_buttons(flat_state: SmartPlaceState) -> None:
    view = _views(_flat_dashboard(flat_state))["blinds"]
    slatted = next(blind for blind in flat_state.blinds.values() if blind.name == "Office balcony door blinds")
    fabric = next(blind for blind in flat_state.blinds.values() if blind.name == "Bedroom blinds")

    elements = _elements_of(view, f"cover.blind_{slatted.id}")
    labels = [element for element in elements if element["type"] == "state-label"]
    buttons = {element["title"]: element for element in elements if element["type"] == "icon"}
    assert [label.get("attribute") for label in labels] == ["current_position", None]
    assert all(label["tap_action"] == {"action": "more-info"} for label in labels)
    assert {title for title, button in buttons.items() if button["tap_action"]["action"] == "perform-action"} == {
        "Down",
        "Vent",
        "Up",
    }
    assert buttons["Down"]["tap_action"] == {
        "action": "perform-action",
        "perform_action": "cover.close_cover",
        "target": {"entity_id": f"cover.blind_{slatted.id}"},
    }
    assert buttons["Vent"]["tap_action"]["perform_action"] == "cover.open_cover_tilt"
    assert buttons["Up"]["tap_action"]["perform_action"] == "cover.open_cover"
    # State icon centre (415, 590); buttons one row (50 px) below at -60 / 0 / +57 px.
    assert labels[0]["style"] == {"left": "41.5%", "top": "83.1%"}
    assert buttons["Down"]["style"] == {"left": "35.5%", "top": "90.14%"}
    assert buttons["Vent"]["style"] == {"left": "41.5%", "top": "90.14%"}
    assert buttons["Up"]["style"] == {"left": "47.2%", "top": "90.14%"}

    fabric_buttons = {
        e["title"]
        for e in _elements_of(view, f"cover.blind_{fabric.id}")
        if e["type"] == "icon" and e["tap_action"]["action"] == "perform-action"
    }
    assert fabric_buttons == {"Down", "Up"}


def test_blocked_blind_shows_its_alarm_and_greyed_buttons(flat_state: SmartPlaceState) -> None:
    """While the cover says ``blocked``, the buttons have no action and an alarm icon replaces the position."""
    view = _views(_flat_dashboard(flat_state))["blinds"]
    slatted = next(blind for blind in flat_state.blinds.values() if blind.name == "Office balcony door blinds")
    entity_id = f"cover.blind_{slatted.id}"

    def attribute(name: str, **match: Any) -> dict[str, Any]:
        return {"condition": "state", "entity": entity_id, "attribute": name, **match}

    elements = _elements_of(view, entity_id)
    icons = {element["title"]: element for element in elements if element["type"] == "icon"}
    position = next(element for element in elements if element.get("attribute") == "current_position")

    # The buttons with an action show unless blocked, the greyed ones while blocked.
    assert _conditions_of(view, entity_id, "Down") == [attribute("blocked", state_not="true")]
    assert _conditions_of(view, entity_id, "Down (blocked by a weather alarm)") == [attribute("blocked", state="true")]
    for title in ("Down", "Vent", "Up"):
        greyed = icons[f"{title} (blocked by a weather alarm)"]
        assert greyed["tap_action"] == {"action": "none"}
        assert greyed["hold_action"] == {"action": "none"}
        assert greyed["icon"] == icons[title]["icon"]
        assert greyed["style"] == {**icons[title]["style"], "opacity": "0.3"}

    # One icon per known alarm at the place of the position text, one for any other word.
    assert {title: icons[title]["icon"] for title in ("Rain alarm", "Wind alarm", "Hail alarm", "Weather alarm")} == {
        "Rain alarm": "mdi:weather-pouring",
        "Wind alarm": "mdi:weather-windy",
        "Hail alarm": "mdi:weather-hail",
        "Weather alarm": "mdi:alert",
    }
    assert _conditions_of(view, entity_id, "Rain alarm") == [attribute("weather_alarm", state="rain")]
    assert _conditions_of(view, entity_id, "Wind alarm") == [attribute("weather_alarm", state="wind")]
    assert _conditions_of(view, entity_id, "Hail alarm") == [attribute("weather_alarm", state="hail")]
    assert _conditions_of(view, entity_id, "Weather alarm") == [
        attribute("blocked", state="true"),
        attribute("weather_alarm", state_not=["rain", "wind", "hail"]),
    ]
    for title in ("Rain alarm", "Wind alarm", "Hail alarm", "Weather alarm"):
        assert icons[title]["style"] == position["style"]
        assert icons[title]["entity"] == entity_id
        assert icons[title]["tap_action"] == {"action": "more-info"}

    fabric = next(blind for blind in flat_state.blinds.values() if blind.name == "Bedroom blinds")
    fabric_greyed = {
        e["title"]
        for e in _elements_of(view, f"cover.blind_{fabric.id}")
        if e["type"] == "icon" and e["tap_action"]["action"] == "none"
    }
    assert fabric_greyed == {"Down (blocked by a weather alarm)", "Up (blocked by a weather alarm)"}


def test_climate_view_has_temperatures_and_step_buttons(flat_state: SmartPlaceState) -> None:
    view = _views(_flat_dashboard(flat_state))["climate"]
    zone = next(zone for zone in flat_state.climate.values() if zone.room == "Large bathroom")
    elements = _elements_of(view, f"climate.zone_{zone.id}")
    labels = [element for element in elements if element["type"] == "state-label"]
    buttons = {element["title"]: element for element in elements if element["type"] == "icon"}
    assert [label.get("attribute") for label in labels] == ["current_temperature", "temperature", None]
    assert labels[0]["suffix"] == " °C"
    assert buttons["Lower"]["tap_action"] == {
        "action": "perform-action",
        "perform_action": "smart_place.adjust_setpoint",
        "target": {"entity_id": f"climate.zone_{zone.id}"},
        "data": {"steps": -1},
    }
    assert buttons["Raise"]["tap_action"]["data"] == {"steps": 1}
    # Marker centre (657, 94.5): buttons 62 px left and right of it.
    assert buttons["Lower"]["style"] == {"left": "59.5%", "top": "13.31%"}
    assert buttons["Raise"]["style"] == {"left": "71.9%", "top": "13.31%"}


def _rows(*entity_ids: str) -> list[dict[str, Any]]:
    return [{"entity": entity_id, "name": {"type": "entity"}} for entity_id in entity_ids]


def test_scenes_view_uses_built_in_cards(flat_state: SmartPlaceState) -> None:
    """Each row shows the entity's own name: the shared device's name ("Scene Night") is left out."""
    view = _views(_flat_dashboard(flat_state))["scenes"]
    assert view["cards"] == [
        {"type": "entities", "title": "Scenes", "entities": _rows("scene.night", "scene.evening")},
        {"type": "entities", "title": "Switches", "entities": _rows("switch.blinds", "switch.doorbell_mute")},
        {"type": "tile", "entity": "fan.ventilation", "features": [{"type": "fan-speed"}]},
        {"type": "entities", "title": "Groups", "entities": _rows("button.all_blinds_up", "button.all_blinds_down")},
    ]


def test_disabled_entities_are_left_out(flat_state: SmartPlaceState) -> None:
    """An entity missing from the mapping (disabled, not created) gets no marker."""
    entities = _flat_entities(flat_state)
    lights = dict(entities.lights)
    removed = lights.pop(13)
    config = _flat_dashboard(
        flat_state,
        entities=DashboardEntities(lights=lights, blinds=entities.blinds, climates=entities.climates),
    )
    assert removed not in repr(config)
    assert len(_views(config)["lights"]["cards"][0]["elements"]) == 14
    assert "scenes" not in _views(config)


def test_devices_without_a_usable_position_are_listed_below_the_plan() -> None:
    plans = {1: PlanConfig(id=1, label="Flat", top=70, left=10, functions=("Leuchten",))}
    lights = {
        1: LightConfig(id=1, name="Placed", kind="schalter", top=289, left=330, plan_id=1),
        2: LightConfig(id=2, name="No position", kind="schalter", top=None, left=None, plan_id=1),
        3: LightConfig(id=3, name="Outside the image", kind="schalter", top=2000, left=330, plan_id=1),
        4: LightConfig(id=4, name="Unknown plan", kind="schalter", top=289, left=330, plan_id=7),
    }
    config = build_dashboard(
        plans=plans,
        images={1: _IMAGE},
        lights=lights,
        blinds={},
        zones={},
        entities=DashboardEntities(lights={n: f"light.l{n}" for n in lights}),
        temperature_unit="°C",
    )
    (view,) = config["views"]
    stack = view["cards"][0]
    assert stack["type"] == "vertical-stack"
    picture, rest = stack["cards"]
    assert [element["entity"] for element in picture["elements"]] == ["light.l1"]
    assert rest == {"type": "entities", "entities": ["light.l2", "light.l3", "light.l4"]}


def test_without_the_image_every_device_is_listed() -> None:
    """Before the plan image was fetched nothing can be placed, but all is reachable."""
    plans = {1: PlanConfig(id=1, label="Flat", top=70, left=10, functions=())}
    blinds = {1: BlindConfig(id=1, name="Blind", kind="jalousie", top=127, left=778, plan_id=1, runtime_s=60)}
    config = build_dashboard(
        plans=plans,
        images={},
        lights={},
        blinds=blinds,
        zones={},
        entities=DashboardEntities(blinds={1: "cover.blind"}),
        temperature_unit="°C",
    )
    (view,) = config["views"]
    assert view["cards"] == [{"type": "entities", "entities": ["cover.blind"]}]


def test_without_any_plan_devices_are_still_listed() -> None:
    zones = {
        1: ClimateZoneConfig(
            id=1, name="Bedroom heating", room="Bedroom", mode="Heizen", top=254, left=419, plan_id=None, fan_coil=False
        ),
    }
    config = build_dashboard(
        plans={},
        images={},
        lights={},
        blinds={},
        zones=zones,
        entities=DashboardEntities(climates={1: "climate.bedroom"}),
        temperature_unit="°C",
    )
    (view,) = config["views"]
    assert (view["title"], view["path"]) == ("Climate", "climate")
    assert view["cards"] == [{"type": "entities", "entities": ["climate.bedroom"]}]


def test_several_plans_repeat_the_views_with_the_plan_label() -> None:
    plans = {
        1: PlanConfig(id=1, label="Ground floor", top=70, left=10, functions=()),
        2: PlanConfig(id=2, label="Upper floor", top=70, left=10, functions=()),
    }
    lights = {
        1: LightConfig(id=1, name="Hall", kind="schalter", top=289, left=330, plan_id=1),
        2: LightConfig(id=2, name="Landing", kind="schalter", top=289, left=330, plan_id=2),
        3: LightConfig(id=3, name="Stray", kind="schalter", top=289, left=330, plan_id=9),
    }
    config = build_dashboard(
        plans=plans,
        images={1: _IMAGE, 2: PlanBackground(entity_id="image.upper", width=500, height=500)},
        lights=lights,
        blinds={},
        zones={},
        entities=DashboardEntities(lights={n: f"light.l{n}" for n in lights}),
        temperature_unit="°C",
    )
    assert [(view["title"], view["path"]) for view in config["views"]] == [
        ("Lights Ground floor", "lights-1"),
        ("Lights Upper floor", "lights-2"),
    ]
    first, second = config["views"]
    # The device of an unknown plan is listed once, in the first plan's view.
    assert first["cards"][0]["cards"][1] == {"type": "entities", "entities": ["light.l3"]}
    assert second["cards"][0]["image_entity"] == "image.upper"
    assert [element["entity"] for element in second["cards"][0]["elements"]] == ["light.l2"]


def test_companion_buttons_stay_inside_the_image() -> None:
    """A blind at the image edge keeps its buttons on the picture."""
    plans = {1: PlanConfig(id=1, label="Flat", top=70, left=10, functions=())}
    # State icon centre (10, 700): Down would be at x = -50, the row at y = 750.
    blinds = {1: BlindConfig(id=1, name="Edge", kind="jalousie", top=784, left=0, plan_id=1, runtime_s=60)}
    config = build_dashboard(
        plans=plans,
        images={1: _IMAGE},
        lights={},
        blinds=blinds,
        zones={},
        entities=DashboardEntities(blinds={1: "cover.edge"}),
        temperature_unit="°C",
    )
    buttons = {e["title"]: e for e in _elements_of(config["views"][0], "cover.edge") if e["type"] == "icon"}
    assert buttons["Down"]["style"] == {"left": "0.0%", "top": "100.0%"}


def test_icon_buttons_do_nothing_on_a_long_press(flat_state: SmartPlaceState) -> None:
    """An icon element without an entity must not get the default hold action (more-info): it ends in an error toast."""
    views = _views(_flat_dashboard(flat_state))
    icons = [
        inner
        for path in ("blinds", "climate")
        for element in views[path]["cards"][0]["elements"]
        for inner in element.get("elements", [element])
        if inner["type"] == "icon" and "entity" not in inner
    ]
    blocked = " (blocked by a weather alarm)"
    assert {icon["title"].removesuffix(blocked) for icon in icons} == {"Down", "Vent", "Up", "Lower", "Raise"}
    assert all(icon["hold_action"] == {"action": "none"} for icon in icons)
    # A light's icon has an entity: a long press opens its details.
    light = views["lights"]["cards"][0]["elements"][0]
    assert light["hold_action"] == {"action": "more-info"}


def test_dashboard_without_entities_has_a_placeholder_view() -> None:
    """The frontend opens a dashboard without views in edit mode; this one is read-only."""
    config = build_dashboard(
        plans={},
        images={},
        lights={},
        blinds={},
        zones={},
        entities=DashboardEntities(),
        temperature_unit="°C",
    )
    (view,) = config["views"]
    assert view["path"] == "waiting"
    assert [card["type"] for card in view["cards"]] == ["markdown"]


def test_placeholder_view_goes_away_with_the_first_entity(flat_state: SmartPlaceState) -> None:
    config = _flat_dashboard(flat_state, entities=DashboardEntities(scenes=["scene.night"]))
    assert [view["path"] for view in config["views"]] == ["scenes"]
