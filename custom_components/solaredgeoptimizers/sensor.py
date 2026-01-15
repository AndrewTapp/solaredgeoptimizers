"""Example integration using DataUpdateCoordinator."""
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import DeviceInfo

import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)

from homeassistant.core import callback

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

    i = 1
    for inverter in site.inverters:
        _LOGGER.info("Adding all optimizers from inverter: %s", i)
        for string in inverter.strings:
            for optimizer in string.optimizers:
                _LOGGER.info(
                    "Added optimizer for panel_id: %s to Home Assistant",
                    optimizer.displayName,
                )

                # extra informatie ophalen
                info = await hass.async_add_executor_job(
                    coordinator.my_api.requestSystemData, optimizer.optimizerId
                )

                if info is not None:
                    for sensortype in SENSOR_TYPE:
                        async_add_entities(
                            [
                                SolarEdgeOptimizersSensor(
                                    coordinator,
                                    hass,
                                    entry,
                                    info,
                                    sensortype,
                                    optimizer,
                                    inverter
                                )
                            ],
                            update_before_add=True,
                        )

    _LOGGER.info(
        "Done adding all optimizers. Now adding sensors, this may take some time!"
    )


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
        paneel: SolarEdgeOptimizerData,
        sensortype,
        optimizer: SolarlEdgeOptimizer,
        inverter
    ) -> None:
        super().__init__(coordinator)
        self._hass = hass
        self._entry = entry
        self._paneelobject = paneel
        self._optimizerobject = optimizer
        self._inverter = inverter
        # AJT: 10-Jan-2025: Fixed typo "paneel_desciption" to "paneel_description" to match corrected attribute name
        self._paneel = paneel.paneel_description
        self._attr_unique_id = "{}_{}".format(paneel.serialnumber, sensortype)
        self._sensor_type = sensortype
        # AJT: 15-Jan-2026: Make sensor names display-friendly by replacing underscores with spaces
        # Special-case last measurement to use lowercase 'm'
        if self._sensor_type is SENSOR_TYPE_LASTMEASUREMENT:
            display_type = "Last measurement"
        else:
            display_type = self._sensor_type.replace("_", " ")
        self._attr_name = "{} {}".format(display_type, optimizer.displayName)

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
            self._attr_device_class = SensorDeviceClass.DATE
            self._attr_state_class = None

    @property
    def device_info(self):
        return {
            "identifiers": {
                # Serial numbers are unique identifiers within a specific domain
                (DOMAIN, self._paneelobject.serialnumber)
            },
            "name": self._optimizerobject.displayName,
            "manufacturer": self._paneelobject.manufacturer,
            "model": self._paneelobject.model,
            "hw_version": self._paneelobject.serialnumber,
            "via_device": (DOMAIN, self._inverter.serialNumber),
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""

        if self.coordinator.data is not None:

            _LOGGER.debug(
                "Update the sensor %s - %s with the info from the coordinator",
                self._paneelobject.paneel_id,
                self._sensor_type,
            )

            for item in self.coordinator.data:
                if item.paneel_id == self._paneelobject.paneel_id:
                    # weird first time after reboot value is None
                    # if self._attr_native_value is not None:
                    if self._sensor_type is SENSOR_TYPE_VOLTAGE:
                        self._attr_native_value = item.voltage
                        break
                    elif self._sensor_type is SENSOR_TYPE_CURRENT:
                        self._attr_native_value = item.current
                        break
                    elif self._sensor_type is SENSOR_TYPE_OPT_VOLTAGE:
                        self._attr_native_value = item.optimizer_voltage
                        break
                    elif self._sensor_type is SENSOR_TYPE_POWER:
                        self._attr_native_value = item.power
                        break
                    elif self._sensor_type is SENSOR_TYPE_ENERGY:
                        # AJT: 10-Jan-2025: Removed redundant else clause that assigned self._attr_native_value = self._attr_native_value
                        if (
                            self._attr_native_value is None
                            or item.lifetime_energy >= self._attr_native_value
                        ):
                            self._attr_native_value = item.lifetime_energy
                        break
                    elif self._sensor_type is SENSOR_TYPE_LASTMEASUREMENT:
                        self._attr_native_value = item.lastmeasurement
                        break
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
