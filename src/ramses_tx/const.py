#!/usr/bin/env python3
"""RAMSES TX - a RAMSES-II protocol transport & packet layer.

This module contains constants, enums, and helper classes used throughout the
library to decode and encode RAMSES-II protocol packets.
"""

from __future__ import annotations

import re
from enum import EnumCheck, IntEnum, StrEnum, verify
from types import SimpleNamespace
from typing import Any, Final, Literal, NoReturn

__dev_mode__ = False  # NOTE: this is const.py
DEV_MODE = __dev_mode__

# used by protocol QoS FSM (echo tout is different? for MQTT)...
DEFAULT_DISABLE_QOS: Final[bool | None] = None
DEFAULT_WAIT_FOR_REPLY: Final[bool | None] = None

#: Waiting for echo packet after cmd sent (seconds)
# NOTE: Increased to 3.0s to support high-latency transports (e.g., MQTT)
DEFAULT_ECHO_TIMEOUT: Final[float] = 3.00

#: Waiting for reply packet after echo packet rcvd (seconds)
# NOTE: Increased to 3.0s to support high-latency transports (e.g., MQTT)
DEFAULT_RPLY_TIMEOUT: Final[float] = 3.00
DEFAULT_BUFFER_SIZE: Final[int] = 32

#: Total waiting for successful send (seconds)
DEFAULT_SEND_TIMEOUT: Final[float] = 20.0
#: For a command to be sent, incl. queuing time (seconds)
MAX_SEND_TIMEOUT: Final[float] = 20.0

#: For a command to be re-sent (not incl. 1st send)
MAX_RETRY_LIMIT: Final[int] = 3

#: Minimum gap between writes (seconds)
MIN_INTER_WRITE_GAP: Final[float] = 0.05
DEFAULT_GAP_DURATION: Final[float] = MIN_INTER_WRITE_GAP
MIN_GAP_DURATION: Final[float] = 0.02  # used in ramses_cc Action schema
MAX_GAP_DURATION: Final[float] = 1.0  # used in ramses_cc Action schema

DEFAULT_MAX_RETRIES: Final[int] = 3

DEFAULT_NUM_REPEATS: Final[int] = 0
MIN_NUM_REPEATS: Final[int] = 1  # used in ramses_cc Action schema
MAX_NUM_REPEATS: Final[int] = 5  # used in ramses_cc Action schema

# SZ_QOS: Final = "qos"  # obsolete?

# SZ_CALLBACK: Final = "callback"  # obsolete?
# SZ_GAP_DURATION: Final = "gap_duration"  # obsolete?
SZ_REPEAT_COUNT: Final = "repeat_count"
SZ_NUM_REPEATS: Final = SZ_REPEAT_COUNT
SZ_PRIORITY: Final = "priority"
SZ_TIMEOUT: Final = "timeout"


# used by transport...
SZ_ACTIVE_GATEWAY: Final = "active_gwy"
SZ_ACTIVE_HGI: Final = SZ_ACTIVE_GATEWAY
SZ_SIGNATURE: Final = "signature"
SZ_IS_EVOFW3: Final = "is_evofw3"
SZ_READER_TASK: Final[str] = "reader_task"
SZ_NAME: Final[str] = "name"

# MQTT topic
SZ_RAMSES_GATEWAY: Final[str] = "RAMSES/GATEWAY"

# default values for transmit rate governers...
DUTY_CYCLE_DURATION = (
    60  #      time window (seconds) where rate limiting occurs
)
MAX_DUTY_CYCLE_RATE = 0.01  #    % bandwidth used per cycle
MAX_TRANSMIT_RATE_TOKENS = 80  # transmits per cycle


@verify(EnumCheck.UNIQUE)
class Priority(IntEnum):
    """Priority levels for protocol messages."""

    LOWEST = 4
    LOW = 2
    DEFAULT = 0
    HIGH = -2
    HIGHEST = -4


