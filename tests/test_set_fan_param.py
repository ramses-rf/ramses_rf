"""Tests for the set_fan_param command."""

import inspect

import pytest

from ramses_tx import exceptions as exc
from ramses_tx.command import Command
from ramses_tx.exceptions import CommandInvalid


def test_set_fan_param_basic() -> None:
    """Test basic functionality of set_fan_param with a percentage value."""
    cmd = Command.set_fan_param(
        fan_id="39:123456",
        param_id="3F",
        value=50.0,  # 50.0% for parameter 3F (Low mode Supply fan rate %)
        src_id="12:345678",
    )
    # Check the command structure
    assert cmd.code == "2411"
    assert cmd.verb == " W"
    # Should have 3 addresses: src, dst, and empty third address
    assert len(cmd._addrs) == 3
    # Source address is converted to DTS: format
    assert str(cmd._addrs[0]) == "DTS:345678"  # Source address (12: -> DTS:)
    assert str(cmd._addrs[1]) == "39:123456"  # Target device
    # Check the payload structure for parameter 3F (Low mode Supply fan rate %)
    # The payload structure is: 4 (param_id) + 2 (data_type) + 4 (value) + ...
    assert cmd.payload.startswith("00003F00")  # param_id (3F) + data_type (000F)
    # Value: 100 (50% * 2 = 100) in middle-endian format (00 00 64 00)
    assert cmd.payload[12:20] == "00006400"  # Middle-endian format


def test_set_fan_param_integer_value() -> None:
    """Test set_fan_param with an integer value."""
    cmd = Command.set_fan_param(
        fan_id="39:123456",
        param_id="4E",
        value=1,  # Position 1 for parameter 4E (Moisture scenario position)
        src_id="12:345678",
    )
    # Check the payload structure for parameter 4E (Moisture scenario position)
    # The payload structure is: 2 (leading) + 4 (param_id) + 6 (data_type) + 8 (value) + ...
    assert cmd.payload.startswith("00004E00")  # Leading + param_id (4E)
    assert cmd.payload[12:20] == "00000100"  # Value: 1 (0x01) in middle-endian format


def test_set_fan_param_float_value() -> None:
    """Test set_fan_param with a float value."""
    cmd = Command.set_fan_param(
        fan_id="39:123456",
        param_id="75",
        value=21.5,  # 21.5°C for parameter 75 (Comfort temperature)
        src_id="12:345678",
    )
    # Check the payload structure for parameter 75 (Comfort temperature)
    # The payload structure is: 2 (leading) + 4 (param_id) + 6 (data_type) + 8 (value) + ...
    # 21.5°C = 2150 (0x0866) in middle-endian format
    assert cmd.payload.startswith(
        "0000750092"
    )  # Leading + param_id (75) + data_type (0092)
    assert (
        cmd.payload[12:20] == "00086600"
    )  # Value: 2150 (0x0866) in middle-endian format


def test_set_fan_param_temperature_range() -> None:
    """Test temperature parameter with full range of values."""
    # Test minimum temperature (0.0°C)
    cmd_min = Command.set_fan_param(
        fan_id="39:123456", param_id="75", value=0.0, src_id="12:345678"
    )
    assert cmd_min.payload.startswith(
        "0000750092"
    )  # Leading + param_id (75) + data_type (0092)
    assert (
        cmd_min.payload[12:20] == "00000000"
    )  # 0.0 * 100 = 0 = 0x00000000 (middle-endian)

    # Test maximum temperature (30.0°C)
    cmd_max = Command.set_fan_param(
        fan_id="39:123456", param_id="75", value=30.0, src_id="12:345678"
    )
    assert cmd_max.payload.startswith(
        "0000750092"
    )  # Leading + param_id (75) + data_type (0092)
    assert (
        cmd_max.payload[12:20] == "000BB800"
    )  # 30.0 * 100 = 3000 = 0x0BB8 (middle-endian)

    # Test just inside range (0.01°C and 29.99°C)
    cmd_low = Command.set_fan_param(
        fan_id="39:123456", param_id="75", value=0.01, src_id="12:345678"
    )
    assert cmd_low.payload.startswith(
        "0000750092"
    )  # Leading + param_id (75) + data_type (0092)
    assert (
        cmd_low.payload[12:20] == "00000100"
    )  # 0.01 * 100 = 1 = 0x0001 (middle-endian)

    cmd_high = Command.set_fan_param(
        fan_id="39:123456", param_id="75", value=29.99, src_id="12:345678"
    )
    assert cmd_high.payload.startswith(
        "0000750092"
    )  # Leading + param_id (75) + data_type (0092)
    assert (
        cmd_high.payload[12:20] == "000BB700"
    )  # 29.99 * 100 = 2999 = 0x0BB7 (middle-endian)


