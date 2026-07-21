from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from serial import SerialException

from ramses_tx.const import SZ_ACTIVE_HGI, SZ_SIGNATURE, Code
from ramses_tx.exceptions import TransportSerialError
from ramses_tx.transport.port import PortTransport, limit_duty_cycle

pytestmark = pytest.mark.asyncio


def _get_transport() -> PortTransport:
    # Helper to instantiate a PortTransport with safely mocked deps
    mock_serial = MagicMock()
    mock_serial.name = "/dev/ttyUSB0"
    mock_serial.fileno.return_value = 1  # Prevent dummy FD errors
    mock_protocol = MagicMock()
    mock_config = MagicMock()

    loop = asyncio.get_running_loop()

    with (
        patch.object(loop, "add_reader"),
        patch.object(loop, "remove_reader"),
        patch("ramses_tx.transport.port.is_hgi80", AsyncMock()),
    ):
        transport = PortTransport(
            mock_serial,
            mock_protocol,
            config=mock_config,
            extra={},
        )

    # Cancel the auto-started connection task to prevent double-execution
    # when tests manually invoke `await transport._create_connection()`.
    for task in asyncio.all_tasks():
        if task.get_name() == "PortTransport._create_connection()":
            task.cancel()

    return transport


async def test_limit_duty_cycle_decorator_limits_execution() -> None:
    # Dummy class to apply the active duty cycle decorator
    class DummyTransport:
        def __init__(self) -> None:
            self._tx_bits_in_bucket: float | None = None
            self._tx_last_time_bit_added: float | None = None

        @limit_duty_cycle(0.01, 3600)
        async def write(self, frame: str) -> None:
            pass

    transport = DummyTransport()

    # The first write should initialize and partially empty the bucket
    await transport.write("1" * 50)
    assert transport._tx_bits_in_bucket is not None
    assert transport._tx_last_time_bit_added is not None


async def test_limit_duty_cycle_decorator_null_wrapper() -> None:
    # Dummy class to test when limits are disabled (<= 0)
    class DummyTransport:
        @limit_duty_cycle(0)
        async def write(self, frame: str) -> None:
            pass

    transport = DummyTransport()
    # Should safely execute without setting any duty cycle bucket limits
    await transport.write("1" * 50)
    assert not hasattr(transport, "_tx_bits_in_bucket")


async def test_port_transport_init_initializes_future_immediately() -> None:
    # Issue #583 Fix Test: Ensure future exists before I/O fires
    transport = _get_transport()

    assert hasattr(transport, "_init_fut")
    assert isinstance(transport._init_fut, asyncio.Future)
    assert not transport._init_fut.done()

    # Simulate read_ready firing immediately before any connection tasks
    transport.serial.read.return_value = b"000 00:000000 00:000000 00\r\n"
    transport._frame_read = MagicMock()

    # This previously raised AttributeError: 'PortTransport' has no _init_fut
    transport._read_ready()

    transport._close()


async def test_create_connection_sans_signature() -> None:
    # Test skipping signature polling when sending is disabled
    transport = _get_transport()
    transport._disable_sending = True
    transport._make_connection = MagicMock()

    with patch("ramses_tx.transport.port.is_hgi80", AsyncMock()):
        await transport._create_connection()
        await transport._init_task  # Await the actual initialization task

    assert transport._init_fut.done()
    assert transport._init_fut.result() is None
    transport._make_connection.assert_called_once_with(gwy_id=None)
    transport._close()


async def test_create_connection_with_signature_success() -> None:
    # Test polling for signature and properly mapping the active HGI
    transport = _get_transport()
    transport._disable_sending = False
    transport._make_connection = MagicMock()
    transport._write_frame = AsyncMock()

    mock_pkt = MagicMock()
    mock_pkt.src.id = "18:123456"

    mock_sig = MagicMock()
    mock_sig.payload = "00"
    mock_sig.__str__.return_value = "000 18:000000 18:000000 1234 001 00"

    # Simulate the packet echo being received immediately after write
    async def delayed_resolve(*args: Any, **kwargs: Any) -> None:
        if not transport._init_fut.done():
            transport._init_fut.set_result(mock_pkt)

    transport._write_frame.side_effect = delayed_resolve

    with (
        patch("ramses_tx.transport.port.is_hgi80", AsyncMock()),
        patch(
            "ramses_tx.transport.port.CommandDTO",
            return_value=mock_sig,
        ),
    ):
        await transport._create_connection()
        await transport._init_task  # Await the actual initialization task

    assert transport._init_fut.done()
    assert transport._init_fut.result() == mock_pkt
    transport._make_connection.assert_called_once_with(gwy_id="18:123456")
    transport._close()


