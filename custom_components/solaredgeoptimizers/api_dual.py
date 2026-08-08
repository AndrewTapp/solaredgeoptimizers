"""
SolarEdge Optimizers Integration - Dual API Wrapper (api_dual.py)

This module provides a unified API interface that intelligently switches between the
SolarEdge One API and the legacy Monitoring API based on availability and data quality.

API Selection Strategy:
- When use_solaredge_one=True (default): Tries One API first, falls back to legacy
- When use_solaredge_one=False: Uses legacy API exclusively
- Fallback triggered when One API fails or returns no valid optimizer measurements

Methods Implemented:
- check_login(): Succeeds if either API authenticates successfully
- verify_authentication(): Raises SolarEdgeAuthError when check_login returns 401 (v2.4.20+)
- requestListOfAllPanels(): Prefers One for site layout, falls back to legacy
- requestAllData(): Tries One first, uses legacy if One has no valid measurements
- get_lifetime_energy_cached(): Uses whichever API was last used for requestAllData
- requestSystemData(): Delegates to last-used API for single optimizer queries
- requestSystemDataBatch(): One API only (batch queries for lightweight checks)
- get_inverter_models(): One API only (legacy doesn't provide inverter models)
- get_site_info_cached(): One API only; returns installationDate, peakPower (kW) from layout/information/site
- get_dashboard_site_production_cached(installation_date): One API only; returns site production (Wh) from dashboard/energy
- get_layout_energy_by_inverter_cached(installation_date): One API only; returns inverter/string energy (Wh) from layout/energy by-inverter
- close(): Closes both API backends (legacy closes all thread-local sessions; One clears tokens);
  idempotent via _closed flag; both backends closed even if one raises; single **info** summary here
  when both succeed; **warning** if either backend close fails (child clients use log_summary=False
  to avoid stacked unload messages)

Tracking:
- _last_used_api: Tracks which API ("one" or "legacy") provided the last data
- _obtained_from: Human-readable string ("One API" or "Legacy API") for the sensor

The "Obtained from" sensor shows users which API is currently providing data,
helping diagnose issues when One API is unavailable or returning stale data.

Authentication (v2.4.20+):
- verify_authentication() and _is_auth_related_error() detect invalid credentials during
  requestAllData when both backends fail with auth-like errors; coordinator maps these to
  ConfigEntryAuthFailed after confirming check_login returns 401.
"""
from __future__ import annotations

import logging
from typing import Any

import requests

from .const import OBTAINED_FROM_ONE, OBTAINED_FROM_LEGACY
from .exceptions import SolarEdgeAuthError
from .solaredgeoptimizers import solaredgeoptimizers
from .solaredge_one_api import solaredge_one

_LOGGER = logging.getLogger(__name__)


def _is_auth_related_error(err: BaseException) -> bool:
    """Return True when an exception may indicate invalid credentials."""
    if isinstance(err, SolarEdgeAuthError):
        return True
    if isinstance(err, requests.HTTPError) and err.response is not None:
        return err.response.status_code in (401, 498)
    message = str(err)
    return "ERROR001" in message and ("401" in message or "498" in message)


