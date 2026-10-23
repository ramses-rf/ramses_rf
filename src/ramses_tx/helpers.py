#!/usr/bin/env python3
"""RAMSES RF - Protocol/Transport layer - Helper functions."""

from __future__ import annotations

import ctypes
import sys
import time
from collections.abc import Iterable, Mapping
from datetime import date, datetime as dt
from typing import Literal, TypeGuard, overload


def is_hex_byte(value: object) -> TypeGuard[HexByte]:
    """Return True if value is a 2-character hex string (1 byte).

    :param value: Value to validate.
    :type value: object
    :returns: True if value is a 2-character hex string.
    :rtype: TypeGuard[HexByte]
    """
    return (
        isinstance(value, str)
        and len(value) == 2
        and all(c in "0123456789abcdefABCDEF" for c in value)
    )


def is_hex_str4(value: object) -> TypeGuard[HexStr4]:
    """Return True if value is a 4-character hex string (2 bytes).

    :param value: Value to validate.
    :type value: object
    :returns: True if value is a 4-character hex string.
    :rtype: TypeGuard[HexStr4]
    """
    return (
        isinstance(value, str)
        and len(value) == 4
        and all(c in "0123456789abcdefABCDEF" for c in value)
    )


def is_hex_str8(value: object) -> TypeGuard[HexStr8]:
    """Return True if value is an 8-character hex string (4 bytes).

    :param value: Value to validate.
    :type value: object
    :returns: True if value is an 8-character hex string.
    :rtype: TypeGuard[HexStr8]
    """
    return (
        isinstance(value, str)
        and len(value) == 8
        and all(c in "0123456789abcdefABCDEF" for c in value)
    )


def is_hex_str12(value: object) -> TypeGuard[HexStr12]:
    """Return True if value is a 12-character hex string (6 bytes).

    :param value: Value to validate.
    :type value: object
    :returns: True if value is a 12-character hex string.
    :rtype: TypeGuard[HexStr12]
    """
    return (
        isinstance(value, str)
        and len(value) == 12
        and all(c in "0123456789abcdefABCDEF" for c in value)
    )


# fmt: off
HexByteAlt = Literal[
    "00", "01", "02", "03", "04", "05", "06", "07", "08", "09", "0A", "0B", "0C", "0D", "0E", "0F",
    "10", "11", "12", "13", "14", "15", "16", "17", "18", "19", "1A", "1B", "1C", "1D", "1E", "1F",
    "20", "21", "22", "23", "24", "25", "26", "27", "28", "29", "2A", "2B", "2C", "2D", "2E", "2F",
    "30", "31", "32", "33", "34", "35", "36", "37", "38", "39", "3A", "3B", "3C", "3D", "3E", "3F",
    "40", "41", "42", "43", "44", "45", "46", "47", "48", "49", "4A", "4B", "4C", "4D", "4E", "4F",
    "50", "51", "52", "53", "54", "55", "56", "57", "58", "59", "5A", "5B", "5C", "5D", "5E", "5F",
    "60", "61", "62", "63", "64", "65", "66", "67", "68", "69", "6A", "6B", "6C", "6D", "6E", "6F",
    "70", "71", "72", "73", "74", "75", "76", "77", "78", "79", "7A", "7B", "7C", "7D", "7E", "7F",
    "80", "81", "82", "83", "84", "85", "86", "87", "88", "89", "8A", "8B", "8C", "8D", "8E", "8F",
    "90", "91", "92", "93", "94", "95", "96", "97", "98", "99", "9A", "9B", "9C", "9D", "9E", "9F",
    "A0", "A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8", "A9", "AA", "AB", "AC", "AD", "AE", "AF",
    "B0", "B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8", "B9", "BA", "BB", "BC", "BD", "BE", "BF",
    "C0", "C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8", "C9", "CA", "CB", "CC", "CD", "CE", "CF",
    "D0", "D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8", "D9", "DA", "DB", "DC", "DD", "DE", "DF",
    "E0", "E1", "E2", "E3", "E4", "E5", "E6", "E7", "E8", "E9", "EA", "EB", "EC", "ED", "EE", "EF",
    "F0", "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "FA", "FB", "FC", "FD", "FE", "FF"
]
# fmt: on

type HexByte = str
type HexStr2 = str  # two characters, one byte
type HexStr4 = str
type HexStr8 = str
type HexStr12 = str
type HexStr14 = str


type ReturnValueDictT = Mapping[str, float | str | None]


