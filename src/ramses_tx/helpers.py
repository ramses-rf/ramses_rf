#!/usr/bin/env python3
"""RAMSES RF - Protocol/Transport layer - Helper functions."""

from __future__ import annotations

import ctypes
import sys
import time
from collections.abc import Iterable, Mapping
from datetime import date, datetime as dt
from typing import Literal, TypeAlias

# TODO: consider returning from helpers as TypeGuard[HexByte]
# fmt: off
HexByteAlt = Literal[
    '00', '01', '02', '03', '04', '05', '06', '07', '08', '09', '0A', '0B', '0C', '0D', '0E', '0F',
    '10', '11', '12', '13', '14', '15', '16', '17', '18', '19', '1A', '1B', '1C', '1D', '1E', '1F',
    '20', '21', '22', '23', '24', '25', '26', '27', '28', '29', '2A', '2B', '2C', '2D', '2E', '2F',
    '30', '31', '32', '33', '34', '35', '36', '37', '38', '39', '3A', '3B', '3C', '3D', '3E', '3F',
    '40', '41', '42', '43', '44', '45', '46', '47', '48', '49', '4A', '4B', '4C', '4D', '4E', '4F',
    '50', '51', '52', '53', '54', '55', '56', '57', '58', '59', '5A', '5B', '5C', '5D', '5E', '5F',
    '60', '61', '62', '63', '64', '65', '66', '67', '68', '69', '6A', '6B', '6C', '6D', '6E', '6F',
    '70', '71', '72', '73', '74', '75', '76', '77', '78', '79', '7A', '7B', '7C', '7D', '7E', '7F',
    '80', '81', '82', '83', '84', '85', '86', '87', '88', '89', '8A', '8B', '8C', '8D', '8E', '8F',
    '90', '91', '92', '93', '94', '95', '96', '97', '98', '99', '9A', '9B', '9C', '9D', '9E', '9F',
    'A0', 'A1', 'A2', 'A3', 'A4', 'A5', 'A6', 'A7', 'A8', 'A9', 'AA', 'AB', 'AC', 'AD', 'AE', 'AF',
    'B0', 'B1', 'B2', 'B3', 'B4', 'B5', 'B6', 'B7', 'B8', 'B9', 'BA', 'BB', 'BC', 'BD', 'BE', 'BF',
    'C0', 'C1', 'C2', 'C3', 'C4', 'C5', 'C6', 'C7', 'C8', 'C9', 'CA', 'CB', 'CC', 'CD', 'CE', 'CF',
    'D0', 'D1', 'D2', 'D3', 'D4', 'D5', 'D6', 'D7', 'D8', 'D9', 'DA', 'DB', 'DC', 'DD', 'DE', 'DF',
    'E0', 'E1', 'E2', 'E3', 'E4', 'E5', 'E6', 'E7', 'E8', 'E9', 'EA', 'EB', 'EC', 'ED', 'EE', 'EF',
    'F0', 'F1', 'F2', 'F3', 'F4', 'F5', 'F6', 'F7', 'F8', 'F9', 'FA', 'FB', 'FC', 'FD', 'FE', 'FF'
]
# fmt: on

HexByte: TypeAlias = str
HexStr2: TypeAlias = str  # two characters, one byte
HexStr4: TypeAlias = str
HexStr8: TypeAlias = str
HexStr12: TypeAlias = str
HexStr14: TypeAlias = str


ReturnValueDictT: TypeAlias = Mapping[str, float | str | None]


class _FILE_TIME(ctypes.Structure):
    """Data structure for GetSystemTimePreciseAsFileTime()."""

    _fields_ = [("dwLowDateTime", ctypes.c_uint), ("dwHighDateTime", ctypes.c_uint)]


file_time = _FILE_TIME()


