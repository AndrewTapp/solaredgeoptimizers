"""
SolarEdge One API client for Home Assistant integration.

This client is used by the dual API (api_dual.py) when Use SolarEdge One is enabled.
Uses the SolarEdge One portal endpoints (monitoring.solaredge.com/services/...):

- **Site structure**: GET .../layout/logical/generic/v2/site/{siteId}?include-optimizers=true
- **Optimizer info + live data**: POST .../layout/information/optimizers (body: list of serials).
  Returns basicInformationList (serial, model e.g. P405-4RM4MRM-NA25) and serialToLiveData.
  Used for full refresh (parallel per optimizer) and for lightweight check via requestSystemDataBatch
  (one call with up to 5 random serials). Timeout 60 s with one automatic retry on read/connect timeout.
- **Inverter information**: GET .../layout/information/inverters?inverter-serials=...
  Returns fullModel (e.g. SE5000H-RW000BNN4). 403 Forbidden is non-fatal; devices still work with position-based identity.
- **Optimizer temperatures**: GET .../layout/energy/site/{siteId}/by-inverter?start-date=...&end-date=...&inverter-serials=...&include-max-temperature=true.
  Returns per-optimizer temperature (°C). Cached 15 min; merged into optimizer data.
- **Lifetime energy**: GET .../layout/energy-graph/site/{siteId}/optimizers?optimizer-serials=...&start-date=...&end-date=...
  One request per optimizer; when cache is cold, requests run in parallel (thread pool); cached 1 h.

Authentication: SolarEdge One uses OAuth/OIDC via login.solaredge.com; we use PKCE, then exchange
the authorization code at /oauth2/token for an access_token and use Bearer token for all /services/ API calls.
"""
import base64
import hashlib
import json
import logging
import os
import secrets
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from requests.sessions import Session
import pytz

from .solaredgeoptimizers import (
    SolarEdgeSite,
    SolarEdgeOptimizerData,
    _lifetime_energy_to_kwh,
)

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://monitoring.solaredge.com"
LOGIN_BASE = "https://login.solaredge.com"
# SolarEdge One OAuth client_id (from monitoring portal redirect)
SOLAREDGE_ONE_CLIENT_ID = "ugfnsujd3384sshcjehaphlh3"
MFE_AUTH_URL = f"{BASE_URL}/mfe/auth/"
MFE_AUTH_CALLBACK = f"{BASE_URL}/mfe/auth/callback"
TOKEN_URL = f"{LOGIN_BASE}/oauth2/token"


