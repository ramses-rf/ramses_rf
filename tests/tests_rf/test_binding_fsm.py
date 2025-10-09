#!/usr/bin/env python3

# TODO: test addenda phase of binding handshake
# TODO: get test working with (and without) disabled QoS

"""TRAMSES RF - Test the binding protocol with a virtual RF.

NB: This test will likely fail with pytest-repeat (pytest -n x); maybe because of
concurrent access to pty.openpty().
"""

import asyncio
from datetime import datetime as dt

import pytest

from ramses_rf import Code, Command, Gateway, Message, Packet
from ramses_rf.binding_fsm import (
    SZ_RESPONDENT,
    SZ_SUPPLICANT,
    BindStateBase,
    _BindStates,
)
from ramses_rf.device import Fakeable
from ramses_tx.protocol import PortProtocol

from .virtual_rf import rf_factory
from .virtual_rf.helpers import ensure_fakeable

# patched constants
DEFAULT_MAX_RETRIES = 0  # #                ramses_tx.protocol
MAINTAIN_STATE_CHAIN = False  # #           ramses_tx.protocol_fsm

# other constants
ASSERT_CYCLE_TIME = 0.0005  # max_cycles_per_assert = max_sleep / ASSERT_CYCLE_TIME
DEFAULT_MAX_SLEEP = 0.1

PKT_FLOW = "packets"

_TENDER = 0
_ACCEPT = 1
_AFFIRM = 2
_RATIFY = 3


GWY_CONFIG = {
    "config": {
        "disable_discovery": True,
        "disable_qos": False,  # this is required for this test
        "enforce_known_list": True,
    }
}

ITHO__ = "itho"
NUAIRE = "nuaire"
ORCON_ = "orcon"

