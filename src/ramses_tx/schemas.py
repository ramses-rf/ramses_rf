#!/usr/bin/env python3
"""RAMSES RF - a RAMSES-II protocol decoder & analyser.

Schema processor for protocol (lower) layer.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Final, Never, NewType, TypeAlias, TypedDict, TypeVar

import voluptuous as vol

from .const import (
    DEFAULT_ECHO_TIMEOUT,
    DEFAULT_RPLY_TIMEOUT,
    DEV_TYPE_MAP,
    DEVICE_ID_REGEX,
    MAX_DUTY_CYCLE_RATE,
    MIN_INTER_WRITE_GAP,
)

_LOGGER = logging.getLogger(__name__)


#
# 0/5: Packet source configuration
SZ_COMMS_PARAMS: Final = "comms_params"
SZ_DUTY_CYCLE_LIMIT: Final = "duty_cycle_limit"
SZ_GAP_BETWEEN_WRITES: Final = "gap_between_writes"
SZ_ECHO_TIMEOUT: Final = "echo_timeout"
SZ_RPLY_TIMEOUT: Final = "reply_timeout"

SCH_COMMS_PARAMS = vol.Schema(
    {
        vol.Required(SZ_DUTY_CYCLE_LIMIT, default=MAX_DUTY_CYCLE_RATE): vol.All(
            float, vol.Range(min=0.005, max=0.2)
        ),
        vol.Required(SZ_GAP_BETWEEN_WRITES, default=MIN_INTER_WRITE_GAP): vol.All(
            float, vol.Range(min=0.05, max=1.0)
        ),
        vol.Required(SZ_ECHO_TIMEOUT, default=DEFAULT_ECHO_TIMEOUT): vol.All(
            float, vol.Range(min=0.01, max=1.0)
        ),
        vol.Required(SZ_RPLY_TIMEOUT, default=DEFAULT_RPLY_TIMEOUT): vol.All(
            float, vol.Range(min=0.01, max=1.0)
        ),
    },
    extra=vol.PREVENT_EXTRA,
)

#
# 1/5: Packet source configuration
SZ_INPUT_FILE: Final = "input_file"
SZ_PACKET_SOURCE: Final = "packet_source"


#
# 2/5: Packet log configuration
SZ_FILE_NAME: Final = "file_name"
SZ_PACKET_LOG: Final = "packet_log"
SZ_ROTATE_BACKUPS: Final = "rotate_backups"
SZ_ROTATE_BYTES: Final = "rotate_bytes"


class PktLogConfigT(TypedDict):
    file_name: str
    rotate_backups: int
    rotate_bytes: int | None


def sch_packet_log_dict_factory(
    default_backups: int = 0,
) -> dict[vol.Required, vol.Any]:
    """Return a packet log dict with a configurable default rotation policy.

    usage:

    SCH_PACKET_LOG_7 = vol.Schema(
        packet_log_dict_factory(default_backups=7), extra=vol.PREVENT_EXTRA
    )
    """

    SCH_PACKET_LOG_CONFIG = vol.Schema(
        {
            vol.Optional(SZ_ROTATE_BACKUPS, default=default_backups): vol.Any(
                None, int
            ),
            vol.Optional(SZ_ROTATE_BYTES): vol.Any(None, int),
        },
        extra=vol.PREVENT_EXTRA,
    )

    SCH_PACKET_LOG_NAME = str

    def NormalisePacketLog(rotate_backups: int = 0) -> Callable[..., Any]:
        def normalise_packet_log(node_value: str | PktLogConfigT) -> PktLogConfigT:
            if isinstance(node_value, str):
                return {
                    SZ_FILE_NAME: node_value,
                    SZ_ROTATE_BACKUPS: rotate_backups,
                    SZ_ROTATE_BYTES: None,
                }
            return node_value

        return normalise_packet_log

    return {  # SCH_PACKET_LOG_DICT
        vol.Required(SZ_PACKET_LOG, default=None): vol.Any(
            None,
            vol.All(
                SCH_PACKET_LOG_NAME,
                NormalisePacketLog(rotate_backups=default_backups),
            ),
            SCH_PACKET_LOG_CONFIG.extend(
                {vol.Required(SZ_FILE_NAME): SCH_PACKET_LOG_NAME}
            ),
        )
    }


SCH_PACKET_LOG = vol.Schema(
    sch_packet_log_dict_factory(default_backups=7), extra=vol.PREVENT_EXTRA
)

#
# 3/5: Serial port configuration
SZ_PORT_CONFIG: Final = "port_config"
SZ_PORT_NAME: Final = "port_name"
SZ_SERIAL_PORT: Final = "serial_port"

SZ_BAUDRATE: Final = "baudrate"
SZ_DSRDTR: Final = "dsrdtr"
SZ_RTSCTS: Final = "rtscts"
SZ_TIMEOUT: Final = "timeout"
SZ_XONXOFF: Final = "xonxoff"


SCH_SERIAL_PORT_CONFIG = vol.Schema(
    {
        vol.Optional(SZ_BAUDRATE, default=115200): vol.All(
            vol.Coerce(int), vol.Any(57600, 115200)
        ),  # NB: HGI80 does not work, except at 115200 - so must be default
        vol.Optional(SZ_DSRDTR, default=False): bool,
        vol.Optional(SZ_RTSCTS, default=False): bool,
        vol.Optional(SZ_TIMEOUT, default=0): vol.Any(None, int),  # default None?
        vol.Optional(SZ_XONXOFF, default=True): bool,  # set True to remove \x11
    },
    extra=vol.PREVENT_EXTRA,
)


class PortConfigT(TypedDict):
    baudrate: int  # 57600, 115200
    dsrdtr: bool
    rtscts: bool
    timeout: int
    xonxoff: bool


def sch_serial_port_dict_factory() -> dict[vol.Required, vol.Any]:
    """Return a serial port dict.

    usage:

    SCH_SERIAL_PORT = vol.Schema(
        sch_serial_port_dict_factory(), extra=vol.PREVENT_EXTRA
    )
    """

    SCH_SERIAL_PORT_NAME = str

    def NormaliseSerialPort() -> Callable[[str | PortConfigT], PortConfigT]:
        def normalise_serial_port(node_value: str | PortConfigT) -> PortConfigT:
            if isinstance(node_value, str):
                return {SZ_PORT_NAME: node_value} | SCH_SERIAL_PORT_CONFIG({})  # type: ignore[no-any-return]
            return node_value

        return normalise_serial_port

    return {  # SCH_SERIAL_PORT_DICT
        vol.Required(SZ_SERIAL_PORT): vol.Any(
            vol.All(
                SCH_SERIAL_PORT_NAME,
                NormaliseSerialPort(),
            ),
            SCH_SERIAL_PORT_CONFIG.extend(
                {vol.Required(SZ_PORT_NAME): SCH_SERIAL_PORT_NAME}
            ),
        )
    }


def extract_serial_port(ser_port_dict: dict[str, Any]) -> tuple[str, PortConfigT]:
    """Extract a serial port, port_config_dict tuple from a sch_serial_port_dict."""
    port_name: str = ser_port_dict.get(SZ_PORT_NAME)  # type: ignore[assignment]
    port_config = {k: v for k, v in ser_port_dict.items() if k != SZ_PORT_NAME}
    return port_name, port_config  # type: ignore[return-value]


#
# 4/5: Traits (of devices) configuration (basic)

_T = TypeVar("_T")


def ConvertNullToDict() -> Callable[[_T | None], _T | dict[Never, Never]]:
    def convert_null_to_dict(node_value: _T | None) -> _T | dict[Never, Never]:
        if node_value is None:
            return {}
        return node_value

    return convert_null_to_dict


SZ_ALIAS: Final = "alias"
SZ_CLASS: Final = "class"
SZ_FAKED: Final = "faked"
SZ_SCHEME: Final = "scheme"

SZ_BLOCK_LIST: Final = "block_list"
SZ_KNOWN_LIST: Final = "known_list"

SCH_DEVICE_ID_ANY = vol.Match(DEVICE_ID_REGEX.ANY)
SCH_DEVICE_ID_SEN = vol.Match(DEVICE_ID_REGEX.SEN)
SCH_DEVICE_ID_CTL = vol.Match(DEVICE_ID_REGEX.CTL)
SCH_DEVICE_ID_DHW = vol.Match(DEVICE_ID_REGEX.DHW)
SCH_DEVICE_ID_HGI = vol.Match(DEVICE_ID_REGEX.HGI)
SCH_DEVICE_ID_APP = vol.Match(DEVICE_ID_REGEX.APP)
SCH_DEVICE_ID_BDR = vol.Match(DEVICE_ID_REGEX.BDR)
SCH_DEVICE_ID_UFC = vol.Match(DEVICE_ID_REGEX.UFC)

_SCH_TRAITS_DOMAINS = ("heat", "hvac")
_SCH_TRAITS_HVAC_SCHEMES = ("itho", "nuaire", "orcon", "vasco", "climarad")


DeviceTraitsT = TypedDict(
    "DeviceTraitsT",
    {
        "alias": str | None,
        "faked": bool | None,
        "class": str | None,
    },
)


def sch_global_traits_dict_factory(
    heat_traits: dict[vol.Optional, vol.Any] | None = None,
    hvac_traits: dict[vol.Optional, vol.Any] | None = None,
) -> tuple[dict[vol.Optional, vol.Any], vol.Any]:
    """Return a global traits dict with a configurable extra traits.

    usage:

    SCH_GLOBAL_TRAITS = vol.Schema(
        sch_global_traits_dict(heat=traits), extra=vol.PREVENT_EXTRA
    )
    """

    heat_traits = heat_traits or {}
    hvac_traits = hvac_traits or {}

    SCH_TRAITS_BASE = vol.Schema(
        {
            vol.Optional(SZ_ALIAS, default=None): vol.Any(None, str),
            vol.Optional(SZ_FAKED, default=None): vol.Any(None, bool),
            vol.Optional(vol.Remove("_note")): str,  # only for convenience, not used
        },
        extra=vol.PREVENT_EXTRA,
    )

    # NOTE: voluptuous doesn't like StrEnums, hence str(s)
    # TIP: the _domain key can be used to force which traits schema to use
    heat_slugs = list(
        str(s) for s in DEV_TYPE_MAP.slugs() if s not in DEV_TYPE_MAP.HVAC_SLUGS
    )
    SCH_TRAITS_HEAT = SCH_TRAITS_BASE.extend(
        {
            vol.Optional("_domain", default="heat"): "heat",
            vol.Optional(SZ_CLASS): vol.Any(
                None, *heat_slugs, *(str(DEV_TYPE_MAP[s]) for s in heat_slugs)
            ),
        }
    )
    SCH_TRAITS_HEAT = SCH_TRAITS_HEAT.extend(
        heat_traits,
        extra=vol.PREVENT_EXTRA if heat_traits else vol.REMOVE_EXTRA,
    )

    # NOTE: voluptuous doesn't like StrEnums, hence str(s)
    hvac_slugs = list(str(s) for s in DEV_TYPE_MAP.HVAC_SLUGS)
    SCH_TRAITS_HVAC = SCH_TRAITS_BASE.extend(
        {
            vol.Optional("_domain", default="hvac"): "hvac",
            vol.Optional(SZ_CLASS, default="HVC"): vol.Any(
                None, *hvac_slugs, *(str(DEV_TYPE_MAP[s]) for s in hvac_slugs)
            ),  # TODO: consider removing None
        }
    )
    SCH_TRAITS_HVAC = SCH_TRAITS_HVAC.extend(
        {vol.Optional(SZ_SCHEME): vol.Any(*_SCH_TRAITS_HVAC_SCHEMES)}
    )
    SCH_TRAITS_HVAC = SCH_TRAITS_HVAC.extend(
        hvac_traits,
        extra=vol.PREVENT_EXTRA if hvac_traits else vol.REMOVE_EXTRA,
    )

    SCH_TRAITS = vol.Any(
        vol.All(None, ConvertNullToDict()),
        vol.Any(SCH_TRAITS_HEAT, SCH_TRAITS_HVAC),
        extra=vol.PREVENT_EXTRA,
    )
    SCH_DEVICE = vol.Schema(
        {vol.Optional(SCH_DEVICE_ID_ANY): SCH_TRAITS},
        extra=vol.PREVENT_EXTRA,
    )

    global_traits_dict = {  # Filter lists with Device traits...
        vol.Optional(SZ_KNOWN_LIST, default={}): vol.Any(
            vol.All(None, ConvertNullToDict()),
            vol.All(SCH_DEVICE, vol.Length(min=0)),
        ),
        vol.Optional(SZ_BLOCK_LIST, default={}): vol.Any(
            vol.All(None, ConvertNullToDict()),
            vol.All(SCH_DEVICE, vol.Length(min=0)),
        ),
    }

    return global_traits_dict, SCH_TRAITS


SCH_GLOBAL_TRAITS_DICT, SCH_TRAITS = sch_global_traits_dict_factory()

#
# Device lists (Engine configuration)


DeviceIdT = NewType("DeviceIdT", str)  # TypeVar('DeviceIdT', bound=str)  #
DevIndexT = NewType("DevIndexT", str)
DeviceListT: TypeAlias = dict[DeviceIdT, DeviceTraitsT]


def select_device_filter_mode(
    enforce_known_list: bool,
    known_list: DeviceListT,
    block_list: DeviceListT,
) -> bool:
    """Determine which device filter to use, if any.

    Either:
     - block if device_id in block_list (could be empty), otherwise
     - allow if device_id in known_list, or
    """

    # warn if not has_exactly_one_valid_hgi(known_list)

    if enforce_known_list and not known_list:
        _LOGGER.warning(
            f"Best practice is to enforce a {SZ_KNOWN_LIST} (an allow list), "
            f"but it is empty, so it can't be used "
        )
        enforce_known_list = False

    if enforce_known_list:
        _LOGGER.info(
            f"A valid {SZ_KNOWN_LIST} was provided, "
            f"and will be enforced as a allow list: length = {len(known_list)}"
        )
        _LOGGER.debug(f"known_list = {known_list}")

    elif block_list:
        _LOGGER.info(
            f"A valid {SZ_BLOCK_LIST} was provided, "
            f"and will be used as a deny list: length = {len(block_list)}"
        )
        _LOGGER.debug(f"block_list = {block_list}")

    elif known_list:
        _LOGGER.warning(
            f"Best practice is to enforce the {SZ_KNOWN_LIST} as an allow list, "
            f"configure: {SZ_ENFORCE_KNOWN_LIST} = True"
        )
        _LOGGER.debug(f"known_list = {known_list}")

    else:
        _LOGGER.warning(
            f"Best practice is to provide a {SZ_KNOWN_LIST} and enforce it, "
            f"configure: {SZ_ENFORCE_KNOWN_LIST} = True"
        )

    return enforce_known_list


#
# 5/5: Gateway (engine) configuration
SZ_DISABLE_SENDING: Final = "disable_sending"
SZ_DISABLE_QOS: Final = "disable_qos"
SZ_ENFORCE_KNOWN_LIST: Final[str] = f"enforce_{SZ_KNOWN_LIST}"
SZ_EVOFW_FLAG: Final = "evofw_flag"
SZ_USE_REGEX: Final = "use_regex"

SCH_ENGINE_DICT = {
    vol.Optional(SZ_DISABLE_SENDING, default=False): bool,
    vol.Optional(SZ_DISABLE_QOS, default=None): vol.Any(
        None,  # None is selective QoS (e.g. QoS only for bindings, schedule, etc.)
        bool,
    ),  # in long term, this default to be True (and no None)
    vol.Optional(SZ_ENFORCE_KNOWN_LIST, default=False): bool,
    vol.Optional(SZ_EVOFW_FLAG): vol.Any(None, str),
    # vol.Optional(SZ_PORT_CONFIG): SCH_SERIAL_PORT_CONFIG,
    vol.Optional(SZ_USE_REGEX): dict,  # vol.All(ConvertNullToDict(), dict),
    vol.Optional(SZ_COMMS_PARAMS): SCH_COMMS_PARAMS,
}
SCH_ENGINE_CONFIG = vol.Schema(SCH_ENGINE_DICT, extra=vol.REMOVE_EXTRA)

SZ_INBOUND: Final = "inbound"  # for use_regex (intentionally obscured)
SZ_OUTBOUND: Final = "outbound"
