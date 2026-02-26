"""Constants and configuration for SolarEdge Optimizers Home Assistant integration."""
from datetime import timedelta
import logging

DOMAIN = "solaredgeoptimizers"
CONF_SITE_ID = "siteid"
CONF_USE_SOLAREDGE_ONE = "use_solaredge_one"  # If True, use SolarEdge One portal API (services/layout/...)
CONF_ENTITY_PREFIX = "entity_id_prefix"  # Optional prefix for entity_id (e.g. "se_" -> sensor.se_power_2065855)
CONF_INCLUDE_SITE_ID_IN_ENTITY_ID = "include_site_id_in_entity_id"  # If True, entity IDs include site ID (e.g. power_2065855_1_1_1); default False
DATA_API_CLIENT = "api_client"

PANEL_DATA = "panel_data"

LOGGER = logging.getLogger(__package__)

# Coordinator tick interval. Actual portal load is controlled by adaptive polling in the coordinator.
UPDATE_DELAY = timedelta(minutes=2)

CHECK_TIME_DELTA = timedelta(hours=2)  # Legacy API: treat live values as stale after 2 hours
CHECK_TIME_DELTA_SOLAREDGE_ONE = timedelta(hours=1)  # SolarEdge One: 1 hour stale threshold
# When data is from legacy API, re-try One this often so we revert to One when it becomes available
REVERT_TO_ONE_RETRY_INTERVAL = timedelta(minutes=30)
# Adaptive polling: min interval between light check and full refresh trigger (avoid thundering herd)
LIGHT_CHECK_MIN_INTERVAL = timedelta(minutes=2)
# Site lifetime: use portal total when aggregated optimizer data is below this (kWh)
RELIABLE_THRESHOLD_KWH = 100.0

SENSOR_TYPE_CURRENT = "Current"
SENSOR_TYPE_OPT_VOLTAGE = "Optimizer_voltage"
SENSOR_TYPE_POWER = "Power"
SENSOR_TYPE_VOLTAGE = "Voltage"
SENSOR_TYPE_ENERGY = "Lifetime_energy"
SENSOR_TYPE_LASTMEASUREMENT = "Last_Measurement"
SENSOR_TYPE_LASTPOLLED = "Last_Polled"
SENSOR_TYPE_CHILD_COUNT = "Child_count"
SENSOR_TYPE_ACTIVE_CHILD_COUNT = "Active_child_count"
SENSOR_TYPE_TEMPERATURE = "Temperature"

# Sensors for individual optimizers
SENSOR_TYPE_INDIVIDUAL = [
    SENSOR_TYPE_CURRENT,
    SENSOR_TYPE_OPT_VOLTAGE,
    SENSOR_TYPE_POWER,
    SENSOR_TYPE_VOLTAGE,
    SENSOR_TYPE_TEMPERATURE,
    SENSOR_TYPE_ENERGY,
    SENSOR_TYPE_LASTMEASUREMENT,
]

# Sensors for aggregated entities (strings and inverters)
SENSOR_TYPE_AGGREGATED_COMMON = [
    SENSOR_TYPE_CURRENT,
    SENSOR_TYPE_POWER,
    SENSOR_TYPE_VOLTAGE,
    SENSOR_TYPE_ENERGY,
    SENSOR_TYPE_LASTMEASUREMENT,
]

SENSOR_TYPE_AGGREGATED_STRING = SENSOR_TYPE_AGGREGATED_COMMON + [
    SENSOR_TYPE_CHILD_COUNT,  # For strings: optimizer count
]

SENSOR_TYPE_AGGREGATED_INVERTER = SENSOR_TYPE_AGGREGATED_COMMON + [
    SENSOR_TYPE_CHILD_COUNT,  # For inverters: string count
]

SENSOR_TYPE_AGGREGATED_SITE = SENSOR_TYPE_AGGREGATED_COMMON + [
    SENSOR_TYPE_CHILD_COUNT,  # For sites: inverter count
]

SENSOR_TYPE = SENSOR_TYPE_INDIVIDUAL  # For backwards compatibility
