"""
SolarEdge Optimizers Integration - Legacy Monitoring API Client (solaredgeoptimizers.py)

This module implements the client for the legacy SolarEdge Monitoring Portal API. It serves
as a fallback when the SolarEdge One API is unavailable or returns no valid data.

Authentication:
- Uses HTTP Basic Authentication with site credentials
- Session-based with CSRF token handling for subsequent requests
- Legacy session bootstrap falls back to ``/solaredge-web/p/logout/slo`` then
  ``/solaredge-web/p/login`` when SolarEdge stops issuing ``CSRF-TOKEN`` from
  the login page alone
- Logs legacy HTTP ``498`` responses explicitly as likely rejected/missing/expired
  CSRF token or legacy web-session failures
- Thread-local sessions for safe concurrent access in ThreadPoolExecutor

API Endpoints (monitoring.solaredge.com/solaredge-apigw/api/...):

1. Login Check:
   GET .../sites/{siteId}/layout/logical
   Validates credentials and returns HTTP status code

2. Site Structure (Logical Layout):
   GET .../sites/{siteId}/layout/logical
   Returns JSON with inverters, strings, and optimizers hierarchy

3. Optimizer Live Data (System Data):
   GET .../solaredge-web/p/systemData?reporterId={optimizerId}&...
   Returns power, voltage, current, optimizer voltage for one optimizer
   Locale-aware measurement keys (supports multiple languages)

4. Lifetime Energy:
   POST .../sites/{siteId}/layout/energy?timeUnit=ALL
   Returns lifetime energy data keyed by optimizer/string ID

5. Historical Data:
   GET .../solaredge-web/p/chartData?reporterId={id}&...
   Returns time-series data for power, current, voltage, energy

Data Classes Defined:
- SolarEdgeSite: Root container with site ID and list of inverters
- SolarEdgeInverter: Inverter with serial, name, status, maxActivePower (kW when from One API), and list of strings
- SolarEdgeString: String with ID, name, status, and list of optimizers
- SolarlEdgeOptimizer: Optimizer with ID, serial, name, display name, status
- SolarEdgeOptimizerData: Live measurement data (power, voltage, current, energy, etc.)
- SolarEdgeAggregatedData: Aggregated data for string/inverter/site levels; site level has
  installation_date and peak_power when provided by One API (layout/information/site);
  inverter level has max_active_power (kW) when provided by One API (layout logical v2).

Key Features:
- Locale-aware measurement key parsing (supports EN, DE, FR, ES, IT, NL, etc.)
- Thread-local session reuse for efficient parallel requests; each HTTP call uses
  ``with session.request(...)`` so the response is closed; close() closes all tracked
  sessions to avoid leaking file descriptors when the integration is unloaded or removed.
- Caching for panels (PANELS_CACHE_TTL_LEGACY) and lifetime energy (LIFETIME_ENERGY_CACHE_TTL)
- Unicode normalization for measurement keys (handles various dash/space variants)
- Timezone-aware date parsing for lastMeasurementDate
"""
import time
import threading
import re
import os

import requests
import json
import logging
import pytz
from concurrent.futures import ThreadPoolExecutor, as_completed

from requests import Session
from datetime import datetime, timedelta
from jsonfinder import jsonfinder

from .const import (
    API_TIMEOUT_SHORT,
    API_TIMEOUT_LONG,
    MAX_PARALLEL_WORKERS,
    USER_AGENT,
    PANELS_CACHE_TTL_LEGACY,
    LIFETIME_ENERGY_CACHE_TTL,
    MEASUREMENT_KEYS,
)
from .exceptions import SolarEdgeAPIError

# Added logger setup to replace print statements with proper logging
_LOGGER = logging.getLogger(__name__)


def _normalize_measurement_key(key):
    """Normalize measurement key so API keys with Unicode variants (dash, space) match our key list."""
    if not key or not isinstance(key, str):
        return key
    # Replace common Unicode variants with ASCII so API keys match MEASUREMENT_KEYS
    key = key.replace("\u2013", "-").replace("\u2014", "-").replace("\u2212", "-")  # en/em dash, minus
    key = key.replace("\u2010", "-").replace("\u2011", "-")  # hyphen, non-breaking hyphen
    key = key.replace("\u00a0", " ")  # non-breaking space
    return key.strip()


def _get_measurement_value(measurements, key_list):
    """Return the first value found for any of the given keys. Used for locale-independent API parsing.
    Keys are normalized so API responses with Unicode variants (e.g. de_DE returning different hyphen)
    still match our known key names."""
    if not measurements or not isinstance(measurements, dict):
        return None
    # Build normalized key -> value mapping so locale/Unicode variants match
    norm_to_value = {}
    for k, v in measurements.items():
        norm_to_value[_normalize_measurement_key(k)] = v
    for key in key_list:
        norm_key = _normalize_measurement_key(key)
        if norm_key in norm_to_value:
            return norm_to_value[norm_key]
    return None


def _lifetime_energy_to_kwh(energy_data):
    """Convert layout/energy API entry to kWh.

    Uses unscaledEnergy (always in Wh) so lifetime energy updates correctly.
    The 'units' field applies only to the display values 'energy' and 'moduleEnergy';
    unscaledEnergy is the raw accumulating value in Wh.
    """
    if not energy_data or not isinstance(energy_data, dict):
        return None
    try:
        raw = energy_data.get("unscaledEnergy")
        if raw is not None:
            return round(float(raw) / 1000.0, 3)  # Wh -> kWh
        # Fallback if API omits unscaledEnergy: derive from energy + units
        units = energy_data.get("units") or "Wh"
        energy = energy_data.get("energy")
        if energy is None:
            return None
        energy = float(energy)
        if units == "kWh":
            return round(energy, 3)
        if units == "MWh":
            return round(energy * 1000.0, 3)
        # Wh
        return round(energy / 1000.0, 3)
    except (TypeError, ValueError):
        pass
    return None


def _parse_system_data_json(json_object, item_id, timezone):
    """Parse and validate systemData JSON into SolarEdgeOptimizerData or None.
    Handles list/dict types, missing lastMeasurementDate, and KeyError.
    """
    if isinstance(json_object, list):
        if len(json_object) == 0:
            _LOGGER.warning("Empty list returned for optimizer %s", item_id)
            return None
        json_object = json_object[0]
    if not isinstance(json_object, dict):
        _LOGGER.error("Unexpected data type returned for optimizer %s: %s", item_id, type(json_object))
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("SolarEdge Optimizers (legacy): Response data: %s", json_object)
        return None
    if json_object.get("lastMeasurementDate") == "":
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("SolarEdge Optimizers (legacy): Skipping optimizer %s without measurements", item_id)
        return None
    try:
        return SolarEdgeOptimizerData(item_id, json_object, timezone)
    except KeyError as e:
        _LOGGER.error("Missing expected key in response for optimizer %s: %s", item_id, e)
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("SolarEdge Optimizers (legacy): Response data: %s", json_object)
        return None
    except Exception as e:  # pylint: disable=broad-except
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("SolarEdge Optimizers (legacy): Response data: %s", json_object)
        raise SolarEdgeAPIError("Error while processing data") from e


