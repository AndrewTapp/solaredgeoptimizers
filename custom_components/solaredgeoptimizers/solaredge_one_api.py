"""
SolarEdge Optimizers Integration - SolarEdge One API Client (solaredge_one_api.py)

This module implements the client for the modern SolarEdge One portal API, which provides
richer data and better performance compared to the legacy Monitoring API.

Authentication:
- Uses OAuth 2.0 with PKCE (Proof Key for Code Exchange) flow
- Authenticates via login.solaredge.com, exchanges authorization code for access token
- Bearer token used for all /services/ API calls

API Endpoints (monitoring.solaredge.com/services/...):

1. Site Structure:
   GET .../layout/logical/generic/v2/site/{siteId}?include-optimizers=true
   Returns hierarchical site layout with inverters, strings, and optimizers

2. Optimizer Live Data:
   POST .../layout/information/optimizers (body: list of serials)
   Returns basicInformationList (serial, model) and serialToLiveData (power, voltage, current)
   Full refresh: one batch POST with all optimizer serials. Lightweight checks: one batch with up to 5 representative serials.

3. Inverter Information:
   GET .../layout/information/inverters?inverter-serials=...
   Returns fullModel (e.g. SE5000H-RW000BNN4) for device registry

4. Site Information:
   GET .../layout/information/site/{siteId}
   Returns installationDate, peakPower (kW), etc. Cached per SITE_INFO_CACHE_TTL (e.g. 2 h).

5. Optimizer Temperatures:
   GET .../layout/energy/site/{siteId}/by-inverter?include-max-temperature=true
   Returns per-optimizer temperature (auto-converts Fahrenheit to Celsius)
   Cached per TEMPERATURE_CACHE_TTL (e.g. 30 min)

6. Lifetime Energy (per-optimizer):
   GET .../layout/energy-graph/site/{siteId}/optimizers?optimizer-serials=...
   Fetches per-optimizer lifetime energy in parallel (thread pool)
   Cached per LIFETIME_ENERGY_CACHE_TTL (e.g. 1 hour)

7. Dashboard Site Production:
   GET .../dashboard/energy/sites/{siteId}?start-date=...&measurement-types=production,yield
   Returns summary.production in Wh for site lifetime when > aggregated.

8. Layout Energy by Inverter:
   GET .../layout/energy/site/{siteId}/by-inverter?start-date=...
   Returns inverter and string lifetime energy (Wh) for portal-override when > aggregated.

Key Features:
- Automatic retry on timeout for optimizer data requests
- Temperature unit normalization (F to C conversion)
- Duplicate name resolution with letter suffixes for same-position devices
- Azimuth and tilt extraction from optimizer module data (radians to degrees)
- Inverter nodes include maxActivePower (API watts → stored as kW) from layout for inverter-level sensor
- Panel cache (PANELS_CACHE_TTL_ONE), site info (SITE_INFO_CACHE_TTL), temperature (TEMPERATURE_CACHE_TTL), lifetime (LIFETIME_ENERGY_CACHE_TTL)
- No persistent sessions; requests use context managers so no file descriptors are held; close() clears tokens.
"""
import math
import base64
import hashlib
import json
import logging
import os
import secrets
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from requests.sessions import Session
import pytz

from .const import (
    USER_AGENT,
    API_TIMEOUT_SHORT,
    API_TIMEOUT_LONG,
    MAX_PARALLEL_WORKERS,
    BASE_URL_MONITORING,
    LOGIN_BASE,
    SOLAREDGE_ONE_CLIENT_ID,
    MFE_AUTH_PATH,
    MFE_AUTH_CALLBACK_PATH,
    TOKEN_PATH,
    PANELS_CACHE_TTL_ONE,
    SITE_INFO_CACHE_TTL,
    LIFETIME_ENERGY_CACHE_TTL,
    TEMPERATURE_CACHE_TTL,
    is_status_active,
)
from .solaredgeoptimizers import (
    SolarEdgeSite,
    SolarEdgeOptimizerData,
    _lifetime_energy_to_kwh,
)

_LOGGER = logging.getLogger(__name__)

