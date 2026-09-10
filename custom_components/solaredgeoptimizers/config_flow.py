"""
SolarEdge Optimizers Integration - Config Flow (config_flow.py)

This module implements the Home Assistant configuration flow for setting up and managing
the integration through the UI (Settings > Devices & Services > Add Integration).

Setup Flow (async_step_user):
- Collects site ID, username, and password for SolarEdge Monitoring Portal
- Optional entity ID prefix for custom sensor naming (e.g., "se_" -> sensor.se_power_...)
- Option to include site ID in entity IDs for multi-site installations
- Option to enable/disable SolarEdge One API (defaults to enabled with legacy fallback)
- Validates credentials using dual API (tries One first, then legacy)
- Creates unique config entry per site ID (prevents duplicate entries)

Reauth Flow (async_step_reauth, async_step_reauth_confirm):
- Triggered when authentication fails (HTTP 401)
- Allows updating username/password without removing the integration
- Preserves existing options (entity prefix, site ID inclusion)

Reconfigure Flow (async_step_reconfigure):
- User-initiated credential update from the integration menu (⋮ → Update credentials)
- Menu label from config.initiate_flow.reconfigure (distinct from Configure cog / options flow)
- Same username/password form and validation as reauth; options unchanged

Options Flow (SolarEdgeOptimizersOptionsFlowHandler):
- Accessible via Configure button on the integration card
- Allows changing entity ID prefix, site ID inclusion, and One API toggle
- Persists ``entity_id_prefix`` with the same normalize as sensors (lowercase, spaces → ``_``)
- Triggers integration reload after saving; marks entity-registry rebuild only when
  entity-id-shaping options changed (prefix/site-id inclusion)

Error Handling:
- CannotConnect: Network/timeout errors during credential validation
- InvalidAuth: HTTP 401 authentication failure

Logging:
- INFO when credentials are updated successfully (reauth or Reconfigure); DEBUG for form display
  and validation close; no credentials at INFO.

Cleanup:
- Credential validation: `api.close()` in a finally block after check_login so temporary
  sessions are not left open when the user cancels or validation fails (DEBUG on success).
- Config entry title: `format_config_entry_title()` substitutes `%(siteid)s` with fallback
  when the translation template is malformed.
- async_remove_entry: Uses shared helpers from `__init__.py` to stop coordinator
  work, pop stored state, close both API backends, and then remove registry
  entities/devices.
"""
from __future__ import annotations

import logging
from typing import Any

import requests
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.translation import async_get_translations
from homeassistant.util import dt as dt_util

from .const import CONF_SITE_ID, CONF_USE_SOLAREDGE_ONE, DOMAIN, format_config_entry_title
from . import (
    async_close_coordinator_api,
    async_shutdown_coordinator,
    remove_entities_and_devices_for_entry,
)

_LOGGER = logging.getLogger(__name__)


def _normalized_prefix(value: str | None) -> str:
    """Normalize entity_id_prefix consistently with sensor setup."""
    return (value or "").strip().lower().replace(" ", "_")


def _entity_id_shape_changed(entry: ConfigEntry, options_data: dict[str, Any]) -> bool:
    """Return True when options change entity-id shaping (prefix/site-id inclusion)."""
    old_prefix_raw = entry.options.get("entity_id_prefix", entry.data.get("entity_id_prefix", ""))
    old_include = bool(
        entry.options.get("include_site_id_in_entity_id", entry.data.get("include_site_id_in_entity_id", False))
    )
    new_prefix = _normalized_prefix(options_data.get("entity_id_prefix", ""))
    new_include = bool(options_data.get("include_site_id_in_entity_id", False))
    return _normalized_prefix(old_prefix_raw) != new_prefix or old_include != new_include


STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required("siteid"): str,
        vol.Required("username"): str,
        vol.Required("password"): str,
        vol.Optional("entity_id_prefix", default=""): str,
        vol.Optional("include_site_id_in_entity_id", default=False): bool,
        vol.Optional(CONF_USE_SOLAREDGE_ONE, default=True): bool,
    }
)


