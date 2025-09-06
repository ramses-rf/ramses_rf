#!/usr/bin/env python3
"""RAMSES RF - a RAMSES-II protocol decoder & analyser.

Works with (amongst others):
- evohome (up to 12 zones)
- sundial (up to 2 zones)
- chronotherm (CM60xNG can do 4 zones)
- hometronics (16? zones)
- vision pro
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ramses_tx import Address, Command, Message, Packet  # noqa: F401

from . import exceptions  # noqa: F401
from .device import Device  # noqa: F401
from .exceptions import CommandInvalid  # noqa: F401
from .gateway import Gateway  # noqa: F401
from .version import VERSION  # noqa: F401

from .const import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    I_,
    RP,
    RQ,
    W_,
    Code,
)

if TYPE_CHECKING:
    from .const import IndexT, VerbT  # noqa: F401, pylint: disable=unused-import


__all__ = [
    "VERSION",
    "Gateway",
    #
    "Address",
    "Command",
    "CommandInvalid",
    "Device",
    "Message",
    "Packet",
    #
    "I_",
    "RP",
    "RQ",
    "W_",
    #
    "Code",
    "IndexT",
    "VerbT",
    #
    "exceptions",
]

_LOGGER = logging.getLogger(__name__)


class GracefulExit(SystemExit):
    code = 1