class _FILE_TIME(ctypes.Structure):
    """Data structure for GetSystemTimePreciseAsFileTime()."""

    _fields_ = [
        ("dwLowDateTime", ctypes.c_uint),
        ("dwHighDateTime", ctypes.c_uint),
    ]


file_time = _FILE_TIME()


def timestamp() -> float:
    """Return the number of seconds since the Unix epoch.

    This function attempts to return a high-precision value, using specific
    system calls on Windows if available.

    :returns: The current timestamp in seconds.
    :rtype: float
    """
    # see: https://www.python.org/dev/peps/pep-0564/
    if sys.platform == "win32":
        # Windows uses a different epoch (1601-01-01)
        ctypes.windll.kernel32.GetSystemTimePreciseAsFileTime(
            ctypes.byref(file_time)
        )
        _time = (
            file_time.dwLowDateTime + (file_time.dwHighDateTime << 32)
        ) / 1e7
        return float(_time - 134774 * 24 * 60 * 60)
    else:
        # Linux/macOS uses the Unix epoch (1970-01-01)
        return time.time_ns() / 1e9


def dt_now() -> dt:
    """Get the current datetime as a local/naive datetime object.

    This is slower, but potentially more accurate, than dt.now(), and is
    used mainly for packet timestamps.

    :returns: The current local datetime.
    :rtype: dt
    """
    if sys.platform == "win32":
        return dt.fromtimestamp(timestamp())
    else:
        return dt.now()


def dt_str() -> str:
    """Return the current datetime as an isoformat string.

    :returns: Current datetime formatted as ISO 8601 string.
    :rtype: str
    """
    return dt_now().isoformat(timespec="microseconds")


####################################################################################################


def hex_to_bool(value: HexStr2) -> bool | None:  # either False, True or None
    """Convert a 2-char hex string into a boolean.

    :param value: The 2-character hex string ('00', 'C8', or 'FF').
    :type value: HexStr2
    :returns: False for '00', True for 'C8', None for 'FF'.
    :rtype: bool | None
    :raises ValueError: If input is not a valid 2-character hex string.
    """
    if not isinstance(value, str) or len(value) != 2:
        raise ValueError(f"Invalid value: {value}, is not a 2-char hex string")
    if value == "FF":
        return None
    return {"00": False, "C8": True}[value]


def hex_from_bool(value: bool | None) -> HexStr2:  # either 00, C8 or FF
    """Convert a boolean into a 2-char hex string.

    :param value: Boolean value or None.
    :type value: bool | None
    :returns: '00' for False, 'C8' for True, 'FF' for None.
    :rtype: HexStr2
    :raises ValueError: If input is not a bool or None.
    """
    if value is None:
        return "FF"
    if not isinstance(value, bool):
        raise ValueError(f"Invalid value: {value}, is not bool")
    return {False: "00", True: "C8"}[value]


def hex_to_date(value: HexStr8) -> str | None:  # YY-MM-DD
    """Convert an 8-char hex string into a date, format YY-MM-DD.

    :param value: The 8-character hex string.
    :type value: HexStr8
    :returns: Formatted date string (YYYY-MM-DD), or None if 'FFFFFFFF'.
    :rtype: str | None
    :raises ValueError: If input is not an 8-character hex string.
    """
    if not isinstance(value, str) or len(value) != 8:
        raise ValueError(
            f"Invalid value: {value}, is not an 8-char hex string"
        )
    if value == "FFFFFFFF":
        return None
    return dt(
        year=int(value[4:8], 16),
        month=int(value[2:4], 16),
        day=int(value[:2], 16) & 0b11111,  # 1st 3 bits: DayOfWeek
    ).strftime("%Y-%m-%d")


@overload
def hex_to_double(value: HexStr4, factor: int) -> float | None: ...
@overload
def hex_to_double(value: HexStr4, factor: Literal[1] = 1) -> int | None: ...
def hex_to_double(value: HexStr4, factor: int = 1) -> int | float | None:
    """Convert a 4-char hex string into a double or int value.

    :param value: The 4-character hex string.
    :type value: HexStr4
    :param factor: Scaling divisor factor, defaults to 1.
    :type factor: int
    :returns: Scaled numeric value, or None if '7FFF'.
    :rtype: int | float | None
    :raises ValueError: If input is not a 4-character hex string.
    """
    if not isinstance(value, str) or len(value) != 4:
        raise ValueError(f"Invalid value: {value}, is not a 4-char hex string")
    if value == "7FFF":
        return None
    raw_val = int(value, 16)
    if factor == 1:
        return raw_val
    return raw_val / factor


