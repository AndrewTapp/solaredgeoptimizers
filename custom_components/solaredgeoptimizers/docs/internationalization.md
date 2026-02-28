# Internationalization (i18n) compliance

This document describes how the SolarEdge Optimizers integration supports multiple languages and what is translated.

## Translation file structure

Each `translations/<code>.json` file has four top-level sections. All four must be present for the integration to work correctly:

| Section   | Purpose |
|-----------|---------|
| **config** | Add-integration form (labels, errors, abort messages, re-auth step), config entry title, integration title. |
| **options** | Reconfigure (Configure) dialog: title, description, Entity ID prefix, Include Site ID in Entity ID, and Use SolarEdge One labels. |
| **entity** | Sensor entity names (Power, Voltage, Obtained from, etc.) under `entity.sensor.<key>.name`; attribute labels (e.g. Panel type) under `entity.sensor.state_attributes.<key>.name`. |
| **device** | Device names (Site, Inverter, String, Optimizer) with placeholders `{site_id}` or `{display_name}` under `device.<key>.name`. |

The integration sets `translation_domain` to the integration domain so the frontend loads these strings. Config entry titles are resolved at runtime via `async_get_translations` in the user's language.

## What is translated

### Config flow (add-integration)

- **Form labels**: Site id, Username, Password, Entity ID prefix (optional), Include Site ID in Entity ID, Use SolarEdge One — from `config.step.user.data.*` in each `translations/<code>.json`.
- **Errors**: "Failed to connect", "Invalid authentication", "Unexpected error" — from `config.error.*`.
- **Abort**: "Device is already configured" — from `config.abort.already_configured`. Re-auth flow: `config.abort.reauth_successful`, `config.abort.reauth_entry_missing`.
- **Re-authentication step**: When credentials expire, the re-auth form (title, description, username, password) — from `config.step.reauth_confirm`.
- **Config entry title**: The name of the integration instance in Devices & services (e.g. "SolarEdge Site 12345") — from `config.title_entry` (supports `%(siteid)s`).
- **Integration title**: The name shown when adding the integration — from `config.title`.

### Options flow (Reconfigure / Configure dialog)

- **Form labels and description**: The Configure dialog (options flow) uses the **options** section: `options.step.init.title`, `options.step.init.description`, `options.step.init.data.entity_id_prefix`, `options.step.init.data.include_site_id_in_entity_id`, `options.step.init.data.use_solaredge_one`. The description shows the current prefix (`{current_entity_id_prefix}`); leave the Entity ID prefix field empty to remove the prefix. The integration sets `translation_domain` so the frontend loads these strings from the integration’s translation files.

### Entities and devices

- **Sensor entity names**: Power, Voltage, Current, Optimizer voltage, Temperature, Lifetime energy, Last measurement, Last polled, Current (average), Voltage (average), Optimizer count, String count, Inverter count, Obtained from — from `entity.sensor.<translation_key>.name` (e.g. `entity.sensor.power.name`, `entity.sensor.obtained_from.name`). Temperature values are stored in °C (the portal may send °C or °F; the integration normalizes to °C); Home Assistant converts to the user’s preferred unit for display.
- **Sensor attribute labels**: The **Panel type** attribute (shown on optimizer sensors when the API provides a panel type/description) is translated from `entity.sensor.state_attributes.panel_type.name` in each translation file.
- **Device names**: Site, Inverter, String, Optimizer device names use `device.site_device`, `device.inverter_device`, `device.string_device`, `device.optimizer_device` with placeholders `{site_id}` or `{display_name}`. At string and optimizer level, `{display_name}` is the **API display name** (e.g. "1.0", "1.0.1"), so device names and entity IDs stay in sync (e.g. "String 1.0", `sensor.lifetime_energy_1_0`). The hierarchy is Site [site], Inverter [site].[i], String [site].[i].[s], Optimizer [site].[i].[s].[o]; the labels (e.g. "Site", "Wechselrichter") are translated.

### API requests

- **Locale**: The integration passes the Home Assistant language to the API client (both SolarEdge One and legacy backends). The **legacy** API uses it to set `locale` and `Accept-Language` (and cookie `SolarEdge_Locale`) on requests, so portal responses can follow the user's language. When the legacy API returns localized measurement keys (e.g. "Leistung [W]" in German), the integration recognises multiple locale variants and normalises decimal separators so power/current/voltage work in all supported languages. The SolarEdge One API returns structured keys (e.g. `power_W`, `voltage_V`) so parsing is locale-independent; the language is still passed for consistency.

## What is not translated (by design)

- **Log messages**: All log text is in English for consistency and debugging.
- **Manufacturer/model**: "SolarEdge", "SITE", "STRING" and similar technical identifiers are left in English.
- **Optimizer/string/inverter display name suffixes**: The numeric part (e.g. "1.0.1", "1.0") comes from the SolarEdge API and is not translated; it is used for device names and entity IDs at string and optimizer level when it parses.

## Supported languages

Supported language codes: **cs**, **da**, **de**, **el**, **en**, **es**, **fi**, **fr**, **hu**, **it**, **ja**, **nb**, **nl**, **pl**, **pt**, **ru**, **sv**, **tr**, **zh**.

For the full table with language names (e.g. Čeština, Deutsch), see the [Translations](../README.md#translations) section in the main README.

## Adding a new language

1. Copy `translations/en.json` to `translations/<code>.json` (e.g. `cs.json` for Czech).
2. Translate all string values. Keep the same JSON structure and keys.
3. Ensure every key present in `en.json` exists in the new file in all four sections: **config**, **options**, **entity**, and **device**. The **options** section is required for the Reconfigure (Configure) dialog to show translated labels and description.
4. Run the project’s checks (e.g. Hassfest) to validate translation files.

## Validation

- Use [Hassfest](https://developers.home-assistant.io/blog/2020/04/16/hassfest) to validate translation files.
- Test the config flow (add integration, re-auth if applicable), the Configure (options) dialog, and entity/device names in the target language in the Home Assistant UI.
