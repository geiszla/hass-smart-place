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
# The same with the box around its drawing; the rest of the image is transparent.
_DRAWN_IMAGE = PlanBackground(
    entity_id="image.smart_place_floor_plan", width=1000, height=710, content=(212, 15, 741, 699)
)


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
        light_buttons=["button.all_lights_off", "button.all_lights_on"],
        blind_buttons=["button.all_blinds_up", "button.all_blinds_down"],
        climate_buttons=["button.all_heating_off", "button.heating_back_on"],
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


def _cards(view: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the cards of a plan view: one section, the plan first."""
    (section,) = view["sections"]
    return section["cards"]


def _anchor(element: dict[str, Any]) -> tuple[str, str]:
    return element["style"]["left"], element["style"]["top"]


def _press_tile(entity_id: str) -> dict[str, Any]:
    press = {"action": "perform-action", "perform_action": "button.press", "target": {"entity_id": entity_id}}
    return {
        "type": "tile",
        "entity": entity_id,
        "name": {"type": "entity"},
        "hide_state": True,
        "tap_action": press,
        "icon_tap_action": press,
        "grid_options": {"columns": 6},
    }


def _elements_of(view: dict[str, Any], entity_id: str) -> list[dict[str, Any]]:
    """Return the picture elements of ``view`` that concern ``entity_id``, conditionals unwrapped."""
    found = []
    for element in _cards(view)[0]["elements"]:
        inner = element.get("elements", [element])
        owners = {condition.get("entity") for condition in element.get("conditions", [])}
        owners |= {item.get("entity") for item in inner}
        owners.add(element.get("tap_action", {}).get("target", {}).get("entity_id"))
        if entity_id in owners:
            found.extend(inner)
    return found


def _conditions_of(view: dict[str, Any], entity_id: str, title: str) -> list[dict[str, Any]]:
    """Return the conditions under which ``entity_id``'s element titled ``title`` is shown."""
    for element in _cards(view)[0]["elements"]:
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
    # One column: at most 500 px wide on a desktop, the screen's width on a phone.
    assert (view["type"], view["max_columns"]) == ("sections", 1)
    card, *below = _cards(view)
    # Below the plan: the two buttons for all lights, side by side.
    assert below == [_press_tile("button.all_lights_off"), _press_tile("button.all_lights_on")]
    assert card["type"] == "picture-elements"
    assert card["image_entity"] == "image.smart_place_floor_plan"
    assert card["grid_options"] == {"columns": "full"}
    assert len(card["elements"]) == 15
    assert {element["type"] for element in card["elements"]} == {"state-icon"}
    assert {element["entity"].split(".")[0] for element in card["elements"]} == {"light", "switch"}

    corridor = next(light for light in flat_state.lights.values() if light.name == "Corridor lights")
    (icon,) = _elements_of(view, f"light.device_{corridor.id}")
    assert icon["tap_action"] == {"action": "toggle"}
    assert icon["hold_action"] == {"action": "more-info"}
    # Centre (386.5, 214.5) of 1000 x 710, the 40 px badge kept inside the card.
    assert _anchor(icon) == (
        "clamp(22px, 38.65%, calc(100% - 22px))",
        "clamp(22px, 30.21%, calc(100% - 22px))",
    )
    assert "var(--card-background-color)" in icon["style"]["background"]


def test_protected_socket_shows_a_lock_and_cannot_be_toggled(flat_state: SmartPlaceState) -> None:
    """A tap on a protected socket does nothing (a long press opens its details); the other markers still toggle."""
    office, bedroom = (
        next(light for light in flat_state.lights.values() if light.name == name)
        for name in ("Office socket", "Bedroom socket")
    )
    view = _views(_flat_dashboard(flat_state, protected_lights={office.id}))["lights"]

    (locked,) = _elements_of(view, f"switch.device_{office.id}")
    assert locked["type"] == "state-icon"
    assert locked["icon"] == "mdi:lock"
    assert locked["tap_action"] == {"action": "none"}
    assert locked["hold_action"] == {"action": "more-info"}

    (free,) = _elements_of(view, f"switch.device_{bedroom.id}")
    assert "icon" not in free
    assert free["tap_action"] == {"action": "toggle"}
    actions = [element["tap_action"]["action"] for element in _cards(view)[0]["elements"]]
    assert actions.count("toggle") == 14


def test_blinds_view_has_a_position_gauge_and_direction_buttons(flat_state: SmartPlaceState) -> None:
    view = _views(_flat_dashboard(flat_state))["blinds"]
    slatted = next(blind for blind in flat_state.blinds.values() if blind.name == "Office balcony door blinds")
    fabric = next(blind for blind in flat_state.blinds.values() if blind.name == "Bedroom blinds")

    elements = _elements_of(view, f"cover.blind_{slatted.id}")
    icons = {element["title"]: element for element in elements if element["type"] == "icon"}
    assert {title for title, icon in icons.items() if icon["tap_action"]["action"] == "perform-action"} == {
        "Down",
        "Vent",
        "Up",
    }
    assert icons["Down"]["tap_action"] == {
        "action": "perform-action",
        "perform_action": "cover.close_cover",
        "target": {"entity_id": f"cover.blind_{slatted.id}"},
    }
    assert icons["Vent"]["tap_action"]["perform_action"] == "cover.open_cover_tilt"
    assert icons["Up"]["tap_action"]["perform_action"] == "cover.open_cover"

    # One marker around the vendor's state icon centre (415, 590): 116 px wide, 32 px high.
    up = icons["Fully up"]
    assert _anchor(up) == (
        "clamp(min(60px, 14.56vw), 41.5%, calc(100% - min(60px, 14.56vw)))",
        "clamp(min(18px, 4.37vw), 83.1%, calc(100% - min(18px, 4.37vw)))",
    )
    assert up["style"]["min-width"] == "min(116px, 28.16vw)"
    # The buttons share the anchor and lie on the right part of the marker, in the vendor's order.
    assert {_anchor(icons[title]) for title in ("Down", "Vent", "Up")} == {_anchor(up)}
    assert icons["Down"]["style"]["transform"] == "translate(calc(-50% - min(10px, 2.43vw)), -50%)"
    assert icons["Vent"]["style"]["transform"] == "translate(calc(-50% + min(16px, 3.88vw)), -50%)"
    assert icons["Up"]["style"]["transform"] == "translate(calc(-50% + min(42px, 10.19vw)), -50%)"

    fabric_buttons = {
        e["title"]: e["style"]["transform"]
        for e in _elements_of(view, f"cover.blind_{fabric.id}")
        if e["type"] == "icon" and e["tap_action"]["action"] == "perform-action"
    }
    # Without Vent the marker is 90 px wide.
    assert fabric_buttons == {
        "Down": "translate(calc(-50% + min(3px, 0.73vw)), -50%)",
        "Up": "translate(calc(-50% + min(29px, 7.04vw)), -50%)",
    }


def test_blind_position_is_a_gauge_with_three_levels(flat_state: SmartPlaceState) -> None:
    """One window outline, its opening drawn as closed by half or fully: "partly" and "down" must not look alike."""
    view = _views(_flat_dashboard(flat_state))["blinds"]
    blind = next(blind for blind in flat_state.blinds.values() if blind.name == "Bedroom blinds")
    entity_id = f"cover.blind_{blind.id}"
    icons = {element["title"]: element for element in _elements_of(view, entity_id) if element["type"] == "icon"}

    def position(**limits: int) -> list[dict[str, Any]]:
        return [{"condition": "numeric_state", "entity": entity_id, "attribute": "current_position", **limits}]

    # Home Assistant's scale: 100 is fully up.
    assert _conditions_of(view, entity_id, "Fully up") == position(above=99)
    assert _conditions_of(view, entity_id, "Partly down") == position(above=0, below=100)
    assert _conditions_of(view, entity_id, "Fully down") == position(below=1)
    backdrop = "color-mix(in srgb, var(--card-background-color) 85%, transparent)"
    box = "linear-gradient(currentColor, currentColor) min(14px, 3.4vw) min(13px, 3.16vw) / min(8px, 1.94vw)"
    assert icons["Fully up"]["style"]["background"] == backdrop
    assert icons["Partly down"]["style"]["background"] == f"{box} min(6px, 1.46vw) no-repeat, {backdrop}"
    assert icons["Fully down"]["style"]["background"] == f"{box} min(11px, 2.67vw) no-repeat, {backdrop}"
    for title in ("Fully up", "Partly down", "Fully down"):
        icon = icons[title]
        assert icon["icon"] == "mdi:window-shutter-open"
        assert icon["entity"] == entity_id
        assert icon["tap_action"] == {"action": "more-info"}
        # Not the colour of the buttons beside it.
        assert icon["style"]["color"] == "var(--primary-color)"
        assert _anchor(icon) == _anchor(icons["Fully up"])


def test_blocked_blind_shows_its_alarm_and_greyed_buttons(flat_state: SmartPlaceState) -> None:
    """While the cover says ``blocked``, the buttons have no action and an alarm icon replaces the position."""
    view = _views(_flat_dashboard(flat_state))["blinds"]
    slatted = next(blind for blind in flat_state.blinds.values() if blind.name == "Office balcony door blinds")
    entity_id = f"cover.blind_{slatted.id}"

    def attribute(name: str, **match: Any) -> dict[str, Any]:
        return {"condition": "state", "entity": entity_id, "attribute": name, **match}

    elements = _elements_of(view, entity_id)
    icons = {element["title"]: element for element in elements if element["type"] == "icon"}
    position = icons["Fully up"]

    # The buttons with an action show unless blocked or unavailable, the greyed ones while blocked.
    assert _conditions_of(view, entity_id, "Down") == [
        attribute("blocked", state_not="true"),
        {"condition": "state", "entity": entity_id, "state_not": ["unknown", "unavailable"]},
    ]
    assert _conditions_of(view, entity_id, "Down (blocked by a weather alarm)") == [attribute("blocked", state="true")]
    for title in ("Down", "Vent", "Up"):
        greyed = icons[f"{title} (blocked by a weather alarm)"]
        assert greyed["tap_action"] == {"action": "none"}
        assert greyed["hold_action"] == {"action": "none"}
        assert greyed["icon"] == icons[title]["icon"]
        assert greyed["style"] == {**icons[title]["style"], "opacity": "0.3"}

    # One icon per known alarm at the place of the position gauge, one for any other word.
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
        # Same place, size and backdrop as the position gauge, in the warning colour.
        for key in ("left", "top", "min-width", "background", "padding"):
            assert icons[title]["style"][key] == position["style"][key]
        assert icons[title]["style"]["color"] == "var(--warning-color)"
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
    assert [label.get("attribute") for label in labels] == [
        "current_temperature",
        "temperature",
        "temperature",
        "temperature",
        None,
        None,
    ]
    assert labels[0]["suffix"] == " °C"
    assert buttons["Lower"]["tap_action"] == {
        "action": "perform-action",
        "perform_action": "smart_place.adjust_setpoint",
        "target": {"entity_id": f"climate.zone_{zone.id}"},
        "data": {"steps": -1},
    }
    assert buttons["Raise"]["tap_action"]["data"] == {"steps": 1}
    # One marker around the centre (657, 94.5): 112 px wide, two rows (48 px) high.
    assert _anchor(labels[0]) == (
        "clamp(min(58px, 14.08vw), 65.7%, calc(100% - min(58px, 14.08vw)))",
        "clamp(min(26px, 6.31vw), 13.31%, calc(100% - min(26px, 6.31vw)))",
    )
    assert labels[0]["style"]["min-width"] == "min(112px, 27.18vw)"
    assert "var(--card-background-color)" in labels[0]["style"]["background"]
    # The target (or the mode) is the second row; the buttons are at the sides.
    # The last element is the state of an unavailable zone, a marker of its own.
    assert {_anchor(element) for element in elements[:-1]} == {_anchor(labels[0])}
    second_row = "translate(-50%, calc(-50% + min(8px, 1.94vw)))"
    assert {label["style"]["transform"] for label in labels[1:5]} == {second_row}
    assert buttons["Lower"]["style"]["transform"] == "translate(calc(-50% - min(42px, 10.19vw)), -50%)"
    assert buttons["Raise"]["style"]["transform"] == "translate(calc(-50% + min(42px, 10.19vw)), -50%)"


def test_zone_that_heats_shows_a_flame_before_the_target(flat_state: SmartPlaceState) -> None:
    """The target row exists three times, by the entity's ``hvac_action``: plain, heating, cooling."""
    view = _views(_flat_dashboard(flat_state))["climate"]
    zone = next(zone for zone in flat_state.climate.values() if zone.room == "Large bathroom")
    entity_id = f"climate.zone_{zone.id}"
    heat = {"condition": "state", "entity": entity_id, "state": "heat"}

    def action(**match: Any) -> dict[str, Any]:
        return {"condition": "state", "entity": entity_id, "attribute": "hvac_action", **match}

    rows = {
        tuple(element.get("title") for element in conditional["elements"]): conditional
        for conditional in _cards(view)[0]["elements"]
        if any(element.get("attribute") == "temperature" for element in conditional["elements"])
        and conditional["conditions"][0]["entity"] == entity_id
    }
    assert set(rows) == {(None,), (None, "Heating"), (None, "Cooling")}
    assert rows[None,]["conditions"] == [heat, action(state_not=["heating", "cooling"])]
    assert rows[None,]["elements"][0]["prefix"] == "→ "

    for title, name, icon, colour in (
        ("Heating", "heating", "mdi:fire", "var(--state-climate-heat-color)"),
        ("Cooling", "cooling", "mdi:snowflake", "var(--state-climate-cool-color)"),
    ):
        conditional = rows[None, title]
        assert conditional["conditions"] == [heat, action(state=name)]
        target, sign = conditional["elements"]
        # The sign stands where the arrow was; the target makes room for it and has its colour.
        assert target["prefix"] == ""
        assert target["style"]["padding-left"] == "min(16px, 3.88vw)"
        assert target["style"]["color"] == colour
        assert sign["icon"] == icon
        assert sign["entity"] == entity_id
        assert sign["tap_action"] == {"action": "more-info"}
        assert sign["style"]["color"] == colour
        assert sign["style"]["--mdc-icon-size"] == "min(16px, 3.88vw)"
        assert sign["style"]["transform"] == "translate(calc(-50% - min(18px, 4.37vw)), calc(-50% + min(8px, 1.94vw)))"
        assert _anchor(sign) == _anchor(target)


def test_unavailable_zone_shows_only_its_state(flat_state: SmartPlaceState) -> None:
    """Temperatures and buttons need the zone; without it the marker is its state on a backdrop."""
    view = _views(_flat_dashboard(flat_state))["climate"]
    zone = next(zone for zone in flat_state.climate.values() if zone.room == "Large bathroom")
    entity_id = f"climate.zone_{zone.id}"

    def state(**match: Any) -> list[dict[str, Any]]:
        return [{"condition": "state", "entity": entity_id, **match}]

    conditionals = [
        element for element in _cards(view)[0]["elements"] if element["conditions"][0]["entity"] == entity_id
    ]
    first, *_targets, mode, alone = conditionals
    assert first["conditions"] == state(state_not="unavailable")
    assert [element.get("title") for element in first["elements"]] == [None, "Lower", "Raise"]
    assert mode["conditions"] == state(state_not=["heat", "unavailable"])
    assert alone["conditions"] == state(state="unavailable")
    (label,) = alone["elements"]
    assert label["type"] == "state-label"
    assert "attribute" not in label
    assert "transform" not in label["style"]
    assert "var(--card-background-color)" in label["style"]["background"]


def test_unavailable_blind_shows_only_its_state(flat_state: SmartPlaceState) -> None:
    view = _views(_flat_dashboard(flat_state))["blinds"]
    blind = next(blind for blind in flat_state.blinds.values() if blind.name == "Bedroom blinds")
    entity_id = f"cover.blind_{blind.id}"
    (alone,) = (
        element
        for element in _elements_of(view, entity_id)
        if element["type"] == "state-label" and "attribute" not in element
    )
    assert "var(--card-background-color)" in alone["style"]["background"]
    assert "min-width" not in alone["style"]


def _rows(*entity_ids: str) -> list[dict[str, Any]]:
    return [{"entity": entity_id, "name": {"type": "entity"}} for entity_id in entity_ids]


def test_scenes_view_uses_built_in_cards(flat_state: SmartPlaceState) -> None:
    """Each row shows the entity's own name: the shared device's name ("Scene Night") is left out."""
    view = _views(_flat_dashboard(flat_state))["scenes"]
    assert view["cards"] == [
        {"type": "entities", "title": "Scenes", "entities": _rows("scene.night", "scene.evening")},
        {"type": "entities", "title": "Switches", "entities": _rows("switch.blinds", "switch.doorbell_mute")},
    ]
    # The buttons for all lights and all blinds are on their plan pages only.
    assert "button." not in repr(view)


def test_buttons_for_all_and_the_ventilation_are_below_their_plan(flat_state: SmartPlaceState) -> None:
    views = _views(_flat_dashboard(flat_state))
    assert _cards(views["blinds"])[1:] == [_press_tile("button.all_blinds_up"), _press_tile("button.all_blinds_down")]
    assert _cards(views["climate"])[1:] == [
        _press_tile("button.all_heating_off"),
        _press_tile("button.heating_back_on"),
        {
            "type": "tile",
            "entity": "fan.ventilation",
            "features": [{"type": "fan-speed"}],
            "grid_options": {"columns": "full"},
        },
    ]


def test_ventilation_without_a_heating_zone_still_gets_the_climate_view() -> None:
    config = build_dashboard(
        plans={},
        images={},
        lights={},
        blinds={},
        zones={},
        entities=DashboardEntities(fans=["fan.ventilation"]),
        temperature_unit="°C",
    )
    (view,) = config["views"]
    assert (view["title"], view["path"]) == ("Climate", "climate")
    assert [card["entity"] for card in _cards(view)] == ["fan.ventilation"]


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
    assert len(_cards(_views(config)["lights"])[0]["elements"]) == 14
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
    picture, rest = _cards(view)
    assert [element["entity"] for element in picture["elements"]] == ["light.l1"]
    assert rest == {
        "type": "entities",
        "entities": ["light.l2", "light.l3", "light.l4"],
        "grid_options": {"columns": "full"},
    }


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
    assert _cards(view) == [{"type": "entities", "entities": ["cover.blind"], "grid_options": {"columns": "full"}}]


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
    assert _cards(view) == [{"type": "entities", "entities": ["climate.bedroom"], "grid_options": {"columns": "full"}}]


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
    assert _cards(first)[1]["entities"] == ["light.l3"]
    (picture,) = _cards(second)
    assert picture["image_entity"] == "image.upper"
    assert picture["aspect_ratio"] == "500:500"
    assert [element["entity"] for element in picture["elements"]] == ["light.l2"]


def test_marker_at_the_edge_is_kept_inside_the_card() -> None:
    """The browser limits the anchor by half the marker's size, so a blind at the image edge stays whole."""
    plans = {1: PlanConfig(id=1, label="Flat", top=70, left=10, functions=())}
    # State icon centre (10, 700) of 1000 x 710.
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
    elements = _elements_of(config["views"][0], "cover.edge")
    position = next(element for element in elements if element.get("title") == "Fully up")
    assert _anchor(position) == (
        "clamp(min(60px, 14.56vw), 1%, calc(100% - min(60px, 14.56vw)))",
        "clamp(min(18px, 4.37vw), 98.59%, calc(100% - min(18px, 4.37vw)))",
    )
    # Every part of the marker has the same anchor: they move together.
    assert {_anchor(element) for element in elements if "transform" in element["style"]} == {_anchor(position)}


def test_plan_without_a_content_box_is_shown_whole(flat_state: SmartPlaceState) -> None:
    for path in ("lights", "blinds", "climate"):
        assert _cards(_views(_flat_dashboard(flat_state))[path])[0]["aspect_ratio"] == "1000:710"


def test_plan_shows_the_drawing_and_the_markers_only(flat_state: SmartPlaceState) -> None:
    """The empty sides of the image are cut, equally on both sides: the card centres the image.

    The drawing spans x 212 to 741 and the outermost marker centre is a
    blind's at x 788, right of the drawing. With 12 px around them the
    window is x 200 to 800; the image centre is x 500.
    """
    views = _views(_flat_dashboard(flat_state, images={1: _DRAWN_IMAGE}))
    # The same window on the plan's three views: the plan keeps its size from tab to tab.
    assert {_cards(views[path])[0]["aspect_ratio"] for path in ("lights", "blinds", "climate")} == {"600:710"}

    corridor = next(light for light in flat_state.lights.values() if light.name == "Corridor lights")
    (icon,) = _elements_of(views["lights"], f"light.device_{corridor.id}")
    # Centre (386.5, 214.5): 186.5 px into the 600 px window, the height unchanged.
    assert _anchor(icon) == (
        "clamp(22px, 31.08%, calc(100% - 22px))",
        "clamp(22px, 30.21%, calc(100% - 22px))",
    )
    # The blind right of the drawing: (788 - 200) / 600.
    lefts = {
        _anchor(element)[0]
        for conditional in _cards(views["blinds"])[0]["elements"]
        for element in conditional["elements"]
    }
    assert "clamp(min(60px, 14.56vw), 98%, calc(100% - min(60px, 14.56vw)))" in lefts


def test_window_is_cut_along_the_axis_that_saves_more() -> None:
    """A wide drawing in a tall image loses the empty top and bottom instead."""
    plans = {1: PlanConfig(id=1, label="Flat", top=70, left=10, functions=())}
    lights = {1: LightConfig(id=1, name="Hall", kind="schalter", top=474, left=444, plan_id=1)}
    config = build_dashboard(
        plans=plans,
        # Drawing rows 300 to 700 of 1000, the full width.
        images={1: PlanBackground(entity_id="image.plan", width=1000, height=1000, content=(0, 300, 1000, 700))},
        lights=lights,
        blinds={},
        zones={},
        entities=DashboardEntities(lights={1: "light.hall"}),
        temperature_unit="°C",
    )
    (card,) = _cards(config["views"][0])
    # 200 px above and below the image centre, plus 12 px each.
    assert card["aspect_ratio"] == "1000:424"
    # Centre (500.5, 399.5): 111.5 px into the 424 px window that starts at row 288.
    assert _anchor(card["elements"][0]) == (
        "clamp(22px, 50.05%, calc(100% - 22px))",
        "clamp(22px, 26.3%, calc(100% - 22px))",
    )


def test_window_never_exceeds_the_image() -> None:
    plans = {1: PlanConfig(id=1, label="Flat", top=70, left=10, functions=())}
    lights = {1: LightConfig(id=1, name="Hall", kind="schalter", top=289, left=330, plan_id=1)}
    config = build_dashboard(
        plans=plans,
        images={1: PlanBackground(entity_id="image.plan", width=1000, height=710, content=(0, 0, 1000, 710))},
        lights=lights,
        blinds={},
        zones={},
        entities=DashboardEntities(lights={1: "light.hall"}),
        temperature_unit="°C",
    )
    assert _cards(config["views"][0])[0]["aspect_ratio"] == "1000:710"


def test_icon_buttons_do_nothing_on_a_long_press(flat_state: SmartPlaceState) -> None:
    """An icon element without an entity must not get the default hold action (more-info): it ends in an error toast."""
    views = _views(_flat_dashboard(flat_state))
    icons = [
        inner
        for path in ("blinds", "climate")
        for element in _cards(views[path])[0]["elements"]
        for inner in element.get("elements", [element])
        if inner["type"] == "icon" and "entity" not in inner
    ]
    blocked = " (blocked by a weather alarm)"
    assert {icon["title"].removesuffix(blocked) for icon in icons} == {"Down", "Vent", "Up", "Lower", "Raise"}
    assert all(icon["hold_action"] == {"action": "none"} for icon in icons)
    # A light's icon has an entity: a long press opens its details.
    light = _cards(views["lights"])[0]["elements"][0]
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
