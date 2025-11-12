#!/usr/bin/env python3
"""RAMSES RF - Test the binding protocol with a virtual RF

NB: This test will likely fail with pytest -n x, because of the protocol's throttle
limits.
"""

import asyncio
import random
from collections.abc import AsyncGenerator, Awaitable
from datetime import datetime as dt

import pytest
import serial  # type: ignore[import-untyped]

from ramses_rf import Command, Message, Packet
from ramses_tx import exceptions as exc
from ramses_tx.protocol import PortProtocol, ReadProtocol, protocol_factory
from ramses_tx.protocol_fsm import (
    Inactive,
    IsInIdle,
    ProtocolContext,
    WantEcho,
    WantRply,
    _ProtocolStateT,
)
from ramses_tx.transport import transport_factory
from ramses_tx.typing import QosParams

from .virtual_rf import VirtualRf

# patched constants
DEFAULT_MAX_RETRIES = 0  # #                ramses_tx.protocol
MAX_DUTY_CYCLE = 1.0  # #                   ramses_tx.protocol

# other constants
CALL_LATER_DELAY = 0.001  # FIXME: this is hardware-specific

ASSERT_CYCLE_TIME = 0.0005  # max_cycles_per_assert = max_sleep / ASSERT_CYCLE_TIME
DEFAULT_MAX_SLEEP = 0.1


# TODO: better handling than AttributeError for this...
# Command("RQ --- 18:111111 01:222222 --:------ 12B0 003 07")

II_CMD_STR_0 = " I --- 01:006056 --:------ 01:006056 1F09 003 0005C8"
II_CMD_0 = Command(II_CMD_STR_0)
II_PKT_0 = Packet(dt.now(), f"... {II_CMD_STR_0}")

# TIP: using 18:000730 as the source will prevent impersonation alerts

RQ_CMD_STR_0 = "RQ --- 18:000730 01:222222 --:------ 12B0 001 00"
RP_CMD_STR_0 = "RP --- 01:222222 18:000730 --:------ 12B0 003 000000"

RQ_CMD_0 = Command(RQ_CMD_STR_0)
RQ_PKT_0 = Packet(dt.now(), f"... {RQ_CMD_STR_0}")
RP_PKT_0 = Packet(dt.now(), f"... {RP_CMD_STR_0}")

RQ_CMD_STR_1 = "RQ --- 18:000730 01:222222 --:------ 12B0 001 01"
RP_CMD_STR_1 = "RP --- 01:222222 18:000730 --:------ 12B0 003 010000"

RQ_CMD_1 = Command(RQ_CMD_STR_1)
RQ_PKT_1 = Packet(dt.now(), f"... {RQ_CMD_STR_1}")
RP_PKT_1 = Packet(dt.now(), f"... {RP_CMD_STR_1}")


# ### FIXTURES #########################################################################


@pytest.fixture()
async def protocol(rf: VirtualRf) -> AsyncGenerator[PortProtocol, None]:
    def _msg_handler(msg: Message) -> None:
        pass

    protocol = protocol_factory(_msg_handler)

    # These values should be asserted as needed for subsequent tests
    assert isinstance(protocol, PortProtocol)  # mypy
    assert isinstance(protocol._context, ProtocolContext)  # mypy

    protocol._disable_qos = False  # HACK: needed for tests to succeed (default: None?)

    assert protocol._context.echo_timeout == 0.5
    assert protocol._context.reply_timeout == 0.5
    assert protocol._context.SEND_TIMEOUT_LIMIT == 20.0

    await assert_protocol_state(protocol, Inactive, max_sleep=0)

    transport = await transport_factory(protocol, port_name=rf.ports[0], port_config={})
    transport._extra["virtual_rf"] = rf  # injected to aid any debugging

    await assert_protocol_state(protocol, IsInIdle, max_sleep=0)

    try:
        yield protocol

    except serial.SerialException as err:
        transport._close(exc=err)
        raise

    except (AssertionError, asyncio.InvalidStateError, TimeoutError):
        transport.close()
        raise

    else:
        await assert_protocol_state(protocol, IsInIdle)
        transport.close()

    finally:
        await assert_protocol_state(protocol, Inactive, max_sleep=0.1)
        await rf.stop()


