"""The SolarEdge Optimizers Data integration."""
from requests import ConnectTimeout, HTTPError
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.util import dt as dt_util

# AJT: 10-Jan-2025: Changed from absolute import to relative import to use local solaredgeoptimizers.py instead of site-packages version
from .solaredgeoptimizers import solaredgeoptimizers
from .const import (
    DOMAIN,
    LOGGER,
)
from .coordinator import MyCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up SolarEdge Optimizers Data from a config entry."""

    # AJT: 18-Jan-2026: Get Home Assistant's configured timezone for date parsing
    ha_timezone = dt_util.get_time_zone(hass.config.time_zone)
    # AJT: 18-Jan-2026: Log timezone configuration for debugging
    LOGGER.info(
        "SolarEdge Optimizers: Using timezone '%s' (HA config: '%s') for date parsing",
        str(ha_timezone),
        hass.config.time_zone
    )
    
    api = solaredgeoptimizers(
        entry.data["siteid"], entry.data["username"], entry.data["password"], ha_timezone
    )
    try:
        http_result_code = await hass.async_add_executor_job(api.check_login)
    except (ConnectTimeout, HTTPError) as ex:
        LOGGER.error("Could not retrieve details from SolarEdge API")
        raise ConfigEntryNotReady from ex

    if http_result_code != 200:
        LOGGER.error("Missing details data in SolarEdge response")
        raise ConfigEntryNotReady

    hass.data.setdefault(DOMAIN, {})

    # AJT: 10-Jan-2025: Pass config_entry to coordinator to enable async_config_entry_first_refresh()
    coordinator = MyCoordinator(hass, api, True, entry)

    # Fetch initial data so we have data when entities subscribe
    #
    # If the refresh fails, async_config_entry_first_refresh will
    # raise ConfigEntryNotReady and setup will try again later
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        # AJT: 11-Jan-2026: Added cleanup of coordinator resources
        coordinator = hass.data[DOMAIN].pop(entry.entry_id, None)
        # Future: If API gets a close() method, call it here
        # if coordinator and hasattr(coordinator, 'my_api'):
        #     await hass.async_add_executor_job(coordinator.my_api.close)

    return unload_ok
