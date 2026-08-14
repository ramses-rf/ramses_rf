"""RAMSES RF - HVAC and Ventilation payload dataclasses.

This module contains strongly-typed dataclass representations for HVAC,
ventilation, air quality, and fan status packet payloads.
"""

import logging
import struct
from dataclasses import dataclass
from datetime import timedelta as td
from typing import Any, ClassVar, Self

from ramses_rf.const import (
    SZ_AIR_QUALITY,
    SZ_AIR_QUALITY_BASIS,
    SZ_BYPASS_POSITION,
    SZ_CO2_LEVEL,
    SZ_DEWPOINT_TEMP,
    SZ_EXHAUST_FAN_SPEED,
    SZ_EXHAUST_FLOW,
    SZ_EXHAUST_TEMP,
    SZ_FAN_INFO,
    SZ_INDOOR_HUMIDITY,
    SZ_INDOOR_TEMP,
    SZ_OUTDOOR_HUMIDITY,
    SZ_OUTDOOR_TEMP,
    SZ_POST_HEAT,
    SZ_PRE_HEAT,
    SZ_REMAINING_MINS,
    SZ_SPEED_CAPABILITIES,
    SZ_SUPPLY_FAN_SPEED,
    SZ_SUPPLY_FLOW,
    SZ_SUPPLY_TEMP,
    SZ_TEMPERATURE,
)
from ramses_rf.protocol.ramses import (
    _31DA_FAN_INFO,
    _2411_PARAMS_SCHEMA,
    SZ_DESCRIPTION,
)

from .base import PayloadBase
from .registry import register_payload

# ----------------------------------------------------------------------


@register_payload("01FF")
@dataclass(frozen=True, slots=True)
class SpiderThermostatPayload(PayloadBase):
    """Spider Thermostat payload (Opcode 01FF).

    5-byte Spider Thermostat binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Header / Domain              : 00
      +1       B      1B   Sub-Header / Flag            : 80
      +2       B      1B   Temperature (int8*2)         : 28 (20.0°C)
      +3       B      1B   Setpoint Min (int8*2)        : 0A (5.0°C)
      +4       B      1B   Setpoint Max (int8*2)        : 46 (35.0°C)
      --------------------------------------------------------------
      Field-spaced hex : 00 80 28 0A 46
      Payload hex      : 0080280A46

    :param temp: Temperature reading in °C, or None if N/A.
    :type temp: float | None
    :param setpoint_min: Minimum setpoint bound in °C, or None if N/A.
    :type setpoint_min: float | None
    :param setpoint_max: Maximum setpoint bound in °C, or None if N/A.
    :type setpoint_max: float | None

    Protocol Notes:
    # unknown_01ff, to/from a Itho Spider/Thermostat
    """

    _STRUCT_FMT: ClassVar[str] = ">BBbbb"

    temp: float | None
    setpoint_min: float | None
    setpoint_max: float | None
    _raw_bytes: bytes | None = None

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack Spider thermostat binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked SpiderThermostatPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 5 bytes.
        """
        if len(raw_data) < 5:
            raise ValueError(f"Invalid payload length for 01FF: {len(raw_data)}")
        t_raw = raw_data[2]
        sp_min_raw = raw_data[3]
        sp_max_raw = (
            raw_data[11]
            if len(raw_data) >= 12 and raw_data[11] == 0x80
            else raw_data[4]
        )
        temp_val = None if t_raw in (0x7F, 0x80, -128) else t_raw / 2.0
        sp_min = None if sp_min_raw in (0x7F, 0x80, -128) else sp_min_raw / 2.0
        sp_max = None if sp_max_raw in (0x7F, 0x80, -128) else sp_max_raw / 2.0
        raw_b = raw_data if len(raw_data) > 5 else None
        return cls(
            temp=temp_val,
            setpoint_min=sp_min,
            setpoint_max=sp_max,
            _raw_bytes=raw_b,
        )

    def to_bytes(self) -> bytes:
        """Pack Spider thermostat data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        if self._raw_bytes is not None:
            return self._raw_bytes
        t_raw = 0x7F if self.temp is None else int(round(self.temp * 2.0))
        sp_min_raw = (
            0x7F if self.setpoint_min is None else int(round(self.setpoint_min * 2.0))
        )
        sp_max_raw = (
            0x7F if self.setpoint_max is None else int(round(self.setpoint_max * 2.0))
        )
        return struct.pack(self._STRUCT_FMT, 0, 128, t_raw, sp_min_raw, sp_max_raw)

    def to_dict(self, msg: Any = None) -> dict[str, Any]:
        """Convert Spider thermostat payload to legacy dictionary layout.

        :param msg: Optional message context object.
        :type msg: Any
        :returns: Decoded Spider thermostat dictionary.
        :rtype: dict[str, Any]
        """
        res: dict[str, Any] = {
            "temperature": self.temp,
            "setpoint_bounds": (self.setpoint_min, self.setpoint_max),
        }
        if self._raw_bytes is not None and len(self._raw_bytes) >= 6:
            b = self._raw_bytes
            res["time_planning"] = bool((b[5] & 0x40) == 0)
            res["temp_adjusted"] = bool(b[5] & 0x20)
        elif self._raw_bytes is not None and len(self._raw_bytes) >= 5:
            res["time_planning"] = False
            res["temp_adjusted"] = False
        return res


# ----------------------------------------------------------------------


@register_payload("10D0")
@dataclass(frozen=True, slots=True)
class HvacFilterChangePayload(PayloadBase):
    """HVAC filter change counter payload (Opcode 10D0).

    6-byte Filter Change binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Header / Domain              : 00
      +1       B      1B   Remaining Days (uint8)       : B4 (180 days)
      +2       B      1B   Lifetime Days (uint8)        : B4 (180 days)
      +3       B      1B   Remaining Percent (uint8)    : C8 (100.0%)
      +4       2s     2B   Reserved / Trailer bytes     : 00 00
      --------------------------------------------------------------
      Field-spaced hex : 00 B4 B4 C8 0000
      Payload hex      : 00B4B4C80000

    :param remaining_days: Remaining filter days integer, or None
        if reset command.
    :type remaining_days: int | None
    :param days_lifetime: Total filter lifetime days integer, or None
        if reset command.
    :type days_lifetime: int | None
    :param remaining_percent: Remaining filter percentage, or None
        if reset command.
    :type remaining_percent: float | None
    :param reset_counter: True if reset command payload (00FF).
    :type reset_counter: bool

    Sample Packet Logs:
    # 2022-07-03T22:52:34.571579 045  W --- 37:171871 32:155617 --:------ 10D0 002 00FF
    # 2022-07-03T22:52:34.596526 066  I --- 32:155617 37:171871 --:------ 10D0 006 0047B44F0000
    # 2022-07-03T23:14:23.854089 000 RQ --- 37:155617 32:155617 --:------ 10D0 002 0000
    # 2022-07-03T23:14:23.876088 084 RP --- 32:155617 37:155617 --:------ 10D0 006 00B4B4C80000
    """

    _STRUCT_FMT_6B: ClassVar[str] = ">BBBB2s"

    remaining_days: int | None = None
    days_lifetime: int | None = None
    remaining_percent: float | None = None
    reset_counter: bool = False

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack HVAC filter change counter binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked HvacFilterChangePayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 2 bytes.
        """
        if len(raw_data) == 2 and raw_data == b"\x00\xff":
            return cls(reset_counter=True)
        if len(raw_data) < 4:
            raise ValueError(f"Invalid payload length for 10D0: {len(raw_data)}")
        parse_data = raw_data if len(raw_data) >= 6 else raw_data.ljust(6, b"\x00")
        _hdr, rem_days, life_days, rem_pct_raw, _trailer = struct.unpack_from(
            cls._STRUCT_FMT_6B, parse_data, 0
        )
        return cls(
            remaining_days=None if rem_days in (0xFE, 0xFF) else rem_days,
            days_lifetime=None if life_days in (0xFE, 0xFF) else life_days,
            remaining_percent=None
            if rem_pct_raw in (0xFE, 0xFF)
            else rem_pct_raw / 2.0,
        )

    def to_bytes(self) -> bytes:
        """Pack HVAC filter change counter data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        if self.reset_counter:
            return b"\x00\xff"
        rem_pct_raw = (
            int(round(self.remaining_percent * 2.0))
            if self.remaining_percent is not None
            else 0
        )
        return struct.pack(
            self._STRUCT_FMT_6B,
            0,
            self.remaining_days or 0,
            self.days_lifetime or 0,
            rem_pct_raw,
            b"\x00\x00",
        )

    def to_dict(self, msg: Any = None) -> dict[str, Any]:
        """Convert HVAC filter change payload to legacy dictionary format.

        :param msg: Optional message context object.
        :type msg: Any
        :returns: Decoded filter change dictionary.
        :rtype: dict[str, Any]
        """
        if self.reset_counter:
            return {"reset_counter": True}
        res: dict[str, Any] = {}
        if self.remaining_days is not None:
            res["days_remaining"] = self.remaining_days
        if self.days_lifetime is not None:
            res["days_lifetime"] = self.days_lifetime
        if self.remaining_percent is not None:
            res["percent_remaining"] = self.remaining_percent / 100.0
        return res


# ----------------------------------------------------------------------


@register_payload("10E2")
@dataclass(frozen=True, slots=True)
class HvacCounterPayload(PayloadBase):
    """HVAC pulse counter payload (Opcode 10E2).

    3-byte HVAC Counter binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Header / Domain              : 00
      +1       H      2B   Counter Value (uint16)       : AD 74
      --------------------------------------------------------------
      Field-spaced hex : 00 AD74
      Payload hex      : 00AD74

    :param counter: Cumulative HVAC operational counter value integer.
    :type counter: int

    Sample Packet Logs:
    # .I --- --:------ --:------ 20:231151 10E2 003 00AD74  # every 2 minutes
    """

    _STRUCT_FMT: ClassVar[str] = ">BH"

    counter: int

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack HVAC pulse counter binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked HvacCounterPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 3 bytes.
        """
        if len(raw_data) < 3:
            raise ValueError(f"Invalid payload length for 10E2: {len(raw_data)}")
        _hdr, val = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(counter=val)

    def to_bytes(self) -> bytes:
        """Pack HVAC pulse counter data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        return struct.pack(self._STRUCT_FMT, 0, self.counter)


# ----------------------------------------------------------------------


@register_payload("1280")
@dataclass(frozen=True, slots=True)
class OutdoorHumidityPayload(PayloadBase):
    """Outdoor humidity reading payload (Opcode 1280).

    2-byte Outdoor Humidity binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Header / Domain              : 00
      +1       B      1B   Humidity percentage (uint8)  : 64 (50.0%)
      --------------------------------------------------------------
      Field-spaced hex : 00 64
      Payload hex      : 0064

    :param humidity_percent: Outdoor relative humidity reading.
    :type humidity_percent: float
    """

    _STRUCT_FMT: ClassVar[str] = ">BB"

    humidity_percent: float | None

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack outdoor humidity binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked OutdoorHumidityPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 2 bytes.
        """
        if len(raw_data) < 2:
            raise ValueError(f"Invalid payload length for 1280: {len(raw_data)}")
        raw_val = raw_data[1]
        # PROTOCOL QUIRK: 0x00, 0xEF, and 0xFF are protocol sentinel
        # null-markers indicating an uninstalled or absent humidity
        # sensor. Zero atmospheric humidity (0.0%) is physically
        # impossible. Normalise sentinel bytes to None to prevent
        # invalid 0.0% domain states (see ramses-rf/ramses_cc#742).
        hum = None if raw_val in (0x00, 0xEF, 0xFF) else raw_val / 2.0
        return cls(humidity_percent=hum)

    def to_bytes(self) -> bytes:
        """Pack outdoor humidity data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        if self.humidity_percent is None:
            return bytes([0, 0x00])
        raw_val = int(round(self.humidity_percent * 2.0))
        return bytes([0, raw_val])


# ----------------------------------------------------------------------


@register_payload("1298")
class Co2Payload(PayloadBase):
    """Master payload dispatcher and base class for Opcode 1298."""

    VARIANTS: ClassVar[tuple[type[PayloadBase], ...]] = ()

    co2_level: int | None

    def __new__(
        cls,
        co2_level: int | None = None,
        domain_idx: int | None = None,
    ) -> Any:
        """Construct Co2 payload variant dynamically from arguments."""
        if cls is not Co2Payload:
            return super().__new__(cls)
        if domain_idx is not None:
            return Co23BPayload(domain_idx=domain_idx, co2_level=co2_level)
        return Co22BPayload(co2_level=co2_level)

    def to_dict(self) -> dict[str, Any]:
        """Convert CO2 payload to legacy dictionary layout."""
        return {"co2_level": getattr(self, "co2_level", None)}

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> "Co2Payload":
        """Unpack binary payload, dispatching by length."""
        if len(raw_data) < 2:
            raise ValueError(f"Invalid payload length for 1298: {len(raw_data)}")
        if len(raw_data) >= 3:
            return Co23BPayload.from_bytes(raw_data)
        return Co22BPayload.from_bytes(raw_data)

    def to_bytes(self) -> bytes:
        """Pack payload base default method.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        :raises NotImplementedError: Master dispatcher must dispatch to sub-dataclass.
        """
        raise NotImplementedError("Use concrete variant sub-dataclass")


