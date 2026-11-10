"""Decide which Home Assistant entity each Smart Place scene becomes.

A scene is one of three things:

- ``OnOn`` (trigger only): a Home Assistant scene.
- ``OnOff`` (real on/off): a switch.
- A level group: on/off scenes whose names differ only in a trailing whole
  number, numbered exactly 1..N with N >= 3 ("Lüftung Stufe 1/2/3"), of
  which exactly one is on when the group is first seen. The server keeps
  one of them active, so together they are one fan with N speeds and get
  no switches. Scenes named like that of which none or several are on are
  independent of each other and stay switches.

PIN-protected scenes become nothing: triggering them needs a PIN exchange
that is not implemented.

Pure functions, no HA imports.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
import re
from typing import Final

from .devices import SCENE_KIND_SWITCH, SCENE_KIND_TRIGGER, SceneConfig

# Two scenes "… 1" and "… 2" alone are an ordinary pair of switches.
MIN_LEVELS: Final = 3

# A name, whitespace, the level: "Fan 1". Not "Fan1", and not a bare "11".
_LEVEL_NAME_RE = re.compile(r"^(.*\S)\s+(\d+)$")

# English names for vendor default level-group names. Only names observed
# live; unknown names pass through unchanged.
_LEVEL_GROUP_GERMAN_TO_ENGLISH: dict[str, str] = {
    "Lüftung Stufe": "Ventilation",
}


@dataclass(frozen=True, slots=True)
class LevelGroup:
    """On/off scenes that are the levels 1..N of one thing.

    ``scene_ids[k - 1]`` is the scene of level ``k``. ``name`` is the
    common part of the scene names as spelled on level 1, in English
    where the vendor default name is known.
    """

    name: str
    scene_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class SceneEntities:
    """Which scene ids become which kind of entity.

    Scenes that wait for the level-group decision are in none of the three.
    """

    triggers: tuple[int, ...]
    switches: tuple[int, ...]
    level_groups: tuple[LevelGroup, ...]


def find_level_groups(scenes: Mapping[int, SceneConfig]) -> tuple[LevelGroup, ...]:
    """Return the on/off scenes without PIN that are named like the levels of one thing.

    The names alone do not make a level group: see :func:`is_level_group`.
    """
    by_base: dict[str, dict[int, SceneConfig]] = {}
    ambiguous: set[str] = set()
    for scene in scenes.values():
        if scene.kind != SCENE_KIND_SWITCH or scene.pin_protected:
            continue
        match = _LEVEL_NAME_RE.match(scene.name)
        if match is None:
            continue
        base = match.group(1).casefold()
        levels = by_base.setdefault(base, {})
        level = int(match.group(2))
        if level in levels:
            ambiguous.add(base)
        levels[level] = scene
    groups = []
    for base, levels in by_base.items():
        count = len(levels)
        if base in ambiguous or count < MIN_LEVELS or set(levels) != set(range(1, count + 1)):
            continue
        first = _LEVEL_NAME_RE.match(levels[1].name)
        assert first is not None
        name = first.group(1)
        groups.append(
            LevelGroup(
                name=_LEVEL_GROUP_GERMAN_TO_ENGLISH.get(name, name),
                scene_ids=tuple(levels[level].id for level in range(1, count + 1)),
            ),
        )
    return tuple(sorted(groups, key=lambda group: group.scene_ids[0]))


def is_level_group(group: LevelGroup, flags: Mapping[int, bool]) -> bool | None:
    """Decide from the scene flags whether scenes named like levels are levels.

    ``True`` when exactly one of them is on (the server keeps one level
    active), ``False`` when none or several are on (they are independent
    scenes), ``None`` while a flag is missing: undecided.
    """
    if any(scene_id not in flags for scene_id in group.scene_ids):
        return None
    return sum(flags[scene_id] for scene_id in group.scene_ids) == 1


def classify_scenes(
    scenes: Mapping[int, SceneConfig],
    flags: Mapping[int, bool],
    *,
    keep_as_switch: Collection[int] = (),
    keep_as_group: Collection[int] = (),
) -> SceneEntities:
    """Assign every scene to a scene entity, a switch or a level group.

    ``flags`` are the on/off flags of the scenes as far as they are known.
    Scenes named like the levels of one thing are a level group if exactly
    one is on, switches if none or several are on, and nothing at all
    while a flag is missing: they wait for the decision.

    What exists decides first, so an entity is never converted when the
    flags or the scene list change later. ``keep_as_switch`` lists scene
    ids that already are switches: a level group that contains one of them
    is not formed (its other members become switches too).
    ``keep_as_group`` lists the level-1 scene ids of the level groups that
    already exist: they stay level groups.
    """
    groups = []
    waiting: set[int] = set()
    for group in find_level_groups(scenes):
        if any(scene_id in keep_as_switch for scene_id in group.scene_ids):
            continue
        decision = True if group.scene_ids[0] in keep_as_group else is_level_group(group, flags)
        if decision is None:
            waiting.update(group.scene_ids)
        elif decision:
            groups.append(group)
    not_switches = waiting.union(*(group.scene_ids for group in groups))
    usable = [scene for _, scene in sorted(scenes.items()) if not scene.pin_protected]
    return SceneEntities(
        triggers=tuple(scene.id for scene in usable if scene.kind == SCENE_KIND_TRIGGER),
        switches=tuple(
            scene.id for scene in usable if scene.kind == SCENE_KIND_SWITCH and scene.id not in not_switches
        ),
        level_groups=tuple(groups),
    )
