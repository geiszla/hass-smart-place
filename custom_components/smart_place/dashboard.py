"""The read-only "Smart Place" dashboard in the sidebar.

Home Assistant has no public API for a dashboard owned by an integration.
This module uses the lovelace integration's internal classes instead: a
``LovelaceConfig`` registered in the lovelace data plus a built-in
frontend panel (the pattern of the dwains-dashboard custom integration).
Everything that touches those internals is in this module, and
``__init__.py`` guards its import and its calls, so a change in them
disables the dashboard only.

The configuration is built on demand from the live state
(``plan_view.async_build_plan_config``). The frontend treats the
dashboard as a YAML one: it cannot be edited in the UI.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components import frontend
from homeassistant.components.lovelace import LOVELACE_DATA
from homeassistant.components.lovelace.const import MODE_YAML
from homeassistant.components.lovelace.dashboard import LovelaceConfig
from homeassistant.const import EVENT_LOVELACE_UPDATED
from homeassistant.core import callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.json import json_bytes, json_fragment

from .plan_view import async_build_plan_config

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.core import CALLBACK_TYPE, HomeAssistant

    from . import SmartPlaceConfigEntry

URL_PATH = "smart-place"
TITLE = "Smart Place"
ICON = "mdi:floor-plan"

# New entities register one by one right after a main-menu reply; one
# "dashboard changed" event after they settled is enough.
_REFRESH_DELAY = 2.0


class SmartPlaceDashboard(LovelaceConfig):
    """Dashboard whose configuration is computed from the integration's state."""

    def __init__(self, hass: HomeAssistant, entry: SmartPlaceConfigEntry) -> None:
        """Bind the dashboard to a config entry."""
        super().__init__(
            hass,
            URL_PATH,
            {
                "mode": MODE_YAML,
                "title": TITLE,
                "icon": ICON,
                "show_in_sidebar": True,
                "require_admin": False,
            },
        )
        self._entry = entry
        self._served: bytes | None = None
        self._refresh_timer: CALLBACK_TYPE | None = None

    @property
    def mode(self) -> str:
        """Report YAML mode: read-only in the frontend."""
        return MODE_YAML

    async def async_get_info(self) -> dict[str, Any]:
        """Return the config info."""
        return {"mode": self.mode, "views": len(async_build_plan_config(self.hass, self._entry)["views"])}

    async def async_load(self, force: bool) -> dict[str, Any]:
        """Build the configuration."""
        config = async_build_plan_config(self.hass, self._entry)
        self._served = json_bytes(config)
        return config

    async def async_json(self, force: bool) -> json_fragment:
        """Build the configuration as JSON."""
        await self.async_load(force)
        assert self._served is not None
        return json_fragment(self._served)

    @callback
    def async_schedule_refresh(self) -> None:
        """Tell open frontends, a moment from now, if the configuration changed."""
        if self._refresh_timer is None:
            self._refresh_timer = async_call_later(self.hass, _REFRESH_DELAY, self._async_refresh)

    @callback
    def _async_refresh(self, _now: datetime) -> None:
        self._refresh_timer = None
        # Nobody loaded the dashboard yet: nothing to refresh.
        if self._served is None:
            return
        if json_bytes(async_build_plan_config(self.hass, self._entry)) != self._served:
            self.hass.bus.async_fire(EVENT_LOVELACE_UPDATED, {"url_path": URL_PATH})

    @callback
    def async_remove(self) -> None:
        """Remove the dashboard and its sidebar panel."""
        if self._refresh_timer is not None:
            self._refresh_timer()
            self._refresh_timer = None
        frontend.async_remove_panel(self.hass, URL_PATH, warn_if_unknown=False)
        self.hass.data[LOVELACE_DATA].dashboards.pop(URL_PATH, None)


@callback
def async_register_dashboard(hass: HomeAssistant, entry: SmartPlaceConfigEntry) -> SmartPlaceDashboard | None:
    """Create the dashboard and its sidebar panel.

    Returns ``None`` when another Smart Place entry already owns the
    dashboard: there is one URL path, so only the first entry gets one.
    Raises if the path is taken by something else.
    """
    dashboards = hass.data[LOVELACE_DATA].dashboards
    if isinstance(dashboards.get(URL_PATH), SmartPlaceDashboard):
        return None
    dashboard = SmartPlaceDashboard(hass, entry)
    frontend.async_register_built_in_panel(
        hass,
        "lovelace",
        sidebar_title=TITLE,
        sidebar_icon=ICON,
        frontend_url_path=URL_PATH,
        config={"mode": MODE_YAML},
    )
    dashboards[URL_PATH] = dashboard
    return dashboard
