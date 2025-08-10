#!/usr/bin/env python3
"""Tests for the HvacVentilator class."""

import asyncio
import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ramses_rf.device.hvac import HvacVentilator
from ramses_rf.gateway import Gateway
from ramses_rf.packets import Packet
from ramses_rf.schemas import SZ_ACTUATOR_CYCLE, SZ_ACTIVE
from ramses_tx.const import Code
from ramses_tx.packet import Packet as TxPacket

# Test data
TEST_DEVICE_ID = "32:123456"
TEST_PARAM_ID = "3F"
TEST_PARAM_VALUE = 50
TEST_MSG_ID = "1234"


@pytest.fixture
def mock_gateway():
    """Create a mock Gateway instance for testing."""
    gateway = MagicMock(spec=Gateway)
    gateway.send_cmd = AsyncMock()
    gateway.dispatcher = MagicMock()
    gateway.dispatcher.send = MagicMock()
    return gateway


@pytest.fixture
def hvac_ventilator(mock_gateway):
    """Create an HvacVentilator instance for testing."""
    return HvacVentilator(mock_gateway, TEST_DEVICE_ID)


class TestHvacVentilatorParameterHandling:
    """Test parameter handling in HvacVentilator class."""

    def test_initialization(self, hvac_ventilator):
        """Test that the ventilator initializes with empty parameter storage."""
        assert hvac_ventilator._parameters == {}
        assert hvac_ventilator._parameter_support == set()
        assert hasattr(hvac_ventilator, "_parameter_lock")
        assert isinstance(hvac_ventilator._parameter_lock, asyncio.Lock)

    async def test_handle_parameter_msg_valid(self, hvac_ventilator):
        """Test handling a valid parameter message."""
        # Create a mock message
        msg = MagicMock()
        msg.code = Code._2411
        msg.payload = {
            "parameter_id": TEST_PARAM_ID,
            "value": TEST_PARAM_VALUE,
            "msg_id": TEST_MSG_ID,
        }

        # Process the message
        hvac_ventilator._handle_parameter_msg(msg)

        # Check that the parameter was stored
        assert TEST_PARAM_ID in hvac_ventilator._parameters
        assert hvac_ventilator._parameters[TEST_PARAM_ID]["value"] == TEST_PARAM_VALUE
        assert TEST_PARAM_ID in hvac_ventilator._parameter_support

    async def test_handle_parameter_msg_invalid(self, hvac_ventilator, caplog):
        """Test handling an invalid parameter message."""
        # Create a mock message with missing parameter ID
        msg = MagicMock()
        msg.code = Code._2411
        msg.payload = {"value": TEST_PARAM_VALUE}  # Missing parameter_id

        # Process the message
        hvac_ventilator._handle_parameter_msg(msg)

        # Check that no parameter was stored
        assert TEST_PARAM_ID not in hvac_ventilator._parameters
        assert "no parameter ID" in caplog.text.lower()

    async def test_update_parameter_new_value(self, hvac_ventilator):
        """Test updating a parameter with a new value."""
        # Initial state
        assert TEST_PARAM_ID not in hvac_ventilator._parameters

        # Update parameter
        hvac_ventilator._update_parameter(TEST_PARAM_ID, TEST_PARAM_VALUE)

        # Check that the parameter was stored
        assert TEST_PARAM_ID in hvac_ventilator._parameters
        assert hvac_ventilator._parameters[TEST_PARAM_ID]["value"] == TEST_PARAM_VALUE
        assert TEST_PARAM_ID in hvac_ventilator._parameter_support

    async def test_update_parameter_same_value(self, hvac_ventilator):
        """Test updating a parameter with the same value doesn't trigger events."""
        # Initial update
        hvac_ventilator._update_parameter(TEST_PARAM_ID, TEST_PARAM_VALUE)

        # Clear the event dispatcher mock
        hvac_ventilator._gwy.dispatcher.send.reset_mock()

        # Update with same value
        hvac_ventilator._update_parameter(TEST_PARAM_ID, TEST_PARAM_VALUE)

        # Check that no event was emitted
        hvac_ventilator._gwy.dispatcher.send.assert_not_called()

    async def test_emit_parameter_update(self, hvac_ventilator):
        """Test emitting a parameter update event."""
        old_value = 40
        new_value = 50

        # Emit the update
        hvac_ventilator._emit_parameter_update(TEST_PARAM_ID, new_value, old_value)

        # Check that the event was emitted with the correct data
        hvac_ventilator._gwy.dispatcher.send.assert_called_once()
        event_name, event_data = hvac_ventilator._gwy.dispatcher.send.call_args[0]

        assert event_name == "fan_parameter_updated"
        assert event_data["device_id"] == TEST_DEVICE_ID
        assert event_data["parameter_id"] == TEST_PARAM_ID
        assert event_data["new_value"] == new_value
        assert event_data["old_value"] == old_value
        assert "timestamp" in event_data

    async def test_get_parameter(self, hvac_ventilator):
        """Test getting a parameter value."""
        # Set up test data
        test_value = 42
        hvac_ventilator._parameters[TEST_PARAM_ID] = {
            "value": test_value,
            "last_updated": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "source": "test",
        }

        # Test getting the parameter
        result = await hvac_ventilator.get_parameter(TEST_PARAM_ID)
        assert result == test_value

        # Test getting a non-existent parameter
        result = await hvac_ventilator.get_parameter("XX")
        assert result is None

        # Test invalid parameter ID
        with pytest.raises(ValueError):
            await hvac_ventilator.get_parameter("")

    def test_get_parameter_metadata(self, hvac_ventilator):
        """Test getting parameter metadata."""
        # Set up test data
        test_metadata = {
            "value": 42,
            "last_updated": "2023-01-01T00:00:00+00:00",
            "source": "test",
        }
        hvac_ventilator._parameters[TEST_PARAM_ID] = test_metadata

        # Test getting the metadata
        result = hvac_ventilator.get_parameter_metadata(TEST_PARAM_ID)
        assert result == test_metadata

        # Test getting metadata for a non-existent parameter
        result = hvac_ventilator.get_parameter_metadata("XX")
        assert result == {}

        # Test invalid parameter ID
        result = hvac_ventilator.get_parameter_metadata("")
        assert result == {}

    def test_is_parameter_supported(self, hvac_ventilator):
        """Test checking if a parameter is supported."""
        # Initially not supported
        assert not hvac_ventilator.is_parameter_supported(TEST_PARAM_ID)

        # Mark as supported
        hvac_ventilator._parameter_support.add(TEST_PARAM_ID)
        assert hvac_ventilator.is_parameter_supported(TEST_PARAM_ID)

        # Case insensitivity
        assert hvac_ventilator.is_parameter_supported(TEST_PARAM_ID.lower())

        # Invalid parameter ID
        assert not hvac_ventilator.is_parameter_supported("")

    def test_get_supported_parameters(self, hvac_ventilator):
        """Test getting all supported parameters."""
        # Initially empty
        assert hvac_ventilator.get_supported_parameters() == set()

        # Add some supported parameters
        test_params = {"3F", "40", "41"}
        hvac_ventilator._parameter_support.update(test_params)

        # Check that all parameters are returned
        assert hvac_ventilator.get_supported_parameters() == test_params

    @patch("ramses_rf.device.hvac.Command.get_fan_param")
    async def test_setup_discovery_cmds(self, mock_cmd, hvac_ventilator):
        """Test that discovery commands are set up correctly."""
        # Mock the command creation
        mock_cmd.return_value = "MOCK_CMD"

        # Call the method
        hvac_ventilator._setup_discovery_cmds()

        # Check that the parameter discovery command was added
        mock_cmd.assert_called_once_with(hvac_ventilator.id, "00")
        hvac_ventilator._add_discovery_cmd.assert_called()

        # Check that the standard FAN discovery commands were added
        assert any(
            call[0][1] == 300  # interval=300s (5 minutes)
            for call in hvac_ventilator._add_discovery_cmd.call_args_list
        )
        assert any(
            call[0][1] == 3600  # interval=3600s (1 hour) for parameter discovery
            for call in hvac_ventilator._add_discovery_cmd.call_args_list
        )

    async def test_handle_msg_parameter_message(self, hvac_ventilator):
        """Test that parameter messages are handled correctly."""
        # Create a mock message
        msg = MagicMock()
        msg.code = Code._2411
        msg.payload = {
            "parameter_id": TEST_PARAM_ID,
            "value": TEST_PARAM_VALUE,
        }

        # Mock the parameter handler
        with patch.object(hvac_ventilator, "_handle_parameter_msg") as mock_handler:
            # Process the message
            hvac_ventilator._handle_msg(msg)

            # Check that the handler was called
            mock_handler.assert_called_once_with(msg)

    async def test_handle_msg_non_parameter_message(self, hvac_ventilator):
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