# ######################################################################################


async def assert_protocol_state(
    protocol: PortProtocol | ReadProtocol,
    expected_state: type[_ProtocolStateT],
    max_sleep: float = DEFAULT_MAX_SLEEP,
) -> None:
    assert isinstance(protocol, PortProtocol)  # mypy
    assert isinstance(protocol._context, ProtocolContext)  # mypy

    for _ in range(int(max_sleep / ASSERT_CYCLE_TIME)):
        await asyncio.sleep(ASSERT_CYCLE_TIME)
        if isinstance(protocol._context.state, expected_state):
            break
    assert isinstance(protocol._context.state, expected_state)


def assert_protocol_state_detail(
    protocol: PortProtocol, cmd: Command | None, num_sends: int
) -> None:
    assert isinstance(protocol._context, ProtocolContext)  # mypy

    assert protocol._context.state.cmd_sent == cmd
    assert protocol._context._cmd_tx_count == num_sends
    assert bool(cmd) is isinstance(protocol._context.state, WantEcho | WantRply)


async def async_pkt_received(  # type: ignore[no-any-unimported]
    protocol: PortProtocol,
    pkt: Packet,
    method: int = 0,
    ser: None | serial.Serial = None,
) -> None:
    # await assert_protocol_state(protocol, ProtocolState.IDLE, max_sleep=0)
    # assert_state_temp(protocol, None, 0)

    if method == 0:
        protocol.pkt_received(pkt)
        return

    if method == 1:
        protocol._loop.call_soon(protocol.pkt_received, pkt)
        return

    assert ser is not None
    frame = bytes(str(pkt).encode("ascii")) + b"\r\n"

    if method == 2:
        ser.write(frame)
    elif method == 3:
        protocol._loop.call_soon(ser.write, frame)
    else:
        protocol._loop.call_later(0.001, ser.write, frame)

    # await assert_protocol_state(protocol, ProtocolState.IDLE, max_sleep=0)
    # assert_state_temp(protocol, None, 0)


# ### TESTS ############################################################################


async def _test_flow_30x(protocol: PortProtocol) -> None:
    # STEP 0: Setup...
    rf: VirtualRf = protocol._transport._extra["virtual_rf"]
    ser = serial.Serial(rf.ports[1])

    qos = QosParams(wait_for_reply=True)

    # STEP 1: Send an I cmd (no reply)...
    task = rf._loop.create_task(protocol._send_cmd(II_CMD_0, qos=qos), name="send_1")
    assert await task == II_CMD_0  # no reply pkt expected

    # STEP 2: Send an RQ cmd, then receive the corresponding RP pkt...
    task = rf._loop.create_task(protocol._send_cmd(RQ_CMD_0, qos=qos), name="send_2")
    protocol._loop.call_later(
        CALL_LATER_DELAY, ser.write, bytes(str(RP_PKT_0).encode("ascii")) + b"\r\n"
    )
    assert await task == RP_PKT_0

    # STEP 3: Send an I cmd (no reply) *twice*...
    task = rf._loop.create_task(protocol._send_cmd(II_CMD_0, qos=qos), name="send_3A")
    assert await task == II_CMD_0  # no reply pkt expected

    task = rf._loop.create_task(protocol._send_cmd(II_CMD_0, qos=qos), name="send_3B")
    assert await task == II_CMD_0  # no reply pkt expected

    # STEP 4: Send an RQ cmd, then receive the corresponding RP pkt...
    task = rf._loop.create_task(protocol._send_cmd(RQ_CMD_1, qos=qos), name="send_4A")
    # sk = rf._loop.create_task(protocol._send_cmd(RQ_CMD_1, qos=qos), name="send_4B")

    # TODO: make these deterministic so ser replies *only after* it receives cmd
    protocol._loop.call_later(
        CALL_LATER_DELAY, ser.write, bytes(str(RP_PKT_0).encode("ascii")) + b"\r\n"
    )
    protocol._loop.call_later(
        CALL_LATER_DELAY, ser.write, bytes(str(RP_PKT_1).encode("ascii")) + b"\r\n"
    )

    assert await task == RP_PKT_1


