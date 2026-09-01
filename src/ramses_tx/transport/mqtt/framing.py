#!/usr/bin/env python3
"""RAMSES RF - MQTT payload framing and rate limiting handlers."""

from __future__ import annotations

import asyncio
import json
import logging
from time import perf_counter
from typing import Final

from ... import exceptions as exc
from ...const import (
    DUTY_CYCLE_DURATION,
    MAX_TRANSMIT_RATE_TOKENS,
    SZ_RAMSES_GATEWAY,
)

_LOGGER = logging.getLogger(__name__)

TOPIC_SUFFIX_TX: Final[str] = "/tx"
TOPIC_SUFFIX_RX: Final[str] = "/rx"
TOPIC_WILDCARD_RX: Final[str] = "/+/rx"


def validate_topic_path(path: str) -> str:
    """Test the topic path and normalise it.

    :param path: The candidate topic path.
    :type path: str
    :return: The valid, normalised path.
    :rtype: str
    :raises TransportMqttError: If the path format is invalid.
    """
    new_path = path or SZ_RAMSES_GATEWAY
    if new_path.startswith("/"):
        new_path = new_path[1:]
    if not new_path.startswith(SZ_RAMSES_GATEWAY):
        raise exc.TransportMqttError(f"Invalid topic path: {path}")
    if new_path == SZ_RAMSES_GATEWAY:
        new_path += "/+"
    if len(new_path.split("/")) != 3:
        raise exc.TransportMqttError(f"Invalid topic path: {path}")
    return new_path


class MqttFramingHandler:
    """Manages token-bucket rate limiting and RAMSES MQTT frame encoding/decoding."""

    _MAX_TOKENS: Final[int] = MAX_TRANSMIT_RATE_TOKENS
    _TIME_WINDOW: Final[int] = DUTY_CYCLE_DURATION
    _TOKEN_RATE: Final[float] = _MAX_TOKENS / _TIME_WINDOW

    def __init__(self) -> None:
        """Initialise the MQTT framing handler and token bucket."""
        self._timestamp = perf_counter()
        self._max_tokens: float = self._MAX_TOKENS * 2
        self._num_tokens: float = self._MAX_TOKENS * 2

    async def throttle_tx(self, disable_tx_limits: bool = False) -> bool:
        """Apply token-bucket rate limiting before transmission.

        :param disable_tx_limits: Whether to bypass transmit limits.
        :type disable_tx_limits: bool
        :return: True if transmission is allowed, False if discarded.
        :rtype: bool
        """
        timestamp = perf_counter()
        elapsed, self._timestamp = timestamp - self._timestamp, timestamp
        self._num_tokens = min(
            self._num_tokens + elapsed * self._TOKEN_RATE, self._max_tokens
        )

        if self._num_tokens < 1.0 - self._TOKEN_RATE and not disable_tx_limits:
            _LOGGER.warning(
                "Discarding write (tokens=%.2f)",
                self._num_tokens,
            )
            return False

        self._num_tokens -= 1.0
        if self._max_tokens > self._MAX_TOKENS:
            self._max_tokens = min(self._max_tokens, self._num_tokens)
            self._max_tokens = max(self._max_tokens, self._MAX_TOKENS)

        if self._num_tokens < 0.0 and not disable_tx_limits:
            delay = (0 - self._num_tokens) / self._TOKEN_RATE
            _LOGGER.debug("Sleeping (seconds=%s)", delay)
            await asyncio.sleep(delay)

        return True

    @staticmethod
    def encode_frame(frame: str) -> str:
        """Encode a raw ASCII frame into an MQTT JSON payload.

        :param frame: The raw frame string.
        :type frame: str
        :return: JSON payload string.
        :rtype: str
        """
        return json.dumps({"msg": frame})

    @staticmethod
    def decode_payload(payload_bytes: bytes) -> tuple[str, str] | None:
        """Decode an incoming MQTT JSON payload into timestamp and frame.

        :param payload_bytes: Raw bytes from the MQTT message.
        :type payload_bytes: bytes
        :return: Tuple of (iso_timestamp, raw_frame) or None if invalid.
        :rtype: tuple[str, str] | None
        """
        try:
            payload = json.loads(payload_bytes)
        except json.JSONDecodeError:
            # Empty payloads are used to clear retained messages —
            # silently ignore them instead of logging a warning.
            if payload_bytes.strip():
                _LOGGER.warning(
                    "%s < Can't decode JSON (ignoring)", payload_bytes
                )
            return None

        if "ts" not in payload or "msg" not in payload:
            return None

        return str(payload["ts"]), str(payload["msg"])
