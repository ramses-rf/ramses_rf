#!/usr/bin/env python3
"""RAMSES RF - Test the binding protocol with a virtual RF

NB: This test will likely fail with pytest -n x, because of the protocol's throttle
limits.
"""

import asyncio
import random
from collections.abc import AsyncGenerator, Awaitable, Generator
from datetime import datetime as dt
from typing import cast
from unittest.mock import patch

import pytest
import serial

from ramses_rf import Message, Packet
from ramses_rf.address import HGI_DEV_ADDR, Address
from ramses_rf.commands.builders import build_dto
from ramses_rf.commands.core import Command as Intent
from ramses_rf.enums import Action
from ramses_tx import exceptions as exc
from ramses_tx.const import DEFAULT_ECHO_TIMEOUT, DEFAULT_RPLY_TIMEOUT
from ramses_tx.dtos import CommandDTO as Command
from ramses_tx.protocol import PortProtocol, ReadProtocol, protocol_factory
from ramses_tx.protocol.fsm import (
    Inactive,
    IsInIdle,
    ProtocolContext,
    WantEcho,
    WantRply,
    _ProtocolStateT,
)
from ramses_tx.transport import TransportConfig, transport_factory
from ramses_tx.transport.port import PortTransport
from ramses_tx.typing import QosParams

from .virtual_rf import VirtualRf


def _assert_pkt_eq_cmd(pkt: Packet, cmd: Command, msg: str = "") -> None:
    # We compare the string components directly
    expected_frame = f"{cmd.verb} --- {cmd.addr1} {cmd.addr2} {cmd.addr3} {cmd.code} {int(len(cmd.payload) / 2):03d} {cmd.payload}"
    assert str(pkt._frame).endswith(expected_frame), (
        f"{msg} | Expected: {expected_frame}, got: {pkt._frame}"
    )


# other constants
CALL_LATER_DELAY = 0.001  # FIXME: this is hardware-specific

ASSERT_CYCLE_TIME = 0.0005  # max_cycles_per_assert = max_sleep / ASSERT_CYCLE_TIME
DEFAULT_MAX_SLEEP = 0.1


def put_sensor_temp(dev_id: str, temperature: float) -> Command:
    return build_dto(
        Intent(
            src=Address(dev_id),
            dst=Address(dev_id),
            action=Action.PUT_SENSOR_TEMP,
            data={"temperature": temperature},
        )
    )


#
# TODO: add tests for send_cmd() with QoSan AttributeError for this...
# Command("RQ --- 18:111111 01:222222 --:------ 12B0 003 07")

II_CMD_STR_0 = " I --- 01:006056 --:------ 01:006056 1F09 003 0005C8"
II_CMD_0 = Command.from_cli(II_CMD_STR_0)
II_PKT_0 = Packet(dt.now(), f"... {II_CMD_STR_0}")

# TIP: using 18:000730 as the source will prevent impersonation alerts

RQ_CMD_STR_0 = "RQ --- 18:000730 01:222222 --:------ 12B0 001 00"
RP_CMD_STR_0 = "RP --- 01:222222 18:000730 --:------ 12B0 003 000000"

RQ_CMD_0 = Command.from_cli(RQ_CMD_STR_0)
RQ_PKT_0 = Packet(dt.now(), f"... {RQ_CMD_STR_0}")
RP_PKT_0 = Packet(dt.now(), f"... {RP_CMD_STR_0}")

RQ_CMD_STR_1 = "RQ --- 18:000730 01:222222 --:------ 12B0 001 01"
RP_CMD_STR_1 = "RP --- 01:222222 18:000730 --:------ 12B0 003 010000"

RQ_CMD_1 = Command.from_cli(RQ_CMD_STR_1)
RQ_PKT_1 = Packet(dt.now(), f"... {RQ_CMD_STR_1}")
RP_PKT_1 = Packet(dt.now(), f"... {RP_CMD_STR_1}")


# ### FIXTURES #################################################################


@pytest.fixture(autouse=True)
def patch_port_transport_delays() -> Generator[None, None, None]:
    """Bypass the real-world signature timeouts and duty cycle limits for tests."""
    with (
        patch("ramses_tx.transport.port._DBG_DISABLE_DUTY_CYCLE_LIMIT", True),
        patch("ramses_tx.transport.port._SIGNATURE_MAX_TRYS", 0),
        patch("ramses_tx.transport.port.MIN_INTER_WRITE_GAP", 0),
    ):
        yield