async def _test_flow_401(protocol: PortProtocol) -> None:
    qos = QosParams(wait_for_reply=False)

    numbers = list(range(24))
    tasks = {}

    for i in numbers:
        cmd = Command.put_sensor_temp("03:123456", i)
        tasks[i] = protocol._loop.create_task(protocol._send_cmd(cmd, qos=qos))

    assert await asyncio.gather(*tasks.values())

    for i in numbers:
        pkt = tasks[i].result()
        assert pkt == Command.put_sensor_temp("03:123456", i)


async def _test_flow_402(protocol: PortProtocol) -> None:
    qos = QosParams(wait_for_reply=False)

    numbers = list(range(24))
    tasks = {}

    for i in numbers:
        cmd = Command.put_sensor_temp("03:123456", i)
        tasks[i] = protocol._loop.create_task(protocol._send_cmd(cmd, qos=qos))

    random.shuffle(numbers)

    for i in numbers:
        pkt = await tasks[i]
        assert pkt == Command.put_sensor_temp("03:123456", i)


async def _test_flow_qos_helper(
    send_cmd_coro: Awaitable, will_fail: bool = False
) -> None:
    try:
        _ = await send_cmd_coro
    except exc.ProtocolSendFailed:
        pass
    else:
        assert False, f"Had expected {exc.ProtocolSendFailed}"


async def _test_flow_60x(protocol: PortProtocol, num_cmds: int = 1) -> None:
    #
    # Setup...
    tasks = []
    for idx in range(num_cmds):
        cmd = Command.get_zone_temp("01:123456", f"{idx:02X}")
        coro = protocol._send_cmd(cmd, qos=QosParams(wait_for_reply=False))
        tasks.append(protocol._loop.create_task(coro, name=f"cmd_{idx:02X}"))

    assert await asyncio.gather(*tasks)


