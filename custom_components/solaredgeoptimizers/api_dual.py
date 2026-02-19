"""
Dual SolarEdge API wrapper: prefer SolarEdge One API, fall back to legacy when One
returns no valid measurements (e.g. "Missing or invalid measurements for optimizer").
Exposes which source was used via _obtained_from for the "Obtained from" sensor.
"""
from __future__ import annotations

import logging
from typing import Any

from .solaredgeoptimizers import solaredgeoptimizers
from .solaredge_one_api import solaredge_one

_LOGGER = logging.getLogger(__name__)

OBTAINED_FROM_ONE = "One API"
OBTAINED_FROM_LEGACY = "Legacy API"


class SolarEdgeDualAPI:
    """
    Wrapper that tries SolarEdge One API first; if One returns no valid optimizer
    measurements, falls back to the legacy SolarEdge API. Tracks which source
    was used in _obtained_from for the site-level "Obtained from" sensor.
    """

    def __init__(self, siteid: str, username: str, password: str, timezone=None, language=None):
        self._one = solaredge_one(siteid, username, password, timezone, language)
        self._legacy = solaredgeoptimizers(siteid, username, password, timezone, language)
        self._last_used_api: str | None = None  # "one" or "legacy"
        self._obtained_from: str = OBTAINED_FROM_ONE  # For sensor; default until first fetch

    def check_login(self) -> int:
        """Succeed if either One or legacy login works (so fallback is available)."""
        code_one = self._one.check_login()
        if code_one == 200:
            code_legacy = self._legacy.check_login()
            if code_legacy != 200 and _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "SolarEdge Dual API: Legacy login returned %s (One succeeded); fallback may be limited",
                    code_legacy,
                )
            return 200
        code_legacy = self._legacy.check_login()
        if code_legacy == 200:
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "SolarEdge Dual API: One login failed (%s), legacy succeeded; using legacy only",
                    code_one,
                )
            return 200
        return code_one if code_one != 200 else code_legacy

    def requestListOfAllPanels(self) -> Any:
        """Prefer One for layout; fall back to legacy on failure."""
        try:
            return self._one.requestListOfAllPanels()
        except Exception as e:
            _LOGGER.warning("SolarEdge Dual API: One requestListOfAllPanels failed (%s), trying legacy", e)
            return self._legacy.requestListOfAllPanels()

    def _one_has_valid_measurements(self, data_list: list) -> bool:
        """Return True if at least one optimizer from One had a non-empty measurements dict."""
        if not data_list:
            return False
        for item in data_list:
            if item is None:
                continue
            if getattr(item, "_has_valid_measurements", False):
                return True
        return False

    def requestAllData(self) -> list[Any]:
        """
        Try One first; if One returns data but no optimizer has valid measurements,
        use legacy and set _obtained_from to Legacy API. Otherwise use One.
        """
        self._obtained_from = OBTAINED_FROM_ONE
        data_list = None
        try:
            data_list = self._one.requestAllData()
        except Exception as e:
            _LOGGER.warning(
                "SolarEdge Dual API: One requestAllData failed (%s), falling back to legacy",
                e,
            )
        if data_list is not None and self._one_has_valid_measurements(data_list):
            self._last_used_api = "one"
            self._obtained_from = OBTAINED_FROM_ONE
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("SolarEdge Dual API: Using One API data")
            return data_list
        # One unavailable or no valid measurements: use legacy
        try:
            data_list = self._legacy.requestAllData()
            self._last_used_api = "legacy"
            self._obtained_from = OBTAINED_FROM_LEGACY
            _LOGGER.info(
                "SolarEdge Dual API: Using Legacy API data (One had no valid measurements or failed)"
            )
            return data_list or []
        except Exception as e:
            _LOGGER.error("SolarEdge Dual API: Legacy requestAllData also failed: %s", e)
            # Return One result if we have it (even if invalid) so UI doesn't break
            if data_list is not None:
                self._last_used_api = "one"
                self._obtained_from = OBTAINED_FROM_ONE
                return data_list
            raise

    def get_lifetime_energy_cached(self) -> dict[str, Any]:
        """Return lifetime energy from the API that was last used for requestAllData."""
        if self._last_used_api == "legacy":
            return self._legacy.get_lifetime_energy_cached()
        return self._one.get_lifetime_energy_cached()

    def requestSystemData(self, item_id: str) -> Any:
        """Delegate to the API we last used for full data (for lightweight check in legacy mode)."""
        if self._last_used_api == "legacy":
            return self._legacy.requestSystemData(item_id)
        return self._one.requestSystemData(item_id)

    def requestSystemDataBatch(self, item_ids: list) -> list[Any]:
        """Use One for batch (only One supports it); used to detect when One has new data."""
        return self._one.requestSystemDataBatch(item_ids)

    def get_inverter_models(self, serials: list) -> dict[str, str]:
        """Only One provides inverter models."""
        return self._one.get_inverter_models(serials)

    def close(self) -> None:
        """Close both API sessions."""
        try:
            self._one.close()
        except Exception as e:  # pylint: disable=broad-except
            _LOGGER.debug("SolarEdge Dual API: Error closing One API: %s", e)
        try:
            self._legacy.close()
        except Exception as e:  # pylint: disable=broad-except
            _LOGGER.debug("SolarEdge Dual API: Error closing legacy API: %s", e)
