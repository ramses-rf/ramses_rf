"""Reproduction test for ramses_cc issue 843.

Water Heater entity not showing current/target temperature after the
Phase 2.95 CQRS cutover in ramses_rf.

The DhwZone getters (temperature, setpoint, mode, relay_demand,
relay_failsafe) now read from CQRS read-models (``temp_state`` /
``dhw_state`` / ``demand_state``) instead of the legacy SQLite message
store.  The CQRS ingestion engines must route DHW opcodes (1260, 10A0,
1F41, 0008, 0009) to the DhwZone so those read-models are hydrated.

The appliance_control (OTB) also emits 10A0/1260 with different semantics
(CH setpoint / null temp) and must NOT clobber the DHW read-models.

See: https://github.com/ramses-rf/ramses_cc/issues/843
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .helpers import TEST_DIR, load_test_gwy

SIMPLE_DIR = Path(f"{TEST_DIR}/systems/heat_simple")
TRV_DIR = Path(f"{TEST_DIR}/systems/_heat_trv_00")


@pytest.mark.asyncio
async def test_dhw_zone_temperature_hydrated_from_1260() -> None:
    """The DhwZone.temperature must reflect the latest 1260 packet."""
    gwy = await load_test_gwy(SIMPLE_DIR)
    try:
        tcs = gwy.tcs
        assert tcs is not None, "no TCS loaded"
        assert tcs.dhw is not None, "no DHW zone loaded"
        temp = await tcs.dhw.temperature()
        assert temp == 21.03, f"DHW temperature not hydrated: {temp!r}"
    finally:
        await gwy.stop()


@pytest.mark.asyncio
async def test_dhw_zone_setpoint_and_mode_hydrated() -> None:
    """The DhwZone.setpoint (10A0) and mode (1F41) must be hydrated, and
    the OTB's 10A0/1260 (CH setpoint / null temp) must NOT clobber them.
    """
    gwy = await load_test_gwy(TRV_DIR)
    try:
        tcs = gwy.tcs
        assert tcs is not None and tcs.dhw is not None

        # 10A0 from the Controller (01:078710) reports setpoint=50.0
        # The OTB (10:047707) also sends 10A0 with setpoint=40.0 — must be
        # ignored for the DhwZone.
        setpoint = await tcs.dhw.setpoint()
        assert setpoint == 50.0, f"DHW setpoint not hydrated/clobbered: {setpoint!r}"

        # 1260 from the DhwSensor (07:017494) reports temperature=29.27
        # The OTB sends 1260 with temperature=None — must be ignored.
        temp = await tcs.dhw.temperature()
        assert temp == 29.27, f"DHW temperature clobbered by OTB: {temp!r}"

        # 1F41 from the Controller reports mode=follow_schedule, active=False
        mode = await tcs.dhw.mode()
        assert mode is not None, "DHW mode not hydrated"
        assert mode["mode"] == "follow_schedule", f"unexpected mode: {mode!r}"
        assert mode["active"] is False, f"unexpected active: {mode!r}"
    finally:
        await gwy.stop()


@pytest.mark.asyncio
async def test_dhw_zone_relay_demand_and_failsafe_hydrated() -> None:
    """The DhwZone.relay_demand (0008 F9) and relay_failsafe (0009 F9) must
    be hydrated via domain_id routing.

    Pre-existing CQRS cutover bugs fixed alongside issue 843:
    - dispatcher mapped relay_demand → heat_demand (wrong field)
    - parser_0009 key ``failsafe_enabled`` didn't match ``relay_failsafe``
    - ingestion path had no domain_id routing for F9/FA → DhwZone
    """
    gwy = await load_test_gwy(TRV_DIR)
    try:
        tcs = gwy.tcs
        assert tcs is not None and tcs.dhw is not None

        # 0008 F914 from the Controller: relay_demand=0.1 for DHW domain
        relay_demand = await tcs.dhw.relay_demand()
        assert relay_demand == 0.1, f"DHW relay_demand not hydrated: {relay_demand!r}"

        # 0009 array with F9 domain: failsafe_enabled=False
        relay_failsafe = await tcs.dhw.relay_failsafe()
        assert relay_failsafe is not None, (
            f"DHW relay_failsafe not hydrated: {relay_failsafe!r}"
        )
        assert not relay_failsafe, (
            f"DHW relay_failsafe expected False: {relay_failsafe!r}"
        )

        # heat_demand must NOT be wrongly set from relay_demand (old bug)
        heat_demand = await tcs.dhw.heat_demand()
        assert heat_demand is None, (
            f"DHW heat_demand wrongly set from relay_demand: {heat_demand!r}"
        )
    finally:
        await gwy.stop()
