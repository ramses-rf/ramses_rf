#!/usr/bin/env python3
"""Tests for the HvacVentilator class."""

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ramses_rf.device.hvac import HvacVentilator
from ramses_rf.gateway import Gateway
from ramses_tx import Address
from ramses_tx.const import Code
from ramses_tx.schemas import DeviceIdT

# Test data
TEST_DEVICE_ID = "32:123456"
TEST_PARAM_ID = "3F"
TEST_PARAM_VALUE = 50
TEST_MSG_ID = "1234"


@pytest.fixture
def mock_gateway() -> Generator[MagicMock, None, None]:
    """Create a mock Gateway instance for testing."""
    gateway = MagicMock(spec=Gateway)
    gateway.send_cmd = AsyncMock()
    gateway.dispatcher = MagicMock()
    gateway.dispatcher.send = MagicMock()

    # Add required attributes
    gateway.config = MagicMock()
    gateway.config.disable_discovery = False
    gateway.config.enable_eavesdrop = False
    gateway._loop = MagicMock()
    gateway._loop.call_soon = MagicMock()
    gateway._loop.call_later = MagicMock()
    gateway._loop.time = MagicMock(return_value=0.0)
    gateway._include = {}

    # Add _zzz attribute that's accessed by the message store
    gateway._zzz = MagicMock()
    gateway._zzz.get.return_value = {}

    yield gateway


@pytest.fixture
def hvac_ventilator(mock_gateway: MagicMock) -> HvacVentilator:
    """Create an HvacVentilator instance for testing."""
    device_id = DeviceIdT(TEST_DEVICE_ID)
    return HvacVentilator(mock_gateway, Address(device_id))


class TestHvacVentilator:
    """Test HvacVentilator class."""

    def test_initialization(self, hvac_ventilator: HvacVentilator) -> None:
        """Test that the ventilator initializes correctly."""
        assert hvac_ventilator._supports_2411 is False
        assert hvac_ventilator._initialized_callback is None
        assert hvac_ventilator._param_update_callback is None
        assert hvac_ventilator._hgi is None
        assert hvac_ventilator._bound_devices == {}

    def test_set_initialized_callback_clear(
        self, hvac_ventilator: HvacVentilator
    ) -> None:
        """Test clearing the initialized callback."""
        # Set a callback first
        mock_callback = MagicMock()
        hvac_ventilator.set_initialized_callback(mock_callback)

        # Now clear it
        hvac_ventilator.set_initialized_callback(None)
        assert hvac_ventilator._initialized_callback is None

    def test_set_initialized_callback_set(
        self, hvac_ventilator: HvacVentilator
    ) -> None:
        """Test setting the initialized callback."""
        # Test initial state
        assert hvac_ventilator._initialized_callback is None

        # Set the callback
        mock_callback = MagicMock()
        hvac_ventilator.set_initialized_callback(mock_callback)
        assert hvac_ventilator._initialized_callback is mock_callback

    def test_set_param_update_callback(self, hvac_ventilator: HvacVentilator) -> None:
        """Test setting the parameter update callback."""
        # Define a mock callback
        mock_callback = MagicMock()

        # Set the callback
        hvac_ventilator.set_param_update_callback(mock_callback)

        # Check that the callback was set
        assert hvac_ventilator._param_update_callback is mock_callback

    def test_handle_2411_message(self, hvac_ventilator: HvacVentilator) -> None:
        """Test handling a 2411 message."""
        # Create a mock message
        msg = MagicMock()
        msg.payload = {"parameter": TEST_PARAM_ID, "value": TEST_PARAM_VALUE}

        # Set up the param update callback
        mock_callback = MagicMock()
        hvac_ventilator.set_param_update_callback(mock_callback)

        # Call the method
        hvac_ventilator._handle_2411_message(msg)

        # Check that supports_2411 was set to True
        assert hvac_ventilator._supports_2411 is True

        # Check that the callback was called with the correct parameters
        mock_callback.assert_called_once_with(TEST_PARAM_ID, TEST_PARAM_VALUE)

    @patch("ramses_rf.device.hvac.Command.get_fan_param")
    async def test_setup_discovery_cmds(
        self, mock_cmd: MagicMock, hvac_ventilator: HvacVentilator
    ) -> None:
        """Test that discovery commands are set up correctly."""
        # Mock the command creation
        mock_cmd.return_value = "MOCK_CMD"

        # Use patch.object to properly mock the method
        with patch.object(hvac_ventilator, "_add_discovery_cmd") as mock_add_cmd:
            # Call the method
            hvac_ventilator._setup_discovery_cmds()

            # Check that _add_discovery_cmd was called at least once
            assert mock_add_cmd.called

    async def test_handle_msg_parameter_message(
        self, hvac_ventilator: HvacVentilator
    ) -> None:
        """Test that parameter messages are handled correctly."""
        # Create a mock message
        msg = MagicMock()
        msg.code = Code._2411
        msg.payload = {
            "parameter": TEST_PARAM_ID,
            "value": TEST_PARAM_VALUE,
        }

        # Mock the 2411 message handler
        with patch.object(hvac_ventilator, "_handle_2411_message") as mock_handler:
            # Process the message
            hvac_ventilator._handle_msg(msg)

            # Check that the 2411 handler was called
            mock_handler.assert_called_once_with(msg)

    async def test_handle_msg_non_parameter_message(
        self, hvac_ventilator: HvacVentilator
    ) -> None:
        """Test that non-parameter messages are passed to the parent class."""
        # Create a mock message with a non-parameter code
        msg = MagicMock()
        msg.code = Code._31DA  # Standard FAN status code
        msg.payload = {"some_key": "some_value"}

        # Mock the parent class's _handle_msg
        with patch(
            "ramses_rf.device.hvac.FilterChange._handle_msg"
        ) as mock_parent_handle_msg:
            # Process the message
            hvac_ventilator._handle_msg(msg)

            # Check that the parent's handler was called
            mock_parent_handle_msg.assert_called_once_with(msg)

            # The parameter handler should not have been called
            assert (
                not hasattr(hvac_ventilator, "_handle_parameter_msg")
                or not mock_parent_handle_msg.called
            )
