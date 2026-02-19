"""Sensor entities for SolarEdge Optimizers Home Assistant integration."""
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import DeviceInfo

import asyncio
import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)

from homeassistant.core import callback
from datetime import datetime, timezone

from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
)

from .const import (
    CONF_INCLUDE_SITE_ID_IN_ENTITY_ID,
    DOMAIN,
    CONF_ENTITY_PREFIX,
    SENSOR_TYPE_INDIVIDUAL,
    SENSOR_TYPE_AGGREGATED_STRING,
    SENSOR_TYPE_AGGREGATED_INVERTER,
    SENSOR_TYPE_AGGREGATED_SITE,
    SENSOR_TYPE_OPT_VOLTAGE,
    SENSOR_TYPE_CURRENT,
    SENSOR_TYPE_POWER,
    SENSOR_TYPE_VOLTAGE,
    SENSOR_TYPE_ENERGY,
    SENSOR_TYPE_LASTMEASUREMENT,
    SENSOR_TYPE_CHILD_COUNT,
    CHECK_TIME_DELTA,
)

# Changed import to use coordinator module
from .coordinator import MyCoordinator

from homeassistant.const import (
    UnitOfPower,
    UnitOfElectricPotential,
    UnitOfElectricCurrent,
    UnitOfEnergy,
)

# Changed from absolute import to relative import to use local solaredgeoptimizers.py instead of site-packages version
from .solaredgeoptimizers import (
    SolarEdgeOptimizerData,
    SolarEdgeAggregatedData,
    SolarlEdgeOptimizer,
)

_LOGGER = logging.getLogger(__name__)


