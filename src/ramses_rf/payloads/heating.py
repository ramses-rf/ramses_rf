"""RAMSES RF - Heating and Evohome payload dataclasses.

This module contains strongly-typed dataclass representations for CH / Evohome
packet payloads.
"""

import struct
from abc import ABC
from dataclasses import dataclass
from datetime import datetime as dt
from typing import Any, ClassVar, Self, cast

from ramses_rf.const import (
    DEV_ROLE_MAP,
    SZ_ACCEPT,
    SZ_BINDINGS,
    SZ_CONFIRM,
    SZ_OFFER,
    SZ_PHASE,
    ZON_MODE_MAP,
    ZON_ROLE_MAP,
)
from ramses_tx.address import ALL_DEV_ADDR, NON_DEV_ADDR, Address, hex_id_to_dev_id
from ramses_tx.helpers import hex_from_dtm, hex_to_dtm, hex_to_percent
from ramses_tx.typing import DeviceIdT

from .base import PayloadBase, parse_idx
from .dhw import DhwParamsBasePayload, DhwParamsPayload
from .registry import register_payload

# ----------------------------------------------------------------------


@register_payload("3150")
@dataclass(frozen=True, slots=True)
class HeatDemandPayload(PayloadBase):
    """Heat demand payload (Opcode 3150).

    2-byte Heat Demand binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Zone Index / Domain (uint8)  : 00
      +1       B      1B   Heat demand percentage (uint8) : C8 (200)
      --------------------------------------------------------------
      Field-spaced hex : 00 C8
      Payload hex      : 00C8

    :param domain_or_zone_idx: Domain or zone index byte.
    :type domain_or_zone_idx: int | None
    :param demand_percent: Heat demand value (0-200, where 200 = 100%).
    :type demand_percent: int
    :param raw_extra: Optional raw extra bytes.
    :type raw_extra: bytes | None

    Sample Packet Logs & Protocol Notes:
    # .I --- 04:136513 --:------ 01:158182 3150 002 01CA  # often seen CA, artefact?
    # .I --- 02:000921 --:------ 01:191718 3150 002 0360
    # TODO: all have a valid domain will UFC/CTL respond to an RQ,
    # 8 for UFC
    # .I --- 02:001107 --:------ 02:001107 22C9 024 00-0834-0A28-01-0108340A2801-0208340A2801-0308340A2801  # noqa: E501
    """

    _STRUCT_FMT_2B: ClassVar[str] = ">BB"

    domain_or_zone_idx: int | None
    demand_percent: int
    raw_extra: bytes | None = None
    _is_array_item: bool = False

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self | list[Self]:
        """Unpack a 1-byte, 2-byte, or multi-byte heat demand payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked HeatDemandPayload instance or list of instances.
        :rtype: Self | list[Self]
        :raises ValueError: If raw_data is empty.
        """
        if not raw_data:
            raise ValueError("Payload data cannot be empty")
        if len(raw_data) >= 4 and len(raw_data) % 2 == 0:
            return [
                cls(
                    domain_or_zone_idx=idx,
                    demand_percent=demand,
                    _is_array_item=True,
                )
                for idx, demand in (
                    struct.unpack_from(cls._STRUCT_FMT_2B, raw_data, i)
                    for i in range(0, len(raw_data), 2)
                )
            ]

        if len(raw_data) == 1:
            return cls(domain_or_zone_idx=None, demand_percent=raw_data[0])
        idx, demand = struct.unpack_from(cls._STRUCT_FMT_2B, raw_data, 0)
        extra = raw_data[2:] if len(raw_data) > 2 else None
        return cls(
            domain_or_zone_idx=idx,
            demand_percent=demand,
            raw_extra=extra,
        )

    def to_bytes(self) -> bytes:
        """Pack heat demand data into a binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        if self.domain_or_zone_idx is not None:
            buf = struct.pack(
                self._STRUCT_FMT_2B,
                self.domain_or_zone_idx,
                self.demand_percent & 0xFF,
            )
        else:
            buf = bytes([self.demand_percent & 0xFF])
        if self.raw_extra is not None:
            buf += self.raw_extra
        return buf

    def to_dict(self, msg: Any = None) -> dict[str, Any]:
        """Convert heat demand payload to legacy dictionary layout.

        :param msg: Optional message context object.
        :type msg: Any
        :returns: Decoded heat demand dictionary.
        :rtype: dict[str, Any]
        """
        if self.demand_percent == 0xF2:
            res: dict[str, Any] = {"heat_demand_fault": "unavailable"}
        else:
            val = self.demand_percent / 200.0
            if val == 1.01:
                val = 1.0
            res = {"heat_demand": val}
        if self.domain_or_zone_idx is not None:
            idx = self.domain_or_zone_idx
            if idx >= 0xF0:
                res["domain_id"] = "FC" if idx == 0xFC else f"{idx:02X}"
            else:
                is_ufc = False
                if msg is not None and getattr(msg, "src", None) is not None:
                    src_str = str(getattr(msg.src, "id", msg.src))
                    if src_str.startswith("02:") or getattr(msg.src, "type", "") in (
                        "02",
                        "UFC",
                    ):
                        is_ufc = True
                idx_name = "ufx_idx" if is_ufc else "zone_idx"
                res[idx_name] = f"{idx:02X}"
        return res


# ----------------------------------------------------------------------


@register_payload("30C9")
class TemperaturePayload(PayloadBase):
    """Master payload dispatcher for temperature (Opcode 30C9).

    Dispatches temperature binary payloads to 2-byte or 3-byte
    variant sub-dataclasses based on payload length.
    """

    VARIANTS: ClassVar[tuple[type[PayloadBase], ...]] = ()

    zone_idx: int | str | None
    temperature: float | bool | None

    def __new__(
        cls,
        zone_idx: int | str | None = None,
        temperature: float | bool | None = None,
    ) -> Any:
        """Construct Temperature payload variant dynamically from arguments."""
        if cls is not TemperaturePayload:
            return super().__new__(cls)
        if zone_idx is not None:
            return Temperature3BPayload(zone_idx=zone_idx, temperature=temperature)
        return Temperature2BPayload(temperature=temperature)

    @classmethod
    def _parse_temp_val(cls, temp_raw: int) -> float | bool | None:
        """Decode raw 16-bit signed integer to temperature value."""
        if temp_raw in (0x31FF, 0x7FFF):
            return None
        if temp_raw == 0x7EFF:
            return False
        return temp_raw / 100.0

    @classmethod
    def from_bytes(
        cls, raw_data: bytes
    ) -> "TemperaturePayload | list[TemperaturePayload]":
        """Unpack binary payload, dispatching by length."""
        if len(raw_data) > 3 and len(raw_data) % 3 == 0:
            return [
                Temperature3BPayload(
                    zone_idx=idx,
                    temperature=cls._parse_temp_val(temp_raw),
                )
                for idx, temp_raw in (
                    struct.unpack_from(">Bh", raw_data, i)
                    for i in range(0, len(raw_data), 3)
                )
            ]
        if len(raw_data) < 3:
            return Temperature2BPayload.from_bytes(raw_data)
        return Temperature3BPayload.from_bytes(raw_data)

    def to_bytes(self) -> bytes:
        """Pack payload base default method.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        :raises NotImplementedError: Master dispatcher must dispatch to
            variant sub-dataclass.
        """
        raise NotImplementedError("Use concrete variant sub-dataclass")


@dataclass(frozen=True, slots=True)
class Temperature2BPayload(TemperaturePayload):
    """2-byte temperature payload layout (Opcode 30C9).

    2-byte Temperature binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       h      2B   Temperature (int16, degC*100): 07 D0 (20.00°C)
      --------------------------------------------------------------
      Field-spaced hex : 07D0
      Payload hex      : 07D0

    :param temperature: Temperature in °C, False if disabled, or None if N/A.
    :type temperature: float | bool | None
    """

    _STRUCT_FMT: ClassVar[str] = ">h"

    zone_idx: None = None
    temperature: float | bool | None = None

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 2-byte temperature payload."""
        if len(raw_data) < 2:
            raise ValueError(
                f"Invalid payload length for Temperature2BPayload: {len(raw_data)}"
            )
        (temp_raw,) = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(temperature=cls._parse_temp_val(temp_raw))

    def to_bytes(self) -> bytes:
        """Pack 2-byte temperature payload."""
        if self.temperature is None:
            temp_raw = 0x7FFF
        elif self.temperature is False:
            temp_raw = 0x7EFF
        else:
            temp_raw = int(round(self.temperature * 100.0))
        return struct.pack(self._STRUCT_FMT, temp_raw)

    def to_dict(self) -> dict[str, Any]:
        """Convert 2-byte temperature payload to legacy dictionary layout."""
        return {"temperature": self.temperature}