async def test_create_connection_with_signature_timeout() -> None:
    # Test timeout raising TransportSerialError when no signature replies
    transport = _get_transport()
    transport._disable_sending = False

    with (
        patch("ramses_tx.transport.port.is_hgi80", AsyncMock()),
        patch("ramses_tx.transport.port.CommandDTO", MagicMock()),
        patch("asyncio.wait_for", side_effect=TimeoutError),
        pytest.raises(TransportSerialError),
    ):
        await transport._create_connection()

    transport._close()


async def test_read_ready_processes_buffer_lines() -> None:
    # Test byte buffer accumulation and splitting on newlines
    transport = _get_transport()
    transport._frame_read = MagicMock()
    transport._dt_now = MagicMock()

    # Split lines to ensure buffer concatenates properly
    transport.serial.read.side_effect = [b"000 ", b"18:111111 00\r\n", b""]

    transport._read_ready()
    transport._frame_read.assert_not_called()  # No newline, no call

    transport._read_ready()
    assert transport._frame_read.call_count == 1  # Reached newline

    transport._close()


async def test_read_ready_handles_serial_exception() -> None:
    # Test safe abortion on serial disconnection
    transport = _get_transport()
    transport._close = MagicMock()
    transport._closing = False

    transport.serial.read.side_effect = SerialException("Test Disconnect")

    transport._read_ready()
    transport._close.assert_called_once()
    transport._close.reset_mock()

    # Ensure it doesn't try to close again if already closing
    transport._closing = True
    transport._read_ready()
    transport._close.assert_not_called()


async def test_pkt_read_resolves_init_fut_on_signature_echo() -> None:
    # Test packet inspection successfully resolving the signature
    transport = _get_transport()
    transport._extra[SZ_SIGNATURE] = "00"

    mock_pkt = MagicMock()
    mock_pkt.code = Code._PUZZ
    mock_pkt.payload = "00"
    mock_pkt.src.id = "18:000000"

    with patch("ramses_tx.transport.base._FullTransport._pkt_read"):
        transport._pkt_read(mock_pkt)

    assert transport._init_fut.done()
    assert transport._init_fut.result() == mock_pkt
    assert transport._extra.get(SZ_ACTIVE_HGI) == "18:000000"
    transport._close()


async def test_write_frame_acquires_semaphore_and_writes() -> None:
    # Ensure traffic is gated by the leaking semaphore
    transport = _get_transport()
    transport._leaker_sem = AsyncMock()

    with patch(
        "ramses_tx.transport.base._FullTransport.write_frame",
        AsyncMock(),
    ):
        await transport.write_frame("000 18:111111 18:222222 1234 001 00")

    transport._leaker_sem.acquire.assert_called_once()
    transport._close()


async def test_write_frame_catches_serial_exception() -> None:
    # Test abortion flow when underlying serial write fails
    transport = _get_transport()
    transport._write = MagicMock(side_effect=SerialException("Write Error"))
    transport._abort = MagicMock()

    await transport._write_frame("000 18:111111 18:222222 1234 001 00")

    transport._abort.assert_called_once()
    transport._close()


async def test_abort_and_close_cancels_tasks() -> None:
    # Test graceful teardown
    transport = _get_transport()

    # Populate tasks directly
    mock_init_task = MagicMock(spec=asyncio.Task)
    mock_leaker_task = MagicMock(spec=asyncio.Task)
    transport._init_task = mock_init_task
    transport._leaker_task = mock_leaker_task

    with patch("ramses_tx.transport.base._FullTransport._close"):
        transport._close()

    mock_init_task.cancel.assert_called_once()
    mock_leaker_task.cancel.assert_called_once()

    # Patch the _abort method residing on the _PortTransportAbstractor class
    with patch("ramses_tx.transport.port._PortTransportAbstractor._abort", create=True):
        transport._abort(SerialException("Fatal"))

    assert mock_init_task.cancel.call_count == 2
    assert mock_leaker_task.cancel.call_count == 2
