"""Diagnostics for Smart Place: session and state snapshot, with secrets removed."""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import REDACTED, async_redact_data

from .const import CONF_TOKEN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from . import SmartPlaceConfigEntry

# The token, and the free-text fields that can carry a parcel PIN
# (``person_info``) or an info-board link (``infoboard_contents``). The
# remote-access credential frames and the LAN address of the ``GlobalGsa``
# reply never enter the state snapshot, so there is nothing of them to redact.
TO_REDACT = {CONF_TOKEN, "person_info", "infoboard_contents"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: SmartPlaceConfigEntry) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    data = entry.runtime_data
    client = data.client
    session = client.state
    state = asdict(data.state)
    # Internal buffer of a reply that is still arriving.
    state.pop("_range_pending")
    # A plan label can be an installation tag.
    for plan in state["plans"].values():
        plan["label"] = REDACTED
    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "client": {
            "connected": client.connected,
            "dry_run": client.dry_run,
            "phase": session.phase.value,
            "main_menu_loaded": session.main_menu_loaded,
            "chart_ids": sorted(session.chart_ids),
            "plan_ids": sorted(session.plan_ids),
            "plan_images": {
                plan_id: {"width": image.width, "height": image.height, "bytes": len(image.content)}
                for plan_id, image in client.plan_images.items()
            },
        },
        "state": async_redact_data(state, TO_REDACT),
    }