BASE_URL = BASE_URL_MONITORING
MFE_AUTH_URL = f"{BASE_URL_MONITORING}{MFE_AUTH_PATH}"
MFE_AUTH_CALLBACK = f"{BASE_URL_MONITORING}{MFE_AUTH_CALLBACK_PATH}"
TOKEN_URL = f"{LOGIN_BASE}{TOKEN_PATH}"


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
    with session.get(login_url, timeout=API_TIMEOUT_SHORT) as r:
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
    with session.post(post_url, data=post_body, headers=post_headers, timeout=API_TIMEOUT_SHORT, allow_redirects=True) as r:
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("SolarEdge One: POST login -> %s, URL: %s", r.status_code, r.url)
        if r.status_code >= 400 and _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("SolarEdge One: login POST %s response: %s", r.status_code, (r.text or "")[:500])
        final_url = r.url
        if r.status_code == 204 and "Location" in r.headers:
            callback_url = r.headers["Location"]
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("SolarEdge One: 204 response, following Location: %s", callback_url)
            with session.get(callback_url, timeout=API_TIMEOUT_SHORT, allow_redirects=True) as r2:
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
    with session.post(TOKEN_URL, data=token_data, headers=token_headers, timeout=API_TIMEOUT_SHORT) as r:
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


def _v2_build_optimizer_logical_node(opt_node: dict, resolved_name: str = None) -> dict:
    """Build one optimizer logical node (data only, no children) for SolarEdgeSite."""
    opt_props = opt_node.get("properties") or {}
    opt_serial = opt_node.get("serial") or opt_props.get("identifier") or opt_node.get("uuid", "")
    opt_name = resolved_name if resolved_name else (opt_node.get("name") or opt_serial)
    opt_display = opt_node.get("displayOrder") or opt_name
    opt_order = opt_node.get("order") or 0
    status_raw = opt_props.get("status") or ""
    status = status_raw.capitalize() if status_raw else ""
    if _LOGGER.isEnabledFor(logging.DEBUG):
        _LOGGER.debug(
            "SolarEdge One: Building optimizer node serial=%s name=%s status=%s",
            opt_serial, opt_name, status or "(none)",
        )
    return {
        "data": {
            "id": opt_serial,
            "serialNumber": opt_serial,
            "name": opt_name,
            "displayName": opt_display,
            "relativeOrder": opt_order,
            "type": "OPTIMIZER",
            "operationsKey": opt_node.get("uuid") or "",
            "status": status,
        }
    }


def _make_duplicate_sort_key(get_status, get_serial, get_position):
    """Create a sort key function for duplicate name resolution (blank or ACTIVE = active first)."""
    def sort_key(idx_item):
        idx, item = idx_item
        status = get_status(item) or ""
        is_active = 1 if is_status_active(status) else 0
        serial = get_serial(item) if get_serial else ""
        position = get_position(item) if get_position else idx
        return (-is_active, serial if serial else "", position)
    return sort_key


def _resolve_duplicate_names_with_suffix(items: list, get_name, get_status, get_serial, get_position) -> dict:
    """Resolve duplicate names by adding suffixes (a, b, c...) based on status and serial/position.
    
    Returns dict mapping item index -> resolved name.
    Active items come first (sorted by serial/position), then other statuses (sorted by serial/position).
    First active item keeps original name, subsequent get 'a', 'b', etc. suffixes.
    """
    name_groups = defaultdict(list)
    for idx, item in enumerate(items):
        name = get_name(item)
        name_groups[name].append(idx)
    
    resolved = {}
    sort_key = _make_duplicate_sort_key(get_status, get_serial, get_position)
    for name, indices in name_groups.items():
        if len(indices) == 1:
            resolved[indices[0]] = name
            continue
        
        group_items = [(idx, items[idx]) for idx in indices]
        sorted_items = sorted(group_items, key=sort_key)
        
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "SolarEdge One: Resolving %d duplicate names for '%s': %s",
                len(sorted_items),
                name,
                [(get_status(items[idx]) or "unknown", get_serial(items[idx]) if get_serial else idx) for idx, _ in sorted_items],
            )
        
        suffix_idx = 0
        for i, (idx, item) in enumerate(sorted_items):
            if i == 0:
                resolved[idx] = name
            else:
                suffix = chr(ord('a') + suffix_idx)
                suffix_idx += 1
                if "." in name:
                    parts = name.rsplit(".", 1)
                    resolved[idx] = f"{parts[0]}.{parts[1]}{suffix}"
                else:
                    resolved[idx] = f"{name}{suffix}"
        
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "SolarEdge One: Resolved names for '%s': %s",
                name,
                {idx: resolved[idx] for idx, _ in sorted_items},
            )
    
    return resolved


