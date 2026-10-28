#!/usr/bin/env python3
"""Tests for CallbackTransport initialization logic."""

import asyncio
from functools import partial
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from ramses_tx import exceptions as exc
from ramses_tx.const import Verb
from ramses_tx.discovery import is_hgi80
from ramses_tx.transport import TransportConfig, transport_factory
from ramses_tx.transport.callback import CallbackTransport
from ramses_tx.transport.file import FileTransport
from ramses_tx.typing import SerPortNameT


async def _async_callback_factory(
    protocol: Any,
    io_writer: Any = None,
    *,
    config: TransportConfig,
    **kwargs: Any,
) -> CallbackTransport:
    """Async wrapper for CallbackTransport to satisfy transport_factory signature."""
    return CallbackTransport(protocol, io_writer, config=config, **kwargs)


@pytest.mark.parametrize(
    ("line", "timestamp"),
    (
        (
            "2026-10-28 19:08:21.449000 "
            "-56  I --- 04:003882 --:------ 01:105025 3150 002 0200",
            "2026-10-28 19:08:21.449000",
        ),
        (
            "2026-10-28T19:08:21.449000 "
            "-56  I --- 04:003882 --:------ 01:105025 3150 002 0200",
            "2026-10-28T19:08:21.449000",
        ),
        (
            "2026-10-28T19:08:21.449000+00:00 "
            "-56  I --- 04:003882 --:------ 01:105025 3150 002 0200",
            "2026-10-28T19:08:21.449000+00:00",
        ),
    ),
)
async def test_file_transport_splits_naive_and_aware_timestamps(
    line: str, timestamp: str
) -> None:
    """Packet-log timestamps may include a UTC offset."""
    transport = object.__new__(FileTransport)
    transport._process_line = AsyncMock()  # type: ignore[method-assign]

    await transport._process_line_from_raw(line)

    transport._process_line.assert_awaited_once_with(
        timestamp,
        "-56  I --- 04:003882 --:------ 01:105025 3150 002 0200",
    )


async def test_callback_transport_handshake() -> None:
    """Test that connection_made is called automatically upon initialization."""
    mock_protocol = Mock()
    mock_writer = AsyncMock()

    transport = CallbackTransport(
        mock_protocol, mock_writer, config=TransportConfig()
    )

    # Assert handshake called immediately
    mock_protocol.connection_made.assert_called_once_with(
        transport, ramses=True
    )


async def test_callback_transport_handshake_idempotency() -> None:
    """Test that manual connection_made calls are safe (idempotent at protocol level)."""
    mock_protocol = Mock()
    mock_writer = AsyncMock()

    transport = CallbackTransport(
        mock_protocol, mock_writer, config=TransportConfig()
    )

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

    transport = CallbackTransport(
        mock_protocol, mock_writer, config=TransportConfig(autostart=False)
    )

    assert transport.is_reading() is False


async def test_callback_transport_autostart_default() -> None:
    """Test that reading is paused by default (backward compatibility)."""
    mock_protocol = Mock()
    mock_writer = AsyncMock()

    transport = CallbackTransport(
        mock_protocol, mock_writer, config=TransportConfig()
    )

    assert transport.is_reading() is False


async def test_callback_transport_autostart_true() -> None:
    """Test that reading is resumed automatically if autostart=True."""
    mock_protocol = Mock()
    mock_writer = AsyncMock()

    transport = CallbackTransport(
        mock_protocol, mock_writer, config=TransportConfig(autostart=True)
    )

    assert transport.is_reading() is True


async def test_factory_routes_autostart_to_custom_constructor() -> None:
    """Check that autostart is passed to a custom transport_constructor."""
    mock_protocol = Mock()
    mock_writer = AsyncMock()

    # 1. Test with autostart=True
    # NOTE: transport_factory awaits the constructor, so we pass an async callable via partial
    transport = await transport_factory(
        mock_protocol,
        transport_constructor=partial(
            _async_callback_factory, io_writer=mock_writer
        ),
        config=TransportConfig(autostart=True),
    )
    assert isinstance(transport, CallbackTransport)
    assert transport.is_reading() is True

    # 2. Test with autostart=False (default)
    transport_paused = await transport_factory(
        mock_protocol,
        transport_constructor=partial(
            _async_callback_factory, io_writer=mock_writer
        ),
        config=TransportConfig(autostart=False),
    )
    assert isinstance(transport_paused, CallbackTransport)
    assert transport_paused.is_reading() is False


