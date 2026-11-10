"""Tests for the typed device parsers in :mod:`smart_place_client.devices`.

Wire strings are frames from the 2026-11-09 capture.
"""

from __future__ import annotations

import pytest

from smart_place_client import (
    BLIND_KIND_FABRIC,
    BLIND_KIND_SLATTED,
    LIGHT_KIND_LED,
    LIGHT_KIND_SOCKET,
    LIGHT_KIND_SWITCH,
    BlindConfig,
    BlindValue,
    ClimateZoneConfig,
    LightConfig,
    NamedFields,
    PlanConfig,
    SceneConfig,
    SubMenuConfig,
    parse_frame,
)
from smart_place_client.devices import (
    parse_blind_config,
    parse_blind_value,
    parse_climate_config,
    parse_light_config,
    parse_plan_config,
    parse_scene_config,
    parse_sub_menu,
)

_EVENING = (
    "INHALTSZENEN10:Evening,250px,535px,0.png,OnOn,Remove,,,19-30,,00-00,NoPw,"
    "SzenenSonnenAufOFF,SzenenSonnenUnterOFF,SzenenSonnenAufOFF,SzenenSonnenUnterOFF,SollDisplayON,SzenenForm1"
)


def _fields(text: str) -> NamedFields:
    frame = parse_frame(text)
    assert isinstance(frame, NamedFields)
    return frame


def test_light_config_reads_top_before_left() -> None:
    """Positions are ``top,left``: the second field is the vertical one."""
    light = parse_light_config(
        _fields("INHALTLeuchten13:Corridor lights,289px,330px,schalter,,dimmkurve1,Uebersicht1,,MemoryOFF"),
    )
    assert light == LightConfig(id=13, name="Corridor lights", kind=LIGHT_KIND_SWITCH, top=289, left=330, plan_id=1)


def test_light_config_kinds() -> None:
    socket = parse_light_config(
        _fields("INHALTLeuchten12:Bedroom socket,237px,464px,steckdose,,dimmkurve1,Uebersicht1,,MemoryOFF"),
    )
    led = parse_light_config(
        _fields("INHALTLeuchten14:Big bathroom lights,126px,544px,led,,dimmkurve1,Uebersicht1,,MemoryOFF"),
    )
    assert socket is not None
    assert socket.kind == LIGHT_KIND_SOCKET
    assert led is not None
    assert led.kind == LIGHT_KIND_LED


def test_blind_config_reads_kind_and_run_time() -> None:
    slatted = parse_blind_config(
        _fields("INHALTJalousien8:Office balcony door blinds,674px,405px,jalousie,,60,Uebersicht1")
    )
    fabric = parse_blind_config(_fields("INHALTJalousien3:Bedroom blinds,303px,563px,jalousiestoff,,39,Uebersicht1"))
    assert slatted == BlindConfig(
        id=8,
        name="Office balcony door blinds",
        kind=BLIND_KIND_SLATTED,
        top=674,
        left=405,
        plan_id=1,
        runtime_s=60,
    )
    assert fabric is not None
    assert (fabric.kind, fabric.runtime_s) == (BLIND_KIND_FABRIC, 39)


def test_climate_config_splits_room_from_name() -> None:
    zone = parse_climate_config(
        _fields(
            "INHALTKlimas3:Living room/dining room/kitchen heating,473px,466px,Heizen,,rgb(0- 0- 0),Uebersicht1,FanCoilOff",
        ),
    )
    assert zone == ClimateZoneConfig(
        id=3,
        name="Living room/dining room/kitchen heating",
        room="Living room/dining room/kitchen",
        mode="Heizen",
        top=473,
        left=466,
        plan_id=1,
        fan_coil=False,
    )


def test_scene_config_reads_type_and_pin_flag() -> None:
    assert parse_scene_config(_fields(_EVENING)) == SceneConfig(
        id=10,
        name="Evening",
        kind="OnOn",
        top=250,
        left=535,
        pin_protected=False,
        displayed=True,
    )
    protected = parse_scene_config(_fields(_EVENING.replace("NoPw", "Pw").replace("OnOn", "OnOff")))
    assert protected is not None
    assert protected.kind == "OnOff"
    assert protected.pin_protected is True