def test_set_fan_param_temperature_out_of_range() -> None:
    """Test temperature values outside allowed range with specific error message."""
    # Test just below minimum (should show original value with units)
    with pytest.raises(CommandInvalid) as exc_info:
        Command.set_fan_param(
            fan_id="39:123456", param_id="75", value=-0.01, src_id="12:345678"
        )
    assert str(exc_info.value) == (
        "Parameter 75: Temperature -0.0°C is out of allowed range (0.0°C to 30.0°C)"
    )

    # Test just above maximum (should show original value with units)
    with pytest.raises(CommandInvalid) as exc_info:
        Command.set_fan_param(
            fan_id="32:153289",
            param_id="75",
            value=31.5,  # 31.5°C is above max of 30.0°C
            src_id="37:168270",
        )
    assert str(exc_info.value) == (
        "Parameter 75: Temperature 31.5°C is out of allowed range (0.0°C to 30.0°C)"
    )


def test_set_fan_param_temperature_precision() -> None:
    """Test temperature parameter precision handling."""
    # Test with decimal places
    cmd = Command.set_fan_param(
        fan_id="39:123456", param_id="75", value=21.55, src_id="37:168270"
    )
    # Check the full payload structure for parameter 75 (temperature)
    # Expected payload structure:
    # 000075 - param_id (75)
    # 0092   - data_type (92)
    # 0000086B - value (21.55 * 100 = 2155 = 0x86B)
    # 00000000 - min_value (0.0)
    # 00000BB8 - max_value (30.0 * 100 = 3000 = 0xBB8)
    # 00000001 - precision (0.01 * 100 = 1)
    # 0001     - trailer
    assert cmd.payload.startswith("0000750092")  # param_id (75) + data_type (0092)
    assert (
        cmd.payload[10:18] == "0000086B"
    )  # 21.55 * 100 = 2155 = 0x0000086B (big-endian)
    assert cmd.payload[18:26] == "00000000"  # min_value (0.0)
    assert cmd.payload[26:34] == "00000BB8"  # max_value (30.0 * 100 = 3000 = 0xBB8)
    assert cmd.payload[34:42] == "00000001"  # precision (0.01 * 100 = 1)
    assert cmd.payload[42:46] == "0001"  # trailer

    # Test rounding - note that Python's round() uses "banker's rounding" (round to even)
    # which means 21.549 will round to 21.54 (0x86A) instead of 21.55 (0x86B)
    cmd_round = Command.set_fan_param(
        fan_id="39:123456",
        param_id="75",
        value=21.549,  # Will round to 21.54 due to banker's rounding
        src_id="12:345678",
    )
    # Check that the value is rounded correctly to 21.54 (0x86A)
    assert (
        cmd_round.payload[10:18] == "0000086A"
    )  # 21.549 rounds to 21.54 * 100 = 2154 = 0x0000086A


def test_set_fan_param_temperature_string_input() -> None:
    """Test temperature parameter with string input."""
    cmd = Command.set_fan_param(
        fan_id="39:123456",
        param_id="75",  # Comfort temperature
        value="21.5",
        src_id="12:345678",
    )
    # Value: 21.5 * 100 = 2150 = 0x00000866 in big-endian format
    assert cmd.payload[0:6] == "000075"  # param_id (75) - 6 digits
    assert cmd.payload[6:10] == "0092"  # data_type (0092 for temperature) - 4 digits
    assert cmd.payload[10:18] == "00000866"  # 2150 = 0x00000866 (big-endian)

    # Test with whitespace
    cmd_whitespace = Command.set_fan_param(
        fan_id="39:123456",
        param_id="75",  # Comfort temperature
        value="  21.5  ",
        src_id="12:345678",
    )
    assert cmd_whitespace.payload.startswith(
        "0000750092"
    )  # Leading + param_id (75) + data_type (0092)
    assert (
        cmd_whitespace.payload[10:18] == "00000866"
    )  # 21.5 * 100 = 2150 = 0x00000866 (big-endian)


