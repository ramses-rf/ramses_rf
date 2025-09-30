#!/usr/bin/env python3

# TODO: Add assert_protocol_ready to VirtualRF factory (or in library?)

"""Test the Virtual RF library - VirtualRF is used for testing."""

import asyncio

import pytest
import serial  # type: ignore[import-untyped]

from ramses_rf import Address, Code, Command, Gateway
from ramses_tx.schemas import DeviceIdT
from ramses_tx.transport import PortTransport
from tests_rf.virtual_rf import VirtualRf, rf_factory

# other constants
ASSERT_CYCLE_TIME = 0.001  # max_cycles_per_assert = max_sleep / ASSERT_CYCLE_TIME
DEFAULT_MAX_SLEEP = 1


GWY_CONFIG = {
    "config": {
        "disable_discovery": True,  # we're testing discovery here
        "enforce_known_list": False,
    },
}


SCHEMA_0 = {
    "orphans_hvac": ["40:000000"],
    "known_list": {"40:000000": {"class": "REM"}},
}

SCHEMA_1 = {
    "orphans_hvac": ["41:111111"],
    "known_list": {"41:111111": {"class": "FAN"}},
}


# ######################################################################################


async def assert_code_in_device_msgindex(
    gwy: Gateway,
    dev_id: DeviceIdT,
    code: Code,
    max_sleep: int = DEFAULT_MAX_SLEEP,
    test_not: bool = False,
) -> None:
    """Fail if the device doesn't exist, or if it doesn't have the code in its msg_db."""

    for _ in range(int(max_sleep / ASSERT_CYCLE_TIME)):
        await asyncio.sleep(ASSERT_CYCLE_TIME)
        if (
            (_ := gwy.device_by_id.get(dev_id))
            and gwy.msg_db
            and (
                gwy.msg_db.contains(src=dev_id, code=str(code))
                or gwy.msg_db.contains(dst=dev_id, code=str(code))
            )
        ) != test_not:
            break
    assert (
        (_ := gwy.device_by_id.get(dev_id))
        and gwy.msg_db
        and (
            gwy.msg_db.contains(src=dev_id, code=str(code))
            or gwy.msg_db.contains(dst=dev_id, code=str(code))
        )
    ) != test_not  # TODO: fix me


async def assert_devices(
    gwy: Gateway, devices: list[str], max_sleep: int = DEFAULT_MAX_SLEEP
) -> None:
    """Fail if the two sets of devices are not equal."""

    devices = [Address(d).id for d in devices]

    for _ in range(int(max_sleep / ASSERT_CYCLE_TIME)):
        await asyncio.sleep(ASSERT_CYCLE_TIME)
        if len(gwy.devices) == len(devices):
            break
    assert sorted(d.id for d in gwy.devices) == sorted(devices)


async def assert_this_pkt(
    transport: PortTransport, cmd: Command, max_sleep: int = DEFAULT_MAX_SLEEP
) -> None:
    """Check, at the transport layer, that the current packet is as expected."""
    for _ in range(int(max_sleep / ASSERT_CYCLE_TIME)):
        await asyncio.sleep(ASSERT_CYCLE_TIME)
        if transport._this_pkt and transport._this_pkt._frame == cmd._frame:
            break
    assert transport._this_pkt and transport._this_pkt._frame == cmd._frame


# ### TESTS ############################################################################


async def _test_virtual_rf_dev_disc(
    rf: VirtualRf, gwy_0: Gateway, gwy_1: Gateway
) -> None:
    """Check the virtual RF network behaves as expected (device discovery)."""

    ser_2 = serial.Serial(rf.ports[2])

    # TEST 0: Tx of fingerprint packet with one on/one off
    await gwy_0.start()
    assert gwy_0._protocol._transport

    await assert_devices(gwy_0, ["18:000000"])
    await assert_devices(gwy_1, [])

    await gwy_1.start()
    assert gwy_1._protocol._transport

    # NOTE: will pick up gwy 18:111111, since Foreign gwy detect has been removed
    await assert_devices(gwy_0, ["18:000000", "18:111111"])
    await assert_devices(gwy_1, ["18:111111"])

    # TEST 1: Tx to all from GWY /dev/pty/0 (NB: no RSSI)
    cmd = Command(" I --- 01:010000 --:------ 01:010000 1F09 003 0004B5")
    gwy_0.send_cmd(cmd)

    await assert_devices(gwy_0, ["01:010000", "18:000000", "18:111111"])
    await assert_devices(gwy_1, ["01:010000", "18:111111"])

    # TEST 2: Tx to all from non-GWY /dev/pty/2 (NB: no RSSI)
    cmd = Command(" I --- 01:011111 --:------ 01:011111 1F09 003 0004B5")
    ser_2.write(bytes(f"{cmd}\r\n".encode("ascii")))

    await assert_devices(gwy_0, ["01:010000", "01:011111", "18:000000", "18:111111"])
    await assert_devices(gwy_1, ["01:010000", "01:011111", "18:111111"])

    # TEST 3: Rx only by *only one* GWY (NB: needs RSSI)
    cmd = Command(" I --- 01:022222 --:------ 01:022222 1F09 003 0004B5")
    list(rf._port_to_object.values())[1].write(bytes(f"000 {cmd}\r\n".encode("ascii")))

    await assert_devices(gwy_0, ["01:010000", "01:011111", "18:000000", "18:111111"])
    await assert_devices(gwy_1, ["01:010000", "01:011111", "01:022222", "18:111111"])


