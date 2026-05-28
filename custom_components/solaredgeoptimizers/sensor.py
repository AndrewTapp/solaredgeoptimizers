"""
SolarEdge Optimizers Integration - Sensor Platform (sensor.py)

This module defines and registers all sensor entities for the integration. It creates:

Individual Optimizer Sensors:
- Power (W), Voltage (V), Current (A), Optimizer Voltage (V)
- Maximum Daily Temperature (°C), Lifetime Energy (kWh), Last Measurement (timestamp)
- Status (Active/Inactive), Azimuth (°), Tilt (°)

Aggregated Sensors (String, Inverter, Site levels):
- Average Current, Total Power, Average Voltage
- Lifetime Energy (summed from children; string portal override aligned by layout relativeOrder,
  skipped if portal Wh exceeds optimizer sum by STRING_LIFETIME_PORTAL_OVERRIDE_MAX_RATIO)
- Last Measurement (string: latest among active optimizers; inverter: among active strings;
  site: among active inverters; unknown/None if no qualifying timestamp—no fake “now”)
- Child Count (optimizers per string, strings per inverter, inverters per site)
- Status (Active/Inactive)
- Inverter-level: Max active power (kW, from portal layout logical v2; One API only)

Site-Level Sensors:
- Last Polled (integration polling timestamp)
- Obtained From (indicates whether data came from One API or Legacy API)
- Installation Date, Peak Power (from portal layout/information/site; One API only)

Key features:
- Entities use CoordinatorEntity for automatic updates from the data coordinator
- Position-based entity IDs ensure hardware swaps don't create duplicate entities
- Device hierarchy: coordinator registers site/inverter/string devices first; optimizer devices
  are registered in the sensor platform before entities are added (`_register_optimizer_devices`,
  `via_device` as a tuple); entities use link_device_info (identifiers only) from device_ids.py
  so HA does not re-apply via_device or create unnamed stub devices
- Entities are added in tier order (site → inverter → string → optimizer), then registered in
  batches of ENTITY_ADD_BATCH_SIZE with event-loop yields between batches (update_before_add=False
  because the coordinator already completed its first refresh); `async_update_listeners()` runs
  after the last batch so states populate without waiting for the next coordinator tick
- Per-optimizer coordinator lookup uses panel_id and display-name position keys (including suffixes
  such as 1.1.1a); `async_restore_last_state` reapplies coordinator data after HA state restore
- Display names: `build_optimizer_tasks()` / `parse_optimizer_display_name_to_indices()` in const.py
  parse trailing letter suffixes (e.g. 1.1.1a); duplicate positions also get a/b/c when needed
- Inactive devices have certain sensors excluded (power, current, voltage, etc.)
- Duplicate optimizer/string/inverter positions get letter suffixes (a, b, c...)
- Supports optional entity ID prefix and site ID inclusion in entity names
- Entity registry rebuild is conditional (only when entity-id-shaping options change)
- Translations (i18n): per-optimizer sensors use short translated labels only (e.g. "Azimuth", "Power");
  has_entity_name=False so entity_id stays e.g. power_1_1_1 without device slug duplication; the
  optimizer device name carries site/string/optimizer context.
"""
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.translation import async_get_translations

import asyncio
import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)

from homeassistant.core import callback
from datetime import date, datetime, timezone

from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
)

from .const import (
    CONF_INCLUDE_SITE_ID_IN_ENTITY_ID,
    DOMAIN,
    CONF_ENTITY_PREFIX,
    ENTITY_ADD_BATCH_SIZE,
    SENSOR_TYPE_INDIVIDUAL,
    SENSOR_TYPE_AGGREGATED_STRING,
    SENSOR_TYPE_AGGREGATED_INVERTER,
    SENSOR_TYPE_AGGREGATED_SITE,
    SENSOR_TYPE_OPT_VOLTAGE,
    SENSOR_TYPE_CURRENT,
    SENSOR_TYPE_POWER,
    SENSOR_TYPE_VOLTAGE,
    SENSOR_TYPE_TEMPERATURE,
    SENSOR_TYPE_ENERGY,
    SENSOR_TYPE_LASTMEASUREMENT,
    SENSOR_TYPE_CHILD_COUNT,
    SENSOR_TYPE_STATUS,
    SENSOR_TYPE_AZIMUTH,
    SENSOR_TYPE_TILT,
    SENSOR_TYPE_INSTALLATION_DATE,
    SENSOR_TYPE_PEAK_POWER,
    SENSOR_TYPE_MAX_ACTIVE_POWER,
    CHECK_TIME_DELTA,
    is_status_active,
    parse_string_display_name_path,
    build_optimizer_tasks,
    string_position_key_from_display_name,
    resolve_duplicate_indices,
    status_display_value,
    status_icon,
    SENSOR_TYPE_INACTIVE_OPTIMIZER_EXCLUDE,
    SENSOR_TYPE_INACTIVE_AGGREGATED_EXCLUDE,
    ICON_AZIMUTH,
    ICON_TILT,
)

# Changed import to use coordinator module
from .coordinator import MyCoordinator
from .device_ids import (
    inv_key_from_entity_id_path,
    inv_str_keys_from_entity_id_path,
    inverter_device_identifier,
    link_device_info,
    opt_keys_from_entity_id_path,
    optimizer_device_identifier,
    site_device_identifier,
    string_device_identifier,
)

from homeassistant.const import (
    UnitOfPower,
    UnitOfElectricPotential,
    UnitOfElectricCurrent,
    UnitOfEnergy,
    UnitOfTemperature,
)

# Changed from absolute import to relative import to use local solaredgeoptimizers.py instead of site-packages version
from .solaredgeoptimizers import (
    SolarEdgeOptimizerData,
    SolarEdgeAggregatedData,
    SolarlEdgeOptimizer,
)

_LOGGER = logging.getLogger(__name__)


def _entity_prefix(entry: ConfigEntry) -> str:
    """Normalize optional entity ID prefix from config (lowercase, underscores). Options override data.
    If the key is present in options (including as ''), use it; only fall back to data when key is missing."""
    if CONF_ENTITY_PREFIX in entry.options:
        raw = entry.options.get(CONF_ENTITY_PREFIX) or ""
    else:
        raw = entry.data.get(CONF_ENTITY_PREFIX) or ""
    return (raw or "").strip().lower().replace(" ", "_")


def _registry_entry_belongs_to_config_entry(reg_entry, entry_id: str) -> bool:
    """Return True if this registry entry is owned by the given config entry."""
    if getattr(reg_entry, "config_entry_id", None) == entry_id:
        return True
    uid = getattr(reg_entry, "unique_id", None)
    return bool(uid and str(uid).startswith(entry_id))


def _add_entity_ids_from_entries_api(to_remove: set[str], ent_reg, entry_id: str) -> None:
    """Add entity IDs from get_entries_for_config_entry_id if available."""
    if hasattr(ent_reg.entities, "get_entries_for_config_entry_id"):
        for e in ent_reg.entities.get_entries_for_config_entry_id(entry_id):
            to_remove.add(e.entity_id)


def _add_entity_ids_from_entities_data(to_remove: set[str], ent_reg, entry_id: str) -> None:
    """Add entity IDs from ent_reg.entities.data that belong to this config entry."""
    if hasattr(ent_reg.entities, "data"):
        for eid, reg_entry in list(getattr(ent_reg.entities, "data", {}).items()):
            if _registry_entry_belongs_to_config_entry(reg_entry, entry_id):
                to_remove.add(eid)


def _add_entity_ids_from_entities_values(to_remove: set[str], ent_reg, entry_id: str) -> None:
    """Add entity IDs from ent_reg.entities.values() that belong to this config entry."""
    if hasattr(ent_reg.entities, "values"):
        for entity in ent_reg.entities.values():
            eid = getattr(entity, "entity_id", None)
            if eid and _registry_entry_belongs_to_config_entry(entity, entry_id):
                to_remove.add(eid)


def _add_entity_ids_from_fallback_iteration(to_remove: set[str], ent_reg, entry_id: str) -> None:
    """Add entity IDs by iterating ent_reg.entities and resolving via async_get/data (with error handling)."""
    try:
        for eid in ent_reg.entities:
            if eid in to_remove:
                continue
            reg_entry = ent_reg.async_get(eid) if hasattr(ent_reg, "async_get") else None
            if reg_entry is None and hasattr(ent_reg.entities, "data"):
                reg_entry = getattr(ent_reg.entities, "data", {}).get(eid)
            if reg_entry and _registry_entry_belongs_to_config_entry(reg_entry, entry_id):
                to_remove.add(eid)
    except Exception as e:  # pylint: disable=broad-except
        _LOGGER.warning(
            "SolarEdge Optimizers sensor: Error during entity registry fallback iteration for entry %s: %s",
            entry_id,
            e,
        )


def _collect_entity_ids_for_config_entry(ent_reg, entry_id: str) -> set[str]:
    """Collect all entity IDs in the registry that belong to this config entry."""
    to_remove: set[str] = set()
    _add_entity_ids_from_entries_api(to_remove, ent_reg, entry_id)
    _add_entity_ids_from_entities_data(to_remove, ent_reg, entry_id)
    _add_entity_ids_from_entities_values(to_remove, ent_reg, entry_id)
    _add_entity_ids_from_fallback_iteration(to_remove, ent_reg, entry_id)
    return to_remove


def _remove_entities_from_registry(ent_reg, entity_ids: set[str]) -> None:
    """Remove the given entity IDs from the entity registry, logging warnings on failure."""
    for eid in entity_ids:
        try:
            ent_reg.async_remove(eid)
        except Exception as e:  # pylint: disable=broad-except
            _LOGGER.warning(
                "SolarEdge Optimizers sensor: Could not remove entity %s: %s",
                eid,
                e,
            )


