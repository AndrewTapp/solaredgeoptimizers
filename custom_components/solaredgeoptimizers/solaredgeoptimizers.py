"""SolarEdge API client for Home Assistant integration."""
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

# AJT: 10-Jan-2025: Added logger setup to replace print statements with proper logging
_LOGGER = logging.getLogger(__name__)

# SolarEdge API returns measurement keys in the user's locale (e.g. "Power [W]" in EN, "Leistung [W]" in DE).
# Try all known locale variants so power/current/voltage work regardless of HA language.
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
        # AJT: 18-Jan-2026: Store timezone for date parsing (default to UTC if not provided)
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
        # AJT: 16-Jan-2026: Thread-local storage for session reuse (one session per thread)
        self._thread_local = threading.local()
        # AJT: 16-Jan-2026: Cache for requestListOfAllPanels() result (TTL: 1 hour)
        self._panels_cache = None
        self._panels_cache_time = None
        self._panels_cache_ttl = timedelta(hours=1)
        # AJT: 16-Jan-2026: Cache for lifetime energy data (TTL: 1 hour, changes slowly)
        self._lifetime_energy_cache = None
        self._lifetime_energy_cache_time = None
        self._lifetime_energy_cache_ttl = timedelta(hours=1)

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
                _LOGGER.debug("Refreshed lifetime energy cache (cached accessor)")
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                # Transient DNS/network errors: keep previous cache, do not update cache time
                _LOGGER.warning(
                    "SolarEdge API unreachable (lifetime energy): %s. Using cached data if available.",
                    e,
                )
                if self._lifetime_energy_cache is None:
                    self._lifetime_energy_cache = {}
        return self._lifetime_energy_cache or {}

    def check_login(self):
        # AJT: 24-Jan-2026: Add detailed debugging for initial setup issues
        _LOGGER.info("SolarEdge Optimizers: Starting login check for site %s", self.siteid)

        # AJT: 16-Jan-2026: Use f-string instead of .format() for better performance
        url = f"https://monitoring.solaredge.com/solaredge-apigw/api/sites/{self.siteid}/layout/logical"
        _LOGGER.debug("SolarEdge Optimizers: Login check URL: %s", url)

        kwargs = {}
        kwargs["auth"] = requests.auth.HTTPBasicAuth(self.username, self.password)
        kwargs["headers"] = {"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/105.0.0.0 Safari/537.36",
                             }
        # AJT: 24-Jan-2026: Add timeout to prevent hanging and log request attempt
        kwargs["timeout"] = 30  # 30 second timeout
        _LOGGER.debug("SolarEdge Optimizers: Making login check request with 30s timeout")

        try:
            # AJT: 11-Jan-2026: Use context manager to ensure response is properly closed
            with requests.get(url, **kwargs) as r:
                _LOGGER.info("SolarEdge Optimizers: Login check completed - Status: %s", r.status_code)
                if _LOGGER.isEnabledFor(logging.DEBUG):
                    _LOGGER.debug("SolarEdge Optimizers: Login check response headers: %s", dict(r.headers))
                    _LOGGER.debug("Login check response body length: %s bytes", len(r.text))
                return r.status_code
        except requests.exceptions.Timeout as e:
            _LOGGER.error("SolarEdge Optimizers: Login check timed out after 30s: %s", e)
            raise
        except requests.exceptions.ConnectionError as e:
            _LOGGER.error("SolarEdge Optimizers: Login check connection error: %s", e)
            raise
        except requests.exceptions.RequestException as e:
            _LOGGER.error("SolarEdge Optimizers: Login check request error: %s", e)
            raise
        except Exception as e:
            _LOGGER.error("SolarEdge Optimizers: Login check unexpected error: %s", e)
            raise

    def requestLogicalLayout(self):
        # AJT: 24-Jan-2026: Add detailed debugging for initial setup issues
        _LOGGER.info("SolarEdge Optimizers: Requesting logical layout for site %s", self.siteid)

        # AJT: 16-Jan-2026: Use f-string instead of .format() for better performance
        url = f"https://monitoring.solaredge.com/solaredge-apigw/api/sites/{self.siteid}/layout/logical"
        _LOGGER.debug("SolarEdge Optimizers: Logical layout URL: %s", url)

        kwargs = {}
        kwargs["auth"] = requests.auth.HTTPBasicAuth(self.username, self.password)
        # AJT: 24-Jan-2026: Add timeout to prevent hanging
        kwargs["timeout"] = 60  # 60 second timeout for layout request
        _LOGGER.debug("SolarEdge Optimizers: Making logical layout request with 60s timeout")

        try:
            # AJT: 11-Jan-2026: Use context manager to ensure response is properly closed
            with requests.get(url, **kwargs) as r:
                _LOGGER.info("SolarEdge Optimizers: Logical layout request completed - Status: %s, Content length: %s", r.status_code, len(r.text))
                if _LOGGER.isEnabledFor(logging.DEBUG):
                    _LOGGER.debug("Endpoint (logical layout): %s", url)
                    _LOGGER.debug("SolarEdge Optimizers: Logical layout response headers: %s", dict(r.headers))
                    _LOGGER.debug("Response from requestLogicalLayout (status %s): %s", r.status_code, r.text[:2000] if len(r.text) > 2000 else r.text)
                return r.text
        except requests.exceptions.Timeout as e:
            _LOGGER.error("SolarEdge Optimizers: Logical layout request timed out after 60s: %s", e)
            raise
        except requests.exceptions.ConnectionError as e:
            _LOGGER.error("SolarEdge Optimizers: Logical layout connection error: %s", e)
            raise
        except requests.exceptions.RequestException as e:
            _LOGGER.error("SolarEdge Optimizers: Logical layout request error: %s", e)
            raise
        except Exception as e:
            _LOGGER.error("SolarEdge Optimizers: Logical layout unexpected error: %s", e)
            raise

    def requestListOfAllPanels(self):
        # AJT: 24-Jan-2026: Add detailed debugging for initial setup issues
        _LOGGER.info("SolarEdge Optimizers: Requesting list of all panels")

        # AJT: 16-Jan-2026: Cache result to avoid repeated API calls (layout rarely changes)
        now = datetime.now()
        if (self._panels_cache is None or
            self._panels_cache_time is None or
            (now - self._panels_cache_time) > self._panels_cache_ttl):
            _LOGGER.debug("SolarEdge Optimizers: Cache miss, fetching fresh layout data")
            try:
                raw_layout = self.requestLogicalLayout()
                _LOGGER.debug("SolarEdge Optimizers: Received raw layout data, parsing JSON")
                json_obj = json.loads(raw_layout)
                # AJT: 22-Jan-2026: Log parsed logical layout JSON for debugging
                if _LOGGER.isEnabledFor(logging.DEBUG):
                    _LOGGER.debug("Parsed logical layout JSON: %s", json_obj)
                _LOGGER.debug("SolarEdge Optimizers: Creating SolarEdgeSite object")
                self._panels_cache = SolarEdgeSite(json_obj)
                self._panels_cache_time = now
                _LOGGER.info("SolarEdge Optimizers: Refreshed panels cache with %s optimizers",
                           self._panels_cache.returnNumberOfOptimizers())
            except json.JSONDecodeError as e:
                _LOGGER.error("SolarEdge Optimizers: Failed to parse layout JSON: %s", e)
                _LOGGER.debug("SolarEdge Optimizers: Raw layout data: %s", raw_layout[:1000] if len(raw_layout) > 1000 else raw_layout)
                raise
            except Exception as e:
                _LOGGER.error("SolarEdge Optimizers: Unexpected error in requestListOfAllPanels: %s", e)
                raise
        else:
            _LOGGER.debug("SolarEdge Optimizers: Using cached panels data (age: %s)",
                         now - self._panels_cache_time)
        return self._panels_cache

    def requestSystemData(self, itemId):
        # AJT: 10-Jan-2025: Fixed endpoint URL - changed from monitoringpublic.solaredge.com/publicSystemData to monitoring.solaredge.com/systemData,
        # changed isPublic=true to false, added locale parameter, and added v parameter with timestamp
        # AJT: 16-Jan-2026: Use f-string instead of .format() for better performance
        locale = self._locale_from_language()
        url = f"https://monitoring.solaredge.com/solaredge-web/p/systemData?reporterId={itemId}&type=panel&activeTab=0&fieldId={self.siteid}&isPublic=false&locale={locale}&v={round(time.time() * 1000)}"

        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("Endpoint (single optimizer systemData): %s", url)

        kwargs = {}
        kwargs["auth"] = requests.auth.HTTPBasicAuth(self.username, self.password)
        # AJT: 11-Jan-2026: Use context manager to ensure response is properly closed
        with requests.get(url, **kwargs) as r:
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("Response from systemData (optimizer %s, status %s)", itemId, r.status_code)
            if r.status_code == 200:
                json_object = self.decodeResult(r.text)
                # AJT: 22-Jan-2026: Log decoded JSON object for debugging
                if _LOGGER.isEnabledFor(logging.DEBUG):
                    _LOGGER.debug("Decoded JSON object for optimizer %s: %s", itemId, json_object)
                try:
                    # AJT: Handle case where decodeResult returns a list instead of dict - extract first element if list
                    if isinstance(json_object, list):
                        if len(json_object) > 0:
                            json_object = json_object[0]
                        else:
                            _LOGGER.warning("Empty list returned for optimizer %s", itemId)
                            return None
                    
                    # AJT: 10-Jan-2025: Ensure we have a dictionary before accessing keys
                    if not isinstance(json_object, dict):
                        _LOGGER.error("Unexpected data type returned for optimizer %s: %s", itemId, type(json_object))
                        _LOGGER.debug("Response data: %s", json_object)
                        return None
                    
                    # AJT: 10-Jan-2025: Changed from direct key access to .get() for safer dictionary access
                    if json_object.get("lastMeasurementDate") == "":
                        _LOGGER.debug("Skipping optimizer %s without measurements", itemId)
                        return None
                    else:
                        # AJT: 18-Jan-2026: Pass timezone to SolarEdgeOptimizerData for correct date parsing
                        return SolarEdgeOptimizerData(itemId, json_object, self._timezone)
                except KeyError as e:
                    # AJT: 10-Jan-2025: Added specific KeyError handling with better logging
                    _LOGGER.error("Missing expected key in response for optimizer %s: %s", itemId, e)
                    _LOGGER.debug("Response data: %s", json_object)
                    return None
                except Exception as e:
                    # AJT: Replaced print() with logging and added more detailed error info
                    _LOGGER.error("Error while processing data for optimizer %s: %s", itemId, e)
                    _LOGGER.debug("Response data: %s", json_object)
                    raise Exception("Error while processing data") from e
            else:
                # AJT: 15-Jan-2026: Treat 5xx errors as temporary with a clean log line
                if 500 <= r.status_code < 600:
                    _LOGGER.warning(
                        "Temporary server error from SolarEdge (HTTP %s). Will retry on next update.",
                        r.status_code,
                    )
                    _LOGGER.debug(
                        "Server error response body for optimizer %s: %s", itemId, r.text
                    )
                    raise Exception(f"Temporary server error from SolarEdge (HTTP {r.status_code})")
                # Other HTTP errors: log status and body at debug only, raise concise exception
                _LOGGER.error("Error sending request to SolarEdge. Status code: %s", r.status_code)
                _LOGGER.debug("Error response body for optimizer %s: %s", itemId, r.text)
                raise Exception(f"Problem sending request to SolarEdge (HTTP {r.status_code})")

    def requestAllData(self):

        solarsite = self.requestListOfAllPanels()

        # AJT: 16-Jan-2026: Cache lifetime energy data to avoid repeated API calls (changes slowly)
        now = datetime.now()
        if (self._lifetime_energy_cache is None or 
            self._lifetime_energy_cache_time is None or 
            (now - self._lifetime_energy_cache_time) > self._lifetime_energy_cache_ttl):
            # AJT: 11-Jan-2026: Added error handling for getLifeTimeEnergy() response
            lifetime_energy_response = self.getLifeTimeEnergy()
            # AJT: 22-Jan-2026: Log raw lifetime energy response for debugging (endpoint already logged in getLifeTimeEnergy)
            if _LOGGER.isEnabledFor(logging.DEBUG):
                response_preview = lifetime_energy_response[:2000] if len(lifetime_energy_response) > 2000 else lifetime_energy_response
                _LOGGER.debug("Response from lifetime energy endpoint: %s", response_preview)
            if lifetime_energy_response.startswith("ERROR001"):
                _LOGGER.error("Failed to get lifetime energy data: %s", lifetime_energy_response)
                lifetimeenergy = {}
            else:
                try:
                    lifetimeenergy = json.loads(lifetime_energy_response)
                    # AJT: 22-Jan-2026: Log parsed lifetime energy data returned from endpoint
                    if _LOGGER.isEnabledFor(logging.DEBUG):
                        _LOGGER.debug("Parsed lifetime energy data (by optimizer/string ID): %s", lifetimeenergy)
                except json.JSONDecodeError as e:
                    _LOGGER.error("Failed to parse lifetime energy JSON: %s", e)
                    lifetimeenergy = {}
            # Update cache
            self._lifetime_energy_cache = lifetimeenergy
            self._lifetime_energy_cache_time = now
            _LOGGER.debug("Refreshed lifetime energy cache")
        else:
            lifetimeenergy = self._lifetime_energy_cache
            _LOGGER.debug("Using cached lifetime energy data")

        # AJT: 16-Jan-2026: Collect all optimizer IDs first for parallel processing
        # AJT: 27-Jan-2026: Use list comprehension for better performance than append in loop
        optimizer_ids = [
            optimizer.optimizerId
            for inverter in solarsite.inverters
            for string in inverter.strings
            for optimizer in string.optimizers
        ]

        # AJT: 16-Jan-2026: Parallelize API calls using ThreadPoolExecutor for 10-20x speedup
        data = []
        # AJT: 27-Jan-2026: Use adaptive worker count based on CPU cores for better performance
        max_workers = min(
            os.cpu_count() or 4,  # Use CPU count, fallback to 4
            len(optimizer_ids),
            10  # Cap at 10 to avoid overwhelming server
        )
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit all requests in parallel
            future_to_id = {
                executor.submit(self.requestSystemData, opt_id): opt_id 
                for opt_id in optimizer_ids
            }
            
            # Process results as they complete
            for future in as_completed(future_to_id):
                optimizer_id = future_to_id[future]
                try:
                    info = future.result()
                    if info is not None:
                        # Look up by string key (JSON keys are strings); fallback to int for robustness
                        optimizer_id_str = str(optimizer_id)
                        energy_data = lifetimeenergy.get(optimizer_id_str) or lifetimeenergy.get(optimizer_id) or {}
                        # Convert to kWh using API 'units' so we never show power (W) scale as energy (kWh)
                        kWh = _lifetime_energy_to_kwh(energy_data)
                        if kWh is not None:
                            info.lifetime_energy = kWh
                        else:
                            _LOGGER.warning("Lifetime energy data missing for optimizer %s, setting to 0", optimizer_id)
                            info.lifetime_energy = 0.0
                        data.append(info)
                except Exception as e:
                    _LOGGER.error("Error fetching data for optimizer %s: %s", optimizer_id, e)

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

        # AJT: 16-Jan-2026: Use f-string instead of .format() for better performance
        url = f'https://monitoring.solaredge.com/solaredge-web/p/chartData?reporterId={itemId}&fieldId={self.siteid}&reporterType=&startDate={starttime:d}&endDate={endtime:d}&uom=W&parameterName={parameter}'

        r = self._doRequestWithCooldown("GET", url)
        if r.startswith("ERROR001"):
            raise Exception(f"Error while doing request: {r}")

        json_object = self.decodeResult(r)
        try:
            # Note: the timestamp provided by SolarEdge is not a pure POSIX timestamp, but in fact contains a timezone offset.
            return {datetime.utcfromtimestamp(pair['date']/1000).astimezone(pytz.utc): pair['value'] for pair in json_object['dateValuePairs']}
        except Exception as e:
            raise Exception("Error while processing data") from e

    def requestPanelHistory(self, itemId, starttime=None, endtime=None, parameter="Power"):
        assert parameter in ("Power", "Current", "Voltage", "Energy", "PowerBox Voltage")
        return self.requestItemHistory(itemId, starttime=starttime, endtime=endtime, parameter=parameter)

    def requestStringHistory(self, itemId, starttime=None, endtime=None, parameter="Power"):
        assert parameter in ("Energy", "Power")
        return self.requestItemHistory(itemId, starttime=starttime, endtime=endtime, parameter=parameter)

    def requestInverterHistory(self, itemId, starttime=None, endtime=None, parameter="Power"):
        # https://monitoring.solaredge.com/solaredge-web/p/chartParamsList?fieldId={}reporterId={}&format=form
        assert parameter in ("AC Energy",
                             "AC Frequency", "AC Frequency P2", "AC Frequency P3",
                             "AC Voltage", "AC Voltage P2", "AC Voltage P3",
                             "AC Current", "AC Current P2", "AC Current P3",
                             "Power", "DC Voltage", "Purchased back feed AC Energy", "Total Reactive Power", "Power Factor")
        return self.requestItemHistory(itemId, starttime=starttime, endtime=endtime, parameter=parameter)

    def requestHistoricalData(self, starttime=None, endtime=None, type="optimizer", parameter="Power"):
        assert type in ("optimizer", "inverter", "string")

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
        # AJT: 16-Jan-2026: Use f-string instead of % formatting for better performance
        e = Exception(f"Could not perform request within {n_retries} retries")
        for i in range(n_retries):
            try:
                time.sleep(wait_sec)
                res = self._doRequest(method=method, request_url=request_url, data=data)
                return res
            except ConnectionError as e:
                if isinstance(e.args[0], Exception) and len(e.args[0].args) > 1 and \
                        isinstance(e.args[0].args[1], ConnectionResetError) and e.args[0].args[1].errno == 10054:
                    time.sleep(cooldown_sec)
                    continue
                raise e
        raise e

    def _get_session(self):
        """Get or create a thread-local session for reuse.
        
        Each thread gets its own session to avoid conflicts when using ThreadPoolExecutor.
        Sessions are reused within the same thread to reduce login overhead.
        """
        if not hasattr(self._thread_local, 'session') or self._thread_local.session is None:
            # AJT: 16-Jan-2026: Create new session for this thread
            self._thread_local.session = Session()
            # Perform initial login setup
            # AJT: 16-Jan-2026: Use f-string instead of .format() for better performance
            # AJT: 27-Jan-2026: Use context manager to ensure response is closed
            with self._thread_local.session.head(
                f"https://monitoring.solaredge.com/solaredge-apigw/api/sites/{self.siteid}/layout/energy",
                headers={"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/105.0.0.0 Safari/537.36",
                         }
            ) as r:
                pass  # Response automatically closed by context manager
            url = "https://monitoring.solaredge.com/solaredge-web/p/login"
            self._thread_local.session.auth = (self.username, self.password)
            # AJT: 27-Jan-2026: Use context manager to ensure response is closed
            with self._thread_local.session.get(url) as r1:
                if r1.status_code != 200:
                    _LOGGER.warning("Login request returned status %d", r1.status_code)
        
        return self._thread_local.session

    def _doRequest(self, method, request_url, data=None):
        # AJT: 16-Jan-2026: Reuse thread-local session to reduce login overhead
        session = self._get_session()

        # Fix the cookie to get a string.
        therightcookie = self.MakeStringFromCookie(session.cookies.get_dict())
        # The csrf-token is needed as a seperate header.
        thecrsftoken = self.GetThecsrfToken(session.cookies.get_dict())
        # AJT: Added check for None CSRF token to prevent errors when token is missing
        if thecrsftoken is None:
            _LOGGER.warning("CSRF token not found in cookies")
            thecrsftoken = ""

        # Build up the request.
        # AJT: 27-Jan-2026: Use context manager to ensure response is properly closed
        with session.request(
            method=method,
            url=request_url,
            headers={
                "authority": "monitoring.solaredge.com",
                "accept": "*/*",
                "accept-language": self._accept_language_header(),
                "content-type": "application/json",
                "cookie": therightcookie,
                "origin": "https://monitoring.solaredge.com",
                # AJT: 16-Jan-2026: Use f-string instead of .format() for better performance
                "referer": f"https://monitoring.solaredge.com/solaredge-web/p/site/{self.siteid}/",
                "sec-ch-ua": '"Google Chrome";v="105", "Not)A;Brand";v="8", "Chromium";v="105"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/105.0.0.0 Safari/537.36",
                "x-csrf-token": thecrsftoken,
                "x-kl-ajax-request": "Ajax_Request",
                "x-requested-with": "XMLHttpRequest",
            },
            data=data
        ) as response:
            # AJT: 22-Jan-2026: Log endpoint and raw response data for debugging
            if _LOGGER.isEnabledFor(logging.DEBUG):
                response_preview = response.text[:2000] if len(response.text) > 2000 else response.text
                _LOGGER.debug("Endpoint: %s %s | Status: %s | Response (preview): %s",
                             method, request_url, response.status_code, response_preview)
            
            # Store response text before context manager closes
            response_text = response.text
            status_code = response.status_code
        
        # Return result after response is closed
        if status_code == 200:
            return response_text
        else:
            # AJT: 16-Jan-2026: Use f-string instead of .format() for better performance
            return f"ERROR001 - HTTP CODE: {status_code}"

    def getLifeTimeEnergy(self):
        # AJT: 16-Jan-2026: Use f-string instead of .format() for better performance
        url = f"https://monitoring.solaredge.com/solaredge-apigw/api/sites/{self.siteid}/layout/energy?timeUnit=ALL"
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("Endpoint (lifetime energy, whole site): %s", url)
        return self._doRequest("POST", url)

    def close(self):
        """Close all thread-local sessions to prevent file descriptor leaks.
        
        This should be called when the API client is no longer needed, e.g., during integration unload.
        """
        if hasattr(self._thread_local, 'session') and self._thread_local.session is not None:
            try:
                self._thread_local.session.close()
                _LOGGER.debug("Closed thread-local session")
            except Exception as e:
                _LOGGER.warning("Error closing thread-local session: %s", e)
            finally:
                self._thread_local.session = None

    def getAlerts(self, only_open=False):
        # Note: this might require FULL_ACCESS rights in the SE portal, as opposed to DASHBOARD_AND_LAYOUT
        # AJT: 16-Jan-2026: Use f-string instead of .format() for better performance
        url = f"https://monitoring.solaredge.com/solaredge-apigw/api/rna/v1.0/site/{self.siteid}/alerts"
        data = None
        if only_open:
            data = [{"fieldFilterOperator": "IN",
                     "fieldName": "status",
                     "fieldValue": ["OPEN"]}]
        return self._doRequest("POST", url, data=json.dumps(data))

    def GetThecsrfToken(self, cookies):
        # AJT: 16-Jan-2026: Optimize using direct dictionary access instead of linear search
        return cookies.get("CSRF-TOKEN")

    def MakeStringFromCookie(self, cookies):
        # AJT: 16-Jan-2026: Optimize string concatenation using list and join() instead of += in loop
        # AJT: 27-Jan-2026: Direct access to known keys instead of iterating all cookies
        cookie_parts = []
        if "CSRF-TOKEN" in cookies:
            cookie_parts.append(f"CSRF-TOKEN={cookies['CSRF-TOKEN']};")
        if "JSESSIONID" in cookies:
            cookie_parts.append(f"JSESSIONID={cookies['JSESSIONID']};")

        # AJT: 10-Jan-2025: Fixed typo "concent" to "consent" in cookie string
        # AJT: 16-Jan-2026: Use f-string instead of .format() for better performance
        locale = self._locale_from_language()
        cookie_parts.append(f"SolarEdge_Locale={locale}; SolarEdge_Locale={locale}; solaredge_cookie_consent=1;SolarEdge_Field_ID={self.siteid}")

        return "".join(cookie_parts)

    def decodeResult(self, result):
        # AJT: 22-Jan-2026: First try to extract JSON from SE.systemData = {...}; line (more specific and reliable)
        # AJT: 27-Jan-2026: Moved import to module level for better performance
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
        
        # AJT: 22-Jan-2026: Fallback to jsonfinder method for backwards compatibility
        json_result = ""
        for _, __, obj in jsonfinder(result, json_only=True):
            json_result = obj
            break
        else:
            raise ValueError("data not found")

        return json_result