def slug(string: str) -> str:
    """Convert a string to snake_case.

    :param string: The input string to convert.
    :return: The string converted to snake_case (lowercase, with non-alphanumerics replaced by underscores).
    """
    return re.sub(r"[\W_]+", "_", string.lower())


# TODO: FIXME: This is a mess - needs converting to StrEnum
# NOTE: Kept as plain `dict` (not `dict[str, Any]`) because ramses_rf/const.py
# uses `None` as a sentinel key in 13 dict literals (e.g. `DevRole.HT1: {None:
# "heating_valve"}`).  Typing as `dict[str, Any]` would expose 13 `dict-item`
# errors at those call sites.  Replacing `None` with `""` would be a runtime
# change (sentinel semantics), which is out of scope for Wave 0 mechanical PRs.
class AttrDict(dict):  # type: ignore[type-arg]
    """A read-only dictionary that supports dot-access and two-way lookup.

    This class is typically used to map hex codes (keys) to human-readable slugs (values),
    while also allowing reverse lookup via dot notation (e.g., ``map.SLUG``).

    .. warning::
        This class is immutable. Attempting to modify it will raise a :exc:`TypeError`.
    """

    _SZ_AKA_SLUG: Final = "_root_slug"
    _SZ_DEFAULT: Final = "_default"
    _SZ_SLUGS: Final = "SLUGS"

    def _readonly(self, *args: Any, **kwargs: Any) -> NoReturn:
        """Raise TypeError for read-only operations."""
        raise TypeError(f"'{self.__class__.__name__}' object is read only")

    def __setitem__(self, key: Any, value: Any) -> NoReturn:
        """Prevent item assignment on read-only dictionary."""
        self._readonly()

    def __delitem__(self, key: Any) -> NoReturn:
        """Prevent item deletion on read-only dictionary."""
        self._readonly()

    def clear(self) -> NoReturn:
        """Prevent mutation on read-only object."""
        self._readonly()

    def pop(self, *args: Any, **kwargs: Any) -> NoReturn:
        """Prevent mutation on read-only object."""
        self._readonly()

    def popitem(self) -> NoReturn:
        """Prevent mutation on read-only object."""
        self._readonly()

    def setdefault(self, *args: Any, **kwargs: Any) -> NoReturn:
        """Prevent mutation on read-only object."""
        self._readonly()

    def update(self, *args: Any, **kwargs: Any) -> NoReturn:
        """Prevent mutation on read-only object."""
        self._readonly()

    def __init__(
        self,
        main_table: dict[str, dict],  # type: ignore[type-arg]  # None keys
        attr_table: dict[str, Any],
    ) -> None:
        """Initialize the AttrDict.

        :param main_table: A dictionary mapping keys (usually hex codes) to property dictionaries.
        :param attr_table: A dictionary of additional attributes to expose on the object.
        """
        self._main_table = main_table
        self._attr_table = attr_table
        self._attr_table[self._SZ_SLUGS] = tuple(sorted(main_table.keys()))

        self._slug_lookup: dict[str | None, str] = {
            None: slug  # noqa: B035
            for slug, table in main_table.items()
            for k in table.values()
            if isinstance(k, str) and table.get(self._SZ_DEFAULT)
        }  # i.e. {None: 'HEA'}
        self._slug_lookup.update(
            {
                k: table.get(self._SZ_AKA_SLUG, slug)
                for slug, table in main_table.items()
                for k in table
                if isinstance(k, str) and len(k) == 2
            }  # e.g. {'00': 'TRV', '01': 'CTL', '04': 'TRV', ...}
        )
        self._slug_lookup.update(
            {
                k: slug
                for slug, table in main_table.items()
                for k in table.values()
                if isinstance(k, str) and table.get(self._SZ_AKA_SLUG) is None
            }  # e.g. {'heat_device':'HEA', 'dhw_sensor':'DHW', ...}
        )

        self._forward = {
            k: v
            for table in main_table.values()
            for k, v in table.items()
            if isinstance(k, str) and k[:1] != "_"
        }  # e.g. {'00': 'radiator_valve', '01': 'controller', ...}
        self._reverse = {
            v: k
            for table in main_table.values()
            for k, v in table.items()
            if isinstance(k, str)
            and k[:1] != "_"
            and self._SZ_AKA_SLUG not in table
        }  # e.g. {'radiator_valve': '00', 'controller': '01', ...}
        self._forward = dict(
            sorted(self._forward.items(), key=lambda item: item[0])
        )

        super().__init__(self._forward)

    def __getitem__(self, key: str) -> Any:
        """Retrieve an item by key from the mapping."""
        if key in self._main_table:  # map[ZON_ROLE.DHW] -> "dhw_sensor"
            return list(self._main_table[key].values())[0]
        # if key in self._forward:  # map["0D"] -> "dhw_sensor"
        #     return self._forward.__getitem__(key)
        if key in self._reverse:  # map["dhw_sensor"] -> "0D"
            return self._reverse.__getitem__(key)
        return super().__getitem__(key)

    def __getattr__(self, name: str) -> Any:
        """Retrieve an attribute by name from the table."""
        if name in self._main_table:  # map.DHW -> "0D" (using slug)
            if (result := list(self._main_table[name].keys())[0]) is not None:
                return result
        elif name in self._attr_table:  # bespoke attrs
            return self._attr_table[name]
        elif (
            len(name) and name[1:] in self._forward
        ):  # map._0D -> "dhw_sensor"
            return self._forward[name[1:]]
        elif (
            name.isupper() and name.lower() in self._reverse
        ):  # map.DHW_SENSOR -> "0D"
            return self[name.lower()]
        raise AttributeError(
            f"'{type(self).__name__}' object has no attribute '{name}'"
        )

    def _hex(self, key: str) -> str:
        """Return the key/ID (2-byte hex string) of the two-way dict (e.g. '04').

        :param key: The lookup key (can be slug or code).
        :raises KeyError: If the key is not found.
        :return: The 2-byte hex string identifier.
        """
        if key in self._main_table:
            hex_key: str = list(self._main_table[key].keys())[0] or ""
            return hex_key
        if key in self._reverse:
            return self._reverse[key]
        raise KeyError(key)

    def _str(self, key: str) -> str:
        """Return the value (string) of the two-way dict (e.g. 'radiator_valve').

        :param key: The lookup key.
        :raises KeyError: If the key is not found.
        :return: The human-readable slug string.
        """
        if key in self._main_table:
            result: str = list(self._main_table[key].values())[0]
            return result
        if key in self:
            return str(self[key])
        raise KeyError(key)

    # def values(self):
    #     return {k: k for k in super().values()}.values()

    def slug(self, key: str) -> str:
        """Return master slug for a hex key/ID.

        Example: 00 -> 'TRV' (master), not 'TR0'.

        :param key: The hex key to look up.
        :return: The master slug.
        """
        slug_ = self._slug_lookup[key]
        # if slug_ in self._attr_table["_TRANSFORMS"]:
        #     return self._attr_table["_TRANSFORMS"][slug_]
        return str(slug_)

    def slugs(self) -> tuple[str]:
        """Return the slugs from the main table.

        :return: A tuple of all available slugs.
        """
        slugs: tuple[str] = self._attr_table[self._SZ_SLUGS]
        return slugs