@pytest.mark.parametrize("flag", ["Pw", "1234", "pw", "NOPW", "", "undefined"])
def test_scene_needs_a_pin_unless_the_flag_says_no_pin(flag: str) -> None:
    """The vendor app asks for the PIN whenever the field is not exactly ``NoPw``."""
    scene = parse_scene_config(_fields(_EVENING.replace("NoPw", flag)))
    assert scene is not None
    assert scene.pin_protected is True


@pytest.mark.parametrize("flag", ["SollDisplayOFF", "", "undefined", "solldisplayon"])
def test_scene_is_hidden_unless_the_display_flag_says_shown(flag: str) -> None:
    """The vendor app only draws the tile for ``SollDisplayON``."""
    scene = parse_scene_config(_fields(_EVENING.replace("SollDisplayON", flag)))
    assert scene is not None
    assert scene.displayed is False
    assert scene.pin_protected is False


def test_scene_without_a_display_field_counts_as_shown() -> None:
    """The vendor app falls back to ``SollDisplayON`` when the frame ends before the field."""
    scene = parse_scene_config(_fields(_EVENING.split(",SzenenSonnenAufOFF", 1)[0]))
    assert scene is not None
    assert scene.displayed is True


def test_scene_config_too_short_to_classify_is_dropped() -> None:
    """Without the type and PIN fields a scene must not become an entity."""
    assert parse_scene_config(NamedFields(name="SceneConfig", fields=("Evening", "250px", "535px"), index=10)) is None


def test_sub_menu_reads_group_message_and_plan() -> None:
    lights = parse_sub_menu(_fields("UnterMenuLeuchten1:All,70px,10px,Leuchten,OnOn,LEUCHTENZENTRAL1,Uebersicht1"))
    blinds = parse_sub_menu(_fields("UnterMenuJalousien1:All,70px,10px,Jalousien,OffOff,JALZENTRAL1,Uebersicht1"))
    assert lights == SubMenuConfig(id=1, name="All", top=70, left=10, group_message="LEUCHTENZENTRAL1", plan_id=1)
    assert blinds is not None
    assert (blinds.group_message, blinds.plan_id) == ("JALZENTRAL1", 1)


def test_plan_config_splits_functions() -> None:
    plan = parse_plan_config(_fields("Floorplan1:XX00-00-00,70px,10px,Leuchten-Jalousien-Klima"))
    assert plan == PlanConfig(id=1, label="XX00-00-00", top=70, left=10, functions=("Leuchten", "Jalousien", "Klima"))


def test_unreadable_position_and_plan_become_none() -> None:
    """A device the vendor never placed still parses; it is just not on a plan."""
    light = parse_light_config(NamedFields(name="LightConfig", fields=("Lamp", "", "auto", "schalter"), index=2))
    assert light == LightConfig(id=2, name="Lamp", kind="schalter", top=None, left=None, plan_id=None)


def test_unnamed_config_is_dropped() -> None:
    assert parse_light_config(NamedFields(name="LightConfig", fields=(), index=2)) is None
    assert parse_blind_config(NamedFields(name="BlindConfig", fields=("",), index=2)) is None


def test_blind_value_position_and_alarms() -> None:
    assert parse_blind_value("0-01") == BlindValue(position=0, alarm=None, flag="01")
    assert parse_blind_value("33-00") == BlindValue(position=33, alarm=None, flag="00")
    assert parse_blind_value("regenalarm-00") == BlindValue(position=None, alarm="rain", flag="00")
    assert parse_blind_value("windalarm-01") == BlindValue(position=None, alarm="wind", flag="01")
    assert parse_blind_value("hagelalarm-00") == BlindValue(position=None, alarm="hail", flag="00")


def test_blind_value_with_an_unknown_word_has_no_position() -> None:
    """A first part that is not a number is an alarm the vendor may add; its word is kept."""
    assert parse_blind_value("sturmalarm-00") == BlindValue(position=None, alarm="sturmalarm", flag="00")
    assert parse_blind_value("Regenalarm-01") == BlindValue(position=None, alarm="Regenalarm", flag="01")


def test_blind_value_without_its_two_parts_is_dropped() -> None:
    assert parse_blind_value("") is None
    assert parse_blind_value("42") is None
