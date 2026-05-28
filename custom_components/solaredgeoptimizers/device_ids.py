"""
SolarEdge Optimizers Integration - Device registry identifiers (device_ids.py)

Shared helpers for device registry identifier strings used by the coordinator when
registering site/inverter/string devices and by the sensor platform when linking entities.

link_device_info() returns identifiers-only DeviceInfo so Home Assistant matches pre-registered
devices without re-applying via_device during async_add_entities (avoids startup warnings from
v2.4.17 onward). Optimizer devices are registered in the sensor platform before entities are
added; registration uses via_device=(DOMAIN, parent_id) as a tuple (not a set).

Path parsers (inv_str_keys_from_entity_id_path, opt_keys_from_entity_id_path, etc.) respect
include_site_id_in_entity_id so device identifiers stay aligned with entity_id_path tuples,
including suffixed optimizers (e.g. opt key 1a). Large sites rely on batched entity registration
in the sensor platform (v2.4.18+).
"""

from __future__ import annotations

from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN


def site_device_identifier(site_id: str | int) -> str:
    """Return the site device registry identifier (without domain)."""
    return f"site_{site_id}"


def inverter_device_identifier(entry_id: str, inv_key: str | int) -> str:
    """Return the inverter device registry identifier for a config entry."""
    return f"{entry_id}_inv_{inv_key}"


def string_device_identifier(entry_id: str, inv_key: str | int, str_key: str | int) -> str:
    """Return the string device registry identifier for a config entry."""
    return f"{entry_id}_str_{inv_key}_{str_key}"


def optimizer_device_identifier(
    entry_id: str, inv_key: str | int, str_key: str | int, opt_key: str | int
) -> str:
    """Return the optimizer device registry identifier for a config entry."""
    return f"{entry_id}_opt_{inv_key}_{str_key}_{opt_key}"


def inv_str_keys_from_entity_id_path(
    entity_id_path: tuple, *, include_site_id_in_entity_id: bool
) -> tuple[str | int, str | int]:
    """Return (inv_key, str_key) from an entity_id_path for device identifiers."""
    if not entity_id_path:
        return 0, 0
    if include_site_id_in_entity_id:
        if len(entity_id_path) >= 3:
            return entity_id_path[-2], entity_id_path[-1]
        if len(entity_id_path) == 2:
            return entity_id_path[0], entity_id_path[1]
    elif len(entity_id_path) >= 2:
        return entity_id_path[-2], entity_id_path[-1]
    elif len(entity_id_path) == 1:
        return entity_id_path[0], 0
    return 0, 0


def inv_key_from_entity_id_path(
    entity_id_path: tuple, *, include_site_id_in_entity_id: bool
) -> str | int:
    """Return the inverter key from an entity_id_path for device identifiers."""
    if not entity_id_path:
        return 0
    if include_site_id_in_entity_id and len(entity_id_path) >= 2:
        return entity_id_path[-1]
    return entity_id_path[-1] if entity_id_path else 0


def opt_keys_from_entity_id_path(
    entity_id_path: tuple, *, include_site_id_in_entity_id: bool
) -> tuple[str | int, str | int, str | int]:
    """Return (inv_key, str_key, opt_key) from an optimizer entity_id_path."""
    if not entity_id_path:
        return 0, 0, 0
    if include_site_id_in_entity_id:
        if len(entity_id_path) >= 4:
            return entity_id_path[-3], entity_id_path[-2], entity_id_path[-1]
        if len(entity_id_path) == 3:
            return entity_id_path[0], entity_id_path[1], entity_id_path[2]
    elif len(entity_id_path) >= 3:
        return entity_id_path[-3], entity_id_path[-2], entity_id_path[-1]
    return 0, 0, 0


def link_device_info(device_identifier: str) -> DeviceInfo:
    """DeviceInfo that links an entity to a pre-registered device without re-stating via_device.

    Home Assistant classifies this as a link-type device info (identifiers only), so entity
    setup does not call async_get_or_create with via_device and trigger parent-order warnings.
    """
    return DeviceInfo(identifiers={(DOMAIN, device_identifier)})
