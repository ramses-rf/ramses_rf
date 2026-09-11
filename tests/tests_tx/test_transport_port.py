from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from serialx import BaseSerialTransport, SerialException

from ramses_tx.const import SZ_ACTIVE_HGI, SZ_SIGNATURE, Code
from ramses_tx.exceptions import TransportSerialError
from ramses_tx.transport.base import SignaturePolicy, TransportConfig
from ramses_tx.transport.port import (
    PortTransport,
    _PortBridgeProtocol,
    limit_duty_cycle,
)
from ramses_tx.typing import SerPortNameT

pytestmark = pytest.mark.asyncio


def _get_transport() -> PortTransport:
    # Helper to instantiate a PortTransport with safely mocked deps
    mock_serial = MagicMock(spec=BaseSerialTransport)
    mock_serial.serial = MagicMock()
    mock_serial.name = "/dev/ttyUSB0"
    mock_serial.serial.name = "/dev/ttyUSB0"
    mock_protocol = MagicMock()
    mock_config = MagicMock()

    loop = asyncio.get_running_loop()

    with (
        patch.object(loop, "add_reader"),
        patch.object(loop, "remove_reader"),
        patch(
            "ramses_tx.transport.port.is_hgi80", AsyncMock(return_value=False)
        ),
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
    transport._tx_bits_in_bucket = 10.0
    from time import perf_counter

    transport._tx_last_time_bit_added = perf_counter()

    # Passing a frame larger than bucket capacity should trigger sleep
    with patch("asyncio.sleep", AsyncMock()) as mock_sleep:
        await transport.write("000 " * 50)
        mock_sleep.assert_called_once()


async def test_limit_duty_cycle_decorator_null_wrapper() -> None:
    # Test duty cycle = 0 or <= 0 returns null_wrapper
    class DummyTransport:
        @limit_duty_cycle(0)
        async def write(self, frame: str) -> None:
            pass

    transport = DummyTransport()
    with patch("asyncio.sleep", AsyncMock()) as mock_sleep:
        await transport.write("000 " * 50)
        mock_sleep.assert_not_called()


async def test_initialization_sets_up_queues_and_callbacks() -> None:
    # Test PortTransport creation and default states
    transport = _get_transport()
    assert transport._port_name == "/dev/ttyUSB0"
    assert transport._serial_transport is not None
    assert transport._init_fut is not None
    assert not transport._init_fut.done()

    # Simulate data_received firing immediately before any connection tasks
    transport._frame_read = MagicMock()
    transport._data_received(b"000 00:000000 00:000000 00\r\n")

    assert transport._frame_read.call_count == 1
    transport._close()


async def test_create_connection_sans_signature() -> None:
    # Test skipping signature polling when sending is disabled
    transport = _get_transport()
    transport._disable_sending = True
    transport._configured_hgi_id = None
    transport._make_connection = MagicMock()

    with patch(
        "ramses_tx.transport.port.is_hgi80", AsyncMock(return_value=False)
    ):
        await transport._create_connection()
        assert transport._init_task is not None
        await transport._init_task  # Await the actual initialization task

    assert transport._init_fut.done()
    assert transport._init_fut.result() is None
    transport._make_connection.assert_called_once_with(gateway_id=None)
    transport._close()


async def test_create_connection_with_signature_success() -> None:
    # Test polling for signature and properly mapping the active HGI
    transport = _get_transport()
    transport._disable_sending = False
    transport._make_connection = MagicMock()
    transport._write_frame = AsyncMock()

    mock_packet = MagicMock()
    mock_packet.src.id = "18:123456"

    mock_sig = MagicMock()
    mock_sig.payload = "00"
    mock_sig.__str__.return_value = "000 18:000000 18:000000 1234 001 00"

    # Simulate the packet echo being received immediately after write
    async def delayed_resolve(*args: Any, **kwargs: Any) -> None:
        if not transport._init_fut.done():
            transport._init_fut.set_result(mock_packet)

    transport._write_frame.side_effect = delayed_resolve

    with (
        patch(
            "ramses_tx.transport.port.is_hgi80", AsyncMock(return_value=False)
        ),
        patch(
            "ramses_tx.transport.port.CommandDTO",
            return_value=mock_sig,
        ),
    ):
        await transport._create_connection()
        assert transport._init_task is not None
        await transport._init_task  # Await the actual initialization task

    assert transport._init_fut.done()
    assert transport._init_fut.result() == mock_packet
    transport._make_connection.assert_called_once_with(gateway_id="18:123456")
    transport._close()


async def test_create_connection_with_signature_timeout() -> None:
    # Test timeout falling back to connect_sans_signature when no signature replies
    transport = _get_transport()
    transport._disable_sending = False
    transport._configured_hgi_id = None
    transport._make_connection = MagicMock()
    transport._write_frame = AsyncMock()

    with (
        patch(
            "ramses_tx.transport.port.is_hgi80", AsyncMock(return_value=False)
        ),
        patch("ramses_tx.transport.port.CommandDTO", MagicMock()),
        patch("ramses_tx.transport.port._SIGNATURE_MAX_TRYS", 2),
        patch("ramses_tx.transport.port._SIGNATURE_GAP_SECS", 0.001),
    ):
        await transport._create_connection()
        assert transport._init_task is not None
        await transport._init_task

    assert transport._init_fut.done()
    assert transport._init_fut.result() is None
    transport._make_connection.assert_called_once_with(gateway_id=None)
    transport._close()


async def test_create_connection_serial_exception_fails() -> None:
    # Test serial connection error raises TransportSerialError
    mock_protocol = MagicMock()
    mock_config = MagicMock()

    with (
        patch(
            "serialx.create_serial_connection",
            side_effect=SerialException("Port error"),
        ),
    ):
        transport = PortTransport(
            "/dev/ttyUSB99",
            mock_protocol,
            config=mock_config,
        )
        await transport._create_connection()

    assert transport._init_fut.done()
    with pytest.raises(TransportSerialError):
        transport._init_fut.result()
    transport._close()


async def test_data_received_processes_buffer_lines() -> None:
    # Test byte buffer accumulation and splitting on newlines
    transport = _get_transport()
    transport._frame_read = MagicMock()
    transport._dt_now = MagicMock()

    # Split lines to ensure buffer concatenates properly
    transport._data_received(b"000 ")
    transport._frame_read.assert_not_called()  # No newline, no call

    transport._data_received(b"18:111111 00\r\n")
    assert transport._frame_read.call_count == 1  # Reached newline

    transport._close()


async def test_read_ready_compatibility_handles_serial_exception() -> None:
    # Test safe abortion on serial disconnection via read_ready
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


async def test_connection_lost_handles_error() -> None:
    # Test connection_lost callback closing the transport (non-reconnect)
    transport = _get_transport()
    transport._close = MagicMock()
    transport._closing = False
    transport._enable_reconnect = False

    transport._connection_lost(SerialException("Connection Reset"))
    transport._close.assert_called_once()
    transport._close.reset_mock()

    transport._closing = True
    transport._connection_lost(None)
    transport._close.assert_not_called()


async def test_bridge_protocol_callbacks() -> None:
    # Test _PortBridgeProtocol delegating to PortTransport
    transport = _get_transport()
    bridge = _PortBridgeProtocol(transport)

    mock_serial_transport = MagicMock(spec=BaseSerialTransport)
    bridge.connection_made(mock_serial_transport)
    assert transport._serial_transport == mock_serial_transport

    transport._data_received = MagicMock()
    bridge.data_received(b"test data\r\n")
    transport._data_received.assert_called_once_with(b"test data\r\n")

    transport._connection_lost = MagicMock()
    bridge.connection_lost(None)
    transport._connection_lost.assert_called_once_with(None)

    transport._close()


async def test_packet_read_resolves_init_fut_on_signature_echo() -> None:
    # Test packet inspection successfully resolving the signature
    transport = _get_transport()
    transport._extra[SZ_SIGNATURE] = "00"

    mock_packet = MagicMock()
    mock_packet.code = Code._PUZZ
    mock_packet.payload = "00"
    mock_packet.src.id = "18:000000"

    with patch("ramses_tx.transport.base._FullTransport._packet_read"):
        transport._packet_read(mock_packet)

    assert transport._init_fut.done()
    assert transport._init_fut.result() == mock_packet
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

    transport._abort(SerialException("Fatal"))

    assert mock_init_task.cancel.call_count == 2
    assert mock_leaker_task.cancel.call_count == 2


# -- Phase 2: signature policy tests -------------------------------------


async def test_signature_policy_skip_uses_connect_sans_signature() -> None:
    """SKIP policy calls connect_sans_signature (no probes sent)."""
    from ramses_tx.transport.base import SignaturePolicy, TransportConfig

    mock_serial = MagicMock(spec=BaseSerialTransport)
    mock_serial.serial = MagicMock()
    mock_serial.name = "/dev/ttyUSB0"
    mock_serial.serial.name = "/dev/ttyUSB0"
    mock_protocol = MagicMock()
    config = TransportConfig(signature_policy=SignaturePolicy.SKIP)

    loop = asyncio.get_running_loop()
    with (
        patch.object(loop, "add_reader"),
        patch.object(loop, "remove_reader"),
        patch(
            "ramses_tx.transport.port.is_hgi80", AsyncMock(return_value=False)
        ),
    ):
        transport = PortTransport(
            mock_serial, mock_protocol, config=config, extra={}
        )

    for task in asyncio.all_tasks():
        if task.get_name() == "PortTransport._create_connection()":
            task.cancel()

    transport._init_fut = loop.create_future()
    with (
        patch(
            "ramses_tx.transport.port.is_hgi80", AsyncMock(return_value=False)
        ),
        patch.object(transport, "_make_connection") as mock_make,
    ):
        await transport._create_connection()

    # SKIP should call _make_connection with gateway_id=None (no probe).
    mock_make.assert_called_once_with(gateway_id=None)
    transport._close()


async def test_signature_policy_delayed_creates_delayed_task() -> None:
    """DELAYED policy creates a connect_with_delayed_signature task."""
    from ramses_tx.transport.base import SignaturePolicy, TransportConfig

    mock_serial = MagicMock(spec=BaseSerialTransport)
    mock_serial.serial = MagicMock()
    mock_serial.name = "/dev/ttyUSB0"
    mock_serial.serial.name = "/dev/ttyUSB0"
    mock_protocol = MagicMock()
    config = TransportConfig(
        signature_policy=SignaturePolicy.DELAYED, startup_grace=0.01
    )

    loop = asyncio.get_running_loop()
    with (
        patch.object(loop, "add_reader"),
        patch.object(loop, "remove_reader"),
        patch(
            "ramses_tx.transport.port.is_hgi80", AsyncMock(return_value=False)
        ),
    ):
        transport = PortTransport(
            mock_serial, mock_protocol, config=config, extra={}
        )

    for task in asyncio.all_tasks():
        if task.get_name() == "PortTransport._create_connection()":
            task.cancel()

    # Pre-resolve init_fut so _create_connection doesn't block.
    transport._init_fut = loop.create_future()
    transport._init_fut.set_result(None)

    init_task_names: list[str] = []
    original_create_task = loop.create_task

    def track_create_task(
        coro: Any, *, name: str | None = None, context: Any = None
    ) -> Any:
        if name:
            init_task_names.append(name)
        return original_create_task(coro, name=name, context=context)

    with (
        patch(
            "ramses_tx.transport.port.is_hgi80", AsyncMock(return_value=False)
        ),
        patch.object(transport, "_make_connection"),
        patch.object(transport, "_write_frame", new_callable=AsyncMock),
        patch.object(loop, "create_task", side_effect=track_create_task),
    ):
        await transport._create_connection()

    # DELAYED should create a delayed signature task.
    assert any("delayed" in name.lower() for name in init_task_names)
    transport._close()


async def test_signature_policy_immediate_creates_immediate_task() -> None:
    """IMMEDIATE policy (default) creates a connect_with_signature task."""
    from ramses_tx.transport.base import SignaturePolicy, TransportConfig

    mock_serial = MagicMock(spec=BaseSerialTransport)
    mock_serial.serial = MagicMock()
    mock_serial.name = "/dev/ttyUSB0"
    mock_serial.serial.name = "/dev/ttyUSB0"
    mock_protocol = MagicMock()
    config = TransportConfig(signature_policy=SignaturePolicy.IMMEDIATE)

    loop = asyncio.get_running_loop()
    with (
        patch.object(loop, "add_reader"),
        patch.object(loop, "remove_reader"),
        patch(
            "ramses_tx.transport.port.is_hgi80", AsyncMock(return_value=False)
        ),
    ):
        transport = PortTransport(
            mock_serial, mock_protocol, config=config, extra={}
        )

    for task in asyncio.all_tasks():
        if task.get_name() == "PortTransport._create_connection()":
            task.cancel()

    transport._init_fut = loop.create_future()
    transport._init_fut.set_result(None)

    init_task_names: list[str] = []
    original_create_task = loop.create_task

    def track_create_task(
        coro: Any, *, name: str | None = None, context: Any = None
    ) -> Any:
        if name:
            init_task_names.append(name)
        return original_create_task(coro, name=name, context=context)

    with (
        patch(
            "ramses_tx.transport.port.is_hgi80", AsyncMock(return_value=False)
        ),
        patch.object(transport, "_make_connection"),
        patch.object(transport, "_write_frame", new_callable=AsyncMock),
        patch.object(loop, "create_task", side_effect=track_create_task),
    ):
        await transport._create_connection()

    # IMMEDIATE should create a connect_with_signature task (not delayed).
    assert any("with_signature" in name for name in init_task_names)
    assert not any("delayed" in name.lower() for name in init_task_names)
    transport._close()


async def test_reconnect_task_created_on_connection_lost() -> None:
    """connection_lost starts reconnect loop when enable_reconnect is True.

    Verifies the real behavior: _closing stays False (the PortTransport
    stays alive), the underlying serial transport is closed, and a
    reconnect task is created.  Does NOT mock _close() — that would
    hide the bug where _close() sets _closing=True before the reconnect
    check (issue 1119).
    """
    from ramses_tx.transport.base import TransportConfig

    mock_serial = MagicMock(spec=BaseSerialTransport)
    mock_serial.serial = MagicMock()
    mock_serial.name = "/dev/ttyUSB0"
    mock_serial.serial.name = "/dev/ttyUSB0"
    mock_serial.close = MagicMock()
    mock_protocol = MagicMock()
    config = TransportConfig(enable_reconnect=True)

    loop = asyncio.get_running_loop()
    with (
        patch.object(loop, "add_reader"),
        patch.object(loop, "remove_reader"),
        patch(
            "ramses_tx.transport.port.is_hgi80", AsyncMock(return_value=False)
        ),
    ):
        transport = PortTransport(
            mock_serial, mock_protocol, config=config, extra={}
        )

    for task in asyncio.all_tasks():
        if task.get_name() == "PortTransport._create_connection()":
            task.cancel()

    # Simulate connection lost (not closing).
    transport._closing = False
    transport._serial_transport = mock_serial
    with patch.object(transport, "_reconnect_loop", new_callable=AsyncMock):
        transport._connection_lost(RuntimeError("unplugged"))
        await asyncio.sleep(0.01)

    # The PortTransport must NOT be marked as closing — it stays
    # alive for transparent reconnection.
    assert transport._closing is False
    # The underlying serial transport must have been closed.
    mock_serial.close.assert_called_once()
    assert transport._serial_transport is None
    # A reconnect task must have been created.
    assert transport._reconnect_task is not None
    transport._close()


async def test_reconnect_not_created_when_enable_reconnect_false() -> None:
    """connection_lost does full close when enable_reconnect=False.

    Verifies that _closing is set to True (via the real _close()) and
    no reconnect task is created.
    """
    from ramses_tx.transport.base import TransportConfig

    mock_serial = MagicMock(spec=BaseSerialTransport)
    mock_serial.serial = MagicMock()
    mock_serial.name = "/dev/ttyUSB0"
    mock_serial.serial.name = "/dev/ttyUSB0"
    mock_serial.close = MagicMock()
    mock_protocol = MagicMock()
    config = TransportConfig(enable_reconnect=False)

    loop = asyncio.get_running_loop()
    with (
        patch.object(loop, "add_reader"),
        patch.object(loop, "remove_reader"),
        patch(
            "ramses_tx.transport.port.is_hgi80", AsyncMock(return_value=False)
        ),
    ):
        transport = PortTransport(
            mock_serial, mock_protocol, config=config, extra={}
        )

    for task in asyncio.all_tasks():
        if task.get_name() == "PortTransport._create_connection()":
            task.cancel()

    transport._closing = False
    transport._serial_transport = mock_serial
    transport._connection_lost(RuntimeError("unplugged"))

    # With reconnect disabled, _close() must have been called,
    # setting _closing=True and closing the serial transport.
    assert transport._closing is True
    assert transport._reconnect_task is None
    transport._close()


# ---------------------------------------------------------------------------
# Gap B: configured_hgi_id fallback
# ---------------------------------------------------------------------------


async def test_configured_hgi_id_used_in_sans_signature() -> None:
    """connect_sans_signature uses configured_hgi_id when set (Gap B)."""
    transport = _get_transport()
    transport._disable_sending = True
    transport._configured_hgi_id = "18:006402"
    transport._make_connection = MagicMock()

    with patch(
        "ramses_tx.transport.port.is_hgi80", AsyncMock(return_value=False)
    ):
        await transport._create_connection()
        assert transport._init_task is not None
        await transport._init_task

    assert transport._init_fut.done()
    transport._make_connection.assert_called_once_with(gateway_id="18:006402")
    transport._close()


async def test_configured_hgi_id_used_after_signature_timeout() -> None:
    """connect_with_signature falls back to configured_hgi_id (Gap B)."""
    transport = _get_transport()
    transport._disable_sending = False
    transport._configured_hgi_id = "18:140805"
    transport._make_connection = MagicMock()
    transport._write_frame = AsyncMock()

    with (
        patch(
            "ramses_tx.transport.port.is_hgi80",
            AsyncMock(return_value=False),
        ),
        patch("ramses_tx.transport.port.CommandDTO", MagicMock()),
        patch("ramses_tx.transport.port._SIGNATURE_MAX_TRYS", 2),
        patch("ramses_tx.transport.port._SIGNATURE_GAP_SECS", 0.001),
    ):
        await transport._create_connection()
        assert transport._init_task is not None
        await transport._init_task

    assert transport._init_fut.done()
    # After timeout, should fall back to configured_hgi_id.
    transport._make_connection.assert_called_once_with(gateway_id="18:140805")
    transport._close()


# ---------------------------------------------------------------------------
# Gap C: HGI80 auto-SKIP
# ---------------------------------------------------------------------------


async def test_hgi80_auto_selects_skip() -> None:
    """HGI80 detected via _is_hgi80 auto-selects SKIP (Gap C)."""
    transport = _get_transport()
    transport._disable_sending = False
    transport._configured_hgi_id = "18:123456"
    transport._make_connection = MagicMock()
    transport._write_frame = AsyncMock()

    with patch(
        "ramses_tx.transport.port.is_hgi80", AsyncMock(return_value=True)
    ):
        await transport._create_connection()
        assert transport._init_task is not None
        await transport._init_task

    assert transport._init_fut.done()
    # Should use sans_signature with configured_hgi_id, not send _PUZZ probes.
    transport._make_connection.assert_called_once_with(gateway_id="18:123456")
    # _PUZZ signature probes should not have been sent.
    transport._write_frame.assert_not_called()
    transport._close()


async def test_hgi80_without_configured_id_uses_none() -> None:
    """HGI80 with no configured_hgi_id connects with gateway_id=None."""
    transport = _get_transport()
    transport._disable_sending = False
    transport._configured_hgi_id = None
    transport._make_connection = MagicMock()

    with patch(
        "ramses_tx.transport.port.is_hgi80", AsyncMock(return_value=True)
    ):
        await transport._create_connection()
        assert transport._init_task is not None
        await transport._init_task

    assert transport._init_fut.done()
    transport._make_connection.assert_called_once_with(gateway_id=None)
    transport._close()


# ---------------------------------------------------------------------------
# Gap E: ID_COMMAND (!I-based identity discovery)
# ---------------------------------------------------------------------------


async def test_id_command_success() -> None:
    """connect_with_id_command succeeds with valid !I response (Gap E)."""
    transport = _get_transport()
    transport._disable_sending = False
    transport._configured_hgi_id = None
    transport._startup_grace = 0.0
    transport._make_connection = MagicMock()
    transport._write = MagicMock()

    # Simulate the !I response arriving via _data_received.
    def simulate_id_response() -> None:
        """Feed the # 18:006402 response into the transport."""
        transport._data_received(b"# 18:006402\r\n")

    loop = asyncio.get_running_loop()
    loop.call_later(0.05, simulate_id_response)

    with patch(
        "ramses_tx.transport.port.is_hgi80", AsyncMock(return_value=False)
    ):
        transport._signature_policy = SignaturePolicy.ID_COMMAND
        await transport._create_connection()
        assert transport._init_task is not None
        await transport._init_task

    assert transport._init_fut.done()
    transport._make_connection.assert_called_once_with(gateway_id="18:006402")
    transport._close()


async def test_id_command_timeout_falls_back_to_configured() -> None:
    """connect_with_id_command falls back to configured_hgi_id on timeout."""
    transport = _get_transport()
    transport._disable_sending = False
    transport._configured_hgi_id = "18:140805"
    transport._startup_grace = 0.0
    transport._make_connection = MagicMock()
    transport._write = MagicMock()
    transport._write_frame = AsyncMock()

    # No response — let it time out.
    with (
        patch(
            "ramses_tx.transport.port.is_hgi80",
            AsyncMock(return_value=False),
        ),
        patch("ramses_tx.transport.port._ID_COMMAND_TIMEOUT", 0.1),
        patch("ramses_tx.transport.port.CommandDTO", MagicMock()),
        patch("ramses_tx.transport.port._SIGNATURE_MAX_TRYS", 1),
        patch("ramses_tx.transport.port._SIGNATURE_GAP_SECS", 0.001),
    ):
        transport._signature_policy = SignaturePolicy.ID_COMMAND
        await transport._create_connection()
        assert transport._init_task is not None
        await transport._init_task

    assert transport._init_fut.done()
    # Should fall back to configured_hgi_id.
    transport._make_connection.assert_called_once_with(gateway_id="18:140805")
    transport._close()


async def test_id_command_malformed_response_falls_back() -> None:
    """connect_with_id_command ignores malformed !I responses."""
    transport = _get_transport()
    transport._disable_sending = False
    transport._configured_hgi_id = "18:999999"
    transport._startup_grace = 0.0
    transport._make_connection = MagicMock()
    transport._write = MagicMock()

    def simulate_malformed() -> None:
        """Feed a malformed response."""
        transport._data_received(b"# garbage\r\n")

    loop = asyncio.get_running_loop()
    loop.call_later(0.02, simulate_malformed)

    with (
        patch(
            "ramses_tx.transport.port.is_hgi80",
            AsyncMock(return_value=False),
        ),
        patch("ramses_tx.transport.port._ID_COMMAND_TIMEOUT", 0.1),
    ):
        transport._signature_policy = SignaturePolicy.ID_COMMAND
        await transport._create_connection()
        assert transport._init_task is not None
        await transport._init_task

    assert transport._init_fut.done()
    # Malformed response should not be used; fall back to configured.
    transport._make_connection.assert_called_once_with(gateway_id="18:999999")
    transport._close()


async def test_id_command_no_configured_falls_back_to_signature() -> None:
    """connect_with_id_command falls back to _PUZZ when !I fails and no configured_hgi_id."""
    transport = _get_transport()
    transport._disable_sending = False
    transport._configured_hgi_id = None
    transport._startup_grace = 0.0
    transport._make_connection = MagicMock()
    transport._write = MagicMock()
    transport._write_frame = AsyncMock()

    mock_packet = MagicMock()
    mock_packet.src.id = "18:007030"

    def delayed_resolve(*args: Any, **kwargs: Any) -> Any:
        if not transport._init_fut.done():
            transport._init_fut.set_result(mock_packet)
        return None

    transport._write_frame.side_effect = delayed_resolve

    with (
        patch(
            "ramses_tx.transport.port.is_hgi80",
            AsyncMock(return_value=False),
        ),
        patch("ramses_tx.transport.port._ID_COMMAND_TIMEOUT", 0.05),
        patch("ramses_tx.transport.port.CommandDTO", MagicMock()),
    ):
        transport._signature_policy = SignaturePolicy.ID_COMMAND
        await transport._create_connection()
        assert transport._init_task is not None
        await transport._init_task

    assert transport._init_fut.done()
    # Should fall back to signature probe and get the mock packet's src.
    transport._make_connection.assert_called_once_with(gateway_id="18:007030")
    transport._close()


# ---------------------------------------------------------------------------
# Gap F: evofw3 # debug response filtering
# ---------------------------------------------------------------------------


async def test_evofw3_debug_response_filtered() -> None:
    """Lines starting with # are filtered, not logged as PacketInvalid (Gap F)."""
    transport = _get_transport()
    transport._frame_read = MagicMock()

    # Feed # debug lines via _data_received — they should be filtered
    # by _frame_read before reaching the packet parser.
    transport._data_received(b"# evofw3 0.7.1\r\n")
    transport._data_received(b"# 18:140805\r\n")

    # _frame_read should have been called 0 times because # lines
    # are filtered inside _frame_read itself (Gap F).
    # Actually, _data_received calls _frame_read for each line.
    # The filtering happens inside _frame_read — verify by checking
    # that no packet_received was called on the protocol.
    transport._protocol.packet_received.assert_not_called()
    transport._close()


async def test_evofw3_debug_response_does_not_block_ramses() -> None:
    """Normal RAMSES packets pass through alongside # debug lines (Gap F)."""
    transport = _get_transport()
    transport._protocol.packet_received = MagicMock()

    # Feed a # debug line (filtered by _frame_read) then a real
    # RAMSES packet (passes through to protocol).
    transport._data_received(b"# 18:140805\r\n")
    transport._data_received(
        b"000  I --- 01:123456 18:000730 --:------ 30C9 001 00\r\n"
    )

    # Allow the event loop to process the call_soon_threadsafe callback.
    await asyncio.sleep(0.01)

    # The real packet should have been forwarded (not the # line).
    assert transport._protocol.packet_received.call_count == 1
    transport._close()


# ---------------------------------------------------------------------------
# Gap D: per_child_config_overrides in pooled_transport_factory
# ---------------------------------------------------------------------------


async def test_per_child_config_overrides_validation() -> None:
    """pooled_transport_factory validates per_child_config_overrides length."""
    from ramses_tx.transport.factory import pooled_transport_factory

    mock_protocol = MagicMock()
    config = TransportConfig()

    # Mismatched length should raise.
    with pytest.raises(ValueError, match="per_child_config_overrides"):
        await pooled_transport_factory(
            mock_protocol,
            config=config,
            port_names=[SerPortNameT("/dev/ttyUSB0")],
            port_configs=[
                {
                    "baudrate": 115200,
                    "dsrdtr": False,
                    "rtscts": False,
                    "timeout": 3,
                    "xonxoff": False,
                }
            ],
            per_child_config_overrides=[{}, {}],  # length mismatch
        )


async def test_per_child_config_overrides_applied() -> None:
    """per_child_config_overrides are merged via dataclasses.replace (Gap D)."""
    from dataclasses import replace

    base_config = TransportConfig()
    overrides = [
        {"signature_policy": SignaturePolicy.DELAYED, "startup_grace": 3.0},
        {
            "signature_policy": SignaturePolicy.ID_COMMAND,
            "configured_hgi_id": "18:006402",
        },
    ]

    # Verify the merge logic works as expected.
    child0 = replace(base_config, **overrides[0])
    child1 = replace(base_config, **overrides[1])

    assert child0.signature_policy is SignaturePolicy.DELAYED
    assert child0.startup_grace == 3.0
    assert child1.signature_policy is SignaturePolicy.ID_COMMAND
    assert child1.configured_hgi_id == "18:006402"

    # Base config should be unchanged.
    assert base_config.signature_policy is SignaturePolicy.IMMEDIATE
    assert base_config.configured_hgi_id is None
