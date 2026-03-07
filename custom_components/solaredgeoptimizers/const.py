"""
SolarEdge Optimizers Integration - Constants (const.py)

This module defines all constants and shared utility functions used throughout the integration:

Configuration Keys:
- DOMAIN: Integration identifier ("solaredgeoptimizers")
- CONF_SITE_ID, CONF_USE_SOLAREDGE_ONE, CONF_ENTITY_PREFIX, CONF_INCLUDE_SITE_ID_IN_ENTITY_ID

Timing Constants:
- UPDATE_DELAY: Coordinator polling interval (2 minutes)
- COORDINATOR_REFRESH_TIMEOUT_SEC: Maximum time for API refresh (30 minutes)
- CHECK_TIME_DELTA: Stale data threshold for legacy API (2 hours)
- CHECK_TIME_DELTA_SOLAREDGE_ONE: Stale data threshold for One API (1 hour)
- REVERT_TO_ONE_RETRY_INTERVAL: How often to retry One API when using legacy (30 minutes)
- LIGHT_CHECK_MIN_INTERVAL: Minimum interval between lightweight checks (2 minutes)

Sensor Type Definitions:
- SENSOR_TYPE_INDIVIDUAL: Sensors created for each optimizer
- SENSOR_TYPE_AGGREGATED_STRING/INVERTER/SITE: Sensors for aggregated levels
- SENSOR_TYPE_INACTIVE_OPTIMIZER_EXCLUDE: Sensors skipped for inactive optimizers
- SENSOR_TYPE_INACTIVE_AGGREGATED_EXCLUDE: Sensors skipped for inactive strings/inverters

Utility Functions:
- parse_string_display_name_path(): Extract (inv, str) from display names like "1.0"
- parse_optimizer_display_name_to_indices(): Extract (inv, str, opt) from display names
- resolve_duplicate_indices(): Assign letter suffixes to duplicate positions
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import timedelta
import logging

DOMAIN = "solaredgeoptimizers"
CONF_SITE_ID = "siteid"
CONF_USE_SOLAREDGE_ONE = "use_solaredge_one"  # If True, use SolarEdge One portal API (services/layout/...)
CONF_ENTITY_PREFIX = "entity_id_prefix"  # Optional prefix for entity_id (e.g. "se_" -> sensor.se_power_2065855)
# If True, entity IDs include site ID (e.g. power_2065855_1_1_1); default False
CONF_INCLUDE_SITE_ID_IN_ENTITY_ID = "include_site_id_in_entity_id"
DATA_API_CLIENT = "api_client"

PANEL_DATA = "panel_data"

LOGGER = logging.getLogger(__package__)

# Coordinator tick interval. Actual portal load is controlled by adaptive polling in the coordinator.
UPDATE_DELAY = timedelta(minutes=2)
# Max seconds for one coordinator refresh (initial and full refresh). Slow API or many optimizers may need >15 min.
COORDINATOR_REFRESH_TIMEOUT_SEC = 1800  # 30 min

CHECK_TIME_DELTA = timedelta(hours=2)  # Legacy API: treat live values as stale after 2 hours
CHECK_TIME_DELTA_SOLAREDGE_ONE = timedelta(hours=1)  # SolarEdge One: 1 hour stale threshold
# When data is from legacy API, re-try One this often so we revert to One when it becomes available
REVERT_TO_ONE_RETRY_INTERVAL = timedelta(minutes=30)
# Adaptive polling: min interval between light check and full refresh trigger (avoid thundering herd)
LIGHT_CHECK_MIN_INTERVAL = timedelta(minutes=2)
# Site lifetime: use portal total when aggregated optimizer data is below this (kWh)
RELIABLE_THRESHOLD_KWH = 100.0

# API request timeouts (seconds)
API_TIMEOUT_SHORT = 30  # For quick requests (login check, single optimizer)
API_TIMEOUT_LONG = 60   # For longer requests (layout, batch operations)

# Batch operation limits
LIGHT_CHECK_BATCH_SIZE = 5  # Number of optimizers to sample in lightweight checks
MAX_PARALLEL_WORKERS = 10   # Maximum threads for parallel API requests

# Common User-Agent string for API requests (Chrome on Windows)
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"

# Status sensor icons
ICON_STATUS_ACTIVE = "mdi:check-circle"
ICON_STATUS_INACTIVE = "mdi:alert-circle"
ICON_STATUS_UNKNOWN = "mdi:help-circle"

# Orientation sensor icons
ICON_AZIMUTH = "mdi:compass"
ICON_TILT = "mdi:angle-acute"

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
SENSOR_TYPE_STATUS = "Status"
SENSOR_TYPE_AZIMUTH = "Azimuth"
SENSOR_TYPE_TILT = "Tilt"

# Sensors for individual optimizers
SENSOR_TYPE_INDIVIDUAL = [
    SENSOR_TYPE_CURRENT,
    SENSOR_TYPE_OPT_VOLTAGE,
    SENSOR_TYPE_POWER,
    SENSOR_TYPE_VOLTAGE,
    SENSOR_TYPE_TEMPERATURE,
    SENSOR_TYPE_ENERGY,
    SENSOR_TYPE_LASTMEASUREMENT,
    SENSOR_TYPE_STATUS,
    SENSOR_TYPE_AZIMUTH,
    SENSOR_TYPE_TILT,
]

# Sensors to exclude for inactive optimizers (only create for active devices)
# These sensors are not meaningful for inactive/disconnected optimizers
SENSOR_TYPE_INACTIVE_OPTIMIZER_EXCLUDE = [
    SENSOR_TYPE_AZIMUTH,
    SENSOR_TYPE_CURRENT,
    SENSOR_TYPE_OPT_VOLTAGE,
    SENSOR_TYPE_POWER,
    SENSOR_TYPE_TEMPERATURE,
    SENSOR_TYPE_TILT,
    SENSOR_TYPE_VOLTAGE,
]

# Sensors to exclude for inactive strings/inverters (only create for active devices)
# Current (average), power, and voltage (average) are not meaningful for inactive devices
SENSOR_TYPE_INACTIVE_AGGREGATED_EXCLUDE = [
    SENSOR_TYPE_CURRENT,
    SENSOR_TYPE_POWER,
    SENSOR_TYPE_VOLTAGE,
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
    SENSOR_TYPE_STATUS,
]

SENSOR_TYPE_AGGREGATED_INVERTER = SENSOR_TYPE_AGGREGATED_COMMON + [
    SENSOR_TYPE_CHILD_COUNT,  # For inverters: string count
    SENSOR_TYPE_STATUS,
]

SENSOR_TYPE_AGGREGATED_SITE = SENSOR_TYPE_AGGREGATED_COMMON + [
    SENSOR_TYPE_CHILD_COUNT,  # For sites: inverter count
]

SENSOR_TYPE = SENSOR_TYPE_INDIVIDUAL  # For backwards compatibility


def parse_string_display_name_path(display_name: str) -> tuple[int, int] | None:
    """Parse string displayName (e.g. '1.0', '1.1', 'String 1.0') into (inv, str) for device/entity IDs.
    Returns None if not in expected format so callers can fall back to position-based indices."""
    if not display_name or not isinstance(display_name, str):
        return None
    raw = display_name.strip()
    if raw.upper().startswith("STRING "):
        raw = raw[7:].strip()
    parts = raw.split(".")
    if len(parts) != 2:
        return None
    try:
        nums = [int(p.strip()) for p in parts if p.strip().isdigit()]
    except (ValueError, TypeError):
        return None
    if len(nums) != 2:
        return None
    return (nums[0], nums[1])


def parse_optimizer_display_name_to_indices(display_name: str) -> tuple[int, int, int] | None:
    """Parse optimizer displayName (e.g. '1.0.1', 'Optimizer 1.0.1') into (inv, str, opt) for device/entity IDs.
    Returns None if not in expected format so callers can fall back to position-based indices."""
    if not display_name or not isinstance(display_name, str):
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
    if len(nums) == 3:
        return (nums[0], nums[1], nums[2])
    return (nums[1], nums[2], nums[3])  # site.inv.str.opt -> inv, str, opt


def make_duplicate_sort_key(item, get_status: Callable, get_serial: Callable) -> tuple[int, str]:
    """Create sort key for duplicate resolution: active first, then alphabetically by serial number.
    
    Used by resolve_duplicate_indices to sort items with the same position key.
    Active devices (status="ACTIVE") sort before inactive ones.
    """
    status = (get_status(item) or "").upper()
    is_active = 0 if status == "ACTIVE" else 1  # 0 sorts before 1
    serial = get_serial(item) or ""
    return (is_active, serial)


def resolve_duplicate_indices(items: list, get_key: Callable, get_status: Callable, get_serial: Callable, logger=None) -> dict[int, str]:
    """Resolve duplicate position keys by adding letter suffixes.
    
    Args:
        items: List of items to check for duplicates
        get_key: Function to extract the position key from an item
        get_status: Function to extract status from an item (for sorting)
        get_serial: Function to extract serial number from an item (for sorting)
        logger: Optional logger for debug output
    
    Returns:
        dict mapping item index -> suffix (empty string for first, 'a', 'b', etc. for duplicates)
    
    Active devices come first (sorted by serial), then alphabetically by serial.
    First item keeps original key, subsequent get 'a', 'b', etc. suffixes.
    """
    key_groups = defaultdict(list)
    for idx, item in enumerate(items):
        key = get_key(item)
        key_groups[key].append(idx)
    
    resolved = {}
    for key, indices in key_groups.items():
        if len(indices) == 1:
            resolved[indices[0]] = ""
            continue
        
        group_items = [(idx, items[idx]) for idx in indices]
        sorted_items = sorted(
            group_items,
            key=lambda x: make_duplicate_sort_key(x[1], get_status, get_serial)
        )
        
        if logger and logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "SolarEdge Optimizers: Resolving %d duplicate keys for '%s': %s",
                len(sorted_items),
                key,
                [(get_status(items[idx]) or "unknown", get_serial(items[idx]) or "unknown") for idx, _ in sorted_items],
            )
        
        suffix_idx = 0
        for i, (idx, _item) in enumerate(sorted_items):
            if i == 0:
                resolved[idx] = ""
            else:
                resolved[idx] = chr(ord('a') + suffix_idx)
                suffix_idx += 1
        
        if logger and logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "SolarEdge Optimizers: Resolved suffixes for '%s': %s",
                key,
                {idx: resolved[idx] or "(none)" for idx, _ in sorted_items},
            )
    
    return resolved
