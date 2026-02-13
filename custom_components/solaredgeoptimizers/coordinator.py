"""Data coordinator for SolarEdge Optimizers Home Assistant integration."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import requests

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import (
    DOMAIN,
    UPDATE_DELAY,
    CHECK_TIME_DELTA,
    CONF_INCLUDE_SITE_ID_IN_ENTITY_ID,
)

# AJT: 10-Jan-2025: Changed from absolute import to relative import to use local solaredgeoptimizers.py instead of site-packages version
from .solaredgeoptimizers import (
    solaredgeoptimizers,
    SolarEdgeAggregatedData,
    _lifetime_energy_to_kwh,
    _site_lifetime_kwh_from_layout_energy,
)

_LOGGER = logging.getLogger(__name__)

class MyCoordinator(DataUpdateCoordinator):
    """My custom coordinator."""

    def __init__(self, hass, my_api: solaredgeoptimizers, first_boot, config_entry=None):
        """Initialize my coordinator."""
        # AJT: 10-Jan-2025: Pass config_entry to parent class to enable async_config_entry_first_refresh()
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
        # AJT: 16-Jan-2026: Pre-compute timetocheck once per update cycle for all sensors
        self._timetocheck = None
        # AJT: 24-Jan-2026: Track when the integration last completed an update
        self._integration_last_polled = None
        # AJT: 24-Jan-2026: Store site structure for aggregated calculations
        self._site_structure = None
        # AJT: 25-Jan-2026: Adaptive polling state
        self._last_full_fetch_utc = None
        self._last_light_check_utc = None
        self._representative_optimizer_id = None
        # Whether to include site ID in entity_id_path (for entity IDs). Options override data; default False when key missing.
        _include = False
        if config_entry:
            _include = config_entry.options.get(
                CONF_INCLUDE_SITE_ID_IN_ENTITY_ID,
                config_entry.data.get(CONF_INCLUDE_SITE_ID_IN_ENTITY_ID, False),
            )
        self._include_site_id_in_entity = bool(_include)
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "SolarEdge Optimizers coordinator: include_site_id_in_entity_id=%s (from options/data)",
                self._include_site_id_in_entity,
            )

    async def _async_setup(self) -> None:
        """Set up the coordinator.

        Can be overwritten by integrations to load data or resources
        only once during the first refresh.
        """
        # AJT: 24-Jan-2026: Add detailed debugging for initial setup issues
        _LOGGER.info("SolarEdge Optimizers: Starting coordinator setup")
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("SolarEdge Optimizers: About to request list of all panels")

        try:
            site = await self.hass.async_add_executor_job(self.my_api.requestListOfAllPanels)
            # AJT: 24-Jan-2026: Store site structure for aggregated calculations
            self._site_structure = site
            # AJT: 25-Jan-2026: Pick a representative optimizer for lightweight update checks
            if self._representative_optimizer_id is None:
                try:
                    for inv in site.inverters:
                        for s in inv.strings:
                            if getattr(s, "optimizers", None):
                                self._representative_optimizer_id = s.optimizers[0].optimizerId
                                raise StopIteration
                except StopIteration:
                    pass
                if _LOGGER.isEnabledFor(logging.DEBUG) and self._representative_optimizer_id:
                    _LOGGER.debug(
                        "SolarEdge Optimizers: Using representative optimizer %s for lightweight checks",
                        self._representative_optimizer_id,
                    )
            _LOGGER.info("SolarEdge Optimizers: Successfully retrieved panel list")

            _LOGGER.info("Found all information for site: %s", site.siteId)
            _LOGGER.info("Site has %s inverters", len(site.inverters))
            _LOGGER.info(
                "Setting up Home Assistant devices and sensors for %s optimizers across %s inverters",
                site.returnNumberOfOptimizers(),
                len(site.inverters)
            )
        except Exception as e:
            _LOGGER.error("SolarEdge Optimizers: Failed to get panel list in coordinator setup: %s", e)
            raise
        

        device_registry = dr.async_get(self.hass)

        site_id = str(site.siteId)
        device_registry.async_get_or_create(
            config_entry_id=self.config_entry.entry_id,
            identifiers={(DOMAIN, f"site_{site.siteId}")},
            manufacturer="SolarEdge",
            model=f"SITE {site.siteId}",
            name=f"Site {site_id}",
        )
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("Created device for site: %s", site_id)

        for inv_idx, inverter in enumerate(site.inverters, start=1):
            _LOGGER.info("Adding all optimizers from inverter: %s", inv_idx)
            inverter_name = f"Inverter {site_id}.{inv_idx}"
            device_registry.async_get_or_create(
                config_entry_id=self.config_entry.entry_id,
                identifiers={(DOMAIN, inverter.serialNumber)},
                manufacturer="SolarEdge",
                model=f"{inverter.type} {inverter.displayName}",
                name=inverter_name,
                hw_version=inverter.serialNumber,
                via_device=(DOMAIN, f"site_{site.siteId}"),
            )

            for str_idx, string in enumerate(inverter.strings, start=1):
                string_name = f"String {site_id}.{inv_idx}.{str_idx}"
                device_registry.async_get_or_create(
                    config_entry_id=self.config_entry.entry_id,
                    identifiers={(DOMAIN, f"{self.config_entry.entry_id}_{string.stringId}")},
                    manufacturer="SolarEdge",
                    model=f"STRING {string.displayName}",
                    name=string_name,
                    via_device=(DOMAIN, inverter.serialNumber),
                )
                if _LOGGER.isEnabledFor(logging.DEBUG):
                    _LOGGER.debug("Created device for string: %s", string_name)

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
        """
        # AJT: 27-Jan-2026: Pre-build lifetime energy lookup with string keys to avoid repeated conversions
        # AJT: 27-Jan-2026: Use dict comprehension for better performance
        lifetime_energy_lookup = {}
        for inv in self._site_structure.inverters:
            for s in inv.strings:
                key = str(s.stringId)
                if key in lifetime_energy_data:
                    lifetime_energy_lookup[s.stringId] = lifetime_energy_data[key]

        # Site-level aggregation variables
        site_current = 0.0
        site_power = 0.0
        site_voltage_sum = 0.0
        site_voltage_count = 0
        site_last_measurement = None
        site_active_optimizers = 0
        site_active_strings = 0
        site_active_inverters = 0
        # AJT: 25-Jan-2026: Site lifetime energy is derived from aggregated inverters
        site_lifetime_energy = 0.0

        site_id_str = str(site_id)
        # Process each inverter
        for inv_idx, inverter in enumerate(self._site_structure.inverters, start=1):
            inverter_current = 0.0
            inverter_power = 0.0
            inverter_voltage_sum = 0.0
            inverter_voltage_count = 0
            inverter_last_measurement = None
            inverter_active_optimizers = 0
            inverter_active_strings = 0
            # AJT: 25-Jan-2026: Inverter lifetime energy is derived from aggregated strings
            inverter_lifetime_energy = 0.0

            # Process each string in the inverter
            for str_idx, string in enumerate(inverter.strings, start=1):
                string_current = 0.0
                string_power = 0.0
                string_voltage_sum = 0.0
                string_voltage_count = 0
                string_last_measurement = None
                string_active_optimizers = 0

                # Aggregate data from all optimizers in this string
                for optimizer in string.optimizers:
                    optimizer_data = data_dict.get(optimizer.optimizerId)
                    if optimizer_data:
                        # AJT: 27-Jan-2026: Cache isinstance check result to avoid repeated calls
                        last_measurement = optimizer_data.lastmeasurement
                        is_datetime = isinstance(last_measurement, datetime)
                        
                        # Track latest measurement time regardless of age
                        if is_datetime:
                            if (string_last_measurement is None or
                                last_measurement > string_last_measurement):
                                string_last_measurement = last_measurement

                        # Check if measurement is recent enough
                        if is_datetime and last_measurement > timetocheck:
                            # AJT: 27-Jan-2026: Cache attribute access to avoid repeated lookups
                            opt_current = optimizer_data.current
                            opt_power = optimizer_data.power
                            opt_voltage = optimizer_data.voltage
                            
                            # AJT: 27-Jan-2026: Avoid 'or 0.0' overhead by checking None explicitly
                            if opt_current is not None:
                                string_current += opt_current
                            if opt_power is not None:
                                string_power += opt_power

                            if opt_voltage:
                                string_voltage_sum += opt_voltage
                                string_voltage_count += 1

                            string_active_optimizers += 1

                # Get lifetime energy from API data (always, even if no active optimizers)
                string_lifetime_energy = 0.0
                energy_data = lifetime_energy_lookup.get(string.stringId)
                if energy_data:
                    kWh = _lifetime_energy_to_kwh(energy_data)
                    if kWh is not None:
                        string_lifetime_energy = kWh

                # AJT: 25-Jan-2026: Accumulate inverter lifetime energy from string lifetime energy
                # AJT: 27-Jan-2026: Round after accumulation to maintain precision
                inverter_lifetime_energy = round(inverter_lifetime_energy + string_lifetime_energy, 3)

                # Create aggregated string data (always, so values can reset to 0)
                string_entity_path = (site_id_str, inv_idx, str_idx) if include_site_id_in_entity_id else (inv_idx, str_idx)
                string_aggregated = SolarEdgeAggregatedData(
                    entity_id=f"string_{string.stringId}",
                    entity_type="string",
                    lifetime_energy=string_lifetime_energy,
                    entity_id_path=string_entity_path,
                )
                # AJT: 27-Jan-2026: Cache divisor to avoid repeated checks and enable faster division
                if string_active_optimizers > 0:
                    string_aggregated.current = string_current / string_active_optimizers
                    string_aggregated.power = string_power
                else:
                    string_aggregated.current = 0.0
                    string_aggregated.power = 0.0
                string_aggregated.voltage = (string_voltage_sum / string_voltage_count) if string_voltage_count > 0 else 0.0
                # AJT: 27-Jan-2026: Avoid 'or' operator overhead - explicit None check is faster
                string_aggregated.lastmeasurement = string_last_measurement if string_last_measurement is not None else current_utc
                string_aggregated.child_count = len(string.optimizers)
                string_aggregated.active_optimizer_count = string_active_optimizers
                string_aggregated.serialnumber = f"String_{string.stringId}"
                string_aggregated.panel_description = string.displayName

                data_dict[string_aggregated.panel_id] = string_aggregated

                # Add to inverter totals (only if this string has active data)
                if string_active_optimizers > 0:
                    inverter_current += string_aggregated.current
                    inverter_active_strings += 1
                    inverter_power += string_power
                    if string_voltage_count > 0:
                        inverter_voltage_sum += string_aggregated.voltage
                        inverter_voltage_count += 1
                    inverter_active_optimizers += string_active_optimizers

                # Track latest measurement time across strings (regardless of age)
                if (inverter_last_measurement is None or
                    string_last_measurement and string_last_measurement > inverter_last_measurement):
                    inverter_last_measurement = string_last_measurement

            # Create aggregated inverter data (always, so values can reset to 0)
            inverter_entity_path = (site_id_str, inv_idx) if include_site_id_in_entity_id else (inv_idx,)
            inverter_aggregated = SolarEdgeAggregatedData(
                entity_id=f"inverter_{inverter.inverterId}",
                entity_type="inverter",
                lifetime_energy=round(inverter_lifetime_energy, 3),
                entity_id_path=inverter_entity_path,
            )
            # AJT: 27-Jan-2026: Cache divisor to avoid repeated checks and enable faster division
            if inverter_active_strings > 0:
                inverter_aggregated.current = inverter_current / inverter_active_strings
                inverter_aggregated.power = inverter_power
            else:
                inverter_aggregated.current = 0.0
                inverter_aggregated.power = 0.0
            inverter_aggregated.voltage = (inverter_voltage_sum / inverter_voltage_count) if inverter_voltage_count > 0 else 0.0
            # AJT: 27-Jan-2026: Avoid 'or' operator overhead - explicit None check is faster
            inverter_aggregated.lastmeasurement = inverter_last_measurement if inverter_last_measurement is not None else current_utc
            inverter_aggregated.child_count = len(inverter.strings)
            inverter_aggregated.active_optimizer_count = inverter_active_optimizers
            inverter_aggregated.serialnumber = inverter.serialNumber or f"Inverter_{inverter.inverterId}"
            inverter_aggregated.panel_description = inverter.displayName

            data_dict[inverter_aggregated.panel_id] = inverter_aggregated
            # AJT: 25-Jan-2026: Accumulate site lifetime energy from inverter lifetime energy
            # AJT: 27-Jan-2026: Round after accumulation to maintain precision
            site_lifetime_energy = round(site_lifetime_energy + inverter_aggregated.lifetime_energy, 3)

            # Accumulate site-level data (only from inverters with active strings)
            if inverter_active_strings > 0:
                site_current += inverter_aggregated.current
                site_active_inverters += 1
                site_power += inverter_power
                if inverter_voltage_count > 0:
                    site_voltage_sum += inverter_aggregated.voltage
                    site_voltage_count += 1

            site_active_optimizers += inverter_active_optimizers
            site_active_strings += inverter_active_strings

            # Track latest measurement time across inverters (regardless of age)
            if (site_last_measurement is None or
                inverter_last_measurement and inverter_last_measurement > site_last_measurement):
                site_last_measurement = inverter_last_measurement

        # Create site-level aggregated data (always)
        # Use portal site total only when aggregated optimizer data is unreliable (e.g. all Wh, no real total)
        _RELIABLE_THRESHOLD_KWH = 100.0
        if (
            portal_site_lifetime_kwh is not None
            and portal_site_lifetime_kwh >= _RELIABLE_THRESHOLD_KWH
            and site_lifetime_energy < _RELIABLE_THRESHOLD_KWH
        ):
            site_lifetime_energy = portal_site_lifetime_kwh
        # Site level always uses actual site ID in entity ID (e.g. sensor.power_2065855)
        site_entity_path = (site_id_str,)
        site_aggregated = SolarEdgeAggregatedData(
            entity_id=f"site_{site_id}",
            entity_type="site",
            lifetime_energy=round(site_lifetime_energy, 3),
            entity_id_path=site_entity_path,
        )
        # AJT: 27-Jan-2026: Cache divisor to avoid repeated checks and enable faster division
        if site_active_inverters > 0:
            site_aggregated.current = site_current / site_active_inverters
            site_aggregated.power = site_power
        else:
            site_aggregated.current = 0.0
            site_aggregated.power = 0.0
        site_aggregated.voltage = (site_voltage_sum / site_voltage_count) if site_voltage_count > 0 else 0.0
        # AJT: 27-Jan-2026: Avoid 'or' operator overhead - explicit None check is faster
        site_aggregated.lastmeasurement = site_last_measurement if site_last_measurement is not None else current_utc
        site_aggregated.child_count = len(self._site_structure.inverters)
        site_aggregated.active_optimizer_count = site_active_optimizers
        # AJT: 27-Jan-2026: Use cached site_id instead of accessing attribute
        site_aggregated.serialnumber = f"Site_{site_id}"
        site_aggregated.panel_description = f"Site {site_id}"

        data_dict[site_aggregated.panel_id] = site_aggregated

    async def _async_update_data(self):
        """Fetch data from API endpoint.

        This is the place to pre-process the data to lookup tables
        so entities can quickly look up their data.
        """
        try:
            # Note: asyncio.TimeoutError and aiohttp.ClientError are already
            # handled by the data update coordinator.
            async with asyncio.timeout(300):
                # AJT: 27-Jan-2026: Only log if debug level is enabled to reduce overhead
                if _LOGGER.isEnabledFor(logging.DEBUG):
                    _LOGGER.debug("Update from the coordinator")
                # AJT: 25-Jan-2026: Adaptive polling:
                # AJT: 25-Jan-2026: - Run a lightweight check frequently (single optimizer) to detect new portal data sooner.
                # AJT: 25-Jan-2026: - Only run a full refresh (requestAllData) when data has changed or on first boot.
                now_utc = datetime.now(timezone.utc)

                # AJT: 27-Jan-2026: Cache data check to avoid repeated getattr/isinstance calls
                current_data = getattr(self, "data", None)
                is_data_dict = isinstance(current_data, dict)
                
                do_full_refresh = (
                    self.first_boot
                    or not is_data_dict
                    or not current_data
                )

                # AJT: 25-Jan-2026: Determine latest known measurement timestamp
                # AJT: 27-Jan-2026: Cache site_id to avoid repeated attribute access
                latest_measurement = None
                if is_data_dict and current_data:
                    site_id = self._site_structure.siteId if self._site_structure else None
                    site_key = f"site_{site_id}" if site_id else None
                    if site_key and site_key in current_data:
                        latest_measurement = getattr(current_data[site_key], "lastmeasurement", None)
                    if not isinstance(latest_measurement, datetime):
                        for v in current_data.values():
                            lm = getattr(v, "lastmeasurement", None)
                            if isinstance(lm, datetime) and (latest_measurement is None or lm > latest_measurement):
                                latest_measurement = lm

                # AJT: 25-Jan-2026: Decide if we should hit the portal for a lightweight check this tick
                measurement_age = (now_utc - latest_measurement) if isinstance(latest_measurement, datetime) else None
                desired_check_interval = timedelta(minutes=15) if (measurement_age is None or measurement_age > CHECK_TIME_DELTA) else timedelta(minutes=2)

                should_light_check = (
                    not do_full_refresh
                    and self._representative_optimizer_id is not None
                    and (
                        self._last_light_check_utc is None
                        or (now_utc - self._last_light_check_utc) >= desired_check_interval
                    )
                )

                if should_light_check:
                    self._last_light_check_utc = now_utc
                    try:
                        if _LOGGER.isEnabledFor(logging.DEBUG):
                            _LOGGER.debug(
                                "Adaptive polling lightweight check (opt_id=%s, interval=%s, latest=%s)",
                                self._representative_optimizer_id,
                                desired_check_interval,
                                latest_measurement,
                            )
                        rep_info = await self.hass.async_add_executor_job(
                            self.my_api.requestSystemData, self._representative_optimizer_id
                        )
                        rep_lm = getattr(rep_info, "lastmeasurement", None) if rep_info else None
                        if isinstance(rep_lm, datetime) and (
                            latest_measurement is None or rep_lm > latest_measurement
                        ):
                            # Portal has new data; refresh, but avoid hammering if multiple checks happen quickly
                            if self._last_full_fetch_utc is None or (now_utc - self._last_full_fetch_utc) >= timedelta(minutes=2):
                                if _LOGGER.isEnabledFor(logging.DEBUG):
                                    _LOGGER.debug(
                                        "Adaptive polling detected new data (rep_last=%s > latest=%s); scheduling full refresh",
                                        rep_lm,
                                        latest_measurement,
                                    )
                                do_full_refresh = True
                    except Exception as e:
                        if _LOGGER.isEnabledFor(logging.DEBUG):
                            _LOGGER.debug("Lightweight update check failed: %s", e)

                data_list = None
                if do_full_refresh:
                    if _LOGGER.isEnabledFor(logging.DEBUG):
                        _LOGGER.debug("Performing full refresh (requestAllData)")
                    data_list = await self.hass.async_add_executor_job(self.my_api.requestAllData)
                    self._last_full_fetch_utc = now_utc

                # AJT: 16-Jan-2026: Pre-compute timetocheck once per update cycle for all sensors
                # AJT: 18-Jan-2026: Use datetime.now(timezone.utc) to ensure correct UTC time
                # dt_util.utcnow() should work, but using explicit UTC for reliability
                current_utc = now_utc
                self._timetocheck = current_utc - CHECK_TIME_DELTA    # tz-aware UTC
                # AJT: 18-Jan-2026: Log timezone debugging info for checking time calculation
                # AJT: 27-Jan-2026: Already using isEnabledFor check - this is optimal
                if _LOGGER.isEnabledFor(logging.DEBUG):
                    # AJT: 27-Jan-2026: Cache timezone access to avoid repeated attribute lookup
                    ha_timezone = self.hass.config.time_zone
                    _LOGGER.debug(
                        "Timezone debug - Current UTC: %s | Checking time (UTC - %s): %s | HA timezone: %s",
                        current_utc,
                        CHECK_TIME_DELTA,
                        self._timetocheck,
                        ha_timezone
                    )
                # AJT: 25-Jan-2026: Build data dictionary:
                # AJT: 25-Jan-2026: - If we did a full refresh, rebuild from the new optimizer list.
                # AJT: 25-Jan-2026: - Otherwise, reuse the existing coordinator data (and just recompute aggregations).
                if data_list is not None:
                    # AJT: 27-Jan-2026: Use dict comprehension for better performance
                    data_dict = {item.panel_id: item for item in data_list if item is not None}
                    self.first_boot = False
                    if _LOGGER.isEnabledFor(logging.DEBUG):
                        _LOGGER.debug(
                            "SolarEdge Optimizers: Full refresh returned %d optimizer/aggregate items",
                            len(data_dict),
                        )
                else:
                    # AJT: 27-Jan-2026: Only copy if we need to modify, otherwise use reference
                    # AJT: 27-Jan-2026: Use cached is_data_dict check
                    data_dict = current_data if is_data_dict else {}
                    if _LOGGER.isEnabledFor(logging.DEBUG):
                        _LOGGER.debug(
                            "SolarEdge Optimizers: Reusing existing data (no full refresh), %d items",
                            len(data_dict) if isinstance(data_dict, dict) else 0,
                        )

                # AJT: 25-Jan-2026: Calculate aggregated data (string → inverter → site)
                if self._site_structure:
                    # AJT: 25-Jan-2026: Use cached lifetime energy (refresh at most hourly) to avoid frequent portal calls
                    # AJT: 27-Jan-2026: Cache site_id once to avoid repeated attribute access
                    site_id = self._site_structure.siteId
                    try:
                        lifetime_energy_data = await self.hass.async_add_executor_job(self.my_api.get_lifetime_energy_cached)
                    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                        _LOGGER.warning(
                            "SolarEdge API unreachable when fetching lifetime energy: %s. Using empty data for this update.",
                            e,
                        )
                        lifetime_energy_data = {}
                    # Portal site total (sum of unscaledEnergy); used for site only when aggregated is unreliable
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

                # Update integration-level last polled timestamp *after* all calculations
                self._integration_last_polled = current_utc

                return data_dict

        except Exception as err:
            # AJT: 11-Jan-2026: Improved exception logging with full traceback
            _LOGGER.exception("Error in updating updater: %s", err)
            raise UpdateFailed(err) from err
