"""Tests for :mod:`smart_place_client.motion`, with an injected clock."""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest

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


def _recorded_positions(blind_id: int) -> list[tuple[float, int]]:
    """Capture time and cover position of every ``JALICO<blind_id>`` frame of the recorded scene echo."""
    positions = []
    for line in (FIXTURES / "scene-echo.ndjson").read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        frame = parse_frame(row["text"])
        if not (isinstance(frame, NamedValue) and frame.name == "BlindState" and frame.index == blind_id):
            continue
        value = parse_blind_value(frame.value)
        assert value is not None
        assert value.position is not None
        positions.append((row["ts"], cover_position(value.position)))
    return positions


@pytest.mark.parametrize("blind_id", [1, 10])
def test_recorded_scene_echo_closes_a_blind_without_a_pause(blind_id: int) -> None:
    """Replay a captured run (open → closed, with gaps) on the recorded clock.

    The direction is read when each frame arrives and again just before
    the next one, as the entity's timer would: a timeout shorter than the
    longest gap between two positions shows as a pause in the run.
    """
    clock = _Clock()
    motion = BlindMotion(clock=clock)
    positions = _recorded_positions(blind_id)
    at_frame = []
    before_next_frame = []
    for (ts, position), (next_ts, _) in zip(positions, [*positions[1:], positions[-1]], strict=True):
        clock.now = ts
        motion.update(position)
        at_frame.append(motion.direction)
        clock.now = max(ts, next_ts - 0.001)
        before_next_frame.append(motion.direction)
    # Open (100) at the start, closing all the way, at rest once fully closed.
    assert positions[0][1] == 100
    assert positions[-1][1] == 0
    assert at_frame[0] is None
    assert set(at_frame[1:-1]) == {"closing"}
    assert set(before_next_frame[1:-1]) == {"closing"}
    assert at_frame[-1] is None


def test_timeout_is_longer_than_the_longest_recorded_gap() -> None:
    """The gap between two distinct positions of a moving blind reaches four seconds."""
    longest = 0.0
    for blind_id in (1, 10):
        changes = _distinct(_recorded_positions(blind_id))
        longest = max(longest, *(later - earlier for (earlier, _), (later, _) in itertools.pairwise(changes)))
    assert 3.0 < longest < BLIND_MOTION_TIMEOUT


def _distinct(positions: list[tuple[float, int]]) -> list[tuple[float, int]]:
    """Drop the frames that repeat the position of the frame before."""
    return [entry for index, entry in enumerate(positions) if index == 0 or entry[1] != positions[index - 1][1]]