@pytest.fixture()
async def protocol(rf: VirtualRf) -> AsyncGenerator[PortProtocol, None]:
    def _msg_handler(msg: Message) -> None:
        pass

    protocol = protocol_factory(_msg_handler)

    # These values should be asserted as needed for subsequent tests
    assert isinstance(protocol, PortProtocol)  # mypy
    assert isinstance(protocol._context, ProtocolContext)  # mypy

    protocol._disable_qos = False  # HACK: needed for tests to succeed

    assert protocol._context.echo_timeout == DEFAULT_ECHO_TIMEOUT
    assert protocol._context.reply_timeout == DEFAULT_RPLY_TIMEOUT
    assert protocol._context.SEND_TIMEOUT_LIMIT == 20.0

    await assert_protocol_state(protocol, Inactive, max_sleep=0)

    _transport = await transport_factory(
        protocol,
        config=TransportConfig(),
        port_name=rf.ports[0],
        port_config=dict(),
    )
    # Cast to PortTransport to access internal test-specific details
    transport = cast(PortTransport, _transport)
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


# ##############################################################################


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

    assert getattr(protocol._context.state, "_sent_cmd", None) == cmd
    assert protocol._context._cmd_tx_count == num_sends
    assert bool(cmd) is isinstance(protocol._context.state, WantEcho | WantRply)