def _pkce_verifier_and_challenge():
    """Return (code_verifier, code_challenge) for S256 PKCE."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


class _FormParser(HTMLParser):
    """Extract first form action and all input name/value from HTML."""

    def __init__(self):
        super().__init__()
        self.form_action = None
        self.form_method = "GET"
        self.inputs = {}
        self._in_form = False
        self._form_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs_d = dict(attrs)
        if tag == "form":
            if not self._in_form:
                self.form_action = attrs_d.get("action", "")
                self.form_method = (attrs_d.get("method") or "GET").upper()
            self._in_form = True
            self._form_depth += 1
        if self._in_form and tag == "input":
            name = attrs_d.get("name")
            if name:
                self.inputs[name] = attrs_d.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form" and self._in_form:
            self._form_depth -= 1
            if self._form_depth <= 0:
                self._in_form = False


def _parse_login_form(html: str):
    """Return form_action, form_method, dict of input name->value."""
    parser = _FormParser()
    try:
        parser.feed(html)
    except Exception as e:  # pylint: disable=broad-except
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("SolarEdge One: HTML form parse error (non-fatal): %s", e)
    return parser.form_action, parser.form_method, parser.inputs


def _oauth_get_login_page(session: Session, login_params: dict) -> tuple[str, str]:
    """GET login page; return (html, final_url). Raises if not on login.solaredge.com."""
    login_url = f"{LOGIN_BASE}/login?{urlencode(login_params)}"
    with session.get(login_url, timeout=30) as r:
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("SolarEdge One: GET login page -> %s", r.status_code)
        login_page_html = r.text
        login_page_url = r.url
    if not login_page_url.startswith(LOGIN_BASE):
        _LOGGER.warning("SolarEdge One: login page not on login.solaredge.com: %s", login_page_url)
        raise requests.RequestException("Login page redirect failed")
    return login_page_html, login_page_url


def _oauth_post_credentials_and_follow(
    session: Session, login_params: dict, post_body: dict, login_page_url: str
) -> str:
    """POST credentials, follow redirects/204 Location; return final URL."""
    post_url = f"{LOGIN_BASE}/login?{urlencode(login_params)}"
    post_headers = {
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "Accept": "*/*",
        "Origin": LOGIN_BASE,
        "Referer": login_page_url,
    }
    with session.post(post_url, data=post_body, headers=post_headers, timeout=30, allow_redirects=True) as r:
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("SolarEdge One: POST login -> %s, URL: %s", r.status_code, r.url)
        if r.status_code >= 400 and _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("SolarEdge One: login POST %s response: %s", r.status_code, (r.text or "")[:500])
        final_url = r.url
        if r.status_code == 204 and "Location" in r.headers:
            callback_url = r.headers["Location"]
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("SolarEdge One: 204 response, following Location: %s", callback_url)
            with session.get(callback_url, timeout=30, allow_redirects=True) as r2:
                final_url = r2.url
    return final_url


def _oauth_extract_code_from_callback(final_url: str) -> str:
    """Extract authorization code from callback URL. Raises if missing."""
    if MFE_AUTH_CALLBACK not in final_url:
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("SolarEdge One: did not reach callback (final URL: %s)", final_url)
        raise requests.RequestException("OAuth callback failed")
    parsed_cb = urlparse(final_url)
    q = parse_qs(parsed_cb.query)
    code = (q.get("code") or [None])[0]
    if not code:
        _LOGGER.warning("SolarEdge One: no code in callback URL")
        raise requests.RequestException("OAuth callback missing code")
    return code


def _oauth_exchange_code_for_tokens(
    session: Session, code: str, code_verifier: str
) -> tuple[str, str | None]:
    """Exchange authorization code for access_token and refresh_token."""
    token_data = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": SOLAREDGE_ONE_CLIENT_ID,
        "redirect_uri": MFE_AUTH_CALLBACK,
        "code_verifier": code_verifier,
    }
    token_headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "*/*",
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/",
        "User-Agent": session.headers["User-Agent"],
    }
    with session.post(TOKEN_URL, data=token_data, headers=token_headers, timeout=30) as r:
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("SolarEdge One: POST oauth2/token -> %s", r.status_code)
        r.raise_for_status()
        tok = r.json()
    access_token = tok.get("access_token")
    refresh_token = tok.get("refresh_token")
    if not access_token:
        _LOGGER.warning("SolarEdge One: token response missing access_token")
        raise requests.RequestException("No access_token in token response")
    if _LOGGER.isEnabledFor(logging.DEBUG):
        _LOGGER.debug("SolarEdge One: OAuth login complete, access token obtained")
    return access_token, refresh_token


def _perform_oauth_pkce_login(session: Session, username: str, password: str) -> tuple[str, str | None]:
    """
    Perform OAuth PKCE flow: GET login page, POST credentials, extract code from callback, exchange for tokens.
    Returns (access_token, refresh_token). Caller must use a Session with User-Agent set.
    """
    code_verifier, code_challenge = _pkce_verifier_and_challenge()
    login_params = {
        "lang": "en",
        "response_type": "code",
        "client_id": SOLAREDGE_ONE_CLIENT_ID,
        "scope": "email openid",
        "redirect_uri": MFE_AUTH_CALLBACK,
        "code_challenge_method": "S256",
        "code_challenge": code_challenge,
    }
    login_page_html, login_page_url = _oauth_get_login_page(session, login_params)
    _, _, form_inputs = _parse_login_form(login_page_html)
    post_body = {k: v for k, v in (form_inputs or {}).items() if k not in ("username", "password", "email")}
    post_body["username"] = username
    post_body["password"] = password

    final_url = _oauth_post_credentials_and_follow(
        session, login_params, post_body, login_page_url
    )
    code = _oauth_extract_code_from_callback(final_url)
    return _oauth_exchange_code_for_tokens(session, code, code_verifier)


def _parse_site_id_from_uuid(uuid_str: str) -> str | None:
    """Extract numeric site ID from v2 uuid e.g. a0000000-0000-0000-0000-000002065855 -> 2065855."""
    if not uuid_str:
        return None
    try:
        # Last segment is zero-padded decimal site ID (e.g. 000002065855 -> 2065855)
        segment = uuid_str.split("-")[-1]
        return str(int(segment, 10))
    except (ValueError, TypeError, IndexError):
        return None


def _v2_find_children_by_type(parent: dict, node_type: str) -> list:
    """Return list of child nodes whose type matches node_type (case-insensitive)."""
    out = []
    for c in (parent.get("children") or []):
        if (c.get("type") or "").upper() == node_type.upper():
            out.append(c)
    return out


def _v2_build_optimizer_logical_node(opt_node: dict) -> dict:
    """Build one optimizer logical node (data only, no children) for SolarEdgeSite."""
    opt_props = opt_node.get("properties") or {}
    opt_serial = opt_node.get("serial") or opt_props.get("identifier") or opt_node.get("uuid", "")
    opt_name = opt_node.get("name") or opt_serial
    opt_display = opt_node.get("displayOrder") or opt_name
    opt_order = opt_node.get("order") or 0
    return {
        "data": {
            "id": opt_serial,
            "serialNumber": opt_serial,
            "name": opt_name,
            "displayName": opt_display,
            "relativeOrder": opt_order,
            "type": "OPTIMIZER",
            "operationsKey": opt_node.get("uuid") or "",
        }
    }


def _v2_build_string_logical_children(str_node: dict) -> list:
    """Build list of optimizer logical nodes from a v2 STRING node's OPTIMIZER folders."""
    opt_children = []
    for opt_folder in (str_node.get("children") or []):
        if (opt_folder.get("type") or "").upper() != "FOLDER" or (opt_folder.get("name") or "").upper() != "OPTIMIZER":
            continue
        for opt_node in (opt_folder.get("children") or []):
            if (opt_node.get("type") or "").upper() != "OPTIMIZER":
                continue
            opt_children.append(_v2_build_optimizer_logical_node(opt_node))
    return opt_children


