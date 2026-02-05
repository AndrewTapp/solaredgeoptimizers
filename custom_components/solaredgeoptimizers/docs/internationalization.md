# Internationalization (i18n) compliance

This document describes how the SolarEdge Optimizers integration supports multiple languages and what is translated.

## What is translated

### Config flow (add-integration)

- **Form labels**: Site id, Username, Password — from `config.step.user.data.*` in each `translations/<code>.json`.
- **Errors**: "Failed to connect", "Invalid authentication", "Unexpected error" — from `config.error.*`.
- **Abort**: "Device is already configured" — from `config.abort.already_configured`.
- **Config entry title**: The name of the integration instance in Devices & services (e.g. "SolarEdge Site 12345") — from `config.title_entry` (supports `%(siteid)s`).
- **Integration title**: The name shown when adding the integration — from `config.title`.

### Entities and devices

- **Sensor entity names**: Power, Voltage, Current, Last measurement, Lifetime energy, Optimizer voltage, Current (average), Voltage (average), Optimizer count, String count, Inverter count, Last polled — from `entity.sensor.<translation_key>.name`.
- **Device names**: Site, Inverter, String, Optimizer device names use `device.site_device`, `device.inverter_device`, `device.string_device`, `device.optimizer_device` with placeholders `{site_id}` or `{display_name}` so labels (e.g. "Site", "Wechselrichter") are translated; the ID/display name comes from the API.

### API requests

- **Locale**: The SolarEdge API client uses the Home Assistant language (e.g. `en`, `de`) to set the `locale` and `accept-language` request parameters where applicable, so portal responses can follow the user’s language when the API supports it.

## What is not translated (by design)

- **Log messages**: All log text is in English for consistency and debugging.
- **Manufacturer/model**: "SolarEdge", "SITE", "STRING" and similar technical identifiers are left in English.
- **Optimizer/string/inverter display names**: The suffix (e.g. "1.1.1") comes from the SolarEdge API and is not translated.

## Supported languages

See the [Translations](../../README.md#translations) section in the main README for the full list of language codes (en, nl, de, fr, es, it, pl, pt, sv, cs, tr, el, hu, ru, zh, ja, da, nb, fi).

## Adding a new language

1. Copy `translations/en.json` to `translations/<code>.json` (e.g. `cs.json` for Czech).
2. Translate all string values. Keep the same JSON structure and keys.
3. Ensure every key present in `en.json` exists in the new file (config, entity, and device sections).
4. Run the project’s checks (e.g. Hassfest) to validate translation files.

## Validation

- Use [Hassfest](https://developers.home-assistant.io/blog/2020/04/16/hassfest) to validate translation files.
- Test the config flow and entity names in the target language in the Home Assistant UI.
