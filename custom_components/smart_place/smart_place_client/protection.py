"""Protected devices: devices no command may be sent for.

The vendor's only on/off command for a light or socket is a toggle. A
"turn on" decided against a stale state would therefore switch the device
off. A socket that powers something that must stay on can be protected:
the caller then sends no command at all for it.

:func:`command_is_protected` is the one check. It goes by the device a
command names (``CommandDefinition.device`` and the vendor id in its first
argument), so it covers every command of that device, whoever issues it.
A command that reaches a device indirectly (a scene, a group button) names
no device and is not covered: what it switches is decided inside the
vendor system.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .commands import CommandDefinition


def command_is_protected(
    command: CommandDefinition,
    args: Sequence[object],
    protected: Mapping[str, Collection[int]],
) -> bool:
    """Return whether ``command`` with ``args`` is for a protected device.

    ``protected`` maps a device family (``DEVICE_LIGHT``, ``DEVICE_BLIND``)
    to the vendor ids that are protected.
    """
    return command.device is not None and args[0] in protected.get(command.device, ())