def _v2_build_string_logical_node(str_node: dict) -> dict:
    """Build one string logical node (data + optimizer children) for SolarEdgeSite."""
    str_props = str_node.get("properties") or {}
    str_identifier = str_props.get("identifier") or str_node.get("uuid") or str_node.get("name", "")
    str_serial = str_node.get("serial") or str_identifier
    str_name = str_node.get("name") or str_identifier
    str_display = str_node.get("displayOrder") or str_name
    str_order = str_node.get("order") or 0
    opt_children = _v2_build_string_logical_children(str_node)
    return {
        "data": {
            "id": str_identifier,
            "serialNumber": str_serial,
            "name": str_name,
            "displayName": str_display,
            "relativeOrder": str_order,
            "type": "STRING",
            "operationsKey": str_node.get("uuid") or "",
        },
        "children": opt_children,
    }


def _v2_build_inverter_logical_children(inv_node: dict) -> list:
    """Build list of string logical nodes from a v2 INVERTER node's STRING folders."""
    string_children = []
    for str_folder in (inv_node.get("children") or []):
        if (str_folder.get("type") or "").upper() != "FOLDER" or (str_folder.get("name") or "").upper() != "STRING":
            continue
        for str_node in (str_folder.get("children") or []):
            if (str_node.get("type") or "").upper() != "STRING":
                continue
            string_children.append(_v2_build_string_logical_node(str_node))
    return string_children


def _v2_build_inverter_logical_node(inv_node: dict) -> dict:
    """Build one inverter logical node (data + string children) for SolarEdgeSite."""
    inv_props = inv_node.get("properties") or {}
    inv_identifier = inv_props.get("identifier") or inv_node.get("serial") or inv_node.get("uuid", "")
    inv_serial = inv_node.get("serial") or inv_identifier
    inv_name = inv_node.get("name") or f"Inverter {inv_identifier}"
    inv_display = inv_node.get("displayOrder") or inv_name
    inv_order = inv_node.get("order") or 0
    string_children = _v2_build_inverter_logical_children(inv_node)
    return {
        "data": {
            "id": inv_identifier,
            "serialNumber": inv_serial,
            "name": inv_name,
            "displayName": inv_display,
            "relativeOrder": inv_order,
            "type": "INVERTER",
            "operationsKey": inv_node.get("uuid") or "",
        },
        "children": string_children,
    }