def timestamp() -> float:
    """Return the number of seconds since the Unix epoch.

    This function attempts to return a high-precision value, using specific
    system calls on Windows if available.
    :return: The current timestamp in seconds.
    :rtype: float
    """

    # see: https://www.python.org/dev/peps/pep-0564/
    if sys.platform == "win32":
        # Windows uses a different epoch (1601-01-01)
        ctypes.windll.kernel32.GetSystemTimePreciseAsFileTime(ctypes.byref(file_time))
        _time = (file_time.dwLowDateTime + (file_time.dwHighDateTime << 32)) / 1e7
        return float(_time - 134774 * 24 * 60 * 60)
    else:
        # Linux/macOS uses the Unix epoch (1970-01-01)
        return time.time_ns() / 1e9


def dt_now() -> dt:
    """Get the current datetime as a local/naive datetime object.

    This is slower, but potentially more accurate, than dt.now(), and is used mainly for
    packet timestamps.

    :return: The current local datetime.
    :rtype: dt
    """
    if sys.platform == "win32":
        return dt.fromtimestamp(timestamp())
    else:
        return dt.now()


def dt_str() -> str:
    """Return the current datetime as an isoformat string."""
    return dt_now().isoformat(timespec="microseconds")


####################################################################################################


def hex_to_bool(value: HexStr2) -> bool | None:  # either False, True or None
    """Convert a 2-char hex string into a boolean."""
    if not isinstance(value, str) or len(value) != 2:
        raise ValueError(f"Invalid value: {value}, is not a 2-char hex string")
    if value == "FF":
        return None
    return {"00": False, "C8": True}[value]


def hex_from_bool(value: bool | None) -> HexStr2:  # either 00, C8 or FF
    """Convert a boolean into a 2-char hex string."""
    if value is None:
        return "FF"
    if not isinstance(value, bool):
        raise ValueError(f"Invalid value: {value}, is not bool")
    return {False: "00", True: "C8"}[value]


def hex_to_date(value: HexStr8) -> str | None:  # YY-MM-DD
    """Convert am 8-char hex string into a date, format YY-MM-DD."""
    if not isinstance(value, str) or len(value) != 8:
        raise ValueError(f"Invalid value: {value}, is not an 8-char hex string")
    if value == "FFFFFFFF":
        return None
    return dt(
        year=int(value[4:8], 16),
        month=int(value[2:4], 16),
        day=int(value[:2], 16) & 0b11111,  # 1st 3 bits: DayOfWeek
    ).strftime("%Y-%m-%d")


# FIXME: factor=1 should return an int
def hex_to_double(value: HexStr4, factor: int = 1) -> float | None:
    """Convert a 4-char hex string into a double."""
    if not isinstance(value, str) or len(value) != 4:
        raise ValueError(f"Invalid value: {value}, is not a 4-char hex string")
    if value == "7FFF":
        return None
    return int(value, 16) / factor


def hex_from_double(value: float | None, factor: int = 1) -> HexStr4:
    """Convert a double into 4-char hex string."""
    if value is None:
        return "7FFF"
    if not isinstance(value, float | int):
        raise ValueError(f"Invalid value: {value}, is not a double (a float/int)")
    return f"{int(value * factor):04X}"


def hex_to_dtm(value: HexStr12 | HexStr14) -> str | None:  # from parsers
    """Convert a 12/14-char hex string to an isoformat datetime (naive, local)."""
    #        00141B0A07E3  (...HH:MM:00)    for system_mode, zone_mode (schedules?)
    #      0400041C0A07E3  (...HH:MM:SS)    for sync_datetime

    if not isinstance(value, str) or len(value) not in (12, 14):
        raise ValueError(f"Invalid value: {value}, is not a 12/14-char hex string")
    if value[-12:] == "FF" * 6:
        return None
    if len(value) == 12:
        value = f"00{value}"
    return dt(
        year=int(value[10:14], 16),
        month=int(value[8:10], 16),
        day=int(value[6:8], 16),
        hour=int(value[4:6], 16) & 0b11111,  # 1st 3 bits: DayOfWeek
        minute=int(value[2:4], 16),
        second=int(value[:2], 16) & 0b1111111,  # 1st bit: used for DST
    ).isoformat(timespec="seconds")


