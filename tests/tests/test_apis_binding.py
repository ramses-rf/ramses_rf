#!/usr/bin/env python3
"""RAMSES RF - Test the wait_for_binding_request, initiate_binding_process APIs."""

import unittest.mock
from types import SimpleNamespace
from typing import TypeVar

import pytest

from ramses_rf.device.base import Fakeable  # initiate_binding_, wait_for_binding_
from ramses_rf.device.heat import DhwSensor, Thermostat  # initiate_binding_process
from ramses_rf.device.hvac import (  # initiate_binding_process
    HvacCarbonDioxideSensor,
    HvacDisplayRemote,
    HvacRemote,
)
from ramses_tx.address import Address
from ramses_tx.const import Code

_FakeableDeviceT = TypeVar("_FakeableDeviceT", bound=Fakeable)


# ### TEST SUITE ######################################################################
ADDR_CLASS_LOOKUP: dict[str, type[Fakeable]] = {
    "07:123456": DhwSensor,
    "03:123456": Thermostat,
    # "09:123456": OutSensor,
    "31:123456": HvacCarbonDioxideSensor,
    "32:123456": HvacDisplayRemote,
    "33:123456": HvacRemote,
}
ADDR_CLASS_MAP = {v: k for k, v in ADDR_CLASS_LOOKUP.items()}

CLASS_CODES_MAP: dict[type[Fakeable], Code | tuple[Code, ...]] = {
    DhwSensor: Code._1260,
    Thermostat: (Code._2309, Code._30C9, Code._0008),
    HvacCarbonDioxideSensor: (Code._31E0, Code._1298, Code._2E10),
    HvacRemote: (Code._22F1, Code._22F3),
}


class GatewayStub:
    config = SimpleNamespace(**{"disable_discovery": True})

    device_by_id: dict[str, Fakeable] = {}
    devices: list[Fakeable] = []

    _include: dict[str] = {}
    msg_db = None

    def _add_device(self, dev: Fakeable) -> None:
        self.device_by_id[dev.id] = dev
        self.devices.append(dev)


# ### FIXTURES ########################################################################


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    def id_fnc(dev_class: type[Fakeable]) -> str:
        return dev_class._SLUG

    metafunc.parametrize("dev_class", ADDR_CLASS_MAP.keys(), ids=id_fnc)


# ### TESTS ###########################################################################


async def test_initiate_binding_process(dev_class: type[Fakeable]) -> None:
    assert issubclass(dev_class, Fakeable)

    gwy = GatewayStub()
    dev_addr = Address(ADDR_CLASS_MAP[dev_class])

    with unittest.mock.patch.object(
        Fakeable, "_initiate_binding_process", return_value=None
    ) as mocked_method:
        gwy._include[dev_addr.id] = {}  # this shouldn't be needed? a BUG?

        dev = dev_class(gwy, dev_addr)
        dev._make_fake()

        _ = await dev.initiate_binding_process()

        if codes := CLASS_CODES_MAP.get(dev_class):
            mocked_method.assert_called_once_with(codes)
        else:
            mocked_method.assert_called_once()