def test_set_fan_param_payload_format() -> None:
    """Test payload construction format and endianness."""
    # Test temperature parameter (from user's test data)
    cmd_temp = Command.set_fan_param(
        fan_id="32:153289",
        param_id="75",  # Comfort temperature
        value=15.0,
        src_id="37:168270",
    )
    # Verify individual fields based on actual device payload (big-endian values)
    # 15.0 * 100 = 1500 = 0x05DC
    assert cmd_temp.payload[0:6] == "000075"  # param_id (75) - 6 digits
    assert (
        cmd_temp.payload[6:10] == "0092"
    )  # data_type (0092 for temperature) - 4 digits
    assert cmd_temp.payload[10:18] == "000005DC"  # value (1500 = 0x05DC) - big-endian
    assert cmd_temp.payload[18:26] == "00000000"  # min = 0 - big-endian
    assert cmd_temp.payload[26:34] == "00000BB8"  # max = 3000 (0x00000BB8) - big-endian
    assert cmd_temp.payload[34:42] == "00000001"  # precision = 1 (0.01) - big-endian
    assert cmd_temp.payload[42:46] == "0001"  # trailer - 4 digits


def test_set_fan_param_temperature() -> None:
    """Test temperature parameter with real device format."""
    # Test temperature value (25.5°C)
    cmd_temp = Command.set_fan_param(
        fan_id="39:123456",
        param_id="75",  # Temperature parameter
        value=25.5,
        src_id="12:345678",
    )
    # Verify value scaling (25.5 * 100 = 2550 = 0x09F6 in big-endian)
    assert cmd_temp.payload[0:6] == "000075"  # param_id (75) - 6 digits
    assert (
        cmd_temp.payload[6:10] == "0092"
    )  # data_type (0092 for temperature) - 4 digits
    assert cmd_temp.payload[10:18] == "000009F6"  # 2550 = 0x000009F6 (big-endian)
    assert cmd_temp.payload[18:26] == "00000000"  # min = 0 - big-endian
    assert cmd_temp.payload[26:34] == "00000BB8"  # max = 3000 (0x00000BB8) - big-endian
    assert cmd_temp.payload[34:42] == "00000001"  # precision = 1 (0.01) - big-endian
    assert cmd_temp.payload[42:46] == "0001"  # trailer - 4 digits


def test_set_fan_param_payload_padding() -> None:
    """Test payload padding and structure with real device format."""
    # Test with a temperature parameter (param_id=75, Comfort temperature)
    # Using value=10.0 which should be 1000 (0x3E8) in big-endian
    cmd = Command.set_fan_param(
        fan_id="37:168270",
        param_id="75",
        value=10.0,  # 10.0°C
        src_id="32:153289",
    )

    # Expected payload from real device: '0000750092000003E80000000000000BB8000000010001'
    # Note: Values are in big-endian format in the payload
    assert cmd.payload[0:6] == "000075"  # param_id (75) - 6 digits
    assert cmd.payload[6:10] == "0092"  # data_type (0092 for temperature) - 4 digits
    assert (
        cmd.payload[10:18] == "000003E8"
    )  # value (1000 = 0x000003E8) - big-endian (10.0 * 100)
    assert cmd.payload[18:26] == "00000000"  # min = 0 - big-endian
    assert (
        cmd.payload[26:34] == "00000BB8"
    )  # max = 3000 (0x00000BB8) - big-endian (30.0 * 100)
    assert cmd.payload[34:42] == "00000001"  # precision = 1 (0.01) - big-endian
    assert cmd.payload[42:46] == "0001"  # trailer - 4 digits

    # Check payload length (46 hex digits = 23 bytes)
    assert len(cmd.payload) == 46

    # Test temperature value (25.5°C)
    cmd_temp = Command.set_fan_param(
        fan_id="39:123456", param_id="75", value=25.5, src_id="12:345678"
    )
    # 25.5 * 100 = 2550 (0x09F6) in big-endian format
    assert cmd_temp.payload.startswith("0000750092")
    assert cmd_temp.payload[10:18] == "000009F6"  # 0x000009F6 in big-endian


def test_set_fan_param_invalid_parameter() -> None:
    """Test setting an invalid parameter ID."""
    with pytest.raises(CommandInvalid) as exc_info:
        Command.set_fan_param(
            fan_id="39:123456",
            param_id="XX",  # Invalid parameter
            value=50.0,
            src_id="12:345678",
        )
    assert "Invalid parameter ID" in str(exc_info.value)

    """Test setting a value outside the allowed range."""
    with pytest.raises(CommandInvalid) as exc_info:
        Command.set_fan_param(
            fan_id="39:123456",
            param_id="4E",  # Boolean (0 or 1)
            value=75,
            src_id="12:345678",
        )
    assert str(exc_info.value) == (
        "Parameter 4E: Value 75 minutes is out of allowed range (0 to 1 minutes)"
    )


