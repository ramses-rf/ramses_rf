#!/usr/bin/env python3
"""Unit tests for the MqttTransport layer."""

import unittest
from unittest.mock import MagicMock, patch

from ramses_tx import exceptions as exc
from ramses_tx.transport import TransportConfig
from ramses_tx.transport.mqtt import MqttTransport, validate_topic_path


class TestMqttTransport(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.mock_protocol = MagicMock()
        self.broker_url = (
            "mqtt://mqtt_user:mqtt_pass@localhost:1883/RAMSES/GATEWAY/01:123456"
        )

        with (
            patch("paho.mqtt.client.Client.connect_async"),
            patch("paho.mqtt.client.Client.loop_start"),
        ):
            self.transport = MqttTransport(
                self.broker_url,
                self.mock_protocol,
                config=TransportConfig(),
            )

    async def asyncTearDown(self) -> None:
        self.transport.close()

    async def test_validate_topic_path_valid(self) -> None:
        """Verify validate_topic_path accepts valid gateway topics."""
        # Act
        valid_topic = validate_topic_path("/RAMSES/GATEWAY/01:123456")

        # Assert
        self.assertEqual(valid_topic, "RAMSES/GATEWAY/01:123456")

    async def test_validate_topic_path_invalid(self) -> None:
        """Verify validate_topic_path raises TransportMqttError on invalid paths."""
        # Act & Assert
        with self.assertRaises(exc.TransportMqttError):
            validate_topic_path("invalid/path/format/extra")

    async def test_write_frame_when_not_connected_raises_state_error(self) -> None:
        """Verify write_frame raises TransportStateError when disconnected."""
        # Arrange
        self.transport._connected = False

        # Act & Assert
        with self.assertRaises(exc.TransportStateError):
            await self.transport.write_frame("--- RQ --- 18:000730 01:195932")

    async def test_on_message_schedules_thread_safe_frame_read(self) -> None:
        """Verify on_message dispatches frame_read onto the asyncio event loop."""
        # Arrange
        self.transport._connection_established = True
        mock_msg = MagicMock()
        mock_msg.topic = "RAMSES/GATEWAY/01:123456/rx"
        mock_msg.payload = b'{"ts": "2026-07-21T12:00:00.000000", "msg": "059 RP --- 01:195932 04:017982 --:------ 313F 009 00FC2300C4150C07E9"}'

        with patch.object(
            self.transport._loop, "call_soon_threadsafe"
        ) as mock_call_soon:
            # Act
            self.transport._on_message(self.transport.client, None, mock_msg)

            # Assert
            mock_call_soon.assert_called_once_with(
                self.transport._frame_read,
                "2026-07-21T12:00:00",
                "059 RP --- 01:195932 04:017982 --:------ 313F 009 00FC2300C4150C07E9",
            )
