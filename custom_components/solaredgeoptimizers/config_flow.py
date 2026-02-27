"""Configuration flow for SolarEdge Optimizers Home Assistant integration."""
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

from .const import CONF_SITE_ID, CONF_USE_SOLAREDGE_ONE, DOMAIN
from . import remove_entities_and_devices_for_entry

_LOGGER = logging.getLogger(__name__)

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


def _reauth_schema(entry: ConfigEntry) -> vol.Schema:
    """Build reauth schema with current username as default."""
    return vol.Schema(
        {
            vol.Required("username", default=entry.data.get("username", "")): str,
            vol.Required("password"): str,
        }
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

    from .api_dual import SolarEdgeDualAPI
    api = SolarEdgeDualAPI(siteid=siteid, username=username, password=password)
    if _LOGGER.isEnabledFor(logging.DEBUG):
        _LOGGER.debug("SolarEdge Optimizers config: Validating dual API for site %s", siteid)
    try:
        code = await hass.async_add_executor_job(api.check_login)
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
        _LOGGER.warning("Connection or timeout during login check: %s", e)
        raise CannotConnect from e
    except Exception as e:  # pylint: disable=broad-except
        _LOGGER.exception("Login check failed: %s", e)
        raise CannotConnect from e

    if code == 200:
        return {"title": translated_title % {"siteid": siteid}}
    raise InvalidAuth


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
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Unexpected exception")
            errors["base"] = "unknown"
        else:
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "SolarEdge Optimizers config flow: Creating entry title=%s",
                    info["title"],
                )
            entry_data = dict(user_input)
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("SolarEdge Optimizers config flow: Creating entry title=%s", info["title"])
            return self.async_create_entry(title=info["title"], data=entry_data)

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
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
                data_schema=_reauth_schema(entry),
                description_placeholders={"title": entry.title},
            )

        errors: dict[str, str] = {}
        # Validate new credentials (reuse same validation as user step)
        translations = await async_get_translations(
            self.hass, self.hass.config.language, "config", [DOMAIN]
        )
        full_key = f"component.{DOMAIN}.config.title_entry"
        title_template = translations.get(
            full_key,
            translations.get("config.title_entry", "SolarEdge Site %(siteid)s"),
        )
        data = {
            **entry.data,
            "username": user_input["username"],
            "password": user_input["password"],
        }
        try:
            await validate_input(self.hass, data, title_template)
        except InvalidAuth:
            errors["base"] = "invalid_auth"
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Unexpected exception during reauth")
            errors["base"] = "unknown"

        if errors:
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=_reauth_schema(entry),
                errors=errors,
                description_placeholders={"title": entry.title},
            )

        # Only update credentials in entry.data; options (entity_id_prefix, include_site_id_in_entity_id) are unchanged
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("SolarEdge Optimizers config flow: Reauth successful, updating entry")
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
        """Clean up device and entity registry when the integration is removed."""
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "SolarEdge Optimizers: async_remove_entry for entry %s",
                entry.entry_id,
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

    def async_show_form(self, *, step_id=None, data_schema=None, errors=None, description_placeholders=None, last_step=None, preview=None):
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
                "entity_id_prefix": (user_input.get("entity_id_prefix") or "").strip(),
                "include_site_id_in_entity_id": bool(user_input.get("include_site_id_in_entity_id", False)),
                CONF_USE_SOLAREDGE_ONE: bool(user_input.get(CONF_USE_SOLAREDGE_ONE, True)),
            }
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "SolarEdge Optimizers options flow: Saving options for entry %s (entity_id_prefix=%r, include_site_id_in_entity_id=%s, use_solaredge_one=%s)",
                    self._entry.entry_id,
                    options_data["entity_id_prefix"],
                    options_data["include_site_id_in_entity_id"],
                    options_data[CONF_USE_SOLAREDGE_ONE],
                )
            # Update options; options override data when reading in sensor/coordinator
            result = self.async_create_entry(title="", data=options_data)
            # Reload after pending work (e.g. config entry save) so setup sees new options and entity_id prefix updates
            entry_id = self._entry.entry_id
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