def attr_dict_factory(
    main_table: dict[str, dict],  # type: ignore[type-arg]  # None keys in ramses_rf/const.py
    attr_table: dict[str, Any] | None = None,
) -> AttrDict:  # is: SlottedAttrDict
    """Create a new AttrDict instance with a slotted subclass.

    :param main_table: The primary mapping of codes to slugs.
    :param attr_table: Optional additional attributes to attach to the instance.
    :return: An instance of a dynamic AttrDict subclass.
    """
    if attr_table is None:
        attr_table = {}

    class SlottedAttrDict(AttrDict):
        pass  # TODO: low priority
        # __slots__ = (
        #     list(main_table.keys())
        #     + [
        #         f"_{k}"
        #         for t in main_table.values()
        #         for k in t.keys()
        #         if isinstance(k, str) and len(k) == 2
        #     ]
        #     + [v for t in main_table.values() for v in t.values()]
        #         + list(attr_table.keys())
        #         + [AttrDict._SZ_AKA_SLUG, AttrDict._SZ_SLUGS]
        # )

    return SlottedAttrDict(main_table, attr_table=attr_table)


DEFAULT_MAX_ZONES: Final = 16 if DEV_MODE else 12
# Evohome: 12 (0-11), older/initial version was 8
# Hometronics: 16 (0-15), or more?
# Sundial RF2: 2 (0-1), usually only one, but ST9520C can do two zones