def hex_from_dtm(
    dtm: date | dt | str | None, is_dst: bool = False, incl_seconds: bool = False
) -> HexStr12 | HexStr14:
    """Convert a datetime (isoformat str, or naive dtm) to a 12/14-char hex str."""

    def _dtm_to_hex(year, mon, mday, hour, min, sec, *args: int) -> str:  # type: ignore[no-untyped-def]
        return f"{sec:02X}{min:02X}{hour:02X}{mday:02X}{mon:02X}{year:04X}"

    if dtm is None:
        return "FF" * (7 if incl_seconds else 6)
    if isinstance(dtm, str):
        dtm = dt.fromisoformat(dtm)
    dtm_str = _dtm_to_hex(*dtm.timetuple())  # TODO: add DST for tm_isdst
    if is_dst:
        dtm_str = f"{int(dtm_str[:2], 16) | 0x80:02X}" + dtm_str[2:]
    return dtm_str if incl_seconds else dtm_str[2:]


def hex_to_dts(value: HexStr12) -> str | None:
    """YY-MM-DD HH:MM:SS."""
    if not isinstance(value, str) or len(value) != 12:
        raise ValueError(f"Invalid value: {value}, is not a 12-char hex string")
    if value == "00000000007F":
        return None
    _seqx = int(value, 16)
    return dt(
        year=(_seqx & 0b1111111 << 24) >> 24,
        month=(_seqx & 0b1111 << 36) >> 36,
        day=(_seqx & 0b11111 << 31) >> 31,
        hour=(_seqx & 0b11111 << 19) >> 19,
        minute=(_seqx & 0b111111 << 13) >> 13,
        second=(_seqx & 0b111111 << 7) >> 7,
    ).strftime("%y-%m-%dT%H:%M:%S")


def hex_from_dts(dtm: dt | str | None) -> HexStr12:  # TODO: WIP
    """Convert a datetime (isoformat str, or dtm) to a packed 12-char hex str."""
    """YY-MM-DD HH:MM:SS."""
    if dtm is None:
        return "00000000007F"
    if isinstance(dtm, str):
        try:
            dtm = dt.strptime(dtm, "%y-%m-%dT%H:%M:%S")
        except ValueError:
            dtm = dt.fromisoformat(dtm)  # type: ignore[arg-type]

    (tm_year, tm_mon, tm_mday, tm_hour, tm_min, tm_sec, *_) = dtm.timetuple()
    result = sum(
        (
            tm_year % 100 << 24,
            tm_mon << 36,
            tm_mday << 31,
            tm_hour << 19,
            tm_min << 13,
            tm_sec << 7,
        )
    )
    return f"{result:012X}"


def hex_to_flag8(byte: HexByte, lsb: bool = False) -> list[int]:  # TODO: use tuple
    """Split a hex str (a byte) into a list of 8 bits, MSB as first bit by default.

    If lsb==True, then the LSB is first.
    The `lsb` boolean is used so that flag[0] is `zone_idx["00"]`, etc.
    """
    if not isinstance(byte, str) or len(byte) != 2:
        raise ValueError(f"Invalid value: '{byte}', is not a 2-char hex string")
    if lsb:  # make LSB is first bit
        return list((int(byte, 16) & (1 << x)) >> x for x in range(8))
    return list((int(byte, 16) & (1 << x)) >> x for x in reversed(range(8)))


def hex_from_flag8(flags: Iterable[int], lsb: bool = False) -> HexByte:
    """Convert list of 8 bits, MSB bit 1 by default, to a two-char ASCII hex string.

    The `lsb` boolean is used so that flag[0] is `zone_idx["00"]`, etc.
    """
    if not isinstance(flags, list) or len(flags) != 8:
        raise ValueError(f"Invalid value: '{flags}', is not a list of 8 bits")
    if lsb:  # LSB is first bit
        return f"{sum(x << idx for idx, x in enumerate(flags)):02X}"
    return f"{sum(x << idx for idx, x in enumerate(reversed(flags))):02X}"