def _apply_lifetime_energy_to_optimizer_info(info, optimizer_id, lifetimeenergy):
    """Set lifetime_energy on optimizer info from lifetimeenergy dict (by string or int key)."""
    optimizer_id_str = str(optimizer_id)
    energy_data = lifetimeenergy.get(optimizer_id_str) or lifetimeenergy.get(optimizer_id) or {}
    kWh = _lifetime_energy_to_kwh(energy_data)
    if kWh is not None:
        info.lifetime_energy = kWh
    else:
        _LOGGER.warning("Lifetime energy data missing for optimizer %s, setting to 0", optimizer_id)
        info.lifetime_energy = 0.0


def _raise_for_system_data_http_error(response, item_id):
    """Raise an appropriate exception for non-200 systemData HTTP response."""
    if 500 <= response.status_code < 600:
        _LOGGER.warning(
            "Temporary server error from SolarEdge (HTTP %s). Will retry on next update.",
            response.status_code,
        )
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("Server error response body for optimizer %s: %s", item_id, response.text)
        raise SolarEdgeAPIError(f"Temporary server error from SolarEdge (HTTP {response.status_code})")
    _LOGGER.error("Error sending request to SolarEdge. Status code: %s", response.status_code)
    if _LOGGER.isEnabledFor(logging.DEBUG):
        _LOGGER.debug("Error response body for optimizer %s: %s", item_id, response.text)
    raise SolarEdgeAPIError(f"Problem sending request to SolarEdge (HTTP {response.status_code})")


def _safe_float(value, default=0.0):
    """Safely convert value to float, handling None, empty strings, comma decimals, and invalid types."""
    if value is None or value == "":
        return default
    if isinstance(value, str):
        value = value.replace(",", ".")
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def _parse_last_measurement_date(rawdate, panelid, timezone):
    """Parse lastMeasurementDate string (ISO or SolarEdge format) to timezone-aware UTC datetime.
    Returns None when rawdate is empty (e.g. inactive optimizers with no lastMeasurement from API).
    """
    if not rawdate:
        return None
    try:
        if "T" in rawdate and ("Z" in rawdate or "+" in rawdate or (len(rawdate) >= 6 and rawdate[-6] in "+-")):
            iso_str = rawdate.strip().replace("Z", "+00:00")
            return datetime.fromisoformat(iso_str).astimezone(pytz.UTC)
        raise ValueError("Not ISO format")
    except (ValueError, TypeError):
        pass
    try:
        date_str = re.sub(r'\s+(?:GMT|UTC|EST|CST|PST|EDT|CDT|PDT|[A-Z]{3})\s+', ' ', rawdate)
        naive_dt = datetime.strptime(date_str.strip(), "%a %b %d %H:%M:%S %Y")
        if naive_dt.tzinfo is None:
            local_dt = timezone.localize(naive_dt) if hasattr(timezone, 'localize') else naive_dt.replace(tzinfo=timezone)
        else:
            local_dt = naive_dt
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "Timezone conversion for optimizer %s: raw='%s' | cleaned='%s' | naive=%s | local=%s (%s) | UTC=%s",
                panelid, rawdate, date_str.strip(), naive_dt, local_dt, str(timezone), local_dt.astimezone(pytz.UTC)
            )
        return local_dt.astimezone(pytz.UTC)
    except (ValueError, IndexError) as e:
        _LOGGER.error("Failed to parse date '%s' for optimizer %s: %s", rawdate, panelid, e)
        return datetime.now(pytz.UTC)


def _normalize_measurements_dict(json_object, panelid):
    """Extract and validate measurements dict from optimizer json_object; return {} if invalid."""
    measurements = json_object.get("measurements", {})
    if not measurements or not isinstance(measurements, dict):
        available_keys = list(json_object.keys()) if _LOGGER.isEnabledFor(logging.WARNING) and isinstance(json_object, dict) else "N/A"
        _LOGGER.warning(
            "Missing or invalid measurements for optimizer %s (panel_id: %s). Available keys: %s",
            panelid, json_object.get("serialNumber", "unknown"), available_keys
        )
        return {}
    return measurements


def _apply_measurements_to_optimizer_data(instance, measurements, json_object, panelid):
    """Set current, optimizer_voltage, power, voltage from measurements and log if all zero."""
    instance.current = _safe_float(_get_measurement_value(measurements, MEASUREMENT_KEYS["current"]), 0.0)
    instance.optimizer_voltage = round(_safe_float(_get_measurement_value(measurements, MEASUREMENT_KEYS["optimizer_voltage"]), 0.0), 2)
    instance.power = round(_safe_float(_get_measurement_value(measurements, MEASUREMENT_KEYS["power"]), 0.0), 2)
    instance.voltage = round(_safe_float(_get_measurement_value(measurements, MEASUREMENT_KEYS["voltage"]), 0.0), 2)
    if instance.current == 0.0 and instance.power == 0.0 and instance.voltage == 0.0 and instance.optimizer_voltage == 0.0:
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "All measurements are zero for optimizer %s (serial: %s). Measurements dict: %s",
                panelid, json_object.get("serialNumber", "unknown"), measurements,
            )


def _site_lifetime_kwh_from_layout_energy(lifetime_energy_data):
    """Compute site total lifetime energy (kWh) from layout/energy API response.

    Sums unscaledEnergy (Wh) across all entries in the response. Works when the
    response contains per-optimizer data (sum = site total) or a mix of panel and
    inverter/string entries (inverter entries dominate; panel values are negligible).
    """
    if not lifetime_energy_data or not isinstance(lifetime_energy_data, dict):
        return None
    total_wh = 0.0
    for _key, entry in lifetime_energy_data.items():
        if not isinstance(entry, dict):
            continue
        raw = entry.get("unscaledEnergy")
        if raw is not None:
            try:
                total_wh += float(raw)
            except (TypeError, ValueError):
                pass
    if total_wh == 0.0 and lifetime_energy_data:
        return None
    return round(total_wh / 1000.0, 3)