def _v2_build_logical_children(structure: dict) -> list:
    """Build list of inverter logical nodes from v2 site structure (FOLDER->INVERTER hierarchy)."""
    logical_children = []
    for inv_folder in _v2_find_children_by_type(structure, "FOLDER"):
        if (inv_folder.get("name") or "").upper() != "INVERTER":
            continue
        for inv_node in (inv_folder.get("children") or []):
            if (inv_node.get("type") or "").upper() != "INVERTER":
                continue
            logical_children.append(_v2_build_inverter_logical_node(inv_node))
    return logical_children


def _site_structure_v2_to_solar_edge_site(site_id: str, raw: dict) -> SolarEdgeSite:
    """Convert SolarEdge One v2 siteStructure JSON to SolarEdgeSite (same shape as legacy API)."""
    structure = raw.get("siteStructure") if "siteStructure" in raw else raw
    sid = site_id or (structure.get("uuid") and _parse_site_id_from_uuid(structure["uuid"])) or site_id
    logical_children = _v2_build_logical_children(structure)
    fake_logical = {"childIds": list(range(len(logical_children))), "children": logical_children}
    fake_json = {"siteId": sid, "logicalTree": fake_logical}
    return SolarEdgeSite(fake_json)


class solaredge_one:
    """API client for SolarEdge One portal (services/layout/... endpoints)."""

    def __init__(self, siteid, username, password, timezone=None, language=None):
        self.siteid = str(siteid)
        self.username = username
        self.password = password
        self._timezone = timezone if timezone is not None else pytz.UTC
        self._language = (language or "en").split("-")[0].lower()
        self._session = None
        self._panels_cache = None
        self._panels_cache_time = None
        self._panels_cache_ttl = timedelta(hours=1)
        self._lifetime_energy_cache = None
        self._lifetime_energy_cache_time = None
        self._lifetime_energy_cache_ttl = timedelta(hours=1)
        self._temperature_cache = None
        self._temperature_cache_time = None
        self._temperature_cache_ttl = timedelta(minutes=15)
        self._access_token = None
        self._refresh_token = None

    def _ensure_token(self) -> str:
        """Obtain OAuth access_token via PKCE flow and token exchange. Returns access_token."""
        if self._access_token:
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("SolarEdge One: Using cached access token")
            return self._access_token

        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("SolarEdge One: No token; starting OAuth PKCE login flow")
        with Session() as session:
            session.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
            self._access_token, self._refresh_token = _perform_oauth_pkce_login(
                session, self.username, self.password
            )
        return self._access_token

    def _request_headers(self):
        """Headers for /services/ requests; use Bearer token from OAuth."""
        token = self._ensure_token()
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/",
        }

    def _get(self, path: str, params: dict | None = None, timeout=60):
        url = f"{BASE_URL}{path}"
        kwargs = {"headers": self._request_headers(), "timeout": timeout}
        if params:
            kwargs["params"] = params
        with requests.get(url, **kwargs) as r:
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("SolarEdge One: GET %s -> %s", path, r.status_code)
            if r.status_code == 401:
                if _LOGGER.isEnabledFor(logging.DEBUG):
                    _LOGGER.debug("SolarEdge One: 401 on GET %s, clearing token for re-login", path)
                self._access_token = None
                r.raise_for_status()
            r.raise_for_status()
            return r.json()

    def _post(self, path: str, json_data, timeout=60):
        url = f"{BASE_URL}{path}"
        with requests.post(
            url,
            headers=self._request_headers(),
            json=json_data,
            timeout=timeout,
        ) as r:
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("SolarEdge One: POST %s -> %s", path, r.status_code)
            if r.status_code == 401:
                if _LOGGER.isEnabledFor(logging.DEBUG):
                    _LOGGER.debug("SolarEdge One: 401 on POST %s, clearing token for re-login", path)
                self._access_token = None
                r.raise_for_status()
            r.raise_for_status()
            return r.json()

    def get_inverter_models(self, serials: list) -> dict[str, str]:
        """
        Fetch inverter information (fullModel) from layout/information/inverters.
        Returns dict mapping serial -> fullModel (e.g. "SE5000H-RW000BNN4").
        Used to show inverter model on the inverter device in Home Assistant.
        """
        if not serials:
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("SolarEdge One: get_inverter_models called with empty serials, skipping")
            return {}
        path = "/services/layout/information/inverters"
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "SolarEdge One: get_inverter_models requesting %d inverter(s): %s",
                len(serials),
                serials,
            )
        try:
            # API accepts comma-separated inverter-serials
            params = {"inverter-serials": ",".join(str(s).strip() for s in serials if s)}
            data = self._get(path, params=params, timeout=30)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 403:
                _LOGGER.warning(
                    "SolarEdge One: Inverter information returned 403 Forbidden (insufficient permissions). "
                    "Inverter model names will not be shown; devices still work with position-based identity."
                )
            else:
                _LOGGER.warning("SolarEdge One: Failed to fetch inverter information: %s", e)
            return {}
        except requests.RequestException as e:
            _LOGGER.warning("SolarEdge One: Failed to fetch inverter information: %s", e)
            return {}
        result = {}
        for item in data.get("basicInformationList") or []:
            serial = (item.get("serial") or "").strip()
            full_model = (item.get("fullModel") or "").strip()
            if serial and full_model:
                result[serial] = full_model
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("SolarEdge One: get_inverter_models -> %s", result)
        return result

    def check_login(self):
        """Verify credentials by fetching site structure (v2)."""
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("SolarEdge One: check_login for site %s", self.siteid)
        path = f"/services/layout/logical/generic/v2/site/{self.siteid}"
        try:
            self._get(path, params={"include-optimizers": "true"}, timeout=30)
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("SolarEdge One: check_login succeeded (200)")
            return 200
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else 0
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("SolarEdge One: check_login failed with HTTP %s", code)
            return code
        except Exception as e:  # pylint: disable=broad-except
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("SolarEdge One: check_login failed: %s", e)
            return 0

    def requestLogicalLayout(self):
        """Return raw JSON string of site structure (v2) for compatibility with code that parses text."""
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("SolarEdge One: requestLogicalLayout for site %s", self.siteid)
        path = f"/services/layout/logical/generic/v2/site/{self.siteid}"
        data = self._get(path, params={"include-optimizers": "true"})
        return json.dumps(data)

    def requestListOfAllPanels(self):
        """Return SolarEdgeSite built from v2 site structure (cached)."""
        now = datetime.now()
        if (
            self._panels_cache is None
            or self._panels_cache_time is None
            or (now - self._panels_cache_time) > self._panels_cache_ttl
        ):
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("SolarEdge One: Panels cache miss, fetching site structure")
            raw = self._get(
                f"/services/layout/logical/generic/v2/site/{self.siteid}",
                params={"include-optimizers": "true"},
            )
            self._panels_cache = _site_structure_v2_to_solar_edge_site(self.siteid, raw)
            self._panels_cache_time = now
            _LOGGER.info(
                "SolarEdge One: Refreshed panels cache with %s optimizers",
                self._panels_cache.returnNumberOfOptimizers(),
            )
        else:
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "SolarEdge One: Using cached panels (age: %s)",
                    now - self._panels_cache_time,
                )
        return self._panels_cache

    def get_optimizer_temperatures_cached(self):
        """
        Return dict optimizer_serial -> temperature (float, Celsius) from layout/energy/site/.../by-inverter
        with include-max-temperature=true. Cached for 15 minutes.
        """
        now = datetime.now()
        if (
            self._temperature_cache is not None
            and self._temperature_cache_time is not None
            and (now - self._temperature_cache_time) <= self._temperature_cache_ttl
        ):
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "SolarEdge One: Using cached optimizer temperatures (age: %s)",
                    now - self._temperature_cache_time,
                )
            return self._temperature_cache
        try:
            site = self.requestListOfAllPanels()
            inverter_serials = [inv.serialNumber for inv in site.inverters if getattr(inv, "serialNumber", None)]
            if not inverter_serials:
                self._temperature_cache = {}
                self._temperature_cache_time = now
                return self._temperature_cache
            today = now.strftime("%Y-%m-%d")
            path = f"/services/layout/energy/site/{self.siteid}/by-inverter"
            params = {
                "start-date": today,
                "end-date": today,
                "inverter-serials": ",".join(inverter_serials),
                "include-max-temperature": "true",
            }
            data = self._get(path, params=params, timeout=30)
            result = {}
            for inv_block in data.get("inverters") or []:
                for opt in inv_block.get("optimizers") or []:
                    serial = (opt.get("serial") or "").strip()
                    temp_obj = opt.get("temperature")
                    if serial and isinstance(temp_obj, dict):
                        t = temp_obj.get("temperature")
                        if t is not None:
                            try:
                                result[serial] = round(float(t), 1)
                            except (TypeError, ValueError):
                                pass
            self._temperature_cache = result
            self._temperature_cache_time = now
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "SolarEdge One: Refreshed optimizer temperature cache (%d optimizers)",
                    len(result),
                )
            return result
        except Exception as e:  # pylint: disable=broad-except
            _LOGGER.warning("SolarEdge One: Optimizer temperatures fetch failed: %s. Using previous cache.", e)
            if self._temperature_cache is None:
                self._temperature_cache = {}
        return self._temperature_cache or {}

    def _description_from_basic(self, basic: dict, item_id: str) -> str:
        """Build panel description from basic info modules, or fallback to item_id."""
        modules = basic.get("modules") or []
        if modules and isinstance(modules[0], dict):
            mod = modules[0]
            return f"{mod.get('manufacturer') or 'Unknown'} {mod.get('model') or ''}".strip() or item_id
        return item_id

    def _measurements_from_live(self, live: dict) -> dict:
        """Build measurements dict from live API response (power, current, voltage to 2 dp where applicable)."""
        measurements = {}
        if live.get("power_W") is not None:
            measurements["Power [W]"] = round(float(live["power_W"]), 2)
        if live.get("current_A") is not None:
            measurements["Current [A]"] = live["current_A"]
        if live.get("voltage_V") is not None:
            measurements["Voltage [V]"] = round(float(live["voltage_V"]), 2)
        if live.get("optimizerVoltage_V") is not None:
            measurements["Optimizer Voltage [V]"] = round(float(live["optimizerVoltage_V"]), 2)
        return measurements

    def _build_optimizer_data_from_response(self, item_id: str, live: dict, basic: dict):
        """Build SolarEdgeOptimizerData from API live/basic dicts for one optimizer. Returns None on error."""
        last_measurement = live.get("lastMeasurement") or ""
        model = basic.get("model") or ""
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "SolarEdge One: Building optimizer data for %s model=%r last_measurement=%s has_live=%s",
                item_id,
                model or "(none)",
                last_measurement or "(none)",
                bool(live),
            )
        desc = self._description_from_basic(basic, item_id)
        measurements = self._measurements_from_live(live)
        json_object = {
            "serialNumber": item_id,
            "description": desc,
            "lastMeasurementDate": last_measurement,
            "model": model,
            "manufacturer": "SolarEdge",
            "measurements": measurements,
        }
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "SolarEdge One: Decoded data for optimizer %s: %s",
                item_id,
                json_object,
            )
        try:
            return SolarEdgeOptimizerData(
                item_id, json_object, self._timezone, has_valid_measurements=bool(measurements)
            )
        except Exception as e:  # pylint: disable=broad-except
            _LOGGER.error("SolarEdge One: Error building optimizer data for %s: %s", item_id, e)
            return None

    def _post_optimizers_with_retry(self, path: str, payload: list, timeout: int = 60):
        """POST to optimizer endpoint with longer timeout and one retry on read/connect timeout."""
        try:
            return self._post(path, payload, timeout=timeout)
        except requests.exceptions.Timeout as e:
            _LOGGER.warning(
                "SolarEdge One: Timeout requesting optimizer data (retrying once): %s", e
            )
            return self._post(path, payload, timeout=timeout)

    def requestSystemData(self, item_id: str):
        """
        Fetch live data for one optimizer by serial (e.g. 130FCCF8-E6).
        Returns SolarEdgeOptimizerData or None.
        """
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("SolarEdge One: requestSystemData for optimizer %s", item_id)
        path = "/services/layout/information/optimizers"
        try:
            data = self._post_optimizers_with_retry(path, [item_id])
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code >= 500:
                _LOGGER.warning("SolarEdge One: Temporary server error (HTTP %s) for optimizer %s", e.response.status_code, item_id)
            raise
        basic_list = data.get("basicInformationList") or []
        live_map = data.get("serialToLiveData") or {}
        live = live_map.get(item_id) or {}
        basic = next((b for b in basic_list if (b.get("serial") or "").strip() == (item_id or "").strip()), None) or {}
        info = self._build_optimizer_data_from_response(item_id, live, basic)
        if info is not None:
            temp_map = self.get_optimizer_temperatures_cached()
            if item_id in temp_map:
                info.temperature = temp_map[item_id]
        return info

    def requestSystemDataBatch(self, item_ids: list):
        """
        Fetch live data for multiple optimizers in one API call.
        Used by the coordinator for lightweight "has any panel updated?" checks so we sample
        several panels (different orientations) instead of one that might be in shade.
        Returns list of SolarEdgeOptimizerData (or None for failed items); order matches item_ids.
        """
        if not item_ids:
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("SolarEdge One: requestSystemDataBatch called with empty list")
            return []
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "SolarEdge One: requestSystemDataBatch for %d optimizer(s): %s",
                len(item_ids),
                item_ids,
            )
        path = "/services/layout/information/optimizers"
        try:
            data = self._post_optimizers_with_retry(path, list(item_ids))
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code >= 500:
                _LOGGER.warning(
                    "SolarEdge One: Temporary server error (HTTP %s) during batch light check",
                    e.response.status_code,
                )
            raise
        basic_list = data.get("basicInformationList") or []
        live_map = data.get("serialToLiveData") or {}
        result = []
        for item_id in item_ids:
            live = live_map.get(item_id) or {}
            basic = next((b for b in basic_list if (b.get("serial") or "").strip() == (str(item_id) or "").strip()), None) or {}
            result.append(self._build_optimizer_data_from_response(item_id, live, basic))
        temp_map = self.get_optimizer_temperatures_cached()
        for i, item_id in enumerate(item_ids):
            if result[i] is not None and item_id in temp_map:
                result[i].temperature = temp_map[item_id]
        return result

    def _fetch_all_optimizer_data(self, optimizer_ids: list) -> list:
        """Fetch live data for many optimizers in parallel. Returns list of SolarEdgeOptimizerData (successful only)."""
        if not optimizer_ids:
            return []
        max_workers = min(os.cpu_count() or 4, len(optimizer_ids), 10)
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "SolarEdge One: requestAllData fetching %d optimizers with max_workers=%d",
                len(optimizer_ids),
                max_workers,
            )
        data = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_id = {
                executor.submit(self.requestSystemData, oid): oid
                for oid in optimizer_ids
            }
            for future in as_completed(future_to_id):
                oid = future_to_id[future]
                try:
                    info = future.result()
                    if info is not None:
                        data.append(info)
                except Exception as e:  # pylint: disable=broad-except
                    _LOGGER.error("SolarEdge One: Error fetching data for optimizer %s: %s", oid, e)
        return data

    def _attach_lifetime_energy_and_temperatures(
        self, data_list: list, lifetimeenergy: dict, temperature_map: dict
    ) -> None:
        """Attach lifetime_energy and temperature to each optimizer data item in place."""
        for info in data_list:
            oid = info.panel_id
            energy_data = lifetimeenergy.get(str(oid)) or lifetimeenergy.get(oid) or {}
            kWh = _lifetime_energy_to_kwh(energy_data)
            info.lifetime_energy = round(kWh, 3) if kWh is not None else 0.0
            if oid in temperature_map:
                info.temperature = temperature_map[oid]

    def requestAllData(self):
        """Fetch live data for all optimizers and attach lifetime energy (same interface as legacy API)."""
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("SolarEdge One: requestAllData starting")
        site = self.requestListOfAllPanels()
        optimizer_ids = [
            opt.optimizerId
            for inv in site.inverters
            for s in inv.strings
            for opt in s.optimizers
        ]
        data = self._fetch_all_optimizer_data(optimizer_ids)
        lifetimeenergy = self.get_lifetime_energy_cached()
        temperature_map = self.get_optimizer_temperatures_cached()
        self._attach_lifetime_energy_and_temperatures(data, lifetimeenergy, temperature_map)
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("SolarEdge One: requestAllData complete, %d optimizers with data", len(data))
        return data

    def getLifeTimeEnergy(self):
        """
        Return lifetime energy data keyed by optimizer serial (same shape as legacy layout/energy).
        Each entry: { "unscaledEnergy": total_wh }.
        SolarEdge One has no single "all layout energy" call; we build from energy-graph per optimizer (cached in get_lifetime_energy_cached).
        """
        return json.dumps(self.get_lifetime_energy_cached())

    def _fetch_one_optimizer_lifetime(self, serial: str) -> tuple[str, dict | None]:
        """Fetch lifetime energy for one optimizer. Returns (serial_str, entry or None)."""
        try:
            path = f"/services/layout/energy-graph/site/{self.siteid}/optimizers"
            params = {
                "chart-time-unit": "years",
                "start-date": "2010-01-01",
                "end-date": datetime.now().strftime("%Y-%m-%d"),
                "optimizer-serials": serial,
            }
            data = self._get(path, params=params, timeout=30)
            total_wh = data.get("totalEnergy")
            if total_wh is not None:
                return (str(serial), {"unscaledEnergy": float(total_wh)})
        except Exception as e:  # pylint: disable=broad-except
            _LOGGER.warning("SolarEdge One: Lifetime energy for %s failed: %s", serial, e)
        return (str(serial), None)

    def _fetch_lifetime_energy_uncached(self) -> dict:
        """Fetch lifetime energy for all optimizers (no cache). Returns dict serial -> { unscaledEnergy: wh }."""
        site = self.requestListOfAllPanels()
        serials = [
            opt.optimizerId
            for inv in site.inverters
            for s in inv.strings
            for opt in s.optimizers
        ]
        result = {}
        max_workers = min(os.cpu_count() or 4, len(serials), 10)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_serial = {
                executor.submit(self._fetch_one_optimizer_lifetime, s): s for s in serials
            }
            for future in as_completed(future_to_serial):
                serial, entry = future.result()
                if entry is not None:
                    result[serial] = entry
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("SolarEdge One: Refreshed lifetime energy cache (%d optimizers)", len(result))
            _LOGGER.debug(
                "SolarEdge One: Decoded lifetime energy data (by optimizer serial): %s",
                result,
            )
        return result

    def get_lifetime_energy_cached(self):
        """Return dict serial -> { unscaledEnergy: wh } (refresh at most hourly)."""
        now = datetime.now()
        if (
            self._lifetime_energy_cache is None
            or self._lifetime_energy_cache_time is None
            or (now - self._lifetime_energy_cache_time) > self._lifetime_energy_cache_ttl
        ):
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("SolarEdge One: Lifetime energy cache miss, fetching per-optimizer")
            try:
                self._lifetime_energy_cache = self._fetch_lifetime_energy_uncached()
                self._lifetime_energy_cache_time = now
            except Exception as e:  # pylint: disable=broad-except
                _LOGGER.warning("SolarEdge One: Lifetime energy fetch failed: %s. Using previous cache.", e)
                if self._lifetime_energy_cache is None:
                    self._lifetime_energy_cache = {}
        else:
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "SolarEdge One: Using cached lifetime energy (age: %s, %d entries)",
                    now - self._lifetime_energy_cache_time,
                    len(self._lifetime_energy_cache or {}),
                )
        return self._lifetime_energy_cache or {}

    def close(self):
        """Clear tokens and release resources."""
        if self._access_token and _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("SolarEdge One: close() clearing access token")
        self._access_token = None
        self._refresh_token = None