class SolarEdgeSite:
    def __init__(self, json_obj):
        # AJT: 24-Jan-2026: Add debugging for site initialization
        _LOGGER.debug("SolarEdge Optimizers: Initializing SolarEdgeSite with siteId: %s", json_obj.get("siteId"))
        self.siteId = json_obj["siteId"]
        _LOGGER.debug("SolarEdge Optimizers: Getting all inverters for site %s", self.siteId)
        self.inverters = self.__GetAllInverters(json_obj)
        _LOGGER.debug("SolarEdge Optimizers: Site %s initialized with %d inverters", self.siteId, len(self.inverters))

    def __GetAllInverters(self, json_obj):
        # AJT: 24-Jan-2026: Add debugging for inverter parsing
        _LOGGER.debug("SolarEdge Optimizers: Parsing inverters from logical tree")

        inverters = []
        child_count = len(json_obj["logicalTree"]["childIds"])
        _LOGGER.debug("SolarEdge Optimizers: Found %d children in logical tree", child_count)

        for i in range(child_count):
            child_name = json_obj["logicalTree"]["children"][i]["data"]["name"]
            _LOGGER.debug("SolarEdge Optimizers: Processing child %d: %s", i, child_name)

            if "PRODUCTION METER" not in child_name.upper():
                _LOGGER.debug("SolarEdge Optimizers: Adding inverter at index %d", i)
                inverters.append(SolarEdgeInverter(json_obj=json_obj, index=i))
            else:
                _LOGGER.debug("SolarEdge Optimizers: Found production meter, processing sub-children")
                sub_child_count = len(json_obj["logicalTree"]["children"][i]["childIds"])
                for j in range(sub_child_count):
                    _LOGGER.debug("SolarEdge Optimizers: Adding inverter at indices %d,%d", i, j)
                    inverters.append(SolarEdgeInverter(json_obj=json_obj, index=i, index2=j, powermeterpresent=True))

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
                    # AJT: 16-Jan-2026: Use f-string instead of .format() for better performance
                    panel_ids.append(f"{optimizer.optimizerId}|{optimizer.serialNumber}")

        return panel_ids


