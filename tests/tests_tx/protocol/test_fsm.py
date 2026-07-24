#!/usr/bin/env python3
"""Tests for the protocol finite state machine (FSM)."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ramses_tx.const import Priority
from ramses_tx.exceptions import TransportError
from ramses_tx.packet import Packet
from ramses_tx.protocol.fsm import Inactive, IsInIdle, ProtocolContext, WantEcho


@pytest.fixture
async def mock_protocol() -> MagicMock:
    """Provide a mocked Protocol instance."""
    protocol = MagicMock()
    # Safely fetch the running loop within an async fixture context
    protocol._loop = asyncio.get_running_loop()
    protocol.hgi_id = "18:000730"
    protocol._tracked_sync_cycles = []
    return protocol


@pytest.fixture
async def fsm_context(mock_protocol: MagicMock) -> ProtocolContext:
    """Provide a fresh FSM context."""
    return ProtocolContext(
        mock_protocol,
        echo_timeout=0.5,
        reply_timeout=0.5,
        max_retry_limit=2,
    )


@pytest.fixture
def mock_cmd() -> MagicMock:
    """Provide a basic mocked Command."""
    cmd = MagicMock()
    cmd.tx_header = "10A0|RQ|01:123456"
    cmd.src.id = "18:000730"
    cmd.dst.id = "01:123456"
    # To satisfy `HGI_DEVICE_ID in cmd.tx_header` checks in IsInIdle
    cmd._hdr_ = cmd.tx_header
    return cmd


@pytest.fixture
def mock_qos() -> MagicMock:
    """Provide a mocked QoS Params object."""
    qos = MagicMock()
    qos.timeout = 1.0
    qos.max_retries = 2  # Added to prevent TypeError in qos.py
    return qos


@pytest.mark.asyncio
async def test_fsm_initial_state(fsm_context: ProtocolContext) -> None:
    """Ensure the FSM initializes correctly in the Inactive state."""
    assert isinstance(fsm_context.state, Inactive)
    assert fsm_context.is_sending is False


@pytest.mark.asyncio
async def test_fsm_connection_made(fsm_context: ProtocolContext) -> None:
    """Test transition from Inactive to IsInIdle upon connection."""
    transport = MagicMock()
    fsm_context.connection_made(transport)
    assert isinstance(fsm_context.state, IsInIdle)


@pytest.mark.asyncio
async def test_fsm_send_cmd_success(
    fsm_context: ProtocolContext,
    mock_cmd: MagicMock,
    mock_qos: MagicMock,
) -> None:
    """Test a successful command send transitioning through WantEcho."""
    fsm_context.connection_made(MagicMock())

    mock_send_fnc = AsyncMock()

    # We create a task for send_cmd, since it will block waiting for fut
    send_task = asyncio.create_task(
        fsm_context.send_cmd(mock_send_fnc, mock_cmd, Priority.HIGH, mock_qos)
    )

    # Allow the event loop to process the enqueue and state transition
    await asyncio.sleep(0.01)

    # Should now be waiting for echo
    assert isinstance(fsm_context.state, WantEcho)
    mock_send_fnc.assert_called_once_with(mock_cmd)

    # Simulate receiving the echo packet
    echo_pkt = MagicMock(spec=Packet)
    echo_pkt._hdr = mock_cmd.tx_header
    fsm_context.pkt_received(echo_pkt)

    # Should resolve and go back to Idle
    await asyncio.sleep(0.01)
    assert isinstance(fsm_context.state, IsInIdle)

    result = await send_task
    assert result == echo_pkt


@pytest.mark.asyncio
async def test_fsm_transport_error_in_send_task(
    fsm_context: ProtocolContext,
    mock_cmd: MagicMock,
    mock_qos: MagicMock,
) -> None:
    """Verify known TransportErrors are caught and fail the send."""
    fsm_context.connection_made(MagicMock())

    async def failing_send_fnc(*args: Any, **kwargs: Any) -> None:
        raise TransportError("Serial port disconnected")

    with pytest.raises(TransportError, match="Serial port disconnected"):
        await fsm_context.send_cmd(failing_send_fnc, mock_cmd, Priority.HIGH, mock_qos)

    assert isinstance(fsm_context.state, IsInIdle)


@pytest.mark.asyncio
async def test_fsm_unhandled_exception_in_send_task(
    fsm_context: ProtocolContext,
    mock_cmd: MagicMock,
    mock_qos: MagicMock,
) -> None:
    """REPLICATION TEST: Verify unexpected errors don't stall the FSM.

    If fsm.py is unpatched, this test will hit a ProtocolTimeoutError
    (because the future never resolves) and will print a messy asyncio
    'Task exception was never retrieved' traceback to the console.

    If patched, it immediately raises the RuntimeError and cleans up.
    """
    fsm_context.connection_made(MagicMock())

    async def unexpected_failing_send_fnc(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Simulated unexpected failure")

    # We expect the RuntimeError to bubble cleanly through the resolved future
    with pytest.raises(RuntimeError, match="Simulated unexpected failure"):
        await fsm_context.send_cmd(
            unexpected_failing_send_fnc, mock_cmd, Priority.HIGH, mock_qos
        )

    # Crucially, the FSM must cleanly reset to Idle, preventing task leaks
    assert isinstance(fsm_context.state, IsInIdle)


@pytest.mark.asyncio
async def test_fsm_connection_lost(fsm_context: ProtocolContext) -> None:
    """Test FSM safely aborts to Inactive on connection loss."""
    fsm_context.connection_made(MagicMock())
    assert isinstance(fsm_context.state, IsInIdle)

    fsm_context.connection_lost(TransportError("Disconnected"))
    assert isinstance(fsm_context.state, Inactive)
