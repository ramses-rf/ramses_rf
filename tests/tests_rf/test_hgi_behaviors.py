#!/usr/bin/env python3

"""RAMSES RF - Check GWY address/type detection and its treatment of addr0."""

import asyncio
from typing import Any

import pytest

from ramses_rf import CommandDTO as Command, Gateway
from ramses_rf.gateway import GatewayConfig
from ramses_tx import exceptions as exc
from ramses_tx.address import HGI_DEVICE_ID, Address
from ramses_tx.protocol import PortProtocol
from ramses_tx.transport.mqtt import MqttTransport
from ramses_tx.typing import DeviceIdT, QosParams

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

pytestmark = pytest.mark.asyncio()


@pytest.fixture(autouse=True)
def patch_strict_checking(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply the strict checking monkeypatch globally to all tests in this module."""
    # Retain the legacy transport patch
    monkeypatch.setattr(
        "ramses_tx.address._DBG_DISABLE_STRICT_CHECKING",
        _DBG_DISABLE_STRICT_CHECKING,
    )
    # Target the new OSI L7 boundary where the logic actually lives
    monkeypatch.setattr(
        "ramses_rf.address._DBG_DISABLE_STRICT_CHECKING",
        _DBG_DISABLE_STRICT_CHECKING,
    )


@pytest.fixture()
def gwy_config() -> dict[str, Any]:
    return {
        "config": GatewayConfig(
            disable_discovery=True,
        ),
        "disable_qos": False,  # this is required for this test
        "enforce_known_list": False,
        "known_list": {HGI_DEVICE_ID: {}},  # req'd to thwart foreign HGI blacklisting
    }


@pytest.fixture()
def gwy_dev_id() -> DeviceIdT:
    return TST_ID_


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    metafunc.parametrize("test_idx", TEST_CMDS)


# ### CORE TEST LOGIC ##################################################################


async def _test_gwy_device(gwy: Gateway, test_idx: int) -> None:
    """Check GWY address/type detection, and behaviour of its treatment of addr0."""

    assert gwy._engine._loop is asyncio.get_running_loop()

    if (
        not isinstance(gwy._engine._protocol, PortProtocol)
        or not gwy._engine._protocol._context
    ):
        assert False, "QoS protocol not enabled"

    assert gwy.hgi  # mypy

    # we replace the (non-sentinel) gwy_id with the real gwy's actual dev_id
    cmd_str = TEST_CMDS[test_idx].replace(TST_ID_, gwy.hgi.id)
    # this is irrevelent for fake (virtual) gwys, as they been assigned this id

    cmd = Command.from_cli(cmd_str)
    assert str(cmd) == cmd_str  # sanity check

    # a HGI80 (ti4310) will silently discard all frames that have addr0 != 18:000730
    is_hgi80 = not gwy._engine._protocol._is_evofw3  # TODO: is_hgi80?

    assert gwy._engine._transport  # mypy

    # Sane timeout safety margins for saturated CI/CD event loops
    if isinstance(gwy._engine._transport, MqttTransport):
        timeout = 2.0
    elif gwy._engine._transport.get_extra_info("virtual_rf"):
        timeout = 1.0  # Virtual RF: Generous 1s threshold to prevent CI flakiness
    else:
        timeout = 2.0  # Real physical serial connection

    try:
        # using gwy._engine._protocol.send_cmd() instead of gwy.async_send_cmd() as the
        # latter may swallow the exception we wish to capture (ProtocolSendFailed)
        pkt = await gwy._engine._protocol.send_cmd(
            cmd, qos=QosParams(timeout=timeout)
        )  # for this test, we only need the cmd echo
    except exc.ProtocolSendFailed:
        if is_hgi80 and cmd_str[7:16] != HGI_DEVICE_ID:
            return  # should have failed, and has
        raise  # should not have failed, but has!

    assert pkt is not None

    if is_hgi80 and cmd_str[7:16] != HGI_DEVICE_ID:
        assert False, pkt  # should have failed, but has not!

    # NOTE: both HGI80/evofw3 will swap out addr0 (only) for its own device_id
    # evofw3 will also patch src if it is the HGI placeholder (incl. --:------
    # which Command normalizes to 18:000730 when addr0 is 18:000730)
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