async def _test_virtual_rf_pkt_flow(
    rf: VirtualRf, gwy_0: Gateway, gwy_1: Gateway
) -> None:
    """Check the virtual RF network behaves as expected (packet flow)."""

    # TEST 1:
    await assert_code_in_device_msgindex(
        gwy_0, "01:022222", Code._1F09, max_sleep=0, test_not=True
    )  # device won't exist

    cmd = Command(" I --- 01:022222 --:------ 01:022222 1F09 003 0004B5")
    gwy_0.send_cmd(cmd, num_repeats=1)

    await assert_devices(gwy_0, ["01:022222", "18:000000", "18:111111", "40:000000"])
    await assert_code_in_device_msgindex(gwy_0, "01:022222", Code._1F09)

    await assert_this_pkt(gwy_0._transport, cmd)
    await assert_this_pkt(gwy_1._transport, cmd)

    # TEST 2:
    # await assert_code_in_device_msgindex(
    #     gwy_0, "40:000000", Code._22F1, max_sleep=0, test_not=True
    # )

    cmd = Command(" I --- 40:000000 --:------ 40:000000 22F1 003 000507")
    gwy_0.send_cmd(cmd, num_repeats=1)

    # await assert_code_in_device_msgindex(gwy_0, "40:000000", Code._22F1)  # ?needs QoS

    await assert_this_pkt(gwy_0._transport, cmd)
    await assert_this_pkt(gwy_1._transport, cmd)

    await assert_devices(gwy_0, ["01:022222", "18:000000", "18:111111", "40:000000"])
    await assert_devices(gwy_1, ["01:022222", "18:111111", "40:000000", "41:111111"])


# NOTE: does not use factory
@pytest.mark.xdist_group(name="virt_serial")
async def test_virtual_rf_dev_disc() -> None:
    """Check the virtual RF network behaves as expected (device discovery)."""

    rf = VirtualRf(3)

    gwy_0: Gateway = None  # type: ignore[assignment]
    gwy_1: Gateway = None  # type: ignore[assignment]

    try:
        rf.set_gateway(rf.ports[0], "18:000000")
        gwy_0 = Gateway(rf.ports[0], **GWY_CONFIG)
        await assert_devices(gwy_0, [])

        rf.set_gateway(rf.ports[1], "18:111111")
        gwy_1 = Gateway(rf.ports[1], **GWY_CONFIG)
        await assert_devices(gwy_1, [])

        await _test_virtual_rf_dev_disc(rf, gwy_0, gwy_1)

    finally:
        if gwy_0:
            await gwy_0.stop()
        if gwy_1:
            await gwy_1.stop()
        await rf.stop()


# NOTE: uses factory
@pytest.mark.xdist_group(name="virt_serial")
async def test_virtual_rf_pkt_flow() -> None:
    """Check the virtual RF network behaves as expected (packet flow)."""

    rf: VirtualRf = None  # type: ignore[assignment]

    try:
        rf, (gwy_0, gwy_1) = await rf_factory(
            [GWY_CONFIG | SCHEMA_0, GWY_CONFIG | SCHEMA_1]
        )

        assert gwy_0._protocol._transport
        # NOTE: will pick up gwy 18:111111, since Foreign gwy detect has been removed
        await assert_devices(gwy_0, ["18:000000", "18:111111", "40:000000"])

        assert gwy_1._protocol._transport
        await assert_devices(gwy_1, ["18:111111", "41:111111"])

        await _test_virtual_rf_pkt_flow(rf, gwy_0, gwy_1)

    finally:
        if rf:
            await gwy_0.stop()
            await gwy_1.stop()
            await rf.stop()