class solaredgeoptimizers:
    def __init__(self, siteid, username, password, timezone=None, language=None):
        self.siteid = siteid
        self.username = username
        self.password = password
        # Store timezone for date parsing (default to UTC if not provided)
        self._timezone = timezone if timezone is not None else pytz.UTC
        # Language for API locale/accept-language (e.g. "en", "de"); default "en"
        self._language = (language or "en").split("-")[0].lower()
        # Map HA language code to SolarEdge locale (language_COUNTRY)
        self._locale_map = {
            "en": "en_US", "nl": "nl_NL", "de": "de_DE", "fr": "fr_FR",
            "es": "es_ES", "it": "it_IT", "pl": "pl_PL", "pt": "pt_PT",
            "sv": "sv_SE", "cs": "cs_CZ", "tr": "tr_TR", "el": "el_GR",
            "hu": "hu_HU", "ru": "ru_RU", "zh": "zh_CN", "ja": "ja_JP",
            "da": "da_DK", "nb": "nb_NO", "fi": "fi_FI",
        }
        # Thread-local storage for session reuse (one session per thread)
        self._thread_local = threading.local()
        # Track all sessions so we can close them on unload (thread pool may create many)
        self._all_sessions: set = set()
        self._sessions_lock = threading.Lock()
        self._closed = False
        # Cache for requestListOfAllPanels() result
        self._panels_cache = None
        self._panels_cache_time = None
        self._panels_cache_ttl = PANELS_CACHE_TTL_LEGACY
        # Cache for lifetime energy data (changes slowly)
        self._lifetime_energy_cache = None
        self._lifetime_energy_cache_time = None
        self._lifetime_energy_cache_ttl = LIFETIME_ENERGY_CACHE_TTL

    def _locale_from_language(self):
        """Return SolarEdge locale string for the configured language."""
        return self._locale_map.get(self._language, "en_US")

    def _accept_language_header(self):
        """Return Accept-Language header value for the configured language."""
        locale = self._locale_from_language()
        primary = locale.replace("_", "-")
        return f"{primary},{self._language};q=0.9,en;q=0.8"

    def get_lifetime_energy_cached(self):
        """Return cached lifetime energy data as dict (refresh at most hourly)."""
        now = datetime.now()
        if (
            self._lifetime_energy_cache is None
            or self._lifetime_energy_cache_time is None
            or (now - self._lifetime_energy_cache_time) > self._lifetime_energy_cache_ttl
        ):
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "SolarEdge Optimizers (legacy): Lifetime energy cache miss (TTL=%s), fetching",
                    self._lifetime_energy_cache_ttl,
                )
            try:
                lifetime_energy_response = self.getLifeTimeEnergy()
                if lifetime_energy_response.startswith("ERROR001"):
                    _LOGGER.error("Failed to get lifetime energy data: %s", lifetime_energy_response)
                    self._lifetime_energy_cache = {}
                else:
                    try:
                        self._lifetime_energy_cache = json.loads(lifetime_energy_response)
                    except json.JSONDecodeError as e:
                        _LOGGER.error("Failed to parse lifetime energy JSON: %s", e)
                        self._lifetime_energy_cache = {}
                self._lifetime_energy_cache_time = now
                if _LOGGER.isEnabledFor(logging.DEBUG):
                    _LOGGER.debug(
                        "SolarEdge Optimizers (legacy): Refreshed lifetime energy cache (%d entries)",
                        len(self._lifetime_energy_cache) if isinstance(self._lifetime_energy_cache, dict) else 0,
                    )
                    _LOGGER.debug(
                        "SolarEdge Optimizers (legacy): Decoded lifetime energy data (by optimizer/string ID): %s",
                        self._lifetime_energy_cache,
                    )
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                # Transient DNS/network errors: keep previous cache, do not update cache time
                _LOGGER.warning(
                    "SolarEdge API unreachable (lifetime energy): %s. Using cached data if available.",
                    e,
                )
                if self._lifetime_energy_cache is None:
                    self._lifetime_energy_cache = {}
        else:
            if _LOGGER.isEnabledFor(logging.DEBUG):
                cache = self._lifetime_energy_cache or {}
                age = now - self._lifetime_energy_cache_time if self._lifetime_energy_cache_time else None
                _LOGGER.debug(
                    "SolarEdge Optimizers (legacy): Using cached lifetime energy (age=%s, TTL=%s, %d entries)",
                    age,
                    self._lifetime_energy_cache_ttl,
                    len(cache) if isinstance(cache, dict) else 0,
                )
        return self._lifetime_energy_cache or {}

    def check_login(self):
        url = f"https://monitoring.solaredge.com/solaredge-apigw/api/sites/{self.siteid}/layout/logical"
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("SolarEdge Optimizers (legacy): Login check URL: %s", url)

        kwargs = {}
        kwargs["auth"] = requests.auth.HTTPBasicAuth(self.username, self.password)
        kwargs["headers"] = {"user-agent": USER_AGENT}
        kwargs["timeout"] = API_TIMEOUT_SHORT
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "SolarEdge Optimizers (legacy): Making login check request (timeout=%ss)",
                API_TIMEOUT_SHORT,
            )

        try:
            with requests.get(url, **kwargs) as r:
                if _LOGGER.isEnabledFor(logging.DEBUG):
                    _LOGGER.debug("SolarEdge Optimizers (legacy): Login check status: %s", r.status_code)
                    _LOGGER.debug("SolarEdge Optimizers (legacy): Login check response headers: %s", dict(r.headers))
                    _LOGGER.debug("SolarEdge Optimizers (legacy): Login check response body length: %s bytes", len(r.text))
                return r.status_code
        except requests.exceptions.Timeout as e:
            _LOGGER.error(
                "SolarEdge Optimizers: Login check timed out (timeout=%ss): %s",
                API_TIMEOUT_SHORT,
                e,
            )
            raise
        except requests.exceptions.ConnectionError as e:
            _LOGGER.error("SolarEdge Optimizers: Login check connection error: %s", e)
            raise
        except requests.exceptions.RequestException as e:
            _LOGGER.error("SolarEdge Optimizers: Login check request error: %s", e)
            raise
        except Exception as e:  # pylint: disable=broad-except
            _LOGGER.error("SolarEdge Optimizers: Login check unexpected error: %s", e)
            raise

    def _fetch_logical_layout(self, url: str, kwargs: dict) -> str:
        """Perform GET for logical layout; return response text. Caller handles exceptions."""
        with requests.get(url, **kwargs) as r:
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("SolarEdge Optimizers (legacy): Logical layout URL: %s", url)
                _LOGGER.debug("SolarEdge Optimizers (legacy): Logical layout status: %s", r.status_code)
                _LOGGER.debug("SolarEdge Optimizers (legacy): Logical layout response headers: %s", dict(r.headers))
                _LOGGER.debug(
                    "SolarEdge Optimizers (legacy): requestLogicalLayout response (status %s): %s",
                    r.status_code,
                    r.text[:2000] if len(r.text) > 2000 else r.text,
                )
            return r.text

    def _log_layout_request_error(self, e: Exception) -> None:
        """Log logical layout request error by exception type."""
        if isinstance(e, requests.exceptions.Timeout):
            _LOGGER.error(
                "SolarEdge Optimizers: Logical layout request timed out (timeout=%ss): %s",
                API_TIMEOUT_LONG,
                e,
            )
        elif isinstance(e, requests.exceptions.ConnectionError):
            _LOGGER.error("SolarEdge Optimizers: Logical layout connection error: %s", e)
        elif isinstance(e, requests.exceptions.RequestException):
            _LOGGER.error("SolarEdge Optimizers: Logical layout request error: %s", e)
        else:
            _LOGGER.error("SolarEdge Optimizers: Logical layout unexpected error: %s", e)

    def requestLogicalLayout(self):
        """Request logical layout JSON for the site. Returns raw response text."""
        url = f"https://monitoring.solaredge.com/solaredge-apigw/api/sites/{self.siteid}/layout/logical"
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("SolarEdge Optimizers (legacy): requestLogicalLayout URL: %s", url)
            _LOGGER.debug(
                "SolarEdge Optimizers (legacy): Making logical layout request (timeout=%ss)",
                API_TIMEOUT_LONG,
            )
        kwargs = {
            "auth": requests.auth.HTTPBasicAuth(self.username, self.password),
            "timeout": API_TIMEOUT_LONG,
        }
        try:
            return self._fetch_logical_layout(url, kwargs)
        except Exception as e:  # pylint: disable=broad-except
            self._log_layout_request_error(e)
            raise

    def _parse_and_cache_layout(self, raw_layout: str, now: datetime):
        """Parse layout JSON, update cache, return SolarEdgeSite. Raises on parse error."""
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("SolarEdge Optimizers (legacy): Received raw layout data, parsing JSON")
        json_obj = json.loads(raw_layout)
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("SolarEdge Optimizers (legacy): Parsed logical layout JSON: %s", json_obj)
            _LOGGER.debug("SolarEdge Optimizers (legacy): Creating SolarEdgeSite object")
        self._panels_cache = SolarEdgeSite(json_obj)
        self._panels_cache_time = now
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "SolarEdge Optimizers (legacy): Refreshed panels cache with %s optimizers",
                self._panels_cache.returnNumberOfOptimizers(),
            )
        return self._panels_cache

    def requestListOfAllPanels(self):
        """Return site layout (SolarEdgeSite). Uses cache when valid."""
        now = datetime.now()
        if (
            self._panels_cache is None
            or self._panels_cache_time is None
            or (now - self._panels_cache_time) > self._panels_cache_ttl
        ):
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "SolarEdge Optimizers (legacy): Panels cache miss (TTL=%s), fetching fresh layout data",
                    self._panels_cache_ttl,
                )
            try:
                raw_layout = self.requestLogicalLayout()
                return self._parse_and_cache_layout(raw_layout, now)
            except json.JSONDecodeError as e:
                _LOGGER.error("SolarEdge Optimizers: Failed to parse layout JSON: %s", e)
                if _LOGGER.isEnabledFor(logging.DEBUG):
                    _LOGGER.debug(
                        "SolarEdge Optimizers: Raw layout data: %s",
                        raw_layout[:1000] if len(raw_layout) > 1000 else raw_layout,
                    )
                raise
            except Exception as e:  # pylint: disable=broad-except
                _LOGGER.error("SolarEdge Optimizers: Unexpected error in requestListOfAllPanels: %s", e)
                raise
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "SolarEdge Optimizers (legacy): Using cached panels (age=%s, TTL=%s)",
                now - self._panels_cache_time,
                self._panels_cache_ttl,
            )
        return self._panels_cache

    def requestSystemData(self, itemId):
        # Fixed endpoint URL - changed from monitoringpublic.solaredge.com/publicSystemData to monitoring.solaredge.com/systemData,
        # changed isPublic=true to false, added locale parameter, and added v parameter with timestamp
        locale = self._locale_from_language()
        base = "https://monitoring.solaredge.com/solaredge-web/p/systemData"
        params = f"reporterId={itemId}&type=panel&activeTab=0&fieldId={self.siteid}&isPublic=false&locale={locale}&v={round(time.time() * 1000)}"
        url = f"{base}?{params}"

        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("Endpoint (single optimizer systemData): %s", url)

        kwargs = {
            "auth": requests.auth.HTTPBasicAuth(self.username, self.password),
            "timeout": API_TIMEOUT_SHORT,
        }
        with requests.get(url, **kwargs) as r:
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("Response from systemData (optimizer %s, status %s)", itemId, r.status_code)
            if r.status_code != 200:
                _raise_for_system_data_http_error(r, itemId)
            json_object = self.decodeResult(r.text)
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("Decoded JSON object for optimizer %s: %s", itemId, json_object)
            return _parse_system_data_json(json_object, itemId, self._timezone)

    def _parse_lifetime_energy_response(self, response: str) -> dict:
        """Parse getLifeTimeEnergy response string to dict. Returns {} on error or ERROR001."""
        if response.startswith("ERROR001"):
            _LOGGER.error("Failed to get lifetime energy data: %s", response)
            return {}
        try:
            data = json.loads(response)
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("Parsed lifetime energy data (by optimizer/string ID): %s", data)
            return data
        except json.JSONDecodeError as e:
            _LOGGER.error("Failed to parse lifetime energy JSON: %s", e)
            return {}

    def _get_cached_lifetime_energy(self):
        """Return lifetime energy dict, from cache or by fetching and parsing getLifeTimeEnergy()."""
        now = datetime.now()
        if (
            self._lifetime_energy_cache is None
            or self._lifetime_energy_cache_time is None
            or (now - self._lifetime_energy_cache_time) > self._lifetime_energy_cache_ttl
        ):
            response = self.getLifeTimeEnergy()
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "Response from lifetime energy endpoint: %s",
                    response[:2000] if len(response) > 2000 else response,
                )
            lifetimeenergy = self._parse_lifetime_energy_response(response)
            self._lifetime_energy_cache = lifetimeenergy
            self._lifetime_energy_cache_time = now
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("Refreshed lifetime energy cache")
        else:
            lifetimeenergy = self._lifetime_energy_cache
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("Using cached lifetime energy data")
        return lifetimeenergy

    def requestAllData(self):
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("SolarEdge Optimizers (legacy): requestAllData starting")
        solarsite = self.requestListOfAllPanels()
        lifetimeenergy = self._get_cached_lifetime_energy()

        optimizer_ids = [
            optimizer.optimizerId
            for inverter in solarsite.inverters
            for string in inverter.strings
            for optimizer in string.optimizers
        ]

        max_workers = min(os.cpu_count() or 4, len(optimizer_ids), MAX_PARALLEL_WORKERS)
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "SolarEdge Optimizers (legacy): requestAllData fetching %d optimizers with max_workers=%d",
                len(optimizer_ids), max_workers,
            )
        data = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_id = {executor.submit(self.requestSystemData, opt_id): opt_id for opt_id in optimizer_ids}
            for future in as_completed(future_to_id):
                optimizer_id = future_to_id[future]
                try:
                    info = future.result()
                    if info is not None:
                        _apply_lifetime_energy_to_optimizer_info(info, optimizer_id, lifetimeenergy)
                        data.append(info)
                except Exception as e:  # pylint: disable=broad-except
                    _LOGGER.error("Error fetching data for optimizer %s: %s", optimizer_id, e)

        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "SolarEdge Optimizers (legacy): requestAllData complete, %d optimizers with data",
                len(data),
            )
        return data

    def requestItemHistory(self, itemId, starttime=None, endtime=None, parameter="Power"):
        """
        Request measurement history of a panel given a time window defined by start- and endtime
        :param itemId: itemId of the item (panel, string, inverter)
        :param starttime: starttime as datetime or unix timestamp in ms, or None for start of today
        :param endtime: endtime as datetime or unix timestamp in ms, or None for 24 hour after starttime
        :param parameter: the measurement parameter to return
            a list of available parameters can be obtained using: https://monitoring.solaredge.com/solaredge-web/p/chartParamsList?fieldId={}reporterId={}&format=form
        :return: dictionary with datetime (keys), value (values) pairs
            Note, time resolution of the result depends on the time range spanned by start- and endtime
        """
        if starttime is None:
            now = datetime.now()
            starttime = datetime(now.year, now.month, now.day)
        if isinstance(starttime, datetime):
            starttime = int(starttime.timestamp() * 1000)
        if endtime is None:
            endtime = int(starttime + timedelta(days=1).total_seconds() * 1000)
        if isinstance(endtime, datetime):
            endtime = int(endtime.timestamp() * 1000)

        # Use f-string instead of .format() for better performance
        base = "https://monitoring.solaredge.com/solaredge-web/p/chartData"
        q = f"reporterId={itemId}&fieldId={self.siteid}&reporterType=&startDate={starttime:d}&endDate={endtime:d}&uom=W&parameterName={parameter}"
        url = f"{base}?{q}"

        r = self._doRequestWithCooldown("GET", url)
        if r.startswith("ERROR001"):
            raise SolarEdgeAPIError(f"Error while doing request: {r}")

        json_object = self.decodeResult(r)
        try:
            # Note: the timestamp provided by SolarEdge is not a pure POSIX timestamp, but in fact contains a timezone offset.
            return {
                datetime.utcfromtimestamp(pair['date'] / 1000).astimezone(pytz.utc): pair['value']
                for pair in json_object['dateValuePairs']
            }
        except Exception as e:  # pylint: disable=broad-except
            raise SolarEdgeAPIError("Error while processing data") from e

    def requestPanelHistory(self, itemId, starttime=None, endtime=None, parameter="Power"):
        allowed = ("Power", "Current", "Voltage", "Energy", "PowerBox Voltage")
        if parameter not in allowed:
            raise ValueError(f"parameter must be one of {allowed}, got {parameter!r}")
        return self.requestItemHistory(itemId, starttime=starttime, endtime=endtime, parameter=parameter)

    def requestStringHistory(self, itemId, starttime=None, endtime=None, parameter="Power"):
        if parameter not in ("Energy", "Power"):
            raise ValueError("parameter must be 'Energy' or 'Power'")
        return self.requestItemHistory(itemId, starttime=starttime, endtime=endtime, parameter=parameter)

    def requestInverterHistory(self, itemId, starttime=None, endtime=None, parameter="Power"):
        # https://monitoring.solaredge.com/solaredge-web/p/chartParamsList?fieldId={}reporterId={}&format=form
        allowed = (
            "AC Energy", "AC Frequency", "AC Frequency P2", "AC Frequency P3",
            "AC Voltage", "AC Voltage P2", "AC Voltage P3",
            "AC Current", "AC Current P2", "AC Current P3",
            "Power", "DC Voltage", "Purchased back feed AC Energy", "Total Reactive Power", "Power Factor",
        )
        if parameter not in allowed:
            raise ValueError(f"parameter must be one of {allowed}, got {parameter!r}")
        return self.requestItemHistory(itemId, starttime=starttime, endtime=endtime, parameter=parameter)

    def requestHistoricalData(self, starttime=None, endtime=None, type="optimizer", parameter="Power"):
        if type not in ("optimizer", "inverter", "string"):
            raise ValueError("type must be 'optimizer', 'inverter', or 'string'")

        solarsite = self.requestListOfAllPanels()

        data = {}
        for inverter in solarsite.inverters:
            if "inverter" in type:
                info = self.requestInverterHistory(inverter.inverterId, starttime, endtime, parameter)
                data[inverter] = info
            for string in inverter.strings:
                if "string" in type:
                    info = self.requestStringHistory(string.stringId, starttime, endtime, parameter)
                    data[string] = info
                for optimizer in string.optimizers:
                    if "optimizer" in type:
                        info = self.requestPanelHistory(optimizer.optimizerId, starttime, endtime, parameter)
                        data[optimizer] = info

        return data

    def _doRequestWithCooldown(self, method, request_url, data=None, wait_sec=0.1, cooldown_sec=5, n_retries=3):
        """
        Same as _doRequest, but waiting before each call, and in between retries in case it fails
        """
        # Use f-string instead of % formatting for better performance
        last_error = SolarEdgeAPIError(f"Could not perform request within {n_retries} retries")
        for i in range(n_retries):
            try:
                time.sleep(wait_sec)
                res = self._doRequest(method=method, request_url=request_url, data=data)
                return res
            except ConnectionError as e:
                if isinstance(e.args[0], Exception) and len(e.args[0].args) > 1 and \
                        isinstance(e.args[0].args[1], ConnectionResetError) and e.args[0].args[1].errno == 10054:
                    last_error = e
                    time.sleep(cooldown_sec)
                    continue
                raise
        raise last_error

    def _prime_session_cookies(self, session: Session):
        """Prime legacy session cookies and recover when CSRF moves off /p/login.

        SolarEdge changed the monitoring portal in May 2026 so some accounts no
        longer receive ``CSRF-TOKEN`` from ``/solaredge-web/p/login``. Keep the
        historical bootstrap for compatibility, then fall back to the reported
        ``/solaredge-web/p/logout/slo`` -> ``/solaredge-web/p/login`` sequence
        when the CSRF cookie is still missing.
        """
        with session.head(
            f"https://monitoring.solaredge.com/solaredge-apigw/api/sites/{self.siteid}/layout/energy",
            headers={"user-agent": USER_AGENT},
            timeout=API_TIMEOUT_SHORT,
        ) as response:
            pass  # Response automatically closed by context manager

        session.auth = (self.username, self.password)
        login_url = "https://monitoring.solaredge.com/solaredge-web/p/login"
        logout_slo_url = "https://monitoring.solaredge.com/solaredge-web/p/logout/slo"

        with session.get(login_url, timeout=API_TIMEOUT_SHORT) as response:
            if response.status_code != 200:
                _LOGGER.warning("Login request returned status %d", response.status_code)

        if self.GetThecsrfToken(session.cookies.get_dict()) is not None:
            return

        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "SolarEdge Optimizers (legacy): CSRF cookie missing after /p/login; "
                "trying logout/login bootstrap"
            )
        with session.get(logout_slo_url, timeout=API_TIMEOUT_SHORT) as response:
            if response.status_code != 200:
                _LOGGER.warning("Logout SLO request returned status %d", response.status_code)
        with session.get(login_url, timeout=API_TIMEOUT_SHORT) as response:
            if response.status_code != 200:
                _LOGGER.warning("Login request after logout bootstrap returned status %d", response.status_code)
        if self.GetThecsrfToken(session.cookies.get_dict()) is None:
            _LOGGER.warning("CSRF token still not found in cookies after logout/login bootstrap")

    def _get_session(self):
        """Get or create a thread-local session for reuse.
        
        Each thread gets its own session to avoid conflicts when using ThreadPoolExecutor.
        Sessions are reused within the same thread to reduce login overhead.
        All sessions are tracked so close() can close them and avoid leaking file descriptors.
        """
        if self._closed:
            raise RuntimeError("SolarEdge legacy API client is closed")
        if not hasattr(self._thread_local, 'session') or self._thread_local.session is None:
            session = Session()
            try:
                self._prime_session_cookies(session)
            except Exception:  # pylint: disable=broad-except
                session.close()
                raise
            with self._sessions_lock:
                self._all_sessions.add(session)
            self._thread_local.session = session
        
        return self._thread_local.session

    def _request_headers(self, cookie: str, csrf_token: str) -> dict:
        """Build headers dict for legacy API request (cookie, CSRF, referer, etc.)."""
        return {
            "authority": "monitoring.solaredge.com",
            "accept": "*/*",
            "accept-language": self._accept_language_header(),
            "content-type": "application/json",
            "cookie": cookie,
            "origin": "https://monitoring.solaredge.com",
            "referer": f"https://monitoring.solaredge.com/solaredge-web/p/site/{self.siteid}/",
            "sec-ch-ua": '"Google Chrome";v="105", "Not)A;Brand";v="8", "Chromium";v="105"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": USER_AGENT,
            "x-csrf-token": csrf_token,
            "x-kl-ajax-request": "Ajax_Request",
            "x-requested-with": "XMLHttpRequest",
        }

    def _doRequest(self, method, request_url, data=None):
        """Execute request with thread-local session; return response text or ERROR001 string.

        Uses 'with session.request(...) as response' so the response body is consumed and the
        connection is released back to the pool (or closed); no file descriptors are left open.
        """
        session = self._get_session()
        therightcookie = self.MakeStringFromCookie(session.cookies.get_dict())
        thecrsftoken = self.GetThecsrfToken(session.cookies.get_dict())
        if thecrsftoken is None:
            _LOGGER.warning("CSRF token not found in cookies; refreshing legacy session bootstrap")
            self._prime_session_cookies(session)
            therightcookie = self.MakeStringFromCookie(session.cookies.get_dict())
            thecrsftoken = self.GetThecsrfToken(session.cookies.get_dict())
            if thecrsftoken is None:
                _LOGGER.warning("CSRF token still not found in cookies after refresh")
                thecrsftoken = ""
        csrf_token_missing_locally = thecrsftoken == ""

        with session.request(
            method=method,
            url=request_url,
            headers=self._request_headers(therightcookie, thecrsftoken),
            data=data,
        ) as response:
            if _LOGGER.isEnabledFor(logging.DEBUG):
                response_preview = response.text[:2000] if len(response.text) > 2000 else response.text
                _LOGGER.debug(
                    "Endpoint: %s %s | Status: %s | Response (preview): %s",
                    method,
                    request_url,
                    response.status_code,
                    response_preview,
                )
            response_text = response.text
            status_code = response.status_code

        if status_code == 200:
            return response_text
        if status_code == 498:
            if csrf_token_missing_locally:
                _LOGGER.warning(
                    "SolarEdge Optimizers (legacy): HTTP 498 from %s %s; request was sent without a local "
                    "CSRF token after bootstrap refresh, so SolarEdge likely rejected the legacy session/CSRF bootstrap",
                    method,
                    request_url,
                )
            else:
                _LOGGER.warning(
                    "SolarEdge Optimizers (legacy): HTTP 498 from %s %s; SolarEdge likely rejected an "
                    "invalid or expired CSRF token / legacy web session",
                    method,
                    request_url,
                )
        return f"ERROR001 - HTTP CODE: {status_code}"

    def getLifeTimeEnergy(self):
        # Use f-string instead of .format() for better performance
        url = f"https://monitoring.solaredge.com/solaredge-apigw/api/sites/{self.siteid}/layout/energy?timeUnit=ALL"
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("Endpoint (lifetime energy, whole site): %s", url)
        return self._doRequest("POST", url)

    def close(self):
        """Close all sessions (all threads) to prevent file descriptor leaks.

        Call when the API client is no longer needed (e.g. integration unload/removal).
        No requests should be in progress when close() is called. Sets _closed so any
        subsequent _get_session() raises. Closes every tracked Session so connection
        pools and file descriptors are released. Idempotent; safe to call multiple times.
        """
        if self._closed:
            return
        self._closed = True
        if hasattr(self._thread_local, "session"):
            self._thread_local.session = None
        with self._sessions_lock:
            sessions_to_close = set(self._all_sessions)
            self._all_sessions.clear()
        closed_count = 0
        for session in sessions_to_close:
            try:
                session.close()
                closed_count += 1
                if _LOGGER.isEnabledFor(logging.DEBUG):
                    _LOGGER.debug("SolarEdge Optimizers (legacy): Closed session")
            except Exception as e:  # pylint: disable=broad-except
                _LOGGER.warning("SolarEdge Optimizers (legacy): Error closing session: %s", e)
        if closed_count:
            _LOGGER.info(
                "SolarEdge Optimizers (legacy): Closed %d session(s) (file descriptors released)",
                closed_count,
            )

    def __del__(self) -> None:
        """Best-effort cleanup if GC collects an unclosed client."""
        try:
            self.close()
        except Exception as exc:  # pylint: disable=broad-except
            # Never raise from destructor, but leave a debug breadcrumb.
            _LOGGER.debug("SolarEdge Optimizers (legacy): Ignoring close error in __del__: %s", exc)

    def getAlerts(self, only_open=False):
        # Note: this might require FULL_ACCESS rights in the SE portal, as opposed to DASHBOARD_AND_LAYOUT
        # Use f-string instead of .format() for better performance
        url = f"https://monitoring.solaredge.com/solaredge-apigw/api/rna/v1.0/site/{self.siteid}/alerts"
        data = None
        if only_open:
            data = [{"fieldFilterOperator": "IN",
                     "fieldName": "status",
                     "fieldValue": ["OPEN"]}]
        return self._doRequest("POST", url, data=json.dumps(data))

    def GetThecsrfToken(self, cookies):
        # Optimize using direct dictionary access instead of linear search
        return cookies.get("CSRF-TOKEN")

    def MakeStringFromCookie(self, cookies):
        # Optimize string concatenation using list and join() instead of += in loop
        # Direct access to known keys instead of iterating all cookies
        cookie_parts = []
        if "CSRF-TOKEN" in cookies:
            cookie_parts.append(f"CSRF-TOKEN={cookies['CSRF-TOKEN']};")
        if "JSESSIONID" in cookies:
            cookie_parts.append(f"JSESSIONID={cookies['JSESSIONID']};")

        # Fixed typo "concent" to "consent" in cookie string
        # Use f-string instead of .format() for better performance
        locale = self._locale_from_language()
        cookie_parts.append(f"SolarEdge_Locale={locale}; SolarEdge_Locale={locale}; solaredge_cookie_consent=1;SolarEdge_Field_ID={self.siteid}")

        return "".join(cookie_parts)

    def decodeResult(self, result):
        # First try to extract JSON from SE.systemData = {...}; line (more specific and reliable)
        # Moved import to module level for better performance
        # Find SE.systemData = and extract the JSON object (handles nested braces)
        se_systemdata_match = re.search(r'SE\.systemData\s*=\s*', result)
        if se_systemdata_match:
            start_pos = se_systemdata_match.end()
            # Find the opening brace
            brace_start = result.find('{', start_pos)
            if brace_start != -1:
                # Count braces to find the matching closing brace
                brace_count = 0
                i = brace_start
                while i < len(result):
                    if result[i] == '{':
                        brace_count += 1
                    elif result[i] == '}':
                        brace_count -= 1
                        if brace_count == 0:
                            # Found matching closing brace
                            json_str = result[brace_start:i+1]
                            try:
                                json_result = json.loads(json_str)
                                if _LOGGER.isEnabledFor(logging.DEBUG):
                                    _LOGGER.debug("Extracted JSON from SE.systemData line")
                                return json_result
                            except json.JSONDecodeError as e:
                                _LOGGER.warning("Failed to parse JSON from SE.systemData line: %s", e)
                                # Fall through to jsonfinder method
                            break
                    i += 1
        
        # Fallback to jsonfinder method for backwards compatibility
        json_result = ""
        for _, __, obj in jsonfinder(result, json_only=True):
            json_result = obj
            break
        else:
            raise ValueError("data not found")

        return json_result

