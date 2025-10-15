"""Parser for 2411 parameter messages.

At the end of the file you can find test_messages that can be used to test the parser.
Known_2411_PARAMS(at the top) holds params that we decoded (partly)
_parse_hex_value is used to parse unknown params in different formats.
    Check these if you see any values that make sense to find the right format.
Just run it from the terminal as python3 2411_parser.py
"""

import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)


# Parameter definitions
# Add parameters that we know how to parse (or parts of it)

Known_2411_PARAMS = {
    # TODO: add params that were decoded.
    #     "000007": {
    #         "name": "base_vent_enabled",
    #         "description": "Base Ventilation Enable/Disable",
    #         "parser": lambda payload, offset: {
    #             "unknown1": payload[6:16],
    #             "enabled": payload[16:18] == "01",
    #             "unknown2": payload[18:],
    #         },
    #     },
}


def parser_2411(payload: str, msg: Any) -> dict[str, Any]:
    """
    Parser for 2411 messages.
    Params not listed in Known_2411_PARAMS are parsed by _parse_unknown_parameter
    as 4byte, 6byte or 8byte blocks, in different formats.

    :param payload: Hex string payload
    :param msg: Message object with verb attribute (RQ/RP/W/I)
    :return: Dictionary with parsed parameter data including all structure components
    """

    # Extract 3-byte parameter ID
    param_id = payload[:6]

    result: dict[str, Any] = {
        "parameter_id": param_id,
        "parameter_hex": f"0x{param_id}",
    }

    # Get parameter definition
    param_def_raw = Known_2411_PARAMS.get(param_id)
    param_def: dict[str, Any] | None = (
        param_def_raw if isinstance(param_def_raw, dict) else None
    )

    if param_def:
        result["parameter_name"] = param_def["name"]
        result["description"] = param_def["description"]
    else:
        result["parameter_name"] = f"unknown_{param_id}"
        result["description"] = "Unknown"
        _LOGGER.warning(f"Unknown parameter ID: {param_id}. Payload: {payload}")

    # For RQ (request) messages, just return parameter info
    if hasattr(msg, "verb") and msg.verb == "RQ":
        return result

    try:
        if param_def and "parser" in param_def:
            # Use the custom parser function from the parameter definition
            parser_func = param_def["parser"]
            offset = param_def.get("offset", 0)
            parsed_data = parser_func(payload, offset)
            result.update(parsed_data)
        else:
            # Unknown parameter - try different parsing strategies
            result.update(_parse_unknown_parameter(payload, param_id))

        # Extract footer/status bytes (last 6 bytes typically)
        if len(payload) >= 46:
            result["type ? (3:5)"] = payload[3:5]
            result["footer ? (-6)"] = payload[-6:]
            result["status_flag ?(19)"] = payload[38:40]  # Byte 19

    except (ValueError, IndexError) as err:
        _LOGGER.error(f"Error parsing 2411 payload for param {param_id}: {err}")
        result["error"] = str(err)
        result["raw_data"] = payload[6:]
    return result


def _parse_unknown_parameter(payload: str, param_id: str) -> dict[str, Any]:
    """
    Try different parsing strategies for unknown 2411 parameters.

    :param payload: Hex string payload
    :param param_id: Parameter ID
    :return: Dictionary with parsed data from different strategies
    """
    result = {}

    # Strategy 1: Try 4-byte blocks from position 6 onwards
    result["strategy_4byte"] = _try_4byte_blocks(payload)

    # Strategy 2: Try 6-byte blocks from position 6 onwards
    result["strategy_6byte"] = _try_6byte_blocks(payload)

    # Strategy 3: Try 8-byte blocks from position 6 onwards
    result["strategy_8byte"] = _try_8byte_blocks(payload)

    return result


