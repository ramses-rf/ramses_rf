#!/usr/bin/env python3
"""Unit and integration tests for VirtualRf harness, network routing, and HGI behaviors."""

import asyncio
from collections.abc import AsyncGenerator
from typing import Any, Final, cast
from unittest.mock import MagicMock, patch

import pytest
import serialx as serial

from ramses_rf import Address, CommandDTO as Command, Gateway
from ramses_rf.gateway import GatewayConfig
from ramses_tx import Code, Packet, exceptions as exc
from ramses_tx.address import HGI_DEVICE_ID
from ramses_tx.config import EngineConfig
from ramses_tx.protocol import PortProtocol
from ramses_tx.transport.mqtt import MqttTransport
from ramses_tx.transport.port import PortTransport
from ramses_tx.typing import DeviceIdT, QosParams
from tests_rf.virtual_rf import VirtualRf, rf_factory, virtual_rf as vrf_mod
from tests_rf.virtual_rf.const import SCHEMA_3, HgiFwTypes

TEST_DATA: Final[bytes] = b"Hello World\r\n"
ASSERT_CYCLE_TIME = 0.001
DEFAULT_MAX_SLEEP = 1

GWY_CONFIG: dict[str, Any] = {
    "disable_discovery": True,
    "enforce_known_list": False,
}

SCHEMA_0: dict[str, Any] = {
    "schema": {"orphans_hvac": ["40:000000"]},
    "known_list": {"40:000000": {"class": "REM"}},
}

SCHEMA_1: dict[str, Any] = {
    "schema": {"orphans_hvac": ["41:111111"]},
    "known_list": {"41:111111": {"class": "FAN"}},
}


@pytest.fixture
async def virtual_rf() -> AsyncGenerator[VirtualRf, None]:
    """Fixture to provide a VirtualRf instance using Async Context Manager."""
    async with VirtualRf(num_ports=3) as vrf:
        yield vrf


@pytest.mark.asyncio
async def test_virtual_rf_lifecycle() -> None:
    """Test start and stop lifecycle via Async Context Manager."""
    async with VirtualRf(num_ports=2) as vrf:
        assert len(vrf.ports) == 2
        assert len(vrf._master_to_port) == 2

    assert len(vrf._master_to_port) == 0


@pytest.mark.asyncio
async def test_broadcast_data(virtual_rf: VirtualRf) -> None:
    """Test that data written to one PTY is broadcast to others."""
    port_0 = virtual_rf.ports[0]
    port_1 = virtual_rf.ports[1]
    fd_0_master = virtual_rf._port_to_master[port_0]

    mock_file_io = MagicMock()
    original_io = virtual_rf._port_to_object[port_1]
    virtual_rf._port_to_object[port_1] = mock_file_io

    try:
        with patch.object(
            virtual_rf._port_to_object[port_0], "read", return_value=TEST_DATA
        ):
            virtual_rf._handle_data_ready(fd_0_master)

        mock_file_io.write.assert_called_with(b"000 " + TEST_DATA)

    finally:
        virtual_rf._port_to_object[port_1] = original_io


@pytest.mark.asyncio
async def test_blocking_io_handling(virtual_rf: VirtualRf) -> None:
    """Test handling of BlockingIOError during broadcast write."""
    port_0 = virtual_rf.ports[0]
    port_1 = virtual_rf.ports[1]
    fd_0_master = virtual_rf._port_to_master[port_0]

    mock_file_io = MagicMock()
    mock_file_io.write.side_effect = BlockingIOError
    original_io = virtual_rf._port_to_object[port_1]
    virtual_rf._port_to_object[port_1] = mock_file_io

    with patch.object(vrf_mod._LOGGER, "warning") as mock_log:
        with patch.object(
            virtual_rf._port_to_object[port_0], "read", return_value=TEST_DATA
        ):
            virtual_rf._handle_data_ready(fd_0_master)

        mock_log.assert_called_with(
            "Buffer full writing to %s, dropping packet", port_1
        )

    virtual_rf._port_to_object[port_1] = original_io


@pytest.mark.asyncio
async def test_gateway_emulation(virtual_rf: VirtualRf) -> None:
    """Test hardware-specific emulation logic for different firmware types."""
    virtual_rf.set_gateway(virtual_rf.ports[0], "18:111111", HgiFwTypes.EVOFW3)
    virtual_rf.set_gateway(
        virtual_rf.ports[1], "18:222222", HgiFwTypes.EVOFW3_FTDI
    )
    virtual_rf.set_gateway(virtual_rf.ports[2], "18:333333", HgiFwTypes.HGI_80)

    for i in range(2):
        port = virtual_rf.ports[i]
        response = virtual_rf._proc_after_rx(port, b"!V")
        assert response == b"# evofw3 0.7.1\r\n"

    hgi_port = virtual_rf.ports[2]
    assert virtual_rf._proc_after_rx(hgi_port, b"!V") is None