def _options_schema(entry: ConfigEntry) -> vol.Schema:
    """Build options schema: entity_id_prefix, include_site_id, use_solaredge_one. Defaults from options then data."""
    data = entry.data
    options = entry.options
    return vol.Schema(
        {
            vol.Optional("entity_id_prefix", default=options.get("entity_id_prefix", data.get("entity_id_prefix", ""))): str,
            vol.Optional(
                "include_site_id_in_entity_id",
                default=options.get("include_site_id_in_entity_id", data.get("include_site_id_in_entity_id", False)),
            ): bool,
            vol.Optional(
                CONF_USE_SOLAREDGE_ONE,
                default=options.get(CONF_USE_SOLAREDGE_ONE, data.get(CONF_USE_SOLAREDGE_ONE, True)),
            ): bool,
        }
    )


def _credentials_schema(entry: ConfigEntry) -> vol.Schema:
    """Build username/password schema with current username as default."""
    return vol.Schema(
        {
            vol.Required("username", default=entry.data.get("username", "")): str,
            vol.Required("password"): str,
        }
    )


async def _async_get_title_template(hass: HomeAssistant) -> str:
    """Resolve config entry title template in the user's language."""
    translations = await async_get_translations(
        hass, hass.config.language, "config", [DOMAIN]
    )
    full_key = f"component.{DOMAIN}.config.title_entry"
    return translations.get(
        full_key,
        translations.get("config.title_entry", "SolarEdge Site %(siteid)s"),
    )


async def _async_validate_credentials_update(
    hass: HomeAssistant,
    entry: ConfigEntry,
    username: str,
    password: str,
) -> dict[str, str]:
    """Validate new credentials for reauth/reconfigure. Returns form errors (empty on success)."""
    title_template = await _async_get_title_template(hass)
    data = {
        **entry.data,
        "username": username,
        "password": password,
    }
    try:
        await validate_input(hass, data, title_template)
    except InvalidAuth:
        return {"base": "invalid_auth"}
    except CannotConnect:
        return {"base": "cannot_connect"}
    except Exception as e:  # pylint: disable=broad-except
        _LOGGER.exception("Unexpected exception during credential validation: %s", e)
        return {"base": "unknown"}
    return {}


def _log_credentials_updated(entry: ConfigEntry, flow_name: str) -> None:
    """Log INFO when credentials were validated and the config entry will reload."""
    _LOGGER.info(
        "SolarEdge Optimizers config flow: Credentials updated via %s for entry %s (site %s); reloading",
        flow_name,
        entry.entry_id,
        (entry.data.get("siteid") or entry.data.get(CONF_SITE_ID) or "?"),
    )


