"""RAMSES RF - Domestic Hot Water (DHW) payload dataclasses.

This module contains strongly-typed dataclass representations for
Domestic Hot Water packet payloads.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from ramses_rf.const import SZ_ACTIVE, SZ_DHW_INDEX, SZ_MODE, SZ_UNTIL, Code
from ramses_rf.typing import PayloadT
from ramses_tx.helpers import hex_to_dtm

from .base import PayloadBase, parse_index
from .registry import register_payload

if TYPE_CHECKING:
    pass

# ----------------------------------------------------------------------


@register_payload(Code._1260)
@dataclass(frozen=True, slots=True)
class DhwTempPayload(PayloadBase):
    """DHW cylinder temperature payload (Opcode 1260).

    3-byte DHW Temp binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   DHW Index (uint8)            : 00
      +1       h      2B   Temperature (int16*100)      : 08 37 (21.03°C)
      --------------------------------------------------------------
      Field-spaced hex : 00 0837
      Payload hex      : 000837
    :param temperature: DHW cylinder temperature in °C, or None if invalid.
    :type temperature: float | None

    Sample Packet Logs:
    # RQ --- 30:185469 01:037519 --:------ 1260 001 00
    # RP --- 01:037519 30:185469 --:------ 1260 003 000837
    # RQ --- 18:200202 10:067219 --:------ 1260 002 0000
    # RP --- 10:067219 18:200202 --:------ 1260 003 007FFF
    """

    _STRUCT_FMT: ClassVar[str] = ">Bh"

    dhw_index: int | str = 0
    temperature: float | None = None

    def __post_init__(self) -> None:
        """Normalise index arguments."""
        if isinstance(self.dhw_index, str):
            object.__setattr__(self, "dhw_index", parse_index(self.dhw_index))

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack DHW cylinder temperature binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked DhwTempPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 3 bytes.
        """
        if len(raw_data) < 3:
            raise ValueError(
                f"Invalid payload length for 1260: {len(raw_data)}"
            )
        index, temp_raw = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        temp_val = None if temp_raw in (0x31FF, 0x7FFF) else temp_raw / 100.0
        return cls(dhw_index=index, temperature=temp_val)

    def to_bytes(self) -> bytes:
        """Pack DHW cylinder temperature data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        index = parse_index(self.dhw_index)
        if self.temperature is None:
            temp_raw = 0x7FFF
        else:
            temp_raw = int(round(self.temperature * 100.0))
        return struct.pack(self._STRUCT_FMT, index, temp_raw)

    def to_dict(self) -> PayloadT:
        """Convert DHW temperature payload to pre-0.60 dictionary layout.

        :returns: Decoded DHW temperature dictionary.
        :rtype: PayloadT
        """
        return {"temperature": self.temperature}


# ----------------------------------------------------------------------


@register_payload(Code._12F0)
@dataclass(frozen=True, slots=True)
class DhwFlowRatePayload(PayloadBase):
    """DHW flow rate payload (Opcode 12F0).

    3-byte DHW Flow Rate binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   DHW Index (uint8)            : 00
      +1       h      2B   Flow Rate (int16*100)        : 03 07 (7.75 L/min)
      --------------------------------------------------------------
      Field-spaced hex : 00 0307
      Payload hex      : 000307

    :param dhw_index: DHW index byte.
    :type dhw_index: int
    :param dhw_flow_rate: DHW flow rate in L/min, or None if invalid.
    :type dhw_flow_rate: float | None

    Sample Packet Logs:
    # RP --- 10:048122 18:006402 --:------ 12F0 003 000307
    # RP --- 10:023327 18:131597 --:------ 12F0 003 000023
    # RP --- 10:051349 18:135447 --:------ 12F0 003 00059F
    """

    _STRUCT_FMT: ClassVar[str] = ">Bh"

    dhw_index: int
    dhw_flow_rate: float | None

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack DHW flow rate binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked DhwFlowRatePayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 3 bytes.
        """
        if len(raw_data) < 3:
            raise ValueError(
                f"Invalid payload length for 12F0: {len(raw_data)}"
            )
        index, flow_raw = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        flow_val = None if flow_raw in (0x31FF, 0x7FFF) else flow_raw / 100.0
        return cls(dhw_index=index, dhw_flow_rate=flow_val)

    def to_bytes(self) -> bytes:
        """Pack DHW flow rate data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        raw_val = (
            0x7FFF
            if self.dhw_flow_rate is None
            else int(round(self.dhw_flow_rate * 100.0))
        )
        return struct.pack(self._STRUCT_FMT, self.dhw_index, raw_val)

    def to_dict(self) -> PayloadT:
        """Convert DHW flow rate payload to pre-0.60 dictionary layout.

        :returns: Decoded DHW flow rate dictionary.
        :rtype: PayloadT
        """
        return {"dhw_flow_rate": self.dhw_flow_rate}


# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DhwConfigPayload(PayloadBase):
    """DHW configuration payload.

    3-byte DHW Config binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   DHW Index (uint8)            : 00
      +1       h      2B   Setpoint Temp (int16*100)    : 13 88 (50.00°C)
      --------------------------------------------------------------
      Field-spaced hex : 00 1388
      Payload hex      : 001388

    :param dhw_index: DHW index byte.
    :type dhw_index: int
    :param setpoint_temp: Target DHW setpoint temperature in °C.
    :type setpoint_temp: float
    """

    _STRUCT_FMT: ClassVar[str] = ">Bh"

    dhw_index: int
    setpoint_temp: float

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack DHW config binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked DhwConfigPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 3 bytes.
        """
        if len(raw_data) < 3:
            raise ValueError(
                f"Invalid payload length for DhwConfigPayload: {len(raw_data)}"
            )
        index, setpoint_raw = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(dhw_index=index, setpoint_temp=setpoint_raw / 100.0)

    def to_bytes(self) -> bytes:
        """Pack DHW config data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        sp_raw = int(round(self.setpoint_temp * 100.0))
        return struct.pack(self._STRUCT_FMT, self.dhw_index, sp_raw)


# ----------------------------------------------------------------------


@register_payload(Code._10A0)
class DhwParamsPayload(PayloadBase):
    """Master payload dispatcher for DHW parameters (Opcode 10A0).

    Dispatches DHW parameters binary payloads to 3-byte or 6-byte
    variant sub-dataclasses based on payload length.

    Sample Packet Logs & Protocol Notes:
    # dhw (cylinder) params  # FIXME: a bit messy
    # these from a RFG...
    # RQ --- 07:045960 01:145038 --:------ 10A0 006 00-1087-00-03E4  # RQ/RP, every 24h
    # RP --- 01:145038 07:045960 --:------ 10A0 006 00-109A-00-03E8
    # RP --- 10:048122 18:006402 --:------ 10A0 003 00-1B58
    # RQ --- 01:136410 10:067219 --:------ 10A0 002 0000
    # RQ --- 07:017494 01:078710 --:------ 10A0 006 00-1566-00-03E4
    # RQ --- 07:045960 01:145038 --:------ 10A0 006 00-31FF-00-31FF  # null
    # RQ --- 07:045960 01:145038 --:------ 10A0 006 00-1770-00-03E8
    # RQ --- 07:045960 01:145038 --:------ 10A0 006 00-1374-00-03E4
    # RQ --- 07:030741 01:102458 --:------ 10A0 006 00-181F-00-03E4
    # RQ --- 07:036831 23:100224 --:------ 10A0 006 01-1566-00-03E4  # non-evohome
    # RQ --- 30:185469 01:037519 --:------ 0005 002 000E
    # RP --- 01:037519 30:185469 --:------ 0005 004 000E0300  # two DHW valves
    # RQ --- 30:185469 01:037519 --:------ 10A0 001 01 (01 )
    # RQ --- 30:185469 01:037519 --:------ 10A0 001 01
    # RQ --- 07:045960 01:145038 --:------ 10A0 006 0013740003E4
    # 045 RQ --- 07:045960 01:145038 --:------ 10A0 006 0013740003E4
    # 037 RQ --- 18:013393 01:145038 --:------ 10A0 001 00
    # RQ --- 18:013393 01:145038 --:------ 10A0 001 00
    # 054 RP --- 01:145038 18:013393 --:------ 10A0 006 0013880003E8
    # RP --- 01:145038 18:013393 --:------ 10A0 006 0013880003E8
    """

    VARIANTS: ClassVar[tuple[type[PayloadBase], ...]] = ()

    dhw_index: int | str
    setpoint: float | None
    overrun: int | None = None
    differential: float | None = None

    @classmethod
    def create(
        cls,
        dhw_index: int | str = 0,
        setpoint: float | None = None,
        overrun: int = 0,
        differential: float = 0.0,
    ) -> DhwParams3BPayload | DhwParams6BPayload:
        """Construct DhwParams payload variant dynamically from arguments."""
        index = parse_index(dhw_index)
        if overrun != 0 or differential != 0.0:
            return DhwParams6BPayload(
                dhw_index=index,
                setpoint=setpoint,
                overrun=overrun,
                differential=differential,
            )
        return DhwParams3BPayload(dhw_index=index, setpoint=setpoint)

    @classmethod
    def from_bytes(
        cls, raw_data: bytes
    ) -> DhwParams3BPayload | DhwParams6BPayload:
        """Unpack DHW parameters binary payload, dispatching by length."""
        if len(raw_data) >= 6:
            return DhwParams6BPayload.from_bytes(raw_data)
        return DhwParams3BPayload.from_bytes(raw_data)

    def to_bytes(self) -> bytes:
        """Pack payload base default method.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        :raises NotImplementedError: Master dispatcher must dispatch to
            variant sub-dataclass.
        """
        raise NotImplementedError("Use concrete variant sub-dataclass")


@dataclass(frozen=True, slots=True)
class DhwParams3BPayload(DhwParamsPayload):
    """DHW 3-byte parameters payload (Opcode 10A0).

    3-byte DHW Parameters binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   DHW Index (uint8)            : 00
      +1       h      2B   Setpoint Temp (int16*100)    : 13 88 (50.00°C)
      --------------------------------------------------------------
      Field-spaced hex : 00 1388
      Payload hex      : 001388

    :param dhw_index: DHW index byte.
    :type dhw_index: int
    :param setpoint: Target setpoint temperature in °C, or None.
    :type setpoint: float | None
    :param overrun: Overrun minutes (None for 3B payload).
    :type overrun: int | None
    :param differential: Differential °C (None for 3B payload).
    :type differential: float | None
    """

    _STRUCT_FMT: ClassVar[str] = ">Bh"

    dhw_index: int | str = 0
    setpoint: float | None = None
    overrun: int | None = None
    differential: float | None = None

    def __post_init__(self) -> None:
        """Normalise index arguments."""
        if isinstance(self.dhw_index, str):
            object.__setattr__(self, "dhw_index", parse_index(self.dhw_index))

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 3-byte DHW parameters binary payload."""
        if len(raw_data) < 3:
            raise ValueError(
                f"Invalid payload length for DhwParams3BPayload: {len(raw_data)}"
            )
        index, sp_raw = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        sp_val = None if sp_raw in (0x31FF, 0x7FFF, 0x639C) else sp_raw / 100.0
        return cls(dhw_index=index, setpoint=sp_val)

    def to_bytes(self) -> bytes:
        """Pack 3-byte DHW parameters into binary payload."""
        index = parse_index(self.dhw_index)
        sp_raw = (
            0x7FFF
            if self.setpoint is None
            else int(round(self.setpoint * 100.0))
        )
        return struct.pack(self._STRUCT_FMT, index, sp_raw)

    def to_dict(self) -> PayloadT:
        """Convert 3-byte DHW parameters payload to pre-0.60 dictionary layout."""
        return {"setpoint": self.setpoint}


