#!/usr/bin/env python3
"""RAMSES RF - Check get/set of zone/DHW schedules."""

import asyncio
from typing import Any

import pytest

from ramses_rf import Gateway
from ramses_rf.devices import Controller
from ramses_rf.gateway import GatewayConfig
from ramses_rf.systems import Evohome, Zone
from ramses_rf.typing import InnerScheduleT
from ramses_tx.address import HGI_DEVICE_ID, Address
from ramses_tx.protocol import PortProtocol
from ramses_tx.typing import DeviceIdT

from .virtual_rf import VirtualRf

TST_ID_ = Address("18:123456").id  # the id of the test HGI80-compatible device


# ### FIXTURES #########################################################################

pytestmark = pytest.mark.asyncio()


@pytest.fixture()
def gwy_config() -> dict[str, Any]:
    return {
        "config": GatewayConfig(
            disable_discovery=True,
        ),
        "disable_qos": False,  # QoS is required for this test
        "enforce_known_list": False,
        "known_list": {HGI_DEVICE_ID: {}},  # req'd to thwart foreign HGI blacklisting
    }


@pytest.fixture()
def gwy_dev_id() -> DeviceIdT:
    return TST_ID_


#######################################################################################


@pytest.mark.xdist_group(name="virt_serial")
async def _test_get_schedule(gwy: Gateway, ctl_id: DeviceIdT, idx: str) -> None:
    """Test obtaining the version and schedule."""

    assert gwy._engine._loop is asyncio.get_running_loop()  # scope BUG is here
    assert isinstance(gwy._engine._protocol, PortProtocol)  # mypy
    assert gwy._engine._protocol._disable_qos is False  # QoS is required for this test

    _: Controller = gwy.device_registry.get_device(ctl_id, cls=Controller)

    tcs: Evohome | None = gwy.tcs
    assert isinstance(tcs, Evohome)  # mypy

    global_ver, did_io = await tcs._schedule_version()
    assert isinstance(global_ver, int) and did_io

    zon: Zone = tcs.get_htg_zone(idx)
    schedule: InnerScheduleT | None = await zon.get_schedule()
    assert schedule is not None
    assert len(schedule) == 7  # days of week


#######################################################################################

# 2021-10-24T15:26:06.084723 023 RQ --- 18:013393 01:145038 --:------ 0404 007 01200008000100
# 2021-10-24T15:26:06.144750 046 RP --- 01:145038 18:013393 --:------ 0404 048 0120000829010368816DCCB10D80300C0551DB710C03515052310303300B73320A440111E0527D9D9C2722A252DF768E
# 2021-10-24T15:26:06.303681 023 RQ --- 18:013393 01:145038 --:------ 0404 007 01200008000203
# 2021-10-24T15:26:06.347579 046 RP --- 01:145038 18:013393 --:------ 0404 048 01200008290203A1AFFB6EFB39FAEEDD46FFDFCD59648AA729780A9E82A7E01978069E8167E025F0127809BC049E83E7
# 2021-10-24T15:26:06.381601 023 RQ --- 18:013393 01:145038 --:------ 0404 007 01200008000303
# 2021-10-24T15:26:06.417568 045 RP --- 01:145038 18:013393 --:------ 0404 038 012000081F0303E039780E5EBEFEB677A52DF66F5FAFB4F5E30578015E80178D77006F8713D1


TEST_SUITE = {
    r"RQ.* 18:.* 01:.* 0006 001 00": "RP --- 01:145038 18:013393 --:------ 0006 004 00050135",
    r"RQ.* 18:.* 01:.* 0404 007 01200008000100": "RP --- 01:145038 18:013393 --:------ 0404 048 0120000829010368816DCCB10D80300C0551DB710C03515052310303300B73320A440111E0527D9D9C2722A252DF768E",
    r"RQ.* 18:.* 01:.* 0404 007 01200008000203": "RP --- 01:145038 18:013393 --:------ 0404 048 01200008290203A1AFFB6EFB39FAEEDD46FFDFCD59648AA729780A9E82A7E01978069E8167E025F0127809BC049E83E7",
    r"RQ.* 18:.* 01:.* 0404 007 01200008000303": "RP --- 01:145038 18:013393 --:------ 0404 038 012000081F0303E039780E5EBEFEB677A52DF66F5FAFB4F5E30578015E80178D77006F8713D1",
}


async def test_get_schedule_fake(fake_evofw3: Gateway) -> None:
    """Test obtaining the schedule from a faked controller via Virtual RF."""

    assert fake_evofw3._engine._transport  # mypy

    # Prime the virtual RF with the expected replies...
    rf: VirtualRf = fake_evofw3._engine._transport.get_extra_info("virtual_rf")
    for k, v in TEST_SUITE.items():
        rf.add_reply_for_cmd(k, v)

    await _test_get_schedule(fake_evofw3, "01:145038", "01")


@pytest.mark.xdist_group(name="real_serial")
async def _test_get_schedule_mqtt(mqtt_evofw3: Gateway) -> None:
    """Test obtaining the schedule from a real controller via MQTT."""

    await _test_get_schedule(mqtt_evofw3, "01:145038", "01")


@pytest.mark.xdist_group(name="real_serial")
async def _test_get_schedule_real(real_evofw3: Gateway) -> None:
    """Test obtaining the schedule from a real controller via RF."""

    await _test_get_schedule(real_evofw3, "01:145038", "01")