TEST_SUITE_300 = [
    # {  # THM to CTL: FIXME: affirm is I|1FC9|07, not I|1FC9|00
    #     SZ_RESPONDENT: {"01:085545": {"class": "CTL"}},
    #     SZ_SUPPLICANT: {"22:057520": {"class": "THM", "faked": True}},  # THM, not STA
    #     PKT_FLOW: (
    #         " I --- 22:057520 --:------ 22:057520 1FC9 024 00-2309-58E0B0 00-30C9-58E0B0 00-0008-58E0B0 00-1FC9-58E0B0",
    #         " W --- 01:085545 22:057520 --:------ 1FC9 006 07-2309-054E29",
    #         " I --- 22:057520 01:085545 --:------ 1FC9 006 00-2309-58E0B0",
    #     ),
    # },
    # #
    {  # RND to CTL
        SZ_RESPONDENT: {"01:220768": {"class": "CTL"}},
        SZ_SUPPLICANT: {"34:259472": {"class": "RND", "faked": True}},
        PKT_FLOW: (
            " I --- 34:259472 --:------ 34:259472 1FC9 024 00-2309-8BF590 00-30C9-8BF590 00-0008-8BF590 00-1FC9-8BF590",
            " W --- 01:220768 34:259472 --:------ 1FC9 006 01-2309-075E60",
            " I --- 34:259472 01:220768 --:------ 1FC9 006 01-2309-8BF590",
            # I --- 34:259472 63:262142 --:------ 10E0 038 00-0001C8380F01-00-F1FF070B07E6030507E15438375246323032350000000000000000000000",
            # I --- 34:259472 --:------ 34:259472 1060 003 00-FF01",
            # I --- 34:259472 --:------ 34:259472 0005 012 000A0000000F000000100000",
            # I --- 34:259472 --:------ 34:259472 000C 018 000A7FFFFFFF000F7FFFFFFF00107FFFFFFF",
        ),
    },
    #
    {  # CO2 to FAN
        SZ_RESPONDENT: {  # "_note": "Spider HRU"
            "18:126620": {"class": "FAN", "scheme": "itho"},
        },
        SZ_SUPPLICANT: {  # "_note": "Spider CO2"
            "37:154011": {"class": "CO2", "scheme": "itho", "faked": True}
        },
        PKT_FLOW: (
            " I --- 37:154011 --:------ 37:154011 1FC9 030 00-31E0-96599B 00-1298-96599B 00-2E10-96599B 01-10E0-96599B 00-1FC9-96599B",
            " W --- 18:126620 37:154011 --:------ 1FC9 012 00-31D9-49EE9C 00-31DA-49EE9C",
            " I --- 37:154011 18:126620 --:------ 1FC9 001 00",
            " I --- 37:154011 63:262142 --:------ 10E0 038 00-000100280901-01-FEFFFFFFFFFF140107E5564D532D31324333390000000000000000000000",
        ),
    },
    #
    {  # REM to FAN (nuaire)
        SZ_RESPONDENT: {  # "_note": "ECO-HEAT-HC"
            "30:098165": {"class": "FAN", "scheme": "nuaire"},
        },
        SZ_SUPPLICANT: {  # "_note": "4-way switch",
            "32:208628": {"class": "REM", "scheme": "nuaire", "faked": True}
        },
        PKT_FLOW: (
            " I --- 32:208628 --:------ 32:208628 1FC9 018 00-22F1-832EF4 6C-10E0-832EF4 00-1FC9-832EF4",
            " W --- 30:098165 32:208628 --:------ 1FC9 006 21-31DA-797F75",
            " I --- 32:208628 30:098165 --:------ 1FC9 001 21",
            " I --- 32:208628 63:262142 --:------ 10E0 030 00-0001C85A0101-6C-FFFFFFFFFFFF010607E0564D4E2D32334C4D48323300",
            # I --- 32:208628 --:------ 32:208628 1060 003 00-FF01",  # sends x3
        ),
    },
    #
    # {  # REM to FAN (orcon): FIXME: tender dst is 63:262142 (not dst=src)
    #     SZ_RESPONDENT: {  # "_note": "HRC-350"
    #         "32:155617": {"class": "FAN", "scheme": "orcon"},
    #     },
    #     SZ_SUPPLICANT: {  # "_note": "VMN-15LF01"
    #         "29:158183": {"class": "REM", "scheme": "orcon", "faked": True}
    #     },
    #     PKT_FLOW: (
    #         " I --- 29:158183 63:262142 --:------ 1FC9 024 00-22F1-7669E7 00-22F3-7669E7 67-10E0-7669E7 00-1FC9-7669E7",
    #         " W --- 32:155617 29:158183 --:------ 1FC9 012 00-31D9-825FE1 00-31DA-825FE1",
    #         " I --- 29:158183 32:155617 --:------ 1FC9 001 00",
    #         " I --- 29:158183 63:262142 --:------ 10E0 038 00-0001C8270901-67-FFFFFFFFFFFF0D0207E3564D4E2D31354C46303100000000000000000000",
    #         # I --- 29:158183 --:------ 29:158183 1060 003 00-FF01",
    #     ),
    # },
    # #
    {  # DIS to FAN
        SZ_RESPONDENT: {
            "32:155617": {"class": "FAN", "scheme": "orcon"},
        },
        SZ_SUPPLICANT: {
            "37:171871": {"class": "DIS", "faked": True}  # "scheme": "orcon",
        },  # , "scheme": "orcon"}},
        PKT_FLOW: (
            " I --- 37:171871 --:------ 37:171871 1FC9 024 00-22F1-969F5F 00-22F3-969F5F 67-10E0-969F5F 00-1FC9-969F5F",
            " W --- 32:155617 37:171871 --:------ 1FC9 012 00-31D9-825FE1 00-31DA-825FE1",
            " I --- 37:171871 32:155617 --:------ 1FC9 001 00",
            " I --- 37:171871 63:262142 --:------ 10E0 038 00-0001C8940301-67-FFFFFFFFFFFF1B0807E4564D492D313557534A3533000000000000000000",
        ),
    },
    #
    {  # DHW to CTL
        SZ_RESPONDENT: {"01:145038": {"class": "CTL"}},
        SZ_SUPPLICANT: {"07:045960": {"class": "DHW", "faked": True}},
        PKT_FLOW: (
            " I --- 07:045960 --:------ 07:045960 1FC9 012 00-1260-1CB388 00-1FC9-1CB388",
            " W --- 01:145038 07:045960 --:------ 1FC9 006 00-10A0-06368E",
            " I --- 07:045960 01:145038 --:------ 1FC9 006 00-1260-1CB388",
        ),  # TODO: need epilogue packets, if any (1060?)
    },
    #
]
# TEST_SUITE_300 = [TEST_SUITE_300[-2]]