@pytest.mark.asyncio
async def test_schema_3_integration(virtual_rf: VirtualRf) -> None:
    """Verify that SCHEMA_3 (HVAC/Generic) initializes without errors."""
    gwy_id = list(SCHEMA_3["known_list"].keys())[0]
    virtual_rf.set_gateway(virtual_rf.ports[0], gwy_id)
    assert virtual_rf.gateways[gwy_id] == virtual_rf.ports[0]


@pytest.mark.asyncio
async def test_rapid_cycling_stress_test() -> None:
    """Stress test: Rapidly start and stop VirtualRf environment."""
    for _ in range(50):
        async with VirtualRf(num_ports=2) as vrf:
            assert len(vrf.ports) == 2
            await asyncio.sleep(0)
        await asyncio.sleep(0)


# --- Virtual Network Helpers & Integration Tests ---


async def assert_code_in_device_msgindex(
    gwy: Gateway,
    dev_id: DeviceIdT,
    code: Code,
    max_sleep: float = DEFAULT_MAX_SLEEP,
    test_not: bool = False,
) -> None:
    """Fail if device doesn't exist or code is missing from msg_db."""

    async def _has_code() -> bool:
        dev = gwy.device_registry.device_by_id.get(dev_id)
        if not dev:
            return False

        if gwy.message_store:
            return await gwy.message_store.contains(
                source=dev_id, code=str(code)
            ) or await gwy.message_store.contains(
                destination=dev_id, code=str(code)
            )

        msgs = await dev.entity_state.get_message_log_flat()
        cached = await dev.entity_state.get_all_messages()
        return code in msgs or code in {m.code for m in cached}

    for _ in range(int(max_sleep / ASSERT_CYCLE_TIME)):
        await asyncio.sleep(ASSERT_CYCLE_TIME)
        if await _has_code() != test_not:
            break

    assert await _has_code() != test_not


async def assert_devices(
    gwy: Gateway, devices: list[str], max_sleep: float = DEFAULT_MAX_SLEEP
) -> None:
    """Fail if device registry does not match expected device set."""
    expected = sorted(Address(d).id for d in devices)

    for _ in range(int(max_sleep / ASSERT_CYCLE_TIME)):
        await asyncio.sleep(ASSERT_CYCLE_TIME)
        if sorted(d.id for d in gwy.device_registry.devices) == expected:
            break

    assert sorted(d.id for d in gwy.device_registry.devices) == expected


async def assert_this_packet(
    transport: PortTransport,
    cmd: Command,
    max_sleep: float = DEFAULT_MAX_SLEEP,
) -> None:
    """Check transport current packet matching expected command."""
    for _ in range(int(max_sleep / ASSERT_CYCLE_TIME)):
        await asyncio.sleep(ASSERT_CYCLE_TIME)
        if (
            transport._this_packet
            and transport._this_packet._frame == Packet._from_cmd(cmd)._frame
        ):
            break
    assert (
        transport._this_packet
        and transport._this_packet._frame == Packet._from_cmd(cmd)._frame
    )


@pytest.mark.xdist_group(name="virt_serial")
async def test_virtual_rf_dev_disc() -> None:
    """Check virtual RF network device discovery."""
    rf = VirtualRf(3)

    gwy_0: Gateway | None = None
    gwy_1: Gateway | None = None

    try:
        gwy_config = GatewayConfig(
            disable_discovery=GWY_CONFIG["disable_discovery"],
            engine=EngineConfig(
                enforce_known_list=GWY_CONFIG["enforce_known_list"]
            ),
        )

        rf.set_gateway(rf.ports[0], "18:000000")
        gwy_0 = Gateway(rf.ports[0], config=gwy_config)
        await assert_devices(gwy_0, [])

        rf.set_gateway(rf.ports[1], "18:111111")
        gwy_1 = Gateway(rf.ports[1], config=gwy_config)
        await assert_devices(gwy_1, [])

        ser_2 = serial.serial_for_url(rf.ports[2])
        ser_2.open()

        await gwy_0.start()
        assert gwy_0._engine._protocol._transport
        await assert_devices(gwy_0, ["18:000000"])

        await gwy_1.start()
        assert gwy_1._engine._protocol._transport
        await assert_devices(gwy_0, ["18:000000", "18:111111"])
        await assert_devices(gwy_1, ["18:000000", "18:111111"])

        cmd = Command.from_cli(
            " I --- 01:010000 --:------ 01:010000 1F09 003 0004B5"
        )
        ser_2.write(bytes(f"{cmd}\r\n".encode("ascii")))

        await assert_devices(gwy_0, ["01:010000", "18:000000", "18:111111"])
        await assert_devices(gwy_1, ["01:010000", "18:000000", "18:111111"])

        cmd = Command.from_cli(
            " I --- 01:011111 --:------ 01:011111 1F09 003 0004B5"
        )
        ser_2.write(bytes(f"{cmd}\r\n".encode("ascii")))

        await assert_devices(
            gwy_0, ["01:010000", "01:011111", "18:000000", "18:111111"]
        )
        await assert_devices(
            gwy_1, ["01:010000", "01:011111", "18:000000", "18:111111"]
        )

    finally:
        ser_2.close()
        if gwy_0:
            await gwy_0.stop()
        if gwy_1:
            await gwy_1.stop()
        await rf.stop()