def _parse_optimizer_display_name_path(display_name: str, include_site_id: bool, site_id: str):
    """Parse optimizer displayName (e.g. 'Optimizer 1.2.1' or '1.2.1') into entity_id_path.
    Returns (inv, str, opt) or (site_id, inv, str, opt) when displayName looks like a path; else None.
    This keeps entity IDs aligned with the friendly name when the API uses different ordering than layout."""
    if not display_name:
        return None
    raw = display_name.strip()
    if raw.upper().startswith("OPTIMIZER "):
        raw = raw[10:].strip()
    parts = raw.split(".")
    if len(parts) not in (3, 4):
        return None
    try:
        nums = [int(p.strip()) for p in parts if p.strip().isdigit()]
    except (ValueError, TypeError):
        return None
    if len(nums) != len(parts):
        return None
    if include_site_id:
        if len(nums) == 4:
            return (str(nums[0]), nums[1], nums[2], nums[3])
        if len(nums) == 3 and site_id:
            return (site_id, nums[0], nums[1], nums[2])
    else:
        if len(nums) == 3:
            return (nums[0], nums[1], nums[2])
        if len(nums) == 4:
            return (nums[1], nums[2], nums[3])
    return None


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
    Called at start of setup so new entities get entity_id from current suggested_object_id
    (e.g. after reconfigure with prefix removed, entities become sensor.power_* not sensor.prefix_power_*).
    Match by config_entry_id and by unique_id prefix (entry_id) so we find entities even if
    config_entry_id was cleared during unload.
    """
    ent_reg = er.async_get(hass)
    entry_id = entry.entry_id
    to_remove = _collect_entity_ids_for_config_entry(ent_reg, entry_id)
    _remove_entities_from_registry(ent_reg, to_remove)
    if to_remove:
        _LOGGER.info(
            "SolarEdge Optimizers sensor: Removing %d existing entities for entry %s so new entity_ids match current prefix",
            len(to_remove),
            entry_id,
        )


def _build_optimizer_tasks(site):
    """Build list of (optimizer, inverter, string, inv_idx, str_idx, opt_idx) for all optimizers."""
    tasks = []
    for inv_idx, inverter in enumerate(site.inverters, start=1):
        _LOGGER.info("Adding all optimizers from inverter: %s", inv_idx)
        for str_idx, string in enumerate(inverter.strings, start=1):
            for opt_idx, optimizer in enumerate(string.optimizers, start=1):
                tasks.append((optimizer, inverter, string, inv_idx, str_idx, opt_idx))
    return tasks


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
    _LOGGER.info(
        "Fetching optimizer data for %d optimizers (rest from coordinator cache)...",
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
    optimizer, _inverter, _string, inv_idx, str_idx, _opt_idx = optimizer_tasks[task_idx]
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
    """Build list of SolarEdgeOptimizersSensor entities for all optimizers that have info."""
    sensors_to_add = []
    for task_idx, (optimizer, inverter, string, inv_idx, str_idx, opt_idx) in enumerate(optimizer_tasks):
        info, skip = _get_optimizer_info_for_task(
            task_idx, optimizer_tasks, coordinator_data, results_by_task_idx
        )
        if skip or info is None:
            continue
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "SolarEdge Optimizers sensor: Adding optimizer panel_id=%s serial=%s model=%s",
                optimizer.optimizerId, getattr(info, "serialnumber", ""), getattr(info, "model", ""),
            )
        path_from_display = _parse_optimizer_display_name_path(
            getattr(optimizer, "displayName", None), include_site_id, site_id
        )
        entity_id_path = (
            path_from_display
            if path_from_display is not None
            else ((site_id, inv_idx, str_idx, opt_idx) if include_site_id else (inv_idx, str_idx, opt_idx))
        )
        for sensortype in SENSOR_TYPE_INDIVIDUAL:
            sensors_to_add.append(
                SolarEdgeOptimizersSensor(
                    coordinator, hass, entry, info, sensortype, optimizer, inverter, string,
                    base_name=base_name, site_id=site_id, entity_id_path=entity_id_path,
                )
            )
    return sensors_to_add


def _build_aggregated_sensors(coordinator, hass, entry, site_struct, base_name, include_site_id, site_id):
    """Build list of SolarEdgeAggregatedSensor entities for strings, inverters, and site."""
    sensors = []
    if not site_struct:
        return sensors
    for inv_idx, inverter in enumerate(site_struct.inverters, start=1):
        for str_idx, string in enumerate(inverter.strings, start=1):
            string_aggregated = SolarEdgeAggregatedData(
                entity_id=f"string_{string.stringId}",
                entity_type="string",
                entity_id_path=(site_id, inv_idx, str_idx) if include_site_id else (inv_idx, str_idx),
            )
            string_aggregated.serialnumber = f"String_{string.stringId}"
            string_aggregated.panel_description = string.displayName
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("Creating aggregated sensors for string: %s", string.displayName)
            for sensortype in SENSOR_TYPE_AGGREGATED_STRING:
                sensors.append(
                    SolarEdgeAggregatedSensor(
                        coordinator, hass, entry, string_aggregated, sensortype, string, inverter,
                        base_name=base_name,
                    )
                )
    for inv_idx, inverter in enumerate(site_struct.inverters, start=1):
        inverter_aggregated = SolarEdgeAggregatedData(
            entity_id=f"inverter_{inverter.inverterId}",
            entity_type="inverter",
            entity_id_path=(site_id, inv_idx) if include_site_id else (inv_idx,),
        )
        inverter_aggregated.serialnumber = inverter.serialNumber or f"Inverter_{inverter.inverterId}"
        inverter_aggregated.panel_description = inverter.displayName
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("Creating aggregated sensors for inverter: %s", inverter.displayName)
        for sensortype in SENSOR_TYPE_AGGREGATED_INVERTER:
            sensors.append(
                SolarEdgeAggregatedSensor(
                    coordinator, hass, entry, inverter_aggregated, sensortype, None, inverter,
                    base_name=base_name,
                )
            )
    site_aggregated = SolarEdgeAggregatedData(
        entity_id=f"site_{site_struct.siteId}",
        entity_type="site",
        entity_id_path=(site_id,),
    )
    site_aggregated.serialnumber = f"Site_{site_struct.siteId}"
    site_aggregated.panel_description = f"Site {site_struct.siteId}"
    if _LOGGER.isEnabledFor(logging.DEBUG):
        _LOGGER.debug("Creating aggregated sensors for site: %s", site_id)
    for sensortype in SENSOR_TYPE_AGGREGATED_SITE:
        sensors.append(
            SolarEdgeAggregatedSensor(
                coordinator, hass, entry, site_aggregated, sensortype, None, None, base_name=base_name,
            )
        )
    return sensors


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add an solarEdge entry."""
    if _LOGGER.isEnabledFor(logging.DEBUG):
        _LOGGER.debug("SolarEdge Optimizers sensor: async_setup_entry for entry_id=%s", entry.entry_id)
    coordinator: MyCoordinator = hass.data[DOMAIN][entry.entry_id]
    site = coordinator._site_structure
    if site is None:
        _LOGGER.error("SolarEdge Optimizers sensor: No site structure on coordinator; setup cannot continue")
        return

    try:
        _remove_sensor_entities_for_entry(hass, entry)
    except Exception as e:  # pylint: disable=broad-except
        _LOGGER.warning(
            "SolarEdge Optimizers sensor: Error removing existing entities before setup: %s", e,
        )

    _LOGGER.info("Found all information for site: %s", site.siteId)
    _LOGGER.info("Site has %s inverters", len(site.inverters))
    _LOGGER.info(
        "Setting up sensors for %s optimizers (plus string/inverter/site aggregations)",
        site.returnNumberOfOptimizers(),
    )
    base_name = _entity_prefix(entry)
    site_id = str(site.siteId)
    include_site_id = entry.options.get(
        CONF_INCLUDE_SITE_ID_IN_ENTITY_ID,
        entry.data.get(CONF_INCLUDE_SITE_ID_IN_ENTITY_ID, False),
    )
    _LOGGER.info(
        "SolarEdge Optimizers sensor: entity_id prefix=%r, include_site_id_in_entity_id=%s",
        base_name or "(empty)", include_site_id,
    )
    if _LOGGER.isEnabledFor(logging.DEBUG):
        _LOGGER.debug(
            "SolarEdge Optimizers sensor: base_name=%r include_site_id=%s site_id=%s",
            base_name, include_site_id, site_id,
        )

    optimizer_tasks = _build_optimizer_tasks(site)
    coordinator_data = coordinator.data if isinstance(coordinator.data, dict) else None
    results_by_task_idx = await _fetch_missing_optimizer_data(
        hass, coordinator, optimizer_tasks, coordinator_data
    )

    sensors_to_add = _build_individual_optimizer_sensors(
        coordinator, hass, entry, optimizer_tasks, coordinator_data, results_by_task_idx,
        base_name, site_id, include_site_id,
    )
    sensors_to_add.append(
        SolarEdgeIntegrationLastPolledSensor(
            coordinator, hass, entry, site_id, base_name=base_name, include_site_id_in_entity_id=include_site_id
        )
    )
    sensors_to_add.append(
        SolarEdgeObtainedFromSensor(
            coordinator, hass, entry, site_id, base_name=base_name, include_site_id_in_entity_id=include_site_id
        )
    )
    sensors_to_add.extend(
        _build_aggregated_sensors(coordinator, hass, entry, coordinator._site_structure, base_name, include_site_id, site_id)
    )

    if sensors_to_add:
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "SolarEdge Optimizers sensor: Adding %d entities (update_before_add=True)", len(sensors_to_add),
            )
        async_add_entities(sensors_to_add, update_before_add=True)
        individual_count = len(optimizer_tasks) * len(SENSOR_TYPE_INDIVIDUAL)
        aggregated_count = len(sensors_to_add) - individual_count
        _LOGGER.info(
            "Done adding all sensors. Added %s sensors in total (%s individual optimizers + %s aggregated sensors).",
            len(sensors_to_add), individual_count, aggregated_count,
        )
    else:
        _LOGGER.warning("No sensors were created - check for errors above")


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
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"site_{site_id}")},
            manufacturer="SolarEdge",
            model=f"SITE {site_id}",
            translation_key="site_device",
            translation_placeholders={"site_id": str(site_id)},
        )

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
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"site_{site_id}")},
            manufacturer="SolarEdge",
            model=f"SITE {site_id}",
            translation_key="site_device",
            translation_placeholders={"site_id": str(site_id)},
        )

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
    }
    # Translation keys for entity names (i18n)
    _TRANSLATION_KEYS = {
        SENSOR_TYPE_LASTMEASUREMENT: "last_measurement",
        SENSOR_TYPE_CHILD_COUNT: None,  # Resolved per entity_type below
        SENSOR_TYPE_CURRENT: "current_average",
        SENSOR_TYPE_VOLTAGE: "voltage_average",
        SENSOR_TYPE_ENERGY: "lifetime_energy",
        SENSOR_TYPE_POWER: "power",
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
    ) -> None:
        super().__init__(coordinator)
        self._hass = hass
        self._entry = entry
        self._panelobject = panel
        self._string = string
        self._inverter = inverter
        self._base_name = (base_name + "_") if base_name else ""
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
        if self._sensor_type is SENSOR_TYPE_CHILD_COUNT:
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

    def _set_aggregated_device_info(
        self, entry: ConfigEntry, panel: SolarEdgeAggregatedData, string, inverter
    ) -> None:
        """Set device_info based on entity type (string, inverter, or site)."""
        if panel.entity_type == "string":
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, f"{entry.entry_id}_{string.stringId}")},
                manufacturer="SolarEdge",
                model=f"STRING {string.displayName}",
                translation_key="string_device",
                translation_placeholders={"display_name": str(string.displayName)},
                via_device=(DOMAIN, inverter.serialNumber),
            )
        elif panel.entity_type == "inverter":
            site_id = self.coordinator._site_structure.siteId if self.coordinator._site_structure else None
            via_device = (DOMAIN, f"site_{site_id}") if site_id else None
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, inverter.serialNumber)},
                translation_key="inverter_device",
                translation_placeholders={"display_name": str(inverter.displayName)},
                via_device=via_device,
            )
        else:
            site_id = panel.panel_id.split("_")[1] if "_" in panel.panel_id else ""
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, f"site_{site_id}")},
                translation_key="site_device",
                translation_placeholders={"site_id": site_id or "—"},
            )

    def _set_aggregated_units_and_state_class(self) -> None:
        """Set native_unit_of_measurement, device_class, state_class and suggested_display_precision from sensor type."""
        if self._sensor_type is SENSOR_TYPE_VOLTAGE:
            self._attr_native_unit_of_measurement = UnitOfElectricPotential.VOLT
            self._attr_device_class = SensorDeviceClass.VOLTAGE
            self._attr_state_class = SensorStateClass.MEASUREMENT
            self._attr_suggested_display_precision = 2
        elif self._sensor_type is SENSOR_TYPE_CURRENT:
            self._attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
            self._attr_device_class = SensorDeviceClass.CURRENT
            self._attr_state_class = SensorStateClass.MEASUREMENT
        elif self._sensor_type is SENSOR_TYPE_POWER:
            self._attr_native_unit_of_measurement = UnitOfPower.WATT
            self._attr_device_class = SensorDeviceClass.POWER
            self._attr_state_class = SensorStateClass.MEASUREMENT
            self._attr_suggested_display_precision = 2
        elif self._sensor_type is SENSOR_TYPE_ENERGY:
            self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
            self._attr_device_class = SensorDeviceClass.ENERGY
            self._attr_state_class = SensorStateClass.TOTAL_INCREASING
            self._attr_suggested_display_precision = 3
        elif self._sensor_type is SENSOR_TYPE_LASTMEASUREMENT:
            self._attr_device_class = SensorDeviceClass.TIMESTAMP
            self._attr_state_class = None
        elif self._sensor_type is SENSOR_TYPE_CHILD_COUNT:
            self._attr_state_class = None

    def _slug_for_sensortype(self) -> str:
        """Return entity_id slug for this sensor type (e.g. power, child_count, inverter_count)."""
        if self._sensor_type is SENSOR_TYPE_CHILD_COUNT:
            if self._panelobject.entity_type == "string":
                return "child_count"
            if self._panelobject.entity_type == "inverter":
                return "child_count"
            return "inverter_count"
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

    def _compute_aggregated_native_value(self, item) -> None:
        """Update _attr_native_value from aggregated item (child_count, energy monotonic, or mapped attribute)."""
        attr_name = self._SENSOR_ATTR_MAP.get(self._sensor_type)
        if not attr_name:
            return
        new_value = getattr(item, attr_name, 0)
        if self._sensor_type is SENSOR_TYPE_CHILD_COUNT:
            new_value = int(new_value) if new_value is not None else 0
        elif self._sensor_type is SENSOR_TYPE_ENERGY:
            if new_value is not None:
                new_value = round(float(new_value), 3) if not isinstance(new_value, float) else round(new_value, 3)
            else:
                new_value = 0.0
            if self._attr_native_value is not None:
                prev = float(self._attr_native_value) if not isinstance(self._attr_native_value, float) else self._attr_native_value
                new_value = max(new_value, prev)
        elif self._sensor_type in (SENSOR_TYPE_POWER, SENSOR_TYPE_VOLTAGE):
            # String/inverter/site: power and voltage (average) to 2 dp
            if new_value is not None:
                new_value = round(float(new_value), 2) if not isinstance(new_value, float) else round(new_value, 2)
            else:
                new_value = 0.0
        self._attr_native_value = new_value

    def _normalize_aggregated_display_value(self) -> None:
        """Convert comma decimals to float and ensure child_count is int. Apply decimal places for display."""
        value = self._attr_native_value
        if isinstance(value, str) and "," in value:
            try:
                num = float(value.replace(",", "."))
                if self._sensor_type is SENSOR_TYPE_ENERGY:
                    num = round(num, 3)
                elif self._sensor_type in (SENSOR_TYPE_POWER, SENSOR_TYPE_VOLTAGE):
                    num = round(num, 2)
                self._attr_native_value = num
            except ValueError:
                _LOGGER.warning("Could not convert value '%s' to float for sensor %s", value, self._log_name)
        if self._sensor_type is SENSOR_TYPE_CHILD_COUNT and self._attr_native_value is not None:
            self._attr_native_value = int(self._attr_native_value)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if self.coordinator.data is None:
            return
        item = self.coordinator.data.get(self._panelobject.panel_id)
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


