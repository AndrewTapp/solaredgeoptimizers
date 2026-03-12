"""
SolarEdge Optimizers Integration - Data Coordinator (coordinator.py)

This module implements the DataUpdateCoordinator that manages all data fetching and caching
for the integration. It serves as the central data hub for all sensor entities.

Key Responsibilities:
- Fetches site structure (inverters, strings, optimizers) on startup
- Polls optimizer data at regular intervals (coordinator tick every 5 minutes)
- Implements adaptive polling: lightweight checks detect new data before full refresh
- Manages One API vs Legacy API fallback (retries One periodically when using legacy)
- Calculates aggregated data (power, voltage, current, energy) at string/inverter/site levels
- Handles hardware swaps by using position-based keys instead of serial numbers
- Registers devices in Home Assistant device registry (site, inverters, strings)

Adaptive Polling Strategy:
- Lightweight check: samples 5 random optimizers (batch, One API) or 1 representative (single, legacy)
- Full refresh: One API uses one batch POST for all optimizers; legacy uses parallel per-optimizer requests
- Full refresh triggered when lightweight check detects newer lastmeasurement
- Stale threshold: 1 hour (One API) or 2 hours (Legacy API)
- Minimum interval between full refreshes: 5 minutes (LIGHT_CHECK_MIN_INTERVAL)

Data Aggregation:
- Power, current, voltage, and lifetime energy aggregate from all devices (any status) with recent data
- Child counts (optimizer/string/inverter count) count only active devices (status blank or "Active")
- Lifetime energy uses portal total when aggregated data is unreliable (<100 kWh)

Duplicate Handling:
- Resolves duplicate positions (same display name) with letter suffixes (a, b, c...)
- Active devices sort first, then alphabetically by serial number
"""
from __future__ import annotations

import asyncio
import logging
import random
from collections import namedtuple
from datetime import datetime, timedelta, timezone

import requests

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .api import SolarEdgeAPIProtocol
from .const import (
    DOMAIN,
    UPDATE_DELAY,
    CHECK_TIME_DELTA,
    CHECK_TIME_DELTA_SOLAREDGE_ONE,
    CONF_INCLUDE_SITE_ID_IN_ENTITY_ID,
    COORDINATOR_REFRESH_TIMEOUT_SEC,
    REVERT_TO_ONE_RETRY_INTERVAL,
    RELIABLE_THRESHOLD_KWH,
    LIGHT_CHECK_MIN_INTERVAL,
    LIGHT_CHECK_BATCH_SIZE,
    LIGHT_CHECK_DESIRED_INTERVAL_FRESH,
    LIGHT_CHECK_DESIRED_INTERVAL_STALE,
    OBTAINED_FROM_LEGACY,
    OBTAINED_FROM_ONE,
    is_status_active,
    parse_string_display_name_path,
    resolve_duplicate_indices,
)
from .solaredgeoptimizers import (
    SolarEdgeAggregatedData,
    _lifetime_energy_to_kwh,
    _site_lifetime_kwh_from_layout_energy,
)

_LOGGER = logging.getLogger(__name__)


def _get_all_optimizer_ids(site) -> list:
    """Return list of all optimizer IDs from site structure (inverters -> strings -> optimizers)."""
    return [
        opt.optimizerId
        for inv in site.inverters
        for s in inv.strings
        for opt in getattr(s, "optimizers") or ()
    ]


def _get_first_optimizer_id(site):
    """Return the first optimizer ID found in site structure, or None."""
    for inv in site.inverters:
        for s in inv.strings:
            if getattr(s, "optimizers", None) and s.optimizers:
                return s.optimizers[0].optimizerId
    return None


# Rollup state types to avoid passing many parameters (CodeFactor: too many arguments)
# inverter_count = inverters with data (for average divisor); active_inverters = for child_count only
SiteRollupState = namedtuple(
    "SiteRollupState",
    [
        "current",
        "power",
        "voltage_sum",
        "voltage_count",
        "last_measurement",
        "active_optimizers",
        "active_strings",
        "active_inverters",
        "inverter_count",
        "lifetime_energy",
    ],
)
InverterRollupResult = namedtuple(
    "InverterRollupResult",
    [
        "aggregated",
        "power",
        "active_strings",
        "string_count",
        "voltage_count",
        "last_measurement",
        "active_optimizers",
    ],
)
# Context for aggregation to reduce parameter count
AggregationContext = namedtuple(
    "AggregationContext",
    [
        "data_dict",
        "timetocheck",
        "lifetime_energy_lookup",
        "current_utc",
        "site_id_str",
        "include_site_id_in_entity_id",
    ],
)
# Inverter aggregation data to reduce parameter count in _create_inverter_aggregated
# string_count = strings with data (for average divisor); active_strings = for child_count only
InverterAggData = namedtuple(
    "InverterAggData",
    [
        "current",
        "power",
        "voltage_sum",
        "voltage_count",
        "last_measurement",
        "lifetime_energy",
        "active_optimizers",
        "active_strings",
        "string_count",
    ],
)
# String aggregation data to reduce parameter count in _create_string_aggregated
# optimizer_count = optimizers with recent data (all statuses) for average divisor; active_optimizers = for child_count only
StringAggData = namedtuple(
    "StringAggData",
    [
        "current",
        "power",
        "voltage_sum",
        "voltage_count",
        "last_measurement",
        "active_optimizers",
        "optimizer_count",
        "lifetime_energy",
    ],
)


