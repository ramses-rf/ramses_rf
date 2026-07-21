#!/usr/bin/env python3
"""RAMSES RF - Parser Helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Final

from ramses_rf.protocol.ramses import _31DA_FAN_INFO
from ramses_tx.address import hex_id_to_dev_id
from ramses_tx.const import (
    FAULT_DEVICE_CLASS,
    FAULT_STATE,
    FAULT_TYPE,
    SZ_AIR_QUALITY,
    SZ_AIR_QUALITY_BASIS,
    SZ_BYPASS_POSITION,
    SZ_CO2_LEVEL,
    SZ_DEVICE_CLASS,
    SZ_DEVICE_ID,
    SZ_DEWPOINT_TEMP,
    SZ_DOMAIN_IDX,
    SZ_EXHAUST_FAN_SPEED,
    SZ_EXHAUST_FLOW,
    SZ_EXHAUST_TEMP,
    SZ_FAN_INFO,
    SZ_FAULT_STATE,
    SZ_FAULT_TYPE,
    SZ_HEAT_DEMAND,
    SZ_INDOOR_HUMIDITY,
    SZ_INDOOR_TEMP,
    SZ_LOG_IDX,
    SZ_OUTDOOR_HUMIDITY,
    SZ_OUTDOOR_TEMP,
    SZ_POST_HEAT,
    SZ_PRE_HEAT,
    SZ_REL_HUMIDITY,
    SZ_REMAINING_MINS,
    SZ_SPEED_CAPABILITIES,
    SZ_SUPPLY_FAN_SPEED,
    SZ_SUPPLY_FLOW,
    SZ_SUPPLY_TEMP,
    SZ_TEMPERATURE,
    SZ_TIMESTAMP,
    FaultDeviceClass,
    FaultState,
    FaultType,
)

if TYPE_CHECKING:
    from ramses_tx.typing import PayDictT

from ramses_tx.helpers import (
    HexStr2,
    HexStr4,
    ReturnValueDictT,
    hex_to_dts,
    hex_to_temp,
)

# Sensor faults
SZ_UNRELIABLE: Final = "unreliable"
SZ_TOO_HIGH: Final = "out_of_range_high"
SZ_TOO_LOW: Final = "out_of_range_low"
# Actuator, Valve/damper faults
SZ_STUCK_VALVE: Final = "stuck_valve"  # Damper/Valve jammed
SZ_STUCK_ACTUATOR: Final = "stuck_actuator"  # Actuator jammed
# Common (to both) faults
SZ_OPEN_CIRCUIT: Final = "open_circuit"
SZ_SHORT_CIRCUIT: Final = "short_circuit"
SZ_UNAVAILABLE: Final = "unavailable"
SZ_OTHER_FAULT: Final = "other_fault"  # Non-specific fault

DEVICE_FAULT_CODES = {
    0x0: SZ_OPEN_CIRCUIT,  # NOTE: open, short
    0x1: SZ_SHORT_CIRCUIT,
    0x2: SZ_UNAVAILABLE,
    0xD: SZ_STUCK_VALVE,
    0xE: SZ_STUCK_ACTUATOR,
    0xF: SZ_OTHER_FAULT,
}
SENSOR_FAULT_CODES = {
    0x0: SZ_SHORT_CIRCUIT,  # NOTE: short, open
    0x1: SZ_OPEN_CIRCUIT,
    0x2: SZ_UNAVAILABLE,
    0x3: SZ_TOO_HIGH,
    0x4: SZ_TOO_LOW,
    0x5: SZ_UNRELIABLE,
    # 0xF: SZ_OTHER_FAULT,  # No evidence is explicitly part of the specification
}


def parse_fault_log_entry(
    payload: str,
) -> PayDictT.FAULT_LOG_ENTRY | PayDictT.FAULT_LOG_ENTRY_NULL:
    """Return the fault log entry."""

    assert len(payload) == 44

    # NOTE: the log_idx will increment as the entry moves down the log, hence '_log_idx'

    # these are only useful for I_, not RP
    if (timestamp := hex_to_dts(payload[18:30])) is None:
        return {f"_{SZ_LOG_IDX}": payload[4:6]}  # type: ignore[return-value]

    result: PayDictT.FAULT_LOG_ENTRY = {
        f"_{SZ_LOG_IDX}": payload[4:6],  # type: ignore[misc]
        SZ_TIMESTAMP: timestamp,
        SZ_FAULT_STATE: FAULT_STATE.get(payload[2:4], FaultState.UNKNOWN),
        SZ_FAULT_TYPE: FAULT_TYPE.get(payload[8:10], FaultType.UNKNOWN),
        SZ_DOMAIN_IDX: payload[10:12],
        SZ_DEVICE_CLASS: FAULT_DEVICE_CLASS.get(
            payload[12:14], FaultDeviceClass.UNKNOWN
        ),
        SZ_DEVICE_ID: hex_id_to_dev_id(payload[38:]),
        "_unknown_3": payload[6:8],  # B0 ?priority
        "_unknown_7": payload[14:18],  # 0000
        "_unknown_15": payload[30:38],  # FFFF7000/1/2
    }

    return result


def _faulted_common(param_name: str, value: str) -> dict[str, str]:
    return {f"{param_name}_fault": f"invalid_{value}"}


def _faulted_sensor(param_name: str, value: str) -> dict[str, str]:
    # assert value[:1] in ("8", "F"), value
    code = int(value[:2], 16) & 0xF
    fault = SENSOR_FAULT_CODES.get(code, f"invalid_{value}")
    return {f"{param_name}_fault": fault}


def _faulted_device(param_name: str, value: str) -> dict[str, str]:
    assert value[:1] in ("8", "F"), value
    code = int(value[:2], 16) & 0xF
    fault: str = DEVICE_FAULT_CODES.get(code, f"invalid_{value}")
    return {f"{param_name}_fault": fault}


# TODO: refactor as per 31DA parsers
def parse_valve_demand(
    value: HexStr2,
) -> dict[str, float] | dict[str, str] | dict[str, None]:
    """Convert a 2-char hex string into a percentage.

    The range is 0-100%, with resolution of 0.5% (high_res) or 1%.
    """  # for a damper (restricts flow), or a valve (permits flow)

    # TODO: remove this...
    if not isinstance(value, str) or len(value) != 2:
        raise ValueError(f"Invalid value: {value}, is not a 2-char hex string")

    if value == "EF":
        return {SZ_HEAT_DEMAND: None}  # Not Implemented

    if int(value, 16) & 0xF0 == 0xF0:
        return _faulted_device(SZ_HEAT_DEMAND, value)

    result = int(value, 16) / 200  # c.f. hex_to_percent
    if result == 1.01:  # HACK - does it mean maximum?
        result = 1.0
    elif result > 1.0:
        raise ValueError(f"Invalid result: {result} (0x{value}) is > 1")

    return {SZ_HEAT_DEMAND: result}


AIR_QUALITY_BASIS: dict[str, str] = {
    "10": "voc",  # volatile compounds
    "20": "co2",  # carbon dioxide
    "40": "rel_humidity",  # relative humidity
}


# 31DA[2:6] and 12C8[2:6]
def parse_air_quality(value: HexStr4) -> PayDictT.AIR_QUALITY:
    """Return the air quality percentage (0.0 to 1.0) and its basis.

    The basis of the air quality level should be one of: VOC, CO2 or relative humidity.
    If air_quality is EF, air_quality_basis should be 00.

    The sensor value is None if there is no sensor present (is not an error).
    The dict does not include the key if there is a sensor fault.

    :param value: The 4-character hex string encoding quality and basis
    :type value: HexStr4
    :return: A dictionary containing the air quality and its basis (e.g., CO2, VOC)
    :rtype: PayDictT.AIR_QUALITY
    """  # VOC: Volatile organic compounds

    # TODO: remove this as API used only internally...
    if not isinstance(value, str) or len(value) != 4:
        raise ValueError(f"Invalid value: {value}, is not a 4-char hex string")

    assert value[:2] != "EF" or value[2:] == "00", value  # TODO: raise exception
    if value == "EF00":  # Not implemented
        return {SZ_AIR_QUALITY: None}

    if int(value[:2], 16) & 0xF0 == 0xF0:
        return _faulted_sensor(SZ_AIR_QUALITY, value)  # type: ignore[return-value]

    level = int(value[:2], 16) / 200  # was: hex_to_percent(value[:2], True)
    assert level <= 1.0, value[:2]  # TODO: raise exception

    assert value[2:] in ("10", "20", "40"), value[2:]  # TODO: remove assert

    basis: str = AIR_QUALITY_BASIS.get(
        value[2:], f"unknown_{value[2:]}"
    )  # TODO: remove get/unknown

    return {SZ_AIR_QUALITY: level, SZ_AIR_QUALITY_BASIS: basis}


def air_quality_code(desc: str) -> str:
    for k, v in AIR_QUALITY_BASIS.items():
        if v == desc:
            return k
    return "00"


# 31DA[6:10] and 1298[2:6]
def parse_co2_level(value: HexStr4) -> PayDictT.CO2_LEVEL:
    """Return the co2 level (ppm).

    The sensor value is None if there is no sensor present (is not an error).
    The dict does not include the key if there is a sensor fault.
    """

    # TODO: remove this...
    if not isinstance(value, str) or len(value) != 4:
        raise ValueError(f"Invalid value: {value}, is not a 4-char hex string")

    if value == "7FFF":  # Not implemented
        return {SZ_CO2_LEVEL: None}

    level = int(value, 16)  # was: hex_to_double(value)  # is it 2's complement?

    if int(value[:2], 16) & 0x80 or level >= 0x8000:
        return _faulted_sensor(SZ_CO2_LEVEL, value)  # type: ignore[return-value]

    # assert int(value[:2], 16) <= 0x8000, value
    return {SZ_CO2_LEVEL: level}


def parse_humidity_element(value: str, index: str) -> PayDictT._12A0:
    """Return the relative humidity (%) and 2 temperatures

    The result may include current temperature ('C) and include dewpoint temperature ('C).
    """
    if index == "01":
        return _parse_hvac_humidity(SZ_REL_HUMIDITY, value[:2], value[2:6], value[6:10])  # type: ignore[return-value]
    if index == "02":
        return _parse_hvac_humidity(
            SZ_OUTDOOR_HUMIDITY, value[:2], value[2:6], value[6:10]
        )  # type: ignore[return-value]
    return _parse_hvac_humidity(SZ_INDOOR_HUMIDITY, value[:2], value[2:6], value[6:10])  # type: ignore[return-value]


# 31DA[10:12] and 12A0[2:12]
def parse_indoor_humidity(value: str) -> PayDictT.INDOOR_HUMIDITY:
    """Return the relative indoor humidity (%).

    The result may include current temperature ('C), and dewpoint temperature ('C).
    """
    return _parse_hvac_humidity(SZ_INDOOR_HUMIDITY, value[:2], value[2:6], value[6:10])  # type: ignore[return-value]


# 31DA[12:14] and 1280[2:12]
def parse_outdoor_humidity(value: str) -> PayDictT.OUTDOOR_HUMIDITY:
    """Return the relative outdoor humidity (%).

    The result may include current temperature ('C), and dewpoint temperature ('C).
    """
    return _parse_hvac_humidity(SZ_OUTDOOR_HUMIDITY, value[:2], value[2:6], value[6:10])  # type: ignore[return-value]


def _parse_hvac_humidity(
    param_name: str, value: HexStr2, temp: HexStr4, dewpoint: HexStr4
) -> ReturnValueDictT:
    """Return the relative humidity, etc. (called by sensor parsers).

    The sensor value is None if there is no sensor present (is not an error).
    The dict does not include the key if there is a sensor fault.
    """

    # TODO: remove this...
    if not isinstance(value, str) or len(value) != 2:
        raise ValueError(f"Invalid value: {value}, is not a 2-char hex string")
    if not isinstance(temp, str) or len(temp) not in (0, 4):
        raise ValueError(f"Invalid temp: {temp}, is not a 4-char hex string")
    if not isinstance(dewpoint, str) or len(dewpoint) not in (0, 4):
        raise ValueError(f"Invalid dewpoint: {dewpoint}, is not a 4-char hex string")

    if value == "EF":  # Not implemented
        return {param_name: None}

    if int(value, 16) & 0xF0 == 0xF0:
        return _faulted_sensor(param_name, value)

    percentage = int(value[:2], 16) / 100
    if percentage > 1.0:  # seen regularly, unknown meaning
        return _faulted_common(param_name, value)

    result: dict[str, float | str | None] = {param_name: percentage}
    if temp:
        result |= {SZ_TEMPERATURE: hex_to_temp(temp)}
    if dewpoint:
        result |= {SZ_DEWPOINT_TEMP: hex_to_temp(dewpoint)}
    return result


# 31DA[14:18]
def parse_exhaust_temp(value: HexStr4) -> PayDictT.EXHAUST_TEMP:
    """Return the exhaust temperature ('C)."""
    return _parse_hvac_temp(SZ_EXHAUST_TEMP, value)  # type: ignore[return-value]


# 31DA[18:22]
def parse_supply_temp(value: HexStr4) -> PayDictT.SUPPLY_TEMP:
    """Return the supply temperature ('C)."""
    return _parse_hvac_temp(SZ_SUPPLY_TEMP, value)  # type: ignore[return-value]


# 31DA[22:26]
def parse_indoor_temp(value: HexStr4) -> PayDictT.INDOOR_TEMP:
    """Return the indoor temperature ('C)."""
    return _parse_hvac_temp(SZ_INDOOR_TEMP, value)  # type: ignore[return-value]


# 31DA[26:30] & 1290[2:6]?
def parse_outdoor_temp(value: HexStr4) -> PayDictT.OUTDOOR_TEMP:
    """Return the outdoor temperature ('C)."""
    return _parse_hvac_temp(SZ_OUTDOOR_TEMP, value)  # type: ignore[return-value]


def _parse_hvac_temp(param_name: str, value: HexStr4) -> Mapping[str, float | None]:
    """Return the temperature ('C) (called by sensor parsers).

    The sensor value is None if there is no sensor present (is not an error).
    The dict does not include the key if there is a sensor fault.
    """

    # TODO: remove this...
    if not isinstance(value, str) or len(value) != 4:
        raise ValueError(f"Invalid value: {value}, is not a 4-char hex string")

    if value == "7FFF":  # Not implemented
        return {param_name: None}
    if value == "31FF":  # Other
        return {param_name: None}

    if int(value[:2], 16) & 0xF0 == 0x80:  # or temperature < -273.15:
        return _faulted_sensor(param_name, value)  # type: ignore[return-value]

    temp: float = int(value, 16)
    temp = (temp if temp < 2**15 else temp - 2**16) / 100
    if temp <= -273:  # TODO: < 273.15?
        return _faulted_sensor(param_name, value)  # type: ignore[return-value]

    return {param_name: temp}


ABILITIES = {
    15: "off",
    14: "low_med_high",  # 3,2,1 = high,med,low?
    13: "timer",
    12: "boost",
    11: "auto",
    10: "speed_4",
    9: "speed_5",
    8: "speed_6",
    7: "speed_7",
    6: "speed_8",
    5: "speed_9",
    4: "speed_10",
    3: "auto_night",
    2: "reserved",
    1: "post_heater",
    0: "pre_heater",
}


# 31DA[30:34]
def parse_capabilities(value: HexStr4) -> PayDictT.CAPABILITIES:
    """Return the speed capabilities (a bitmask).

    The sensor value is None if there is no sensor present (is not an error).
    The dict does not include the key if there is a sensor fault.
    """

    # TODO: remove this...
    if not isinstance(value, str) or len(value) != 4:
        raise ValueError(f"Invalid value: {value}, is not a 4-char hex string")

    if value == "7FFF":  # TODO: Not implemented???
        return {SZ_SPEED_CAPABILITIES: None}

    # assert value in ("0002", "4000", "4808", "F000", "F001", "F800", "F808"), value

    return {
        SZ_SPEED_CAPABILITIES: [
            v for k, v in ABILITIES.items() if int(value, 16) & 2**k
        ]
    }


def capability_bits(cap_list: list[str]) -> int:
    # 0xF800 = 0b1111100000000000
    cap_res: int = 0
    for cap in cap_list:
        for k, v in ABILITIES.items():
            if v == cap:
                cap_res |= 2**k  # set bit
    return cap_res


# 31DA[34:36]
def parse_bypass_position(value: HexStr2) -> PayDictT.BYPASS_POSITION:
    """Return the bypass position (%), usually fully open or closed (0%, no bypass).

    The sensor value is None if there is no sensor present (is not an error).
    The dict does not include the key if there is a sensor fault.
    """

    # TODO: remove this...
    if not isinstance(value, str) or len(value) != 2:
        raise ValueError(f"Invalid value: {value}, is not a 2-char hex string")

    if value == "EF":  # Not implemented
        return {SZ_BYPASS_POSITION: None}

    if int(value[:2], 16) & 0xF0 == 0xF0:
        return _faulted_device(SZ_BYPASS_POSITION, value)  # type: ignore[return-value]

    bypass_pos = int(value, 16) / 200  # was: hex_to_percent(value)
    assert bypass_pos <= 1.0, value

    return {SZ_BYPASS_POSITION: bypass_pos}


# 31DA[36:38]  # TODO: WIP (3 more bits), also 22F3 and 22F4?
def parse_fan_info(value: HexStr2) -> PayDictT.FAN_INFO:
    """Return the fan state (lookup table for current speed and mode).

    The sensor value is None if there is no sensor present (is not an error).
    The dict does not include the key if there is a sensor fault.
    """

    # TODO: remove this...
    if not isinstance(value, str) or len(value) != 2:
        raise ValueError(f"Invalid value: {value}, is not a 2-char hex string")

    # TODO: Not implemented???  # EF, FF = no data / not implemented
    if value in ("EF", "FF"):
        return {
            SZ_FAN_INFO: None,
            "_unknown_fan_info_flags": [0, 0, 0],
        }

    if int(value, 16) & 0xE0 not in (0x00, 0x20, 0x40, 0x60, 0x80):
        # Unknown fan_info code (e.g. Ventura 0x1F) — return as unknown
        # instead of crashing with AssertionError.  The quirks layer
        # will prevent it from overwriting a valid fan_info from 22F1/22F4.
        return {
            SZ_FAN_INFO: f"-unknown 0x{value}-",
            "_unknown_fan_info_flags": [
                (int(value, 16) >> x) & 1 for x in range(7, 4, -1)
            ],
        }

    flags = list((int(value, 16) & (1 << x)) >> x for x in range(7, 4, -1))

    return {
        SZ_FAN_INFO: _31DA_FAN_INFO[
            int(value, 16) & 0x1F
        ],  # lookup description from code
        "_unknown_fan_info_flags": flags,
    }


def fan_info_to_byte(info: str) -> int:
    for k, v in _31DA_FAN_INFO.items():
        if v == info:
            return int(k) & 0x1F
    return 0x0000


def fan_info_flags(flags_list: list[int]) -> int:
    flag_res: int = 0
    for index, shift in enumerate(range(7, 4, -1)):  # index = 7, 6 and 5
        if flags_list[index] == 1:
            flag_res |= 1 << shift  # set bits
    return flag_res


# 31DA[38:40], also 2210
def parse_exhaust_fan_speed(value: HexStr2) -> PayDictT.EXHAUST_FAN_SPEED:
    """Return the exhaust fan speed (% of max speed)."""
    return _parse_fan_speed(SZ_EXHAUST_FAN_SPEED, value)  # type: ignore[return-value]


# 31DA[40:42]
def parse_supply_fan_speed(value: HexStr2) -> PayDictT.SUPPLY_FAN_SPEED:
    """Return the supply fan speed (% of max speed)."""
    return _parse_fan_speed(SZ_SUPPLY_FAN_SPEED, value)  # type: ignore[return-value]


def _parse_fan_speed(param_name: str, value: HexStr2) -> Mapping[str, float | None]:
    """Return the fan speed (called by sensor parsers).

    The sensor value is None if there is no sensor present (is not an error).
    The dict does not include the key if there is a sensor fault.
    """

    # TODO: remove this...
    if not isinstance(value, str) or len(value) != 2:
        raise ValueError(f"Invalid value: {value}, is not a 2-char hex string")

    if value == "FF":  # Not implemented (is definitely FF, not EF!)
        return {param_name: None}

    percentage = int(value, 16) / 200  # was: hex_to_percent(value, True)
    if percentage > 1.0:
        return _faulted_common(param_name, value)  # type: ignore[return-value]

    return {param_name: percentage}


# 31DA[42:46] & 22F3[2:6]  # TODO: make 22F3-friendly
def parse_remaining_mins(value: HexStr4) -> PayDictT.REMAINING_MINUTES:
    """Return the remaining time for temporary modes (whole minutes).

    The sensor value is None if there is no sensor present (is not an error).
    The dict does not include the key if there is a sensor fault.
    """

    # TODO: remove this...
    if not isinstance(value, str) or len(value) != 4:
        raise ValueError(f"Invalid value: {value}, is not a 4-char hex string")

    if value == "0000":
        return {SZ_REMAINING_MINS: 0}
    if value == "3FFF":
        return {SZ_REMAINING_MINS: None}

    minutes = int(value, 16)  # was: hex_to_double(value)
    assert minutes > 0, value  # TODO: raise assert

    return {SZ_REMAINING_MINS: minutes}  # usu. 0-60 mins


# 31DA[46:48]
def parse_post_heater(value: HexStr2) -> PayDictT.POST_HEATER:
    """Return the post-heater state (% of max heat)."""
    return _parse_fan_heater(SZ_POST_HEAT, value)  # type: ignore[return-value]


# 31DA[48:50]
def parse_pre_heater(value: HexStr2) -> PayDictT.PRE_HEATER:
    """Return the pre-heater state (% of max heat)."""
    return _parse_fan_heater(SZ_PRE_HEAT, value)  # type: ignore[return-value]


def _parse_fan_heater(param_name: str, value: HexStr2) -> Mapping[str, float | None]:
    """Return the heater state (called by sensor parsers).

    The sensor value is None if there is no sensor present (is not an error).
    The dict does not include the key if there is a sensor fault.
    """

    # TODO: remove this...
    if not isinstance(value, str) or len(value) != 2:
        raise ValueError(f"Invalid value: {value}, is not a 2-char hex string")

    if value == "EF":  # Not implemented
        return {param_name: None}

    if int(value, 16) & 0xF0 == 0xF0:
        return _faulted_sensor(param_name, value)  # type: ignore[return-value]

    percentage = int(value, 16) / 200  # Siber DF EVO 2 is /200, not /100 (Others?)
    assert percentage <= 1.0, value  # TODO: raise exception if > 1.0?

    return {param_name: percentage}  # was: percent_from_hex(value, high_res=False)


# 31DA[50:54]
def parse_supply_flow(value: HexStr4) -> PayDictT.SUPPLY_FLOW:
    """Return the supply flow rate in m^3/hr (Orcon) ?or L/sec (?Itho)."""
    return _parse_fan_flow(SZ_SUPPLY_FLOW, value)  # type: ignore[return-value]


# 31DA[54:58]
def parse_exhaust_flow(value: HexStr4) -> PayDictT.EXHAUST_FLOW:
    """Return the exhaust flow rate in m^3/hr (Orcon) ?or L/sec (?Itho)"""
    return _parse_fan_flow(SZ_EXHAUST_FLOW, value)  # type: ignore[return-value]


def _parse_fan_flow(param_name: str, value: HexStr4) -> Mapping[str, float | None]:
    """Return the air flow rate (called by sensor parsers).

    The sensor value is None if there is no sensor present (is not an error).
    The dict does not include the key if there is a sensor fault.
    """

    # TODO: remove this...
    if not isinstance(value, str) or len(value) != 4:
        raise ValueError(f"Invalid value: {value}, is not a 4-char hex string")

    if value == "7FFF":  # Not implemented
        return {param_name: None}

    if int(value[:2], 16) & 0x80:
        return _faulted_sensor(param_name, value)  # type: ignore[return-value]

    flow = int(value, 16) / 100  # was: hex_to_double(value, factor=100)
    assert flow >= 0, value  # TODO: raise exception if < 0?

    return {param_name: flow}