class SolarEdgeInverter:

    def __init__(self, json_obj, index, index2=0, powermeterpresent=False):
        if powermeterpresent:
            self.inverterId = json_obj["logicalTree"]["children"][index]["children"][index2]["data"]["id"]
            self.serialNumber = json_obj["logicalTree"]["children"][index]["children"][index2]["data"]["serialNumber"]
            self.name = json_obj["logicalTree"]["children"][index]["children"][index2]["data"]["name"]
            self.displayName = json_obj["logicalTree"]["children"][index]["children"][index2]["data"]["displayName"]
            self.relativeOrder = json_obj["logicalTree"]["children"][index]["children"][index2]["data"]["relativeOrder"]
            self.type = json_obj["logicalTree"]["children"][index]["children"][index2]["data"]["type"]
            self.operationsKey = json_obj["logicalTree"]["children"][index]["children"][index2]["data"]["operationsKey"]

            self.strings = self.__GetStringInformation(json_obj["logicalTree"]["children"][index]["children"][index2]["children"], index2)
        else:
            self.inverterId = json_obj["logicalTree"]["children"][index]["data"]["id"]
            self.serialNumber = json_obj["logicalTree"]["children"][index]["data"]["serialNumber"]
            self.name = json_obj["logicalTree"]["children"][index]["data"]["name"]
            self.displayName = json_obj["logicalTree"]["children"][index]["data"]["displayName"]
            self.relativeOrder = json_obj["logicalTree"]["children"][index]["data"]["relativeOrder"]
            self.type = json_obj["logicalTree"]["children"][index]["data"]["type"]
            self.operationsKey = json_obj["logicalTree"]["children"][index]["data"]["operationsKey"]

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
        self.stringId = json_obj["data"]["id"]
        self.serialNumber = json_obj["data"]["serialNumber"]
        self.name = json_obj["data"]["name"]
        self.displayName = json_obj["data"]["displayName"]
        self.relativeOrder = json_obj["data"]["relativeOrder"]
        self.type = json_obj["data"]["type"]
        self.operationsKey = json_obj["data"]["operationsKey"]
        self.optimizers = self.__GetOptimizers(json_obj)

    def __GetOptimizers(self, json_obj):
        optimizers = []

        for i in range(len(json_obj["children"])):
            optimizers.append(SolarlEdgeOptimizer(json_obj["children"][i]))

        return optimizers