@pytest.mark.xdist_group(name="virt_serial")
async def test_virtual_rf_packet_flow() -> None:
    """Check virtual RF network packet flow."""
    rf: VirtualRf | None = None
    gwy_0: Gateway | None = None
    gwy_1: Gateway | None = None

    try:
        rf, (gwy_0, gwy_1) = await rf_factory(
            [GWY_CONFIG | SCHEMA_0, GWY_CONFIG | SCHEMA_1]
        )

        assert gwy_0._engine._protocol._transport
        await assert_devices(gwy_0, ["18:000000", "18:111111", "40:000000"])

        assert gwy_1._engine._protocol._transport
        await assert_devices(gwy_1, ["18:111111", "41:111111"])

        await assert_code_in_device_msgindex(
            gwy_0,
            cast(DeviceIdT, "01:022222"),
            Code._1F09,
            max_sleep=0,
            test_not=True,
        )

        cmd = Command.from_cli(
            " I --- 01:022222 --:------ 01:022222 1F09 003 0004B5"
        )
        await gwy_0._async_send_dto(cmd, num_repeats=1)

        await assert_devices(
            gwy_0, ["01:022222", "18:000000", "18:111111", "40:000000"]
        )
        await assert_code_in_device_msgindex(
            gwy_0, cast(DeviceIdT, "01:022222"), Code._1F09
        )

        await assert_this_packet(gwy_0._engine._transport, cmd)
        await assert_this_packet(gwy_1._engine._transport, cmd)

    finally:
        if rf:
            if gwy_0:
                await gwy_0.stop()
            if gwy_1:
                await gwy_1.stop()
            await rf.stop()


# --- HGI Behavior & Address Filtering Tests ---

TST_ID_ = Address("18:222222").id

TEST_CMDS = {
    10: f"RQ --- {TST_ID_} 63:262142 --:------ 10E0 001 00",
    11: r"RQ --- 18:000730 63:262142 --:------ 10E0 001 00",
    20: f" I --- {TST_ID_} {TST_ID_} --:------ 30C9 003 000222",
    21: f" I --- 18:000730 {TST_ID_} --:------ 30C9 003 000333",
    30: f"RP --- {TST_ID_} 18:000730 --:------ 30C9 003 000444",
    31: r"RP --- 18:000730 18:000730 --:------ 30C9 003 000555",
}


@pytest.fixture(autouse=True)
def patch_strict_checking(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply strict checking monkeypatch globally to tests."""
    monkeypatch.setattr(
        "ramses_tx.address._DBG_DISABLE_STRICT_CHECKING",
        True,
    )
    monkeypatch.setattr(
        "ramses_rf.address._DBG_DISABLE_STRICT_CHECKING",
        True,
    )


async def _test_gwy_device(gwy: Gateway, test_index: int) -> None:
    """Check GWY address/type detection, and behaviour of its treatment of addr0."""
    assert gwy._engine._loop is asyncio.get_running_loop()

    if not isinstance(gwy._engine._protocol, PortProtocol):
        assert False, "QoS protocol not enabled"

    assert gwy.hgi

    cmd_str = TEST_CMDS[test_index].replace(TST_ID_, gwy.hgi.id)
    cmd = Command.from_cli(cmd_str)
    assert str(cmd) == cmd_str

    is_hgi80 = not gwy._engine._protocol._is_evofw3
    assert gwy._engine._transport

    if isinstance(gwy._engine._transport, MqttTransport):
        timeout = 2.0
    elif gwy._engine._transport.get_extra_info("virtual_rf"):
        timeout = 1.0
    else:
        timeout = 2.0

    try:
        packet = await gwy._engine._protocol.send_cmd(
            cmd, qos=QosParams(timeout=timeout)
        )
    except exc.ProtocolSendFailed:
        if is_hgi80 and cmd_str[7:16] != HGI_DEVICE_ID:
            return
        raise

    assert packet is not None

    if is_hgi80 and cmd_str[7:16] != HGI_DEVICE_ID:
        assert False, packet

    if cmd_str[7:16] == HGI_DEVICE_ID:
        packet_str = cmd_str[:7] + gwy.hgi.id + cmd_str[16:]
    else:
        packet_str = cmd_str

    assert packet._frame == packet_str


@pytest.fixture()
def gwy_config() -> dict[str, Any]:
    return {
        "config": GatewayConfig(
            disable_discovery=True,
        ),
        "disable_qos": False,
        "enforce_known_list": False,
        "known_list": {HGI_DEVICE_ID: {}},
    }


@pytest.fixture()
def gwy_dev_id() -> DeviceIdT:
    return TST_ID_


@pytest.mark.xdist_group(name="virt_serial")
@pytest.mark.parametrize("test_index", list(TEST_CMDS.keys()))
async def test_fake_evofw3(fake_evofw3: Gateway, test_index: int) -> None:
    """Check the behaviour of the fake (virtual) evofw3 against GWY test."""
    await _test_gwy_device(fake_evofw3, test_index)
