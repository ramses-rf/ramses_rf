"""Reproduction test for ramses_cc issue 1414.

OpenTherm Bridge ``rel_modulation_level`` read at ~half the true value
except for periodic (≈5 min) spikes to the correct value, when compared
against an OTGW reference.

Root cause: OTBs (``10:``) report the OpenTherm Relative Modulation Level
in the ``3EF0``/``3EF1`` modulation byte in 1% steps (0x00-0x64), whereas
BDRs (``13:``) use the evohome demand scale (0x00-0xC8).  The payload
decoders assume the 0-200 scale for all senders, so every ``3EF0``
broadcast writes exactly half the true fraction.  Controller-driven
``3220|0x11`` polls (~5 min cadence) then overwrite it with the correct
value — the observed sawtooth.  ``max_rel_modulation`` (9-byte ``3EF0``)
is affected identically.

See: https://github.com/ramses-rf/ramses_cc/issues/1414
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from ramses_tx.typing import DeviceIdT

from .helpers import TEST_DIR, load_test_gwy

OTB_MOD_DIR = Path(f"{TEST_DIR}/systems/_heat_otb_relmod_issue_1414")


@pytest.mark.asyncio
async def test_otb_3ef0_rel_mod_uses_percent_scale() -> None:
    """An OTB's 3EF0/3EF1 modulation byte (1% steps) must match 3220|0x11.

    The final packet is a 3EF0 broadcast carrying the same 46% as the
    preceding 3220|0x11 Read-Ack: both writers must agree, i.e. there is
    no sawtooth between the broadcast and polled values.
    """
    gwy = await load_test_gwy(OTB_MOD_DIR)
    try:
        otb = gwy.device_registry.device_by_id.get(
            cast(DeviceIdT, "10:048456")
        )
        assert otb is not None, "OTB not loaded"
        state = otb.opentherm_state
        assert state is not None, "no OpenTherm state"

        # last write is a 3EF0 broadcast with byte 0x2E (46%); the 3220|11
        # Read-Ack before it also decoded 0.46 — the values must agree
        assert state.rel_modulation_level == pytest.approx(0.46), (
            f"OTB rel_modulation_level halved: {state.rel_modulation_level!r}"
        )
        # 9-byte 3EF0 max_rel_modulation byte 0x64 is 100%, not 50%
        assert state.max_rel_modulation == pytest.approx(1.0), (
            f"OTB max_rel_modulation halved: {state.max_rel_modulation!r}"
        )
    finally:
        await gwy.stop()


@pytest.mark.asyncio
async def test_bdr_3ef0_modulation_keeps_demand_scale() -> None:
    """A BDR's 3EF0 modulation byte stays on the 0-200 demand scale."""
    gwy = await load_test_gwy(OTB_MOD_DIR)
    try:
        bdr = gwy.device_registry.device_by_id.get(
            cast(DeviceIdT, "13:164839")
        )
        assert bdr is not None, "BDR not loaded"
        state = await bdr.actuator_state()
        assert state is not None, "no actuator state"
        assert state.modulation_level == pytest.approx(1.0), (
            f"BDR modulation_level rescaled: {state.modulation_level!r}"
        )
    finally:
        await gwy.stop()