def _remove_sensor_entities_for_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove all sensor entities for this config entry from the entity registry.
    Called only when setup requested an entity-id-shape rebuild (e.g. options changed
    prefix or include_site_id), so new entities use the current suggested_object_id.
    Match by config_entry_id and by unique_id prefix (entry_id) so we find entities even if
    config_entry_id was cleared during unload.
    """
    ent_reg = er.async_get(hass)
    entry_id = entry.entry_id
    to_remove = _collect_entity_ids_for_config_entry(ent_reg, entry_id)
    _remove_entities_from_registry(ent_reg, to_remove)
    if to_remove:
        _LOGGER.info(
            "SolarEdge Optimizers sensor: Removing %d existing entities for entry %s so new entity_ids match current options",
            len(to_remove),
            entry_id,
        )


def _should_rebuild_entities_this_setup(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Return True when options flow requested entity-registry rebuild for this setup."""
    rebuild_set = hass.data.get(DOMAIN, {}).get("_rebuild_entity_registry")
    if not isinstance(rebuild_set, set):
        return False
    if entry.entry_id in rebuild_set:
        rebuild_set.discard(entry.entry_id)
        return True
    return False


async def _fetch_missing_optimizer_data(hass, coordinator, optimizer_tasks, coordinator_data):
    """Fetch optimizer data from API for optimizers not already in coordinator cache. Returns dict task_idx -> result."""
    optimizers_to_fetch = [
        (task_idx, opt)
        for task_idx, (opt, *_) in enumerate(optimizer_tasks)
        if coordinator_data is None or coordinator_data.get(opt.optimizerId) is None
    ]
    results_by_task_idx = {}
    if not optimizers_to_fetch:
        return results_by_task_idx
    if _LOGGER.isEnabledFor(logging.DEBUG):
        _LOGGER.debug(
            "SolarEdge Optimizers sensor: Fetching data for %d optimizers not present in coordinator cache",
            len(optimizers_to_fetch),
        )
    fetch_results = await asyncio.gather(
        *[
            hass.async_add_executor_job(coordinator.my_api.requestSystemData, opt.optimizerId)
            for _task_idx, opt in optimizers_to_fetch
        ],
        return_exceptions=True,
    )
    for (task_idx, _opt), result in zip(optimizers_to_fetch, fetch_results):
        results_by_task_idx[task_idx] = result
    return results_by_task_idx


def _get_optimizer_info_for_task(task_idx, optimizer_tasks, coordinator_data, results_by_task_idx):
    """Resolve optimizer info from coordinator cache or fetch results. Returns (info, skip) where skip=True to skip this task."""
    optimizer, _inverter, _string, inv_idx, str_idx, _opt_idx, _suffix = optimizer_tasks[task_idx]
    if coordinator_data and coordinator_data.get(optimizer.optimizerId) is not None:
        return coordinator_data.get(optimizer.optimizerId), False
    if task_idx not in results_by_task_idx:
        return None, True
    raw = results_by_task_idx[task_idx]
    if isinstance(raw, Exception):
        _LOGGER.error("Error fetching data for optimizer %s: %s", optimizer.optimizerId, raw)
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "SolarEdge Optimizers sensor: Skipping sensors for optimizer %s (inverter %s string %s) due to exception",
                optimizer.optimizerId, inv_idx, str_idx,
            )
        return None, True
    return raw, False


def _build_individual_optimizer_sensors(
    coordinator, hass, entry, optimizer_tasks, coordinator_data, results_by_task_idx,
    base_name, site_id, include_site_id,
):
    """Build list of SolarEdgeOptimizersSensor entities for all optimizers that have info.
    
    For inactive optimizers (not active: status not blank and not 'ACTIVE'), certain sensors
    are excluded as they are not meaningful: azimuth, current, optimizer voltage, power, temperature, tilt, voltage.
    
    Returns (sensors_to_add, active_count, inactive_count, skipped_sensor_count).
    """
    sensors_to_add = []
    active_optimizer_count = 0
    inactive_optimizer_count = 0
    skipped_sensor_count = 0
    
    for task_idx, (optimizer, inverter, string, inv_idx, str_idx, opt_idx, suffix) in enumerate(optimizer_tasks):
        info, skip = _get_optimizer_info_for_task(
            task_idx, optimizer_tasks, coordinator_data, results_by_task_idx
        )
        if skip or info is None:
            continue
        
        # Check if optimizer is active (blank or Active from layout API)
        optimizer_status_raw = getattr(optimizer, "status", "") or ""
        is_active = is_status_active(optimizer_status_raw)
        
        if is_active:
            active_optimizer_count += 1
        else:
            inactive_optimizer_count += 1
        
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "SolarEdge Optimizers sensor: Adding optimizer panel_id=%s serial=%s model=%s "
                "panel_type=%s status=%s azimuth=%s° tilt=%s° suffix=%s is_active=%s",
                optimizer.optimizerId,
                getattr(info, "serialnumber", ""),
                getattr(info, "model", ""),
                getattr(info, "panel_description", "") or "(none)",
                optimizer_status_raw or "(none)",
                getattr(info, "azimuth", None),
                getattr(info, "tilt", None),
                suffix or "(none)",
                is_active,
            )
        # Use position-based path with suffix for unique_id so all optimizers (including duplicates)
        # get distinct entity IDs. Suffix is empty for first item, 'a', 'b', etc. for duplicates.
        entity_id_path = (
            (site_id, inv_idx, str_idx, f"{opt_idx}{suffix}") if include_site_id else (inv_idx, str_idx, f"{opt_idx}{suffix}")
        )
        for sensortype in SENSOR_TYPE_INDIVIDUAL:
            # Skip certain sensors for inactive optimizers
            if not is_active and sensortype in SENSOR_TYPE_INACTIVE_OPTIMIZER_EXCLUDE:
                skipped_sensor_count += 1
                if _LOGGER.isEnabledFor(logging.DEBUG):
                    _LOGGER.debug(
                        "SolarEdge Optimizers sensor: Skipping %s sensor for inactive optimizer %s",
                        sensortype, optimizer.optimizerId,
                    )
                continue
            sensors_to_add.append(
                SolarEdgeOptimizersSensor(
                    coordinator, hass, entry, info, sensortype, optimizer, inverter, string,
                    base_name=base_name, site_id=site_id, entity_id_path=entity_id_path,
                    include_site_id_in_entity_id=include_site_id,
                )
            )
    return sensors_to_add, active_optimizer_count, inactive_optimizer_count, skipped_sensor_count


def _register_optimizer_devices(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator,
    optimizer_tasks: list,
    site_id: str,
    include_site_id: bool,
) -> None:
    """Create optimizer device registry entries before entities link by identifier only.

    Without this, Home Assistant auto-creates stub devices (shown as Unnamed device) when
    entities reference optimizer identifiers that are not yet in the registry.
    """
    if not optimizer_tasks:
        return
    device_registry = dr.async_get(hass)
    data = coordinator.data if isinstance(coordinator.data, dict) else {}
    registered = 0
    for optimizer, _inverter, _string, inv_num, str_num, opt_num, suffix in optimizer_tasks:
        opt_key = f"{opt_num}{suffix}"
        inv_key, str_key = inv_num, str_num
        if include_site_id:
            optimizer_name = f"Optimizer {site_id}.{inv_num}.{str_num}.{opt_key}"
        else:
            optimizer_name = f"Optimizer {inv_num}.{str_num}.{opt_key}"
        info = data.get(optimizer.optimizerId)
        model = ""
        if info is not None:
            model = (getattr(info, "model", None) or "").strip()
            panel_desc = (getattr(info, "panel_description", None) or "").strip()
            if panel_desc and model:
                model = f"{model} {panel_desc}"
            elif panel_desc:
                model = panel_desc
        if not model:
            model = (
                getattr(optimizer, "type", None)
                or getattr(optimizer, "displayName", None)
                or "Optimizer"
            )
        str_device_id = string_device_identifier(entry.entry_id, inv_key, str_key)
        opt_device_id = optimizer_device_identifier(entry.entry_id, inv_key, str_key, opt_key)
        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, opt_device_id)},
            manufacturer="SolarEdge",
            model=model,
            name=optimizer_name,
            hw_version=getattr(optimizer, "serialNumber", None),
            via_device=(DOMAIN, str_device_id),
        )
        registered += 1
    _LOGGER.info(
        "SolarEdge Optimizers sensor: Registered %d optimizer devices before adding entities",
        registered,
    )


def _create_string_aggregated_data(string, string_entity_path):
    """Create SolarEdgeAggregatedData for a string."""
    string_aggregated = SolarEdgeAggregatedData(
        entity_id=f"string_{string.stringId}",
        entity_type="string",
        entity_id_path=string_entity_path,
    )
    string_aggregated.serialnumber = f"String_{string.stringId}"
    string_aggregated.panel_description = string.displayName
    string_aggregated.status = getattr(string, "status", "") or ""
    return string_aggregated


def _create_string_sensors(  # pylint: disable=too-many-arguments
    coordinator, hass, entry, string, string_aggregated, inverter, base_name, is_active,
    include_site_id_in_entity_id: bool = False,
):
    """Create sensor entities for a string, skipping excluded sensors for inactive strings.
    
    Returns (sensors_list, skipped_count).
    """
    sensors = []
    skipped = 0
    for sensortype in SENSOR_TYPE_AGGREGATED_STRING:
        if not is_active and sensortype in SENSOR_TYPE_INACTIVE_AGGREGATED_EXCLUDE:
            skipped += 1
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "SolarEdge Optimizers sensor: Skipping %s sensor for inactive string %s",
                    sensortype, string.displayName,
                )
            continue
        sensors.append(
            SolarEdgeAggregatedSensor(
                coordinator, hass, entry, string_aggregated, sensortype, string, inverter,
                base_name=base_name,
                include_site_id_in_entity_id=include_site_id_in_entity_id,
            )
        )
    return sensors, skipped