# TODO: add a wrapper for EF, & 0xF0
def hex_to_percent(
    value: HexStr2, high_res: bool = True
) -> float | None:  # c.f. valve_demand
    """Convert a 2-char hex string into a percentage.

    The range is 0-100%, with resolution of 0.5% (high_res, 00-C8) or 1% (00-64).
    """
    if not isinstance(value, str) or len(value) != 2:
        raise ValueError(f"Invalid value: {value}, is not a 2-char hex string")
    if value == "EF":  # TODO: when EF, when 7F?
        return None  # TODO: raise NotImplementedError
    if (raw_result := int(value, 16)) & 0xF0 == 0xF0:
        return None  # TODO: raise errors
    result = float(raw_result) / (200 if high_res else 100)
    if result > 1.0:  # move to outer wrapper
        raise ValueError(f"Invalid result: {result} (0x{value}) is > 1")
    return result


def hex_from_percent(value: float | None, high_res: bool = True) -> HexStr2:
    """Convert a percentage into a 2-char hex string.

    The range is 0-100%, with resolution of 0.5% (high_res, 00-C8) or 1% (00-64).
    """
    if value is None:
        return "EF"
    if not isinstance(value, float | int) or not 0 <= value <= 1:
        raise ValueError(f"Invalid value: {value}, is not a percentage")
    result = int(value * (200 if high_res else 100))
    return f"{result:02X}"


def hex_to_str(value: str) -> str:  # printable ASCII characters
    """Return a string of printable ASCII characters."""
    # result = bytearray.fromhex(value).split(b"\x7F")[0]  # TODO: needs checking
    if not isinstance(value, str):
        raise ValueError(f"Invalid value: {value}, is not a string")
    result = bytearray([x for x in bytearray.fromhex(value) if 31 < x < 127])
    return result.decode("ascii").strip() if result else ""


def hex_from_str(value: str) -> str:
    """Convert a string to a variable-length ASCII hex string."""
    if not isinstance(value, str):
        raise ValueError(f"Invalid value: {value}, is not a string")
    return "".join(f"{ord(x):02X}" for x in value)  # or: value.encode().hex()


def hex_to_temp(value: HexStr4) -> bool | float | None:  # TODO: remove bool
    """Convert a 4-byte 2's complement hex string to a float temperature ('C).

    :param value: The 4-character hex string (e.g., '07D0')
    :type value: HexStr4
    :return: The temperature in Celsius, or None if N/A
    :rtype: float | None
    :raises ValueError: If input is not a 4-char hex string or temperature is invalid.
    """
    if not isinstance(value, str) or len(value) != 4:
        raise ValueError(f"Invalid value: {value}, is not a 4-char hex string")
    if value == "31FF":  # means: N/A (== 127.99, 2s complement), signed?
        return None
    if value == "7EFF":  # possibly only for setpoints? unsigned?
        return False
    if value == "7FFF":  # also: FFFF?, means: N/A (== 327.67)
        return None
    temp: float = int(value, 16)
    temp = (temp if temp < 2**15 else temp - 2**16) / 100
    if temp < -273.15:
        raise ValueError(f"Invalid value: {temp} (0x{value}) is < -273.15")
    return temp


def hex_from_temp(value: bool | float | None) -> HexStr4:
    """Convert a float to a 2's complement 4-byte hex string."""
    if value is None:
        return "7FFF"  # or: "31FF"?
    if value is False:
        return "7EFF"
    if not isinstance(value, float | int):
        raise TypeError(f"Invalid temp: {value} is not a float")
    # if not -(2**7) <= value < 2**7:  # TODO: tighten range
    #     raise ValueError(f"Invalid temp: {value} is out of range")
    temp = int(value * 100)
    return f"{temp if temp >= 0 else temp + 2**16:04X}"


########################################################################################
