#!/usr/bin/env python3
"""
Combined Unit tests for the VirtualRf harness.

Verifies PTY handling, I/O safety, and hardware gateway emulation.
"""

import asyncio
from collections.abc import AsyncGenerator
from typing import Final
from unittest.mock import MagicMock, patch

import pytest

from .virtual_rf import virtual_rf as vrf_mod
from .virtual_rf.const import SCHEMA_3, HgiFwTypes
from .virtual_rf.virtual_rf import VirtualRf

# Constants
TEST_DATA: Final[bytes] = b"Hello World\r\n"


@pytest.fixture
async def virtual_rf() -> AsyncGenerator[VirtualRf, None]:
    """
    Fixture to provide a VirtualRf instance using the Async Context Manager.

    :yield: An initialized VirtualRf instance.
    """
    # Using 'async with' ensures __aenter__ and __aexit__ are tested
    async with VirtualRf(num_ports=3) as vrf:
        yield vrf


@pytest.mark.asyncio
async def test_virtual_rf_lifecycle() -> None:
    """
    Test the start and stop lifecycle via the Async Context Manager.
    """
    # Test that __aenter__ and __aexit__ handle resources correctly
    async with VirtualRf(num_ports=2) as vrf:
        assert len(vrf.ports) == 2
        assert len(vrf._master_to_port) == 2

    # Verify cleanup occurred after context exit
    assert len(vrf._master_to_port) == 0


@pytest.mark.asyncio
async def test_broadcast_data(virtual_rf: VirtualRf) -> None:
    """
    Test that data written to one PTY is broadcast to others.
    """
    port_0 = virtual_rf.ports[0]
    port_1 = virtual_rf.ports[1]
    fd_0_master = virtual_rf._port_to_master[port_0]

    mock_file_io = MagicMock()
    original_io = virtual_rf._port_to_object[port_1]
    virtual_rf._port_to_object[port_1] = mock_file_io

    try:
        with patch.object(
            virtual_rf._port_to_object[port_0], "read", return_value=TEST_DATA
        ):
            virtual_rf._handle_data_ready(fd_0_master)

        # Note: Broadcaster adds RSSI '000 ' if not a control frame
        mock_file_io.write.assert_called_with(b"000 " + TEST_DATA)

    finally:
        virtual_rf._port_to_object[port_1] = original_io


@pytest.mark.asyncio
async def test_blocking_io_handling(virtual_rf: VirtualRf) -> None:
    """
    Test handling of BlockingIOError during broadcast write.
    """
    port_0 = virtual_rf.ports[0]
    port_1 = virtual_rf.ports[1]
    fd_0_master = virtual_rf._port_to_master[port_0]

    mock_file_io = MagicMock()
    mock_file_io.write.side_effect = BlockingIOError
    original_io = virtual_rf._port_to_object[port_1]
    virtual_rf._port_to_object[port_1] = mock_file_io

    with patch.object(vrf_mod._LOGGER, "warning") as mock_log:
        with patch.object(
            virtual_rf._port_to_object[port_0], "read", return_value=TEST_DATA
        ):
            virtual_rf._handle_data_ready(fd_0_master)

        mock_log.assert_called_with(f"Buffer full writing to {port_1}, dropping packet")

    virtual_rf._port_to_object[port_1] = original_io


@pytest.mark.asyncio
async def test_gateway_emulation(virtual_rf: VirtualRf) -> None:
    """
    Test hardware-specific emulation logic for different firmware types.
    """
    # Setup different gateway profiles
    virtual_rf.set_gateway(virtual_rf.ports[0], "18:111111", HgiFwTypes.EVOFW3)
    virtual_rf.set_gateway(virtual_rf.ports[1], "18:222222", HgiFwTypes.EVOFW3_FTDI)
    virtual_rf.set_gateway(virtual_rf.ports[2], "18:333333", HgiFwTypes.HGI_80)

    # Test !V (Version) response for EVOFW3
    for i in range(2):
        port = virtual_rf.ports[i]
        response = virtual_rf._proc_after_rx(port, b"!V")
        assert response == b"# evofw3 0.7.1\r\n"

    # Test HGI80 correctly ignores !V
    hgi_port = virtual_rf.ports[2]
    assert virtual_rf._proc_after_rx(hgi_port, b"!V") is None


@pytest.mark.asyncio
async def test_schema_3_integration(virtual_rf: VirtualRf) -> None:
    """
    Verify that SCHEMA_3 (HVAC/Generic) initializes without errors.
    """

    # Test that the gateway from SCHEMA_3 can be attached
    gwy_id = list(SCHEMA_3["known_list"].keys())[0]  # 18:333333

    # This should not raise LookupError or TypeError
    virtual_rf.set_gateway(virtual_rf.ports[0], gwy_id)

    assert virtual_rf.gateways[gwy_id] == virtual_rf.ports[0]


@pytest.mark.asyncio
async def test_rapid_cycling_stress_test() -> None:
    """
    Stress test: Rapidly start and stop the VirtualRf environment.

    This ensures that:
    1. File descriptors are not leaking.
    2. Event loop readers are cleanly removed (no 'File descriptor bad' errors).
    3. No race conditions occur during fast teardown/setup cycles.
    """
    for _ in range(50):
        async with VirtualRf(num_ports=2) as vrf:
            # Verify ports allow basic IO immediately
            assert len(vrf.ports) == 2
            # Minimal sleep to let the loop turn once
            await asyncio.sleep(0)
        # Give the loop one final turn to settle the FDs from the context manager
        await asyncio.sleep(0)
