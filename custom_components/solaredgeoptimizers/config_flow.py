"""Configuration flow for SolarEdge Optimizers Home Assistant integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.translation import async_get_translations

from .const import CONF_SITE_ID, DOMAIN

# AJT: 10-Jan-2025: Changed from absolute import to relative import to use local solaredgeoptimizers.py instead of site-packages version
from .solaredgeoptimizers import solaredgeoptimizers

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required("siteid"): str,
        vol.Required("username"): str,
        vol.Required("password"): str,
        vol.Optional("entity_id_prefix", default=""): str,
        vol.Optional("include_site_id_in_entity_id", default=False): bool,
    }
)


def _options_schema(entry: ConfigEntry) -> vol.Schema:
    """Build options schema. entity_id_prefix uses default='' so clearing the field saves ''; current value shown in description."""
    data = entry.data
    options = entry.options
    return vol.Schema(
        {
            vol.Optional("entity_id_prefix", default=""): str,
            vol.Optional(
                "include_site_id_in_entity_id",
                default=options.get("include_site_id_in_entity_id", data.get("include_site_id_in_entity_id", False)),
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


class SolarEdgeWebAuth:
    """Handles authentication with SolarEdge API."""

    def __init__(self, siteid: str) -> None:
        """Initialize."""
        self.siteid = siteid

    async def authenticate(
        self, hass: HomeAssistant, username: str, password: str
    ) -> bool:
        """Test to check if siteid, username and password are correct."""
        api = solaredgeoptimizers(
            siteid=self.siteid, username=username, password=password
        )
        # http_result_code = api.check_login()
        http_result_code = await hass.async_add_executor_job(api.check_login)
        if http_result_code == 200:
            return True
        else:
            return False


async def validate_input(
    hass: HomeAssistant, data: dict[str, Any], translated_title: str
) -> dict[str, Any]:
    """Validate the user input allows us to connect.

    Data has the keys from STEP_USER_DATA_SCHEMA with values provided by the user.
    """
    hub = SolarEdgeWebAuth(data["siteid"])

    if not await hub.authenticate(hass, data["username"], data["password"]):
        raise InvalidAuth

    # Return info that you want to store in the config entry (title uses translation).
    # Translations use %(siteid)s placeholder, so use %-formatting not .format()
    return {"title": translated_title % {"siteid": data["siteid"]}}


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
            return self.async_create_entry(title=info["title"], data=user_input)

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
        ent_reg = er.async_get(hass)
        dev_reg = dr.async_get(hass)

        # --- Entities: collect entity_ids to remove (then remove in a second pass)
        entity_ids_to_remove: list[str] = []
        if hasattr(ent_reg.entities, "get_entries_for_config_entry_id"):
            for entity_entry in ent_reg.entities.get_entries_for_config_entry_id(
                entry.entry_id
            ):
                entity_ids_to_remove.append(entity_entry.entity_id)
        # Fallback: iterate registry; match by config_entry_id or by our unique_id prefix
        if not entity_ids_to_remove:
            def _entity_matches(e: Any) -> bool:
                if e is None:
                    return False
                if getattr(e, "config_entry_id", None) == entry.entry_id:
                    return True
                uid = getattr(e, "unique_id", None)
                return uid is not None and str(uid).startswith(entry.entry_id)

            if hasattr(ent_reg.entities, "values"):
                for entity in ent_reg.entities.values():
                    if _entity_matches(entity):
                        entity_ids_to_remove.append(entity.entity_id)
            else:
                # Some HA versions: iterate keys (entity_id) then async_get
                for maybe_key in ent_reg.entities:
                    entity = ent_reg.async_get(maybe_key) if hasattr(ent_reg, "async_get") else None
                    if entity is None and hasattr(ent_reg.entities, "data"):
                        entity = ent_reg.entities.data.get(maybe_key)
                    if _entity_matches(entity):
                        eid = getattr(entity, "entity_id", maybe_key)
                        entity_ids_to_remove.append(eid)
        for eid in entity_ids_to_remove:
            ent_reg.async_remove(eid)
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "SolarEdge Optimizers: Removed entity %s for config entry %s",
                    eid,
                    entry.entry_id,
                )

        # --- Devices: collect device entries to remove (then remove in a second pass)
        devices_to_remove: list[tuple[str, Any]] = []  # (id, name_or_identifiers)
        if hasattr(dev_reg.devices, "get_devices_for_config_entry_id"):
            for dev in dev_reg.devices.get_devices_for_config_entry_id(entry.entry_id):
                devices_to_remove.append((dev.id, dev.name or dev.identifiers))
        if not devices_to_remove and hasattr(dev_reg.devices, "values"):
            for device in dev_reg.devices.values():
                if entry.entry_id in device.config_entries:
                    devices_to_remove.append((device.id, device.name or device.identifiers))
        # Fallback: remove site device by identifier (we have siteid from entry.data)
        if not devices_to_remove:
            siteid = (entry.data.get(CONF_SITE_ID) or "").strip()
            if siteid:
                site_device = dev_reg.async_get_device(
                    identifiers={(DOMAIN, f"site_{siteid}")}
                )
                if site_device:
                    devices_to_remove.append(
                        (site_device.id, site_device.name or site_device.identifiers)
                    )
        for device_id, name_or_ids in devices_to_remove:
            dev_reg.async_remove_device(device_id)
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "SolarEdge Optimizers: Removed device %s (%s) for config entry %s",
                    device_id,
                    name_or_ids,
                    entry.entry_id,
                )

        if not entity_ids_to_remove and not devices_to_remove:
            _LOGGER.warning(
                "SolarEdge Optimizers: async_remove_entry found no entities or devices "
                "to remove for entry %s. Delete the integration from Settings → Devices & "
                "services → Integrations (not only from HACS) while the integration is still "
                "installed so that cleanup can run.",
                entry.entry_id,
            )


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
            }
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "SolarEdge Optimizers options flow: Saving options for entry %s (entity_id_prefix=%r, include_site_id_in_entity_id=%s)",
                    self._entry.entry_id,
                    options_data["entity_id_prefix"],
                    options_data["include_site_id_in_entity_id"],
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

        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "SolarEdge Optimizers options flow: Showing options form for entry %s",
                self._entry.entry_id,
            )
        entry = self._entry
        current_prefix = entry.options.get("entity_id_prefix", entry.data.get("entity_id_prefix", "")) or "(none)"
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
