#!/usr/bin/env python3
"""Unittests for the HvacVentilator class."""

from collections.abc import Generator
from enum import Enum
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ramses_rf import exceptions as exc
from ramses_rf.const import DevType
from ramses_rf.devices import HvacVentilator
from ramses_rf.gateway import Gateway
from ramses_rf.models.state_base import DeviceTraits
from ramses_rf.models.state_hvac import HvacState
from ramses_rf.state import MessageStore
from ramses_tx import Address
from ramses_tx.const import Code
from ramses_tx.typing import DeviceIdT

# Test data
TEST_DEVICE_ID = "32:123456"
TEST_PARAM_ID = "3F"
TEST_PARAM_VALUE = 50
TEST_MSG_ID = "1234"
TEST_BOUND_DEVICE_ID = "37:123456"
TEST_BOUND_DEVICE_TYPE = DevType.REM


@pytest.fixture
def mock_gateway() -> Generator[MagicMock, None, None]:
    """Create a mock Gateway instance for testing.

    :return: A generator yielding the mock Gateway.
    :rtype: Generator[MagicMock, None, None]
    """
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
    # Add msg_db attribute accessed by the message store
    gateway.message_store = MessageStore(maintain=False)

    yield gateway


@pytest.fixture
def hvac_ventilator(mock_gateway: MagicMock) -> HvacVentilator:
    """Create an HvacVentilator instance for testing.

    :param mock_gateway: The mocked Gateway fixture.
    :type mock_gateway: MagicMock
    :return: An initialized HvacVentilator.
    :rtype: HvacVentilator
    """
    device_id = DeviceIdT(TEST_DEVICE_ID)
    return HvacVentilator(mock_gateway, Address(device_id))