# ### FIXTURES #########################################################################


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    def id_fnc(test_set: dict) -> str:
        r_class = list(test_set[SZ_RESPONDENT].values())[0]["class"]
        s_class = list(test_set[SZ_SUPPLICANT].values())[0]["class"]
        return str(s_class + " binding to " + r_class)

    metafunc.parametrize("test_set", TEST_SUITE_300, ids=id_fnc)


# ######################################################################################


async def assert_context_state(
    device: Fakeable, state: type[BindStateBase], max_sleep: float = DEFAULT_MAX_SLEEP
) -> None:
    assert device._bind_context

    for _ in range(int(max_sleep / ASSERT_CYCLE_TIME)):
        await asyncio.sleep(ASSERT_CYCLE_TIME)
        if isinstance(device._bind_context.state, state):
            break
    assert isinstance(device._bind_context.state, state)


# ### TESTS ############################################################################


# TODO: test addenda phase of binding handshake
async def _test_flow_10x(
    gwy_r: Gateway, gwy_s: Gateway, pkt_flow_expected: list[str]
) -> None:
    """Check the change of state during a binding at context layer."""

    # asyncio.create_task() should be OK (no need to pass in an event loop)

    # STEP 0: Setup...
    respondent = gwy_r.devices[0]
    supplicant = gwy_s.devices[0]
    ensure_fakeable(respondent)

    assert isinstance(respondent, Fakeable)  # mypy
    assert isinstance(supplicant, Fakeable)  # mypy

    await assert_context_state(respondent, _BindStates.IS_IDLE_DEVICE)
    await assert_context_state(supplicant, _BindStates.IS_IDLE_DEVICE)

    assert respondent._bind_context
    assert supplicant._bind_context

    assert not respondent._bind_context.is_binding
    assert not supplicant._bind_context.is_binding

    #
    # Step R0: Respondent initial state
    respondent._bind_context.set_state(_BindStates.NEEDING_TENDER)
    await assert_context_state(respondent, _BindStates.NEEDING_TENDER)
    assert respondent._bind_context.is_binding

    #
    # Step S0: Supplicant initial state
    supplicant._bind_context.set_state(_BindStates.NEEDING_ACCEPT)  # type: ignore[unreachable]
    await assert_context_state(supplicant, _BindStates.NEEDING_ACCEPT)
    assert supplicant._bind_context.is_binding

    #
    # Step R1: Respondent expects an Offer
    resp_task = asyncio.create_task(respondent._bind_context._wait_for_offer())

    #
    # Step S1: Supplicant sends an Offer (makes Offer) and expects an Accept
    msg = Message(Packet(dt.now(), "000 " + pkt_flow_expected[_TENDER]))
    codes = [b[1] for b in msg.payload["bindings"] if b[1] != Code._1FC9]

    pkt = await supplicant._bind_context._make_offer(codes)
    await assert_context_state(supplicant, _BindStates.NEEDING_ACCEPT)
    assert pkt is not None

    await resp_task
    await assert_context_state(respondent, _BindStates.NEEDING_AFFIRM)

    if not isinstance(gwy_r._protocol, PortProtocol) or not gwy_r._protocol._context:
        assert False, "QoS protocol not enabled"  # use assert, not skip

    tender = resp_task.result()
    assert tender._pkt == pkt, "Resp's Msg doesn't match Supp's Offer cmd"

    supp_task = asyncio.create_task(supplicant._bind_context._wait_for_accept(tender))

    #
    # Step R2: Respondent expects a Confirm after sending an Accept (accepts Offer)
    msg = Message(Packet(dt.now(), "000 " + pkt_flow_expected[_ACCEPT]))
    codes = [b[1] for b in msg.payload["bindings"]]

    pkt = await respondent._bind_context._accept_offer(tender, codes)
    await assert_context_state(respondent, _BindStates.NEEDING_AFFIRM)
    assert pkt is not None

    await supp_task
    await assert_context_state(supplicant, _BindStates.TO_SEND_AFFIRM)

    accept = supp_task.result()
    assert accept._pkt == pkt, "Supp's Msg doesn't match Resp's Accept cmd"

    resp_task = asyncio.create_task(respondent._bind_context._wait_for_confirm(accept))

    #
    # Step S2: Supplicant sends a Confirm (confirms Accept)
    msg = Message(Packet(dt.now(), "000 " + pkt_flow_expected[_AFFIRM]))
    codes = [b[1] for b in msg.payload["bindings"] if len(b) > 1]

    pkt = await supplicant._bind_context._confirm_accept(accept, confirm_code=codes)
    await assert_context_state(supplicant, _BindStates.HAS_BOUND_SUPP)
    assert pkt is not None

    if len(pkt_flow_expected) > _RATIFY:  # FIXME
        supplicant._bind_context.set_state(
            _BindStates.TO_SEND_RATIFY
        )  # HACK: easiest way

    await resp_task
    await assert_context_state(respondent, _BindStates.HAS_BOUND_RESP)

    if len(pkt_flow_expected) > _RATIFY:  # FIXME
        respondent._bind_context.set_state(
            _BindStates.NEEDING_RATIFY
        )  # HACK: easiest way

    affirm = resp_task.result()
    assert affirm._pkt == pkt, "Resp's Msg doesn't match Supp's Confirm cmd"

    #
    # Some bindings don't include an Addenda...
    if len(pkt_flow_expected) <= _RATIFY:  # i.e. no addenda  FIXME
        return

    await assert_context_state(respondent, _BindStates.NEEDING_RATIFY)
    await assert_context_state(supplicant, _BindStates.TO_SEND_RATIFY)

    # # Step R3: Respondent expects an Addenda (optional)
    # resp_task = asyncio.create_task(
    #     respondent._context._wait_for_addenda(accept, timeout=0.05)
    # )

    # # Step S3: Supplicant sends an Addenda (optional)
    # msg = Message(Packet(dt.now(), "000 " + pkt_flow_expected[_RATIFY]))
    # old code:
    # supplicant._msgz[msg.code] = {msg.verb: {msg._pkt._ctx: msg}}
    # now: only supplicant ?! explains a lot of failures
    # gwy_r.msg_db.add(msg)  # (for supplicant only?) >> remove from respondent/filter while adding
    # pkt = await supplicant._context._cast_addenda()
    # await assert_context_state(supplicant, _BindStates.HAS_BOUND_SUPP)
    # assert pkt is not None

    # await assert_context_state(respondent, _BindStates.HAS_BOUND_RESP)
    # await resp_task

    # ratify = resp_task.result()
    # assert ratify._pkt == pkt, "Resp's Msg doesn't match Supp's Addenda cmd"


