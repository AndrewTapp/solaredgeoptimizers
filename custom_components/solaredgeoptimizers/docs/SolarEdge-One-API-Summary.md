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

## Recommendation: prefer SolarEdge One

The **legacy API** is known to be unreliable, with **delayed readings** that can lag by **an hour or more**. Data can be stale or inconsistent, which affects live power, voltage, and optimizer values in Home Assistant.

**I recommend using the new SolarEdge One API** whenever your account supports it. Keep **Use SolarEdge One portal** enabled (it is on by default when adding the integration). Turn it off only if your account has not yet been migrated to the SolarEdge One portal. The new API generally provides more timely and reliable data.

## How it replaces the old API

- **Same purpose**: Both APIs expose site layout (inverters → strings → optimizers), live per-optimizer data (power, voltage, current, optimizer voltage), and lifetime energy. The integration presents the same hierarchy and sensors either way.

- **Different tech**:
  - **Legacy**: Basic Auth + web session (cookies/CSRF), separate endpoints for layout, per-panel `systemData`, and layout/energy. Data is scraped from web-style URLs and responses.
  - **SolarEdge One**: OAuth/OIDC (PKCE) via `login.solaredge.com`; access token is then used as Bearer on all `/services/` calls. Layout comes from a single v2 layout endpoint; optimizer live + basic info from a POST to `/layout/information/optimizers`; lifetime energy from the energy-graph API. Cleaner, REST-style design.

- **Same credentials**: Site ID, username, and password are unchanged; only the backend (portal and API) changes when “Use SolarEdge One portal” is on.

## Benefits of SolarEdge One (for the integration)

- **Auth**: Single OAuth flow and Bearer token instead of mixing Basic Auth and session cookies.
- **Data**: Structured JSON (e.g. `power_W`, `voltage_V`, `optimizerVoltage_V`) instead of locale-dependent labels; layout in a single v2 structure.
- **Devices**: Optimizer and inverter **model** (and serial) come from the API, so HA devices can show real model names (e.g. P405-4RM4MRM-NA25, SE5000H-RW000BNN4).
- **Polling**: Lightweight “any new data?” check can use a batch request (e.g. a few optimizers) instead of one panel; 1-hour stale threshold for live values (legacy used 2 hours).

## In this integration

- One config option (**Use SolarEdge One portal**, default **on**) chooses the backend.
- Both backends implement the same `SolarEdgeAPIProtocol` and feed the same coordinator and sensor layer, so behaviour (sensors, hierarchy, aggregation) is the same; only the API client (legacy vs `solaredge_one_api`) and the exact endpoints/auth differ.