# SolarEdge One API

## Upgrading from an earlier version

If you are on a version prior to v2.4.0 (or want a clean registry after any upgrade), the steps below ensure entities and devices are recreated correctly:

1. **Update the integration via HACS** so the new code (including cleanup) is installed.
2. **Restart Home Assistant** (optional but recommended).
3. **Remove the integration from Home Assistant:** **Settings → Devices & services → Integrations** → SolarEdge Optimizers → **Delete**. This runs `async_remove_entry` and cleans entity and device registries for that entry.
4. **Restart Home Assistant** (optional but recommended).
5. **Clear browser cache** [Ctrl]+[Shift]+r on Microsoft Edge.
6. **Re-add the integration** with the same Site ID, username, password, and options. You get fresh entities and a clean registry; history reconnects because `unique_id`s are the same.

## What it is

SolarEdge One is SolarEdge’s newer monitoring portal and API. It replaces the older “legacy” monitoring portal and its ad-hoc endpoints with a structured, service-oriented API under `monitoring.solaredge.com/services/...`.

## Dual API: One first, legacy fallback

The integration **always tries SolarEdge One first**. If One returns no valid measurements (e.g. "Missing or invalid measurements" for all optimizers) or One login fails, it **automatically falls back** to the legacy SolarEdge API. When data is currently from the legacy API, the integration **forces a full refresh every 30 minutes** so it re-tries SolarEdge One and can switch back when One is available again. You do not choose the API—the integration picks the best available source each time.

A site-level sensor **Obtained from** shows whether current data came from **"One API"** or **"Legacy API"**. The legacy API can have delayed readings (an hour or more); when One works, it generally provides more timely data.

## How it replaces the old API

- **Same purpose**: Both APIs expose site layout (inverters → strings → optimizers), live per-optimizer data (power, voltage, current, optimizer voltage), and lifetime energy. The integration presents the same hierarchy and sensors either way.

- **Different tech**:
  - **Legacy**: Basic Auth + web session (cookies/CSRF), separate endpoints for layout, per-panel `systemData`, and layout/energy. Data is scraped from web-style URLs and responses.
  - **SolarEdge One**: OAuth/OIDC (PKCE) via `login.solaredge.com`; access token is then used as Bearer on all `/services/` calls. Layout comes from a single v2 layout endpoint; optimizer live + basic info from a POST to `/layout/information/optimizers`; lifetime energy from the energy-graph API. Cleaner, REST-style design.

- **Same credentials**: Site ID, username, and password are unchanged; the integration uses them for both One and legacy and chooses which backend to use automatically at each refresh.

## Benefits of SolarEdge One (for the integration)

- **Auth**: Single OAuth flow and Bearer token instead of mixing Basic Auth and session cookies.
- **Data**: Structured JSON (e.g. `power_W`, `voltage_V`, `optimizerVoltage_V`) instead of locale-dependent labels; layout in a single v2 structure.
- **Devices**: Optimizer and inverter **model** (and serial) come from the API, so HA devices can show real model names (e.g. P405-4RM4MRM-NA25, SE5000H-RW000BNN4).
- **Polling**: Lightweight “any new data?” check can use a batch request (e.g. a few optimizers) instead of one panel; 1-hour stale threshold for live values (legacy used 2 hours).

## In this integration

- A **dual API** wrapper (`api_dual.py`) tries SolarEdge One first; if One has no valid measurements or fails, it uses the legacy API. There is no user option—the integration decides at each refresh.
- Both backends implement the same `SolarEdgeAPIProtocol` and feed the same coordinator and sensor layer, so behaviour (sensors, hierarchy, aggregation) is the same; only the API client (legacy vs `solaredge_one_api`) and the exact endpoints/auth differ. The **Obtained from** sensor on the site device shows which source provided the current data.
- **Temperature sensor updates (SolarEdge One):** When the coordinator does not perform a full refresh (e.g. it reuses existing data after a lightweight check), it still refreshes optimizer temperatures about every 15 minutes via `get_optimizer_temperatures_cached()` (layout/energy by-inverter with `include-max-temperature`). So temperature sensors stay up to date even when power, voltage, and current are not being refreshed. The coordinator calls `_refresh_temperature_when_no_full_refresh()` for this; the cache TTL is 15 minutes so the API is only hit when the cache has expired.
- **Lifetime energy (SolarEdge One):** Per-optimizer lifetime energy comes from the energy-graph API (one GET per optimizer, 1-hour cache). When the cache is cold, `get_lifetime_energy_cached()` now fetches these requests **in parallel** (thread pool) instead of sequentially, so sites with many optimizers complete the initial refresh much faster and avoid coordinator timeouts.
- **Optimizer requests:** POSTs to `/layout/information/optimizers` use a 60 s timeout and **one automatic retry** on read/connect timeout to reduce failures when the portal is slow.
- **Inverter information:** If the inverter information API returns **403 Forbidden** (e.g. some accounts lack that permission), the integration still works: inverter and optimizer devices use position-based identity, so model names may be missing but all sensors and devices function. A one-time warning is logged.