class SolarEdgeDualAPI:
    """
    Wrapper that tries SolarEdge One API first; if One returns no valid optimizer
    measurements, falls back to the legacy SolarEdge API. When use_solaredge_one
    is False, always uses the legacy API only. Tracks which source was used in
    _obtained_from for the site-level "Obtained from" sensor.
    """

    def __init__(  # pylint: disable=too-many-arguments
        self, siteid: str, username: str, password: str, timezone: str | None = None, language: str | None = None, use_solaredge_one: bool = True
    ):
        self._use_solaredge_one = bool(use_solaredge_one)
        self._one = solaredge_one(siteid, username, password, timezone, language)
        self._legacy = solaredgeoptimizers(siteid, username, password, timezone, language)
        self._last_used_api: str | None = "legacy" if not self._use_solaredge_one else None  # "one" or "legacy"
        self._obtained_from: str = OBTAINED_FROM_LEGACY if not self._use_solaredge_one else OBTAINED_FROM_ONE
        self._closed = False
        # When legacy-only, hide batch API so coordinator uses single-optimizer light check and 2h stale threshold
        if not self._use_solaredge_one:
            self.requestSystemDataBatch = None  # type: ignore[assignment]

    def check_login(self) -> int:
        """Return 200 if either backend authenticates, else best-available status code."""
        if not self._use_solaredge_one:
            return self._legacy.check_login()
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
        # Prefer explicit auth failure when either backend returned it.
        if code_one == 401 or code_legacy == 401:
            return 401
        # Otherwise return the first meaningful non-zero status, falling back to 0.
        return code_one or code_legacy or 0

    def verify_authentication(self) -> None:
        """Raise SolarEdgeAuthError when stored credentials are rejected (HTTP 401)."""
        if self.check_login() == 401:
            raise SolarEdgeAuthError("Invalid or expired SolarEdge credentials")

    def requestListOfAllPanels(self) -> Any:
        """Prefer One for layout (unless use_solaredge_one is False); fall back to legacy on failure."""
        if not self._use_solaredge_one:
            return self._legacy.requestListOfAllPanels()
        try:
            return self._one.requestListOfAllPanels()
        except Exception as e:  # pylint: disable=broad-except
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
        When use_solaredge_one is False, always use legacy. Otherwise try One first;
        if One returns no valid measurements or fails, use legacy and set _obtained_from to Legacy API.
        """
        previous_api = self._last_used_api
        if not self._use_solaredge_one:
            data_list = self._legacy.requestAllData()
            self._last_used_api = "legacy"
            self._obtained_from = OBTAINED_FROM_LEGACY
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("SolarEdge Dual API: Using Legacy API only (use_solaredge_one=False)")
            return data_list or []

        self._obtained_from = OBTAINED_FROM_ONE
        data_list = None
        one_error: BaseException | None = None
        try:
            data_list = self._one.requestAllData()
        except Exception as e:  # pylint: disable=broad-except
            one_error = e
            _LOGGER.warning(
                "SolarEdge Dual API: One requestAllData failed (%s), falling back to legacy",
                e,
            )
        if data_list is not None and self._one_has_valid_measurements(data_list):
            self._last_used_api = "one"
            self._obtained_from = OBTAINED_FROM_ONE
            if previous_api == "legacy":
                _LOGGER.info("SolarEdge Dual API: Switched back to One API data")
            elif _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("SolarEdge Dual API: Using One API data")
            return data_list
        # One unavailable or no valid measurements: use legacy
        try:
            data_list = self._legacy.requestAllData()
            self._last_used_api = "legacy"
            self._obtained_from = OBTAINED_FROM_LEGACY
            if previous_api != "legacy":
                _LOGGER.info(
                    "SolarEdge Dual API: Using Legacy API data (One had no valid measurements or failed)"
                )
            elif _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "SolarEdge Dual API: Continuing to use Legacy API data "
                    "(One had no valid measurements or failed)"
                )
            return data_list or []
        except Exception as e:  # pylint: disable=broad-except
            _LOGGER.error("SolarEdge Dual API: Legacy requestAllData also failed: %s", e)
            if _is_auth_related_error(e) or (one_error is not None and _is_auth_related_error(one_error)):
                self.verify_authentication()
            # Return One result if we have it (even if invalid) so UI doesn't break
            if data_list is not None:
                self._last_used_api = "one"
                self._obtained_from = OBTAINED_FROM_ONE
                return data_list
            raise

    def get_lifetime_energy_cached(self) -> dict[str, Any]:
        """Return lifetime energy from the API that was last used for requestAllData."""
        if not self._use_solaredge_one or self._last_used_api == "legacy":
            return self._legacy.get_lifetime_energy_cached()
        return self._one.get_lifetime_energy_cached()

    def requestSystemData(self, item_id: str) -> Any:
        """Delegate to the API we last used for full data (for lightweight check in legacy mode)."""
        if not self._use_solaredge_one or self._last_used_api == "legacy":
            return self._legacy.requestSystemData(item_id)
        return self._one.requestSystemData(item_id)

    def requestSystemDataBatch(self, item_ids: list) -> list[Any]:
        """Use One for batch (only One supports it); used to detect when One has new data. Set to None when use_solaredge_one is False."""
        return self._one.requestSystemDataBatch(item_ids)

    def get_inverter_models(self, serials: list) -> dict[str, str]:
        """Only One provides inverter models; when use_solaredge_one is False return empty."""
        if not self._use_solaredge_one:
            return {}
        return self._one.get_inverter_models(serials)

    def get_site_info_cached(self) -> dict:
        """Site info (installation date, peak power) from One API; when legacy-only return empty."""
        if not self._use_solaredge_one:
            return {}
        return self._one.get_site_info_cached()

    def get_dashboard_site_production_cached(self, installation_date: str | None) -> float | None:
        """Site lifetime production (Wh) from dashboard/energy; when legacy-only return None."""
        if not self._use_solaredge_one:
            return None
        return self._one.get_dashboard_site_production_cached(installation_date)

    def get_layout_energy_by_inverter_cached(self, installation_date: str | None) -> dict:
        """Inverter/string lifetime energy from layout/energy by-inverter; when legacy-only return empty."""
        if not self._use_solaredge_one:
            return {}
        return self._one.get_layout_energy_by_inverter_cached(installation_date)

    def close(self) -> None:
        """Close both API clients and release all file descriptors (sessions, connection pools).
        Idempotent; safe to call multiple times. Both clients are always closed even if one raises;
        the second client is still closed after the first raises to avoid leaking its descriptors.
        """
        if self._closed:
            return
        self._closed = True
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("SolarEdge Dual API: Closing both API clients (One and Legacy)")
        last_error = None
        for name, client in [("One", self._one), ("Legacy", self._legacy)]:
            try:
                client.close(log_summary=False)
                if _LOGGER.isEnabledFor(logging.DEBUG):
                    _LOGGER.debug("SolarEdge Dual API: Closed %s API client", name)
            except Exception as e:  # pylint: disable=broad-except
                last_error = e
                _LOGGER.warning(
                    "SolarEdge Dual API: Error closing %s API (file descriptors may leak): %s",
                    name,
                    e,
                )
        if last_error is None:
            _LOGGER.info("SolarEdge Dual API: Closed One and legacy API clients (file descriptors released)")
        else:
            _LOGGER.warning(
                "SolarEdge Dual API: At least one client failed to close (%s); "
                "the other backend was still closed — reload the integration if sessions linger",
                last_error,
            )

    def __del__(self) -> None:
        """Best-effort cleanup if GC collects an unclosed client."""
        try:
            self.close()
        except Exception as exc:  # pylint: disable=broad-except
            # Never raise from destructor, but leave a debug breadcrumb.
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("SolarEdge Dual API: Ignoring close error in __del__: %s", exc)