def hex_from_double(value: float | None, factor: int = 1) -> HexStr4:
    """Convert a double into 4-char hex string.

    :param value: Numeric value or None.
    :type value: float | int | None
    :param factor: Scaling multiplier factor, defaults to 1.
    :type factor: int
    :returns: 4-character hex string, or '7FFF' if None.
    :rtype: HexStr4
    :raises ValueError: If input is not a float, int, or None.
    """
    if value is None:
        return "7FFF"
    if not isinstance(value, float | int):
        raise ValueError(
            f"Invalid value: {value}, is not a double (a float/int)"
        )
    return f"{int(value * factor):04X}"


def hex_to_dtm(value: HexStr12 | HexStr14) -> str | None:  # from parsers
    """Convert a 12/14-char hex string to an isoformat datetime (naive, local).

    :param value: 12 or 14-character hex string.
    :type value: HexStr12 | HexStr14
    :returns: ISO 8601 formatted datetime string, or None if empty.
    :rtype: str | None
    :raises ValueError: If input is not a 12 or 14-character hex string.
    """
    #        00141B0A07E3  (...HH:MM:00)    for system_mode, zone_mode (schedules?)
    #      0400041C0A07E3  (...HH:MM:SS)    for sync_datetime

    if not isinstance(value, str) or len(value) not in (12, 14):
        raise ValueError(
            f"Invalid value: {value}, is not a 12/14-char hex string"
        )
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
    dtm: date | dt | str | None,
    is_daylight_saving: bool = False,
    incl_seconds: bool = False,
) -> HexStr12 | HexStr14:
    """Convert a datetime to a 12/14-character hex string.

    :param dtm: The datetime object, date, ISO format string, or None.
    :type dtm: date | dt | str | None
    :param is_daylight_saving: Explicitly set Daylight Saving Time flag, defaults to False.
    :type is_daylight_saving: bool
    :param incl_seconds: Include seconds byte (14-char output), defaults to False.
    :type incl_seconds: bool
    :returns: The 12 or 14 character hex string representation.
    :rtype: HexStr12 | HexStr14
    """

    def _dtm_to_hex(
        year: int,
        month: int,
        mday: int,
        hour: int,
        minute: int,
        second: int,
        *args: int,
    ) -> str:
        return f"{second:02X}{minute:02X}{hour:02X}{mday:02X}{month:02X}{year:04X}"

    if dtm is None:
        return "FF" * (7 if incl_seconds else 6)
    if isinstance(dtm, str):
        dtm = dt.fromisoformat(dtm)
    t_tuple = dtm.timetuple()
    dtm_str = _dtm_to_hex(*t_tuple)
    if is_daylight_saving or t_tuple[8] > 0:
        dtm_str = f"{int(dtm_str[:2], 16) | 0x80:02X}" + dtm_str[2:]
    return dtm_str if incl_seconds else dtm_str[2:]


def hex_to_dts(value: HexStr12) -> str | None:
    """Convert a packed 12-char hex string to YY-MM-DD HH:MM:SS.

    :param value: The 12-character hex string.
    :type value: HexStr12
    :returns: The formatted datetime string, or None if empty.
    :rtype: str | None
    :raises ValueError: If input is not a 12-character hex string.
    """
    if not isinstance(value, str) or len(value) != 12:
        raise ValueError(
            f"Invalid value: {value}, is not a 12-char hex string"
        )
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


def hex_from_dts(dtm: dt | str | None) -> HexStr12:
    """Convert a datetime (isoformat str, or dtm) to a packed 12-char hex str.

    Format: YY-MM-DD HH:MM:SS.

    :param dtm: The datetime object, ISO format string, or None.
    :type dtm: dt | str | None
    :returns: A packed 12-character hex string.
    :rtype: HexStr12
    """
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


def hex_to_flag8(byte: HexByte, lsb: bool = False) -> list[int]:
    """Split a hex byte string into a list of 8 bits.

    If lsb=True, the LSB is first in the list.

    :param byte: 2-character hex byte string.
    :type byte: HexByte
    :param lsb: Order bits with LSB first if True, defaults to False.
    :type lsb: bool
    :returns: List of 8 bit integers (0 or 1).
    :rtype: list[int]
    :raises ValueError: If input is not a 2-character hex string.
    """
    if not isinstance(byte, str) or len(byte) != 2:
        raise ValueError(
            f"Invalid value: '{byte}', is not a 2-char hex string"
        )
    if lsb:  # make LSB is first bit
        return list((int(byte, 16) & (1 << x)) >> x for x in range(8))
    return list((int(byte, 16) & (1 << x)) >> x for x in reversed(range(8)))


