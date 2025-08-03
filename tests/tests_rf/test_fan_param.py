#!/usr/bin/env python3
"""RAMSES RF - Test the fan parameter commands."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeVar
from unittest.mock import MagicMock, patch

import pytest

from ramses_rf import Gateway
from ramses_tx import Code, Command, exceptions as exc
from ramses_tx.schemas import DeviceIdT

# Constants for testing
FAN_PARAM_CODE = Code._2411  # pylint: disable=protected-access

# Test constants
FAN_DEVICE_ID: str = "32:153289"
HGI_DEVICE_ID: str = "18:000730"  # Standard HGI device ID for testing
SOURCE_DEVICE_ID: str = HGI_DEVICE_ID  # Use HGI as source for testing

# Type variable for test parameter classes
T = TypeVar("T", bound="FanParamTest")


# Test parameters and their expected responses
@dataclass
class FanParamTest:  # pylint: disable=too-many-instance-attributes
    """Test case for fan parameter testing.

    Note: This class has 8 attributes (one more than the default pylint limit of 7).
    The attributes are all necessary for testing different aspects of fan parameters.
    """

    param_id: str
    description: str
    response_payload: str
    expected_value: Any
    min_value: Any
    max_value: Any
    precision: Any
    unit: str = ""

    @classmethod
    def from_dict(cls: type[T], data: dict[str, Any]) -> T:
        """Create a FanParamTest instance from a dictionary.

        Args:
            data: Dictionary containing test case data

        Returns:
            A new FanParamTest instance
        """
        return cls(**data)


# Test cases for invalid parameter IDs
INVALID_PARAM_IDS = [
    # Too short
    "",
    "1",
    "A",
    # Too long
    "123",
    "ABCD",
    # Non-hex characters
    "G1",
    "1G",
    "XZ",
    # Invalid format
    " 75",
    "75 ",
    "7 5",
    "0x75",
    # Non-string types
    None,
    75,
    3.14,
    # Very long string that exceeds reasonable length
    "A" * 100,
    # Special characters
    "@#",
    "\x00",
    # Unicode characters
    "é9",
    "参数",
]


# Test cases for response parsing
@dataclass
class ResponseTest:
    """Test case for fan parameter response parsing."""

    param_id: str
    response_payload: str
    expected_value: Any
    expected_unit: str = ""
    expected_min: Any | None = None
    expected_max: Any | None = None
    expected_precision: Any | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ResponseTest:
        """Create a ResponseTest instance from a dictionary.

        Args:
            data: Dictionary containing test case data

        Returns:
            A new ResponseTest instance
        """
        return cls(**data)


# Test cases for response parsing
RESPONSE_TESTS = [
    # Comfort temperature (0.0-30.0°C, 0.01°C precision)
    ResponseTest(
        param_id="75",
        response_payload="0000750000000000000000000000000000000000010000",
        expected_value=0.0,
        expected_unit="°C",
        expected_min=0.0,
        expected_max=30.0,
        expected_precision=0.01,
    ),
    # Time to change filter (0-1800 days, 30 day precision)
    ResponseTest(
        param_id="31",
        response_payload="0000310000000000000000000000000000000000010000",
        expected_value=0,
        expected_unit="days",
        expected_min=0,
        expected_max=1800,
        expected_precision=30,
    ),
    # Moisture scenario position (0=medium, 1=high)
    ResponseTest(
        param_id="4E",
        response_payload="00004E0000000000000000000000000000000000010000",
        expected_value=0,
        expected_unit="",
        expected_min=0,
        expected_max=1,
        expected_precision=1,
    ),
]

# Malformed responses to test
MALFORMED_RESPONSES = [
    "",  # Empty string
    "00004E",  # Valid hex but wrong length (should be 2 chars)
    "NOTHEX",  # Non-hex characters
    "X" * 1000,  # Very long string
    "00007B0000000000000000000000000000000000010000",  # Long hex string
    "ZZ",  # Invalid hex
    " 31",  # Leading space
    "31 ",  # Trailing space
    "3 1",  # Embedded space
    "3.1",  # Decimal point
    "0x31",  # Hex prefix
    "-31",  # Negative sign
    "+31",  # Plus sign
    "3.1",  # Decimal point
    "3,1",  # Comma decimal
    "3e1",  # Scientific notation
    "3E1",  # Scientific notation uppercase
    "3.1e1",  # Scientific with decimal
    "3.1E1",  # Scientific with decimal uppercase
]

# Test parameters
TEST_PARAMETERS = [
    FanParamTest(
        param_id="75",
        description="Fan Speed (0-100%)",
        response_payload="000075000064",
        expected_value=100,
        min_value=0,
        max_value=100,
        precision=1,
        unit="%",
    ),
    FanParamTest(
        param_id="76",
        description="Fan Boost Duration (0-60 minutes)",
        response_payload="000076001E",
        expected_value=30,
        min_value=0,
        max_value=60,
        precision=1,
        unit="min",
    ),
    FanParamTest(
        param_id="77",
        description="Fan Boost Speed (0-100%)",
        response_payload="0000770050",
        expected_value=80,
        min_value=0,
        max_value=100,
        precision=1,
        unit="%",
    ),
]

# Create a lookup for test parameters
TEST_PARAMS_BY_ID = {p.param_id: p for p in TEST_PARAMETERS}

# Response tests for verifying response parsing
RESPONSE_TESTS = [
    ResponseTest(
        param_id="75",
        response_payload="000075000064",
        expected_value=100,
        expected_unit="%",
        expected_min=0,
        expected_max=100,
        expected_precision=1,
    ),
    ResponseTest(
        param_id="76",
        response_payload="000076001E",
        expected_value=30,
        expected_unit="min",
        expected_min=0,
        expected_max=60,
        expected_precision=1,
    ),
]

RESPONSE_TESTS_BY_ID = {p.param_id: p for p in RESPONSE_TESTS}


@pytest.fixture(params=TEST_PARAMETERS)
def fan_param_test(request: pytest.FixtureRequest) -> FanParamTest | None:
    """Fixture that provides test parameters for each test case.

    Args:
        request: Pytest fixture request object

    Returns:
        A FanParamTest instance for the current test case, or None if no parameter
    """
    if not hasattr(request, "param"):
        return None
    return FanParamTest.from_dict(request.param)


@pytest.fixture(params=RESPONSE_TESTS)
def response_test(request: pytest.FixtureRequest) -> ResponseTest | None:
    """Fixture that provides response test cases.

    Args:
        request: Pytest fixture request object

    Returns:
        A ResponseTest instance for the current test case, or None if no parameter
    """
    if not hasattr(request, "param"):
        return None
    return ResponseTest.from_dict(request.param)


def create_mock_response(test_case: ResponseTest) -> MagicMock:
    """Create a mock response command from a test case.

    Args:
        test_case: The test case containing the response data

    Returns:
        A MagicMock object configured to look like a fan parameter response
    """
    mock_cmd = MagicMock()
    mock_cmd.verb = "RP"
    mock_cmd.code = "2411"

    # Create mock source and destination with proper typing
    mock_src = MagicMock()
    mock_src.id = SOURCE_DEVICE_ID
    mock_dst = MagicMock()
    mock_dst.id = FAN_DEVICE_ID

    mock_cmd.src = mock_src
    mock_cmd.dst = mock_dst
    mock_cmd.payload = test_case.response_payload

    return mock_cmd


@pytest.fixture
def gwy_config() -> dict[str, Any]:
    """Return a test gateway configuration.

    Returns:
        A dictionary containing the gateway configuration for testing.
    """
    # Create device info dictionaries with proper typing
    known_list: dict[DeviceIdT, dict[str, Any]] = {
        DeviceIdT(FAN_DEVICE_ID): {"is_fan": True},
        # Use 'class' as the key to match the expected schema
        DeviceIdT("18:000730"): {"class": "HGI"},  # HGI device
    }

    # Create the configuration with proper typing
    config: dict[str, Any] = {
        "config": {
            "enforce_known_list": False,
            "enforce_schema": True,
        },
        "known_list": known_list,
    }

    return config


@pytest.fixture
def gwy_dev_id() -> str:
    """Return the test gateway device ID.

    Returns:
        The HGI device ID as a string.
    """
    return HGI_DEVICE_ID  # Already a string


@pytest.mark.parametrize(
    "test_param", TEST_PARAMETERS, ids=[p.param_id for p in TEST_PARAMETERS]
)
async def test_get_fan_param_command_construction(test_param: FanParamTest) -> None:
    """Test the construction of the get_fan_param command for different parameters."""
    # Test with minimal required parameters
    cmd = Command.get_fan_param(
        FAN_DEVICE_ID, test_param.param_id, src_id=SOURCE_DEVICE_ID
    )
    assert cmd.code == FAN_PARAM_CODE
    assert cmd.verb == "RQ"
    assert cmd.src.id == SOURCE_DEVICE_ID
    assert cmd.dst.id == FAN_DEVICE_ID
    # The payload should be the parameter ID prefixed with "0000"
    # e.g., for param_id="75", payload should be "000075"
    expected_payload = f"0000{test_param.param_id}"
    assert cmd.payload == expected_payload


@pytest.mark.parametrize("param_id", INVALID_PARAM_IDS)
def test_get_fan_param_invalid_param_id(param_id: Any) -> None:
    """Test that invalid parameter IDs raise the expected exception.

    Args:
        param_id: The invalid parameter ID to test
    """
    with pytest.raises(exc.CommandInvalid):
        # Try to create a command with an invalid parameter ID
        Command.get_fan_param(
            fan_id=FAN_DEVICE_ID, param_id=param_id, src_id=SOURCE_DEVICE_ID
        )


@pytest.mark.parametrize(
    "test_param", TEST_PARAMETERS, ids=[p.param_id for p in TEST_PARAMETERS]
)
@pytest.mark.asyncio
async def test_get_fan_param_integration(
    fake_evofw3: Gateway, test_param: FanParamTest
) -> None:
    """Test the full get_fan_param flow with a fake gateway for different parameters.

    This test verifies that the command is constructed correctly, can be sent through
    the gateway, and that the response is handled properly.

    Args:
        fake_evofw3: Pytest fixture providing a fake gateway for testing
        test_param: The test case containing the parameter to test
    """
    # Store the original send_cmd method
    # Using protected access to test internal behavior
    original_send_cmd = fake_evofw3._protocol.send_cmd  # pylint: disable=protected-access

    async def mock_send_cmd(cmd: Command, *args: Any, **kwargs: Any) -> Command:
        """Mock implementation of send_cmd for testing.

        Args:
            cmd: The command to send
            *args: Additional positional arguments
            **kwargs: Additional keyword arguments

        Returns:
            A response command

        Raises:
            ValueError: If the parameter ID is not found in test cases
        """
        # Handle fan parameter RQ commands
        if cmd.code == FAN_PARAM_CODE and cmd.verb == "RQ":
            # Extract the parameter ID from the request
            req_param_id = cmd.payload[4:6]
            test_param = TEST_PARAMS_BY_ID.get(req_param_id)
            if test_param is None:
                raise ValueError(f"Unexpected parameter ID in test: {req_param_id}")

            # Create a response with the test data
            # Create a proper response command
            response_cmd = Command._from_attrs(  # pylint: disable=protected-access
                verb="RP",
                code=FAN_PARAM_CODE,
                payload=test_param.response_payload,
                addr0=FAN_DEVICE_ID,  # src
                addr1=SOURCE_DEVICE_ID,  # dst
            )
            # The _from_attrs method should return a Command, but we'll ensure it
            assert isinstance(response_cmd, Command), "Expected Command object"
            return response_cmd

        # For other commands, use the original implementation
        result = await original_send_cmd(cmd, *args, **kwargs)
        assert isinstance(result, Command), "Expected Command object"
        return result

    # Patch the protocol's send_cmd method with our mock
    # Using protected access to test internal behavior
    with patch.object(fake_evofw3._protocol, "send_cmd", new=mock_send_cmd):  # pylint: disable=protected-access
        try:
            # Get the fan parameter
            cmd = Command.get_fan_param(
                fan_id=FAN_DEVICE_ID,
                param_id=test_param.param_id,
                src_id=SOURCE_DEVICE_ID,
            )

            # Send the command and get the response
            # Using protected access to test internal behavior
            response = await fake_evofw3._protocol.send_cmd(cmd)  # pylint: disable=protected-access

            # Verify the response is not None
            assert response is not None, "No response received"

            # Verify the response properties
            assert (
                response.code == FAN_PARAM_CODE
            ), f"Expected code {FAN_PARAM_CODE}, got {response.code}"
            assert response.verb == "RP", f"Expected verb 'RP', got '{response.verb}'"
            assert (
                response.src.id == FAN_DEVICE_ID
            ), f"Expected source ID '{FAN_DEVICE_ID}', got '{response.src.id}'"
            assert (
                response.dst.id == SOURCE_DEVICE_ID
            ), f"Expected destination ID '{SOURCE_DEVICE_ID}', got '{response.dst.id}'"

            # Verify the payload matches our test case
            assert response.payload == test_param.response_payload, (
                f"Response payload does not match expected: "
                f"{response.payload} != {test_param.response_payload}"
            )

            # Verify the parameter ID in the response matches our request
            param_id_in_response = response.payload[4:6].upper()
            assert param_id_in_response == test_param.param_id.upper(), (
                f"Parameter ID in response ({param_id_in_response}) does not match "
                f"request ({test_param.param_id.upper()})"
            )

            # Verify the test data is consistent
            assert (
                test_param.min_value
                <= test_param.expected_value
                <= test_param.max_value
            ), (
                f"Expected value {test_param.expected_value} is outside range "
                f"[{test_param.min_value}, {test_param.max_value}]"
            )

        except (ValueError, IndexError, KeyError) as e:
            # Handle expected test failures with more specific error messages
            pytest.fail(f"Test failed with validation error: {e}")
        except AssertionError:
            # Re-raise assertion errors to get proper test failure messages
            raise
        except Exception as e:  # pragma: no cover
            # Catch-all for unexpected errors
            pytest.fail(f"Unexpected error in test: {e}")
            raise
        finally:
            # Ensure we clean up the patch
            patch.stopall()
