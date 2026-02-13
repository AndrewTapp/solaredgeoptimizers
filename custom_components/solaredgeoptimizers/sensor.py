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

# AJT: 10-Jan-2025: Changed import to use coordinator module
from .coordinator import MyCoordinator

from homeassistant.const import (
    UnitOfPower,
    UnitOfElectricPotential,
    UnitOfElectricCurrent,
    UnitOfEnergy,
)

# AJT: 10-Jan-2025: Changed from absolute import to relative import to use local solaredgeoptimizers.py instead of site-packages version
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


def _remove_sensor_entities_for_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove all sensor entities for this config entry from the entity registry.
    Called at start of setup so new entities get entity_id from current suggested_object_id
    (e.g. after reconfigure with prefix removed, entities become sensor.power_* not sensor.prefix_power_*).
    Match by config_entry_id and by unique_id prefix (entry_id) so we find entities even if
    config_entry_id was cleared during unload.
    """
    ent_reg = er.async_get(hass)
    entry_id = entry.entry_id
    to_remove: set[str] = set()
    # 1) By config_entry_id index
    if hasattr(ent_reg.entities, "get_entries_for_config_entry_id"):
        for e in ent_reg.entities.get_entries_for_config_entry_id(entry_id):
            to_remove.add(e.entity_id)
    # 2) Iterate registry: add any entity whose unique_id starts with entry_id (all our entities)
    def _match(reg_entry) -> bool:
        if getattr(reg_entry, "config_entry_id", None) == entry_id:
            return True
        uid = getattr(reg_entry, "unique_id", None)
        return bool(uid and str(uid).startswith(entry_id))
    if hasattr(ent_reg.entities, "data"):
        for eid, reg_entry in list(getattr(ent_reg.entities, "data", {}).items()):
            if _match(reg_entry):
                to_remove.add(eid)
    if hasattr(ent_reg.entities, "values"):
        for entity in ent_reg.entities.values():
            eid = getattr(entity, "entity_id", None)
            if eid and _match(entity):
                to_remove.add(eid)
    # 3) Fallback: iterate by entity_id (ent_reg.entities is dict-like)
    try:
        for eid in ent_reg.entities:
            if eid in to_remove:
                continue
            reg_entry = ent_reg.async_get(eid) if hasattr(ent_reg, "async_get") else None
            if reg_entry is None and hasattr(ent_reg.entities, "data"):
                reg_entry = getattr(ent_reg.entities, "data", {}).get(eid)
            if reg_entry and _match(reg_entry):
                to_remove.add(eid)
    except Exception:  # pylint: disable=broad-except
        pass
    for eid in to_remove:
        try:
            ent_reg.async_remove(eid)
        except Exception as e:  # pylint: disable=broad-except
            _LOGGER.warning(
                "SolarEdge Optimizers sensor: Could not remove entity %s: %s",
                eid,
                e,
            )
    if to_remove:
        _LOGGER.info(
            "SolarEdge Optimizers sensor: Removing %d existing entities for entry %s so new entity_ids match current prefix",
            len(to_remove),
            entry_id,
        )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add an solarEdge entry."""
    if _LOGGER.isEnabledFor(logging.DEBUG):
        _LOGGER.debug(
            "SolarEdge Optimizers sensor: async_setup_entry for entry_id=%s",
            entry.entry_id,
        )
    # Add the needed sensors to hass (use coordinator's site structure from first refresh; no duplicate API call)
    coordinator: MyCoordinator = hass.data[DOMAIN][entry.entry_id]
    site = coordinator._site_structure
    if site is None:
        _LOGGER.error("SolarEdge Optimizers sensor: No site structure on coordinator; setup cannot continue")
        return

    # Remove existing sensor entities for this entry so new ones get entity_id from current options (e.g. prefix)
    try:
        _remove_sensor_entities_for_entry(hass, entry)
    except Exception as e:  # pylint: disable=broad-except
        _LOGGER.warning(
            "SolarEdge Optimizers sensor: Error removing existing entities before setup: %s",
            e,
        )

    _LOGGER.info("Found all information for site: %s", site.siteId)
    _LOGGER.info("Site has %s inverters", len(site.inverters))
    _LOGGER.info(
        "Setting up sensors for %s optimizers (plus string/inverter/site aggregations)",
        site.returnNumberOfOptimizers(),
    )

    base_name = _entity_prefix(entry)
    site_id = str(site.siteId)
    # Options override data; default False when key missing (e.g. upgraded from old version)
    include_site_id = entry.options.get(
        CONF_INCLUDE_SITE_ID_IN_ENTITY_ID,
        entry.data.get(CONF_INCLUDE_SITE_ID_IN_ENTITY_ID, False),
    )
    _LOGGER.info(
        "SolarEdge Optimizers sensor: entity_id prefix=%r, include_site_id_in_entity_id=%s",
        base_name or "(empty)",
        include_site_id,
    )
    optimizer_tasks = []
    for inv_idx, inverter in enumerate(site.inverters, start=1):
        _LOGGER.info("Adding all optimizers from inverter: %s", inv_idx)
        for str_idx, string in enumerate(inverter.strings, start=1):
            for opt_idx, optimizer in enumerate(string.optimizers, start=1):
                optimizer_tasks.append((optimizer, inverter, string, inv_idx, str_idx, opt_idx))

    # AJT: 16-Jan-2026: Parallelize API calls using asyncio.gather for 10-20x speedup
    _LOGGER.info("Fetching optimizer data in parallel...")
    results = await asyncio.gather(
        *[
            hass.async_add_executor_job(
                coordinator.my_api.requestSystemData, opt.optimizerId
            )
            for opt, *_ in optimizer_tasks
        ],
        return_exceptions=True
    )

    sensors_to_add = []
    for (optimizer, inverter, string, inv_idx, str_idx, opt_idx), info in zip(optimizer_tasks, results):
        if isinstance(info, Exception):
            _LOGGER.error(
                "Error fetching data for optimizer %s: %s",
                optimizer.optimizerId,
                info
            )
            continue

        if info is not None:
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "Added optimizer for panel_id: %s to Home Assistant",
                    optimizer.displayName,
                )
            for sensortype in SENSOR_TYPE_INDIVIDUAL:
                sensors_to_add.append(
                    SolarEdgeOptimizersSensor(
                        coordinator,
                        hass,
                        entry,
                        info,
                        sensortype,
                        optimizer,
                        inverter,
                        string,
                        base_name=base_name,
                        site_id=site_id,
                        entity_id_path=(site_id, inv_idx, str_idx, opt_idx) if include_site_id else (inv_idx, str_idx, opt_idx),
                    )
                )

    sensors_to_add.append(
        SolarEdgeIntegrationLastPolledSensor(
            coordinator, hass, entry, site_id, base_name=base_name, include_site_id_in_entity_id=include_site_id
        )
    )

    site_struct = coordinator._site_structure
    if site_struct:
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
                    sensors_to_add.append(
                        SolarEdgeAggregatedSensor(
                            coordinator,
                            hass,
                            entry,
                            string_aggregated,
                            sensortype,
                            string,
                            inverter,
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
                sensors_to_add.append(
                    SolarEdgeAggregatedSensor(
                        coordinator,
                        hass,
                        entry,
                        inverter_aggregated,
                        sensortype,
                        None,
                        inverter,
                    base_name=base_name,
                )
                )

        site_aggregated = SolarEdgeAggregatedData(
            entity_id=f"site_{site_struct.siteId}",
            entity_type="site",
            entity_id_path=(site_id,),  # Site level always uses actual site ID in entity ID
        )
        site_aggregated.serialnumber = f"Site_{site_struct.siteId}"
        site_aggregated.panel_description = f"Site {site_struct.siteId}"

        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("Creating aggregated sensors for site: %s", site_id)
        for sensortype in SENSOR_TYPE_AGGREGATED_SITE:
            sensors_to_add.append(
                SolarEdgeAggregatedSensor(
                    coordinator,
                    hass,
                    entry,
                    site_aggregated,
                    sensortype,
                    None,
                    None,
                    base_name=base_name,
                )
            )

    # Add all sensors at once
    if sensors_to_add:
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "SolarEdge Optimizers sensor: Adding %d entities (update_before_add=True)",
                len(sensors_to_add),
            )
        async_add_entities(sensors_to_add, update_before_add=True)
        individual_count = len(optimizer_tasks) * len(SENSOR_TYPE_INDIVIDUAL)
        aggregated_count = len(sensors_to_add) - individual_count
        _LOGGER.info(
            "Done adding all sensors. Added %s sensors in total (%s individual optimizers + %s aggregated sensors).",
            len(sensors_to_add),
            individual_count,
            aggregated_count
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


# class MyEntity(CoordinatorEntity, SensorEntity):
class SolarEdgeAggregatedSensor(CoordinatorEntity, SensorEntity):
    """An entity for aggregated SolarEdge measurements at string/inverter level."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_has_entity_name = True

    # AJT: 27-Jan-2026: Class-level constant for sensor attribute mapping to avoid recreating on every update
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
        path_str = "_".join(map(str, getattr(panel, "entity_id_path", ())))
        # Site level: always use numeric site id for entity_id (never panel_id "site_2065855") and ensure prefix is applied
        if panel.entity_type == "site" and not path_str and "_" in getattr(panel, "panel_id", ""):
            path_str = str(panel.panel_id).split("_", 1)[1]
        slug = self._slug_for_sensortype()
        # Include prefix in unique_id so reconfigure (add/remove prefix) produces new entity_id instead of keeping old one
        _uid_parts = [entry.entry_id]
        if self._base_name:
            _uid_parts.append(self._base_name.rstrip("_"))
        _uid_parts.append(slug)
        _uid_parts.append(path_str if path_str else panel.panel_id)
        self._attr_unique_id = "_".join(_uid_parts)

        # Force HA to use our full object_id (no device-name prefix like "site_123_" or "inverter_1_").
        # Always set both so entity_id is sensor.[prefix]slug_path (e.g. sensor.[prefix]power_2065855 at site level).
        object_id = f"{self._base_name}{slug}_{path_str}" if path_str else f"{self._base_name}{slug}_{panel.panel_id}"
        if object_id.strip("_"):  # avoid setting empty or underscore-only
            self.internal_integration_suggested_object_id = object_id

        # Translation key for entity name (i18n)
        if self._sensor_type is SENSOR_TYPE_CHILD_COUNT:
            if panel.entity_type == "string":
                self._attr_translation_key = "optimizer_count"
            elif panel.entity_type == "inverter":
                self._attr_translation_key = "string_count"
            else:
                self._attr_translation_key = "inverter_count"
        else:
            self._attr_translation_key = self._TRANSLATION_KEYS.get(
                self._sensor_type,
                self._sensor_type.lower().replace(" ", "_"),
            )
        # Stable name for logging ( _attr_name may be unset when using translation_key )
        self._log_name = f"{panel.panel_id}_{sensortype}"

        # Set device info based on entity type (names from device translations)
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
            site_id = None
            if self.coordinator._site_structure:
                site_id = self.coordinator._site_structure.siteId
            via_device = (DOMAIN, f"site_{site_id}") if site_id else None
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, inverter.serialNumber)},
                translation_key="inverter_device",
                translation_placeholders={"display_name": str(inverter.displayName)},
                via_device=via_device,
            )
        else:  # site
            site_id = panel.panel_id.split('_')[1] if '_' in panel.panel_id else ""
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, f"site_{site_id}")},
                translation_key="site_device",
                translation_placeholders={"site_id": site_id or "—"},
            )

        # Set appropriate units and device classes based on sensor type
        if self._sensor_type is SENSOR_TYPE_VOLTAGE:
            self._attr_native_unit_of_measurement = UnitOfElectricPotential.VOLT
            self._attr_device_class = SensorDeviceClass.VOLTAGE
            self._attr_state_class = SensorStateClass.MEASUREMENT
        elif self._sensor_type is SENSOR_TYPE_CURRENT:
            self._attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
            self._attr_device_class = SensorDeviceClass.CURRENT
            self._attr_state_class = SensorStateClass.MEASUREMENT
        elif self._sensor_type is SENSOR_TYPE_POWER:
            self._attr_native_unit_of_measurement = UnitOfPower.WATT
            self._attr_device_class = SensorDeviceClass.POWER
            self._attr_state_class = SensorStateClass.MEASUREMENT
        elif self._sensor_type is SENSOR_TYPE_ENERGY:
            self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
            self._attr_device_class = SensorDeviceClass.ENERGY
            self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        elif self._sensor_type is SENSOR_TYPE_LASTMEASUREMENT:
            self._attr_device_class = SensorDeviceClass.TIMESTAMP
            self._attr_state_class = None
        elif self._sensor_type is SENSOR_TYPE_CHILD_COUNT:
            # Number of child entities (optimizers for strings, strings for inverters)
            self._attr_state_class = SensorStateClass.MEASUREMENT

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

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if self.coordinator.data is not None:
            item = self.coordinator.data.get(self._panelobject.panel_id)
            if item is None and _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "SolarEdge Optimizers sensor: No data for panel_id=%s (%s) in coordinator",
                    self._panelobject.panel_id,
                    self._sensor_type,
                )
            if item and hasattr(item, 'entity_type'):
                # This is aggregated data, handle it directly
                # AJT: 27-Jan-2026: Use class-level constant instead of creating dict on every update
                attr_name = self._SENSOR_ATTR_MAP.get(self._sensor_type)
                if attr_name:
                    new_value = getattr(item, attr_name, 0)
                    # Inverter count, string count, optimizer count must be integers
                    if self._sensor_type is SENSOR_TYPE_CHILD_COUNT:
                        new_value = int(new_value) if new_value is not None else 0
                    # AJT: 27-Jan-2026: For lifetime energy, ensure value never decreases (rounding/precision issues)
                    elif self._sensor_type is SENSOR_TYPE_ENERGY:
                        # Round to 3 decimal places for consistency
                        # AJT: 27-Jan-2026: Only convert to float if needed (may already be float)
                        if new_value is not None:
                            new_value = round(float(new_value), 3) if not isinstance(new_value, float) else round(new_value, 3)
                        else:
                            new_value = 0.0
                        # Ensure value never decreases (use max of current and previous)
                        if self._attr_native_value is not None:
                            # AJT: 27-Jan-2026: Cache float conversion to avoid repeated calls
                            previous_value = float(self._attr_native_value) if not isinstance(self._attr_native_value, float) else self._attr_native_value
                            new_value = max(new_value, previous_value)
                    
                    self._attr_native_value = new_value

                # Handle string conversion for numeric values
                value = self._attr_native_value
                if isinstance(value, str) and "," in value:
                    try:
                        self._attr_native_value = float(value.replace(",", ""))
                    except ValueError:
                        _LOGGER.warning("Could not convert value '%s' to float for sensor %s", value, self._log_name)
                # Inverter/string/optimizer count must always be integer
                if self._sensor_type is SENSOR_TYPE_CHILD_COUNT and self._attr_native_value is not None:
                    self._attr_native_value = int(self._attr_native_value)

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

    # AJT: 27-Jan-2026: Class-level constant for sensor attribute mapping to avoid recreating on every update
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
        path_str = "_".join(map(str, entity_id_path)) if entity_id_path else ""
        slug = self._TRANSLATION_KEYS.get(
            sensortype, sensortype.lower().replace(" ", "_")
        )
        # Include prefix in unique_id so reconfigure (add/remove prefix) produces new entity_id instead of keeping old one
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
        self._optimizer_display_name = f"{site_id}.{'.'.join(map(str, entity_id_path[1:]))}" if len(entity_id_path) >= 4 else str(optimizer.displayName)

        # Force HA to use our full object_id (no device prefix like "optimizer_1_1_1_").
        # Always set when we have path so entity_id is sensor.[prefix]slug_path regardless of locale.
        if slug and path_str:
            self.internal_integration_suggested_object_id = f"{self._base_name}{slug}_{path_str}"

        # Set full device_info in _attr so HA's cached_property returns it; include
        # via_device so optimizer devices are grouped under the string device.
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

        if self._sensor_type is SENSOR_TYPE_VOLTAGE:
            self._attr_native_unit_of_measurement = UnitOfElectricPotential.VOLT
            self._attr_device_class = SensorDeviceClass.VOLTAGE
            self._attr_state_class = SensorStateClass.MEASUREMENT
        elif self._sensor_type is SENSOR_TYPE_CURRENT:
            self._attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
            self._attr_device_class = SensorDeviceClass.CURRENT
            self._attr_state_class = SensorStateClass.MEASUREMENT
        elif self._sensor_type is SENSOR_TYPE_OPT_VOLTAGE:
            self._attr_native_unit_of_measurement = UnitOfElectricPotential.VOLT
            self._attr_device_class = SensorDeviceClass.VOLTAGE
            self._attr_state_class = SensorStateClass.MEASUREMENT
        elif self._sensor_type is SENSOR_TYPE_POWER:
            self._attr_native_unit_of_measurement = UnitOfPower.WATT
            self._attr_device_class = SensorDeviceClass.POWER
            self._attr_state_class = SensorStateClass.MEASUREMENT
        elif self._sensor_type is SENSOR_TYPE_ENERGY:
            self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
            self._attr_device_class = SensorDeviceClass.ENERGY
            self._attr_state_class = SensorStateClass.TOTAL_INCREASING
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

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""

        if self.coordinator.data is not None:
            # Cache panel_id once for this update (used for lookup and optional debug)
            panel_id = self._panelobject.panel_id
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "Update the sensor %s - %s with the info from the coordinator",
                    panel_id,
                    self._sensor_type,
                )
            item = self.coordinator.data.get(panel_id)
            if item is not None:
                # AJT: 16-Jan-2026: Use pre-computed timetocheck from coordinator (calculated once per update)
                # Timestamp should be timezone-aware (converted in coordinator), but add safety check
                timetocheck = self.coordinator._timetocheck
                if timetocheck is None:
                    # AJT: 18-Jan-2026: Use datetime.now(timezone.utc) to ensure correct UTC time
                    timetocheck = datetime.now(timezone.utc) - CHECK_TIME_DELTA
                
                # AJT: 16-Jan-2026: Safety check - ensure timestamp is timezone-aware before comparison
                ts = item.lastmeasurement
                if isinstance(ts, datetime) and ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                    item.lastmeasurement = ts  # Update in place for future use
                
                measurement_too_old = ts <= timetocheck
                
                # AJT: 16-Jan-2026: Use dictionary mapping for sensor updates instead of long if/elif chain
                # AJT: 22-Jan-2026: Lifetime energy and last measurement always update regardless of age
                if self._sensor_type is SENSOR_TYPE_ENERGY:
                    # AJT: 27-Jan-2026: Round to 3 decimal places for consistency and ensure value never decreases
                    lifetime_energy = item.lifetime_energy
                    if lifetime_energy is not None:
                        # AJT: 27-Jan-2026: Only convert to float if needed (may already be float)
                        new_value = round(float(lifetime_energy), 3) if not isinstance(lifetime_energy, float) else round(lifetime_energy, 3)
                    else:
                        new_value = 0.0
                    
                    # AJT: 27-Jan-2026: Cache previous value conversion to avoid repeated float() calls
                    if self._attr_native_value is None:
                        self._attr_native_value = new_value
                    else:
                        prev_value = float(self._attr_native_value) if not isinstance(self._attr_native_value, float) else self._attr_native_value
                        if new_value >= prev_value:
                            self._attr_native_value = new_value
                        else:
                            # Value decreased (likely due to rounding), keep previous value
                            if _LOGGER.isEnabledFor(logging.DEBUG):
                                _LOGGER.debug(
                                    "Lifetime energy decreased for %s (new: %s, previous: %s), keeping previous value",
                                    self._log_name,
                                    new_value,
                                    self._attr_native_value
                                )
                elif self._sensor_type is SENSOR_TYPE_LASTMEASUREMENT:
                    self._attr_native_value = item.lastmeasurement
                else:
                    # AJT: 16-Jan-2026: Dictionary mapping for sensor type to attribute name
                    # AJT: 27-Jan-2026: Use class-level constant instead of creating dict on every update
                    attr_name = self._SENSOR_ATTR_MAP.get(self._sensor_type)
                    if attr_name:
                        # For other sensors: set to 0 if measurement is older than 1 hour, else use actual value
                        actual_value = getattr(item, attr_name, 0)
                        if measurement_too_old:
                            # AJT: 17-Jan-2026: Log when measurements are zeroed due to old timestamp
                            if actual_value != 0 and _LOGGER.isEnabledFor(logging.DEBUG):
                                _LOGGER.debug(
                                    "Sensor %s (%s) set to 0: measurement too old (last: %s, threshold: %s)",
                                    self._log_name,
                                    attr_name,
                                    ts,
                                    timetocheck
                                )
                            self._attr_native_value = 0
                        else:
                            # AJT: 17-Jan-2026: Log if actual value is 0 but measurement is recent (potential API issue)
                            if actual_value == 0 and _LOGGER.isEnabledFor(logging.DEBUG):
                                _LOGGER.debug(
                                    "Sensor %s (%s) has zero value but measurement is recent (last: %s). "
                                    "This may indicate missing data in API response.",
                                    self._log_name,
                                    attr_name,
                                    ts
                                )
                            self._attr_native_value = actual_value
        else:
            # AJT: 22-Jan-2026: Set the value to zero. (BUT NOT FOR LIFETIME ENERGY OR LAST MEASUREMENT)
            # AJT: 10-Jan-2025: Fixed comparison syntax from "not self._sensor_type is" to "self._sensor_type is not"
            if (self._sensor_type is not SENSOR_TYPE_ENERGY) and (
                self._sensor_type is not SENSOR_TYPE_LASTMEASUREMENT
            ):
                if _LOGGER.isEnabledFor(logging.DEBUG):
                    _LOGGER.debug(
                        "SolarEdge Optimizers sensor: Coordinator data is None, zeroing %s (%s)",
                        self._log_name,
                        self._sensor_type,
                    )
                self._attr_native_value = 0

        # AJT: 27-Jan-2026: Only process string conversion if value is actually a string
        value = self._attr_native_value
        if isinstance(value, str):
            # AJT: 27-Jan-2026: Check for comma before calling replace to avoid unnecessary string operation
            if "," in value:
                # AJT: 11-Jan-2026: Added error handling for float conversion
                try:
                    self._attr_native_value = float(value.replace(",", ""))
                except ValueError:
                    if _LOGGER.isEnabledFor(logging.WARNING):
                        _LOGGER.warning("Could not convert value '%s' to float for sensor %s", value, self._log_name)
                    # Keep original value

        self.async_write_ha_state()
