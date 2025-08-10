"""Tests for the DIS fan parameter methods."""

from __future__ import annotations

import asyncio
from typing import Any, cast, Dict, Optional, Type, TYPE_CHECKING

import pytest
from ramses_rf.device.hvac import HvacDisplayRemote
from ramses_rf.exceptions import DeviceNotRecognised, CommandInvalid
from ramses_rf.gateway import Gateway
from ramses_tx.command import Command
from ramses_tx.packet import Packet
from unittest.mock import (
    AsyncMock,
    MagicMock,
    patch,
    PropertyMock,
    call,
    create_autospec,
)

# Type checking imports
if TYPE_CHECKING:
    from ramses_tx import Priority

# Test data
VALID_DIS_ID = "01:123456"
VALID_FAN_ID = "32:123456"
VALID_PARAM_ID = "3F"
INVALID_FAN_ID = "invalid_id"
INVALID_PARAM_ID = "ZZ"


class TestHvacDisplayRemote:
    """Test the HvacDisplayRemote class with fan parameter methods."""

    @pytest.fixture
    def device(self) -> HvacDisplayRemote:
        """Create a real HvacDisplayRemote instance with mocked gateway."""
        # Create a real instance but with a mocked gateway
        mock_gateway = create_autospec(Gateway, instance=True)
        mock_device = MagicMock()
        mock_device.id = VALID_FAN_ID
        mock_gateway.get_device.return_value = mock_device

        # Create a real instance with proper initialization
        device = HvacDisplayRemote(mock_gateway, {"id": VALID_DIS_ID})

        # Set up mock send_cmd to return a command with a payload
        async def mock_send_cmd(cmd: Command, **kwargs: Any) -> Packet:
            # Create a response command with the expected payload
            response = MagicMock(spec=Command)
            response.code = "2411"
            response.payload = ""
            response.src = MagicMock()
            response.src.id = VALID_DIS_ID
            response.dst = MagicMock()
            response.dst.id = VALID_FAN_ID
            if cmd.code == "2411":
                if cmd.payload.startswith("0000"):  # Get parameter
                    response.payload = f"0000{VALID_PARAM_ID}000F5400000000000000000000000000000000000000"
                else:  # Set parameter
                    response.payload = f"0001{VALID_PARAM_ID}00000000000000000000000000000000000000000000"
            return cast(Packet, response)

        # Create a properly typed async mock
        mock_send_cmd_mock = AsyncMock(
            spec=mock_send_cmd,
            side_effect=mock_send_cmd,
        )

        # Assign the mock to the gateway
        mock_gateway.send_cmd = mock_send_cmd_mock

        # Set up device ID as a property
        type(device).id = PropertyMock(return_value=VALID_DIS_ID)

        return device

    async def test_get_fan_param_valid(self, device: HvacDisplayRemote) -> None:
        """Test get_fan_param with valid parameters."""
        # Call the method
        result = await device.get_fan_param(VALID_FAN_ID, VALID_PARAM_ID)

        # Verify the result is a command with the expected payload
        assert result is not None
        assert hasattr(result, "payload")
        assert VALID_PARAM_ID in result.payload  # Verify param ID is in the payload

        # Verify the command was sent correctly
        send_cmd_mock = device._gwy.send_cmd
        assert isinstance(send_cmd_mock, AsyncMock)
        send_cmd_mock.assert_awaited_once()

        # Get the command that was sent
        assert send_cmd_mock.await_args is not None
        cmd = send_cmd_mock.await_args[0][0]

        # Verify command properties
        assert cmd.code == "2411"
        assert VALID_PARAM_ID in cmd.payload
        assert cmd.dst.id == VALID_FAN_ID
        assert cmd.src.id == VALID_DIS_ID

    async def test_get_fan_param_invalid_fan_id(
        self, device: HvacDisplayRemote
    ) -> None:
        """Test get_fan_param with an invalid fan ID."""
        # Create a mock for the gateway's get_device method
        mock_gateway = MagicMock()
        mock_gateway.get_device.return_value = None

        # Patch the device's gateway with our mock
        with patch.object(device, "_gwy", mock_gateway):
            # Call the method and expect an exception
            with pytest.raises(DeviceNotRecognised):
                await device.get_fan_param(INVALID_FAN_ID, VALID_PARAM_ID)

    async def test_get_fan_param_invalid_param_id(
        self, device: HvacDisplayRemote
    ) -> None:
        """Test get_fan_param with an invalid parameter ID."""
        # Create a mock device
        mock_device = MagicMock()
        mock_device.id = VALID_FAN_ID

        # Create a mock for the gateway
        mock_gateway = MagicMock()
        mock_gateway.get_device.return_value = mock_device

        # Patch the device's gateway with our mock
        with patch.object(device, "_gwy", mock_gateway):
            # Call the method and expect an exception
            with pytest.raises(CommandInvalid):
                await device.get_fan_param(VALID_FAN_ID, INVALID_PARAM_ID)

    async def test_set_fan_param_valid(self, device: HvacDisplayRemote) -> None:
        """Test set_fan_param with valid parameters."""
        # Call the method
        result = await device.set_fan_param(VALID_FAN_ID, VALID_PARAM_ID, 42)

        # Verify the result is a command with the expected payload
        assert result is not None
        assert hasattr(result, "payload")
        assert VALID_PARAM_ID in result.payload  # Verify param ID is in the payload

        # Verify the command was sent correctly
        send_cmd_mock = device._gwy.send_cmd
        assert isinstance(send_cmd_mock, AsyncMock)
        send_cmd_mock.assert_awaited_once()

        # Get the command that was sent
        assert send_cmd_mock.await_args is not None
        cmd = send_cmd_mock.await_args[0][0]

        # Verify command properties
        assert cmd.code == "2411"
        assert VALID_PARAM_ID in cmd.payload
        assert cmd.dst.id == VALID_FAN_ID
        assert cmd.src.id == VALID_DIS_ID

    async def test_set_fan_param_invalid_fan_id(
        self, device: HvacDisplayRemote
    ) -> None:
        """Test set_fan_param with an invalid fan ID."""
        # Create a mock for the gateway
        mock_gateway = MagicMock()
        mock_gateway.get_device.return_value = None

        # Patch the device's gateway with our mock
        with patch.object(device, "_gwy", mock_gateway):
            # Call the method and expect an exception
            with pytest.raises(DeviceNotRecognised):
                await device.set_fan_param(INVALID_FAN_ID, VALID_PARAM_ID, 42)

    async def test_set_fan_param_invalid_param_id(
        self, device: HvacDisplayRemote
    ) -> None:
        """Test set_fan_param with an invalid parameter ID."""
        # Create a mock device
        mock_device = MagicMock()
        mock_device.id = VALID_FAN_ID

        # Create a mock for the gateway
        mock_gateway = MagicMock()
        mock_gateway.get_device.return_value = mock_device

        # Patch the device's gateway with our mock
        with patch.object(device, "_gwy", mock_gateway):
            # Call the method and expect an exception
            with pytest.raises(CommandInvalid):
                await device.set_fan_param(VALID_FAN_ID, INVALID_PARAM_ID, 42)

    @pytest.mark.parametrize(
        "value,expected_value_int",
        [
            (42, 42),
            (42.0, 42),
            (True, 1),
            (False, 0),
        ],
    )
    async def test_set_fan_param_value_types(
        self, device: HvacDisplayRemote, value: Any, expected_value_int: int
    ) -> None:
        """Test set_fan_param with different value types."""
        # Create a mock device
        mock_device = MagicMock()
        mock_device.id = VALID_FAN_ID

        # Mock the send_cmd to return a command with expected payload
        async def mock_send_cmd(cmd: Command, **kwargs: Any) -> Packet:
            response = MagicMock(spec=Command)
            response.code = "2411"
            response.payload = f"0001{VALID_PARAM_ID}{expected_value_int:04X}000000000000000000000000000000000000"
            response.src = MagicMock()
            response.src.id = VALID_DIS_ID
            response.dst = MagicMock()
            response.dst.id = VALID_FAN_ID
            return cast(Packet, response)

        # Create a mock for the gateway
        mock_gateway = MagicMock()
        mock_gateway.get_device.return_value = mock_device
        mock_gateway.send_cmd = AsyncMock(side_effect=mock_send_cmd)

        # Patch the device's gateway with our mock
        with patch.object(device, "_gwy", mock_gateway):
            # Call the method with different value types
            result = await device.set_fan_param(VALID_FAN_ID, VALID_PARAM_ID, value)

            # Verify the result
            assert result is not None
            assert hasattr(result, "payload")
            assert VALID_PARAM_ID in result.payload

            # Verify the command was sent with the correct value
            mock_gateway.send_cmd.assert_awaited_once()

            # Get the command that was sent
            cmd = mock_gateway.send_cmd.await_args[0][0]

            # Verify command properties
            assert cmd.code == "2411"
            assert VALID_PARAM_ID in cmd.payload
            assert cmd.dst.id == VALID_FAN_ID
            assert cmd.src.id == VALID_DIS_ID