class SolarEdgeSite:
    def __init__(self, json_obj):
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("SolarEdge Optimizers: Initializing SolarEdgeSite with siteId: %s", json_obj.get("siteId"))
        self.siteId = json_obj["siteId"]
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("SolarEdge Optimizers: Getting all inverters for site %s", self.siteId)
        self.inverters = self.__GetAllInverters(json_obj)
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("SolarEdge Optimizers: Site %s initialized with %d inverters", self.siteId, len(self.inverters))

    def __GetAllInverters(self, json_obj):
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("SolarEdge Optimizers: Parsing inverters from logical tree")

        inverters = []
        child_count = len(json_obj["logicalTree"]["childIds"])
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("SolarEdge Optimizers: Found %d children in logical tree", child_count)

        for i in range(child_count):
            child_name = json_obj["logicalTree"]["children"][i]["data"]["name"]
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("SolarEdge Optimizers: Processing child %d: %s", i, child_name)

            if "PRODUCTION METER" not in child_name.upper():
                if _LOGGER.isEnabledFor(logging.DEBUG):
                    _LOGGER.debug("SolarEdge Optimizers: Adding inverter at index %d", i)
                inverters.append(SolarEdgeInverter(json_obj=json_obj, index=i))
            else:
                if _LOGGER.isEnabledFor(logging.DEBUG):
                    _LOGGER.debug("SolarEdge Optimizers: Found production meter, processing sub-children")
                sub_child_count = len(json_obj["logicalTree"]["children"][i]["childIds"])
                for j in range(sub_child_count):
                    if _LOGGER.isEnabledFor(logging.DEBUG):
                        _LOGGER.debug("SolarEdge Optimizers: Adding inverter at indices %d,%d", i, j)
                    inverters.append(SolarEdgeInverter(json_obj=json_obj, index=i, index2=j, powermeterpresent=True))

        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("SolarEdge Optimizers: Completed parsing %d inverters", len(inverters))
        return inverters

    def returnNumberOfOptimizers(self):
        i = 0

        for inverter in self.inverters:
            for string in inverter.strings:
                i = i + len(string.optimizers)

        return i

    def ReturnAllPanelsIds(self):

        panel_ids = []

        for inverter in self.inverters:
            for string in inverter.strings:
                for optimizer in string.optimizers:
                    # Use f-string instead of .format() for better performance
                    panel_ids.append(f"{optimizer.optimizerId}|{optimizer.serialNumber}")

        return panel_ids


