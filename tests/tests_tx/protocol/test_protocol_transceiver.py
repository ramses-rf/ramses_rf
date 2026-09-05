#!/usr/bin/env python3
"""Tests for the stateless PortProtocol command transceiver."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from ramses_tx.const import Code, Priority, Verb
from ramses_tx.dtos import CommandDTO
from ramses_tx.exceptions import (
    ProtocolSendFailed,
    ProtocolTimeoutError,
    TransportError,
)
from ramses_tx.packet import Packet
from ramses_tx.protocol.core import PortProtocol
from ramses_tx.routing import (
    RoutedCommand,
    RouteRequest,
    WriteOutcome,
)
from ramses_tx.typing import QosParams


@pytest.fixture
def mock_msg_handler() -> MagicMock:
    """Provide a mock message handler."""
    return MagicMock()


@pytest.fixture
async def port_protocol(mock_msg_handler: MagicMock) -> PortProtocol:
    """Provide an instantiated PortProtocol instance."""
    return PortProtocol(
        mock_msg_handler,
        echo_timeout=0.05,
        max_retry_limit=2,
        send_timeout_limit=1.0,
    )


@pytest.fixture
def sample_cmd() -> CommandDTO:
    """Provide a sample CommandDTO."""
    return CommandDTO(
        verb=Verb.RQ,
        addr1="18:000730",
        addr2="01:123456",
        addr3="--:------",
        code=Code._10A0,
        payload="00",
    )


def _make_mock_transport() -> MagicMock:
    """Create a mock transport with the routing API wired up.

    ``prepare_command`` returns a real ``RoutedCommand`` wrapping the
    original command (no source patching for a non-pooled transport).
    ``write_routed`` is an ``AsyncMock`` returning ``SUBMITTED``.
    """
    mock_transport = MagicMock()
    mock_transport.get_extra_info.return_value = None
    mock_transport.write_frame = AsyncMock()

    def _prepare_command(request: RouteRequest) -> RoutedCommand:
        return RoutedCommand(child_id="self", command=request.command)

    mock_transport.prepare_command = MagicMock(side_effect=_prepare_command)
    mock_transport.write_routed = AsyncMock(
        return_value=WriteOutcome.SUBMITTED
    )
    return mock_transport


@pytest.mark.asyncio
async def test_port_protocol_initial_state(
    port_protocol: PortProtocol,
) -> None:
    """Ensure PortProtocol initializes correctly in inactive state."""
    assert port_protocol.qsize == 0
    assert port_protocol.is_sending is False
    assert port_protocol._is_active is False


@pytest.mark.asyncio
async def test_port_protocol_connection_made(
    port_protocol: PortProtocol,
) -> None:
    """Test connection_made activates transceiver and starts tx worker."""
    mock_transport = MagicMock()
    mock_transport.get_extra_info.return_value = None

    port_protocol.connection_made(mock_transport, ramses=True)
    assert port_protocol._is_active is True
    assert port_protocol._tx_worker_task is not None
    assert not port_protocol._tx_worker_task.done()

    port_protocol.connection_lost(None)


@pytest.mark.asyncio
async def test_port_protocol_send_cmd_success(
    port_protocol: PortProtocol, sample_cmd: CommandDTO
) -> None:
    """Test successful command transmit resolved by matching echo packet."""
    mock_transport = _make_mock_transport()

    port_protocol.connection_made(mock_transport, ramses=True)

    send_task = asyncio.create_task(
        port_protocol.send_cmd(sample_cmd, priority=Priority.HIGH)
    )

    await asyncio.sleep(0.01)
    assert mock_transport.write_routed.called

    echo_pkt = MagicMock(spec=Packet)
    echo_pkt._is_echo = True
    echo_pkt._hdr = sample_cmd.tx_header
    echo_pkt._hdr_ = sample_cmd.tx_header

    port_protocol._packet_received(echo_pkt)

    result = await send_task
    assert result == echo_pkt
    assert port_protocol.is_sending is False

    port_protocol.connection_lost(None)


@pytest.mark.asyncio
async def test_port_protocol_send_cmd_echo_timeout_and_retry(
    port_protocol: PortProtocol, sample_cmd: CommandDTO
) -> None:
    """Test command transmission retry on missing echo and eventual timeout."""
    mock_transport = _make_mock_transport()

    port_protocol.connection_made(mock_transport, ramses=True)

    qos = QosParams(max_retries=1, timeout=0.02)

    with pytest.raises(ProtocolTimeoutError):
        await port_protocol.send_cmd(sample_cmd, qos=qos)

    assert mock_transport.write_routed.call_count == 2
    assert port_protocol.is_sending is False

    port_protocol.connection_lost(None)


@pytest.mark.asyncio
async def test_port_protocol_transport_send_failure(
    port_protocol: PortProtocol, sample_cmd: CommandDTO
) -> None:
    """Test write_routed hardware failure sets ProtocolSendFailed exception."""
    mock_transport = _make_mock_transport()
    mock_transport.write_routed = AsyncMock(
        return_value=WriteOutcome.AMBIGUOUS
    )

    port_protocol.connection_made(mock_transport, ramses=True)

    with pytest.raises(ProtocolSendFailed, match="Failed to transmit frame"):
        await port_protocol.send_cmd(sample_cmd)

    port_protocol.connection_lost(None)


@pytest.mark.asyncio
async def test_port_protocol_connection_lost_cancels_queue(
    port_protocol: PortProtocol, sample_cmd: CommandDTO
) -> None:
    """Test connection_lost cancels queued commands with TransportError."""
    mock_transport = _make_mock_transport()

    port_protocol.connection_made(mock_transport, ramses=True)
    port_protocol.pause_writing()

    send_task = asyncio.create_task(port_protocol.send_cmd(sample_cmd))
    await asyncio.sleep(0.01)

    port_protocol.connection_lost(TransportError("Modem disconnected"))

    with pytest.raises(TransportError, match="Modem disconnected"):
        await send_task

    assert port_protocol.qsize == 0
    assert port_protocol._is_active is False


@pytest.mark.asyncio
async def test_port_protocol_pause_and_resume_writing(
    port_protocol: PortProtocol, sample_cmd: CommandDTO
) -> None:
    """Test pause_writing holds queue until resume_writing is called."""
    mock_transport = _make_mock_transport()

    port_protocol.connection_made(mock_transport, ramses=True)
    port_protocol.pause_writing()

    send_task = asyncio.create_task(port_protocol.send_cmd(sample_cmd))
    await asyncio.sleep(0.02)
    assert not mock_transport.write_routed.called

    port_protocol.resume_writing()
    await asyncio.sleep(0.01)
    assert mock_transport.write_routed.called

    echo_pkt = MagicMock(spec=Packet)
    echo_pkt._is_echo = True
    echo_pkt._hdr = sample_cmd.tx_header
    echo_pkt._hdr_ = sample_cmd.tx_header
    port_protocol._packet_received(echo_pkt)

    await send_task
    port_protocol.connection_lost(None)
