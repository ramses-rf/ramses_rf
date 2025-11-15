#!/usr/bin/env python3
"""Parser for 2411 HVAC messages."""

from typing import Any, dict

# Parameter definitions
# Add parameters that we know how to parse (or parts of it)

Known_2411_PARAMS: dict[str, dict[str, str | object]] = {
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
    and added to known_params in ramses_tx.parsers.

    :param payload: 2411 message payload
    :param msg: Message object
    :return: Decoded message dictionary
    """

    class MockMessage:
        def __init__(self, verb: str) -> None:
            self.verb = verb

    # Use the actual parser from ramses_tx.parsers
    from ramses_tx.parsers import parser_2411 as actual_parser

    result = actual_parser(payload, MockMessage(" I"))
    result["verb"] = " I"  # Add verb to result for display
    return result