@dataclass(frozen=True, slots=True)
class Co22BPayload(Co2Payload):
    """2-byte CO2 sensor reading payload (Opcode 1298).

    2-byte CO2 binary layout (Big-Endian):
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       H      2B   CO2 level in PPM (uint16)    : 02 D0 (720 PPM)
      --------------------------------------------------------------
      Field-spaced hex : 02D0
      Payload hex      : 02D0

    :param co2_level: CO2 concentration level in PPM (parts per million).
    :type co2_level: int | None
    """

    _STRUCT_FMT: ClassVar[str] = ">H"

    co2_level: int | None

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 2-byte CO2 payload."""
        if len(raw_data) < 2:
            raise ValueError(
                f"Invalid payload length for Co22BPayload: {len(raw_data)}"
            )
        (val,) = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        co2 = None if val in (32767, 0x7FFF, 0xFFFF) else val
        return cls(co2_level=co2)

    def to_bytes(self) -> bytes:
        """Pack 2-byte CO2 payload."""
        val = 32767 if self.co2_level is None else self.co2_level
        return struct.pack(self._STRUCT_FMT, val)


@dataclass(frozen=True, slots=True)
class Co23BPayload(Co2Payload):
    """3-byte CO2 sensor reading payload (Opcode 1298).

    3-byte CO2 binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Header / Domain              : 00
      +1       H      2B   CO2 level in PPM (uint16)    : 02 D0 (720 PPM)
      --------------------------------------------------------------
      Field-spaced hex : 00 02D0
      Payload hex      : 0002D0

    :param domain_idx: Domain index byte.
    :type domain_idx: int
    :param co2_level: CO2 concentration level in PPM (parts per million).
    :type co2_level: int | None
    """

    _STRUCT_FMT: ClassVar[str] = ">BH"

    domain_idx: int
    co2_level: int | None

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 3-byte CO2 payload."""
        if len(raw_data) < 3:
            raise ValueError(
                f"Invalid payload length for Co23BPayload: {len(raw_data)}"
            )
        hdr, val = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        co2 = None if val in (32767, 0x7FFF, 0xFFFF) else val
        return cls(domain_idx=hdr, co2_level=co2)

    def to_bytes(self) -> bytes:
        """Pack 3-byte CO2 payload."""
        val = 32767 if self.co2_level is None else self.co2_level
        return struct.pack(self._STRUCT_FMT, self.domain_idx, val)


# Update VARIANTS property after variants are defined
Co2Payload.VARIANTS = (
    Co22BPayload,
    Co23BPayload,
)


# ----------------------------------------------------------------------


