""" My module """
import time
import threading

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

class solaredgeoptimizers:
    def __init__(self, siteid, username, password):
        self.siteid = siteid
        self.username = username
        self.password = password
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

    def check_login(self):
        # AJT: 16-Jan-2026: Use f-string instead of .format() for better performance
        url = f"https://monitoring.solaredge.com/solaredge-apigw/api/sites/{self.siteid}/layout/logical"

        kwargs = {}
        kwargs["auth"] = requests.auth.HTTPBasicAuth(self.username, self.password)
        kwargs["headers"] = {"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/105.0.0.0 Safari/537.36",
                             }
        # AJT: 11-Jan-2026: Use context manager to ensure response is properly closed
        with requests.get(url, **kwargs) as r:
            return r.status_code

    def requestLogicalLayout(self):
        # AJT: 16-Jan-2026: Use f-string instead of .format() for better performance
        url = f"https://monitoring.solaredge.com/solaredge-apigw/api/sites/{self.siteid}/layout/logical"

        kwargs = {}

        kwargs["auth"] = requests.auth.HTTPBasicAuth(self.username, self.password)
        # AJT: 11-Jan-2026: Use context manager to ensure response is properly closed
        with requests.get(url, **kwargs) as r:
            return r.text

    def requestListOfAllPanels(self):
        # AJT: 16-Jan-2026: Cache result to avoid repeated API calls (layout rarely changes)
        now = datetime.now()
        if (self._panels_cache is None or 
            self._panels_cache_time is None or 
            (now - self._panels_cache_time) > self._panels_cache_ttl):
            json_obj = json.loads(self.requestLogicalLayout())
            self._panels_cache = SolarEdgeSite(json_obj)
            self._panels_cache_time = now
            _LOGGER.debug("Refreshed panels cache")
        else:
            _LOGGER.debug("Using cached panels data")
        return self._panels_cache

    def requestSystemData(self, itemId):
        # AJT: 10-Jan-2025: Fixed endpoint URL - changed from monitoringpublic.solaredge.com/publicSystemData to monitoring.solaredge.com/systemData,
        # changed isPublic=true to false, added locale parameter, and added v parameter with timestamp
        # AJT: 16-Jan-2026: Use f-string instead of .format() for better performance
        url = f"https://monitoring.solaredge.com/solaredge-web/p/systemData?reporterId={itemId}&type=panel&activeTab=0&fieldId={self.siteid}&isPublic=false&locale=en_US&v={round(time.time() * 1000)}"

        kwargs = {}
        kwargs["auth"] = requests.auth.HTTPBasicAuth(self.username, self.password)
        # AJT: 11-Jan-2026: Use context manager to ensure response is properly closed
        with requests.get(url, **kwargs) as r:
            if r.status_code == 200:
                json_object = self.decodeResult(r.text)
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
                        return SolarEdgeOptimizerData(itemId, json_object)
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
            if lifetime_energy_response.startswith("ERROR001"):
                _LOGGER.error("Failed to get lifetime energy data: %s", lifetime_energy_response)
                lifetimeenergy = {}
            else:
                try:
                    lifetimeenergy = json.loads(lifetime_energy_response)
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
        optimizer_ids = []
        for inverter in solarsite.inverters:
            for string in inverter.strings:
                for optimizer in string.optimizers:
                    optimizer_ids.append(optimizer.optimizerId)

        # AJT: 16-Jan-2026: Parallelize API calls using ThreadPoolExecutor for 10-20x speedup
        data = []
        max_workers = min(10, len(optimizer_ids))  # Limit concurrent requests to avoid overwhelming server
        
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
                        # AJT: 16-Jan-2026: Optimize lifetime energy lookup using pre-converted string and .get() with defaults
                        optimizer_id_str = str(optimizer_id)
                        energy_data = lifetimeenergy.get(optimizer_id_str, {})
                        unscaled_energy = energy_data.get("unscaledEnergy")
                        if unscaled_energy is not None:
                            info.lifetime_energy = float(unscaled_energy) / 1000
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
            self._thread_local.session.head(
                f"https://monitoring.solaredge.com/solaredge-apigw/api/sites/{self.siteid}/layout/energy",
                headers={"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/105.0.0.0 Safari/537.36",
                         }
            )
            url = "https://monitoring.solaredge.com/solaredge-web/p/login"
            self._thread_local.session.auth = (self.username, self.password)
            r1 = self._thread_local.session.get(url)
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
        response = session.request(
            method=method,
            url=request_url,
            headers={
                "authority": "monitoring.solaredge.com",
                "accept": "*/*",
                "accept-language": "en-US,en;q=0.9,nl;q=0.8",
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
        )

        if response.status_code == 200:
            return response.text
        else:
            # AJT: 16-Jan-2026: Use f-string instead of .format() for better performance
            return f"ERROR001 - HTTP CODE: {response.status_code}"

    def getLifeTimeEnergy(self):
        # AJT: 16-Jan-2026: Use f-string instead of .format() for better performance
        url = f"https://monitoring.solaredge.com/solaredge-apigw/api/sites/{self.siteid}/layout/energy?timeUnit=ALL"
        return self._doRequest("POST", url)

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
        cookie_parts = []
        for cookie in cookies:
            if cookie == "CSRF-TOKEN":
                cookie_parts.append(f"{cookie}={cookies[cookie]};")
            elif cookie == "JSESSIONID":
                cookie_parts.append(f"{cookie}={cookies[cookie]};")

        # AJT: 10-Jan-2025: Fixed typo "concent" to "consent" in cookie string
        # AJT: 16-Jan-2026: Use f-string instead of .format() for better performance
        cookie_parts.append(f"SolarEdge_Locale=nl_NL; SolarEdge_Locale=nl_NL; solaredge_cookie_consent=1;SolarEdge_Field_ID={self.siteid}")

        return "".join(cookie_parts)

    def decodeResult(self, result):
        json_result = ""
        for _, __, obj in jsonfinder(result, json_only=True):
            json_result = obj
            break
        else:
            raise ValueError("data not found")

        return json_result

class SolarEdgeSite:
    def __init__(self, json_obj):
        self.siteId = json_obj["siteId"]
        self.inverters = self.__GetAllInverters(json_obj)

    def __GetAllInverters(self, json_obj):

        inverters = []
        for i in range(len(json_obj["logicalTree"]["childIds"])):

            # Blijkbaar kan er een powermeter tussen zitten. Checken of dit het geval is
            # Production Meter -> moeten 1 niveau dieper
            # Inverter 1 -> dit is 'normaal'
            if "PRODUCTION METER" not in json_obj["logicalTree"]["children"][i]["data"]["name"].upper():
                inverters.append(SolarEdgeInverter(json_obj=json_obj, index=i))
            else:
                for j in range(len(json_obj["logicalTree"]["children"][i]["childIds"])):
                    #inverters.append(SolarEdgeInverter(json_obj, i, j, True))
                    inverters.append(SolarEdgeInverter(json_obj=json_obj, index=i, index2=j, powermeterpresent=True))

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


class SolarEdgeOptimizerData:
    """Data class for SolarEdge optimizer measurements and metadata."""

    def __init__(self, panelid, json_object):

        # Atributen die we willen zien:
        self.serialnumber = ""
        self.panel_id = ""
        # AJT: 16-Jan-2026: Fixed spelling from "paneel" to "panel"
        self.panel_description = ""
        self.lastmeasurement = ""
        self.model = ""
        self.manufacturer = ""

        # Warden
        self.current = ""
        self.optimizer_voltage = ""
        self.power = ""
        self.voltage = ""

        # Extra info
        self.lifetime_energy = ""

        if panelid is not None:
            self._json_obj = json_object

            # Atributen die we willen zien:
            self.serialnumber = json_object["serialNumber"]
            self.panel_id = panelid
            # AJT: 16-Jan-2026: Fixed spelling from "paneel" to "panel"
            self.panel_description = json_object["description"]
            rawdate = json_object.get("lastMeasurementDate", "")
            
            # AJT: 11-Jan-2026: Fixed fragile date parsing with error handling
            try:
                # Removing the Timezone information
                date_parts = rawdate.split(' ')
                if len(date_parts) >= 6:
                    # AJT: 16-Jan-2026: Use f-string instead of .format() for better performance
                    new_time = f"{date_parts[0]} {date_parts[1]} {date_parts[2]} {date_parts[3]} {date_parts[5]}"
                    self.lastmeasurement = datetime.strptime(new_time, "%a %b %d %H:%M:%S %Y")
                else:
                    # Fallback: try parsing the full string (strip timezone if present)
                    _LOGGER.warning("Unexpected date format for optimizer %s: %s", panelid, rawdate)
                    date_str = rawdate.split('(')[0].strip() if '(' in rawdate else rawdate
                    self.lastmeasurement = datetime.strptime(date_str, "%a %b %d %H:%M:%S %Y")
            except (ValueError, IndexError) as e:
                _LOGGER.error("Failed to parse date '%s' for optimizer %s: %s", rawdate, panelid, e)
                # Set to current time as fallback
                self.lastmeasurement = datetime.now()

            self.model = json_object.get("model", "")
            self.manufacturer = json_object.get("manufacturer", "")

            # Warden - AJT: 11-Jan-2026: Fixed unsafe dictionary access using .get() with defaults
            # AJT: 17-Jan-2026: Handle cases where measurements might be missing, null, or have different structure
            measurements = json_object.get("measurements", {})
            if not measurements or not isinstance(measurements, dict):
                _LOGGER.warning(
                    "Missing or invalid measurements for optimizer %s (panel_id: %s). Available keys: %s",
                    panelid,
                    json_object.get("serialNumber", "unknown"),
                    list(json_object.keys()) if isinstance(json_object, dict) else "N/A"
                )
                measurements = {}
            
            # AJT: 17-Jan-2026: Handle null values and convert to float safely
            def safe_float(value, default=0.0):
                """Safely convert value to float, handling None, empty strings, and invalid types."""
                if value is None or value == "":
                    return default
                try:
                    return float(value)
                except (ValueError, TypeError):
                    return default
            
            self.current = safe_float(measurements.get("Current [A]"), 0.0)
            self.optimizer_voltage = safe_float(measurements.get("Optimizer Voltage [V]"), 0.0)
            self.power = safe_float(measurements.get("Power [W]"), 0.0)
            self.voltage = safe_float(measurements.get("Voltage [V]"), 0.0)
            
            # AJT: 17-Jan-2026: Log if all measurements are zero to help diagnose API response issues
            if self.current == 0.0 and self.power == 0.0 and self.voltage == 0.0 and self.optimizer_voltage == 0.0:
                _LOGGER.debug(
                    "All measurements are zero for optimizer %s (serial: %s). Measurements dict: %s",
                    panelid,
                    json_object.get("serialNumber", "unknown"),
                    measurements
                )