async def test_factory_passes_config_to_standard_transport() -> None:
    """Check that config is strictly passed to standard transports.

    If it isn't passed, the standard transports (PortTransport/MqttTransport)
    would raise TypeError missing 'config' in __init__.
    """
    mock_protocol = Mock()
    mock_protocol.wait_for_connection_made = AsyncMock()

    # We patch where they are USED (factory.py), not where they are DEFINED
    with patch(
        "ramses_tx.transport.factory.PortTransport"
    ) as MockPortTransport:
        # valid-looking config so factory enters the Serial branch
        port_config: Any = {}
        transport_config = TransportConfig(autostart=True)

        await transport_factory(
            mock_protocol,
            port_name=SerPortNameT("/dev/ttyUSB0"),
            port_config=port_config,
            config=transport_config,
        )

        # Assert PortTransport was called
        assert MockPortTransport.call_count == 1

        # Verify 'autostart' was NOT in the call args
        call_args = MockPortTransport.call_args
        assert "config" in call_args.kwargs
        assert call_args.kwargs["config"] is transport_config


async def test_factory_passes_config_to_mqtt_transport() -> None:
    """Check that config is strictly passed to MqttTransport."""
    mock_protocol = Mock()
    mock_protocol.wait_for_connection_made = AsyncMock()

    # We patch where it is USED (factory.py)
    with patch(
        "ramses_tx.transport.factory.MqttTransport"
    ) as MockMqttTransport:
        # valid-looking config so factory enters the MQTT branch
        # We must provide port_config because transport_factory validates it
        # is not None even for MQTT
        port_config: Any = {}
        transport_config = TransportConfig(autostart=True)

        await transport_factory(
            mock_protocol,
            port_name=SerPortNameT("mqtt://broker:1883"),
            port_config=port_config,
            config=transport_config,
        )

        assert MockMqttTransport.call_count == 1
        call_args = MockMqttTransport.call_args
        assert "config" in call_args.kwargs
        assert call_args.kwargs["config"] is transport_config


async def test_port_transport_close_robustness() -> None:
    """Check that PortTransport.close() does not raise AttributeError if init failed.

    This ensures that _close() checks for the existence of _init_task before
    attempting to cancel it.
    """
    from ramses_tx.transport.port import PortTransport

    mock_protocol = Mock()
    mock_serial = Mock()

    transport = PortTransport(
        mock_serial, mock_protocol, config=TransportConfig()
    )

    # Cancel auto-started connection task to avoid unawaited task warnings
    for task in asyncio.all_tasks():
        if task.get_name() == "PortTransport._create_connection()":
            task.cancel()

    # Pre-condition: _init_task is created asynchronously, so it shouldn't exist yet
    # because we haven't yielded to the event loop
    assert not hasattr(transport, "_init_task")

    # Execute close - should not raise AttributeError
    transport.close()


async def test_is_hgi80_async_file_check() -> None:
    """Check that is_hgi80 uses loop.run_in_executor for file existence checks."""

    # We define a path that contains "by-id" and "evofw3".
    # This ensures that is_hgi80 returns False immediately after the file check,
    # preventing it from proceeding to the complex 'comports' logic which triggers I/O.
    test_port = SerPortNameT("/dev/serial/by-id/usb-SparkFun_evofw3_TEST")

    # 1. Test: File exists (should return False due to 'evofw3' in name)
    # We patch os.path.exists where it is used (port.py)
    with patch(
        "ramses_tx.discovery.os.path.exists", return_value=True
    ) as mock_exists:
        result = await is_hgi80(test_port)

        # Assert: os.path.exists was called with the correct path
        mock_exists.assert_called_once_with(test_port)
        # Assert: Logic correctly identified it as NOT HGI80 (due to evofw3 name)
        assert result is False

    # 2. Test: File does NOT exist (should raise TransportSerialError)
    # We patch os.path.exists to return False
    with patch(
        "ramses_tx.discovery.os.path.exists", return_value=False
    ) as mock_exists:
        with pytest.raises(exc.TransportSerialError):
            await is_hgi80(test_port)

        # Assert: os.path.exists was called
        mock_exists.assert_called_once_with(test_port)


# --- Event-loop-closed guards (issue 802) ---


def _make_read_transport(loop: asyncio.AbstractEventLoop) -> Any:
    """Create a minimal _ReadTransport for testing _packet_read/_close."""
    from ramses_tx.transport.base import _ReadTransport

    transport = _ReadTransport.__new__(_ReadTransport)
    transport._loop = loop
    transport._protocol = Mock()
    transport._closing = False
    transport._reading = False
    transport._this_packet = None
    transport._prev_packet = None
    transport._extra = {}
    transport._evofw_flag = None
    return transport


