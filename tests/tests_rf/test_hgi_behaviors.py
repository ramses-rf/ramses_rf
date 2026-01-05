#!/usr/bin/env python3

# TODO: Remove unittest.mock.patch (use monkeypatch instead of unittest patch)
# TODO: Test with strict address checking

"""RAMSES RF - Check GWY address/type detection and its treatment of addr0."""

import asyncio
from unittest.mock import patch

import pytest

from ramses_rf import Command, Gateway
from ramses_tx import exceptions as exc
from ramses_tx.address import HGI_DEVICE_ID, Address
from ramses_tx.protocol import PortProtocol
from ramses_tx.schemas import DeviceIdT
from ramses_tx.transport import MqttTransport
from ramses_tx.typing import QosParams

from .conftest import _GwyConfigDictT

# patched constants
_DBG_DISABLE_STRICT_CHECKING = True  # #    ramses_tx.address

# other constants
ASSERT_CYCLE_TIME = 0.001  # max_cycles_per_assert = max_sleep / ASSERT_CYCLE_TIME
DEFAULT_MAX_SLEEP = 0.05  # 0.01/0.05 minimum for mocked (virtual RF)/actual


HGI_ID_ = HGI_DEVICE_ID  # the sentinel value
TST_ID_ = Address("18:222222").id  # the id of the test HGI80-compatible device

TEST_CMDS = {  # test command strings (no impersonation)
    10: f"RQ --- {TST_ID_} 63:262142 --:------ 10E0 001 00",
    11: r"RQ --- 18:000730 63:262142 --:------ 10E0 001 00",
    20: f" I --- {TST_ID_} {TST_ID_} --:------ 30C9 003 000222",
    21: f" I --- 18:000730 {TST_ID_} --:------ 30C9 003 000333",
    30: f"RP --- {TST_ID_} 18:000730 --:------ 30C9 003 000444",
    31: r"RP --- 18:000730 18:000730 --:------ 30C9 003 000555",
    40: f" I --- {TST_ID_} --:------ {TST_ID_} 30C9 003 000666",
    41: f" I --- 18:000730 --:------ {TST_ID_} 30C9 003 000777",
    50: f" I --- {TST_ID_} --:------ 18:000730 30C9 003 000888",
    51: r" I --- 18:000730 --:------ 18:000730 30C9 003 000999",
    60: f" I --- --:------ --:------ {TST_ID_} 0008 002 00AA",
    61: r" I --- --:------ --:------ 18:000730 0008 002 00BB",
}
# NOTE: HGI80 will silently discard all frames that have addr0 != 18:000730
TEST_CMDS_FAIL_ON_HGI80 = [k for k, v in TEST_CMDS.items() if v[7:16] == TST_ID_]


# ### FIXTURES #########################################################################

pytestmark = pytest.mark.asyncio()  # scope="module")


@pytest.fixture()
def gwy_config() -> _GwyConfigDictT:
    return {
        "config": {
            "disable_discovery": True,
            "disable_qos": False,  # this is required for this test
            "enforce_known_list": False,
        },
        "known_list": {HGI_DEVICE_ID: {}},  # req'd to thwart foreign HGI blacklisting
    }


@pytest.fixture()
def gwy_dev_id() -> DeviceIdT:
    return TST_ID_


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    metafunc.parametrize("test_idx", TEST_CMDS)


# ### TESTS ############################################################################


@patch(  # DISABLE_STRICT_CHECKING
    "ramses_tx.address._DBG_DISABLE_STRICT_CHECKING",
    _DBG_DISABLE_STRICT_CHECKING,
)
async def _test_gwy_device(gwy: Gateway, test_idx: int) -> None:
    """Check GWY address/type detection, and behaviour of its treatment of addr0."""

    assert gwy._loop is asyncio.get_running_loop()  # scope BUG is here

    if not isinstance(gwy._protocol, PortProtocol) or not gwy._protocol._context:
        assert False, "QoS protocol not enabled"  # use assert, not skip

    assert gwy.hgi  # mypy

    # we replace the (non-sentinel) gwy_id with the real gwy's actual dev_id
    cmd_str = TEST_CMDS[test_idx].replace(TST_ID_, gwy.hgi.id)
    # this is irrevelent for fake (virtual) gwys, as they been assigned this id

    cmd = Command(cmd_str)
    assert str(cmd) == cmd_str  # sanity check

    # a HGI80 (ti4310) will silently discard all frames that have addr0 != 18:000730
    is_hgi80 = not gwy._protocol._is_evofw3  # TODO: is_hgi80?

    assert gwy._transport  # mypy

    # NOTE: timeout values are empirical, and may need to be adjusted
    if isinstance(gwy._transport, MqttTransport):  # MQTT
        timeout = 0.375 * 2  # intesting, fail: 0.370 work: 0.375: 0.75 margin of safety
    elif gwy._transport.get_extra_info("virtual_rf"):  #   # fake
        timeout = 0.010 * 2  # was 0.003 * 2: increased to 20ms for CI stability
    else:  #                                        # real
        timeout = 0.355 * 2  # intesting, fail: 0.350 work: 0.355: 0.71 margin of safety

    try:
        # using gwy._protocol.send_cmd() instead of gwy.async_send_cmd() as the
        # latter may swallow the exception we wish to capture (ProtocolSendFailed)
        pkt = await gwy._protocol.send_cmd(
            cmd, qos=QosParams(wait_for_reply=False, timeout=timeout)
        )  # for this test, we only need the cmd echo
    except exc.ProtocolSendFailed:
        if is_hgi80 and cmd_str[7:16] != HGI_DEVICE_ID:
            return  # should have failed, and has
        raise  # should not have failed, but has!

    assert pkt is not None

    if is_hgi80 and cmd_str[7:16] != HGI_DEVICE_ID:
        assert False, pkt  # should have failed, but has not!

    # NOTE: both HGI80/evofw3 will swap out addr0 (only) for its own device_id

    if cmd_str[7:16] == HGI_DEVICE_ID:
        pkt_str = cmd_str[:7] + gwy.hgi.id + cmd_str[16:]
    else:
        pkt_str = cmd_str

    assert pkt._frame == pkt_str


# ### TESTS ############################################################################


@pytest.mark.xdist_group(name="virt_serial")
async def test_fake_evofw3(fake_evofw3: Gateway, test_idx: int) -> None:
    """Check the behaviour of the fake (virtual) evofw3 against the GWY test."""

    await _test_gwy_device(fake_evofw3, test_idx)


@pytest.mark.xdist_group(name="virt_serial")
async def test_fake_ti3410(fake_ti3410: Gateway, test_idx: int) -> None:
    """Check the behaviour of the fake (virtual) HGI80 against the GWY test."""

    await _test_gwy_device(fake_ti3410, test_idx)


@pytest.mark.xdist_group(name="real_serial")
async def test_mqtt_evofw3(mqtt_evofw3: Gateway, test_idx: int) -> None:
    """Validate the GWY test against an MQTT server."""

    await _test_gwy_device(mqtt_evofw3, test_idx)


@pytest.mark.xdist_group(name="real_serial")
async def test_real_evofw3(real_evofw3: Gateway, test_idx: int) -> None:
    """Validate the GWY test against a real (physical) evofw3."""

    await _test_gwy_device(real_evofw3, test_idx)


@pytest.mark.xdist_group(name="real_serial")
async def test_real_ti3410(real_ti3410: Gateway, test_idx: int) -> None:
    """Validate the GWY test against a real (physical) HGI80."""

    await _test_gwy_device(real_ti3410, test_idx)