@register_payload("12A0")
@dataclass(frozen=True, slots=True)
class RelativeHumidityPayload(PayloadBase):
    """Relative humidity payload (Opcode 12A0).

    Multi-element 12A0 arrays (Ventura V1x, Orcon, etc.) contain indoor (00),
    supply (01), and outdoor (02) sensor readings. See ramses_cc#742.

    1-2 byte Relative Humidity binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Humidity percentage (uint8)  : 64 (50.0%)
      --------------------------------------------------------------
      Field-spaced hex : 64
      Payload hex      : 64

    :param humidity_percent: Relative humidity value (0.0 - 100.0%).
    :type humidity_percent: float
    """

    _STRUCT_FMT_1B: ClassVar[str] = ">B"

    humidity_percent: float | None
    _hvac_idx: str | None = None
    _temperature: float | None = None
    _dewpoint_temp: float | None = None

    @property
    def humidity(self) -> float | None:
        """Alias for humidity_percent for backward compatibility.

        :returns: Humidity percentage or None.
        :rtype: float | None
        """
        return self.humidity_percent

    @property
    def hvac_idx(self) -> str | None:
        """HVAC index string.

        :returns: HVAC index or None.
        :rtype: str | None
        """
        return self._hvac_idx

    @property
    def temperature(self) -> float | None:
        """Temperature reading in °C.

        :returns: Temperature or None.
        :rtype: float | None
        """
        return self._temperature

    @property
    def dewpoint_temp(self) -> float | None:
        """Dewpoint temperature in °C.

        :returns: Dewpoint temperature or None.
        :rtype: float | None
        """
        return self._dewpoint_temp

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self | list[Self]:
        """Unpack relative humidity binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked RelativeHumidityPayload instance or list of instances.
        :rtype: Self | list[Self]
        :raises ValueError: If raw_data is empty.
        """
        if not raw_data:
            raise ValueError("Payload data cannot be empty")

        if len(raw_data) > 7 and len(raw_data) % 7 == 0:
            return [
                cls._from_7b_bytes(raw_data[i : i + 7], is_array=True)
                for i in range(0, len(raw_data), 7)
            ]

        if len(raw_data) >= 6:
            return cls._from_7b_bytes(raw_data, is_array=False)

        offset = 1 if len(raw_data) >= 2 and raw_data[0] == 0 else 0
        raw_val = raw_data[offset]
        scale = 100.0 if len(raw_data) >= 2 else 2.0
        hum = None if raw_val in (0x00, 0xEF, 0xFF) else raw_val / scale
        return cls(humidity_percent=hum)

    @classmethod
    def _from_7b_bytes(cls, raw_data: bytes, is_array: bool) -> Self:
        idx = f"{raw_data[0]:02X}" if is_array else None
        offset = (
            1 if (idx is not None or (raw_data[0] == 0 and len(raw_data) >= 6)) else 0
        )
        hum_raw = raw_data[offset]
        hum = None if hum_raw in (0x00, 0xEF, 0xFF) else hum_raw / 100.0
        (temp_raw,) = struct.unpack_from(">h", raw_data, offset + 1)
        temp = None if temp_raw in (0x7FFF, 0x31FF) else temp_raw / 100.0
        (dew_raw,) = struct.unpack_from(">h", raw_data, offset + 3)
        dew = None if dew_raw in (0x7FFF, 0x31FF) else dew_raw / 100.0
        return cls(
            humidity_percent=hum, _hvac_idx=idx, _temperature=temp, _dewpoint_temp=dew
        )

    def to_bytes(self) -> bytes:
        """Pack relative humidity data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        if self.humidity_percent is None:
            return struct.pack(self._STRUCT_FMT_1B, 0x00)
        raw_val = int(round(self.humidity_percent * 2.0))
        return struct.pack(self._STRUCT_FMT_1B, raw_val)

    def to_dict(self, msg: Any = None) -> dict[str, Any]:
        """Convert humidity payload to legacy dictionary format.

        :param msg: Optional message context object.
        :type msg: Any
        :returns: Decoded humidity dictionary.
        :rtype: dict[str, Any]
        """
        res: dict[str, Any] = {}
        if self.hvac_idx is not None:
            res["hvac_idx"] = self.hvac_idx
            if self.hvac_idx == "00":
                res[SZ_INDOOR_HUMIDITY] = self.humidity
            elif self.hvac_idx == "02":
                res[SZ_OUTDOOR_HUMIDITY] = self.humidity
            else:
                res["rel_humidity"] = self.humidity
        else:
            res[SZ_INDOOR_HUMIDITY] = self.humidity

        if (
            self.temperature is not None
            or self.dewpoint_temp is not None
            or len(getattr(self, "_raw", b"")) >= 6
        ):
            res[SZ_TEMPERATURE] = self.temperature
            res[SZ_DEWPOINT_TEMP] = self.dewpoint_temp
        return res


# ----------------------------------------------------------------------


@register_payload("12C8")
@dataclass(frozen=True, slots=True)
class AirQualityBasisPayload(PayloadBase):
    """Air quality basis payload (Opcode 12C8).

    2-byte Air Quality Basis binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Air Quality Percent (uint8)  : 64 (50.0%)
      +1       B      1B   Basis Code Flag (uint8)      : 00
      --------------------------------------------------------------
      Field-spaced hex : 64 00
      Payload hex      : 6400

    :param air_quality_percent: Air quality measurement percentage.
    :type air_quality_percent: float
    :param basis_flag: Air quality basis classification flag.
    :type basis_flag: int
    """

    _STRUCT_FMT: ClassVar[str] = ">BB"

    air_quality_percent: float
    basis_flag: int

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack air quality basis binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked AirQualityBasisPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 2 bytes.
        """
        if len(raw_data) < 2:
            raise ValueError(f"Invalid payload length for 12C8: {len(raw_data)}")
        offset = 1 if len(raw_data) >= 3 else 0
        aq_pct = raw_data[offset] / 200.0
        basis = raw_data[offset + 1]
        return cls(air_quality_percent=aq_pct, basis_flag=basis)

    def to_bytes(self) -> bytes:
        """Pack air quality basis data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        aq_raw = int(round(self.air_quality_percent * 200.0))
        return bytes([aq_raw, self.basis_flag])

    def to_dict(self, msg: Any = None) -> dict[str, Any]:
        """Convert air quality basis payload to legacy dictionary format.

        :param msg: Optional message context object.
        :type msg: Any
        :returns: Decoded air quality dictionary.
        :rtype: dict[str, Any]
        """
        basis_map = {0x10: "voc", 0x20: "co2", 0x40: "rel_humidity"}
        return {
            SZ_AIR_QUALITY: self.air_quality_percent,
            SZ_AIR_QUALITY_BASIS: basis_map.get(
                self.basis_flag, f"unknown_{self.basis_flag:02X}"
            ),
        }


# ----------------------------------------------------------------------


@register_payload("1470")
@dataclass(frozen=True, slots=True)
class HvacProgrammeSchemePayload(PayloadBase):
    """HVAC programme schedule scheme payload (Opcode 1470).

    2-byte Programme Scheme binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Scheme Code (uint8)          : 0B
      +1       B      1B   Daily Setpoints (uint8)      : 03
      --------------------------------------------------------------
      Field-spaced hex : 0B 03
      Payload hex      : 0B03

    :param scheme_code: Schedule scheme classification code byte.
    :type scheme_code: int
    :param daily_setpoints: Daily setpoint count byte.
    :type daily_setpoints: int
    """

    _STRUCT_FMT: ClassVar[str] = ">BB"

    scheme_code: int
    daily_setpoints: int

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack HVAC programme scheme binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked HvacProgrammeSchemePayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 2 bytes.
        """
        if len(raw_data) < 2:
            raise ValueError(f"Invalid payload length for 1470: {len(raw_data)}")
        scheme, setpoints = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(scheme_code=scheme, daily_setpoints=setpoints)

    def to_bytes(self) -> bytes:
        """Pack HVAC programme scheme data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        return struct.pack(self._STRUCT_FMT, self.scheme_code, self.daily_setpoints)


# ----------------------------------------------------------------------


@register_payload("1F70")
@dataclass(frozen=True, slots=True)
class HvacProgrammeConfigPayload(PayloadBase):
    """HVAC programme schedule configuration payload (Opcode 1F70).

    4-byte Programme Config binary layout (Big-Endian):
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Day Index (uint8)            : 01
      +1       B      1B   Setpoint Index (uint8)       : 00
      +2       H      2B   Start Time Mins (uint16)     : 01 68 (360 mins)
      --------------------------------------------------------------
      Field-spaced hex : 01 00 0168
      Payload hex      : 01000168

    :param day_idx: Schedule day index byte.
    :type day_idx: int
    :param setpoint_idx: Schedule setpoint index byte.
    :type setpoint_idx: int
    :param start_time_mins: Start time in minutes past midnight.
    :type start_time_mins: int
    """

    _STRUCT_FMT: ClassVar[str] = ">BBH"

    day_idx: int
    setpoint_idx: int
    start_time_mins: int

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack HVAC programme config binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked HvacProgrammeConfigPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 4 bytes.
        """
        if len(raw_data) < 4:
            raise ValueError(f"Invalid payload length for 1F70: {len(raw_data)}")
        d_idx, sp_idx, t_mins = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(day_idx=d_idx, setpoint_idx=sp_idx, start_time_mins=t_mins)

    def to_bytes(self) -> bytes:
        """Pack HVAC programme config data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        return struct.pack(
            self._STRUCT_FMT, self.day_idx, self.setpoint_idx, self.start_time_mins
        )


# ----------------------------------------------------------------------


@register_payload("1FCA")
@dataclass(frozen=True, slots=True)
class HvacDevicePairingPayload(PayloadBase):
    """HVAC device pairing configuration payload (Opcode 1FCA).

    Variable Device Pairing binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Pairing Type Code (uint8)    : 00
      +1       xs     Var  Paired Device Raw Bytes      : 01 02 03
      --------------------------------------------------------------
      Field-spaced hex : 00 010203
      Payload hex      : 00010203

    :param pairing_type: Pairing type code byte.
    :type pairing_type: int
    :param device_bytes: Paired device raw bytes sequence.
    :type device_bytes: bytes

    Sample Packet Logs:
    # .W --- 30:248208 34:021943 --:------ 1FCA 009 00-01FF-7BC990-FFFFFF  # sent x2
    """

    _STRUCT_FMT_HEADER: ClassVar[str] = ">B"

    pairing_type: int
    device_bytes: bytes

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack HVAC device pairing binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked HvacDevicePairingPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data is empty.
        """
        if not raw_data:
            raise ValueError("Payload data cannot be empty")
        (pairing_type,) = struct.unpack_from(cls._STRUCT_FMT_HEADER, raw_data, 0)
        return cls(pairing_type=pairing_type, device_bytes=raw_data[1:])

    def to_bytes(self) -> bytes:
        """Pack HVAC device pairing data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        return (
            struct.pack(self._STRUCT_FMT_HEADER, self.pairing_type) + self.device_bytes
        )


# ----------------------------------------------------------------------


@register_payload("2210")
@dataclass(frozen=True, slots=True)
class HvacAutoRequestPayload(PayloadBase):
    """HVAC auto demand request payload (Opcode 2210).

    2-byte Auto Request binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Requested Fan Percent        : 64 (50.0%)
      +1       B      1B   Request Reason Code          : 02 (CO2)
      --------------------------------------------------------------
      Field-spaced hex : 64 02
      Payload hex      : 6402

    :param exhaust_fan_speed: Exhaust fan speed percentage.
    :type exhaust_fan_speed: float | None
    :param req_reason: Request reason string.
    :type req_reason: str | None
    :param unknown_78: Optional unknown field 78 string.
    :type unknown_78: str | None
    :param unknown_80: Optional unknown field 80 string.
    :type unknown_80: str | None
    :param unknown_82: Optional unknown field 82 string.
    :type unknown_82: str | None
    :param requested_fan_percent: Auto requested fan speed percentage.
    :type requested_fan_percent: float | None
    :param request_reason: Request reason classification code byte.
    :type request_reason: int | None
    """

    _STRUCT_FMT: ClassVar[str] = ">BB"

    exhaust_fan_speed: float | None = None
    req_reason: str | None = None
    unknown_78: str | None = None
    unknown_80: str | None = None
    unknown_82: str | None = None
    requested_fan_percent: float | None = None
    request_reason: int | None = None

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack HVAC auto request binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked HvacAutoRequestPayload instance.
        :rtype: Self
        """
        if len(raw_data) == 1:
            return cls()
        if len(raw_data) >= 42:
            spd_raw = raw_data[5]
            spd = None if spd_raw == 0xFF else spd_raw / 200.0
            reason_raw = raw_data[10]
            reason_map = {0xFF: "IDL", 0: "IDL", 2: "CO2", 3: "HUM"}
            reason_str = reason_map.get(reason_raw, f"{reason_raw:02X}")
            return cls(
                exhaust_fan_speed=spd,
                req_reason=reason_str,
                unknown_78=f"{raw_data[39]:02X}",
                unknown_80=f"{raw_data[40]:02X}",
                unknown_82=f"{raw_data[41]:02X}",
            )
        if len(raw_data) >= 2:
            fan_pct = raw_data[0] / 2.0
            reason = raw_data[1]
            return cls(requested_fan_percent=fan_pct, request_reason=reason)
        return cls()

    def to_bytes(self) -> bytes:
        """Pack HVAC auto request data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        if self.requested_fan_percent is not None and self.request_reason is not None:
            fan_raw = int(round(self.requested_fan_percent * 2.0))
            return bytes([fan_raw, self.request_reason])
        return b"\x00"

    def to_dict(self, msg: Any = None) -> dict[str, Any]:
        """Convert HVAC auto request payload to legacy dictionary format.

        :param msg: Optional message context object.
        :type msg: Any
        :returns: Decoded auto request dictionary.
        :rtype: dict[str, Any]
        """
        if self.req_reason is not None:
            return {
                "exhaust_fan_speed": self.exhaust_fan_speed,
                "req_reason": self.req_reason,
                "unknown_78": self.unknown_78,
                "unknown_80": self.unknown_80,
                "unknown_82": self.unknown_82,
            }
        if self.requested_fan_percent is not None and self.request_reason is not None:
            return {
                "requested_fan_percent": self.requested_fan_percent,
                "request_reason": self.request_reason,
            }
        return {}


# ----------------------------------------------------------------------


@register_payload("22B0")
@dataclass(frozen=True, slots=True)
class HvacProgrammeEnabledPayload(PayloadBase):
    """HVAC programme enabled status payload (Opcode 22B0).

    2-byte Programme Enabled binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Header / Domain              : 00
      +1       B      1B   Enabled Flag (5=True, 6=False): 05
      --------------------------------------------------------------
      Field-spaced hex : 00 05
      Payload hex      : 0005

    :param enabled: True if schedule program calendar is enabled.
    :type enabled: bool

    Sample Packet Logs & Protocol Notes:
    # Seen on Orcon: see 1470, 1F70, 22B0
    # WIP: HVAC auto requests (confirmed for Orcon, others?)
    # .W --- 37:171871 32:155617 --:------ 22B0 002 0005  # enable, calendar on
    # .I --- 32:155617 37:171871 --:------ 22B0 002 0005
    # .W --- 37:171871 32:155617 --:------ 22B0 002 0006  # disable, calendar off
    # .I --- 32:155617 37:171871 --:------ 22B0 002 0006
    """

    _STRUCT_FMT: ClassVar[str] = ">BB"

    enabled: bool

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack HVAC programme enabled status binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked HvacProgrammeEnabledPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 2 bytes.
        """
        if len(raw_data) < 2:
            raise ValueError(f"Invalid payload length for 22B0: {len(raw_data)}")
        is_enabled = raw_data[1] == 5
        return cls(enabled=is_enabled)

    def to_bytes(self) -> bytes:
        """Pack HVAC programme enabled status data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        code = 5 if self.enabled else 6
        return bytes([0, code])


# ----------------------------------------------------------------------


@register_payload("22E0")
# ----------------------------------------------------------------------

@register_payload("22E5")
# ----------------------------------------------------------------------

@register_payload("22E9")
@dataclass(frozen=True, slots=True)
class HvacVentilationStatusPayload(PayloadBase):
    """HVAC ventilation status payload (Opcode 22E0, 22E5, 22E9).

    2-byte Ventilation Status binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Fan Speed / Flow Mode        : 01
      +1       B      1B   Status Flags / State         : 00
      --------------------------------------------------------------
      Field-spaced hex : 01 00
      Payload hex      : 0100

    :param flow_mode: Current ventilation flow mode byte.
    :type flow_mode: int
    :param status_flags: Status flags byte.
    :type status_flags: int

    Sample Packet Logs:
    # RP --- 32:155617 18:005904 --:------ 22E0 004 00-34-A0-1E
    # RP --- 32:153258 18:005904 --:------ 22E0 004 00-64-A0-1E
    # RP --- 32:153258 18:005904 --:------ 22E5 004 00-96-C8-14
    # RP --- 32:155617 18:005904 --:------ 22E5 004 00-72-C8-14
    """

    _STRUCT_FMT: ClassVar[str] = ">BB"

    flow_mode: int
    status_flags: int
    _percent_2: float | None = None
    _percent_4: float | None = None
    _percent_6: float | None = None
    _raw_bytes: bytes | None = None

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack ventilation status binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked HvacVentilationStatusPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 2 bytes.
        """
        if len(raw_data) < 2:
            raise ValueError(f"Invalid payload length: {len(raw_data)}")
        if len(raw_data) >= 4:
            hdr, r2, r4, r6 = struct.unpack_from(">BBBB", raw_data, 0)
            return cls(
                flow_mode=hdr,
                status_flags=r2,
                _percent_2=round(r2 / 200.0, 2),
                _percent_4=round(r4 / 200.0, 2),
                _percent_6=round(r6 / 200.0, 2),
                _raw_bytes=raw_data,
            )
        f_mode, s_flags = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(
            flow_mode=f_mode,
            status_flags=s_flags,
            _raw_bytes=raw_data if len(raw_data) > 2 else None,
        )

    def to_bytes(self) -> bytes:
        """Pack ventilation status data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        if self._raw_bytes is not None:
            return self._raw_bytes
        return struct.pack(self._STRUCT_FMT, self.flow_mode, self.status_flags)

    def to_dict(self, msg: Any = None) -> dict[str, Any]:
        """Convert ventilation status payload to legacy dictionary layout.

        :param msg: Optional message context object.
        :type msg: Any
        :returns: Decoded ventilation status dictionary.
        :rtype: dict[str, Any]
        """
        if self._raw_bytes is not None and len(self._raw_bytes) >= 4:
            hdr, r2, r4, r6 = struct.unpack_from(">BBBB", self._raw_bytes, 0)
            if r2 == 1:
                return {"unknown_4": f"{r4:02X}", "unknown_6": f"{r6:02X}"}
            return {
                "percent_2": round(r2 / 200.0, 2),
                "percent_4": round(r4 / 200.0, 2),
                "percent_6": round(r6 / 200.0, 2),
            }
        return {
            "flow_mode": self.flow_mode,
            "status_flags": self.status_flags,
        }


# ----------------------------------------------------------------------


@register_payload("22F1")
@dataclass(frozen=True, slots=True)
class HvacFanModePayload(PayloadBase):
    """Fan mode setting payload (Opcode 22F1).

    3-byte HVAC Fan Mode binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Header / Domain Byte         : 00
      +1       B      1B   Fan Mode Index (uint8)       : 02 (Low)
      +2       B      1B   Max Fan Mode (uint8)         : 04 (Itho)
      --------------------------------------------------------------
      Field-spaced hex : 00 02 04
      Payload hex      : 000204

    :param header: Domain or header index byte.
    :type header: int
    :param mode_idx: Selected fan mode integer index, or None if unconfigured.
    :type mode_idx: int | None
    :param mode_max: Maximum supported fan mode integer index, or None if unconfigured.
    :type mode_max: int | None

    Protocol Notes:
    # ClimaRad Ventura fan & remote
    # the broadcast address (NON_DEV_ADDR). Directed 22F1 packets from Itho
    """

    header: int
    mode_idx: int | None
    mode_max: int | None

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack fan mode binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked HvacFanModePayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 3 bytes.
        """
        if len(raw_data) < 3:
            raise ValueError(f"Invalid payload length for 22F1: {len(raw_data)}")
        hdr, raw_idx, raw_max = struct.unpack_from(">BBB", raw_data, 0)
        mode_idx = None if raw_idx in (0xEF, 0xFE, 0xFF) else raw_idx
        mode_max = None if raw_max in (0xEF, 0xFE, 0xFF) else raw_max
        return cls(header=hdr, mode_idx=mode_idx, mode_max=mode_max)

    def to_bytes(self) -> bytes:
        """Pack fan mode data into 3-byte binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        raw_idx = 0xFF if self.mode_idx is None else self.mode_idx
        raw_max = 0xFF if self.mode_max is None else self.mode_max
        return struct.pack(">BBB", self.header, raw_idx, raw_max)

    def to_dict(self, msg: Any = None) -> dict[str, Any]:
        """Convert fan mode payload to legacy dictionary format.

        :param msg: Optional message context object.
        :type msg: Any
        :returns: Decoded fan mode dictionary.
        :rtype: dict[str, Any]
        """
        if self.mode_idx is None:
            return {}
        if self.mode_max == 4:
            mode_map = {0: "off", 1: "auto", 2: "low", 3: "medium", 4: "high"}
        elif self.mode_max == 5:
            mode_map = {0: "off", 1: "away", 2: "medium", 3: "high", 4: "boost"}
        elif self.mode_max == 6:
            mode_map = {1: "away", 2: "low", 3: "medium", 4: "high", 5: "auto"}
        elif self.mode_max == 7:
            mode_map = {
                0: "away",
                1: "low",
                2: "medium",
                3: "high",
                4: "auto",
                5: "auto_alt",
                6: "boost",
                7: "off",
            }
        elif self.mode_max == 10:
            mode_map = {2: "normal", 3: "boost", 9: "heater_off", 10: "heater_auto"}
        else:
            mode_map = {}
        fan_mode = mode_map.get(self.mode_idx, f"{self.mode_idx:02X}")
        scheme = (
            {
                4: "itho",
                5: "nuaire",
                6: "vasco",
                7: "orcon",
                10: "orcon",
            }.get(self.mode_max, "orcon")
            if self.mode_max is not None
            else "orcon"
        )
        return {
            "fan_mode": fan_mode,
            "_mode_idx": f"{self.mode_idx:02X}",
            "_mode_max": f"{self.mode_max:02X}" if self.mode_max is not None else None,
            "_scheme": scheme,
        }


FanModePayload = HvacFanModePayload


# ----------------------------------------------------------------------


@register_payload("22F2")
@dataclass(frozen=True, slots=True)
class HvacFlowRatePayload(PayloadBase):
    """Flow rate measurement payload (Opcode 22F2).

    3-byte HVAC Flow Rate binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   HVAC Index (uint8)           : 00
      +1       H      2B   Flow Rate (uint16 * 100)     : 00 64 (1.00 L/s)
      --------------------------------------------------------------
      Field-spaced hex : 00 0064
      Payload hex      : 000064

    :param measures: Tuple of (hvac_idx, flow_rate) pairs.
    :type measures: tuple[tuple[int, float], ...]
    """

    measures: tuple[tuple[int, float], ...]

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack flow rate binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked HvacFlowRatePayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 3 bytes.
        """
        if len(raw_data) < 3 or len(raw_data) % 3 != 0:
            raise ValueError(f"Invalid payload length for 22F2: {len(raw_data)}")
        items: list[tuple[int, float]] = []
        for i in range(0, len(raw_data), 3):
            idx, val = struct.unpack_from(">BH", raw_data, i)
            items.append((idx, round(val / 100.0, 2)))
        return cls(measures=tuple(items))

    def to_bytes(self) -> bytes:
        """Pack flow rate data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        b = bytearray()
        for idx, m in self.measures:
            b.extend(struct.pack(">BH", idx, int(round(m * 100.0))))
        return bytes(b)

    def to_dict(self, msg: Any = None) -> list[dict[str, Any]]:
        """Convert flow rate payload to legacy dictionary format.

        :param msg: Optional message context object.
        :type msg: Any
        :returns: List of decoded measure dictionaries.
        :rtype: list[dict[str, Any]]
        """
        return [{"hvac_idx": f"{idx:02X}", "measure": m} for idx, m in self.measures]


# ----------------------------------------------------------------------


@register_payload("22F7")
# ----------------------------------------------------------------------

@register_payload("22F4")
@dataclass(frozen=True, slots=True)
class HvacFanRatePayload(PayloadBase):
    """Fan rate payload (Opcode 22F4).

    3-byte HVAC Fan Rate binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Header / Domain Byte         : 00
      +1       B      1B   Fan Mode Byte (uint8)        : 40 (Auto)
      +2       B      1B   Fan Rate Byte (uint8)        : E6 (Speed 2)
      --------------------------------------------------------------
      Field-spaced hex : 00 40 E6
      Payload hex      : 0040E6

    :param raw_bytes: Raw binary payload bytes.
    :type raw_bytes: bytes
    """

    _STRUCT_FMT: ClassVar[str] = ">BBB"

    raw_bytes: bytes

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack fan rate binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked HvacFanRatePayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 3 bytes.
        """
        if len(raw_data) < 3:
            raise ValueError(f"Invalid payload length for 22F4: {len(raw_data)}")
        return cls(raw_bytes=raw_data)

    def to_bytes(self) -> bytes:
        """Pack fan rate data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        return self.raw_bytes

    def to_dict(self, msg: Any = None) -> dict[str, Any]:
        """Convert fan rate payload to legacy dictionary format.

        :param msg: Optional message context object.
        :type msg: Any
        :returns: Decoded fan rate dictionary.
        :rtype: dict[str, Any]
        """
        b = self.raw_bytes
        mode_byte = (
            b[5]
            if b[1] == 0x00 and len(b) >= 7 and b[5] in (0x20, 0x40, 0x60)
            else b[1]
        )
        rate_byte = (
            b[6] if b[2] == 0x00 and len(b) >= 7 and b[6] not in (0x00, 0xFF) else b[2]
        )
        if mode_byte == 0x60:
            mode_str = "manual"
        elif mode_byte == 0x40:
            mode_str = "auto"
        elif mode_byte == 0x20:
            mode_str = "paused"
        else:
            mode_str = f"{mode_byte:02X}"

        rate_map = {
            0xDD: "speed 1",
            0xC9: "speed 1",
            0xE5: "speed 1",
            0xE6: "speed 2",
            0xCA: "speed 2",
            0xCB: "speed 3",
            0xB0: "speed 0",
            0xE4: "speed 0",
            0x30: "speed 0",
            0x00: "speed 0",
        }
        rate_str = rate_map.get(rate_byte, f"0x{rate_byte:02X}")
        return {"fan_mode": mode_str, "fan_rate": rate_str}


# ----------------------------------------------------------------------


@register_payload("22F7")
# ----------------------------------------------------------------------

@register_payload("22F8")
@dataclass(frozen=True, slots=True)
class HvacBypassPositionPayload(PayloadBase):
    """Bypass position payload (Opcode 22F7).

    3-byte HVAC Bypass Position binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Header / Domain Byte         : 00
      +1       B      1B   Bypass Mode (uint8)          : 00
      +2       B      1B   Bypass Position (uint8)      : C8 (100.0%)
      --------------------------------------------------------------
      Field-spaced hex : 00 00 C8
      Payload hex      : 0000C8

    :param raw_bytes: Raw binary payload bytes.
    :type raw_bytes: bytes

    Sample Packet Logs:
    # .W --- 32:111111 37:111111 --:------ 22F8 003 630203
    """

    _STRUCT_FMT: ClassVar[str] = ">BBB"

    raw_bytes: bytes

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack bypass position binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked HvacBypassPositionPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 3 bytes.
        """
        if len(raw_data) < 3:
            raise ValueError(f"Invalid payload length for 22F7: {len(raw_data)}")
        return cls(raw_bytes=raw_data)

    def to_bytes(self) -> bytes:
        """Pack bypass position data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        return self.raw_bytes

    def to_dict(self, msg: Any = None) -> dict[str, Any]:
        """Convert bypass position payload to legacy dictionary format.

        :param msg: Optional message context object.
        :type msg: Any
        :returns: Decoded bypass position dictionary.
        :rtype: dict[str, Any]
        """
        b = self.raw_bytes
        b1, b2 = b[1], b[2]
        mode_map = {0x00: "off", 0xC8: "on", 0xFF: "auto"}
        mode = mode_map.get(b1, f"{b1:02X}")
        if b2 == 0xEF:
            return {"bypass_mode": mode}
        state = "on" if b2 == 0xC8 else ("off" if b2 == 0x00 else f"{b2:02X}")
        pos = 1.0 if b2 == 0xC8 else (0.0 if b2 == 0x00 else round(b2 / 200.0, 2))
        return {"bypass_mode": mode, "bypass_position": pos, "bypass_state": state}


# ----------------------------------------------------------------------


@register_payload("22F3")
@dataclass(frozen=True, slots=True)
class HvacVentilationControlPayload(PayloadBase):
    """HVAC Ventilation Control / Boost payload (Opcode 22F3).

    3-7 byte 22F3 binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Header / Domain (uint8)      : 00
      +1       B      1B   Flags (uint8)                : 00
      +2       B      1B   Minutes (uint8)              : 0A (10 mins)
      --------------------------------------------------------------
      Field-spaced hex : 00 00 0A
      Payload hex      : 00000A

    :param header: Header or domain index byte.
    :type header: int
    :param flags_byte: Flags integer byte.
    :type flags_byte: int
    :param minutes: Duration in minutes.
    :type minutes: int
    :param fan_mode_byte: Optional fan mode byte identifier.
    :type fan_mode_byte: int | None
    :param fallback_mode_byte: Optional fallback mode byte.
    :type fallback_mode_byte: int | None
    :param fallback_fan_mode_byte: Optional fallback fan mode byte.
    :type fallback_fan_mode_byte: int | None

    Protocol Notes:
    # NOTE: for boost timer for high
    """

    _STRUCT_FMT: ClassVar[str] = ">BBB"

    header: int
    flags_byte: int
    minutes: int
    fan_mode_byte: int | None = None
    fallback_mode_byte: int | None = None
    fallback_fan_mode_byte: int | None = None

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 22F3 ventilation control binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked HvacVentilationControlPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 3 bytes.
        """
        if len(raw_data) < 3:
            raise ValueError(f"Invalid payload length for 22F3: {len(raw_data)}")
        hdr = raw_data[0]
        flg = raw_data[1]
        mins = raw_data[2]
        if len(raw_data) >= 7:
            if flg & 0x40:
                mins = mins * 60
            fm = raw_data[3]
            fb_m = raw_data[4]
            fb_fm = raw_data[5] if len(raw_data) > 5 else None
            return cls(
                header=hdr,
                flags_byte=flg,
                minutes=mins,
                fan_mode_byte=fm,
                fallback_mode_byte=fb_m,
                fallback_fan_mode_byte=fb_fm,
            )
        return cls(header=hdr, flags_byte=flg, minutes=mins)

    def to_bytes(self) -> bytes:
        """Pack 22F3 ventilation control binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        if self.fan_mode_byte is not None:
            mins = self.minutes // 60 if self.flags_byte & 0x10 else self.minutes
            fb_fm = (
                0
                if self.fallback_fan_mode_byte is None
                else self.fallback_fan_mode_byte
            )
            return bytes(
                [
                    self.header,
                    self.flags_byte,
                    mins,
                    self.fan_mode_byte,
                    self.fallback_mode_byte or 0,
                    fb_fm,
                    0,
                ]
            )
        return bytes([self.header, self.flags_byte, self.minutes])

    def to_dict(self, msg: Any = None) -> dict[str, Any]:
        """Convert 22F3 payload to legacy dictionary format.

        :param msg: Optional message context object.
        :type msg: Any
        :returns: Decoded ventilation control dictionary.
        :rtype: dict[str, Any]
        """
        flags = [(self.flags_byte >> i) & 1 for i in range(7, -1, -1)]
        res: dict[str, Any] = {
            "minutes": self.minutes,
            "flags": flags,
        }
        if self.flags_byte == 0:
            res["new_speed_mode"] = "fan_boost"
            res["fallback_speed_mode"] = "per_vent_speed"
        else:
            res["new_speed_mode"] = "per_request"
            res["fallback_speed_mode"] = (
                "per_request" if self.flags_byte & 0x10 else "per_vent_speed"
            )

        if self.fan_mode_byte is not None and self.flags_byte != 0:
            fan_mode_map = {
                0: "away" if self.fallback_mode_byte == 4 else "off",
                1: "low",
                2: "medium",
                3: "high",
                4: "high" if self.fallback_mode_byte in (3, 6) else "away",
            }
            if self.fan_mode_byte in fan_mode_map:
                res["fan_mode"] = fan_mode_map[self.fan_mode_byte]
        if self.fallback_fan_mode_byte is not None and self.fallback_fan_mode_byte != 0:
            fb_map = {4: "auto"}
            if self.fallback_fan_mode_byte in fb_map:
                res["fallback_fan_mode"] = fb_map[self.fallback_fan_mode_byte]
        res["_scheme"] = "orcon"
        return res


# ----------------------------------------------------------------------


@register_payload("2411")
@dataclass(frozen=True, slots=True)
class HvacFanParamPayload(PayloadBase):
    """HVAC fan parameters payload (Opcode 2411).

    23-byte HVAC Fan Parameter binary layout (Big-Endian):
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Header / Flag byte           : 00
      +1       H      2B   Parameter ID (uint16)        : 00 0A (Param 10)
      +3       B      1B   Flags (uint8)                : 00
      +4       B      1B   Data Type (uint8)            : 10
      +5       i      4B   Scaled Value (int32)         : 00 00 00 05 (5)
      +9       i      4B   Min Value Scaled (int32)     : 00 00 00 00 (0)
      +13      i      4B   Max Value Scaled (int32)     : 00 00 00 64 (100)
      +17      i      4B   Precision Scaled (int32)     : 00 00 00 01 (1)
      +21      2s     2B   Reserved / Trailer bytes     : 00 01
      --------------------------------------------------------------
      Field-spaced hex : 00 000A 00 10 00000005 00000000 00000064 00000001 0001
      Payload hex      : 00000A0010000000050000000000000064000000010001

    Protocol Notes:
      # see: https://github.com/zxdavb/ramses_rf/issues/73 & 101
      # See: ramses-rf/ramses_rf#830
      # FFFFFFFF) is a sentinel meaning "not available" (issue 830).
      # 4-byte boolean parameter values: 0 = False, 1 = True.
      # Sentinel values (e.g. 0x000000FF, 0xFFFFFFFF) indicate parameter N/A.

    :param param_id: Fan parameter identifier integer.
    :type param_id: int
    :param data_type: Parameter data type integer.
    :type data_type: int
    :param value_scaled: Current scaled parameter value integer, or None if sentinel/N/A.
    :type value_scaled: int | None
    :param min_val_scaled: Minimum allowed scaled parameter value integer.
    :type min_val_scaled: int
    :param max_val_scaled: Maximum allowed scaled parameter value integer.
    :type max_val_scaled: int
    :param precision_scaled: Parameter scaling precision integer.
    :type precision_scaled: int
    :param trailer_bytes: Reserved trailer bytes sequence.
    :type trailer_bytes: bytes
    """

    _STRUCT_FMT: ClassVar[str] = ">BHBBiiii2s"

    param_id: int
    data_type: int
    value_scaled: int | None
    min_val_scaled: int | None
    max_val_scaled: int | None
    precision_scaled: int
    trailer_bytes: bytes
    _raw_3b: bytes | None = None

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack HVAC fan parameters binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked HvacFanParamPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 3 bytes.
        """
        if len(raw_data) < 3:
            raise ValueError(f"Invalid payload length for 2411: {len(raw_data)}")
        if len(raw_data) < 22:
            return cls(
                param_id=raw_data[2] if len(raw_data) >= 3 else 0,
                data_type=0,
                value_scaled=None,
                min_val_scaled=0,
                max_val_scaled=0,
                precision_scaled=0,
                trailer_bytes=b"",
                _raw_3b=raw_data,
            )
        parse_data = raw_data if len(raw_data) >= 23 else raw_data + b"\x20"
        (
            _,
            p_id,
            _,
            d_type,
            val_s,
            min_s,
            max_s,
            prec_s,
            trailer,
        ) = struct.unpack_from(cls._STRUCT_FMT, parse_data, 0)
        val_scaled = None if val_s in (0x000000FF, 0xFFFFFFFF, -1) else val_s
        min_scaled = None if min_s in (0x000000FF, 0xFFFFFFFF, -1) else min_s
        max_scaled = None if max_s in (0x000000FF, 0xFFFFFFFF, -1) else max_s
        return cls(
            param_id=p_id,
            data_type=d_type,
            value_scaled=val_scaled,
            min_val_scaled=min_scaled,
            max_val_scaled=max_scaled,
            precision_scaled=prec_s,
            trailer_bytes=trailer,
        )

    def to_bytes(self) -> bytes:
        """Pack HVAC fan parameters data into 23-byte binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        if self._raw_3b is not None:
            return self._raw_3b
        val_s = -1 if self.value_scaled is None else self.value_scaled
        min_s = -1 if self.min_val_scaled is None else self.min_val_scaled
        max_s = -1 if self.max_val_scaled is None else self.max_val_scaled
        return struct.pack(
            self._STRUCT_FMT,
            0,
            self.param_id,
            0,
            self.data_type,
            val_s,
            min_s,
            max_s,
            self.precision_scaled,
            self.trailer_bytes,
        )

    def to_dict(self, msg: Any = None) -> dict[str, Any]:
        """Convert HVAC fan parameters payload to legacy dictionary format.

        :param msg: Optional message context object.
        :type msg: Any
        :returns: Decoded fan parameter dictionary.
        :rtype: dict[str, Any]
        """
        p_str = f"{self.param_id:02X}"
        schema_info = _2411_PARAMS_SCHEMA.get(p_str)
        desc = (
            schema_info.get(SZ_DESCRIPTION, p_str)
            if isinstance(schema_info, dict)
            else p_str
        )
        if self._raw_3b is not None:
            return {"parameter": p_str, "description": desc}
        return {
            "parameter": p_str,
            "description": desc,
            "value": self.value_scaled,
            "min_value": self.min_val_scaled,
            "max_value": self.max_val_scaled,
            "precision": self.precision_scaled,
        }


# ----------------------------------------------------------------------


@register_payload("3110")
# ----------------------------------------------------------------------

@register_payload("3120")
@dataclass(frozen=True, slots=True)
class HvacAirQualityPayload(PayloadBase):
    """HVAC indoor air quality sensor payload (Opcode 3110, 3120, 313E).

    2-byte Air Quality binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       H      2B   Air Quality Index / VOC      : 00 C8 (200 AQI)
      --------------------------------------------------------------
      Field-spaced hex : 00C8
      Payload hex      : 00C8

    :param air_quality_aqi: Air quality index / VOC measurement value.
    :type air_quality_aqi: int

    Sample Packet Logs:
    # .I --- 02:248945 02:250708 --:------ 3110 004 0000C820  # cooling, 100%
    # .I --- 21:042656 --:------ 21:042656 3110 004 00000010  # heating, 0%
    # .I --- 34:136285 --:------ 34:136285 3120 007 0070B0000000FF  # every ~3:45:00!
    # RP --- 20:008749 18:142609 --:------ 3120 007 0070B000009CFF
    # .I --- 37:258565 --:------ 37:258565 3120 007 0080B0010003FF
    """

    _STRUCT_FMT: ClassVar[str] = ">H"

    air_quality_aqi: int | None = None
    _header: int = 0
    _unknown_0: str | None = None
    _unknown_5: str | None = None
    _unknown_2: str | None = None
    _raw_4b: bytes | None = None

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack air quality binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked HvacAirQualityPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 2 bytes.
        """
        if len(raw_data) < 2:
            raise ValueError(f"Invalid payload length: {len(raw_data)}")
        if len(raw_data) == 2:
            (aqi,) = struct.unpack_from(">H", raw_data, 0)
            return cls(air_quality_aqi=aqi)
        hdr = raw_data[0]
        if len(raw_data) == 4:
            return cls(_header=hdr, _raw_4b=raw_data)
        if len(raw_data) >= 7:
            u0 = raw_data[1:5].hex().upper()
            u5 = raw_data[5:6].hex().upper()
            u2 = raw_data[6:7].hex().upper()
            return cls(_header=hdr, _unknown_0=u0, _unknown_5=u5, _unknown_2=u2)

        (aqi,) = struct.unpack_from(">H", raw_data, 0)
        return cls(_header=hdr, air_quality_aqi=aqi)

    def to_bytes(self) -> bytes:
        """Pack air quality data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        if self._raw_4b is not None:
            return self._raw_4b
        if (
            self._unknown_0 is not None
            and self._unknown_5 is not None
            and self._unknown_2 is not None
        ):
            return (
                bytes([self._header])
                + bytes.fromhex(self._unknown_0)
                + bytes.fromhex(self._unknown_5)
                + bytes.fromhex(self._unknown_2)
            )
        val = 0 if self.air_quality_aqi is None else self.air_quality_aqi
        return struct.pack(">H", val)

    def to_dict(self, msg: Any = None) -> dict[str, Any]:
        """Convert air quality payload to legacy dictionary format.

        :param msg: Optional message context object.
        :type msg: Any
        :returns: Decoded air quality dictionary.
        :rtype: dict[str, Any]
        """
        if self._raw_4b is not None:
            b = self._raw_4b
            dm = round(b[2] / 200.0, 3)
            if b[3] == 0x20:
                md = "cooling"
            elif b[3] == 0x10:
                md = "heating"
            elif b[3] == 0x00:
                md = "disabled"
            else:
                md = f"{b[3]:02X}"
            res: dict[str, Any] = {"mode": md}
            if md != "disabled":
                res["demand"] = dm
            if b[0] != 0:
                res["zone_idx"] = f"{b[0]:02X}"
            return res
        if self._unknown_0 is not None:
            return {
                "unknown_0": self._unknown_0,
                "unknown_5": self._unknown_5,
                "unknown_2": self._unknown_2,
            }
        return {"air_quality_aqi": self.air_quality_aqi}


# ----------------------------------------------------------------------


@register_payload("31D9")
@dataclass(frozen=True, slots=True)
class HvacBypassStatePayload(PayloadBase):
    """HVAC bypass damper state payload (Opcode 31D9, 31E0).

    Long payloads (Orcon, Brofer) send raw hex bytes for fan_mode, while
    short payloads (Vasco, ClimaRad) use semantic mappings. See ramses_cc#723.

    2-byte Bypass State binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Bypass Position (0-100%)     : 64 (100%)
      +1       B      1B   Bypass Mode Flags            : 00
      --------------------------------------------------------------
      Field-spaced hex : 64 00
      Payload hex      : 6400

    :param bypass_position: Bypass damper position percentage (0-100).
    :type bypass_position: int
    :param mode_flags: Bypass mode flags byte.
    :type mode_flags: int

    Protocol Notes:
    # NOTE: Itho and ClimaRad use 0x00-C8 for %, whilst Nuaire uses 0x00-64
    # NOTE: 31D9[4:6] is fan_speed (ClimaRad minibox, Itho) *or* fan_mode (Orcon, Vasco)
    # Orcon and Brofer long payloads provide accurate fan speed in 31DA.
    # Itho formats end with "00", whereas Orcon/Brofer formats end with "04" or "08".
    # Fan Mode Lookup 1 for Vasco codes
    # _31D9_FAN_INFO for Vasco D60 HRU and ClimaRad Minibox, S-Fan
    # From an Orcon 15RF Display
    # 16 Actual supply flow rate (m3/h) SZ_SUPPLY_FLOW (Orcon is m3/h, data is L/s)
    """

    _STRUCT_FMT: ClassVar[str] = ">BB"

    bypass_position: int | None = None
    mode_flags: int | None = None
    _header: int = 0
    _flags_byte: int = 0
    _speed_byte: int = 0
    _raw_len: int = 2
    _unknown_16: str | None = None

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack bypass damper state binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked HvacBypassStatePayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 2 bytes.
        """
        if len(raw_data) < 2:
            raise ValueError(f"Invalid payload length: {len(raw_data)}")
        if len(raw_data) == 2:
            return cls(
                bypass_position=raw_data[0],
                mode_flags=raw_data[1],
                _header=0,
                _flags_byte=raw_data[0],
                _speed_byte=raw_data[1],
                _raw_len=2,
            )
        u16 = f"{raw_data[16]:02X}" if len(raw_data) >= 17 else None
        return cls(
            bypass_position=raw_data[2],
            mode_flags=raw_data[1],
            _header=raw_data[0],
            _flags_byte=raw_data[1],
            _speed_byte=raw_data[2],
            _raw_len=len(raw_data),
            _unknown_16=u16,
        )

    def to_bytes(self) -> bytes:
        """Pack bypass damper state data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        if self._raw_len == 2:
            return bytes([self.bypass_position or 0, self.mode_flags or 0])
        return bytes([self._header, self._flags_byte, self._speed_byte])

    def to_dict(self, msg: Any = None) -> dict[str, Any]:
        """Convert 31D9 payload to legacy dictionary format.

        :param msg: Optional message context object.
        :type msg: Any
        :returns: Decoded bypass damper state dictionary.
        :rtype: dict[str, Any]
        """
        if self._raw_len == 2:
            return {
                "bypass_position": self.bypass_position,
                "mode_flags": self.mode_flags,
            }
        flg = self._flags_byte
        spd = self._speed_byte
        is_bound_rem = (
            msg is not None
            and getattr(msg, "src", None)
            and getattr(msg.src, "id", "").startswith("29:")
            and getattr(getattr(msg, "dst", None), "id", "").startswith("37:")
        )
        is_4b_orcon = self._raw_len == 4
        if (
            msg is not None
            and getattr(msg, "src", None)
            and (
                getattr(msg.src, "id", "").startswith("29:")
                or getattr(msg.src, "id", "").startswith("32:")
            )
            and not is_bound_rem
            and not is_4b_orcon
        ):
            fan_mode_map = {
                0: "off",
                1: "1 (trickle)",
                2: "2 (low)",
                3: "3 (medium)",
                4: "4 (boost)",
                5: "auto",
                0xC8: "III (boost)",
                0x50: "I (low)",
                0x1E: "0 (very low)",
            }
        else:
            fan_mode_map = {0: "off", 5: "auto"}
        res: dict[str, Any] = {}
        if not (self._unknown_16 is not None and self._unknown_16 != "00"):
            res["exhaust_fan_speed"] = None if spd == 0xFF else spd / 200.0
        # For long-payload devices (Orcon, Brofer, etc.), unmapped fan_mode
        # raw bytes (e.g. 0x04, 0xC8, 0xFF) are NOT semantic names and conflict
        # with semantic fan_mode from 22F4/22F1. Vasco/ClimaRad short payloads
        # are mapped via fan_mode_map, while unmapped bytes return raw hex string.
        # See ramses_cc issue 723.
        if self._unknown_16 is not None and self._unknown_16 != "00":
            res["fan_mode"] = f"{spd:02X}"
        else:
            res["fan_mode"] = fan_mode_map.get(spd, f"{spd:02X}")
        res["passive"] = bool(flg & 0x02)
        res["damper_only"] = bool(flg & 0x04)
        res["filter_dirty"] = bool(flg & 0x20)
        res["frost_cycle"] = bool(flg & 0x10)
        res["has_fault"] = bool(flg & 0x80)

        if self._unknown_16 is not None:
            res["unknown_16"] = self._unknown_16
        if (
            msg is not None
            and getattr(msg, "_pkt", None)
            and getattr(msg._pkt, "_seqn", None)
        ):
            res["seqx_num"] = msg._pkt._seqn
        return res


# ----------------------------------------------------------------------


@register_payload("31DA")
@dataclass(frozen=True, slots=True)
class HvacVentilationStatePayload(PayloadBase):
    """Extended ventilation state payload (Opcode 31DA).

    Handles null-marker normalisation (EF/FF fan_info) and fault
    codes for Ventura V1x and Orcon hardware. See ramses_cc#742.

    Variable-length Extended Ventilation State binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       vB     vB   Raw State Byte Array         : 00 01 02
      --------------------------------------------------------------
      Field-spaced hex : 000102
      Payload hex      : 000102

    :param raw_bytes: Raw binary payload bytes.
    :type raw_bytes: bytes
    """

    _STRUCT_FMT: ClassVar[str] = ">BB"

    raw_bytes: bytes

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack extended ventilation state payload.

        :param raw_data: Raw binary payload bytes.
        :type raw_data: bytes
        :returns: Unpacked HvacVentilationStatePayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 2 bytes.
        """
        if len(raw_data) < 2:
            raise ValueError(f"Invalid payload length for 31DA: {len(raw_data)}")
        return cls(raw_bytes=raw_data)

    def to_bytes(self) -> bytes:
        """Pack extended ventilation state payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        return self.raw_bytes

    def to_dict(self) -> dict[str, Any]:
        """Convert extended ventilation state payload to legacy dictionary format.

        :returns: Decoded ventilation parameters dictionary.
        :rtype: dict[str, Any]
        """
        raw_hex = self.raw_bytes.hex().upper()
        if len(raw_hex) < 58:
            return {"raw_bytes": self.raw_bytes.hex()}

        res: dict[str, Any] = {}

        def _parse_val(
            hex_str: str,
            key: str,
            scale: float = 1.0,
            null_hex: str | tuple[str, ...] = ("EF", "7FFF", "FFFF"),
            is_signed: bool = False,
        ) -> None:
            nulls = (null_hex,) if isinstance(null_hex, str) else null_hex
            if hex_str in nulls:
                res[key] = None
                return
            b0 = hex_str[:2]
            if is_signed:
                if (int(b0, 16) & 0xE0) in (0x80, 0x90):
                    b0_norm = f"8{b0[1]}" if b0.startswith("9") else b0
                    fault_map = {
                        "80": "short_circuit",
                        "81": "open_circuit",
                        "82": "unavailable",
                        "83": "out_of_range_high",
                        "84": "out_of_range_low",
                        "85": "unreliable",
                    }
                    res[f"{key}_fault"] = fault_map.get(b0_norm, f"invalid_{hex_str}")
                    return
                val = int(hex_str, 16)
                if val >= 32768:
                    val -= 65536
                res[key] = val / scale
                return

            if (
                b0.startswith("F")
                or b0.startswith("D")
                or (b0.startswith("8") and key != SZ_BYPASS_POSITION)
                or b0 in ("FC", "FD", "FE")
            ):
                if key == SZ_AIR_QUALITY and b0 == "EF":
                    res[key] = None
                    return
                fault_map = {
                    "F0": "open_circuit"
                    if key == SZ_BYPASS_POSITION
                    else "short_circuit",
                    "F1": "short_circuit"
                    if key == SZ_BYPASS_POSITION
                    else "open_circuit",
                    "F2": "unavailable",
                    "F3": "out_of_range_high",
                    "F4": "out_of_range_low",
                    "F5": "unreliable",
                    "80": "short_circuit",
                    "81": "open_circuit",
                    "82": "unavailable",
                    "83": "out_of_range_high",
                    "84": "out_of_range_low",
                    "85": "unreliable",
                    "FD": "stuck_valve",
                    "FE": "stuck_actuator",
                    "FF": "other_fault"
                    if key == SZ_BYPASS_POSITION
                    else f"invalid_{hex_str}",
                }
                res[f"{key}_fault"] = fault_map.get(b0, f"invalid_{hex_str}")
                return
            val_num = (
                int(hex_str[:2], 16)
                if len(hex_str) == 4 and scale == 200.0
                else int(hex_str, 16)
            )
            res[key] = val_num if scale == 1.0 else val_num / scale

        # 1. Exhaust Fan Speed [38:40]
        val_38 = raw_hex[38:40]
        if val_38 != "FF":
            res[SZ_EXHAUST_FAN_SPEED] = int(val_38, 16) / 200
        else:
            res[SZ_EXHAUST_FAN_SPEED] = None

        # 2. Fan Info [36:38]
        val_36 = raw_hex[36:38]
        if val_36 in ("EF", "FF"):
            res[SZ_FAN_INFO] = None
            res["_unknown_fan_info_flags"] = [0, 0, 0]
        elif int(val_36, 16) & 0xE0 not in (0x00, 0x20, 0x40, 0x60, 0x80):
            res[SZ_FAN_INFO] = f"-unknown 0x{val_36}-"
            res["_unknown_fan_info_flags"] = [
                (int(val_36, 16) >> x) & 1 for x in range(7, 4, -1)
            ]
        else:
            res[SZ_FAN_INFO] = _31DA_FAN_INFO.get(int(val_36, 16) & 0x1F, "off")
            res["_unknown_fan_info_flags"] = [
                (int(val_36, 16) >> x) & 1 for x in range(7, 4, -1)
            ]

        # 3. Air Quality [2:6]
        _parse_val(raw_hex[2:6], SZ_AIR_QUALITY, scale=200.0, null_hex="EF00")
        if SZ_AIR_QUALITY in res and res[SZ_AIR_QUALITY] is not None:
            val_2_basis = raw_hex[4:6]
            basis_map = {"10": "voc", "20": "co2", "40": "rel_humidity"}
            res[SZ_AIR_QUALITY_BASIS] = basis_map.get(
                val_2_basis, f"unknown_{val_2_basis}"
            )

        # 4. CO2 Level [6:10]
        _parse_val(raw_hex[6:10], SZ_CO2_LEVEL, scale=1.0, null_hex="7FFF")

        # 5. Indoor Humidity [10:12] (EF = spec null marker; 0x00 normalisation in quirks.py)
        _parse_val(raw_hex[10:12], SZ_INDOOR_HUMIDITY, scale=100.0, null_hex="EF")

        # 6. Outdoor Humidity [12:14] (EF = spec null marker; 0x00 normalisation in quirks.py)
        _parse_val(raw_hex[12:14], SZ_OUTDOOR_HUMIDITY, scale=100.0, null_hex="EF")

        # 7-10. Temps: Exhaust [14:18], Supply [18:22], Indoor [22:26], Outdoor [26:30]
        _parse_val(
            raw_hex[14:18],
            SZ_EXHAUST_TEMP,
            scale=100.0,
            null_hex=("7FFF", "31FF"),
            is_signed=True,
        )
        _parse_val(
            raw_hex[18:22],
            SZ_SUPPLY_TEMP,
            scale=100.0,
            null_hex=("7FFF", "31FF"),
            is_signed=True,
        )
        _parse_val(
            raw_hex[22:26],
            SZ_INDOOR_TEMP,
            scale=100.0,
            null_hex=("7FFF", "31FF"),
            is_signed=True,
        )
        _parse_val(
            raw_hex[26:30],
            SZ_OUTDOOR_TEMP,
            scale=100.0,
            null_hex=("7FFF", "31FF"),
            is_signed=True,
        )

        # 11. Capabilities [30:34]
        val_30 = raw_hex[30:34]
        if val_30 == "7FFF":
            res[SZ_SPEED_CAPABILITIES] = None
        else:
            abilities_map = {
                15: "off",
                14: "low_med_high",
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
            cap_val = int(val_30, 16)
            res[SZ_SPEED_CAPABILITIES] = [
                name for bit, name in abilities_map.items() if cap_val & (1 << bit)
            ]

        # 12. Bypass Position [34:36]
        _parse_val(raw_hex[34:36], SZ_BYPASS_POSITION, scale=200.0, null_hex="EF")

        # 13. Supply Fan Speed [40:42]
        val_40 = raw_hex[40:42]
        if val_40 != "FF":
            res[SZ_SUPPLY_FAN_SPEED] = int(val_40, 16) / 200
        else:
            res[SZ_SUPPLY_FAN_SPEED] = None

        # 14. Remaining Minutes [42:46]
        val_42 = raw_hex[42:46]
        if val_42 == "0000":
            res[SZ_REMAINING_MINS] = 0
        elif val_42 in ("3FFF", "FFFF"):
            res[SZ_REMAINING_MINS] = None
        else:
            res[SZ_REMAINING_MINS] = int(val_42, 16)

        # 15-16. Heaters: Post [46:48], Pre [48:50]
        _parse_val(raw_hex[46:48], SZ_POST_HEAT, scale=200.0, null_hex="EF")
        _parse_val(raw_hex[48:50], SZ_PRE_HEAT, scale=200.0, null_hex="EF")

        # 17-18. Flows: Supply [50:54], Exhaust [54:58]
        _parse_val(raw_hex[50:54], SZ_SUPPLY_FLOW, scale=100.0, null_hex="7FFF")
        _parse_val(raw_hex[54:58], SZ_EXHAUST_FLOW, scale=100.0, null_hex="7FFF")

        if len(raw_hex) > 58:
            res["_extra"] = raw_hex[58:]

        return res


# ----------------------------------------------------------------------


@register_payload("4401")
@dataclass(frozen=True, slots=True)
class HvacFaultLogEntryPayload(PayloadBase):
    """HVAC fault log entry payload (Opcode 4401).

    2-byte Fault Log Entry binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Fault Index (uint8)          : 00
      +1       B      1B   Fault Code (uint8)           : 01
      --------------------------------------------------------------
      Field-spaced hex : 00 01
      Payload hex      : 0001

    :param fault_idx: Fault log index byte.
    :type fault_idx: int
    :param fault_code: HVAC fault code integer.
    :type fault_code: int

    Sample Packet Logs:
    # 2022-07-28T14:21:38.895354 095  W --- 37:010164 37:010151 --:------ 4401 020 10  7E-E99E90C8  00-E99E90C7-3BFF  7E-E99E90C8-000B
    # 2022-07-28T14:21:57.414447 076 RQ --- 20:225479 20:257336 --:------ 4401 020 10  2E-E99E90DB  00-00000000-0000  00-00000000-000B
    # 2022-07-28T14:21:57.625474 045  I --- 20:257336 20:225479 --:------ 4401 020 10  2E-E99E90DB  00-E99E90DA-F0FF  BD-00000000-000A
    # 2022-07-28T14:22:02.932576 088 RQ --- 37:010188 20:257336 --:------ 4401 020 10  22-E99E90E0  00-00000000-0000  00-00000000-000B
    # 2022-07-28T14:22:03.053744 045  I --- 20:257336 37:010188 --:------ 4401 020 10  22-E99E90E0  00-E99E90E0-75FF  BD-00000000-000A
    # 2022-07-28T14:22:20.516363 045 RQ --- 20:255710 20:257400 --:------ 4401 020 10  0B-E99E90F2  00-00000000-0000  00-00000000-000B
    # 2022-07-28T14:22:20.571640 085  I --- 20:255251 20:229597 --:------ 4401 020 10  39-E99E90F1  00-E99E90F1-5CFF  40-00000000-000A
    # 2022-07-28T14:22:20.648696 058  I --- 20:257400 20:255710 --:------ 4401 020 10  0B-E99E90F2  00-E99E90F1-D4FF  DA-00000000-000B
    # 2022-11-03T23:00:04.854479 088 RQ --- 20:256717 37:013150 --:------ 4401 020 10  00-00259261  00-00000000-0000  00-00000000-0063
    # 2022-11-03T23:00:05.102491 045  I --- 37:013150 20:256717 --:------ 4401 020 10  00-00259261  00-000C9E4C-1800  00-00000000-0063
    # 2022-11-03T23:00:17.820659 072  I --- 20:256112 20:255825 --:------ 4401 020 10  00-00F1EB91  00-00E8871B-B700  00-00000000-0063
    # 2022-11-03T23:01:25.495391 065  I --- 20:257732 20:257680 --:------ 4401 020 10  00-002E9C98  00-00107923-9E00  00-00000000-0063
    # 2022-11-03T23:01:33.753467 066 RQ --- 20:257732 20:256112 --:------ 4401 020 10  00-0010792C  00-00000000-0000  00-00000000-0063
    # 2022-11-03T23:01:33.997485 072  I --- 20:256112 20:257732 --:------ 4401 020 10  00-0010792C  00-00E88767-AD00  00-00000000-0063
    # 2022-11-03T23:01:52.391989 090  I --- 20:256717 20:255301 --:------ 4401 020 10  00-009870E1  00-002592CC-6300  00-00000000-0063
    """

    _STRUCT_FMT: ClassVar[str] = ">BB"

    fault_idx: int
    fault_code: int

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack HVAC fault log entry binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked HvacFaultLogEntryPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 2 bytes.
        """
        if len(raw_data) < 2:
            raise ValueError(f"Invalid payload length for 4401: {len(raw_data)}")
        idx, code = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(fault_idx=idx, fault_code=code)

    def to_bytes(self) -> bytes:
        """Pack HVAC fault log entry data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        return struct.pack(self._STRUCT_FMT, self.fault_idx, self.fault_code)


# ----------------------------------------------------------------------


@register_payload("4E01")
@dataclass(frozen=True, slots=True)
class HvacSpiderTemperaturesPayload(PayloadBase):
    """Spider HVAC temperatures payload (Opcode 4E01).

    4-byte Spider Temperatures binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Header / Domain Byte         : 00
      +1       h      2B   Temperature (int16 * 100)    : 08 34 (21.00°C)
      +3       B      1B   Trailer Byte                 : 00
      --------------------------------------------------------------
      Field-spaced hex : 00 0834 00
      Payload hex      : 00083400

    :param hdr: Domain index / header byte.
    :type hdr: int
    :param temperatures: Tuple of temperature values in °C or None.
    :type temperatures: tuple[float | None, ...]
    :param trailer: Trailer byte.
    :type trailer: int

    Sample Packet Logs & Protocol Notes:
    # .I --- 02:248945 02:250708 --:------ 4E01 018 00-7FFF7FFF7FFF09077FFF7FFF7FFF7FFF-00
    # .I --- 02:250984 02:250704 --:------ 4E01 018 00-7FFF7FFF7FFF7FFF08387FFF7FFF7FFF-00
    # .I --- 02:250704 02:250984 --:------ 4E0D 002 0100  # Itho Autotemp
    # .I --- 02:250704 02:250984 --:------ 4E0D 002 0101  # context?
    # .I --- 02:250984 02:250704 --:------ 4E16 007 00000000000000  # Itho Autotemp: slave -> master
    # TODO: hvac_4e16 - Itho spider/autotemp
    # TODO: Fan characteristics - Itho
    # TODO: Potentiometer control - Itho
    """

    _STRUCT_FMT: ClassVar[str] = ">Bhb"

    hdr: int
    temperatures: tuple[float | None, ...]
    trailer: int

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack Spider temperatures binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked HvacSpiderTemperaturesPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 4 bytes.
        """
        if len(raw_data) < 4:
            raise ValueError(f"Invalid payload length for 4E01: {len(raw_data)}")
        hdr = raw_data[0]
        trailer = raw_data[-1]
        temp_bytes = raw_data[1:-1]
        temps: list[float | None] = []
        for i in range(0, len(temp_bytes), 2):
            (val,) = struct.unpack_from(">h", temp_bytes, i)
            temps.append(None if val in (0x31FF, 0x7FFF) else val / 100.0)
        return cls(hdr=hdr, temperatures=tuple(temps), trailer=trailer)

    def to_bytes(self) -> bytes:
        """Pack Spider temperatures data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        temp_bytes = bytearray()
        for t in self.temperatures:
            val = 0x7FFF if t is None else int(round(t * 100.0))
            temp_bytes.extend(struct.pack(">h", val))
        return bytes([self.hdr]) + temp_bytes + bytes([self.trailer])

    def to_dict(self, msg: Any = None) -> dict[str, Any]:
        """Convert Spider temperatures payload to legacy dictionary format.

        :param msg: Optional message context object.
        :type msg: Any
        :returns: Decoded temperatures dictionary.
        :rtype: dict[str, Any]
        """
        return {"temperatures": list(self.temperatures)}


# ----------------------------------------------------------------------


@register_payload("22C9")
@register_payload("4E02")
@dataclass(frozen=True, slots=True)
class HvacSpiderSetpointBoundsPayload(PayloadBase):
    """Spider HVAC setpoint bounds payload (Opcode 22C9, 4E02).

    7-byte Spider Setpoint Bounds binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Header / Domain Byte         : 00
      +1       h      2B   Setpoint Min (int16 * 100)   : 08 34 (21.00°C)
      +3       B      1B   Mode Code (uint8)            : 04 (Heat)
      +4       h      2B   Setpoint Max (int16 * 100)   : 08 98 (22.00°C)
      --------------------------------------------------------------
      Field-spaced hex : 00 0834 04 0898
      Payload hex      : 000834040898

    :param hdr: Domain index / header byte.
    :type hdr: int
    :param mode_code: Mode code integer.
    :type mode_code: int
    :param setpoint_bounds: Tuple of setpoint bound pairs or None.
    :type setpoint_bounds: tuple[tuple[float | None, float | None] | None, ...]

    Protocol Notes & Sample Packet Logs:
    # setpoint_bounds, TODO: max length = 24?
    # .I --- 02:001107 --:------ 02:001107 22C9 024 00-0834-0A28-01-0108340A2801-0208340A2801-0308340A2801
    # .I --- 02:001107 --:------ 02:001107 22C9 006 04-0834-0A28-01
    # .I --- 21:064743 --:------ 21:064743 22C9 006 00-07D0-0834-02
    # .W --- 21:064743 02:250708 --:------ 22C9 006 03-07D0-0834-02
    # .I --- 02:250708 21:064743 --:------ 22C9 008 03-07D0-7FFF-020203
    """

    _STRUCT_FMT: ClassVar[str] = ">BhBh"

    hdr: int
    mode_code: int
    setpoint_bounds: tuple[tuple[float | None, float | None] | None, ...]

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack Spider setpoint bounds binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked HvacSpiderSetpointBoundsPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 7 bytes.
        """
        if len(raw_data) < 6:
            raise ValueError(f"Invalid payload length for 4E02: {len(raw_data)}")
        hdr = raw_data[0]
        num_pairs = (len(raw_data) - 2) // 4
        min_bytes = raw_data[1 : 1 + num_pairs * 2]
        mode_code = raw_data[1 + num_pairs * 2]
        max_bytes = raw_data[2 + num_pairs * 2 :]
        bounds: list[tuple[float | None, float | None] | None] = []
        for i in range(num_pairs):
            (min_val,) = struct.unpack_from(">h", min_bytes, i * 2)
            (max_val,) = struct.unpack_from(">h", max_bytes, i * 2)
            min_t = None if min_val in (0x31FF, 0x7FFF) else min_val / 100.0
            max_t = None if max_val in (0x31FF, 0x7FFF) else max_val / 100.0
            if min_t is None and max_t is None:
                bounds.append(None)
            else:
                bounds.append((min_t, max_t))
        return cls(hdr=hdr, mode_code=mode_code, setpoint_bounds=tuple(bounds))

    def to_bytes(self) -> bytes:
        """Pack Spider setpoint bounds data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        min_b = bytearray()
        max_b = bytearray()
        for b in self.setpoint_bounds:
            min_t, max_t = b if b is not None else (None, None)
            min_val = 0x7FFF if min_t is None else int(round(min_t * 100.0))
            max_val = 0x7FFF if max_t is None else int(round(max_t * 100.0))
            min_b.extend(struct.pack(">h", min_val))
            max_b.extend(struct.pack(">h", max_val))
        return bytes([self.hdr]) + min_b + bytes([self.mode_code]) + max_b

    def to_dict(self, msg: Any = None) -> dict[str, Any]:
        """Convert Spider setpoint bounds payload to legacy dictionary layout.

        :param msg: Optional message context object.
        :type msg: Any
        :returns: Decoded setpoint bounds dictionary.
        :rtype: dict[str, Any]
        """
        mode_str = {0: "off", 2: "cool", 4: "heat"}.get(
            self.mode_code, f"{self.mode_code:02X}"
        )
        return {
            "mode": mode_str,
            "setpoint_bounds": list(self.setpoint_bounds),
        }


# ----------------------------------------------------------------------


@register_payload("4E04")
@dataclass(frozen=True, slots=True)
class HvacSpiderModePayload(PayloadBase):
    """Spider HVAC mode payload (Opcode 4E04).

    3-byte Spider Mode binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Header / Domain Byte         : 00
      +1       B      1B   Mode Code (uint8)            : 01 (Heat)
      +2       B      1B   Trailer Byte                 : 00
      --------------------------------------------------------------
      Field-spaced hex : 00 01 00
      Payload hex      : 000100

    :param hdr: Domain index / header byte.
    :type hdr: int
    :param mode_code: Mode code integer.
    :type mode_code: int
    :param unknown_2: Hex string for unknown trailing byte.
    :type unknown_2: str
    """

    _STRUCT_FMT: ClassVar[str] = ">BBB"

    hdr: int
    mode_code: int
    unknown_2: str

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack Spider mode binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked HvacSpiderModePayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 3 bytes.
        """
        if len(raw_data) < 3:
            raise ValueError(f"Invalid payload length for 4E04: {len(raw_data)}")
        h_val, m_val, u_val = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(hdr=h_val, mode_code=m_val, unknown_2=f"{u_val:02X}")

    def to_bytes(self) -> bytes:
        """Pack Spider mode data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        return struct.pack(
            self._STRUCT_FMT, self.hdr, self.mode_code, int(self.unknown_2, 16)
        )

    def to_dict(self, msg: Any = None) -> dict[str, Any]:
        """Convert Spider mode payload to legacy dictionary format.

        :param msg: Optional message context object.
        :type msg: Any
        :returns: Decoded mode dictionary.
        :rtype: dict[str, Any]
        """
        mode_str = {0: "off", 1: "heat", 2: "cool", 4: "heat"}.get(
            self.mode_code, f"{self.mode_code:02X}"
        )
        return {"mode": mode_str, "_unknown_2": self.unknown_2}


# ----------------------------------------------------------------------


@register_payload("4E0D")
# ----------------------------------------------------------------------

@register_payload("4E14")
# ----------------------------------------------------------------------

@register_payload("4E15")
@dataclass(frozen=True, slots=True)
class HvacSpiderStatusPayload(PayloadBase):
    """Spider HVAC status payload (Opcode 4E15).

    2-byte Spider Status binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Header / Domain Byte         : 00
      +1       B      1B   Status Flags (uint8)         : 01
      --------------------------------------------------------------
      Field-spaced hex : 00 01
      Payload hex      : 0001

    :param hdr: Domain index / header byte.
    :type hdr: int
    :param flags: Status bit flags byte.
    :type flags: int
    """

    _STRUCT_FMT: ClassVar[str] = ">BB"

    hdr: int
    flags: int

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack Spider status binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked HvacSpiderStatusPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 2 bytes.
        """
        if len(raw_data) < 2:
            raise ValueError(f"Invalid payload length for 4E15: {len(raw_data)}")
        h_val, f_val = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(hdr=h_val, flags=f_val)

    def to_bytes(self) -> bytes:
        """Pack Spider status data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        return struct.pack(self._STRUCT_FMT, self.hdr, self.flags)

    def to_dict(self, msg: Any = None) -> dict[str, Any]:
        """Convert Spider status payload to legacy dictionary layout.

        :param msg: Optional message context object.
        :type msg: Any
        :returns: Decoded status dictionary.
        :rtype: dict[str, Any]
        """
        flg = self.flags
        return {
            "is_cooling": bool(flg & 0x01),
            "is_heating": bool(flg & 0x02),
            "is_dhw_ing": bool(flg & 0x04),
        }


# ----------------------------------------------------------------------


@register_payload("4E16")
# ----------------------------------------------------------------------

@register_payload("4E20")
# ----------------------------------------------------------------------

@register_payload("4E21")
@dataclass(frozen=True, slots=True)
class HvacFaultStatusPayload(PayloadBase):
    """HVAC fault log status payload (Opcode 4E01, 4E02-4E21).

    2-byte Fault Status binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Fault Code (uint8)           : 00
      +1       B      1B   Severity / Flags             : 00
      --------------------------------------------------------------
      Field-spaced hex : 00 00
      Payload hex      : 0000

    :param fault_code: HVAC fault code.
    :type fault_code: int
    :param flags: Fault status flags.
    :type flags: int

    Protocol Notes:
    # temperatures (see: 4e02) - Itho spider/autotemp
    # setpoint_bounds (see: 4e01) - Itho spider/autotemp
    # WIP: AT outdoor low - Itho spider/autotemp
    # AT fault circulation - Itho spider/autotemp
    # wpu_state (hvac state) - Itho spider/autotemp
    """

    _STRUCT_FMT: ClassVar[str] = ">BB"

    fault_code: int
    flags: int

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack fault status binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked HvacFaultStatusPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 2 bytes.
        """
        if len(raw_data) < 2:
            raise ValueError(f"Invalid payload length: {len(raw_data)}")
        code, flg = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(fault_code=code, flags=flg)

    def to_bytes(self) -> bytes:
        """Pack fault status data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        return struct.pack(self._STRUCT_FMT, self.fault_code, self.flags)


# ----------------------------------------------------------------------


@register_payload("12B0")
class WindowStatePayload(PayloadBase):
    """Master payload dispatcher and base class for Opcode 12B0."""

    VARIANTS: ClassVar[tuple[type[PayloadBase], ...]] = ()

    zone_idx: int
    window_open: bool | None

    def __new__(
        cls,
        zone_idx: int = 0,
        window_open: bool | None = None,
    ) -> Any:
        """Construct WindowState payload variant dynamically from arguments."""
        if cls is not WindowStatePayload:
            return super().__new__(cls)
        return WindowState3BPayload(zone_idx=zone_idx, window_open=window_open)

    def to_dict(self) -> dict[str, Any]:
        """Convert window state payload to legacy dictionary layout."""
        z_idx = getattr(self, "zone_idx", 0)
        open_val = getattr(self, "window_open", None)
        idx_str = f"{z_idx:02X}"
        return {"zone_idx": idx_str, "window_open": open_val}

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> "WindowStatePayload":
        """Unpack window state binary payload, dispatching by length."""
        if len(raw_data) < 2:
            raise ValueError(f"Invalid payload length for 12B0: {len(raw_data)}")
        if len(raw_data) >= 3:
            return WindowState3BPayload.from_bytes(raw_data)
        return WindowState2BPayload.from_bytes(raw_data)

    def to_bytes(self) -> bytes:
        """Pack payload base default method.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        :raises NotImplementedError: Master dispatcher must dispatch to sub-dataclass.
        """
        raise NotImplementedError("Use concrete variant sub-dataclass")


@dataclass(frozen=True, slots=True)
class WindowState2BPayload(WindowStatePayload):
    """2-byte window state payload (Opcode 12B0).

    2-byte Window State binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Zone Index (uint8)           : 00
      +1       B      1B   Window Open Flag (0=No,1=Yes): 00
      --------------------------------------------------------------
      Field-spaced hex : 00 00
      Payload hex      : 0000
    """

    _STRUCT_FMT: ClassVar[str] = ">BB"

    zone_idx: int
    window_open: bool | None

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 2-byte window state binary payload."""
        if len(raw_data) < 2:
            raise ValueError(
                f"Invalid payload length for WindowState2BPayload: {len(raw_data)}"
            )
        z_idx, open_flag = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(zone_idx=z_idx, window_open=bool(open_flag))

    def to_bytes(self) -> bytes:
        """Pack 2-byte window state binary payload."""
        val = 0xFF if self.window_open is None else int(self.window_open)
        return struct.pack(self._STRUCT_FMT, self.zone_idx, val)


