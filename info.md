# SolarEdge Optimizers Integration

Brings your SolarEdge optimizer data from the SolarEdge monitoring portal into Home Assistant. You see current production, voltage, power, and lifetime energy for each optimizer (plus optimizer voltage and last measurement), and combined totals for each string, inverter, and the whole site.

**You need:** Site ID, username, and password (from the SolarEdge portal). Optionally: an **Entity ID prefix** (e.g. `se_`) so all entity IDs start with that prefix; **Include Site ID in Entity ID** (default off) to include the site ID in inverter/string/optimizer entity IDs (site level always shows the site ID). When upgrading from an older version without re-adding the integration, these two options default to blank and off if not present. **One config entry per site:** each Site ID can only be added once; adding the same site again is aborted. **Re-authentication:** if credentials become invalid (e.g. after a password change or expiry), the integration triggers a re-auth flow; you enter your current username and password in a translated form, then the integration reloads with the new credentials. Only credentials are updated; options (Entity ID prefix, Include Site ID in Entity ID) are preserved. **Reconfigure:** you can change Entity ID prefix or Include Site ID in Entity ID from the integration’s Configure (options flow) without removing the entry. The dialog shows the current prefix in the description; leave the Entity ID prefix field empty to remove the prefix. Saving reloads the integration. Changing these can change entity IDs and unique_ids, so history and statistics for existing entities may be lost; the options form warns users and recommends backing up or exporting first.

**In Home Assistant** your system appears as a hierarchy: **Site [site]** → **Inverter [site].[i]** → **String [site].[i].[s]** → **Optimizer [site].[i].[s].[o]** (e.g. Site 9999999, Inverter 9999999.1, String 9999999.1.1, Optimizer 9999999.1.1.1). Entity IDs are path-based (e.g. `sensor.xyz_power_9999999_1_1` or, with “Include Site ID” off, `sensor.xyz_power_1_1` for a string; site level always includes the site ID). Optimizers are grouped under their string device; each device shows what it's **connected via** in Settings → Devices & services. The config entry title shows the site (e.g. "SolarEdge Site 9999999"), using the translated title with the site ID substituted.

**Updates:** The integration checks for new data every few minutes and refreshes when the portal has new readings. Lifetime energy is updated from the portal about once per hour (unscaledEnergy, Wh). When optimizer-level lifetime data is reliable, site lifetime is the sum of optimizers; when it is not (e.g. mixed or missing data), the site uses the portal’s total directly.

**When an optimizer is offline:** Live values (voltage, current, optimizer voltage, power) show 0 if the last measurement is older than two hours. Lifetime energy and last measurement always show the last known values.

**Reliability:** Temporary SolarEdge or network issues (e.g. HTTP 5xx, DNS) are handled with cached data where possible; connections are closed properly on remove/reload. When you delete the integration from **Settings → Devices & services → Integrations** (not only from HACS), all associated entities and devices are removed from the entity and device registries so no leftovers remain.

**Child counts:** Optimizer count (per string), String count (per inverter), and Inverter count (per site) are always reported as integers.

**Debug logging:** To troubleshoot setup or updates, set the logger for `solaredgeoptimizers` to `debug` in your `configuration.yaml` (see README). Debug logging covers the full lifecycle (config flow, setup/unload, coordinator, sensors, API). Output is only generated when the log level is debug.

## Installation

Install via HACS as a custom repository: add `https://github.com/AndrewTapp/solaredgeoptimizers` (Integration), then install **SolarEdge Optimizers** (or **SolarEdge Optimizers Data** in HACS), restart Home Assistant, and add the integration with your Site ID, username, password, optional Entity ID prefix, and optional **Include Site ID in Entity ID** (default off). Initial setup can take a while if you have many optimizers.

## Translations

The integration is localized for multiple languages (config flow, options/reconfigure dialog, sensor and device names, API locale). Translation files use the same structure as `en.json`: config, options, entity, and device sections. Supported codes: cs, da, de, el, en, es, fi, fr, hu, it, ja, nb, nl, pl, pt, ru, sv, tr, zh. See [Internationalization (i18n)](docs/internationalization.md) for details.
