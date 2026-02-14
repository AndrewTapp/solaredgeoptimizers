# SolarEdge Optimizers Integration

[![Release](https://img.shields.io/github/release/AndrewTapp/solaredgeoptimizers.svg)](https://github.com/AndrewTapp/solaredgeoptimizers/releases)
[![CodeFactor Grade](https://img.shields.io/codefactor/grade/github/AndrewTapp/solaredgeoptimizers.svg)](https://www.codefactor.io/repository/github/AndrewTapp/solaredgeoptimizers)
[![Donate](https://img.shields.io/badge/Donate-PayPal-green.svg)](https://www.paypal.me/AndrewJTapp)
[![Donate](https://img.shields.io/badge/Donate-BuyMeACoffee-green.svg)](https://buymeacoffee.com/andrewtapp)

This integration brings your SolarEdge optimizer data from the SolarEdge monitoring portal into Home Assistant. You can see current production, voltage, power, and lifetime energy at the level of individual optimizers, strings, inverters, or the whole site.

**📖 [Technical documentation (Wiki)](https://github.com/AndrewTapp/solaredgeoptimizers/wiki)** — architecture, data flow, sensors reference, troubleshooting, and more.

## Upgrading from a version prior to v2.3.4:

1. **Update the integration via HACS** so the new code (including cleanup) is installed.
2. **Restart Home Assistant** (optional but recommended).
3. **Remove the integration from Home Assistant:** **Settings → Devices & services → Integrations** → SolarEdge Optimizers → **Delete**. This runs `async_remove_entry` and cleans entity and device registries for that entry.
4. **Restart Home Assistant** (optional but recommended).
5. **Clear browser cache** [Ctrl]+[Shift]+r on Microsoft Edge.
6. **Re-add the integration** with the same Site ID, username, password, and options. You get fresh entities and a clean registry; history reconnects because `unique_id`s are the same.

## What You Need

To set up the integration you will need:

- Your **Site ID** (from the SolarEdge portal)
- Your **Username**
- Your **Password**
- **Entity ID prefix** (optional) – If you run more than one site or want to avoid clashes with other integrations, you can set a short prefix (e.g. `se_`). All entity IDs will then start with that prefix (e.g. `sensor.se_power_9999999`). Leave blank for no prefix. If you upgrade from an older version without removing the integration, this defaults to blank when not set.
- **Include Site ID in Entity ID** (optional, default **off**) – When off, entity IDs for inverter, string, and optimizer levels omit the site ID (e.g. `sensor.power_1_1`, `sensor.power_1_1_1`). The site level always shows the actual site ID (e.g. `sensor.power_9999999`). Turn on to include the site ID in every level (e.g. `sensor.power_9999999_1_1_1`). If you upgrade from an older version without removing the integration, this defaults to off when not set.

## How Your System Is Shown in Home Assistant

Your solar system is organised in a simple hierarchy. Device and entity names include the site so that multiple sites stay distinct (e.g. no duplicate `sensor.power` when you have two sites):

- **Site [site]** – Your whole installation (e.g. Site 9999999). Entity IDs look like `sensor.[prefix]power_9999999`, `sensor.[prefix]inverter_count_9999999`, and so on.
- **Inverter [site].[i]** – e.g. “Inverter 9999999.1”, “Inverter 9999999.2”. Entity IDs: `sensor.[prefix]power_9999999_1`, etc.
- **String [site].[i].[s]** – e.g. “String 9999999.1.1”. Entity IDs: `sensor.[prefix]power_9999999_1_1`, etc.
- **Optimizer [site].[i].[s].[o]** – e.g. “Optimizer 9999999.1.1.1”. Entity IDs: `sensor.[prefix]power_9999999_1_1_1`, etc.

*[prefix]* is your optional Entity ID prefix (blank if not set). By default, **Include Site ID in Entity ID** is off, so inverter/string/optimizer entity IDs are shorter; the site level always shows the actual site ID. Entity IDs are path-based only (no device-name prefix), e.g. `sensor.xyz_power_99999995_1_1` for a string or `sensor.xyz_power_9999999_1_1_7` for an optimizer. The device hierarchy is **Site → Inverter → String → Optimizer**; optimizers are grouped under their string. In **Settings → Devices & services**, each device shows what it's **connected via** (e.g. an optimizer shows its string, a string shows its inverter). Friendly names and “connected via” use this same hierarchy so the layout is easy to follow. The integration entry title shows your site (e.g. "SolarEdge Site 9999999").

## What Data You Get

### Per optimizer (each panel)

- **Voltage**, **Current**, **Optimizer voltage**, **Power** – Live values when the optimizer is reporting.
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

Names are kept short (e.g. “Current (average)”, “Power”) because the device name (e.g. “String 1.1” or “Inverter 1”) already tells you where the value comes from.

## How Often Data Updates

- The integration checks for new data every few minutes. When the SolarEdge portal has new readings, a full refresh runs so all sensors update.
- **Lifetime energy** is only refreshed from the portal about once per hour, because that value changes slowly. It is derived from the API’s unscaled energy (Wh), not the display units, so values update correctly. Totals for strings, inverters, and the site are calculated from that data.

So in normal use you see updates every few minutes when the portal has new data, and lifetime energy at most once per hour.

## When an Optimizer Is Offline or Not Reporting

- If the **last measurement** is older than two hours, **Voltage**, **Current**, **Optimizer voltage**, and **Power** are shown as **0** for that optimizer (and any aggregates that depend on it). This avoids showing stale “live” values.
- **Lifetime energy** and **Last measurement** always show the last known values, so you can still see historical production even when a panel is temporarily offline.

## One config entry per site

You can add the integration once per **Site ID**. If you try to add the same site again, Home Assistant will show "Device is already configured". To use multiple sites, add a separate integration entry for each site (each with its own Site ID).

## Re-authentication

If your SolarEdge credentials expire or become invalid (for example after a password change), the integration will detect this (e.g. when the API returns 401) and Home Assistant will prompt you to **re-authenticate**. You will see a form to enter your current username and password; after saving, the integration reloads with the new credentials. Only your username and password are updated; any settings you changed in **Configure** (e.g. Entity ID prefix, Include Site ID in Entity ID) are kept. The re-authentication step is translated like the rest of the config flow (all supported languages).

## Reliability and Errors

- Temporary problems on SolarEdge’s servers (e.g. HTTP 5xx errors) or network/DNS issues (e.g. “Failed to resolve monitoring.solaredge.com”) are handled without crashing: the integration uses cached data where possible and will try again on the next update.
- Connections and sessions are closed properly when the integration is removed or reloaded, so it's safe to run for long periods.
- When you **delete the integration** (remove the config entry), the integration removes all associated entities from the entity registry and all associated devices from the device registry, so no leftover entries remain. Delete from **Settings → Devices & services → Integrations** (not only from HACS) so that this cleanup runs.

## Installation

Until this integration is part of Home Assistant Core, installing via HACS is recommended.

1. **Add the repository in HACS**
   - Go to **HACS** → click the three dots (top right) → **Custom repositories**.
   - Repository URL: `https://github.com/AndrewTapp/solaredgeoptimizers`
   - Category: **Integration** → **Add**.

2. **Install the integration**
   - In HACS, open **SolarEdge Optimizers** and click **Download**.

3. **Restart Home Assistant.**

4. **Configure**
   - **Settings** → **Devices & services** → **Add Integration** → search for **SolarEdge Optimizers**.
   - Enter your **Site ID**, **Username**, and **Password**.
   - Optionally set **Entity ID prefix** (e.g. `se_`) so all entity IDs start with that prefix; leave blank for no prefix.
   - Optionally enable **Include Site ID in Entity ID** (default off) to include the site ID in inverter/string/optimizer entity IDs; site-level entities always show the site ID.

**Reconfigure (optional):** After setup, you can change **Entity ID prefix** or **Include Site ID in Entity ID** without deleting the integration: go to **Settings** → **Devices & services** → **SolarEdge Optimizers** → your site → **Configure**. The dialog shows your current prefix in the description; leave the Entity ID prefix field empty to remove the prefix. Saving will reload the integration. **Note:** Changing these options can change entity IDs and unique_ids, so existing entity history and statistics may be lost; consider backing up or exporting data first.

The first load can take a while if you have many optimizers; the integration fetches and organises all of them.

## Debug logging

To help troubleshoot setup or update issues, you can enable debug logging for this integration. In your Home Assistant `configuration.yaml` add:

```yaml
logger:
  default: info
  logs:
    solaredgeoptimizers: debug
```

Restart Home Assistant for the change to take effect. Debug logging covers the full lifecycle: config flow (user form, validation, entry creation, reauth form, options/reconfigure form, removal), setup and unload (coordinator, platforms, API sessions), coordinator updates (adaptive polling, full refresh vs reuse, representative optimizer, include_site_id_in_entity_id), sensor setup (entities added, config options), and API requests (login, layout, system data, lifetime energy). All debug output is guarded so there is no performance cost when the log level is `info`. Turn logging back to `info` when you are done.

## Translations

The integration is localized for multiple languages: config flow (labels, errors, entry title), sensor and device names, and API locale follow the user’s Home Assistant language where supported. See [Internationalization (i18n)](https://github.com/AndrewTapp/solaredgeoptimizers/blob/main/custom_components/solaredgeoptimizers/docs/internationalization.md) for details.

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
| FFoXXaNN |  |  |  |
| apf-doit | JochenGr | James Kaiser | dselb |
