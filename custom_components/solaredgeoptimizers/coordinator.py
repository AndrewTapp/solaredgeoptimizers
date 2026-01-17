"""Example integration using DataUpdateCoordinator."""
from datetime import datetime, timezone

import logging
import async_timeout

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    UPDATE_DELAY,
    CHECK_TIME_DELTA,
)

# AJT: 10-Jan-2025: Changed from absolute import to relative import to use local solaredgeoptimizers.py instead of site-packages version
from .solaredgeoptimizers import (
    solaredgeoptimizers,
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
            # Polling interval. Will only be polled if there are subscribers.
            update_interval=UPDATE_DELAY,
            config_entry=config_entry,
        )
        self.my_api = my_api
        self.first_boot = first_boot
        # AJT: 16-Jan-2026: Pre-compute timetocheck once per update cycle for all sensors
        self._timetocheck = None

    async def _async_setup(self) -> None:
        """Set up the coordinator.

        Can be overwritten by integrations to load data or resources
        only once during the first refresh.
        """

        site = await self.hass.async_add_executor_job(self.my_api.requestListOfAllPanels)

        _LOGGER.info("Found all information for site: %s", site.siteId)
        _LOGGER.info("Site has %s inverters", len(site.inverters))
        _LOGGER.info(
            "Adding all optimizers (%s) found to Home Assistant",
            site.returnNumberOfOptimizers(),
        )
        

        # AJT: 16-Jan-2026: Use enumerate instead of manual counter variable
        for i, inverter in enumerate(site.inverters, start=1):
            _LOGGER.info("Adding all optimizers from inverter: %s", i)

            device_registry = dr.async_get(self.hass)
            device_registry.async_get_or_create(
                config_entry_id=self.config_entry.entry_id,
                identifiers={(DOMAIN, inverter.serialNumber)},
                manufacturer="SolarEdge",
                model=inverter.type,
                name=inverter.displayName,
            )

    async def _async_update_data(self):
        """Fetch data from API endpoint.

        This is the place to pre-process the data to lookup tables
        so entities can quickly look up their data.
        """
        try:
            # Note: asyncio.TimeoutError and aiohttp.ClientError are already
            # handled by the data update coordinator.
            async with async_timeout.timeout(300):
                _LOGGER.debug("Update from the coordinator")
                data_list = await self.hass.async_add_executor_job(
                    self.my_api.requestAllData
                )

                # AJT: 16-Jan-2026: Pre-compute timetocheck once per update cycle for all sensors
                self._timetocheck = dt_util.utcnow() - CHECK_TIME_DELTA    # tz-aware UTC
                update = False

                # AJT: 16-Jan-2026: Pre-compute timezone-aware timestamps and check time efficiently
                # Only check if we have data to process
                if data_list:
                    for optimizer in data_list:
                        # AJT: 16-Jan-2026: Pre-convert timestamps to timezone-aware once in coordinator
                        # Ensure it's a datetime object and convert if naive
                        if isinstance(optimizer.lastmeasurement, datetime):
                            if optimizer.lastmeasurement.tzinfo is None:
                                optimizer.lastmeasurement = optimizer.lastmeasurement.replace(tzinfo=timezone.utc)
                        
                        # AJT: 16-Jan-2026: Reduce debug logging overhead - only log if debug level is enabled
                        if _LOGGER.isEnabledFor(logging.DEBUG):
                            # AJT: 10-Jan-2025: Fixed typo "measerument" to "measurement" in log message
                            _LOGGER.debug(
                                "Checking time: %s | Versus last measurement: %s",
                                self._timetocheck,
                                optimizer.lastmeasurement,
                            )
                    
                        if optimizer.lastmeasurement > self._timetocheck:
                            update = True
                            break

                # AJT: 11-Jan-2026: Always return data to allow lifetime_energy and last_measurement sensors to update
                # The time check is only used for logging purposes - all sensors need to update
                if update or self.first_boot:
                    _LOGGER.debug("We enter new data")
                    self.first_boot = False
                else:
                    _LOGGER.debug("No new measurements within time window, but returning data for cumulative sensors")
                
                # AJT: 16-Jan-2026: Convert list to dictionary for O(1) lookup instead of O(n) linear search
                data_dict = {item.panel_id: item for item in data_list}
                return data_dict

        except Exception as err:
            # AJT: 11-Jan-2026: Improved exception logging with full traceback
            _LOGGER.exception("Error in updating updater: %s", err)
            raise UpdateFailed(err) from err
