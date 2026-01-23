# SolarEdge Optimizers Integration

<a href="https://github.com/AndrewTapp/solaredgeoptimizers/releases"><img src="https://img.shields.io/github/release/AndrewTapp/solaredgeoptimizers.svg"></a>
[![Donate](https://img.shields.io/badge/Donate-PayPal-green.svg)](https://www.paypal.me/AndrewJTapp)
[![Donate](https://img.shields.io/badge/Donate-BuyMeACoffee-green.svg)](https://buymeacoffee.com/andrewtapp)

Integration to get optimizer information from the SolarEdge monitoring portal.

This integration works by gathering information from the SolarEdge portal website. Current data per optimizer is gathered and shown in Home Assistant. The total lifetime energy produced per optimizer is also added as a sensor.

For this integration to work, you need to provide:
- Your **Site ID**
- Your **Username** 
- Your **Password**

## Available Sensors

For each optimizer, the following sensors are created:

- **Voltage** - Panel voltage (V)
- **Current** - Panel current (A)  
- **Optimizer voltage** - Optimizer output voltage (V)
- **Power** - Current power output (W)
- **Lifetime energy** - Total cumulative energy produced (kWh) - always updates regardless of measurement age
- **Last measurement** - Timestamp of the last measurement - obtained from the SolarEdge portal
- **Last polled** - When the Home Assistant integration last queried the optimizer

Sensor names are displayed in a user-friendly format (e.g., "Current 1.1.1", "Last measurement 1.1.1").

## Update Behavior

This integration updates its sensors every 15 minutes. More frequent updates are not useful because the SolarEdge portal only updates data every 15 minutes. See https://www.solaredge.com/uk/support/system-owner/mysolaredge-app-does-not-display-production-data and https://www.solaredge.com/us/support/system-owner/app-does-not-display-production-data

**Important:** When an optimizer is offline or not reporting:
- If the last measurement is **older than 1 hour**, non-cumulative sensors (Voltage, Current, Optimizer voltage, Power) will show **0**
- **Lifetime energy** and **Last measurement** sensors will always show their actual values, regardless of measurement age
- This ensures you can still see historical cumulative data even when the optimizer is temporarily offline

## Error Handling

The integration includes robust error handling:
- Temporary server errors (HTTP 5xx) from SolarEdge are handled gracefully with clear log messages
- The integration will automatically retry on the next update cycle
- File descriptor leaks have been fixed for long-running stability

## Installation

Until this integration is adopted by Home Assistant Core, HACS is the recommended method to install as a custom repository.

1. Add this repository as a custom repository to HACS:
   - Go to **HACS** → Click the three dots in the upper right corner → Click **Custom repositories**
   - In the repository field, enter: `https://github.com/AndrewTapp/solaredgeoptimizers`
   - For type, select **Integration**
   - Click **Add**

2. Install the integration:
   - Go back to **HACS** → Select the **SolarEdge Optimizers** integration
   - Click the blue **Download** button (bottom right) and install it

3. Restart Home Assistant

4. Configure the integration:
   - In Home Assistant, go to **Settings** → **Devices & services**
   - Click **Add Integration** and search for **SolarEdge Optimizers**
   - Enter your **Site ID**, **Username**, and **Password**

**Note:** The initial setup can take some time, especially if you have many optimizers. Please be patient.
________________________________________________________________________

## Thanks to the following people

[@proudem](https://github.com/proudem)
[@Mariusthvdb](https://github.com/Mariusthvdb)
[@stepsolar](https://github.com/stepsolar)
[@slyoldfox](https://github.com/slyoldfox)

## Donators

Thank you to the PayPal and Buy Me a Coffee donators

|  |  |  |  | 
|--------------------|--------------------|----------------------|----------------------|
| apf-doit | JochenGr | James Kaiser | dselb |
