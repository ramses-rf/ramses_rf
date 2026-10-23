"""RAMSES RF - Heating and Evohome payload dataclasses.

This module contains strongly-typed dataclass representations for CH / Evohome
packet payloads.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import datetime as dt
from typing import TYPE_CHECKING, ClassVar, Self, cast

from ramses_rf.const import (
    DEV_ROLE_MAP,
    SZ_ACCEPT,
    SZ_ACTUATOR_COUNTDOWN,
    SZ_ACTUATOR_SYNC,
    SZ_BINDINGS,
    SZ_CH_ACTIVE,
    SZ_CH_ENABLED,
    SZ_CH_SETPOINT,
    SZ_CONFIRM,
    SZ_COOLING_ACTIVE,
    SZ_CYCLE_COUNTDOWN,
    SZ_CYCLE_RATE,
    SZ_DEVICE_ROLE,
    SZ_DEVICES,
    SZ_DHW_ACTIVE,
    SZ_DOMAIN_INDEX,
    SZ_DURATION,
    SZ_FLAME_ACTIVE,
    SZ_FRAGMENT,
    SZ_FRAGMENT_NUMBER,
    SZ_HEAT_DEMAND,
    SZ_HEAT_DEMAND_FAULT,
    SZ_LOCAL_OVERRIDE,
    SZ_MAX_FLOW_SETPOINT,
    SZ_MAX_REL_MODULATION,
    SZ_MAX_TEMP,
    SZ_MIN_FLOW_SETPOINT,
    SZ_MIN_OFF_TIME,
    SZ_MIN_ON_TIME,
    SZ_MIN_TEMP,
    SZ_MODE,
    SZ_MODULATION_LEVEL,
    SZ_MULTIROOM_MODE,
    SZ_NAME,
    SZ_OFFER,
    SZ_OPENWINDOW_FUNCTION,
    SZ_PHASE,
    SZ_PRESSURE,
    SZ_PROPORTIONAL_BAND_WIDTH,
    SZ_PUMP_RELAY_STATE,
    SZ_PUMP_RUN_TIME,
    SZ_RELAY_DEMAND,
    SZ_SETPOINT,
    SZ_SYNC_FLAG,
    SZ_TEMPERATURE,
    SZ_TOTAL_FRAGMENTS,
    SZ_UFH_INDEX,
    SZ_UNTIL,
    SZ_VALVE_RUN_TIME,
    SZ_ZONE_CLASS,
    SZ_ZONE_INDEX,
    SZ_ZONE_MASK,
    SZ_ZONE_TYPE,
    ZON_MODE_MAP,
    ZON_ROLE_MAP,
    Code,
    Verb,
)
from ramses_rf.enums import DevType, PumpRelayState
from ramses_rf.typing import PayloadT
from ramses_tx.address import (
    ALL_DEV_ADDR,
    NON_DEV_ADDR,
    Address,
    hex_id_to_dev_id,
)
from ramses_tx.const import F9, FA, FC
from ramses_tx.helpers import hex_from_dtm, hex_to_dtm, hex_to_percent
from ramses_tx.typing import DeviceIdT

from .base import PayloadBase, parse_index
from .registry import register_payload

if TYPE_CHECKING:
    from ramses_rf.messages.base import Message

# ----------------------------------------------------------------------


@register_payload(Code._3150)
class HeatDemandPayload(PayloadBase):
    """Master payload dispatcher for heat demand (Opcode 3150)."""

    VARIANTS: ClassVar[tuple[type[PayloadBase], ...]] = ()

    domain_or_zone_index: int | None
    demand_percent: int
    raw_extra: bytes | None

    @classmethod
    def create(
        cls,
        domain_or_zone_index: int | None = None,
        demand_percent: int = 0,
        raw_extra: bytes | None = None,
        _is_array_item: bool = False,
    ) -> HeatDemand1BPayload | HeatDemand2BPayload:
        """Construct HeatDemand payload variant dynamically."""
        if domain_or_zone_index is not None:
            return HeatDemand2BPayload(
                domain_or_zone_index=domain_or_zone_index,
                demand_percent=demand_percent,
                raw_extra=raw_extra,
                _is_array_item=_is_array_item,
            )
        return HeatDemand1BPayload(demand_percent=demand_percent)

    @classmethod
    def from_bytes(
        cls, raw_data: bytes
    ) -> HeatDemandPayload | list[HeatDemandPayload]:
        """Unpack heat demand payload, dispatching by length."""
        if not raw_data:
            raise ValueError("Payload data cannot be empty")
        if len(raw_data) >= 4 and len(raw_data) % 2 == 0:
            return [
                HeatDemand2BPayload(
                    domain_or_zone_index=index,
                    demand_percent=demand,
                    _is_array_item=True,
                )
                for index, demand in (
                    struct.unpack_from(">BB", raw_data, i)
                    for i in range(0, len(raw_data), 2)
                )
            ]
        if len(raw_data) == 1:
            return HeatDemand1BPayload.from_bytes(raw_data)
        return HeatDemand2BPayload.from_bytes(raw_data)

    def to_bytes(self) -> bytes:
        """Pack payload base default method.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        :raises NotImplementedError: Master dispatcher must dispatch to
            variant sub-dataclass.
        """
        raise NotImplementedError("Use concrete variant sub-dataclass")


@dataclass(frozen=True, slots=True)
class HeatDemand1BPayload(HeatDemandPayload):
    """1-byte heat demand payload (Opcode 3150).

    1-byte Heat Demand binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Heat demand percentage (uint8) : C8 (200)
      --------------------------------------------------------------
      Field-spaced hex : C8
      Payload hex      : C8

    :param demand_percent: Heat demand value (0-200, where 200 = 100%).
    :type demand_percent: int
    """

    _STRUCT_FMT: ClassVar[str] = ">B"

    demand_percent: int
    domain_or_zone_index: int | None = None
    raw_extra: bytes | None = None

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 1-byte heat demand binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked HeatDemand1BPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 1 byte.
        """
        if len(raw_data) < 1:
            raise ValueError(
                f"Invalid payload length for HeatDemand1BPayload: {len(raw_data)}"
            )
        (demand,) = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(demand_percent=demand)

    def to_bytes(self) -> bytes:
        """Pack 1-byte heat demand binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        return struct.pack(self._STRUCT_FMT, self.demand_percent & 0xFF)

    def to_dict(self, msg: Message | None = None) -> PayloadT:
        """Convert heat demand payload to legacy dictionary layout.

        :param msg: Optional message context object.
        :type msg: Any
        :returns: Decoded heat demand dictionary.
        :rtype: PayloadT
        """
        if self.demand_percent == 0xF2:
            return {SZ_HEAT_DEMAND_FAULT: "unavailable"}
        value = self.demand_percent / 200.0
        if value == 1.01:
            value = 1.0
        return {SZ_HEAT_DEMAND: value}


@dataclass(frozen=True, slots=True)
class HeatDemand2BPayload(HeatDemandPayload):
    """2-byte heat demand payload (Opcode 3150).

    2-byte Heat Demand binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Zone Index / Domain (uint8)  : 00
      +1       B      1B   Heat demand percentage (uint8) : C8 (200)
      --------------------------------------------------------------
      Field-spaced hex : 00 C8
      Payload hex      : 00C8

    :param domain_or_zone_index: Domain or zone index byte.
    :type domain_or_zone_index: int
    :param demand_percent: Heat demand value (0-200, where 200 = 100%).
    :type demand_percent: int
    :param raw_extra: Optional raw extra bytes.
    :type raw_extra: bytes | None
    """

    _STRUCT_FMT: ClassVar[str] = ">BB"

    domain_or_zone_index: int
    demand_percent: int
    raw_extra: bytes | None = None
    _is_array_item: bool = False

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 2-byte heat demand binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked HeatDemand2BPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 2 bytes.
        """
        if len(raw_data) < 2:
            raise ValueError(
                f"Invalid payload length for HeatDemand2BPayload: {len(raw_data)}"
            )
        index, demand = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        extra = raw_data[2:] if len(raw_data) > 2 else None
        return cls(
            domain_or_zone_index=index, demand_percent=demand, raw_extra=extra
        )

    def to_bytes(self) -> bytes:
        """Pack 2-byte heat demand binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        buffer = struct.pack(
            self._STRUCT_FMT,
            self.domain_or_zone_index,
            self.demand_percent & 0xFF,
        )
        if self.raw_extra is not None:
            buffer += self.raw_extra
        return buffer

    def to_dict(self, msg: Message | None = None) -> PayloadT:
        """Convert heat demand payload to legacy dictionary layout.

        :param msg: Optional message context object.
        :type msg: Any
        :returns: Decoded heat demand dictionary.
        :rtype: PayloadT
        """
        if self.demand_percent == 0xF2:
            result: PayloadT = {SZ_HEAT_DEMAND_FAULT: "unavailable"}
        else:
            value = self.demand_percent / 200.0
            if value == 1.01:
                value = 1.0
            result = {SZ_HEAT_DEMAND: value}
        index = self.domain_or_zone_index
        if index >= 0xF0:
            result[SZ_DOMAIN_INDEX] = FC if index == 0xFC else f"{index:02X}"
        else:
            is_ufc_circuit = False
            if msg is not None and getattr(msg, "src", None) is not None:
                src_str = str(getattr(msg.src, "id", msg.src))
                is_src_ufc = src_str.startswith("02:") or getattr(
                    msg.src, "type", ""
                ) in (
                    "02",
                    DevType.UFC,
                )
                if is_src_ufc:
                    dst = getattr(msg, "dst", None)
                    dst_str = (
                        str(getattr(dst, "id", dst)) if dst is not None else ""
                    )
                    dst_is_ctl = dst_str.startswith("01:") or (
                        dst is not None
                        and getattr(dst, "type", "") in ("01", DevType.CTL)
                    )
                    if not dst_is_ctl:
                        is_ufc_circuit = True
            index_name = SZ_UFH_INDEX if is_ufc_circuit else SZ_ZONE_INDEX
            result[index_name] = f"{index:02X}"
        return result


# Update VARIANTS property after variants are defined
HeatDemandPayload.VARIANTS = (
    HeatDemand1BPayload,
    HeatDemand2BPayload,
)


# ----------------------------------------------------------------------


@register_payload(Code._30C9)
class TemperaturePayload(PayloadBase):
    """Master payload dispatcher for temperature (Opcode 30C9).

    Dispatches temperature binary payloads to 2-byte or 3-byte
    variant sub-dataclasses based on payload length.
    """

    VARIANTS: ClassVar[tuple[type[PayloadBase], ...]] = ()

    zone_index: int | str | None
    temperature: float | bool | None

    @classmethod
    def create(
        cls,
        zone_index: int | str | None = None,
        temperature: float | bool | None = None,
    ) -> Temperature2BPayload | Temperature3BPayload:
        """Construct Temperature payload variant dynamically from arguments."""
        if zone_index is not None:
            return Temperature3BPayload(
                zone_index=zone_index, temperature=temperature
            )
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
    ) -> TemperaturePayload | list[TemperaturePayload]:
        """Unpack binary payload, dispatching by length."""
        if len(raw_data) > 3 and len(raw_data) % 3 == 0:
            return [
                Temperature3BPayload(
                    zone_index=index,
                    temperature=cls._parse_temp_val(temp_raw),
                )
                for index, temp_raw in (
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

    zone_index: None = None
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

    def to_dict(self) -> PayloadT:
        """Convert 2-byte temperature payload to legacy dictionary layout."""
        return {SZ_TEMPERATURE: self.temperature}


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

    :param zone_index: Zone index byte.
    :type zone_index: int | str
    :param temperature: Temperature in °C, False if disabled, or None if N/A.
    :type temperature: float | bool | None
    """

    _STRUCT_FMT: ClassVar[str] = ">Bh"

    zone_index: int | str
    temperature: float | bool | None

    def __post_init__(self) -> None:
        """Normalise index arguments."""
        if isinstance(self.zone_index, str):
            object.__setattr__(
                self, SZ_ZONE_INDEX, parse_index(self.zone_index)
            )

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 3-byte temperature payload."""
        if len(raw_data) < 3:
            raise ValueError(
                f"Invalid payload length for Temperature3BPayload: {len(raw_data)}"
            )
        index, temp_raw = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(zone_index=index, temperature=cls._parse_temp_val(temp_raw))

    def to_bytes(self) -> bytes:
        """Pack 3-byte temperature payload."""
        if self.temperature is None:
            temp_raw = 0x7FFF
        elif self.temperature is False:
            temp_raw = 0x7EFF
        else:
            temp_raw = int(round(self.temperature * 100.0))
        index = parse_index(self.zone_index)
        return struct.pack(self._STRUCT_FMT, index, temp_raw)

    def to_dict(self) -> PayloadT:
        """Convert 3-byte temperature payload to legacy dictionary layout."""
        index_str = (
            f"{self.zone_index:02X}"
            if isinstance(self.zone_index, int)
            else str(self.zone_index)
        )
        return {SZ_ZONE_INDEX: index_str, SZ_TEMPERATURE: self.temperature}


# Update VARIANTS property after variants are defined
TemperaturePayload.VARIANTS = (
    Temperature2BPayload,
    Temperature3BPayload,
)


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

    :param zone_index: Zone/domain index byte.
    :type zone_index: int
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

    zone_index: int | str
    frag_number: int
    total_frags: int
    fragment_bytes: bytes
    _header_prefix: bytes | None = None

    def __post_init__(self) -> None:
        """Normalise index arguments."""
        if isinstance(self.zone_index, str):
            object.__setattr__(
                self, SZ_ZONE_INDEX, parse_index(self.zone_index)
            )

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
        raw_index, prefix, _frag_len, frag_num, total_frags = (
            struct.unpack_from(cls._STRUCT_FMT_HEADER, raw_data, 0)
        )
        zone_index: int | str = (
            "HW" if raw_index == 0 and prefix == b"\x23\x00\x08" else raw_index
        )
        return cls(
            zone_index=zone_index,
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
        index = parse_index(self.zone_index)
        prefix = (
            self._header_prefix
            if self._header_prefix is not None
            else (b"\x23\x00\x08" if index == 0xFA else b"\x20\x00\x08")
        )
        byte_index = (
            0x00 if prefix == b"\x23\x00\x08" and index == 0xFA else index
        )
        hdr = struct.pack(
            self._STRUCT_FMT_HEADER,
            byte_index,
            prefix,
            len(self.fragment_bytes),
            self.frag_number,
            self.total_frags,
        )
        return hdr + self.fragment_bytes

    def to_dict(self, msg: Message | None = None) -> PayloadT:
        """Convert schedule fragment payload to legacy dictionary layout.

        :param msg: Optional message context object.
        :type msg: Any
        :returns: Decoded schedule fragment dictionary.
        :rtype: PayloadT
        """
        if self.zone_index in (0xFA, "HW"):
            zone_str = "HW"
        elif isinstance(self.zone_index, int):
            zone_str = f"{self.zone_index:02X}"
        else:
            zone_str = str(self.zone_index)
        result: PayloadT = {
            SZ_ZONE_INDEX: zone_str,
            SZ_FRAGMENT_NUMBER: self.frag_number,
            SZ_TOTAL_FRAGMENTS: self.total_frags
            if self.total_frags != 0
            else None,
        }
        if self.fragment_bytes:
            result[SZ_FRAGMENT] = self.fragment_bytes.hex().upper()
        return result


# ----------------------------------------------------------------------


@register_payload(Code._0404)
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

    :param zone_index: Zone/domain index byte.
    :type zone_index: int
    :param day_of_week: Day of week integer (1-7).
    :type day_of_week: int
    :param time_of_day_mins: Time of day in minutes.
    :type time_of_day_mins: int
    :param setpoint_value: Setpoint value or state raw uint16.
    :type setpoint_value: int
    """

    _STRUCT_FMT: ClassVar[str] = "<xxxxBxxxBxxxHxxHH"

    zone_index: int
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
            raise ValueError(
                f"Invalid payload length for 0404: {len(raw_data)}"
            )

        index, dow, tod, setpoint_raw, _ = struct.unpack(
            cls._STRUCT_FMT, raw_data
        )
        return cls(
            zone_index=index,
            day_of_week=dow,
            time_of_day_mins=tod,
            setpoint_value=setpoint_raw,
        )

    @classmethod
    def from_switchpoint(
        cls,
        zone_index: int | str,
        day_of_week: int,
        time_of_day_mins: int,
        setpoint: float | bool | None,
    ) -> Self:
        """Create a ScheduleSwitchpointPayload from switchpoint domain values.

        :param zone_index: Zone or domain index byte or string.
        :type zone_index: int | str
        :param day_of_week: Day of week integer (0-6).
        :type day_of_week: int
        :param time_of_day_mins: Time of day in minutes.
        :type time_of_day_mins: int
        :param setpoint: Temperature setpoint float, boolean state, or None.
        :type setpoint: float | bool | None
        :returns: A populated ScheduleSwitchpointPayload instance.
        :rtype: Self
        """
        index = parse_index(zone_index)
        if isinstance(setpoint, bool):
            value = int(setpoint)
        elif isinstance(setpoint, (int, float)):
            value = int(setpoint * 100)
        else:
            value = 0
        return cls(
            zone_index=index,
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
            self.zone_index,
            self.day_of_week,
            self.time_of_day_mins,
            self.setpoint_value,
            0,  # Reserved / trailer 2-byte field (0x0000)
        )