@dataclass(frozen=True, slots=True)
class Temperature3BPayload(TemperaturePayload):
    """3-byte temperature payload layout (Opcode 30C9).

    3-byte Temperature binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Zone Index (uint8)           : 01
      +1       h      2B   Temperature (int16, degC*100): 07 D0 (20.00°C)
      --------------------------------------------------------------
      Field-spaced hex : 01 07D0
      Payload hex      : 0107D0

    :param zone_idx: Zone index byte.
    :type zone_idx: int | str
    :param temperature: Temperature in °C, False if disabled, or None if N/A.
    :type temperature: float | bool | None
    """

    _STRUCT_FMT: ClassVar[str] = ">Bh"

    zone_idx: int | str
    temperature: float | bool | None

    def __post_init__(self) -> None:
        """Normalise index arguments."""
        if isinstance(self.zone_idx, str):
            object.__setattr__(self, "zone_idx", parse_idx(self.zone_idx))

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 3-byte temperature payload."""
        if len(raw_data) < 3:
            raise ValueError(
                f"Invalid payload length for Temperature3BPayload: {len(raw_data)}"
            )
        idx, temp_raw = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(zone_idx=idx, temperature=cls._parse_temp_val(temp_raw))

    def to_bytes(self) -> bytes:
        """Pack 3-byte temperature payload."""
        if self.temperature is None:
            temp_raw = 0x7FFF
        elif self.temperature is False:
            temp_raw = 0x7EFF
        else:
            temp_raw = int(round(self.temperature * 100.0))
        idx = parse_idx(self.zone_idx)
        return struct.pack(self._STRUCT_FMT, idx, temp_raw)

    def to_dict(self) -> dict[str, Any]:
        """Convert 3-byte temperature payload to legacy dictionary layout."""
        idx_str = (
            f"{self.zone_idx:02X}"
            if isinstance(self.zone_idx, int)
            else str(self.zone_idx)
        )
        return {"zone_idx": idx_str, "temperature": self.temperature}


# Update VARIANTS property after variants are defined
TemperaturePayload.VARIANTS = (
    Temperature2BPayload,
    Temperature3BPayload,
)


@register_payload("1030")
@dataclass(frozen=True, slots=True)
class ScheduleFragmentPayload(PayloadBase):
    """Schedule fragment payload (Opcode 0404, 1030 fragment).

    Multi-byte schedule fragment binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Zone Index / Domain (uint8)  : 01
      +1       B      1B   Fragment Flags               : 20
      +2       H      2B   Padding                      : 00 00
      +4       B      1B   Fragment Length / Header     : 08
      +5       B      1B   Fragment Number (uint8)      : 01
      +6       B      1B   Total Fragments (uint8)      : 03
      +7       bytes  var  Fragment switchpoint bytes   : 68 81 ...
      --------------------------------------------------------------
      Field-spaced hex : 01 20 0000 08 01 03 6881
      Payload hex      : 012000000801036881

    :param zone_idx: Zone/domain index byte.
    :type zone_idx: int
    :param frag_number: Fragment index number (1-based).
    :type frag_number: int
    :param total_frags: Total fragment count for schedule transfer.
    :type total_frags: int
    :param fragment_bytes: Raw binary fragment data bytes.
    :type fragment_bytes: bytes

    Sample Packet Logs:
    # .I --- 01:145038 --:------ 01:145038 1030 016 0A-C80137-C9010F-CA0196-CB0100
    # .I --- --:------ --:------ 12:144017 1030 016 01-C80137-C9010F-CA0196-CB010F
    # RP --- 32:155617 18:005904 --:------ 1030 007 00-200100-21011F
    """

    _STRUCT_FMT_HEADER: ClassVar[str] = ">B3sBBB"

    zone_idx: int | str
    frag_number: int
    total_frags: int
    fragment_bytes: bytes
    _header_prefix: bytes | None = None

    def __post_init__(self) -> None:
        """Normalise index arguments."""
        if isinstance(self.zone_idx, str):
            object.__setattr__(self, "zone_idx", parse_idx(self.zone_idx))

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack schedule fragment binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked ScheduleFragmentPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 7 bytes.
        """
        if len(raw_data) < 7:
            raise ValueError(
                f"Invalid fragment payload length for 0404: {len(raw_data)}"
            )
        raw_idx, prefix, _frag_len, frag_num, total_frags = struct.unpack_from(
            cls._STRUCT_FMT_HEADER, raw_data, 0
        )
        zone_idx: int | str = (
            "HW" if raw_idx == 0 and prefix == b"\x23\x00\x08" else raw_idx
        )
        return cls(
            zone_idx=zone_idx,
            frag_number=frag_num,
            total_frags=total_frags,
            fragment_bytes=raw_data[7:],
            _header_prefix=prefix,
        )

    def to_bytes(self) -> bytes:
        """Pack schedule fragment data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        idx = parse_idx(self.zone_idx)
        prefix = (
            self._header_prefix
            if self._header_prefix is not None
            else (b"\x23\x00\x08" if idx == 0xFA else b"\x20\x00\x08")
        )
        byte_idx = 0x00 if prefix == b"\x23\x00\x08" and idx == 0xFA else idx
        hdr = struct.pack(
            self._STRUCT_FMT_HEADER,
            byte_idx,
            prefix,
            len(self.fragment_bytes),
            self.frag_number,
            self.total_frags,
        )
        return hdr + self.fragment_bytes

    def to_dict(self, msg: Any = None) -> dict[str, Any]:
        """Convert schedule fragment payload to legacy dictionary layout.

        :param msg: Optional message context object.
        :type msg: Any
        :returns: Decoded schedule fragment dictionary.
        :rtype: dict[str, Any]
        """
        if self.zone_idx in (0xFA, "HW"):
            zone_str = "HW"
        elif isinstance(self.zone_idx, int):
            zone_str = f"{self.zone_idx:02X}"
        else:
            zone_str = str(self.zone_idx)
        res: dict[str, Any] = {
            "zone_idx": zone_str,
            "frag_number": self.frag_number,
            "total_frags": self.total_frags if self.total_frags != 0 else None,
        }
        if self.fragment_bytes:
            res["fragment"] = self.fragment_bytes.hex().upper()
        return res


# ----------------------------------------------------------------------


@register_payload("0404")
@dataclass(frozen=True, slots=True)
class ScheduleSwitchpointPayload(PayloadBase):
    """Schedule switchpoint payload (Opcode 0404).

    20-byte Schedule Switchpoint binary layout (Little-Endian):
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       4x     4B   Padding / Header bytes       : 00 00 00 00
      +4       B      1B   Zone/Domain index (uint8)    : 01
      +5       3x     3B   Padding bytes                : 00 00 00
      +8       B      1B   Day of week (uint8, 1-7)     : 01
      +9       3x     3B   Padding bytes                : 00 00 00
      +12      H      2B   Time of day (uint16 mins)    : 68 01
      +14      2x     2B   Padding bytes                : 00 00
      +16      H      2B   Setpoint value / state (u16) : D0 07
      +18      H      2B   Reserved / Trailer bytes     : 00 00
      --------------------------------------------------------------
      Field-spaced hex : 00000000 01 000000 01 000000 6801 0000 D007 0000
      Payload hex      : 00000000010000000100000068010000D0070000

    :param zone_idx: Zone/domain index byte.
    :type zone_idx: int
    :param day_of_week: Day of week integer (1-7).
    :type day_of_week: int
    :param time_of_day_mins: Time of day in minutes.
    :type time_of_day_mins: int
    :param setpoint_value: Setpoint value or state raw uint16.
    :type setpoint_value: int
    """

    _STRUCT_FMT: ClassVar[str] = "<xxxxBxxxBxxxHxxHH"

    zone_idx: int
    day_of_week: int
    time_of_day_mins: int
    setpoint_value: int

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self | ScheduleFragmentPayload:
        """Unpack a compressed 20-byte schedule switchpoint or fragment.

        :param raw_data: Raw schedule binary block.
        :type raw_data: bytes
        :returns: ScheduleSwitchpointPayload or ScheduleFragmentPayload instance.
        :rtype: Self | ScheduleFragmentPayload
        :raises ValueError: If raw_data length is invalid.
        """
        if len(raw_data) != 20:
            if len(raw_data) >= 7:
                return ScheduleFragmentPayload.from_bytes(raw_data)
            raise ValueError(f"Invalid payload length for 0404: {len(raw_data)}")

        idx, dow, tod, val, _ = struct.unpack(cls._STRUCT_FMT, raw_data)
        return cls(
            zone_idx=idx,
            day_of_week=dow,
            time_of_day_mins=tod,
            setpoint_value=val,
        )

    @classmethod
    def from_switchpoint(
        cls,
        zone_idx: int | str,
        day_of_week: int,
        time_of_day_mins: int,
        setpoint: float | bool | None,
    ) -> Self:
        """Create a ScheduleSwitchpointPayload from switchpoint domain values.

        :param zone_idx: Zone or domain index byte or string.
        :type zone_idx: int | str
        :param day_of_week: Day of week integer (0-6).
        :type day_of_week: int
        :param time_of_day_mins: Time of day in minutes.
        :type time_of_day_mins: int
        :param setpoint: Temperature setpoint float, boolean state, or None.
        :type setpoint: float | bool | None
        :returns: A populated ScheduleSwitchpointPayload instance.
        :rtype: Self
        """
        idx = parse_idx(zone_idx)
        if isinstance(setpoint, bool):
            value = int(setpoint)
        elif isinstance(setpoint, (int, float)):
            value = int(setpoint * 100)
        else:
            value = 0
        return cls(
            zone_idx=idx,
            day_of_week=day_of_week,
            time_of_day_mins=time_of_day_mins,
            setpoint_value=value,
        )

    def to_bytes(self) -> bytes:
        """Pack schedule switchpoint information into bytes.

        :returns: Packed 20-byte binary payload bytes.
        :rtype: bytes
        """
        return struct.pack(
            self._STRUCT_FMT,
            self.zone_idx,
            self.day_of_week,
            self.time_of_day_mins,
            self.setpoint_value,
            0,  # Reserved / trailer 2-byte field (0x0000)
        )


# ----------------------------------------------------------------------


@register_payload("10A0")
class DhwTemperaturePayload(PayloadBase):
    """Master payload dispatcher and base class for Opcode 10A0."""

    VARIANTS: ClassVar[tuple[type[PayloadBase], ...]] = ()

    dhw_idx: int | str | None
    temperature: float | bool | None

    def __new__(
        cls,
        dhw_idx: int | str | None = None,
        temperature: float | bool | None = None,
    ) -> Any:
        """Construct DhwTemperature payload variant dynamically from arguments."""
        if cls is not DhwTemperaturePayload:
            return super().__new__(cls)
        if dhw_idx is not None:
            return DhwTemperature3BPayload(dhw_idx=dhw_idx, temperature=temperature)
        return DhwTemperature2BPayload(temperature=temperature)

    @classmethod
    def _parse_temp_val(cls, temp_raw: int) -> float | bool | None:
        """Decode raw 16-bit signed integer to DHW temperature value."""
        if temp_raw in (0x31FF, 0x7FFF, 0x639C):
            return None
        if temp_raw == 0x7EFF:
            return False
        return temp_raw / 100.0

    @classmethod
    def from_bytes(
        cls, raw_data: bytes
    ) -> "DhwTemperaturePayload | DhwParamsBasePayload":
        """Unpack DHW temperature or parameters binary payload."""
        if len(raw_data) >= 6:
            return DhwParamsPayload.from_bytes(raw_data)
        if len(raw_data) == 2:
            return DhwTemperature2BPayload.from_bytes(raw_data)
        if len(raw_data) >= 3:
            return DhwTemperature3BPayload.from_bytes(raw_data)
        raise ValueError(f"Invalid payload length for 10A0: {len(raw_data)}")

    def to_bytes(self) -> bytes:
        """Pack payload base default method.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        :raises NotImplementedError: Master dispatcher must dispatch to
            variant sub-dataclass.
        """
        raise NotImplementedError("Use concrete variant sub-dataclass")


@dataclass(frozen=True, slots=True)
class DhwTemperature2BPayload(DhwTemperaturePayload):
    """2-byte DHW temperature payload (Opcode 10A0).

    2-byte DHW Temp binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       h      2B   Temperature (int16, degC*100): 0E D8 (38.00°C)
      --------------------------------------------------------------
      Field-spaced hex : 0ED8
      Payload hex      : 0ED8

    :param temperature: DHW temperature in °C, False if disabled, or None.
    :type temperature: float | bool | None
    """

    _STRUCT_FMT: ClassVar[str] = ">h"

    temperature: float | bool | None

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 2-byte DHW temperature binary payload."""
        if len(raw_data) < 2:
            raise ValueError(
                f"Invalid payload length for DhwTemperature2BPayload: {len(raw_data)}"
            )
        (temp_raw,) = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(temperature=cls._parse_temp_val(temp_raw))

    def to_bytes(self) -> bytes:
        """Pack 2-byte DHW temperature binary payload."""
        if self.temperature is None:
            temp_raw = 0x7FFF
        elif self.temperature is False:
            temp_raw = 0x7EFF
        else:
            temp_raw = int(round(self.temperature * 100.0))
        return struct.pack(self._STRUCT_FMT, temp_raw)

    def to_dict(self) -> dict[str, Any]:
        """Convert 2-byte DHW temperature payload to legacy dictionary layout."""
        return {"setpoint": self.temperature}


@dataclass(frozen=True, slots=True)
class DhwTemperature3BPayload(DhwTemperaturePayload):
    """3-byte DHW temperature payload (Opcode 10A0).

    3-byte DHW Temp binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   DHW Index (uint8)            : 00
      +1       h      2B   Temperature (int16, degC*100): 0E D8 (38.00°C)
      --------------------------------------------------------------
      Field-spaced hex : 00 0ED8
      Payload hex      : 000ED8

    :param dhw_idx: DHW index byte.
    :type dhw_idx: int | str
    :param temperature: DHW temperature in °C, False if disabled, or None.
    :type temperature: float | bool | None
    """

    _STRUCT_FMT: ClassVar[str] = ">Bh"

    dhw_idx: int | str
    temperature: float | bool | None

    def __post_init__(self) -> None:
        """Normalise index arguments."""
        if isinstance(self.dhw_idx, str):
            object.__setattr__(self, "dhw_idx", parse_idx(self.dhw_idx))

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 3-byte DHW temperature binary payload."""
        if len(raw_data) < 3:
            raise ValueError(
                f"Invalid payload length for DhwTemperature3BPayload: {len(raw_data)}"
            )
        idx, temp_raw = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(dhw_idx=idx, temperature=cls._parse_temp_val(temp_raw))

    def to_bytes(self) -> bytes:
        """Pack 3-byte DHW temperature binary payload."""
        if self.temperature is None:
            temp_raw = 0x7FFF
        elif self.temperature is False:
            temp_raw = 0x7EFF
        else:
            temp_raw = int(round(self.temperature * 100.0))
        idx = parse_idx(self.dhw_idx)
        return struct.pack(self._STRUCT_FMT, idx, temp_raw)

    def to_dict(self) -> dict[str, Any]:
        """Convert 3-byte DHW temperature payload to legacy dictionary layout."""
        return {"setpoint": self.temperature}


# Update VARIANTS property after variants are defined
DhwTemperaturePayload.VARIANTS = (
    DhwTemperature2BPayload,
    DhwTemperature3BPayload,
)


# ----------------------------------------------------------------------


@register_payload("1030")
class SystemSyncPayload(PayloadBase):
    """Master payload dispatcher and base class for Opcode 1030."""

    VARIANTS: ClassVar[tuple[type[PayloadBase], ...]] = ()

    sync_flag: int
    max_flow_setpoint: int | None
    min_flow_setpoint: int | None
    valve_run_time: int | None
    pump_run_time: int | None

    def __new__(
        cls,
        sync_flag: int = 0,
        max_flow_setpoint: int | None = None,
        min_flow_setpoint: int | None = None,
        valve_run_time: int | None = None,
        pump_run_time: int | None = None,
        _boolean_cc: int | None = None,
        _unknown_20: int | None = None,
        _unknown_21: int | None = None,
        _raw_extra: bytes | None = None,
    ) -> Any:
        """Construct SystemSync payload variant dynamically from arguments."""
        if cls is not SystemSyncPayload:
            return super().__new__(cls)
        if any(
            x is not None
            for x in (
                max_flow_setpoint,
                min_flow_setpoint,
                valve_run_time,
                pump_run_time,
                _boolean_cc,
                _unknown_20,
                _unknown_21,
                _raw_extra,
            )
        ):
            return SystemSyncVarPayload(
                sync_flag=sync_flag,
                max_flow_setpoint=max_flow_setpoint,
                min_flow_setpoint=min_flow_setpoint,
                valve_run_time=valve_run_time,
                pump_run_time=pump_run_time,
                _boolean_cc=_boolean_cc,
                _unknown_20=_unknown_20,
                _unknown_21=_unknown_21,
                _raw_extra=_raw_extra,
            )
        return SystemSync1BPayload(sync_flag=sync_flag)

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> "SystemSyncPayload":
        """Unpack system sync binary payload, dispatching by length."""
        if len(raw_data) <= 1:
            return SystemSync1BPayload.from_bytes(raw_data)
        return SystemSyncVarPayload.from_bytes(raw_data)

    def to_bytes(self) -> bytes:
        """Pack payload base default method.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        :raises NotImplementedError: Master dispatcher must dispatch to
            variant sub-dataclass.
        """
        raise NotImplementedError("Use concrete variant sub-dataclass")


@dataclass(frozen=True, slots=True)
class SystemSync1BPayload(SystemSyncPayload):
    """1-byte system sync status payload (Opcode 1030).

    1-byte System Sync binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Sync Flag / Counter (uint8)  : 00
      --------------------------------------------------------------
      Field-spaced hex : 00
      Payload hex      : 00

    :param sync_flag: System synchronization counter or status byte.
    :type sync_flag: int
    """

    _STRUCT_FMT: ClassVar[str] = ">B"

    sync_flag: int
    max_flow_setpoint: int | None = None
    min_flow_setpoint: int | None = None
    valve_run_time: int | None = None
    pump_run_time: int | None = None

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 1-byte system sync binary payload."""
        if not raw_data:
            raise ValueError("Payload data cannot be empty")
        (sync_flag,) = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(sync_flag=sync_flag)

    def to_bytes(self) -> bytes:
        """Pack 1-byte system sync binary payload."""
        return struct.pack(self._STRUCT_FMT, self.sync_flag)

    def to_dict(self) -> dict[str, Any]:
        """Convert 1-byte system sync payload to legacy dictionary layout."""
        return {
            "sync_flag": self.sync_flag,
            "max_flow_setpoint": None,
            "min_flow_setpoint": None,
            "valve_run_time": None,
            "pump_run_time": None,
        }


