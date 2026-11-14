"""Tests for :mod:`smart_place_client.motion`, with an injected clock."""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest

from smart_place_client import BLIND_MOTION_TIMEOUT, KNOWN_COMMANDS, BlindMotion, Commands, cover_position, parse_frame
from smart_place_client.commands import CommandDefinition
from smart_place_client.devices import parse_blind_value
from smart_place_client.motion import BLIND_COMMAND_SETTLE
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


_BLIND = 8


def _at(position: int | None) -> tuple[BlindMotion, _Clock]:
    """A blind at rest at ``position`` (Home Assistant's scale: 100 is fully up)."""
    clock = _Clock()
    motion = BlindMotion(clock=clock)
    motion.update(position)
    return motion, clock


def _send(
    motion: BlindMotion, command: CommandDefinition, position: int | None, args: tuple[object, ...] = (_BLIND,)
) -> bool:
    """Note ``command`` for the blind, sent while it reports ``position``."""
    return motion.command_sent(command, args, _BLIND, position)


def _closing_from(position: int) -> tuple[BlindMotion, _Clock]:
    """A blind that reported ``position`` and then a position further down, longer ago than the settle time."""
    motion, clock = _at(position)
    motion.update(position - 3)
    assert motion.direction == "closing"
    clock.now += BLIND_COMMAND_SETTLE
    return motion, clock


def test_a_command_from_rest_gives_the_direction_before_the_first_position() -> None:
    motion, clock = _at(100)
    assert _send(motion, Commands.BlindDown, 100) is True
    assert motion.direction == "closing"
    # The server repeats the old position until the blind reports the next one.
    assert motion.update(100) is False
    assert motion.direction == "closing"
    clock.now += 0.9
    assert motion.update(97) is True
    assert motion.direction == "closing"
    motion, _ = _at(40)
    _send(motion, Commands.BlindUp, 40)
    assert motion.direction == "opening"


def test_a_command_that_moves_nothing_counts_only_for_the_timeout() -> None:
    motion, clock = _at(40)
    _send(motion, Commands.BlindDown, 40)
    clock.now += BLIND_MOTION_TIMEOUT - 0.1
    assert motion.direction == "closing"
    clock.now += 0.1
    assert motion.direction is None


_RUN_TIME = 39


def _reported_at(position: int) -> tuple[BlindMotion, _Clock]:
    """A blind with a run time that the server reports at rest at ``position``."""
    clock = _Clock()
    motion = BlindMotion(clock=clock, run_time=_RUN_TIME)
    motion.update(position)
    return motion, clock


@pytest.mark.parametrize(
    ("command", "position", "direction"), [(Commands.BlindDown, 0, "closing"), (Commands.BlindUp, 100, "opening")]
)
def test_a_command_towards_the_end_the_blind_is_reported_at_counts_for_the_run_time(
    command: CommandDefinition, position: int, direction: str
) -> None:
    """The reported position can be wrong: the blind then moves and no position follows."""
    motion, clock = _reported_at(position)
    assert _send(motion, command, position) is True
    assert motion.direction == direction
    assert motion.remaining == _RUN_TIME
    # The server goes on repeating the position.
    assert motion.update(position) is False
    clock.now += _RUN_TIME - 0.1
    assert motion.direction == direction
    clock.now += 0.1
    assert motion.direction is None
    assert motion.remaining is None


def test_without_a_run_time_such_a_command_counts_for_the_timeout() -> None:
    motion, _ = _at(0)
    _send(motion, Commands.BlindDown, 0)
    assert motion.direction == "closing"
    assert motion.remaining == BLIND_MOTION_TIMEOUT


def test_a_second_command_in_the_run_time_is_the_stop() -> None:
    """Seen on 2026-11-13: the second down command came 9 s after the first and stopped the blind."""
    motion, clock = _reported_at(0)
    _send(motion, Commands.BlindDown, 0)
    clock.now += 9
    assert motion.direction == "closing"
    _send(motion, Commands.BlindDown, 0)
    assert motion.direction is None


def test_the_opposite_command_in_the_run_time_turns_the_blind_round() -> None:
    motion, clock = _reported_at(0)
    _send(motion, Commands.BlindDown, 0)
    clock.now += 13
    _send(motion, Commands.BlindUp, 0)
    assert motion.direction == "opening"
    assert motion.remaining == BLIND_MOTION_TIMEOUT


def test_a_position_in_the_run_time_says_where_the_blind_goes() -> None:
    motion, clock = _reported_at(0)
    _send(motion, Commands.BlindDown, 0)
    clock.now += BLIND_COMMAND_SETTLE
    assert motion.update(3) is True
    assert motion.direction == "opening"
    assert motion.remaining == BLIND_MOTION_TIMEOUT


@pytest.mark.parametrize(("command", "position"), [(Commands.BlindsGroupDown, 0), (Commands.BlindsGroupUp, 100)])
def test_a_group_button_starts_nothing_at_the_end_it_moves_to(command: CommandDefinition, position: int) -> None:
    motion, _ = _reported_at(position)
    assert _send(motion, command, position, (1,)) is True
    assert motion.direction is None


