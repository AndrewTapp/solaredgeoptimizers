"""
SolarEdge Optimizers Integration - Entry Point (__init__.py)

This module serves as the main entry point for the Home Assistant integration. It handles:

- Config entry setup and teardown (async_setup_entry, async_unload_entry)
- API client initialization using the dual API wrapper (SolarEdge One + legacy fallback)
- Data coordinator creation for polling optimizer data at regular intervals
- Platform registration (sensor platform) for exposing optimizer data as HA entities
- Migration of legacy configuration options (use_solaredge_one default)
- Entity and device registry cleanup when the integration is removed or reloaded
- Timezone configuration for proper date/time parsing of API responses

The integration authenticates with the SolarEdge Monitoring Portal using site credentials,
retrieves the site layout (inverters, strings, optimizers), and creates sensor entities
for power, voltage, current, maximum daily optimizer temperature, energy, and status at optimizer, string,
inverter, and site levels. Site level includes installation date and peak power when
using the SolarEdge One API; inverter level includes max active power (kW) from the
layout. The coordinator aggregates lifetime energy with portal overrides (dashboard /
by-inverter) and guards string-level overrides when portal Wh far exceeds optimizer sums;
last-measurement rollups at string/inverter/site use active devices only. On unload or
config entry removal, ``async_unload_entry`` / ``async_remove_entry`` call ``api.close()``
so legacy thread-local sessions and dual API clients release connections (file descriptors).

Device registry: ``coordinator.ensure_devices_registered()`` runs after the first coordinator
refresh and again before the sensor platform is forwarded, so site/inverter/string devices
exist before entities are added. Entities link to those devices by identifier only
(``device_ids.link_device_info``), avoiding ``via_device`` warnings on startup.

The sensor platform registers per-optimizer entities with ``has_entity_name`` disabled so
``entity_id`` matches the path-based ``suggested_object_id`` (e.g. ``sensor.power_1_1_1``)
without Home Assistant prepending the optimizer device slug; friendly names use short
translated sensor labels only (device name shows optimizer position). Optimizer devices are
created in the sensor platform before entities are added. On large sites, entities are added
in batches (``ENTITY_ADD_BATCH_SIZE``) with event-loop yields so startup does not flood the
entity registry or websocket bus; coordinator listeners are notified after registration so
sensor states can update without waiting for the next poll.
"""
import logging
from typing import Any

