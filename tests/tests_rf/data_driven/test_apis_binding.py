#!/usr/bin/env python3
"""RAMSES RF - Test the wait_for_binding_request, initiate_binding_process APIs."""

import unittest.mock
from types import SimpleNamespace
from typing import Any, TypeVar

import pytest

from ramses_rf.devices import (  # initiate_binding_process  # initiate_binding_process
    DhwSensor,
    HvacCarbonDioxideSensor,
    HvacDisplayRemote,
    HvacRemote,
    Thermostat,
)
from ramses_rf.devices.dev_base import (
    Fakeable,  # initiate_binding_, wait_for_binding_
)
from ramses_rf.models import DeviceTraits
from ramses_rf.state import MessageStore
from ramses_rf.strategies import NuaireStrategy
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
    """A stub for the Gateway to provide isolated state for testing bindings."""

    def __init__(self) -> None:
        """Initialize the GatewayStub."""
        self.config = SimpleNamespace(disable_discovery=True, known_list={})

        self.device_by_id: dict[str, Any] = {}
        self.devices: list[Any] = []

        # Explicitly type as Any to prevent strict mode from complaining about missing mock attributes
        self._engine: Any = unittest.mock.MagicMock()
        self.dispatcher = unittest.mock.MagicMock()
        self.dispatcher.send = unittest.mock.AsyncMock()
        self.message_store = MessageStore(maintain=False)

    @property
    def device_registry(self) -> "GatewayStub":
        """Act as our own DeviceRegistry for testing purposes."""
        return self

    def _add_device(self, dev: Any) -> None:
        self.device_by_id[dev.id] = dev
        self.devices.append(dev)

    def get_device(self, device_id: str) -> Any:
        """Return a previously registered test device."""
        return self.device_by_id[device_id]


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
        gwy._engine._include[
            dev_addr.id
        ] = {}  # this shouldn't be needed? a BUG?

        dev = dev_class(gwy, dev_addr)
        dev._make_fake()

        _ = await dev.initiate_binding_process()

        if codes := CLASS_CODES_MAP.get(dev_class):
            mocked_method.assert_called_once_with(codes)
        else:
            mocked_method.assert_called_once()

        if isinstance(dev, HvacRemote):
            mocked_method.reset_mock()
            dev.set_strategy(NuaireStrategy())

            await dev.initiate_binding_process()

            mocked_method.assert_called_once_with((Code._22F1,))

            mocked_method.reset_mock()
            configured_remote = HvacRemote(
                gwy,
                Address("33:123457"),
                traits=DeviceTraits(scheme="nuaire"),
            )

            await configured_remote.initiate_binding_process()

            mocked_method.assert_called_once_with((Code._22F1,))

        if isinstance(dev, HvacCarbonDioxideSensor):
            mocked_method.reset_mock()
            fan_id = "32:123459"
            gwy.device_by_id[fan_id] = SimpleNamespace(
                id=fan_id,
                _scheme="orcon",
                _strategy=None,
                entity_state=SimpleNamespace(
                    get_value=unittest.mock.AsyncMock(return_value=None)
                ),
            )
            configured_sensor = HvacCarbonDioxideSensor(
                gwy,
                Address("29:123458"),
            )
            gwy.config.known_list[configured_sensor.id] = {"bound": fan_id}
            await configured_sensor.initiate_binding_process()
            mocked_method.assert_called_once_with(
                (("00", Code._31E0), ("01", Code._31E0), ("00", Code._1298))
            )
