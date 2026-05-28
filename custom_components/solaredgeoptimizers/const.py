"""
SolarEdge Optimizers Integration - Constants (const.py)

This module defines all constants and shared utility functions used throughout the integration.
Each constant is documented below with a full description of what it is used for.

=== CONFIGURATION KEYS (config flow, options, entry data) ===
- DOMAIN: Integration identifier for Home Assistant ("solaredgeoptimizers"); used in
  device/entity identifiers and platform registration.
- CONF_SITE_ID: Config key for SolarEdge site ID ("siteid"); used in setup and API calls.
- CONF_USE_SOLAREDGE_ONE: Config key to enable/disable SolarEdge One API ("use_solaredge_one").
  When True, integration uses One API with legacy fallback; when False, legacy only.
- CONF_ENTITY_PREFIX: Optional prefix for entity_id (e.g. "se_" -> sensor.se_power_...).
- CONF_INCLUDE_SITE_ID_IN_ENTITY_ID: When True, entity IDs include site ID for multi-site setups.
- DATA_API_CLIENT: Key used in hass.data for the API client (internal).
- PANEL_DATA: Key for panel data in coordinator (internal).

=== TIMING & POLLING ===
- UPDATE_DELAY: Coordinator tick interval (how often the coordinator runs its update logic).
  Actual portal load is controlled by adaptive polling (light check vs full refresh).
- COORDINATOR_REFRESH_TIMEOUT_SEC: Maximum seconds for one coordinator refresh (initial or full).
  Slow API or many optimizers may need more than 15 min; 30 min avoids premature timeout.
- CHECK_TIME_DELTA: Legacy API stale threshold. Live values older than this are treated as stale.
- CHECK_TIME_DELTA_SOLAREDGE_ONE: SolarEdge One API stale threshold (shorter than legacy).
- REVERT_TO_ONE_RETRY_INTERVAL: When data is from legacy API, how often to re-try One API
  so we revert to One when it becomes available.
- LIGHT_CHECK_MIN_INTERVAL: Minimum interval between lightweight check and next full refresh
  trigger; avoids thundering herd when many optimizers update at once (adaptive polling).
- LIGHT_CHECK_DESIRED_INTERVAL_FRESH: When data is fresh (within stale delta), desired
  interval between lightweight checks (e.g. 5 min).
- LIGHT_CHECK_DESIRED_INTERVAL_STALE: When data is stale (or age unknown), desired
  interval between lightweight checks (e.g. 30 min).

=== CACHE TTLs (SolarEdge One API) ===
- PANELS_CACHE_TTL_ONE: How long to cache site structure (layout) from One API before
  refetching; reduces portal calls when coordinator repeatedly needs panel list.
- SITE_INFO_CACHE_TTL: How long to cache site information (installation date, peak power)
  from layout/information/site; same duration as layout (e.g. 2 h).
- LIFETIME_ENERGY_CACHE_TTL: How long to cache lifetime energy data (One and legacy);
  changes slowly so 1 hour is typical.
- STRING_LIFETIME_PORTAL_OVERRIDE_MAX_RATIO: Skip string-level portal lifetime override when
  by-inverter string Wh exceeds this multiple of the optimizer-sum aggregate (bad portal buckets).
- TEMPERATURE_CACHE_TTL: How long to cache optimizer maximum daily temperatures from One API (e.g. 30 min).

=== CACHE TTLs (Legacy API) ===
- PANELS_CACHE_TTL_LEGACY: How long to cache site structure from legacy API (e.g. 2 h).

=== API REQUEST SETTINGS ===
- API_TIMEOUT_SHORT: Timeout in seconds for quick requests (login check, single optimizer).
- API_TIMEOUT_LONG: Timeout for longer requests (layout, batch operations).
- LIGHT_CHECK_BATCH_SIZE: Number of optimizers to sample in One API lightweight checks;
  sampling several (e.g. 5) from different strings improves detection of new data.
- ENTITY_ADD_BATCH_SIZE: Number of sensor entities registered per startup batch in the sensor
  platform (with event-loop yields between batches on large sites).
- MAX_PARALLEL_WORKERS: Maximum threads for parallel API requests (legacy per-optimizer, etc.).
- USER_AGENT: Common User-Agent string for API requests (Chrome on Windows).

=== SOLAREDGE ONE API (URLs & OAuth) ===
- BASE_URL_MONITORING: Base URL for SolarEdge monitoring portal (One API services).
- LOGIN_BASE: Base URL for SolarEdge login (OAuth).
- SOLAREDGE_ONE_CLIENT_ID: OAuth client_id for SolarEdge One (from portal redirect).
- MFE_AUTH_PATH, MFE_AUTH_CALLBACK_PATH, TOKEN_PATH: Path segments for OAuth (combined with bases).

=== API SOURCE LABELS ===
- OBTAINED_FROM_ONE: Human-readable label when data came from SolarEdge One API.
- OBTAINED_FROM_LEGACY: Human-readable label when data came from legacy Monitoring API.

=== LOCALE-DEPENDENT MEASUREMENT KEYS (Legacy API) ===
- MEASUREMENT_KEYS: Map of measurement type -> list of locale-specific key strings returned
  by legacy API (e.g. "Power [W]", "Leistung [W]") so power/current/voltage work in any language.

=== SENSOR ICONS & STATUS DISPLAY ===
- ICON_STATUS_ACTIVE / INACTIVE / UNKNOWN: Icons for status sensors (check, alert, question mark).
- STATUS_DISPLAY_BLANK: Display value "blank" when API status is empty (treated as active).
- is_status_active(status): True if status is blank or "ACTIVE" (for aggregation and sorting).
- status_display_value(raw): "blank" | "Active" | "Inactive" | raw (for sensor state).
- status_icon(display_value): active icon for blank/Active, inactive for Inactive, unknown for other.
- ICON_AZIMUTH / ICON_TILT: Icons for orientation sensors.

=== SENSOR TYPE STRINGS & LISTS ===
- SENSOR_TYPE_*: Sensor type identifiers and lists (individual, aggregated, exclude lists).
  SENSOR_TYPE_INSTALLATION_DATE, SENSOR_TYPE_PEAK_POWER: site-only (from portal layout/information/site).
  SENSOR_TYPE_MAX_ACTIVE_POWER: inverter-only (from portal layout logical v2 maxActivePower, displayed in kW).
  See in-code comments for which entities get which sensor types.

Utility Functions:
- parse_string_display_name_path(): Extract (inv, str, suffix) from string display names (e.g. 1.0a)
- parse_optimizer_display_name_to_indices(): Extract (inv, str, opt, suffix) from optimizer names
  (e.g. 1.1.1a -> suffix a for replaced/duplicate panels)
- string_position_key_from_display_name(): (inv, str) key for string duplicate resolution
- build_optimizer_tasks(): Optimizer task list for sensor setup and coordinator position indexing
- resolve_duplicate_indices(): Assign letter suffixes to duplicate positions
- ENTITY_ADD_BATCH_SIZE: Startup batch size for async_add_entities (default 50)
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
UPDATE_DELAY = timedelta(minutes=5)
# Max seconds for one coordinator refresh (initial and full refresh). Slow API or many optimizers may need >15 min.
COORDINATOR_REFRESH_TIMEOUT_SEC = 1800  # 30 min

CHECK_TIME_DELTA = timedelta(hours=2)  # Legacy API: treat live values as stale after 2 hours
CHECK_TIME_DELTA_SOLAREDGE_ONE = timedelta(hours=1)  # SolarEdge One: 1 hour stale threshold
# When data is from legacy API, re-try One this often so we revert to One when it becomes available
REVERT_TO_ONE_RETRY_INTERVAL = timedelta(minutes=30)
# Adaptive polling: min interval between light check and full refresh trigger (avoid thundering herd)
LIGHT_CHECK_MIN_INTERVAL = timedelta(minutes=5)
# Desired interval between lightweight checks when data is fresh (within stale delta)
LIGHT_CHECK_DESIRED_INTERVAL_FRESH = timedelta(minutes=5)
# Desired interval between lightweight checks when data is stale or age unknown
LIGHT_CHECK_DESIRED_INTERVAL_STALE = timedelta(minutes=30)

# --- Cache TTLs: SolarEdge One API ---
PANELS_CACHE_TTL_ONE = timedelta(hours=2)       # Site structure (layout) cache
SITE_INFO_CACHE_TTL = timedelta(hours=2)        # Site information (installation date, peak power) - same as layout
LIFETIME_ENERGY_CACHE_TTL = timedelta(hours=1)  # Lifetime energy cache (One and legacy)
# String-level portal override (layout/energy by-inverter): skip when portal Wh is this many times
# larger than the optimizer-sum aggregate. Some sites return a wrong per-string bucket (e.g. near
# inverter-total-minus-other-strings); optimizers stay authoritative in that case.
STRING_LIFETIME_PORTAL_OVERRIDE_MAX_RATIO = 3.0
TEMPERATURE_CACHE_TTL = timedelta(minutes=30)   # Optimizer temperatures cache (One API)
# --- Cache TTLs: Legacy API ---
PANELS_CACHE_TTL_LEGACY = timedelta(hours=2)    # Site structure cache for legacy API

# API request timeouts (seconds)
API_TIMEOUT_SHORT = 30  # For quick requests (login check, single optimizer)
API_TIMEOUT_LONG = 60   # For longer requests (layout, batch operations)

# Batch operation limits
LIGHT_CHECK_BATCH_SIZE = 5  # Number of optimizers to sample in lightweight checks
ENTITY_ADD_BATCH_SIZE = 50  # Number of entities to register per startup batch
MAX_PARALLEL_WORKERS = 10   # Maximum threads for parallel API requests

# Common User-Agent string for API requests (Chrome on Windows)
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"

# --- SolarEdge One API: base URLs and OAuth ---
BASE_URL_MONITORING = "https://monitoring.solaredge.com"
LOGIN_BASE = "https://login.solaredge.com"
SOLAREDGE_ONE_CLIENT_ID = "ugfnsujd3384sshcjehaphlh3"
MFE_AUTH_PATH = "/mfe/auth/"
MFE_AUTH_CALLBACK_PATH = "/mfe/auth/callback"
TOKEN_PATH = "/oauth2/token"

# --- API source labels (for "Obtained from" sensor and logging) ---
OBTAINED_FROM_ONE = "One API"
OBTAINED_FROM_LEGACY = "Legacy API"

# --- Legacy API: locale-dependent measurement keys (power/current/voltage/optimizer_voltage) ---
# SolarEdge returns keys in user locale (e.g. "Power [W]" in EN, "Leistung [W]" in DE).
MEASUREMENT_KEYS = {
    "power": [
        "Power [W]", "Leistung [W]", "Puissance [W]", "Potencia [W]", "Potenza [W]",
        "Vermogen [W]", "Effekt [W]", "Moc [W]", "Výkon [W]", "Teljesítmény [W]",
        "Ισχύς [W]", "Güç [W]", "Мощность [W]", "功率 [W]", "電力 [W]", "Teho [W]",
    ],
    "current": [
        "Current [A]", "Strom [A]", "Courant [A]", "Corriente [A]", "Corrente [A]",
        "Stroom [A]", "Strøm [A]", "Ström [A]", "Prąd [A]", "Proud [A]", "Áram [A]",
        "Ρεύμα [A]", "Akım [A]", "Ток [A]", "电流 [A]", "電流 [A]", "Virta [A]",
    ],
    "voltage": [
        "Voltage [V]", "Spannung [V]", "Tension [V]", "Tensión [V]", "Tensione [V]",
        "Spanning [V]", "Spänning [V]", "Spænding [V]", "Spenning [V]", "Napięcie [V]",
        "Napětí [V]", "Feszültség [V]", "Τάση [V]", "Gerilim [V]", "Напряжение [V]",
        "电压 [V]", "電圧 [V]", "Jännite [V]",
    ],
    "optimizer_voltage": [
        "Optimizer Voltage [V]", "Optimierer-Spannung [V]", "Optimizer-Spannung [V]",
    ],
}

# Status sensor icons (active=check, inactive=alert, unknown=question mark)
ICON_STATUS_ACTIVE = "mdi:check-circle"
ICON_STATUS_INACTIVE = "mdi:alert-circle"
ICON_STATUS_UNKNOWN = "mdi:help-circle"

# Display value when status is blank (treated as active)
STATUS_DISPLAY_BLANK = "blank"


def is_status_active(status: str | None) -> bool:
    """Return True if the device should be treated as active: blank/empty or 'ACTIVE' (case-insensitive)."""
    s = (status or "").strip()
    return s == "" or s.upper() == "ACTIVE"


def status_display_value(raw_status: str | None) -> str:
    """Convert raw API status to display value: blank→'blank', ACTIVE→'Active', INACTIVE→'Inactive', else as-is."""
    s = (raw_status or "").strip()
    if s == "":
        return STATUS_DISPLAY_BLANK
    u = s.upper()
    if u == "ACTIVE":
        return "Active"
    if u == "INACTIVE":
        return "Inactive"
    return s


def status_icon(display_value: str | None) -> str:
    """Return icon for status sensor from display value: blank/Active→active icon, Inactive→inactive, else unknown."""
    v = (display_value or "").strip()
    if v == STATUS_DISPLAY_BLANK or v == "Active":
        return ICON_STATUS_ACTIVE
    if v == "Inactive":
        return ICON_STATUS_INACTIVE
    return ICON_STATUS_UNKNOWN

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
SENSOR_TYPE_INSTALLATION_DATE = "Installation_date"
SENSOR_TYPE_PEAK_POWER = "Peak_power"
SENSOR_TYPE_MAX_ACTIVE_POWER = "Max_active_power"

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
    SENSOR_TYPE_MAX_ACTIVE_POWER,  # Inverter max active power (kW) from layout logical v2
]

SENSOR_TYPE_AGGREGATED_SITE = SENSOR_TYPE_AGGREGATED_COMMON + [
    SENSOR_TYPE_CHILD_COUNT,  # For sites: inverter count
    SENSOR_TYPE_INSTALLATION_DATE,  # Site installation date from portal layout/information/site
    SENSOR_TYPE_PEAK_POWER,  # Site peak power (kW) from portal layout/information/site
]

SENSOR_TYPE = SENSOR_TYPE_INDIVIDUAL  # For backwards compatibility


def parse_string_display_name_path(display_name: str) -> tuple[int, int, str] | None:
    """Parse string displayName into (inv, str, suffix) for device/entity IDs.

    Examples: '1.0' -> (1, 0, ''); '1.0a' / 'String 1.0a' -> (1, 0, 'a').
    Letter suffix applies only to the string index (second segment).
    """
    if not display_name or not isinstance(display_name, str):
        return None
    raw = display_name.strip()
    if raw.upper().startswith("STRING "):
        raw = raw[7:].strip()
    parts = [p.strip() for p in raw.split(".") if p.strip()]
    if len(parts) != 2:
        return None
    parsed = [_parse_index_segment(p) for p in parts]
    if any(seg is None for seg in parsed):
        return None
    (inv, inv_suffix), (str_num, str_suffix) = parsed
    if inv_suffix:
        return None
    return inv, str_num, str_suffix


def string_position_key_from_display_name(
    display_name: str, inv_idx: int, str_idx: int
) -> tuple[int, int]:
    """Position key (inv, str) for duplicate resolution; ignores letter suffix on string index."""
    parsed = parse_string_display_name_path(display_name or "")
    if parsed is not None:
        return (parsed[0], parsed[1])
    return (inv_idx, str_idx)


def _parse_index_segment(segment: str) -> tuple[int, str] | None:
    """Parse one dotted segment into (index, letter_suffix). Suffix only on the last segment is used."""
    if not segment or not isinstance(segment, str):
        return None
    part = segment.strip()
    digit_len = 0
    while digit_len < len(part) and part[digit_len].isdigit():
        digit_len += 1
    if digit_len == 0:
        return None
    suffix = part[digit_len:].lower()
    if suffix and not suffix.isalpha():
        return None
    return int(part[:digit_len]), suffix


def parse_optimizer_display_name_to_indices(
    display_name: str,
) -> tuple[int, int, int, str] | None:
    """Parse optimizer displayName into (inv, str, opt, suffix) for device/entity IDs.

    Examples: '1.0.1' -> (1, 0, 1, ''); '1.1.1a' / 'Optimizer 1.1.1a' -> (1, 1, 1, 'a');
    'site.1.0.1b' (4 segments) -> (1, 0, 1, 'b'). Letter suffix applies only to the optimizer
    index (last segment). Returns None if not parseable so callers can use enumerate indices.
    """
    if not display_name or not isinstance(display_name, str):
        return None
    raw = display_name.strip()
    if raw.upper().startswith("OPTIMIZER "):
        raw = raw[10:].strip()
    parts = [p.strip() for p in raw.split(".") if p.strip()]
    if len(parts) not in (3, 4):
        return None
    parsed = [_parse_index_segment(p) for p in parts]
    if any(seg is None for seg in parsed):
        return None
    if len(parsed) == 3:
        (inv, inv_suffix), (str_num, str_suffix), (opt, opt_suffix) = parsed
        if inv_suffix or str_suffix:
            return None
        return inv, str_num, opt, opt_suffix
    (_site, _site_suffix), (inv, inv_suffix), (str_num, str_suffix), (opt, opt_suffix) = parsed
    if inv_suffix or str_suffix:
        return None
    return inv, str_num, opt, opt_suffix


def make_duplicate_sort_key(item, get_status: Callable, get_serial: Callable) -> tuple[int, str]:
    """Create sort key for duplicate resolution: active first, then alphabetically by serial number.
    
    Used by resolve_duplicate_indices to sort items with the same position key.
    Active devices (blank or status='ACTIVE') sort before inactive ones.
    """
    status = get_status(item) or ""
    is_active = 0 if is_status_active(status) else 1  # 0 sorts before 1
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


def build_optimizer_tasks(site) -> list:
    """Build optimizer task tuples for sensor setup and coordinator position indexing.

    Each task is (optimizer, inverter, string, inv_num, str_num, opt_num, suffix).
    Uses display-name-based (inv, str, opt) when optimizer.displayName parses (e.g. '1.0.1',
    '1.1.1a'). Suffix comes from the display name when present, else duplicate positions get
    letter suffixes (a, b, c...) from resolve_duplicate_indices.
    """
    logger = logging.getLogger(__name__)
    tasks_with_keys = []
    for inv_idx, inverter in enumerate(site.inverters, start=1):
        for str_idx, string in enumerate(inverter.strings, start=1):
            for opt_idx, optimizer in enumerate(string.optimizers, start=1):
                parsed = parse_optimizer_display_name_to_indices(
                    getattr(optimizer, "displayName", "") or ""
                )
                if parsed is not None:
                    inv_num, str_num, opt_num, display_suffix = parsed
                else:
                    inv_num, str_num, opt_num = inv_idx, str_idx, opt_idx
                    display_suffix = ""
                position_key = (inv_num, str_num, opt_num)
                tasks_with_keys.append(
                    (
                        optimizer,
                        inverter,
                        string,
                        inv_num,
                        str_num,
                        opt_num,
                        position_key,
                        display_suffix,
                    )
                )

    suffix_map = resolve_duplicate_indices(
        tasks_with_keys,
        get_key=lambda item: item[6],
        get_status=lambda item: getattr(item[0], "status", "") or "",
        get_serial=lambda item: getattr(item[0], "serialNumber", "") or "",
        logger=logger,
    )

    tasks = []
    duplicates_count = sum(1 for suffix in suffix_map.values() if suffix)
    for idx, item in enumerate(tasks_with_keys):
        optimizer, inverter, string, inv_num, str_num, opt_num, _key, display_suffix = item
        suffix = display_suffix or suffix_map.get(idx, "")
        tasks.append((optimizer, inverter, string, inv_num, str_num, opt_num, suffix))
    if duplicates_count > 0:
        logger.info(
            "SolarEdge Optimizers: Found %d duplicate optimizer positions, assigned suffixes (a, b, c...)",
            duplicates_count,
        )
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "SolarEdge Optimizers: Built %d optimizer tasks (%d with duplicate-assigned suffixes)",
            len(tasks),
            duplicates_count,
        )
    return tasks