@dataclass(frozen=True, slots=True)
class WindowState3BPayload(WindowStatePayload):
    """3-byte window state payload (Opcode 12B0).

    3-byte Window State binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Zone Index (uint8)           : 00
      +1       B      1B   Window Open Flag (0=No,1=Yes): 00
      +2       B      1B   Trailing Flag Byte           : 00
      --------------------------------------------------------------
      Field-spaced hex : 00 00 00
      Payload hex      : 000000
    """

    _STRUCT_FMT: ClassVar[str] = ">BBB"

    zone_idx: int
    window_open: bool | None

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 3-byte window state binary payload."""
        if len(raw_data) < 3:
            raise ValueError(
                f"Invalid payload length for WindowState3BPayload: {len(raw_data)}"
            )
        z_idx, open_flag, _trailer = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        val = None if (open_flag, _trailer) == (0xFF, 0xFF) else bool(open_flag)
        return cls(zone_idx=z_idx, window_open=val)

    def to_bytes(self) -> bytes:
        """Pack 3-byte window state binary payload."""
        if self.window_open is None:
            return struct.pack(self._STRUCT_FMT, self.zone_idx, 0xFF, 0xFF)
        return struct.pack(self._STRUCT_FMT, self.zone_idx, int(self.window_open), 0x00)


# Update VARIANTS property after variants are defined
WindowStatePayload.VARIANTS = (
    WindowState2BPayload,
    WindowState3BPayload,
)


# ----------------------------------------------------------------------


@register_payload("31E0")
class HvacVentilationDemandPayload(PayloadBase):
    """Master payload dispatcher and base class for Opcode 31E0.

    Sample Packet Logs & Protocol Notes:
    # ufc_demand, HVAC (Itho autotemp / spider)
    # .I --- 37:005302 32:132403 --:------ 31E0 008 00-0000-00 01-0064-00
    # .I --- 29:146052 32:023459 --:------ 31E0 003 00-0000
    # .I --- 29:146052 32:023459 --:------ 31E0 003 00-00C8
    """

    VARIANTS: ClassVar[tuple[type[PayloadBase], ...]] = ()
    flags: int
    demand_percent: float

    def __new__(
        cls,
        flags: int = 0,
        demand_percent: float = 0.0,
    ) -> Any:
        """Construct HvacVentilationDemand payload variant dynamically from arguments."""
        if cls is not HvacVentilationDemandPayload:
            return super().__new__(cls)
        return HvacVentilationDemand4BPayload(
            flags=flags, demand_percent=demand_percent
        )

    def to_dict(self, msg: Any = None) -> dict[str, Any]:
        """Convert ventilation demand payload to legacy dictionary layout."""
        flg = getattr(self, "flags", 0)
        dem = getattr(self, "demand_percent", 0.0)
        return {"flags": f"{flg:02X}", "vent_demand": dem}

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> PayloadBase | list[PayloadBase]:
        """Unpack ventilation demand binary payload, dispatching by length."""
        if len(raw_data) >= 8 and len(raw_data) % 4 == 0:
            return [
                HvacVentilationDemand4BPayload.from_bytes(raw_data[i : i + 4])
                for i in range(0, len(raw_data), 4)
            ]
        if len(raw_data) < 3:
            return HvacVentilationDemand2BPayload.from_bytes(raw_data)
        if len(raw_data) == 4:
            return HvacVentilationDemand4BPayload.from_bytes(raw_data)
        return HvacVentilationDemand3BPayload.from_bytes(raw_data)

    def to_bytes(self) -> bytes:
        """Pack payload base default method.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        :raises NotImplementedError: Master dispatcher must dispatch to sub-dataclass.
        """
        raise NotImplementedError("Use concrete variant sub-dataclass")


@dataclass(frozen=True, slots=True)
class HvacVentilationDemand2BPayload(HvacVentilationDemandPayload):
    """2-byte ventilation demand payload (Opcode 31E0).

    2-byte Ventilation Demand binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Flags (uint8)                : 00
      +1       B      1B   Demand uint8 (0-200)         : 64 (50%)
      --------------------------------------------------------------
      Field-spaced hex : 00 64
      Payload hex      : 0064
    """

    _STRUCT_FMT: ClassVar[str] = ">BB"

    flags: int
    demand_percent: float

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 2-byte ventilation demand payload."""
        if len(raw_data) < 2:
            raise ValueError(
                f"Invalid payload length for HvacVentilationDemand2BPayload: {len(raw_data)}"
            )
        flg, demand_raw = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(flags=flg, demand_percent=demand_raw / 200.0)

    def to_bytes(self) -> bytes:
        """Pack 2-byte ventilation demand payload."""
        d_raw = min(200, max(0, int(round(self.demand_percent * 200.0))))
        return struct.pack(self._STRUCT_FMT, self.flags, d_raw)


