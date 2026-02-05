# SolarEdge Optimizers Integration

[![Release](https://img.shields.io/github/release/AndrewTapp/solaredgeoptimizers.svg)](https://github.com/AndrewTapp/solaredgeoptimizers/releases)
[![Donate](https://img.shields.io/badge/Donate-PayPal-green.svg)](https://www.paypal.me/AndrewJTapp)
[![Donate](https://img.shields.io/badge/Donate-BuyMeACoffee-green.svg)](https://buymeacoffee.com/andrewtapp)

This integration brings your SolarEdge optimizer data from the SolarEdge monitoring portal into Home Assistant. You can see current production, voltage, power, and lifetime energy at the level of individual optimizers, strings, inverters, or the whole site.

**📖 [Technical documentation (Wiki)](https://github.com/AndrewTapp/solaredgeoptimizers/wiki)** — architecture, data flow, sensors reference, troubleshooting, and more.

## What You Need

To set up the integration you will need:

- Your **Site ID** (from the SolarEdge portal)
- Your **Username**
- Your **Password**

## How Your System Is Shown in Home Assistant

Your solar system is organised in a simple hierarchy:

- **Site** – Your whole installation (e.g. Site 2nnnnnn).
- **Inverters** – Shown as “Inverter 1”, “Inverter 2”, and so on.
- **Strings** – Under each inverter, e.g. “String 1.1”, “String 1.2”.
- **Optimizers** – Under each string, e.g. “Optimizer 1.1.1”, “Optimizer 1.1.2”.

In **Settings → Devices & services**, each device shows what it's **connected via** (e.g. an optimizer shows “String 1.1”, a string shows “Inverter 1”). This makes it easy to see how everything is linked.

## What Data You Get

### Per optimizer (each panel)

- **Voltage**, **Current**, **Optimizer voltage**, **Power** – Live values when the optimizer is reporting.
- **Lifetime energy** – Total energy produced (kWh); this only goes up over time.
- **Last measurement** – When the portal last had a reading for this optimizer.

### Per string, inverter, and site

For each string, inverter, and the site you get combined (aggregated) sensors:

- **Current (average)** and **Voltage (average)**
- **Power** (total for that level)
- **Lifetime energy** (total for that level)
- **Last measurement**
- **Optimizer count** (strings) / **String count** (inverters) / **Inverter count** (site)
- **Last polled** (site device only) – When the integration last successfully fetched data from the SolarEdge portal. Handy for checking that updates are running.

Names are kept short (e.g. “Current (average)”, “Power”) because the device name (e.g. “String 1.1” or “Inverter 1”) already tells you where the value comes from.

## How Often Data Updates

- The integration checks for new data every few minutes. When the SolarEdge portal has new readings, a full refresh runs so all sensors update.
- **Lifetime energy** is only refreshed from the portal about once per hour, because that value changes slowly. Totals for strings, inverters, and the site are calculated from that data.

So in normal use you see updates every few minutes when the portal has new data, and lifetime energy at most once per hour.

## When an Optimizer Is Offline or Not Reporting

- If the **last measurement** is older than one hour, **Voltage**, **Current**, **Optimizer voltage**, and **Power** are shown as **0** for that optimizer (and any aggregates that depend on it). This avoids showing stale “live” values.
- **Lifetime energy** and **Last measurement** always show the last known values, so you can still see historical production even when a panel is temporarily offline.

## Reliability and Errors

- Temporary problems on SolarEdge’s servers (e.g. HTTP 5xx errors) are handled without crashing; the integration will try again on the next update.
- Connections and sessions are closed properly when the integration is removed or reloaded, so it's safe to run for long periods.

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

The first load can take a while if you have many optimizers; the integration fetches and organises all of them.

## Translations

The integration is localized for multiple languages: config flow (labels, errors, entry title), sensor and device names, and API locale follow the user’s Home Assistant language where supported. See [Internationalization (i18n)](docs/internationalization.md) for details.

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

To add another language, add a `translations/<code>.json` file with the same structure as `en.json` (config, entity, and device sections).

---

## Many thanks to the following people

[@proudem](https://github.com/proudem) creator of the original integration.

[@Mariusthvdb](https://github.com/Mariusthvdb) for his help getting me up and running with this fork of the original integration.

## Donators

Thank you to the PayPal and Buy Me a Coffee donators.

|  |  |  |  | 
|--------------------|--------------------|----------------------|----------------------|
| FFoXXaNN |  |  |  |
| apf-doit | JochenGr | James Kaiser | dselb |
