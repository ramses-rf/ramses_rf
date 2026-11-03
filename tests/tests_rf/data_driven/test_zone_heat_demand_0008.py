"""Regression test for ramses_cc issue 1395 (zone heat demand).

A zone whose sensor is the controller itself (no bound TRV) never
receives ``3150|idx`` broadcasts — ``zone.demand_state.heat_demand``
stays ``None`` and the ramses_cc ``sensor.*_heat_demand`` shows
``unknown`` forever.

The controller does broadcast the zone's relay demand (``0008|idx``),
so ``Zone.heat_demand()`` falls back to ``demand_state.relay_demand``
when no ``3150``-sourced demand exists.  A real ``3150|idx`` packet
still takes precedence, and a zone with neither signal still returns
``None``.

Fixtures: ``_issue_1395_zone_0008`` sends only ``0008|00`` relay
demands; ``_issue_1395_zone_3150`` additionally sends a ``3150|00``
from the zone's TRV.  Zone ``04`` receives no demand packets at all.

See: https://github.com/ramses-rf/ramses_cc/issues/1395
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .helpers import TEST_DIR, load_test_gwy

ZONE_0008_DIR = Path(f"{TEST_DIR}/systems/_issue_1395_zone_0008")
ZONE_3150_DIR = Path(f"{TEST_DIR}/systems/_issue_1395_zone_3150")


@pytest.mark.asyncio
async def test_zone_heat_demand_falls_back_to_relay_demand() -> None:
    """``0008|00`` must hydrate ``zone.heat_demand`` when no ``3150|00``
    was broadcast; an untouched zone must stay ``None``."""
    gwy = await load_test_gwy(ZONE_0008_DIR)
    try:
        tcs = gwy.tcs
        assert tcs is not None, "no TCS loaded"

        zone_00 = tcs.zone_by_index.get("00")
        zone_04 = tcs.zone_by_index.get("04")
        assert zone_00 is not None and zone_04 is not None

        # last 0008|00 is 0x3E/200 = 0.31 relay demand
        assert zone_00.demand_state.heat_demand is None
        assert zone_00.demand_state.relay_demand == 0.31
        assert await zone_00.heat_demand() == 0.31

        # no 3150 and no 0008 for zone 04 -> still unknown
        assert await zone_04.heat_demand() is None
    finally:
        await gwy.stop()


@pytest.mark.asyncio
async def test_zone_heat_demand_prefers_3150_over_relay() -> None:
    """A ``3150|00`` broadcast takes precedence over the relay
    fallback."""
    gwy = await load_test_gwy(ZONE_3150_DIR)
    try:
        tcs = gwy.tcs
        assert tcs is not None, "no TCS loaded"
        zone_00 = tcs.zone_by_index.get("00")
        assert zone_00 is not None

        # relay demand 0.31 from 0008|00 is overridden by 3150|00 0x96
        assert zone_00.demand_state.heat_demand == 0.75
        assert await zone_00.heat_demand() == 0.75
    finally:
        await gwy.stop()
