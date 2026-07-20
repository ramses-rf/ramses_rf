#!/usr/bin/env python3

import asyncio
import logging
from dataclasses import replace
from datetime import datetime as dt, timedelta as td
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ramses_rf.gateway import Gateway
from ramses_rf.messages import ApplicationMessage
from ramses_tx.address import HGI_DEV_ADDR
from ramses_tx.config import EngineConfig
from ramses_tx.const import Code, Priority
from ramses_tx.dtos import CommandDTO as Command, PacketDTO
from ramses_tx.engine import Engine


@pytest.fixture
def mock_dto() -> PacketDTO:
    # Create a fresh mock packet DTO for tests
    return PacketDTO(
        timestamp=dt.now(),
        rssi="045",
        verb="RQ",
        seq="---",
        addr1="18:006402",
        addr2="13:049798",
        addr3="--:------",
        code="1FC9",
        length="001",
        payload="00",
    )


@pytest.fixture
async def dummy_engine() -> Engine:
    # Create an async dummy engine instance configured to disable sending.
    # Being an async fixture ensures it binds to the current test's event
    # loop.
    return Engine(
        config=EngineConfig(port_name="/dev/null", disable_sending=True),
    )


@pytest.mark.asyncio
async def test_engine_init_missing_source_raises() -> None:
    # Initializing without port_name or input_file must raise TypeError
    with pytest.raises(
        TypeError,
        match="Either a port_name or an input_file",
    ):
        Engine(config=EngineConfig(port_name=None, disable_sending=True))


