"""Decide which Home Assistant entity each Smart Place scene becomes.

A scene is one of three things:

- ``OnOn`` (trigger only): a Home Assistant scene.
- ``OnOff`` (real on/off): a switch.
- A level group: on/off scenes whose names differ only in a trailing whole
  number, numbered exactly 1..N with N >= 3 ("Lüftung Stufe 1/2/3"). The
  server keeps one of them active, so together they are one fan with N
  speeds and get no switches.

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

_LEVEL_NAME_RE = re.compile(r"^(.+?)\s*(\d+)$")

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
    """Which scene ids become which kind of entity."""

    triggers: tuple[int, ...]
    switches: tuple[int, ...]
    level_groups: tuple[LevelGroup, ...]


def find_level_groups(scenes: Mapping[int, SceneConfig]) -> tuple[LevelGroup, ...]:
    """Return the level groups among the on/off scenes that need no PIN."""
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


def classify_scenes(scenes: Mapping[int, SceneConfig], keep_as_switch: Collection[int] = ()) -> SceneEntities:
    """Assign every scene to a scene entity, a switch or a level group.

    ``keep_as_switch`` lists scene ids that already are switches. A level
    group that contains one of them is not formed (its other members
    become switches too): an entity that exists is never converted when
    the scene list changes later.
    """
    groups = tuple(
        group for group in find_level_groups(scenes) if not any(sid in keep_as_switch for sid in group.scene_ids)
    )
    grouped = {sid for group in groups for sid in group.scene_ids}
    usable = [scene for _, scene in sorted(scenes.items()) if not scene.pin_protected]
    return SceneEntities(
        triggers=tuple(scene.id for scene in usable if scene.kind == SCENE_KIND_TRIGGER),
        switches=tuple(scene.id for scene in usable if scene.kind == SCENE_KIND_SWITCH and scene.id not in grouped),
        level_groups=groups,
    )