@dataclass(frozen=True, slots=True)
class HvacVentilationDemand3BPayload(HvacVentilationDemandPayload):
    """3-byte ventilation demand payload (Opcode 31E0).

    3-byte Ventilation Demand binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Flags (uint8)                : 00
      +1       B      1B   Padding Byte                 : 00
      +2       B      1B   Demand uint8 (0-200)         : 64 (50%)
      --------------------------------------------------------------
      Field-spaced hex : 00 00 64
      Payload hex      : 000064
    """

    _STRUCT_FMT: ClassVar[str] = ">BBB"

    flags: int
    demand_percent: float

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 3-byte ventilation demand payload."""
        if len(raw_data) < 3:
            raise ValueError(
                f"Invalid payload length for HvacVentilationDemand3BPayload: {len(raw_data)}"
            )
        flg, _p, demand_raw = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(flags=flg, demand_percent=demand_raw / 200.0)

    def to_bytes(self) -> bytes:
        """Pack 3-byte ventilation demand payload."""
        d_raw = min(200, max(0, int(round(self.demand_percent * 200.0))))
        return struct.pack(self._STRUCT_FMT, self.flags, 0, d_raw)


@dataclass(frozen=True, slots=True)
class HvacVentilationDemand4BPayload(HvacVentilationDemandPayload):
    """4-byte ventilation demand payload (Opcode 31E0).

    4-byte Ventilation Demand binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Header / Domain              : 00
      +1       B      1B   Flags (uint8)                : 00
      +2       B      1B   Demand uint8 (0-200)         : 64 (50%)
      +3       B      1B   Padding / Status Byte        : 00
      --------------------------------------------------------------
      Field-spaced hex : 00 00 64 00
      Payload hex      : 00006400
    """

    _STRUCT_FMT: ClassVar[str] = ">BBBB"

    flags: int
    demand_percent: float

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 4-byte ventilation demand payload."""
        if len(raw_data) < 4:
            raise ValueError(
                f"Invalid payload length for HvacVentilationDemand4BPayload: {len(raw_data)}"
            )
        _p, flg, demand_raw, _t = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(flags=flg, demand_percent=demand_raw / 200.0)

    def to_bytes(self) -> bytes:
        """Pack 4-byte ventilation demand payload."""
        d_raw = min(200, max(0, int(round(self.demand_percent * 200.0))))
        return struct.pack(self._STRUCT_FMT, 0, self.flags, d_raw, 0)


