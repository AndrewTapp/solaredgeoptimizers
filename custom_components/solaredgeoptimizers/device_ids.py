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
including suffixed optimizers (e.g. opt key 1a). string_device_keys_for_registration() and
build_string_device_key_lookup() mirror coordinator string device registration (duplicate and
portal suffixes on str_key) so optimizer via_device links to the correct parent string.
Large sites rely on batched entity registration in the sensor platform (v2.4.18+).
"""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.entity import DeviceInfo

from .const import (
    DOMAIN,
    parse_string_display_name_path,
    resolve_duplicate_indices,
    string_position_key_from_display_name,
)


def site_device_identifier(site_id: str | int) -> str:
    """Return the site device registry identifier (without domain)."""
    return f"site_{site_id}"


def inverter_device_identifier(entry_id: str, inv_key: str | int) -> str:
    """Return the inverter device registry identifier for a config entry."""
    return f"{entry_id}_inv_{inv_key}"


def string_device_identifier(entry_id: str, inv_key: str | int, str_key: str | int) -> str:
    """Return the string device registry identifier for a config entry."""
    return f"{entry_id}_str_{inv_key}_{str_key}"


def string_device_keys_for_registration(
    string,
    *,
    inv_idx: int,
    str_idx: int,
    inv_idx_str: str,
    str_suffix: str = "",
) -> tuple[str | int, str | int]:
    """Return (inv_key, str_key) matching coordinator string device registration."""
    parsed = parse_string_display_name_path(getattr(string, "displayName", "") or "")
    if parsed is not None:
        inv_num, str_num, display_suffix = parsed
        str_num_str = f"{str_num}{display_suffix or str_suffix}"
        return inv_num, str_num_str
    str_idx_str = f"{str_idx}{str_suffix}"
    return inv_idx_str, str_idx_str


def build_string_device_key_lookup(site, logger=None) -> dict[Any, tuple[str | int, str | int]]:
    """Map string.stringId -> (inv_key, str_key) for string device registry identifiers."""
    lookup: dict[Any, tuple[str | int, str | int]] = {}
    inv_suffix_map = resolve_duplicate_indices(
        site.inverters,
        get_key=lambda inv: getattr(inv, "displayName", "") or str(getattr(inv, "inverterId", "")),
        get_status=lambda inv: getattr(inv, "status", "") or "",
        get_serial=lambda inv: getattr(inv, "serialNumber", "") or "",
        logger=logger,
    )
    for inv_idx, inverter in enumerate(site.inverters, start=1):
        inv_suffix = inv_suffix_map.get(inv_idx - 1, "")
        inv_idx_str = f"{inv_idx}{inv_suffix}"
        indexed_strings = list(enumerate(inverter.strings, start=1))
        str_suffix_map = resolve_duplicate_indices(
            indexed_strings,
            get_key=lambda t: string_position_key_from_display_name(
                getattr(t[1], "displayName", "") or "", inv_idx, t[0]
            ),
            get_status=lambda t: getattr(t[1], "status", "") or "",
            get_serial=lambda t: getattr(
                t[1], "serialNumber", ""
            ) or str(getattr(t[1], "stringId", "")),
            logger=logger,
        )
        for list_idx, (str_idx, string) in enumerate(indexed_strings):
            str_suffix = str_suffix_map.get(list_idx, "")
            inv_key, str_key = string_device_keys_for_registration(
                string,
                inv_idx=inv_idx,
                str_idx=str_idx,
                inv_idx_str=inv_idx_str,
                str_suffix=str_suffix,
            )
            string_id = getattr(string, "stringId", None)
            if string_id is not None:
                lookup[string_id] = (inv_key, str_key)
    return lookup


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