class SolarEdgeInverter:

    def __init__(self, json_obj, index, index2=0, powermeterpresent=False):
        if powermeterpresent:
            data = json_obj["logicalTree"]["children"][index]["children"][index2]["data"]
            self.inverterId = data["id"]
            self.serialNumber = data["serialNumber"]
            self.name = data["name"]
            self.displayName = data["displayName"]
            self.relativeOrder = data["relativeOrder"]
            self.type = data["type"]
            self.operationsKey = data["operationsKey"]
            self.status = data.get("status", "")
            self.manufacturer = data.get("manufacturer", "SolarEdge")
            self.model = data.get("model", "")
            self.maxActivePower = data.get("maxActivePower")  # kW (One API) or None

            self.strings = self.__GetStringInformation(json_obj["logicalTree"]["children"][index]["children"][index2]["children"], index2)
        else:
            data = json_obj["logicalTree"]["children"][index]["data"]
            self.inverterId = data["id"]
            self.serialNumber = data["serialNumber"]
            self.name = data["name"]
            self.displayName = data["displayName"]
            self.relativeOrder = data["relativeOrder"]
            self.type = data["type"]
            self.operationsKey = data["operationsKey"]
            self.status = data.get("status", "")
            self.manufacturer = data.get("manufacturer", "SolarEdge")
            self.model = data.get("model", "")
            self.maxActivePower = data.get("maxActivePower")  # kW (One API) or None

            self.strings = self.__GetStringInformation(json_obj["logicalTree"]["children"][index]["children"], index)


    def __GetStringInformation(self, json_obj, index):
        strings = []

        for i in range(len(json_obj)):
            if "STRING" in json_obj[i]["data"]["name"].upper():
                strings.append(SolarEdgeString(json_obj[i]))
            else:
                for j in range(len(json_obj[i]["children"])):
                    strings.append(SolarEdgeString(json_obj[i]["children"][j]))

        return strings