HvacVentilationDemandPayload.VARIANTS = (
    HvacVentilationDemand2BPayload,
    HvacVentilationDemand3BPayload,
    HvacVentilationDemand4BPayload,
)


# ----------------------------------------------------------------------


@register_payload("2209")
# ----------------------------------------------------------------------

@register_payload("22C9")
@dataclass(frozen=True, slots=True)
class SetpointBoundsPayload(PayloadBase):
    """Temperature setpoint bounds payload (Opcode 2209, 22C9).

    6-byte Setpoint Bounds binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   UFH / Zone Index             : 00
      +1       h      2B   Min Temp (int16*100)         : 01 F4 (5.00°C)
      +3       h      2B   Max Temp (int16*100)         : 0E 10 (36.00°C)
      +5       B      1B   Mode Code (uint8)            : 01 (Heat)
      --------------------------------------------------------------
      Field-spaced hex : 00 01F4 0E10 01
      Payload hex      : 0001F40E1001

    :param ufh_idx: UFH or zone index byte.
    :type ufh_idx: int
    :param min_temp: Minimum setpoint temperature bound in °C.
    :type min_temp: float
    :param max_temp: Maximum setpoint temperature bound in °C.
    :type max_temp: float
    :param mode_code: Mode code integer.
    :type mode_code: int
    """

    _STRUCT_FMT: ClassVar[str] = ">BhhB"

    ufh_idx: int
    min_temp: float | None
    max_temp: float | None
    mode_code: int

    @classmethod
    def _parse_temp(cls, raw_val: int) -> float | None:
        """Decode raw 16-bit signed integer temperature bound."""
        if raw_val in (0x31FF, 0x7FFF):
            return None
        return raw_val / 100.0

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self | list[Self]:
        """Unpack setpoint bounds binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked SetpointBoundsPayload instance or list of instances.
        :rtype: Self | list[Self]
        :raises ValueError: If raw_data length is less than 6 bytes.
        """
        if len(raw_data) >= 6 and len(raw_data) % 6 == 0:
            res: list[Self] = []
            for i in range(0, len(raw_data), 6):
                idx, min_t, max_t, mode_code = struct.unpack_from(">BhhB", raw_data, i)
                res.append(
                    cls(
                        ufh_idx=idx,
                        min_temp=cls._parse_temp(min_t),
                        max_temp=cls._parse_temp(max_t),
                        mode_code=mode_code,
                    )
                )
            return res

        if len(raw_data) < 6:
            raise ValueError(f"Invalid payload length for 22C9: {len(raw_data)}")
        idx, min_t, max_t, mode_code = struct.unpack_from(">BhhB", raw_data, 0)
        return cls(
            ufh_idx=idx,
            min_temp=cls._parse_temp(min_t),
            max_temp=cls._parse_temp(max_t),
            mode_code=mode_code,
        )

    def to_bytes(self) -> bytes:
        """Pack setpoint bounds data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        min_raw = 0x7FFF if self.min_temp is None else int(round(self.min_temp * 100.0))
        max_raw = 0x7FFF if self.max_temp is None else int(round(self.max_temp * 100.0))
        return struct.pack(
            ">BhhB",
            self.ufh_idx,
            min_raw,
            max_raw,
            self.mode_code,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert setpoint bounds payload to legacy dictionary layout.

        :returns: Decoded setpoint bounds dictionary.
        :rtype: dict[str, Any]
        """
        mode_str = {0: "off", 1: "heat", 2: "cool"}.get(
            self.mode_code, f"{self.mode_code:02X}"
        )
        return {
            "ufh_idx": f"{self.ufh_idx:02X}",
            "setpoint_bounds": (self.min_temp, self.max_temp),
            "mode": mode_str,
        }


