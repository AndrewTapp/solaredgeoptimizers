"""Unit tests for SolarEdge One SMI parent layout parsing (Issue #74)."""
from __future__ import annotations

import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock

_ROOT = Path(__file__).resolve().parents[1]
_FIXTURE = _ROOT / "miscellaneous" / "site_structure_christiangue.json"
_FIXTURE_STRUKTUR = _ROOT / "miscellaneous" / "site_structure_struktur.json"


def _ensure_stubs() -> None:
    """Stub optional third-party deps so tests run without HA / full env."""
    if "pytz" not in sys.modules:
        pytz = types.ModuleType("pytz")
        pytz.UTC = object()
        sys.modules["pytz"] = pytz

    if "requests" not in sys.modules:
        try:
            import requests as _requests_real  # noqa: F401
        except ImportError:
            requests = types.ModuleType("requests")

            class HTTPError(Exception):
                def __init__(self, *args, response=None, **kwargs):
                    super().__init__(*args)
                    self.response = response

            class Session:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                headers = {}

            requests.HTTPError = HTTPError
            requests.RequestException = Exception
            requests.Session = Session
            requests.get = MagicMock()
            requests.post = MagicMock()
            session_mod = types.ModuleType("requests.sessions")
            session_mod.Session = Session
            sys.modules["requests"] = requests
            sys.modules["requests.sessions"] = session_mod
            requests.sessions = session_mod
            requests.auth = types.SimpleNamespace(HTTPBasicAuth=MagicMock)
            requests.exceptions = types.SimpleNamespace(
                ConnectionError=ConnectionError,
                Timeout=TimeoutError,
            )

    if "solaredgeoptimizers" not in sys.modules:
        pkg = types.ModuleType("solaredgeoptimizers")
        pkg.__path__ = [str(_ROOT)]
        pkg.__file__ = str(_ROOT / "__init__.py")
        sys.modules["solaredgeoptimizers"] = pkg


_ensure_stubs()

from solaredgeoptimizers.solaredge_one_api import (  # noqa: E402
    _site_structure_v2_to_solar_edge_site,
    _v2_collect_parent_nodes,
    _v2_normalize_parent_type,
)


class TestSmiLayoutParsing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = json.loads(_FIXTURE.read_text(encoding="utf-8"))

    def test_normalize_parent_type(self):
        self.assertEqual(_v2_normalize_parent_type("SMI"), "SMI")
        self.assertEqual(_v2_normalize_parent_type("inverter"), "INVERTER")
        self.assertEqual(_v2_normalize_parent_type(None), "INVERTER")
        self.assertEqual(_v2_normalize_parent_type("BATTERY"), "INVERTER")

    def test_collect_smi_parents_from_christiangue_fixture(self):
        structure = self.raw["siteStructure"]
        parents = _v2_collect_parent_nodes(structure)
        self.assertEqual(len(parents), 4)
        self.assertTrue(all((p.get("type") or "").upper() == "SMI" for p in parents))
        self.assertEqual(
            [p.get("displayOrder") for p in parents],
            ["1", "2", "3", "4"],
        )

    def test_site_structure_maps_smi_to_parent_slots(self):
        site = _site_structure_v2_to_solar_edge_site("3874876", self.raw)
        self.assertEqual(len(site.inverters), 4)
        self.assertEqual({inv.type for inv in site.inverters}, {"SMI"})
        self.assertEqual([inv.displayName for inv in site.inverters], ["1", "2", "3", "4"])
        opt_total = sum(len(s.optimizers) for inv in site.inverters for s in inv.strings)
        self.assertEqual(opt_total, 77)
        # displayOrder-based optimizer names stay three-segment (1.0.1 …)
        first_opt = site.inverters[0].strings[0].optimizers[0]
        self.assertEqual(first_opt.displayName, "1.0.1")
        second_smi_opt = site.inverters[1].strings[0].optimizers[0]
        self.assertEqual(second_smi_opt.displayName, "2.0.1")

    def test_battery_meter_folders_ignored(self):
        """neo8101-style BATTERY/METER siblings must not become parents."""
        mixed = {
            "siteStructure": {
                "uuid": "1",
                "type": "SITE",
                "name": "mixed",
                "children": [
                    {
                        "type": "FOLDER",
                        "name": "SMI",
                        "children": [
                            {
                                "type": "SMI",
                                "name": "SMI 1",
                                "serial": "SMI-1",
                                "displayOrder": "1",
                                "order": 1,
                                "properties": {"status": "ACTIVE"},
                                "children": [],
                            }
                        ],
                    },
                    {
                        "type": "FOLDER",
                        "name": "BATTERY",
                        "children": [
                            {
                                "type": "BATTERY",
                                "name": "Battery 1.1",
                                "serial": "BAT-1",
                                "displayOrder": "1.1",
                                "properties": {"status": "ACTIVE"},
                                "children": [],
                            }
                        ],
                    },
                    {
                        "type": "FOLDER",
                        "name": "METER",
                        "children": [
                            {
                                "type": "METER",
                                "name": "Import / Export",
                                "serial": "MTR-1",
                                "properties": {"status": "ACTIVE"},
                                "children": [],
                            }
                        ],
                    },
                ],
            }
        }
        site = _site_structure_v2_to_solar_edge_site("1", mixed)
        self.assertEqual(len(site.inverters), 1)
        self.assertEqual(site.inverters[0].type, "SMI")
        self.assertEqual(site.inverters[0].serialNumber, "SMI-1")

    def test_struktur_keeps_both_smis_with_shared_display_order(self):
        """Issue #80: both ACTIVE SMIs reuse displayOrder '1'; collapse must keep both."""
        raw = json.loads(_FIXTURE_STRUKTUR.read_text(encoding="utf-8"))
        parents = _v2_collect_parent_nodes(raw["siteStructure"])
        self.assertEqual(len(parents), 2)
        self.assertEqual([p.get("displayOrder") for p in parents], ["1", "1"])
        self.assertEqual([p.get("name") for p in parents], ["SMI 1.1", "SMI 2.1"])

        site = _site_structure_v2_to_solar_edge_site("struktur", raw)
        self.assertEqual(len(site.inverters), 2)
        self.assertEqual({inv.type for inv in site.inverters}, {"SMI"})
        self.assertEqual([inv.name for inv in site.inverters], ["SMI 1.1", "SMI 2.1"])
        opt_counts = [
            sum(len(s.optimizers) for s in inv.strings) for inv in site.inverters
        ]
        self.assertEqual(opt_counts, [22, 8])


if __name__ == "__main__":
    unittest.main()