class SolarEdgeOptimizersSensor(CoordinatorEntity, SensorEntity):
    """An entity using CoordinatorEntity.

    The CoordinatorEntity class provides:
      should_poll
      async_update
      async_added_to_hass
      available

    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_has_entity_name = True

    # Class-level constant for sensor attribute mapping to avoid recreating on every update
    _SENSOR_ATTR_MAP = {
        SENSOR_TYPE_VOLTAGE: "voltage",
        SENSOR_TYPE_CURRENT: "current",
        SENSOR_TYPE_OPT_VOLTAGE: "optimizer_voltage",
        SENSOR_TYPE_POWER: "power",
    }
    # Translation keys for entity names (i18n)
    _TRANSLATION_KEYS = {
        SENSOR_TYPE_VOLTAGE: "voltage",
        SENSOR_TYPE_CURRENT: "current",
        SENSOR_TYPE_OPT_VOLTAGE: "optimizer_voltage",
        SENSOR_TYPE_POWER: "power",
        SENSOR_TYPE_ENERGY: "lifetime_energy",
        SENSOR_TYPE_LASTMEASUREMENT: "last_measurement",
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
        self._panel = panel.panel_description
        self._sensor_type = sensortype
        self._set_optimizer_identity(entry, panel, sensortype, optimizer, site_id, entity_id_path)
        self._set_optimizer_device_info(entry, panel, string)
        self._set_optimizer_units_and_state_class()

    def _set_optimizer_identity(
        self, entry: ConfigEntry, panel: SolarEdgeOptimizerData, sensortype,
        optimizer: SolarlEdgeOptimizer, site_id: str, entity_id_path: tuple,
    ) -> None:
        """Set unique_id, translation_key, suggested_object_id and display names for this optimizer sensor."""
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
        self._attr_translation_key = self._TRANSLATION_KEYS.get(
            self._sensor_type, self._sensor_type.lower().replace(" ", "_")
        )
        self._log_name = f"{self._sensor_type} {optimizer.displayName}"
        self._optimizer_display_name = (
            f"{site_id}.{'.'.join(map(str, entity_id_path[1:]))}" if len(entity_id_path) >= 4 else str(optimizer.displayName)
        )
        if slug and path_str:
            self.internal_integration_suggested_object_id = f"{self._base_name}{slug}_{path_str}"

    def _set_optimizer_device_info(self, entry: ConfigEntry, panel: SolarEdgeOptimizerData, string) -> None:
        """Set device_info with via_device so optimizer is grouped under string device."""
        via_device = (DOMAIN, f"{entry.entry_id}_{string.stringId}") if string else None
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, panel.serialnumber)},
            manufacturer=panel.manufacturer,
            model=panel.model,
            hw_version=panel.serialnumber,
            via_device=via_device,
            translation_key="optimizer_device",
            translation_placeholders={"display_name": self._optimizer_display_name},
        )

    def _set_optimizer_units_and_state_class(self) -> None:
        """Set native_unit_of_measurement, device_class, state_class and suggested_display_precision from sensor type."""
        if self._sensor_type is SENSOR_TYPE_VOLTAGE:
            self._attr_native_unit_of_measurement = UnitOfElectricPotential.VOLT
            self._attr_device_class = SensorDeviceClass.VOLTAGE
            self._attr_state_class = SensorStateClass.MEASUREMENT
            self._attr_suggested_display_precision = 2
        elif self._sensor_type is SENSOR_TYPE_CURRENT:
            self._attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
            self._attr_device_class = SensorDeviceClass.CURRENT
            self._attr_state_class = SensorStateClass.MEASUREMENT
        elif self._sensor_type is SENSOR_TYPE_OPT_VOLTAGE:
            self._attr_native_unit_of_measurement = UnitOfElectricPotential.VOLT
            self._attr_device_class = SensorDeviceClass.VOLTAGE
            self._attr_state_class = SensorStateClass.MEASUREMENT
            self._attr_suggested_display_precision = 2
        elif self._sensor_type is SENSOR_TYPE_POWER:
            self._attr_native_unit_of_measurement = UnitOfPower.WATT
            self._attr_device_class = SensorDeviceClass.POWER
            self._attr_state_class = SensorStateClass.MEASUREMENT
            self._attr_suggested_display_precision = 2
        elif self._sensor_type is SENSOR_TYPE_ENERGY:
            self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
            self._attr_device_class = SensorDeviceClass.ENERGY
            self._attr_state_class = SensorStateClass.TOTAL_INCREASING
            self._attr_suggested_display_precision = 3
        elif self._sensor_type is SENSOR_TYPE_LASTMEASUREMENT:
            self._attr_device_class = SensorDeviceClass.TIMESTAMP
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

    def _update_optimizer_value_from_item(self, item, timetocheck, ts: datetime) -> None:
        """Set _attr_native_value from item (energy monotonic, lastmeasurement, or mapped value with age check)."""
        measurement_too_old = ts <= timetocheck
        if self._sensor_type is SENSOR_TYPE_ENERGY:
            lifetime_energy = item.lifetime_energy
            new_value = (
                round(float(lifetime_energy), 3) if not isinstance(lifetime_energy, float) else round(lifetime_energy, 3)
                if lifetime_energy is not None else 0.0
            )
            if self._attr_native_value is None:
                self._attr_native_value = new_value
            else:
                prev = float(self._attr_native_value) if not isinstance(self._attr_native_value, float) else self._attr_native_value
                if new_value >= prev:
                    self._attr_native_value = new_value
                elif _LOGGER.isEnabledFor(logging.DEBUG):
                    _LOGGER.debug(
                        "Lifetime energy decreased for %s (new: %s, previous: %s), keeping previous value",
                        self._log_name, new_value, self._attr_native_value,
                    )
        elif self._sensor_type is SENSOR_TYPE_LASTMEASUREMENT:
            self._attr_native_value = item.lastmeasurement
        else:
            attr_name = self._SENSOR_ATTR_MAP.get(self._sensor_type)
            if attr_name:
                actual_value = getattr(item, attr_name, 0)
                if measurement_too_old:
                    if actual_value != 0 and _LOGGER.isEnabledFor(logging.DEBUG):
                        _LOGGER.debug(
                            "Sensor %s (%s) set to 0: measurement too old (last: %s, threshold: %s)",
                            self._log_name, attr_name, ts, timetocheck,
                        )
                    self._attr_native_value = 0
                else:
                    if actual_value == 0 and _LOGGER.isEnabledFor(logging.DEBUG):
                        _LOGGER.debug(
                            "Sensor %s (%s) has zero value but measurement is recent (last: %s). "
                            "This may indicate missing data in API response.",
                            self._log_name, attr_name, ts,
                        )
                    # Optimizer level: voltage, power, optimizer voltage to 2 dp
                    if self._sensor_type in (SENSOR_TYPE_POWER, SENSOR_TYPE_VOLTAGE, SENSOR_TYPE_OPT_VOLTAGE):
                        actual_value = round(float(actual_value), 2) if actual_value is not None else 0.0
                    self._attr_native_value = actual_value

    def _zero_optimizer_when_no_data(self) -> None:
        """Zero native value when coordinator has no data (except energy and lastmeasurement)."""
        if (self._sensor_type is not SENSOR_TYPE_ENERGY) and (self._sensor_type is not SENSOR_TYPE_LASTMEASUREMENT):
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
                if self._sensor_type is SENSOR_TYPE_ENERGY:
                    num = round(num, 3)
                elif self._sensor_type in (SENSOR_TYPE_POWER, SENSOR_TYPE_VOLTAGE, SENSOR_TYPE_OPT_VOLTAGE):
                    num = round(num, 2)
                self._attr_native_value = num
            except ValueError:
                if _LOGGER.isEnabledFor(logging.WARNING):
                    _LOGGER.warning("Could not convert value '%s' to float for sensor %s", value, self._log_name)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if self.coordinator.data is not None:
            panel_id = self._panelobject.panel_id
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "Update the sensor %s - %s with the info from the coordinator", panel_id, self._sensor_type,
                )
            item = self.coordinator.data.get(panel_id)
            if item is not None:
                timetocheck, ts = self._timetocheck_and_ts(item)
                self._update_optimizer_value_from_item(item, timetocheck, ts)
        else:
            self._zero_optimizer_when_no_data()
        self._normalize_optimizer_display_value()
        self.async_write_ha_state()
