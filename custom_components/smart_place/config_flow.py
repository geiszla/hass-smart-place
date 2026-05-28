"""Config flow for Smart Place — single-step token entry.

Validates the token by walking the same discovery → routed page →
app WS → bootstrap-read flow the integration uses at runtime
(DESIGN §6.2). This avoids accepting a token that can route but cannot
open the real app channel.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol

from smart_place_client import SmartPlaceAuthError, SmartPlaceClient

from .const import CONF_TOKEN, CONFIG_FLOW_TIMEOUT, DOMAIN

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
                # from configuring the same Smart Place twice.
                await self.async_set_unique_id(token[:16])
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
                existing = self._get_reauth_entry()
                return self.async_update_reload_and_abort(
                    existing,
                    data={**existing.data, CONF_TOKEN: token},
                )
            errors["base"] = error

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_TOKEN): str}),
            errors=errors,
        )

    async def _validate_token(self, token: str) -> str | None:
        """Return None on success, or an error key for the form."""
        session = async_get_clientsession(self.hass)
        client = SmartPlaceClient.live(token=token, session=session)
        try:
            async with asyncio.timeout(CONFIG_FLOW_TIMEOUT):
                runner = asyncio.create_task(client.run())
                try:
                    await client.wait_for_bootstrap()
                finally:
                    await client.aclose()
                    runner.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await runner
        except SmartPlaceAuthError:
            return "invalid_auth"
        except TimeoutError:
            _LOGGER.warning("Smart Place config flow timed out validating token")
            return "cannot_connect"
        except Exception:
            _LOGGER.exception("unexpected error validating Smart Place token")
            return "unknown"
        return None