class MyCoordinator(DataUpdateCoordinator):
    """Coordinator for SolarEdge optimizer data and aggregation."""

    def __init__(
        self,
        hass: HomeAssistant,
        my_api: SolarEdgeAPIProtocol,
        first_boot: bool,
        config_entry: ConfigEntry | None = None,
    ) -> None:
        """Initialize coordinator. config_entry enables async_config_entry_first_refresh."""
        super().__init__(
            hass,
            _LOGGER,
            # Name of the data. For logging purposes.
            name="SolarEdgeOptimizer",
            # Polling interval.
            update_interval=UPDATE_DELAY,
            config_entry=config_entry,
        )
        self.my_api = my_api
        self.first_boot = first_boot
        # Pre-compute timetocheck once per update cycle for all sensors
        self._timetocheck = None
        # Track when the integration last completed an update
        self._integration_last_polled = None
        # Store site structure for aggregated calculations
        self._site_structure = None
        # Adaptive polling state
        self._last_full_fetch_utc = None
        self._last_light_check_utc = None
        self._representative_optimizer_id = None
        # When API supports batch (e.g. SolarEdge One), sample several optimizers from different
        # strings so at least one is likely to have new data regardless of sun/orientation.
        self._light_check_optimizer_ids = None
        # Whether to include site ID in entity_id_path (for entity IDs). Options override data; default False when key missing.
        _include = False
        if config_entry:
            _include = config_entry.options.get(
                CONF_INCLUDE_SITE_ID_IN_ENTITY_ID,
                config_entry.data.get(CONF_INCLUDE_SITE_ID_IN_ENTITY_ID, False),
            )
        self._include_site_id_in_entity = bool(_include)
        # SolarEdge One API: inverter serial -> fullModel (e.g. SE5000H-RW000BNN4) for device model
        self._inverter_models = {}
        # Which API provided current data ("One API" or "Legacy API"); set after full refresh
        self._obtained_from = OBTAINED_FROM_ONE
        # Cache batch-API capability to avoid repeated getattr in update loop
        self._has_batch_api = getattr(my_api, "requestSystemDataBatch", None) is not None
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "SolarEdge Optimizers coordinator: include_site_id_in_entity_id=%s (from options/data)",
                self._include_site_id_in_entity,
            )

    def _pick_light_check_optimizers(self, site) -> None:
        """Set _light_check_optimizer_ids (batch API) or _representative_optimizer_id for lightweight checks."""
        if self._representative_optimizer_id is not None or self._light_check_optimizer_ids is not None:
            return
        if self._has_batch_api:
            all_ids = _get_all_optimizer_ids(site)
            ids = random.sample(all_ids, min(LIGHT_CHECK_BATCH_SIZE, len(all_ids))) if all_ids else []
            if ids:
                self._light_check_optimizer_ids = ids
                if _LOGGER.isEnabledFor(logging.DEBUG):
                    _LOGGER.debug(
                        "SolarEdge Optimizers: Using %d representative optimizers for lightweight checks (batch): %s",
                        len(ids), ids,
                    )
        if not self._light_check_optimizer_ids:
            self._representative_optimizer_id = _get_first_optimizer_id(site)
            if _LOGGER.isEnabledFor(logging.DEBUG) and self._representative_optimizer_id:
                _LOGGER.debug(
                    "SolarEdge Optimizers: Using representative optimizer %s for lightweight checks",
                    self._representative_optimizer_id,
                )

    async def _fetch_inverter_models(self, site) -> None:
        """Fetch inverter models from API if supported; set self._inverter_models (with error handling)."""
        if getattr(self.my_api, "get_inverter_models", None) is None:
            return
        inv_serials = [inv.serialNumber for inv in site.inverters if getattr(inv, "serialNumber", None)]
        if not inv_serials:
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("SolarEdge Optimizers: No inverter serials to fetch models for")
            return
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "SolarEdge Optimizers: Fetching inverter models for %d inverter(s): %s",
                len(inv_serials), inv_serials,
            )
        try:
            self._inverter_models = await self.hass.async_add_executor_job(
                self.my_api.get_inverter_models, inv_serials
            ) or {}
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("SolarEdge Optimizers: Inverter models received: %s", self._inverter_models)
        except Exception as e:  # pylint: disable=broad-except
            _LOGGER.warning("SolarEdge Optimizers: Could not fetch inverter models: %s", e)

    def _build_lifetime_energy_lookup(self, lifetime_energy_data):
        """Build stringId -> energy_data lookup; derive string total from optimizer sum when needed."""
        lifetime_energy_lookup = {}
        for inv in self._site_structure.inverters:
            for s in inv.strings:
                key = str(s.stringId)
                if key in lifetime_energy_data:
                    lifetime_energy_lookup[s.stringId] = lifetime_energy_data[key]
                else:
                    total_wh = 0.0
                    for opt in s.optimizers:
                        ent = lifetime_energy_data.get(str(opt.optimizerId)) or lifetime_energy_data.get(opt.optimizerId)
                        if ent and isinstance(ent.get("unscaledEnergy"), (int, float)):
                            total_wh += float(ent["unscaledEnergy"])
                    if total_wh > 0:
                        lifetime_energy_lookup[s.stringId] = {"unscaledEnergy": total_wh}
        return lifetime_energy_lookup

    def _aggregate_optimizers_in_string(self, string, data_dict, timetocheck):
        """Aggregate optimizer data for one string.
        
        Returns (current, power, voltage_sum, voltage_count, last_measurement, active_optimizers, optimizer_count).
        Power, current, voltage, and last_measurement include ALL optimizers with recent data (all statuses).
        active_optimizers is the count of active (blank or ACTIVE) optimizers, used for child_count only.
        optimizer_count is the count of optimizers with recent data, used for average divisor.
        """
        string_current = 0.0
        string_power = 0.0
        string_voltage_sum = 0.0
        string_voltage_count = 0
        string_last_measurement = None
        string_active_optimizers = 0
        string_optimizer_count = 0
        for optimizer in string.optimizers:
            optimizer_status_raw = getattr(optimizer, "status", "") or ""
            if is_status_active(optimizer_status_raw):
                string_active_optimizers += 1
            optimizer_data = data_dict.get(optimizer.optimizerId)
            if optimizer_data:
                optimizer_data.status = optimizer_status_raw
                if _LOGGER.isEnabledFor(logging.DEBUG):
                    _LOGGER.debug(
                        "SolarEdge Optimizers: Set optimizer %s status=%s",
                        optimizer.optimizerId, optimizer_data.status or "(none)",
                    )
                last_measurement = optimizer_data.lastmeasurement
                is_datetime = isinstance(last_measurement, datetime)
                if is_datetime and (string_last_measurement is None or last_measurement > string_last_measurement):
                    string_last_measurement = last_measurement
                if is_datetime and last_measurement > timetocheck:
                    opt_current = optimizer_data.current
                    opt_power = optimizer_data.power
                    opt_voltage = optimizer_data.voltage
                    if opt_current is not None:
                        string_current += opt_current
                    if opt_power is not None:
                        string_power += opt_power
                    if opt_voltage:
                        string_voltage_sum += opt_voltage
                        string_voltage_count += 1
                    string_optimizer_count += 1
        return (
            string_current,
            string_power,
            string_voltage_sum,
            string_voltage_count,
            string_last_measurement,
            string_active_optimizers,
            string_optimizer_count,
        )

    def _create_string_aggregated(self, string, agg_data: StringAggData, current_utc, string_entity_path):
        """Build SolarEdgeAggregatedData for a string.
        
        agg_data: StringAggData namedtuple with aggregated values.
        string_entity_path: display-name-based (e.g. (1, 0)) or position-based (inv_idx, str_idx).
        """
        string_aggregated = SolarEdgeAggregatedData(
            entity_id=f"string_{string.stringId}",
            entity_type="string",
            lifetime_energy=agg_data.lifetime_energy,
            entity_id_path=string_entity_path,
        )
        if agg_data.optimizer_count > 0:
            string_aggregated.current = agg_data.current / agg_data.optimizer_count
            string_aggregated.power = round(agg_data.power, 2)
        else:
            string_aggregated.current = 0.0
            string_aggregated.power = 0.0
        string_aggregated.voltage = (
            round(agg_data.voltage_sum / agg_data.voltage_count, 2) if agg_data.voltage_count > 0 else 0.0
        )
        string_aggregated.lastmeasurement = agg_data.last_measurement if agg_data.last_measurement is not None else current_utc
        # Child count = only active (and blank-status) optimizers in this string
        string_aggregated.child_count = int(agg_data.active_optimizers)
        string_aggregated.active_optimizer_count = agg_data.active_optimizers
        string_aggregated.serialnumber = f"String_{string.stringId}"
        string_aggregated.panel_description = string.displayName
        string_aggregated.status = getattr(string, "status", "") or ""
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "SolarEdge Optimizers: Created string aggregated %s status=%s child_count=%d (active optimizers) active=%d",
                string_aggregated.panel_id, string_aggregated.status or "(none)",
                string_aggregated.child_count, string_aggregated.active_optimizer_count,
            )
        return string_aggregated

    def _create_inverter_aggregated(self, inverter, inv_idx, inv_data, ctx):
        """Build SolarEdgeAggregatedData for an inverter.
        
        Args:
            inverter: The inverter object from site structure
            inv_idx: Inverter index string (e.g. "1" or "1a" for duplicates)
            inv_data: InverterRollupResult with aggregated values
            ctx: AggregationContext with shared parameters
        """
        inverter_entity_path = (ctx.site_id_str, inv_idx) if ctx.include_site_id_in_entity_id else (inv_idx,)
        inverter_aggregated = SolarEdgeAggregatedData(
            entity_id=f"inverter_{inverter.inverterId}",
            entity_type="inverter",
            lifetime_energy=round(inv_data.lifetime_energy, 3),
            entity_id_path=inverter_entity_path,
        )
        if inv_data.string_count > 0:
            inverter_aggregated.current = inv_data.current / inv_data.string_count
            inverter_aggregated.power = round(inv_data.power, 2)
        else:
            inverter_aggregated.current = 0.0
            inverter_aggregated.power = 0.0
        inverter_aggregated.voltage = round((inv_data.voltage_sum / inv_data.voltage_count), 2) if inv_data.voltage_count > 0 else 0.0
        inverter_aggregated.lastmeasurement = inv_data.last_measurement if inv_data.last_measurement is not None else ctx.current_utc
        # Child count = only active (and blank-status) strings in this inverter
        inverter_aggregated.child_count = int(inv_data.active_strings)
        inverter_aggregated.active_optimizer_count = inv_data.active_optimizers
        inverter_aggregated.serialnumber = inverter.serialNumber or f"Inverter_{inverter.inverterId}"
        inverter_aggregated.panel_description = inverter.displayName
        inverter_aggregated.status = getattr(inverter, "status", "") or ""
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "SolarEdge Optimizers: Created inverter aggregated %s status=%s child_count=%d (active strings) active_optimizers=%d",
                inverter_aggregated.panel_id, inverter_aggregated.status or "(none)",
                inverter_aggregated.child_count, inverter_aggregated.active_optimizer_count,
            )
        return inverter_aggregated

    def _create_site_aggregated(self, site_id, site_state: SiteRollupState, current_utc):
        """Build SolarEdgeAggregatedData for the site.
        
        Args:
            site_id: The site ID
            site_state: SiteRollupState with aggregated values
            current_utc: Current UTC datetime for fallback lastmeasurement
        """
        site_id_str = str(site_id)
        site_entity_path = (site_id_str,)
        site_aggregated = SolarEdgeAggregatedData(
            entity_id=f"site_{site_id}",
            entity_type="site",
            lifetime_energy=round(site_state.lifetime_energy, 3),
            entity_id_path=site_entity_path,
        )
        if site_state.inverter_count > 0:
            site_aggregated.current = site_state.current / site_state.inverter_count
            site_aggregated.power = round(site_state.power, 2)
        else:
            site_aggregated.current = 0.0
            site_aggregated.power = 0.0
        site_aggregated.voltage = round((site_state.voltage_sum / site_state.voltage_count), 2) if site_state.voltage_count > 0 else 0.0
        site_aggregated.lastmeasurement = site_state.last_measurement if site_state.last_measurement is not None else current_utc
        # Child count = only active (and blank-status) inverters at site
        site_aggregated.child_count = int(site_state.active_inverters)
        site_aggregated.active_optimizer_count = site_state.active_optimizers
        site_aggregated.serialnumber = f"Site_{site_id}"
        site_aggregated.panel_description = f"Site {site_id}"
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "SolarEdge Optimizers: Created site aggregated %s child_count=%d (active inverters) active_optimizers=%d",
                site_aggregated.panel_id, site_aggregated.child_count, site_aggregated.active_optimizer_count,
            )
        return site_aggregated

    def _register_inverter_and_string_devices(
        self, device_registry, site_id: str, inverter, inv_idx: int, inv_suffix: str = ""
    ) -> None:
        """Create device registry entries for one inverter and its strings.
        
        inv_suffix is empty for first inverter at position, 'a', 'b', etc. for duplicates.
        """
        inv_idx_str = f"{inv_idx}{inv_suffix}"
        _LOGGER.info("Adding all optimizers from inverter: %s", inv_idx_str)
        inverter_name = f"Inverter {site_id}.{inv_idx_str}"
        inv_model = self._inverter_models.get(inverter.serialNumber) if self._inverter_models else None
        model = (inv_model or f"{inverter.type} {inverter.displayName}").strip() or inverter.serialNumber
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "SolarEdge Optimizers: Creating inverter device serial=%s model=%r (from_api=%s) suffix=%s",
                inverter.serialNumber, model, inv_model is not None, inv_suffix or "(none)",
            )
        inv_device_id = f"{self.config_entry.entry_id}_inv_{inv_idx_str}"
        device_registry.async_get_or_create(
            config_entry_id=self.config_entry.entry_id,
            identifiers={(DOMAIN, inv_device_id)},
            manufacturer="SolarEdge",
            model=model,
            name=inverter_name,
            hw_version=inverter.serialNumber,
            via_device=(DOMAIN, f"site_{self._site_structure.siteId}"),
        )
        
        # Resolve duplicate strings within this inverter
        string_suffix_map = resolve_duplicate_indices(
            inverter.strings,
            get_key=lambda s: parse_string_display_name_path(getattr(s, "displayName", "") or "") or (inv_idx, len([x for x in inverter.strings if x == s])),
            get_status=lambda s: getattr(s, "status", "") or "",
            get_serial=lambda s: getattr(s, "serialNumber", "") or str(getattr(s, "stringId", "")),
            logger=_LOGGER,
        )
        
        # Log duplicate string count for this inverter
        str_duplicates = sum(1 for suffix in string_suffix_map.values() if suffix)
        if str_duplicates > 0 and _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "SolarEdge Optimizers: Found %d duplicate string positions in inverter %s, assigned suffixes",
                str_duplicates, inv_idx_str,
            )
        
        for str_idx, string in enumerate(inverter.strings, start=1):
            str_suffix = string_suffix_map.get(str_idx - 1, "")  # 0-indexed in suffix_map
            parsed = parse_string_display_name_path(getattr(string, "displayName", "") or "")
            if parsed is not None:
                inv_num, str_num = parsed
                str_num_str = f"{str_num}{str_suffix}"
                string_name = f"String {site_id}.{inv_num}.{str_num_str}"
                str_device_id = f"{self.config_entry.entry_id}_str_{inv_num}_{str_num_str}"
            else:
                str_idx_str = f"{str_idx}{str_suffix}"
                string_name = f"String {site_id}.{inv_idx_str}.{str_idx_str}"
                str_device_id = f"{self.config_entry.entry_id}_str_{inv_idx_str}_{str_idx_str}"
            device_registry.async_get_or_create(
                config_entry_id=self.config_entry.entry_id,
                identifiers={(DOMAIN, str_device_id)},
                manufacturer="SolarEdge",
                model=f"STRING {string.displayName}",
                name=string_name,
                via_device=(DOMAIN, inv_device_id),
            )
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("Created device for string: %s (suffix=%s)", string_name, str_suffix or "(none)")

    def ensure_devices_registered(self) -> None:
        """Ensure site, inverter, and string devices exist in the device registry.

        Call from the sensor platform before adding entities so via_device references
        resolve (avoids 'references a non existing via_device' when setup order differs).
        """
        if self._site_structure is not None:
            self._register_site_and_inverter_devices(self._site_structure)

    def _register_site_and_inverter_devices(self, site) -> None:
        """Create device registry entries for site, inverters, and strings.
        
        No deduplication - all inverters and strings are shown. When duplicates exist
        (same position), active devices come first (sorted by serial), and subsequent
        duplicates get letter suffixes (a, b, c...).
        """
        device_registry = dr.async_get(self.hass)
        site_id = str(site.siteId)
        
        if _LOGGER.isEnabledFor(logging.DEBUG):
            total_strings = sum(len(inv.strings) for inv in site.inverters)
            _LOGGER.debug(
                "SolarEdge Optimizers: Registering devices for site %s: %d inverters, %d strings",
                site_id, len(site.inverters), total_strings,
            )
        
        device_registry.async_get_or_create(
            config_entry_id=self.config_entry.entry_id,
            identifiers={(DOMAIN, f"site_{site.siteId}")},
            manufacturer="SolarEdge",
            model=f"SITE {site.siteId}",
            name=f"Site {site_id}",
        )
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("SolarEdge Optimizers: Created device for site: %s", site_id)
        
        # Resolve duplicate inverters across the site
        inv_suffix_map = resolve_duplicate_indices(
            site.inverters,
            get_key=lambda inv: getattr(inv, "displayName", "") or str(getattr(inv, "inverterId", "")),
            get_status=lambda inv: getattr(inv, "status", "") or "",
            get_serial=lambda inv: getattr(inv, "serialNumber", "") or "",
            logger=_LOGGER,
        )
        
        # Log duplicate inverter count
        inv_duplicates = sum(1 for suffix in inv_suffix_map.values() if suffix)
        if inv_duplicates > 0:
            _LOGGER.info(
                "SolarEdge Optimizers: Found %d duplicate inverter positions for device registration, assigned suffixes",
                inv_duplicates,
            )
        
        for inv_idx, inverter in enumerate(site.inverters, start=1):
            inv_suffix = inv_suffix_map.get(inv_idx - 1, "")  # 0-indexed in suffix_map
            self._register_inverter_and_string_devices(
                device_registry, site_id, inverter, inv_idx, inv_suffix
            )

    async def _async_setup(self) -> None:
        """Set up the coordinator.

        Can be overwritten by integrations to load data or resources
        only once during the first refresh.
        """
        _LOGGER.info("SolarEdge Optimizers: Starting coordinator setup")
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("SolarEdge Optimizers: About to request list of all panels")
        try:
            site = await self.hass.async_add_executor_job(self.my_api.requestListOfAllPanels)
            self._site_structure = site
            self._pick_light_check_optimizers(site)
            _LOGGER.info("SolarEdge Optimizers: Successfully retrieved panel list")
            _LOGGER.info("Found all information for site: %s", site.siteId)
            _LOGGER.info("Site has %s inverters", len(site.inverters))
            _LOGGER.info(
                "Setting up Home Assistant devices and sensors for %s optimizers across %s inverters",
                site.returnNumberOfOptimizers(),
                len(site.inverters),
            )
            await self._fetch_inverter_models(site)
        except Exception as e:  # pylint: disable=broad-except
            _LOGGER.error("SolarEdge Optimizers: Failed to get panel list in coordinator setup: %s", e)
            raise
        self._register_site_and_inverter_devices(site)

    def _build_string_entity_path(self, string, str_suffix, str_idx, inv_idx_str, ctx):
        """Build entity path for a string based on display name or position."""
        parsed = parse_string_display_name_path(getattr(string, "displayName", "") or "")
        if parsed is not None:
            inv_num, str_num = parsed
            str_num_str = f"{str_num}{str_suffix}"
            return (ctx.site_id_str, inv_num, str_num_str) if ctx.include_site_id_in_entity_id else (inv_num, str_num_str)
        str_idx_str = f"{str_idx}{str_suffix}"
        return (ctx.site_id_str, inv_idx_str, str_idx_str) if ctx.include_site_id_in_entity_id else (inv_idx_str, str_idx_str)

    def _get_string_lifetime_energy(self, string, ctx):
        """Get lifetime energy for a string from the lookup cache."""
        energy_data = ctx.lifetime_energy_lookup.get(string.stringId)
        if energy_data:
            kWh = _lifetime_energy_to_kwh(energy_data)
            if kWh is not None:
                return round(kWh, 3)
        return 0.0

    def _process_single_string(self, string, str_suffix, str_idx, inv_idx_str, ctx):
        """Process a single string: aggregate optimizers and create aggregated data.
        
        Returns (string_aggregated, string_is_active, agg_data: StringAggData).
        """
        (
            string_current,
            string_power,
            string_voltage_sum,
            string_voltage_count,
            string_last_measurement,
            string_active_optimizers,
            string_optimizer_count,
        ) = self._aggregate_optimizers_in_string(string, ctx.data_dict, ctx.timetocheck)

        string_lifetime_energy = self._get_string_lifetime_energy(string, ctx)
        string_status_raw = getattr(string, "status", "") or ""
        string_is_active = is_status_active(string_status_raw)

        agg_data = StringAggData(
            current=string_current,
            power=string_power,
            voltage_sum=string_voltage_sum,
            voltage_count=string_voltage_count,
            last_measurement=string_last_measurement,
            active_optimizers=string_active_optimizers,
            optimizer_count=string_optimizer_count,
            lifetime_energy=string_lifetime_energy,
        )
        string_entity_path = self._build_string_entity_path(string, str_suffix, str_idx, inv_idx_str, ctx)
        string_aggregated = self._create_string_aggregated(string, agg_data, ctx.current_utc, string_entity_path)
        ctx.data_dict[string_aggregated.panel_id] = string_aggregated

        return (string_aggregated, string_is_active, agg_data)

    def _accumulate_string_into_inverter(self, inv_state, string_aggregated, agg_data: StringAggData, string_is_active: bool):
        """Accumulate a string's values into inverter totals (all strings included for power/current/voltage/lifetime).
        
        inv_state is a dict with current inverter accumulation values.
        string_is_active: if True, this string counts toward active_strings (child_count).
        """
        if agg_data.optimizer_count > 0:
            inv_state["current"] += string_aggregated.current
            inv_state["power"] += agg_data.power
            if agg_data.voltage_count > 0:
                inv_state["voltage_sum"] += string_aggregated.voltage
                inv_state["voltage_count"] += 1
            inv_state["string_count"] += 1
        if string_is_active:
            inv_state["active_strings"] += 1
            inv_state["active_optimizers"] += agg_data.active_optimizers
        inv_state["lifetime_energy"] = round(inv_state["lifetime_energy"] + agg_data.lifetime_energy, 3)
        if inv_state["last_measurement"] is None or (
            agg_data.last_measurement and agg_data.last_measurement > inv_state["last_measurement"]
        ):
            inv_state["last_measurement"] = agg_data.last_measurement

    def _process_inverter_strings(self, inverter, inv_idx, inv_suffix, ctx):
        """Process all strings for one inverter; write string aggregates into ctx.data_dict.

        Returns (inverter_current, inverter_power, inverter_voltage_sum, inverter_voltage_count,
                 inverter_last_measurement, inverter_active_optimizers, inverter_active_strings,
                 inverter_string_count, inverter_lifetime_energy).
        
        inv_suffix is empty for first inverter at position, 'a', 'b', etc. for duplicates.
        ctx is an AggregationContext namedtuple with shared aggregation parameters.
        
        Power, current, voltage, and lifetime include ALL strings; child_count uses active_strings only.
        """
        inv_state = {
            "current": 0.0, "power": 0.0, "voltage_sum": 0.0, "voltage_count": 0,
            "last_measurement": None, "active_optimizers": 0, "active_strings": 0, "string_count": 0,
            "lifetime_energy": 0.0,
        }
        inv_idx_str = f"{inv_idx}{inv_suffix}"
        
        str_suffix_map = resolve_duplicate_indices(
            inverter.strings,
            get_key=lambda s: parse_string_display_name_path(getattr(s, "displayName", "") or "") or (inv_idx, 0),
            get_status=lambda s: getattr(s, "status", "") or "",
            get_serial=lambda s: getattr(s, "serialNumber", "") or str(getattr(s, "stringId", "")),
            logger=_LOGGER,
        )
        
        str_duplicates = sum(1 for suffix in str_suffix_map.values() if suffix)
        if str_duplicates > 0 and _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "SolarEdge Optimizers: Found %d duplicate string positions in inverter %s for aggregation, assigned suffixes",
                str_duplicates, inv_idx_str,
            )

        for str_idx, string in enumerate(inverter.strings, start=1):
            str_suffix = str_suffix_map.get(str_idx - 1, "")
            string_aggregated, string_is_active, agg_data = self._process_single_string(
                string, str_suffix, str_idx, inv_idx_str, ctx
            )
            self._accumulate_string_into_inverter(inv_state, string_aggregated, agg_data, string_is_active)

        return (
            inv_state["current"], inv_state["power"], inv_state["voltage_sum"], inv_state["voltage_count"],
            inv_state["last_measurement"], inv_state["active_optimizers"], inv_state["active_strings"],
            inv_state["string_count"], inv_state["lifetime_energy"],
        )

    def _merge_inverter_into_site_rollup(
        self, site: SiteRollupState, inv_result: InverterRollupResult, inverter_is_active: bool
    ) -> SiteRollupState:
        """Update site rollup state with one inverter's aggregated data (all inverters included).
        inverter_is_active: if True, this inverter counts toward active_inverters (child_count).
        """
        lifetime_energy = round(site.lifetime_energy + inv_result.aggregated.lifetime_energy, 3)
        active_optimizers = site.active_optimizers + (inv_result.active_optimizers if inverter_is_active else 0)
        active_strings = site.active_strings + (inv_result.active_strings if inverter_is_active else 0)
        last_measurement = site.last_measurement
        if last_measurement is None or (
            inv_result.last_measurement and inv_result.last_measurement > last_measurement
        ):
            last_measurement = inv_result.last_measurement

        current = site.current
        power = site.power
        voltage_sum = site.voltage_sum
        voltage_count = site.voltage_count
        active_inverters = site.active_inverters + (1 if inverter_is_active else 0)
        inverter_count = site.inverter_count
        if inv_result.string_count > 0:
            current += inv_result.aggregated.current
            power += inv_result.power
            inverter_count += 1
            if inv_result.voltage_count > 0:
                voltage_sum += inv_result.aggregated.voltage
                voltage_count += 1

        return SiteRollupState(
            current=current,
            power=power,
            voltage_sum=voltage_sum,
            voltage_count=voltage_count,
            last_measurement=last_measurement,
            active_optimizers=active_optimizers,
            active_strings=active_strings,
            active_inverters=active_inverters,
            inverter_count=inverter_count,
            lifetime_energy=lifetime_energy,
        )

    def _process_single_inverter(self, inverter, inv_idx, inv_suffix, ctx, site):
        """Process a single inverter and update site rollup state.
        
        Returns updated SiteRollupState.
        """
        inv_idx_str = f"{inv_idx}{inv_suffix}"
        (
            inverter_current, inverter_power, inverter_voltage_sum, inverter_voltage_count,
            inverter_last_measurement, inverter_active_optimizers, inverter_active_strings,
            inverter_string_count, inverter_lifetime_energy,
        ) = self._process_inverter_strings(inverter, inv_idx, inv_suffix, ctx)

        inv_data = InverterAggData(
            current=inverter_current, power=inverter_power, voltage_sum=inverter_voltage_sum,
            voltage_count=inverter_voltage_count, last_measurement=inverter_last_measurement,
            lifetime_energy=inverter_lifetime_energy, active_optimizers=inverter_active_optimizers,
            active_strings=inverter_active_strings, string_count=inverter_string_count,
        )
        inverter_aggregated = self._create_inverter_aggregated(inverter, inv_idx_str, inv_data, ctx)
        ctx.data_dict[inverter_aggregated.panel_id] = inverter_aggregated

        inverter_status_raw = getattr(inverter, "status", "") or ""
        inverter_is_active = is_status_active(inverter_status_raw)
        
        inv_result = InverterRollupResult(
            aggregated=inverter_aggregated, power=inverter_power, active_strings=inverter_active_strings,
            string_count=inverter_string_count, voltage_count=inverter_voltage_count,
            last_measurement=inverter_last_measurement, active_optimizers=inverter_active_optimizers,
        )
        return self._merge_inverter_into_site_rollup(site, inv_result, inverter_is_active)

    def _resolve_inverter_duplicates(self):
        """Resolve duplicate inverters and return suffix map."""
        inv_suffix_map = resolve_duplicate_indices(
            self._site_structure.inverters,
            get_key=lambda inv: getattr(inv, "displayName", "") or str(getattr(inv, "inverterId", "")),
            get_status=lambda inv: getattr(inv, "status", "") or "",
            get_serial=lambda inv: getattr(inv, "serialNumber", "") or "",
            logger=_LOGGER,
        )
        inv_duplicates = sum(1 for suffix in inv_suffix_map.values() if suffix)
        if inv_duplicates > 0 and _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "SolarEdge Optimizers: Found %d duplicate inverter positions for aggregation, assigned suffixes",
                inv_duplicates,
            )
        return inv_suffix_map

    def _calculate_aggregated_data(
        self,
        data_dict,
        current_utc,
        timetocheck,
        lifetime_energy_data,
        site_id,
        portal_site_lifetime_kwh=None,
        include_site_id_in_entity_id=False,
    ):
        """Calculate aggregated data at site, inverter, and string levels.

        Site lifetime energy uses aggregated optimizer data when reliable. When
        aggregated is unreliable (e.g. very small while portal has a real total),
        site-level uses the portal total (sum of unscaledEnergy from layout/energy).
        include_site_id_in_entity_id: when False, entity_id_path omits site_id (shorter entity IDs).
        
        No deduplication - all inverters and strings are shown. When duplicates exist
        (same position), active devices come first (sorted by serial), and subsequent
        duplicates get letter suffixes (a, b, c...).
        
        Power, current, voltage, and lifetime energy aggregate ALL devices (all statuses).
        Child counts (optimizer count, string count, inverter count) count only active (blank or ACTIVE) devices.
        """
        lifetime_energy_lookup = self._build_lifetime_energy_lookup(lifetime_energy_data)
        site_id_str = str(site_id)
        site = SiteRollupState(
            current=0.0, power=0.0, voltage_sum=0.0, voltage_count=0,
            last_measurement=None, active_optimizers=0, active_strings=0,
            active_inverters=0, inverter_count=0, lifetime_energy=0.0,
        )
        
        inv_suffix_map = self._resolve_inverter_duplicates()
        ctx = AggregationContext(
            data_dict=data_dict, timetocheck=timetocheck, lifetime_energy_lookup=lifetime_energy_lookup,
            current_utc=current_utc, site_id_str=site_id_str, include_site_id_in_entity_id=include_site_id_in_entity_id,
        )

        for inv_idx, inverter in enumerate(self._site_structure.inverters, start=1):
            inv_suffix = inv_suffix_map.get(inv_idx - 1, "")
            site = self._process_single_inverter(inverter, inv_idx, inv_suffix, ctx, site)

        if (
            portal_site_lifetime_kwh is not None
            and portal_site_lifetime_kwh >= RELIABLE_THRESHOLD_KWH
            and site.lifetime_energy < RELIABLE_THRESHOLD_KWH
        ):
            site = site._replace(lifetime_energy=portal_site_lifetime_kwh)

        site_aggregated = self._create_site_aggregated(site_id, site, current_utc)
        data_dict[site_aggregated.panel_id] = site_aggregated

    def _decide_initial_full_refresh(self, is_data_dict, current_data) -> bool:
        """True if we must do a full refresh (first boot or no existing data)."""
        return self.first_boot or not is_data_dict or not current_data

    def _should_retry_revert_to_one(self, do_full_refresh, is_data_dict, current_data, now_utc) -> bool:
        """True when data is from legacy API and we should re-try SolarEdge One."""
        if do_full_refresh or not is_data_dict or not current_data:
            return False
        if getattr(self, "_obtained_from", None) != OBTAINED_FROM_LEGACY:
            return False
        if self._last_full_fetch_utc is None:
            return False
        return (now_utc - self._last_full_fetch_utc) >= REVERT_TO_ONE_RETRY_INTERVAL

    def _get_latest_measurement_from_data(self, current_data):
        """Return latest lastmeasurement datetime from current_data, or None."""
        if not current_data:
            return None
        site_id = self._site_structure.siteId if self._site_structure else None
        site_key = f"site_{site_id}" if site_id else None
        latest_measurement = None
        if site_key and site_key in current_data:
            latest_measurement = getattr(current_data[site_key], "lastmeasurement", None)
        if not isinstance(latest_measurement, datetime):
            for v in current_data.values():
                lm = getattr(v, "lastmeasurement", None)
                if isinstance(lm, datetime) and (latest_measurement is None or lm > latest_measurement):
                    latest_measurement = lm
        return latest_measurement

    def _get_stale_delta(self) -> timedelta:
        """Stale threshold: SolarEdge One 1 h, legacy 2 h."""
        return CHECK_TIME_DELTA_SOLAREDGE_ONE if self._has_batch_api else CHECK_TIME_DELTA

    def _should_do_light_check(
        self, do_full_refresh, now_utc, latest_measurement, stale_delta
    ) -> bool:
        """True if we should run a lightweight check this tick."""
        if do_full_refresh:
            return False
        if self._representative_optimizer_id is None and not self._light_check_optimizer_ids:
            return False
        measurement_age = (now_utc - latest_measurement) if isinstance(latest_measurement, datetime) else None
        desired_interval = (
            LIGHT_CHECK_DESIRED_INTERVAL_STALE
            if (measurement_age is None or measurement_age > stale_delta)
            else LIGHT_CHECK_DESIRED_INTERVAL_FRESH
        )
        if self._last_light_check_utc is None:
            return True
        return (now_utc - self._last_light_check_utc) >= desired_interval

    async def _fetch_light_check_rep_list(self):
        """Fetch representative optimizer data for light check (batch or single). Returns list of data items."""
        if self._light_check_optimizer_ids and self._has_batch_api:
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "Adaptive polling lightweight check (batch, %d optimizers)",
                    len(self._light_check_optimizer_ids),
                )
            return await self.hass.async_add_executor_job(
                self.my_api.requestSystemDataBatch,
                self._light_check_optimizer_ids,
            )
        rep_list = []
        if self._representative_optimizer_id:
            rep_info = await self.hass.async_add_executor_job(
                self.my_api.requestSystemData,
                self._representative_optimizer_id,
            )
            rep_list = [rep_info] if rep_info else []
        if _LOGGER.isEnabledFor(logging.DEBUG) and self._representative_optimizer_id:
            _LOGGER.debug(
                "Adaptive polling lightweight check (opt_id=%s)",
                self._representative_optimizer_id,
            )
        return rep_list

    def _light_check_should_trigger_full_refresh(self, rep_list, latest_measurement, now_utc) -> bool:
        """True if any rep_list item has newer lastmeasurement and cooldown passed."""
        for rep_info in rep_list or []:
            rep_lm = getattr(rep_info, "lastmeasurement", None) if rep_info else None
            if not isinstance(rep_lm, datetime):
                continue
            if latest_measurement is not None and rep_lm <= latest_measurement:
                continue
            if self._last_full_fetch_utc is not None and (now_utc - self._last_full_fetch_utc) < LIGHT_CHECK_MIN_INTERVAL:
                continue
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "Adaptive polling detected new data (rep_last=%s > latest=%s); scheduling full refresh",
                    rep_lm,
                    latest_measurement,
                )
            return True
        return False

    async def _run_light_check(self, now_utc, latest_measurement) -> bool:
        """
        Run lightweight check (batch or single optimizer). Return True if caller should set do_full_refresh.
        Sets _last_light_check_utc.
        """
        self._last_light_check_utc = now_utc
        try:
            rep_list = await self._fetch_light_check_rep_list()
            return self._light_check_should_trigger_full_refresh(rep_list, latest_measurement, now_utc)
        except Exception as e:  # pylint: disable=broad-except
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("Lightweight update check failed: %s", e)
            return False

    def _index_optimizers_by_position(self, data_dict: dict) -> None:
        """Add position keys (inv_idx, str_idx, opt_idx) to data_dict for stable lookup after hardware swap."""
        if not self._site_structure:
            return
        for inv_idx, inverter in enumerate(self._site_structure.inverters, start=1):
            for str_idx, string in enumerate(inverter.strings, start=1):
                for opt_idx, optimizer in enumerate(
                    getattr(string, "optimizers") or (), start=1
                ):
                    pos_key = (inv_idx, str_idx, opt_idx)
                    item = data_dict.get(optimizer.optimizerId)
                    if item is not None:
                        data_dict[pos_key] = item

    def _build_data_dict(self, data_list, current_data, is_data_dict):
        """Build data_dict from full refresh list or reuse current_data. Updates first_boot and _obtained_from.

        Optimizer data is keyed by both panel_id (serial) and by (inv_idx, str_idx, opt_idx) so that
        after a hardware swap the same logical position keeps the same entity and shows the new unit's data.
        """
        if data_list is not None:
            data_dict = {}
            for item in data_list:
                if item is None:
                    continue
                data_dict[item.panel_id] = item
            self._index_optimizers_by_position(data_dict)
            self.first_boot = False
            self._obtained_from = getattr(self.my_api, "_obtained_from", OBTAINED_FROM_ONE)
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "SolarEdge Optimizers: Full refresh returned %d optimizer/aggregate items (source: %s)",
                    len(data_dict),
                    self._obtained_from,
                )
            return data_dict
        data_dict = current_data if is_data_dict else {}
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "SolarEdge Optimizers: Reusing existing data (no full refresh), %d items",
                len(data_dict) if isinstance(data_dict, dict) else 0,
            )
        return data_dict

    async def _refresh_temperature_when_no_full_refresh(self, data_dict) -> None:
        """
        When we did not do a full refresh, still refresh optimizer temperatures if the API
        supports it (e.g. SolarEdge One). get_optimizer_temperatures_cached() only hits the
        API when its temperature cache (TEMPERATURE_CACHE_TTL) is expired, so this keeps
        temperature updated periodically even when power/voltage etc. are not updating.
        """
        get_temps = getattr(self.my_api, "get_optimizer_temperatures_cached", None)
        if get_temps is None or not data_dict:
            return
        try:
            temp_map = await self.hass.async_add_executor_job(get_temps)
            if not temp_map:
                return
            for item in data_dict.values():
                pid = getattr(item, "panel_id", None)
                if pid is not None and pid in temp_map:
                    setattr(item, "temperature", temp_map[pid])
        except Exception as e:  # pylint: disable=broad-except
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "SolarEdge Optimizers: Optional temperature refresh (no full refresh) failed: %s",
                    e,
                )

    async def _fetch_lifetime_energy_and_aggregate(self, data_dict, current_utc, site_id):
        """Fetch lifetime energy, then run aggregated data calculation."""
        try:
            lifetime_energy_data = await self.hass.async_add_executor_job(
                self.my_api.get_lifetime_energy_cached
            )
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "SolarEdge Optimizers: Lifetime energy data has %d entries for aggregation",
                    len(lifetime_energy_data) if isinstance(lifetime_energy_data, dict) else 0,
                )
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            _LOGGER.warning(
                "SolarEdge API unreachable when fetching lifetime energy: %s. Using empty data for this update.",
                e,
            )
            lifetime_energy_data = {}
        portal_site_lifetime_kwh = _site_lifetime_kwh_from_layout_energy(lifetime_energy_data)
        self._calculate_aggregated_data(
            data_dict,
            current_utc,
            self._timetocheck,
            lifetime_energy_data,
            site_id,
            portal_site_lifetime_kwh=portal_site_lifetime_kwh,
            include_site_id_in_entity_id=self._include_site_id_in_entity,
        )

    def _log_update_cycle_debug(
        self,
        do_full_refresh: bool,
        should_light_check: bool,
        latest_measurement,
        stale_delta: timedelta,
        now_utc: datetime,
    ) -> None:
        """Log debug info for the current update cycle."""
        if not _LOGGER.isEnabledFor(logging.DEBUG):
            return
        measurement_age = (now_utc - latest_measurement) if isinstance(latest_measurement, datetime) else None
        is_stale = measurement_age is None or measurement_age > stale_delta
        desired_interval = (
            LIGHT_CHECK_DESIRED_INTERVAL_STALE
            if is_stale
            else LIGHT_CHECK_DESIRED_INTERVAL_FRESH
        )
        interval_kind = "stale" if is_stale else "fresh"
        _LOGGER.debug(
            "SolarEdge Optimizers coordinator: update cycle do_full_refresh=%s should_light_check=%s "
            "measurement_age=%s desired_interval=%s interval_kind=%s latest_measurement=%s",
            do_full_refresh,
            should_light_check,
            measurement_age,
            desired_interval,
            interval_kind,
            latest_measurement,
        )

    async def _run_update_cycle(
        self,
        now_utc: datetime,
        do_full_refresh: bool,
        current_data,
        is_data_dict: bool,
        stale_delta: timedelta,
    ):
        """Execute one update cycle: fetch if needed, build data_dict, aggregate, return data_dict."""
        data_list = None
        if do_full_refresh:
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("SolarEdge Optimizers coordinator: Performing full refresh (requestAllData)")
            data_list = await self.hass.async_add_executor_job(self.my_api.requestAllData)
            self._last_full_fetch_utc = now_utc

        current_utc = now_utc
        self._timetocheck = current_utc - stale_delta
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "Timezone debug - Current UTC: %s | Stale delta: %s | Checking time: %s | HA timezone: %s",
                current_utc,
                stale_delta,
                self._timetocheck,
                self.hass.config.time_zone,
            )

        data_dict = self._build_data_dict(data_list, current_data, is_data_dict)

        if not do_full_refresh:
            await self._refresh_temperature_when_no_full_refresh(data_dict)

        if self._site_structure:
            site_id = self._site_structure.siteId
            await self._fetch_lifetime_energy_and_aggregate(data_dict, current_utc, site_id)

        self._integration_last_polled = current_utc
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "SolarEdge Optimizers: Update complete, data_dict has %d entries, last_polled=%s",
                len(data_dict) if isinstance(data_dict, dict) else 0,
                self._integration_last_polled,
            )
        return data_dict

    def _check_revert_to_one_and_log(self, do_full_refresh: bool, is_data_dict: bool, current_data: dict | None, now_utc: datetime) -> bool:
        """Check if we should retry One API and log if so. Returns updated do_full_refresh."""
        if self._should_retry_revert_to_one(do_full_refresh, is_data_dict, current_data, now_utc):
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "SolarEdge Optimizers: Re-trying SolarEdge One API (data from legacy for %s)",
                    now_utc - self._last_full_fetch_utc,
                )
            return True
        return do_full_refresh

    async def _determine_refresh_strategy(self, now_utc: datetime, current_data: dict | None, is_data_dict: bool) -> tuple[bool, timedelta]:
        """Determine if full refresh is needed and get stale delta. Returns (do_full_refresh, stale_delta)."""
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "SolarEdge Optimizers: Determining refresh strategy (is_data_dict=%s, has_data=%s)",
                is_data_dict, current_data is not None,
            )
        do_full_refresh = self._decide_initial_full_refresh(is_data_dict, current_data)
        do_full_refresh = self._check_revert_to_one_and_log(do_full_refresh, is_data_dict, current_data, now_utc)

        latest_measurement = (
            self._get_latest_measurement_from_data(current_data)
            if (is_data_dict and current_data)
            else None
        )
        stale_delta = self._get_stale_delta()
        should_light_check = self._should_do_light_check(
            do_full_refresh, now_utc, latest_measurement, stale_delta
        )

        self._log_update_cycle_debug(
            do_full_refresh, should_light_check, latest_measurement, stale_delta, now_utc
        )

        if should_light_check and await self._run_light_check(now_utc, latest_measurement):
            do_full_refresh = True

        return do_full_refresh, stale_delta

    async def _async_update_data(self) -> dict:
        """Fetch data from API endpoint.

        This is the place to pre-process the data to lookup tables
        so entities can quickly look up their data.
        """
        try:
            if self._site_structure is None:
                await self._async_setup()
            
            async with asyncio.timeout(COORDINATOR_REFRESH_TIMEOUT_SEC):
                now_utc = datetime.now(timezone.utc)
                current_data = getattr(self, "data", None)
                is_data_dict = isinstance(current_data, dict)

                do_full_refresh, stale_delta = await self._determine_refresh_strategy(
                    now_utc, current_data, is_data_dict
                )

                return await self._run_update_cycle(
                    now_utc, do_full_refresh, current_data, is_data_dict, stale_delta
                )

        except (asyncio.TimeoutError, TimeoutError) as err:
            _LOGGER.error(
                "SolarEdge Optimizers: Refresh timed out after %s s (slow API or many optimizers). Consider checking network or SolarEdge portal.",
                COORDINATOR_REFRESH_TIMEOUT_SEC,
            )
            raise UpdateFailed(err) from err
        except (ConnectionError, OSError) as err:
            _LOGGER.error("SolarEdge Optimizers: Network error during update: %s", err)
            raise UpdateFailed(err) from err
        except (ValueError, KeyError, TypeError) as err:
            _LOGGER.error("SolarEdge Optimizers: Data parsing error during update: %s", err)
            raise UpdateFailed(err) from err
        except UpdateFailed:
            raise
        except RuntimeError as err:
            _LOGGER.exception("SolarEdge Optimizers: Runtime error during update: %s", err)
            raise UpdateFailed(err) from err