DEVICE_ID_REGEX = SimpleNamespace(
    ANY=re.compile(r"^[0-9]{2}:[0-9]{6}$"),
    BDR=re.compile(r"^13:[0-9]{6}$"),
    CTL=re.compile(r"^(01|23):[0-9]{6}$"),
    DHW=re.compile(r"^07:[0-9]{6}$"),
    HGI=re.compile(r"^18:[0-9]{6}$"),
    APP=re.compile(r"^(10|13):[0-9]{6}$"),
    UFC=re.compile(r"^02:[0-9]{6}$"),
    SEN=re.compile(r"^(01|03|04|12|22|34):[0-9]{6}$"),
)

# HGI device class prefix — all HGI/gateway device IDs start with "18:".
# Centralised here so downstream packages (ramses_cc) don't hardcode the
# prefix (separation of concerns / CQRS).
HGI_PREFIX: Final = "18:"

# HGI device ID pattern without anchors, for embedding in larger regexes
# (e.g. URL parsing).  Use DEVICE_ID_REGEX.HGI for full-match validation.
HGI_ID_PATTERN: Final = r"18:[0-9]{6}"

# Domains
F6: Final = "F6"
F7: Final = "F7"
F8: Final = "F8"
F9: Final = "F9"
FA: Final = "FA"
FB: Final = "FB"
FC: Final = "FC"
FD: Final = "FD"
FE: Final = "FE"
FF: Final = "FF"

DOMAIN_TYPE_MAP: dict[str, str] = {
    F6: "cooling_valve",  # cooling
    F7: "domain_f7",
    F8: "domain_f8",
    F9: "heating_valve",  # Heating Valve
    FA: "hotwater_valve",  # HW Valve (or UFH loop if src.type == UFC?)
    FB: "domain_fb",  # also: cooling valve?
    FC: "appliance_control",  # appliance_control
    FD: "domain_fd",  # seen with hometronics
    # "FE": ???
    # FF: "system",  # TODO: remove this, is not a domain
}  # "21": "Ventilation", "88": ???

# DOMAIN_TYPE_LOOKUP = {v: k for k, v in DOMAIN_TYPE_MAP.items() if k != FF}  # obsolete?

# DHW_STATE_MAP: dict[str, str] = {"00": "off", "01": "on"}  # obsolete?
# DHW_STATE_LOOKUP = {v: k for k, v in DHW_STATE_MAP.items()}  # obsolete?

# DTM_LONG_REGEX = re.compile(
#     r"\d{4}-[01]\d-[0-3]\d(T| )[0-2]\d:[0-5]\d:[0-5]\d\.\d{6} ?"
# )  # 2020-11-30T13:15:00.123456  # obsolete?
# DTM_TIME_REGEX = re.compile(r"[0-2]\d:[0-5]\d:[0-5]\d\.\d{3} ?")  # 13:15:00.123  # obsolete?

