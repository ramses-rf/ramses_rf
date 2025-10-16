#!/usr/bin/env python3
"""Unittests for the HvacVentilator class."""

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ramses_rf.const import DevType
from ramses_rf.database import MessageIndex
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
TEST_BOUND_DEVICE_ID = "37:123456"
TEST_BOUND_DEVICE_TYPE = DevType.REM


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
    # Add msg_db attribute accessed by the message store, activates the SQLite MessageIndex
    gateway.msg_db = MessageIndex(maintain=False)

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
        # Create a mock message with all required attributes
        msg = MagicMock()
        msg.code = Code._2411
        # Create proper mock objects for src and dst with id attribute
        msg.src = MagicMock()
        msg.src.id = TEST_DEVICE_ID
        msg.dst = MagicMock()
        msg.dst.id = TEST_DEVICE_ID
        msg.verb = " I"
        msg.payload = {"parameter": TEST_PARAM_ID, "value": TEST_PARAM_VALUE}

        # Set up the message store
        hvac_ventilator._params_2411 = {}

        # Set up the param update callback
        mock_callback = MagicMock()
        hvac_ventilator.set_param_update_callback(mock_callback)

        # Call the method
        hvac_ventilator._handle_2411_message(msg)

        # Check that supports_2411 was set to True
        assert hvac_ventilator._supports_2411 is True

        # Check that the message was stored correctly
        assert TEST_PARAM_ID in hvac_ventilator._params_2411
        assert hvac_ventilator._params_2411[TEST_PARAM_ID] == TEST_PARAM_VALUE

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
            hvac_ventilator._setup_discovery_cmds()

            # Check that _add_discovery_cmd was called at least once
            assert mock_add_cmd.called

    async def test_handle_msg_parameter_message(
        self, hvac_ventilator: HvacVentilator
    ) -> None:
        """Test that parameter messages are handled correctly."""
        # Create a mock message with all required attributes
        msg = MagicMock()
        msg.code = Code._2411
        # Create proper mock objects for src and dst with id attribute
        msg.src = MagicMock()
        msg.src.id = TEST_DEVICE_ID
        msg.dst = MagicMock()
        msg.dst.id = TEST_DEVICE_ID
        msg.verb = " I"
        msg.payload = {
            "parameter": TEST_PARAM_ID,
            "value": TEST_PARAM_VALUE,
            "_hgi": MagicMock(),
        }

        # Set up the message store  # deprecated, TODO(eb): remove Q1 2026
        if not hvac_ventilator._gwy.msg_db:
            hvac_ventilator._msgs_ = {}

        # Patch the _handle_2411_message method
        with patch.object(hvac_ventilator, "_handle_2411_message") as mock_handle:
            # Call the method
            hvac_ventilator._handle_msg(msg)

            # Check that _handle_2411_message was called
            mock_handle.assert_called_once_with(msg)

    async def test_handle_msg_non_parameter_message(
        self, hvac_ventilator: HvacVentilator
    ) -> None:
        """Test that non-parameter messages are passed to the parent class."""
        # Create a mock message with a non-parameter code and required attributes
        msg = MagicMock()
        msg.code = Code._31DA  # Standard FAN status code
        # Create proper mock objects for src and dst with id attribute
        msg.src = MagicMock()
        msg.src.id = TEST_DEVICE_ID
        msg.dst = MagicMock()
        msg.dst.id = TEST_DEVICE_ID
        msg.verb = " I"
        msg.payload = {"some_key": "some_value"}

        # Set up the message store  # deprecated, TODO(eb): remove Q1 2026
        if not hvac_ventilator._gwy.msg_db:
            hvac_ventilator._msgs_ = {}

        # Patch the parent class's _handle_msg method
        with patch(
            "ramses_rf.device.hvac.FilterChange._handle_msg"
        ) as mock_parent_handle:
            # Call the method
            hvac_ventilator._handle_msg(msg)

            # Check that the parent's _handle_msg was called
            mock_parent_handle.assert_called_once_with(msg)

            # The parameter handler should not have been called
            assert not hasattr(hvac_ventilator, "_handle_parameter_msg")

    def test_add_bound_device(
        self, hvac_ventilator: HvacVentilator, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test adding a bound device."""
        # Ensure the logger is at the right level to capture the warning
        import logging

        logger = logging.getLogger("ramses_rf.device.hvac")
        logger.setLevel(logging.WARNING)

        # Clear any existing log handlers to avoid duplicates
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)

        # Add a bound device
        hvac_ventilator.add_bound_device(TEST_BOUND_DEVICE_ID, TEST_BOUND_DEVICE_TYPE)

        # Verify it was added
        assert TEST_BOUND_DEVICE_ID in hvac_ventilator._bound_devices
        assert (
            hvac_ventilator._bound_devices[TEST_BOUND_DEVICE_ID]
            == TEST_BOUND_DEVICE_TYPE
        )

        # Test with invalid device type - should log a warning but not raise
        invalid_device_id = "00:123456"

        # Clear the caplog and add a handler to capture the logs
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="ramses_rf.device.hvac"):
            hvac_ventilator.add_bound_device(invalid_device_id, "INVALID_TYPE")

        # Check if any record contains the expected message
        expected_message = f"Cannot bind device {invalid_device_id} of type INVALID_TYPE to FAN {hvac_ventilator.id}: must be REM or DIS"

        # Print debug info if the test fails
        if not any(expected_message in record.message for record in caplog.records):
            print("\nCaptured log records:")
            for i, record in enumerate(caplog.records):
                print(f"  {i}: {record.levelname}: {record.message}")

        # Check if the warning was logged
        assert any(expected_message in record.message for record in caplog.records), (
            f"Expected warning message not found in logs. Expected: {expected_message}"
        )

    def test_remove_bound_device(self, hvac_ventilator: HvacVentilator) -> None:
        """Test removing a bound device."""
        # Add then remove a device
        hvac_ventilator.add_bound_device(TEST_BOUND_DEVICE_ID, TEST_BOUND_DEVICE_TYPE)
        hvac_ventilator.remove_bound_device(TEST_BOUND_DEVICE_ID)

        # Verify it was removed
        assert TEST_BOUND_DEVICE_ID not in hvac_ventilator._bound_devices

        # Removing non-existent device should not raise
        hvac_ventilator.remove_bound_device("nonexistent:device")

    def test_get_bound_rem(self, hvac_ventilator: HvacVentilator) -> None:
        """Test getting a bound REM device."""
        # Initially should return None
        assert hvac_ventilator.get_bound_rem() is None

        # Add a REM device
        hvac_ventilator.add_bound_device(TEST_BOUND_DEVICE_ID, DevType.REM)

        # Should return the REM device
        assert hvac_ventilator.get_bound_rem() == TEST_BOUND_DEVICE_ID

        # Add a DIS device, should still return the REM device
        hvac_ventilator.add_bound_device("38:123456", DevType.DIS)
        assert hvac_ventilator.get_bound_rem() == TEST_BOUND_DEVICE_ID

    def test_get_fan_param_supported(self, hvac_ventilator: HvacVentilator) -> None:
        """Test getting a supported fan parameter."""
        # Set up the parameter in the device's parameter store
        hvac_ventilator._params_2411[TEST_PARAM_ID] = TEST_PARAM_VALUE

        # Mark as supporting 2411
        hvac_ventilator._supports_2411 = True

        # Test getting the parameter
        value = hvac_ventilator.get_fan_param(TEST_PARAM_ID)
        assert value == TEST_PARAM_VALUE

    def test_get_fan_param_unsupported(
        self, hvac_ventilator: HvacVentilator, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test getting a parameter when 2411 is not supported."""
        # Ensure 2411 is not supported and clear any existing messages
        hvac_ventilator._supports_2411 = False

        # Test getting a parameter
        caplog.clear()
        value = hvac_ventilator.get_fan_param(TEST_PARAM_ID)
        assert value is None

    def test_get_fan_param_normalization(self, hvac_ventilator: HvacVentilator) -> None:
        """Test parameter ID normalization."""
        # Set up the parameter with leading zeros in the parameter store
        # The get_fan_param method normalizes "03F" to "3F"
        hvac_ventilator._params_2411["3F"] = 75
        hvac_ventilator._supports_2411 = True

        # Test with different formats of the same parameter ID
        assert hvac_ventilator.get_fan_param("03F") == 75
        assert hvac_ventilator.get_fan_param("3F") == 75
        assert hvac_ventilator.get_fan_param("0003F") == 75

    def test_initialized_callback(self, hvac_ventilator: HvacVentilator) -> None:
        """Test the initialized callback behaviour."""
        # Set up a mock callback
        mock_callback = MagicMock()
        hvac_ventilator.set_initialized_callback(mock_callback)

        # Initially, the callback shouldn't be called
        mock_callback.assert_not_called()

        # Set supports_2411 to True and call _handle_initialized_callback
        hvac_ventilator._supports_2411 = True
        hvac_ventilator._handle_initialized_callback()

        # The callback should be called once
        mock_callback.assert_called_once()

        # The callback should be cleared after being called
        assert hvac_ventilator._initialized_callback is None

        # Calling again should not call the callback again
        hvac_ventilator._handle_initialized_callback()
        mock_callback.assert_called_once()  # Still only called once

    def test_hgi_property(
        self, hvac_ventilator: HvacVentilator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test the hgi property and its caching behaviour."""
        # Get the gateway's hgi
        gateway_hgi = hvac_ventilator._gwy.hgi

        # First call should get the value from the gateway
        assert hvac_ventilator.hgi is gateway_hgi

        # The gateway's hgi property should only be called once
        # (second access comes from the cache)
        assert hvac_ventilator.hgi is gateway_hgi
        assert hvac_ventilator._hgi is gateway_hgi  # Check the cache directly

        # Test the caching behaviour by creating a new mock for the gateway's hgi
        new_hgi = MagicMock()

        # Use monkeypatch to temporarily replace the hgi property
        monkeypatch.setattr(hvac_ventilator._gwy, "hgi", new_hgi)

        # The property still returns the original cached value
        assert hvac_ventilator.hgi is gateway_hgi
        assert hvac_ventilator.hgi is not new_hgi

        # Clear the cache
        hvac_ventilator._hgi = None

        # Now it should get the new value
        assert hvac_ventilator.hgi is new_hgi
        assert hvac_ventilator._hgi is new_hgi  # Check the cache was updated

    def test_invalid_message_handling(self, hvac_ventilator: HvacVentilator) -> None:
        """Test handling of invalid messages."""
        # Create an invalid message (missing payload)
        msg = MagicMock()
        msg.verb = " I"
        msg.src = MagicMock()
        msg.src.id = TEST_DEVICE_ID
        msg.dst = MagicMock()
        msg.dst.id = TEST_DEVICE_ID
        msg.payload = None  # Invalid payload

        # Set up a callback to verify it's not called
        mock_callback = MagicMock()
        hvac_ventilator.set_param_update_callback(mock_callback)

        # This should not raise an exception
        hvac_ventilator._handle_2411_message(msg)

        # No parameter update callback should be called
        mock_callback.assert_not_called()

    def test_missing_callback(self, hvac_ventilator: HvacVentilator) -> None:
        """Test behaviour when callbacks are not set."""
        # This should not raise an exception
        hvac_ventilator._handle_param_update("3F", 50)

        # And with a message that would trigger callbacks
        msg = MagicMock()
        msg.code = Code._2411
        msg.verb = " I"
        msg.src = MagicMock()
        msg.src.id = TEST_DEVICE_ID
        msg.dst = MagicMock()
        msg.dst.id = TEST_DEVICE_ID
        msg.payload = {"parameter": "3F", "value": 50}

        # This should not raise an exception
        hvac_ventilator._handle_2411_message(msg)

        # Now set a callback and verify it's called
        mock_callback = MagicMock()
        hvac_ventilator.set_param_update_callback(mock_callback)

        # Process the message again
        hvac_ventilator._handle_2411_message(msg)

        # Callback should be called with the parameter and value
        mock_callback.assert_called_once_with("3F", 50)
