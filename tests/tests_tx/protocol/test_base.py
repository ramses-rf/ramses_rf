"""Tests for the RAMSES-II base protocol layer."""

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ramses_tx.address import HGI_DEV_ADDR
from ramses_tx.exceptions import ProtocolError, TransportError
from ramses_tx.protocol.base import _DeviceIdFilterMixin
from ramses_tx.typing import DeviceIdT

# Ensure all tests in this file run within an asyncio event loop
pytestmark = pytest.mark.asyncio


class DummyProtocol(_DeviceIdFilterMixin):
    """Testable protocol class incorporating device ID filtering."""

    def __init__(self, msg_handler: Any) -> None:
        # Initialize the mixin, which handles the exclusion/inclusion list setup
        super().__init__(msg_handler)

    async def _send_cmd(self, cmd: Any, **kwargs: Any) -> Any:
        """Override the abstract _send_cmd to prevent NotImplementedError."""
        return cmd


@pytest.fixture
def mock_msg_handler() -> AsyncMock:
    """Provide a dummy message handler for the protocol."""
    return AsyncMock()


@pytest.fixture
async def protocol(mock_msg_handler: AsyncMock) -> DummyProtocol:
    """Provide a fresh instance of the testable base protocol."""
    return DummyProtocol(mock_msg_handler)


# --- CONNECTION LIFECYCLE TESTS (Issue #560 Fixes) ---


async def test_wait_for_connection_lost_no_connection(protocol: DummyProtocol) -> None:
    """Test wait_for_connection_lost when no connection was ever made."""
    result = await protocol.wait_for_connection_lost()
    assert result is None


async def test_wait_for_connection_lost_clean_disconnect(
    protocol: DummyProtocol,
) -> None:
    """Test wait_for_connection_lost returns None on a clean disconnect."""
    mock_transport = MagicMock()
    protocol.connection_made(mock_transport)
    protocol.connection_lost(None)

    result = await protocol.wait_for_connection_lost()
    assert result is None


async def test_wait_for_connection_lost_with_exception(protocol: DummyProtocol) -> None:
    """Test wait_for_connection_lost returns (does not raise) transport exceptions."""
    mock_transport = MagicMock()
    protocol.connection_made(mock_transport)

    expected_exc = Exception("Device disconnected unexpectedly")
    protocol.connection_lost(expected_exc)

    result = await protocol.wait_for_connection_lost()
    assert result is expected_exc


async def test_wait_for_connection_lost_timeout(protocol: DummyProtocol) -> None:
    """Test wait_for_connection_lost raises TransportError if it times out."""
    mock_transport = MagicMock()
    protocol.connection_made(mock_transport)

    with pytest.raises(TransportError, match="Transport did not unbind from Protocol"):
        await protocol.wait_for_connection_lost(timeout=0.01)


# --- DEVICE ID FILTERING TESTS (_is_wanted_addrs) ---


async def test_is_wanted_addrs_empty_filters(protocol: DummyProtocol) -> None:
    """Test default behavior with no filters set."""
    assert (
        protocol._is_wanted_addrs(DeviceIdT("01:111111"), DeviceIdT("01:222222"))
        is True
    )


async def test_is_wanted_addrs_exclude_list(protocol: DummyProtocol) -> None:
    """Test that devices in the exclude list are rejected."""
    protocol._exclude = [DeviceIdT("01:111111")]
    assert (
        protocol._is_wanted_addrs(DeviceIdT("01:111111"), DeviceIdT("01:222222"))
        is False
    )
    assert (
        protocol._is_wanted_addrs(DeviceIdT("01:222222"), DeviceIdT("01:111111"))
        is False
    )
    assert (
        protocol._is_wanted_addrs(DeviceIdT("01:333333"), DeviceIdT("01:444444"))
        is True
    )


async def test_is_wanted_addrs_enforce_include(protocol: DummyProtocol) -> None:
    """Test enforce_include logic ensures ALL addresses are in the include list."""
    protocol.enforce_include = True
    protocol._include = [DeviceIdT("01:111111")]

    # Only one device included, the other isn't -> False
    assert (
        protocol._is_wanted_addrs(DeviceIdT("01:111111"), DeviceIdT("01:222222"))
        is False
    )

    # Both devices included -> True
    protocol._include = [DeviceIdT("01:111111"), DeviceIdT("01:222222")]
    assert (
        protocol._is_wanted_addrs(DeviceIdT("01:111111"), DeviceIdT("01:222222"))
        is True
    )


async def test_is_wanted_addrs_active_hgi(protocol: DummyProtocol) -> None:
    """Test that the active HGI bypasses the enforce_include filter."""
    protocol.enforce_include = True
    protocol._include = [DeviceIdT("01:111111")]
    protocol._active_hgi = DeviceIdT("18:999999")

    # 18:999999 is the active HGI, so it should be permitted despite not being in _include
    assert (
        protocol._is_wanted_addrs(DeviceIdT("01:111111"), DeviceIdT("18:999999"))
        is True
    )