class TestHvacVentilator:
    """Test HvacVentilator class."""

    def test_initialization(self, hvac_ventilator: HvacVentilator) -> None:
        """Test that the ventilator initializes correctly.

        :param hvac_ventilator: The HvacVentilator fixture.
        :type hvac_ventilator: HvacVentilator
        """
        assert hvac_ventilator._supports_2411 is False
        assert hvac_ventilator._initialized_callback is None
        assert hvac_ventilator._param_update_callback is None
        assert hvac_ventilator._hgi is None
        assert hvac_ventilator._bound_devices == {}

        if hvac_ventilator._gwy.message_store:
            hvac_ventilator._gwy.message_store.stop()  # close sqlite3 connection

    def test_set_initialized_callback_clear(
        self, hvac_ventilator: HvacVentilator
    ) -> None:
        """Test clearing the initialized callback.

        :param hvac_ventilator: The HvacVentilator fixture.
        :type hvac_ventilator: HvacVentilator
        """
        # Set a callback first
        mock_callback = MagicMock()
        hvac_ventilator.set_initialized_callback(mock_callback)

        # Now clear it
        hvac_ventilator.set_initialized_callback(None)
        assert hvac_ventilator._initialized_callback is None

        if hvac_ventilator._gwy.message_store:
            hvac_ventilator._gwy.message_store.stop()  # close sqlite3 connection

    def test_set_initialized_callback_set(
        self, hvac_ventilator: HvacVentilator
    ) -> None:
        """Test setting the initialized callback.

        :param hvac_ventilator: The HvacVentilator fixture.
        :type hvac_ventilator: HvacVentilator
        """
        # Test initial state
        assert hvac_ventilator._initialized_callback is None

        if hvac_ventilator._gwy.message_store:
            hvac_ventilator._gwy.message_store.stop()  # close sqlite3 connection

        # Set the callback
        mock_callback = MagicMock()
        hvac_ventilator.set_initialized_callback(mock_callback)

        assert hvac_ventilator._initialized_callback is mock_callback

    def test_set_param_update_callback(self, hvac_ventilator: HvacVentilator) -> None:
        """Test setting the parameter update callback.

        :param hvac_ventilator: The HvacVentilator fixture.
        :type hvac_ventilator: HvacVentilator
        """
        if hvac_ventilator._gwy.message_store:
            hvac_ventilator._gwy.message_store.stop()  # close sqlite3 connection

        # Define a mock callback
        mock_callback = MagicMock()

        # Set the callback
        hvac_ventilator.set_param_update_callback(mock_callback)

        # Check that the callback was set
        assert hvac_ventilator._param_update_callback is mock_callback

    def test_handle_2411_message(self, hvac_ventilator: HvacVentilator) -> None:
        """Test handling a 2411 message.

        :param hvac_ventilator: The HvacVentilator fixture.
        :type hvac_ventilator: HvacVentilator
        """
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

        if hvac_ventilator._gwy.message_store:
            hvac_ventilator._gwy.message_store.stop()  # close sqlite3 connection

    async def test_setup_discovery_cmds(self, hvac_ventilator: HvacVentilator) -> None:
        """Test that discovery commands are set up correctly.

        :param hvac_ventilator: The HvacVentilator fixture.
        :type hvac_ventilator: HvacVentilator
        """

        # Use patch.object to properly mock the discovery service component directly
        # Phase 4 Update: we now call self.discovery.add_cmd instead of the bridge method
        with patch.object(hvac_ventilator.discovery, "add_cmd") as mock_add_cmd:
            hvac_ventilator._setup_discovery_cmds()

            # Check that add_cmd was called at least once
            assert mock_add_cmd.called

        if hvac_ventilator._gwy.message_store:
            hvac_ventilator._gwy.message_store.stop()  # close sqlite3 connection

        if hvac_ventilator._gwy.message_store:
            hvac_ventilator._gwy.message_store.stop()  # close sqlite3 connection

    def test_add_bound_device(
        self, hvac_ventilator: HvacVentilator, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test adding a bound device.

        :param hvac_ventilator: The HvacVentilator fixture.
        :type hvac_ventilator: HvacVentilator
        :param caplog: The pytest log capture fixture.
        :type caplog: pytest.LogCaptureFixture
        """
        # Ensure the logger is at the right level to capture the warning
        import logging

        logger = logging.getLogger("ramses_rf.devices")
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
        with caplog.at_level(logging.WARNING, logger="ramses_rf.devices"):
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

        if hvac_ventilator._gwy.message_store:
            hvac_ventilator._gwy.message_store.stop()  # close sqlite3 connection

    def test_remove_bound_device(self, hvac_ventilator: HvacVentilator) -> None:
        """Test removing a bound device.

        :param hvac_ventilator: The HvacVentilator fixture.
        :type hvac_ventilator: HvacVentilator
        """
        # Add then remove a device
        hvac_ventilator.add_bound_device(TEST_BOUND_DEVICE_ID, TEST_BOUND_DEVICE_TYPE)
        hvac_ventilator.remove_bound_device(TEST_BOUND_DEVICE_ID)

        # Verify it was removed
        assert TEST_BOUND_DEVICE_ID not in hvac_ventilator._bound_devices

        # Removing non-existent device should not raise
        hvac_ventilator.remove_bound_device("nonexistent:device")

        if hvac_ventilator._gwy.message_store:
            hvac_ventilator._gwy.message_store.stop()  # close sqlite3 connection

    def test_get_bound_rem(self, hvac_ventilator: HvacVentilator) -> None:
        """Test getting a bound REM device.

        :param hvac_ventilator: The HvacVentilator fixture.
        :type hvac_ventilator: HvacVentilator
        """
        # Initially should return None
        assert hvac_ventilator.get_bound_rem() is None

        # Add a REM device
        hvac_ventilator.add_bound_device(TEST_BOUND_DEVICE_ID, DevType.REM)

        # Should return the REM device
        assert hvac_ventilator.get_bound_rem() == TEST_BOUND_DEVICE_ID

        # Add a DIS device, should still return the REM device
        hvac_ventilator.add_bound_device("38:123456", DevType.DIS)
        assert hvac_ventilator.get_bound_rem() == TEST_BOUND_DEVICE_ID

        if hvac_ventilator._gwy.message_store:
            hvac_ventilator._gwy.message_store.stop()  # close sqlite3 connection

    def test_get_fan_param_supported(self, hvac_ventilator: HvacVentilator) -> None:
        """Test getting a supported fan parameter.

        :param hvac_ventilator: The HvacVentilator fixture.
        :type hvac_ventilator: HvacVentilator
        """
        # Set up the parameter in the device's parameter store
        hvac_ventilator._params_2411[TEST_PARAM_ID] = TEST_PARAM_VALUE

        # Mark as supporting 2411
        hvac_ventilator._supports_2411 = True

        # Test getting the parameter
        value = hvac_ventilator.get_fan_param(TEST_PARAM_ID)
        assert value == TEST_PARAM_VALUE

        if hvac_ventilator._gwy.message_store:
            hvac_ventilator._gwy.message_store.stop()  # close sqlite3 connection

    def test_get_fan_param_unsupported(
        self, hvac_ventilator: HvacVentilator, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test getting a parameter when 2411 is not supported.

        :param hvac_ventilator: The HvacVentilator fixture.
        :type hvac_ventilator: HvacVentilator
        :param caplog: The pytest log capture fixture.
        :type caplog: pytest.LogCaptureFixture
        """
        # Ensure 2411 is not supported and clear any existing messages
        hvac_ventilator._supports_2411 = False

        # Test getting a parameter
        caplog.clear()
        value = hvac_ventilator.get_fan_param(TEST_PARAM_ID)
        assert value is None

        if hvac_ventilator._gwy.message_store:
            hvac_ventilator._gwy.message_store.stop()  # close sqlite3 connection

    def test_get_fan_param_normalization(self, hvac_ventilator: HvacVentilator) -> None:
        """Test parameter ID normalization.

        :param hvac_ventilator: The HvacVentilator fixture.
        :type hvac_ventilator: HvacVentilator
        """
        # Set up the parameter with leading zeros in the parameter store
        # The get_fan_param method normalizes "03F" to "3F"
        hvac_ventilator._params_2411["3F"] = 75
        hvac_ventilator._supports_2411 = True

        # Test with different formats of the same parameter ID
        assert hvac_ventilator.get_fan_param("03F") == 75
        assert hvac_ventilator.get_fan_param("3F") == 75
        assert hvac_ventilator.get_fan_param("0003F") == 75

        if hvac_ventilator._gwy.message_store:
            hvac_ventilator._gwy.message_store.stop()  # close sqlite3 connection

    def test_initialized_callback(self, hvac_ventilator: HvacVentilator) -> None:
        """Test the initialized callback behaviour.

        :param hvac_ventilator: The HvacVentilator fixture.
        :type hvac_ventilator: HvacVentilator
        """
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

        if hvac_ventilator._gwy.message_store:
            hvac_ventilator._gwy.message_store.stop()  # close sqlite3 connection

    def test_hgi_property(
        self, hvac_ventilator: HvacVentilator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test the hgi property and its caching behaviour.

        :param hvac_ventilator: The HvacVentilator fixture.
        :type hvac_ventilator: HvacVentilator
        :param monkeypatch: The pytest monkeypatch fixture.
        :type monkeypatch: pytest.MonkeyPatch
        """
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
        if hvac_ventilator._gwy.message_store:
            hvac_ventilator._gwy.message_store.stop()  # close sqlite3 connection

        # Now it should get the new value
        assert hvac_ventilator.hgi is new_hgi
        assert hvac_ventilator._hgi is new_hgi  # Check the cache was updated

    def test_invalid_message_handling(self, hvac_ventilator: HvacVentilator) -> None:
        """Test handling of invalid messages.

        :param hvac_ventilator: The HvacVentilator fixture.
        :type hvac_ventilator: HvacVentilator
        """
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

        if hvac_ventilator._gwy.message_store:
            hvac_ventilator._gwy.message_store.stop()  # close sqlite3 connection

    def test_missing_callback(self, hvac_ventilator: HvacVentilator) -> None:
        """Test behaviour when callbacks are not set.

        :param hvac_ventilator: The HvacVentilator fixture.
        :type hvac_ventilator: HvacVentilator
        """
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

        if hvac_ventilator._gwy.message_store:
            hvac_ventilator._gwy.message_store.stop()  # close sqlite3 connection

    def test_2411_param_01_data_type_20(self, hvac_ventilator: HvacVentilator) -> None:
        """Test parsing of 2411 parameter 01 with data type 20 (GitHub issue #342).

        :param hvac_ventilator: The HvacVentilator fixture.
        :type hvac_ventilator: HvacVentilator
        """
        msg = MagicMock()
        msg.code = Code._2411
        msg.verb = "RP"
        msg.src = MagicMock()
        msg.src.id = TEST_DEVICE_ID
        msg.dst = MagicMock()
        msg.dst.id = TEST_DEVICE_ID
        msg.payload = {
            "parameter": "01",
            "description": "Support",
            "value": 3307,
            "_value_06": "0020",
            "min_value": 0,
            "max_value": 65535,
            "precision": 1,
            "_value_42": "B500",
        }

        hvac_ventilator._handle_2411_message(msg)

        assert hvac_ventilator.supports_2411
        stored_value = hvac_ventilator.get_fan_param("1")
        assert stored_value == 3307

        if hvac_ventilator._gwy.message_store:
            hvac_ventilator._gwy.message_store.stop()

    def test_2411_param_3e_data_type_90(self, hvac_ventilator: HvacVentilator) -> None:
        """Test parsing of 2411 parameter 3E with data type 90 (GitHub issue #317).

        :param hvac_ventilator: The HvacVentilator fixture.
        :type hvac_ventilator: HvacVentilator
        """
        msg = MagicMock()
        msg.code = Code._2411
        msg.verb = "RP"
        msg.src = MagicMock()
        msg.src.id = TEST_DEVICE_ID
        msg.dst = MagicMock()
        msg.dst.id = TEST_DEVICE_ID
        msg.payload = {
            "parameter": "3E",
            "description": "Away mode Exhaust fan rate (%)",
            "value": 800,
            "_value_06": "7690",
            "min_value": 0,
            "max_value": 2000,
            "precision": 1,
            "_value_42": "8A33",
        }

        hvac_ventilator._handle_2411_message(msg)

        assert hvac_ventilator.supports_2411
        stored_value = hvac_ventilator.get_fan_param("3E")
        assert stored_value == 800

        if hvac_ventilator._gwy.message_store:
            hvac_ventilator._gwy.message_store.stop()


async def test_set_fan_mode_with_bound_rem() -> None:
    """Test set_fan_mode uses the bound REM as the source ID."""
    dev = MagicMock(spec=HvacVentilator)
    dev.id = "32:123456"
    dev._scheme = "orcon"
    dev.get_bound_rem.return_value = "37:654321"

    dev._gwy = MagicMock()
    dev._gwy.dispatcher.send = AsyncMock(return_value="mock_packet")

    # Call the unbound method passing our mock as 'self'
    result = await HvacVentilator.set_fan_mode(dev, "low")

    # 1. Verify it checked for a bound remote
    dev.get_bound_rem.assert_called_once()

    # 2. Verify the intent was transmitted with the correct QoS
    dev._gwy.dispatcher.send.assert_awaited_once()
    intent = dev._gwy.dispatcher.send.await_args[0][0]

    from ramses_rf.enums import Action

    assert intent.action == Action.SET_FAN_MODE
    assert intent.src.id == "37:654321"
    assert intent.dst.id == "32:123456"
    assert intent.data == {"fan_mode": "low", "scheme": "orcon"}
    assert result == "mock_packet"


async def test_set_fan_mode_with_hgi_fallback() -> None:
    """Test set_fan_mode falls back to the HGI if no REM is bound."""
    dev = MagicMock(spec=HvacVentilator)
    dev.id = "32:123456"
    dev._scheme = "orcon"
    dev.get_bound_rem.return_value = None

    # Simulate an available HGI
    dev.hgi = MagicMock()
    dev.hgi.id = "18:000730"

    dev._gwy = MagicMock()
    dev._gwy.dispatcher.send = AsyncMock(return_value="mock_packet")

    await HvacVentilator.set_fan_mode(dev, "high")

    # Verify the intent was built using the HGI's ID ("18:000730")
    dev._gwy.dispatcher.send.assert_awaited_once()
    intent = dev._gwy.dispatcher.send.await_args[0][0]

    from ramses_rf.enums import Action

    assert intent.action == Action.SET_FAN_MODE
    assert intent.src.id == "18:000730"
    assert intent.dst.id == "32:123456"
    assert intent.data == {"fan_mode": "high", "scheme": "orcon"}


async def test_set_fan_mode_no_src_id_raises() -> None:
    """Test set_fan_mode raises CommandInvalid if no src_id can be determined."""
    dev = MagicMock(spec=HvacVentilator)
    dev.id = "32:123456"
    dev.get_bound_rem.return_value = None

    # Simulate NO available HGI (e.g. gateway not fully initialized)
    dev.hgi = None

    # Verify it raises the expected custom library exception
    with pytest.raises(
        exc.CommandInvalid, match="Cannot set fan mode without a bound REM or HGI"
    ):
        await HvacVentilator.set_fan_mode(dev, "auto")


class MockEnum(Enum):
    """Mock enum to simulate boundary leakage."""

    TEST_VAL = "active_fault"


def test_device_traits_to_dict_unboxes_enums() -> None:
    """Ensure DeviceTraits serialises Enums to raw strings for legacy APIs."""

    # Arrange
    traits = DeviceTraits(
        device_class=cast(str, MockEnum.TEST_VAL),
        scheme=cast(str, MockEnum.TEST_VAL),
    )

    # Act
    result = traits.to_dict()

    # Assert
    assert result["class"] == "active_fault"
    assert result["scheme"] == "active_fault"
    assert not isinstance(result["class"], Enum)


@pytest.mark.asyncio
async def test_hvac_ventilator_status_dictionary_shim() -> None:
    """Ensure the status dictionary explicitly maintains legacy keys."""

    # Arrange
    mock_gwy = MagicMock()
    mock_gwy.config.disable_discovery = True

    ventilator = HvacVentilator(mock_gwy, Address("32:123456"))
    ventilator.hvac_state = HvacState(
        indoor_temp=21.5,
        fan_mode=cast(str, MockEnum.TEST_VAL),
    )

    # Act
    status_dict = await ventilator.status()

    # Assert
    assert "temperature" in status_dict
    assert status_dict["temperature"] == 21.5

    # Bug A: indoor_temp is a primary key that Home Assistant expects.
    # It must not be dropped when mapping to 'temperature'.
    assert "indoor_temp" in status_dict
    assert status_dict["indoor_temp"] == 21.5

    assert status_dict["fan_mode"] == "active_fault"
    assert not isinstance(status_dict["fan_mode"], Enum)


@pytest.mark.asyncio
async def test_hvac_ventilator_status_shim_unboxes_list_of_enums() -> None:
    """Ensure the status shim safely unboxes lists containing Enums."""

    # Arrange
    mock_gwy = MagicMock()
    mock_gwy.config.disable_discovery = True

    ventilator = HvacVentilator(mock_gwy, Address("32:123456"))
    ventilator.hvac_state = HvacState(
        speed_capabilities=cast(list[str], [MockEnum.TEST_VAL, MockEnum.TEST_VAL])
    )

    # Act
    status_dict = await ventilator.status()

    # Assert
    assert "speed_capabilities" in status_dict
    capabilities = status_dict["speed_capabilities"]

    # Bug B: speed_capabilities is a list of Enums. The duck-typing getattr()
    # approach silently fails to unbox the items inside the list, causing JSON
    # serialisation failures downstream.
    assert isinstance(capabilities, list)
    assert all(not isinstance(item, Enum) for item in capabilities)
    assert capabilities[0] == "active_fault"
