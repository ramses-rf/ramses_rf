"""Tests for BDR re-parenting from hotwater_valve to appliance_control.

Regression tests for issue 834: Evohome schema discovery incorrectly
categorising a BDR as a hot water control.

When the controller's 000C binding table has a BDR in the HTG slot (domain
FA = hotwater_valve), but the BDR is actually the appliance_control (domain
FC), the discovery system must be able to re-parent the BDR from the
DhwZone to the System when a higher-confidence binding (000C APP, or
3B00/3EF0 eavesdrop) arrives.

See: https://github.com/ramses-rf/ramses_cc/issues/834
"""

from __future__ import annotations

import asyncio
import contextlib
import tempfile

import pytest

from ramses_rf import Gateway
from ramses_rf.config import GatewayConfig
from ramses_rf.const import FA, FC
from ramses_rf.devices import BdrSwitch, Controller, DhwSensor, OtbGateway
from ramses_tx.config import EngineConfig

CTL_ID = "01:145038"
BDR_ID = "13:121025"
DHW_SENSOR_ID = "07:046947"


def _make_gateway(
    known_list: dict[str, dict[str, str]] | None = None,
) -> Gateway:
    """Create a minimal Gateway for topology testing."""
    if known_list is None:
        known_list = {
            CTL_ID: {"class": "CTL"},
            BDR_ID: {"class": "BDR"},
        }
    # Use a temp file as input to satisfy the Engine's port_name/file requirement
    with tempfile.NamedTemporaryFile(delete=False, suffix=".log") as tmp:
        tmp_path = tmp.name
    config = GatewayConfig(
        disable_discovery=True,
        known_list=known_list,
        engine=EngineConfig(
            disable_sending=True,
            enforce_known_list=True,
            input_file=tmp_path,
        ),
    )
    return Gateway(None, config=config)


async def _drain_queues(gwy: Gateway) -> None:
    """Yield to the event loop so any pending callbacks fire."""
    for _ in range(50):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_bdr_reparent_from_hotwater_valve_to_appliance_control() -> None:
    """A BDR bound as hotwater_valve (FA) must be re-parented to
    appliance_control (FC) when no DHW sensor exists.

    This is the core issue 834 scenario: the 000C HTG binding (FA) arrives
    first, incorrectly placing the BDR as hotwater_valve.  When the 000C
    APP binding (FC) or eavesdrop arrives later, the BDR must be moved to
    the System as appliance_control.
    """
    gwy = _make_gateway()
    await gwy.start(start_discovery=False)
    await _drain_queues(gwy)

    try:
        # 1. Get the controller and instantiate its TCS
        ctl = gwy.device_registry.get_device(CTL_ID)
        assert isinstance(ctl, Controller)
        ctl._make_tcs_controller()
        tcs = ctl.tcs
        assert tcs is not None

        # 2. Bind the BDR as hotwater_valve (FA) — this creates a DhwZone
        bdr = gwy.device_registry.get_device(BDR_ID, parent=tcs, child_id=FA)
        assert isinstance(bdr, BdrSwitch)
        assert tcs.dhw is not None
        assert tcs.dhw.hotwater_valve is not None
        assert tcs.dhw.hotwater_valve.id == BDR_ID
        assert tcs.appliance_control is None

        # 3. Now bind the same BDR as appliance_control (FC)
        # This should re-parent the BDR from DhwZone to System
        bdr2 = gwy.device_registry.get_device(BDR_ID, parent=tcs, child_id=FC)
        assert bdr2 is bdr

        # 4. Verify the BDR is now appliance_control, NOT hotwater_valve
        app_cntrl: BdrSwitch | OtbGateway | None = tcs.appliance_control
        assert app_cntrl is not None
        assert app_cntrl.id == BDR_ID

        # 5. Verify the DhwZone was cleaned up (no sensor, no valves)
        assert tcs.dhw is None or tcs.dhw.hotwater_valve is None
    finally:
        await gwy.stop()


@pytest.mark.asyncio
async def test_bdr_no_reparent_when_dhw_sensor_exists() -> None:
    """A BDR must NOT be re-parented from hotwater_valve to
    appliance_control when a DHW sensor exists.

    If the system genuinely has DHW (sensor present), the BDR may truly be
    the hotwater_valve, so the re-parenting must be suppressed.
    """
    known_list = {
        CTL_ID: {"class": "CTL"},
        BDR_ID: {"class": "BDR"},
        DHW_SENSOR_ID: {"class": "DHW"},
    }
    gwy = _make_gateway(known_list=known_list)
    await gwy.start(start_discovery=False)
    await _drain_queues(gwy)

    try:
        # 1. Get the controller and instantiate its TCS
        ctl = gwy.device_registry.get_device(CTL_ID)
        assert isinstance(ctl, Controller)
        ctl._make_tcs_controller()
        tcs = ctl.tcs
        assert tcs is not None

        # 2. Bind the DHW sensor first (FA, is_sensor=True)
        sensor = gwy.device_registry.get_device(
            DHW_SENSOR_ID, parent=tcs, child_id=FA, is_sensor=True
        )
        assert isinstance(sensor, DhwSensor)
        assert tcs.dhw is not None
        assert tcs.dhw.sensor is not None

        # 3. Bind the BDR as hotwater_valve (FA)
        bdr = gwy.device_registry.get_device(BDR_ID, parent=tcs, child_id=FA)
        assert isinstance(bdr, BdrSwitch)
        assert tcs.dhw.hotwater_valve is not None
        assert tcs.dhw.hotwater_valve.id == BDR_ID

        # 4. Attempt to bind the same BDR as appliance_control (FC)
        # This should NOT re-parent because a DHW sensor exists
        with contextlib.suppress(Exception):
            gwy.device_registry.get_device(BDR_ID, parent=tcs, child_id=FC)

        # 5. Verify the BDR is still hotwater_valve (not re-parented)
        assert tcs.dhw is not None
        assert tcs.dhw.hotwater_valve is not None
        assert tcs.dhw.hotwater_valve.id == BDR_ID
    finally:
        await gwy.stop()


@pytest.mark.asyncio
async def test_bdr_appliance_control_first_no_dhw_zone() -> None:
    """When the BDR is bound as appliance_control (FC) first, no DhwZone
    should be created.

    This verifies the normal (non-race) case: the 000C APP binding arrives
    before the 000C HTG binding, so the BDR is correctly placed as
    appliance_control and no spurious DhwZone is created.
    """
    gwy = _make_gateway()
    await gwy.start(start_discovery=False)
    await _drain_queues(gwy)

    try:
        ctl = gwy.device_registry.get_device(CTL_ID)
        assert isinstance(ctl, Controller)
        ctl._make_tcs_controller()
        tcs = ctl.tcs
        assert tcs is not None

        # Bind the BDR directly as appliance_control (FC)
        bdr = gwy.device_registry.get_device(BDR_ID, parent=tcs, child_id=FC)
        assert isinstance(bdr, BdrSwitch)
        assert tcs.appliance_control is not None
        assert tcs.appliance_control.id == BDR_ID
        # No DhwZone should exist
        assert tcs.dhw is None
    finally:
        await gwy.stop()
