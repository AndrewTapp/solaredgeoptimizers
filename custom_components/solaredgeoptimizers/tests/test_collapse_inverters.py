"""Unit tests for collapse_duplicate_inverter_slots (Issues #66 / #80)."""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parents[1]


def _ensure_package() -> None:
    if "solaredgeoptimizers" not in sys.modules:
        pkg = types.ModuleType("solaredgeoptimizers")
        pkg.__path__ = [str(_ROOT)]
        pkg.__file__ = str(_ROOT / "__init__.py")
        sys.modules["solaredgeoptimizers"] = pkg


_ensure_package()

from solaredgeoptimizers.const import (  # noqa: E402
    collapse_duplicate_inverter_slots,
    inverter_position_key,
)


def _inv(display_name, serial, status, optimizer_count=0, *, name=None, parent_type="INVERTER"):
    strings = []
    if optimizer_count:
        opts = [SimpleNamespace() for _ in range(optimizer_count)]
        strings = [SimpleNamespace(optimizers=opts)]
    return SimpleNamespace(
        displayName=display_name,
        name=name if name is not None else f"Inverter {display_name}",
        serialNumber=serial,
        status=status,
        inverterId=serial,
        type=parent_type,
        strings=strings,
    )


class TestCollapseDuplicateInverterSlots(unittest.TestCase):
    def test_noop_single_inverter(self):
        invs = [_inv("1", "AAAA", "ACTIVE", 10)]
        self.assertEqual(collapse_duplicate_inverter_slots(invs), invs)

    def test_distinct_display_orders_kept(self):
        invs = [
            _inv("1", "AAAA", "ACTIVE", 10),
            _inv("2", "BBBB", "ACTIVE", 8),
        ]
        out = collapse_duplicate_inverter_slots(invs)
        self.assertEqual(len(out), 2)
        self.assertEqual([i.serialNumber for i in out], ["AAAA", "BBBB"])

    def test_inactive_empty_dropped_for_active_tree(self):
        """Issue #66 / neo8101 shape: same slot, INACTIVE empty vs ACTIVE with tree."""
        bad = _inv("1", "xxxxxxEE-E3", "INACTIVE", 0)
        good = _inv("1", "xxxxxxD1-50", "ACTIVE", 30)
        out = collapse_duplicate_inverter_slots([bad, good])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].serialNumber, "xxxxxxD1-50")

    def test_active_preferred_when_order_reversed(self):
        good = _inv("1", "GOOD", "ACTIVE", 5)
        bad = _inv("1", "BAD", "INACTIVE", 0)
        out = collapse_duplicate_inverter_slots([good, bad])
        self.assertEqual(out[0].serialNumber, "GOOD")

    def test_both_active_with_optimizers_kept(self):
        """Issue #80: two live peers in one displayOrder slot must not collapse."""
        thin = _inv("1", "THIN", "ACTIVE", 2)
        rich = _inv("1", "RICH", "ACTIVE", 20)
        out = collapse_duplicate_inverter_slots([thin, rich])
        self.assertEqual(len(out), 2)
        self.assertEqual({i.serialNumber for i in out}, {"THIN", "RICH"})

    def test_blank_status_treated_as_active(self):
        blank = _inv("1", "BLANK", "", 12)
        inactive = _inv("1", "OLD", "INACTIVE", 0)
        out = collapse_duplicate_inverter_slots([inactive, blank])
        self.assertEqual(out[0].serialNumber, "BLANK")

    def test_smi_same_display_order_distinct_names_kept(self):
        """Issue #80 struktur shape: SMI names differ, displayOrder both '1'."""
        smi1 = _inv(
            "1", "SMI-A", "ACTIVE", 22,
            name="SMI 1.1", parent_type="SMI",
        )
        smi2 = _inv(
            "1", "SMI-B", "ACTIVE", 8,
            name="SMI 2.1", parent_type="SMI",
        )
        self.assertNotEqual(inverter_position_key(smi1), inverter_position_key(smi2))
        out = collapse_duplicate_inverter_slots([smi1, smi2])
        self.assertEqual(len(out), 2)
        self.assertEqual([i.serialNumber for i in out], ["SMI-A", "SMI-B"])

    def test_smi_same_name_and_display_order_keeps_live_peers(self):
        """Belt-and-suspenders when SMI names also collide but both have trees."""
        smi1 = _inv(
            "1", "SMI-A", "ACTIVE", 22,
            name="SMI 1.1", parent_type="SMI",
        )
        smi2 = _inv(
            "1", "SMI-B", "ACTIVE", 8,
            name="SMI 1.1", parent_type="SMI",
        )
        out = collapse_duplicate_inverter_slots([smi1, smi2])
        self.assertEqual(len(out), 2)
        self.assertEqual({i.serialNumber for i in out}, {"SMI-A", "SMI-B"})


if __name__ == "__main__":
    unittest.main()
