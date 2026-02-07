# SolarEdge Optimizers Integration

Brings your SolarEdge optimizer data from the SolarEdge monitoring portal into Home Assistant. You see current production, voltage, power, and lifetime energy for each optimizer (plus optimizer voltage and last measurement), and combined totals for each string, inverter, and the whole site.

**You need:** Site ID, username, and password (from the SolarEdge portal). Optionally, an **Entity ID prefix** (e.g. `se_`) so all entity IDs start with that prefix—useful when you have multiple sites or want to avoid clashes with other integrations.

**In Home Assistant** your system appears as a hierarchy: **Site [site]** → **Inverter [site].[i]** → **String [site].[i].[s]** → **Optimizer [site].[i].[s].[o]** (e.g. Site 9999999, Inverter 9999999.1, String 9999999.1.1, Optimizer 9999999.1.1.1). Entity IDs are short and path-based only—no device-name prefix (e.g. `sensor.xyz_power_9999999_1_1` for a string, `sensor.xyz_power_9999999_1_1_7` for an optimizer, where *xyz* is your optional prefix). Optimizers are grouped under their string device; each device shows what it's **connected via** in Settings → Devices & services. The config entry title shows the site (e.g. "SolarEdge Site 9999999"), using the translated title with the site ID substituted.

**Updates:** The integration checks for new data every few minutes and refreshes when the portal has new readings. Lifetime energy is updated from the portal about once per hour, using the API’s raw value (unscaledEnergy, Wh) so it updates correctly across all sites.

**When an optimizer is offline:** Live values (voltage, current, optimizer voltage, power) show 0 if the last measurement is older than one hour. Lifetime energy and last measurement always show the last known values.

**Reliability:** Temporary SolarEdge or network issues (e.g. HTTP 5xx, DNS) are handled with cached data where possible; connections are closed properly on remove/reload.

## Installation

Install via HACS as a custom repository: add `https://github.com/AndrewTapp/solaredgeoptimizers` (Integration), then install **SolarEdge Optimizers** (or **SolarEdge Optimizers Data** in HACS), restart Home Assistant, and add the integration with your Site ID, username, password, and optional Entity ID prefix. Initial setup can take a while if you have many optimizers.

## Translations

The integration is localized for multiple languages (config flow, sensor and device names, API locale). Supported codes: cs, da, de, el, en, es, fi, fr, hu, it, ja, nb, nl, pl, pt, ru, sv, tr, zh. See [Internationalization (i18n)](docs/internationalization.md) for details.
