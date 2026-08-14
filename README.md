# SolarEdge Optimizers Integration

[![Release](https://img.shields.io/github/release/AndrewTapp/solaredgeoptimizers.svg)](https://github.com/AndrewTapp/solaredgeoptimizers/releases)
[![CodeFactor Grade](https://img.shields.io/codefactor/grade/github/AndrewTapp/solaredgeoptimizers.svg)](https://www.codefactor.io/repository/github/AndrewTapp/solaredgeoptimizers)
[![Donate](https://img.shields.io/badge/Donate-PayPal-green.svg)](https://www.paypal.me/AndrewJTapp)
[![Donate](https://img.shields.io/badge/Donate-BuyMeACoffee-green.svg)](https://buymeacoffee.com/andrewtapp)

This integration brings your SolarEdge optimizer data from the SolarEdge monitoring
portal into Home Assistant. You can see current production, voltage, power,
maximum daily optimizer temperature (SolarEdge One only), and lifetime energy at the level of individual
optimizers, strings, inverters, or the whole site.

At site level you get **Installation date** and **Peak power** (SolarEdge One only;
from the portal layout/information/site). At inverter level you get
**Max active power** in kW (SolarEdge One only; from the portal layout logical v2,
same cache as layout).

**📖 [Technical documentation (Wiki)](https://github.com/AndrewTapp/solaredgeoptimizers/wiki)** — architecture, data flow, sensors reference, troubleshooting, and more.

## Upgrading from an earlier version

As with previous versions you may need to delete and re-add this integration after updating through HACS.

If you are on a version prior to v2.4.0 (or want a clean registry after any upgrade), the steps below ensure entities and devices are recreated correctly:

1. **Update the integration via HACS** so the new code (including cleanup) is installed.
2. **Restart Home Assistant** (optional but recommended).
3. **Remove the integration from Home Assistant:** **Settings → Devices & services → Integrations**
   → SolarEdge Optimizers → **Delete**. This runs `async_remove_entry` and cleans entity
   and device registries for that entry.
4. **Restart Home Assistant** (optional but recommended).
5. **Clear browser cache** [Ctrl]+[Shift]+r on Microsoft Edge.
6. **Re-add the integration** with the same Site ID, username, password, and options. You get fresh entities and a clean registry; history reconnects because `unique_id`s are the same.

## SolarEdge One and legacy API (dual API)

The SolarEdge monitoring portal is being upgraded to **SolarEdge One**.
The integration uses a **dual API**:

- When **Use SolarEdge One** is **Yes** (default), it tries the **SolarEdge One API**
  first (`/services/layout/...`).
- If One returns no valid measurements (e.g. no optimizer with a non-empty measurements dict;
  per-optimizer "Missing or invalid measurements" for a single inactive/replaced serial is
  expected and from **v2.4.19** logs at **debug** only) or One login fails, it automatically
  falls back to the **legacy** API.
- When One starts returning valid data again, the integration switches back to One
  either when a lightweight check sees newer data, or every 30 minutes while data is
  currently from legacy.
- When **Use SolarEdge One** is **No**, the integration always uses the **legacy**
  portal only.

Use the same Site ID, username, and password for both. The site-level
**Obtained from** sensor shows whether current data came from **"One API"**
or **"Legacy API"**. When data is from One, optimizer and inverter devices show
the **model** (e.g. P405-4RM4MRM-NA25, SE5000H-RW000BNN4) and serial number.
When the API provides a **panel type** (description, e.g. SunPower SPR-MAX3-400),
it is included in the optimizer device model and exposed as a **panel_type**
attribute on optimizer sensors.

## What You Need

**Current release:** **2.4.21** (`manifest.json`). No third-party Python packages are declared
(`requirements: []`). The integration uses Home Assistant’s bundled `requests` and `pytz`.
Legacy portal `systemData` parsing uses stdlib `json` only (the old `jsonfinder` requirement
was removed in v2.4.21 after Home Assistant Core 2026.8.1 could not install it).

To set up the integration you will need:

- Your **Site ID** (from the SolarEdge portal)
- Your **Username**
- Your **Password**
- **Entity ID prefix** (optional) – If you run more than one site or want to avoid
  clashes with other integrations, you can set a short prefix (e.g. `se_`).
  All entity IDs then start with that prefix (e.g. `sensor.se_power_9999999`).
  Leave blank for no prefix. If you upgrade from an older version without removing
  the integration, this defaults to blank when not set.
- **Include Site ID in Entity ID** (optional, default **off**) – When off, entity IDs
  for inverter, string, and optimizer levels omit the site ID (e.g.
  `sensor.power_1_1`, `sensor.power_1_1_1`). The site level always shows the actual
  site ID (e.g. `sensor.power_9999999`). Turn on to include the site ID in every
  level (e.g. `sensor.power_9999999_1_1_1`). If you upgrade from an older version
  without removing the integration, this defaults to off when not set.
- **Use SolarEdge One** (optional, default **on**) – When **Yes**, the integration
  tries the SolarEdge One API first and falls back to the legacy API when needed.
  When **No**, the integration always uses the legacy portal only. You can change
  this later in **Configure**.

## How Your System Is Shown in Home Assistant

Your solar system is organised in a simple hierarchy. Device and entity names include the site so that multiple sites stay distinct (e.g. no duplicate `sensor.power` when you have two sites):

- **Site [site]** – Your whole installation (e.g. Site 9999999). Entity IDs look like `sensor.[prefix]power_9999999`, `sensor.[prefix]inverter_count_9999999`, and so on.
- **Inverter [site].[i]** – e.g. “Inverter 9999999.1”, “Inverter 9999999.2”. Entity IDs: `sensor.[prefix]power_9999999_1`, etc.
- **String [site].[i].[s]** – e.g. “String 9999999.1.1”. Entity IDs: `sensor.[prefix]power_9999999_1_1`, `sensor.[prefix]lifetime_energy_1_0`, etc. (Device names and entity IDs at string/optimizer level follow the API display name when it parses.)
- **Optimizer [site].[i].[s].[o]** – e.g. “Optimizer 9999999.1.1.1”. Entity IDs: `sensor.[prefix]power_9999999_1_1_1`, `sensor.[prefix]lifetime_energy_1_0_1`, etc.

*[prefix]* is your optional Entity ID prefix (blank if not set). By default,
**Include Site ID in Entity ID** is off, so inverter/string/optimizer entity IDs are
shorter; the site level always shows the actual site ID.

At **string and optimizer** level, device names and entity IDs are based on the API
display name when it parses (e.g. "1.0" → "String 1.0"; "1.0.1" → "Optimizer 1.0.1";
"1.1.1a" → suffix **a** on the optimizer index for replaced panels at the same
logical position). If parsing fails, position-based indices are used. When the API
returns duplicate names without a letter, the integration assigns **a**, **b**, **c**
as needed. Site and inverter remain position-based.

**Per-optimizer** sensors use `has_entity_name=False` and a full path-based
`suggested_object_id` (e.g. `power_1_1_1`), so Home Assistant does **not** prepend
the optimizer device slug to the entity id (avoiding ids like
`sensor.optimizer_1_1_1_power_1_1_1`). Friendly names use only the short translated
sensor label (e.g. “Power”, “Azimuth”); the optimizer **device** name provides
site/string/optimizer context.

Entity IDs remain path-based only (no device-name prefix), for example
`sensor.xyz_power_1_0` for a string or `sensor.xyz_power_1_0_1` for an optimizer when
the API uses that numbering. Site, inverter, and string devices are registered in the device registry before
entities are added; **optimizer** devices are registered in the sensor platform
immediately before optimizer entities are added (so devices are not “Unnamed”).
Optimizer `via_device` uses the same string device identifier as the coordinator,
including duplicate and portal suffixes on string keys (e.g. `_str_1_0a` for a
duplicate string at position 1.0). Entity `device_info` uses identifiers-only links so Home Assistant does not
re-apply `via_device` during `async_add_entities` (avoiding "references a non
existing via_device" on startup).

The device hierarchy is **Site → Inverter → String → Optimizer**; optimizers are
grouped under their string. In **Settings → Devices & services**, each device shows
what it's **connected via** (e.g. optimizer → string, string → inverter). Friendly
names and “connected via” follow the same hierarchy. The integration entry title shows
your site (e.g. "SolarEdge Site 9999999"). If an older entry still shows the literal
template **"SolarEdge Site %(siteid)s"**, reload or restart after upgrading to v2.4.18+;
setup auto-repairs the title when it contains `%(siteid)s`.

## What Data You Get

### Per optimizer (each panel)

- **Voltage**, **Current**, **Optimizer voltage**, **Power** – Live values when the optimizer is reporting.
- **Temperature** – Optimizer maximum daily temperature from the SolarEdge One API
  (layout/energy by-inverter with `include-max-temperature`). The portal may report
  in °C or °F (`temperatureUnit`); the integration normalizes to °C for storage and
  Home Assistant displays in your preferred unit. Only available when using the One
  API; shown as “unknown” when missing or when using the legacy API.
  When the integration is not doing a full refresh (e.g. reusing data after a light
  check), it still refreshes temperatures when the temperature cache expires
  (30 minutes, TEMPERATURE_CACHE_TTL), so temperature stays up to date even when
  power/voltage are not updating.
- **Lifetime energy** – Total energy produced (kWh); this only goes up over time.
  The integration uses the API’s raw energy value (unscaledEnergy, in Wh) so it
  updates correctly regardless of how the portal displays units (Wh/kWh/MWh).
  **Site** lifetime uses the portal's dashboard production (Wh) when it is greater
  than the aggregated total; **inverter** and **string** use the portal's
  layout/energy by-inverter values (Wh) when greater than their aggregated totals,
  with string keys aligned to layout strings by **relativeOrder** (not raw list
  index alone). If a portal string bucket is far larger than the sum of that
  string’s optimizers (ratio > `STRING_LIFETIME_PORTAL_OVERRIDE_MAX_RATIO` in
  `const.py`, default 3×), the string keeps the optimizer sum instead.
  The start date for these portal calls is the site **installation date** from
  layout/information/site. String totals are otherwise derived by summing that
  string's optimizer entries, not from API string-level keys that can be site totals.
- **Last measurement** – When the portal last had a reading for this optimizer (`lastMeasurement` / `lastMeasurementDate`). For **inactive** optimizers the API often omits this; the integration preserves the previous value across refreshes so the sensor shows when the optimizer was actually last updated (or unknown before any previous data exists).
- **Status** – The optimizer's status from the API. Blank (empty) status is treated as active and displayed as **blank** with the active icon. Values are shown in proper case: "Active", "Inactive", or the raw value for any other status. The icon changes based on status: check-circle for Active or blank, alert-circle for Inactive, help-circle for unknown (any other) status.
- **Azimuth** – The panel's compass direction in degrees (0–360°), converted from radians. Only available when the API provides module orientation data. Icon: compass.
- **Tilt** – The panel's angle from horizontal in degrees, converted from radians. Only available when the API provides module orientation data. Icon: angle-acute.

### Per string, inverter, and site

For each string, inverter, and the site you get combined (aggregated) sensors:

- **Current (average)** and **Voltage (average)**
- **Power** (total for that level)
- **Lifetime energy** (total for that level)
- **Last measurement** – At **string** level: latest timestamp among **active** optimizers on that string. At **inverter** level: latest among **active** strings. At **site** level: latest among **active** inverters. If nothing qualifies, the sensor state is unknown (the integration does not substitute the current time).
- **Optimizer count** (strings) / **String count** (inverters) / **Inverter count** (site) – Count only **active** devices (status blank or "Active"); always reported as integers (e.g. 3, not 3.0).
- **Status** (strings and inverters) – The status from the API. Blank is displayed as **blank** (active icon); "Inactive" as Inactive (inactive icon); any other value shown as-is with the unknown (help-circle) icon.
- **Last polled** (site device only) – When the integration last successfully fetched data from the SolarEdge portal. Handy for checking that updates are running.
- **Obtained from** (site device only) – Which API provided the current data: **"One API"** or **"Legacy API"**. Entity ID: `sensor.[prefix]obtained_from_[site]` (or `sensor.[prefix]obtained_from` when site ID is not included in entity IDs).
- **Installation date** (site only, SolarEdge One) – Date the site was installed (from portal layout/information/site). Entity ID: `sensor.[prefix]installation_date_[site]` (or `sensor.[prefix]installation_date` when site ID is not in entity IDs).
- **Peak power** (site only, SolarEdge One) – Site peak power in kW (from portal layout/information/site). Entity ID: `sensor.[prefix]peak_power_[site]` (or `sensor.[prefix]peak_power` when site ID is not in entity IDs).
- **Max active power** (inverter only, SolarEdge One) – Inverter maximum active power in kW (from portal layout logical v2, `maxActivePower` in watts shown as kW). Entity ID: `sensor.[prefix]max_active_power_[site]_[inverter]` or `sensor.[prefix]max_active_power_[inverter]` when site ID is not in entity IDs. Updated with the same cache as the layout (2 h).

For **aggregated** string/inverter/site sensors, names are kept short (e.g. “Current (average)”, “Power”) because the device name (e.g. “String 1.1” or “Inverter 1”) already tells you where the value comes from. **Per-optimizer** sensors also use short translated labels (e.g. “Power”, “Azimuth”); the optimizer device name identifies the panel.

## How Often Data Updates

- The **coordinator** runs every **5 minutes** (`UPDATE_DELAY`).
  It does a lightweight check (one or a few optimizers) to see if the portal has
  new readings. When data is **fresh**, the desired interval between light checks is
  about **5 minutes**; when data is **stale or missing**, about **30 minutes**.
  When the light check detects new data, a full refresh runs so all sensors update;
  a full refresh is not triggered again within **5 minutes** of the last one
  (`LIGHT_CHECK_MIN_INTERVAL`). When using **SolarEdge One**, up to
  `LIGHT_CHECK_BATCH_SIZE` (5) optimizers are chosen at random for each check so
  different orientations and shade don't block updates; the sample is **rotated each
  light check** (v2.4.21+). When falling back to the legacy API, a single representative
  optimizer is used for the light check (also re-sampled each check). Auth-like failures
  during the light check raise **Re-authenticate** the same as a full refresh (v2.4.21+).
  When data is currently from the **legacy** API, the integration also forces a full
  refresh every **30 minutes** so it re-tries the SolarEdge One API and can switch
  back to One when it becomes available again.
- **Lifetime energy** is only refreshed from the portal about **once per hour**
  (`LIFETIME_ENERGY_CACHE_TTL`), because that value changes slowly.
  It is derived from the API’s unscaled energy (Wh), not the display units, so
  values update correctly. Totals for strings, inverters, and the site are
  calculated from that data. **Site** lifetime prefers dashboard production when the
  One dashboard helper is available (v2.4.21+: no layout-sum fallback in that case).
  **String** and **inverter** portal overrides are skipped when the aggregate is 0 or
  portal Wh exceeds the aggregate by more than **STRING_LIFETIME_PORTAL_OVERRIDE_MAX_RATIO**
  (default 3×).

- **Layout (panels)** is cached for **2 hours** when using SolarEdge One (`PANELS_CACHE_TTL_ONE`) or the legacy API (`PANELS_CACHE_TTL_LEGACY`).

- **Temperature** (SolarEdge One only): Optimizer maximum daily temperature is refreshed
  from the SolarEdge One API. When the integration does not perform a full
  refresh (e.g. it reuses existing data after a light check), it still refreshes
  optimizer maximum daily temperatures when the temperature cache expires (**30 minutes**,
  `TEMPERATURE_CACHE_TTL`). So temperature sensors stay updated even when power,
  voltage, and current are not being refreshed.

So in normal use you see updates when the coordinator runs and the portal has new
data; temperature (when using One API) is refreshed at most every 30 minutes when
there is no full refresh, and lifetime energy at most once per hour.

## Inactive Devices

When an optimizer, string, or inverter is marked as **Inactive** in the SolarEdge portal, certain sensors are not created because they are not meaningful for inactive/disconnected devices:

- **Inactive optimizers**: Azimuth, Current, Optimizer voltage, Power, Temperature, Tilt, and Voltage sensors are not created. Only Lifetime energy, Last measurement, and Status sensors are created.
- **Inactive strings/inverters**: Current (average), Power, and Voltage (average) sensors are not created. Only Lifetime energy, Last measurement, Child count, and Status sensors are created.

**Aggregation:** Power, current (average), voltage (average), and lifetime energy at string, inverter, and site level include data from **all** devices (any status) that have recent measurements. **Child counts** (Optimizer count per string, String count per inverter, Inverter count per site) count only **active** devices (status blank or "Active") and are always integers.

## When an Optimizer Is Offline or Not Reporting

- If the **last measurement** is older than the stale threshold, **Voltage**, **Current**, **Optimizer voltage**, and **Power** are shown as **0** for that optimizer (and any aggregates that depend on it). This avoids showing stale “live” values. The threshold is **1 hour** when data is from **One API** and **2 hours** when from **Legacy API**. Check the **Obtained from** sensor to see which API is in use.
- **Temperature** (when available from SolarEdge One) is not zeroed when stale; it
  shows the last known optimizer maximum daily temperature or “unknown” if missing.
- **Lifetime energy** and **Last measurement** always show the last known values, so you can still see historical production even when a panel is temporarily offline. For inactive optimizers, when the API does not send a last measurement, the integration keeps the previous value so Last measurement reflects when the optimizer was last updated rather than the current time.

## Handling duplicate names

When the SolarEdge API returns multiple inverters, strings, or optimizers with the same name (e.g. two "Inverter 1" entries after a hardware replacement), the integration resolves duplicates automatically:

- **Inverters**: Active (including blank status) inverters come first (sorted by serial number), then other statuses. The first active inverter keeps the original name (e.g. "Inverter 1"); subsequent duplicates get alphabetical suffixes ("Inverter 1a", "Inverter 1b", etc.).
- **Strings**: Active (including blank status) strings come first (sorted by their position in the API response), then other statuses. The first active string keeps the original name; duplicates get suffixes.
- **Optimizers**: Active (including blank status) optimizers come first (sorted by serial number), then other statuses. The first active optimizer keeps the original name; duplicates get suffixes. If the portal already names a unit **1.1.1a**, that letter is preserved.

This ensures each device and sensor has a unique name and entity ID, even when the API returns duplicate names.

## Replacing optimizers or inverters (hardware swap)

When an optimizer or inverter is replaced (e.g. after a hardware failure), the integration keeps **one device and one set of sensors per logical position**. Device and entity **names and IDs** at string and optimizer level follow the API display name when it parses (e.g. "1.0", "1.0.1"); data is still keyed by position. So when you swap in new hardware at the same position:

- The **same** sensor (e.g. Power for optimizer 1.0.1) continues to show data; after the next refresh it shows the **new** unit’s values. No duplicate entities.
- Inverter and string devices are identified by position (or by parsed display name for strings); replacing an inverter does not create a second inverter device. String and inverter sensors attach to those same devices, so the hierarchy (site → inverter → string → optimizer) stays correct.

If you already see duplicate sensors or devices from an earlier swap, remove the integration (**Settings → Devices & services → Integrations** → SolarEdge Optimizers → **Delete**) and add it again. That cleans up the registry and leaves a single set of position-based devices and sensors.

## One config entry per site

You can add the integration once per **Site ID**. If you try to add the same site again, Home Assistant will show "Device is already configured". To use multiple sites, add a separate integration entry for each site (each with its own Site ID).

## Re-authentication and updating credentials

You do **not** need to delete and re-add the integration to change your SolarEdge username or password.

### Update credentials proactively

1. Go to **Settings** → **Devices & services** → **SolarEdge Optimizers** → your site.
2. Open the integration menu (three dots) and choose **Update credentials**.
3. Enter your current **Username** and **Password**, then submit.

Your **Configure** options (Entity ID prefix, Include Site ID in Entity ID, Use SolarEdge One) are unchanged. Only credentials in the config entry are updated, then the integration reloads.

**Configure** is for naming and API options only — it does not include username or password fields.

### When credentials expire or become invalid

If your SolarEdge credentials expire or become invalid (for example after a password change), the integration detects auth failures during setup, reload, or polling (for example HTTP 401 or repeated legacy session errors). Home Assistant then prompts you to **Re-authenticate** on the integration card.

You will see a form to enter your current username and password; after saving, the integration reloads with the new credentials. Only your username and password are updated; any settings you changed in **Configure** are kept.

If sensors go stale after a password change but you do not see a re-auth prompt, try **Update credentials** (above) or **Reload** the integration from the integration menu — that re-runs login validation and can surface the re-auth flow.

The re-authentication and reconfigure steps are translated like the rest of the config flow (all supported languages).

### Delete/re-add is not required for credential changes

Removing the integration is still useful for registry cleanup (for example duplicate entities after a hardware swap), but it should not be necessary just to update login details.

## Reliability and Errors

- Temporary problems on SolarEdge’s servers (e.g. HTTP 5xx errors) or network/DNS issues (e.g. “Failed to resolve monitoring.solaredge.com”) are handled without crashing: the integration uses cached data where possible and will try again on the next update.
- If the inverter information API returns **403 Forbidden** (e.g. some accounts lack that permission), the integration still works: inverter and optimizer devices use position-based identity, so model names may be missing but all sensors and devices function. The integration logs a one-time warning; no action is required.
- A full refresh can take several minutes on sites with many optimizers. When using **SolarEdge One**, optimizer live data is fetched in **one batch POST** (all serials) per full refresh, reducing portal load; if that batch fails, the integration falls back to per-optimizer requests. When the lifetime-energy cache is cold, the integration fetches it **in parallel** (thread pool, up to `MAX_PARALLEL_WORKERS` = 10 concurrent requests) instead of one request per optimizer, so large sites complete much faster. The integration allows up to **30 minutes** for a full refresh before timing out (configurable via `COORDINATOR_REFRESH_TIMEOUT_SEC` in `const.py`), so slow API connections or sites with many optimizers can complete. API requests use configurable timeouts (`API_TIMEOUT_SHORT` = 30s for quick requests, `API_TIMEOUT_LONG` = 60s for longer operations).
- All API sessions and connections are released when the integration is removed or reloaded: the **legacy** client tracks every thread-local `requests.Session`, closes each in `close()`, and uses `with session.request(..., timeout=API_TIMEOUT_LONG)` so each response is consumed and connections return to the pool or close (v2.4.21+ timeout on `_doRequest`). Legacy `close()` waits for in-flight HTTP calls (up to ~30s) before releasing sessions. If legacy session bootstrap fails while priming cookies/CSRF, that temporary `Session` is closed immediately instead of being reused. For accounts where SolarEdge no longer sets `CSRF-TOKEN` on `.../solaredge-web/p/login`, the legacy client retries `.../solaredge-web/p/logout/slo` and then `.../solaredge-web/p/login` before CSRF-protected POSTs. If SolarEdge still rejects a legacy POST with **HTTP 498**, the logs now call that out explicitly as a likely rejected/missing/expired CSRF token or legacy web session. The **SolarEdge One** client uses `with requests.get/post(...)` for routine calls; on HTTP 401 the first response is **closed before** token refresh/`refresh_token`/PKCE and a single retry (v2.4.21+); OAuth uses `with Session()` only during login/refresh under a token lock; and `with ThreadPoolExecutor(...)` for parallel lifetime fetches so workers shut down; `close()` clears tokens and marks the client closed. **`async_unload_entry`** shuts down the coordinator (when `async_shutdown` is available) **before** closing the API, then pops the coordinator; **`async_remove_entry`** also calls `await hass.async_add_executor_job(api.close)` on the dual API (which closes One and legacy even if one side errors). From **v2.4.19**, the dual API emits **one** INFO close summary on unload/removal when both backends close cleanly; a **WARNING** is logged if either backend close fails. Per-backend close details are DEBUG when invoked via the dual wrapper. Setup failures also close the API in a `finally` block when the coordinator was not stored. This avoids leaking sockets/file descriptors during long HA uptime.
- If setup fails with `Requirements for solaredgeoptimizers not found: ['jsonfinder==0.4.2']`, update to **v2.4.21+** via HACS and restart Home Assistant. That package is no longer required (`manifest.json` `requirements` is empty; legacy decode uses stdlib `json`).
- **Replaced/inactive optimizers (v2.4.19+):** SolarEdge may keep old serials in layout with empty live measurements. Harmless if only those serials warn; the integration logs empty measurements at **debug** for inactive layout status (One and legacy), excludes inactive serials from lightweight polling samples, and still includes them on full refresh for status/history. See `miscellaneous/support-replaced-optimizer-warnings.md`.
- When you **delete the integration** (remove the config entry), the integration removes all associated entities and devices from the registries via a shared cleanup routine (used by both the config flow and unload), so no leftover entries remain. Delete from **Settings → Devices & services → Integrations** (not only from HACS) so that this cleanup runs.
- **Large sites (many optimizers):** If startup previously flooded Home Assistant with hundreds of entity-registry updates or triggered “Client unable to keep up with pending messages” in the browser, update to **v2.4.18+**, which registers sensors in batches instead of one bulk `async_add_entities` call. Tune `ENTITY_ADD_BATCH_SIZE` in `const.py` if needed (lower = gentler on HA, higher = faster setup). From **v2.4.21**, missing-optimizer backfill at setup prefers One batch API and otherwise caps concurrency at `MAX_PARALLEL_WORKERS`. After reload, per-optimizer values usually appear within seconds (coordinator listener notify + `async_added_to_hass` reapply when coordinator data is already loaded) or within the next coordinator refresh (a few minutes on a slow first full fetch); **unknown** on many sensors right after setup is often normal until data arrives.
- **Entity ID ending in `_2` (e.g. `sensor.power_1_0_1_2`):** The integration builds three-segment optimizer paths (e.g. `power_1_0_1`). A four-segment id with `_2` at the end is usually Home Assistant registry disambiguation when a stale entity kept the base `entity_id`. Check **Settings → Entities** for both `power_1_0_1` and `power_1_0_1_2`, compare `unique_id`, then **remove and re-add** the integration if a stale row persists after upgrading.

## Installation

Until this integration is part of Home Assistant Core, installing via HACS is recommended.

1. **Add the repository in HACS**
   - Go to **HACS** → click the three dots (top right) → **Custom repositories**.
   - Repository URL: `https://github.com/AndrewTapp/solaredgeoptimizers`
   - Category: **Integration** → **Add**.

2. **Install the integration**
   - In HACS, open **SolarEdge Optimizers** (or **SolarEdge Optimizers Data**) and click **Download**.

3. **Restart Home Assistant.**

4. **Configure**
   - **Settings** → **Devices & services** → **Add Integration** → search for **SolarEdge Optimizers**.
   - Enter your **Site ID**, **Username**, and **Password**.
  - Optionally set **Entity ID prefix** (e.g. `se_`) so all entity IDs start with that prefix; leave blank for no prefix.
  - Optionally enable **Include Site ID in Entity ID** (default off) to include the site ID in inverter/string/optimizer entity IDs; site-level entities always show the site ID.
  - **Use SolarEdge One** (default on): when **Yes**, the integration tries SolarEdge One first and falls back to legacy when needed; when **No**, it always uses the legacy portal only.

The **Obtained from** sensor on the site device shows "One API" or "Legacy API".

**Configure (optional):** After setup, you can change **Entity ID prefix**, **Include Site ID in Entity ID**, or **Use SolarEdge One** without deleting the integration: go to **Settings** → **Devices & services** → **SolarEdge Optimizers** → your site → **Configure**. The dialog shows Entity ID prefix (description shows current prefix; leave empty to remove it), Include Site ID in Entity ID, and Use SolarEdge One. Saving will reload the integration. The integration only rebuilds entities when entity-ID shaping actually changed (prefix or Include Site ID), so normal restarts/reloads do not churn the entity registry. **Note:** Changing Entity ID prefix or Include Site ID can change entity IDs and unique_ids, so existing entity history and statistics may be lost; consider backing up or exporting data first. To update **username or password**, use **Update credentials** from the integration menu (three dots), not **Configure** — see [Re-authentication and updating credentials](#re-authentication-and-updating-credentials) above.

On first load, the integration fetches all optimizer data once in the coordinator; the sensor platform then reuses that data when creating entities, so it does not send duplicate API calls for each optimizer. Optimizer devices are created in the registry before optimizer sensors are added. With many optimizers, entities are registered in batches of `ENTITY_ADD_BATCH_SIZE` (default 50) with short yields to the event loop between batches, so Home Assistant can process registry and state events without flooding the websocket bus. After the last batch, the coordinator notifies all entity listeners so states can update immediately. `update_before_add` is not used at registration time because the coordinator has already completed its first refresh. The initial fetch and batched device/entity creation may still take a short while on large sites.

## Debug logging

To help troubleshoot setup or update issues, you can enable debug logging for this integration. In your Home Assistant `configuration.yaml` add:

```yaml
logger:
  default: info
  logs:
    solaredgeoptimizers: debug
```

Restart Home Assistant for the change to take effect. Debug logging covers the full lifecycle: config flow (including `format_config_entry_title`, title self-repair at setup, reauth, **Reconfigure** credential forms (v2.4.20+), and Configure/options with normalized prefix (v2.4.21+)); setup and unload (including coordinator `async_shutdown` before dual `api.close()`, with per-backend DEBUG close detail); coordinator (device hierarchy, `build_optimizer_tasks` position indexing with suffixes such as `1.1.1a`, **active-only** lightweight check sampling **rotated each check**, auth-like failure handling on full refresh and light check (v2.4.20+/v2.4.21+), adaptive polling, aggregation, portal lifetime overrides with zero-agg and max-ratio guards); sensor setup (optimizer device registration with `build_string_device_key_lookup`, missing-optimizer backfill INFO, batched `async_add_entities` with per-batch range, post-batch `coordinator.async_update_listeners()` — sync, not awaited — `_lookup_optimizer_data_item`, coordinator reapply in `async_added_to_hass` on optimizer sensors (v2.4.21+), inactive skipping, short translated names); and API traffic (SolarEdge One with redacted OAuth URLs, refresh_token, close-before-401-retry; legacy including `_doRequest` timeout, CSRF/498 diagnostics, layout-status-aware measurement parsing, and `decodeResult` / stdlib embedded-JSON fallback path selection; dual API fallback, verify_authentication on auth-like total failure (v2.4.20+), and `close()`). From **v2.4.19**, empty measurements for inactive/replaced optimizers log at **debug** in `_normalize_measurements_dict`. `Info` logging keeps high-value summaries (setup complete for entry/site, batched registration totals, missing-optimizer backfill count, duplicate suffix counts, optimizer device registration count, config entry title repair, credential updates via reauth/Reconfigure (v2.4.20+), OAuth refresh→PKCE re-login (v2.4.21+), **one** dual API close summary on unload/removal when both backends succeed) and avoids credentials or per-refresh cache chatter. Dual API logs a **warning** if either backend fails to close. Debug calls use `isEnabledFor(logging.DEBUG)` (destructor breadcrumbs in `__del__` are the only exceptions). Prefixes: `SolarEdge Optimizers`, `SolarEdge Optimizers coordinator`, `SolarEdge Optimizers sensor`, `SolarEdge One`, `SolarEdge Optimizers (legacy)`, `SolarEdge Dual API`. Turn logging back to `info` when you are done.

**Deploy note:** Copy the **entire** `custom_components/solaredgeoptimizers/` folder to Home Assistant together — mixed versions (e.g. new `const.py` with old `sensor.py`) cause setup errors. A valid `sensor.py` is ~1,800+ lines and defines `async_setup_entry`.

**Code quality:** The project uses Pylint and pycodestyle (aligned with [CodeFactor](https://www.codefactor.io/repository/github/AndrewTapp/solaredgeoptimizers) on the repo). Root `.pylintrc` sets `max-args=10`, `max-module-lines=2000`, `max-line-length=159`; `setup.cfg` sets pycodestyle `max-line-length=159`. Prefer focused functions (e.g. shared portal lifetime override helper, shared One API 401-retry path), guard debug logging, avoid logging credentials or repetitive per-refresh summaries at `info`, and use inline `# pylint: disable=...` only where the Home Assistant/coordinator design needs extra parameters or branches. CI runs `compileall` and unit tests under `.github/workflows/ci.yml`.

## Translations

The integration is localized for multiple languages: config flow (labels, errors, entry title), sensor and device names, and API locale follow the user’s Home Assistant language where supported. See [Internationalization (i18n)](https://github.com/AndrewTapp/solaredgeoptimizers/blob/main/docs/internationalization.md) for details.

The config flow (add-integration setup) is translated into:

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
| pt   | Português   |
| ru   | Русский    |
| sv   | Svenska    |
| tr   | Türkçe     |
| zh   | 中文       |

To add another language, add a `translations/<code>.json` file with the same structure as `en.json` (config, options, entity, and device sections). The **options** section is used for the Reconfigure (Configure) dialog labels and description.

---

## Many thanks to the following people

[@proudelm](https://github.com/proudelm) creator of the original integration.  
[@Mariusthvdb](https://github.com/Mariusthvdb) for his help getting me up and running with this fork of the original integration.

## Donators

Thank you to the PayPal and Buy Me a Coffee donators.

|  |  |  |  | 
|--------------------|--------------------|----------------------|----------------------|
| Neo8101 | FFoXXaNN |  |  |
| apf-doit | JochenGr | James Kaiser | dselb |
