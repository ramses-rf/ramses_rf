#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#

# TODO: replace protocol_decorator with a factory or fixture

"""RAMSES RF - Test the binding protocol with a virtual RF

NB: This test will likely fail with pytest -n x, because of the protocol's throttle
limits.
"""

import asyncio
from collections.abc import Callable

import pytest
import serial

from ramses_rf import Message
from ramses_tx.const import SZ_ACTIVE_HGI, SZ_IS_EVOFW3, Code
from ramses_tx.protocol import RamsesProtocolT, create_stack, protocol_factory
from ramses_tx.schemas import DeviceIdT
from ramses_tx.transport import RamsesTransportT, transport_factory

from .virtual_rf import HgiFwTypes, VirtualRf

# ######################################################################################


async def assert_stack_state(
    protocol: RamsesProtocolT, transport: RamsesTransportT
) -> None:
    assert transport._this_pkt.code == Code._PUZZ
    assert transport._this_pkt.src.id == GWY_ID
    assert transport._prev_pkt is None

    assert transport.get_extra_info(SZ_ACTIVE_HGI) == GWY_ID
    assert transport.get_extra_info(SZ_IS_EVOFW3) is True

    assert protocol._active_hgi == GWY_ID
    assert protocol._is_evofw3 is True


def _msg_handler(msg: Message) -> None:
    pass


# ### TESTS ############################################################################


async def _test_create_stack(
    rf: VirtualRf,
    /,
    *,
    protocol_factory_: Callable | None = None,
    transport_factory_: Callable | None = None,
    disable_qos: bool | None = False,
    disable_sending: bool | None = False,
    enforce_include_list: bool = False,
    exclude_list: dict[DeviceIdT, dict] | None = None,
    include_list: dict[DeviceIdT, dict] | None = None,
    **kwargs,  # TODO: these are for the transport_factory
) -> None:
    protocol: RamsesProtocolT
    transport: RamsesTransportT

    protocol, transport = await create_stack(
        _msg_handler,
        disable_qos=disable_qos,
        disable_sending=disable_sending,
        enforce_include_list=enforce_include_list,
        exclude_list=exclude_list,
        include_list=include_list,
        **kwargs,
    )

    try:
        await assert_stack_state(protocol, transport)
    except serial.SerialException as err:
        transport._close(err=err)
        raise
    except (AssertionError, asyncio.InvalidStateError, TimeoutError):
        transport.close()
        raise
    else:
        transport.close()


async def _test_factories(
    rf: VirtualRf,
    /,
    *,
    protocol_factory_: Callable | None = None,
    transport_factory_: Callable | None = None,
    disable_qos: bool | None = False,
    disable_sending: bool | None = False,
    enforce_include_list: bool = False,
    exclude_list: dict[DeviceIdT, dict] | None = None,
    include_list: dict[DeviceIdT, dict] | None = None,
    **kwargs,  # TODO: these are for the transport_factory
) -> None:
    protocol: RamsesProtocolT = protocol_factory(
        _msg_handler,
        disable_qos=disable_qos,
        disable_sending=disable_sending,
        enforce_include_list=enforce_include_list,
        exclude_list=exclude_list,
        include_list=include_list,
    )
    transport: RamsesTransportT = await transport_factory(
        protocol,
        disable_sending=disable_sending,
        **kwargs,
    )

    try:
        await assert_stack_state(protocol, transport)
    except serial.SerialException as err:
        transport._close(err=err)
        raise
    except (AssertionError, asyncio.InvalidStateError, TimeoutError):
        transport.close()
        raise
    else:
        transport.close()


# ######################################################################################


GWY_ID = "18:111111"
OTH_ID = "18:123456"

TEST_SUITE_GOOD = {
    "00": {
        "include_list": {},
    },
    "01": {
        "enforce_include_list": True,
        "include_list": {GWY_ID: {"class": "HGI"}},
    },
    "02": {
        "enforce_include_list": True,
        "include_list": {GWY_ID: {"class": "HGI"}, OTH_ID: {"class": "HGI"}},
    },
    "03": {
        "enforce_include_list": True,
        "include_list": {OTH_ID: {"class": "HGI"}, GWY_ID: {"class": "HGI"}},
    },
    "04": {
        "enforce_include_list": True,
        "include_list": {OTH_ID: {"class": "HGI"}},
    },
}
TEST_SUITE_FAIL = {  # fails because exclude_list is checked before active_hgi
    "10": {
        "exclude_list": {GWY_ID: {"class": "HGI"}},
    },
    "11": {
        "enforce_include_list": True,
        "exclude_list": {GWY_ID: {"class": "HGI"}},
        "include_list": {GWY_ID: {"class": "HGI"}},
    },
}


@pytest.mark.xdist_group(name="virt_serial")
@pytest.mark.parametrize("idx", TEST_SUITE_GOOD)
async def test_create_stack(idx: str) -> None:
    """Check that Transport calls Protocol.connection_made() correctly."""

    rf = VirtualRf(2, start=True)
    rf.set_gateway(rf.ports[0], GWY_ID, fw_type=HgiFwTypes.EVOFW3)

    kwargs = {
        "port_name": rf.ports[0],
        "port_config": {},
        "extra": {"virtual_rf": rf},
    }

    try:
        await _test_create_stack(rf, **TEST_SUITE_GOOD[idx], **kwargs)
    finally:
        await rf.stop()


@pytest.mark.xdist_group(name="virt_serial")
@pytest.mark.parametrize("idx", TEST_SUITE_GOOD)
async def test_create_s_alt(idx: str) -> None:
    """Check that Transport calls Protocol.connection_made() correctly."""

    rf = VirtualRf(2, start=True)
    rf.set_gateway(rf.ports[0], GWY_ID, fw_type=HgiFwTypes.EVOFW3)

    kwargs = {
        "port_name": rf.ports[0],
        "port_config": {},
        "protocol_factory_": protocol_factory,
        "transport_factory_": transport_factory,
        "extra": {"virtual_rf": rf},
    }

    try:
        await _test_create_stack(rf, **TEST_SUITE_GOOD[idx], **kwargs)
    finally:
        await rf.stop()


@pytest.mark.xdist_group(name="virt_serial")
@pytest.mark.parametrize("idx", TEST_SUITE_GOOD)
async def test_factories_01(idx: str) -> None:
    """Check that Transport calls Protocol.connection_made() correctly.

    This is the method used by ramses_tf.gateway.py.
    """

    rf = VirtualRf(2, start=True)
    rf.set_gateway(rf.ports[0], GWY_ID, fw_type=HgiFwTypes.EVOFW3)

    kwargs = {
        "port_name": rf.ports[0],
        "port_config": {},
        "extra": {"virtual_rf": rf},
    }

    try:
        await _test_factories(rf, **TEST_SUITE_GOOD[idx], **kwargs)
    finally:
        await rf.stop()
