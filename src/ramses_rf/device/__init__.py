#!/usr/bin/env python3
"""RAMSES RF - Heating devices (e.g. CTL, OTB, BDR, TRV)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ramses_rf.const import DEV_TYPE_MAP
from ramses_rf.models import DeviceTraits
from ramses_tx.const import DevType

from .base import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    BASE_CLASS_BY_SLUG as _BASE_CLASS_BY_SLUG,
    Device,
    Fakeable,
    DeviceHeat,
    HgiGateway,
    DeviceHvac,
)


from .heat import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    HEAT_CLASS_BY_SLUG as _HEAT_CLASS_BY_SLUG,
    BdrSwitch,
    Controller,
    DhwSensor,
    OtbGateway,
    OutSensor,
    Temperature,
    Thermostat,
    TrvActuator,
    UfhCircuit,
    UfhController,
    class_dev_heat,
)


from .hvac import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    HVAC_CLASS_BY_SLUG as _HVAC_CLASS_BY_SLUG,
    HvacCarbonDioxideSensor,
    HvacHumiditySensor,
    HvacRemote,
    HvacVentilator,
    RfsGateway,
    class_dev_hvac,
)

if TYPE_CHECKING:
    from ramses_rf import Gateway
    from ramses_tx import Address, Message


__all__ = [
    # .base
    "Device",
    "Fakeable",
    "DeviceHeat",
    "HgiGateway",
    "DeviceHvac",
    # .heat
    "BdrSwitch",
    "Controller",
    "DhwSensor",
    "OtbGateway",
    "OutSensor",
    "Temperature",
    "Thermostat",
    "TrvActuator",
    "UfhCircuit",
    "UfhController",
    "class_dev_heat",
    # .hvac
    "HvacCarbonDioxideSensor",
    "HvacHumiditySensor",
    "HvacRemote",
    "HvacVentilator",
    "RfsGateway",
    "class_dev_hvac",
    #
    "best_dev_role",
    "device_factory",
]

_LOGGER = logging.getLogger(__name__)


_CLASS_BY_SLUG = _BASE_CLASS_BY_SLUG | _HEAT_CLASS_BY_SLUG | _HVAC_CLASS_BY_SLUG

HEAT_DEV_CLASS_BY_SLUG = {
    k: v for k, v in _HEAT_CLASS_BY_SLUG.items() if k is not DevType.HEA
}
HVAC_DEV_CLASS_BY_SLUG = {
    k: v for k, v in _HVAC_CLASS_BY_SLUG.items() if k is not DevType.HVC
}


def best_dev_role(
    dev_addr: Address,
    *,
    msg: Message | None = None,
    eavesdrop: bool = False,
    traits: DeviceTraits | None = None,
) -> type[Device]:
    """Return the best device role (object class) for a given device id/msg/schema.

    Heat (CH/DHW) devices can reliably be determined by their address type (e.g. '04:').
    Any device without a known Heat type is considered a HVAC device.

    HVAC devices must be explicitly typed, or fingerprinted/eavesdropped.
    The generic HVAC class can be promoted later on, when more information is available.
    """

    cls: type[Device]
    slug: str | None

    traits = traits or DeviceTraits()

    try:  # convert (say) 'dhw_sensor' to DHW
        slug = DEV_TYPE_MAP.slug(traits.device_class)  # type: ignore[arg-type]
    except KeyError:
        slug = traits.device_class

    # a specified device class always takes precedence (even if it is wrong)...
    if slug and slug in _CLASS_BY_SLUG:
        cls = _CLASS_BY_SLUG[slug]
        _LOGGER.debug(
            f"Using an explicitly-defined class for: {dev_addr!r} ({cls._SLUG})"
        )
        return cls

    if dev_addr.type == DEV_TYPE_MAP.HGI:
        _LOGGER.debug(f"Using the default class for: {dev_addr!r} ({HgiGateway._SLUG})")
        return HgiGateway

    try:  # or, is it a well-known CH/DHW class, derived from the device type...
        if cls := class_dev_heat(dev_addr, msg=msg, eavesdrop=eavesdrop):
            _LOGGER.debug(
                f"Using the default Heat class for: {dev_addr!r} ({cls._SLUG})"
            )
            return cls
    except TypeError:
        pass

    try:  # or, a HVAC class, eavesdropped from the message code/payload...
        if cls := class_dev_hvac(dev_addr, msg=msg, eavesdrop=eavesdrop):
            _LOGGER.debug(
                f"Using eavesdropped HVAC class for: {dev_addr!r} ({cls._SLUG})"
            )
            return cls  # includes DeviceHvac
    except TypeError:
        pass

    # otherwise, use the default device class...
    _LOGGER.debug(
        f"Using a promotable HVAC class for: {dev_addr!r} ({DeviceHvac._SLUG})"
    )
    return DeviceHvac


def device_factory(
    gwy: Gateway,
    dev_addr: Address,
    *,
    msg: Message | None = None,
    traits: DeviceTraits | None = None,
) -> Device:
    """Return the initial device class for a given device id/msg/traits.

    Devices of certain classes are promotable to a compatible sub class.
    """

    traits = traits or DeviceTraits()

    cls: type[Device] = best_dev_role(
        dev_addr,
        msg=msg,
        eavesdrop=gwy.config.enable_eavesdrop,
        traits=traits,
    )

    if (
        issubclass(cls, DeviceHvac)
        and traits.device_class in (DevType.HVC, None)
        and traits.faked
    ):
        raise TypeError(
            f"Faked devices from the HVAC domain must have an explicit class: {dev_addr}"
        )

    return cls.create_from_schema(gwy, dev_addr, traits=traits)