# Used by Packet.from_raw_line to validate unparsed ASCII line structures
r = r"(-{3}|\d{3}|\.{3})"  # RSSI, '...' was used by an older version of evofw3
v = r"( I|RP|RQ| W)"  # verb
d = r"(-{2}:-{6}|\d{2}:\d{6})"  # device ID
c = r"[0-9A-F]{4}"  # code
l = r"\d{3}"  # length # noqa: E741
p = r"([0-9A-F]{2}){1,48}"  # payload

RAW_LINE_REGEX = re.compile(f"^{v} {r} {d} {d} {d} {c} {l} {p}$")
COMMAND_REGEX = RAW_LINE_REGEX  # Backward-compatibility alias


# Used by 0418/system_fault parser
class FaultDeviceClass(StrEnum):
    """Device classes for system faults."""

    CONTROLLER = "controller"
    SENSOR = "sensor"
    SETPOINT = "setpoint"
    ACTUATOR = "actuator"  # if domain is FC, then "boiler_relay"
    DHW_ACTUATOR = "dhw_sensor"
    RF_GATEWAY = "rf_gateway"
    BOILER_RELAY = "boiler_relay"
    UNKNOWN = "unknown"


FAULT_DEVICE_CLASS: Final[dict[str, FaultDeviceClass]] = {
    "00": FaultDeviceClass.CONTROLLER,
    "01": FaultDeviceClass.SENSOR,
    "02": FaultDeviceClass.SETPOINT,
    "04": FaultDeviceClass.ACTUATOR,  # if domain is FC, then BOILER_RELAY
    "05": FaultDeviceClass.DHW_ACTUATOR,
    "06": FaultDeviceClass.RF_GATEWAY,
}


class FaultState(StrEnum):
    """States for system faults."""

    FAULT = "fault"
    RESTORE = "restore"
    UNKNOWN_C0 = "unknown_c0"
    UNKNOWN = "unknown"


FAULT_STATE: Final[dict[str, FaultState]] = {  # a bitmap?
    "00": FaultState.FAULT,
    "40": FaultState.RESTORE,
    "C0": FaultState.UNKNOWN_C0,  # C0s do not appear in the evohome UI
}


class FaultType(StrEnum):
    """Types of system faults."""

    SYSTEM_FAULT = "system_fault"
    MAINS_LOW = "mains_low"
    BATTERY_LOW = "battery_low"
    BATTERY_ERROR = "battery_error"  # actually: 'evotouch_battery_error'
    COMMS_FAULT = "comms_fault"
    SENSOR_FAULT = "sensor_fault"  # seen with zone sensor
    SENSOR_ERROR = "sensor_error"
    BAD_VALUE = "bad_value"
    UNKNOWN = "unknown"


FAULT_TYPE: Final[dict[str, FaultType]] = {
    "01": FaultType.SYSTEM_FAULT,
    "03": FaultType.MAINS_LOW,
    "04": FaultType.BATTERY_LOW,
    "05": FaultType.BATTERY_ERROR,  # actually: 'evotouch_battery_error'
    "06": FaultType.COMMS_FAULT,
    "07": FaultType.SENSOR_FAULT,  # seen with zone sensor
    "0A": FaultType.SENSOR_ERROR,
}


class SystemType(StrEnum):
    """System types (e.g. Evohome, Hometronics)."""

    CHRONOTHERM = "chronotherm"
    EVOHOME = "evohome"
    HOMETRONICS = "hometronics"
    PROGRAMMER = "programmer"
    SUNDIAL = "sundial"
    GENERIC = "generic"


# used by 22Fx parser, and FanSwitch devices
# SZ_BOOST_TIMER:Final = "boost_timer"  # minutes, e.g. 10, 20, 30 minutes
# HEATER_MODE: Final = "heater_mode"  # e.g. auto, off  # obsolete?
FAN_MODE: Final = "fan_mode"  # e.g. low. high   # .     deprecated, use SZ_FAN_MODE, to be removed in Q1 2026
FAN_RATE: Final = "fan_rate"  # percentage, 0.0 - 1.0  # deprecated, use SZ_FAN_MODE, to be removed in Q1 2026