@dataclass(frozen=True, slots=True)
class DhwParams6BPayload(DhwParamsPayload):
    """DHW 6-byte parameters payload (Opcode 10A0).

    6-byte DHW Parameters binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   DHW Index (uint8)            : 00
      +1       h      2B   Setpoint Temp (int16*100)    : 13 88 (50.00°C)
      +3       B      1B   Overrun minutes (uint8)      : 00
      +4       h      2B   Differential °C (int16*100)  : 03 E4 (10.00°C)
      --------------------------------------------------------------
      Field-spaced hex : 00 1388 00 03E4
      Payload hex      : 0013880003E4

    :param dhw_index: DHW index byte.
    :type dhw_index: int
    :param setpoint: Target setpoint temperature in °C, or None.
    :type setpoint: float | None
    :param overrun: Overrun time in minutes.
    :type overrun: int
    :param differential: Temperature differential in °C.
    :type differential: float
    """

    _STRUCT_FMT: ClassVar[str] = ">BhBh"

    dhw_index: int | str = 0
    setpoint: float | None = None
    overrun: int = 0
    differential: float = 0.0

    def __post_init__(self) -> None:
        """Normalise index arguments."""
        if isinstance(self.dhw_index, str):
            object.__setattr__(self, "dhw_index", parse_index(self.dhw_index))

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 6-byte DHW parameters binary payload."""
        if len(raw_data) < 6:
            raise ValueError(
                f"Invalid payload length for DhwParams6BPayload: {len(raw_data)}"
            )
        index, sp_raw, overrun, diff_raw = struct.unpack_from(
            cls._STRUCT_FMT, raw_data, 0
        )
        sp_val = None if sp_raw in (0x31FF, 0x7FFF, 0x639C) else sp_raw / 100.0
        return cls(
            dhw_index=index,
            setpoint=sp_val,
            overrun=overrun,
            differential=diff_raw / 100.0,
        )

    def to_bytes(self) -> bytes:
        """Pack 6-byte DHW parameters into binary payload."""
        index = parse_index(self.dhw_index)
        sp_raw = (
            0x7FFF
            if self.setpoint is None
            else int(round(self.setpoint * 100.0))
        )
        diff_raw = int(round(self.differential * 100.0))
        return struct.pack(
            self._STRUCT_FMT, index, sp_raw, self.overrun, diff_raw
        )

    def to_dict(self) -> PayloadT:
        """Convert 6-byte DHW parameters payload to pre-0.60 dictionary layout."""
        return {
            "setpoint": self.setpoint,
            "overrun": self.overrun,
            "differential": self.differential,
        }


DhwParamsPayload.VARIANTS = (DhwParams3BPayload, DhwParams6BPayload)


# ----------------------------------------------------------------------


@register_payload(Code._1F41)
class DhwStatePayload(PayloadBase):
    """Master payload dispatcher for DHW state (Opcode 1F41).

    Dispatches DHW state binary payloads to 2-byte, 3-byte, or
    extended override variant sub-dataclasses based on payload length.

    Sample Packet Logs & Protocol Notes:
    # 053 RP --- 01:145038 18:013393 --:------ 1F41 006 00FF00FFFFFF  # no stored DHW
    # Note: Evohome DHW acknowledges W 1F41 with I 1F41 rather than RP 1F41.
    """

    VARIANTS: ClassVar[tuple[type[PayloadBase], ...]] = ()

    def __new__(
        cls,
        dhw_index: int = 0,
        active_flag: int = 0,
        mode_value: int | None = None,
        raw_bytes: bytes | None = None,
    ) -> DhwStatePayload:
        """Construct DhwState payload variant dynamically from arguments."""
        if cls is not DhwStatePayload:
            return super().__new__(cls)
        if raw_bytes is not None and mode_value is not None:
            return DhwStateOverridePayload(
                dhw_index=dhw_index,
                active_flag=active_flag,
                mode_value=mode_value,
                raw_bytes=raw_bytes,
            )
        if mode_value is not None:
            return DhwState3BPayload(
                dhw_index=dhw_index,
                active_flag=active_flag,
                mode_value=mode_value,
            )
        return DhwState2BPayload(dhw_index=dhw_index, active_flag=active_flag)

    @classmethod
    def from_bytes(
        cls, raw_data: bytes
    ) -> DhwState2BPayload | DhwState3BPayload | DhwStateOverridePayload:
        """Unpack DHW state binary payload, dispatching by length."""
        if len(raw_data) >= 6:
            return DhwStateOverridePayload.from_bytes(raw_data)
        if len(raw_data) >= 3:
            return DhwState3BPayload.from_bytes(raw_data)
        return DhwState2BPayload.from_bytes(raw_data)

    def to_bytes(self) -> bytes:
        """Pack payload base default method."""
        raise NotImplementedError("Use concrete variant sub-dataclass")


@dataclass(frozen=True, slots=True)
class DhwState2BPayload(DhwStatePayload):
    """DHW 2-byte state payload (Opcode 1F41).

    2-byte DHW State binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   DHW Index (uint8)            : 00
      +1       B      1B   DHW Active Flag (0=Off, 1=On): 01
      --------------------------------------------------------------
      Field-spaced hex : 00 01
      Payload hex      : 0001

    :param dhw_index: DHW index byte.
    :type dhw_index: int
    :param active_flag: DHW active status flag byte.
    :type active_flag: int
    :param mode_value: Optional mode value (None for 2-byte payload).
    :type mode_value: None
    """

    _STRUCT_FMT: ClassVar[str] = ">BB"

    dhw_index: int
    active_flag: int
    mode_value: None = None

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 2-byte DHW state binary payload."""
        if len(raw_data) < 2:
            raise ValueError(
                f"Invalid payload length for DhwState2BPayload: {len(raw_data)}"
            )
        index, active = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(dhw_index=index, active_flag=active)

    def to_bytes(self) -> bytes:
        """Pack 2-byte DHW state into binary payload."""
        return struct.pack(self._STRUCT_FMT, self.dhw_index, self.active_flag)

    def to_dict(self) -> PayloadT:
        """Convert 2-byte DHW state to pre-0.60 dictionary format."""
        result: PayloadT = {SZ_DHW_INDEX: f"{self.dhw_index:02X}"}
        result[SZ_ACTIVE] = (
            None if self.active_flag == 0xFF else (self.active_flag == 0x01)
        )
        if self.active_flag != 0xFF:
            result[SZ_MODE] = "follow_schedule"
        return result