def _parse_hex_value(hex_str: str) -> dict[str, Any]:
    """
    Parse a hex string into multiple useful representations.

    :param hex_str: Hex string to parse
    :return: Dictionary with parsed representations
    """
    result: dict[str, Any] = {"raw": hex_str, "hex": f"0x{hex_str.upper()}"}

    try:
        # Basic decimal value
        dec = int(hex_str, 16)
        result["dec"] = dec

        # Byte-swapped version (big-endian to little-endian)
        if len(hex_str) % 2 == 0:  # Only if even number of hex digits
            # Swap byte order (e.g., "A1B2" -> "B2A1")
            swapped = "".join(
                reversed([hex_str[i : i + 2] for i in range(0, len(hex_str), 2)])
            )
            result["swapped_hex"] = f"0x{swapped.upper()}"
            result["swapped_dec"] = int(swapped, 16)

            # Little-endian interpretation
            le_bytes = bytes.fromhex(hex_str)
            le_value = int.from_bytes(le_bytes, byteorder="little", signed=False)
            result["le_dec"] = le_value
            result["le_hex"] = f"0x{le_value:X}"

            # Signed integer interpretation
            if len(hex_str) in [4, 8]:  # 16-bit or 32-bit
                result["signed_dec"] = int.from_bytes(
                    le_bytes, byteorder="big", signed=True
                )

        # Binary representation
        result["bin"] = f"0b{dec:0{len(hex_str) * 4}b}"

        # ASCII interpretation if possible (for 2 or 4 character hex)
        if len(hex_str) in [2, 4, 6, 8]:
            try:
                ascii_str = bytes.fromhex(hex_str).decode("ascii", errors="replace")
                if all(32 <= ord(c) <= 126 for c in ascii_str):
                    result["ascii"] = ascii_str
            except (UnicodeDecodeError, ValueError):
                pass

    except ValueError as e:
        result["error"] = str(e)

    return result


def _try_4byte_blocks(payload: str) -> dict[str, Any]:
    """
    Try parsing as 4-byte (8 hex character) blocks.

    For each 4-byte block, provides:
    - raw: Original hex string
    - hex: Formatted hex with 0x prefix
    - dec: Unsigned decimal value
    - swapped_hex: Bytes in reverse order
    - swapped_dec: Decimal of swapped bytes
    - le_dec: Little-endian decimal
    - le_hex: Little-endian hex
    - signed_dec: Signed decimal (if applicable)
    - bin: Binary representation
    - ascii: ASCII interpretation (if possible)
    - offset: Position in the original payload

    :param payload: Hex string payload
    :return: Dictionary with parsed 4-byte blocks
    """
    blocks = {}
    data_section = payload[6:]  # Skip parameter ID

    for i in range(0, min(len(data_section), 32), 4):  # Up to 8 blocks
        if i + 4 <= len(data_section):
            block = data_section[i : i + 4]
            block_info = _parse_hex_value(block)
            block_info["offset"] = 6 + i  # Add offset to the original payload
            blocks[f"block_{i // 4 + 1}"] = block_info

    return blocks


def _try_6byte_blocks(payload: str) -> dict[str, Any]:
    """
    Try parsing as 6-byte (12 hex character) blocks.

    For each 6-byte block, provides:
    - raw: Original hex string
    - hex: Formatted hex with 0x prefix
    - dec: Unsigned decimal value
    - swapped_hex: Bytes in reverse order
    - swapped_dec: Decimal of swapped bytes
    - le_dec: Little-endian decimal
    - le_hex: Little-endian hex
    - signed_dec: Signed decimal (if applicable)
    - bin: Binary representation
    - ascii: ASCII interpretation (if possible)
    - offset: Position in the original payload

    :param payload: Hex string payload
    :return: Dictionary with parsed 6-byte blocks
    """
    blocks = {}
    data_section = payload[6:]  # Skip parameter ID

    for i in range(0, min(len(data_section), 30), 6):  # Up to 5 blocks
        if i + 6 <= len(data_section):
            block = data_section[i : i + 6]
            block_info = _parse_hex_value(block)
            block_info["offset"] = 6 + i
            blocks[f"block_{i // 6 + 1}"] = block_info

    return blocks