# ----------------------------------------------------------------------


@register_payload(Code._1030)
class SystemSyncPayload(PayloadBase):
    """Master payload dispatcher and base class for Opcode 1030."""

    VARIANTS: ClassVar[tuple[type[PayloadBase], ...]] = ()

    sync_flag: int
    max_flow_setpoint: int | None
    min_flow_setpoint: int | None
    valve_run_time: int | None
    pump_run_time: int | None

    @classmethod
    def create(
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
    ) -> SystemSync1BPayload | SystemSyncVarPayload:
        """Construct SystemSync payload variant dynamically from arguments."""
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
    def from_bytes(cls, raw_data: bytes) -> SystemSyncPayload:
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

    def to_dict(self) -> PayloadT:
        """Convert 1-byte system sync payload to legacy dictionary layout."""
        return {
            SZ_SYNC_FLAG: self.sync_flag,
            SZ_MAX_FLOW_SETPOINT: None,
            SZ_MIN_FLOW_SETPOINT: None,
            SZ_VALVE_RUN_TIME: None,
            SZ_PUMP_RUN_TIME: None,
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
                param_id, _sub, param_value = struct.unpack_from(
                    cls._STRUCT_FMT_PARAM, raw_data, i
                )
                if param_id == 0xC8:
                    max_flow = param_value
                elif param_id == 0xC9:
                    min_flow = param_value
                elif param_id == 0xCA:
                    v_time = param_value
                elif param_id == 0xCB:
                    p_time = param_value
                elif param_id == 0xCC:
                    b_cc = param_value
                elif param_id == 0x20:
                    u20 = param_value
                elif param_id == 0x21:
                    u21 = param_value
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
        result = struct.pack(self._STRUCT_FMT_BYTE, self.sync_flag)
        if self.max_flow_setpoint is not None:
            result += struct.pack(
                self._STRUCT_FMT_PARAM, 0xC8, 1, self.max_flow_setpoint
            )
        if self.min_flow_setpoint is not None:
            result += struct.pack(
                self._STRUCT_FMT_PARAM, 0xC9, 1, self.min_flow_setpoint
            )
        if self.valve_run_time is not None:
            result += struct.pack(
                self._STRUCT_FMT_PARAM, 0xCA, 1, self.valve_run_time
            )
        if self.pump_run_time is not None:
            result += struct.pack(
                self._STRUCT_FMT_PARAM, 0xCB, 1, self.pump_run_time
            )
        if self._boolean_cc is not None:
            result += struct.pack(
                self._STRUCT_FMT_PARAM, 0xCC, 1, self._boolean_cc
            )
        if self._unknown_20 is not None:
            result += struct.pack(
                self._STRUCT_FMT_PARAM, 0x20, 1, self._unknown_20
            )
        if self._unknown_21 is not None:
            result += struct.pack(
                self._STRUCT_FMT_PARAM, 0x21, 1, self._unknown_21
            )
        if self._raw_extra and len(result) == 1:
            result += self._raw_extra
        return result

    def to_dict(self) -> PayloadT:
        """Convert multi-parameter system sync payload to dictionary."""
        result: PayloadT = {}
        if self._unknown_20 is not None or self._unknown_21 is not None:
            if self._unknown_20 is not None:
                result["unknown_20"] = self._unknown_20
            if self._unknown_21 is not None:
                result["unknown_21"] = self._unknown_21
            return result

        result[SZ_ZONE_INDEX] = f"{self.sync_flag:02X}"
        if self.max_flow_setpoint is not None:
            result[SZ_MAX_FLOW_SETPOINT] = self.max_flow_setpoint
        if self.min_flow_setpoint is not None:
            result[SZ_MIN_FLOW_SETPOINT] = self.min_flow_setpoint
        if self.valve_run_time is not None:
            result[SZ_VALVE_RUN_TIME] = self.valve_run_time
        if self.pump_run_time is not None:
            result[SZ_PUMP_RUN_TIME] = self.pump_run_time
        if self._boolean_cc is not None:
            result["boolean_cc"] = self._boolean_cc
        return result


# Update VARIANTS property after variants are defined
SystemSyncPayload.VARIANTS = (
    SystemSync1BPayload,
    SystemSyncVarPayload,
)


# ----------------------------------------------------------------------


@register_payload(Code._1FC9)
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
        return (
            struct.pack(self._STRUCT_FMT_HDR, self.binding_type)
            + self.binding_data
        )

    def to_dict(self, msg: Message | None = None) -> PayloadT:
        """Convert 1FC9 binding payload to legacy dictionary representation.

        :param msg: Optional Message context for legacy compatibility.
        :type msg: Any
        :returns: Dictionary representation of binding payload.
        :rtype: PayloadT
        """
        payload_hex = (
            (bytes([self.binding_type]) + self.binding_data).hex().upper()
        )
        result: PayloadT = {}

        if msg is not None:
            verb = getattr(msg, "verb", Verb.I_)
            src_id = str(
                getattr(getattr(msg, "src", ""), "id", getattr(msg, "src", ""))
            )
            dst_id = str(
                getattr(getattr(msg, "dst", ""), "id", getattr(msg, "dst", ""))
            )
            v_str = (
                str(getattr(verb, "value", str(verb))).split(".")[-1].strip()
            )

            if v_str in ("I", "I_") and (
                dst_id
                in (
                    src_id,
                    "63:262142",
                    ALL_DEV_ADDR.id,
                    NON_DEV_ADDR.id,
                    "--:------",
                )
            ):
                bind_phase = SZ_OFFER
            elif v_str in ("W", "W_") and src_id != dst_id:
                bind_phase = SZ_ACCEPT
            elif v_str in ("I", "I_"):
                bind_phase = SZ_CONFIRM
            else:
                bind_phase = None

            if bind_phase is not None:
                result[SZ_PHASE] = bind_phase

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
        result[SZ_BINDINGS] = bindings

        return result

    @property
    def index(self) -> str:
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


@register_payload(Code._000A)
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
      # 000A (Zone Info) is sent by THMs (22:) with their zone_index as payload (e.g. RQ 000A 001 01).
      # CTL (01:) sends 000A with full zone configuration arrays.
      # Sample Packet Logs:
      # .I --- 01:158182 --:------ 01:158182 000A 048 001201F409C4011101F409C40...
      # .I --- 01:158182 --:------ 01:158182 000A 006 081001F409C4

    :param zone_index: Zone index integer.
    :type zone_index: int
    :param zone_flags: Zone flags byte.
    :type zone_flags: int
    :param min_temp: Minimum zone temperature setting in °C.
    :type min_temp: float
    :param max_temp: Maximum zone temperature setting in °C.
    :type max_temp: float
    """

    _STRUCT_FMT: ClassVar[str] = ">BBhh"

    zone_index: int | str
    zone_flags: int
    min_temp: float | None
    max_temp: float | None

    def __post_init__(self) -> None:
        """Normalise index arguments."""
        if isinstance(self.zone_index, str):
            object.__setattr__(
                self, SZ_ZONE_INDEX, parse_index(self.zone_index)
            )

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
        # Unpack index, flags, min_temp, max_temp directly from offset
        index, flags, min_raw, max_raw = struct.unpack_from(
            cls._STRUCT_FMT, raw_data, offset
        )
        return cls(
            zone_index=index,
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
            return cls(
                zone_index=raw_data[0],
                zone_flags=0,
                min_temp=None,
                max_temp=None,
            )
        if len(raw_data) == 2:
            return cls(
                zone_index=raw_data[0],
                zone_flags=raw_data[1],
                min_temp=None,
                max_temp=None,
            )
        if len(raw_data) < 6 or len(raw_data) % 6 != 0:
            raise ValueError(
                f"Invalid payload length for 000A: {len(raw_data)}"
            )
        if len(raw_data) > 6:
            return [
                cls._from_bytes_single(raw_data, i)
                for i in range(0, len(raw_data), 6)
            ]

        return cls._from_bytes_single(raw_data, 0)

    def to_bytes(self) -> bytes:
        """Pack zone config data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        index = parse_index(self.zone_index)
        min_raw = (
            0x7FFF
            if self.min_temp is None
            else int(round(self.min_temp * 100.0))
        )
        max_raw = (
            0x7FFF
            if self.max_temp is None
            else int(round(self.max_temp * 100.0))
        )
        return struct.pack(
            self._STRUCT_FMT,
            index,
            self.zone_flags,
            min_raw,
            max_raw,
        )

    def to_dict(self) -> PayloadT:
        """Convert zone config payload to legacy dictionary layout.

        :returns: Decoded zone config dictionary.
        :rtype: PayloadT
        """
        bitmap = self.zone_flags
        # zone_index is included so that _resolve_logical_targets in
        # state_projector.py can route the payload to the correct zone.
        # Without it, 000A packets are never ingested (issue 1102).
        index = self.zone_index
        if isinstance(index, int):
            index = f"{index:02X}"
        return {
            SZ_ZONE_INDEX: index,
            SZ_MIN_TEMP: self.min_temp,
            SZ_MAX_TEMP: self.max_temp,
            SZ_LOCAL_OVERRIDE: not bool(bitmap & 1),
            SZ_OPENWINDOW_FUNCTION: not bool(bitmap & 2),
            SZ_MULTIROOM_MODE: not bool(bitmap & 16),
        }


@register_payload(Code._0004)
class ZoneNamePayload(PayloadBase):
    """Master payload dispatcher for zone name (Opcode 0004)."""

    VARIANTS: ClassVar[tuple[type[PayloadBase], ...]] = ()

    zone_index: int | str
    name: str | None
    setpoint_temp: float | None

    @classmethod
    def create(
        cls,
        zone_index: int | str = 0,
        name: str | None = None,
        setpoint_temp: float | None = None,
    ) -> ZoneName22BPayload | ZoneNameShort3BPayload:
        """Construct ZoneName payload variant dynamically."""
        if setpoint_temp is not None:
            return ZoneNameShort3BPayload(
                zone_index=zone_index, setpoint_temp=setpoint_temp
            )
        return ZoneName22BPayload(zone_index=zone_index, name=name)

    @classmethod
    def from_bytes(
        cls, raw_data: bytes
    ) -> ZoneName22BPayload | ZoneNameShort3BPayload:
        """Unpack zone name payload, dispatching by length."""
        if len(raw_data) >= 22:
            return ZoneName22BPayload.from_bytes(raw_data)
        if len(raw_data) >= 3:
            return ZoneNameShort3BPayload.from_bytes(raw_data)
        raise ValueError(f"Invalid payload length for 0004: {len(raw_data)}")

    def to_bytes(self) -> bytes:
        """Pack payload base default method.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        :raises NotImplementedError: Master dispatcher must dispatch to
            variant sub-dataclass.
        """
        raise NotImplementedError("Use concrete variant sub-dataclass")


@dataclass(frozen=True, slots=True)
class ZoneName22BPayload(ZoneNamePayload):
    """22-byte zone name payload (Opcode 0004).

    22-byte Zone Name binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Zone Index (uint8)           : 00
      +1       B      1B   Flag Byte (uint8)            : 00
      +2       20s    20B  ASCII Zone Name (20B null-pad): 4C 6F 75 6E 67 65 00... ("Lounge")
      --------------------------------------------------------------
      Field-spaced hex : 00 00 4C6F756E67650000000000000000000000000000
      Payload hex      : 00004C6F756E67650000000000000000000000000000

    Protocol Notes:
      # RQ payload is zz00; limited to 12 chars in evohome UI? if "7F"*20: not a zone

    :param zone_index: Zone index byte.
    :type zone_index: int | str
    :param name: ASCII Zone Name string (max 20 chars).
    :type name: str | None
    """

    _STRUCT_FMT: ClassVar[str] = ">Bx20s"

    zone_index: int | str
    name: str | None = None

    def __post_init__(self) -> None:
        """Normalise index arguments."""
        if isinstance(self.zone_index, str):
            object.__setattr__(
                self, SZ_ZONE_INDEX, parse_index(self.zone_index)
            )

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 22-byte zone name binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked ZoneName22BPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 22 bytes.
        """
        if len(raw_data) < 22:
            raise ValueError(
                f"Invalid payload length for ZoneName22BPayload: {len(raw_data)}"
            )
        index, name_raw = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        if name_raw == b"\x7f" * 20:
            name = None
        else:
            name = name_raw.rstrip(b"\x00").decode("ascii", errors="replace")
        return cls(zone_index=index, name=name)

    def to_bytes(self) -> bytes:
        """Pack 22-byte zone name data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        index = parse_index(self.zone_index)
        name_bytes = (
            b"\x7f" * 20
            if self.name is None
            else self.name.encode("ascii", errors="replace")[:20].ljust(
                20, b"\x00"
            )
        )
        return struct.pack(self._STRUCT_FMT, index, name_bytes)

    def to_dict(self) -> PayloadT:
        """Convert zone name payload to legacy dictionary layout.

        :returns: Decoded zone name dictionary.
        :rtype: PayloadT
        """
        if self.name is None:
            return {}
        index_str = (
            f"{self.zone_index:02X}"
            if isinstance(self.zone_index, int)
            else self.zone_index
        )
        return {SZ_ZONE_INDEX: index_str, SZ_NAME: self.name}


@dataclass(frozen=True, slots=True)
class ZoneNameShort3BPayload(ZoneNamePayload):
    """3-byte zone name / setpoint short variant (Opcode 0004).

    3-byte Zone Setpoint binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Zone Index (uint8)           : 01
      +1       h      2B   Target Setpoint (int16*100)  : 07 D0 (20.00°C)
      --------------------------------------------------------------
      Field-spaced hex : 01 07D0
      Payload hex      : 0107D0

    :param zone_index: Zone index byte.
    :type zone_index: int | str
    :param setpoint_temp: Target temperature in °C.
    :type setpoint_temp: float
    """

    _STRUCT_FMT: ClassVar[str] = ">Bh"

    zone_index: int | str
    setpoint_temp: float

    def __post_init__(self) -> None:
        """Normalise index arguments."""
        if isinstance(self.zone_index, str):
            object.__setattr__(
                self, SZ_ZONE_INDEX, parse_index(self.zone_index)
            )

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 3-byte zone setpoint binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked ZoneNameShort3BPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 3 bytes.
        """
        if len(raw_data) < 3:
            raise ValueError(
                f"Invalid payload length for ZoneNameShort3BPayload: {len(raw_data)}"
            )
        index, sp_raw = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(zone_index=index, setpoint_temp=sp_raw / 100.0)

    def to_bytes(self) -> bytes:
        """Pack 3-byte zone setpoint data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        sp_raw = int(round(self.setpoint_temp * 100.0))
        index = parse_index(self.zone_index)
        return bytes([index]) + sp_raw.to_bytes(
            2, byteorder="big", signed=True
        )

    def to_dict(self) -> PayloadT:
        """Convert zone setpoint payload to legacy dictionary layout.

        :returns: Decoded zone setpoint dictionary.
        :rtype: PayloadT
        """
        index_str = (
            f"{self.zone_index:02X}"
            if isinstance(self.zone_index, int)
            else self.zone_index
        )
        return {SZ_ZONE_INDEX: index_str, SZ_SETPOINT: self.setpoint_temp}


# Update VARIANTS property after variants are defined
ZoneNamePayload.VARIANTS = (
    ZoneName22BPayload,
    ZoneNameShort3BPayload,
)


# ----------------------------------------------------------------------


@register_payload(Code._12C0)
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
            raise ValueError(
                f"Invalid payload length for 12C0: {len(raw_data)}"
            )
        if len(raw_data) >= 3 and raw_data[0] == 0:
            _hdr, raw_temp, unit_byte = struct.unpack_from(
                cls._STRUCT_FMT_LEGACY, raw_data, 0
            )
            if raw_temp == 0x80:
                temp = None
            elif unit_byte == 1:
                temp = raw_temp / 2.0
            else:
                temp = round((raw_temp - 32) * 5.0 / 9.0, 2)
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

    def to_dict(self, msg: Message | None = None) -> PayloadT:
        """Convert 12C0 payload to legacy dictionary format.

        :param msg: Optional message context object.
        :type msg: Any
        :returns: Decoded temperature dictionary.
        :rtype: PayloadT
        """
        result: PayloadT = {SZ_TEMPERATURE: self.temperature}
        if self._units is not None:
            result["units"] = {
                "00": "Celsius",
                "01": "Celsius",
                "02": "Fahrenheit",
            }.get(self._units, self._units)
        return result


# ----------------------------------------------------------------------


@register_payload(Code._2309)
class ZoneSetpointPayload(PayloadBase):
    """Master payload dispatcher for zone setpoint (Opcode 2309)."""

    VARIANTS: ClassVar[tuple[type[PayloadBase], ...]] = ()

    zone_index: int | str
    setpoint_temp: float | bool | None

    @classmethod
    def create(
        cls,
        zone_index: int | str = 0,
        setpoint_temp: float | bool | None = None,
    ) -> ZoneSetpoint3BPayload:
        """Construct ZoneSetpoint payload variant dynamically."""
        return ZoneSetpoint3BPayload(
            zone_index=zone_index, setpoint_temp=setpoint_temp
        )

    @classmethod
    def from_bytes(
        cls, raw_data: bytes
    ) -> ZoneSetpointPayload | list[ZoneSetpointPayload]:
        """Unpack zone setpoint payload (single or array entries)."""
        if len(raw_data) < 3:
            raise ValueError(
                f"Invalid payload length for 2309: {len(raw_data)}"
            )
        if len(raw_data) > 3 and len(raw_data) % 3 == 0:
            return [
                ZoneSetpoint3BPayload.from_bytes(raw_data[i : i + 3])
                for i in range(0, len(raw_data), 3)
            ]
        return ZoneSetpoint3BPayload.from_bytes(raw_data)

    def to_bytes(self) -> bytes:
        """Pack payload base default method.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        :raises NotImplementedError: Master dispatcher must dispatch to
            variant sub-dataclass.
        """
        raise NotImplementedError("Use concrete variant sub-dataclass")


@dataclass(frozen=True, slots=True)
class ZoneSetpoint3BPayload(ZoneSetpointPayload):
    """3-byte zone setpoint layout (Opcode 2309).

    3-byte Set-point Info binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Zone Index (uint8)           : 00
      +1       h      2B   Setpoint Temp (int16*100)    : 08 34 (21.00°C)
      --------------------------------------------------------------
      Field-spaced hex : 00 0834
      Payload hex      : 000834

    Protocol Notes:
      # RQ --- 12:010740 01:145038 --:------ 2309 003 03073A
      # RQ --- 22:131874 01:063844 --:------ 2309 003 020708
      # NOTE: 12 uses: r"^0[0-9A-F]$"

    :param zone_index: Zone index byte.
    :type zone_index: int | str
    :param setpoint_temp: Setpoint temperature in °C.
    :type setpoint_temp: float | bool | None
    """

    _STRUCT_FMT: ClassVar[str] = ">Bh"

    zone_index: int | str
    setpoint_temp: float | bool | None

    def __post_init__(self) -> None:
        """Normalise index arguments."""
        if isinstance(self.zone_index, str):
            object.__setattr__(
                self, SZ_ZONE_INDEX, parse_index(self.zone_index)
            )

    @classmethod
    def _parse_sp_val(cls, sp_raw: int) -> float | bool | None:
        """Decode raw 16-bit signed integer to setpoint temperature value."""
        if sp_raw in (0x31FF, 0x7FFF):
            return None
        if sp_raw == 0x7EFF:
            return False
        return sp_raw / 100.0

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 3-byte setpoint info binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked ZoneSetpoint3BPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 3 bytes.
        """
        if len(raw_data) < 3:
            raise ValueError(
                f"Invalid payload length for ZoneSetpoint3BPayload: {len(raw_data)}"
            )
        index, sp_raw = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(zone_index=index, setpoint_temp=cls._parse_sp_val(sp_raw))

    def to_bytes(self) -> bytes:
        """Pack 3-byte setpoint info data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        if self.setpoint_temp is None:
            sp_raw = 0x7FFF
        elif self.setpoint_temp is False:
            sp_raw = 0x7EFF
        else:
            sp_raw = int(round(self.setpoint_temp * 100.0))
        index = parse_index(self.zone_index)
        return struct.pack(self._STRUCT_FMT, index, sp_raw)

    def to_dict(self) -> PayloadT:
        """Convert setpoint info payload to legacy dictionary layout.

        :returns: Decoded setpoint info dictionary.
        :rtype: PayloadT
        """
        index_str = (
            f"{self.zone_index:02X}"
            if isinstance(self.zone_index, int)
            else self.zone_index
        )
        return {
            SZ_ZONE_INDEX: index_str,
            SZ_SETPOINT: self.setpoint_temp,
        }


# Update VARIANTS property after variants are defined
ZoneSetpointPayload.VARIANTS = (ZoneSetpoint3BPayload,)

# Alias for backward compatibility
SetPointInfoPayload = ZoneSetpointPayload


# ----------------------------------------------------------------------


@register_payload(Code._3200)
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

    :param domain_index: Domain index byte.
    :type domain_index: int
    :param temperature: Flow temperature in °C, or None if invalid.
    :type temperature: float | None
    """

    _STRUCT_FMT: ClassVar[str] = ">Bh"

    domain_index: int
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
            raise ValueError(
                f"Invalid payload length for 3200: {len(raw_data)}"
            )
        index, temp_raw = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        temp_val = None if temp_raw in (0x31FF, 0x7FFF) else temp_raw / 100.0
        return cls(domain_index=index, temperature=temp_val)

    def to_bytes(self) -> bytes:
        """Pack flow temperature data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        temp_raw = (
            0x7FFF
            if self.temperature is None
            else int(round(self.temperature * 100.0))
        )
        return struct.pack(self._STRUCT_FMT, self.domain_index, temp_raw)

    def to_dict(self) -> PayloadT:
        """Convert flow temperature payload to legacy dictionary layout.

        :returns: Decoded flow temperature dictionary.
        :rtype: PayloadT
        """
        return {SZ_TEMPERATURE: self.temperature}


# ----------------------------------------------------------------------


@register_payload(Code._0005)
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

    @classmethod
    def create(
        cls,
        zone_type: int = 0,
        zone_mask: int = 0,
        zone_class_id: int = 0,
    ) -> SystemZones4BPayload:
        """Construct SystemZones payload variant dynamically from arguments."""
        return SystemZones4BPayload(
            zone_type=zone_type,
            zone_mask=zone_mask,
            zone_class_id=zone_class_id if zone_class_id != 0 else zone_type,
        )

    def to_dict(  # type: ignore[override]
        self, msg: Message | None = None
    ) -> PayloadT | list[PayloadT]:
        """Convert system zones payload to legacy dictionary layout."""
        z_type = getattr(self, SZ_ZONE_TYPE, 0)
        z_mask = getattr(self, SZ_ZONE_MASK, 0)

        type_str = f"{z_type:02X}"
        zone_class = ZON_ROLE_MAP.get(
            type_str, DEV_ROLE_MAP.get(type_str, "heating_zone")
        )

        bits = [(z_mask >> i) & 1 for i in range(16)]

        return {
            SZ_ZONE_TYPE: type_str,
            SZ_ZONE_MASK: bits,
            SZ_ZONE_CLASS: zone_class,
        }

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> PayloadBase | list[PayloadBase]:
        """Unpack system zones binary payload, dispatching by length."""
        if len(raw_data) > 4 and len(raw_data) % 4 == 0:
            result: list[PayloadBase] = []
            for i in range(0, len(raw_data), 4):
                result.append(
                    SystemZones4BPayload.from_bytes(raw_data[i : i + 4])
                )
            return result
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
        return struct.pack(
            self._STRUCT_FMT, 0x00, self.zone_type, self.zone_mask
        )


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
        return struct.pack(
            self._STRUCT_FMT_HDR, 0x00, self.zone_type
        ) + struct.pack("<H", self.zone_mask)


# Update VARIANTS property after variants are defined
SystemZonesPayload.VARIANTS = (
    SystemZones3BPayload,
    SystemZones4BPayload,
)


# ----------------------------------------------------------------------


@register_payload(Code._0008)
class RelayDemandPayload(PayloadBase):
    """Master payload dispatcher for relay demand (Opcode 0008)."""

    VARIANTS: ClassVar[tuple[type[PayloadBase], ...]] = ()

    domain_or_zone_index: int
    demand_percent: float
    raw_extra: bytes | None

    @classmethod
    def create(
        cls,
        domain_or_zone_index: int = 0,
        demand_percent: float = 0.0,
        raw_extra: bytes | None = None,
    ) -> RelayDemand2BPayload:
        """Construct RelayDemand payload variant dynamically."""
        return RelayDemand2BPayload(
            domain_or_zone_index=domain_or_zone_index,
            demand_percent=demand_percent,
            raw_extra=raw_extra,
        )

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> RelayDemand2BPayload:
        """Unpack relay demand payload, dispatching to variant."""
        return RelayDemand2BPayload.from_bytes(raw_data)

    def to_bytes(self) -> bytes:
        """Pack payload base default method.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        :raises NotImplementedError: Master dispatcher must dispatch to
            variant sub-dataclass.
        """
        raise NotImplementedError("Use concrete variant sub-dataclass")


@dataclass(frozen=True, slots=True)
class RelayDemand2BPayload(RelayDemandPayload):
    """2-byte relay demand payload (Opcode 0008).

    2-byte Relay Demand binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Domain / Zone Index (uint8)  : 00
      +1       B      1B   Relay Demand uint8 (0-200)   : 64 (50%)
      --------------------------------------------------------------
      Field-spaced hex : 00 64
      Payload hex      : 0064

    Protocol Notes:
      # RP --- 13:109598 18:199952 --:------ 0008 002 0000
      # RP --- 13:109598 18:199952 --:------ 0008 002 00C8

    :param domain_or_zone_index: Domain or zone index byte.
    :type domain_or_zone_index: int
    :param demand_percent: Heat demand percentage (0.0 - 100.0).
    :type demand_percent: float
    :param raw_extra: Optional trailing payload bytes for 13-byte Jasper payloads.
    :type raw_extra: bytes | None
    """

    _STRUCT_FMT: ClassVar[str] = ">BB"

    domain_or_zone_index: int
    demand_percent: float
    raw_extra: bytes | None = None

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 2-byte relay demand binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked RelayDemand2BPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 2 bytes.
        """
        if len(raw_data) < 2:
            raise ValueError(
                f"Invalid payload length for RelayDemand2BPayload: {len(raw_data)}"
            )
        index, demand_raw = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        extra = raw_data[2:] if len(raw_data) > 2 else None
        return cls(
            domain_or_zone_index=index,
            demand_percent=demand_raw / 200.0,
            raw_extra=extra,
        )

    def to_bytes(self) -> bytes:
        """Pack 2-byte relay demand binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        demand_raw = min(200, max(0, int(round(self.demand_percent * 200.0))))
        result = struct.pack(
            self._STRUCT_FMT, self.domain_or_zone_index, demand_raw
        )
        if self.raw_extra is not None:
            result += self.raw_extra
        return result

    def to_dict(self) -> PayloadT:
        """Convert relay demand payload to legacy dictionary layout.

        :returns: Decoded relay demand dictionary.
        :rtype: PayloadT
        """
        # domain_index is included so that _update_demand_state can
        # populate the TCS's per-domain _relay_demands dict (issue 1102).
        idx = self.domain_or_zone_index
        domain = FC if idx == 0xFC else f"{idx:02X}"
        return {
            SZ_DOMAIN_INDEX: domain,
            SZ_RELAY_DEMAND: self.demand_percent,
        }


# Update VARIANTS property after variants are defined
RelayDemandPayload.VARIANTS = (RelayDemand2BPayload,)


# ----------------------------------------------------------------------


@register_payload(Code._000C)
class ZoneDevicesPayload(PayloadBase):
    """Master payload dispatcher and base class for Opcode 000C.

    Protocol & Heuristic Notes:
      # NOTE: Both len=5 and len=6 elements are valid! So collision when len = 036!
      # Note: 000C sent to a UFC device represents ufh_index rather than zone_index
      # If sent to/from a UFC device, byte 0 is ufh_index and sub_index is zone_index
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

    @classmethod
    def create(
        cls,
        zone_index_raw: int = 0,
        device_role_id: int = 0,
        device_id_raw: int = 0,
        sub_index: int = 0,
    ) -> ZoneDevices5BPayload | ZoneDevices6BPayload:
        """Construct ZoneDevices payload variant dynamically from arguments."""
        if sub_index != 0:
            return ZoneDevices6BPayload(
                zone_index_raw=zone_index_raw,
                device_role_id=device_role_id,
                sub_index=sub_index,
                device_id_raw=device_id_raw,
            )
        return ZoneDevices5BPayload(
            zone_index_raw=zone_index_raw,
            device_role_id=device_role_id,
            device_id_raw=device_id_raw,
            sub_index=0,
        )

    @property
    def zone_index(self) -> int | None:
        """Return numeric zone index or None if domain-level binding."""
        if getattr(self, "device_role_id", 0) in (0x0F, 0x0E, 0x0D):
            return None
        sub = getattr(self, "sub_index", 0)
        z_raw = getattr(self, "zone_index_raw", 0)
        return (
            sub
            if getattr(self, "device_role_id", 0) == 0x09 and sub
            else z_raw
        )

    def to_dict(self, msg: Message | None = None) -> PayloadT:
        """Convert zone devices payload to legacy dictionary layout."""
        role_id = getattr(self, "device_role_id", 0)
        z_raw = getattr(self, "zone_index_raw", 0)
        sub = getattr(self, "sub_index", 0)
        dev_raw = getattr(self, "device_id_raw", 0)

        role_hex = f"{role_id:02X}"
        if role_hex == "0E" and z_raw == 1:
            device_role = "heating_valve"
        else:
            device_role = DEV_ROLE_MAP.get(role_hex, role_hex)

        result: PayloadT = {
            SZ_ZONE_TYPE: role_hex,
            SZ_DEVICE_ROLE: device_role,
        }

        if role_hex == "09":
            is_ufc_device = False
            if msg is not None and getattr(msg, "src", None) is not None:
                source_id_str = str(getattr(msg.src, "id", msg.src))
                if source_id_str.startswith("02:") or getattr(
                    msg.src, "type", ""
                ) in (
                    "02",
                    DevType.UFC,
                ):
                    is_ufc_device = True

            if is_ufc_device or (sub is not None and sub != 0x7F):
                result[SZ_UFH_INDEX] = f"{z_raw:02X}"
                result[SZ_ZONE_INDEX] = None if sub == 0x7F else f"{sub:02X}"
            else:
                result[SZ_ZONE_INDEX] = f"{z_raw:02X}"
        elif role_hex in ("0D", "0E"):
            result[SZ_DOMAIN_INDEX] = FA if z_raw == 0 else F9
        elif role_hex == "0F":
            result[SZ_DOMAIN_INDEX] = FC
        else:
            result[SZ_ZONE_INDEX] = f"{z_raw:02X}"

        dev_hex = f"{dev_raw:06X}"
        if dev_hex in ("7FFFFF", "FFFFFF", "000000"):
            result[SZ_DEVICES] = []
        else:
            result[SZ_DEVICES] = [Address.convert_from_hex(dev_hex)]

        return result

    @property
    def domain_id(self) -> str | None:
        """Derive authoritative binding domain ID (FC, FA, F9) from role & index."""
        role_id = getattr(self, "device_role_id", 0)
        z_raw = getattr(self, "zone_index_raw", 0)
        if role_id == 0x0F:
            return FC
        if role_id in (0x0E, 0x0D):
            return FA if z_raw == 0 else F9
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
            raise ValueError(
                f"Invalid payload length for 000C: {len(raw_data)}"
            )

        if (
            len(raw_data) >= 11
            and (len(raw_data) - 6) % 5 == 0
            and (len(raw_data) % 6 != 0 or raw_data[6] != raw_data[0])
        ):
            res_list: list[PayloadBase] = [
                ZoneDevices6BPayload.from_bytes(raw_data[:6])
            ]
            zone_index = raw_data[0]
            for i in range(6, len(raw_data), 5):
                role_id_seq, sub_index_seq, dev_bytes_seq = struct.unpack_from(
                    ">BB3s", raw_data, i
                )
                res_list.append(
                    ZoneDevices6BPayload(
                        zone_index_raw=zone_index,
                        device_role_id=role_id_seq,
                        sub_index=sub_index_seq,
                        device_id_raw=int.from_bytes(
                            dev_bytes_seq, byteorder="big"
                        ),
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

    zone_index_raw: int
    device_role_id: int
    device_id_raw: int
    sub_index: int = 0

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 5-byte zone devices binary payload."""
        if len(raw_data) < 5:
            raise ValueError(
                f"Invalid payload length for ZoneDevices5BPayload: {len(raw_data)}"
            )
        zone_index, role_id, dev_bytes = struct.unpack_from(
            cls._STRUCT_FMT, raw_data, 0
        )
        return cls(
            zone_index_raw=zone_index,
            device_role_id=role_id,
            device_id_raw=int.from_bytes(dev_bytes, byteorder="big"),
            sub_index=0,
        )

    def to_bytes(self) -> bytes:
        """Pack 5-byte zone devices binary payload."""
        dev_bytes = self.device_id_raw.to_bytes(3, byteorder="big")
        return struct.pack(
            self._STRUCT_FMT,
            self.zone_index_raw,
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

    zone_index_raw: int
    device_role_id: int
    sub_index: int
    device_id_raw: int

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 6-byte zone devices binary payload."""
        if len(raw_data) < 6:
            raise ValueError(
                f"Invalid payload length for ZoneDevices6BPayload: {len(raw_data)}"
            )
        zone_index, role_id, sub_index, dev_bytes = struct.unpack_from(
            cls._STRUCT_FMT, raw_data, 0
        )
        return cls(
            zone_index_raw=zone_index,
            device_role_id=role_id,
            sub_index=sub_index,
            device_id_raw=int.from_bytes(dev_bytes, byteorder="big"),
        )

    def to_bytes(self) -> bytes:
        """Pack 6-byte zone devices binary payload."""
        dev_bytes = self.device_id_raw.to_bytes(3, byteorder="big")
        return struct.pack(
            self._STRUCT_FMT,
            self.zone_index_raw,
            self.device_role_id,
            self.sub_index,
            dev_bytes,
        )


# Update VARIANTS property after variants are defined
ZoneDevicesPayload.VARIANTS = (
    ZoneDevices5BPayload,
    ZoneDevices6BPayload,
)


# ----------------------------------------------------------------------


@register_payload(Code._1081)
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
            raise ValueError(
                f"Invalid payload length for 1081: {len(raw_data)}"
            )
        _hdr, temp_raw = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(setpoint_temp=temp_raw / 100.0)

    def to_bytes(self) -> bytes:
        """Pack max CH setpoint data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        temp_raw = int(round(self.setpoint_temp * 100.0))
        return struct.pack(self._STRUCT_FMT, 0x00, temp_raw)

    def to_dict(self) -> PayloadT:
        """Convert max CH setpoint payload to legacy dictionary layout.

        :returns: Decoded setpoint dictionary.
        :rtype: PayloadT
        """
        return {SZ_SETPOINT: self.setpoint_temp}


# ----------------------------------------------------------------------


@register_payload(Code._1090)
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
            raise ValueError(
                f"Invalid payload length for 1090: {len(raw_data)}"
            )
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


@register_payload(Code._1100)
class TpiParamsPayload(PayloadBase):
    """Master payload dispatcher for TPI parameters (Opcode 1100).

    Dispatches TPI parameter binary payloads to 4-byte or 8-byte
    variant sub-dataclasses based on payload length.

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

    VARIANTS: ClassVar[tuple[type[PayloadBase], ...]] = ()

    domain_id: int
    cycle_rate: int
    min_on_time: float
    min_off_time: float
    proportional_band_width: float | None = None

    @classmethod
    def from_bytes(
        cls, raw_data: bytes
    ) -> TpiParams4BPayload | TpiParams8BPayload:
        """Unpack TPI params binary payload, dispatching by length."""
        if len(raw_data) >= 8:
            return TpiParams8BPayload.from_bytes(raw_data)
        return TpiParams4BPayload.from_bytes(raw_data)

    def to_bytes(self) -> bytes:
        """Pack payload base default method.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        :raises NotImplementedError: Master dispatcher must dispatch to
            variant sub-dataclass.
        """
        raise NotImplementedError("Use concrete variant sub-dataclass")


@dataclass(frozen=True, slots=True)
class TpiParams4BPayload(TpiParamsPayload):
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
    :param proportional_band_width: Proportional band width (None for 4B
        payload).
    :type proportional_band_width: float | None
    """

    _STRUCT_FMT: ClassVar[str] = ">BBBB"

    domain_id: int
    cycle_rate: int
    min_on_time: float
    min_off_time: float
    proportional_band_width: float | None = None

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

    def to_dict(self) -> PayloadT:
        """Convert TPI parameters payload to legacy dictionary layout."""
        dom = (
            f"{self.domain_id:02X}"
            if isinstance(self.domain_id, int)
            else self.domain_id
        )
        return {
            SZ_DOMAIN_INDEX: dom,
            SZ_CYCLE_RATE: self.cycle_rate,
            SZ_MIN_ON_TIME: self.min_on_time,
            SZ_MIN_OFF_TIME: self.min_off_time,
            SZ_PROPORTIONAL_BAND_WIDTH: None,
        }


@dataclass(frozen=True, slots=True)
class TpiParams8BPayload(TpiParamsPayload):
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
    proportional_band_width: float | None = None

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

    def to_dict(self) -> PayloadT:
        """Convert TPI parameters payload to legacy dictionary layout."""
        dom = (
            f"{self.domain_id:02X}"
            if isinstance(self.domain_id, int)
            else self.domain_id
        )
        return {
            SZ_DOMAIN_INDEX: dom,
            SZ_CYCLE_RATE: self.cycle_rate,
            SZ_MIN_ON_TIME: self.min_on_time,
            SZ_MIN_OFF_TIME: self.min_off_time,
            SZ_PROPORTIONAL_BAND_WIDTH: self.proportional_band_width,
        }


# Update VARIANTS property after variants are defined
TpiParamsPayload.VARIANTS = (
    TpiParams4BPayload,
    TpiParams8BPayload,
)


# ----------------------------------------------------------------------


@register_payload(Code._1300)
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

    Protocol Notes:
      # 0x09F6 (2550 dec = 2.55 bar), 0x31FF, 0x7FFF appear to be sentinel values.

    :param pressure_bar: Pressure in Bar float, or None if
        sentinel/invalid.
    :type pressure_bar: float | None
    """

    _STRUCT_FMT: ClassVar[str] = ">Bh"

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
            raise ValueError(
                f"Invalid payload length for ChPressurePayload: {len(raw_data)}"
            )
        _hdr, p_raw = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        p_val = (
            None if p_raw in (0x09F6, 0x31FF, 0x7FFF, 32767) else p_raw / 100.0
        )
        return cls(pressure_bar=p_val)

    def to_bytes(self) -> bytes:
        """Pack CH pressure data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        if self.pressure_bar is None:
            return struct.pack(self._STRUCT_FMT, 0x00, 0x7FFF)
        p_raw = int(round(self.pressure_bar * 100.0))
        return struct.pack(self._STRUCT_FMT, 0x00, p_raw)

    def to_dict(self) -> PayloadT:
        """Convert CH pressure payload to legacy dictionary layout."""
        return {SZ_PRESSURE: self.pressure_bar}


# ----------------------------------------------------------------------


@register_payload(Code._2349)
class ZoneModePayload(PayloadBase):
    """Master payload dispatcher for zone mode (Opcode 2349).

    Protocol Notes:
      # RP --- 30:258557 34:225071 --:------ 2349 013 007FFF00FFFFFFFFFFFFFFFFFF
      # RP --- 30:253184 34:010943 --:------ 2349 013 00064000FFFFFF00110E0507E5
      # RQ --- 34:225071 30:258557 --:------ 2349 001 00
      # .W --- 18:141846 01:050858 --:------ 2349 013 02-0960-04-FFFFFF-0409160607E5
      # .W --- 18:141846 01:050858 --:------ 2349 007 02-08FC-01-FFFFFF
    """

    VARIANTS: ClassVar[tuple[type[PayloadBase], ...]] = ()

    zone_index: int | str
    setpoint_temp: float | None
    mode_code: int | str
    duration_minutes: int | None
    until_dtm: str | dt | bytes | None

    @classmethod
    def create(
        cls,
        zone_index: int | str = 0,
        setpoint_temp: float | None = None,
        mode_code: int | str = 0,
        duration_minutes: int | None = None,
        until_dtm: str | dt | bytes | None = None,
    ) -> ZoneMode7BPayload | ZoneMode13BPayload:
        """Construct ZoneMode payload variant dynamically."""
        if until_dtm is not None:
            return ZoneMode13BPayload(
                zone_index=zone_index,
                setpoint_temp=setpoint_temp,
                mode_code=mode_code,
                duration_minutes=duration_minutes,
                until_dtm=until_dtm,
            )
        return ZoneMode7BPayload(
            zone_index=zone_index,
            setpoint_temp=setpoint_temp,
            mode_code=mode_code,
            duration_minutes=duration_minutes,
        )

    @classmethod
    def from_bytes(
        cls, raw_data: bytes
    ) -> ZoneMode4BPayload | ZoneMode7BPayload | ZoneMode13BPayload:
        """Unpack zone mode payload, dispatching by length."""
        if len(raw_data) >= 13:
            return ZoneMode13BPayload.from_bytes(raw_data)
        if len(raw_data) >= 7:
            return ZoneMode7BPayload.from_bytes(raw_data)
        if len(raw_data) >= 4:
            return ZoneMode4BPayload.from_bytes(raw_data)
        raise ValueError(f"Invalid payload length for 2349: {len(raw_data)}")

    def to_bytes(self) -> bytes:
        """Pack payload base default method.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        :raises NotImplementedError: Master dispatcher must dispatch to
            variant sub-dataclass.
        """
        raise NotImplementedError("Use concrete variant sub-dataclass")


@dataclass(frozen=True, slots=True)
class ZoneMode4BPayload(ZoneModePayload):
    """4-byte zone mode basic payload (Opcode 2349).

    4-byte Zone Mode binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Zone Index (uint8)           : 00
      +1       h      2B   Setpoint Temp (int16*100)    : 08 34 (21.00°C)
      +3       B      1B   Zone Mode Code (uint8)       : 00 (Follow)
      --------------------------------------------------------------
      Field-spaced hex : 00 0834 00
      Payload hex      : 00083400

    :param zone_index: Zone index byte.
    :type zone_index: int | str
    :param setpoint_temp: Target setpoint temperature in °C.
    :type setpoint_temp: float | None
    :param mode_code: Zone mode code integer.
    :type mode_code: int | str
    """

    _STRUCT_FMT: ClassVar[str] = ">BhB"

    zone_index: int | str
    setpoint_temp: float | None
    mode_code: int | str
    duration_minutes: int | None = None
    until_dtm: str | dt | bytes | None = None

    def __post_init__(self) -> None:
        """Normalise index arguments."""
        if isinstance(self.zone_index, str):
            object.__setattr__(
                self, SZ_ZONE_INDEX, parse_index(self.zone_index)
            )
        if isinstance(self.mode_code, str):
            object.__setattr__(self, "mode_code", int(self.mode_code, 16))

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 4-byte zone mode binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked ZoneMode4BPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 4 bytes.
        """
        if len(raw_data) < 4:
            raise ValueError(
                f"Invalid payload length for ZoneMode4BPayload: {len(raw_data)}"
            )
        raw_index, sp_raw, mode = struct.unpack_from(
            cls._STRUCT_FMT, raw_data, 0
        )
        setpoint = None if sp_raw in (0x31FF, 0x7FFF) else sp_raw / 100.0
        return cls(
            zone_index=raw_index, setpoint_temp=setpoint, mode_code=mode
        )

    def to_bytes(self) -> bytes:
        """Pack 4-byte zone mode data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        index = parse_index(self.zone_index)
        if self.setpoint_temp is None:
            sp_raw = 0x7FFF
        else:
            sp_raw = int(round(self.setpoint_temp * 100.0))
        mode = (
            int(self.mode_code, 16)
            if isinstance(self.mode_code, str)
            else self.mode_code
        )
        return struct.pack(self._STRUCT_FMT, index, sp_raw, mode)

    def to_dict(self) -> PayloadT:
        """Convert 4-byte zone mode payload to legacy dictionary layout.

        :returns: Decoded zone mode dictionary.
        :rtype: PayloadT
        """
        mode_code_hex = (
            f"{self.mode_code:02X}"
            if isinstance(self.mode_code, int)
            else str(self.mode_code)
        )
        mode_str = ZON_MODE_MAP.get(mode_code_hex, mode_code_hex)
        index_str = (
            f"{self.zone_index:02X}"
            if isinstance(self.zone_index, int)
            else self.zone_index
        )
        return {
            SZ_ZONE_INDEX: index_str,
            SZ_MODE: mode_str,
            SZ_SETPOINT: self.setpoint_temp,
        }


@dataclass(frozen=True, slots=True)
class ZoneMode7BPayload(ZoneModePayload):
    """7-byte zone mode override with duration payload (Opcode 2349).

    7-byte Zone Mode binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Zone Index (uint8)           : 00
      +1       h      2B   Setpoint Temp (int16*100)    : 08 34 (21.00°C)
      +3       B      1B   Zone Mode Code (uint8)       : 00 (Follow)
      +4       3B     3B   Duration Minutes (uint24)    : FF FF FF
      --------------------------------------------------------------
      Field-spaced hex : 00 0834 00 FFFFFF
      Payload hex      : 00083400FFFFFF

    :param zone_index: Zone index byte.
    :type zone_index: int | str
    :param setpoint_temp: Target setpoint temperature in °C.
    :type setpoint_temp: float | None
    :param mode_code: Zone mode code integer.
    :type mode_code: int | str
    :param duration_minutes: Override duration in minutes, if present.
    :type duration_minutes: int | None
    :param until_dtm: Expiration datetime (None for 7B payload).
    :type until_dtm: str | dt | bytes | None
    """

    _STRUCT_FMT: ClassVar[str] = ">BhB"

    zone_index: int | str
    setpoint_temp: float | None
    mode_code: int | str
    duration_minutes: int | None = None
    until_dtm: str | dt | bytes | None = None

    def __post_init__(self) -> None:
        """Normalise index arguments."""
        if isinstance(self.zone_index, str):
            object.__setattr__(
                self, SZ_ZONE_INDEX, parse_index(self.zone_index)
            )
        if isinstance(self.mode_code, str):
            object.__setattr__(self, "mode_code", int(self.mode_code, 16))

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 7-byte zone mode binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked ZoneMode7BPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 7 bytes.
        """
        if len(raw_data) < 7:
            raise ValueError(
                f"Invalid payload length for ZoneMode7BPayload: {len(raw_data)}"
            )
        raw_index, sp_raw, mode = struct.unpack_from(
            cls._STRUCT_FMT, raw_data, 0
        )
        setpoint = None if sp_raw in (0x31FF, 0x7FFF) else sp_raw / 100.0
        dur = None
        if raw_data[4:7] != b"\xff\xff\xff":
            dur = int.from_bytes(raw_data[4:7], byteorder="big")
        return cls(
            zone_index=raw_index,
            setpoint_temp=setpoint,
            mode_code=mode,
            duration_minutes=dur,
        )

    def to_bytes(self) -> bytes:
        """Pack 7-byte zone mode data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        index = parse_index(self.zone_index)
        if self.setpoint_temp is None:
            sp_raw = 0x7FFF
        else:
            sp_raw = int(round(self.setpoint_temp * 100.0))
        mode = (
            int(self.mode_code, 16)
            if isinstance(self.mode_code, str)
            else self.mode_code
        )
        result = struct.pack(self._STRUCT_FMT, index, sp_raw, mode)
        if self.duration_minutes is not None:
            result += self.duration_minutes.to_bytes(3, byteorder="big")
        else:
            result += b"\xff\xff\xff"
        return result

    def to_dict(self) -> PayloadT:
        """Convert 7-byte zone mode payload to legacy dictionary layout.

        :returns: Decoded zone mode dictionary.
        :rtype: PayloadT
        """
        mode_code_hex = (
            f"{self.mode_code:02X}"
            if isinstance(self.mode_code, int)
            else str(self.mode_code)
        )
        mode_str = ZON_MODE_MAP.get(mode_code_hex, mode_code_hex)
        index_str = (
            f"{self.zone_index:02X}"
            if isinstance(self.zone_index, int)
            else self.zone_index
        )
        result: PayloadT = {
            SZ_ZONE_INDEX: index_str,
            SZ_MODE: mode_str,
            SZ_SETPOINT: self.setpoint_temp,
        }
        if self.duration_minutes is not None:
            result[SZ_DURATION] = self.duration_minutes
        return result


@dataclass(frozen=True, slots=True)
class ZoneMode13BPayload(ZoneModePayload):
    """13-byte zone mode override with expiry payload (Opcode 2349).

    13-byte Zone Mode binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Zone Index (uint8)           : 00
      +1       h      2B   Setpoint Temp (int16*100)    : 08 34 (21.00°C)
      +3       B      1B   Zone Mode Code (uint8)       : 00 (Follow)
      +4       3B     3B   Duration Minutes (uint24)    : FF FF FF
      +7       6B     6B   Expiration Datetime          : 00 11 0E 05 07 E5
      --------------------------------------------------------------
      Field-spaced hex : 00 0834 00 FFFFFF 00110E0507E5
      Payload hex      : 00083400FFFFFF00110E0507E5

    :param zone_index: Zone index byte.
    :type zone_index: int | str
    :param setpoint_temp: Target setpoint temperature in °C.
    :type setpoint_temp: float | None
    :param mode_code: Zone mode code integer.
    :type mode_code: int | str
    :param duration_minutes: Override duration in minutes, if present.
    :type duration_minutes: int | None
    :param until_dtm: Expiration datetime for temporary override.
    :type until_dtm: str | dt | bytes | None
    """

    _STRUCT_FMT: ClassVar[str] = ">BhB"

    zone_index: int | str
    setpoint_temp: float | None
    mode_code: int | str
    duration_minutes: int | None = None
    until_dtm: str | dt | bytes | None = None

    def __post_init__(self) -> None:
        """Normalise index arguments."""
        if isinstance(self.zone_index, str):
            object.__setattr__(
                self, SZ_ZONE_INDEX, parse_index(self.zone_index)
            )
        if isinstance(self.mode_code, str):
            object.__setattr__(self, "mode_code", int(self.mode_code, 16))

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 13-byte zone mode binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked ZoneMode13BPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 13 bytes.
        """
        if len(raw_data) < 13:
            raise ValueError(
                f"Invalid payload length for ZoneMode13BPayload: {len(raw_data)}"
            )
        raw_index, sp_raw, mode = struct.unpack_from(
            cls._STRUCT_FMT, raw_data, 0
        )
        setpoint = None if sp_raw in (0x31FF, 0x7FFF) else sp_raw / 100.0
        dur = None
        if raw_data[4:7] != b"\xff\xff\xff":
            dur = int.from_bytes(raw_data[4:7], byteorder="big")
        until_raw = hex_to_dtm(raw_data[7:13].hex().upper())
        return cls(
            zone_index=raw_index,
            setpoint_temp=setpoint,
            mode_code=mode,
            duration_minutes=dur,
            until_dtm=until_raw,
        )

    def to_bytes(self) -> bytes:
        """Pack 13-byte zone mode data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        index = parse_index(self.zone_index)
        if self.setpoint_temp is None:
            sp_raw = 0x7FFF
        else:
            sp_raw = int(round(self.setpoint_temp * 100.0))
        mode = (
            int(self.mode_code, 16)
            if isinstance(self.mode_code, str)
            else self.mode_code
        )
        result = struct.pack(self._STRUCT_FMT, index, sp_raw, mode)
        if self.duration_minutes is not None:
            result += self.duration_minutes.to_bytes(3, byteorder="big")
        else:
            result += b"\xff\xff\xff"
        if self.until_dtm is not None:
            if isinstance(self.until_dtm, bytes):
                result += self.until_dtm
            elif (
                isinstance(self.until_dtm, str)
                and len(self.until_dtm) == 12
                and all(c in "0123456789ABCDEFabcdef" for c in self.until_dtm)
            ):
                result += bytes.fromhex(self.until_dtm)
            else:
                result += bytes.fromhex(hex_from_dtm(self.until_dtm))
        else:
            result += b"\x00" * 6
        return result

    def to_dict(self) -> PayloadT:
        """Convert zone mode payload to legacy dictionary layout.

        :returns: Decoded zone mode dictionary.
        :rtype: PayloadT
        """
        mode_code_hex = (
            f"{self.mode_code:02X}"
            if isinstance(self.mode_code, int)
            else str(self.mode_code)
        )
        mode_str = ZON_MODE_MAP.get(mode_code_hex, mode_code_hex)
        index_str = (
            f"{self.zone_index:02X}"
            if isinstance(self.zone_index, int)
            else self.zone_index
        )
        result: PayloadT = {
            SZ_ZONE_INDEX: index_str,
            SZ_MODE: mode_str,
            SZ_SETPOINT: self.setpoint_temp,
        }
        if self.duration_minutes is not None:
            result[SZ_DURATION] = self.duration_minutes
        if self.until_dtm is not None:
            result[SZ_UNTIL] = self.until_dtm
        return result


# Update VARIANTS property after variants are defined
ZoneModePayload.VARIANTS = (
    ZoneMode4BPayload,
    ZoneMode7BPayload,
    ZoneMode13BPayload,
)


# ----------------------------------------------------------------------


@register_payload(Code._2389)
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

    Protocol Notes:
      # .I 024 03:052382 --:------ 03:052382 2389 003 02001B
      # State (of cooling?), from BDR91T, Hometronics CTL.

    :param domain_or_zone_index: Domain or zone index byte.
    :type domain_or_zone_index: int
    :param target_temp: Target temperature in °C.
    :type target_temp: float
    """

    _STRUCT_FMT: ClassVar[str] = ">Bh"

    domain_or_zone_index: int
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
            raise ValueError(
                f"Invalid payload length for 2389: {len(raw_data)}"
            )
        index, temp_raw = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(
            domain_or_zone_index=index,
            target_temp=temp_raw / 100.0,
        )

    def to_bytes(self) -> bytes:
        """Pack setpoint override data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        temp_raw = int(round(self.target_temp * 100.0))
        return struct.pack(
            self._STRUCT_FMT, self.domain_or_zone_index, temp_raw
        )

    def to_dict(self) -> PayloadT:
        """Convert setpoint override payload to legacy dictionary layout.

        :returns: Decoded setpoint override dictionary.
        :rtype: PayloadT
        """
        return {SZ_SETPOINT: self.target_temp}


# ----------------------------------------------------------------------


@register_payload(Code._3B00)
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
            raise ValueError(
                f"Invalid payload length for 3B00: {len(raw_data)}"
            )
        domain_id, sync_flag = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(domain_id=domain_id, sync_flag=sync_flag)

    def to_bytes(self) -> bytes:
        """Pack actuator sync data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        return struct.pack(self._STRUCT_FMT, self.domain_id, self.sync_flag)

    def to_dict(self) -> PayloadT:
        """Convert actuator sync payload to legacy dictionary layout.

        :returns: Decoded actuator sync dictionary.
        :rtype: PayloadT
        """
        return {SZ_ACTUATOR_SYNC: self.sync_flag in (0xC8, 0xFF)}


# ----------------------------------------------------------------------


@register_payload(Code._3EF0)
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
            raise ValueError(
                f"Invalid payload length for 3EF0: {len(raw_data)}"
            )
        domain_id, mod_raw, f2 = struct.unpack_from(
            cls._STRUCT_FMT, raw_data, 0
        )
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
        )

    def to_bytes(self) -> bytes:
        """Pack actuator state data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        mod_raw = min(200, max(0, int(round(self.modulation_level * 200.0))))
        result = struct.pack(
            self._STRUCT_FMT, self.domain_id, mod_raw, self.flags_2
        )
        if self.flags_3 is not None:
            result += bytes([self.flags_3])
        if (
            self.unknown_4 is not None
            and self.unknown_5 is not None
            and self.flags_6 is not None
            and self.ch_setpoint is not None
            and self.max_rel_modulation is not None
        ):
            max_mod_raw = min(
                200, max(0, int(round(self.max_rel_modulation * 200.0)))
            )
            result += struct.pack(
                self._STRUCT_FMT_EXT,
                self.unknown_4,
                self.unknown_5,
                self.flags_6,
                self.ch_setpoint,
                max_mod_raw,
            )
        return result

    def to_dict(self, msg: Message | None = None) -> PayloadT:
        """Convert actuator state payload to legacy dictionary layout.

        When decoded from a 9-byte Underfloor Heating/Cooling controller (UFC /
        device type 02), flags_3 represents the pump heating/cooling relay state.

        :param msg: Optional legacy message context.
        :type msg: Any
        :returns: Decoded actuator state dictionary.
        :rtype: PayloadT
        """
        result: PayloadT = {
            SZ_MODULATION_LEVEL: self.modulation_level,
        }
        if self.flags_3 is not None:
            f3 = self.flags_3
            result.update(
                {
                    SZ_CH_ACTIVE: bool(f3 & (1 << 1)),
                    SZ_DHW_ACTIVE: bool(f3 & (1 << 2)),
                    SZ_FLAME_ACTIVE: bool(f3 & (1 << 3)),
                    SZ_COOLING_ACTIVE: bool(f3 & (1 << 4)),
                }
            )
        if self.flags_6 is not None:
            result[SZ_CH_ENABLED] = bool(self.flags_6 & (1 << 0))
        if self.ch_setpoint is not None:
            result[SZ_CH_SETPOINT] = self.ch_setpoint
        if self.max_rel_modulation is not None:
            result[SZ_MAX_REL_MODULATION] = self.max_rel_modulation

        if (
            self.unknown_4 is not None
            and self.flags_3 is not None
            and getattr(getattr(msg, "src", None), "type", None)
            in ("02", DevType.UFC)
        ):
            relay_byte = self.flags_3
            result[SZ_PUMP_RELAY_STATE] = (
                PumpRelayState.COOLING
                if relay_byte & 0x10
                else PumpRelayState.HEATING
                if relay_byte & 0x02
                else PumpRelayState.OFF
            )

        return result


# ----------------------------------------------------------------------


@register_payload(Code._3EF1)
class ActuatorCyclePayload(PayloadBase):
    """Master payload dispatcher and base class for Opcode 3EF1.

    Protocol Notes:
      # RP --- 13:109598 18:002563 --:------ 3EF1 007 0000BF-00BFC8FF
      # RP --- 10:048122 18:140805 --:------ 3EF1 007 007FFF-003C2A10  # 10:s RP always 7FFF
      # RP --- 13:109598 18:199952 --:------ 3EF1 007 0001B8-01B800FF  # 13:s RP
      # RQ --- 31:004811 13:077615 --:------ 3EF1 001 00
      # RP --- 13:077615 31:004811 --:------ 3EF1 007 00024D001300FF
      # RQ --- 22:068154 13:031208 --:------ 3EF1 002 0000
      # RP --- 13:031208 22:068154 --:------ 3EF1 007 00024E00E000FF
    """

    VARIANTS: ClassVar[tuple[type[PayloadBase], ...]] = ()

    @classmethod
    def create(
        cls,
        cycle_countdown_sec: int | None = None,
        actuator_countdown_sec: int | None = None,
        modulation_level: float | None = None,
        domain_index: int | None = None,
    ) -> ActuatorCycle6BPayload | ActuatorCycle7BPayload:
        """Construct ActuatorCycle payload variant dynamically from arguments."""
        if domain_index is not None:
            return ActuatorCycle7BPayload(
                domain_index=domain_index,
                cycle_countdown_sec=cycle_countdown_sec,
                actuator_countdown_sec=actuator_countdown_sec,
                modulation_level=modulation_level,
            )
        return ActuatorCycle6BPayload(
            cycle_countdown_sec=cycle_countdown_sec,
            actuator_countdown_sec=actuator_countdown_sec,
            modulation_level=modulation_level,
        )

    def to_dict(self, msg: Message | None = None) -> PayloadT:
        """Convert actuator cycle payload to legacy dictionary layout."""
        return {
            SZ_CYCLE_COUNTDOWN: getattr(self, "cycle_countdown_sec", None),
            SZ_ACTUATOR_COUNTDOWN: getattr(
                self, "actuator_countdown_sec", None
            ),
            SZ_MODULATION_LEVEL: getattr(self, SZ_MODULATION_LEVEL, None),
        }

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> PayloadBase:
        """Unpack actuator cycle binary payload, dispatching by length."""
        if len(raw_data) < 6:
            raise ValueError(
                f"Invalid payload length for 3EF1: {len(raw_data)}"
            )
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
        c_raw, a_raw, _flag, mod_byte = struct.unpack_from(
            cls._STRUCT_FMT, raw_data, 0
        )
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
        c_raw = (
            0x7FFF
            if self.cycle_countdown_sec is None
            else self.cycle_countdown_sec
        )
        a_raw = (
            0x7FFF
            if self.actuator_countdown_sec is None
            else self.actuator_countdown_sec
        )
        if self.modulation_level is None:
            mod_raw = 0xFF
        else:
            mod_raw = min(
                200, max(0, int(round(self.modulation_level * 200.0)))
            )
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

    domain_index: int
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
        index, c_raw, a_raw, mod_byte, _flag = struct.unpack_from(
            cls._STRUCT_FMT, raw_data, 0
        )
        c_down = None if c_raw == 0x7FFF else c_raw
        a_down = c_down if (a_raw < 0 or a_raw == 0x7FFF) else a_raw
        mod = hex_to_percent(f"{mod_byte:02X}")
        return cls(
            domain_index=index,
            cycle_countdown_sec=c_down,
            actuator_countdown_sec=a_down,
            modulation_level=mod,
        )

    def to_bytes(self) -> bytes:
        """Pack 7-byte actuator cycle binary payload."""
        c_raw = (
            0x7FFF
            if self.cycle_countdown_sec is None
            else self.cycle_countdown_sec
        )
        a_raw = (
            0x7FFF
            if self.actuator_countdown_sec is None
            else self.actuator_countdown_sec
        )
        if self.modulation_level is None:
            mod_raw = 0xFF
        else:
            mod_raw = min(
                200, max(0, int(round(self.modulation_level * 200.0)))
            )
        return struct.pack(
            self._STRUCT_FMT, self.domain_index, c_raw, a_raw, mod_raw, 0xFF
        )


# Update VARIANTS property after variants are defined
ActuatorCyclePayload.VARIANTS = (
    ActuatorCycle6BPayload,
    ActuatorCycle7BPayload,
)
