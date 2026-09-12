#!/usr/bin/env python3
"""RAMSES RF - a RAMSES-II protocol decoder & analyser.

`ramses_tx` takes care of the RF protocol (lower) layer.
"""

from __future__ import annotations

import asyncio
from functools import partial
from logging.handlers import QueueListener
from typing import TYPE_CHECKING, Any

from .address import (
    ALL_DEV_ADDR,
    ALL_DEVICE_ID,
    NON_DEV_ADDR,
    NON_DEVICE_ID,
    Address,
)
from .const import (
    DEVICE_ID_REGEX,
    F9,
    FA,
    FC,
    FF,
    HGI_ID_PATTERN,
    HGI_PREFIX,
    SZ_ACTIVE_GATEWAY,
    SZ_ACTIVE_HGI,
    SZ_REPEAT_COUNT,
    IndexT,
    Priority,
    Verb,
)
from .discovery import is_hgi80
from .dtos import CommandDTO, PacketDTO
from .engine import Engine
from .logger import set_packet_logging
from .packet import PKT_LOGGER, Packet
from .protocol import (
    PortProtocol,
    RamsesProtocolT,
    ReadProtocol,
    protocol_factory,
)
from .schemas import SZ_SERIAL_PORT
from .transport import RamsesTransportT, ZigbeeTransport, transport_factory
from .typing import DeviceIdT, QosParams
from .version import VERSION

from .const import (  # isort: skip
    I_,
    RP,
    RQ,
    W_,
    Code,
)


__all__ = [
    "VERSION",
    "Engine",
    #
    "SZ_ACTIVE_GATEWAY",
    "SZ_ACTIVE_HGI",
    "SZ_DEVICE_ROLE",
    "SZ_DOMAIN_ID",
    "SZ_REPEAT_COUNT",
    "SZ_SERIAL_PORT",
    "SZ_ZONE_CLASS",
    "SZ_ZONE_INDEX",
    "SZ_ZONE_MASK",
    "SZ_ZONE_TYPE",
    #
    "ALL_DEV_ADDR",
    "ALL_DEVICE_ID",
    "NON_DEV_ADDR",
    "NON_DEVICE_ID",
    #
    "I_",
    "RP",
    "RQ",
    "W_",
    "F9",
    "FA",
    "FC",
    "FF",
    "HGI_PREFIX",
    "HGI_ID_PATTERN",
    #
    "DEVICE_ID_REGEX",
    #
    "DeviceIdT",
    "IndexT",
    "Verb",
    #
    "Address",
    "Code",
    "CommandDTO",
    "Packet",
    "PacketDTO",
    "Priority",
    "QosParams",
    #
    "PortProtocol",
    "ReadProtocol",
    "RamsesProtocolT",
    "protocol_factory",
    #
    "RamsesTransportT",
    "ZigbeeTransport",
    "is_hgi80",
    "transport_factory",
    #
    "is_valid_dev_id",
    "set_packet_logging_config",
]


is_valid_dev_id = Address.is_valid

if TYPE_CHECKING:
    from logging import Logger


async def set_packet_logging_config(
    **config: Any,
) -> tuple[Logger, QueueListener | None]:
    """Set up ramses packet logging to a file or port.

    Must run async in executor to prevent HA blocking call opening packet log file.

    :param config: if file_name is included, opens packet_log file
    :return: a tuple (logging.Logger, QueueListener)
    """
    loop = asyncio.get_running_loop()
    listener = await loop.run_in_executor(
        None, partial(set_packet_logging, PKT_LOGGER, **config)
    )
    return PKT_LOGGER, listener
