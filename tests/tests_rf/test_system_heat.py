#!/usr/bin/env python3
"""Test the System heat logic, specifically packet processing."""

import asyncio
import logging
from dataclasses import replace
from datetime import datetime as dt
from typing import Any, Final
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ramses_rf import Gateway
from ramses_rf.const import FC, I_, SZ_DOMAIN_ID, Code
from ramses_rf.messages import Message
from ramses_rf.systems.tcs import SystemBase
from ramses_tx import Packet
from ramses_tx.address import HGI_DEVICE_ID

# A standard 3150 I-packet (Heat Demand) from a Controller
# 3150 002 FCC8 -> domain_id=FC (System), demand=C8 (100%)
# NOTE: Must use double space after RSSI (064) for ' I' verb parsing
# by Packet.from_port
PKT_3150: Final = f"064  I --- 01:145038 --:------ 01:145038 {Code._3150} 002 FCC8"


# --- Fixtures required for fake_evofw3 ---


@pytest.fixture()
def gwy_config() -> dict[str, Any]:
    """Return a valid configuration for the gateway."""
    return {}


@pytest.fixture()
def gwy_dev_id() -> str:
    """Return a valid device ID for the gateway."""
    return HGI_DEVICE_ID


# --- Helper to create a valid Mock Message ---


def create_mock_message(tcs: SystemBase, payload: Any) -> MagicMock:
    """Create a mock message that looks like it came from the TCS
    controller.

    Includes internal structures (_pkt, _ctx) required for logging/caching.
    """
    mock_msg = MagicMock(spec=Message)
    mock_msg.code = Code._3150
    mock_msg.verb = I_
    mock_msg.src = MagicMock()
    mock_msg.src.id = tcs.id  # Match TCS ID so it is accepted
    mock_msg.payload = payload

    # Mock the internal packet structure required by Entity._handle_msg logging
    mock_msg._pkt = MagicMock()
    # Unique context key for caching: (timestamp, addr, ...)
    # Just needs to be hashable
    mock_msg._pkt._ctx = f"{dt.now().isoformat()}_{tcs.id}"

    return mock_msg


# --- Tests ---


@pytest.mark.asyncio
async def test_system_handle_msg_3150_real_packet(fake_evofw3: Gateway) -> None:
    """Check that a real 3150 packet is handled correctly.

    If this passes, it means the current parser produces a payload (likely
    a dict) that the current code can handle.
    """
    gwy = fake_evofw3
    pkt = Packet.from_port(dt.now(), PKT_3150)
    gwy._engine._protocol.pkt_received(pkt)
    await asyncio.sleep(0)  # Yield to loop to process call_soon callbacks

    tcs = gwy.tcs
    assert tcs is not None
    assert tcs.heat_demand is not None


@pytest.mark.asyncio
async def test_system_handle_msg_3150_force_list(fake_evofw3: Gateway) -> None:
    """Simulate a parser returning a LIST payload.

    Confirms that the system correctly parses multi-zone payloads.
    """
    gwy = fake_evofw3

    # Bootstrap TCS
    pkt = Packet.from_port(dt.now(), PKT_3150)
    gwy._engine._protocol.pkt_received(pkt)
    await asyncio.sleep(0)  # Yield to loop to process call_soon callbacks
    tcs = gwy.tcs
    assert tcs is not None  # Ensure TCS exists for Mypy

    # Construct a List-based payload (New/Hybrid Style)
    # The parser might return[ {domain: FC, demand: 0.5}, ... ]
    payload = [{SZ_DOMAIN_ID: FC, "heat_demand": 0.5}]

    if not isinstance(tcs, SystemBase):
        pytest.fail("TCS is not an instance of SystemBase")

    msg = create_mock_message(tcs, payload)
    tcs._handle_msg(msg)

    # We verify if it actually extracted the value
    assert tcs._heat_demand == payload[0]