# RP --- 01:054173 18:006402 --:------ 0005 004 00100000  # before adding RFG100
# .I --- 01:054173 --:------ 01:054173 1FC9 012 0010E004D39D001FC904D39D
# .W --- 30:248208 01:054173 --:------ 1FC9 012 0010E07BC9900012907BC990
# .I --- 01:054173 30:248208 --:------ 1FC9 006 00FFFF04D39D

# RP --- 01:054173 18:006402 --:------ 0005 004 00100100  # after adding RFG100
# RP --- 01:054173 18:006402 --:------ 000C 006 0010007BC990  # 30:082155
# RP --- 01:054173 18:006402 --:------ 0005 004 00100100  # before deleting RFG from CTL
# .I --- 01:054173 --:------ 01:054173 0005 004 00100000  # when the RFG was deleted
# RP --- 01:054173 18:006402 --:------ 0005 004 00100000  # after deleting the RFG

# RP|zone_devices | 000E0... || {'domain_id': 'FA', 'device_role': 'dhw_valve', 'devices': ['13:081807']}  # noqa: E501
# RP|zone_devices | 010E0... || {'domain_id': 'FA', 'device_role': 'htg_valve', 'devices': ['13:106039']}  # noqa: E501

# Example of:
#  - Sundial RF2 Pack 3: 23:(ST9420C), 07:(CS92), and 22:(DTS92(E))

# HCW80 has option of being wired (normally wireless)
# ST9420C has battery back-up (as does evohome)


# Below, verbs & codes - can use Verb/Code/Index for mypy type checking
@verify(EnumCheck.UNIQUE)
class Verb(StrEnum):
    """Protocol verbs (message types)."""

    I_ = " I"
    """Information / broadcast transmission."""
    RQ = "RQ"
    """Request / query transmission."""
    RP = "RP"
    """Response to request transmission."""
    W_ = " W"
    """Write / command transmission."""


I_: Final = Verb.I_
RQ: Final = Verb.RQ
RP: Final = Verb.RP
W_: Final = Verb.W_


@verify(EnumCheck.UNIQUE)
class MsgId(StrEnum):
    """Message identifiers."""

    _00 = "00"
    _03 = "03"
    _06 = "06"
    _01 = "01"
    _05 = "05"
    _0E = "0E"
    _0F = "0F"
    _11 = "11"
    _12 = "12"
    _13 = "13"
    _19 = "19"
    _1A = "1A"
    _1B = "1B"
    _1C = "1C"
    _30 = "30"
    _31 = "31"
    _38 = "38"
    _39 = "39"
    _71 = "71"  # unclear if supported by OTB
    _72 = "72"  # unclear if supported by OTB
    _73 = "73"
    _74 = "74"  # unclear if supported by OTB
    _75 = "75"  # unclear if supported by OTB
    _76 = "76"  # unclear if supported by OTB
    _77 = "77"  # unclear if supported by OTB
    _78 = "78"  # unclear if supported by OTB
    _79 = "79"  # unclear if supported by OTB
    _7A = "7A"  # unclear if supported by OTB
    _7B = "7B"  # unclear if supported by OTB
    _7F = "7F"