def _v2_build_string_logical_children(str_node: dict) -> list:
    """Build list of optimizer logical nodes from a v2 STRING node's OPTIMIZER folders."""
    raw_optimizers = []
    for opt_folder in (str_node.get("children") or []):
        if (opt_folder.get("type") or "").upper() != "FOLDER" or (opt_folder.get("name") or "").upper() != "OPTIMIZER":
            continue
        for opt_node in (opt_folder.get("children") or []):
            if (opt_node.get("type") or "").upper() != "OPTIMIZER":
                continue
            raw_optimizers.append(opt_node)
    
    resolved_names = _resolve_duplicate_names_with_suffix(
        raw_optimizers,
        get_name=lambda o: o.get("name") or o.get("serial") or "",
        get_status=lambda o: (o.get("properties") or {}).get("status") or "",
        get_serial=lambda o: o.get("serial") or "",
        get_position=None,
    )
    
    opt_children = []
    for idx, opt_node in enumerate(raw_optimizers):
        resolved_name = resolved_names.get(idx)
        opt_children.append(_v2_build_optimizer_logical_node(opt_node, resolved_name))
    return opt_children


def _v2_build_string_logical_node(str_node: dict, resolved_name: str = None) -> dict:
    """Build one string logical node (data + optimizer children) for SolarEdgeSite."""
    str_props = str_node.get("properties") or {}
    str_identifier = str_props.get("identifier") or str_node.get("uuid") or str_node.get("name", "")
    str_serial = str_node.get("serial") or str_identifier
    str_name = resolved_name if resolved_name else (str_node.get("name") or str_identifier)
    str_display = str_node.get("displayOrder") or str_name
    str_order = str_node.get("order") or 0
    status_raw = str_props.get("status") or ""
    status = status_raw.capitalize() if status_raw else ""
    if _LOGGER.isEnabledFor(logging.DEBUG):
        _LOGGER.debug(
            "SolarEdge One: Building string node id=%s name=%s status=%s",
            str_identifier, str_name, status or "(none)",
        )
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
            "status": status,
        },
        "children": opt_children,
    }


def _v2_build_inverter_logical_children(inv_node: dict) -> list:
    """Build list of string logical nodes from a v2 INVERTER node's STRING folders."""
    raw_strings = []
    for str_folder in (inv_node.get("children") or []):
        if (str_folder.get("type") or "").upper() != "FOLDER" or (str_folder.get("name") or "").upper() != "STRING":
            continue
        for str_node in (str_folder.get("children") or []):
            if (str_node.get("type") or "").upper() != "STRING":
                continue
            raw_strings.append(str_node)
    
    resolved_names = _resolve_duplicate_names_with_suffix(
        raw_strings,
        get_name=lambda s: s.get("name") or "",
        get_status=lambda s: (s.get("properties") or {}).get("status") or "",
        get_serial=None,
        get_position=lambda s: s.get("order") or 0,
    )
    
    string_children = []
    for idx, str_node in enumerate(raw_strings):
        resolved_name = resolved_names.get(idx)
        string_children.append(_v2_build_string_logical_node(str_node, resolved_name))
    return string_children


def _v2_build_inverter_logical_node(inv_node: dict, resolved_name: str = None) -> dict:
    """Build one inverter logical node (data + string children) for SolarEdgeSite."""
    inv_props = inv_node.get("properties") or {}
    inv_identifier = inv_props.get("identifier") or inv_node.get("serial") or inv_node.get("uuid", "")
    inv_serial = inv_node.get("serial") or inv_identifier
    inv_name = resolved_name if resolved_name else (inv_node.get("name") or f"Inverter {inv_identifier}")
    inv_display = inv_node.get("displayOrder") or inv_name
    inv_order = inv_node.get("order") or 0
    status_raw = inv_props.get("status") or ""
    status = status_raw.capitalize() if status_raw else ""
    manufacturer = inv_props.get("manufacturer") or "SolarEdge"
    model = inv_props.get("model") or inv_props.get("partNumber") or ""
    # maxActivePower from API is in watts; store as kW for display (same as site peak_power)
    max_active_w = inv_props.get("maxActivePower")
    max_active_kw = (float(max_active_w) / 1000.0) if max_active_w is not None else None
    if _LOGGER.isEnabledFor(logging.DEBUG):
        _LOGGER.debug(
            "SolarEdge One: Building inverter node serial=%s name=%s status=%s manufacturer=%s model=%s maxActivePower=%s kW",
            inv_serial, inv_name, status or "(none)", manufacturer, model or "(none)", max_active_kw,
        )
    string_children = _v2_build_inverter_logical_children(inv_node)
    data = {
        "id": inv_identifier,
        "serialNumber": inv_serial,
        "name": inv_name,
        "displayName": inv_display,
        "relativeOrder": inv_order,
        "type": "INVERTER",
        "operationsKey": inv_node.get("uuid") or "",
        "status": status,
        "manufacturer": manufacturer,
        "model": model,
    }
    if max_active_kw is not None:
        data["maxActivePower"] = max_active_kw
    return {
        "data": data,
        "children": string_children,
    }


