"""RAMSES RF - OpenTherm bridge payload dataclasses.

This module contains strongly-typed dataclass representations for OpenTherm bridge
and boiler gateway packet payloads.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from ramses_rf.typing import PayloadT
from ramses_tx.const import Code

from ..const import (
    SZ_DIAGNOSTIC_CODE,
    SZ_FLAGS,
    SZ_FRAME_CODE,
    SZ_HEAT_DEMAND,
    SZ_MESSAGE_ID,
    SZ_RAW_VALUE,
    SZ_STATUS_CODE,
    SZ_TEMPERATURE,
    SZ_TICKER,
    SZ_VALUE,
)
from ..protocol.opentherm import (
    EN,
    SZ_DESCRIPTION,
    SZ_MSG_ID,
    SZ_MSG_NAME,
    SZ_MSG_TYPE,
    decode_frame,
    parity,
)
from .base import PayloadBase
from .registry import register_payload

if TYPE_CHECKING:
    from ramses_rf.messages.base import Message

# ----------------------------------------------------------------------


@register_payload(Code._3220)
class OpenThermMsgPayload(PayloadBase):
    """Master payload dispatcher for Opcode 3220.

    Dispatches OpenTherm message binary payloads to 4-byte or 5-byte
    variant sub-dataclasses based on payload length.

    :param opentherm_index: OpenTherm index byte (0 for single OTB).
    :type opentherm_index: int
    :param msg_id: OpenTherm Data ID byte (0-255).
    :type msg_id: int
    :param msg_type: OpenTherm message type classification (0-7).
    :type msg_type: int
    :param raw_value: Raw 2-byte OpenTherm data value bytes.
    :type raw_value: bytes

    Domain Notes & Sample Packet Logs:
      # RQs have a context: msg_id and data_id.
      # Note: 0x47AB/0x1980 are bogus echos for some unsupported msg_ids —
      # they are filtered per msg_id in decode_frame (_INVALID_F8_8_VALUES).
      # NOTE: Unknown-DataId isn't an invalid payload & is useful to train the OTB device
      # 2021-11-05T06:25:20.669382 066 RP --- 10:023327 18:131597 --:------ 3220 005 00C01307C0
      # 2021-11-05T06:35:20.721228 066 RP --- 10:023327 18:131597 --:------ 3220 005 0040130059
      # 2021-12-06T06:35:55.949502 071 RP --- 10:051349 18:135447 --:------ 3220 005 00C0130ECC
    """

    VARIANTS: ClassVar[tuple[type[PayloadBase], ...]] = ()

    opentherm_index: int
    msg_id: int
    msg_type: int
    raw_value: bytes

    @classmethod
    def create(
        cls,
        msg_id: int = 0,
        msg_type: int = 0,
        raw_value: bytes = b"\x00\x00",
        opentherm_index: int | None = None,
    ) -> OpenThermMsg4BPayload | OpenThermMsg5BPayload:
        """Construct OpenThermMsg payload variant dynamically from arguments.

        :param msg_id: OpenTherm Data ID byte (0-255).
        :type msg_id: int
        :param msg_type: OpenTherm message type classification (0-7).
        :type msg_type: int
        :param raw_value: Raw 2-byte data value bytes.
        :type raw_value: bytes
        :param opentherm_index: Optional OpenTherm index for 5-byte variants.
        :type opentherm_index: int | None
        :returns: Instantiated variant payload instance.
        :rtype: OpenThermMsg4BPayload | OpenThermMsg5BPayload
        """
        if opentherm_index is not None:
            return OpenThermMsg5BPayload(
                opentherm_index=opentherm_index,
                msg_id=msg_id,
                msg_type=msg_type,
                raw_value=raw_value,
            )
        return OpenThermMsg4BPayload(
            opentherm_index=0,
            msg_id=msg_id,
            msg_type=msg_type,
            raw_value=raw_value,
        )

    def _header_byte(self) -> int:
        """Compute the 1-byte OpenTherm header with parity bit.

        :returns: Formatted 1-byte header integer with parity bit.
        :rtype: int
        """
        m_type = getattr(self, "msg_type", 0)
        m_id = getattr(self, "msg_id", 0)
        r_val = getattr(self, "raw_value", b"\x00\x00")
        header_byte = (m_type & 0x07) << 4
        frame_bytes = struct.pack(
            ">BB2s",
            header_byte & 0x7F,
            m_id,
            r_val[:2],
        )
        (frame_val,) = struct.unpack(">I", frame_bytes)
        if parity(frame_val) == 1:
            header_byte |= 0x80
        return header_byte

    def to_dict(self, msg: Message | None = None) -> PayloadT:
        """Convert OpenTherm message payload to pre-0.60 dictionary layout.

        :param msg: Optional message context object.
        :type msg: Any
        :returns: Decoded OpenTherm message dictionary.
        :rtype: PayloadT
        """
        m_id = getattr(self, "msg_id", 0)
        m_type = getattr(self, "msg_type", 0)
        r_val = getattr(self, "raw_value", b"\x00\x00")
        frame_bytes = struct.pack(
            ">BB2s",
            self._header_byte(),
            m_id,
            r_val[:2],
        )
        frame_hex = frame_bytes.hex().upper()

        try:
            ot_type, ot_id, ot_val, ot_schema = decode_frame(frame_hex)
            result: PayloadT = {
                SZ_MSG_ID: ot_id,
                SZ_MSG_TYPE: str(ot_type),
                SZ_MSG_NAME: ot_val.pop(SZ_MSG_NAME, None),
            }
            if str(ot_type) not in ("Read-Ack", "Write-Ack", "Write-Data"):
                for k in [k for k, v in ot_val.items() if v is None]:
                    ot_val.pop(k, None)
            result.update(ot_val)

            if ot_schema:
                result[SZ_DESCRIPTION] = ot_schema.get(EN)
            return result

        except (ValueError, TypeError, KeyError):
            return {
                SZ_MESSAGE_ID: m_id,
                SZ_MSG_TYPE: m_type,
                SZ_RAW_VALUE: r_val.hex().upper(),
            }

    @classmethod
    def from_bytes(
        cls, raw_data: bytes
    ) -> OpenThermMsg4BPayload | OpenThermMsg5BPayload:
        """Unpack binary payload, dispatching by length.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked concrete variant sub-dataclass instance.
        :rtype: OpenThermMsg4BPayload | OpenThermMsg5BPayload
        :raises ValueError: If raw_data length is less than 4 bytes.
        """
        if len(raw_data) < 4:
            raise ValueError(
                f"Invalid payload length for 3220: {len(raw_data)}"
            )
        if len(raw_data) >= 5:
            return OpenThermMsg5BPayload.from_bytes(raw_data)
        return OpenThermMsg4BPayload.from_bytes(raw_data)

    def to_bytes(self) -> bytes:
        """Pack payload base default method.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        :raises NotImplementedError: Master dispatcher must dispatch to
            variant sub-dataclass.
        """
        raise NotImplementedError("Use concrete variant sub-dataclass")


@dataclass(frozen=True, slots=True)
class OpenThermMsg4BPayload(OpenThermMsgPayload):
    """4-byte OpenTherm message payload (Opcode 3220).

    4-byte OpenTherm binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Header / MsgType & Flags     : 00
      +1       B      1B   Message ID (uint8)           : 00
      +2       2s     2B   Raw Value bytes              : 00 00
      --------------------------------------------------------------
      Field-spaced hex : 00 00 0000
      Payload hex      : 00000000

    :param msg_id: OpenTherm Data ID byte (0-255).
    :type msg_id: int
    :param msg_type: OpenTherm message type classification (0-7).
    :type msg_type: int
    :param raw_value: Raw 2-byte OpenTherm data value bytes.
    :type raw_value: bytes
    :param opentherm_index: OpenTherm index (defaults to 0 for 4B frames).
    :type opentherm_index: int
    """

    _STRUCT_FMT: ClassVar[str] = ">BB2s"

    msg_id: int
    msg_type: int
    raw_value: bytes
    opentherm_index: int = 0

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 4-byte OpenTherm message binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked OpenThermMsg4BPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 4 bytes.
        """
        if len(raw_data) < 4:
            raise ValueError(
                f"Invalid payload length for OpenThermMsg4BPayload: {len(raw_data)}"
            )
        header_byte, m_id, raw_val = struct.unpack_from(
            cls._STRUCT_FMT, raw_data, 0
        )
        m_type = (header_byte >> 4) & 0x07
        return cls(
            opentherm_index=0, msg_id=m_id, msg_type=m_type, raw_value=raw_val
        )

    def to_bytes(self) -> bytes:
        """Pack 4-byte OpenTherm message binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        return struct.pack(
            self._STRUCT_FMT,
            self._header_byte(),
            self.msg_id,
            self.raw_value[:2],
        )


@dataclass(frozen=True, slots=True)
class OpenThermMsg5BPayload(OpenThermMsgPayload):
    """5-byte OpenTherm message payload (Opcode 3220).

    5-byte OpenTherm binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   OpenTherm Index (uint8)      : 00
      +1       B      1B   Header / MsgType & Flags     : 00
      +2       B      1B   Message ID (uint8)           : 00
      +3       2s     2B   Raw Value bytes              : 00 00
      --------------------------------------------------------------
      Field-spaced hex : 00 00 00 0000
      Payload hex      : 0000000000

    :param opentherm_index: OpenTherm gateway index byte (0-255).
    :type opentherm_index: int
    :param msg_id: OpenTherm Data ID byte (0-255).
    :type msg_id: int
    :param msg_type: OpenTherm message type classification (0-7).
    :type msg_type: int
    :param raw_value: Raw 2-byte OpenTherm data value bytes.
    :type raw_value: bytes
    """

    _STRUCT_FMT: ClassVar[str] = ">BBB2s"

    opentherm_index: int
    msg_id: int
    msg_type: int
    raw_value: bytes

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 5-byte OpenTherm message binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked OpenThermMsg5BPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 5 bytes.
        """
        if len(raw_data) < 5:
            raise ValueError(
                f"Invalid payload length for OpenThermMsg5BPayload: {len(raw_data)}"
            )
        opentherm_index, header_byte, m_id, raw_val = struct.unpack_from(
            cls._STRUCT_FMT, raw_data, 0
        )
        m_type = (header_byte >> 4) & 0x07
        return cls(
            opentherm_index=opentherm_index,
            msg_id=m_id,
            msg_type=m_type,
            raw_value=raw_val,
        )

    def to_bytes(self) -> bytes:
        """Pack 5-byte OpenTherm message binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        return struct.pack(
            self._STRUCT_FMT,
            self.opentherm_index,
            self._header_byte(),
            self.msg_id,
            self.raw_value[:2],
        )


# Update VARIANTS property after variants are defined
OpenThermMsgPayload.VARIANTS = (
    OpenThermMsg4BPayload,
    OpenThermMsg5BPayload,
)


# ----------------------------------------------------------------------


@register_payload(Code._0150)
@dataclass(frozen=True, slots=True)
class OpenThermStatusPayload(PayloadBase):
    """OpenTherm status payload (Opcode 0150).

    2-byte OpenTherm Status binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Master Status Flags (uint8)  : 01
      +1       B      1B   Slave Status Flags (uint8)   : 00
      --------------------------------------------------------------
      Field-spaced hex : 01 00
      Payload hex      : 0100

    :param master_status: Master status flags byte.
    :type master_status: int
    :param slave_status: Slave status flags byte.
    :type slave_status: int
    """

    _STRUCT_FMT: ClassVar[str] = ">BB"

    master_status: int
    slave_status: int

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack OpenTherm status binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked OpenThermStatusPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 2 bytes.
        """
        if len(raw_data) < 2:
            raise ValueError(
                f"Invalid payload length for 0150: {len(raw_data)}"
            )
        master, slave = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(master_status=master, slave_status=slave)

    def to_bytes(self) -> bytes:
        """Pack OpenTherm status data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        return struct.pack(
            self._STRUCT_FMT, self.master_status, self.slave_status
        )


# ----------------------------------------------------------------------


@register_payload(Code._1098)
@dataclass(frozen=True, slots=True)
class OpenThermSetpointPayload(PayloadBase):
    """OpenTherm control setpoint payload (Opcode 1098).

    2-byte OpenTherm Setpoint binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       h      2B   Setpoint Temperature (int16*100): 13 88 (50.00°C)
      --------------------------------------------------------------
      Field-spaced hex : 1388
      Payload hex      : 1388

    :param setpoint_temp: Target control setpoint temperature in °C.
    :type setpoint_temp: float
    """

    _STRUCT_FMT: ClassVar[str] = ">h"

    setpoint_temp: float

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack OpenTherm setpoint binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked OpenThermSetpointPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 2 bytes.
        """
        if len(raw_data) < 2:
            raise ValueError(
                f"Invalid payload length for 1098: {len(raw_data)}"
            )
        (sp_raw,) = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(setpoint_temp=sp_raw / 100.0)

    def to_bytes(self) -> bytes:
        """Pack OpenTherm setpoint data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        sp_raw = int(round(self.setpoint_temp * 100.0))
        return struct.pack(self._STRUCT_FMT, sp_raw)


# ----------------------------------------------------------------------


@register_payload(Code._10B0)
@dataclass(frozen=True, slots=True)
class OpenThermTemperaturePayload(PayloadBase):
    """OpenTherm boiler water temperature payload (Opcode 10B0).

    2-byte OpenTherm Temperature binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       h      2B   Water Temperature (int16*100): 17 70 (60.00°C)
      --------------------------------------------------------------
      Field-spaced hex : 1770
      Payload hex      : 1770

    :param temperature: Water temperature reading in °C.
    :type temperature: float
    """

    _STRUCT_FMT: ClassVar[str] = ">h"

    temperature: float

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack OpenTherm temperature binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked OpenThermTemperaturePayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 2 bytes.
        """
        if len(raw_data) < 2:
            raise ValueError(
                f"Invalid payload length for 10B0: {len(raw_data)}"
            )
        (temp_raw,) = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(temperature=temp_raw / 100.0)

    def to_bytes(self) -> bytes:
        """Pack OpenTherm temperature data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        temp_raw = int(round(self.temperature * 100.0))
        return struct.pack(self._STRUCT_FMT, temp_raw)


# ----------------------------------------------------------------------


@register_payload(Code._1FD0)
@dataclass(frozen=True, slots=True)
class OpenThermDiagnosticsPayload(PayloadBase):
    """OpenTherm diagnostics payload (Opcode 1FD0).

    2-byte OpenTherm Diagnostics binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Diagnostic Code (uint8)      : 00
      +1       B      1B   Diagnostic Flags (uint8)     : 00
      --------------------------------------------------------------
      Field-spaced hex : 00 00
      Payload hex      : 0000

    :param diagnostic_code: OpenTherm diagnostic code.
    :type diagnostic_code: int
    :param flags: Diagnostic flags.
    :type flags: int
    """

    _STRUCT_FMT: ClassVar[str] = ">BB"

    diagnostic_code: int
    flags: int

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack OpenTherm diagnostics binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked OpenThermDiagnosticsPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 2 bytes.
        """
        if len(raw_data) < 2:
            raise ValueError(
                f"Invalid payload length for 1FD0: {len(raw_data)}"
            )
        code, flg = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(diagnostic_code=code, flags=flg)

    def to_bytes(self) -> bytes:
        """Pack OpenTherm diagnostics data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        return struct.pack(self._STRUCT_FMT, self.diagnostic_code, self.flags)

    def to_dict(self, msg: Message | None = None) -> PayloadT:
        """Convert OpenTherm diagnostics payload to pre-0.60 dictionary layout.

        :param msg: Optional message context object.
        :type msg: Any
        :returns: Decoded OpenTherm diagnostics dictionary.
        :rtype: PayloadT
        """
        return {
            SZ_DIAGNOSTIC_CODE: self.diagnostic_code,
            SZ_FLAGS: self.flags,
        }


# ----------------------------------------------------------------------


@register_payload(Code._1FD4)
class OpenThermFaultFlagsPayload(PayloadBase):
    """Master payload dispatcher and base class for Opcode 1FD4.

    Dispatches OpenTherm slave ticker / fault flag binary payloads to
    2-byte or 3-byte variant sub-dataclasses based on payload length.

    Protocol Notes:
      # Spider Autotemp, slave 'ticker': 2/min for R8810, every ~210 sec for R8820.

    Sample Packet Logs:
      # 2021-10-11T15:28:16.255433 083  I --- --:------ --:------ 10:051349 1FD4 003 002E22
      # 2021-10-23T11:20:38.641748 045  I --- 10:040239 --:------ 10:040239 1FD4 003 00AF65
      # 2021-10-24T21:00:01.000000 ---  I --- --:------ --:------ 10:052644 1FD4 003 00AAB5
    """

    VARIANTS: ClassVar[tuple[type[PayloadBase], ...]] = ()

    @classmethod
    def create(
        cls,
        fault_code: int = 0,
        flags: int = 0,
        hdr: int | None = None,
    ) -> OpenThermFaultFlags2BPayload | OpenThermFaultFlags3BPayload:
        """Construct OpenThermFaultFlags payload variant dynamically from arguments.

        :param fault_code: Fault code identifier byte (0-255).
        :type fault_code: int
        :param flags: Fault flags byte (0-255).
        :type flags: int
        :param hdr: Optional header/domain index byte for 3-byte variants.
        :type hdr: int | None
        :returns: Instantiated variant payload instance.
        :rtype: OpenThermFaultFlags2BPayload | OpenThermFaultFlags3BPayload
        """
        if hdr is not None:
            return OpenThermFaultFlags3BPayload(
                hdr=hdr, fault_code=fault_code, flags=flags
            )
        return OpenThermFaultFlags2BPayload(fault_code=fault_code, flags=flags)

    def to_dict(self, msg: Message | None = None) -> PayloadT:
        """Convert OpenTherm fault flags payload to pre-0.60 dictionary layout.

        :param msg: Optional message context object.
        :type msg: Any
        :returns: Decoded OpenTherm fault flags dictionary.
        :rtype: PayloadT
        """
        f_code = getattr(self, "fault_code", 0)
        flg = getattr(self, "flags", 0)
        return {SZ_TICKER: (f_code << 8) | flg}

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> PayloadBase:
        """Unpack binary payload, dispatching by length.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked concrete variant sub-dataclass instance.
        :rtype: PayloadBase
        :raises ValueError: If raw_data length is less than 2 bytes.
        """
        if len(raw_data) < 2:
            raise ValueError(
                f"Invalid payload length for 1FD4: {len(raw_data)}"
            )
        if len(raw_data) >= 3:
            return OpenThermFaultFlags3BPayload.from_bytes(raw_data)
        return OpenThermFaultFlags2BPayload.from_bytes(raw_data)

    def to_bytes(self) -> bytes:
        """Pack payload base default method.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        :raises NotImplementedError: Master dispatcher must dispatch to sub-dataclass.
        """
        raise NotImplementedError("Use concrete variant sub-dataclass")


@dataclass(frozen=True, slots=True)
class OpenThermFaultFlags2BPayload(OpenThermFaultFlagsPayload):
    """2-byte OpenTherm fault flags payload (Opcode 1FD4).

    2-byte OpenTherm Fault Flags binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Fault Code (uint8)           : 00
      +1       B      1B   Fault Flags (uint8)          : 00
      --------------------------------------------------------------
      Field-spaced hex : 00 00
      Payload hex      : 0000

    :param fault_code: Fault code identifier byte (0-255).
    :type fault_code: int
    :param flags: Fault and status flags byte (0-255).
    :type flags: int
    """

    _STRUCT_FMT: ClassVar[str] = ">BB"

    fault_code: int
    flags: int

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 2-byte OpenTherm fault flags binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked OpenThermFaultFlags2BPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 2 bytes.
        """
        if len(raw_data) < 2:
            msg = f"Invalid payload length for OpenThermFaultFlags2BPayload: {len(raw_data)}"
            raise ValueError(msg)
        code, flg = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(fault_code=code, flags=flg)

    def to_bytes(self) -> bytes:
        """Pack 2-byte OpenTherm fault flags binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        return struct.pack(self._STRUCT_FMT, self.fault_code, self.flags)


@dataclass(frozen=True, slots=True)
class OpenThermFaultFlags3BPayload(OpenThermFaultFlagsPayload):
    """3-byte OpenTherm fault flags payload (Opcode 1FD4).

    3-byte OpenTherm Fault Flags binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Header / Domain              : 00
      +1       B      1B   Fault Code (uint8)           : 00
      +2       B      1B   Fault Flags (uint8)          : 00
      --------------------------------------------------------------
      Field-spaced hex : 00 00 00
      Payload hex      : 000000

    :param hdr: Header / Domain identifier byte (0-255).
    :type hdr: int
    :param fault_code: Fault code identifier byte (0-255).
    :type fault_code: int
    :param flags: Fault and status flags byte (0-255).
    :type flags: int
    """

    _STRUCT_FMT: ClassVar[str] = ">BBB"

    hdr: int
    fault_code: int
    flags: int

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 3-byte OpenTherm fault flags binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked OpenThermFaultFlags3BPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 3 bytes.
        """
        if len(raw_data) < 3:
            msg = f"Invalid payload length for OpenThermFaultFlags3BPayload: {len(raw_data)}"
            raise ValueError(msg)
        hdr, code, flg = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(hdr=hdr, fault_code=code, flags=flg)

    def to_bytes(self) -> bytes:
        """Pack 3-byte OpenTherm fault flags binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        return struct.pack(
            self._STRUCT_FMT, self.hdr, self.fault_code, self.flags
        )


# Update VARIANTS property after variants are defined
OpenThermFaultFlagsPayload.VARIANTS = (
    OpenThermFaultFlags2BPayload,
    OpenThermFaultFlags3BPayload,
)


# ----------------------------------------------------------------------


@register_payload(Code._2400)
@dataclass(frozen=True, slots=True)
class OpenThermConfigPayload(PayloadBase):
    """OpenTherm configuration parameter payload (Opcode 2400).

    2-byte OpenTherm Configuration binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Parameter Index (uint8)      : 00
      +1       B      1B   Parameter Value (uint8)      : 64 (100)
      --------------------------------------------------------------
      Field-spaced hex : 00 64
      Payload hex      : 0064

    :param parameter_index: Parameter index byte.
    :type parameter_index: int
    :param parameter_value: Parameter value byte.
    :type parameter_value: int

    Sample Packet Logs:
    # RP --- 32:155617 18:005904 --:------ 2400 045 00001111-1010929292921110101020110010000080100010100000009191111191910011119191111111111100  # Orcon FAN
    # RP --- 10:048122 18:006402 --:------ 2400 004 0000000F
    """

    _STRUCT_FMT: ClassVar[str] = ">BB"

    parameter_index: int
    parameter_value: int

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack OpenTherm configuration binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked OpenThermConfigPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 2 bytes.
        """
        if len(raw_data) < 2:
            raise ValueError(
                f"Invalid payload length for 2400: {len(raw_data)}"
            )
        index, value = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(parameter_index=index, parameter_value=value)

    def to_bytes(self) -> bytes:
        """Pack OpenTherm configuration data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        return struct.pack(
            self._STRUCT_FMT, self.parameter_index, self.parameter_value
        )


# ----------------------------------------------------------------------


@register_payload(Code._2401)
@dataclass(frozen=True, slots=True)
class OpenThermParamsPayload(PayloadBase):
    """OpenTherm operational parameters payload (Opcode 2401).

    2-byte OpenTherm Parameters binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Parameter Index (uint8)      : 00
      +1       B      1B   Parameter Value (uint8)      : 01
      --------------------------------------------------------------
      Field-spaced hex : 00 01
      Payload hex      : 0001

    :param parameter_index: Parameter index byte.
    :type parameter_index: int
    :param parameter_value: Parameter value byte.
    :type parameter_value: int
    """

    _STRUCT_FMT: ClassVar[str] = ">BB"

    parameter_index: int
    parameter_value: int

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack OpenTherm parameters binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked OpenThermParamsPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 2 bytes.
        """
        if len(raw_data) < 2:
            raise ValueError(
                f"Invalid payload length for 2401: {len(raw_data)}"
            )
        index, value = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(parameter_index=index, parameter_value=value)

    def to_bytes(self) -> bytes:
        """Pack OpenTherm parameters data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        return struct.pack(
            self._STRUCT_FMT, self.parameter_index, self.parameter_value
        )

    def to_dict(self, msg: Message | None = None) -> PayloadT:
        """Convert OpenTherm parameters payload to pre-0.60 dictionary layout.

        :param msg: Optional message context object.
        :type msg: Any
        :returns: Decoded OpenTherm parameters dictionary.
        :rtype: PayloadT
        """
        heat_demand = self.parameter_value / 200.0
        # 1.01 (raw uint8 202) represents 100% modulation in pre-0.60 OpenTherm packets
        if heat_demand == 1.01:
            heat_demand = 1.0
        return {SZ_HEAT_DEMAND: heat_demand}


# ----------------------------------------------------------------------


@register_payload(Code._2410)
@dataclass(frozen=True, slots=True)
class OpenThermCapacityPayload(PayloadBase):
    """OpenTherm capacity payload (Opcode 2410).

    2-byte OpenTherm Capacity binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Capacity Index (uint8)       : 00
      +1       B      1B   Capacity Value (uint8)       : 64 (100)
      --------------------------------------------------------------
      Field-spaced hex : 00 64
      Payload hex      : 0064

    :param capacity_index: Capacity index byte.
    :type capacity_index: int
    :param capacity_value: Capacity value byte.
    :type capacity_value: int

    Sample Packet Logs:
    # RP --- 10:048122 18:006402 --:------ 2410 020 00-00000000-00000000-00000001-00000001-00000C  # OTB
    # RP --- 32:155617 18:005904 --:------ 2410 020 00-00003EE8-00000000-FFFFFFFF-00000000-1002A6  # Orcon Fan
    """

    _STRUCT_FMT: ClassVar[str] = ">BB"

    capacity_index: int
    capacity_value: int

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack OpenTherm capacity binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked OpenThermCapacityPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 2 bytes.
        """
        if len(raw_data) < 2:
            raise ValueError(
                f"Invalid payload length for 2410: {len(raw_data)}"
            )
        index, value = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(capacity_index=index, capacity_value=value)

    def to_bytes(self) -> bytes:
        """Pack OpenTherm capacity data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        return struct.pack(
            self._STRUCT_FMT, self.capacity_index, self.capacity_value
        )


# ----------------------------------------------------------------------


@register_payload(Code._2420)
@dataclass(frozen=True, slots=True)
class OpenThermModulationPayload(PayloadBase):
    """OpenTherm modulation payload (Opcode 2420).

    2-byte OpenTherm Modulation binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Modulation Index (uint8)     : 00
      +1       B      1B   Modulation Percent (uint8)   : C8 (100%)
      --------------------------------------------------------------
      Field-spaced hex : 00 C8
      Payload hex      : 00C8

    :param modulation_index: Modulation index byte.
    :type modulation_index: int
    :param mod_percent: Modulation percentage byte.
    :type mod_percent: int
    """

    _STRUCT_FMT: ClassVar[str] = ">BB"

    modulation_index: int
    mod_percent: int

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack OpenTherm modulation binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked OpenThermModulationPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 2 bytes.
        """
        if len(raw_data) < 2:
            raise ValueError(
                f"Invalid payload length for 2420: {len(raw_data)}"
            )
        index, pct = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(modulation_index=index, mod_percent=pct)

    def to_bytes(self) -> bytes:
        """Pack OpenTherm modulation data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        return struct.pack(
            self._STRUCT_FMT, self.modulation_index, self.mod_percent
        )


# ----------------------------------------------------------------------


@register_payload(Code._3221)
@dataclass(frozen=True, slots=True)
class OpenThermFrameExPayload(PayloadBase):
    """OpenTherm extended frame payload (Opcode 3221).

    2-byte OpenTherm Frame Ex binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Frame Code (uint8)           : 00
      +1       B      1B   Frame Flags (uint8)          : 00
      --------------------------------------------------------------
      Field-spaced hex : 00 00
      Payload hex      : 0000

    :param frame_code: Frame code byte.
    :type frame_code: int
    :param flags: Frame flags byte.
    :type flags: int

    Sample Packet Logs:
    # RP --- 10:052644 18:198151 --:------ 3221 002 000F
    # RP --- 10:048122 18:006402 --:------ 3221 002 0000
    # RP --- 32:155617 18:005904 --:------ 3221 002 000A
    """

    _STRUCT_FMT: ClassVar[str] = ">BB"

    frame_code: int
    flags: int

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack OpenTherm extended frame binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked OpenThermFrameExPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 2 bytes.
        """
        if len(raw_data) < 2:
            raise ValueError(
                f"Invalid payload length for 3221: {len(raw_data)}"
            )
        code, flg = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(frame_code=code, flags=flg)

    def to_bytes(self) -> bytes:
        """Pack OpenTherm extended frame data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        return struct.pack(self._STRUCT_FMT, self.frame_code, self.flags)

    def to_dict(self, msg: Message | None = None) -> PayloadT:
        """Convert OpenTherm extended frame payload to pre-0.60 dictionary layout.

        :param msg: Optional message context object.
        :type msg: Any
        :returns: Decoded OpenTherm extended frame dictionary.
        :rtype: PayloadT
        """
        if self.frame_code == 0 and self.flags == 0:
            return {SZ_VALUE: 0}
        return {SZ_FRAME_CODE: self.frame_code, SZ_FLAGS: self.flags}


# ----------------------------------------------------------------------


@register_payload(Code._3223)
@dataclass(frozen=True, slots=True)
class OpenThermBridgeStatusPayload(PayloadBase):
    """OpenTherm bridge operational status payload (Opcode 3223).

    2-byte OpenTherm Bridge Status binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Bridge Status Code (uint8)   : 00
      +1       B      1B   Bridge Flags (uint8)         : 00
      --------------------------------------------------------------
      Field-spaced hex : 00 00
      Payload hex      : 0000

    :param status_code: Bridge status code integer.
    :type status_code: int
    :param flags: Bridge flags byte.
    :type flags: int
    """

    _STRUCT_FMT: ClassVar[str] = ">BB"

    status_code: int
    flags: int

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack OpenTherm bridge status binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked OpenThermBridgeStatusPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 2 bytes.
        """
        if len(raw_data) < 2:
            raise ValueError(
                f"Invalid payload length for 3223: {len(raw_data)}"
            )
        code, flg = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(status_code=code, flags=flg)

    def to_bytes(self) -> bytes:
        """Pack OpenTherm bridge status data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        return struct.pack(self._STRUCT_FMT, self.status_code, self.flags)

    def to_dict(self, msg: Message | None = None) -> PayloadT:
        """Convert OpenTherm bridge status payload to pre-0.60 dictionary layout.

        :param msg: Optional message context object.
        :type msg: Any
        :returns: Decoded OpenTherm bridge status dictionary.
        :rtype: PayloadT
        """
        if self.status_code == 0 and self.flags == 0:
            return {SZ_VALUE: 0}
        return {SZ_STATUS_CODE: self.status_code, SZ_FLAGS: self.flags}


# ----------------------------------------------------------------------


@register_payload(Code._3210)
@dataclass(frozen=True, slots=True)
class ReturnTempPayload(PayloadBase):
    """OpenTherm boiler return water temperature payload (Opcode 3210).

    3-byte Return Temperature binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Header / Index               : 00
      +1       h      2B   Return Temp (int16*100)      : 13 88 (50.00°C)
      --------------------------------------------------------------
      Field-spaced hex : 00 1388
      Payload hex      : 001388

    :param return_temp: Return water temperature in °C.
    :type return_temp: float
    """

    _STRUCT_FMT: ClassVar[str] = ">Bh"

    return_temp: float | None

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack return temperature binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked ReturnTempPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 3 bytes.
        """
        if len(raw_data) < 3:
            raise ValueError(
                f"Invalid payload length for 3210: {len(raw_data)}"
            )
        _hdr, temp_raw = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        t_val = None if temp_raw in (0x31FF, 0x7FFF) else temp_raw / 100.0
        return cls(return_temp=t_val)

    def to_bytes(self) -> bytes:
        """Pack return temperature data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        if self.return_temp is None:
            t_raw = 0x7FFF
        else:
            t_raw = int(round(self.return_temp * 100.0))
        return struct.pack(self._STRUCT_FMT, 0, t_raw)

    def to_dict(self, msg: Message | None = None) -> PayloadT:
        """Convert return temperature payload to pre-0.60 dictionary layout.

        :param msg: Optional message context object.
        :type msg: Any
        :returns: Decoded temperature dictionary.
        :rtype: PayloadT
        """
        return {SZ_TEMPERATURE: self.return_temp}