async def test_packet_read_raises_transport_error_when_loop_closed() -> None:
    """_packet_read raises TransportError (not RuntimeError) when loop is closed.

    This is the fix for issue 802: the paho-mqtt thread receives a message
    after the asyncio loop has been closed, and call_soon_threadsafe would
    raise RuntimeError('Event loop is closed').
    """
    mock_loop = MagicMock(spec=asyncio.AbstractEventLoop)
    mock_loop.is_closed.return_value = True
    transport = _make_read_transport(mock_loop)

    from ramses_tx import Packet

    packet = Packet.from_file(
        "2026-07-13T04:40:36",
        "045  I --- 18:130140 32:022222 --:------ 22F2 001 00",
    )
    with pytest.raises(exc.TransportError, match="Event loop is closed"):
        transport._packet_read(packet)


async def test_packet_read_raises_transport_error_on_runtime_error() -> None:
    """_packet_read catches RuntimeError from call_soon_threadsafe and wraps it.

    Even if is_closed() returns False, the loop may close between the check
    and the call (race condition). The RuntimeError is caught and re-raised
    as TransportError so the MQTT _on_message handler can suppress it.
    """
    mock_loop = MagicMock(spec=asyncio.AbstractEventLoop)
    mock_loop.is_closed.return_value = False
    mock_loop.call_soon_threadsafe.side_effect = RuntimeError(
        "Event loop is closed"
    )
    transport = _make_read_transport(mock_loop)

    from ramses_tx import Packet

    packet = Packet.from_file(
        "2026-07-13T04:40:36",
        "045  I --- 18:130140 32:022222 --:------ 22F2 001 00",
    )

    with pytest.raises(exc.TransportError, match="Event loop is closed"):
        transport._packet_read(packet)


async def test_close_does_not_crash_when_loop_closed() -> None:
    """_close() does not raise when the event loop is already closed."""
    mock_loop = MagicMock(spec=asyncio.AbstractEventLoop)
    mock_loop.is_closed.return_value = True
    transport = _make_read_transport(mock_loop)

    # Should not raise
    transport._close(None)

    assert transport._closing is True
    # protocol.connection_lost should NOT have been called
    transport._protocol.connection_lost.assert_not_called()


async def test_close_catches_runtime_error_from_call_soon() -> None:
    """_close() catches RuntimeError if loop closes between check and call."""
    loop = asyncio.get_event_loop()
    transport = _make_read_transport(loop)

    with (
        patch.object(type(loop), "is_closed", return_value=False),
        patch.object(
            loop,
            "call_soon_threadsafe",
            side_effect=RuntimeError("Event loop is closed"),
        ),
    ):
        # Should not raise
        transport._close(None)

    assert transport._closing is True


async def test_make_connection_does_not_crash_when_loop_closed() -> None:
    """_make_connection() does not raise when the event loop is closed."""
    loop = asyncio.get_event_loop()
    transport = _make_read_transport(loop)

    with patch.object(type(loop), "is_closed", return_value=True):
        # Should not raise
        transport._make_connection("18:130140")

    # protocol.connection_made should NOT have been called
    transport._protocol.connection_made.assert_not_called()


async def test_write_frame_dispatches_outbound_dto_with_is_tx_true() -> None:
    # Arrange
    from ramses_tx.dtos import PacketDTO
    from ramses_tx.transport.base import _FullTransport

    class DummyTransport(_FullTransport):
        def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
            super().__init__(
                config=TransportConfig(disable_sending=False), loop=loop
            )
            self._protocol = Mock()

        async def _write_frame(self, frame: str) -> None:
            pass

    loop = asyncio.get_event_loop()
    transport = DummyTransport(loop)
    frame = "RQ --- 18:071939 32:123456 --:------ 2210 001 00"

    # Act: Transmit frame
    await transport.write_frame(frame)

    # Assert: Protocol _msg_received received PacketDTO with is_tx=True
    assert transport._protocol._msg_received.called
    dto: PacketDTO = transport._protocol._msg_received.call_args[0][0]
    assert dto.is_tx is True
    assert dto.verb == Verb.RQ