async def test_is_wanted_addrs_sending_to_hgi(protocol: DummyProtocol) -> None:
    """Test that sending to the generic HGI address is permitted."""
    protocol.enforce_include = True
    protocol._include = [DeviceIdT("01:111111")]

    # When sending, HGI_DEV_ADDR (18:000730) is always allowed
    assert (
        protocol._is_wanted_addrs(DeviceIdT("01:111111"), HGI_DEV_ADDR.id, sending=True)
        is True
    )
    # But not when receiving
    assert (
        protocol._is_wanted_addrs(
            DeviceIdT("01:111111"), HGI_DEV_ADDR.id, sending=False
        )
        is False
    )


async def test_is_wanted_addrs_foreign_hgi_not_blocked(protocol: DummyProtocol) -> None:
    """Foreign HGIs (18:) must not be blocked even if in the exclude list.

    A foreign HGI communicates with our controller and the controller's
    responses (e.g. 0004 zone names) are addressed to the foreign HGI.
    Blocking the foreign HGI would prevent the active gateway from
    eavesdropping on those responses (issue 822).
    """
    protocol._active_hgi = DeviceIdT("18:191664")
    protocol._exclude = [DeviceIdT("18:072981")]  # foreign HGI in block_list

    # Packet from controller to foreign HGI (e.g. 0004 RP zone name)
    assert (
        protocol._is_wanted_addrs(DeviceIdT("01:216136"), DeviceIdT("18:072981"))
        is True
    )
    # Packet from foreign HGI to controller (e.g. 0004 RQ)
    assert (
        protocol._is_wanted_addrs(DeviceIdT("18:072981"), DeviceIdT("01:216136"))
        is True
    )


async def test_is_wanted_addrs_hgi_dev_addr_still_blocked(
    protocol: DummyProtocol,
) -> None:
    """HGI_DEV_ADDR (18:000730) is the generic broadcast, not a specific HGI.

    It must still be subject to the block_list — only specific foreign HGIs
    (18:XXXXXX where XXXXXX != 000730) are exempt.
    """
    protocol._exclude = [HGI_DEV_ADDR.id]

    assert protocol._is_wanted_addrs(DeviceIdT("01:216136"), HGI_DEV_ADDR.id) is False


# --- INBOUND PACKET TESTS (_pkt_received) ---


async def test_pkt_received_included(protocol: DummyProtocol) -> None:
    """Test that wanted packets are passed up to the parent class."""
    mock_pkt = MagicMock()
    mock_pkt.src.id = "01:111111"
    mock_pkt.dst.id = "01:222222"

    # Patch the base class to prevent the mock from triggering validation errors
    with patch("ramses_tx.protocol.base._BaseProtocol._pkt_received") as mock_base_recv:
        protocol._pkt_received(mock_pkt)
        mock_base_recv.assert_called_once_with(mock_pkt)


async def test_pkt_received_excluded(
    protocol: DummyProtocol, caplog: pytest.LogCaptureFixture
) -> None:
    """Test that unwanted packets are dropped and logged."""
    protocol._exclude = [DeviceIdT("01:111111")]
    mock_pkt = MagicMock()
    mock_pkt.src.id = "01:111111"
    mock_pkt.dst.id = "01:222222"

    with (
        caplog.at_level(logging.DEBUG),
        patch("ramses_tx.protocol.base._BaseProtocol._pkt_received") as mock_base_recv,
    ):
        protocol._pkt_received(mock_pkt)
        mock_base_recv.assert_not_called()

    assert "Packet excluded by device_id filter" in caplog.text


# --- OUTBOUND COMMAND TESTS (send_cmd) ---


async def test_send_cmd_included(protocol: DummyProtocol) -> None:
    """Test that wanted commands are sent down to the parent class."""
    mock_cmd = MagicMock()
    mock_cmd.src.id = "01:111111"
    mock_cmd.dst.id = "01:222222"
    protocol._is_evofw3 = False  # Avoids triggering deep address parsing on the mock

    result = await protocol.send_cmd(mock_cmd)

    assert result is mock_cmd


async def test_send_cmd_excluded(protocol: DummyProtocol) -> None:
    """Test that sending unwanted commands raises a ProtocolError."""
    protocol._exclude = [DeviceIdT("01:111111")]
    mock_cmd = MagicMock()
    mock_cmd.src.id = "01:111111"
    mock_cmd.dst.id = "01:222222"

    with pytest.raises(ProtocolError, match="Command excluded by device_id filter"):
        await protocol.send_cmd(mock_cmd)


async def test_patch_cmd_if_needed_evofw3(protocol: DummyProtocol) -> None:
    """Test that _patch_cmd_if_needed swaps the default HGI address for evofw3."""
    from ramses_tx.command import Command

    protocol._is_evofw3 = True
    protocol._known_hgi = DeviceIdT("18:123456")  # Safely sets the hgi_id property

    original_cmd = Command("RQ --- 18:000730 01:222222 --:------ 12B0 001 00")

    patched_cmd = protocol._patch_cmd_if_needed(original_cmd)

    assert patched_cmd is not original_cmd
    assert patched_cmd.src.id == "18:123456"
    assert patched_cmd.dst.id == "01:222222"
    assert original_cmd.src.id == "18:000730"  # Enforces immutability
