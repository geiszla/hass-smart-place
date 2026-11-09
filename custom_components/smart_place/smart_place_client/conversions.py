"""Conversions between vendor values and what Home Assistant entities report.

Pure functions, no HA imports. The vendor scales:

- Blind position: percent, 0 = fully up, 100 = fully down. Home Assistant
  covers count the other way (0 = closed, 100 = open).
- Heating setpoint: whole degrees 18..26. The vendor app labels 18
  "forced off"; the integration shows it as HVAC mode Off and offers
  19..26 as target temperatures.
- Light level: 0 = off, anything above = on (switch-type loads report 250
  or 255).
"""

from __future__ import annotations

from typing import Final

from .commands import SETPOINT_MAX, SETPOINT_OFF

# Lowest setpoint that still heats; 18 itself means Off.
SETPOINT_MIN: Final = SETPOINT_OFF + 1
# Setpoint used when heating is switched on and no earlier one is known.
SETPOINT_DEFAULT: Final = 21


def cover_position(vendor_position: int) -> int:
    """Convert a vendor blind position to a Home Assistant cover position."""
    return 100 - vendor_position


def light_is_on(level: int) -> bool:
    """Return whether a ``leuchte<n>`` level means on."""
    return level > 0


def heating_is_off(setpoint: int) -> bool:
    """Return whether a vendor setpoint means heating is switched off."""
    return setpoint <= SETPOINT_OFF


def target_temperature(setpoint: int) -> int | None:
    """Return the target temperature a setpoint stands for, ``None`` when off."""
    return None if heating_is_off(setpoint) else setpoint


def setpoint_for_temperature(temperature: float) -> int:
    """Round a requested target temperature to a heating setpoint (19..26)."""
    return min(max(round(temperature), SETPOINT_MIN), SETPOINT_MAX)


def step_setpoint(setpoint: int, steps: int) -> int:
    """Move ``steps`` along the vendor scale 18..26 (18 = Off), clamped at both ends."""
    return min(max(setpoint + steps, SETPOINT_OFF), SETPOINT_MAX)