from requests import ConnectTimeout, HTTPError, RequestException
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from .api_dual import SolarEdgeDualAPI
from .const import (
    DOMAIN,
    LOGGER,
    CONF_SITE_ID,
    CONF_USE_SOLAREDGE_ONE,
)
from .coordinator import MyCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def _migrate_use_solaredge_one(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Ensure use_solaredge_one exists in data or options (default True). Reinstated for legacy-only option."""
    data = dict(entry.data)
    options = dict(entry.options)
    changed = False
    default_one = True
    if CONF_USE_SOLAREDGE_ONE not in data and CONF_USE_SOLAREDGE_ONE not in options:
        options[CONF_USE_SOLAREDGE_ONE] = default_one
        changed = True
    if changed:
        hass.config_entries.async_update_entry(entry, data=data, options=options)
        LOGGER.info("SolarEdge Optimizers: Migrated config entry %s (set %s=%s)", entry.entry_id, CONF_USE_SOLAREDGE_ONE, default_one)


def _log_setup_context(hass: HomeAssistant, entry: ConfigEntry, use_solaredge_one: bool, ha_timezone) -> None:
    """Log setup context details at debug level."""
    if LOGGER.isEnabledFor(logging.DEBUG):
        LOGGER.debug(
            "SolarEdge Optimizers: Setup context entry=%s site=%s use_solaredge_one=%s timezone=%s (HA=%s)",
            entry.entry_id,
            entry.data.get("siteid", "?"),
            use_solaredge_one,
            str(ha_timezone),
            hass.config.time_zone,
        )


async def _async_check_login(hass: HomeAssistant, api: SolarEdgeDualAPI) -> int:
    """Run login check and normalize network/data failures to ConfigEntryNotReady."""
    if LOGGER.isEnabledFor(logging.DEBUG):
        LOGGER.debug("SolarEdge Optimizers: Starting login check")
    try:
        http_result_code = await hass.async_add_executor_job(api.check_login)
    except ConnectTimeout as ex:
        LOGGER.error("SolarEdge Optimizers: Connection timeout during login check: %s", ex)
        raise ConfigEntryNotReady from ex
    except HTTPError as ex:
        LOGGER.error("SolarEdge Optimizers: HTTP error during login check: %s", ex)
        raise ConfigEntryNotReady from ex
    except RequestException as ex:
        LOGGER.error("SolarEdge Optimizers: Network error during login check: %s", ex)
        raise ConfigEntryNotReady from ex
    except (ValueError, KeyError, TypeError) as ex:
        LOGGER.error("SolarEdge Optimizers: Data parsing error during login check: %s", ex)
        raise ConfigEntryNotReady from ex

    if LOGGER.isEnabledFor(logging.DEBUG):
        LOGGER.debug("SolarEdge Optimizers: Login check result: %s", http_result_code)
    return http_result_code


def _validate_login_status(http_result_code: int) -> None:
    """Validate login status and raise setup exceptions when required."""
    if http_result_code == 401:
        LOGGER.error("SolarEdge Optimizers: Authentication failed (401); please re-authenticate")
        raise ConfigEntryAuthFailed("Invalid or expired credentials; please re-authenticate")
    if http_result_code != 200:
        LOGGER.error("SolarEdge Optimizers: Missing details data in SolarEdge response (status: %s)", http_result_code)
        raise ConfigEntryNotReady


async def _async_initial_refresh(coordinator: MyCoordinator) -> None:
    """Perform first coordinator refresh and keep existing exception behavior."""
    if LOGGER.isEnabledFor(logging.DEBUG):
        LOGGER.debug("SolarEdge Optimizers: Starting initial coordinator refresh")
    try:
        await coordinator.async_config_entry_first_refresh()
        LOGGER.info("SolarEdge Optimizers: Initial coordinator refresh completed successfully")
    except (ConnectTimeout, HTTPError, RequestException) as ex:
        LOGGER.error("SolarEdge Optimizers: Network error during initial coordinator refresh: %s", ex)
        raise
    except (ValueError, KeyError, TypeError) as ex:
        LOGGER.error("SolarEdge Optimizers: Data parsing error during initial coordinator refresh: %s", ex)
        raise
    except ConfigEntryNotReady:
        raise
    except RuntimeError as ex:
        LOGGER.error("SolarEdge Optimizers: Runtime error during initial coordinator refresh: %s", ex)
        raise


async def _async_forward_platforms(hass: HomeAssistant, entry: ConfigEntry, coordinator: MyCoordinator) -> None:
    """Store coordinator, ensure devices, and forward setup to integration platforms."""
    hass.data[DOMAIN][entry.entry_id] = coordinator
    if LOGGER.isEnabledFor(logging.DEBUG):
        LOGGER.debug(
            "SolarEdge Optimizers: Stored coordinator for entry %s, forwarding to platforms: %s",
            entry.entry_id,
            PLATFORMS,
        )
    if coordinator.ensure_devices_registered() and LOGGER.isEnabledFor(logging.DEBUG):
        LOGGER.debug(
            "SolarEdge Optimizers: Device registry ready before sensor platform (entry %s)",
            entry.entry_id,
        )
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        # Platform setup failed; remove partial state so caller closes API.
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        raise


async def _async_close_api_after_setup_failure(hass: HomeAssistant, api: SolarEdgeDualAPI, entry_id: str) -> None:
    """Close API after setup failure; never raise from cleanup path."""
    try:
        await hass.async_add_executor_job(api.close)
        if LOGGER.isEnabledFor(logging.DEBUG):
            LOGGER.debug(
                "SolarEdge Optimizers: Closed API after setup failure for entry %s",
                entry_id,
            )
    except Exception as e:  # pylint: disable=broad-except
        LOGGER.warning(
            "SolarEdge Optimizers: Error closing API after setup failure: %s",
            e,
        )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up SolarEdge Optimizers Data from a config entry."""
    await _migrate_use_solaredge_one(hass, entry)
    LOGGER.info("SolarEdge Optimizers: Starting setup for config entry: %s", entry.entry_id)
    ha_timezone = dt_util.get_time_zone(hass.config.time_zone)
    use_solaredge_one = entry.options.get(CONF_USE_SOLAREDGE_ONE, entry.data.get(CONF_USE_SOLAREDGE_ONE, True))
    _log_setup_context(hass, entry, use_solaredge_one, ha_timezone)

    api = SolarEdgeDualAPI(
        entry.data["siteid"],
        entry.data["username"],
        entry.data["password"],
        ha_timezone,
        language=hass.config.language,
        use_solaredge_one=use_solaredge_one,
    )
    coordinator_stored = False
    try:
        http_result_code = await _async_check_login(hass, api)
        _validate_login_status(http_result_code)

        if LOGGER.isEnabledFor(logging.DEBUG):
            LOGGER.debug("SolarEdge Optimizers: Login successful, creating coordinator")

        hass.data.setdefault(DOMAIN, {})

        # Pass config_entry to coordinator to enable async_config_entry_first_refresh()
        if LOGGER.isEnabledFor(logging.DEBUG):
            LOGGER.debug("SolarEdge Optimizers: Creating coordinator instance")
        coordinator = MyCoordinator(hass, api, True, entry)

        # Fetch initial data so we have data when entities subscribe
        #
        # If the refresh fails, async_config_entry_first_refresh will
        # raise ConfigEntryNotReady and setup will try again later
        await _async_initial_refresh(coordinator)
        # Mark setup as successful only after platform forwarding completes.
        await _async_forward_platforms(hass, entry, coordinator)
        coordinator_stored = True
        if LOGGER.isEnabledFor(logging.DEBUG):
            LOGGER.debug("SolarEdge Optimizers: Platform setup complete for entry %s", entry.entry_id)
        return True
    finally:
        if not coordinator_stored:
            await _async_close_api_after_setup_failure(hass, api, entry.entry_id)


def _entity_matches_entry(e: Any, entry_id: str) -> bool:
    """Return True if entity (or registry entry) belongs to the given config entry."""
    if e is None:
        return False
    if getattr(e, "config_entry_id", None) == entry_id:
        return True
    uid = getattr(e, "unique_id", None)
    return uid is not None and str(uid).startswith(entry_id)


def _collect_entity_ids_to_remove(ent_reg, entry_id: str) -> list[str]:
    """Collect entity IDs in the registry that belong to this config entry (multiple HA paths)."""
    to_remove: list[str] = []
    if hasattr(ent_reg.entities, "get_entries_for_config_entry_id"):
        for e in ent_reg.entities.get_entries_for_config_entry_id(entry_id):
            to_remove.append(e.entity_id)
    if not to_remove and hasattr(ent_reg.entities, "values"):
        for entity in ent_reg.entities.values():
            if _entity_matches_entry(entity, entry_id):
                to_remove.append(entity.entity_id)
    if not to_remove:
        for maybe_key in ent_reg.entities:
            entity = ent_reg.async_get(maybe_key) if hasattr(ent_reg, "async_get") else None
            if entity is None and hasattr(ent_reg.entities, "data"):
                entity = getattr(ent_reg.entities, "data", {}).get(maybe_key)
            if _entity_matches_entry(entity, entry_id):
                to_remove.append(getattr(entity, "entity_id", maybe_key))
    return to_remove


def _collect_device_ids_to_remove(dev_reg, entry: ConfigEntry, entry_id: str) -> list[str]:
    """Collect device IDs in the registry that belong to this config entry (multiple HA paths)."""
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
    return dev_ids


def _remove_entities_for_entry(ent_reg, entity_ids: list[str], entry_id: str) -> None:
    """Remove the given entity IDs from the entity registry and log at debug."""
    for eid in entity_ids:
        ent_reg.async_remove(eid)
        if LOGGER.isEnabledFor(logging.DEBUG):
            LOGGER.debug("SolarEdge Optimizers: Removed entity %s for config entry %s", eid, entry_id)


def _remove_devices_for_entry(dev_reg, device_ids: list[str], entry_id: str) -> None:
    """Remove the given device IDs from the device registry and log at debug."""
    for did in device_ids:
        dev_reg.async_remove_device(did)
        if LOGGER.isEnabledFor(logging.DEBUG):
            LOGGER.debug("SolarEdge Optimizers: Removed device %s for config entry %s", did, entry_id)


def _log_removal_result(entity_ids: list[str], device_ids: list[str], entry_id: str) -> None:
    """Log summary or warning when no entities/devices were found to remove."""
    if entity_ids or device_ids:
        LOGGER.info(
            "SolarEdge Optimizers: Removed %d entities and %d devices for entry %s",
            len(entity_ids), len(device_ids), entry_id,
        )
    else:
        LOGGER.warning(
            "SolarEdge Optimizers: remove_entities_and_devices_for_entry found no entities or "
            "devices to remove for entry %s. Delete the integration from Settings → Devices & "
            "services → Integrations (not only from HACS) while the integration is still "
            "installed so that cleanup can run.",
            entry_id,
        )


def remove_entities_and_devices_for_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove all entities and devices for this config entry from entity and device registries.

    Used by unload (when user removes the integration) and by config flow async_remove_entry.
    Paths differ across Home Assistant versions; we try get_entries_for_config_entry_id /
    get_devices_for_config_entry_id first, then fallbacks for older or different HA builds.
    """
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    entry_id = entry.entry_id
    to_remove = _collect_entity_ids_to_remove(ent_reg, entry_id)
    _remove_entities_for_entry(ent_reg, to_remove, entry_id)
    dev_ids = _collect_device_ids_to_remove(dev_reg, entry, entry_id)
    _remove_devices_for_entry(dev_reg, dev_ids, entry_id)
    _log_removal_result(to_remove, dev_ids, entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry. Always closes API (releases file descriptors) and pops coordinator."""
    if LOGGER.isEnabledFor(logging.DEBUG):
        LOGGER.debug("SolarEdge Optimizers: Unloading config entry %s", entry.entry_id)
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    # Always pop coordinator and close API so file descriptors are released even if platform unload failed
    coordinator = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    try:
        if unload_ok:
            try:
                remove_entities_and_devices_for_entry(hass, entry)
            except Exception as e:  # pylint: disable=broad-except
                LOGGER.warning(
                    "SolarEdge Optimizers: Error cleaning registries during unload: %s",
                    e,
                )
    finally:
        if coordinator is not None and hasattr(coordinator, "my_api"):
            if LOGGER.isEnabledFor(logging.DEBUG):
                LOGGER.debug(
                    "SolarEdge Optimizers: Unload finally: closing API sessions for entry %s",
                    entry.entry_id,
                )
            try:
                await hass.async_add_executor_job(coordinator.my_api.close)
                LOGGER.info(
                    "SolarEdge Optimizers: Closed API clients for entry %s (file descriptors released)",
                    entry.entry_id,
                )
            except Exception as e:  # pylint: disable=broad-except
                LOGGER.warning("SolarEdge Optimizers: Error closing API sessions: %s", e)

    if not unload_ok and LOGGER.isEnabledFor(logging.DEBUG):
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
