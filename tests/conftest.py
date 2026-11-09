"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path

import pytest

from smart_place_client import SmartPlaceState, parse_frame

FIXTURES = Path(__file__).parent / "fixtures"


def _fold_fixture(state: SmartPlaceState, name: str) -> None:
    for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row["direction"] == "server":
            state.apply(parse_frame(row["text"]))


@pytest.fixture
def fold_fixture() -> Callable[[SmartPlaceState, str], None]:
    """Return a function that folds the server frames of a capture fixture into a state."""
    return _fold_fixture


@pytest.fixture
def flat_state() -> SmartPlaceState:
    """The reference flat after a full bootstrap (redacted 2026-11-09 capture)."""
    state = SmartPlaceState()
    _fold_fixture(state, "control-bootstrap.ndjson")
    return state