@dataclass(frozen=True, slots=True)
class SystemSyncVarPayload(SystemSyncPayload):
    """Multi-parameter system sync / mixing valve payload (Opcode 1030).

    Multi-parameter binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Sync Flag / Counter (uint8)  : 00
      +1      >BBB    3B*  Parameter Tuples (id,sub,val): C8 01 37
      --------------------------------------------------------------
      Field-spaced hex : 00 C8 01 37
      Payload hex      : 00C80137

    :param sync_flag: System synchronization counter or status byte.
    :type sync_flag: int
    :param max_flow_setpoint: Maximum flow setpoint temperature in °C.
    :type max_flow_setpoint: int | None
    :param min_flow_setpoint: Minimum flow setpoint temperature in °C.
    :type min_flow_setpoint: int | None
    :param valve_run_time: Valve run time in seconds.
    :type valve_run_time: int | None
    :param pump_run_time: Pump run time in seconds.
    :type pump_run_time: int | None
    :param _boolean_cc: Internal Boolean CC parameter.
    :type _boolean_cc: int | None
    :param _unknown_20: Internal unknown mixing parameter 0x20.
    :type _unknown_20: int | None
    :param _unknown_21: Internal unknown mixing parameter 0x21.
    :type _unknown_21: int | None
    :param _raw_extra: Optional raw payload bytes beyond sync_flag.
    :type _raw_extra: bytes | None
    """

    _STRUCT_FMT_BYTE: ClassVar[str] = ">B"
    _STRUCT_FMT_PARAM: ClassVar[str] = ">BBB"

    sync_flag: int
    max_flow_setpoint: int | None = None
    min_flow_setpoint: int | None = None
    valve_run_time: int | None = None
    pump_run_time: int | None = None
    _boolean_cc: int | None = None
    _unknown_20: int | None = None
    _unknown_21: int | None = None
    _raw_extra: bytes | None = None

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack multi-parameter system sync binary payload."""
        if not raw_data:
            raise ValueError("Payload data cannot be empty")

        (sync_flag,) = struct.unpack_from(cls._STRUCT_FMT_BYTE, raw_data, 0)
        max_flow = None
        min_flow = None
        v_time = None
        p_time = None
        b_cc = None
        u20 = None
        u21 = None
        extra = raw_data[1:] if len(raw_data) > 1 else None

        if len(raw_data) >= 4:
            i = 1
            while i + 2 < len(raw_data):
                param_id, _sub, val = struct.unpack_from(
                    cls._STRUCT_FMT_PARAM, raw_data, i
                )
                if param_id == 0xC8:
                    max_flow = val
                elif param_id == 0xC9:
                    min_flow = val
                elif param_id == 0xCA:
                    v_time = val
                elif param_id == 0xCB:
                    p_time = val
                elif param_id == 0xCC:
                    b_cc = val
                elif param_id == 0x20:
                    u20 = val
                elif param_id == 0x21:
                    u21 = val
                i += 3

        return cls(
            sync_flag=sync_flag,
            max_flow_setpoint=max_flow,
            min_flow_setpoint=min_flow,
            valve_run_time=v_time,
            pump_run_time=p_time,
            _boolean_cc=b_cc,
            _unknown_20=u20,
            _unknown_21=u21,
            _raw_extra=extra,
        )

    def to_bytes(self) -> bytes:
        """Pack multi-parameter system sync binary payload."""
        res = struct.pack(self._STRUCT_FMT_BYTE, self.sync_flag)
        if self.max_flow_setpoint is not None:
            res += struct.pack(self._STRUCT_FMT_PARAM, 0xC8, 1, self.max_flow_setpoint)
        if self.min_flow_setpoint is not None:
            res += struct.pack(self._STRUCT_FMT_PARAM, 0xC9, 1, self.min_flow_setpoint)
        if self.valve_run_time is not None:
            res += struct.pack(self._STRUCT_FMT_PARAM, 0xCA, 1, self.valve_run_time)
        if self.pump_run_time is not None:
            res += struct.pack(self._STRUCT_FMT_PARAM, 0xCB, 1, self.pump_run_time)
        if self._boolean_cc is not None:
            res += struct.pack(self._STRUCT_FMT_PARAM, 0xCC, 1, self._boolean_cc)
        if self._unknown_20 is not None:
            res += struct.pack(self._STRUCT_FMT_PARAM, 0x20, 1, self._unknown_20)
        if self._unknown_21 is not None:
            res += struct.pack(self._STRUCT_FMT_PARAM, 0x21, 1, self._unknown_21)
        if self._raw_extra and len(res) == 1:
            res += self._raw_extra
        return res

    def to_dict(self) -> dict[str, Any]:
        """Convert multi-parameter system sync payload to dictionary."""
        res: dict[str, Any] = {}
        if self._unknown_20 is not None or self._unknown_21 is not None:
            if self._unknown_20 is not None:
                res["unknown_20"] = self._unknown_20
            if self._unknown_21 is not None:
                res["unknown_21"] = self._unknown_21
            return res

        res["zone_idx"] = f"{self.sync_flag:02X}"
        if self.max_flow_setpoint is not None:
            res["max_flow_setpoint"] = self.max_flow_setpoint
        if self.min_flow_setpoint is not None:
            res["min_flow_setpoint"] = self.min_flow_setpoint
        if self.valve_run_time is not None:
            res["valve_run_time"] = self.valve_run_time
        if self.pump_run_time is not None:
            res["pump_run_time"] = self.pump_run_time
        if self._boolean_cc is not None:
            res["boolean_cc"] = self._boolean_cc
        return res


# Update VARIANTS property after variants are defined
SystemSyncPayload.VARIANTS = (
    SystemSync1BPayload,
    SystemSyncVarPayload,
)


# ----------------------------------------------------------------------


@register_payload("1FC9")
@dataclass(frozen=True, slots=True)
class BindingPayload(PayloadBase):
    """Binding payload (Opcode 1FC9).

    Variable-length Binding binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Binding Type / Command Code  : 00
      +1       xB     vB   Binding Data Byte Sequence   : 10 E0 00 00 00
      --------------------------------------------------------------
      Field-spaced hex : 00 10E0000000
      Payload hex      : 0010E0000000

    Discovery & Protocol Notes:
      # 1FC9 (Binding) is used to pair devices to zones or controllers.
      # Sample Packet Logs:
      # .I --- 34:145039 --:------ 34:145039 1FC9 012 00-30C9-8A368F 00-1FC9-8A368F
      # .W --- 01:054173 34:145039 --:------ 1FC9 006 03-2309-04D39D

    :param binding_type: Binding type or domain byte.
    :type binding_type: int
    :param binding_data: Binding payload raw byte data.
    :type binding_data: bytes
    """

    _STRUCT_FMT_HDR: ClassVar[str] = ">B"

    binding_type: int
    binding_data: bytes

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack binding binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked BindingPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data is empty.
        """
        if not raw_data:
            raise ValueError("Payload data cannot be empty")
        (b_type,) = struct.unpack_from(cls._STRUCT_FMT_HDR, raw_data, 0)
        b_data = raw_data[1:]
        return cls(binding_type=b_type, binding_data=b_data)

    def to_bytes(self) -> bytes:
        """Pack binding data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        return struct.pack(self._STRUCT_FMT_HDR, self.binding_type) + self.binding_data

    def to_dict(self, msg: Any = None) -> dict[str, Any]:
        """Convert 1FC9 binding payload to legacy dictionary representation.

        :param msg: Optional Message context for legacy compatibility.
        :type msg: Any
        :returns: Dictionary representation of binding payload.
        :rtype: dict[str, Any]
        """
        payload_hex = (bytes([self.binding_type]) + self.binding_data).hex().upper()
        res: dict[str, Any] = {}

        if msg is not None:
            verb = getattr(msg, "verb", " I")
            src_id = str(
                getattr(getattr(msg, "src", ""), "id", getattr(msg, "src", ""))
            )
            dst_id = str(
                getattr(getattr(msg, "dst", ""), "id", getattr(msg, "dst", ""))
            )
            v_str = str(getattr(verb, "value", str(verb))).split(".")[-1].strip()

            if v_str in ("I", "I_") and (
                dst_id
                in (src_id, "63:262142", ALL_DEV_ADDR.id, NON_DEV_ADDR.id, "--:------")
            ):
                bind_phase = SZ_OFFER
            elif v_str in ("W", "W_") and src_id != dst_id:
                bind_phase = SZ_ACCEPT
            elif v_str in ("I", "I_"):
                bind_phase = SZ_CONFIRM
            else:
                bind_phase = None

            if bind_phase is not None:
                res[SZ_PHASE] = bind_phase

        bindings: list[list[str]] = []
        if len(payload_hex) >= 12:
            for i in range(0, len(payload_hex) - 11, 12):
                chunk = payload_hex[i : i + 12]
                domain_id_hex = chunk[:2]
                opcode_hex = chunk[2:6]
                dev_hex = chunk[6:12]
                try:
                    bound_dev_id = hex_id_to_dev_id(dev_hex)
                except ValueError:
                    bound_dev_id = cast(DeviceIdT, dev_hex)
                bindings.append([domain_id_hex, opcode_hex, bound_dev_id])
        elif len(payload_hex) == 2:
            bindings.append([payload_hex])
        res[SZ_BINDINGS] = bindings

        return res

    @property
    def idx(self) -> str:
        """Return two-character hex index string for binding type.

        :returns: Two-character uppercase hex index string.
        :rtype: str
        """
        return f"{self.binding_type:02X}"

    @property
    def vendor_code(self) -> str | None:
        """Extract vendor code from ratification addenda data if present.

        :returns: Two-character uppercase hex vendor code string or None.
        :rtype: str | None
        """
        if len(self.binding_data) >= 7:
            return f"{self.binding_data[6]:02X}"
        return None


# ----------------------------------------------------------------------


@register_payload("000A")
@dataclass(frozen=True, slots=True)
class ZoneConfigPayload(PayloadBase):
    """Zone configuration payload (Opcode 000A).

    6-byte Zone Config binary layout (Big-Endian):
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Zone Index (uint8)           : 00
      +1       B      1B   Zone Type / Flags            : 00
      +2       h      2B   Min Temp (int16, degC*100)   : 01 F4 (5.00°C)
      +4       h      2B   Max Temp (int16, degC*100)   : 0D B8 (35.00°C)
      --------------------------------------------------------------
      Field-spaced hex : 00 00 01F4 0DB8
      Payload hex      : 000001F40DB8

    Discovery & Protocol Notes:
      # 000A (Zone Info) is sent by THMs (22:) with their zone_idx as payload (e.g. RQ 000A 001 01).
      # CTL (01:) sends 000A with full zone configuration arrays.
      # Sample Packet Logs:
      # .I --- 01:158182 --:------ 01:158182 000A 048 001201F409C4011101F409C40...
      # .I --- 01:158182 --:------ 01:158182 000A 006 081001F409C4

    :param zone_idx: Zone index integer.
    :type zone_idx: int
    :param zone_flags: Zone flags byte.
    :type zone_flags: int
    :param min_temp: Minimum zone temperature setting in °C.
    :type min_temp: float
    :param max_temp: Maximum zone temperature setting in °C.
    :type max_temp: float
    """

    _STRUCT_FMT: ClassVar[str] = ">BBhh"

    zone_idx: int | str
    zone_flags: int
    min_temp: float | None
    max_temp: float | None

    def __post_init__(self) -> None:
        """Normalise index arguments."""
        if isinstance(self.zone_idx, str):
            object.__setattr__(self, "zone_idx", parse_idx(self.zone_idx))

    @classmethod
    def _from_bytes_single(cls, raw_data: bytes, offset: int = 0) -> Self:
        """Unpack a single 6-byte zone config binary payload from offset.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :param offset: Byte offset within raw_data to unpack from.
        :type offset: int
        :returns: Unpacked ZoneConfigPayload instance.
        :rtype: Self
        """
        # Unpack idx, flags, min_temp, max_temp directly from offset
        idx, flags, min_raw, max_raw = struct.unpack_from(
            cls._STRUCT_FMT, raw_data, offset
        )
        return cls(
            zone_idx=idx,
            zone_flags=flags,
            min_temp=None if min_raw in (0x7FFF, 0x31FF) else min_raw / 100.0,
            max_temp=None if max_raw in (0x7FFF, 0x31FF) else max_raw / 100.0,
        )

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self | list[Self]:
        """Unpack zone config binary payload (single or multi-zone array).

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Single ZoneConfigPayload instance or list of instances.
        :rtype: Self | list[Self]
        :raises ValueError: If raw_data length is invalid.
        """
        if len(raw_data) == 1:
            return cls(zone_idx=raw_data[0], zone_flags=0, min_temp=None, max_temp=None)
        if len(raw_data) == 2:
            return cls(
                zone_idx=raw_data[0],
                zone_flags=raw_data[1],
                min_temp=None,
                max_temp=None,
            )
        if len(raw_data) < 6 or len(raw_data) % 6 != 0:
            raise ValueError(f"Invalid payload length for 000A: {len(raw_data)}")
        if len(raw_data) > 6:
            return [
                cls._from_bytes_single(raw_data, i) for i in range(0, len(raw_data), 6)
            ]

        return cls._from_bytes_single(raw_data, 0)

    def to_bytes(self) -> bytes:
        """Pack zone config data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        idx = parse_idx(self.zone_idx)
        min_raw = 0x7FFF if self.min_temp is None else int(round(self.min_temp * 100.0))
        max_raw = 0x7FFF if self.max_temp is None else int(round(self.max_temp * 100.0))
        return struct.pack(
            self._STRUCT_FMT,
            idx,
            self.zone_flags,
            min_raw,
            max_raw,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert zone config payload to legacy dictionary layout.

        :returns: Decoded zone config dictionary.
        :rtype: dict[str, Any]
        """
        bitmap = self.zone_flags
        return {
            "min_temp": self.min_temp,
            "max_temp": self.max_temp,
            "local_override": not bool(bitmap & 1),
            "openwindow_function": not bool(bitmap & 2),
            "multiroom_mode": not bool(bitmap & 16),
        }


@dataclass(frozen=True, slots=True)
class ZoneNamePayload(PayloadBase):
    """Zone name payload (Opcode 0004 W / 0004 RP name variant).

    22-byte Zone Name binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Zone Index (uint8)           : 00
      +1       B      1B   Flag Byte (uint8)            : 00
      +2       20s    20B  ASCII Zone Name (20B null-pad): 4C 6F 75 6E 67 65 00... ("Lounge")
      --------------------------------------------------------------
      Field-spaced hex : 00 00 4C6F756E67650000000000000000000000000000
      Payload hex      : 00004C6F756E67650000000000000000000000000000

    :param zone_idx: Zone index byte.
    :type zone_idx: int | str
    :param name: ASCII Zone Name string (max 20 chars).
    :type name: str

    Domain Notes:
    # RQ payload is zz00; limited to 12 chars in evohome UI? if "7F"*20: not a zone
    """

    _STRUCT_FMT: ClassVar[str] = ">Bx20s"

    zone_idx: int | str
    name: str | None = None

    def __post_init__(self) -> None:
        """Normalise index arguments."""
        if isinstance(self.zone_idx, str):
            object.__setattr__(self, "zone_idx", parse_idx(self.zone_idx))

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack zone name binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked ZoneNamePayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 22 bytes.
        """
        if len(raw_data) < 22:
            raise ValueError(
                f"Invalid payload length for ZoneNamePayload: {len(raw_data)}"
            )
        # Unpack zone_idx (uint8), skip 1 pad byte, and extract 20-byte string
        idx, name_raw = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        if name_raw == b"\x7f" * 20:
            name = None
        else:
            name = name_raw.rstrip(b"\x00").decode("ascii", errors="replace")
        return cls(zone_idx=idx, name=name)

    def to_bytes(self) -> bytes:
        """Pack zone name data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        idx = parse_idx(self.zone_idx)
        name_bytes = (
            b"\x7f" * 20
            if self.name is None
            else self.name.encode("ascii", errors="replace")[:20].ljust(20, b"\x00")
        )
        return struct.pack(self._STRUCT_FMT, idx, name_bytes)

    def to_dict(self) -> dict[str, Any]:
        """Convert zone name payload to legacy dictionary layout.

        :returns: Decoded zone name dictionary.
        :rtype: dict[str, Any]
        """
        if self.name is None:
            return {}
        idx_str = (
            f"{self.zone_idx:02X}" if isinstance(self.zone_idx, int) else self.zone_idx
        )
        return {"zone_idx": idx_str, "name": self.name}


# ----------------------------------------------------------------------


@register_payload("0004")
@dataclass(frozen=True, slots=True)
class ZoneSetpointPayload(PayloadBase):
    """Zone target setpoint payload (Opcode 0004).

    3-byte Zone Setpoint binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Zone Index (uint8)           : 01
      +1       h      2B   Target Setpoint (int16*100)  : 07 D0 (20.00°C)
      --------------------------------------------------------------
      Field-spaced hex : 01 07D0
      Payload hex      : 0107D0

    :param zone_idx: Zone index byte.
    :type zone_idx: int | str
    :param setpoint_temp: Target temperature in °C.
    :type setpoint_temp: float
    """

    zone_idx: int | str
    setpoint_temp: float

    def __post_init__(self) -> None:
        """Normalise index arguments."""
        if isinstance(self.zone_idx, str):
            object.__setattr__(self, "zone_idx", parse_idx(self.zone_idx))

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self | ZoneNamePayload:
        """Unpack zone setpoint binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked ZoneSetpointPayload or ZoneNamePayload instance.
        :rtype: Self | ZoneNamePayload
        :raises ValueError: If raw_data length is less than 3 bytes.
        """
        if len(raw_data) >= 22:
            return ZoneNamePayload.from_bytes(raw_data)
        if len(raw_data) < 3:
            raise ValueError(f"Invalid payload length for 0004: {len(raw_data)}")
        # Unpack zone_idx (uint8) and setpoint_raw (int16) directly from offset 0
        idx, sp_raw = struct.unpack_from(">Bh", raw_data, 0)
        return cls(zone_idx=idx, setpoint_temp=sp_raw / 100.0)

    def to_bytes(self) -> bytes:
        """Pack zone setpoint data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        sp_raw = int(round(self.setpoint_temp * 100.0))
        idx = parse_idx(self.zone_idx)
        return bytes([idx]) + sp_raw.to_bytes(2, byteorder="big", signed=True)


# ----------------------------------------------------------------------


@register_payload("2249")
@register_payload("12C0")
@dataclass(frozen=True, slots=True)
class OutdoorTempPayload(PayloadBase):
    """Outdoor temperature reading payload (Opcode 12C0, 2249).

    Protocol Notes & Sample Packet Logs:
    # see: https://github.com/jrosser/honeymon/blob/master/decoder.cpp#L357-L370
    # .I --- 23:100224 --:------ 23:100224 2249 007 00-7EFF-7EFF-FFFF

    Standard 2-byte Outdoor Temp binary layout (Big-Endian int16*100):
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       h      2B   Outdoor Temperature (int16*100): 05 DC (15.00°C)
      --------------------------------------------------------------
      Field-spaced hex : 05DC
      Payload hex      : 05DC

    Legacy 3-byte weather payloads use layout '00 <val> <unit_byte>' where
    offset 0 is header 00, offset 1 is uint8 raw value (80 = invalid), and
    offset 2 is unit byte (01 = Celsius half-degrees, 02 = Fahrenheit).

    :param temperature: Outdoor temperature reading in °C, or None if
        invalid.
    :type temperature: float | None
    :param _units: Raw unit byte hex string (e.g. '01' for Celsius
        half-degrees, '02' for Fahrenheit) present only on legacy
        3-byte 12C0 weather payloads (00 <val> <unit_byte>). None for
        standard 2-byte RAMSES payloads.
    :type _units: str | None

    Sample Packet Logs:
    # displayed temperature (on a TR87RF bound to a RFG100)
    """

    _STRUCT_FMT: ClassVar[str] = ">h"
    _STRUCT_FMT_LEGACY: ClassVar[str] = ">BBB"

    temperature: float | None
    _units: str | None = None

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack outdoor temperature binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked OutdoorTempPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 2 bytes.
        """
        if len(raw_data) < 2:
            raise ValueError(f"Invalid payload length for 12C0: {len(raw_data)}")
        if len(raw_data) >= 3 and raw_data[0] == 0:
            _hdr, val, unit_byte = struct.unpack_from(
                cls._STRUCT_FMT_LEGACY, raw_data, 0
            )
            if val == 0x80:
                temp = None
            elif unit_byte == 1:
                temp = val / 2.0
            else:
                temp = round((val - 32) * 5.0 / 9.0, 2)
            u_str = f"{unit_byte:02X}"
            return cls(temperature=temp, _units=u_str)

        (temp_raw,) = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        temp = None if temp_raw in (0x7FFF, 0x31FF) else temp_raw / 100.0
        return cls(temperature=temp, _units=None)

    def to_bytes(self) -> bytes:
        """Pack outdoor temperature data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        if self.temperature is None:
            return struct.pack(self._STRUCT_FMT, 0x7FFF)
        temp_raw = int(round(self.temperature * 100.0))
        return struct.pack(self._STRUCT_FMT, temp_raw)

    def to_dict(self, msg: Any = None) -> dict[str, Any]:
        """Convert 12C0 payload to legacy dictionary format.

        :param msg: Optional message context object.
        :type msg: Any
        :returns: Decoded temperature dictionary.
        :rtype: dict[str, Any]
        """
        res: dict[str, Any] = {"temperature": self.temperature}
        if self._units is not None:
            res["units"] = {
                "00": "Celsius",
                "01": "Celsius",
                "02": "Fahrenheit",
            }.get(self._units, self._units)
        return res


# ----------------------------------------------------------------------


@register_payload("2309")
@dataclass(frozen=True, slots=True)
class SetPointInfoPayload(PayloadBase):
    """Set-point info payload (Opcode 2309).

    3-byte Set-point Info binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Zone Index (uint8)           : 00
      +1       h      2B   Setpoint Temp (int16*100)    : 08 34 (21.00°C)
      --------------------------------------------------------------
      Field-spaced hex : 00 0834
      Payload hex      : 000834

    :param zone_idx: Zone index byte.
    :type zone_idx: int
    :param setpoint_temp: Setpoint temperature in °C.
    :type setpoint_temp: float

    Sample Packet Logs & Protocol Notes:
    # RQ --- 22:131874 01:063844 --:------ 2309 003 020708
    # zone_mode  # TODO: messy
    """

    _STRUCT_FMT_ENTRY: ClassVar[str] = ">Bh"

    zone_idx: int
    setpoint_temp: float | bool | None

    @classmethod
    def _parse_sp_val(cls, sp_raw: int) -> float | bool | None:
        """Decode raw 16-bit signed integer to setpoint temperature value."""
        if sp_raw in (0x31FF, 0x7FFF):
            return None
        if sp_raw == 0x7EFF:
            return False
        return sp_raw / 100.0

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self | list[Self]:
        """Unpack setpoint info binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked SetPointInfoPayload instance or list of instances.
        :rtype: Self | list[Self]
        :raises ValueError: If raw_data length is less than 3 bytes.
        """
        if len(raw_data) < 3:
            raise ValueError(f"Invalid payload length for 2309: {len(raw_data)}")
        if len(raw_data) > 3 and len(raw_data) % 3 == 0:
            return [
                cls(
                    zone_idx=idx,
                    setpoint_temp=cls._parse_sp_val(sp_raw),
                )
                for idx, sp_raw in (
                    struct.unpack_from(cls._STRUCT_FMT_ENTRY, raw_data, i)
                    for i in range(0, len(raw_data), 3)
                )
            ]

        idx, sp_raw = struct.unpack_from(cls._STRUCT_FMT_ENTRY, raw_data, 0)
        return cls(zone_idx=idx, setpoint_temp=cls._parse_sp_val(sp_raw))

    def to_bytes(self) -> bytes:
        """Pack setpoint info data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        if self.setpoint_temp is None:
            sp_raw = 0x7FFF
        elif self.setpoint_temp is False:
            sp_raw = 0x7EFF
        else:
            sp_raw = int(round(self.setpoint_temp * 100.0))
        return struct.pack(self._STRUCT_FMT_ENTRY, self.zone_idx, sp_raw)

    def to_dict(self) -> dict[str, Any]:
        """Convert setpoint info payload to legacy dictionary layout.

        :returns: Decoded setpoint info dictionary.
        :rtype: dict[str, Any]
        """
        return {
            "zone_idx": f"{self.zone_idx:02X}",
            "setpoint": self.setpoint_temp,
        }


# ----------------------------------------------------------------------


@register_payload("3200")
@dataclass(frozen=True, slots=True)
class FlowTempPayload(PayloadBase):
    """Boiler supply flow temperature payload (Opcode 3200).

    3-byte Flow Temperature binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Domain Index (uint8)         : 00
      +1       h      2B   Temperature (int16*100)      : 13 1A (48.90°C)
      --------------------------------------------------------------
      Field-spaced hex : 00 131A
      Payload hex      : 00131A

    Sample Packet Logs:
      # RP --- 10:048122 18:006402 --:------ 3200 003 00131A

    :param domain_idx: Domain index byte.
    :type domain_idx: int
    :param temperature: Flow temperature in °C, or None if invalid.
    :type temperature: float | None
    """

    _STRUCT_FMT: ClassVar[str] = ">Bh"

    domain_idx: int
    temperature: float | None

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack flow temperature binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked FlowTempPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 3 bytes.
        """
        if len(raw_data) < 3:
            raise ValueError(f"Invalid payload length for 3200: {len(raw_data)}")
        idx, temp_raw = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        temp_val = None if temp_raw in (0x31FF, 0x7FFF) else temp_raw / 100.0
        return cls(domain_idx=idx, temperature=temp_val)

    def to_bytes(self) -> bytes:
        """Pack flow temperature data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        temp_raw = (
            0x7FFF if self.temperature is None else int(round(self.temperature * 100.0))
        )
        return struct.pack(self._STRUCT_FMT, self.domain_idx, temp_raw)

    def to_dict(self) -> dict[str, Any]:
        """Convert flow temperature payload to legacy dictionary layout.

        :returns: Decoded flow temperature dictionary.
        :rtype: dict[str, Any]
        """
        return {"temperature": self.temperature}


# ----------------------------------------------------------------------


@register_payload("0005")
class SystemZonesPayload(PayloadBase):
    """Master payload dispatcher and base class for Opcode 0005.

    Sample Packet Logs & Protocol Notes:
    # the ST9520C can support two heating zones, so: msg.len in (7, 14)?
    # Note: ATC928G1000 (1st gen monochrome model) uses 3-byte payload (max 8 zones).
    # UFC devices use seqx[2:4] for UFH zone mapping.
    # .I --- 01:145038 --:------ 01:145038 0005 004 00000100
    # RP --- 02:017205 18:073736 --:------ 0005 004 0009001F
    # .I --- 34:064023 --:------ 34:064023 0005 012 000A0000-000F0000-00100000
    """

    VARIANTS: ClassVar[tuple[type[PayloadBase], ...]] = ()

    def __new__(
        cls,
        zone_type: int = 0,
        zone_mask: int = 0,
        zone_class_id: int = 0,
    ) -> Any:
        """Construct SystemZones payload variant dynamically from arguments."""
        if cls is not SystemZonesPayload:
            return super().__new__(cls)
        return SystemZones4BPayload(
            zone_type=zone_type,
            zone_mask=zone_mask,
            zone_class_id=zone_class_id if zone_class_id != 0 else zone_type,
        )

    def to_dict(self, msg: Any = None) -> dict[str, Any] | list[dict[str, Any]]:
        """Convert system zones payload to legacy dictionary layout."""
        z_type = getattr(self, "zone_type", 0)
        z_mask = getattr(self, "zone_mask", 0)

        type_str = f"{z_type:02X}"
        zone_class = ZON_ROLE_MAP.get(
            type_str, DEV_ROLE_MAP.get(type_str, "heating_zone")
        )

        bits = [(z_mask >> i) & 1 for i in range(16)]

        return {
            "zone_type": type_str,
            "zone_mask": bits,
            "zone_class": zone_class,
        }

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> PayloadBase | list[PayloadBase]:
        """Unpack system zones binary payload, dispatching by length."""
        if len(raw_data) > 4 and len(raw_data) % 4 == 0:
            res: list[PayloadBase] = []
            for i in range(0, len(raw_data), 4):
                res.append(SystemZones4BPayload.from_bytes(raw_data[i : i + 4]))
            return res
        if len(raw_data) == 3:
            return SystemZones3BPayload.from_bytes(raw_data)
        if len(raw_data) >= 4:
            return SystemZones4BPayload.from_bytes(raw_data)
        raise ValueError(f"Invalid payload length for 0005: {len(raw_data)}")

    def to_bytes(self) -> bytes:
        """Pack payload base default method.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        :raises NotImplementedError: Master dispatcher must dispatch to
            variant sub-dataclass.
        """
        raise NotImplementedError("Use concrete variant sub-dataclass")


@dataclass(frozen=True, slots=True)
class SystemZones3BPayload(SystemZonesPayload):
    """3-byte system zones payload (Opcode 0005, e.g. ATC928G1000).

    3-byte System Zones binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Header / Index               : 00
      +1       B      1B   Zone Type / Class ID         : 00
      +2       B      1B   Zone Mask uint8              : 01
      --------------------------------------------------------------
      Field-spaced hex : 00 00 01
      Payload hex      : 000001
    """

    _STRUCT_FMT: ClassVar[str] = ">BBB"

    zone_type: int
    zone_mask: int
    zone_class_id: int

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 3-byte system zones binary payload."""
        if len(raw_data) < 3:
            raise ValueError(
                f"Invalid payload length for SystemZones3BPayload: {len(raw_data)}"
            )
        _hdr, z_type, mask = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(zone_type=z_type, zone_mask=mask, zone_class_id=z_type)

    def to_bytes(self) -> bytes:
        """Pack 3-byte system zones binary payload."""
        return struct.pack(self._STRUCT_FMT, 0x00, self.zone_type, self.zone_mask)


@dataclass(frozen=True, slots=True)
class SystemZones4BPayload(SystemZonesPayload):
    """4-byte system zones payload (Opcode 0005).

    4-byte System Zones binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Header / Index               : 00
      +1       B      1B   Zone Type / Class ID         : 00
      +2       H      2B   Zone Mask uint16             : 01 00
      --------------------------------------------------------------
      Field-spaced hex : 00 00 0100
      Payload hex      : 00000100
    """

    _STRUCT_FMT_HDR: ClassVar[str] = ">BB"

    zone_type: int
    zone_mask: int
    zone_class_id: int

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 4-byte system zones binary payload."""
        if len(raw_data) < 4:
            raise ValueError(
                f"Invalid payload length for SystemZones4BPayload: {len(raw_data)}"
            )
        _hdr, z_type = struct.unpack_from(cls._STRUCT_FMT_HDR, raw_data, 0)
        (mask,) = struct.unpack_from("<H", raw_data, 2)
        return cls(zone_type=z_type, zone_mask=mask, zone_class_id=z_type)

    def to_bytes(self) -> bytes:
        """Pack 4-byte system zones binary payload."""
        return struct.pack(self._STRUCT_FMT_HDR, 0x00, self.zone_type) + struct.pack(
            "<H", self.zone_mask
        )


# Update VARIANTS property after variants are defined
SystemZonesPayload.VARIANTS = (
    SystemZones3BPayload,
    SystemZones4BPayload,
)


# ----------------------------------------------------------------------


@register_payload("0008")
@dataclass(frozen=True, slots=True)
class RelayDemandPayload(PayloadBase):
    """Relay demand payload (Opcode 0008).

    2-byte Relay Demand binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Domain / Zone Index (uint8)  : 00
      +1       B      1B   Relay Demand uint8 (0-200)   : 64 (50%)
      --------------------------------------------------------------
      Field-spaced hex : 00 64
      Payload hex      : 0064

    Sample Packet Logs & Protocol Notes:
      # See: https://www.domoticaforum.eu/viewtopic.php?f=7&t=5806&start=105#p73681
      # .I --- 01:145038 --:------ 01:145038 0008 002 0314
      # .I --- 01:145038 --:------ 01:145038 0008 002 F914
      # .I --- 01:054173 --:------ 01:054173 0008 002 FA00
      # .I --- 01:145038 --:------ 01:145038 0008 002 FC14
      # RP --- 13:109598 18:199952 --:------ 0008 002 0000
      # RP --- 13:109598 18:199952 --:------ 0008 002 00C8
      # Note: 0008[2:4] maps to 3EF0[2:4] and 3EF1[10:12]. Honeywell Jasper (JST) uses 13-byte variant.

    :param domain_or_zone_idx: Domain or zone index byte.
    :type domain_or_zone_idx: int
    :param demand_percent: Heat demand percentage (0.0 - 100.0).
    :type demand_percent: float
    :param raw_extra: Optional trailing payload bytes for 13-byte Jasper payloads.
    :type raw_extra: bytes | None
    """

    _STRUCT_FMT_2B: ClassVar[str] = ">BB"

    domain_or_zone_idx: int
    demand_percent: float
    raw_extra: bytes | None = None

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack relay demand binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked RelayDemandPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 2 bytes.
        """
        if len(raw_data) < 2:
            raise ValueError(f"Invalid payload length for 0008: {len(raw_data)}")
        idx, demand_raw = struct.unpack_from(cls._STRUCT_FMT_2B, raw_data, 0)
        extra = raw_data[2:] if len(raw_data) > 2 else None
        return cls(
            domain_or_zone_idx=idx,
            demand_percent=demand_raw / 200.0,
            raw_extra=extra,
        )

    def to_bytes(self) -> bytes:
        """Pack relay demand data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        demand_raw = min(200, max(0, int(round(self.demand_percent * 200.0))))
        res = struct.pack(self._STRUCT_FMT_2B, self.domain_or_zone_idx, demand_raw)
        if self.raw_extra is not None:
            res += self.raw_extra
        return res

    def to_dict(self) -> dict[str, Any]:
        """Convert relay demand payload to legacy dictionary layout.

        :returns: Decoded relay demand dictionary.
        """
        return {"relay_demand": self.demand_percent}


# ----------------------------------------------------------------------


@register_payload("000C")
class ZoneDevicesPayload(PayloadBase):
    """Master payload dispatcher and base class for Opcode 000C.

    Protocol & Heuristic Notes:
      # TODO: 000C to a UFC should be ufh_idx, not zone_idx
      # NOTE: Both len=5 and len=6 elements are valid! So collision when len = 036!
      # Note: 000C sent to a UFC device represents ufh_idx rather than zone_idx
      # If sent to/from a UFC device, byte 0 is ufh_idx and sub_idx is zone_idx
      # Sample Packet Logs:
      # .I --- 34:092243 --:------ 34:092243 000C 018 00-0A-7F-FFFFFF
      # RP --- 01:145038 18:013393 --:------ 000C 006 00-00-00-10DAFD
      # RP --- 01:145038 18:013393 --:------ 000C 012 01-00-00-10DAF5 01-00-00-10DAFB
      # RP --- 01:239474 18:198929 --:------ 000C 012 06-00-00119A99 06-00-00119B21
      # RP --- 01:069616 18:205592 --:------ 000C 011 01-00-00121B54    00-00121B52
      # RP --- 01:239700 18:009874 --:------ 000C 018 07-08-001099C3 07-08-001099C5
      # RP --- 01:059885 18:010642 --:------ 000C 016 00-00-0011EDAA    00-0011ED92
    """

    VARIANTS: ClassVar[tuple[type[PayloadBase], ...]] = ()

    def __new__(
        cls,
        zone_idx_raw: int = 0,
        device_role_id: int = 0,
        device_id_raw: int = 0,
        sub_idx: int = 0,
    ) -> Any:
        """Construct ZoneDevices payload variant dynamically from arguments."""
        if cls is not ZoneDevicesPayload:
            return super().__new__(cls)
        if sub_idx != 0:
            return ZoneDevices6BPayload(
                zone_idx_raw=zone_idx_raw,
                device_role_id=device_role_id,
                sub_idx=sub_idx,
                device_id_raw=device_id_raw,
            )
        return ZoneDevices5BPayload(
            zone_idx_raw=zone_idx_raw,
            device_role_id=device_role_id,
            device_id_raw=device_id_raw,
            sub_idx=0,
        )

    @property
    def zone_idx(self) -> int | None:
        """Return numeric zone index or None if domain-level binding."""
        if getattr(self, "device_role_id", 0) in (0x0F, 0x0E, 0x0D):
            return None
        sub = getattr(self, "sub_idx", 0)
        z_raw = getattr(self, "zone_idx_raw", 0)
        return sub if getattr(self, "device_role_id", 0) == 0x09 and sub else z_raw

    def to_dict(self, msg: Any = None) -> dict[str, Any]:
        """Convert zone devices payload to legacy dictionary layout."""
        role_id = getattr(self, "device_role_id", 0)
        z_raw = getattr(self, "zone_idx_raw", 0)
        sub = getattr(self, "sub_idx", 0)
        dev_raw = getattr(self, "device_id_raw", 0)

        role_hex = f"{role_id:02X}"
        if role_hex == "0E" and z_raw == 1:
            device_role = "heating_valve"
        else:
            device_role = DEV_ROLE_MAP.get(role_hex, role_hex)

        result: dict[str, Any] = {
            "zone_type": role_hex,
            "device_role": device_role,
        }

        if role_hex == "09":
            is_ufc_device = False
            if msg is not None and getattr(msg, "src", None) is not None:
                source_id_str = str(getattr(msg.src, "id", msg.src))
                if source_id_str.startswith("02:") or getattr(msg.src, "type", "") in (
                    "02",
                    "UFC",
                ):
                    is_ufc_device = True

            if is_ufc_device or (sub is not None and sub != 0x7F):
                result["ufh_idx"] = f"{z_raw:02X}"
                result["zone_idx"] = None if sub == 0x7F else f"{sub:02X}"
            else:
                result["zone_idx"] = f"{z_raw:02X}"
        elif role_hex in ("0D", "0E"):
            result["domain_id"] = "FA" if z_raw == 0 else "F9"
        elif role_hex == "0F":
            result["domain_id"] = "FC"
        else:
            result["zone_idx"] = f"{z_raw:02X}"

        dev_hex = f"{dev_raw:06X}"
        if dev_hex in ("7FFFFF", "FFFFFF", "000000"):
            result["devices"] = []
        else:
            result["devices"] = [Address.convert_from_hex(dev_hex)]

        return result

    @property
    def domain_id(self) -> str | None:
        """Derive authoritative binding domain ID (FC, FA, F9) from role & index."""
        role_id = getattr(self, "device_role_id", 0)
        z_raw = getattr(self, "zone_idx_raw", 0)
        if role_id == 0x0F:
            return "FC"
        if role_id in (0x0E, 0x0D):
            return "FA" if z_raw == 0 else "F9"
        return None

    @property
    def device_id_str(self) -> str:
        """Format raw 24-bit device ID to standard RAMSES device ID string."""
        dev_raw = getattr(self, "device_id_raw", 0)
        return f"{dev_raw:06X}"

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> PayloadBase | list[PayloadBase]:
        """Unpack zone devices binary payload, dispatching by length."""
        if len(raw_data) < 5:
            raise ValueError(f"Invalid payload length for 000C: {len(raw_data)}")

        if (
            len(raw_data) >= 11
            and (len(raw_data) - 6) % 5 == 0
            and (len(raw_data) % 6 != 0 or raw_data[6] != raw_data[0])
        ):
            res_list: list[PayloadBase] = [
                ZoneDevices6BPayload.from_bytes(raw_data[:6])
            ]
            zone_idx = raw_data[0]
            for i in range(6, len(raw_data), 5):
                role_id_seq, sub_idx_seq, dev_bytes_seq = struct.unpack_from(
                    ">BB3s", raw_data, i
                )
                res_list.append(
                    ZoneDevices6BPayload(
                        zone_idx_raw=zone_idx,
                        device_role_id=role_id_seq,
                        sub_idx=sub_idx_seq,
                        device_id_raw=int.from_bytes(dev_bytes_seq, byteorder="big"),
                    )
                )
            return res_list

        if len(raw_data) >= 6 and len(raw_data) % 6 == 0:
            return [
                ZoneDevices6BPayload.from_bytes(raw_data[i : i + 6])
                for i in range(0, len(raw_data), 6)
            ]

        if len(raw_data) > 5 and len(raw_data) % 5 == 0 and len(raw_data) != 6:
            return [
                ZoneDevices5BPayload.from_bytes(raw_data[i : i + 5])
                for i in range(0, len(raw_data), 5)
            ]

        if len(raw_data) >= 6:
            return ZoneDevices6BPayload.from_bytes(raw_data)
        return ZoneDevices5BPayload.from_bytes(raw_data)

    def to_bytes(self) -> bytes:
        """Pack payload base default method.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        :raises NotImplementedError: Master dispatcher must dispatch to sub-dataclass.
        """
        raise NotImplementedError("Use concrete variant sub-dataclass")


@dataclass(frozen=True, slots=True)
class ZoneDevices5BPayload(ZoneDevicesPayload):
    """5-byte zone device mapping payload (Opcode 000C).

    5-byte Zone Devices binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Zone Index Raw (uint8)       : 00
      +1       B      1B   Device Role ID (uint8)       : 00
      +2       3s     3B   Device ID Raw                : 01 23 45
      --------------------------------------------------------------
      Field-spaced hex : 00 00 012345
      Payload hex      : 0000012345
    """

    _STRUCT_FMT: ClassVar[str] = ">BB3s"

    zone_idx_raw: int
    device_role_id: int
    device_id_raw: int
    sub_idx: int = 0

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 5-byte zone devices binary payload."""
        if len(raw_data) < 5:
            raise ValueError(
                f"Invalid payload length for ZoneDevices5BPayload: {len(raw_data)}"
            )
        zone_idx, role_id, dev_bytes = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(
            zone_idx_raw=zone_idx,
            device_role_id=role_id,
            device_id_raw=int.from_bytes(dev_bytes, byteorder="big"),
            sub_idx=0,
        )

    def to_bytes(self) -> bytes:
        """Pack 5-byte zone devices binary payload."""
        dev_bytes = self.device_id_raw.to_bytes(3, byteorder="big")
        return struct.pack(
            self._STRUCT_FMT,
            self.zone_idx_raw,
            self.device_role_id,
            dev_bytes,
        )


@dataclass(frozen=True, slots=True)
class ZoneDevices6BPayload(ZoneDevicesPayload):
    """6-byte zone device mapping payload (Opcode 000C).

    6-byte Zone Devices binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Zone Index Raw (uint8)       : 00
      +1       B      1B   Device Role ID (uint8)       : 00
      +2       B      1B   Sub Index (uint8)            : 00
      +3       3s     3B   Device ID Raw                : 01 23 45
      --------------------------------------------------------------
      Field-spaced hex : 00 00 00 012345
      Payload hex      : 000000012345
    """

    _STRUCT_FMT: ClassVar[str] = ">BBB3s"

    zone_idx_raw: int
    device_role_id: int
    sub_idx: int
    device_id_raw: int

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 6-byte zone devices binary payload."""
        if len(raw_data) < 6:
            raise ValueError(
                f"Invalid payload length for ZoneDevices6BPayload: {len(raw_data)}"
            )
        zone_idx, role_id, sub_idx, dev_bytes = struct.unpack_from(
            cls._STRUCT_FMT, raw_data, 0
        )
        return cls(
            zone_idx_raw=zone_idx,
            device_role_id=role_id,
            sub_idx=sub_idx,
            device_id_raw=int.from_bytes(dev_bytes, byteorder="big"),
        )

    def to_bytes(self) -> bytes:
        """Pack 6-byte zone devices binary payload."""
        dev_bytes = self.device_id_raw.to_bytes(3, byteorder="big")
        return struct.pack(
            self._STRUCT_FMT,
            self.zone_idx_raw,
            self.device_role_id,
            self.sub_idx,
            dev_bytes,
        )


# Update VARIANTS property after variants are defined
ZoneDevicesPayload.VARIANTS = (
    ZoneDevices5BPayload,
    ZoneDevices6BPayload,
)


# ----------------------------------------------------------------------


@register_payload("1081")
@dataclass(frozen=True, slots=True)
class MaxChSetpointPayload(PayloadBase):
    """Maximum CH supply setpoint temperature payload (Opcode 1081).

    3-byte Max CH Setpoint binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Header / Index               : 00
      +1       h      2B   Setpoint Temp (int16*100)    : 1F 40 (80.00°C)
      --------------------------------------------------------------
      Field-spaced hex : 00 1F40
      Payload hex      : 001F40

    :param setpoint_temp: Maximum CH setpoint temperature in °C.
    :type setpoint_temp: float
    """

    _STRUCT_FMT: ClassVar[str] = ">Bh"

    setpoint_temp: float

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack max CH setpoint binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked MaxChSetpointPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 3 bytes.
        """
        if len(raw_data) < 3:
            raise ValueError(f"Invalid payload length for 1081: {len(raw_data)}")
        _hdr, temp_raw = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(setpoint_temp=temp_raw / 100.0)

    def to_bytes(self) -> bytes:
        """Pack max CH setpoint data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        temp_raw = int(round(self.setpoint_temp * 100.0))
        return struct.pack(self._STRUCT_FMT, 0x00, temp_raw)

    def to_dict(self) -> dict[str, Any]:
        """Convert max CH setpoint payload to legacy dictionary layout.

        :returns: Decoded setpoint dictionary.
        :rtype: dict[str, Any]
        """
        return {"setpoint": self.setpoint_temp}


# ----------------------------------------------------------------------


@register_payload("1090")
@dataclass(frozen=True, slots=True)
class Opcode1090Payload(PayloadBase):
    """Dual temperature status payload (Opcode 1090).

    5-byte Opcode 1090 binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Header / Index               : 00
      +1       h      2B   Temperature 0 (int16*100)    : 07 D0 (20.00°C)
      +3       h      2B   Temperature 1 (int16*100)    : 01 F4 (5.00°C)
      --------------------------------------------------------------
      Field-spaced hex : 00 07D0 01F4
      Payload hex      : 0007D001F4

    :param temp_0: First temperature value in °C.
    :type temp_0: float
    :param temp_1: Second temperature value in °C.
    :type temp_1: float

    Sample Packet Logs & Protocol Notes:
    # unknown_1090 (non-Evohome, e.g. ST9520C)
    # 14:08:05.176 095 RP --- 23:100224 22:219457 --:------ 1090 005
    # 18:08:05.809 095 RP --- 23:100224 22:219457 --:------ 1090 005
    """

    _STRUCT_FMT: ClassVar[str] = ">Bhh"

    temp_0: float
    temp_1: float

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack opcode 1090 binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked Opcode1090Payload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 5 bytes.
        """
        if len(raw_data) < 5:
            raise ValueError(f"Invalid payload length for 1090: {len(raw_data)}")
        _hdr, t0_raw, t1_raw = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(temp_0=t0_raw / 100.0, temp_1=t1_raw / 100.0)

    def to_bytes(self) -> bytes:
        """Pack opcode 1090 data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        t0_raw = int(round(self.temp_0 * 100.0))
        t1_raw = int(round(self.temp_1 * 100.0))
        return struct.pack(self._STRUCT_FMT, 0x00, t0_raw, t1_raw)


# ----------------------------------------------------------------------


@register_payload("1100")
class TpiParamsBasePayload(PayloadBase, ABC):
    """Abstract base class for TPI parameters (Opcode 1100)."""


@dataclass(frozen=True, slots=True)
class TpiParams4BPayload(TpiParamsBasePayload):
    """TPI 4-byte parameters layout (Opcode 1100).

    4-byte TPI Parameters binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Domain ID                    : FC
      +1       B      1B   Cycle Rate (cycles/hr * 4)   : 18 (6 cph)
      +2       B      1B   Min On Time (min * 4)        : 04 (1 min)
      +3       B      1B   Min Off Time (min * 4)       : 04 (1 min)
      --------------------------------------------------------------
      Field-spaced hex : FC 18 04 04
      Payload hex      : FC180404

    :param domain_id: Domain identifier byte.
    :type domain_id: int
    :param cycle_rate: Cycle rate in cycles per hour.
    :type cycle_rate: int
    :param min_on_time: Minimum on-time in minutes.
    :type min_on_time: float
    :param min_off_time: Minimum off-time in minutes.
    :type min_off_time: float
    """

    _STRUCT_FMT: ClassVar[str] = ">BBBB"

    domain_id: int
    cycle_rate: int
    min_on_time: float
    min_off_time: float

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 4-byte TPI parameters binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked TpiParams4BPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 4 bytes.
        """
        if len(raw_data) < 4:
            raise ValueError(
                f"Invalid payload length for TpiParams4BPayload: {len(raw_data)}"
            )
        domain_id, crate_raw, on_raw, off_raw = struct.unpack_from(
            cls._STRUCT_FMT, raw_data, 0
        )
        return cls(
            domain_id=domain_id,
            cycle_rate=crate_raw // 4,
            min_on_time=on_raw / 4.0,
            min_off_time=off_raw / 4.0,
        )

    def to_bytes(self) -> bytes:
        """Pack 4-byte TPI parameters into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        crate_raw = self.cycle_rate * 4
        on_raw = int(round(self.min_on_time * 4.0))
        off_raw = int(round(self.min_off_time * 4.0))
        return struct.pack(
            self._STRUCT_FMT,
            self.domain_id,
            crate_raw,
            on_raw,
            off_raw,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert TPI parameters payload to legacy dictionary layout."""
        dom = (
            f"{self.domain_id:02X}"
            if isinstance(self.domain_id, int)
            else self.domain_id
        )
        return {
            "domain_id": dom,
            "cycle_rate": self.cycle_rate,
            "min_on_time": self.min_on_time,
            "min_off_time": self.min_off_time,
            "proportional_band_width": None,
        }


@dataclass(frozen=True, slots=True)
class TpiParams8BPayload(TpiParamsBasePayload):
    """TPI 8-byte parameters layout (Opcode 1100).

    8-byte TPI Parameters binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Domain ID                    : FC
      +1       B      1B   Cycle Rate (cycles/hr * 4)   : 18 (6 cph)
      +2       B      1B   Min On Time (min * 4)        : 04 (1 min)
      +3       B      1B   Min Off Time (min * 4)       : 04 (1 min)
      +4       B      1B   Flags / Trailing             : 00
      +5       h      2B   Proportional Band Width      : 7F FF (None)
      +7       B      1B   Trailing byte                : 00
      --------------------------------------------------------------
      Field-spaced hex : FC 18 04 04 00 7FFF 00
      Payload hex      : FC180404007FFF00

    :param domain_id: Domain identifier byte.
    :type domain_id: int
    :param cycle_rate: Cycle rate in cycles per hour.
    :type cycle_rate: int
    :param min_on_time: Minimum on-time in minutes.
    :type min_on_time: float
    :param min_off_time: Minimum off-time in minutes.
    :type min_off_time: float
    :param proportional_band_width: Optional proportional bandwidth value.
    :type proportional_band_width: float | None
    """

    _STRUCT_FMT: ClassVar[str] = ">BBBBBhB"

    domain_id: int
    cycle_rate: int
    min_on_time: float
    min_off_time: float
    proportional_band_width: float | None

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 8-byte TPI parameters binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked TpiParams8BPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 8 bytes.
        """
        if len(raw_data) < 8:
            raise ValueError(
                f"Invalid payload length for TpiParams8BPayload: {len(raw_data)}"
            )
        domain_id, crate_raw, on_raw, off_raw, _flag, pbw_raw, _tail = (
            struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        )
        pbw = None if pbw_raw in (0x7FFF, 32767) else pbw_raw / 100.0
        return cls(
            domain_id=domain_id,
            cycle_rate=crate_raw // 4,
            min_on_time=on_raw / 4.0,
            min_off_time=off_raw / 4.0,
            proportional_band_width=pbw,
        )

    def to_bytes(self) -> bytes:
        """Pack 8-byte TPI parameters into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        crate_raw = self.cycle_rate * 4
        on_raw = int(round(self.min_on_time * 4.0))
        off_raw = int(round(self.min_off_time * 4.0))
        pbw_raw = (
            0x7FFF
            if self.proportional_band_width is None
            else int(round(self.proportional_band_width * 100.0))
        )
        return struct.pack(
            self._STRUCT_FMT,
            self.domain_id,
            crate_raw,
            on_raw,
            off_raw,
            0x00,
            pbw_raw,
            0x00,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert TPI parameters payload to legacy dictionary layout."""
        dom = (
            f"{self.domain_id:02X}"
            if isinstance(self.domain_id, int)
            else self.domain_id
        )
        return {
            "domain_id": dom,
            "cycle_rate": self.cycle_rate,
            "min_on_time": self.min_on_time,
            "min_off_time": self.min_off_time,
            "proportional_band_width": self.proportional_band_width,
        }


@register_payload("1100")
class TpiParamsPayload(PayloadBase, ABC):
    """Master payload dispatcher for Opcode 1100.

    Domain Notes & Sample Packet Logs:
    # tpi_params (domain/zone/device)  # FIXME: a bit messy
    # for:             TPI              // heatpump
    #  - cycle_rate:   6 (3, 6, 9, 12)  // ?? (1-9)
    #  - min_on_time:  1 (1-5)          // ?? (1, 5, 10,...30)
    #  - min_off_time: 1 (1-?)          // ?? (0, 5, 10, 15)
    #  I --- 01:172368 --:------ 01:172368 1100 008 FC180400007FFF00
    #  I --- 01:172368 13:040439 --:------ 1100 008 FC042814007FFF00
    # RQ --- 01:145038 13:163733 --:------ 1100 008 00180400007FFF01  # boiler relay
    # RP --- 13:163733 01:145038 --:------ 1100 008 00180400FF7FFF01
    # RQ --- 01:145038 13:035462 --:------ 1100 008 FC240428007FFF01  # not boiler relay
    # RP --- 13:035462 01:145038 --:------ 1100 008 00240428007FFF01
    # only 10:040239 does 0b01000000, only Itho Autotemp does 0b00010000
    """

    VARIANTS: ClassVar[tuple[type[PayloadBase], ...]] = (
        TpiParams4BPayload,
        TpiParams8BPayload,
    )

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> TpiParams4BPayload | TpiParams8BPayload:
        """Unpack TPI params binary payload, dispatching by length."""
        if len(raw_data) >= 8:
            return TpiParams8BPayload.from_bytes(raw_data)
        return TpiParams4BPayload.from_bytes(raw_data)


# ----------------------------------------------------------------------


@register_payload("1300")
@dataclass(frozen=True, slots=True)
class ChPressurePayload(PayloadBase):
    """Central heating system pressure payload (Opcode 1300).

    3-byte CH Pressure binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Header / Index               : 00
      +1       h      2B   Pressure in Bar (int16*100)  : 00 EA (2.34 Bar)
      --------------------------------------------------------------
      Field-spaced hex : 00 00EA
      Payload hex      : 0000EA

    :param pressure_bar: CH system pressure in Bar, or None if invalid.
    :type pressure_bar: float | None

    Domain Notes:
    # 0x9F6 (2550 dec = 2.55 bar) appears to be a sentinel value
    """

    pressure_bar: float | None

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack CH pressure binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked ChPressurePayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 3 bytes.
        """
        if len(raw_data) < 3:
            raise ValueError(f"Invalid payload length for 1300: {len(raw_data)}")
        if raw_data[1:3] in (b"\x09\xf6", b"\x7f\xff"):
            return cls(pressure_bar=None)
        val_raw = int.from_bytes(raw_data[1:3], byteorder="big", signed=True)
        return cls(
            pressure_bar=None if val_raw in (0x31FF, 0x7FFF) else val_raw / 100.0
        )

    def to_bytes(self) -> bytes:
        """Pack CH pressure data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        if self.pressure_bar is None:
            return b"\x00\x09\xf6"
        val_raw = int(round(self.pressure_bar * 100.0))
        return b"\x00" + val_raw.to_bytes(2, byteorder="big", signed=True)

    def to_dict(self) -> dict[str, Any]:
        """Convert CH pressure payload to legacy dictionary layout.

        :returns: Decoded pressure dictionary.
        :rtype: dict[str, Any]
        """
        return {"pressure": self.pressure_bar}


# ----------------------------------------------------------------------


@register_payload("2349")
@dataclass(frozen=True, slots=True)
class ZoneModePayload(PayloadBase):
    """Zone operating mode and override payload (Opcode 2349).

    7-byte Zone Mode binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Zone Index (uint8)           : 00
      +1       h      2B   Setpoint Temp (int16*100)    : 08 34 (21.00°C)
      +3       B      1B   Zone Mode Code (uint8)       : 00 (Follow)
      +4       3B     3B   Duration / Until Bytes       : FF FF FF
      --------------------------------------------------------------
      Field-spaced hex : 00 0834 00 FFFFFF
      Payload hex      : 00083400FFFFFF

    Protocol Notes & Sample Packet Logs:
      # Hometronic controllers do not react to W/2349 but
      # require W/2309 instead.
      # RP --- 30:258557 34:225071 --:------ 2349 013 007FFF00FFFFFFFFFFFFFFFFFF
      # RP --- 30:253184 34:010943 --:------ 2349 013 00064000FFFFFF00110E0507E5
      # RQ --- 34:225071 30:258557 --:------ 2349 001 00
      # .I --- 10:067219 --:------ 10:067219 2349 004 00000001
      # .W --- 18:141846 01:050858 --:------ 2349 013 02-0960-04-FFFFFF-0409160607E5

    :param zone_idx: Zone index byte.
    :type zone_idx: int
    :param setpoint_temp: Target setpoint temperature in °C.
    :type setpoint_temp: float
    :param mode_code: Zone mode code integer.
    :type mode_code: int
    :param duration_minutes: Override duration in minutes, if present.
    :type duration_minutes: int | None
    :param until_dtm: Expiration datetime for temporary override.
    :type until_dtm: str | dt | bytes | None
    """

    _STRUCT_FMT_BASE: ClassVar[str] = ">BhB"

    zone_idx: int | str
    setpoint_temp: float | None
    mode_code: int | str
    duration_minutes: int | None = None
    until_dtm: str | dt | bytes | None = None

    def __post_init__(self) -> None:
        """Normalise index arguments."""
        if isinstance(self.zone_idx, str):
            object.__setattr__(self, "zone_idx", parse_idx(self.zone_idx))
        if isinstance(self.mode_code, str):
            object.__setattr__(self, "mode_code", int(self.mode_code, 16))

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack zone mode binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked ZoneModePayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 4 bytes.
        """
        if len(raw_data) < 4:
            raise ValueError(f"Invalid payload length for 2349: {len(raw_data)}")
        raw_idx, sp_raw, mode = struct.unpack_from(cls._STRUCT_FMT_BASE, raw_data, 0)
        setpoint = None if sp_raw in (0x31FF, 0x7FFF) else sp_raw / 100.0
        dur = None
        if len(raw_data) >= 7 and raw_data[4:7] != b"\xff\xff\xff":
            dur = int.from_bytes(raw_data[4:7], byteorder="big")
        until_raw = None
        if len(raw_data) >= 13:
            until_raw = hex_to_dtm(raw_data[7:13].hex().upper())
        return cls(
            zone_idx=raw_idx,
            setpoint_temp=setpoint,
            mode_code=mode,
            duration_minutes=dur,
            until_dtm=until_raw,
        )

    def to_bytes(self) -> bytes:
        """Pack zone mode data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        idx = parse_idx(self.zone_idx)
        if self.setpoint_temp is None:
            sp_raw = 0x7FFF
        else:
            sp_raw = int(round(self.setpoint_temp * 100.0))
        mode = (
            int(self.mode_code, 16)
            if isinstance(self.mode_code, str)
            else self.mode_code
        )
        res = struct.pack(self._STRUCT_FMT_BASE, idx, sp_raw, mode)
        if self.duration_minutes is not None:
            res += self.duration_minutes.to_bytes(3, byteorder="big")
        else:
            res += b"\xff\xff\xff"
        if self.until_dtm is not None:
            if isinstance(self.until_dtm, bytes):
                res += self.until_dtm
            elif (
                isinstance(self.until_dtm, str)
                and len(self.until_dtm) == 12
                and all(c in "0123456789ABCDEFabcdef" for c in self.until_dtm)
            ):
                res += bytes.fromhex(self.until_dtm)
            else:
                res += bytes.fromhex(hex_from_dtm(self.until_dtm))
        return res

    def to_dict(self) -> dict[str, Any]:
        """Convert zone mode payload to legacy dictionary layout.

        :returns: Decoded zone mode dictionary.
        :rtype: dict[str, Any]
        """
        mode_code_hex = (
            f"{self.mode_code:02X}"
            if isinstance(self.mode_code, int)
            else str(self.mode_code)
        )
        mode_str = ZON_MODE_MAP.get(mode_code_hex, mode_code_hex)
        idx_str = (
            f"{self.zone_idx:02X}" if isinstance(self.zone_idx, int) else self.zone_idx
        )
        res: dict[str, Any] = {
            "zone_idx": idx_str,
            "mode": mode_str,
            "setpoint": self.setpoint_temp,
        }
        if self.duration_minutes is not None:
            res["duration"] = self.duration_minutes
        if self.until_dtm is not None:
            res["until"] = self.until_dtm
        return res


# ----------------------------------------------------------------------


@register_payload("2389")
@dataclass(frozen=True, slots=True)
class SetpointOverridePayload(PayloadBase):
    """Target setpoint override payload (Opcode 2389).

    3-byte Setpoint Override binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Domain / Zone Index (uint8)  : 00
      +1       h      2B   Target Temp (int16*100)      : 07 D0 (20.00°C)
      --------------------------------------------------------------
      Field-spaced hex : 00 07D0
      Payload hex      : 0007D0

    :param domain_or_zone_idx: Domain or zone index byte.
    :type domain_or_zone_idx: int
    :param target_temp: Target temperature in °C.
    :type target_temp: float
    """

    _STRUCT_FMT: ClassVar[str] = ">Bh"

    domain_or_zone_idx: int
    target_temp: float

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack setpoint override binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked SetpointOverridePayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 3 bytes.
        """
        if len(raw_data) < 3:
            raise ValueError(f"Invalid payload length for 2389: {len(raw_data)}")
        idx, temp_raw = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(
            domain_or_zone_idx=idx,
            target_temp=temp_raw / 100.0,
        )

    def to_bytes(self) -> bytes:
        """Pack setpoint override data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        temp_raw = int(round(self.target_temp * 100.0))
        return struct.pack(self._STRUCT_FMT, self.domain_or_zone_idx, temp_raw)

    def to_dict(self) -> dict[str, Any]:
        """Convert setpoint override payload to legacy dictionary layout.

        :returns: Decoded setpoint override dictionary.
        :rtype: dict[str, Any]
        """
        return {"setpoint": self.target_temp}


# ----------------------------------------------------------------------


@register_payload("3B00")
@dataclass(frozen=True, slots=True)
class ActuatorSyncPayload(PayloadBase):
    """TPI cycle actuator sync payload (Opcode 3B00).

    2-byte Actuator Sync binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Domain ID / Header           : FC
      +1       B      1B   Sync Flag / Command          : C8 (200)
      --------------------------------------------------------------
      Field-spaced hex : FC C8
      Payload hex      : FCC8

    Protocol & Heuristic Notes:
      # 3B00/3EF0 FC broadcasts are emitted by system timing masters and heater relays.
      # Hotwater valves (FA) also broadcast 3B00/3EF0, so 000C binding table overrides 3B00 hints.
      # Sample Packet Logs:
      # 053  I --- 13:209679 --:------ 13:209679 3B00 002 00C8
      # 045  I --- 01:158182 --:------ 01:158182 3B00 002 FCC8

    :param domain_id: Domain identifier byte.
    :type domain_id: int
    :param sync_flag: Sync flag byte.
    :type sync_flag: int

    Sample Packet Logs:
    # 052  I --- 13:209679 --:------ 13:209679 3B00 002 00C8
    # 063  I --- 01:078710 --:------ 01:078710 3B00 002 FCC8
    # 064  I --- 01:078710 --:------ 01:078710 3B00 002 FCC8
    """

    _STRUCT_FMT: ClassVar[str] = ">BB"

    domain_id: int
    sync_flag: int

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack actuator sync binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked ActuatorSyncPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 2 bytes.
        """
        if len(raw_data) < 2:
            raise ValueError(f"Invalid payload length for 3B00: {len(raw_data)}")
        domain_id, sync_flag = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(domain_id=domain_id, sync_flag=sync_flag)

    def to_bytes(self) -> bytes:
        """Pack actuator sync data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        return struct.pack(self._STRUCT_FMT, self.domain_id, self.sync_flag)

    def to_dict(self) -> dict[str, Any]:
        """Convert actuator sync payload to legacy dictionary layout.

        :returns: Decoded actuator sync dictionary.
        :rtype: dict[str, Any]
        """
        return {"actuator_sync": self.sync_flag in (0xC8, 0xFF)}


# ----------------------------------------------------------------------


@register_payload("3EF0")
@dataclass(frozen=True, slots=True)
class ActuatorStatePayload(PayloadBase):
    """Actuator modulation state payload (Opcode 3EF0).

    9-byte Actuator State binary layout (3-byte base + optional trailing fields):
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Domain / Zone Index          : 00
      +1       B      1B   Modulation Level (0-200)     : 64 (50%)
      +2       B      1B   Flags / Status Byte          : FF
      +3       B      1B   Optional Flags 3 Byte        : 10
      +4       B      1B   Optional Unknown Byte 4      : 00
      +5       B      1B   Optional Unknown Byte 5      : FF
      +6       B      1B   Optional OpenTherm Flags 6   : 01
      +7       B      1B   Optional CH Setpoint uint8   : 14 (20°C)
      +8       B      1B   Optional Max Mod uint8       : C8 (100%)
      --------------------------------------------------------------
      Field-spaced hex : 00 64 FF 10 00 FF 01 14 C8
      Payload hex      : 0064FF1000FF0114C8

    Protocol Notes:
      # Honeywell Jasper (JIM) devices emit 4-byte payload containing flags_3.
      # Header context payload[:2] is normally 00.
      # R8820A OpenTherm Bridges emit 9-byte payload containing flags_6,
      # ch_setpoint, and max_rel_modulation.
      # NOTE: some [2:4] appear to intend 0x00-0x64 (high_res=False), instead of 0x00-0xC8
      # NOTE: for best compatibility, all will be switched to 0x00-0xC8 (high_res=True)
      # TODO: These two should be picked up by the regex
      # .I --- 13:042805 --:------ 13:042805 3EF0 003 0000FF
      # .I --- 13:023770 --:------ 13:023770 3EF0 003 00C8FF
      # RP --- 10:004598 34:003611 --:------ 3EF0 006 0000100000FF
      # RP --- 10:004598 34:003611 --:------ 3EF0 006 0000110000FF
      # RP --- 10:138822 01:187666 --:------ 3EF0 006 0064100C00FF
      # RP --- 10:138822 01:187666 --:------ 3EF0 006 0064100200FF
      # RP --- 10:138822 01:187666 --:------ 3EF0 006 000110FA00FF
      # RP --- 13:109598 18:002563 --:------ 3EF1 007 0000BF-00BFC8FF
      # RP --- 10:048122 18:140805 --:------ 3EF1 007 007FFF-003C2A10  # 10:s RP always 7FFF
      # RP --- 13:109598 18:199952 --:------ 3EF1 007 0001B8-01B800FF  # 13:s RP
      # RQ --- 31:004811 13:077615 --:------ 3EF1 001 00
      # RP --- 13:077615 31:004811 --:------ 3EF1 007 00024D001300FF
      # RQ --- 22:068154 13:031208 --:------ 3EF1 002 0000
      # RP --- 13:031208 22:068154 --:------ 3EF1 007 00024E00E000FF

    :param domain_id: Domain or zone index byte.
    :type domain_id: int
    :param modulation_level: Modulation level fraction (0.0 - 1.0).
    :type modulation_level: float
    :param flags_2: Secondary status flag byte.
    :type flags_2: int
    :param flags_3: Optional tertiary status flag byte.
    :type flags_3: int | None
    :param unknown_4: Optional byte 4 status/header byte.
    :type unknown_4: int | None
    :param unknown_5: Optional byte 5 status/header byte.
    :type unknown_5: int | None
    :param flags_6: Optional byte 6 OpenTherm status flag byte.
    :type flags_6: int | None
    :param ch_setpoint: Optional central heating setpoint in °C.
    :type ch_setpoint: int | None
    :param max_rel_modulation: Optional max relative modulation fraction.
    :type max_rel_modulation: float | None
    """

    _STRUCT_FMT: ClassVar[str] = ">BBB"
    _STRUCT_FMT_EXT: ClassVar[str] = ">BBBBB"

    domain_id: int
    modulation_level: float
    flags_2: int
    flags_3: int | None = None
    unknown_4: int | None = None
    unknown_5: int | None = None
    flags_6: int | None = None
    ch_setpoint: int | None = None
    max_rel_modulation: float | None = None
    _raw_length: int = 0

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack actuator state binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked ActuatorStatePayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 3 bytes.
        """
        if len(raw_data) < 3:
            raise ValueError(f"Invalid payload length for 3EF0: {len(raw_data)}")
        domain_id, mod_raw, f2 = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        f3 = raw_data[3] if len(raw_data) >= 4 else None

        u4, u5, f6, ch_setpoint, max_rel_mod = None, None, None, None, None
        if len(raw_data) >= 9:
            u4, u5, f6, ch_setpoint, max_mod_raw = struct.unpack_from(
                cls._STRUCT_FMT_EXT, raw_data, 4
            )
            max_rel_mod = max_mod_raw / 200.0

        return cls(
            domain_id=domain_id,
            modulation_level=mod_raw / 200.0,
            flags_2=f2,
            flags_3=f3,
            unknown_4=u4,
            unknown_5=u5,
            flags_6=f6,
            ch_setpoint=ch_setpoint,
            max_rel_modulation=max_rel_mod,
            _raw_length=len(raw_data),
        )

    def to_bytes(self) -> bytes:
        """Pack actuator state data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        mod_raw = min(200, max(0, int(round(self.modulation_level * 200.0))))
        res = struct.pack(self._STRUCT_FMT, self.domain_id, mod_raw, self.flags_2)
        if self.flags_3 is not None:
            res += bytes([self.flags_3])
        if (
            self.unknown_4 is not None
            and self.unknown_5 is not None
            and self.flags_6 is not None
            and self.ch_setpoint is not None
            and self.max_rel_modulation is not None
        ):
            max_mod_raw = min(200, max(0, int(round(self.max_rel_modulation * 200.0))))
            res += struct.pack(
                self._STRUCT_FMT_EXT,
                self.unknown_4,
                self.unknown_5,
                self.flags_6,
                self.ch_setpoint,
                max_mod_raw,
            )
        return res

    def to_dict(self, msg: Any = None) -> dict[str, Any]:
        """Convert actuator state to the legacy dictionary layout.

        Nine-byte UFC packets expose the HCC100 relay state from byte three;
        other sources and packet lengths retain the standard fields only.
        """
        res: dict[str, Any] = {
            "modulation_level": self.modulation_level,
        }
        if self.flags_3 is not None:
            f3 = self.flags_3
            res.update(
                {
                    "ch_active": bool(f3 & (1 << 1)),
                    "dhw_active": bool(f3 & (1 << 2)),
                    "flame_on": bool(f3 & (1 << 3)),
                    "cool_active": bool(f3 & (1 << 4)),
                }
            )
        if self.flags_6 is not None:
            res["ch_enabled"] = bool(self.flags_6 & (1 << 0))
        if self.ch_setpoint is not None:
            res["ch_setpoint"] = self.ch_setpoint
        if self.max_rel_modulation is not None:
            res["max_rel_modulation"] = self.max_rel_modulation

        if (
            self._raw_length == 9
            and self.flags_3 is not None
            and getattr(getattr(msg, "src", None), "type", None) == "02"
        ):
            relay_byte = self.flags_3
            res["pump_relay_state"] = (
                "cooling"
                if relay_byte & 0x10
                else "heating"
                if relay_byte & 0x02
                else "off"
            )
            if relay_byte & ~0x12 or relay_byte & 0x12 == 0x12:
                res["relay_byte_raw"] = relay_byte
        return res


# ----------------------------------------------------------------------


@register_payload("3EF1")
class ActuatorCyclePayload(PayloadBase):
    """Master payload dispatcher and base class for Opcode 3EF1."""

    VARIANTS: ClassVar[tuple[type[PayloadBase], ...]] = ()

    def __new__(
        cls,
        cycle_countdown_sec: int | None = None,
        actuator_countdown_sec: int | None = None,
        modulation_level: float | None = None,
        domain_idx: int | None = None,
    ) -> Any:
        """Construct ActuatorCycle payload variant dynamically from arguments."""
        if cls is not ActuatorCyclePayload:
            return super().__new__(cls)
        if domain_idx is not None:
            return ActuatorCycle7BPayload(
                domain_idx=domain_idx,
                cycle_countdown_sec=cycle_countdown_sec,
                actuator_countdown_sec=actuator_countdown_sec,
                modulation_level=modulation_level,
            )
        return ActuatorCycle6BPayload(
            cycle_countdown_sec=cycle_countdown_sec,
            actuator_countdown_sec=actuator_countdown_sec,
            modulation_level=modulation_level,
        )

    def to_dict(self, msg: Any = None) -> dict[str, Any]:
        """Convert actuator cycle payload to legacy dictionary layout."""
        return {
            "cycle_countdown": getattr(self, "cycle_countdown_sec", None),
            "actuator_countdown": getattr(self, "actuator_countdown_sec", None),
            "modulation_level": getattr(self, "modulation_level", None),
        }

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> PayloadBase:
        """Unpack actuator cycle binary payload, dispatching by length."""
        if len(raw_data) < 6:
            raise ValueError(f"Invalid payload length for 3EF1: {len(raw_data)}")
        if len(raw_data) >= 7:
            return ActuatorCycle7BPayload.from_bytes(raw_data)
        return ActuatorCycle6BPayload.from_bytes(raw_data)

    def to_bytes(self) -> bytes:
        """Pack payload base default method.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        :raises NotImplementedError: Master dispatcher must dispatch to sub-dataclass.
        """
        raise NotImplementedError("Use concrete variant sub-dataclass")


@dataclass(frozen=True, slots=True)
class ActuatorCycle6BPayload(ActuatorCyclePayload):
    """6-byte actuator cycle payload (Opcode 3EF1).

    Secondary 6-byte binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       h      2B   Cycle Countdown Sec (int16)  : 00 BF (191s)
      +2       h      2B   Actuator Countdown (int16)   : 00 BF (191s)
      +4       B      1B   Flags / Status               : 10
      +5       B      1B   Modulation Level uint8       : C8 (100%)
      --------------------------------------------------------------
      Field-spaced hex : 00BF 00BF 10 C8
      Payload hex      : 00BF00BF10C8
    """

    _STRUCT_FMT: ClassVar[str] = ">hhBB"

    cycle_countdown_sec: int | None
    actuator_countdown_sec: int | None
    modulation_level: float | None

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 6-byte actuator cycle binary payload."""
        if len(raw_data) < 6:
            raise ValueError(
                f"Invalid payload length for ActuatorCycle6BPayload: {len(raw_data)}"
            )
        c_raw, a_raw, _flag, mod_byte = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        c_down = None if c_raw == 0x7FFF else c_raw
        a_down = c_down if (a_raw < 0 or a_raw == 0x7FFF) else a_raw
        mod = hex_to_percent(f"{mod_byte:02X}")
        return cls(
            cycle_countdown_sec=c_down,
            actuator_countdown_sec=a_down,
            modulation_level=mod,
        )

    def to_bytes(self) -> bytes:
        """Pack 6-byte actuator cycle binary payload."""
        c_raw = 0x7FFF if self.cycle_countdown_sec is None else self.cycle_countdown_sec
        a_raw = (
            0x7FFF
            if self.actuator_countdown_sec is None
            else self.actuator_countdown_sec
        )
        if self.modulation_level is None:
            mod_raw = 0xFF
        else:
            mod_raw = min(200, max(0, int(round(self.modulation_level * 200.0))))
        return struct.pack(self._STRUCT_FMT, c_raw, a_raw, 0x10, mod_raw)


@dataclass(frozen=True, slots=True)
class ActuatorCycle7BPayload(ActuatorCyclePayload):
    """7-byte actuator cycle payload (Opcode 3EF1).

    Primary 7-byte Actuator Cycle binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Domain / Zone Index (uint8)  : 00
      +1       h      2B   Cycle Countdown Sec (int16)  : 00 BF (191s)
      +3       h      2B   Actuator Countdown (int16)   : 00 BF (191s)
      +5       B      1B   Modulation Level uint8       : C8 (100%)
      +6       B      1B   Flags / Padding Byte         : FF
      --------------------------------------------------------------
      Field-spaced hex : 00 00BF 00BF C8 FF
      Payload hex      : 0000BF00BFC8FF
    """

    _STRUCT_FMT: ClassVar[str] = ">BhhBB"

    domain_idx: int
    cycle_countdown_sec: int | None
    actuator_countdown_sec: int | None
    modulation_level: float | None

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 7-byte actuator cycle binary payload."""
        if len(raw_data) < 7:
            raise ValueError(
                f"Invalid payload length for ActuatorCycle7BPayload: {len(raw_data)}"
            )
        idx, c_raw, a_raw, mod_byte, _flag = struct.unpack_from(
            cls._STRUCT_FMT, raw_data, 0
        )
        c_down = None if c_raw == 0x7FFF else c_raw
        a_down = c_down if (a_raw < 0 or a_raw == 0x7FFF) else a_raw
        mod = hex_to_percent(f"{mod_byte:02X}")
        return cls(
            domain_idx=idx,
            cycle_countdown_sec=c_down,
            actuator_countdown_sec=a_down,
            modulation_level=mod,
        )

    def to_bytes(self) -> bytes:
        """Pack 7-byte actuator cycle binary payload."""
        c_raw = 0x7FFF if self.cycle_countdown_sec is None else self.cycle_countdown_sec
        a_raw = (
            0x7FFF
            if self.actuator_countdown_sec is None
            else self.actuator_countdown_sec
        )
        if self.modulation_level is None:
            mod_raw = 0xFF
        else:
            mod_raw = min(200, max(0, int(round(self.modulation_level * 200.0))))
        return struct.pack(
            self._STRUCT_FMT, self.domain_idx, c_raw, a_raw, mod_raw, 0xFF
        )


# Update VARIANTS property after variants are defined
ActuatorCyclePayload.VARIANTS = (
    ActuatorCycle6BPayload,
    ActuatorCycle7BPayload,
)
