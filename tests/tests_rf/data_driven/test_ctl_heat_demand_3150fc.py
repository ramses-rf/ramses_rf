"""Reproduction test for ramses_cc issue 1395.

Controller (01:) heat_demand sensor/climate attribute reported empty.

The reporter's packet log showed the controller broadcasts the proper
2-byte ``3150|FC`` form (``FCC8``, ``FC8C``), so ingestion must hydrate
``tcs.demand_state.heat_demand`` — the attribute the ramses_cc
controller climate entity and ``sensor.*_heat_demand`` read.

The log also showed the controller only emits ``3150|FC`` when the
demand is (re)computed — there is no broadcast while the demand is
zero — so an empty attribute before the first broadcast is expected,
not an ingestion gap.

See: https://github.com/ramses-rf/ramses_cc/issues/1395
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .helpers import TEST_DIR, load_test_gwy

CTL_3150_DIR = Path(f"{TEST_DIR}/systems/_issue_1395_ctl_3150")


@pytest.mark.asyncio
async def test_tcs_heat_demand_hydrated_from_ctl_3150_fc() -> None:
    """A ``3150|FC`` broadcast from the controller must populate the
    TCS's aggregate heat demand (and the CTL device's own)."""
    gwy = await load_test_gwy(CTL_3150_DIR)
    try:
        tcs = gwy.tcs
        assert tcs is not None, "no TCS loaded"
        ctl = gwy.device_registry.device_by_id.get("01:115583")
        assert ctl is not None, "no CTL device loaded"

        # last packet in the log is FC8C (140/200 = 0.7)
        assert tcs.demand_state.heat_demand == 0.7, (
            f"TCS heat demand not hydrated from 3150|FC: "
            f"{tcs.demand_state.heat_demand!r}"
        )
        assert ctl.demand_state.heat_demand == 0.7, (
            f"CTL heat demand not hydrated from 3150|FC: "
            f"{ctl.demand_state.heat_demand!r}"
        )
        assert "FC" in tcs._heat_demands, (
            "per-domain demand dict missing FC entry"
        )
    finally:
        await gwy.stop()