# ----------------------------------------------------------------------


@register_payload("2249")
@dataclass(frozen=True, slots=True)
class NowNextSetpointPayload(PayloadBase):
    """Current and upcoming setpoint payload (Opcode 2249).

    7-byte Now/Next Setpoint binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Zone Index (uint8)           : 00
      +1       h      2B   Setpoint Now (int16*100)     : 08 34 (21.00°C)
      +3       h      2B   Setpoint Next (int16*100)    : 07 D0 (20.00°C)
      +5       H      2B   Minutes Remaining uint16     : 00 3C (60m)
      --------------------------------------------------------------
      Field-spaced hex : 00 0834 07D0 003C
      Payload hex      : 00083407D0003C

    :param zone_idx: Zone index byte.
    :type zone_idx: int
    :param setpoint_now: Current target setpoint temperature in °C.
    :type setpoint_now: float
    :param setpoint_next: Upcoming scheduled setpoint temperature in °C.
    :type setpoint_next: float
    :param minutes_remaining: Minutes remaining until next switchpoint.
    :type minutes_remaining: int
    """

    _STRUCT_FMT: ClassVar[str] = ">BhhH"

    zone_idx: int
    setpoint_now: float
    setpoint_next: float
    minutes_remaining: int

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack now/next setpoint binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked NowNextSetpointPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 7 bytes.
        """
        if len(raw_data) < 7:
            raise ValueError(f"Invalid payload length for 2249: {len(raw_data)}")
        # Unpack zone_idx, setpoint_now, setpoint_next, mins directly from offset 0
        idx, sp_now, sp_next, mins = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(
            zone_idx=idx,
            setpoint_now=sp_now / 100.0,
            setpoint_next=sp_next / 100.0,
            minutes_remaining=mins,
        )

    def to_bytes(self) -> bytes:
        """Pack now/next setpoint data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        now_raw = int(round(self.setpoint_now * 100.0))
        next_raw = int(round(self.setpoint_next * 100.0))
        return struct.pack(
            self._STRUCT_FMT,
            self.zone_idx,
            now_raw,
            next_raw,
            self.minutes_remaining,
        )