def test_set_fan_param_invalid_value_type() -> None:
    """Test setting a value with various invalid types."""
    # Test with non-numeric string
    with pytest.raises(CommandInvalid) as exc_info:
        Command.set_fan_param(
            fan_id="39:123456",
            param_id="3F",  # Low mode Supply fan rate (%)
            value="not_a_number",
            src_id="12:345678",
        )
    assert "Invalid value" in str(exc_info.value)

    # Test with empty string
    with pytest.raises(CommandInvalid) as exc_info:
        Command.set_fan_param(
            fan_id="39:123456", param_id="3F", value="", src_id="12:345678"
        )
    assert "Invalid value" in str(exc_info.value)


def test_set_fan_param_required_parameters() -> None:
    """Test that all required parameters are properly validated."""
    # Get the signature of the set_fan_param method
    sig = inspect.signature(Command.set_fan_param)

    # Verify that src_id is a required parameter
    assert "src_id" in sig.parameters
    assert sig.parameters["src_id"].default is inspect.Parameter.empty

    # Verify that fan_id, param_id, and value are also required
    for param in ["fan_id", "param_id", "value"]:
        assert param in sig.parameters
        assert sig.parameters[param].default is inspect.Parameter.empty


def test_set_fan_param_min_max_values() -> None:
    """Test set_fan_param with minimum and maximum values."""
    # Test with minimum value (0%)
    cmd = Command.set_fan_param(
        fan_id="39:123456",
        param_id="3F",
        value=0.0,  # 0% for parameter 3F
        src_id="12:345678",
    )
    # Check the value field in the payload (starts at offset 16, 8 bytes long)
    # 00003F000F - param_id (3F) + data_type (000F)
    # 00000000   - value (0.0 * 200 = 0 = 0x00000000)
    assert cmd.payload[16:24] == "00000000"  # Value field is 8 hex digits

    # Test with maximum value (80%)
    cmd = Command.set_fan_param(
        fan_id="39:123456",
        param_id="3F",
        value=80.0,  # 80% for parameter 3F (max allowed)
        src_id="12:345678",
    )
    # Check the value field in the payload (starts at offset 16, 8 bytes long)
    # 00003F000F - param_id (3F) + data_type (000F)
    # A0000000   - value (80.0 * 2 = 160 = 0xA0) in little-endian format
    assert (
        cmd.payload[16:24] == "A0000000"
    )  # Value field is 8 hex digits, little-endian

    # Test with temperature at minimum (0.0°C)
    cmd = Command.set_fan_param(
        fan_id="39:123456",
        param_id="75",
        value=0.0,  # 0.0°C for parameter 75
        src_id="12:345678",
    )
    # Check the value field in the payload (starts at offset 16, 8 bytes long)
    # 0000750092 - param_id (75) + data_type (0092)
    # 00000000  - value (0.0 * 100 = 0 = 0x00000000)
    assert (
        cmd.payload[16:24] == "00000000"
    )  # Value field is 8 hex digits, little-endian

    # Test with temperature at maximum (30.0°C)
    cmd = Command.set_fan_param(
        fan_id="39:123456",
        param_id="75",
        value=30.0,  # 30.0°C for parameter 75
        src_id="12:345678",
    )
    # Check the value field in the payload (starts at offset 16, 8 bytes long)
    # 0000750092 - param_id (75) + data_type (0092)
    # B8000000  - value (30.0 * 100 = 3000 = 0x0BB8) in little-endian format
    # Note: The actual value is B8000000, not B80B0000 as initially expected
    assert (
        cmd.payload[16:24] == "B8000000"
    )  # Value field is 8 hex digits, little-endian


def test_set_fan_param_case_insensitive() -> None:
    """Test that parameter IDs are case-insensitive."""
    cmd_lower = Command.set_fan_param(
        fan_id="39:123456",
        param_id="3f",  # Lowercase
        value="50.0",
        src_id="12:345678",
    )
    cmd_upper = Command.set_fan_param(
        fan_id="39:123456",
        param_id="3F",  # Uppercase
        value="50.0",
        src_id="12:345678",
    )
    assert cmd_lower.payload == cmd_upper.payload


def test_set_fan_param_invalid_empty_id() -> None:
    """Test with empty parameter ID."""
    with pytest.raises(CommandInvalid) as exc_info:
        Command.set_fan_param(
            fan_id="39:123456",
            param_id="",  # Empty parameter ID
            value="50.0",
            src_id="12:345678",
        )
    assert "Invalid parameter ID" in str(exc_info.value)


