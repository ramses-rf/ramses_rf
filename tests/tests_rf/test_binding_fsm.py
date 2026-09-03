#!/usr/bin/env python3

# TODO: test addenda phase of binding handshake
# TODO: get test working with (and without) disabled QoS

"""RAMSES RF - Test the binding protocol with a virtual RF.

NB: This test will likely fail with pytest-repeat (pytest -n x); maybe because of
concurrent access to pty.openpty().
"""

import asyncio
from collections.abc import Generator
from datetime import UTC, datetime as dt
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ramses_rf import Code, Gateway, Message, Packet
from ramses_rf.binding_fsm import (
    SZ_RESPONDENT,
    SZ_SUPPLICANT,
    BindingManager,
    BindPhase,
    BindStateBase,
    _BindStates,
)
from ramses_rf.commands.builders import build_dto
from ramses_rf.commands.core import Command
from ramses_rf.const import SZ_OEM_CODE, SZ_PHASE
from ramses_rf.devices import Fakeable
from ramses_rf.enums import Action
from ramses_rf.gateway import GatewayConfig
from ramses_tx import Address, IndexT, Verb
from ramses_tx.dtos import CommandDTO
from ramses_tx.protocol import PortProtocol

from .virtual_rf import rf_factory

# patched constants
DEFAULT_MAX_RETRIES = 0  # #                ramses_tx.protocol
MAINTAIN_STATE_CHAIN = False  # #           ramses_tx.protocol_fsm

# other constants
ASSERT_CYCLE_TIME = (
    0.0005  # max_cycles_per_assert = max_sleep / ASSERT_CYCLE_TIME
)
DEFAULT_MAX_SLEEP = 0.1

PKT_FLOW = "packets"

