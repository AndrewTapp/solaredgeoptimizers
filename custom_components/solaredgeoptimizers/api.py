"""
SolarEdge Optimizers Integration - API Protocol (api.py)

This module defines the typing Protocol that specifies the interface contract for
SolarEdge API clients. Both the legacy Monitoring API (solaredgeoptimizers) and
the SolarEdge One API (solaredge_one) implement this interface.

Required Methods:
- check_login() -> int: Validate credentials, return HTTP status (200=success, 401=auth failed)
- requestListOfAllPanels() -> Any: Return site structure with inverters/strings/optimizers
- requestAllData() -> list[Any]: Fetch live data for all optimizers
- get_lifetime_energy_cached() -> dict[str, Any]: Return cached lifetime energy data
- close() -> None: Release resources and close sessions

Optional Methods (detected via getattr in coordinator):
- requestSystemDataBatch(item_ids: list) -> list[Any]: Batch query for multiple optimizers
- get_inverter_models(serials: list) -> dict[str, str]: Fetch inverter model names

This Protocol enables type checking and IDE support while allowing the coordinator
to work with either API implementation or the dual API wrapper interchangeably.
"""
from __future__ import annotations

from typing import Any, Protocol


class SolarEdgeAPIProtocol(Protocol):
    """Protocol for SolarEdge optimizer API clients used by the coordinator.

    Both legacy (solaredgeoptimizers) and SolarEdge One (solaredge_one) implement
    this interface. Optional methods (requestSystemDataBatch, get_inverter_models)
    are detected via getattr in the coordinator.
    """

    def check_login(self) -> int:
        """Return HTTP status code (200 = success, 401 = auth failed)."""
        ...

    def requestListOfAllPanels(self) -> Any:
        """Return site structure (e.g. SolarEdgeSite) with inverters/strings/optimizers."""
        ...

    def requestAllData(self) -> list[Any]:
        """Fetch live data for all optimizers; return list of optimizer data objects."""
        ...

    def get_lifetime_energy_cached(self) -> dict[str, Any]:
        """Return cached lifetime energy data (dict keyed by optimizer/string id)."""
        ...

    def close(self) -> None:
        """Close sessions and release resources."""
        ...