class SolarEdgeString:
    def __init__(self, json_obj):
        data = json_obj["data"]
        self.stringId = data["id"]
        self.serialNumber = data["serialNumber"]
        self.name = data["name"]
        self.displayName = data["displayName"]
        self.relativeOrder = data["relativeOrder"]
        self.type = data["type"]
        self.operationsKey = data["operationsKey"]
        self.status = data.get("status", "")
        self.optimizers = self.__GetOptimizers(json_obj)

    def __GetOptimizers(self, json_obj):
        optimizers = []

        for i in range(len(json_obj["children"])):
            optimizers.append(SolarlEdgeOptimizer(json_obj["children"][i]))

        return optimizers


class SolarlEdgeOptimizer:
    def __init__(self, json_obj):
        data = json_obj["data"]
        self.optimizerId = data["id"]
        self.serialNumber = data["serialNumber"]
        self.name = data["name"]
        self.displayName = data["displayName"]
        self.relativeOrder = data["relativeOrder"]
        self.type = data["type"]
        self.operationsKey = data["operationsKey"]
        self.status = data.get("status", "")


class SolarEdgeAggregatedData:
    """Data class for aggregated SolarEdge measurements at string/inverter/site level."""

    __slots__ = (
        'panel_id', 'entity_type', 'entity_id_path', 'serialnumber', 'panel_description',
        'lastmeasurement', 'model', 'manufacturer', 'current', 'optimizer_voltage', 'power',
        'voltage', 'lifetime_energy', 'child_count', 'active_optimizer_count', 'status',
        'installation_date', 'peak_power', 'max_active_power',
    )

    def __init__(self, entity_id, entity_type, lifetime_energy=None, entity_id_path=None):
        self.panel_id = entity_id  # Used for coordinator data lookup (e.g. site_2065855, inverter_123, string_1_1)
        self.entity_type = entity_type  # "string", "inverter", or "site"
        self.entity_id_path = entity_id_path or ()  # (site,) or (site, i) or (site, i, s) for entity_id generation
        self.serialnumber = ""
        self.panel_description = ""
        self.lastmeasurement = None
        self.model = ""
        self.manufacturer = ""

        # Aggregated measurements
        self.current = 0.0
        self.optimizer_voltage = 0.0  # Not used for aggregated
        self.power = 0.0
        self.voltage = 0.0

        # Lifetime energy from API
        self.lifetime_energy = lifetime_energy or 0.0

        # Additional aggregated info
        self.child_count = 0  # Number of optimizers in string, or strings in inverter
        self.active_optimizer_count = 0  # Number of optimizers with recent data
        self.status = ""  # Status from API (Active, Inactive, etc.)

        # Site-only: from portal layout/information/site (installation date, peak power kW)
        self.installation_date = None  # "YYYY-MM-DD" or None
        self.peak_power = None  # float kW or None
        # Inverter-only: from portal layout logical v2 (maxActivePower in watts, stored as kW)
        self.max_active_power = None  # float kW or None