async def _test_flow_qos(protocol: PortProtocol) -> None:
    assert isinstance(protocol._context, ProtocolContext)  # mypy

    # HACK: to reduce test time
    protocol._context.SEND_TIMEOUT_LIMIT = 0.01
    protocol._context.max_retry_limit = 0

    #
    # ### Simple test for an I (does not expect any reply)...

    cmd = Command.put_sensor_temp("03:000111", 19.5)
    pkt = await protocol._send_cmd(cmd)  # qos == QosParams()
    assert pkt == cmd, "Should be echo as there's no reply to wait for"

    cmd = Command.put_sensor_temp("03:000222", 19.5)
    pkt = await protocol._send_cmd(cmd, qos=None)  # qos == QosParams()
    assert pkt == cmd, "Should be echo as there's no reply to wait for"

    cmd = Command.put_sensor_temp("03:000333", 19.5)
    pkt = await protocol._send_cmd(cmd, qos=QosParams())
    assert pkt == cmd, "Should be echo as there's no reply to wait for"

    cmd = Command.put_sensor_temp("03:000444", 19.5)
    pkt = await protocol._send_cmd(cmd, qos=QosParams(wait_for_reply=None))
    assert pkt == cmd, "should be echo as there is no wait_for_reply"

    cmd = Command.put_sensor_temp("03:000555", 19.5)
    pkt = await protocol._send_cmd(cmd, qos=QosParams(wait_for_reply=False))
    assert pkt == cmd, "should be echo as there is no wait_for_reply"

    cmd = Command.put_sensor_temp("03:000666", 19.5)
    pkt = await protocol._send_cmd(cmd, qos=QosParams(wait_for_reply=True))
    assert pkt == cmd, "Should be echo as there's no reply to wait for"

    # # ### Simple test for an RQ (expects an RP)...

    cmd = Command.get_system_time("01:000111")
    pkt = await protocol._send_cmd(cmd)
    assert pkt == cmd, "Should be echo as there's no reply to wait for"

    cmd = Command.get_system_time("01:000222")
    pkt = await protocol._send_cmd(cmd, qos=None)
    assert pkt == cmd, "Should be echo as there's no reply to wait for"

    cmd = Command.get_system_time("01:000333")
    pkt = await protocol._send_cmd(cmd, qos=QosParams())
    assert pkt == cmd, "Should be echo as there's no reply to wait for"

    cmd = Command.get_system_time("01:000444")
    pkt = await protocol._send_cmd(cmd, qos=QosParams(wait_for_reply=None))
    assert pkt == cmd, "Should be echo as there is no wait_for_reply"

    cmd = Command.get_system_time("01:000555")
    pkt = await protocol._send_cmd(cmd, qos=QosParams(wait_for_reply=False))
    assert pkt == cmd, "Should be echo as there is no wait_for_reply"

    cmd = Command.get_system_time("01:000666")
    coro = protocol._send_cmd(cmd, qos=QosParams(wait_for_reply=True, timeout=0.05))
    await _test_flow_qos_helper(coro)

    # # ### Simple test for an I (does not expect any reply)...

    cmd = Command.put_sensor_temp("03:000999", 19.5)
    pkt = await protocol._send_cmd(cmd)
    assert pkt == cmd


# ######################################################################################


@pytest.mark.xdist_group(name="virt_serial")
async def test_flow_300(protocol: PortProtocol) -> None:
    """Check state change of RQ/I/RQ cmds using protocol methods."""
    await _test_flow_30x(protocol)


@pytest.mark.xdist_group(name="virt_serial")
async def test_flow_401(protocol: PortProtocol) -> None:
    """Throw a bunch of commands in a random order, and see that all are echo'd."""
    await _test_flow_401(protocol)


@pytest.mark.xdist_group(name="virt_serial")
async def test_flow_402(protocol: PortProtocol) -> None:
    """Throw a bunch of commands in a random order, and see that all are echo'd."""
    await _test_flow_402(protocol)


@pytest.mark.xdist_group(name="virt_serial")
async def test_flow_601(protocol: PortProtocol) -> None:
    """Check the wait_for_reply kwarg."""
    await _test_flow_60x(protocol)


@pytest.mark.xdist_group(name="virt_serial")
async def test_flow_602(protocol: PortProtocol) -> None:
    """Check the wait_for_reply kwarg."""
    await _test_flow_60x(protocol, num_cmds=2)


@pytest.mark.xdist_group(name="virt_serial")
async def test_flow_qos(protocol: PortProtocol) -> None:
    """Check the wait_for_reply kwarg."""
    await _test_flow_qos(protocol)


# @pytest_asyncio.fixture
# async def async_benchmark(benchmark: pytest.FixtureDef) -> Callable[..., None]:
#     event_loop = asyncio.get_running_loop()

#     def _wrapper(func: Callable, *args: Any, **kwargs: Any) -> None:
#         if asyncio.iscoroutinefunction(func):

#             @benchmark
#             def _():
#                 return event_loop.run_until_complete(func(*args, **kwargs))

#         else:
#             benchmark(func, *args, **kwargs)

#     return _wrapper


# @pytest.mark.xdist_group(name="virt_serial")
# def test_benchmark_100(async_benchmark) -> None:
#     async_benchmark(_test_flow_10x)


# @pytest.mark.xdist_group(name="virt_serial")
# def test_benchmark_300(async_benchmark) -> None:
#     async_benchmark(_test_flow_30x)
