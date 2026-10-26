"""The Energi Fyn integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import EnergiFynClient
from .const import CONF_CUSTOMER_ID, CONF_REFRESH_TOKEN
from .coordinator import EnergiFynCoordinator

PLATFORMS = [Platform.SENSOR]

type EnergiFynConfigEntry = ConfigEntry[EnergiFynCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: EnergiFynConfigEntry) -> bool:
    def save_token(token: str) -> None:
        # The server rotates the refresh token on every use; losing it forces a re-login.
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_REFRESH_TOKEN: token}
        )

    client = EnergiFynClient(async_get_clientsession(hass), token_callback=save_token)
    client.refresh_token = entry.data[CONF_REFRESH_TOKEN]
    client.customer_id = entry.data[CONF_CUSTOMER_ID]
    coordinator = EnergiFynCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EnergiFynConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