async def test_is_recent_tx_matches_hgi80_echo_with_addr_substitution() -> (
    None
):
    """_is_recent_tx must match HGI80 echoes where addr1 has been substituted.

    The HGI80 hardware replaces the placeholder 18:000730 with its real ID in
    the echo.  The TX frame was recorded with 18:000730, so the echo's addr1
    (e.g. 18:002965) won't match directly — _is_recent_tx must check both
    variants (issue 835, cc 864).
    """
    from ramses_tx.transport.base import TransportConfig, _FullTransport

    class DummyTransport(_FullTransport):
        def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
            super().__init__(
                config=TransportConfig(disable_sending=False), loop=loop
            )
            self._protocol = Mock()

        async def _write_frame(self, frame: str) -> None:
            pass

    loop = asyncio.get_event_loop()
    transport = DummyTransport(loop)

    # TX frame uses the placeholder 18:000730 (as _patch_cmd_if_needed
    # ensures for HGI80).  01FF payload from issue 835.
    _payload = "008026262AD0008000143C28320000C10280800280FF80040020"
    tx_frame = f" W --- 18:000730 21:057310 --:------ 01FF 026 {_payload}"
    await transport.write_frame(tx_frame)

    # HGI80 echo arrives with the real HGI ID (18:002965) as addr1.
    echo_frame = (
        f"000  W --- 18:002965 21:057310 --:------ 01FF 026 {_payload}"
    )
    assert transport._is_recent_tx(echo_frame) is True


async def test_is_recent_tx_matches_evofw3_echo_directly() -> None:
    """_is_recent_tx still matches evofw3 echoes where addr1 is unchanged."""
    from ramses_tx.transport.base import TransportConfig, _FullTransport

    class DummyTransport(_FullTransport):
        def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
            super().__init__(
                config=TransportConfig(disable_sending=False), loop=loop
            )
            self._protocol = Mock()

        async def _write_frame(self, frame: str) -> None:
            pass

    loop = asyncio.get_event_loop()
    transport = DummyTransport(loop)

    _payload = "008026262AD0008000143C28320000C10280800280FF80040020"
    tx_frame = f" W --- 18:002965 21:057310 --:------ 01FF 026 {_payload}"
    await transport.write_frame(tx_frame)

    echo_frame = (
        f"000  W --- 18:002965 21:057310 --:------ 01FF 026 {_payload}"
    )
    assert transport._is_recent_tx(echo_frame) is True


async def test_is_recent_tx_rejects_unrelated_frame() -> None:
    """_is_recent_tx returns False for a frame that was not recently sent."""
    from ramses_tx.transport.base import TransportConfig, _FullTransport

    class DummyTransport(_FullTransport):
        def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
            super().__init__(
                config=TransportConfig(disable_sending=False), loop=loop
            )
            self._protocol = Mock()

        async def _write_frame(self, frame: str) -> None:
            pass

    loop = asyncio.get_event_loop()
    transport = DummyTransport(loop)

    _payload = "008026262AD0008000143C28320000C10280800280FF80040020"
    tx_frame = f" W --- 18:000730 21:057310 --:------ 01FF 026 {_payload}"
    await transport.write_frame(tx_frame)

    # Completely different frame
    other_frame = (
        "000  I --- 21:057310 18:002965 --:------ 22C9 006 00086608CA02"
    )
    assert transport._is_recent_tx(other_frame) is False


@pytest.mark.asyncio
async def test_normalise_evofw3_tx_echo_hash_prefix() -> None:
    """_normalise must convert evofw3 '# '-prefixed TX echoes to parseable frames.

    evofw3 sends TX echoes with a '#' prefix (e.g. '# I --- ...').
    Without normalisation, _partition() treats '#' as a comment delimiter,
    leaving an empty packet body → PacketInvalid("Null packet").

    The fix converts '# I --- ...' → '...  I --- ...' (1-char verbs get
    an extra space to form the canonical ' I') and '# RQ --- ...' →
    '... RQ --- ...' (2-char verbs need no extra space).

    See: https://github.com/ramses-rf/ramses_cc/issues/1067
    """
    from datetime import datetime as dt

    from ramses_tx.packet import Packet
    from ramses_tx.transport.helpers import _normalise

    test_cases = [
        "# I --- 37:123789 32:161442 --:------ 22F1 003 000207",
        "# I --- 37:123789 32:161442 --:------ 22F3 007 00120F03040404",
        "# W --- 37:123789 32:161442 --:------ 22F7 003 00FFEF",
        "# RQ --- 37:123789 32:161442 --:------ 10D0 001 00",
        "# RP --- 37:123789 32:161442 --:------ 10D0 002 00FF",
    ]

    for raw in test_cases:
        normalised = _normalise(raw)
        # Must not start with # (comment delimiter)
        assert not normalised.startswith("#"), (
            f"Normalised line still starts with #: {normalised!r}"
        )
        # Must parse as a valid packet
        pkt = Packet.from_file(dt.now().isoformat(), normalised, is_echo=True)
        dto = pkt.to_dto()
        assert dto.addr1 == "37:123789"
        assert dto.addr2 == "32:161442"