_TENDER = 0
_ACCEPT = 1
_AFFIRM = 2
_RATIFY = 3

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
    {  # CO2 to FAN (orcon)
        SZ_RESPONDENT: {
            "32:134044": {"class": "FAN", "scheme": "orcon"},
        },
        SZ_SUPPLICANT: {
            "29:150156": {"class": "CO2", "scheme": "orcon", "faked": True}
        },
        PKT_FLOW: (
            " I --- 29:150156 --:------ 29:150156 1FC9 030 00-31E0-764A8C 01-31E0-764A8C 00-1298-764A8C 67-10E0-764A8C 00-1FC9-764A8C",
            " W --- 32:134044 29:150156 --:------ 1FC9 012 00-31D9-820B9C 00-31DA-820B9C",
            " I --- 29:150156 32:134044 --:------ 1FC9 001 00",
            " I --- 29:150156 63:262142 --:------ 10E0 038 00-0001C8500B01-67-FEFFFFFFFFFF090307E1564D532D31354331360000000000000000000000",
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


@pytest.fixture(autouse=True)
def patch_port_transport_delays() -> Generator[None, None, None]:
    """Bypass the real-world signature timeouts and duty cycle limits for tests."""
    with (
        patch("ramses_tx.transport.port._DBG_DISABLE_DUTY_CYCLE_LIMIT", True),
        patch("ramses_tx.transport.port._SIGNATURE_MAX_TRYS", 0),
    ):
        yield


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Parametrize tests dynamically based on TEST_SUITE_300."""
    if "test_set" not in metafunc.fixturenames:
        return

    def id_fnc(test_set: dict[str, Any]) -> str:
        """Generate a test ID based on the test set."""
        r_class = list(test_set[SZ_RESPONDENT].values())[0]["class"]
        s_class = list(test_set[SZ_SUPPLICANT].values())[0]["class"]
        return str(s_class + " binding to " + r_class)

    metafunc.parametrize("test_set", TEST_SUITE_300, ids=id_fnc)


# ######################################################################################


def _build_gateway_config(
    test_set: dict[str, Any], role: str
) -> dict[str, Any]:
    """Build the gateway configuration for a specific role.

    Strips the 'faked' trait from the known_list to prevent DeviceNotFaked
    exceptions during gateway initialization or message dispatch.

    :param test_set: The test configuration data set.
    :param role: The specific role (SZ_RESPONDENT or SZ_SUPPLICANT).
    :return: A dictionary representing the Gateway configuration.
    """
    devices = [d for d in test_set.values() if isinstance(d, dict)]

    known_list: dict[str, Any] = {"18:000000": {"class": "HGI"}}
    for d in devices:
        for k, v in d.items():
            known_list[k] = {
                key: val for key, val in v.items() if key != "faked"
            }

    return {
        "config": GatewayConfig(disable_discovery=True),
        "disable_qos": False,
        "enforce_known_list": True,
        "known_list": known_list,
        "schema": {
            "orphans_hvac": list(test_set[role])
        },  # TODO: used by Heat domain too!
    }


def _setup_fakeable_device(gwy: Gateway, device_id: str) -> Fakeable:
    """Dynamically convert a gateway device into a Fakeable one for testing."""
    dev = gwy.device_registry.device_by_id[cast(Any, device_id)]

    class _Fakeable(dev.__class__, Fakeable):  # type: ignore[misc, name-defined]
        pass

    if not isinstance(dev, Fakeable | _Fakeable):
        dev.__class__ = _Fakeable

    fake_dev = cast(Fakeable, dev)

    if not getattr(fake_dev, "_binding_manager", None):
        setattr(  # noqa: B010
            fake_dev,
            "_binding_manager",
            BindingManager(fake_dev, gwy.dispatcher),
        )

    fake_dev._make_fake()
    return fake_dev


async def assert_context_state(
    device: Fakeable,
    state: type[BindStateBase],
    max_sleep: float = DEFAULT_MAX_SLEEP,
) -> None:
    """Assert that the device's context state transitions correctly within max_sleep."""
    assert device._binding_manager

    for _ in range(int(max_sleep / ASSERT_CYCLE_TIME)):
        await asyncio.sleep(ASSERT_CYCLE_TIME)
        if isinstance(device._binding_manager.state, state):
            break
    assert isinstance(device._binding_manager.state, state)


# ### TESTS ############################################################################


def _assert_l7_parity(
    msg: Message, cmd: Packet | CommandDTO | Message
) -> None:
    """Assert that a received L7 Message matches a transmitted L3/L4 Packet."""
    assert msg.verb == cmd.verb
    assert str(msg.code) == str(cmd.code)
    if isinstance(cmd, Message):
        assert msg.src.id == cmd.src.id
        assert msg.dst.id == cmd.dst.id
        assert msg.payload == cmd.payload
    elif isinstance(cmd, CommandDTO):
        assert cmd.addr1 in (msg.src.id, msg.dst.id, "--:------")
        assert cmd.addr2 in (msg.src.id, msg.dst.id, "--:------")
        assert msg._dto.payload == cmd.payload
    else:
        assert msg.src.id == cmd.src.id
        assert msg.dst.id == cmd.dst.id
        assert msg._dto.payload == cmd.payload


# TODO: test addenda phase of binding handshake
async def _test_flow_10x(
    gwy_r: Gateway,
    gwy_s: Gateway,
    test_set: dict[str, Any],
    packet_flow_expected: list[str],
) -> None:
    """Check the change of state during a binding at context layer."""

    # asyncio.create_task() should be OK (no need to pass in an event loop)

    # STEP 0: Setup...
    resp_id = list(test_set[SZ_RESPONDENT].keys())[0]
    supp_id = list(test_set[SZ_SUPPLICANT].keys())[0]

    respondent = _setup_fakeable_device(gwy_r, resp_id)
    supplicant = _setup_fakeable_device(gwy_s, supp_id)

    await assert_context_state(respondent, _BindStates.IS_IDLE_DEVICE)
    await assert_context_state(supplicant, _BindStates.IS_IDLE_DEVICE)

    # Local assignments let Mypy lock in the narrowed types reliably across awaits
    resp_bm = respondent._binding_manager
    supp_bm = supplicant._binding_manager
    assert resp_bm is not None
    assert supp_bm is not None

    async with asyncio.TaskGroup() as tg:
        #
        # Step R0: Respondent initial state
        resp_bm.set_state(_BindStates.NEEDING_TENDER)
        await assert_context_state(respondent, _BindStates.NEEDING_TENDER)

        #
        # Step S0: Supplicant initial state
        supp_bm.set_state(_BindStates.NEEDING_ACCEPT)
        await assert_context_state(supplicant, _BindStates.NEEDING_ACCEPT)

        #
        # Step R1: Respondent expects an Offer
        resp_task = tg.create_task(resp_bm._wait_for_offer())

        #
        # Step S1: Supplicant sends an Offer (makes Offer) and expects an Accept
        packet_str = "000 " + packet_flow_expected[_TENDER]
        msg = Message(Packet(dt.now(tz=UTC), packet_str).to_dto())
        codes = [b[1] for b in msg.payload["bindings"] if b[1] != Code._1FC9]

        packet = await supp_bm._make_offer(codes)
        await assert_context_state(supplicant, _BindStates.NEEDING_ACCEPT)
        assert packet is not None

        await resp_task
        await assert_context_state(respondent, _BindStates.NEEDING_AFFIRM)

        if (
            not isinstance(gwy_r._engine._protocol, PortProtocol)
            or getattr(gwy_r._engine._protocol, "_context", None) is None
        ):
            pytest.skip("QoS protocol not enabled")

        tender = resp_task.result()
        _assert_l7_parity(tender, packet)

        supp_task = tg.create_task(supp_bm._wait_for_accept(tender))

        #
        # Step R2: Respondent expects a Confirm after sending an Accept (accepts Offer)
        packet_str = "000 " + packet_flow_expected[_ACCEPT]
        msg = Message(Packet(dt.now(tz=UTC), packet_str).to_dto())
        codes = [b[1] for b in msg.payload["bindings"]]

        packet = await resp_bm._accept_offer(tender, codes)
        await assert_context_state(respondent, _BindStates.NEEDING_AFFIRM)
        assert packet is not None

        await supp_task
        await assert_context_state(supplicant, _BindStates.TO_SEND_AFFIRM)

        accept = supp_task.result()
        _assert_l7_parity(accept, packet)

        resp_task = tg.create_task(resp_bm._wait_for_confirm(accept))

        #
        # Step S2: Supplicant sends a Confirm (confirms Accept)
        packet_str = "000 " + packet_flow_expected[_AFFIRM]
        msg = Message(Packet(dt.now(tz=UTC), packet_str).to_dto())
        codes = [b[1] for b in msg.payload["bindings"] if len(b) > 1]

        packet = await supp_bm._confirm_accept(accept, confirm_code=codes)
        await assert_context_state(supplicant, _BindStates.HAS_BOUND_SUPP)
        assert packet is not None

        if len(packet_flow_expected) > _RATIFY:  # FIXME
            supp_bm.set_state(_BindStates.TO_SEND_RATIFY)  # HACK: easiest way

        await resp_task
        await assert_context_state(respondent, _BindStates.HAS_BOUND_RESP)

        if len(packet_flow_expected) > _RATIFY:  # FIXME
            resp_bm.set_state(_BindStates.NEEDING_RATIFY)  # HACK: easiest way

        affirm = resp_task.result()
        _assert_l7_parity(affirm, packet)

        #
        # Some bindings don't include an Addenda...
        if len(packet_flow_expected) <= _RATIFY:  # i.e. no addenda  FIXME
            return

        await assert_context_state(respondent, _BindStates.NEEDING_RATIFY)
        await assert_context_state(supplicant, _BindStates.TO_SEND_RATIFY)


# TODO: test addenda phase of binding handshake
async def _test_flow_20x(
    gwy_r: Gateway,
    gwy_s: Gateway,
    test_set: dict[str, Any],
    packet_flow_expected: list[str],
) -> None:
    """Check the change of state during a binding at device layer."""

    # STEP 0: Setup...
    resp_id = list(test_set[SZ_RESPONDENT].keys())[0]
    supp_id = list(test_set[SZ_SUPPLICANT].keys())[0]

    respondent = _setup_fakeable_device(gwy_r, resp_id)
    supplicant = _setup_fakeable_device(gwy_s, supp_id)

    assert respondent.id == packet_flow_expected[_ACCEPT][7:16], (
        "bad test suite config"
    )
    assert supplicant.id == packet_flow_expected[_TENDER][7:16], (
        "bad test suite config"
    )

    # Step R1: Respondent expects an Offer
    payload = packet_flow_expected[_ACCEPT][46:]
    accept_codes = [payload[i : i + 4] for i in range(2, len(payload), 12)]

    index = payload[:2]
    require_ratify = len(packet_flow_expected) > _RATIFY

    resp_coro = respondent._wait_for_binding_request(
        accept_codes, zone_index=index, require_ratify=require_ratify
    )

    # Step S1: Supplicant sends an Offer (makes Offer) and expects an Accept
    payload = packet_flow_expected[_TENDER][46:]
    offer_codes = [
        (cast(IndexT, payload[i : i + 2]), Code(payload[i + 2 : i + 6]))
        for i in range(0, len(payload), 12)
        if payload[i + 2 : i + 6] != Code._1FC9
    ]

    confirm_code = packet_flow_expected[_AFFIRM][48:52] or None
    if len(packet_flow_expected) > _RATIFY:
        raw = packet_flow_expected[_RATIFY]
        clean_raw = raw[:46] + raw[46:].replace("-", "")
        ratify_cmd = CommandDTO.from_cli(clean_raw)
    else:
        ratify_cmd = None

    supp_coro = supplicant._initiate_binding_process(
        offer_codes, confirm_code=confirm_code, ratify_command=ratify_cmd
    )

    # Step 2: Wait until flow is completed (or timeout)
    async with asyncio.TaskGroup() as tg:
        resp_task = tg.create_task(resp_coro)
        supp_task = tg.create_task(supp_coro)

    resp_flow = resp_task.result()
    supp_flow = supp_task.result()

    for i in range(len(packet_flow_expected)):
        # We only want to remove hyphens in the payload, which is from character 46 onwards
        raw = packet_flow_expected[i]
        clean_raw = raw[:46] + raw[46:].replace("-", "")
        expected_cmd = CommandDTO.from_cli(clean_raw)

        r_obj = cast(Any, resp_flow[i])
        if isinstance(r_obj, Message):
            _assert_l7_parity(r_obj, expected_cmd)
        else:
            assert str(r_obj) == packet_flow_expected[i]

        s_obj = cast(Any, supp_flow[i])
        if isinstance(s_obj, Message):
            _assert_l7_parity(s_obj, expected_cmd)
        else:
            assert str(s_obj) == packet_flow_expected[i]


@pytest.mark.xdist_group(name="virt_serial")
async def test_flow_100(test_set: dict[str, Any]) -> None:
    """Check packet flow / state change of a binding at context layer."""

    config = {
        role: _build_gateway_config(test_set, role)
        for role in (SZ_RESPONDENT, SZ_SUPPLICANT)
    }

    packet_flow = [
        x[:46] + x[46:].replace(" ", "").replace("-", "")
        for x in test_set.get(PKT_FLOW, [])
    ]

    # can't use fixture for this, as new schema required for every test
    rf, gwys = await rf_factory([config[SZ_RESPONDENT], config[SZ_SUPPLICANT]])

    try:
        await _test_flow_10x(gwys[0], gwys[1], test_set, packet_flow)
    finally:
        # Cancel any pending binding FSM timers before stopping gateways.
        for gwy in gwys:
            for dev in gwy.device_registry.device_by_id.values():
                bm = getattr(dev, "_binding_manager", None)
                if bm:
                    bm.cancel()
        for gwy in gwys:
            await gwy.stop()
        await rf.stop()
        # Yield to the event loop so cancelled TimerHandles are processed
        # before pytest's verify_cleanup fixture checks for lingering timers.
        await asyncio.sleep(0)


@pytest.mark.xdist_group(name="virt_serial")
async def test_flow_200(test_set: dict[str, Any]) -> None:
    """Check packet flow / state change of a binding at device layer."""

    config = {
        role: _build_gateway_config(test_set, role)
        for role in (SZ_RESPONDENT, SZ_SUPPLICANT)
    }

    packet_flow = [
        x[:46] + x[46:].replace(" ", "").replace("-", "")
        for x in test_set.get(PKT_FLOW, [])
    ]

    # can't use fixture for this, as new schema required for every test
    rf, gwys = await rf_factory([config[SZ_RESPONDENT], config[SZ_SUPPLICANT]])

    try:
        await _test_flow_20x(gwys[0], gwys[1], test_set, packet_flow)
    finally:
        # Cancel any pending binding FSM timers before stopping gateways.
        for gwy in gwys:
            for dev in gwy.device_registry.device_by_id.values():
                bm = getattr(dev, "_binding_manager", None)
                if bm:
                    bm.cancel()
        for gwy in gwys:
            await gwy.stop()
        await rf.stop()
        await asyncio.sleep(0)


def test_binding_phase_tender_intent() -> None:
    """Verify PUT_BIND offer intent constructs a TENDER phase payload."""
    # Arrange
    supplicant_id = "34:259472"
    codes = [Code._2309, Code._30C9]
    vendor_code = "01"
    intent = Command(
        src=Address(supplicant_id),
        dst=Address(supplicant_id),
        action=Action.PUT_BIND,
        data={
            "verb": Verb.I_,
            "codes": codes,
            "vendor_code": vendor_code,
            SZ_OEM_CODE: vendor_code,
        },
    )

    # Act
    dto = build_dto(intent)
    msg = Message._from_cmd(dto)

    # Assert
    assert msg.payload[SZ_PHASE] == BindPhase.TENDER


def test_binding_phase_accept_intent() -> None:
    """Verify PUT_BIND accept intent constructs an ACCEPT phase payload."""
    # Arrange
    respondent_id = "01:220768"
    supplicant_id = "34:259472"
    codes = [Code._2309]
    zone_index = "01"
    intent = Command(
        src=Address(respondent_id),
        dst=Address(supplicant_id),
        action=Action.PUT_BIND,
        data={"verb": Verb.W_, "codes": codes, "index": zone_index},
    )

    # Act
    dto = build_dto(intent)
    msg = Message._from_cmd(dto)

    # Assert
    assert msg.payload[SZ_PHASE] == BindPhase.ACCEPT


def test_binding_phase_affirm_intent() -> None:
    """Verify PUT_BIND confirm intent constructs an AFFIRM phase payload."""
    # Arrange
    supplicant_id = "34:259472"
    respondent_id = "01:220768"
    confirm_code = Code._2309
    index = "01"
    intent = Command(
        src=Address(supplicant_id),
        dst=Address(respondent_id),
        action=Action.PUT_BIND,
        data={"verb": Verb.I_, "codes": confirm_code, "index": index},
    )

    # Act
    dto = build_dto(intent)
    msg = Message._from_cmd(dto)

    # Assert
    assert msg.payload[SZ_PHASE] == BindPhase.AFFIRM


@pytest.mark.asyncio
async def test_binding_manager_cast_methods_phase_parity() -> None:
    """Verify BindingManager cast methods produce intents matching expected BindPhases."""
    # Arrange
    mock_dev = MagicMock()
    mock_dev.id = "34:259472"
    mock_dev._gateway = MagicMock()
    mock_dispatcher = MagicMock()

    sent_intents: list[Command] = []

    async def _mock_send(intent: Command, **kwargs: Any) -> Message:
        sent_intents.append(intent)
        dto = build_dto(intent)
        return Message._from_cmd(dto)

    mock_dispatcher.send = AsyncMock(side_effect=_mock_send)
    mgr = BindingManager(mock_dev, mock_dispatcher)
    mgr._state = MagicMock()
    mgr._state.cast_confirm_accept = AsyncMock()

    # Act 1: Cast Offer (Tender)
    await mgr._make_offer(codes=[Code._2309, Code._30C9], vendor_code="01")

    # Assert 1
    assert len(sent_intents) == 1
    msg1 = Message._from_cmd(build_dto(sent_intents[0]))
    assert msg1.payload[SZ_PHASE] == BindPhase.TENDER

    # Act 2: Accept Offer (Accept)
    offer_packet = Packet.from_port(
        dt.now(UTC),
        "000  I --- 34:259472 --:------ 34:259472 1FC9 012 0023098BF5900030C98BF590",
    )
    tender_msg = Message._from_packet(offer_packet)
    mock_dev.id = "01:220768"
    await mgr._accept_offer(
        tender=tender_msg, codes=[Code._2309], zone_index="01"
    )

    # Assert 2
    assert len(sent_intents) == 2
    msg2 = Message._from_cmd(build_dto(sent_intents[1]))
    assert msg2.payload[SZ_PHASE] == BindPhase.ACCEPT

    # Act 3: Confirm Accept (Affirm)
    accept_packet = Packet.from_port(
        dt.now(UTC),
        "000  W --- 01:220768 34:259472 --:------ 1FC9 006 012309075E60",
    )
    accept_msg = Message._from_packet(accept_packet)
    mock_dev.id = "34:259472"
    await mgr._confirm_accept(accept=accept_msg, confirm_code=Code._2309)

    # Assert 3
    assert len(sent_intents) == 3
    msg3 = Message._from_cmd(build_dto(sent_intents[2]))
    assert msg3.payload[SZ_PHASE] == BindPhase.AFFIRM