class SolarEdgeOptimizerData:
    """Data class for SolarEdge optimizer measurements and metadata."""

    # Use __slots__ to reduce memory overhead and improve attribute access speed
    __slots__ = (
        '_timezone', '_json_obj', '_has_valid_measurements', 'serialnumber', 'panel_id', 'panel_description',
        'lastmeasurement', 'model', 'manufacturer', 'current', 'optimizer_voltage',
        'power', 'voltage', 'temperature', 'lifetime_energy', 'azimuth', 'tilt', 'status'
    )

    def __init__(self, panelid, json_object, timezone=None, has_valid_measurements=None):
        self._timezone = timezone if timezone is not None else pytz.UTC
        # True when the API provided a non-empty measurements dict (used for One vs legacy fallback)
        if has_valid_measurements is not None:
            self._has_valid_measurements = bool(has_valid_measurements)
        else:
            self._has_valid_measurements = bool(json_object.get("measurements") if isinstance(json_object.get("measurements"), dict) else False)

        self.serialnumber = ""
        self.panel_id = ""
        self.panel_description = ""
        self.lastmeasurement = ""
        self.model = ""
        self.manufacturer = ""
        self.current = ""
        self.optimizer_voltage = ""
        self.power = ""
        self.voltage = ""
        self.temperature = ""
        self.lifetime_energy = ""
        self.azimuth = None
        self.tilt = None
        self.status = ""

        if panelid is not None:
            self._json_obj = json_object
            self.serialnumber = json_object["serialNumber"]
            self.panel_id = panelid
            self.panel_description = json_object.get("description", "")
            rawdate = json_object.get("lastMeasurementDate", "")
            self.lastmeasurement = _parse_last_measurement_date(rawdate, panelid, self._timezone)
            self.model = json_object.get("model", "")
            self.manufacturer = json_object.get("manufacturer", "")
            measurements = _normalize_measurements_dict(json_object, panelid)
            _apply_measurements_to_optimizer_data(self, measurements, json_object, panelid)