def test_remaining_counts_down_to_the_end_of_the_timeout() -> None:
    motion, clock = _at(40)
    assert motion.remaining is None
    motion.update(45)
    assert motion.remaining == BLIND_MOTION_TIMEOUT
    clock.now += 2
    assert motion.remaining == BLIND_MOTION_TIMEOUT - 2


def test_a_command_without_a_position_starts_nothing() -> None:
    """A weather alarm holds the blind, or its value was not received yet."""
    motion, _ = _at(None)
    _send(motion, Commands.BlindDown, None)
    assert motion.direction is None


def test_the_command_for_the_direction_a_blind_moves_in_stops_it() -> None:
    motion, clock = _closing_from(60)
    assert _send(motion, Commands.BlindDown, 57) is True
    assert motion.direction is None
    # A position that was on its way when the command was sent does not start the blind again.
    clock.now += 0.2
    assert motion.update(54) is False
    assert motion.direction is None
    # At rest now: the same command starts it again.
    _send(motion, Commands.BlindDown, 54)
    assert motion.direction == "closing"


def test_a_stop_also_ends_a_direction_that_only_a_command_gave() -> None:
    motion, _ = _at(40)
    _send(motion, Commands.BlindUp, 40)
    _send(motion, Commands.BlindUp, 40)
    assert motion.direction is None


def test_a_blind_that_goes_on_after_a_stop_is_moving_again() -> None:
    """The stop did not reach the blind, or somebody started it again."""
    motion, clock = _closing_from(60)
    _send(motion, Commands.BlindDown, 57)
    clock.now += BLIND_COMMAND_SETTLE
    assert motion.update(51) is True
    assert motion.direction == "closing"


def test_the_opposite_command_turns_a_moving_blind_round() -> None:
    motion, clock = _closing_from(60)
    _send(motion, Commands.BlindUp, 57)
    assert motion.direction == "opening"
    # A position further down that was on its way does not turn it back.
    clock.now += 0.2
    assert motion.update(54) is False
    assert motion.direction == "opening"
    assert motion.update(57) is True
    assert motion.direction == "opening"


def test_the_end_a_blind_was_sent_to_ends_the_movement_also_right_after_the_command() -> None:
    motion, clock = _at(3)
    _send(motion, Commands.BlindDown, 3)
    clock.now += 0.2
    motion.update(0)
    assert motion.direction is None


def test_a_group_button_starts_its_direction_and_does_not_stop_a_blind() -> None:
    """The plan in the argument is not looked at: which blinds a group button moves is decided in the vendor system."""
    motion, _ = _at(50)
    assert _send(motion, Commands.BlindsGroupDown, 50, (1,)) is True
    assert motion.direction == "closing"
    _send(motion, Commands.BlindsGroupDown, 50, (1,))
    assert motion.direction == "closing"
    _send(motion, Commands.BlindsGroupUp, 50, (1,))
    assert motion.direction == "opening"


@pytest.mark.parametrize(
    ("command", "args"), [(Commands.BlindVent, (_BLIND,)), (Commands.BlindsGroupMessage, ("JALZENTRAL2",))]
)
def test_after_vent_or_a_group_message_the_positions_say_where_the_blind_goes(
    command: CommandDefinition, args: tuple[object, ...]
) -> None:
    motion, clock = _closing_from(60)
    assert _send(motion, command, 57, args) is True
    assert motion.direction is None
    clock.now += BLIND_COMMAND_SETTLE
    motion.update(54)
    assert motion.direction == "closing"


def test_commands_for_other_devices_change_nothing() -> None:
    motion, _ = _closing_from(60)
    concerned = {defn.name for defn in KNOWN_COMMANDS if _send(motion, defn, 57, defn.example_args)}
    assert concerned == {"BlindUp", "BlindDown", "BlindVent", "BlindsGroupUp", "BlindsGroupDown", "BlindsGroupMessage"}
    motion, _ = _closing_from(60)
    for command in (Commands.BlindUp, Commands.BlindDown, Commands.BlindVent):
        assert _send(motion, command, 57, (7,)) is False
    # Light 8 is not blind 8.
    assert _send(motion, Commands.LightToggle, 57) is False
    assert motion.direction == "closing"


def test_a_command_counts_from_the_position_that_comes_with_it() -> None:
    """Sent at the end of a weather alarm, before the tracker was fed the first position after it."""
    motion, _ = _at(None)
    assert _send(motion, Commands.BlindDown, 100) is True
    assert motion.direction == "closing"
    # The frame's value arrives afterwards and changes nothing.
    assert motion.update(100) is False
    assert motion.direction == "closing"


def test_a_position_that_comes_with_a_command_for_another_blind_is_not_taken() -> None:
    """The caller's own feed must see the change, or it would not look again at the timeout."""
    motion, _ = _at(60)
    assert _send(motion, Commands.BlindDown, 57, (7,)) is False
    assert motion.update(57) is True