def hex_from_flag8(flags: Iterable[int], lsb: bool = False) -> HexByte:
    """Convert a sequence of 8 bits into a 2-char ASCII hex string.

    :param flags: Iterable sequence of 8 bit integers (0 or 1).
    :type flags: Iterable[int]
    :param lsb: Order bits with LSB first if True, defaults to False.
    :type lsb: bool
    :returns: 2-character hex string.
    :rtype: HexByte
    :raises ValueError: If flags is not a list or tuple of 8 bits.
    """
    if not isinstance(flags, list | tuple) or len(flags) != 8:
        raise ValueError(
            f"Invalid value: '{flags}', is not a list/tuple of 8 bits"
        )
    if lsb:  # LSB is first bit
        return (
            f"{sum(x << bit_index for bit_index, x in enumerate(flags)):02X}"
        )
    return f"{sum(x << bit_index for bit_index, x in enumerate(reversed(flags))):02X}"


# TODO: add a wrapper for EF, & 0xF0
def hex_to_percent(
    value: HexStr2, high_res: bool = True
) -> float | None:  # c.f. valve_demand
    """Convert a 2-char hex string into a percentage float (0.0 to 1.0).

    Range is 0-100% with resolution of 0.5% (high_res, 00-C8) or 1% (00-64).

    :param value: 2-character hex string.
    :type value: HexStr2
    :param high_res: Use 0.5% resolution if True, defaults to True.
    :type high_res: bool
    :returns: Float percentage between 0.0 and 1.0, or None if EF/sentinel.
    :rtype: float | None
    :raises ValueError: If value is invalid or > 1.0.
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
    """Convert a percentage float (0.0 to 1.0) into a 2-char hex string.

    :param value: Percentage float between 0.0 and 1.0, or None.
    :type value: float | int | None
    :param high_res: Use 0.5% resolution if True, defaults to True.
    :type high_res: bool
    :returns: 2-character hex string, or 'EF' if None.
    :rtype: HexStr2
    :raises ValueError: If value is out of range 0.0 to 1.0.
    """
    if value is None:
        return "EF"
    if not isinstance(value, float | int) or not 0 <= value <= 1:
        raise ValueError(f"Invalid value: {value}, is not a percentage")
    result = int(value * (200 if high_res else 100))
    return f"{result:02X}"


def hex_to_str(value: str) -> str:
    """Return a string of printable ASCII characters from a hex string.

    :param value: Variable-length hex string.
    :type value: str
    :returns: Filtered printable ASCII string.
    :rtype: str
    :raises ValueError: If value is not a string.
    """
    if not isinstance(value, str):
        raise ValueError(f"Invalid value: {value}, is not a string")
    result = bytearray([x for x in bytearray.fromhex(value) if 31 < x < 127])
    return result.decode("ascii").strip() if result else ""


def hex_from_str(value: str) -> str:
    """Convert an ASCII string to a variable-length hex string.

    :param value: ASCII text string.
    :type value: str
    :returns: Hex-encoded string.
    :rtype: str
    :raises ValueError: If value is not a string.
    """
    if not isinstance(value, str):
        raise ValueError(f"Invalid value: {value}, is not a string")
    return "".join(f"{ord(x):02X}" for x in value)  # or: value.encode().hex()


def hex_to_temp(value: HexStr4) -> float | Literal[False] | None:
    """Convert a 4-byte 2's complement hex string to a float temperature ('C).

    :param value: The 4-character hex string (e.g., '07D0')
    :type value: HexStr4
    :returns: Temperature in Celsius, False if disabled (0x7EFF), None if N/A.
    :rtype: float | Literal[False] | None
    :raises ValueError: If input is not a 4-char hex string or temp invalid.
    """
    if not is_hex_str4(value):
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


def hex_from_temp(value: bool | float | int | None) -> HexStr4:
    """Convert a temperature float or bool into a 4-char hex string.

    :param value: Temperature float ('C), False if disabled, or None.
    :type value: bool | float | int | None
    :returns: 4-character hex string ('7FFF' for None, '7EFF' for False).
    :rtype: HexStr4
    :raises TypeError: If value is not a float, int, bool, or None.
    """
    if value is None:
        return "7FFF"  # or: "31FF"?
    if value is False:
        return "7EFF"
    if isinstance(value, bool) or not isinstance(value, float | int):
        raise TypeError(f"Invalid temp: {value} is not a float or int")
    temp = int(value * 100)
    return f"{temp if temp >= 0 else temp + 2**16:04X}"


########################################################################################