# StrEnum is intended to include all known codes, see: test suite, code schema in ramses.py
@verify(EnumCheck.UNIQUE)
class Code(StrEnum):
    """Protocol command codes."""

    _0001 = "0001"
    """RF binding / unknown."""
    _0002 = "0002"
    """Outdoor temperature sensor."""
    _0004 = "0004"
    """Zone name."""
    _0005 = "0005"
    """System zones / zone list."""
    _0006 = "0006"
    """System controller device ID / schedule version."""
    _0008 = "0008"
    """Relay demand / heat demand."""
    _0009 = "0009"
    """System fault status / fault log index."""
    _000A = "000A"
    """Zone configuration."""
    _000C = "000C"
    """Zone device binding / zone devices."""
    _000E = "000E"
    """OEM code."""
    _0016 = "0016"
    """System operating mode / heat/cool state."""
    _0100 = "0100"
    """Controller language configuration."""
    _0150 = "0150"
    """OpenTherm controller configuration."""
    _01D0 = "01D0"
    """RF binding (DHW)."""
    _01E9 = "01E9"
    """RF binding (zone sensor)."""
    _01FF = "01FF"
    """HVAC unknown configuration."""
    _0204 = "0204"
    """OpenTherm parameters."""
    _0404 = "0404"
    """Zone heating schedule."""
    _0418 = "0418"
    """System fault log entry."""
    _042F = "042F"
    """System fault log index."""
    _0B04 = "0B04"
    """RF binding (remote sensor)."""
    _1030 = "1030"
    """System synchronization cycle."""
    _1060 = "1060"
    """Domestic hot water (DHW) temperature."""
    _1081 = "1081"
    """Heating cycle parameters."""
    _1090 = "1090"
    """Boiler controller parameters."""
    _1098 = "1098"
    """OpenTherm boiler data."""
    _10A0 = "10A0"
    """Domestic hot water (DHW) parameters."""
    _10B0 = "10B0"
    """OpenTherm status."""
    _10D0 = "10D0"
    """HVAC configuration."""
    _10E0 = "10E0"
    """RF device binding (actuator)."""
    _10E1 = "10E1"
    """RF device binding (sensor)."""
    _10E2 = "10E2"
    """HVAC binding state."""
    _1100 = "1100"
    """RF binding (heating zone)."""
    _11F0 = "11F0"
    """Domestic hot water (DHW) temperature sensors."""
    _1260 = "1260"
    """Domestic hot water (DHW) measured temperature."""
    _1280 = "1280"
    """Outdoor relative humidity."""
    _1290 = "1290"
    """Outdoor temperature."""
    _1298 = "1298"
    """Carbon dioxide (CO2) concentration."""
    _12A0 = "12A0"
    """Indoor relative humidity."""
    _12B0 = "12B0"
    """Window / door contact switch state."""
    _12C0 = "12C0"
    """Indoor temperature."""
    _12C8 = "12C8"
    """Indoor air quality."""
    _12F0 = "12F0"
    """Domestic hot water (DHW) setpoint temperature."""
    _1300 = "1300"
    """Heating circuit configuration."""
    _1470 = "1470"
    """HVAC damper position."""
    _1F09 = "1F09"
    """Controller sync cycle / tick."""
    _1F41 = "1F41"
    """Domestic hot water (DHW) operating mode."""
    _1F70 = "1F70"
    """HVAC remote control state."""
    _1FC9 = "1FC9"
    """RF binding handshake."""
    _1FCA = "1FCA"
    """RF binding accept."""
    _1FD0 = "1FD0"
    """OpenTherm diagnostic data."""
    _1FD4 = "1FD4"
    """OpenTherm system status."""
    _2209 = "2209"
    """HVAC ventilation setpoint bounds."""
    _2210 = "2210"
    """Exhaust fan speed / power."""
    _2249 = "2249"
    """Current / next scheduled setpoint."""
    _22C9 = "22C9"
    """Underfloor heating (UFH) demand / setpoint bounds."""
    _22D0 = "22D0"
    """Underfloor heating (UFH) system mode."""
    _22D9 = "22D9"
    """Boiler target setpoint temperature."""
    _22E0 = "22E0"
    """HVAC fan status."""
    _22E5 = "22E5"
    """HVAC fan parameters."""
    _22E9 = "22E9"
    """HVAC fan mode."""
    _22F1 = "22F1"
    """HVAC ventilation fan mode / speed."""
    _22F2 = "22F2"
    """HVAC ventilation timer."""
    _22F3 = "22F3"
    """HVAC ventilation boost mode."""
    _22F4 = "22F4"
    """HVAC ventilation flow rate."""
    _22F7 = "22F7"
    """HVAC bypass damper mode."""
    _22F8 = "22F8"
    """HVAC filter status."""
    _22B0 = "22B0"
    """HVAC operating parameters."""
    _2309 = "2309"
    """Zone setpoint temperature."""
    _2349 = "2349"
    """Heating zone control parameters."""
    _2389 = "2389"
    """Auxiliary heating demand."""
    _2400 = "2400"
    """OpenTherm transparent slave data."""
    _2401 = "2401"
    """OpenTherm transparent master data."""
    _2410 = "2410"
    """OpenTherm fault flags."""
    _2411 = "2411"
    """HVAC device parameters."""
    _2420 = "2420"
    """OpenTherm modulation level."""
    _2D49 = "2D49"
    """Cooling relay control state."""
    _2E04 = "2E04"
    """System operating mode (legacy)."""
    _2E10 = "2E10"
    """HVAC status flags."""
    _30C9 = "30C9"
    """Zone measured temperature."""
    _3110 = "3110"
    """HVAC indoor air quality / VOC level."""
    _3120 = "3120"
    """HVAC indoor air quality."""
    _313E = "313E"
    """HVAC Zulu time offset."""
    _313F = "313F"
    """System datetime / clock synchronization."""
    _3150 = "3150"
    """Zone heat demand percentage."""
    _31D9 = "31D9"
    """HVAC bypass damper state."""
    _31DA = "31DA"
    """HVAC extended ventilation state."""
    _31E0 = "31E0"
    """HVAC ventilation demand."""
    _3200 = "3200"
    """Heating system flame / fault status."""
    _3210 = "3210"
    """OpenTherm boiler temperature."""
    _3220 = "3220"
    """OpenTherm raw message frame."""
    _3221 = "3221"
    """OpenTherm extended parameters."""
    _3222 = "3222"
    """RF binding (OpenTherm bridge)."""
    _3223 = "3223"
    """OpenTherm boiler status."""
    _3B00 = "3B00"
    """Actuator relay state / duty cycle."""
    _3EF0 = "3EF0"
    """Actuator modulation state."""
    _3EF1 = "3EF1"
    """Actuator electrical status."""
    _4401 = "4401"
    """HVAC fault log entry."""
    _4E01 = "4E01"
    """Spider HVAC temperature sensors."""
    _4E02 = "4E02"
    """Spider HVAC setpoint bounds."""
    _4E04 = "4E04"
    """Spider HVAC operating mode."""
    _4E0D = "4E0D"
    """Spider HVAC status (Autotemp)."""
    _4E14 = "4E14"
    """Spider HVAC status."""
    _4E15 = "4E15"
    """Spider HVAC status flags."""
    _4E16 = "4E16"
    """HVAC fault log status (Spider/Autotemp)."""
    _4E20 = "4E20"
    """HVAC fault status."""
    _4E21 = "4E21"
    """HVAC fault status flags."""
    _PUZZ = "7FFF"  # for internal use: not to be a RAMSES II code
    """Internal puzzle / unrecognised packet placeholder."""


# fmt: off
IndexT = Literal[
    "00", "01", "02", "03", "04", "05", "06", "07", "08", "09", "0A", "0B", "0C", "0D", "0E", "0F",
    "21",  # used by Nuaire
    "F0", "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "FA", "FB", "FC", "FD", "FE", "FF"
]
# fmt: on


LOOKUP_PUZZ = {
    "10": "engine",  # .    # version str, e.g. v0.14.0
    "11": "impersonating",  # packet header, e.g. 30C9| I|03:123001 (15 characters, packed)
    "12": "message",  # .   # message only, max len is 16 ascii characters
    "13": "message",  # .   # message only, but without a timestamp, max len 22 chars
    "20": "engine",  # .    # version str, e.g. v0.50.0, has higher-precision timestamp
    "7F": "null",  # .      # packet is null / was nullified: payload to be ignored
}  # "00" is reserved