def _try_8byte_blocks(payload: str) -> dict[str, Any]:
    """
    Try parsing as 8-byte (16 hex character) blocks.

    For each 8-byte block, provides:
    - raw: Original hex string
    - hex: Formatted hex with 0x prefix
    - dec: Unsigned decimal value
    - swapped_hex: Bytes in reverse order
    - swapped_dec: Decimal of swapped bytes
    - le_dec: Little-endian decimal
    - le_hex: Little-endian hex
    - signed_dec: Signed decimal (if applicable)
    - bin: Binary representation
    - ascii: ASCII interpretation (if possible)
    - offset: Position in the original payload

    :param payload: Hex string payload
    :return: Dictionary with parsed 8-byte blocks
    """
    blocks = {}
    data_section = payload[6:]  # Skip parameter ID

    for i in range(0, min(len(data_section), 32), 8):  # Up to 4 blocks
        if i + 8 <= len(data_section):
            block = data_section[i : i + 8]
            block_info = _parse_hex_value(block)
            block_info["offset"] = 6 + i
            blocks[f"block_{i // 8 + 1}"] = block_info

    return blocks


def format_field(value: Any, width: int, align: str = "left") -> str:
    """
    Format a value to a specific width with alignment.

    :param value: The value to format (will be converted to string)
    :param width: The desired total width
    :param align: 'left', 'right', or 'center'
    :return: Formatted string padded with spaces to the specified width
    """
    if value is None:
        value = "N/A"

    text = str(value)

    # Truncate if too long
    if len(text) > width:
        text = text[: width - 3] + "..." if width > 3 else text[:width]

    # Apply alignment
    if align == "right":
        return f"{text:>{width}}"
    elif align == "center":
        return f"{text:^{width}}"
    else:  # left align
        return f"{text:<{width}}"


def format_block_table(blocks: dict[str, Any], title: str) -> str:
    """
    Format a dictionary of blocks as a table.

    :param blocks: Dictionary of parsed blocks
    :param title: Title for the table
    :return: Formatted table string
    """
    if not blocks:
        return f"{title}: No blocks found\n"

    # Get all unique keys from all blocks
    all_keys = set()
    for block_info in blocks.values():
        all_keys.update(block_info.keys())

    # Remove keys that shouldn't be in the table
    exclude_keys = {"raw", "offset"}
    all_keys_set = set()
    for block_info in blocks.values():
        all_keys_set.update(block_info.keys())

    # Custom ordering: put bin at the end, keep others in order
    display_keys = []
    for key in sorted(all_keys_set):
        if key not in exclude_keys:
            if key == "bin":
                continue  # Save for last
            display_keys.append(key)
    display_keys.append("bin")  # Put bin at the end

    if not display_keys:
        return f"{title}: No displayable data\n"

    # Define column specifications: (width, align)
    column_specs = [
        (12, "left"),  # Block (+1)
        (8, "left"),  # Raw (-2)
    ]

    # Add specs for each display key
    for key in display_keys:
        if key == "dec":
            column_specs.append((9, "right"))  # Increased for large decimal numbers
        elif key in ["swapped_dec", "le_dec", "signed_dec"]:
            column_specs.append((11, "right"))  # Increased for swapped_dec
        elif key == "hex":
            column_specs.append(
                (10, "left")
            )  # Increased for hex (10 chars for 0x12345678)
        elif key == "le_hex":
            column_specs.append((9, "left"))  # Increased for le_hex (8 digits + 0x)
        elif key == "swapped_hex":
            column_specs.append((11, "left"))  # Increased for swapped_hex
        elif key == "bin":
            column_specs.append(
                (50, "left")
            )  # Very large width for full binary display
        else:
            column_specs.append((8, "left"))

    # Create header
    header = f"{title} ({len(blocks)} blocks):\n"
    header_parts = []
    header_names = ["Block", "Raw"] + display_keys
    for i, (width, align) in enumerate(column_specs):
        header_parts.append(format_field(header_names[i], width, align))
    header += " ".join(header_parts) + "\n"
    # Calculate separator length based on header with spaces
    header_with_spaces = " ".join(header_parts)
    header += "-" * len(header_with_spaces) + "\n"

    # Create rows
    rows = []
    for block_name, block_info in blocks.items():
        offset = block_info.get("offset", "N/A")
        block_display = f"{block_name} ({offset})"
        raw_value = block_info.get("raw", "N/A")

        row_parts = []
        # Format Block and Raw columns
        row_parts.append(format_field(block_display, 12, "left"))  # Block (+1)
        row_parts.append(format_field(raw_value, 8, "left"))  # Raw (-2)

        # Format each data column
        for key in display_keys:
            value = block_info.get(key, "N/A")
            if key == "dec":
                row_parts.append(
                    format_field(value, 9, "right")
                )  # Increased for large decimal numbers
            elif key in ["swapped_dec", "le_dec", "signed_dec"]:
                row_parts.append(format_field(value, 11, "right"))
            elif key == "hex":
                row_parts.append(format_field(value, 10, "left"))
            elif key == "le_hex":
                row_parts.append(format_field(value, 9, "left"))
            elif key == "swapped_hex":
                row_parts.append(format_field(value, 11, "left"))
            elif key == "bin":
                # No truncation for binary - let it be as long as needed
                row_parts.append(format_field(value, 50, "left"))
            else:
                row_parts.append(format_field(value, 8, "left"))

        row = " ".join(row_parts)  # Join with spaces between columns
        rows.append(row)

    return header + "\n".join(rows) + "\n"


