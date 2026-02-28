# SolarEdge Optimizers Integration

[![Release](https://img.shields.io/github/release/AndrewTapp/solaredgeoptimizers.svg)](https://github.com/AndrewTapp/solaredgeoptimizers/releases)
[![CodeFactor Grade](https://img.shields.io/codefactor/grade/github/AndrewTapp/solaredgeoptimizers.svg)](https://www.codefactor.io/repository/github/AndrewTapp/solaredgeoptimizers)
[![Donate](https://img.shields.io/badge/Donate-PayPal-green.svg)](https://www.paypal.me/AndrewJTapp)
[![Donate](https://img.shields.io/badge/Donate-BuyMeACoffee-green.svg)](https://buymeacoffee.com/andrewtapp)

This integration brings your SolarEdge optimizer data from the SolarEdge monitoring portal into Home Assistant. You can see current production, voltage, power, temperature (SolarEdge One only), and lifetime energy at the level of individual optimizers, strings, inverters, or the whole site.

**📖 [Technical documentation (Wiki)](https://github.com/AndrewTapp/solaredgeoptimizers/wiki)** — architecture, data flow, sensors reference, troubleshooting, and more.

## Upgrading from an earlier version

If you are on a version prior to v2.4.0 (or want a clean registry after any upgrade), the steps below ensure entities and devices are recreated correctly:

1. **Update the integration via HACS** so the new code (including cleanup) is installed.
2. **Restart Home Assistant** (optional but recommended).
3. **Remove the integration from Home Assistant:** **Settings → Devices & services → Integrations** → SolarEdge Optimizers → **Delete**. This runs `async_remove_entry` and cleans entity and device registries for that entry.
4. **Restart Home Assistant** (optional but recommended).
5. **Clear browser cache** [Ctrl]+[Shift]+r on Microsoft Edge.
6. **Re-add the integration** with the same Site ID, username, password, and options. You get fresh entities and a clean registry; history reconnects because `unique_id`s are the same.

## SolarEdge One and legacy API (dual API)

The SolarEdge monitoring portal is being upgraded to **SolarEdge One**. The integration uses a **dual API**: when **Use SolarEdge One** is **Yes** (default), it always tries the **SolarEdge One API** first (`/services/layout/...`). If One returns no valid measurements (e.g. "Missing or invalid measurements" for all optimizers) or One login fails, it automatically falls back to the **legacy** SolarEdge API. When One starts returning valid data again, the integration switches back to One—either when a lightweight check sees newer data from One, or **every 30 minutes** when data is currently from legacy (so One is re-tried periodically). When **Use SolarEdge One** is **No**, the integration always uses the **legacy** portal only. Same Site ID, username, and password for both. A site-level sensor **Obtained from** shows whether current data came from **"One API"** or **"Legacy API"**. When data is from One, optimizer and inverter devices show the **model** (e.g. P405-4RM4MRM-NA25, SE5000H-RW000BNN4) and serial number. When the API provides a **panel type** (description, e.g. SunPower SPR-MAX3-400), it is included in the optimizer device model and exposed as a **panel_type** attribute on optimizer sensors.

## What You Need

To set up the integration you will need:

- Your **Site ID** (from the SolarEdge portal)
- Your **Username**
- Your **Password**
- **Entity ID prefix** (optional) – If you run more than one site or want to avoid clashes with other integrations, you can set a short prefix (e.g. `se_`). All entity IDs will then start with that prefix (e.g. `sensor.se_power_9999999`). Leave blank for no prefix. If you upgrade from an older version without removing the integration, this defaults to blank when not set.
- **Include Site ID in Entity ID** (optional, default **off**) – When off, entity IDs for inverter, string, and optimizer levels omit the site ID (e.g. `sensor.power_1_1`, `sensor.power_1_1_1`). The site level always shows the actual site ID (e.g. `sensor.power_9999999`). Turn on to include the site ID in every level (e.g. `sensor.power_9999999_1_1_1`). If you upgrade from an older version without removing the integration, this defaults to off when not set.
- **Use SolarEdge One** (optional, default **on**) – When **Yes**, the integration tries the SolarEdge One API first and falls back to the legacy API when needed (see above). When **No**, the integration always uses the legacy portal only. You can change this later in **Configure**.

## How Your System Is Shown in Home Assistant

Your solar system is organised in a simple hierarchy. Device and entity names include the site so that multiple sites stay distinct (e.g. no duplicate `sensor.power` when you have two sites):

- **Site [site]** – Your whole installation (e.g. Site 9999999). Entity IDs look like `sensor.[prefix]power_9999999`, `sensor.[prefix]inverter_count_9999999`, and so on.
- **Inverter [site].[i]** – e.g. “Inverter 9999999.1”, “Inverter 9999999.2”. Entity IDs: `sensor.[prefix]power_9999999_1`, etc.
- **String [site].[i].[s]** – e.g. “String 9999999.1.1”. Entity IDs: `sensor.[prefix]power_9999999_1_1`, `sensor.[prefix]lifetime_energy_1_0`, etc. (Device names and entity IDs at string/optimizer level follow the API display name when it parses.)
- **Optimizer [site].[i].[s].[o]** – e.g. “Optimizer 9999999.1.1.1”. Entity IDs: `sensor.[prefix]power_9999999_1_1_1`, `sensor.[prefix]lifetime_energy_1_0_1`, etc.

*[prefix]* is your optional Entity ID prefix (blank if not set). By default, **Include Site ID in Entity ID** is off, so inverter/string/optimizer entity IDs are shorter; the site level always shows the actual site ID. At **string and optimizer** level, device names and entity IDs are **based on the API display name** when it parses (e.g. "1.0" → "String 1.0" and `sensor.lifetime_energy_1_0`; "1.0.1" → "Optimizer 1.0.1" and `sensor.lifetime_energy_1_0_1`). If the API display name does not parse, position-based indices are used. Site and inverter stay position-based. Entity IDs are path-based only (no device-name prefix), e.g. `sensor.xyz_power_1_0` for a string or `sensor.xyz_power_1_0_1` for an optimizer when the API uses that numbering. The integration ensures site, inverter, and string devices are created before optimizer entities are added, avoiding "references a non existing via_device". The device hierarchy is **Site → Inverter → String → Optimizer**; optimizers are grouped under their string. In **Settings → Devices & services**, each device shows what it's **connected via** (e.g. an optimizer shows its string, a string shows its inverter). Friendly names and “connected via” use this same hierarchy so the layout is easy to follow. The integration entry title shows your site (e.g. "SolarEdge Site 9999999").

## What Data You Get

### Per optimizer (each panel)

- **Voltage**, **Current**, **Optimizer voltage**, **Power** – Live values when the optimizer is reporting.
- **Temperature** – Optimizer temperature from the SolarEdge One API (layout/energy by-inverter with `include-max-temperature`). The portal may report in °C or °F (`temperatureUnit`); the integration normalizes to °C for storage and Home Assistant displays in your preferred unit. Only available when using the One API; shown as “unknown” when missing or when using the legacy API. When the integration is not doing a full refresh (e.g. reusing data after a light check), it still refreshes temperatures about every 15 minutes via a cached API call, so temperature stays up to date even when power/voltage are not updating.
- **Lifetime energy** – Total energy produced (kWh); this only goes up over time. The integration uses the API’s raw energy value (unscaledEnergy, in Wh) so it updates correctly regardless of how the portal displays units (Wh/kWh/MWh). When optimizer-level lifetime data is reliable, site lifetime is the sum of optimizers; when it is not (e.g. mixed or missing data), the site uses the portal’s total directly.
- **Last measurement** – When the portal last had a reading for this optimizer.

### Per string, inverter, and site

For each string, inverter, and the site you get combined (aggregated) sensors:

- **Current (average)** and **Voltage (average)**
- **Power** (total for that level)
- **Lifetime energy** (total for that level)
- **Last measurement**
- **Optimizer count** (strings) / **String count** (inverters) / **Inverter count** (site) – Always reported as integers (e.g. 3, not 3.0).
- **Last polled** (site device only) – When the integration last successfully fetched data from the SolarEdge portal. Handy for checking that updates are running.
- **Obtained from** (site device only) – Which API provided the current data: **"One API"** or **"Legacy API"**. Entity ID: `sensor.[prefix]obtained_from_[site]` (or `sensor.[prefix]obtained_from` when site ID is not included in entity IDs).

Names are kept short (e.g. “Current (average)”, “Power”) because the device name (e.g. “String 1.1” or “Inverter 1”) already tells you where the value comes from.

## How Often Data Updates

- The integration checks for new data every few minutes. It does a lightweight check (one or a few optimizers) to see if the portal has new readings; when it detects new data, a full refresh runs so all sensors update. When using **SolarEdge One**, up to five optimizers are chosen at random for each check so different orientations and shade don't block updates; when falling back to the legacy API, a single representative optimizer is used for the light check. When data is currently from the **legacy** API, the integration also forces a full refresh **every 30 minutes** so it re-tries the SolarEdge One API and can switch back to One when it becomes available again.
- **Lifetime energy** is only refreshed from the portal about once per hour, because that value changes slowly. It is derived from the API’s unscaled energy (Wh), not the display units, so values update correctly. Totals for strings, inverters, and the site are calculated from that data.

- **Temperature** (SolarEdge One only): When the integration does not perform a full refresh (e.g. it reuses existing data after a light check), it still refreshes optimizer temperatures about every 15 minutes via a cached temperature API. So temperature sensors stay updated even when power, voltage, and current are not being refreshed.

So in normal use you see updates every few minutes when the portal has new data; temperature (when using One API) is refreshed about every 15 minutes even when there is no full refresh, and lifetime energy at most once per hour.

## When an Optimizer Is Offline or Not Reporting

- If the **last measurement** is older than the stale threshold, **Voltage**, **Current**, **Optimizer voltage**, and **Power** are shown as **0** for that optimizer (and any aggregates that depend on it). This avoids showing stale “live” values. The threshold is **1 hour** when data is from **One API** and **2 hours** when from **Legacy API**. Check the **Obtained from** sensor to see which API is in use.
- **Temperature** (when available from SolarEdge One) is not zeroed when stale; it shows the last known value or “unknown” if missing.
- **Lifetime energy** and **Last measurement** always show the last known values, so you can still see historical production even when a panel is temporarily offline.

## Replacing optimizers or inverters (hardware swap)

When an optimizer or inverter is replaced (e.g. after a hardware failure), the integration keeps **one device and one set of sensors per logical position**. Device and entity **names and IDs** at string and optimizer level follow the API display name when it parses (e.g. "1.0", "1.0.1"); data is still keyed by position. So when you swap in new hardware at the same position:

- The **same** sensor (e.g. Power for optimizer 1.0.1) continues to show data; after the next refresh it shows the **new** unit’s values. No duplicate entities.
- Inverter and string devices are identified by position (or by parsed display name for strings); replacing an inverter does not create a second inverter device. String and inverter sensors attach to those same devices, so the hierarchy (site → inverter → string → optimizer) stays correct.

If you already see duplicate sensors or devices from an earlier swap, remove the integration (**Settings → Devices & services → Integrations** → SolarEdge Optimizers → **Delete**) and add it again. That cleans up the registry and leaves a single set of position-based devices and sensors.

## One config entry per site

You can add the integration once per **Site ID**. If you try to add the same site again, Home Assistant will show "Device is already configured". To use multiple sites, add a separate integration entry for each site (each with its own Site ID).

## Re-authentication

If your SolarEdge credentials expire or become invalid (for example after a password change), the integration will detect this (e.g. when the API returns 401) and Home Assistant will prompt you to **re-authenticate**. You will see a form to enter your current username and password; after saving, the integration reloads with the new credentials. Only your username and password are updated; any settings you changed in **Configure** (e.g. Entity ID prefix, Include Site ID in Entity ID, Use SolarEdge One) are kept. The re-authentication step is translated like the rest of the config flow (all supported languages).

## Reliability and Errors

- Temporary problems on SolarEdge’s servers (e.g. HTTP 5xx errors) or network/DNS issues (e.g. “Failed to resolve monitoring.solaredge.com”) are handled without crashing: the integration uses cached data where possible and will try again on the next update.
- If the inverter information API returns **403 Forbidden** (e.g. some accounts lack that permission), the integration still works: inverter and optimizer devices use position-based identity, so model names may be missing but all sensors and devices function. The integration logs a one-time warning; no action is required.
- A full refresh can take several minutes on sites with many optimizers. When the lifetime-energy cache is cold, the integration fetches it **in parallel** (thread pool) instead of one request per optimizer, so large sites complete much faster. The integration allows up to **30 minutes** for a full refresh before timing out (configurable via `COORDINATOR_REFRESH_TIMEOUT_SEC` in `const.py`), so slow API connections or sites with many optimizers can complete.
- Connections and sessions are closed properly when the integration is removed or reloaded, so it's safe to run for long periods.
- When you **delete the integration** (remove the config entry), the integration removes all associated entities and devices from the registries via a shared cleanup routine (used by both the config flow and unload), so no leftover entries remain. Delete from **Settings → Devices & services → Integrations** (not only from HACS) so that this cleanup runs.

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

**Reconfigure (optional):** After setup, you can change **Entity ID prefix**, **Include Site ID in Entity ID**, or **Use SolarEdge One** without deleting the integration: go to **Settings** → **Devices & services** → **SolarEdge Optimizers** → your site → **Configure**. The dialog shows Entity ID prefix (description shows current prefix; leave empty to remove it), Include Site ID in Entity ID, and Use SolarEdge One. Saving will reload the integration. **Note:** Changing Entity ID prefix or Include Site ID can change entity IDs and unique_ids, so existing entity history and statistics may be lost; consider backing up or exporting data first.

On first load, the integration fetches all optimizer data once in the coordinator; the sensor platform then reuses that data when creating entities, so it does not send duplicate API calls for each optimizer. With many optimizers, the initial fetch and device creation may still take a short while.

## Debug logging

To help troubleshoot setup or update issues, you can enable debug logging for this integration. In your Home Assistant `configuration.yaml` add:

```yaml
logger:
  default: info
  logs:
    solaredgeoptimizers: debug
```

Restart Home Assistant for the change to take effect. Debug logging covers the full lifecycle: config flow (user form, validation, unique_id check, entry creation, reauth form, options/reconfigure form when showing or saving — including entity_id_prefix, include_site_id_in_entity_id, use_solaredge_one — removal), setup and unload (dual API with use_solaredge_one, coordinator, platform forward, API session close, registry cleanup), coordinator updates (inverter models fetch, device creation with model, adaptive polling, full refresh vs reuse, obtained_from source, revert-to-One retry every 30 min when data from legacy, representative optimizers or random batch, lifetime energy entries, update complete), sensor setup (base_name, include_site_id, per-optimizer serial/model/panel_type, aggregated sensors, obtained_from sensor, entity count), and API requests (SolarEdge One: OAuth, token, GET/POST to /services/, optimizer/inverter information including panel type (description), cache hit/miss, optimizer temperatures with unit and F→°C conversion when portal sends Fahrenheit; legacy: login, layout, system data, lifetime energy; dual API: use_solaredge_one/legacy-only, fallback to legacy when One has no valid measurements or fails). All debug output is guarded with `isEnabledFor(logging.DEBUG)`, so there is no performance cost when the log level is `info`. Debug messages use consistent prefixes (e.g. `SolarEdge Optimizers`, `SolarEdge Optimizers coordinator`, `SolarEdge Optimizers sensor`, `SolarEdge One`, `SolarEdge Optimizers (legacy)`) so you can filter logs easily. Turn logging back to `info` when you are done.

## Translations

The integration is localized for multiple languages: config flow (labels, errors, entry title), sensor and device names, and API locale follow the user’s Home Assistant language where supported. See [Internationalization (i18n)](https://github.com/AndrewTapp/solaredgeoptimizers/blob/main/docs/Internationalization.md) for details.

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