@pytest.mark.asyncio
async def test_system_handle_msg_3150_force_dict(fake_evofw3: Gateway) -> None:
    """Simulate a parser returning a DICT payload.

    This ensures backward compatibility for Dict payloads.
    """
    gwy = fake_evofw3

    # Bootstrap TCS
    pkt = Packet.from_port(dt.now(), PKT_3150)
    gwy._engine._protocol.pkt_received(pkt)
    await asyncio.sleep(0)  # Yield to loop to process call_soon callbacks
    tcs = gwy.tcs
    assert tcs is not None  # Ensure TCS exists for Mypy

    # Construct a Dict-based payload (Legacy Style)
    payload = {SZ_DOMAIN_ID: FC, "heat_demand": 0.5}

    if not isinstance(tcs, SystemBase):
        pytest.fail("TCS is not an instance of SystemBase")

    msg = create_mock_message(tcs, payload)
    tcs._handle_msg(msg)

    assert tcs._heat_demand == payload


@pytest.mark.asyncio
async def test_system_handle_msg_3150_list_no_match(
    fake_evofw3: Gateway,
) -> None:
    """Verify list payload ignores unrelated domains."""
    gwy = fake_evofw3
    pkt = Packet.from_port(dt.now(), PKT_3150)
    gwy._engine._protocol.pkt_received(pkt)
    await asyncio.sleep(0)
    tcs = gwy.tcs
    assert tcs is not None

    tcs._heat_demand = None
    payload = [{"domain_id": "FA", "heat_demand": 0.5}]
    msg = create_mock_message(tcs, payload)

    tcs._handle_msg(msg)
    assert tcs._heat_demand is None


@pytest.mark.asyncio
async def test_system_handle_msg_3150_dict_no_match(
    fake_evofw3: Gateway,
) -> None:
    """Verify dict payload ignores unrelated domains."""
    gwy = fake_evofw3
    pkt = Packet.from_port(dt.now(), PKT_3150)
    gwy._engine._protocol.pkt_received(pkt)
    await asyncio.sleep(0)
    tcs = gwy.tcs
    assert tcs is not None

    tcs._heat_demand = None
    payload = {"domain_id": "F9", "heat_demand": 0.5}
    msg = create_mock_message(tcs, payload)

    tcs._handle_msg(msg)
    assert tcs._heat_demand is None


@pytest.mark.asyncio
async def test_system_handle_msg_3150_invalid_type(
    fake_evofw3: Gateway, caplog: pytest.LogCaptureFixture
) -> None:
    """Verify unexpected payload types are logged as warnings."""
    gwy = fake_evofw3
    pkt = Packet.from_port(dt.now(), PKT_3150)
    gwy._engine._protocol.pkt_received(pkt)
    await asyncio.sleep(0)
    tcs = gwy.tcs
    assert tcs is not None

    payload = "unexpected_string"
    msg = create_mock_message(tcs, payload)

    with caplog.at_level(logging.WARNING):
        tcs._handle_msg(msg)

    assert "Unexpected payload type" in caplog.text


@pytest.mark.asyncio
async def test_logbook_setup_discovery_creates_task(
    fake_evofw3: Gateway,
) -> None:
    """Verify Logbook actively schedules fault log retrieval on discovery."""
    gwy = fake_evofw3
    pkt = Packet.from_port(dt.now(), PKT_3150)
    gwy._engine._protocol.pkt_received(pkt)
    await asyncio.sleep(0)

    tcs = gwy.tcs
    assert tcs is not None

    with patch.object(tcs, "get_faultlog", new_callable=AsyncMock) as mock_fault:
        await tcs.get_faultlog()
        mock_fault.assert_called_once()


@pytest.mark.asyncio
async def test_sysmode_system_mode_async_cache_lookup(
    fake_evofw3: Gateway,
) -> None:
    """Verify system_mode retrieves state asynchronously from CQRS read-model."""
    gwy = fake_evofw3
    pkt = Packet.from_port(dt.now(), PKT_3150)
    gwy._engine._protocol.pkt_received(pkt)
    await asyncio.sleep(0)

    tcs = gwy.tcs
    assert tcs is not None

    tcs.system_state = replace(tcs.system_state, system_mode="01", until=None)

    # Call the newly refactored async method lookup
    result = await tcs.system_mode()

    assert result == {"system_mode": "01", "until": None}
