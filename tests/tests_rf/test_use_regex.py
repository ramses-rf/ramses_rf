#!/usr/bin/env python3
"""RAMSES RF - Test the use_regex feature."""

import asyncio
from datetime import datetime as dt

import pytest
import serial  # type: ignore[import-untyped]

from ramses_rf import Command, Gateway, Packet
from ramses_tx.protocol import PortProtocol
from ramses_tx.schemas import SZ_INBOUND, SZ_OUTBOUND, SZ_USE_REGEX
from ramses_tx.transport import _str
from tests_rf.virtual_rf import VirtualRf

# other constants
ASSERT_CYCLE_TIME = 0.0005  # max_cycles_per_assert = max_sleep / ASSERT_CYCLE_TIME
DEFAULT_MAX_SLEEP = 0.1


RULES_INBOUND = {
    "63:262143": "04:262143",
    "(W.*) 1FC9 (...) 21": "\\g<1> 1FC9 \\g<2> 00",
    "--:------ --:------ 12:215819": "01:215819 --:------ 01:215819",
    "000C 006 02(04|08)00FFFFFF": "000C 006 02\\g<1>0013FFFF",
}

RULES_OUTBOUND = {
    "04:262143": "63:262143",
    "(W.*) 1FC9 (...) 00": "\\g<1> 1FC9 \\g<2> 21",
    "01:215819 --:------ 01:215819": "--:------ --:------ 12:215819",
}

RULES_COMBINED = {SZ_INBOUND: RULES_INBOUND, SZ_OUTBOUND: RULES_OUTBOUND}

TESTS_OUTBOUND = {  # sent, received by other
    " I 003 01:215819 --:------ 01:215819 0009 003 0000FF": " I 003 --:------ --:------ 12:215819 0009 003 0000FF",
    " I --- 04:262143 --:------ 04:262143 30C9 003 000713": " I --- 63:262143 --:------ 63:262143 30C9 003 000713",
    " I --- 04:262143 --:------ 01:182924 2309 003 0205DC": " I --- 63:262143 --:------ 01:182924 2309 003 0205DC",
    # NOTE: the below doesn't work with QoS, as expects a response pkt (would be an I)
    " W --- 30:098165 32:206251 --:------ 1FC9 006 0031DA797F75": " W --- 30:098165 32:206251 --:------ 1FC9 006 2131DA797F75",
}

TESTS_INBOUND = {  # sent by other, received
    " I 003 --:------ --:------ 12:215819 0009 003 0000FF": " I 003 01:215819 --:------ 01:215819 0009 003 0000FF",
    " I --- 63:262143 --:------ 63:262143 30C9 003 000713": " I --- 04:262143 --:------ 04:262143 30C9 003 000713",
    " I --- 63:262143 --:------ 01:182924 2309 003 0205DC": " I --- 04:262143 --:------ 01:182924 2309 003 0205DC",
    # NOTE: the below won't work with QoS - why? - still the case with refactored QoS
    " W --- 30:098165 32:206251 --:------ 1FC9 006 2131DA797F75": " W --- 30:098165 32:206251 --:------ 1FC9 006 0031DA797F75",
    "RP --- 01:182924 30:068640 --:------ 000C 006 020400FFFFFF": "RP --- 01:182924 30:068640 --:------ 000C 006 02040013FFFF",
    "RP --- 01:182924 30:068640 --:------ 000C 006 020800FFFFFF": "RP --- 01:182924 30:068640 --:------ 000C 006 02080013FFFF",
}

GWY_CONFIG = {
    "config": {
        "disable_discovery": True,
        "disable_qos": False,  # QoS is required for this test
        "enforce_known_list": False,
    }
}


# ### FIXTURES #########################################################################


@pytest.fixture(autouse=True)
def patches_for_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ramses_tx.protocol._DBG_DISABLE_IMPERSONATION_ALERTS", True)
    monkeypatch.setattr("ramses_tx.transport.MIN_INTER_WRITE_GAP", 0)


async def assert_this_pkt(
    gwy: Gateway, expected: Command, max_sleep: float = DEFAULT_MAX_SLEEP
) -> None:
    """Check, at the gateway layer, that the current packet is as expected."""
    for _ in range(int(max_sleep / ASSERT_CYCLE_TIME)):
        await asyncio.sleep(ASSERT_CYCLE_TIME)
        if gwy._this_msg and gwy._this_msg._pkt._frame == expected._frame:
            break
    assert gwy._this_msg and gwy._this_msg._pkt._frame == expected._frame


# ### TESTS ############################################################################


@pytest.mark.xdist_group(name="virt_serial")
async def test_regex_inbound_() -> None:
    """Check the inbound filters work as expected (this test works without QoS)."""

    rf = VirtualRf(2)

    # NOTE: the absence of reciprocal outbound tests is intentional
    config = GWY_CONFIG
    config["config"].update({SZ_USE_REGEX: {SZ_INBOUND: RULES_INBOUND}})  # type: ignore[dict-item]

    gwy_0 = Gateway(rf.ports[0], **config)
    ser_1 = serial.Serial(rf.ports[1])

    try:
        await gwy_0.start()
        assert gwy_0._protocol._transport

        for cmd, pkt in TESTS_INBOUND.items():
            ser_1.write(bytes(cmd.encode("ascii")) + b"\r\n")

            await assert_this_pkt(gwy_0, Command(pkt))

    finally:
        await gwy_0.stop()
        await rf.stop()


@pytest.mark.xdist_group(name="virt_serial")
async def test_regex_with_qos() -> None:
    """Check the in/outbound regex filters work as expected with QoS."""

    rf = VirtualRf(2)

    config = GWY_CONFIG
    config["config"].update({SZ_USE_REGEX: RULES_COMBINED})  # type: ignore[dict-item]

    gwy_0 = Gateway(rf.ports[0], **config)
    ser_1 = serial.Serial(rf.ports[1])

    if not isinstance(gwy_0._protocol, PortProtocol) or not gwy_0._protocol._context:
        await rf.stop()
        pytest.skip("QoS protocol not enabled")
    else:
        assert gwy_0._protocol._disable_qos is False  # needed for successful tests

    try:
        await gwy_0.start()
        assert gwy_0._protocol._transport

        _ = ser_1.read(ser_1.in_waiting)  # ser_1.flush() doesn't work?

        for before, after in TESTS_OUTBOUND.items():
            cmd = Command(before)
            if cmd.rx_header:  # we won't be getting any replies
                continue

            pkt_src = await gwy_0.async_send_cmd(cmd)  # , timeout=DEFAULT_WAIT_TIMEOUT)
            assert str(pkt_src) == before

            pkt_dst = Packet.from_port(dt.now(), _str(ser_1.read(ser_1.in_waiting)))
            assert str(pkt_dst) == after

        for before, after in TESTS_INBOUND.items():
            ser_1.write(bytes(before.encode("ascii")) + b"\r\n")
            await assert_this_pkt(gwy_0, Command(after))

    finally:
        await gwy_0.stop()
        await rf.stop()
