"""Protocol for SolarEdge API clients (legacy and SolarEdge One)."""
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
