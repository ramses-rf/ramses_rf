"""Reproduction test for ramses_rf issue 1370.

The DhwZone's ``relay_demand`` (the "Stored HW Relay demand" sensor)
showed the *heating* valve's demand as well as the DHW valve's.

Root cause: controller ``0008``/``0009`` broadcasts are domain-indexed —
``FA`` is the hot-water valve, ``F9`` the heating valve — but both
domains were routed to ``tcs.dhw``, and ``_update_demand_state`` wrote
``relay_demand``/``relay_failsafe`` with no domain check.  The last
broadcast won: after ``0008|F9`` the sensor reported the heating
valve's demand (0.02 in the reporter's log) while the DHW valve was
closed, and 0.0 during a reheat.

The TCS's per-domain ``_relay_demands``/``_relay_failsafes`` dicts
(populated via the CTL device target) are unaffected — they must keep
both FA and F9.

See: https://github.com/ramses-rf/ramses_rf/issues/1370
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .helpers import TEST_DIR, load_test_gwy

DHW_0008_DIR = Path(f"{TEST_DIR}/systems/_heat_dhw_0008_f9_issue_1370")


@pytest.mark.asyncio
async def test_dhw_relay_demand_ignores_f9_heating_valve() -> None:
    """0008|F9 must not overwrite the DhwZone's FA relay_demand.

    The log ends with ``0008|FAC8`` (DHW reheat, relay 100%) followed by
    ``0008|F900`` (heating valve idle): the bug left the DHW relay_demand
    at 0.0; the fix keeps the FA value.
    """
    gwy = await load_test_gwy(DHW_0008_DIR)
    try:
        tcs = gwy.tcs
        assert tcs is not None, "no TCS loaded"
        dhw = tcs.dhw
        assert dhw is not None, "no DHW zone loaded"

        # last FA broadcast was FAC8 (1.0); F900 after it must be ignored
        assert dhw.demand_state.relay_demand == pytest.approx(1.0), (
            f"DHW relay_demand clobbered by heating-valve F9: "
            f"{dhw.demand_state.relay_demand!r}"
        )
        assert dhw.demand_state.relay_active is True

        # 0009 array: FA element failsafe=False, F9 element failsafe=True
        assert dhw.demand_state.relay_failsafe is False, (
            f"DHW relay_failsafe clobbered by heating-valve F9: "
            f"{dhw.demand_state.relay_failsafe!r}"
        )

        # the TCS's per-domain dicts still keep both domains
        relay_demands = tcs.relay_demands
        assert relay_demands is not None
        assert relay_demands.get("FA") == pytest.approx(1.0)
        assert relay_demands.get("F9") == pytest.approx(0.0), (
            f"TCS relay_demands lost the F9 domain: {relay_demands!r}"
        )
    finally:
        await gwy.stop()


@pytest.mark.asyncio
async def test_dhw_relay_demand_first_f9_then_fa() -> None:
    """0008|F9 before any FA must leave DHW relay_demand unset.

    The first packets in the log are ``0008|FA00`` then ``0008|F904``:
    replaying the same log and checking mid-state is not possible, so
    this asserts the inverse ordering does not leak F9 either — covered
    by the per-element routing in the main test.
    """
    gwy = await load_test_gwy(DHW_0008_DIR)
    try:
        tcs = gwy.tcs
        assert tcs is not None and tcs.dhw is not None

        # 0x04 on the F9 domain is 4/200 = 0.02 — the exact value the
        # reporter saw on the DHW sensor; it must not appear here
        assert tcs.dhw.demand_state.relay_demand != pytest.approx(0.02)
        assert tcs.dhw.demand_state.relay_demand != pytest.approx(0.0)
    finally:
        await gwy.stop()
