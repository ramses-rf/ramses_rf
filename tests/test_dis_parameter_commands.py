"""Test DIS parameter commands."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from ramses_rf import Gateway
from ramses_rf.device.hvac import HvacDisplayRemote

# Type checking imports
if TYPE_CHECKING:
    pass

# Type definitions for test data
MOCKED_PACKET: dict[str, Any] = {}
MOCKED_SERIAL_CONFIG: dict[str, Any] = {}
MOCKED_SERIAL_PORT = ""
TEST_HELPERS_AVAILABLE = False

# Type for the mock protocol fixture
if TYPE_CHECKING:
    from unittest.mock import MagicMock

    MockProtocol = MagicMock
else:
    MockProtocol = Any  # Can't import the actual type due to circular imports


def assert_this_request(mock_protocol: Any, **kwargs: Any) -> None:
    """Dummy function for when test helpers are not available."""
    pass


def find_device_tcs(gwy: Gateway, dev_id: str) -> Any | None:
    """Dummy function for when test helpers are not available."""
    return None


# Try to import test helpers if available
try:
    from tests.test_rf.helpers import (
        MOCKED_PACKET as IMPORTED_PACKET,
        MOCKED_SERIAL_CONFIG as IMPORTED_SERIAL_CONFIG,
        MOCKED_SERIAL_PORT as IMPORTED_SERIAL_PORT,
        assert_this_request as imported_assert_this_request,
        find_device_tcs as imported_find_device_tcs,
    )

    # Override with imported values
    MOCKED_PACKET = IMPORTED_PACKET
    MOCKED_SERIAL_CONFIG = IMPORTED_SERIAL_CONFIG
    MOCKED_SERIAL_PORT = IMPORTED_SERIAL_PORT
    assert_this_request = imported_assert_this_request
    find_device_tcs = imported_find_device_tcs
    TEST_HELPERS_AVAILABLE = True
except ImportError:
    # Use the dummy implementations defined above
    pass


@pytest.mark.xdist_group(name="dis_param_commands")
@pytest.mark.skipif(not TEST_HELPERS_AVAILABLE, reason="Test helpers not available")
@pytest.mark.asyncio
async def test_dis_get_fan_param(
    mock_protocol: MockProtocol,
) -> None:
    """Test getting a fan parameter through a DIS device."""
    # Setup test environment
    gwy = Gateway(
        MOCKED_SERIAL_PORT,
        **MOCKED_SERIAL_CONFIG,
        enforce_include_list={"devices": ["01:123456"]},  # Only include the DIS device
    )
    await gwy.start()

    # Get the DIS device
    dis = find_device_tcs(gwy, "01:123456")
    assert dis is not None
    assert isinstance(dis, HvacDisplayRemote)
    assert dis._SLUG == "DIS"

    # Mock the response from the fan
    mock_protocol.send_side_effect = [
        MOCKED_PACKET["31DA"],  # Mock fan response
    ]

    # Test getting a parameter
    result = await dis.get_fan_param("32:123456", "parameter_id")

    # Verify the result
    assert result is not None  # Adjust based on expected response

    # Verify the command was sent correctly
    assert_this_request(
        mock_protocol,
        has_payload={
            "verb": " I",
            "code": "2411",
            "src": "01:123456",
            "dst": "32:123456",
            "len": "00C",
            "payload": "0000parameter_id",
        },
    )

    await gwy.stop()


@pytest.mark.xdist_group(name="dis_param_commands")
@pytest.mark.skipif(not TEST_HELPERS_AVAILABLE, reason="Test helpers not available")
@pytest.mark.asyncio
async def test_dis_set_fan_param(
    mock_protocol: MockProtocol,
) -> None:
    """Test setting a fan parameter through a DIS device."""
    # Setup test environment
    gwy = Gateway(
        MOCKED_SERIAL_PORT,
        **MOCKED_SERIAL_CONFIG,
        enforce_include_list={"devices": ["01:123456"]},  # Only include the DIS device
    )
    await gwy.start()

    # Get the DIS device
    dis = find_device_tcs(gwy, "01:123456")
    assert dis is not None
    assert isinstance(dis, HvacDisplayRemote)
    assert dis._SLUG == "DIS"

    # Mock the response from the fan
    mock_protocol.send_side_effect = [
        MOCKED_PACKET["31DA"],  # Mock fan response
    ]

    # Test setting a parameter
    await dis.set_fan_param("32:123456", "parameter_id", 42)

    # Verify the command was sent correctly
    assert_this_request(
        mock_protocol,
        has_payload={
            "verb": " I",
            "code": "2411",
            "src": "01:123456",
            "dst": "32:123456",
            "len": "00C",
            "payload": "0001parameter_id",
        },
    )

    await gwy.stop()