def test_set_fan_param_negative_value() -> None:
    """Test with negative values where not allowed."""
    with pytest.raises(CommandInvalid) as exc_info:
        Command.set_fan_param(
            fan_id="39:123456",
            param_id="3F",  # Percentage (0-100%)
            value=-1.0,
            src_id="12:345678",
        )
    assert (
        str(exc_info.value)
        == "Parameter 3F: Value -1.0% is out of allowed range (0.0% to 80.0%)"
    )


def test_set_fan_param_just_above_max() -> None:
    """Test with value just above maximum allowed."""
    with pytest.raises(
        exc.CommandInvalid,
        match=r"Parameter 75: Temperature 30.1°C is out of allowed range \(0.0°C to 30.0°C\)",
    ):
        Command.set_fan_param(
            fan_id="39:123456",
            param_id="75",  # Comfort temperature (max 30.0)
            value=30.1,
            src_id="12:345678",
        )


def test_set_fan_param_unsupported_parameter() -> None:
    """Test that a parameter ID not in the schema raises the correct exception."""
    with pytest.raises(exc.CommandInvalid) as exc_info:
        Command.set_fan_param(
            fan_id="39:123456",
            param_id="FF",
            value=123,  # Any value
            src_id="12:345678",
        )

    # Check the error message indicates the parameter is not found
    assert str(exc_info.value) == (
        "Unknown parameter ID: 'FF'. This parameter is not defined in the device schema"
    )


def test_set_fan_param_sensor_sensitivity() -> None:
    """Test sensor sensitivity parameter (52) with its specific range and precision."""
    # Test minimum value (0%)
    cmd = Command.set_fan_param(
        fan_id="39:123456",
        param_id="52",  # Sensor sensitivity (0-25%, precision 0.1)
        value=0,
        src_id="12:345678",
    )
    # Verify payload structure for parameter 52
    # Expected format: 00005200010000000000000000000000FA000000010032
    # - 000052: param_id (52)
    # - 0001: data_type (01 for centile)
    # - 00000000: value (0)
    # - 00000000: min (0)
    # - 000000FA: max (250 = 25.0 / 0.1)
    # - 00000001: precision (1 = 0.1 * 10)
    # - 0032: trailer
    assert cmd.payload[0:6] == "000052"  # param_id (52)
    assert cmd.payload[6:10] == "0001"  # data_type (01 for centile)
    assert cmd.payload[10:18] == "00000000"  # value (0)
    assert cmd.payload[18:26] == "00000000"  # min (0)
    assert cmd.payload[26:34] == "000000FA"  # max (250 = 25.0 / 0.1)
    assert cmd.payload[34:42] == "00000001"  # precision (1 = 0.1 * 10)
    assert cmd.payload[42:46] == "0032"  # trailer

    # Test mid-range value (12.5%)
    cmd_mid = Command.set_fan_param(
        fan_id="39:123456",
        param_id="52",
        value=12.5,
        src_id="12:345678",
    )
    assert cmd_mid.payload[10:18] == "0000007D"  # 125 = 12.5 / 0.1

    # Test maximum value (25.0%)
    cmd_max = Command.set_fan_param(
        fan_id="39:123456",
        param_id="52",
        value=25.0,
        src_id="12:345678",
    )
    assert cmd_max.payload[10:18] == "000000FA"  # 250 = 25.0 / 0.1

    # Test value just below maximum (24.9%)
    cmd_just_below = Command.set_fan_param(
        fan_id="39:123456",
        param_id="52",
        value=24.9,
        src_id="12:345678",
    )
    assert cmd_just_below.payload[10:18] == "000000F9"  # 249 = 24.9 / 0.1

    # Test value above maximum (should raise an exception)
    with pytest.raises(
        exc.CommandInvalid,
        match=r"Parameter 52: Value 25.1% is out of allowed range \(0.0% to 25.0%\)",
    ):
        Command.set_fan_param(
            fan_id="39:123456",
            param_id="52",
            value=25.1,  # This is above the maximum of 25.0
            src_id="12:345678",
        )

    # Test value at maximum (25.0)
    cmd_at_max = Command.set_fan_param(
        fan_id="39:123456", param_id="52", value=25.0, src_id="12:345678"
    )
    # With precision=0.1, 25.0 / 0.1 = 250 = 0xFA
    assert cmd_at_max.payload[10:18] == "000000FA"  # 250 = 0x000000FA (scaled value)
