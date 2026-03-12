# SolarEdge Optimizers – Technical Documentation

**Full documentation for the SolarEdge Optimizers Home Assistant integration.**  
Use this as the main wiki page or copy sections into your GitHub wiki.

---

## Table of contents

1. [Overview](#1-overview)
2. [Architecture](#2-architecture)
3. [Device and entity hierarchy](#3-device-and-entity-hierarchy)
4. [Data flow and polling](#4-data-flow-and-polling)
5. [Installation](#5-installation)
6. [Configuration](#6-configuration)
7. [Sensors and entities reference](#7-sensors-and-entities-reference)
8. [Update behaviour and caches](#8-update-behaviour-and-caches)
9. [Inactive devices](#9-inactive-devices)
10. [Offline and stale data handling](#10-offline-and-stale-data-handling)
11. [Internationalization (i18n)](#11-internationalization-i18n)
12. [API client and SolarEdge portal](#12-api-client-and-solaredge-portal)
13. [Troubleshooting and logging](#13-troubleshooting-and-logging)
14. [File structure and constants](#14-file-structure-and-constants)
15. [Credits and links](#15-credits-and-links)

---

## 1. Overview

The **SolarEdge Optimizers** integration pulls data from the SolarEdge monitoring portal into Home Assistant. It exposes:

- **Per-optimizer (per-panel)** sensors: voltage, current, optimizer voltage, power, temperature (SolarEdge One only), lifetime energy, last measurement, status, azimuth, and tilt.
- **Aggregated** sensors at **string**, **inverter**, and **site** level: current (average), voltage (average), power, lifetime energy, last measurement, child counts (optimizer/string/inverter count), and status (strings and inverters only).
- A **Last polled** sensor and an **Obtained from** sensor on the site device (last polled = when the integration last fetched data; obtained from = "One API" or "Legacy API" depending on which source provided the current data).

### Features

| Feature | Description |
|--------|-------------|
| **Config flow** | Single-step setup: Site ID, username, password, optional Entity ID prefix, optional Include Site ID in Entity ID, Use SolarEdge One (default on). Field order: site ID → username → password → Entity ID prefix → Include Site ID in Entity ID → Use SolarEdge One. One config entry per site (same site cannot be added twice). Validation uses dual API: login succeeds if either SolarEdge One or legacy returns 200. |
| **Re-authentication** | When the API returns 401 (invalid or expired credentials), the integration raises `ConfigEntryAuthFailed`; Home Assistant shows a re-auth form (username, password). Only credentials are updated; options (entity ID prefix, Include Site ID in Entity ID, Use SolarEdge One) are preserved. Re-auth step and abort messages are translated. |
| **Options (reconfigure)** | Entity ID prefix, Include Site ID in Entity ID, and Use SolarEdge One can be changed via the integration’s Configure (options flow) without removing the entry. The form shows Entity ID prefix (description shows current prefix; leave empty to remove it), Include Site ID in Entity ID, and Use SolarEdge One. Saving reloads the integration. Documented: changing prefix or Include Site ID can change entity IDs and unique_ids, so history/statistics can be lost. |
| **Cloud polling** | Uses SolarEdge’s cloud API; no local hardware discovery. |
| **Adaptive polling** | Coordinator runs every **5 minutes** (`UPDATE_DELAY`). Lightweight checks: when data is **fresh**, desired interval about **5 minutes** (`LIGHT_CHECK_DESIRED_INTERVAL_FRESH`); when **stale or missing**, about **30 minutes** (`LIGHT_CHECK_DESIRED_INTERVAL_STALE`). Full refresh when new data is detected, on first boot, or **every 30 minutes when data is from legacy** (re-try One). A full refresh is not triggered again within **5 minutes** of the last one (`LIGHT_CHECK_MIN_INTERVAL`). When using **SolarEdge One:** up to `LIGHT_CHECK_BATCH_SIZE` (5) optimizers chosen at random, one batch API call (`requestSystemDataBatch`). When using **legacy API:** one representative optimizer. The integration tries One first; if One has no valid measurements or fails, it uses legacy. |
| **Optimizer live data (full refresh)** | **SolarEdge One:** one batch POST (all serials). **Legacy:** parallel per-optimizer requests. |
| **Sensor setup optimization** | After the coordinator’s first refresh, the sensor platform uses `coordinator.data` for optimizer info when present and only calls the API for optimizers missing from that data, avoiding duplicate fetches and speeding setup. |
| **Caching** | Logical layout: **2 h** when using SolarEdge One (`PANELS_CACHE_TTL_ONE`), **1 h** when using legacy (`PANELS_CACHE_TTL_LEGACY`). Lifetime energy: **1 h** (`LIFETIME_ENERGY_CACHE_TTL`). Optimizer temperatures (One only): **30 min** (`TEMPERATURE_CACHE_TTL`). |
| **Multi-language** | Config flow (including re-auth step and abort messages) and entity names translated; API locale follows HA language. |
| **Stale handling** | Live values (V, I, P) zeroed when last measurement is older than threshold: **1 hour** when data is from One API, **2 hours** when from Legacy API. The **Obtained from** sensor on the site device shows "One API" or "Legacy API". |
| **Reliability** | Temporary server/network errors (5xx, DNS) handled with cached data. **Session/connection cleanup:** On remove/reload, all API sessions and connections are closed. **SolarEdge One** uses no persistent session (each request uses `requests.get`/`requests.post` with context managers; OAuth login uses `with Session() as session`); `close()` clears tokens and marks the client closed. **Legacy** uses thread-local sessions; every session created (including those in the thread pool) is tracked and closed on unload, avoiding file descriptor leaks. Dual API `close()` is called on unload and closes both backends. **SolarEdge One** optimizer data requests use `API_TIMEOUT_LONG` (60s) timeout and one automatic retry on read/connect timeout. API requests use configurable timeouts (`API_TIMEOUT_SHORT` = 30s, `API_TIMEOUT_LONG` = 60s). |
| **Removal cleanup** | When the integration is deleted from **Settings → Integrations**, config flow’s `async_remove_entry` closes the API client (releasing file descriptors) if the coordinator is still in `hass.data`, then calls the shared helper `remove_entities_and_devices_for_entry` (defined in `__init__.py`), which removes all associated entities and devices from the registries. The same helper is used on unload. No leftover registry entries remain. |
| **Hardware replacement** | Optimizer, string, and inverter devices and entities are identified by **logical position** (e.g. inverter index, string index, optimizer index), not by serial number. When an optimizer or inverter is replaced (e.g. after a hardware failure), the same device and sensors continue to show the new unit’s data after the next refresh; no duplicate entities. Data is keyed by position so sensor lookup finds the unit currently at that position. If duplicates already exist from an earlier swap, remove the integration and add it again to clean up. |
| **Duplicate name handling** | When the API returns multiple inverters, strings, or optimizers with the same name (e.g. after hardware replacement), the integration resolves duplicates automatically: active (including blank status) items come first (sorted by serial number for inverters/optimizers, by position for strings), then other statuses. The first active item keeps the original name; subsequent duplicates get alphabetical suffixes (e.g. "Inverter 1a", "String 1.0a", "Optimizer 1.0.1a"). |

### Requirements

- **Home Assistant** (tested with recent versions).
- **SolarEdge monitoring account**: Site ID, username (email), password.
- **Network**: Outbound HTTPS to `monitoring.solaredge.com` (legacy and SolarEdge One both use this host; SolarEdge One also uses `login.solaredge.com` for OAuth).
- **Python dependency**: `jsonfinder==0.4.2` (in `manifest.json`; used by the legacy API client only).

---

## 2. Architecture

High-level components and how they interact:

```mermaid
flowchart TB
    subgraph HA["Home Assistant"]
        CF[Config Flow]
        INIT[__init__.py]
        COORD[DataUpdateCoordinator]
        SENSOR[sensor platform]
    end

    subgraph INT["Integration (solaredgeoptimizers)"]
        API[Dual API: One first, legacy fallback]
    end

    subgraph SE["SolarEdge Cloud"]
        GW[API Gateway]
        WEB[Web / systemData]
    end

    U((User)) --> CF
    CF -->|Validate credentials| API
    API -->|HTTPS| GW
    INIT -->|Create API + Coordinator| COORD
    COORD -->|Poll data| API
    API -->|Layout, optimizer batch (One), energy| GW
    API -->|systemData per optimizer (legacy)| WEB
    COORD -->|Data dict| SENSOR
    SENSOR -->|Entities| U
```

### Component roles

| Component | Role |
|-----------|------|
| **Config flow** | Collects Site ID, username, password, optional Entity ID prefix, optional Include Site ID in Entity ID; validates via dual API `check_login()` (succeeds if either One or legacy returns 200); creates config entry with translated title. On setup, migrates existing entries to ensure `use_solaredge_one` is set (default True) if missing. On removal, `async_remove_entry` closes the API client (if present) then calls the shared helper `remove_entities_and_devices_for_entry` to clear entities and devices for that config entry. |
| **`__init__.py`** | Runs migration to ensure `use_solaredge_one` exists in data/options (default True); sets up dual API client (`SolarEdgeDualAPI` from `api_dual.py`, with HA timezone, language, and `use_solaredge_one`), runs login check, creates coordinator, runs first refresh, forwards to sensor platform. Defines `remove_entities_and_devices_for_entry(hass, entry)`, used by both unload and config flow for registry cleanup. |
| **Coordinator** | Runs every `UPDATE_DELAY` (**5 min**); implements adaptive polling (light check vs full refresh); when data is from legacy, forces full refresh every 30 min (`REVERT_TO_ONE_RETRY_INTERVAL`) to re-try One; when not doing a full refresh, still refreshes optimizer temperatures (SolarEdge One) via `_refresh_temperature_when_no_full_refresh()` when the temperature cache (`TEMPERATURE_CACHE_TTL`, 30 min) expires; builds data dict keyed by serial and by (inv_idx, str_idx, opt_idx) for optimizers so hardware swap keeps same sensor; full refresh timeout **30 min** (`COORDINATOR_REFRESH_TIMEOUT_SEC`, default 1800 s); exposes `_obtained_from` ("One API" or "Legacy API") to the Obtained from sensor. Types the API client via `SolarEdgeAPIProtocol` (`api.py`). |
| **Sensor platform** | At setup, removes existing sensor entities for this config entry so new entity IDs match current options (e.g. prefix, Include Site ID). Creates one device per site, inverter, string, optimizer (at string/optimizer level keyed by parsed API display name when it parses, else position). **String and inverter aggregated sensors** use the same position-based device identifiers and `via_device` as the coordinator (`_set_aggregated_device_info`), so they attach to the devices created in `_register_site_and_inverter_devices` and the hierarchy (site → inverter → string → optimizer) is correct; avoids “references a non existing via_device” and duplicate inverter devices. Creates sensors (individual + aggregated + **Last polled** + **Obtained from** on site device). Optimizer sensors look up coordinator data by (inv_idx, str_idx, opt_idx) first so replacement hardware at the same position updates the same entity. Uses `coordinator.data` for optimizer info when available (after first refresh), only calling the API for optimizers missing from that data; deduplicates optimizer tasks by position. |
| **API client** | **Dual API** (`api_dual.py`): when `use_solaredge_one` is True, tries **SolarEdge One** (`solaredge_one_api.py`) first; if One returns no valid measurements or fails (e.g. login), uses **legacy** (`solaredgeoptimizers.py`). When `use_solaredge_one` is False, always uses legacy only. Exposes `_obtained_from` for the site-level sensor. Both backends conform to `SolarEdgeAPIProtocol` (`api.py`). Layout and lifetime energy cached; **One API:** single batch POST for full-refresh optimizer data; **legacy:** parallel per-optimizer; locale/language from HA. |

---

## 3. Device and entity hierarchy

How the physical layout maps to Home Assistant devices and entities:

```mermaid
flowchart TD
    subgraph Site[Site device]
        LP[Last polled]
        OF[Obtained from]
        S_P[Power]
        S_V[Voltage average]
        S_C[Current average]
        S_E[Lifetime energy]
        S_L[Last measurement]
        S_I[Inverter count]
    end

    subgraph Inv[Inverter device]
        I_P[Power]
        I_V[Voltage average]
        I_C[Current average]
        I_E[Lifetime energy]
        I_L[Last measurement]
        I_S[String count]
        I_ST[Status]
    end

    subgraph Str[String device]
        R_P[Power]
        R_V[Voltage average]
        R_C[Current average]
        R_E[Lifetime energy]
        R_L[Last measurement]
        R_O[Optimizer count]
        R_ST[Status]
    end

    subgraph Opt[Optimizer device]
        O_P[Power]
        O_V[Voltage]
        O_C[Current]
        O_OV[Optimizer voltage]
        O_T[Temperature]
        O_E[Lifetime energy]
        O_L[Last measurement]
        O_S[Status]
        O_AZ[Azimuth]
        O_TI[Tilt]
    end

    Site --> Inv
    Inv --> Str
    Str --> Opt
```

- **Site** → **Inverters** → **Strings** → **Optimizers**.  
- **Device names** include the site so multiple sites don’t clash: **Site [site]**, **Inverter [site].[i]**, **String [site].[i].[s]**, **Optimizer [site].[i].[s].[o]** (e.g. Site 9999999, Inverter 9999999.1, String 9999999.1.1, Optimizer 9999999.1.1.1).  
- **Device model and serial:** When using **SolarEdge One**, each **optimizer** device shows the optimizer **model** (e.g. P405-4RM4MRM-NA25) and serial number from the API; when the API provides a **panel type** (description, e.g. SunPower SPR-MAX3-400), it is appended to the device model (e.g. P405-4RM4MRM-NA25 - SunPower SPR-MAX3-400) and exposed as a **panel_type** attribute on optimizer sensors. Each **inverter** device shows the inverter **model** (e.g. SE5000H-RW000BNN4, from `fullModel`) and serial number. Legacy API does not fetch model for inverters; optimizer model may come from layout where available.  
- **Device identity:** At **string and optimizer** level, device names and entity IDs are **based on the API display name** when it parses (e.g. "1.0", "1.0.1"); if the display name does not parse, position-based indices are used. Site and inverter stay position-based. Devices and sensor data are keyed by logical position (not serial number). The coordinator creates site, inverter, and string devices with these identifiers; the sensor platform attaches **aggregated** (string/inverter) sensors to the same devices so the hierarchy (site → inverter → string → optimizer) is correct and there is no “non existing via_device” or duplicate inverter. When an optimizer or inverter is replaced (hardware swap), the same device and sensors show the new unit’s data after the next refresh; no duplicate entities. Device identifiers use `entry_id_inv_{i}`, `entry_id_str_{i}_{s}`, `entry_id_opt_{i}_{s}_{o}` (for string/optimizer the indices come from the parsed API display name when available) so identity is stable across hardware replacement.  
- **Entity IDs** follow a path that may or may not include the site ID, depending on **Include SiteID in EntityID** (default off). When off: site level always has the site ID (e.g. `sensor.[base]power_[site]`); inverter/string/optimizer levels omit it (e.g. `sensor.[base]power_[i]_[s]_[o]`). When on: all levels include the site ID (e.g. `sensor.[base]power_[site]_[i]_[s]_[o]`). At string/optimizer level the path reflects the API display name when it parses (e.g. 1_0, 1_0_1). *[base]* is the optional Entity ID prefix from config (blank if not set). Entity IDs have no device-name prefix. This keeps entity IDs short by default while still unique per site.  
- In **Settings → Devices & services**, “Connected via” shows the parent (e.g. optimizer → string, string → inverter). Optimizers are grouped under their string device.  
- Entity names are translated (e.g. “Power”, “Last measurement”) and combined with the device name.

---

## 4. Data flow and polling

### Setup (first load)

```mermaid
sequenceDiagram
    participant U as User
    participant HA as Home Assistant
    participant C as Coordinator
    participant API as API client
    participant SE as SolarEdge

    U->>HA: Add integration (Site ID, user, pass)
    HA->>API: check_login()
    Note over API: Dual API: try One, then legacy if needed
    API->>SE: GET layout/logical (One or legacy)
    SE-->>API: 200 + layout
    API-->>HA: 200
    HA->>C: Create coordinator, first refresh
    C->>C: _async_update_data: if _site_structure None, await _async_setup()
    C->>API: requestListOfAllPanels() [layout cache]
    API->>SE: GET layout/logical (if cache miss)
    SE-->>API: layout JSON
    API-->>C: SolarEdgeSite (inverters/strings/optimizers)
    C->>HA: Register site/inverter/string devices (_register_site_and_inverter_devices)
    C->>API: requestAllData()
    Note over API: Dual API: try One first - if no valid measurements or fail then call legacy
    API->>SE: getLifeTimeEnergy / layout/energy (cached 1h)
    API->>SE: requestSystemDataBatch(all) [One] or requestSystemData(opt_id) × N [legacy]
    SE-->>API: per-optimizer data
    API-->>C: list of SolarEdgeOptimizerData + lifetime, _obtained_from set
    C->>C: _calculate_aggregated_data and store _obtained_from
    C-->>HA: data_dict (panel_id + position → data)
    HA->>C: async_setup_entry → sensor platform
    Note over HA,C: Sensor platform uses coordinator.data for optimizer info - only calls API for missing data. Position-based unique_ids avoid duplicate ID errors.
    HA->>U: Sensors appear
```

### Ongoing updates (adaptive polling)

```mermaid
flowchart LR
    subgraph Every 5 min
        TICK[Coordinator tick]
    end

    TICK --> FIRST{First boot or no data?}
    FIRST -->|Yes| FULL[Full refresh: requestAllData]
    FIRST -->|No| LIGHT{Time for light check?}
    LIGHT -->|No| REUSE[Reuse existing data]
    LIGHT -->|Yes| ONE[Light check: 1 opt legacy / batch of 5 random SE One]
    ONE --> NEW{New data?}
    NEW -->|Yes| FULL
    NEW -->|No| REUSE
    FULL --> AGG[Aggregate string/inverter/site]
    REUSE --> AGG
    AGG --> LAST[Update last_polled and obtained_from]
```

**Note:** A full refresh is also triggered when data is from legacy and at least 30 minutes have passed since the last full refresh (re-try One so the integration can switch back). When the coordinator **reuses** existing data (no full refresh), it still refreshes optimizer temperatures if the API supports it (e.g. SolarEdge One) via `_refresh_temperature_when_no_full_refresh()`, using `get_optimizer_temperatures_cached()` so temperatures stay updated when the temperature cache expires (30 min, `TEMPERATURE_CACHE_TTL`).

- **Light check interval**: About **5 minutes** when data is recent (`LIGHT_CHECK_DESIRED_INTERVAL_FRESH`); about **30 minutes** when data is old or missing (`LIGHT_CHECK_DESIRED_INTERVAL_STALE`). A full refresh is not triggered again within **5 minutes** of the last one (`LIGHT_CHECK_MIN_INTERVAL`).  
- **Light check strategy**: Which API is used follows the **last full refresh**. When the dual API last used **legacy** for full data, the light check uses a single representative optimizer (`requestSystemData`). When it last used **SolarEdge One**, the light check uses up to `LIGHT_CHECK_BATCH_SIZE` (5) optimizers chosen at random; one batch request (`requestSystemDataBatch`) returns live data for all of them. If any has a newer `lastMeasurement` than the coordinator’s latest, a full refresh is triggered. Random selection avoids always checking the same panel (e.g. one that’s often in shade).  
- **Full refresh**: Coordinator calls the dual API's `requestAllData()`: dual API tries One first; if no valid measurements or fail, uses legacy; sets `_obtained_from`. **SolarEdge One** fetches optimizer live data in one batch POST (`requestSystemDataBatch` with all serials); **legacy** uses parallel per-optimizer requests. Lifetime energy from cache when possible. When data is currently from **legacy** (`_obtained_from` = Legacy API), the coordinator forces a full refresh **every 30 minutes** (`REVERT_TO_ONE_RETRY_INTERVAL` in `const.py`) so One is re-tried and the integration can switch back when One is available.  
- **Lifetime energy**: Dual API's `get_lifetime_energy_cached()` returns the cache from whichever API was last used for full data (One or legacy). TTL 1 hour; aggregations from that cache.  
- **Sensor setup**: After the coordinator’s first refresh, the sensor platform receives `data_dict` and uses it for optimizer info when creating entities; it only calls the API for optimizers missing from that data, so setup avoids duplicate fetches and is faster.

---

## 5. Installation

### Via HACS (recommended)

1. **HACS** → **Custom repositories** → add `https://github.com/AndrewTapp/solaredgeoptimizers` as **Integration**.  
2. **Integrations** → find **SolarEdge Optimizers** → **Download**.  
3. **Restart Home Assistant.**  
4. **Settings** → **Devices & services** → **Add Integration** → search **SolarEdge Optimizers**.

### Manual

1. Clone or download the repo into `custom_components/solaredgeoptimizers/`.  
2. Restart Home Assistant.  
3. Add the integration as above.

Ensure `custom_components/solaredgeoptimizers/` contains at least: `__init__.py`, `api.py`, `api_dual.py`, `config_flow.py`, `const.py`, `coordinator.py`, `manifest.json`, `sensor.py`, `solaredgeoptimizers.py`, `solaredge_one_api.py`, `strings.json`, and the `translations/` folder.

---

## 6. Configuration

- **Single step**: Site ID, Username (email), Password, optional **Entity ID prefix**, and optional **Include Site ID in Entity ID** (default **off**). **Field order** in the form: Site ID → Username → Password → Entity ID prefix → Include Site ID in Entity ID.  
- **Use SolarEdge One**: Optional, default **on**. When **Yes**, the integration uses the dual API (see below). When **No**, the integration always uses the legacy portal only. Stored in data on first setup; can be changed in options (Configure). Migration ensures existing entries have this set (default True) if missing.
- **Dual API** (when Use SolarEdge One is on): The integration tries the **SolarEdge One API** first (`monitoring.solaredge.com/services/layout/...`). If One returns no valid measurements (e.g. all optimizers with empty measurements) or One login fails, it automatically falls back to the **legacy** SolarEdge API. When One starts returning valid data again, the integration switches back to One—either when the light check sees newer data from One, or **every 30 minutes** when data is currently from legacy (coordinator forces a full refresh to re-try One). A site-level **Obtained from** sensor shows "One API" or "Legacy API". When data is from One, optimizer and inverter devices show model (e.g. P405-4RM4MRM-NA25, SE5000H-RW000BNN4) and serial; when the API provides a panel type (description), it is included in the optimizer device model and as the **panel_type** attribute on optimizer sensors. Validation: `check_login()` succeeds if either One or legacy returns 200.  
- **One config entry per site**: The config flow sets `unique_id` to the Site ID. If you try to add the same site again, `_abort_if_unique_id_configured()` runs and the flow aborts with "Device is already configured". Multiple sites require one integration entry per site (each with a different Site ID).  
- **Entity ID prefix**: Optional. If set (e.g. `se_`), all entity IDs start with that prefix (e.g. `sensor.se_power_9999999`). Normalised to lowercase with spaces as underscores. Leave blank for no prefix. Useful when running multiple sites or avoiding clashes with other integrations. When upgrading from an older version without removing the integration, this defaults to blank if the key is not present.  
- **Include Site ID in Entity ID**: Optional, default **off**. When off, inverter/string/optimizer entity IDs omit the site ID (e.g. `sensor.power_1_1`); site level always includes the actual site ID (e.g. `sensor.power_9999999`). When on, all levels include the site ID in the path. When upgrading from an older version without removing the integration, this defaults to off if the key is not present.  
- **Validation**: Uses dual API `check_login()`: tries SolarEdge One first, then legacy; success if either returns 200.  
- **Config entry title**: Translated, e.g. “SolarEdge Site 12345” (from `config.title_entry` with `%(siteid)s`).  
- **Errors**: “Failed to connect”, “Invalid authentication”, “Unexpected error” (keys `cannot_connect`, `invalid_auth`, `unknown`); all translatable.  
- **Abort**: “Device is already configured” when the same site is already set up; "Re-authentication successful" and "Re-authentication could not find the config entry." for the re-auth flow (all translatable).  
- **Re-authentication**: If the API returns 401 (invalid or expired credentials), `__init__.py` raises `ConfigEntryAuthFailed`. Home Assistant then starts the reauth flow: `async_step_reauth` sets `unique_id` and runs `async_step_reauth_confirm`, which shows a form (title, description, username, password) from `config.step.reauth_confirm`. On success, only the entry’s username and password are updated in `entry.data`; options (entity ID prefix, Include Site ID in Entity ID, Use SolarEdge One) are left unchanged. The integration is then reloaded. Abort reasons: `reauth_successful`, `reauth_entry_missing`. All re-auth strings are in `translations/<code>.json`.  
- **Options (reconfigure)**: Users can change **Entity ID prefix** or **Include Site ID in Entity ID** without deleting the entry: use the integration’s **Configure** (options flow). The flow shows a form (step_id `init`; translations from **options.step.init**: title, description, data.entity_id_prefix, data.include_site_id_in_entity_id, data.use_solaredge_one). **Field order**: Entity ID prefix, then Include Site ID in Entity ID, then Use SolarEdge One. The description shows the current prefix (`{current_entity_id_prefix}`); leave the Entity ID prefix field empty to remove the prefix. Values are stored in `entry.options` and override `entry.data` when the integration reads config. On save, the options flow updates the entry and triggers a reload so sensors are recreated. **Important:** Changing these options can change entity IDs and `unique_id`s. When `unique_id` or entity ID changes, Home Assistant may treat entities as new, so **history and statistics for the previous entities can be lost**. The options form description warns users and recommends backing up or exporting data first.

No YAML configuration is required; all configuration is via the config flow.

---

## 7. Sensors and entities reference

### Per-optimizer (individual panel)

| Sensor | Device class | Unit | Description |
|--------|--------------|------|-------------|
| Power | power | W | Instantaneous power. |
| Voltage | voltage | V | Panel voltage. |
| Current | current | A | Panel current. |
| Optimizer voltage | voltage | V | Optimizer output voltage. |
| Temperature | temperature | °C | Optimizer temperature from SolarEdge One API (layout/energy by-inverter with `include-max-temperature`). Portal may send °C or °F (`temperatureUnit`); integration converts to °C for storage; HA displays in your preferred unit. Only available when using One API; shows “unknown” when missing or when using legacy API. Refreshed when the temperature cache expires (30 min, `TEMPERATURE_CACHE_TTL`) even when the coordinator does not do a full refresh. Not zeroed when stale. |
| Lifetime energy | energy | kWh | Total energy (monotonic). Sourced from the API’s `unscaledEnergy` (Wh); the portal’s `units` field applies only to display values `energy` and `moduleEnergy`. At site level, when aggregated optimizer data is unreliable (e.g. very small total while portal has a real total), the site uses the portal’s total (sum of all `unscaledEnergy` from layout/energy) instead of aggregating. |
| Last measurement | timestamp | — | Time of last measurement from portal. |
| Status | — | — | Optimizer status from API. **Blank** (empty) is treated as active and displayed as **blank** with the active icon. Shown in proper case: "Active", "Inactive", or raw value for any other status. Icon: check-circle for Active/blank, alert-circle for Inactive, help-circle for unknown. |
| Azimuth | — | ° | Panel compass direction (0–360°), converted from radians. Only available when API provides module orientation data. Icon: compass. |
| Tilt | — | ° | Panel angle from horizontal in degrees, converted from radians. Only available when API provides module orientation data. Icon: angle-acute. |

- **Panel type (attribute):** When the SolarEdge One API provides a description (panel type, e.g. SunPower SPR-MAX3-400), it is exposed as a **panel_type** attribute on each optimizer sensor and included in the optimizer device model. Entity IDs are unchanged.

- **Stale rule**: If last measurement is older than the threshold (**1 h** when data is from One API, **2 h** when from Legacy API—see **Obtained from** sensor), **Power, Voltage, Current, Optimizer voltage** are shown as **0**. **Temperature** (when from SolarEdge One) is not zeroed; it shows the last known value or unknown if missing. Lifetime energy, Last measurement, Status, Azimuth, and Tilt always show last known value.

### Per-string (aggregated)

| Sensor | Description |
|--------|--------------|
| Power | Sum of optimizer power (with recent data). |
| Current (average) | Average current of optimizers with recent data. |
| Voltage (average) | Average voltage of optimizers with recent data. |
| Lifetime energy | Sum of optimizer lifetime energy (from API, by string; uses `unscaledEnergy` in Wh). Site level: when reliable, sum of inverters; when unreliable (aggregated below `RELIABLE_THRESHOLD_KWH` and portal total ≥ that threshold), uses portal total (sum of all `unscaledEnergy` from layout/energy). |
| Last measurement | Latest last measurement among optimizers in the string. |
| Optimizer count | Number of **active** (status blank or "Active") optimizers in the string (always an integer). |
| Status | String status from API. Blank → "blank" (active icon); "Inactive" → Inactive (inactive icon); other → raw value (unknown icon). |

### Per-inverter (aggregated)

| Sensor | Description |
|--------|--------------|
| Power | Sum of string power. |
| Current (average) / Voltage (average) | Averages over strings with recent data. |
| Lifetime energy | Sum of string lifetime energy. |
| Last measurement | Latest among strings. |
| String count | Number of **active** (status blank or "Active") strings under the inverter (always an integer). |
| Status | Inverter status from API. Blank → "blank" (active icon); "Inactive" → Inactive (inactive icon); other → raw value (unknown icon). |

### Per-site (aggregated)

| Sensor | Description |
|--------|--------------|
| Same as inverter | But over all inverters. |
| Inverter count | Number of **active** (status blank or "Active") inverters (always an integer). |
| **Last polled** | (Site device only.) When the integration last successfully finished an update. |
| **Obtained from** | (Site device only.) Which API provided the current data: **"One API"** or **"Legacy API"**. Entity ID: `sensor.[prefix]obtained_from_[site]` or `sensor.[prefix]obtained_from` when site ID is not included in entity IDs. |

All aggregated sensors use the same naming pattern (e.g. “Power”, “Current (average)”) with the device name indicating the level. Entity IDs include the path so they are unique. When **Include Site ID in Entity ID** is **off** (default), inverter/string/optimizer IDs omit the site; site level and Last polled always show the site ID. When **on**, all levels include the site ID.

| Level    | Example (prefix blank, Include SiteID **off**) | Example (prefix blank, Include SiteID **on**) |
|----------|------------------------------------------------|-----------------------------------------------|
| Site     | `sensor.power_9999999`                         | `sensor.power_9999999`                        |
| Inverter | `sensor.power_1`                               | `sensor.power_9999999_1`                      |
| String   | `sensor.power_1_1`                             | `sensor.power_9999999_1_1`                    |
| Optimizer| `sensor.power_1_1_1`, `sensor.temperature_1_1_1` | `sensor.power_9999999_1_1_1`, `sensor.temperature_9999999_1_1_1` |
| Last polled | `sensor.last_polled`                        | `sensor.last_polled_9999999`                  |
| Obtained from | `sensor.obtained_from`                    | `sensor.obtained_from_9999999`                |

Child-count sensors: `inverter_count` at site level, `string_count` at inverter level, and `optimizer_count` at string level, with the same path suffix.

---

## 8. Update behaviour and caches

| Item | Interval / TTL | Notes |
|------|----------------|--------|
| Coordinator tick | 5 minutes | `UPDATE_DELAY` in `const.py`. |
| Light check | ~5 min (recent data) or ~30 min (old/none) | Desired interval: `LIGHT_CHECK_DESIRED_INTERVAL_FRESH` (5 min) when data fresh, `LIGHT_CHECK_DESIRED_INTERVAL_STALE` (30 min) when stale/missing. Full refresh not retriggered within `LIGHT_CHECK_MIN_INTERVAL` (5 min). When last full data was from **legacy:** single optimizer `requestSystemData`. When from **SolarEdge One:** up to `LIGHT_CHECK_BATCH_SIZE` (5) optimizers at random, one `requestSystemDataBatch`; if any has newer data, full refresh. |
| Full refresh | When light check sees new data, first boot / no data, or **every 30 min when data is from legacy** (re-try One) | Dual API `requestAllData()`: tries One first; if no valid measurements or fail, uses legacy. Returns all optimizers + lifetime energy; sets `_obtained_from`. **SolarEdge One:** optimizer live data via **one batch POST** (`requestSystemDataBatch` with all serials); when the lifetime-energy cache is cold, `get_lifetime_energy_cached()` fetches per-optimizer energy in **parallel** (thread pool, up to `MAX_PARALLEL_WORKERS` = 10). **Legacy:** parallel per-optimizer requests for live data. `REVERT_TO_ONE_RETRY_INTERVAL` = 30 min. **Timeout:** 30 minutes (`COORDINATOR_REFRESH_TIMEOUT_SEC`, 1800 s). |
| Layout (panels) cache | 2 h (One) / 1 h (legacy) | One API: `PANELS_CACHE_TTL_ONE` (2 h). Legacy: `PANELS_CACHE_TTL_LEGACY` (1 h). `requestListOfAllPanels()` (dual API prefers One; fallback legacy). |
| Lifetime energy cache | 1 hour | `LIFETIME_ENERGY_CACHE_TTL`. Dual API `get_lifetime_energy_cached()` returns cache from the API that was last used for full data (One or legacy). **SolarEdge One:** on cache miss, per-optimizer energy-graph requests run in parallel (thread pool). Converted to kWh from `unscaledEnergy` (Wh); `units` applies only to display fields. |
| Optimizer temperatures cache (One only) | 30 minutes | `TEMPERATURE_CACHE_TTL`. SolarEdge One `get_optimizer_temperatures_cached()`; layout/energy by-inverter with `include-max-temperature=true`. API may return °C or °F per `temperatureUnit`; integration normalizes to °C. When the coordinator does **not** do a full refresh (e.g. reuses data after a light check), it still calls `_refresh_temperature_when_no_full_refresh()`, which uses this cache so optimizer temperatures are updated when the cache expires (30 min) even when power/voltage are not. Merged into optimizer data on full refresh and on this optional refresh. Debug: when unit is Fahrenheit, logs per-optimizer conversion (raw °F → °C) and summary count. |
| Full-refresh cooldown | 5 minutes | `LIGHT_CHECK_MIN_INTERVAL` in `const.py`; avoids triggering a full refresh again within 5 minutes of the last full refresh when the light check detects new data. |

Aggregations (string/inverter/site) are computed in the coordinator from optimizer data and cached lifetime energy; they are not separate API calls. **Site lifetime energy** uses aggregated data when reliable; when aggregated is below `RELIABLE_THRESHOLD_KWH` (100 kWh) and the portal total (sum of all `unscaledEnergy` from layout/energy) is at least that threshold, the site uses the portal total so installations with unreliable per-optimizer lifetime data still get a correct site total.

---

## 9. Inactive devices

When an optimizer, string, or inverter is marked as **Inactive** in the SolarEdge portal, certain sensors are not created because they are not meaningful for inactive/disconnected devices:

### Sensors excluded for inactive devices

| Device type | Sensors NOT created | Sensors still created |
|-------------|---------------------|----------------------|
| **Optimizer** | Azimuth, Current, Optimizer voltage, Power, Temperature, Tilt, Voltage | Lifetime energy, Last measurement, Status |
| **String** | Current (average), Power, Voltage (average) | Lifetime energy, Last measurement, Optimizer count, Status |
| **Inverter** | Current (average), Power, Voltage (average) | Lifetime energy, Last measurement, String count, Status |

### Aggregation behaviour

**Aggregation values** (power, current, voltage, lifetime energy) at string, inverter, and site level include data from **all** devices (any status) that have recent measurements. Averages (current, voltage) use the count of devices that contributed data; totals (power, lifetime energy) sum all contributing devices.

**Child counts** (Optimizer count per string, String count per inverter, Inverter count per site) count only **active** devices (status **blank** or **"Active"**) and are always integers. This lets you see how many active vs inactive devices exist at each level.

**Note:** Lifetime energy and last measurement are still tracked for inactive devices and shown in their individual sensors. Inactive devices contribute to aggregated power/current/voltage/lifetime when they have recent data; they are excluded from the child-count sensors.

---

## 10. Offline and stale data handling

- **Threshold**: When data is from **One API**, the coordinator uses `CHECK_TIME_DELTA_SOLAREDGE_ONE` = 1 hour; when from **Legacy API**, uses `CHECK_TIME_DELTA` = 2 hours (in `const.py`). The threshold follows the current data source (see **Obtained from** sensor).  
- **Rule**: For each optimizer, if `lastmeasurement` is older than the threshold:
  - **Voltage, Current, Optimizer voltage, Power** → reported as **0** (so dashboards don’t show stale “live” values).
  - **Temperature** (when from SolarEdge One) → not zeroed; shows last known value or unknown if missing.
  - **Lifetime energy** and **Last measurement** → always last known value (historical view still possible).
- Aggregated sensors (string/inverter/site) only include optimizers with **recent** measurements in power/current/voltage; lifetime energy and last measurement still aggregate from all.

---

## 11. Internationalization (i18n)

- **Config flow**: Labels (Site id, Username, Password, **Entity ID prefix (optional)**, **Include Site ID in Entity ID**, **Use SolarEdge One** — in that order), errors, abort messages (including **reauth_successful**, **reauth_entry_missing**), and config entry title are translated. The **re-authentication** step (`config.step.reauth_confirm`: title, description, data.username, data.password) is translated in all supported languages.
- **Options flow (Reconfigure dialog)**: The Configure form uses the **options** translation section (`options.step.init`: title, description, data.entity_id_prefix, data.include_site_id_in_entity_id, data.use_solaredge_one). Field order: Entity ID prefix, then Include Site ID in Entity ID, then Use SolarEdge One. The integration sets `translation_domain` to the integration domain so the frontend loads these strings.  
- **Entity names**: Sensor names (Power, Voltage, Last measurement, etc.) use `translation_key` and are translated.  
- **API**: `locale` and `Accept-Language` (and cookie `SolarEdge_Locale`) follow HA language (e.g. `en`, `de`, `nl`). The SolarEdge API may return measurement keys in the user’s language (e.g. “Leistung [W]” in German); the integration recognises multiple locale variants and normalises decimal separators (e.g. comma to dot) so power/current/voltage work in all supported languages.

Supported languages:

| Code | Language   |
|------|------------|
| cs   | Čeština    |
| da   | Dansk      |
| de   | Deutsch    |
| el   | Ελληνικά   |
| en   | English    |
| es   | Español    |
| fi   | Suomi      |
| fr   | Français   |
| hu   | Magyar     |
| it   | Italiano   |
| ja   | 日本語     |
| nb   | Norsk      |
| nl   | Nederlands |
| pl   | Polski     |
| pt   | Português  |
| ru   | Русский    |
| sv   | Svenska    |
| tr   | Türkçe     |
| zh   | 中文       |

Translation files: `translations/<code>.json` with **config**, **options**, **entity**, and **device** sections. The config section covers the initial setup and re-auth steps; the options section covers the Reconfigure (Configure) dialog. See [Internationalization (i18n)](internationalization.md) in the repo for details.

---

## 12. API client and SolarEdge portal

The integration uses a **dual API** (`api_dual.py`): it always tries **SolarEdge One** first; if One returns no valid optimizer measurements or One login fails, it falls back to the **legacy** API. The site-level **Obtained from** sensor shows "One API" or "Legacy API". There is no user option to choose the API.

### Legacy API (used when SolarEdge One has no valid data or fails)

| Purpose | Method | Endpoint (concept) |
|---------|--------|---------------------|
| Login check / layout | GET | `.../api/sites/{siteid}/layout/logical` |
| Per-optimizer data | GET | `.../solaredge-web/p/systemData?reporterId={id}&...&locale={locale}` |
| Lifetime energy | POST | `.../api/sites/{siteid}/layout/energy` (and energy cache) |
| Session / CSRF | GET/POST | `.../solaredge-web/p/login`, etc. |

- **Auth**: HTTP Basic Auth (username/password) for layout and systemData; web session (cookies + CSRF) for energy endpoint.  
- **Locale**: From HA language (e.g. `en` → `en_US`); used in `systemData` and request headers/cookies.

### SolarEdge One API (tried first by the dual API)

| Purpose | Method | Endpoint (concept) |
|---------|--------|---------------------|
| Login | GET/POST | `login.solaredge.com` (OAuth PKCE flow), then `POST .../oauth2/token` for access token |
| Site structure / layout | GET | `.../services/layout/logical/generic/v2/site/{siteId}?include-optimizers=true` |
| Per-optimizer live data + basic info | POST | `.../services/layout/information/optimizers` (body: list of optimizer serials). Returns `basicInformationList` (serial, model e.g. P405-4RM4MRM-NA25, optional description/panel type) and `serialToLiveData`. When description is present it is used for the optimizer device model and the **panel_type** sensor attribute. **Full refresh:** One API uses one batch with all serials; legacy uses parallel per-optimizer. **Lightweight check:** one batch with up to 5 random serials (`requestSystemDataBatch`). **Timeout**: 60 s with **one automatic retry** on read/connect timeout (log: "Timeout requesting optimizer data (retrying once)"). |
| Inverter information | GET | `.../services/layout/information/inverters?inverter-serials=...` (fullModel e.g. SE5000H-RW000BNN4). Fetched at setup to set inverter device model. **403 Forbidden** is non-fatal: integration logs a warning and continues; devices use position-based identity so model names may be missing but all sensors work. |
| Optimizer temperatures | GET | `.../services/layout/energy/site/{siteId}/by-inverter?start-date=...&end-date=...&inverter-serials=...&include-max-temperature=true`. Returns per-optimizer temperature; may be °C or °F per `temperatureUnit`. Integration normalizes to °C. Cached 30 min (`TEMPERATURE_CACHE_TTL`); merged into optimizer data when using One API. |
| Lifetime energy | GET | `.../services/layout/energy-graph/site/{siteId}/optimizers?optimizer-serials=...&start-date=...&end-date=...` (one request per optimizer; when cache is cold, requests run **in parallel** via thread pool; cached 1 h) |

- **Auth**: OAuth/OIDC with PKCE at `login.solaredge.com`; authorization code exchanged for `access_token`; all `/services/` requests use `Authorization: Bearer <access_token>`. On 401, token is cleared and login flow is retried.  
- **Host**: `monitoring.solaredge.com` for API; `login.solaredge.com` for login and token.  
- **Device model**: Optimizer device model and serial come from the optimizers information response (`model`); when the API provides a description (panel type), it is appended to the model and exposed as the **panel_type** attribute on optimizer sensors. Inverter device model and serial come from the inverters information response (`fullModel`, `serial`) when available; the coordinator calls `get_inverter_models(serials)` at setup. If that call returns 403 Forbidden, devices still work (inverter/optimizer identity is position-based).  
- **Identity**: Coordinator stores optimizer data keyed by both serial and `(inv_idx, str_idx, opt_idx)`; at string/optimizer level these indices come from the parsed API display name when it parses (e.g. "1.0.1" → 1, 0, 1), otherwise from position. Sensors look up by position first so a hardware swap (new serial at same position) updates the same entity. Device identifiers use config entry id + path (e.g. `entry_id_opt_1_0_1`) so one device per logical slot; names and entity IDs match the API display name when it parses.

### Data and units (both APIs)

The layout/energy (legacy) or energy-graph (SolarEdge One) response provides per-optimizer (and per-string where applicable) data. The integration converts lifetime energy to kWh from **`unscaledEnergy`** (Wh) so values update correctly; display **`units`** apply only to portal display. The coordinator computes a **site total** (from API or sum of optimizers) and uses it for the site-level lifetime energy sensor when aggregated optimizer data is unreliable. For SolarEdge One, string-level lifetime is derived by summing optimizer lifetimes when the API does not return string-level keys.

### Caching

- **Layout**: 1 h TTL (in One and legacy clients); dual API prefers One for `requestListOfAllPanels()`; avoids repeated layout calls during setup and polling.  
- **Lifetime energy**: 1 h TTL in each backend; the dual API's `get_lifetime_energy_cached()` returns the cache from whichever backend was last used for full data (One or legacy). Used by coordinator aggregation.  
- **Optimizer temperatures** (SolarEdge One only): 30 min TTL (`TEMPERATURE_CACHE_TTL`); `get_optimizer_temperatures_cached()` calls layout/energy by-inverter with `include-max-temperature=true`; API may return °C or °F (`temperatureUnit`); integration converts F→°C for storage. Result merged into optimizer data in `requestSystemData`, `requestSystemDataBatch`, and `requestAllData`. When the coordinator does not do a full refresh, it still calls `_refresh_temperature_when_no_full_refresh()` so temperatures stay updated when the cache expires (30 min) even when power/voltage are not. Debug logging when Fahrenheit: per-optimizer conversion (raw °F → °C) and summary count.  
- **Panels list**: Same as layout (returned by `requestListOfAllPanels()`; dual API delegates to One first, then legacy on failure).

### Data models (conceptual)

- **SolarEdgeSite**: `siteId`, `inverters[]`.  
- **SolarEdgeInverter**: `inverterId`, `serialNumber`, `displayName`, `strings[]`.  
- **SolarEdgeString**: `stringId`, `displayName`, `optimizers[]`.  
- **SolarEdgeOptimizer**: `optimizerId`, `serialNumber`, `displayName`.  
- **SolarEdgeOptimizerData**: `panel_id`, `panel_description` (panel type from API when available, e.g. SunPower SPR-MAX3-400), `voltage`, `current`, `power`, `optimizer_voltage`, `temperature` (°C; from SolarEdge One by-inverter when available; portal may send °F via `temperatureUnit`, we convert to °C), `lifetime_energy` (kWh from API `unscaledEnergy`), `lastmeasurement`, `_has_valid_measurements` (True when the API provided a non-empty measurements dict; used by dual API to decide if One data is valid or to fall back to legacy), etc.  
- **SolarEdgeAggregatedData**: `panel_id`, `entity_type` (string/inverter/site), same measurement fields plus `child_count`, etc.

---

## 13. Troubleshooting and logging

- **Log namespace**: The integration uses `logging.getLogger(__name__)` per module (e.g. `solaredgeoptimizers.sensor`); the top-level logger name is `solaredgeoptimizers`.  
- **Levels**: `info` for setup and main steps, `debug` for URLs, responses, timezone, and per-optimizer details, `warning` for missing/zero measurements and server 5xx, `error` for auth/connect/parse failures. All debug calls are guarded with `isEnabledFor(logging.DEBUG)`, so there is no performance cost when the log level is `info` or higher. Debug messages use consistent prefixes (`SolarEdge Optimizers`, `SolarEdge Optimizers coordinator`, `SolarEdge Optimizers sensor`, `SolarEdge Optimizers (legacy)`, `SolarEdge One`, `SolarEdge Dual API`) so you can filter logs by component.

**What debug logging covers:** Config flow (user form, validating input, unique_id check, creating entry with title, reauth form, options/reconfigure form when showing — with current prefix, include_site_id_in_entity_id, use_solaredge_one — or saving, removal and device count); setup (dual API with use_solaredge_one, login check, coordinator with include_site_id_in_entity_id, inverter models fetch, site/inverter/string device creation with model and suffix, platform forward, coordinator stored); unload (unload start, platform result, API session close, registry cleanup when nothing to remove, unload complete); coordinator (panel list request, representative optimizers or random batch with configurable batch size (`LIGHT_CHECK_BATCH_SIZE`), update cycle — do_full_refresh, should_light_check, measurement_age, desired_interval, latest_measurement, obtained_from, revert-to-One retry when data from legacy — adaptive light check and full refresh, refresh strategy determination, full refresh vs reuse item count, lifetime energy entry count, timezone, update complete, inactive device skipping with status, duplicate position resolution with suffix assignment, string/inverter/site aggregated data creation with status and child counts); sensor platform (setup entry, base_name and include_site_id and site_id, adding optimizer with panel_id/serial/model/panel_type/status, duplicate optimizer position resolution with suffixes, inactive device sensor skipping, aggregated sensors with status, obtained_from sensor, entity count, device status summary when inactive devices exist, status value updates, skip on exception); API client (dual API: use_solaredge_one/legacy-only, fallback to legacy when One has no valid measurements or fails; legacy: login, layout, requestAllData with configurable timeouts (`API_TIMEOUT_SHORT`/`API_TIMEOUT_LONG`), get_lifetime_energy_cached; SolarEdge One: OAuth steps, token, get_inverter_models, requestSystemData/requestSystemDataBatch, requestAllData (single batch for all optimizers; on batch failure, per-optimizer fallback with ThreadPoolExecutor), timeout retry (warning), _build_optimizer_data_from_response, cache, optimizer temperatures with unit and F→°C conversion when portal sends Fahrenheit, errors). Enable with `logger: logs: solaredgeoptimizers: debug` in `configuration.yaml`.

### Logging

To enable debug logging for this integration, add the following to your `configuration.yaml`. If you already have a `logger:` section, add only the `logs:` entry (and the line under it) instead of duplicating the whole block. Use the logger name `solaredgeoptimizers` (the integration package name).

```yaml
# Logging
logger:
  default: info
  logs:
    solaredgeoptimizers: debug
```

**How to edit `configuration.yaml` directly**

- **File Editor add-on** (recommended): Install **File editor** from the Add-on Store (Settings → Add-ons). Open it from the sidebar, open `configuration.yaml`, add or merge the `logger` block above, save, then use **Developer tools** → **YAML** → **Reload** or restart Home Assistant.
- **SSH / Terminal**: With the **SSH** or **Terminal & SSH** add-on, edit with `nano /config/configuration.yaml` (or `vi`). Save, then reload YAML or restart.
- **Other editors**: Same idea if you use Samba, Studio Code Server, or any access to the config directory: edit `configuration.yaml`, save, then reload YAML or restart.

### Common issues

| Symptom | What to check |
|--------|----------------|
| “Invalid authentication” | Correct Site ID, email, password; account can log in at monitoring.solaredge.com. The integration tries SolarEdge One first and falls back to the legacy API; login succeeds if either returns 200. |
| “Failed to connect” | Network, firewall, DNS; outbound HTTPS to monitoring.solaredge.com. |
| Config entry not loading | Logs for `ConfigEntryNotReady`; first refresh may fail if API is slow or returns errors. |
| Sensors stay 0 | Last measurement age: 1 h when Obtained from is One API, 2 h when Legacy API. Check “Last measurement”, Last polled, and Obtained from; debug logs for API responses. If using a non-English HA language, ensure you’re on a version that supports locale-aware measurement keys (e.g. “Leistung [W]” for German). |
| Slow first load | **SolarEdge One:** one batch POST for optimizer data; when cache is cold, parallel lifetime-energy fetch. **Legacy:** parallel per-optimizer requests. Layout and lifetime energy cached after first run. Full refresh timeout 30 min (`COORDINATOR_REFRESH_TIMEOUT_SEC`). |
| Duplicate entity IDs (e.g. sensor.power_2) | Use a unique Entity ID prefix per site, or ensure you’re on a version that uses the new path-based entity IDs (site in path). |
| Duplicate sensors/devices after optimizer or inverter swap | The integration uses position-based identity; after an update you should see one sensor per position. If you still have duplicates from before that change, **remove the integration** (Settings → Integrations → Delete) and **add it again** so the registry is cleaned and recreated with position-based devices. |
| Two inverters (one with sensors, one with strings) or “references a non existing via_device” | Aggregated string/inverter sensors must use the same position-based device IDs as the coordinator. If you see this after an update, ensure you are on a version that sets string/inverter device info by position in `_set_aggregated_device_info`. Then **remove the integration** and **add it again** so devices and entities are recreated correctly. |
| 403 Forbidden on inverter information | Non-fatal. The integration logs a warning; inverter and optimizer devices use position-based identity so model names may be missing but all sensors and devices work. |
| TimeoutError on first load (many optimizers) | Full refresh has a 30-minute (1800 s) timeout (`COORDINATOR_REFRESH_TIMEOUT_SEC`). Lifetime energy is fetched in parallel when the cache is cold. If timeouts persist on very slow connections, check network or SolarEdge portal responsiveness; the coordinator logs a clear message when the timeout is reached. |
| Entity IDs show a device prefix (e.g. sensor.site_123_power_123 instead of sensor.power_123) | Rare (can depend on locale/HA version). Remove the integration, restart Home Assistant, then add the integration again after updating to the latest version so entity IDs are created with the correct path-based format. |
| "Read timed out" or "Error fetching data for optimizer …" (SolarEdge One) | Optimizer requests use a 60 s timeout and one retry. If timeouts persist, check network/firewall or SolarEdge status; enable debug logging to see "Timeout requesting optimizer data (retrying once)". |

- **5xx from SolarEdge**: Logged as temporary; coordinator retries on next cycle.  
- **DNS/connection errors** (e.g. “Failed to resolve monitoring.solaredge.com”): Lifetime energy and aggregation fall back to cached or empty data so the coordinator still completes; next cycle will retry.  
- **Read timed out / timeout errors**: Optimizer data requests (SolarEdge One) use a 60 s timeout and one automatic retry. If you see "Error fetching data for optimizer … Read timed out" or "Timeout requesting optimizer data (retrying once)" in logs, the first attempt timed out and the retry was used. If timeouts persist, check network latency, firewall, or SolarEdge portal status; the next poll will try again.
- **Unload**: Coordinator is removed and API client `close()` is called to release sessions.  
- **Removing the integration**: When you delete the config entry from **Settings → Devices & services → Integrations** (not only from HACS), the config flow’s `async_remove_entry` runs and calls the shared helper `remove_entities_and_devices_for_entry(hass, entry)` (defined in `__init__.py`), which removes all entities and devices linked to that entry from the registries. The same helper is used on unload. No manual cleanup of leftover devices or entities is needed.

---

## 14. File structure and constants

### Repo layout (relevant files)

```
solaredgeoptimizers/
├── __init__.py            # Entry point, migration (ensure use_solaredge_one), setup dual API + coordinator, remove_entities_and_devices_for_entry (shared cleanup) and helpers
├── api.py                 # SolarEdgeAPIProtocol: typing protocol for API clients (used by coordinator)
├── api_dual.py            # SolarEdgeDualAPI: use_solaredge_one; when True tries One first then legacy, when False legacy only; exposes _obtained_from
├── config_flow.py         # Config flow, validation (dual API), translated title, async_remove_entry (calls shared cleanup helper)
├── const.py               # DOMAIN, intervals, cache TTLs, sensor type constants, status helpers (is_status_active, status_display_value, status_icon), parse_string_display_name_path, parse_optimizer_display_name_to_indices, make_duplicate_sort_key, resolve_duplicate_indices
├── exceptions.py          # SolarEdgeAPIError: custom exception for API/processing errors (used by legacy client)
├── coordinator.py         # DataUpdateCoordinator, adaptive polling, revert-to-One retry (30 min when from legacy), aggregation, _obtained_from, AggregationContext namedtuple, uses resolve_duplicate_indices from const.py
├── hacs.json              # HACS metadata
├── info.md                # Integration info (e.g. for HACS)
├── manifest.json         # Domain, version, requirements (e.g. jsonfinder)
├── sensor.py              # Sensor entities (optimizer, aggregated, last polled, obtained_from); at setup removes existing sensor entities for this entry then creates new ones using coordinator.data when available; uses resolve_duplicate_indices from const.py for entity ID duplicate resolution
├── solaredgeoptimizers.py # Legacy API client, data models, SolarEdge legacy API calls
├── solaredge_one_api.py   # SolarEdge One API client (OAuth, /services/layout/..., optimizer requests with 60 s timeout and retry)
├── strings.json           # Config flow strings (references to common keys)
├── translations/          # en.json, nl.json, de.json, ...
└── docs/
    ├── Internationalization.md
    ├── SolarEdge-One-API-Summary.md
    └── Wiki-Home.md             # This file
```

### Main constants (`const.py`)

| Constant | Value | Meaning |
|----------|--------|--------|
| `DOMAIN` | `"solaredgeoptimizers"` | Integration domain. |
| `CONF_SITE_ID` | `"siteid"` | Config key for Site ID; used in config flow and reauth. |
| `CONF_USE_SOLAREDGE_ONE` | `"use_solaredge_one"` | Optional; when True (default), dual API (One first, legacy fallback). When False, integration always uses legacy portal only. Setup and Configure (options). Migration sets default True if missing. |
| `CONF_ENTITY_PREFIX` | `"entity_id_prefix"` | Optional config key for entity ID prefix (e.g. `se_`). |
| `CONF_INCLUDE_SITE_ID_IN_ENTITY_ID` | `"include_site_id_in_entity_id"` | Optional config key; when true, entity IDs for inverter/string/optimizer include the site ID (default false). Site level always includes site ID. |
| `UPDATE_DELAY` | 5 minutes | Coordinator update interval. |
| `CHECK_TIME_DELTA` | 2 hours | Age threshold for zeroing live values (legacy API). |
| `CHECK_TIME_DELTA_SOLAREDGE_ONE` | 1 hour | Age threshold for zeroing live values when using SolarEdge One API. |
| `COORDINATOR_REFRESH_TIMEOUT_SEC` | 1800 (30 min) | Max seconds for one coordinator refresh (initial and full refresh). Slow API or many optimizers may need this; increase in `const.py` if timeouts persist. |
| `REVERT_TO_ONE_RETRY_INTERVAL` | 30 minutes | When data is from legacy API, coordinator forces a full refresh this often to re-try SolarEdge One so the integration can switch back when One is available. |
| `LIGHT_CHECK_MIN_INTERVAL` | 5 minutes | Minimum time between a light check that detects new data and triggering a full refresh; avoids back-to-back full refreshes. |
| `LIGHT_CHECK_DESIRED_INTERVAL_FRESH` | 5 minutes | Desired interval between lightweight checks when data is fresh (within stale delta). |
| `LIGHT_CHECK_DESIRED_INTERVAL_STALE` | 30 minutes | Desired interval between lightweight checks when data is stale or age unknown. |
| `RELIABLE_THRESHOLD_KWH` | 100.0 | Site-level lifetime energy: when aggregated optimizer total is below this (kWh) and the portal total (from layout/energy) is ≥ this, the site sensor uses the portal total instead of the aggregated sum. |
| `API_TIMEOUT_SHORT` | 30 | Timeout in seconds for quick API requests (login check, single optimizer). |
| `API_TIMEOUT_LONG` | 60 | Timeout in seconds for longer API requests (layout, batch operations). |
| `LIGHT_CHECK_BATCH_SIZE` | 5 | Number of optimizers to sample in lightweight checks (SolarEdge One). |
| `MAX_PARALLEL_WORKERS` | 10 | Maximum threads for parallel API requests (lifetime energy when cache cold; legacy optimizer data; One API per-optimizer fallback when batch fails). |
| `SENSOR_TYPE_*` | e.g. `Current`, `Power`, `Voltage` | Sensor type identifiers for individual and aggregated sensors. |
| `SENSOR_TYPE_INACTIVE_OPTIMIZER_EXCLUDE` | List of sensor types | Sensors not created for inactive optimizers: Azimuth, Current, Optimizer voltage, Power, Temperature, Tilt, Voltage. |
| `SENSOR_TYPE_INACTIVE_AGGREGATED_EXCLUDE` | List of sensor types | Sensors not created for inactive strings/inverters: Current, Power, Voltage. |

### Shared utility functions (`const.py`)

| Function | Purpose |
|----------|---------|
| `parse_string_display_name_path()` | Parse string displayName (e.g. "1.0") into (inv, str) tuple for device/entity IDs. |
| `parse_optimizer_display_name_to_indices()` | Parse optimizer displayName (e.g. "1.0.1") into (inv, str, opt) tuple for device/entity IDs. |
| `make_duplicate_sort_key()` | Create sort key for duplicate resolution: active devices first, then alphabetically by serial number. |
| `resolve_duplicate_indices()` | Resolve duplicate position keys by adding letter suffixes (a, b, c...). Used by sensor.py and coordinator.py for entity ID duplicate resolution. |

---

## 15. Credits and links

- **Repository**: [github.com/AndrewTapp/solaredgeoptimizers](https://github.com/AndrewTapp/solaredgeoptimizers)  
- **Issues**: [GitHub Issues](https://github.com/AndrewTapp/solaredgeoptimizers/issues)  
- **Original integration**: [@proudelm](https://github.com/proudelm)  
- **Thanks**: [@Mariusthvdb](https://github.com/Mariusthvdb) for help with this fork  

---

*This document is the main technical reference for the SolarEdge Optimizers Home Assistant integration. For end-user installation and feature summary, see the main [README](https://github.com/AndrewTapp/solaredgeoptimizers/blob/main/README.md).*
