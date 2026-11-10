"""Tests for :mod:`smart_place_client.motion`, with an injected clock."""

from __future__ import annotations

import json
from pathlib import Path

from smart_place_client import BLIND_MOTION_TIMEOUT, BlindMotion, cover_position, parse_frame
from smart_place_client.devices import parse_blind_value
from smart_place_client.protocol import NamedValue

FIXTURES = Path(__file__).parent / "fixtures"


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_first_position_gives_no_direction() -> None:
    motion = BlindMotion(clock=_Clock())
    motion.update(40)
    assert motion.direction is None


def test_direction_follows_distinct_positions() -> None:
    motion = BlindMotion(clock=_Clock())
    assert motion.update(40) is False
    assert motion.update(45) is True
    assert motion.direction == "opening"
    # A repeated position neither changes the direction nor restarts the timeout.
    assert motion.update(45) is False
    assert motion.direction == "opening"
    assert motion.update(30) is True
    assert motion.direction == "closing"


def test_end_stops_end_the_movement() -> None:
    motion = BlindMotion(clock=_Clock())
    motion.update(3)
    motion.update(0)
    assert motion.direction is None
    motion.update(97)
    motion.update(100)
    assert motion.direction is None


def test_leaving_an_end_stop_starts_a_movement() -> None:
    motion = BlindMotion(clock=_Clock())
    motion.update(100)
    motion.update(97)
    assert motion.direction == "closing"


def test_missing_position_ends_the_movement() -> None:
    """A weather alarm (or a dropped connection) reports no position."""
    motion = BlindMotion(clock=_Clock())
    motion.update(40)
    motion.update(45)
    motion.update(None)
    assert motion.direction is None
    # The next position is a fresh start, not a jump from 45.
    motion.update(80)
    assert motion.direction is None


def test_movement_times_out_without_new_positions() -> None:
    clock = _Clock()
    motion = BlindMotion(clock=clock)
    motion.update(40)
    motion.update(45)
    clock.now += BLIND_MOTION_TIMEOUT - 0.1
    assert motion.direction == "opening"
    clock.now += 0.1
    assert motion.direction is None


def test_recorded_scene_echo_closes_the_bath_blind() -> None:
    """Replay the captured run of blind 1 (0 → 100 in 2 % steps, with gaps)."""
    clock = _Clock()
    motion = BlindMotion(clock=clock)
    directions = []
    for line in (FIXTURES / "scene-echo.ndjson").read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        frame = parse_frame(row["text"])
        if not (isinstance(frame, NamedValue) and frame.name == "BlindState" and frame.index == 1):
            continue
        value = parse_blind_value(frame.value)
        assert value is not None
        assert value.position is not None
        clock.now = row["ts"]
        motion.update(cover_position(value.position))
        directions.append(motion.direction)
    # Open (100) at the start, closing all the way, at rest once fully closed.
    assert directions[0] is None
    assert set(directions[1:-1]) == {"closing"}
    assert directions[-1] is None
