"""Habidat (ista España) integration."""

from __future__ import annotations

import aiohttp
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import HabidatClient
from .const import CONF_VIVIENDA
from .coordinator import HabidatConfigEntry, HabidatCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]


def crear_cliente(
    hass: HomeAssistant, email: str, password: str, *, auto_cleanup: bool = True
) -> HabidatClient:
    """Client with its own cookie jar (the PHP session lives there)."""
    session = async_create_clientsession(
        hass, auto_cleanup=auto_cleanup, cookie_jar=aiohttp.CookieJar()
    )
    return HabidatClient(session, email, password)


async def async_setup_entry(hass: HomeAssistant, entry: HabidatConfigEntry) -> bool:
    """Set up one dwelling."""
    client = crear_cliente(hass, entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD])
    coordinator = HabidatCoordinator(hass, entry, client, entry.data[CONF_VIVIENDA])
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HabidatConfigEntry) -> bool:
    """Unload a config entry."""
    # The HTTP session was created during setup, so Home Assistant releases it.
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
