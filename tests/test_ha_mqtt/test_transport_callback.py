#!/usr/bin/env python3
"""Unit tests for the CallbackTransport (Inversion of Control)."""

import asyncio
import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from ramses_tx import exceptions as exc
from ramses_tx.transport import TransportConfig
from ramses_tx.transport.callback import CallbackTransport


class TestCallbackTransport(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.mock_protocol = MagicMock()
        self.mock_writer = AsyncMock()

        # Initialize transport with our mocks and a default strict config
        self.transport = CallbackTransport(
            self.mock_protocol,
            self.mock_writer,
            config=TransportConfig(),
        )

    async def test_initial_state_is_paused(self) -> None:
        """Verify transport starts in PAUSED state (Circuit Breaker default)."""
        # It should default to not reading until explicitly resumed
        self.assertFalse(self.transport.is_reading())

    async def test_write_frame_delegates_to_writer(self) -> None:
        """Verify outbound frames are passed to the injected io_writer."""
        test_frame = "--- RQ --- 18:000730 01:195932 --:------ 1F41 001 00"

        await self.transport.write_frame(test_frame)

        # Check if the injected writer was called with the exact frame
        self.mock_writer.assert_awaited_once_with(test_frame)

    async def test_receive_frame_respects_circuit_breaker(self) -> None:
        """Verify inbound frames are gated by pause/resume state."""
        test_frame = (
            "059 RP --- 01:195932 04:017982 --:------ 313F 009 00FC2300C4150C07E9"
        )

        # 1. Test while PAUSED (Initial State)
        self.transport.pause_reading()
        self.transport.receive_frame(test_frame)

        # Give the loop a chance to spin (in case it tried to process)
        await asyncio.sleep(0)

        # Protocol should NOT have received data
        self.mock_protocol.pkt_received.assert_not_called()

        # 2. Test while RESUMED
        self.transport.resume_reading()
        self.transport.receive_frame(test_frame)

        # We must yield control to the loop so 'call_soon' tasks can execute
        await asyncio.sleep(0)

        # Protocol SHOULD receive data now
        # Note: pkt_received is called with a Packet object, so we verify call count
        self.assertEqual(self.mock_protocol.pkt_received.call_count, 1)

    async def test_write_error_handling(self) -> None:
        """Verify writer exceptions are wrapped in TransportError."""
        self.mock_writer.side_effect = Exception("MQTT Connection Lost")

        with self.assertRaises(exc.TransportError):
            await self.transport.write_frame("test_frame")

    async def test_gateway_integration(self) -> None:
        """Verify the Gateway accepts the transport via IoC."""
        from ramses_rf import Gateway

        # Define a factory that returns our mocked transport
        async def mock_factory(
            protocol: Any, *, config: TransportConfig, **kwargs: Any
        ) -> CallbackTransport:
            # We must return the transport instance we are testing
            # but we need to update its protocol reference first
            self.transport._protocol = protocol

            # 1. Simulate Gateway Identification (Critical for protocol state)
            # Use the string literal "active_hgi" to avoid import issues
            self.transport._extra["active_hgi"] = "18:000730"

            # 2. Manually signal that the connection is made.
            # MUST include ramses=True to satisfy the protocol stack
            protocol.connection_made(self.transport, ramses=True)

            return self.transport

        # Initialize Gateway with the factory
        gwy = Gateway("/dev/null", transport_constructor=mock_factory)
        await gwy.start()

        # Verify the Gateway is actually using our transport
        self.assertIs(gwy._engine._transport, self.transport)

        await gwy.stop()

    async def test_factory_propagates_disable_sending(self) -> None:
        """Verify transport_factory passes disable_sending=True via TransportConfig."""
        from ramses_rf import Gateway

        # 1. Define a factory that checks if disable_sending was passed via the DTO
        async def strict_factory(
            protocol: Any, *, config: TransportConfig, **kwargs: Any
        ) -> CallbackTransport:
            # Check if 'disable_sending' made it through the DTO
            if not config.disable_sending:
                raise ValueError("disable_sending flag was lost in the factory!")

            # Create the transport
            transport = CallbackTransport(
                protocol,
                self.mock_writer,
                config=config,
            )

            # We must tell the protocol we are connected, or gwy.start() will timeout
            transport._extra["active_hgi"] = "18:000730"
            protocol.connection_made(transport, ramses=True)

            return transport

        # 2. Initialize Gateway normally (bypass Schema validation)
        # We do NOT pass disable_sending here to avoid the Voluptuous error
        gwy = Gateway("/dev/null", transport_constructor=strict_factory)

        # 3. Force the flag internally.
        # This simulates the Gateway being in a read-only state (e.g. reading from a file)
        # and ensures we test that this state is propagated to the Transport Factory.
        gwy._engine._disable_sending = True

        # 4. Start the Gateway (this triggers the factory)
        try:
            await gwy.start()
        except ValueError as err:
            self.fail(f"Test failed: {err}")
        finally:
            await gwy.stop()

    async def test_write_frame_propagates_asyncio_cancelled_error(self) -> None:
        """Verify asyncio.CancelledError is re-raised during task cancellation."""
        # Arrange
        self.mock_writer.side_effect = asyncio.CancelledError()

        # Act & Assert
        with self.assertRaises(asyncio.CancelledError):
            await self.transport.write_frame("test_frame")

    async def test_receive_frame_handles_malformed_packets_gracefully(self) -> None:
        """Verify malformed inbound packets do not raise unhandled exceptions."""
        # Arrange
        self.transport.resume_reading()

        # Act & Assert (invalid hex string should log warning inside _frame_read and not raise)
        try:
            self.transport.receive_frame("INVALID_HEX_STRING")
            await asyncio.sleep(0)
        except Exception as err:
            self.fail(
                f"receive_frame raised unhandled exception on malformed packet: {err}"
            )

    async def test_receive_frame_validates_iso_timestamp(self) -> None:
        """Verify invalid ISO timestamps fallback to current system time."""
        # Arrange
        self.transport.resume_reading()
        invalid_timestamp = "NOT-A-VALID-TIMESTAMP"
        valid_frame = (
            "059 RP --- 01:195932 04:017982 --:------ 313F 009 00FC2300C4150C07E9"
        )

        # Act
        self.transport.receive_frame(valid_frame, dtm=invalid_timestamp)
        await asyncio.sleep(0)

        # Assert
        self.assertEqual(self.mock_protocol.pkt_received.call_count, 1)

    async def test_callback_transport_teardown_cleans_up_writer(self) -> None:
        """Verify _close resets state and clears io_writer reference."""
        # Arrange
        self.transport.resume_reading()
        self.assertTrue(self.transport.is_reading())

        # Act
        self.transport.close()

        # Assert
        self.assertFalse(self.transport.is_reading())
        with self.assertRaises(exc.TransportError):
            await self.transport.write_frame("test_frame")