@dataclass(frozen=True, slots=True)
class DhwState3BPayload(DhwStatePayload):
    """DHW 3-byte state payload (Opcode 1F41).

    3-byte DHW State binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   DHW Index (uint8)            : 00
      +1       B      1B   DHW Active Flag (0=Off, 1=On): 01
      +2       B      1B   DHW Mode Value (uint8)       : 00
      --------------------------------------------------------------
      Field-spaced hex : 00 01 00
      Payload hex      : 000100

    :param dhw_index: DHW index byte.
    :type dhw_index: int
    :param active_flag: DHW active status flag byte.
    :type active_flag: int
    :param mode_value: DHW mode value byte.
    :type mode_value: int
    """

    _STRUCT_FMT: ClassVar[str] = ">BBB"

    dhw_index: int
    active_flag: int
    mode_value: int

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 3-byte DHW state binary payload."""
        if len(raw_data) < 3:
            raise ValueError(
                f"Invalid payload length for DhwState3BPayload: {len(raw_data)}"
            )
        index, active, mode = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(dhw_index=index, active_flag=active, mode_value=mode)

    def to_bytes(self) -> bytes:
        """Pack 3-byte DHW state into binary payload."""
        return struct.pack(
            self._STRUCT_FMT, self.dhw_index, self.active_flag, self.mode_value
        )

    def to_dict(self) -> PayloadT:
        """Convert 3-byte DHW state to pre-0.60 dictionary format."""
        result: PayloadT = {SZ_DHW_INDEX: f"{self.dhw_index:02X}"}
        result[SZ_ACTIVE] = (
            None if self.active_flag == 0xFF else (self.active_flag == 0x01)
        )
        mode_map = {
            0: "follow_schedule",
            1: "advanced_override",
            2: "permanent_override",
            3: "countdown_override",
            4: "temporary_override",
        }
        if self.mode_value in mode_map:
            result[SZ_MODE] = mode_map[self.mode_value]
        elif self.active_flag != 0xFF:
            result[SZ_MODE] = "follow_schedule"
        return result


@dataclass(frozen=True, slots=True)
class DhwStateOverridePayload(DhwStatePayload):
    """DHW extended state payload with until timestamp (Opcode 1F41).

    Extended DHW State binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   DHW Index (uint8)            : 00
      +1       B      1B   DHW Active Flag (0=Off, 1=On): 01
      +2       B      1B   DHW Mode Value (uint8)       : 01
      +3       3s     3B   Reserved Padding             : FF FF FF
      +6       6s     6B   ISO Until Timestamp Bytes    : 21 11 05 06 25 20
      --------------------------------------------------------------
      Field-spaced hex : 00 01 01 FFFFFF 211105062520
      Payload hex      : 000101FFFFFF211105062520

    :param dhw_index: DHW index byte.
    :type dhw_index: int
    :param active_flag: DHW active status flag byte.
    :type active_flag: int
    :param mode_value: DHW mode value byte.
    :type mode_value: int
    :param raw_bytes: Complete raw payload bytes sequence.
    :type raw_bytes: bytes
    """

    _STRUCT_FMT: ClassVar[str] = ">BBB"

    dhw_index: int
    active_flag: int
    mode_value: int
    raw_bytes: bytes

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack extended DHW state binary payload."""
        if len(raw_data) < 3:
            msg = f"Invalid payload length for DhwStateOverridePayload: {len(raw_data)}"
            raise ValueError(msg)
        index, active, mode = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(
            dhw_index=index,
            active_flag=active,
            mode_value=mode,
            raw_bytes=raw_data,
        )

    def to_bytes(self) -> bytes:
        """Pack extended DHW state into binary payload."""
        return self.raw_bytes

    def to_dict(self) -> PayloadT:
        """Convert extended DHW state to pre-0.60 dictionary format."""
        result: PayloadT = {SZ_DHW_INDEX: f"{self.dhw_index:02X}"}
        result[SZ_ACTIVE] = (
            None if self.active_flag == 0xFF else (self.active_flag == 0x01)
        )
        mode_map = {
            0: "follow_schedule",
            1: "advanced_override",
            2: "permanent_override",
            3: "countdown_override",
            4: "temporary_override",
        }
        if self.mode_value in mode_map:
            result[SZ_MODE] = mode_map[self.mode_value]
        elif self.active_flag != 0xFF:
            result[SZ_MODE] = "follow_schedule"

        raw_hex = self.raw_bytes.hex().upper()
        if len(raw_hex) >= 24:
            dtm_hex = raw_hex[12:24]
            if dtm_hex != "FFFFFFFFFFFF":
                result[SZ_UNTIL] = hex_to_dtm(dtm_hex)
            else:
                result[SZ_UNTIL] = None
        return result


DhwStatePayload.VARIANTS = (
    DhwState2BPayload,
    DhwState3BPayload,
    DhwStateOverridePayload,
)


# ----------------------------------------------------------------------


@register_payload(Code._11F0)
@dataclass(frozen=True, slots=True)
class DhwHeatpumpRelayPayload(PayloadBase):
    """Heatpump relay status payload (Opcode 11F0).

    9-byte Heatpump Relay Status binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       9B     9B   Raw Relay Status Byte Array  : 00 00 09 00 00 00 00 00 00
      --------------------------------------------------------------
      Field-spaced hex : 00 00 09 00 00 00 00 00 00
      Payload hex      : 000009000000000000

    :param raw_status_bytes: Raw status bytes sequence.
    :type raw_status_bytes: bytes
    """

    raw_status_bytes: bytes

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack heatpump relay status binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked DhwHeatpumpRelayPayload instance.
        :rtype: Self
        """
        return cls(raw_status_bytes=raw_data)

    def to_bytes(self) -> bytes:
        """Pack heatpump relay status data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        return self.raw_status_bytes


# ----------------------------------------------------------------------
