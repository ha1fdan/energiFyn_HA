"""Config flow for Energi Fyn."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import AuthError, EnergiFynClient, EnergiFynError
from .const import CONF_CUSTOMER_ID, CONF_REFRESH_TOKEN, DOMAIN


class EnergiFynConfigFlow(ConfigFlow, domain=DOMAIN):
    """Log in with the app credentials; only the refresh token is stored."""

    VERSION = 1

    _reauth_username: str | None = None

    async def _login(self, username: str, password: str) -> tuple[str, str]:
        client = EnergiFynClient(async_get_clientsession(self.hass))
        refresh_token = await client.login(username, password)
        customers = await client.customers()
        if not customers:
            raise LookupError
        return refresh_token, str(customers[0]["customerNumber"])

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                token, customer = await self._login(
                    user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
                )
            except AuthError:
                errors["base"] = "invalid_auth"
            except LookupError:
                errors["base"] = "no_customer"
            except (aiohttp.ClientError, EnergiFynError):
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(customer)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Energi Fyn {customer}",
                    data={
                        CONF_USERNAME: user_input[CONF_USERNAME],
                        CONF_CUSTOMER_ID: customer,
                        CONF_REFRESH_TOKEN: token,
                    },
                )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_USERNAME): str, vol.Required(CONF_PASSWORD): str}
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        self._reauth_username = entry_data[CONF_USERNAME]
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            try:
                token, customer = await self._login(
                    entry.data[CONF_USERNAME], user_input[CONF_PASSWORD]
                )
            except AuthError:
                errors["base"] = "invalid_auth"
            except LookupError:
                errors["base"] = "no_customer"
            except (aiohttp.ClientError, EnergiFynError):
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(customer)
                self._abort_if_unique_id_mismatch()
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_REFRESH_TOKEN: token}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            errors=errors,
        )
