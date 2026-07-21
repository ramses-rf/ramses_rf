#!/usr/bin/env python3
"""RAMSES RF - Check get of TCS fault logs."""

import asyncio
import re
from typing import Any

import pytest

from ramses_rf import Gateway
from ramses_rf.devices import Controller
from ramses_rf.gateway import GatewayConfig
from ramses_rf.systems import Evohome
from ramses_tx.address import HGI_DEVICE_ID, Address
from ramses_tx.protocol import PortProtocol
from ramses_tx.typing import DeviceIdT
from tests_rf.virtual_rf import VirtualRf

from ramses_rf.const import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    I_,
    RP,
    RQ,
    W_,
    Code,
)

from .conftest import TEST_DIR

LOGS_DIR = f"{TEST_DIR}/logs"

TST_ID_ = Address("18:123456").id  # the id of the test HGI80-compatible device

TST_FAULTLOG_LIMIT = 3  #  max is 63 (0x3F) entries, 3 is enough for testing


# ### FIXTURES ########################################################################

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


# ### TESTS ###########################################################################


async def _test_get_faultlog(gwy: Gateway, ctl_id: DeviceIdT) -> None:
    """Test obtaining the fault log."""

    assert gwy._engine._loop is asyncio.get_running_loop()  # scope BUG is here
    assert isinstance(gwy._engine._protocol, PortProtocol)  # mypy
    assert gwy._engine._protocol._disable_qos is False  # QoS is required for this test

    _: Controller = gwy.device_registry.get_device(ctl_id, cls=Controller)

    tcs: Evohome | None = gwy.tcs
    assert isinstance(tcs, Evohome)  # mypy

    faultlog = await tcs.get_faultlog(limit=TST_FAULTLOG_LIMIT)
    assert isinstance(faultlog, dict)  # Can be empty {} if no faults exist


#######################################################################################


def _create_test_suite(log_file_name: str) -> dict[str, str]:
    def proc_log_line_pair(rq: str, rp: str) -> dict[str, str]:
        if RQ not in rq and RP not in rp:
            # RQ --- 18:006402 01:145038 --:------ 0418 003 000000
            # RP --- 01:145038 18:006402 --:------ 0418 022 004000B00400000000004A18...
            raise ValueError(f"Bad log file RQ/RP pair at line {rq}")

        rq_ = re.sub(r" 18:...... ", f" {TST_ID_} ", rq[31:].strip())
        rp_ = re.sub(r" 18:...... ", f" {TST_ID_} ", rp[31:].strip())
        return {rq_: rp_}

    result: dict[str, str] = {}

    with open(log_file_name) as file:
        lines = [line for line in file if line.strip()]  # Skip blank lines
        for i in range(0, len(lines), 2):
            result |= proc_log_line_pair(lines[i], lines[i + 1])

    return result


TEST_SUITE = _create_test_suite(f"{LOGS_DIR}/test_api_faultlog.log")


async def test_get_faultlog_fake(fake_evofw3: Gateway) -> None:
    """Test obtaining a populated schedule from a faked controller via Virtual RF."""

    assert fake_evofw3._engine._transport  # mypy

    # Prime the virtual RF with the expected replies...
    rf: VirtualRf = fake_evofw3._engine._transport.get_extra_info("virtual_rf")
    for k, v in TEST_SUITE.items():
        rf.add_reply_for_cmd(k, v)

    # we'll need null replies for all the other fault log slots (max 64?)
    for idx in range(len(TEST_SUITE), 64):
        rf.add_reply_for_cmd(
            list(TEST_SUITE.keys())[-1][:-2] + f"{idx:02X}",
            list(TEST_SUITE.values())[-1],
        )

    await _test_get_faultlog(fake_evofw3, "01:145038")

    tcs = fake_evofw3.tcs
    assert tcs  # mypy

    _ = await tcs.get_faultlog(limit=64, force_refresh=True)

    assert len(tcs._faultlog._log) == 49

    # Test the newly standardized public properties in heat.py
    assert (
        tcs.latest_event
        == "24-04-20T12:44:52, restore, battery_low, 00:000001, 00, controller"
    )
    assert (
        tcs.latest_fault
        == "24-04-20T09:26:49, fault,   battery_low, 00:000001, 00, controller"
    )
    assert tcs.active_faults == (
        "24-03-20T20:11:13, fault,   comms_fault, 07:123456, FA, dhw_sensor",
    )


async def test_get_faultlog_empty(fake_evofw3: Gateway) -> None:
    """Test obtaining an empty fault log from a faked controller via Virtual RF."""

    assert fake_evofw3._engine._transport  # mypy

    # Prime the virtual RF with entirely empty (null) replies...
    rf: VirtualRf = fake_evofw3._engine._transport.get_extra_info("virtual_rf")
    for idx in range(0, 64):
        rq = f"RQ --- {TST_ID_} 01:145038 --:------ 0418 003 0000{idx:02X}"
        # A real controller always replies with idx 00 for empty slots:
        rp = f"RP --- 01:145038 {TST_ID_} --:------ 0418 022 000000B0000000000000000000007FFFFF7000000000"
        rf.add_reply_for_cmd(rq, rp)

    await _test_get_faultlog(fake_evofw3, "01:145038")

    tcs = fake_evofw3.tcs
    assert tcs  # mypy

    faultlog = await tcs.get_faultlog(limit=64, force_refresh=True)

    assert len(faultlog) == 0
    assert tcs._faultlog.is_current is True

    # The public properties should gracefully handle the empty state
    assert tcs.latest_event is None
    assert tcs.latest_fault is None
    assert tcs.active_faults is None


@pytest.mark.xdist_group(name="real_serial")
async def test_get_faultlog_mqtt(mqtt_evofw3: Gateway) -> None:
    """Test obtaining the fault log from a real controller via MQTT."""

    await _test_get_faultlog(mqtt_evofw3, "01:145038")


@pytest.mark.xdist_group(name="real_serial")
async def test_get_faultlog_real(real_evofw3: Gateway) -> None:
    """Test obtaining the fault log from a real controller via RF."""

    await _test_get_faultlog(real_evofw3, "01:145038")