# ----------------------------------------------------------------------


@register_payload("22D0")
@dataclass(frozen=True, slots=True)
class UfhSystemModePayload(PayloadBase):
    """Underfloor heating system mode payload (Opcode 22D0).

    2-byte UFH System Mode binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   UFH Index (uint8)            : 00
      +1       B      1B   Mode Flags (uint8)           : 14
      --------------------------------------------------------------
      Field-spaced hex : 00 14
      Payload hex      : 0014

    :param idx: UFH index byte.
    :type idx: int
    :param flags: Raw mode flags byte.
    :type flags: int
    :param cool_mode: Cool mode enabled flag boolean.
    :type cool_mode: bool
    :param heat_mode: Heat mode enabled flag boolean.
    :type heat_mode: bool
    :param is_active: UFH active status flag boolean.
    :type is_active: bool
    """

    _STRUCT_FMT: ClassVar[str] = ">BB"

    idx: int
    flags: int
    cool_mode: bool
    heat_mode: bool
    is_active: bool

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack UFH system mode binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked UfhSystemModePayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 2 bytes.
        """
        if len(raw_data) < 2:
            raise ValueError(f"Invalid payload length for 22D0: {len(raw_data)}")
        ufh_idx, flg = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(
            idx=ufh_idx,
            flags=flg,
            cool_mode=bool(flg & 0x02),
            heat_mode=bool(flg & 0x04),
            is_active=bool(flg & 0x10),
        )

    def to_bytes(self) -> bytes:
        """Pack UFH system mode data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        return struct.pack(self._STRUCT_FMT, self.idx, self.flags)

    def to_dict(self, msg: Any = None) -> dict[str, Any]:
        """Convert UFH system mode payload to legacy dictionary format.

        :param msg: Optional message context object.
        :type msg: Any
        :returns: Decoded UFH mode dictionary.
        :rtype: dict[str, Any]
        """
        return {
            "idx": f"{self.idx:02X}",
            "cool_mode": self.cool_mode,
            "heat_mode": self.heat_mode,
            "is_active": self.is_active,
        }


# ----------------------------------------------------------------------


@register_payload("22D9")
@dataclass(frozen=True, slots=True)
class DesiredBoilerSetpointPayload(PayloadBase):
    """Target boiler setpoint temperature payload (Opcode 22D9).

    3-byte Desired Boiler Setpoint binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Domain / Zone Index (uint8)  : 00
      +1       h      2B   Target Temp (int16*100)      : 19 64 (65.00°C)
      --------------------------------------------------------------
      Field-spaced hex : 00 1964
      Payload hex      : 001964

    :param domain_or_zone_idx: Domain or zone index byte.
    :type domain_or_zone_idx: int
    :param target_temp: Target boiler temperature setpoint in °C.
    :type target_temp: float
    """

    _STRUCT_FMT: ClassVar[str] = ">Bh"

    domain_or_zone_idx: int
    target_temp: float | None

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack desired boiler setpoint binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked DesiredBoilerSetpointPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 3 bytes.
        """
        if len(raw_data) < 3:
            raise ValueError(f"Invalid payload length for 22D9: {len(raw_data)}")
        idx, t_raw = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        t_val = None if t_raw in (0x31FF, 0x7FFF) else t_raw / 100.0
        return cls(
            domain_or_zone_idx=idx,
            target_temp=t_val,
        )

    def to_bytes(self) -> bytes:
        """Pack desired boiler setpoint data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        t_raw = (
            0x7FFF if self.target_temp is None else int(round(self.target_temp * 100.0))
        )
        return struct.pack(self._STRUCT_FMT, self.domain_or_zone_idx, t_raw)

    def to_dict(self) -> dict[str, Any]:
        """Convert desired boiler setpoint payload to legacy dictionary layout.

        :returns: Decoded setpoint dictionary.
        :rtype: dict[str, Any]
        """
        return {"setpoint": self.target_temp}


# ----------------------------------------------------------------------


@register_payload("2D49")
@dataclass(frozen=True, slots=True)
class CoolingStatePayload(PayloadBase):
    """HCC100 cooling-demand payload (Opcode 2D49).

    3-byte HCC100 Cooling Demand binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Zone Index                    : 1E
      +1       B      1B   Cooling Demand (00/C8)        : C8
      +2       B      1B   Reserved                      : 00
      --------------------------------------------------------------
      Field-spaced hex : 1E C8 00
      Payload hex      : 1EC800

    The HCC100 encodes active cooling as ``C8``. Unknown demand values are
    conservatively treated as inactive and logged for diagnostics.
    """

    _STRUCT_FMT: ClassVar[str] = ">BBB"

    domain_or_zone_idx: int
    state: bool
    reserved: int

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack an exact three-byte HCC100 cooling-demand payload."""
        if len(raw_data) != 3:
            raise ValueError(f"Invalid payload length for 2D49: {len(raw_data)}")

        idx, demand_raw, reserved = struct.unpack(cls._STRUCT_FMT, raw_data)
        if demand_raw not in (0x00, 0xC8):
            _LOGGER.warning(
                "Unknown 2D49 cooling demand byte: %02X (payload=%s)",
                demand_raw,
                raw_data.hex().upper(),
            )
        return cls(
            domain_or_zone_idx=idx,
            state=demand_raw == 0xC8,
            reserved=reserved,
        )

    def to_bytes(self) -> bytes:
        """Pack the HCC100 cooling-demand payload."""
        return struct.pack(
            self._STRUCT_FMT,
            self.domain_or_zone_idx,
            0xC8 if self.state else 0x00,
            self.reserved,
        )

    def to_dict(self, msg: Any = None) -> dict[str, Any]:
        """Convert the HCC100 payload to the established packet dictionary."""
        return {
            "zone_idx": f"{self.domain_or_zone_idx:02X}",
            "cooling_demand": self.state,
        }


# ----------------------------------------------------------------------


@register_payload("313E")
@dataclass(frozen=True, slots=True)
class HvacTimeOffsetPayload(PayloadBase):
    """HVAC Zulu time offset payload (Opcode 313E).

    11-byte HVAC Zulu time offset binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Prefix constant byte (0x00)  : 00
      +1       I      4B   Minutes offset (uint32 BE)   : 00 00 3C A0
      +5       B      1B   Seconds offset (uint8)       : 00
      +6       5s     5B   Trailer constant bytes       : 00 3C 80 00 00
      --------------------------------------------------------------
      Field-spaced hex : 00 00003CA0 00 003C800000
      Payload hex      : 0000003CA000003C800000


    :param offset_mins: Time offset in minutes (uint32).
    :type offset_mins: int
    :param offset_secs: Time offset in seconds (uint8).
    :type offset_secs: int
    :param _raw_extra: Raw 5-byte trailer constant bytes.
    :type _raw_extra: bytes
    """

    _STRUCT_FMT: ClassVar[str] = ">BIB5s"

    offset_mins: int
    offset_secs: int
    _raw_extra: bytes = b"\x00\x3c\x80\x00\x00"

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack HVAC time offset binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked HvacTimeOffsetPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is not 11 bytes.
        """
        if len(raw_data) != 11:
            raise ValueError(f"Invalid payload length for 313E: {len(raw_data)}")
        prefix, mins, secs, suffix = struct.unpack(cls._STRUCT_FMT, raw_data)
        if prefix != 0 or suffix != b"\x00\x3c\x80\x00\x00":
            raise ValueError(f"Invalid constant bytes for 313E: {raw_data.hex()}")
        return cls(offset_mins=mins, offset_secs=secs, _raw_extra=suffix)

    def to_bytes(self) -> bytes:
        """Pack HVAC time offset data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        return struct.pack(
            self._STRUCT_FMT, 0, self.offset_mins, self.offset_secs, self._raw_extra
        )

    def to_dict(self, msg: Any | None = None) -> dict[str, Any]:
        """Convert payload to dictionary representation.

        :param msg: Optional message object containing packet timestamp context.
        :type msg: Any | None
        :returns: Decoded payload dictionary.
        :rtype: dict[str, Any]
        """
        val_02 = f"{self.offset_mins:08X}"
        val_10 = f"{self.offset_secs:02X}"
        val_12 = self._raw_extra.hex().upper()
        res: dict[str, Any] = {
            "value_02": val_02,
            "value_10": val_10,
            "value_12": val_12,
        }
        if msg is not None and getattr(msg, "dtm", None) is not None:
            zulu_dt = msg.dtm - td(minutes=self.offset_mins, seconds=self.offset_secs)
            res["zulu"] = zulu_dt.isoformat().split("+")[0]
        return res
