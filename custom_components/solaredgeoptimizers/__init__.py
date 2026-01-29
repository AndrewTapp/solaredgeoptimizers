"""SolarEdge Optimizers integration for Home Assistant."""
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

    # AJT: 24-Jan-2026: Add detailed debugging for initial setup issues
    LOGGER.info("SolarEdge Optimizers: Starting setup for config entry: %s", entry.entry_id)
    LOGGER.info("SolarEdge Optimizers: Config data - siteid: %s, username: %s",
               entry.data.get("siteid", "MISSING"), entry.data.get("username", "MISSING"))

    # AJT: 18-Jan-2026: Get Home Assistant's configured timezone for date parsing
    ha_timezone = dt_util.get_time_zone(hass.config.time_zone)
    # AJT: 18-Jan-2026: Log timezone configuration for debugging
    LOGGER.info(
        "SolarEdge Optimizers: Using timezone '%s' (HA config: '%s') for date parsing",
        str(ha_timezone),
        hass.config.time_zone
    )

    LOGGER.debug("SolarEdge Optimizers: Creating API instance")
    api = solaredgeoptimizers(
        entry.data["siteid"], entry.data["username"], entry.data["password"], ha_timezone
    )

    LOGGER.debug("SolarEdge Optimizers: Starting login check")
    try:
        http_result_code = await hass.async_add_executor_job(api.check_login)
        LOGGER.info("SolarEdge Optimizers: Login check result: %s", http_result_code)
    except (ConnectTimeout, HTTPError) as ex:
        LOGGER.error("SolarEdge Optimizers: Could not retrieve details from SolarEdge API: %s", ex)
        raise ConfigEntryNotReady from ex
    except Exception as ex:
        LOGGER.error("SolarEdge Optimizers: Unexpected error during login check: %s", ex)
        raise ConfigEntryNotReady from ex

    if http_result_code != 200:
        LOGGER.error("SolarEdge Optimizers: Missing details data in SolarEdge response (status: %s)", http_result_code)
        raise ConfigEntryNotReady

    LOGGER.debug("SolarEdge Optimizers: Login successful, creating coordinator")

    hass.data.setdefault(DOMAIN, {})

    # AJT: 10-Jan-2025: Pass config_entry to coordinator to enable async_config_entry_first_refresh()
    LOGGER.debug("SolarEdge Optimizers: Creating coordinator instance")
    coordinator = MyCoordinator(hass, api, True, entry)

    # Fetch initial data so we have data when entities subscribe
    #
    # If the refresh fails, async_config_entry_first_refresh will
    # raise ConfigEntryNotReady and setup will try again later
    LOGGER.debug("SolarEdge Optimizers: Starting initial coordinator refresh")
    try:
        await coordinator.async_config_entry_first_refresh()
        LOGGER.info("SolarEdge Optimizers: Initial coordinator refresh completed successfully")
    except Exception as ex:
        LOGGER.error("SolarEdge Optimizers: Initial coordinator refresh failed: %s", ex)
        raise

    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        # AJT: 11-Jan-2026: Added cleanup of coordinator resources
        coordinator = hass.data[DOMAIN].pop(entry.entry_id, None)
        # AJT: 27-Jan-2026: Close API sessions to prevent file descriptor leaks
        if coordinator and hasattr(coordinator, 'my_api'):
            try:
                await hass.async_add_executor_job(coordinator.my_api.close)
                LOGGER.debug("SolarEdge Optimizers: Closed API sessions during unload")
            except Exception as e:
                LOGGER.warning("SolarEdge Optimizers: Error closing API sessions: %s", e)

    return unload_ok
