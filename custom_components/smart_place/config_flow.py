"""Config flow for Smart Place — single-step token entry.

Validates the token by walking the same discovery → routed page →
app WS → bootstrap-read flow the integration uses at runtime
(DESIGN §6.2). This avoids accepting a token that can route but cannot
open the real app channel.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol

from .const import CONF_TOKEN, CONFIG_FLOW_TIMEOUT, DOMAIN
from .smart_place_client import SmartPlaceAuthError, SmartPlaceClient, SmartPlaceOfflineError


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

_LOGGER = logging.getLogger(__name__)


class SmartPlaceConfigFlow(ConfigFlow, domain=DOMAIN):
    """Smart Place config flow."""

    VERSION = 1

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
            data_schema=vol.Schema({vol.Required(CONF_TOKEN): str}),
            errors=errors,
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
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(),
                    data_updates={CONF_TOKEN: token},
                )
            errors["base"] = error

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_TOKEN): str}),
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
