"""Example integration using DataUpdateCoordinator."""
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
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
from homeassistant.util import dt as dt_util
from datetime import datetime, timezone

from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
)

from .const import (
    DOMAIN,
    SENSOR_TYPE,
    SENSOR_TYPE_OPT_VOLTAGE,
    SENSOR_TYPE_CURRENT,
    SENSOR_TYPE_POWER,
    SENSOR_TYPE_VOLTAGE,
    SENSOR_TYPE_ENERGY,
    SENSOR_TYPE_LASTMEASUREMENT,
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
    SolarlEdgeOptimizer,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add an solarEdge entry."""
    # Add the needed sensors to hass
    coordinator: MyCoordinator = hass.data[DOMAIN][entry.entry_id]

    site = await hass.async_add_executor_job(coordinator.my_api.requestListOfAllPanels)

    _LOGGER.info("Found all information for site: %s", site.siteId)
    _LOGGER.info("Site has %s inverters", len(site.inverters))
    _LOGGER.info(
        "Adding all optimizers (%s) found to Home Assistant",
        site.returnNumberOfOptimizers(),
    )

    # AJT: 16-Jan-2026: Collect all optimizer/inverter pairs first for parallel processing
    optimizer_tasks = []
    for i, inverter in enumerate(site.inverters, start=1):
        _LOGGER.info("Adding all optimizers from inverter: %s", i)
        for string in inverter.strings:
            for optimizer in string.optimizers:
                optimizer_tasks.append((optimizer, inverter))

    # AJT: 16-Jan-2026: Parallelize API calls using asyncio.gather for 10-20x speedup
    _LOGGER.info("Fetching optimizer data in parallel...")
    results = await asyncio.gather(
        *[
            hass.async_add_executor_job(
                coordinator.my_api.requestSystemData, opt.optimizerId
            )
            for opt, _ in optimizer_tasks
        ],
        return_exceptions=True
    )

    # Process results and create sensors
    sensors_to_add = []
    for (optimizer, inverter), info in zip(optimizer_tasks, results):
        if isinstance(info, Exception):
            _LOGGER.error(
                "Error fetching data for optimizer %s: %s",
                optimizer.optimizerId,
                info
            )
            continue
        
        if info is not None:
            _LOGGER.debug(
                "Added optimizer for panel_id: %s to Home Assistant",
                optimizer.displayName,
            )
            for sensortype in SENSOR_TYPE:
                sensors_to_add.append(
                    SolarEdgeOptimizersSensor(
                        coordinator,
                        hass,
                        entry,
                        info,
                        sensortype,
                        optimizer,
                        inverter
                    )
                )

    # Add all sensors at once
    if sensors_to_add:
        async_add_entities(sensors_to_add, update_before_add=True)
        _LOGGER.info(
            "Done adding all optimizers. Added %s sensors in total.",
            len(sensors_to_add)
        )
    else:
        _LOGGER.warning("No sensors were created - check for errors above")


# class MyEntity(CoordinatorEntity, SensorEntity):
class SolarEdgeOptimizersSensor(CoordinatorEntity, SensorEntity):
    """An entity using CoordinatorEntity.

    The CoordinatorEntity class provides:
      should_poll
      async_update
      async_added_to_hass
      available

    """

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator,
        hass: HomeAssistant,
        entry: ConfigEntry,
        panel: SolarEdgeOptimizerData,
        sensortype,
        optimizer: SolarlEdgeOptimizer,
        inverter
    ) -> None:
        super().__init__(coordinator)
        self._hass = hass
        self._entry = entry
        self._panelobject = panel
        self._optimizerobject = optimizer
        self._inverter = inverter
        # AJT: 16-Jan-2026: Fixed spelling from "paneel" to "panel"
        self._panel = panel.panel_description
        # AJT: 16-Jan-2026: Use f-string instead of .format() for better performance
        self._attr_unique_id = f"{panel.serialnumber}_{sensortype}"
        self._sensor_type = sensortype
        # AJT: 15-Jan-2026: Make sensor names display-friendly by replacing underscores with spaces
        # Special-case last measurement to use lowercase 'm'
        if self._sensor_type is SENSOR_TYPE_LASTMEASUREMENT:
            display_type = "Last measurement"
        else:
            display_type = self._sensor_type.replace("_", " ")
        # AJT: 16-Jan-2026: Use f-string instead of .format() for better performance
        self._attr_name = f"{display_type} {optimizer.displayName}"

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}")},
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
            # AJT: 17-Jan-2026: Use TIMESTAMP instead of DATE to show both date and time
            self._attr_device_class = SensorDeviceClass.TIMESTAMP
            self._attr_state_class = None

    @property
    def device_info(self):
        return {
            "identifiers": {
                # Serial numbers are unique identifiers within a specific domain
                (DOMAIN, self._panelobject.serialnumber)
            },
            "name": self._optimizerobject.displayName,
            "manufacturer": self._panelobject.manufacturer,
            "model": self._panelobject.model,
            "hw_version": self._panelobject.serialnumber,
            "via_device": (DOMAIN, self._inverter.serialNumber),
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""

        if self.coordinator.data is not None:
            # AJT: 16-Jan-2026: Reduce debug logging overhead - only log if debug level is enabled
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "Update the sensor %s - %s with the info from the coordinator",
                    self._panelobject.panel_id,
                    self._sensor_type,
                )

            # AJT: 16-Jan-2026: Use dictionary lookup (O(1)) instead of linear search (O(n))
            item = self.coordinator.data.get(self._panelobject.panel_id)
            if item is not None:
                # AJT: 16-Jan-2026: Use pre-computed timetocheck from coordinator (calculated once per update)
                # Timestamp should be timezone-aware (converted in coordinator), but add safety check
                timetocheck = self.coordinator._timetocheck
                if timetocheck is None:
                    timetocheck = dt_util.utcnow() - CHECK_TIME_DELTA
                
                # AJT: 16-Jan-2026: Safety check - ensure timestamp is timezone-aware before comparison
                ts = item.lastmeasurement
                if isinstance(ts, datetime) and ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                    item.lastmeasurement = ts  # Update in place for future use
                
                measurement_too_old = ts <= timetocheck
                
                # AJT: 16-Jan-2026: Use dictionary mapping for sensor updates instead of long if/elif chain
                # Lifetime energy and last measurement always update regardless of age
                if self._sensor_type is SENSOR_TYPE_ENERGY:
                    # AJT: 10-Jan-2025: Removed redundant else clause that assigned self._attr_native_value = self._attr_native_value
                    if (
                        self._attr_native_value is None
                        or item.lifetime_energy >= self._attr_native_value
                    ):
                        self._attr_native_value = item.lifetime_energy
                elif self._sensor_type is SENSOR_TYPE_LASTMEASUREMENT:
                    self._attr_native_value = item.lastmeasurement
                else:
                    # AJT: 16-Jan-2026: Dictionary mapping for sensor type to attribute name
                    sensor_attr_map = {
                        SENSOR_TYPE_VOLTAGE: "voltage",
                        SENSOR_TYPE_CURRENT: "current",
                        SENSOR_TYPE_OPT_VOLTAGE: "optimizer_voltage",
                        SENSOR_TYPE_POWER: "power",
                    }
                    attr_name = sensor_attr_map.get(self._sensor_type)
                    if attr_name:
                        # For other sensors: set to 0 if measurement is older than 1 hour, else use actual value
                        actual_value = getattr(item, attr_name, 0)
                        if measurement_too_old:
                            # AJT: 17-Jan-2026: Log when measurements are zeroed due to old timestamp
                            if actual_value != 0 and _LOGGER.isEnabledFor(logging.DEBUG):
                                _LOGGER.debug(
                                    "Sensor %s (%s) set to 0: measurement too old (last: %s, threshold: %s)",
                                    self._attr_name,
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
                                    self._attr_name,
                                    attr_name,
                                    ts
                                )
                            self._attr_native_value = actual_value
        else:
            # Set the value to zero. (BUT NOT FOR LIFETIME ENERGY)
            # AJT: 10-Jan-2025: Fixed comparison syntax from "not self._sensor_type is" to "self._sensor_type is not"
            if (self._sensor_type is not SENSOR_TYPE_ENERGY) and (
                self._sensor_type is not SENSOR_TYPE_LASTMEASUREMENT
            ):
                self._attr_native_value = 0

        value = self._attr_native_value
        if isinstance(value, str) and "," in value:
            # AJT: 11-Jan-2026: Added error handling for float conversion
            try:
                self._attr_native_value = float(value.replace(",", ""))
            except ValueError:
                _LOGGER.warning("Could not convert value '%s' to float for sensor %s", value, self._attr_name)
                # Keep original value

        self.async_write_ha_state()