# TODO: test addenda phase of binding handshake
async def _test_flow_20x(
    gwy_r: Gateway, gwy_s: Gateway, pkt_flow_expected: list[str]
) -> None:
    """Check the change of state during a binding at device layer."""

    # STEP 0: Setup...
    respondent = gwy_r.devices[0]
    supplicant = gwy_s.devices[0]
    ensure_fakeable(respondent)

    assert isinstance(respondent, Fakeable)  # mypy
    assert isinstance(supplicant, Fakeable)  # mypy

    assert respondent.id == pkt_flow_expected[_ACCEPT][7:16], "bad test suite config"
    assert supplicant.id == pkt_flow_expected[_TENDER][7:16], "bad test suite config"

    # Step R1: Respondent expects an Offer
    payload = pkt_flow_expected[_ACCEPT][46:]
    accept_codes = [payload[i : i + 4] for i in range(2, len(payload), 12)]

    idx = payload[:2]
    require_ratify = len(pkt_flow_expected) > _RATIFY

    resp_coro = respondent._wait_for_binding_request(
        accept_codes, idx=idx, require_ratify=require_ratify
    )
    resp_task = asyncio.create_task(resp_coro)

    # Step S1: Supplicant sends an Offer (makes Offer) and expects an Accept
    payload = pkt_flow_expected[_TENDER][46:]
    offer_codes = [payload[i : i + 4] for i in range(2, len(payload), 12)]
    offer_codes = [c for c in offer_codes if c != Code._1FC9]

    confirm_code = pkt_flow_expected[_AFFIRM][48:52] or None
    if len(pkt_flow_expected) > _RATIFY:
        ratify_cmd = Command(pkt_flow_expected[_RATIFY])
    else:
        ratify_cmd = None

    supp_coro = supplicant._initiate_binding_process(
        offer_codes, confirm_code=confirm_code, ratify_cmd=ratify_cmd
    )
    supp_task = asyncio.create_task(supp_coro)

    # Step 2: Wait until flow is completed (or timeout)
    await asyncio.gather(resp_task, supp_task)

    resp_flow = resp_task.result()
    supp_flow = supp_task.result()

    for i in range(len(pkt_flow_expected)):
        assert resp_flow[i] == supp_flow[i]
        assert resp_flow[i] == Command(pkt_flow_expected[i])

        assert str(resp_flow[i]) == str(supp_flow[i])
        assert str(resp_flow[i]) == pkt_flow_expected[i]