async def async_pkt_received(
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


# ### TESTS ####################################################################


async def _test_flow_30x(protocol: PortProtocol) -> None:
    assert (
        protocol._transport is not None
    )  # mypy: fixture ensures transport is connected
    # STEP 0: Setup...
    rf: VirtualRf = protocol._transport.get_extra_info("virtual_rf")
    ser = serial.Serial(rf.ports[1])

    qos = QosParams()

    # STEP 1: Send an I cmd (no reply)...
    task = rf._loop.create_task(protocol._send_cmd(II_CMD_0, qos=qos), name="send_1")
    _assert_pkt_eq_cmd(await task, II_CMD_0)  # no reply pkt expected

    # STEP 2: Send an RQ cmd (returns echo at L3)...
    task = rf._loop.create_task(protocol._send_cmd(RQ_CMD_0, qos=qos), name="send_2")
    protocol._loop.call_later(
        CALL_LATER_DELAY,
        ser.write,
        bytes(str(RP_PKT_0).encode("ascii")) + b"\r\n",
    )
    _assert_pkt_eq_cmd(await task, RQ_CMD_0)

    # STEP 3: Send an I cmd (no reply) *twice*...
    task = rf._loop.create_task(protocol._send_cmd(II_CMD_0, qos=qos), name="send_3A")
    _assert_pkt_eq_cmd(await task, II_CMD_0)  # no reply pkt expected

    task = rf._loop.create_task(protocol._send_cmd(II_CMD_0, qos=qos), name="send_3B")
    _assert_pkt_eq_cmd(await task, II_CMD_0)  # no reply pkt expected

    # STEP 4: Send an RQ cmd (returns echo at L3)...
    task = rf._loop.create_task(protocol._send_cmd(RQ_CMD_1, qos=qos), name="send_4A")

    # TODO: make these deterministic so ser replies *only after* it receives cmd
    protocol._loop.call_later(
        CALL_LATER_DELAY,
        ser.write,
        bytes(str(RP_PKT_0).encode("ascii")) + b"\r\n",
    )
    protocol._loop.call_later(
        CALL_LATER_DELAY,
        ser.write,
        bytes(str(RP_PKT_1).encode("ascii")) + b"\r\n",
    )

    _assert_pkt_eq_cmd(await task, RQ_CMD_1)


async def _test_flow_401(protocol: PortProtocol) -> None:
    qos = QosParams()

    numbers = list(range(24))
    tasks = dict()

    for i in numbers:
        cmd = put_sensor_temp("03:123456", i)
        tasks[i] = protocol._loop.create_task(protocol._send_cmd(cmd, qos=qos))

    assert await asyncio.gather(*tasks.values())

    for i in numbers:
        pkt = tasks[i].result()
        _assert_pkt_eq_cmd(pkt, put_sensor_temp("03:123456", i))


async def _test_flow_402(protocol: PortProtocol) -> None:
    qos = QosParams()

    numbers = list(range(24))
    tasks = dict()

    for i in numbers:
        cmd = put_sensor_temp("03:123456", i)
        tasks[i] = protocol._loop.create_task(protocol._send_cmd(cmd, qos=qos))

    random.shuffle(numbers)

    for i in numbers:
        pkt = await tasks[i]
        _assert_pkt_eq_cmd(pkt, put_sensor_temp("03:123456", i))


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
    tasks = list()
    for idx in range(num_cmds):
        cmd = build_dto(
            Intent(
                src=HGI_DEV_ADDR,
                dst=Address("01:123456"),
                action=Action.GET_ZONE_TEMP,
                data={"zone_idx": f"{idx:02X}"},
            )
        )
        coro = protocol._send_cmd(cmd, qos=QosParams())
        tasks.append(protocol._loop.create_task(coro, name=f"cmd_{idx:02X}"))

    assert await asyncio.gather(*tasks)


async def _test_flow_qos(protocol: PortProtocol) -> None:
    assert isinstance(protocol._context, ProtocolContext)  # mypy

    # HACK: to reduce test time
    protocol._context.SEND_TIMEOUT_LIMIT = 0.01
    protocol._context.max_retry_limit = 0

    #
    # ### Simple test for an I (does not expect any reply)...

    cmd = put_sensor_temp("03:000111", 19.5)
    pkt = await protocol._send_cmd(cmd)  # qos == QosParams()
    _assert_pkt_eq_cmd(pkt, cmd, "Should be echo as there's no reply to wait for")

    cmd = put_sensor_temp("03:000222", 19.5)
    pkt = await protocol._send_cmd(cmd, qos=None)  # qos == QosParams()
    _assert_pkt_eq_cmd(pkt, cmd, "Should be echo as there's no reply to wait for")

    cmd = put_sensor_temp("03:000333", 19.5)
    pkt = await protocol._send_cmd(cmd, qos=QosParams())
    _assert_pkt_eq_cmd(pkt, cmd, "Should be echo as there's no reply to wait for")

    cmd = put_sensor_temp("03:000444", 19.5)
    pkt = await protocol._send_cmd(cmd, qos=QosParams())
    _assert_pkt_eq_cmd(pkt, cmd, "should be echo as there is no wait_for_reply")

    cmd = put_sensor_temp("03:000555", 19.5)
    pkt = await protocol._send_cmd(cmd, qos=QosParams())
    _assert_pkt_eq_cmd(pkt, cmd, "should be echo as there is no wait_for_reply")

    cmd = put_sensor_temp("03:000666", 19.5)
    pkt = await protocol._send_cmd(cmd, qos=QosParams())
    _assert_pkt_eq_cmd(pkt, cmd, "Should be echo as there's no reply to wait for")

    # # ### Simple test for an RQ (expects an RP)...

    cmd = build_dto(
        Intent(
            src=HGI_DEV_ADDR,
            dst=Address("01:000111"),
            action=Action.GET_SYSTEM_TIME,
            data={},
        )
    )
    pkt = await protocol._send_cmd(cmd)
    _assert_pkt_eq_cmd(pkt, cmd, "Should be echo as there's no reply to wait for")

    cmd = build_dto(
        Intent(
            src=HGI_DEV_ADDR,
            dst=Address("01:000222"),
            action=Action.GET_SYSTEM_TIME,
            data={},
        )
    )
    pkt = await protocol._send_cmd(cmd, qos=None)
    _assert_pkt_eq_cmd(pkt, cmd, "Should be echo as there's no reply to wait for")

    cmd = build_dto(
        Intent(
            src=HGI_DEV_ADDR,
            dst=Address("01:000333"),
            action=Action.GET_SYSTEM_TIME,
            data={},
        )
    )
    pkt = await protocol._send_cmd(cmd, qos=QosParams())
    _assert_pkt_eq_cmd(pkt, cmd, "Should be echo as there's no reply to wait for")

    cmd = build_dto(
        Intent(
            src=HGI_DEV_ADDR,
            dst=Address("01:000444"),
            action=Action.GET_SYSTEM_TIME,
            data={},
        )
    )
    pkt = await protocol._send_cmd(cmd, qos=QosParams())
    _assert_pkt_eq_cmd(pkt, cmd, "Should be echo as there is no wait_for_reply")

    cmd = build_dto(
        Intent(
            src=HGI_DEV_ADDR,
            dst=Address("01:000555"),
            action=Action.GET_SYSTEM_TIME,
            data={},
        )
    )
    pkt = await protocol._send_cmd(cmd, qos=QosParams())
    _assert_pkt_eq_cmd(pkt, cmd, "Should be echo as there is no wait_for_reply")

    cmd = build_dto(
        Intent(
            src=HGI_DEV_ADDR,
            dst=Address("01:000666"),
            action=Action.GET_SYSTEM_TIME,
            data={},
        )
    )
    pkt = await protocol._send_cmd(cmd, qos=QosParams(timeout=0.05))
    _assert_pkt_eq_cmd(pkt, cmd, "Should be echo as there's no reply to wait for")

    # # ### Simple test for an I (does not expect any reply)...

    cmd = put_sensor_temp("03:000999", 19.5)
    pkt = await protocol._send_cmd(cmd)
    _assert_pkt_eq_cmd(pkt, cmd)


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
