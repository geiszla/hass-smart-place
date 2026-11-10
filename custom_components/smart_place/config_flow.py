"""Config flow for Smart Place — single-step token entry, and the options.

Validates the token by walking the same discovery → routed page →
app WS → bootstrap-read flow the integration uses at runtime
(DESIGN §6.2). This avoids accepting a token that can route but cannot
open the real app channel.

The options flow sets the protected sockets and the waiting time of
"Lower after weather alarm". Options take effect without a reload: the
entry has an update listener (``__init__.py``), so no flow here may use
Home Assistant's reloading helpers together with it.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntryState, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
import probatio

from .const import CONF_LOWER_DELAY_MINUTES, CONF_PROTECTED_SOCKETS, CONF_TOKEN, CONFIG_FLOW_TIMEOUT, DOMAIN
from .smart_place_client import (
    DISCOVERY_ORIGIN,
    LIGHT_KIND_SOCKET,
    SmartPlaceAuthError,
    SmartPlaceClient,
    SmartPlaceOfflineError,
)


def _token_fingerprint(token: str) -> str:
    """Return a stable opaque id for the token that doesn't leak its prefix.

    The unique_id ends up on disk in HA's ``core.config_entries`` and
    surfaces in diagnostics, so using ``token[:16]`` would leak the
    first quarter of the secret. A truncated SHA-256 is collision-safe
    for the number of installations one user is realistically going
    to have and reveals nothing about the original value.
    """
    return hashlib.sha256(token.encode()).hexdigest()[:32]


if TYPE_CHECKING:
    from collections.abc import Mapping

    from . import SmartPlaceConfigEntry

_LOGGER = logging.getLogger(__name__)

# Longest wait between the end of a weather alarm and the down command.
_MAX_LOWER_DELAY_MINUTES = 240


class SmartPlaceConfigFlow(ConfigFlow, domain=DOMAIN):
    """Smart Place config flow."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: SmartPlaceConfigEntry) -> SmartPlaceOptionsFlow:
        """Return the options flow."""
        return SmartPlaceOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the initial token-entry step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            token: str = user_input[CONF_TOKEN].strip()
            error = await self._validate_token(token)
            if error is None:
                # Single installation per token; key uniqueness keeps the user
                # from configuring the same Smart Place twice. Use a hash so
                # the unique_id (which persists to disk + surfaces in
                # diagnostics) doesn't leak any of the secret.
                await self.async_set_unique_id(_token_fingerprint(token))
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="Smart Place",
                    data={CONF_TOKEN: token},
                )
            errors["base"] = error

        return self.async_show_form(
            step_id="user",
            data_schema=probatio.Schema({probatio.Required(CONF_TOKEN): str}),
            errors=errors,
            # hassfest forbids literal URLs in translation strings; the example
            # URL is injected here instead (strings reference it as {url}).
            description_placeholders={"url": f"{DISCOVERY_ORIGIN}/Start5?..."},
        )

    async def async_step_reauth(self, _entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Re-prompt for token when the live token is rejected."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Re-validate a new token and update the existing entry."""
        errors: dict[str, str] = {}
        if user_input is not None:
            token: str = user_input[CONF_TOKEN].strip()
            error = await self._validate_token(token)
            if error is None:
                # ``data_updates`` is the HA-recommended idiom for
                # reauth (HA merges into the existing entry data);
                # ``data=...`` works too but replaces the whole dict
                # and forces us to remember the spread.
                # The reload is scheduled here, also for an unchanged token
                # (the client stopped when the token was rejected):
                # ``async_update_reload_and_abort`` must not be used on an
                # entry with an update listener.
                entry = self._get_reauth_entry()
                result = self.async_update_and_abort(entry, data_updates={CONF_TOKEN: token})
                self.hass.config_entries.async_schedule_reload(entry.entry_id)
                return result
            errors["base"] = error

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=probatio.Schema({probatio.Required(CONF_TOKEN): str}),
            errors=errors,
        )

    async def _validate_token(self, token: str) -> str | None:
        """Return None on success, or an error key for the form.

        Uses the one-shot :meth:`SmartPlaceClient.connect_and_bootstrap`
        rather than ``run()`` so auth / offline errors propagate
        immediately instead of being swallowed by the reconnect loop
        and surfacing as a generic ``cannot_connect`` timeout.
        """
        session = async_get_clientsession(self.hass)
        client = SmartPlaceClient.live(token=token, session=session)
        try:
            async with asyncio.timeout(CONFIG_FLOW_TIMEOUT):
                try:
                    await client.connect_and_bootstrap()
                finally:
                    await client.aclose()
        except SmartPlaceAuthError:
            return "invalid_auth"
        except SmartPlaceOfflineError:
            _LOGGER.info("Smart Place installation reported offline during config flow")
            return "cannot_connect"
        except TimeoutError:
            _LOGGER.warning("Smart Place config flow timed out validating token")
            return "cannot_connect"
        except Exception:
            _LOGGER.exception("unexpected error validating Smart Place token")
            return "unknown"
        return None


class SmartPlaceOptionsFlow(OptionsFlow):
    """Smart Place options: the protected sockets and the wait before a blind is lowered after its alarm."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Choose the sockets no command may be sent for, and the waiting time."""
        entry: SmartPlaceConfigEntry = self.config_entry
        if entry.state is not ConfigEntryState.LOADED:
            # The sockets are only known while the integration runs.
            return self.async_abort(reason="not_loaded")
        if user_input is not None:
            # The switches "Lower after weather alarm" keep their state in the options too.
            return self.async_create_entry(
                data={
                    **entry.options,
                    CONF_PROTECTED_SOCKETS: sorted(
                        int(light_id) for light_id in user_input.get(CONF_PROTECTED_SOCKETS, [])
                    ),
                    CONF_LOWER_DELAY_MINUTES: int(user_input.get(CONF_LOWER_DELAY_MINUTES, 0)),
                },
            )

        protected: list[int] = entry.options.get(CONF_PROTECTED_SOCKETS, [])
        sockets = {
            light.id: light.name
            for light in entry.runtime_data.state.lights.values()
            if light.kind == LIGHT_KIND_SOCKET
        }
        # A protected socket the server has not listed (yet) stays in the
        # list: left out, saving the form would silently unprotect it.
        for light_id in protected:
            sockets.setdefault(light_id, f"Socket {light_id}")
        schema = probatio.Schema(
            {
                probatio.Optional(CONF_PROTECTED_SOCKETS): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            SelectOptionDict(value=str(light_id), label=name)
                            for light_id, name in sorted(sockets.items())
                        ],
                        multiple=True,
                        mode=SelectSelectorMode.LIST,
                    ),
                ),
                probatio.Optional(CONF_LOWER_DELAY_MINUTES): NumberSelector(
                    NumberSelectorConfig(
                        min=0,
                        max=_MAX_LOWER_DELAY_MINUTES,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="min",
                    ),
                ),
            },
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                schema,
                {
                    CONF_PROTECTED_SOCKETS: [str(light_id) for light_id in protected],
                    CONF_LOWER_DELAY_MINUTES: entry.options.get(CONF_LOWER_DELAY_MINUTES, 0),
                },
            ),
        )
