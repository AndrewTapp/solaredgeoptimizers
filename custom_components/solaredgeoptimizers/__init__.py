"""SolarEdge Optimizers integration for Home Assistant."""
import logging
from typing import Any

from requests import ConnectTimeout, HTTPError
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

# AJT: 10-Jan-2025: Changed from absolute import to relative import to use local solaredgeoptimizers.py instead of site-packages version
from .solaredgeoptimizers import solaredgeoptimizers
from .const import (
    DOMAIN,
    LOGGER,
    CONF_SITE_ID,
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

    if LOGGER.isEnabledFor(logging.DEBUG):
        LOGGER.debug("SolarEdge Optimizers: Creating API instance")
    api = solaredgeoptimizers(
        entry.data["siteid"],
        entry.data["username"],
        entry.data["password"],
        ha_timezone,
        language=hass.config.language,
    )

    if LOGGER.isEnabledFor(logging.DEBUG):
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

    if http_result_code == 401:
        LOGGER.error("SolarEdge Optimizers: Authentication failed (401); please re-authenticate")
        raise ConfigEntryAuthFailed("Invalid or expired credentials; please re-authenticate")
    if http_result_code != 200:
        LOGGER.error("SolarEdge Optimizers: Missing details data in SolarEdge response (status: %s)", http_result_code)
        raise ConfigEntryNotReady

    if LOGGER.isEnabledFor(logging.DEBUG):
        LOGGER.debug("SolarEdge Optimizers: Login successful, creating coordinator")

    hass.data.setdefault(DOMAIN, {})

    # AJT: 10-Jan-2025: Pass config_entry to coordinator to enable async_config_entry_first_refresh()
    if LOGGER.isEnabledFor(logging.DEBUG):
        LOGGER.debug("SolarEdge Optimizers: Creating coordinator instance")
    coordinator = MyCoordinator(hass, api, True, entry)

    # Fetch initial data so we have data when entities subscribe
    #
    # If the refresh fails, async_config_entry_first_refresh will
    # raise ConfigEntryNotReady and setup will try again later
    if LOGGER.isEnabledFor(logging.DEBUG):
        LOGGER.debug("SolarEdge Optimizers: Starting initial coordinator refresh")
    try:
        await coordinator.async_config_entry_first_refresh()
        LOGGER.info("SolarEdge Optimizers: Initial coordinator refresh completed successfully")
    except Exception as ex:
        LOGGER.error("SolarEdge Optimizers: Initial coordinator refresh failed: %s", ex)
        raise

    hass.data[DOMAIN][entry.entry_id] = coordinator
    if LOGGER.isEnabledFor(logging.DEBUG):
        LOGGER.debug(
            "SolarEdge Optimizers: Stored coordinator for entry %s, forwarding to platforms: %s",
            entry.entry_id,
            PLATFORMS,
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    if LOGGER.isEnabledFor(logging.DEBUG):
        LOGGER.debug("SolarEdge Optimizers: Platform setup complete for entry %s", entry.entry_id)

    return True


def _remove_config_entry_from_registries(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove all entities and devices for this config entry from entity and device registries.
    Called from unload so cleanup runs when the user deletes the integration (unload runs first).
    """
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    entry_id = entry.entry_id

    # Entities
    to_remove: list[str] = []
    if hasattr(ent_reg.entities, "get_entries_for_config_entry_id"):
        for e in ent_reg.entities.get_entries_for_config_entry_id(entry_id):
            to_remove.append(e.entity_id)
    if not to_remove and hasattr(ent_reg.entities, "values"):
        for entity in ent_reg.entities.values():
            if getattr(entity, "config_entry_id", None) == entry_id:
                to_remove.append(entity.entity_id)
            elif getattr(entity, "unique_id", None) and str(entity.unique_id).startswith(entry_id):
                to_remove.append(entity.entity_id)
    for eid in to_remove:
        ent_reg.async_remove(eid)

    # Devices
    dev_ids: list[str] = []
    if hasattr(dev_reg.devices, "get_devices_for_config_entry_id"):
        for dev in dev_reg.devices.get_devices_for_config_entry_id(entry_id):
            dev_ids.append(dev.id)
    if not dev_ids and hasattr(dev_reg.devices, "values"):
        for device in dev_reg.devices.values():
            if entry_id in device.config_entries:
                dev_ids.append(device.id)
    if not dev_ids:
        siteid = (entry.data.get(CONF_SITE_ID) or "").strip()
        if siteid:
            site_dev = dev_reg.async_get_device(identifiers={(DOMAIN, f"site_{siteid}")})
            if site_dev:
                dev_ids.append(site_dev.id)
    for did in dev_ids:
        dev_reg.async_remove_device(did)

    if to_remove or dev_ids:
        LOGGER.info(
            "SolarEdge Optimizers: Removed %d entities and %d devices for entry %s",
            len(to_remove),
            len(dev_ids),
            entry_id,
        )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if LOGGER.isEnabledFor(logging.DEBUG):
        LOGGER.debug("SolarEdge Optimizers: Unloading config entry %s", entry.entry_id)
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        # Remove from entity and device registries so no leftovers after delete
        try:
            _remove_config_entry_from_registries(hass, entry)
        except Exception as e:  # pylint: disable=broad-except
            LOGGER.warning(
                "SolarEdge Optimizers: Error cleaning registries during unload: %s",
                e,
            )
        # AJT: 11-Jan-2026: Added cleanup of coordinator resources
        coordinator = hass.data[DOMAIN].pop(entry.entry_id, None)
        # AJT: 27-Jan-2026: Close API sessions to prevent file descriptor leaks
        if coordinator and hasattr(coordinator, 'my_api'):
            try:
                await hass.async_add_executor_job(coordinator.my_api.close)
                if LOGGER.isEnabledFor(logging.DEBUG):
                    LOGGER.debug("SolarEdge Optimizers: Closed API sessions during unload")
            except Exception as e:  # pylint: disable=broad-except
                LOGGER.warning("SolarEdge Optimizers: Error closing API sessions: %s", e)
    else:
        if LOGGER.isEnabledFor(logging.DEBUG):
            LOGGER.debug(
                "SolarEdge Optimizers: Platform unload failed or skipped for entry %s",
                entry.entry_id,
            )

    if LOGGER.isEnabledFor(logging.DEBUG):
        LOGGER.debug(
            "SolarEdge Optimizers: Unload complete for entry %s (unload_ok=%s)",
            entry.entry_id,
            unload_ok,
        )
    return unload_ok