def _create_inverter_aggregated_data(inverter, inv_idx_str, include_site_id, site_id):
    """Create SolarEdgeAggregatedData for an inverter."""
    inverter_aggregated = SolarEdgeAggregatedData(
        entity_id=f"inverter_{inverter.inverterId}",
        entity_type="inverter",
        entity_id_path=(site_id, inv_idx_str) if include_site_id else (inv_idx_str,),
    )
    inverter_aggregated.serialnumber = inverter.serialNumber or f"Inverter_{inverter.inverterId}"
    inverter_aggregated.panel_description = inverter.displayName
    inverter_aggregated.status = getattr(inverter, "status", "") or ""
    inverter_aggregated.manufacturer = getattr(inverter, "manufacturer", "") or "SolarEdge"
    inverter_aggregated.model = getattr(inverter, "model", "") or ""
    return inverter_aggregated


def _create_inverter_sensors(  # pylint: disable=too-many-arguments
    coordinator, hass, entry, inverter, inverter_aggregated, base_name, is_active,
    include_site_id_in_entity_id: bool = False,
):
    """Create sensor entities for an inverter, skipping excluded sensors for inactive inverters.
    
    Returns (sensors_list, skipped_count).
    """
    sensors = []
    skipped = 0
    for sensortype in SENSOR_TYPE_AGGREGATED_INVERTER:
        if not is_active and sensortype in SENSOR_TYPE_INACTIVE_AGGREGATED_EXCLUDE:
            skipped += 1
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "SolarEdge Optimizers sensor: Skipping %s sensor for inactive inverter %s",
                    sensortype, inverter.displayName,
                )
            continue
        sensors.append(
            SolarEdgeAggregatedSensor(
                coordinator, hass, entry, inverter_aggregated, sensortype, None, inverter,
                base_name=base_name,
                include_site_id_in_entity_id=include_site_id_in_entity_id,
            )
        )
    return sensors, skipped


def _resolve_inverter_duplicates(site_struct) -> dict[int, str]:
    """Resolve duplicate inverters and return suffix map with logging."""
    inv_suffix_map = resolve_duplicate_indices(
        site_struct.inverters,
        get_key=lambda inv: getattr(inv, "displayName", "") or str(getattr(inv, "inverterId", "")),
        get_status=lambda inv: getattr(inv, "status", "") or "",
        get_serial=lambda inv: getattr(inv, "serialNumber", "") or "",
        logger=_LOGGER,
    )
    
    inv_duplicates = sum(1 for suffix in inv_suffix_map.values() if suffix)
    if inv_duplicates > 0:
        _LOGGER.info(
            "SolarEdge Optimizers sensor: Found %d duplicate inverter positions, assigned suffixes",
            inv_duplicates,
        )
    return inv_suffix_map


def _build_string_sensors_for_inverter(  # pylint: disable=too-many-arguments
    coordinator, hass: HomeAssistant, entry: ConfigEntry, inverter, inv_idx: int, inv_suffix: str,
    base_name: str, include_site_id: bool, site_id: str
) -> tuple[list, int, int, int]:
    """Build string sensors for one inverter. Returns (sensors, active_count, inactive_count, skipped_count)."""
    sensors = []
    active_count = 0
    inactive_count = 0
    skipped_count = 0
    
    inv_idx_str = f"{inv_idx}{inv_suffix}"
    
    indexed_strings = list(enumerate(inverter.strings, start=1))
    str_suffix_map = resolve_duplicate_indices(
        indexed_strings,
        get_key=lambda t: string_position_key_from_display_name(
            getattr(t[1], "displayName", "") or "", inv_idx, t[0]
        ),
        get_status=lambda t: getattr(t[1], "status", "") or "",
        get_serial=lambda t: getattr(t[1], "serialNumber", "") or str(getattr(t[1], "stringId", "")),
        logger=_LOGGER,
    )

    for list_idx, (str_idx, string) in enumerate(indexed_strings):
        str_suffix = str_suffix_map.get(list_idx, "")
        str_idx_str = f"{str_idx}{str_suffix}"

        parsed = parse_string_display_name_path(getattr(string, "displayName", "") or "")
        if parsed is not None:
            inv_num, str_num, display_suffix = parsed
            str_num_str = f"{str_num}{display_suffix or str_suffix}"
            string_entity_path = (site_id, inv_num, str_num_str) if include_site_id else (inv_num, str_num_str)
        else:
            string_entity_path = (site_id, inv_idx_str, str_idx_str) if include_site_id else (inv_idx_str, str_idx_str)
        
        string_aggregated = _create_string_aggregated_data(string, string_entity_path)
        string_is_active = is_status_active(string_aggregated.status)
        
        if string_is_active:
            active_count += 1
        else:
            inactive_count += 1
        
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "SolarEdge Optimizers sensor: Creating aggregated sensors for string: %s status=%s suffix=%s is_active=%s",
                string.displayName, string_aggregated.status or "(none)", str_suffix or "(none)", string_is_active,
            )
        
        string_sensors, skipped = _create_string_sensors(
            coordinator, hass, entry, string, string_aggregated, inverter, base_name, string_is_active,
            include_site_id_in_entity_id=include_site_id,
        )
        sensors.extend(string_sensors)
        skipped_count += skipped
    
    return sensors, active_count, inactive_count, skipped_count


def _build_all_string_sensors(  # pylint: disable=too-many-arguments
    coordinator, hass: HomeAssistant, entry: ConfigEntry, site_struct, inv_suffix_map: dict, base_name: str, include_site_id: bool, site_id: str
) -> tuple[list, int, int, int]:
    """Build string sensors for all inverters. Returns (sensors, active_count, inactive_count, skipped_count)."""
    sensors = []
    active_count = 0
    inactive_count = 0
    skipped_count = 0
    
    if _LOGGER.isEnabledFor(logging.DEBUG):
        total_strings = sum(len(inv.strings) for inv in site_struct.inverters)
        _LOGGER.debug(
            "SolarEdge Optimizers sensor: Building string sensors for %d inverters with %d total strings",
            len(site_struct.inverters), total_strings,
        )
    
    for inv_idx, inverter in enumerate(site_struct.inverters, start=1):
        inv_suffix = inv_suffix_map.get(inv_idx - 1, "")
        inv_sensors, inv_active, inv_inactive, inv_skipped = _build_string_sensors_for_inverter(
            coordinator, hass, entry, inverter, inv_idx, inv_suffix,
            base_name, include_site_id, site_id
        )
        sensors.extend(inv_sensors)
        active_count += inv_active
        inactive_count += inv_inactive
        skipped_count += inv_skipped
    
    return sensors, active_count, inactive_count, skipped_count


def _build_all_inverter_sensors(  # pylint: disable=too-many-arguments
    coordinator, hass: HomeAssistant, entry: ConfigEntry, site_struct, inv_suffix_map: dict, base_name: str, include_site_id: bool, site_id: str
) -> tuple[list, int, int, int]:
    """Build inverter sensors for all inverters. Returns (sensors, active_count, inactive_count, skipped_count)."""
    sensors = []
    active_count = 0
    inactive_count = 0
    skipped_count = 0
    
    if _LOGGER.isEnabledFor(logging.DEBUG):
        _LOGGER.debug(
            "SolarEdge Optimizers sensor: Building inverter sensors for %d inverters",
            len(site_struct.inverters),
        )
    
    for inv_idx, inverter in enumerate(site_struct.inverters, start=1):
        inv_suffix = inv_suffix_map.get(inv_idx - 1, "")
        inv_idx_str = f"{inv_idx}{inv_suffix}"
        
        inverter_aggregated = _create_inverter_aggregated_data(inverter, inv_idx_str, include_site_id, site_id)
        inverter_is_active = is_status_active(inverter_aggregated.status)
        
        if inverter_is_active:
            active_count += 1
        else:
            inactive_count += 1
        
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "SolarEdge Optimizers sensor: Creating aggregated sensors for inverter: %s status=%s manufacturer=%s model=%s suffix=%s is_active=%s",
                inverter.displayName, inverter_aggregated.status or "(none)",
                inverter_aggregated.manufacturer, inverter_aggregated.model or "(none)", inv_suffix or "(none)", inverter_is_active,
            )
        
        inverter_sensors, skipped = _create_inverter_sensors(
            coordinator, hass, entry, inverter, inverter_aggregated, base_name, inverter_is_active,
            include_site_id_in_entity_id=include_site_id,
        )
        sensors.extend(inverter_sensors)
        skipped_count += skipped
    
    return sensors, active_count, inactive_count, skipped_count


def _build_site_sensors(  # pylint: disable=too-many-arguments
    coordinator, hass: HomeAssistant, entry: ConfigEntry, site_struct, base_name: str, site_id: str,
    include_site_id_in_entity_id: bool = False,
) -> list:
    """Build site-level aggregated sensors. Returns list of sensors."""
    sensors = []
    site_aggregated = SolarEdgeAggregatedData(
        entity_id=f"site_{site_struct.siteId}",
        entity_type="site",
        entity_id_path=(site_id,),
    )
    site_aggregated.serialnumber = f"Site_{site_struct.siteId}"
    site_aggregated.panel_description = f"Site {site_struct.siteId}"
    
    if _LOGGER.isEnabledFor(logging.DEBUG):
        _LOGGER.debug("SolarEdge Optimizers sensor: Creating aggregated sensors for site: %s", site_id)
    
    for sensortype in SENSOR_TYPE_AGGREGATED_SITE:
        sensors.append(
            SolarEdgeAggregatedSensor(
                coordinator, hass, entry, site_aggregated, sensortype, None, None, base_name=base_name,
                include_site_id_in_entity_id=include_site_id_in_entity_id,
            )
        )
    return sensors