# TODO: binding working without QoS  # @patch("ramses_tx.protocol._DBG_DISABLE_QOS", True)
@pytest.mark.xdist_group(name="virt_serial")
async def test_flow_100(test_set: dict[str, dict]) -> None:
    """Check packet flow / state change of a binding at context layer."""

    config = {}
    for role in (SZ_RESPONDENT, SZ_SUPPLICANT):
        devices = [d for d in test_set.values() if isinstance(d, dict)]
        config[role] = GWY_CONFIG | {
            "known_list": {k: v for d in devices for k, v in d.items()},
            "orphans_hvac": list(test_set[role]),  # TODO: used by Heat domain too!
        }

    pkt_flow = [
        x[:46] + x[46:].replace(" ", "").replace("-", "")
        for x in test_set.get(PKT_FLOW, [])
    ]

    # can't use fixture for this, as new schema required for every test
    rf, gwys = await rf_factory([config[SZ_RESPONDENT], config[SZ_SUPPLICANT]])

    try:
        await _test_flow_10x(gwys[0], gwys[1], pkt_flow)
    finally:
        for gwy in gwys:
            await gwy.stop()
        await rf.stop()


# TODO: binding working without QoS  # @patch("ramses_tx.protocol._DBG_DISABLE_QOS", True)
@pytest.mark.xdist_group(name="virt_serial")
async def test_flow_200(test_set: dict[str, dict]) -> None:
    """Check packet flow / state change of a binding at device layer."""

    config = {}
    for role in (SZ_RESPONDENT, SZ_SUPPLICANT):
        devices = [d for d in test_set.values() if isinstance(d, dict)]
        config[role] = GWY_CONFIG | {
            "known_list": {k: v for d in devices for k, v in d.items()},
            "orphans_hvac": list(test_set[role]),  # TODO: used by Heat domain too!
        }

    pkt_flow = [
        x[:46] + x[46:].replace(" ", "").replace("-", "")
        for x in test_set.get(PKT_FLOW, [])
    ]

    # can't use fixture for this, as new schema required for every test
    rf, gwys = await rf_factory(
        [config[SZ_RESPONDENT], config[SZ_SUPPLICANT]]
    )  # can pop orphans_hvac

    try:
        await _test_flow_20x(gwys[0], gwys[1], pkt_flow)
    finally:
        for gwy in gwys:
            await gwy.stop()
        await rf.stop()