async def validate_input(
    hass: HomeAssistant, data: dict[str, Any], translated_title: str
) -> dict[str, Any]:
    """Validate the user input allows us to connect.

    Data has the keys from STEP_USER_DATA_SCHEMA with values provided by the user.
    Raises InvalidAuth for 401, CannotConnect for connection/timeout.
    Uses dual API (One preferred, legacy fallback) so either portal can authenticate.
    """
    siteid = (data.get("siteid") or "").strip()
    username = data.get("username") or ""
    password = data.get("password") or ""
    use_solaredge_one = data.get(CONF_USE_SOLAREDGE_ONE, True)
    ha_timezone = dt_util.get_time_zone(hass.config.time_zone)

    from .api_dual import SolarEdgeDualAPI
    api = SolarEdgeDualAPI(
        siteid=siteid,
        username=username,
        password=password,
        timezone=ha_timezone,
        language=hass.config.language,
        use_solaredge_one=use_solaredge_one,
    )
    if _LOGGER.isEnabledFor(logging.DEBUG):
        _LOGGER.debug("SolarEdge Optimizers config: Validating dual API for site %s", siteid)
    try:
        code = await hass.async_add_executor_job(api.check_login)
    except requests.exceptions.Timeout as e:
        _LOGGER.warning("Timeout during login check: %s", e)
        raise CannotConnect from e
    except requests.exceptions.ConnectionError as e:
        _LOGGER.warning("Connection error during login check: %s", e)
        raise CannotConnect from e
    except requests.exceptions.HTTPError as e:
        _LOGGER.warning("HTTP error during login check: %s", e)
        raise CannotConnect from e
    except requests.exceptions.RequestException as e:
        _LOGGER.warning("Request error during login check: %s", e)
        raise CannotConnect from e
    except (ValueError, KeyError, TypeError) as e:
        _LOGGER.warning("Data parsing error during login check: %s", e)
        raise CannotConnect from e
    finally:
        # Always close API to release any sessions/connections opened during check_login
        try:
            await hass.async_add_executor_job(api.close)
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("SolarEdge Optimizers config: Closed API after validation (site %s)", siteid)
        except Exception as e:  # pylint: disable=broad-except
            _LOGGER.warning("SolarEdge Optimizers config: Error closing API after validation: %s", e)

    if code == 200:
        return {"title": format_config_entry_title(translated_title, siteid)}
    if code == 401:
        _LOGGER.warning(
            "SolarEdge Optimizers config: Authentication failed for site %s "
            "(check email/password at monitoring.solaredge.com; legacy-only: disable Use SolarEdge One)",
            siteid,
        )
        raise InvalidAuth
    if code == 0:
        _LOGGER.warning(
            "SolarEdge Optimizers config: Login check returned no HTTP status for site %s "
            "(often SolarEdge One OAuth did not complete; try legacy-only or verify credentials)",
            siteid,
        )
    raise CannotConnect


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for SolarEdge Optimizers Data."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        if user_input is None:
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("SolarEdge Optimizers config flow: Showing user form")
            return self.async_show_form(
                step_id="user", data_schema=STEP_USER_DATA_SCHEMA
            )

        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "SolarEdge Optimizers config flow: Validating input for siteid=%s",
                user_input.get("siteid", "MISSING"),
            )
        errors = {}

        # One config entry per site globally: set unique_id and abort if this site is already configured
        site_id = (user_input.get("siteid") or "").strip()
        await self.async_set_unique_id(site_id)
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("SolarEdge Optimizers config flow: Checking unique_id (site_id) %s", site_id)
        self._abort_if_unique_id_configured()

        # Resolve config entry title in the user's language
        # async_get_translations(hass, language, category, integrations)
        # integrations must be an iterable (e.g. [DOMAIN]), not a string, or
        # set("solaredgeoptimizers") becomes single-letter "integrations"
        translations = await async_get_translations(
            self.hass, self.hass.config.language, "config", [DOMAIN]
        )
        # HA returns keys as component.<domain>.<category>.<key> for single integration
        full_key = f"component.{DOMAIN}.config.title_entry"
        title_template = translations.get(
            full_key, translations.get("config.title_entry", "SolarEdge Site %(siteid)s")
        )

        try:
            info = await validate_input(self.hass, user_input, title_template)
        except CannotConnect:
            errors["base"] = "cannot_connect"
        except InvalidAuth:
            errors["base"] = "invalid_auth"
        except Exception as e:  # pylint: disable=broad-except
            _LOGGER.exception("Unexpected exception: %s", e)
            errors["base"] = "unknown"
        else:
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "SolarEdge Optimizers config flow: Creating entry title=%s",
                    info["title"],
                )
            entry_data = dict(user_input)
            entry_data["entity_id_prefix"] = _normalized_prefix(entry_data.get("entity_id_prefix"))
            return self.async_create_entry(title=info["title"], data=entry_data)

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_reauth(self, _entry_data: dict[str, Any]) -> FlowResult:
        """Perform reauth when authentication has failed (e.g. 401)."""
        entry = self._get_reauth_entry()
        if entry is not None:
            # Tie this flow to the entry so we update the correct config entry
            await self.async_set_unique_id(entry.unique_id or entry.data.get(CONF_SITE_ID, ""))
            self._abort_if_unique_id_mismatch()
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("SolarEdge Optimizers config flow: Starting reauth for entry")
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show reauth form and update entry on success."""
        entry = self._get_reauth_entry()
        if entry is None:
            return self.async_abort(reason="reauth_entry_missing")

        if user_input is None:
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("SolarEdge Optimizers config flow: Showing reauth form")
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=_credentials_schema(entry),
                description_placeholders={"title": entry.title},
            )

        errors = await _async_validate_credentials_update(
            self.hass,
            entry,
            user_input["username"],
            user_input["password"],
        )
        if errors:
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=_credentials_schema(entry),
                errors=errors,
                description_placeholders={"title": entry.title},
            )

        # Only update credentials in entry.data; options (entity_id_prefix, include_site_id_in_entity_id) are unchanged
        _log_credentials_updated(entry, "reauth")
        return self.async_update_reload_and_abort(
            entry,
            data_updates={
                "username": user_input["username"],
                "password": user_input["password"],
            },
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Allow updating username/password without removing the integration."""
        entry = self._get_reconfigure_entry()
        if entry is None:
            return self.async_abort(reason="reconfigure_entry_missing")

        if user_input is None:
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "SolarEdge Optimizers config flow: Showing reconfigure credentials form for entry %s",
                    entry.entry_id,
                )
            return self.async_show_form(
                step_id="reconfigure",
                data_schema=_credentials_schema(entry),
                description_placeholders={"title": entry.title},
            )

        errors = await _async_validate_credentials_update(
            self.hass,
            entry,
            user_input["username"],
            user_input["password"],
        )
        if errors:
            return self.async_show_form(
                step_id="reconfigure",
                data_schema=_credentials_schema(entry),
                errors=errors,
                description_placeholders={"title": entry.title},
            )

        _log_credentials_updated(entry, "update credentials")
        return self.async_update_reload_and_abort(
            entry,
            data_updates={
                "username": user_input["username"],
                "password": user_input["password"],
            },
        )

    async def async_remove_entry(
        self, hass: HomeAssistant, entry: ConfigEntry
    ) -> None:
        """Shut down polling, close API resources, then remove entities and devices."""
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "SolarEdge Optimizers: async_remove_entry for entry %s",
                entry.entry_id,
            )
        coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        await async_shutdown_coordinator(
            coordinator, f"removal of entry {entry.entry_id}"
        )
        stored_coordinator = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        await async_close_coordinator_api(
            hass,
            stored_coordinator or coordinator,
            f"removal of entry {entry.entry_id}",
        )
        remove_entities_and_devices_for_entry(hass, entry)

    @staticmethod
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> "SolarEdgeOptimizersOptionsFlowHandler":
        """Return the options flow handler for this entry."""
        return SolarEdgeOptimizersOptionsFlowHandler(config_entry)


class SolarEdgeOptimizersOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle SolarEdge Optimizers options (reconfigure entity ID prefix and Include Site ID)."""

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self._entry = entry

    def async_show_form(  # pylint: disable=too-many-arguments
        self, *, step_id=None, data_schema=None, errors=None, description_placeholders=None, last_step=None, preview=None
    ):
        """Show form and ensure frontend uses this integration's translations (options.step.init.data.*)."""
        result = super().async_show_form(
            step_id=step_id,
            data_schema=data_schema,
            errors=errors,
            description_placeholders=description_placeholders,
            last_step=last_step,
            preview=preview,
        )
        result["translation_domain"] = DOMAIN
        return result

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options (reconfigure) step."""
        if user_input is not None:
            options_data = {
                "entity_id_prefix": _normalized_prefix(user_input.get("entity_id_prefix")),
                "include_site_id_in_entity_id": bool(user_input.get("include_site_id_in_entity_id", False)),
                CONF_USE_SOLAREDGE_ONE: bool(user_input.get(CONF_USE_SOLAREDGE_ONE, True)),
            }
            should_rebuild_entities = _entity_id_shape_changed(self._entry, options_data)
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "SolarEdge Optimizers options flow: Saving options for entry %s "
                    "(entity_id_prefix=%r, include_site_id_in_entity_id=%s, use_solaredge_one=%s, rebuild_entities=%s)",
                    self._entry.entry_id,
                    options_data["entity_id_prefix"],
                    options_data["include_site_id_in_entity_id"],
                    options_data[CONF_USE_SOLAREDGE_ONE],
                    should_rebuild_entities,
                )
            # Update options; options override data when reading in sensor/coordinator
            result = self.async_create_entry(title="", data=options_data)
            # Reload after pending work (e.g. config entry save) so setup sees new options and entity_id prefix updates
            entry_id = self._entry.entry_id
            if should_rebuild_entities:
                rebuild_set = self.hass.data.setdefault(DOMAIN, {}).setdefault("_rebuild_entity_registry", set())
                rebuild_set.add(entry_id)

            async def _reload_after_save() -> None:
                await self.hass.async_block_till_done()
                await self.hass.config_entries.async_reload(entry_id)

            self.hass.async_create_task(_reload_after_save())
            return result

        entry = self._entry
        current_prefix = entry.options.get("entity_id_prefix", entry.data.get("entity_id_prefix", "")) or "(none)"
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "SolarEdge Optimizers options flow: Showing options form for entry %s (current prefix=%r, include_site_id=%s, use_solaredge_one=%s)",
                self._entry.entry_id,
                current_prefix,
                entry.options.get("include_site_id_in_entity_id", entry.data.get("include_site_id_in_entity_id")),
                entry.options.get(CONF_USE_SOLAREDGE_ONE, entry.data.get(CONF_USE_SOLAREDGE_ONE, True)),
            )
        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(entry),
            description_placeholders={
                "title": entry.title,
                "current_entity_id_prefix": current_prefix,
            },
        )


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""