def _build_aggregated_sensors(  # pylint: disable=too-many-arguments
    coordinator, hass, entry, site_struct, base_name, include_site_id, site_id
):
    """Build list of SolarEdgeAggregatedSensor entities for strings, inverters, and site.
    
    No deduplication - all inverters and strings are shown. When duplicates exist
    (same position), active devices come first (sorted by serial), and subsequent
    duplicates get letter suffixes (a, b, c...).
    
    For inactive strings/inverters (not active: status not blank and not 'ACTIVE'), certain
    sensors are excluded as they are not meaningful: current (average), power, voltage (average).
    
    Returns (sensors, active_strings, inactive_strings, active_inverters, inactive_inverters, skipped_count).
    """
    if not site_struct:
        return [], 0, 0, 0, 0, 0
    
    if _LOGGER.isEnabledFor(logging.DEBUG):
        total_strings = sum(len(inv.strings) for inv in site_struct.inverters)
        _LOGGER.debug(
            "SolarEdge Optimizers sensor: Building aggregated sensors for %d inverters, %d strings",
            len(site_struct.inverters), total_strings,
        )
    
    inv_suffix_map = _resolve_inverter_duplicates(site_struct)
    
    # Build string sensors
    string_sensors, active_str, inactive_str, skipped_str = _build_all_string_sensors(
        coordinator, hass, entry, site_struct, inv_suffix_map, base_name, include_site_id, site_id
    )
    
    # Build inverter sensors
    inverter_sensors, active_inv, inactive_inv, skipped_inv = _build_all_inverter_sensors(
        coordinator, hass, entry, site_struct, inv_suffix_map, base_name, include_site_id, site_id
    )
    
    # Build site sensors
    site_sensors = _build_site_sensors(
        coordinator, hass, entry, site_struct, base_name, site_id, include_site_id_in_entity_id=include_site_id,
    )
    
    # Site and inverter entities before string entities so any device_info updates see parents first.
    sensors = site_sensors + inverter_sensors + string_sensors
    skipped_total = skipped_str + skipped_inv
    
    return sensors, active_str, inactive_str, active_inv, inactive_inv, skipped_total


def _get_setup_config(entry: ConfigEntry, site) -> tuple[str | None, str, bool]:
    """Extract setup configuration from entry and site. Returns (base_name, site_id, include_site_id)."""
    base_name = _entity_prefix(entry)
    site_id = str(site.siteId)
    include_site_id = entry.options.get(
        CONF_INCLUDE_SITE_ID_IN_ENTITY_ID,
        entry.data.get(CONF_INCLUDE_SITE_ID_IN_ENTITY_ID, False),
    )
    return base_name, site_id, include_site_id


def _log_setup_info(site, base_name: str, include_site_id: bool, site_id: str) -> None:
    """Log setup information for the integration."""
    _LOGGER.info(
        "SolarEdge Optimizers sensor: Preparing entities for site %s (%s optimizers across %s inverters)",
        site.siteId,
        site.returnNumberOfOptimizers(),
        len(site.inverters),
    )
    if _LOGGER.isEnabledFor(logging.DEBUG):
        _LOGGER.debug(
            "SolarEdge Optimizers sensor: entity_id prefix=%r include_site_id_in_entity_id=%s site_id=%s",
            base_name or "(empty)",
            include_site_id,
            site_id,
        )


def _create_site_level_sensors(  # pylint: disable=too-many-arguments
    coordinator, hass: HomeAssistant, entry: ConfigEntry, site_id: str, base_name: str | None, include_site_id: bool
) -> list:
    """Create site-level sensors (Last Polled, Obtained From). Returns list of sensors."""
    return [
        SolarEdgeIntegrationLastPolledSensor(
            coordinator, hass, entry, site_id, base_name=base_name, include_site_id_in_entity_id=include_site_id
        ),
        SolarEdgeObtainedFromSensor(
            coordinator, hass, entry, site_id, base_name=base_name, include_site_id_in_entity_id=include_site_id
        ),
    ]


def _log_device_status_summary(
    active_opt: int, inactive_opt: int,
    active_str: int, inactive_str: int,
    active_inv: int, inactive_inv: int,
    skipped_opt: int, skipped_agg: int
) -> None:
    """Log summary of active/inactive devices and skipped sensors."""
    if inactive_opt > 0 or inactive_str > 0 or inactive_inv > 0:
        _LOGGER.info(
            "SolarEdge Optimizers sensor: Device status summary - Optimizers: %d active, %d inactive | "
            "Strings: %d active, %d inactive | Inverters: %d active, %d inactive",
            active_opt, inactive_opt, active_str, inactive_str, active_inv, inactive_inv,
        )
        total_skipped = skipped_opt + skipped_agg
        if total_skipped > 0:
            _LOGGER.info(
                "SolarEdge Optimizers sensor: Skipped %d sensors for inactive devices "
                "(%d optimizer sensors, %d aggregated sensors)",
                total_skipped, skipped_opt, skipped_agg,
            )


def _sensor_entity_device_tier(sensor: SensorEntity) -> int:
    """Sort key so parent devices exist before child entities are added (site → inverter → string → optimizer)."""
    device_info = getattr(sensor, "_attr_device_info", None) or {}
    identifiers = device_info.get("identifiers") or set()
    if not identifiers:
        return 50
    device_id = next(iter(identifiers))[1]
    if "_opt_" in device_id:
        return 40
    if "_str_" in device_id:
        return 30
    if "_inv_" in device_id:
        return 20
    if device_id.startswith("site_"):
        return 10
    return 50