@pytest.mark.asyncio
async def test_normalise_evofw3_tx_echo_is_recent_tx_match() -> None:
    """_is_recent_tx must match evofw3 '# '-prefixed echoes after normalisation.

    After _normalise converts the '#' prefix, the echo frame must be
    detectable by _is_recent_tx so the FSM recognises it as a hardware
    echo and doesn't retry or timeout.

    See: https://github.com/ramses-rf/ramses_cc/issues/1067
    """
    from ramses_tx.transport.base import TransportConfig, _FullTransport
    from ramses_tx.transport.helpers import _normalise

    class DummyTransport(_FullTransport):
        def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
            super().__init__(
                config=TransportConfig(disable_sending=False), loop=loop
            )
            self._protocol = Mock()

        async def _write_frame(self, frame: str) -> None:
            pass

    loop = asyncio.get_event_loop()
    transport = DummyTransport(loop)

    # TX frame as sent by the protocol layer
    tx_frame = " I --- 37:123789 32:161442 --:------ 22F1 003 000207"
    await transport.write_frame(tx_frame)

    # evofw3 echo arrives with '# ' prefix
    echo_raw = "# I --- 37:123789 32:161442 --:------ 22F1 003 000207"
    echo_normalised = _normalise(echo_raw)
    assert transport._is_recent_tx(echo_normalised) is True


@pytest.mark.asyncio
async def test_normalise_does_not_affect_comment_lines() -> None:
    """_normalise must not alter lines starting with '#' that are not verb echoes."""
    from ramses_tx.transport.helpers import _normalise

    # Non-verb lines starting with # should be unchanged
    assert _normalise("# This is a comment") == "# This is a comment"
    assert _normalise("# Checksum error") == "# Checksum error"


@pytest.mark.asyncio
async def test_limit_duty_cycle_deducts_frame_bits() -> None:
    """Verify limit_duty_cycle tracks and deducts frame bits from bucket."""
    from ramses_tx.transport.port import limit_duty_cycle

    class MockPortTransport:
        def __init__(self) -> None:
            self._tx_bits_in_bucket: float | None = None
            self._tx_last_time_bit_added: float | None = None

        @limit_duty_cycle(max_duty_cycle=0.01)
        async def send(self, frame: str) -> None:
            pass

    transport = MockPortTransport()
    frame = "... RQ ... 18:000730 01:123456 --:------ 313F 001 00"

    # Act
    await transport.send(frame)

    # Assert
    assert transport._tx_bits_in_bucket is not None
    # Initial bucket is 38400 * 0.01 * 3600 = 1382400. Frame bits = 330 + len(frame[46:]) * 10 = 400
    assert transport._tx_bits_in_bucket < 1382400.0


@pytest.mark.asyncio
async def test_limit_duty_cycle_throttles_when_bucket_insufficient() -> None:
    """Verify limit_duty_cycle triggers asyncio.sleep when bucket is depleted."""
    from ramses_tx.transport.port import limit_duty_cycle

    class MockPortTransport:
        def __init__(self) -> None:
            from time import perf_counter

            self._tx_bits_in_bucket: float | None = 10.0  # severely depleted
            self._tx_last_time_bit_added: float | None = perf_counter()

        @limit_duty_cycle(max_duty_cycle=0.01)
        async def send(self, frame: str) -> None:
            pass

    transport = MockPortTransport()
    frame = "... RQ ... 18:000730 01:123456 --:------ 313F 001 00"

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        # Act
        await transport.send(frame)

        # Assert
        mock_sleep.assert_called_once()


@pytest.mark.asyncio
async def test_limit_duty_cycle_zero_returns_null_wrapper() -> None:
    """Verify limit_duty_cycle(0.0) bypasses bucket tracking."""
    from ramses_tx.transport.port import limit_duty_cycle

    class MockPortTransport:
        def __init__(self) -> None:
            self._tx_bits_in_bucket: float | None = None
            self._tx_last_time_bit_added: float | None = None

        @limit_duty_cycle(max_duty_cycle=0.0)
        async def send(self, frame: str) -> None:
            pass

    transport = MockPortTransport()
    frame = "... RQ ... 18:000730 01:123456 --:------ 313F 001 00"

    # Act
    await transport.send(frame)

    # Assert
    assert transport._tx_bits_in_bucket is None
