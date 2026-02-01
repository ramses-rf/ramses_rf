#!/usr/bin/env python3
"""Tests for CallbackTransport initialization logic."""

from typing import Any
from unittest.mock import AsyncMock, Mock, patch

from ramses_tx.transport import CallbackTransport, transport_factory


async def _async_callback_factory(*args: Any, **kwargs: Any) -> CallbackTransport:
    """Async wrapper for CallbackTransport to satisfy transport_factory signature."""
    return CallbackTransport(*args, **kwargs)


async def test_callback_transport_handshake() -> None:
    """Test that connection_made is called automatically upon initialization."""
    mock_protocol = Mock()
    mock_writer = AsyncMock()

    transport = CallbackTransport(mock_protocol, mock_writer)

    # Assert handshake called immediately
    mock_protocol.connection_made.assert_called_once_with(transport, ramses=True)


async def test_callback_transport_handshake_idempotency() -> None:
    """Test that manual connection_made calls are safe (idempotent at protocol level)."""
    mock_protocol = Mock()
    mock_writer = AsyncMock()

    transport = CallbackTransport(mock_protocol, mock_writer)

    # Verify initial call
    mock_protocol.connection_made.assert_called_once()

    # Manually call again (simulating legacy consumer behavior)
    mock_protocol.connection_made(transport, ramses=True)

    # Assert called twice without error (protocol impl handles idempotency logic)
    assert mock_protocol.connection_made.call_count == 2


async def test_callback_transport_autostart_false() -> None:
    """Test that reading is paused by default (autostart=False)."""
    mock_protocol = Mock()
    mock_writer = AsyncMock()

    transport = CallbackTransport(mock_protocol, mock_writer, autostart=False)

    assert transport.is_reading() is False


async def test_callback_transport_autostart_default() -> None:
    """Test that reading is paused by default (backward compatibility)."""
    mock_protocol = Mock()
    mock_writer = AsyncMock()

    transport = CallbackTransport(mock_protocol, mock_writer)

    assert transport.is_reading() is False


async def test_callback_transport_autostart_true() -> None:
    """Test that reading is resumed automatically if autostart=True."""
    mock_protocol = Mock()
    mock_writer = AsyncMock()

    transport = CallbackTransport(mock_protocol, mock_writer, autostart=True)

    assert transport.is_reading() is True


async def test_factory_routes_autostart_to_custom_constructor() -> None:
    """Check that autostart is passed to a custom transport_constructor."""
    mock_protocol = Mock()
    mock_writer = AsyncMock()

    # 1. Test with autostart=True
    # NOTE: transport_factory awaits the constructor, so we must pass an async callable
    transport = await transport_factory(
        mock_protocol,
        transport_constructor=_async_callback_factory,
        io_writer=mock_writer,
        autostart=True,
    )
    assert isinstance(transport, CallbackTransport)
    assert transport.is_reading() is True

    # 2. Test with autostart=False (default)
    transport_paused = await transport_factory(
        mock_protocol,
        transport_constructor=_async_callback_factory,
        io_writer=mock_writer,
        autostart=False,
    )
    assert isinstance(transport_paused, CallbackTransport)
    assert transport_paused.is_reading() is False


async def test_factory_strips_autostart_for_standard_transport() -> None:
    """Check that autostart is REMOVED before calling standard transports.

    If it isn't removed, the standard transports (PortTransport/MqttTransport)
    would raise TypeError because they don't accept 'autostart' in __init__.
    """
    mock_protocol = Mock()
    mock_protocol.wait_for_connection_made = AsyncMock()

    # We must patch serial_for_url because transport_factory calls it via
    # get_serial_instance BEFORE creating PortTransport.
    with (
        patch("ramses_tx.transport.PortTransport") as MockPortTransport,
        patch("ramses_tx.transport.serial_for_url") as mock_serial_for_url,
    ):
        # Setup the mock serial object to pass validity checks
        mock_serial = Mock()
        mock_serial.portstr = "/dev/ttyUSB0"
        mock_serial_for_url.return_value = mock_serial

        # valid-looking config so factory enters the Serial branch
        port_config: Any = {}

        await transport_factory(
            mock_protocol,
            port_name="/dev/ttyUSB0",
            port_config=port_config,
            autostart=True,  # This argument must be filtered out!
        )

        # Assert PortTransport was called
        assert MockPortTransport.call_count == 1

        # Verify 'autostart' was NOT in the call args
        call_args = MockPortTransport.call_args
        assert "autostart" not in call_args.kwargs
        assert "autostart" not in call_args.args  # just in case


async def test_factory_strips_autostart_for_mqtt_transport() -> None:
    """Check that autostart is REMOVED before calling MqttTransport."""
    mock_protocol = Mock()
    mock_protocol.wait_for_connection_made = AsyncMock()

    with patch("ramses_tx.transport.MqttTransport") as MockMqttTransport:
        # valid-looking config so factory enters the MQTT branch
        # We must provide port_config because transport_factory validates it
        # is not None even for MQTT
        port_config: Any = {}

        await transport_factory(
            mock_protocol,
            port_name="mqtt://broker:1883",
            port_config=port_config,
            autostart=True,  # This must be filtered out
        )

        assert MockMqttTransport.call_count == 1
        call_args = MockMqttTransport.call_args
        assert "autostart" not in call_args.kwargs