class SolarlEdgeOptimizer:
    def __init__(self, json_obj):
        self.optimizerId = json_obj["data"]["id"]
        self.serialNumber = json_obj["data"]["serialNumber"]
        self.name = json_obj["data"]["name"]
        self.displayName = json_obj["data"]["displayName"]
        self.relativeOrder = json_obj["data"]["relativeOrder"]
        self.type = json_obj["data"]["type"]
        self.operationsKey = json_obj["data"]["operationsKey"]


class SolarEdgeAggregatedData:
    """Data class for aggregated SolarEdge measurements at string/inverter level."""

    __slots__ = (
        'panel_id', 'entity_type', 'entity_id_path', 'serialnumber', 'panel_description',
        'lastmeasurement', 'model', 'manufacturer', 'current', 'optimizer_voltage', 'power',
        'voltage', 'lifetime_energy', 'child_count', 'active_optimizer_count'
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


class SolarEdgeOptimizerData:
    """Data class for SolarEdge optimizer measurements and metadata."""
    
    # AJT: 27-Jan-2026: Use __slots__ to reduce memory overhead and improve attribute access speed
    __slots__ = (
        '_timezone', '_json_obj', 'serialnumber', 'panel_id', 'panel_description',
        'lastmeasurement', 'model', 'manufacturer', 'current', 'optimizer_voltage',
        'power', 'voltage', 'lifetime_energy'
    )

    def __init__(self, panelid, json_object, timezone=None):

        # AJT: 18-Jan-2026: Store timezone for date parsing (default to UTC if not provided)
        self._timezone = timezone if timezone is not None else pytz.UTC

        self.serialnumber = ""
        self.panel_id = ""
        # AJT: 16-Jan-2026: Fixed spelling from "paneel" to "panel"
        self.panel_description = ""
        self.lastmeasurement = ""
        self.model = ""
        self.manufacturer = ""

        self.current = ""
        self.optimizer_voltage = ""
        self.power = ""
        self.voltage = ""

        # Extra info
        self.lifetime_energy = ""

        if panelid is not None:
            self._json_obj = json_object

            self.serialnumber = json_object["serialNumber"]
            self.panel_id = panelid
            # AJT: 16-Jan-2026: Fixed spelling from "paneel" to "panel"
            self.panel_description = json_object["description"]
            rawdate = json_object.get("lastMeasurementDate", "")
            
            # AJT: 24-Jan-2026: Simplified timezone handling - always parse as local time using Home Assistant timezone
            # The SolarEdge API returns timestamps in the local timezone where the optimizers are installed,
            # but since we don't know the optimizer locations, we use the Home Assistant timezone as a reasonable approximation.
            # The date string format is typically: "Fri Jan 23 16:04:21 GMT 2026" where the time is local but labeled as GMT
            try:
                # Clean the date string by removing any timezone indicators (GMT, UTC, etc.)
                # This handles formats like "Fri Jan 23 16:04:21 GMT 2026" -> "Fri Jan 23 16:04:21 2026"
                date_str = rawdate
                # Remove timezone abbreviations that appear before the year
                # AJT: 27-Jan-2026: re is already imported at module level
                date_str = re.sub(r'\s+(?:GMT|UTC|EST|CST|PST|EDT|CDT|PDT|[A-Z]{3})\s+', ' ', date_str)

                # Parse as naive datetime (no timezone info)
                naive_dt = datetime.strptime(date_str.strip(), "%a %b %d %H:%M:%S %Y")

                # Apply Home Assistant timezone (treat as local time)
                if naive_dt.tzinfo is None:
                    if hasattr(self._timezone, 'localize'):
                        # pytz timezone
                        local_dt = self._timezone.localize(naive_dt)
                    else:
                        # ZoneInfo or other timezone
                        local_dt = naive_dt.replace(tzinfo=self._timezone)
                else:
                    local_dt = naive_dt

                # Convert to UTC for consistent storage
                self.lastmeasurement = local_dt.astimezone(pytz.UTC)

                # AJT: 24-Jan-2026: Updated logging to reflect simplified timezone handling
                if _LOGGER.isEnabledFor(logging.DEBUG):
                    _LOGGER.debug(
                        "Timezone conversion for optimizer %s: raw='%s' | cleaned='%s' | naive=%s | local=%s (%s) | UTC=%s",
                        panelid,
                        rawdate,
                        date_str.strip(),
                        naive_dt,
                        local_dt,
                        str(self._timezone),
                        self.lastmeasurement
                    )
            except (ValueError, IndexError) as e:
                _LOGGER.error("Failed to parse date '%s' for optimizer %s: %s", rawdate, panelid, e)
                # Set to current UTC time as fallback
                self.lastmeasurement = datetime.now(pytz.UTC)

            self.model = json_object.get("model", "")
            self.manufacturer = json_object.get("manufacturer", "")

            # AJT: 11-Jan-2026: Fixed unsafe dictionary access using .get() with defaults
            # AJT: 17-Jan-2026: Handle cases where measurements might be missing, null, or have different structure
            measurements = json_object.get("measurements", {})
            if not measurements or not isinstance(measurements, dict):
                # AJT: 27-Jan-2026: Only build keys list if logging is enabled to reduce overhead
                available_keys = None
                if _LOGGER.isEnabledFor(logging.WARNING):
                    available_keys = list(json_object.keys()) if isinstance(json_object, dict) else "N/A"
                _LOGGER.warning(
                    "Missing or invalid measurements for optimizer %s (panel_id: %s). Available keys: %s",
                    panelid,
                    json_object.get("serialNumber", "unknown"),
                    available_keys or "N/A"
                )
                measurements = {}
            
            # Handle null values and convert to float safely. Normalize locale decimal separator (e.g. "26,18" -> 26.18).
            def safe_float(value, default=0.0):
                """Safely convert value to float, handling None, empty strings, comma decimals, and invalid types."""
                if value is None or value == "":
                    return default
                if isinstance(value, str):
                    value = value.replace(",", ".")
                try:
                    return float(value)
                except (ValueError, TypeError):
                    return default

            self.current = safe_float(_get_measurement_value(measurements, MEASUREMENT_KEYS["current"]), 0.0)
            self.optimizer_voltage = safe_float(_get_measurement_value(measurements, MEASUREMENT_KEYS["optimizer_voltage"]), 0.0)
            self.power = safe_float(_get_measurement_value(measurements, MEASUREMENT_KEYS["power"]), 0.0)
            self.voltage = safe_float(_get_measurement_value(measurements, MEASUREMENT_KEYS["voltage"]), 0.0)
            
            # AJT: 17-Jan-2026: Log if all measurements are zero to help diagnose API response issues
            if self.current == 0.0 and self.power == 0.0 and self.voltage == 0.0 and self.optimizer_voltage == 0.0:
                _LOGGER.debug(
                    "All measurements are zero for optimizer %s (serial: %s). Measurements dict: %s",
                    panelid,
                    json_object.get("serialNumber", "unknown"),
                    measurements
                )