async def _finalize_sensor_setup(
    async_add_entities: AddEntitiesCallback,
    sensors_to_add: list[SensorEntity],
    optimizer_count: int,
    aggregated_count: int,
    coordinator: MyCoordinator | None = None,
) -> None:
    """Add sensors to Home Assistant and log completion."""
    if sensors_to_add:
        sensors_to_add = sorted(sensors_to_add, key=_sensor_entity_device_tier)
        batch_size = max(1, ENTITY_ADD_BATCH_SIZE)
        total_entities = len(sensors_to_add)
        batch_count = (total_entities + batch_size - 1) // batch_size
        _LOGGER.info(
            "SolarEdge Optimizers sensor: Registering %d entities in %d batches (batch size %d)",
            total_entities,
            batch_count,
            batch_size,
        )
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "SolarEdge Optimizers sensor: Adding %d entities in batches of %d (update_before_add=False)",
                total_entities,
                batch_size,
            )
        for start_idx in range(0, total_entities, batch_size):
            batch = sensors_to_add[start_idx:start_idx + batch_size]
            async_add_entities(batch, update_before_add=False)
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "SolarEdge Optimizers sensor: Added entity batch %d-%d of %d",
                    start_idx + 1,
                    start_idx + len(batch),
                    total_entities,
                )
            if start_idx + len(batch) < total_entities:
                # Yield to the event loop so HA can process registry/state events between batches.
                await asyncio.sleep(0)
        # update_before_add=False skips per-entity refresh; push coordinator data to all listeners.
        if coordinator is not None and coordinator.data is not None:
            coordinator.async_update_listeners()
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "SolarEdge Optimizers sensor: Notified coordinator listeners after batched entity registration",
                )
        _LOGGER.info(
            "Done adding all sensors. Added %d sensors in total (%d optimizer sensors + %d aggregated sensors + 2 site sensors).",
            total_entities, optimizer_count, aggregated_count,
        )
    else:
        _LOGGER.warning("No sensors were created - check for errors above")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add a SolarEdge config entry."""
    if _LOGGER.isEnabledFor(logging.DEBUG):
        _LOGGER.debug("SolarEdge Optimizers sensor: async_setup_entry for entry_id=%s", entry.entry_id)
    
    coordinator: MyCoordinator = hass.data[DOMAIN][entry.entry_id]
    site = coordinator._site_structure
    if site is None:
        _LOGGER.error("SolarEdge Optimizers sensor: No site structure on coordinator; setup cannot continue")
        return

    if not coordinator.ensure_devices_registered():
        _LOGGER.error(
            "SolarEdge Optimizers sensor: Device registration skipped (no site structure); "
            "entity setup may fail for entry %s",
            entry.entry_id,
        )
    elif _LOGGER.isEnabledFor(logging.DEBUG):
        _LOGGER.debug(
            "SolarEdge Optimizers sensor: Device registry confirmed before building entities (entry %s)",
            entry.entry_id,
        )

    if _should_rebuild_entities_this_setup(hass, entry):
        try:
            _remove_sensor_entities_for_entry(hass, entry)
        except Exception as e:  # pylint: disable=broad-except
            _LOGGER.warning(
                "SolarEdge Optimizers sensor: Error removing existing entities before setup: %s", e,
            )
    elif _LOGGER.isEnabledFor(logging.DEBUG):
        _LOGGER.debug(
            "SolarEdge Optimizers sensor: Skipping entity-registry rebuild for entry %s (no entity-id shape change)",
            entry.entry_id,
        )

    base_name, site_id, include_site_id = _get_setup_config(entry, site)
    _log_setup_info(site, base_name, include_site_id, site_id)

    # Build optimizer sensors
    optimizer_tasks = build_optimizer_tasks(site)
    coordinator_data = coordinator.data if isinstance(coordinator.data, dict) else None
    results_by_task_idx = await _fetch_missing_optimizer_data(
        hass, coordinator, optimizer_tasks, coordinator_data
    )

    optimizer_sensors, active_opt, inactive_opt, skipped_opt = _build_individual_optimizer_sensors(
        coordinator, hass, entry, optimizer_tasks, coordinator_data, results_by_task_idx,
        base_name, site_id, include_site_id,
    )
    
    # Build site-level sensors
    site_level_sensors = _create_site_level_sensors(
        coordinator, hass, entry, site_id, base_name, include_site_id
    )
    
    # Build aggregated sensors
    aggregated_sensors, active_str, inactive_str, active_inv, inactive_inv, skipped_agg = _build_aggregated_sensors(
        coordinator, hass, entry, coordinator._site_structure, base_name, include_site_id, site_id
    )

    # Site/inverter aggregated sensors before optimizers so device hierarchy resolves cleanly.
    sensors_to_add = site_level_sensors + aggregated_sensors + optimizer_sensors

    # Log status summary
    _log_device_status_summary(
        active_opt, inactive_opt, active_str, inactive_str, active_inv, inactive_inv, skipped_opt, skipped_agg
    )

    _register_optimizer_devices(
        hass, entry, coordinator, optimizer_tasks, site_id, include_site_id
    )

    # Finalize setup
    await _finalize_sensor_setup(
        async_add_entities,
        sensors_to_add,
        len(optimizer_sensors),
        len(aggregated_sensors),
        coordinator,
    )


class SolarEdgeIntegrationLastPolledSensor(CoordinatorEntity, SensorEntity):
    """Single integration-level 'last polled' timestamp sensor."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_state_class = None
    _attr_has_entity_name = True
    _attr_translation_key = "last_polled"

    def __init__(
        self,
        coordinator: MyCoordinator,
        hass: HomeAssistant,
        entry: ConfigEntry,
        site_id: str,
        base_name: str = "",
        include_site_id_in_entity_id: bool = False,
    ) -> None:
        super().__init__(coordinator)
        self._hass = hass
        self._entry = entry
        self._site_id = site_id
        self._base_name = (base_name + "_") if base_name else ""
        self._include_site_id_in_entity_id = include_site_id_in_entity_id

        # Include prefix in unique_id so reconfigure (add/remove prefix) produces new entity_id instead of keeping old one
        _uid_parts = [entry.entry_id]
        if self._base_name:
            _uid_parts.append(self._base_name.rstrip("_"))
        _uid_parts.append("last_polled")
        if include_site_id_in_entity_id:
            _uid_parts.append(site_id)
        self._attr_unique_id = "_".join(_uid_parts)
        # Full object_id so HA does not prefix with device name (e.g. avoid sensor.site_123_last_polled_123)
        obj_id = f"{self._base_name}last_polled_{self._site_id}" if include_site_id_in_entity_id else f"{self._base_name}last_polled"
        self.internal_integration_suggested_object_id = obj_id
        self._attr_device_info = link_device_info(site_device_identifier(site_id))

    @property
    def suggested_object_id(self) -> str | None:
        """Suggest full entity object_id (no device prefix)."""
        return getattr(self, "internal_integration_suggested_object_id", None) or (
            f"{self._base_name}last_polled_{self._site_id}" if self._include_site_id_in_entity_id else f"{self._base_name}last_polled"
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        self._attr_native_value = getattr(self.coordinator, "_integration_last_polled", None)
        self.async_write_ha_state()


class SolarEdgeObtainedFromSensor(CoordinatorEntity, SensorEntity):
    """Site-level sensor indicating which API provided the current data (One API or Legacy API)."""

    _attr_device_class = None
    _attr_state_class = None
    _attr_has_entity_name = True
    _attr_translation_key = "obtained_from"

    def __init__(
        self,
        coordinator: MyCoordinator,
        hass: HomeAssistant,
        entry: ConfigEntry,
        site_id: str,
        base_name: str = "",
        include_site_id_in_entity_id: bool = False,
    ) -> None:
        super().__init__(coordinator)
        self._hass = hass
        self._entry = entry
        self._site_id = site_id
        self._base_name = (base_name + "_") if base_name else ""
        self._include_site_id_in_entity_id = include_site_id_in_entity_id

        _uid_parts = [entry.entry_id]
        if self._base_name:
            _uid_parts.append(self._base_name.rstrip("_"))
        _uid_parts.append("obtained_from")
        if include_site_id_in_entity_id:
            _uid_parts.append(site_id)
        self._attr_unique_id = "_".join(_uid_parts)
        obj_id = (
            f"{self._base_name}obtained_from_{self._site_id}"
            if include_site_id_in_entity_id
            else f"{self._base_name}obtained_from"
        )
        self.internal_integration_suggested_object_id = obj_id
        self._attr_device_info = link_device_info(site_device_identifier(site_id))

    @property
    def suggested_object_id(self) -> str | None:
        """Suggest full entity object_id (no device prefix)."""
        return getattr(self, "internal_integration_suggested_object_id", None) or (
            f"{self._base_name}obtained_from_{self._site_id}"
            if self._include_site_id_in_entity_id
            else f"{self._base_name}obtained_from"
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        self._attr_native_value = getattr(self.coordinator, "_obtained_from", None)
        self.async_write_ha_state()


# class MyEntity(CoordinatorEntity, SensorEntity):
class SolarEdgeAggregatedSensor(CoordinatorEntity, SensorEntity):
    """An entity for aggregated SolarEdge measurements at string/inverter level."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_has_entity_name = True

    # Class-level constant for sensor attribute mapping to avoid recreating on every update
    _SENSOR_ATTR_MAP = {
        SENSOR_TYPE_VOLTAGE: "voltage",
        SENSOR_TYPE_CURRENT: "current",
        SENSOR_TYPE_POWER: "power",
        SENSOR_TYPE_ENERGY: "lifetime_energy",
        SENSOR_TYPE_LASTMEASUREMENT: "lastmeasurement",
        SENSOR_TYPE_CHILD_COUNT: "child_count",
        SENSOR_TYPE_STATUS: "status",
        SENSOR_TYPE_INSTALLATION_DATE: "installation_date",
        SENSOR_TYPE_PEAK_POWER: "peak_power",
        SENSOR_TYPE_MAX_ACTIVE_POWER: "max_active_power",
    }
    # Translation keys for entity names (i18n)
    _TRANSLATION_KEYS = {
        SENSOR_TYPE_LASTMEASUREMENT: "last_measurement",
        SENSOR_TYPE_CHILD_COUNT: None,  # Resolved per entity_type below
        SENSOR_TYPE_CURRENT: "current_average",
        SENSOR_TYPE_VOLTAGE: "voltage_average",
        SENSOR_TYPE_ENERGY: "lifetime_energy",
        SENSOR_TYPE_POWER: "power",
        SENSOR_TYPE_STATUS: "status",
        SENSOR_TYPE_INSTALLATION_DATE: "installation_date",
        SENSOR_TYPE_PEAK_POWER: "peak_power",
        SENSOR_TYPE_MAX_ACTIVE_POWER: "max_active_power",
    }
    # (unit, device_class, state_class, suggested_display_precision) per sensor type
    _AGGREGATED_UNITS_MAP = {
        SENSOR_TYPE_VOLTAGE: (UnitOfElectricPotential.VOLT, SensorDeviceClass.VOLTAGE, SensorStateClass.MEASUREMENT, 2),
        SENSOR_TYPE_CURRENT: (UnitOfElectricCurrent.AMPERE, SensorDeviceClass.CURRENT, SensorStateClass.MEASUREMENT, None),
        SENSOR_TYPE_POWER: (UnitOfPower.WATT, SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT, 2),
        SENSOR_TYPE_ENERGY: (UnitOfEnergy.KILO_WATT_HOUR, SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING, 3),
        SENSOR_TYPE_INSTALLATION_DATE: (None, SensorDeviceClass.DATE, None, None),
        SENSOR_TYPE_PEAK_POWER: (UnitOfPower.KILO_WATT, SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT, 2),
        SENSOR_TYPE_MAX_ACTIVE_POWER: (UnitOfPower.KILO_WATT, SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT, 2),
    }

    def __init__(
        self,
        coordinator,
        hass: HomeAssistant,
        entry: ConfigEntry,
        panel: SolarEdgeAggregatedData,
        sensortype,
        string=None,
        inverter=None,
        base_name: str = "",
        include_site_id_in_entity_id: bool = False,
    ) -> None:
        super().__init__(coordinator)
        self._hass = hass
        self._entry = entry
        self._panelobject = panel
        self._string = string
        self._inverter = inverter
        self._base_name = (base_name + "_") if base_name else ""
        self._include_site_id_in_entity_id = include_site_id_in_entity_id
        self._panel = panel.panel_description
        self._sensor_type = sensortype
        self._set_aggregated_identity(entry, panel, sensortype)
        self._set_aggregated_device_info(entry, panel, string, inverter)
        self._set_aggregated_units_and_state_class()

    def _set_aggregated_identity(self, entry: ConfigEntry, panel: SolarEdgeAggregatedData, sensortype) -> None:
        """Set unique_id, suggested_object_id, translation_key and _log_name for this aggregated sensor."""
        path_str = "_".join(map(str, getattr(panel, "entity_id_path", ())))
        if panel.entity_type == "site" and not path_str and "_" in getattr(panel, "panel_id", ""):
            path_str = str(panel.panel_id).split("_", 1)[1]
        slug = self._slug_for_sensortype()
        _uid_parts = [entry.entry_id]
        if self._base_name:
            _uid_parts.append(self._base_name.rstrip("_"))
        _uid_parts.append(slug)
        _uid_parts.append(path_str if path_str else panel.panel_id)
        self._attr_unique_id = "_".join(_uid_parts)
        object_id = f"{self._base_name}{slug}_{path_str}" if path_str else f"{self._base_name}{slug}_{panel.panel_id}"
        if object_id.strip("_"):
            self.internal_integration_suggested_object_id = object_id
        if self._sensor_type == SENSOR_TYPE_CHILD_COUNT:
            if panel.entity_type == "string":
                self._attr_translation_key = "optimizer_count"
            elif panel.entity_type == "inverter":
                self._attr_translation_key = "string_count"
            else:
                self._attr_translation_key = "inverter_count"
        else:
            self._attr_translation_key = self._TRANSLATION_KEYS.get(
                self._sensor_type, self._sensor_type.lower().replace(" ", "_"),
            )
        self._log_name = f"{panel.panel_id}_{sensortype}"

    def _device_info_for_string(
        self, entry: ConfigEntry, panel: SolarEdgeAggregatedData, _string
    ) -> DeviceInfo:
        """Link to the pre-registered string device (identifiers only; via_device set at registration)."""
        inv_key, str_key = inv_str_keys_from_entity_id_path(
            panel.entity_id_path or (),
            include_site_id_in_entity_id=self._include_site_id_in_entity_id,
        )
        return link_device_info(string_device_identifier(entry.entry_id, inv_key, str_key))

    def _device_info_for_inverter(
        self, entry: ConfigEntry, panel: SolarEdgeAggregatedData, _inverter
    ) -> DeviceInfo:
        """Link to the pre-registered inverter device (identifiers only; via_device set at registration)."""
        inv_key = inv_key_from_entity_id_path(
            panel.entity_id_path or (),
            include_site_id_in_entity_id=self._include_site_id_in_entity_id,
        )
        return link_device_info(inverter_device_identifier(entry.entry_id, inv_key))

    def _device_info_for_site(self, entry: ConfigEntry, panel: SolarEdgeAggregatedData) -> DeviceInfo:
        """Link to the pre-registered site device (identifiers only)."""
        site_id = panel.panel_id.split("_")[1] if "_" in panel.panel_id else ""
        return link_device_info(site_device_identifier(site_id))

    def _set_aggregated_device_info(
        self, entry: ConfigEntry, panel: SolarEdgeAggregatedData, string, inverter
    ) -> None:
        """Set device_info based on entity type (string, inverter, or site).

        Links to devices pre-registered by the coordinator via identifiers only
        (device_ids.link_device_info); hierarchy via_device is set at registration time.
        """
        if panel.entity_type == "string":
            self._attr_device_info = self._device_info_for_string(entry, panel, string)
        elif panel.entity_type == "inverter":
            self._attr_device_info = self._device_info_for_inverter(entry, panel, inverter)
        else:
            self._attr_device_info = self._device_info_for_site(entry, panel)

    def _set_aggregated_units_and_state_class(self) -> None:
        """Set native_unit_of_measurement, device_class, state_class and suggested_display_precision from sensor type."""
        entry = self._AGGREGATED_UNITS_MAP.get(self._sensor_type)
        if entry:
            unit, device_class, state_class, precision = entry
            self._attr_native_unit_of_measurement = unit
            self._attr_device_class = device_class
            self._attr_state_class = state_class
            if precision is not None:
                self._attr_suggested_display_precision = precision
        elif self._sensor_type == SENSOR_TYPE_LASTMEASUREMENT:
            self._attr_device_class = SensorDeviceClass.TIMESTAMP
            self._attr_state_class = None
        elif self._sensor_type == SENSOR_TYPE_CHILD_COUNT:
            self._attr_state_class = None
        elif self._sensor_type == SENSOR_TYPE_STATUS:
            self._attr_state_class = None

    def _slug_for_sensortype(self) -> str:
        """Return entity_id slug for this sensor type (e.g. power, child_count, inverter_count)."""
        if self._sensor_type == SENSOR_TYPE_CHILD_COUNT:
            if self._panelobject.entity_type == "string":
                return "child_count"
            if self._panelobject.entity_type == "inverter":
                return "child_count"
            return "inverter_count"
        if self._sensor_type == SENSOR_TYPE_STATUS:
            return "status"
        return self._TRANSLATION_KEYS.get(
            self._sensor_type,
            self._sensor_type.lower().replace(" ", "_"),
        ) or ""

    @property
    def suggested_object_id(self) -> str | None:
        """Suggest full entity object_id (no device prefix). Return same as internal_integration_suggested_object_id when set."""
        # Prefer our precomputed full id so HA does not combine with device name (avoids sensor.site_123_power_123)
        if getattr(self, "internal_integration_suggested_object_id", None):
            return self.internal_integration_suggested_object_id
        slug = self._slug_for_sensortype()
        if not slug:
            return None
        path = getattr(self._panelobject, "entity_id_path", None)
        if path:
            path_str = "_".join(map(str, path))
            return f"{self._base_name}{slug}_{path_str}"
        # Site level: use numeric site id (strip "site_" from panel_id) so entity_id is [prefix]slug_2065855 not [prefix]slug_site_2065855
        if getattr(self._panelobject, "entity_type", None) == "site" and "_" in getattr(self._panelobject, "panel_id", ""):
            path_str = str(self._panelobject.panel_id).split("_", 1)[1]
            return f"{self._base_name}{slug}_{path_str}"
        return f"{self._base_name}{slug}_{self._panelobject.panel_id}"

    def _normalize_aggregated_energy_value(self, raw_value, prev_value) -> float:
        """Round energy to 3 dp and enforce monotonic (never decrease)."""
        if raw_value is not None:
            value = round(float(raw_value), 3) if not isinstance(raw_value, float) else round(raw_value, 3)
        else:
            value = 0.0
        if prev_value is not None:
            prev = float(prev_value) if not isinstance(prev_value, float) else prev_value
            value = max(value, prev)
        return value

    def _normalize_aggregated_live_value(self, raw_value, decimals: int = 2) -> float:
        """Round power/voltage to given decimals; 0.0 if None."""
        if raw_value is not None:
            return round(float(raw_value), decimals) if not isinstance(raw_value, float) else round(raw_value, decimals)
        return 0.0

    def _get_aggregated_default_value(self):
        """Default value for aggregated sensor by type (for getattr fallback)."""
        if self._sensor_type == SENSOR_TYPE_STATUS:
            return ""
        if self._sensor_type in (
            SENSOR_TYPE_INSTALLATION_DATE,
            SENSOR_TYPE_PEAK_POWER,
            SENSOR_TYPE_MAX_ACTIVE_POWER,
            SENSOR_TYPE_LASTMEASUREMENT,
        ):
            return None
        return 0

    def _norm_aggregated_child_count(self, value):
        """Normalize child count to int."""
        return int(value) if value is not None else 0

    def _norm_aggregated_energy(self, value):
        """Normalize energy with monotonic enforcement."""
        return self._normalize_aggregated_energy_value(value, self._attr_native_value)

    def _norm_aggregated_power_voltage(self, value):
        """Normalize power/voltage to 2 decimals."""
        return self._normalize_aggregated_live_value(value, 2)

    def _norm_aggregated_peak_power(self, value):
        """Normalize peak power to 2 decimals; None if missing."""
        return self._normalize_aggregated_live_value(value, 2) if value is not None else None

    def _norm_aggregated_max_active_power(self, value):
        """Normalize max active power to 2 decimals; None if missing."""
        return self._normalize_aggregated_live_value(value, 2) if value is not None else None

    def _norm_aggregated_installation_date(self, value):
        """Parse installation date to date object for device class."""
        if value is None:
            return None
        if isinstance(value, str) and value.strip():
            try:
                return date.fromisoformat(value.strip())
            except (ValueError, TypeError):
                return None
        return value if isinstance(value, date) else None

    def _norm_aggregated_status(self, value):
        """Normalize status to display value and log debug."""
        raw = str(value).strip() if value else ""
        display = status_display_value(raw)
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "SolarEdge Optimizers sensor: %s aggregated status updated to '%s'",
                self._log_name,
                display or "(empty)",
            )
        return display

    def _compute_aggregated_native_value(self, item) -> None:
        """Update _attr_native_value from aggregated item (child_count, energy monotonic, status, or mapped attribute)."""
        attr_name = self._SENSOR_ATTR_MAP.get(self._sensor_type)
        if not attr_name:
            return
        default = self._get_aggregated_default_value()
        raw_value = getattr(item, attr_name, default)
        normalizers = {
            SENSOR_TYPE_CHILD_COUNT: self._norm_aggregated_child_count,
            SENSOR_TYPE_ENERGY: self._norm_aggregated_energy,
            SENSOR_TYPE_POWER: self._norm_aggregated_power_voltage,
            SENSOR_TYPE_VOLTAGE: self._norm_aggregated_power_voltage,
            SENSOR_TYPE_PEAK_POWER: self._norm_aggregated_peak_power,
            SENSOR_TYPE_MAX_ACTIVE_POWER: self._norm_aggregated_max_active_power,
            SENSOR_TYPE_INSTALLATION_DATE: self._norm_aggregated_installation_date,
            SENSOR_TYPE_STATUS: self._norm_aggregated_status,
        }
        normalizer = normalizers.get(self._sensor_type)
        self._attr_native_value = normalizer(raw_value) if normalizer else raw_value

    def _normalize_aggregated_display_value(self) -> None:
        """Convert comma decimals to float and ensure child_count is int. Apply decimal places for display."""
        value = self._attr_native_value
        if isinstance(value, str) and "," in value:
            try:
                num = float(value.replace(",", "."))
                if self._sensor_type == SENSOR_TYPE_ENERGY:
                    num = round(num, 3)
                elif self._sensor_type in (SENSOR_TYPE_POWER, SENSOR_TYPE_VOLTAGE):
                    num = round(num, 2)
                self._attr_native_value = num
            except ValueError:
                _LOGGER.warning("Could not convert value '%s' to float for sensor %s", value, self._log_name)
        if self._sensor_type == SENSOR_TYPE_CHILD_COUNT and self._attr_native_value is not None:
            self._attr_native_value = int(self._attr_native_value)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        data = self.coordinator.data
        if data is None:
            return
        item = data.get(self._panelobject.panel_id)
        if item is None and _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "SolarEdge Optimizers sensor: No data for panel_id=%s (%s) in coordinator",
                self._panelobject.panel_id, self._sensor_type,
            )
        if item and hasattr(item, "entity_type"):
            self._compute_aggregated_native_value(item)
            self._normalize_aggregated_display_value()
            self.async_write_ha_state()

    @property
    def device_info(self):
        return self._attr_device_info

    @property
    def icon(self) -> str | None:
        """Return dynamic icon for status sensors: blank/Active→active, Inactive→inactive, other→unknown."""
        if self._sensor_type != SENSOR_TYPE_STATUS:
            return None
        return status_icon(self._attr_native_value)


class SolarEdgeOptimizersSensor(CoordinatorEntity, SensorEntity):
    """An entity using CoordinatorEntity.

    The CoordinatorEntity class provides:
      should_poll
      async_update
      async_added_to_hass
      available

    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    # False: entity_id stays e.g. sensor.power_1_1_1 (full suggested_object_id) without HA
    # prepending the device slug (optimizer_1_1_1_power_1_1_1). Friendly name is set in
    # async_added_to_hass from translations + optimizer position.
    _attr_has_entity_name = False

    # Class-level constant for sensor attribute mapping to avoid recreating on every update
    _SENSOR_ATTR_MAP = {
        SENSOR_TYPE_VOLTAGE: "voltage",
        SENSOR_TYPE_CURRENT: "current",
        SENSOR_TYPE_OPT_VOLTAGE: "optimizer_voltage",
        SENSOR_TYPE_POWER: "power",
        SENSOR_TYPE_TEMPERATURE: "temperature",
        SENSOR_TYPE_STATUS: "status",
        SENSOR_TYPE_AZIMUTH: "azimuth",
        SENSOR_TYPE_TILT: "tilt",
    }
    # Translation keys for entity names (i18n)
    _TRANSLATION_KEYS = {
        SENSOR_TYPE_VOLTAGE: "voltage",
        SENSOR_TYPE_CURRENT: "current",
        SENSOR_TYPE_OPT_VOLTAGE: "optimizer_voltage",
        SENSOR_TYPE_POWER: "power",
        SENSOR_TYPE_TEMPERATURE: "temperature",
        SENSOR_TYPE_ENERGY: "lifetime_energy",
        SENSOR_TYPE_LASTMEASUREMENT: "last_measurement",
        SENSOR_TYPE_STATUS: "status",
        SENSOR_TYPE_AZIMUTH: "azimuth",
        SENSOR_TYPE_TILT: "tilt",
    }
    # (unit, device_class, state_class, suggested_display_precision) per sensor type
    _OPTIMIZER_UNITS_MAP = {
        SENSOR_TYPE_VOLTAGE: (UnitOfElectricPotential.VOLT, SensorDeviceClass.VOLTAGE, SensorStateClass.MEASUREMENT, 2),
        SENSOR_TYPE_CURRENT: (UnitOfElectricCurrent.AMPERE, SensorDeviceClass.CURRENT, SensorStateClass.MEASUREMENT, None),
        SENSOR_TYPE_OPT_VOLTAGE: (UnitOfElectricPotential.VOLT, SensorDeviceClass.VOLTAGE, SensorStateClass.MEASUREMENT, 2),
        SENSOR_TYPE_POWER: (UnitOfPower.WATT, SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT, 2),
        SENSOR_TYPE_TEMPERATURE: (UnitOfTemperature.CELSIUS, SensorDeviceClass.TEMPERATURE, SensorStateClass.MEASUREMENT, 1),
        SENSOR_TYPE_ENERGY: (UnitOfEnergy.KILO_WATT_HOUR, SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING, 3),
        SENSOR_TYPE_AZIMUTH: ("°", None, None, 2),
        SENSOR_TYPE_TILT: ("°", None, None, 2),
    }

    def __init__(
        self,
        coordinator,
        hass: HomeAssistant,
        entry: ConfigEntry,
        panel: SolarEdgeOptimizerData,
        sensortype,
        optimizer: SolarlEdgeOptimizer,
        inverter,
        string=None,
        base_name: str = "",
        site_id: str = "",
        entity_id_path: tuple = (),
        include_site_id_in_entity_id: bool = False,
    ) -> None:
        super().__init__(coordinator)
        self._hass = hass
        self._entry = entry
        self._panelobject = panel
        self._optimizerobject = optimizer
        self._inverter = inverter
        self._string = string
        self._base_name = (base_name + "_") if base_name else ""
        self._entity_id_path = entity_id_path
        self._include_site_id_in_entity_id = include_site_id_in_entity_id
        self._panel = panel.panel_description
        self._sensor_type = sensortype
        self._set_optimizer_identity(entry, panel, sensortype, optimizer, site_id, entity_id_path)
        self._set_optimizer_device_info(entry, panel, string)
        self._set_optimizer_units_and_state_class()

    async def async_added_to_hass(self) -> None:
        """Set short translated friendly name (sensor type only); device shows optimizer position."""
        await super().async_added_to_hass()
        translations = await async_get_translations(
            self.hass, self.hass.config.language, "entity", [DOMAIN]
        )
        slug = self._sensor_translation_slug
        sensor_full = f"component.{DOMAIN}.entity.sensor.{slug}.name"
        sensor_label = translations.get(sensor_full) or translations.get(
            f"entity.sensor.{slug}.name", slug.replace("_", " ").title()
        )
        self._attr_name = sensor_label
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "SolarEdge Optimizers sensor: Optimizer entity name=%r slug=%s suggested_object_id=%s",
                self._attr_name,
                slug,
                getattr(self, "internal_integration_suggested_object_id", None),
            )
        self.async_write_ha_state()

    async def async_restore_last_state(self) -> None:
        """Re-apply coordinator data after HA restores persisted state (avoids stale Unknown)."""
        await super().async_restore_last_state()
        if self.coordinator.data is not None:
            self._handle_coordinator_update()

    def _set_optimizer_identity(
        self, entry: ConfigEntry, panel: SolarEdgeOptimizerData, sensortype,
        optimizer: SolarlEdgeOptimizer, site_id: str, entity_id_path: tuple,
    ) -> None:
        """Set unique_id, suggested_object_id, translation slug, and display names for this optimizer sensor."""
        path_str = "_".join(map(str, entity_id_path)) if entity_id_path else ""
        slug = self._TRANSLATION_KEYS.get(sensortype, sensortype.lower().replace(" ", "_"))
        _uid_parts = [entry.entry_id]
        if self._base_name:
            _uid_parts.append(self._base_name.rstrip("_"))
        if path_str:
            _uid_parts.extend([slug, path_str])
        else:
            _uid_parts.extend([panel.serialnumber, sensortype])
        self._attr_unique_id = "_".join(_uid_parts)
        # Short friendly name is set in async_added_to_hass (translated entity.sensor.<slug>.name only).
        self._sensor_translation_slug = self._TRANSLATION_KEYS.get(
            self._sensor_type, self._sensor_type.lower().replace(" ", "_")
        )
        self._attr_translation_key = None
        self._log_name = f"{self._sensor_type} {optimizer.displayName}"
        # Use position-based display name so devices with duplicate API display names stay distinct
        self._optimizer_display_name = (
            f"{site_id}.{'.'.join(map(str, entity_id_path[1:]))}" if len(entity_id_path) >= 4
            else ".".join(map(str, entity_id_path)) if entity_id_path
            else str(optimizer.displayName)
        )
        if slug and path_str:
            self.internal_integration_suggested_object_id = f"{self._base_name}{slug}_{path_str}"

    def _set_optimizer_device_info(self, entry: ConfigEntry, panel: SolarEdgeOptimizerData, string) -> None:
        """Set device_info with via_device so optimizer is grouped under string device.

        Uses position-based identifiers so when an optimizer is replaced (hardware swap),
        the same device and sensors continue to show the new unit's data without duplicates.
        Device hierarchy (string/inverter/site) is assigned in the coordinator device registry.
        """
        inv_key, str_key, opt_key = opt_keys_from_entity_id_path(
            self._entity_id_path,
            include_site_id_in_entity_id=self._include_site_id_in_entity_id,
        )
        self._attr_device_info = link_device_info(
            optimizer_device_identifier(entry.entry_id, inv_key, str_key, opt_key)
        )

    def _set_optimizer_units_and_state_class(self) -> None:
        """Set native_unit_of_measurement, device_class, state_class and suggested_display_precision from sensor type."""
        entry = self._OPTIMIZER_UNITS_MAP.get(self._sensor_type)
        if entry:
            unit, device_class, state_class, precision = entry
            self._attr_native_unit_of_measurement = unit
            self._attr_device_class = device_class
            self._attr_state_class = state_class
            if precision is not None:
                self._attr_suggested_display_precision = precision
        elif self._sensor_type == SENSOR_TYPE_LASTMEASUREMENT:
            self._attr_device_class = SensorDeviceClass.TIMESTAMP
            self._attr_state_class = None
        elif self._sensor_type == SENSOR_TYPE_STATUS:
            self._attr_state_class = None

    @property
    def suggested_object_id(self) -> str | None:
        """Suggest full entity object_id (no device prefix). Return same as internal when set."""
        if getattr(self, "internal_integration_suggested_object_id", None):
            return self.internal_integration_suggested_object_id
        slug = self._TRANSLATION_KEYS.get(
            self._sensor_type, self._sensor_type.lower().replace(" ", "_")
        )
        if not slug or not self._entity_id_path:
            return None
        path_str = "_".join(map(str, self._entity_id_path))
        return f"{self._base_name}{slug}_{path_str}"

    @property
    def extra_state_attributes(self) -> dict:
        """Expose panel type (description from API) as attribute; entity IDs remain unchanged.
        Uses current coordinator data so the value is up to date after refresh."""
        attrs = {}
        data = self.coordinator.data if self.coordinator else None
        if isinstance(data, dict):
            path = self._entity_id_path
            lookup_key = (path[-3], path[-2], path[-1]) if len(path) >= 3 else None
            item = data.get(lookup_key) if lookup_key else None
            if item is None:
                item = data.get(self._panelobject.panel_id)
            if item is not None:
                desc = getattr(item, "panel_description", None)
                if desc:
                    attrs["panel_type"] = desc
        if not attrs and getattr(self, "_panel", None):
            attrs["panel_type"] = self._panel
        return attrs

    @property
    def icon(self) -> str | None:
        """Return icon for status, azimuth, and tilt sensors."""
        if self._sensor_type == SENSOR_TYPE_AZIMUTH:
            return ICON_AZIMUTH
        if self._sensor_type == SENSOR_TYPE_TILT:
            return ICON_TILT
        if self._sensor_type != SENSOR_TYPE_STATUS:
            return None
        return status_icon(self._attr_native_value)

    def _lookup_optimizer_data_item(self, data: dict):
        """Resolve optimizer measurement object from coordinator data_dict."""
        panel_id = getattr(self._panelobject, "panel_id", None)
        if panel_id:
            item = data.get(panel_id)
            if item is not None and not hasattr(item, "entity_type"):
                return item
        inv, str_key, opt_key = opt_keys_from_entity_id_path(
            self._entity_id_path,
            include_site_id_in_entity_id=self._include_site_id_in_entity_id,
        )
        candidates = []
        if self._entity_id_path:
            opt_str = str(opt_key)
            digit_len = 0
            while digit_len < len(opt_str) and opt_str[digit_len].isdigit():
                digit_len += 1
            letter_suffix = opt_str[digit_len:].lower() if digit_len else ""
            # Try suffixed key first (e.g. 1.1.1a), then bare index, then raw path segment.
            if digit_len and letter_suffix:
                candidates.append((inv, str_key, f"{int(opt_str[:digit_len])}{letter_suffix}"))
            if digit_len:
                candidates.append((inv, str_key, int(opt_str[:digit_len])))
            candidates.append((inv, str_key, opt_key))
            if opt_str.isdigit():
                candidates.append((inv, str_key, int(opt_str)))
        for key in candidates:
            item = data.get(key)
            if item is not None and not hasattr(item, "entity_type"):
                return item
        return None

    def _zero_optimizer_when_item_missing(self) -> None:
        """Apply safe defaults when coordinator has data but this optimizer is not indexed yet."""
        if self._sensor_type in (
            SENSOR_TYPE_ENERGY,
            SENSOR_TYPE_LASTMEASUREMENT,
            SENSOR_TYPE_TEMPERATURE,
            SENSOR_TYPE_AZIMUTH,
            SENSOR_TYPE_TILT,
            SENSOR_TYPE_STATUS,
        ):
            return
        self._attr_native_value = 0

    def _timetocheck_and_ts(self, item):
        """Return (timetocheck, ts) with ts timezone-aware. May mutate item.lastmeasurement."""
        timetocheck = self.coordinator._timetocheck
        if timetocheck is None:
            timetocheck = datetime.now(timezone.utc) - CHECK_TIME_DELTA
        ts = item.lastmeasurement
        if isinstance(ts, datetime) and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
            item.lastmeasurement = ts
        return timetocheck, ts

    def _normalize_lifetime_energy(self, lifetime_energy):
        """Return lifetime_energy as float rounded to 3 decimals, or 0.0 if None."""
        if lifetime_energy is None:
            return 0.0
        if isinstance(lifetime_energy, float):
            return round(lifetime_energy, 3)
        return round(float(lifetime_energy), 3)

    def _set_energy_value_from_item(self, item) -> None:
        """Set _attr_native_value from item.lifetime_energy (monotonic: never decrease)."""
        new_value = self._normalize_lifetime_energy(item.lifetime_energy)
        if self._attr_native_value is None:
            self._attr_native_value = new_value
            return
        prev = float(self._attr_native_value) if not isinstance(self._attr_native_value, float) else self._attr_native_value
        if new_value >= prev:
            self._attr_native_value = new_value
        elif _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "Lifetime energy decreased for %s (new: %s, previous: %s), keeping previous value",
                self._log_name, new_value, self._attr_native_value,
            )

    def _set_temperature_value_from_item(self, item) -> None:
        """Set _attr_native_value from item.temperature."""
        actual_value = getattr(item, "temperature", None)
        if actual_value is None or actual_value == "":
            self._attr_native_value = None
            return
        try:
            self._attr_native_value = round(float(actual_value), 1)
        except (TypeError, ValueError):
            self._attr_native_value = None

    def _log_measurement_too_old(self, attr_name: str, ts: datetime, timetocheck, actual_value) -> None:
        """Log when measurement is too old and value is non-zero."""
        if actual_value != 0 and _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "Sensor %s (%s) set to 0: measurement too old (last: %s, threshold: %s)",
                self._log_name, attr_name, ts, timetocheck,
            )

    def _log_zero_value_recent(self, attr_name: str, ts: datetime) -> None:
        """Log when value is zero but measurement is recent (possible missing API data)."""
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "Sensor %s (%s) has zero value but measurement is recent (last: %s). "
                "This may indicate missing data in API response.",
                self._log_name, attr_name, ts,
            )

    def _round_live_value(self, actual_value) -> float | int:
        """Round live value for power/voltage/opt_voltage; return as-is for current."""
        if self._sensor_type in (SENSOR_TYPE_POWER, SENSOR_TYPE_VOLTAGE, SENSOR_TYPE_OPT_VOLTAGE):
            return round(float(actual_value), 2) if actual_value is not None else 0.0
        return actual_value

    def _set_live_value_from_item(self, item, measurement_too_old: bool, ts: datetime, timetocheck) -> None:
        """Set _attr_native_value from mapped attr (power/voltage/current/opt_voltage) with age check."""
        attr_name = self._SENSOR_ATTR_MAP.get(self._sensor_type)
        if not attr_name:
            return
        actual_value = getattr(item, attr_name, 0)
        if measurement_too_old:
            self._log_measurement_too_old(attr_name, ts, timetocheck, actual_value)
            self._attr_native_value = 0
            return
        if actual_value == 0:
            self._log_zero_value_recent(attr_name, ts)
        self._attr_native_value = self._round_live_value(actual_value)

    def _set_status_value_from_item(self, item) -> None:
        """Set _attr_native_value from item.status (display: blank, Active, Inactive, or raw)."""
        raw = getattr(item, "status", None)
        raw_str = str(raw).strip() if raw else ""
        self._attr_native_value = status_display_value(raw_str)
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "SolarEdge Optimizers sensor: %s status updated to '%s'",
                self._log_name, self._attr_native_value or "(empty)",
            )

    def _set_azimuth_value_from_item(self, item) -> None:
        """Set _attr_native_value from item.azimuth."""
        actual_value = getattr(item, "azimuth", None)
        if actual_value is None:
            self._attr_native_value = None
            return
        try:
            self._attr_native_value = round(float(actual_value), 2)
        except (TypeError, ValueError):
            self._attr_native_value = None

    def _set_tilt_value_from_item(self, item) -> None:
        """Set _attr_native_value from item.tilt."""
        actual_value = getattr(item, "tilt", None)
        if actual_value is None:
            self._attr_native_value = None
            return
        try:
            self._attr_native_value = round(float(actual_value), 2)
        except (TypeError, ValueError):
            self._attr_native_value = None

    def _update_optimizer_value_from_item(self, item, timetocheck, ts: datetime | None) -> None:
        """Set _attr_native_value from item (energy monotonic, lastmeasurement, status, azimuth, tilt, or mapped value with age check)."""
        if self._sensor_type == SENSOR_TYPE_ENERGY:
            self._set_energy_value_from_item(item)
        elif self._sensor_type == SENSOR_TYPE_LASTMEASUREMENT:
            self._attr_native_value = item.lastmeasurement
        elif self._sensor_type == SENSOR_TYPE_TEMPERATURE:
            self._set_temperature_value_from_item(item)
        elif self._sensor_type == SENSOR_TYPE_STATUS:
            self._set_status_value_from_item(item)
        elif self._sensor_type == SENSOR_TYPE_AZIMUTH:
            self._set_azimuth_value_from_item(item)
        elif self._sensor_type == SENSOR_TYPE_TILT:
            self._set_tilt_value_from_item(item)
        else:
            # Live sensors: when lastmeasurement is None (e.g. inactive with no API date), treat as too old
            if ts is None:
                self._attr_native_value = 0
            else:
                self._set_live_value_from_item(item, ts <= timetocheck, ts, timetocheck)

    def _zero_optimizer_when_no_data(self) -> None:
        """Zero native value when coordinator has no data (except energy, lastmeasurement, temperature)."""
        if self._sensor_type in (SENSOR_TYPE_ENERGY, SENSOR_TYPE_LASTMEASUREMENT, SENSOR_TYPE_TEMPERATURE):
            if self._sensor_type == SENSOR_TYPE_TEMPERATURE:
                self._attr_native_value = None
            return
        if (self._sensor_type != SENSOR_TYPE_ENERGY) and (self._sensor_type != SENSOR_TYPE_LASTMEASUREMENT):
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "SolarEdge Optimizers sensor: Coordinator data is None, zeroing %s (%s)",
                    self._log_name, self._sensor_type,
                )
            self._attr_native_value = 0

    def _normalize_optimizer_display_value(self) -> None:
        """Convert comma decimal strings to float for display. Apply decimal places (2 dp for power/voltage, 3 dp for energy)."""
        value = self._attr_native_value
        if isinstance(value, str) and "," in value:
            try:
                # Support comma as decimal separator (e.g. "26,18" -> 26.18)
                num = float(value.replace(",", "."))
                if self._sensor_type == SENSOR_TYPE_ENERGY:
                    num = round(num, 3)
                elif self._sensor_type in (SENSOR_TYPE_POWER, SENSOR_TYPE_VOLTAGE, SENSOR_TYPE_OPT_VOLTAGE):
                    num = round(num, 2)
                elif self._sensor_type == SENSOR_TYPE_TEMPERATURE:
                    num = round(num, 1)
                self._attr_native_value = num
            except ValueError:
                if _LOGGER.isEnabledFor(logging.WARNING):
                    _LOGGER.warning("Could not convert value '%s' to float for sensor %s", value, self._log_name)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        data = self.coordinator.data
        if data is not None:
            item = self._lookup_optimizer_data_item(data)
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "Update the sensor %s - %s with the info from the coordinator",
                    self._panelobject.panel_id, self._sensor_type,
                )
            if item is not None:
                self._panel = getattr(item, "panel_description", "") or ""
                # Update device info so device card shows panel type when it becomes available
                desc = (getattr(item, "panel_description", None) or "").strip()
                if desc and getattr(self, "_attr_device_info", None):
                    new_model = f"{getattr(item, 'model', '')} - {desc}".strip(" -") or desc
                    self._attr_device_info = {**self._attr_device_info, "model": new_model}
                timetocheck, ts = self._timetocheck_and_ts(item)
                self._update_optimizer_value_from_item(item, timetocheck, ts)
            else:
                if _LOGGER.isEnabledFor(logging.DEBUG):
                    _LOGGER.debug(
                        "SolarEdge Optimizers sensor: No coordinator data for panel_id=%s path=%s (%s)",
                        self._panelobject.panel_id,
                        self._entity_id_path,
                        self._sensor_type,
                    )
                self._zero_optimizer_when_item_missing()
        else:
            self._zero_optimizer_when_no_data()
        self._normalize_optimizer_display_value()
        self.async_write_ha_state()
