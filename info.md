# SolarEdge Optimizers Integration

Brings your SolarEdge optimizer data from the SolarEdge monitoring portal into Home Assistant. You see current production, power, and lifetime energy for each optimizer, and combined totals for each string, inverter, and the whole site.

**You need:** Site ID, username, and password (from the SolarEdge portal).

**In Home Assistant** your system appears as a simple hierarchy: **Site** → **Inverters** (e.g. “Inverter 1”) → **Strings** (e.g. “String 1.1”) → **Optimizers** (e.g. “Optimizer 1.1.1”). Each device shows what it's connected via, so the layout is easy to follow.

**Updates:** The integration checks for new data every few minutes and refreshes when the portal has new readings. Lifetime energy is updated from the portal about once per hour.

**When an optimizer is offline:** Live values (voltage, current, power) show 0 if the last measurement is older than one hour. Lifetime energy and last measurement always show the last known values.

## Installation

Install via HACS as a custom repository: add `https://github.com/AndrewTapp/solaredgeoptimizers` (Integration), then install **SolarEdge Optimizers Data**, restart Home Assistant, and add the integration with your Site ID, username, and password. Initial setup can take a while if you have many optimizers.