def format_result_table(result: dict[str, Any], description: str) -> str:
    """
    Format a complete result as tables.

    :param result: Parsed result dictionary
    :param description: Description for the result
    :return: Formatted result string with tables
    """
    output = []
    output.append(f"\n{description}")
    output.append("=" * 60)
    output.append(f"Parameter: {result['parameter_name']} ({result['parameter_id']})")
    output.append(f"Verb: {result.get('verb', 'N/A')}")
    output.append(f"Description: {result['description']}")
    output.append(f"Payload: {result.get('payload', 'N/A')}")

    # Print strategy tables
    for key, value in result.items():
        if key not in [
            "parameter_id",
            "parameter_name",
            "description",
            "payload",
            "verb",
        ] and isinstance(value, dict):
            output.append(format_block_table(value, key))

    # Print simple values
    simple_values = []
    for key, value in result.items():
        if key not in [
            "parameter_id",
            "parameter_name",
            "description",
            "payload",
            "verb",
        ] and not isinstance(value, dict):
            simple_values.append(f"{key}: {value}")

    if simple_values:
        output.append("Other values:")
        for value in simple_values:
            output.append(f"  {value}")

    output.append("-" * 60)
    return "\n".join(output)


def decode_2411_message(raw_message: str, verb: str = "RP") -> dict[str, Any]:
    """
    Convenience function to decode a raw 2411 message.

    :param raw_message: Raw 2411 message string
    :param verb: Message verb (default: "RP")
    :return: Decoded message dictionary
    """

    class MockMessage:
        def __init__(self, verb):
            self.verb = verb

    result = parser_2411(raw_message, MockMessage(verb))
    result["verb"] = verb  # Add verb to result for display
    return result


# Example usage and testing
if __name__ == "__main__":
    # Test messages from your logs
    # Example:
    #   ("0000070000000000010000000000000001000000018A00", "RP", "Base vent is ON")
    test_messages = [
        ("00000700000000000000000000000000000000000000", "W", "Base vent set to OFF"),
        ("00000700000000000100000000000000000000000000", "W", "Base vent set to ON"),
        ("0000070000000000010000000000000001000000018A00", " I", "Base vent is ON"),
        ("0000070000000000000000000000000001000000018A00", "RP", "Base vent is OFF"),
        ("0000070000000000010000000000000001000000018A00", "RP", "Base vent is ON"),
        # ("0000871400000000000000000000000002000000018A00", "RP", "Parameter 0x87"),
        # ("0000DA7F00000000000000000000000003000000018A00", "RP", "Parameter 0xDA"),
        # ("0000881510000002BC000001900000076C000000018A33", "RP", "Timer configuration"),
    ]

    print("2411 Message Parser Test\n" + "=" * 50)

    for payload, verb, description in test_messages:
        result = decode_2411_message(payload, verb)
        print(format_result_table(result, description))
