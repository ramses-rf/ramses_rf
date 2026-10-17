#!/usr/bin/env python3
"""RAMSES RF - Zigbee chunking and frame reassembly handlers."""

from __future__ import annotations

import asyncio
import logging
import math
import re
from typing import Any, Final

from ... import exceptions as exc
from ...helpers import dt_now

_LOGGER = logging.getLogger(__name__)


class ZigbeeFramingHandler:
    """Manages APS-safe frame chunking, payload decoding, and chunk assembly."""

    _CHUNK_TIMEOUT: Final[float] = 5.0

    def __init__(
        self,
        *,
        max_char_len: int = 63,
        chunk_body_len: int = 32,
        chunk_timeout: float = _CHUNK_TIMEOUT,
    ) -> None:
        """Initialise the Zigbee framing handler.

        :param max_char_len: Maximum character length allowed for a chunk.
        :type max_char_len: int
        :param chunk_body_len: Maximum body length before adding chunk headers.
        :type chunk_body_len: int
        :param chunk_timeout: Maximum gap between incoming chunks, in seconds.
        :type chunk_timeout: float
        """
        self._max_char_len = max_char_len
        self._chunk_body_len = chunk_body_len
        self._chunk_timeout = chunk_timeout
        self._chunk_buffers: dict[str, dict[str, Any]] = {}

    def cleanup_chunk_buffers(self) -> None:
        """Remove chunk buffers that have exceeded the TTL to prevent leaks."""
        now = dt_now()
        stale_keys = [
            key
            for key, chunk_buffer in self._chunk_buffers.items()
            if (now - chunk_buffer["timestamp"]).total_seconds()
            > self._chunk_timeout
        ]
        for key in stale_keys:
            _LOGGER.warning(
                "Dropping stale incomplete chunk buffer for %s", key
            )
            del self._chunk_buffers[key]

    @staticmethod
    def parse_chunk(payload: str) -> tuple[int, int, str] | None:
        """Parse a chunk header of the form 'seq/total|body'.

        :param payload: The raw payload.
        :type payload: str
        :return: Tuple containing (seq, total, body) or None if malformed.
        :rtype: tuple[int, int, str] | None
        """
        try:
            m = re.match(r"^(\d{1,3})/(\d{1,3})\|(.*)$", payload, re.DOTALL)
            if not m:
                return None
            sequence_number = int(m.group(1))
            total = int(m.group(2))
            body = m.group(3)
            if sequence_number < 1 or total < 1 or sequence_number > total:
                return None
            return (sequence_number, total, body)
        except asyncio.CancelledError:
            raise
        except (ValueError, TypeError, IndexError) as err:
            _LOGGER.exception("Failed to parse chunk: %s", err)
            return None

    def chunk_payload(self, payload: str) -> list[tuple[int, int, str]]:
        """Split a string payload into sequence-numbered chunks.

        :param payload: The full string to chunk.
        :type payload: str
        :raises TransportZigbeeError: If chunk header exceeds size limits.
        :return: A list of tuples containing (seq, total, chunk_string).
        :rtype: list[tuple[int, int, str]]
        """
        if len(payload) <= self._max_char_len:
            return [(1, 1, payload)]

        total = math.ceil(len(payload) / self._chunk_body_len)
        chunks: list[tuple[int, int, str]] = []

        for chunk_index in range(total):
            start = chunk_index * self._chunk_body_len
            body = payload[start : start + self._chunk_body_len]
            header = f"{chunk_index + 1}/{total}|"
            allowed = self._max_char_len - len(header)
            if allowed <= 0:
                raise exc.TransportZigbeeError(
                    "Chunk header exceeds Zigbee char-string limit"
                )
            body = body[:allowed]
            chunks.append((chunk_index + 1, total, header + body))

        return chunks

    def process_incoming_chunk(
        self, device_key: str, payload: str
    ) -> tuple[bool, str | None, str | None]:
        """Process incoming chunk, buffering until complete.

        :param device_key: Unique identifier for the sending device.
        :type device_key: str
        :param payload: Raw chunk payload string.
        :type payload: str
        :return: Tuple of (is_chunk, ack_to_send, assembled_frame_if_ready).
        :rtype: tuple[bool, str | None, str | None]
        """
        self.cleanup_chunk_buffers()

        parsed = self.parse_chunk(payload)
        if not parsed:
            return False, None, None

        sequence_number, total, body = parsed
        chunk_buffer = self._chunk_buffers.get(device_key)

        if not chunk_buffer or chunk_buffer.get("total") != total:
            chunk_buffer = {
                "total": total,
                "parts": [None] * total,
                "received": 0,
                "timestamp": dt_now(),
            }
            self._chunk_buffers[device_key] = chunk_buffer
        else:
            chunk_buffer["timestamp"] = dt_now()

        ack_to_schedule: str | None = None
        parts = chunk_buffer["parts"]
        if parts[sequence_number - 1] is None:
            parts[sequence_number - 1] = body
            chunk_buffer["received"] += 1
            ack_to_schedule = f"ACK {sequence_number}/{total}"

        if chunk_buffer["received"] < total:
            return True, ack_to_schedule, None

        assembled = "".join(p if p is not None else "" for p in parts)
        self._chunk_buffers.pop(device_key, None)
        return True, ack_to_schedule, assembled

    @staticmethod
    def decode_command_payload(args: Any) -> str | None:
        """Safely decode incoming ZCL command arguments into a string payload.

        :param args: The arbitrary payload argument block.
        :type args: Any
        :return: The decoded string or None if unparsable.
        :rtype: str | None
        """
        if isinstance(args, str):
            return args

        if isinstance(args, (bytes, bytearray)):
            raw = bytes(args)
        elif (
            isinstance(args, list)
            and args
            and all(isinstance(x, int) for x in args)
        ):
            raw = bytes(args)
        elif isinstance(args, (list, tuple)) and args:
            return ZigbeeFramingHandler.decode_command_payload(args[0])
        else:
            return None

        if not raw:
            return None

        if len(raw) >= 2 and 0 < raw[0] <= len(raw) - 1:
            string_data = raw[1 : 1 + raw[0]]
            try:
                data_str = string_data.decode("ascii", errors="strict")
                if len(data_str) >= 4 and data_str[0].isdigit():
                    slash_pos = data_str.find("/")
                    if 0 < slash_pos < 3:
                        pipe_pos = data_str.find("|", slash_pos)
                        if slash_pos < pipe_pos < 6:
                            return data_str
            except (UnicodeDecodeError, AttributeError):
                pass

            return string_data.decode("ascii", errors="ignore")

        return raw.decode("ascii", errors="ignore")