@pytest.mark.asyncio
async def test_engine_init_port_and_file_ignores_file(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Providing both should log a warning and ignore the file
    engine = Engine(
        config=EngineConfig(port_name="/dev/null", input_file="test.log"),
    )
    assert "Port (/dev/null) specified, so file (test.log) ignored" in caplog.text
    assert engine._input_file is None
    assert engine.ser_name == "/dev/null"


@pytest.mark.asyncio
async def test_engine_str_representations() -> None:
    # Test __str__ correctly identifies the active HGI ID
    engine_hgi = Engine(
        config=EngineConfig(port_name="/dev/null", hgi_id="18:123456"),
    )
    assert str(engine_hgi) == "18:123456 (/dev/null)"

    engine_no_hgi = Engine(config=EngineConfig(port_name="/dev/null"))
    assert str(engine_no_hgi) == f"{HGI_DEV_ADDR.id} (/dev/null)"

    engine_no_hgi._transport = MagicMock()
    engine_no_hgi._transport.get_extra_info.return_value = "01:654321"
    assert str(engine_no_hgi) == "01:654321 (/dev/null)"


@pytest.mark.asyncio
async def test_engine_dt_now(dummy_engine: Engine) -> None:
    # Ensure dt_now falls back to dt.now() when transport isn't active
    time_now = dummy_engine._dt_now()
    assert isinstance(time_now, dt)

    dummy_engine._transport = MagicMock()
    custom_dt = dt(2000, 1, 1)
    dummy_engine._transport._dt_now = lambda: custom_dt
    assert dummy_engine._dt_now() == custom_dt


@pytest.mark.asyncio
async def test_engine_message_history_encapsulation(
    mock_dto: PacketDTO,
) -> None:
    # Verify the thread-safe message history updates correctly on Gateway
    gwy = Gateway(port_name="/dev/null")
    msg1 = ApplicationMessage(mock_dto)
    msg2 = ApplicationMessage(mock_dto)

    gwy.update_message_history(msg1)
    assert gwy._this_msg is msg1
    assert gwy._prev_msg is None

    gwy.update_message_history(msg2)
    assert gwy._this_msg is msg2

    # Use cast to bypass Mypy's strict sequential attribute narrowing
    assert cast(Any, gwy._prev_msg) is msg1

    gwy.clear_message_history()
    assert cast(Any, gwy._this_msg) is None
    assert cast(Any, gwy._prev_msg) is None


@pytest.mark.asyncio
@patch("ramses_tx.engine.transport_factory", new_callable=AsyncMock)
async def test_engine_start_serial(
    mock_factory: AsyncMock, dummy_engine: Engine
) -> None:
    # Starting with a serial port correctly triggers transport_factory
    mock_transport = MagicMock()
    mock_factory.return_value = mock_transport
    dummy_engine._protocol.wait_for_connection_made = AsyncMock()

    await dummy_engine.start()

    mock_factory.assert_called_once()
    dummy_engine._protocol.wait_for_connection_made.assert_awaited_once()


@pytest.mark.asyncio
@patch("ramses_tx.engine.transport_factory", new_callable=AsyncMock)
async def test_engine_start_file(mock_factory: AsyncMock) -> None:
    # Starting via file forces wait_for_connection_lost up to 86400
    # seconds
    engine = Engine(
        config=EngineConfig(port_name=None, input_file="test.log"),
    )
    mock_transport = MagicMock()
    mock_factory.return_value = mock_transport

    engine._protocol.wait_for_connection_made = AsyncMock()
    engine._protocol.wait_for_connection_lost = AsyncMock()

    await engine.start()
    engine._protocol.wait_for_connection_lost.assert_awaited_once_with(timeout=86400)


@pytest.mark.asyncio
async def test_engine_stop_cleans_tasks_and_transport(
    dummy_engine: Engine,
) -> None:
    # Tasks are correctly cancelled, transport is closed, exceptions logged
    async def dummy_coro() -> None:
        await asyncio.sleep(0.1)

    async def failing_coro() -> None:
        raise ValueError("Simulated task failure")

    task1 = dummy_engine._loop.create_task(dummy_coro())
    task2 = dummy_engine._loop.create_task(failing_coro())
    dummy_engine.add_task(task1)
    dummy_engine.add_task(task2)

    # Let the failing coro complete so exception is set
    await asyncio.sleep(0.01)

    mock_transport = MagicMock()
    dummy_engine._transport = mock_transport
    dummy_engine._protocol.wait_for_connection_lost = AsyncMock()

    await dummy_engine.stop()

    assert task1.cancelled()
    mock_transport.close.assert_called_once()
    dummy_engine._protocol.wait_for_connection_lost.assert_awaited_once()


@pytest.mark.asyncio
async def test_engine_pause_resume(dummy_engine: Engine) -> None:
    # State flags map properly across _pause and _resume
    mock_transport = MagicMock()
    dummy_engine._transport = mock_transport
    dummy_engine._disable_sending = False

    dummy_engine._protocol.pause_writing = MagicMock()
    dummy_engine._protocol.resume_writing = MagicMock()

    await dummy_engine._pause("custom_arg")
    await asyncio.sleep(0)  # Yield to flush loop.call_soon callbacks

    # Use cast to bypass Mypy's strict attribute narrowing across method
    # calls
    assert cast(Any, dummy_engine._engine_state) is not None
    assert cast(Any, dummy_engine._disable_sending) is True
    dummy_engine._protocol.pause_writing.assert_called_once()
    mock_transport.pause_reading.assert_called_once()

    args = await dummy_engine._resume()
    await asyncio.sleep(0)  # Yield to flush loop.call_soon callbacks

    assert cast(Any, dummy_engine._engine_state) is None
    assert cast(Any, dummy_engine._disable_sending) is False

    # Cast to list prevents Mypy comparison-overlap with `args` tuple type
    # hint
    assert list(args) == ["custom_arg"]

    dummy_engine._protocol.resume_writing.assert_called_once()
    mock_transport.resume_reading.assert_called_once()


@pytest.mark.asyncio
async def test_engine_pause_already_paused_raises(dummy_engine: Engine) -> None:
    # Pausing an already paused engine raises RuntimeError
    await dummy_engine._pause()
    with pytest.raises(RuntimeError, match="it is already paused"):
        await dummy_engine._pause()


@pytest.mark.asyncio
async def test_engine_resume_not_paused_raises(dummy_engine: Engine) -> None:
    # Resuming an unpaused engine raises RuntimeError
    with pytest.raises(RuntimeError, match="it was not paused"):
        await dummy_engine._resume()


@pytest.mark.asyncio
async def test_engine_pause_lock_failed_raises(dummy_engine: Engine) -> None:
    # Inability to acquire the lock raises RuntimeError
    await dummy_engine._engine_lock.acquire()
    with pytest.raises(RuntimeError, match="failed to acquire lock"):
        await dummy_engine._pause()


@pytest.mark.asyncio
async def test_engine_drop_msg(
    caplog: pytest.LogCaptureFixture, dummy_engine: Engine
) -> None:
    # The drop handler safely drops messages and logs them
    dto = PacketDTO(
        timestamp=dt.now(),
        rssi="045",
        verb="RQ",
        seq="---",
        addr1="18:006402",
        addr2="13:049798",
        addr3="--:------",
        code="1FC9",
        length="001",
        payload="00",
    )
    msg = ApplicationMessage(dto)
    with caplog.at_level(logging.DEBUG):
        await dummy_engine._drop_msg(msg)

    assert "Message dropped while engine paused" in caplog.text


@pytest.mark.asyncio
async def test_engine_create_cmd() -> None:
    # Engine wraps CommandDTO creation natively
    cmd = Engine.create_cmd("RQ", "18:006402", Code._1FC9, "00")
    assert isinstance(cmd, Command)
    assert cmd.code == "1FC9"
    assert cmd.verb == "RQ"


@pytest.mark.asyncio
async def test_engine_async_send_cmd(dummy_engine: Engine) -> None:
    # Sends pass through effectively to protocol.send_cmd
    from ramses_tx.dtos import CommandDTO

    cmd = CommandDTO(
        verb="RQ",
        addr1="18:000730",
        addr2="18:006402",
        addr3="--:------",
        code=Code._1FC9,
        payload="00",
    )
    dummy_engine._protocol.send_cmd = AsyncMock(return_value="mock_reply")

    reply = await dummy_engine.async_send_cmd(cmd, priority=Priority.HIGH)
    assert reply == "mock_reply"
    dummy_engine._protocol.send_cmd.assert_awaited_once()


@pytest.mark.asyncio
async def test_engine_msg_handler(dummy_engine: Engine, mock_dto: PacketDTO) -> None:
    # Validates that engine routes the DTO to the registered handler
    mock_handler = AsyncMock()
    dummy_engine._handle_msg = mock_handler

    await dummy_engine._msg_handler(mock_dto)

    mock_handler.assert_awaited_once_with(mock_dto)


def test_application_message_bind_context(mock_dto: PacketDTO) -> None:
    # Bind context successfully sets arbitrary properties
    app_msg = ApplicationMessage(mock_dto)
    mock_gwy = object()
    app_msg.bind_context(mock_gwy)
    assert app_msg._gwy is mock_gwy


def test_application_message_expired_1f09_logic(mock_dto: PacketDTO) -> None:
    # Payload specific expiration correctly resolves via remaining_seconds
    modified_dto = replace(mock_dto, verb="RP", code=str(Code._1F09))

    app_msg = ApplicationMessage(modified_dto)
    app_msg._payload = {"remaining_seconds": 2}

    # Needs Mock Engine to simulate immediate dt_now vs elapsed time
    app_msg.set_gateway(MagicMock())
    app_msg._engine._dt_now = lambda: modified_dto.timestamp + td(seconds=5)

    # Lifespan fraction (5 - 3) / 2 = 1.0 (Less than HAS_EXPIRED: 2.0)
    assert app_msg._expired is False
    assert app_msg._fraction_expired == 1.0


def test_application_message_expired_lifespan_false(
    mock_dto: PacketDTO,
) -> None:
    # Packets specifically stating False for lifespan evaluate identically
    # to CANT_EXPIRE
    app_msg = ApplicationMessage(mock_dto)
    app_msg.set_gateway(MagicMock())
    app_msg._engine._dt_now = lambda: mock_dto.timestamp

    with patch.object(app_msg, "_get_lifespan", return_value=False):
        assert app_msg._expired is False
        assert app_msg._fraction_expired == ApplicationMessage.CANT_EXPIRE


def test_application_message_expired_lifespan_true_raises(
    mock_dto: PacketDTO,
) -> None:
    # Packets stating True for lifespan are not implemented yet
    app_msg = ApplicationMessage(mock_dto)
    app_msg.set_gateway(MagicMock())
    app_msg._engine._dt_now = lambda: mock_dto.timestamp

    with (
        patch.object(app_msg, "_get_lifespan", return_value=True),
        pytest.raises(NotImplementedError, match="Lifespan True not implemented"),
    ):
        _ = app_msg._expired


def test_application_message_expired_standard_lifespan(
    mock_dto: PacketDTO,
) -> None:
    # Lifespan durations are resolved based on standard td offsets
    app_msg = ApplicationMessage(mock_dto)
    app_msg.set_gateway(MagicMock())
    app_msg._engine._dt_now = lambda: mock_dto.timestamp + td(seconds=25)

    with patch.object(app_msg, "_get_lifespan", return_value=td(seconds=10)):
        # Fraction: (25 - 3) / 10 = 2.2 >= 2.0 (HAS_EXPIRED)
        assert app_msg._expired is True
        assert app_msg._fraction_expired == 2.2


def test_application_message_expired_fast_path(mock_dto: PacketDTO) -> None:
    # Early returns using pre-calculated fractions bypass dt calculation
    app_msg = ApplicationMessage(mock_dto)
    app_msg._fraction_expired = ApplicationMessage.CANT_EXPIRE
    assert app_msg._expired is False

    app_msg._fraction_expired = ApplicationMessage.HAS_EXPIRED
    assert app_msg._expired is True