def _v2_build_logical_children(structure: dict) -> list:
    """Build list of inverter logical nodes from v2 site structure (FOLDER->INVERTER hierarchy)."""
    raw_inverters = []
    for inv_folder in _v2_find_children_by_type(structure, "FOLDER"):
        if (inv_folder.get("name") or "").upper() != "INVERTER":
            continue
        for inv_node in (inv_folder.get("children") or []):
            if (inv_node.get("type") or "").upper() != "INVERTER":
                continue
            raw_inverters.append(inv_node)
    
    resolved_names = _resolve_duplicate_names_with_suffix(
        raw_inverters,
        get_name=lambda i: i.get("name") or "",
        get_status=lambda i: (i.get("properties") or {}).get("status") or "",
        get_serial=lambda i: i.get("serial") or "",
        get_position=None,
    )
    
    logical_children = []
    for idx, inv_node in enumerate(raw_inverters):
        resolved_name = resolved_names.get(idx)
        logical_children.append(_v2_build_inverter_logical_node(inv_node, resolved_name))
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
        self._panels_cache_ttl = PANELS_CACHE_TTL_ONE
        self._site_info_cache = None
        self._site_info_cache_time = None
        self._site_info_cache_ttl = SITE_INFO_CACHE_TTL
        self._lifetime_energy_cache = None
        self._lifetime_energy_cache_time = None
        self._lifetime_energy_cache_ttl = LIFETIME_ENERGY_CACHE_TTL
        self._temperature_cache = None
        self._temperature_cache_time = None
        self._temperature_cache_ttl = TEMPERATURE_CACHE_TTL
        self._access_token = None
        self._refresh_token = None
        self._closed = False

    def _ensure_token(self) -> str:
        """Obtain OAuth access_token via PKCE flow and token exchange. Returns access_token."""
        if self._closed:
            raise RuntimeError("SolarEdge One API client is closed")
        if self._access_token:
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("SolarEdge One: Using cached access token")
            return self._access_token

        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("SolarEdge One: No token; starting OAuth PKCE login flow")
        with Session() as session:
            session.headers["User-Agent"] = USER_AGENT
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
            "User-Agent": USER_AGENT,
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/",
        }

    def _get(self, path: str, params: dict | None = None, timeout: int = API_TIMEOUT_LONG):
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

    def _post(self, path: str, json_data, timeout: int = API_TIMEOUT_LONG):
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

    def get_site_info_cached(self) -> dict:
        """
        Fetch site information (installation date, peak power) from layout/information/site.
        Returns dict with keys installationDate (str "YYYY-MM-DD"), peakPower (float kW).
        Cached per SITE_INFO_CACHE_TTL (same as layout cache).
        """
        now = datetime.now()
        if (
            self._site_info_cache is not None
            and self._site_info_cache_time is not None
            and (now - self._site_info_cache_time) <= self._site_info_cache_ttl
        ):
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "SolarEdge One: Using cached site info (age=%s)",
                    now - self._site_info_cache_time,
                )
            return self._site_info_cache
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("SolarEdge One: Site info cache miss (TTL=%s), fetching", self._site_info_cache_ttl)
        try:
            path = f"/services/layout/information/site/{self.siteid}"
            data = self._get(path, timeout=API_TIMEOUT_SHORT)
            result = {}
            if data.get("installationDate") is not None:
                result["installationDate"] = str(data["installationDate"])
            if data.get("peakPower") is not None:
                try:
                    result["peakPower"] = float(data["peakPower"])
                except (TypeError, ValueError):
                    pass
            self._site_info_cache = result
            self._site_info_cache_time = now
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("SolarEdge One: Fetched site info: %s", result)
            return result
        except Exception as e:  # pylint: disable=broad-except
            _LOGGER.warning("SolarEdge One: Site info fetch failed: %s. Using previous cache.", e)
            return self._site_info_cache if self._site_info_cache is not None else {}

    def get_dashboard_site_production_cached(self, installation_date: str | None) -> float | None:
        """
        Fetch site lifetime production (Wh) from dashboard/energy summary.production.
        start-date=installation_date, end-date=today. Cached per LIFETIME_ENERGY_CACHE_TTL.
        Returns production in watt-hours or None on failure/missing.
        """
        if not installation_date:
            return None
        now = datetime.now()
        cache_key = (installation_date, now.strftime("%Y-%m-%d"))
        if getattr(self, "_dashboard_production_cache_key", None) == cache_key and getattr(self, "_dashboard_production_cache", None) is not None:
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("SolarEdge One: Using cached dashboard production")
            return self._dashboard_production_cache
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "SolarEdge One: Dashboard production cache miss (start=%s), fetching",
                installation_date,
            )
        try:
            end_date = now.strftime("%Y-%m-%d")
            path = f"/services/dashboard/energy/sites/{self.siteid}"
            params = {
                "start-date": installation_date,
                "end-date": end_date,
                "chart-time-unit": "years",
                "measurement-types": "production,yield",
            }
            data = self._get(path, params=params, timeout=API_TIMEOUT_SHORT)
            summary = data.get("summary") or {}
            prod = summary.get("production")
            if prod is not None:
                # Portal returns production in watt-hours (Wh), e.g. 2.7341768E7
                val = float(prod)
                self._dashboard_production_cache = val
                self._dashboard_production_cache_key = cache_key
                if _LOGGER.isEnabledFor(logging.DEBUG):
                    _LOGGER.debug("SolarEdge One: Dashboard production (Wh): %s", val)
                return val
        except Exception as e:  # pylint: disable=broad-except
            _LOGGER.warning("SolarEdge One: Dashboard production fetch failed: %s", e)
        return getattr(self, "_dashboard_production_cache", None)

    def get_layout_energy_by_inverter_cached(self, installation_date: str | None) -> dict:
        """
        Fetch inverter and string lifetime energy (Wh) from layout/energy by-inverter.
        start-date=installation_date. Returns dict: inv_serial -> {"energy_wh": float, "strings": {string_relative_order: float}}.
        Cached per LIFETIME_ENERGY_CACHE_TTL.
        """
        if not installation_date:
            return {}
        now = datetime.now()
        cache_key = (installation_date, now.strftime("%Y-%m-%d"))
        if getattr(self, "_by_inverter_energy_cache_key", None) == cache_key and getattr(self, "_by_inverter_energy_cache", None) is not None:
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("SolarEdge One: Using cached by-inverter energy")
            return self._by_inverter_energy_cache
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "SolarEdge One: By-inverter energy cache miss (start=%s), fetching",
                installation_date,
            )
        try:
            site = self.requestListOfAllPanels()
            inverter_serials = [inv.serialNumber for inv in site.inverters if getattr(inv, "serialNumber", None)]
            if not inverter_serials:
                return {}
            end_date = now.strftime("%Y-%m-%d")
            path = f"/services/layout/energy/site/{self.siteid}/by-inverter"
            params = {
                "start-date": installation_date,
                "end-date": end_date,
                "inverter-serials": ",".join(inverter_serials),
                "include-max-temperature": "false",
            }
            data = self._get(path, params=params, timeout=API_TIMEOUT_LONG)
            # Portal returns energy values in watt-hours (Wh), e.g. 2.8453492E7
            result = {}
            for inv_block in data.get("inverters") or []:
                serial = (inv_block.get("serial") or "").strip()
                if not serial:
                    continue
                inv_energy = inv_block.get("energy")
                energy_wh = None
                if isinstance(inv_energy, dict):
                    v = inv_energy.get("value")
                    if v is not None:
                        try:
                            energy_wh = float(v)  # Wh
                        except (TypeError, ValueError):
                            pass
                strings_map = {}
                for s in inv_block.get("strings") or []:
                    order = s.get("stringRelativeOrder")
                    if order is None:
                        continue
                    try:
                        order = int(order)
                    except (TypeError, ValueError):
                        continue
                    e = s.get("energy")
                    if isinstance(e, dict):
                        v = e.get("value")
                        if v is not None:
                            try:
                                strings_map[order] = float(v)  # Wh
                            except (TypeError, ValueError):
                                pass
                result[serial] = {"energy_wh": energy_wh, "strings": strings_map}
            self._by_inverter_energy_cache = result
            self._by_inverter_energy_cache_key = cache_key
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("SolarEdge One: By-inverter energy cache refreshed: %d inverters", len(result))
            return result
        except Exception as e:  # pylint: disable=broad-except
            _LOGGER.warning("SolarEdge One: By-inverter energy fetch failed: %s", e)
        return getattr(self, "_by_inverter_energy_cache", {})

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
            data = self._get(path, params=params, timeout=API_TIMEOUT_SHORT)
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
            self._get(path, params={"include-optimizers": "true"}, timeout=API_TIMEOUT_SHORT)
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
                _LOGGER.debug(
                    "SolarEdge One: Panels cache miss (TTL=%s), fetching site structure",
                    self._panels_cache_ttl,
                )
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
                    "SolarEdge One: Using cached panels (age=%s, TTL=%s)",
                    now - self._panels_cache_time,
                    self._panels_cache_ttl,
                )
        return self._panels_cache

    def _parse_temperature_response(self, data: dict) -> tuple[dict[str, float], int]:
        """
        Parse layout/energy by-inverter API response into optimizer_serial -> temperature (Celsius).
        Returns (result_dict, fahrenheit_count) for logging.
        """
        result = {}
        fahrenheit_count = 0
        for inv_block in data.get("inverters") or []:
            for opt in inv_block.get("optimizers") or []:
                serial = (opt.get("serial") or "").strip()
                temp_obj = opt.get("temperature")
                if not serial or not isinstance(temp_obj, dict):
                    continue
                t = temp_obj.get("temperature")
                if t is None:
                    continue
                try:
                    temp_val = float(t)
                    unit = (temp_obj.get("temperatureUnit") or "CELSIUS").strip().upper()
                    # Portal may send FAHRENHEIT or F (e.g. US). Convert to Celsius for storage.
                    if unit in ("FAHRENHEIT", "F"):
                        raw_f = temp_val
                        temp_val = (temp_val - 32.0) * (5.0 / 9.0)
                        fahrenheit_count += 1
                        if _LOGGER.isEnabledFor(logging.DEBUG):
                            _LOGGER.debug(
                                "SolarEdge One: Temperature unit %s for optimizer %s: %.1f°F -> %.1f°C",
                                unit, serial, raw_f, temp_val,
                            )
                    result[serial] = round(temp_val, 1)
                except (TypeError, ValueError):
                    pass
        return result, fahrenheit_count

    def get_optimizer_temperatures_cached(self):
        """
        Return dict optimizer_serial -> temperature (float, Celsius) from layout/energy/site/.../by-inverter
        with include-max-temperature=true. Cached per TEMPERATURE_CACHE_TTL.
        """
        now = datetime.now()
        if (
            self._temperature_cache is not None
            and self._temperature_cache_time is not None
            and (now - self._temperature_cache_time) <= self._temperature_cache_ttl
        ):
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "SolarEdge One: Using cached optimizer temperatures (age=%s, TTL=%s)",
                    now - self._temperature_cache_time,
                    self._temperature_cache_ttl,
                )
            return self._temperature_cache
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "SolarEdge One: Temperature cache miss or expired (TTL=%s), fetching",
                self._temperature_cache_ttl,
            )
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
            data = self._get(path, params=params, timeout=API_TIMEOUT_SHORT)
            result, fahrenheit_count = self._parse_temperature_response(data)
            self._temperature_cache = result
            self._temperature_cache_time = now
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "SolarEdge One: Refreshed optimizer temperature cache (%d optimizers%s)",
                    len(result),
                    f", {fahrenheit_count} in Fahrenheit (converted to °C)" if fahrenheit_count else "",
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

    def _extract_azimuth_tilt_from_basic(self, basic: dict) -> tuple:
        """Extract azimuth and tilt from basic info modules, converting from radians to degrees."""
        modules = basic.get("modules") or []
        if not modules or not isinstance(modules[0], dict):
            return None, None
        mod = modules[0]
        azimuth_rad = mod.get("azimuth")
        tilt_rad = mod.get("tilt")
        azimuth_deg = None
        tilt_deg = None
        if azimuth_rad is not None:
            try:
                azimuth_deg = round(math.degrees(float(azimuth_rad)), 2)
            except (TypeError, ValueError):
                pass
        if tilt_rad is not None:
            try:
                tilt_deg = round(math.degrees(float(tilt_rad)), 2)
            except (TypeError, ValueError):
                pass
        return azimuth_deg, tilt_deg

    def _build_optimizer_data_from_response(self, item_id: str, live: dict, basic: dict):
        """Build SolarEdgeOptimizerData from API live/basic dicts for one optimizer. Returns None on error."""
        last_measurement = live.get("lastMeasurement") or ""
        model = basic.get("model") or ""
        # Prefer API description (panel type e.g. 'SunPower SPR-MAX3-400') when present; else build from modules
        api_desc = (basic.get("description") or live.get("description") or "").strip()
        desc = api_desc if api_desc else self._description_from_basic(basic, item_id)
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "SolarEdge One: Building optimizer data for %s model=%r panel_type=%r last_measurement=%s has_live=%s",
                item_id,
                model or "(none)",
                desc or "(none)",
                last_measurement or "(none)",
                bool(live),
            )
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
            opt_data = SolarEdgeOptimizerData(
                item_id, json_object, self._timezone, has_valid_measurements=bool(measurements)
            )
            azimuth, tilt = self._extract_azimuth_tilt_from_basic(basic)
            opt_data.azimuth = azimuth
            opt_data.tilt = tilt
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "SolarEdge One: Optimizer %s azimuth=%s° tilt=%s°",
                    item_id, azimuth, tilt,
                )
            return opt_data
        except Exception as e:  # pylint: disable=broad-except
            _LOGGER.error("SolarEdge One: Error building optimizer data for %s: %s", item_id, e)
            return None

    def _post_optimizers_with_retry(self, path: str, payload: list, timeout: int = API_TIMEOUT_LONG):
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
        """Fetch live data for all optimizers in one batch POST. Returns list of SolarEdgeOptimizerData (successful only)."""
        if not optimizer_ids:
            return []
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "SolarEdge One: requestAllData fetching %d optimizers (single batch)",
                len(optimizer_ids),
            )
        try:
            result = self.requestSystemDataBatch(optimizer_ids)
            return [item for item in result if item is not None]
        except Exception as e:  # pylint: disable=broad-except
            _LOGGER.warning(
                "SolarEdge One: Batch optimizer fetch failed (%s), falling back to per-optimizer",
                e,
            )
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "SolarEdge One: Per-optimizer fallback: fetching %d optimizers with ThreadPoolExecutor",
                    len(optimizer_ids),
                )
            # Fallback: one request per optimizer if batch fails
            data = []
            with ThreadPoolExecutor(
                max_workers=min(os.cpu_count() or 4, len(optimizer_ids), MAX_PARALLEL_WORKERS)
            ) as executor:
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
                    except Exception as err:  # pylint: disable=broad-except
                        _LOGGER.error(
                            "SolarEdge One: Error fetching data for optimizer %s: %s",
                            oid,
                            err,
                        )
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
            data = self._get(path, params=params, timeout=API_TIMEOUT_SHORT)
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
        max_workers = min(os.cpu_count() or 4, len(serials), MAX_PARALLEL_WORKERS)
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
                _LOGGER.debug(
                    "SolarEdge One: Lifetime energy cache miss (TTL=%s), fetching per-optimizer",
                    self._lifetime_energy_cache_ttl,
                )
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
                    "SolarEdge One: Using cached lifetime energy (age=%s, TTL=%s, %d entries)",
                    now - self._lifetime_energy_cache_time,
                    self._lifetime_energy_cache_ttl,
                    len(self._lifetime_energy_cache or {}),
                )
        return self._lifetime_energy_cache or {}

    def close(self):
        """Clear tokens and release resources. Idempotent; safe to call multiple times.

        One API uses requests.get/post with context managers (no persistent Session),
        so no file descriptors are held. Clearing tokens ensures no reuse after close.
        """
        if self._closed:
            return
        self._closed = True
        if self._access_token and _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("SolarEdge One: close() clearing access token")
        self._access_token = None
        self._refresh_token = None
