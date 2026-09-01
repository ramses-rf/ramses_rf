"""RAMSES RF - System, Clock and Binding payload dataclasses.

This module contains strongly-typed dataclass representations for system clock,
device binding, fault log, and gateway heartbeat packet payloads.
"""

import re
import struct
from dataclasses import dataclass
from typing import Any, ClassVar, Self

from ramses_rf.const import (
    DEV_ROLE_MAP,
    SYS_MODE_MAP,
    SZ_DEVICE_CLASS,
    SZ_DEVICE_ID,
    SZ_DOMAIN_INDEX,
    SZ_FAULT_STATE,
    SZ_FAULT_TYPE,
    SZ_IS_DAYLIGHT_SAVING,
    SZ_LOG_ENTRY,
    SZ_LOG_INDEX,
    SZ_SYSTEM_MODE,
    SZ_TIMESTAMP,
    SZ_UNTIL,
    SZ_ZONE_INDEX,
    Code,
)
from ramses_rf.enums import DevRole
from ramses_rf.payloads.helpers import parse_fault_log_entry
from ramses_tx.const import F9, FA, FC, RQ, FaultDeviceClass, Verb
from ramses_tx.helpers import hex_from_dtm, hex_to_date, hex_to_dtm, hex_to_dts

from .base import PayloadBase
from .registry import register_payload

# ----------------------------------------------------------------------


@register_payload(Code._0001)
@dataclass(frozen=True, slots=True)
class SystemClockPayload(PayloadBase):
    """System clock payload (Opcode 0001).

    5-byte System Clock binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Header / Domain (uint8)      : 00
      +1       B      1B   Hour (uint8, 0-23)           : 0C (12 hours)
      +2       B      1B   Minute (uint8, 0-59)         : 1E (30 mins)
      +3       B      1B   Second (uint8, 0-59)         : 00 (0 secs)
      +4       B      1B   Day of Week (uint8, 1-7)     : 01 (Monday)
      --------------------------------------------------------------
      Field-spaced hex : 00 0C 1E 00 01
      Payload hex      : 000C1E0001

    :param hour: Hour integer (0-23).
    :type hour: int
    :param minute: Minute integer (0-59).
    :type minute: int
    :param second: Second integer (0-59).
    :type second: int
    :param day_of_week: Day of week integer (1-7).
    :type day_of_week: int

    Protocol Notes:
      # When in test mode, a 12: will send a W ?every 6 seconds.
      # Sent by a CTL before an rf_check.
      # Sent by a THM when in signal strength test mode (0505, except 1st packet).
      # Loopback (not Tx'd) by a HGI80 whenever its button is pressed.

    Sample Packet Logs:
    # .I --- 30:082155 30:082155 --:------ 0001 005 0005080007  # every ~10:00
    # .I --- 34:021943 34:021943 --:------ 0001 005 000D000003  # every ~20:00
    # 12:39:56.099 061  W --- 12:010740 --:------ 12:010740 0001 005 0000000501
    # 13:48:38.518 080  W --- 12:010740 --:------ 12:010740 0001 005 0000000501
    # 13:48:45.518 074  W --- 12:010740 --:------ 12:010740 0001 005 0000000501
    # 16:53:34.635 058  W --- 04:166090 --:------ 01:032820 0001 005 0100000505
    # 00:22:41.540 ---  I --- --:------ --:------ --:------ 0001 005 00FFFF02FF
    # 00:22:41.757 ---  I --- --:------ --:------ --:------ 0001 005 00FFFF0200
    # 00:22:43.320 ---  I --- --:------ --:------ --:------ 0001 005 00FFFF02FF
    # 00:22:43.415 ---  I --- --:------ --:------ --:------ 0001 005 00FFFF0200
    """

    _STRUCT_FMT: ClassVar[str] = ">BBBBB"

    hour: int
    minute: int
    second: int
    day_of_week: int
    _raw_bytes: bytes | None = None

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack system clock binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked SystemClockPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 5 bytes.
        """
        if len(raw_data) < 5:
            raise ValueError(
                f"Invalid payload length for 0001: {len(raw_data)}"
            )
        if len(raw_data) >= 8:
            return cls(
                hour=0, minute=0, second=0, day_of_week=0, _raw_bytes=raw_data
            )
        _hdr, hr, mn, sc, dow = struct.unpack_from(
            cls._STRUCT_FMT, raw_data, 0
        )
        return cls(
            hour=hr,
            minute=mn,
            second=sc,
            day_of_week=dow,
        )

    def to_bytes(self) -> bytes:
        """Pack system clock data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        if self._raw_bytes is not None:
            return self._raw_bytes
        return struct.pack(
            self._STRUCT_FMT,
            0,
            self.hour,
            self.minute,
            self.second,
            self.day_of_week,
        )

    def to_dict(self, msg: Any = None) -> dict[str, Any]:
        """Convert system clock payload to legacy dictionary format.

        :param msg: Optional message context object.
        :type msg: Any
        :returns: Decoded system clock dictionary.
        :rtype: dict[str, Any]
        """
        if self._raw_bytes is not None and len(self._raw_bytes) >= 8:
            b = self._raw_bytes
            return {
                "slot_num": f"{b[3]:02X}",
                "param_num": f"{b[5]:02X}",
                "next_slot_num": f"{b[6]:02X}",
                "boolean_14": bool(b[7] != 0),
                "payload": b.hex().upper(),
            }
        return {
            "hour": self.hour,
            "minute": self.minute,
            "second": self.second,
            "day_of_week": self.day_of_week,
        }


# ----------------------------------------------------------------------


@register_payload(Code._0002)
@dataclass(frozen=True, slots=True)
class SystemDatePayload(PayloadBase):
    """System date payload (Opcode 0002).

    4-byte System Date binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Header / Domain              : 00
      +1       B      1B   Year (uint8, YY past 2000)   : 1A (2026)
      +2       B      1B   Month (uint8, 1-12)          : 08 (August)
      +3       B      1B   Day (uint8, 1-31)            : 07 (7th)
      --------------------------------------------------------------
      Field-spaced hex : 00 1A 08 07
      Payload hex      : 001A0807

    :param year: Year past 2000 integer.
    :type year: int
    :param month: Month integer (1-12).
    :type month: int
    :param day: Day integer (1-31).
    :type day: int
    """

    _STRUCT_FMT: ClassVar[str] = ">BBBB"

    year: int
    month: int
    day: int

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack system date binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked SystemDatePayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 4 bytes.
        """
        if len(raw_data) < 4:
            raise ValueError(
                f"Invalid payload length for 0002: {len(raw_data)}"
            )
        _hdr, yr, mo, dy = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(year=yr, month=mo, day=dy)

    def to_bytes(self) -> bytes:
        """Pack system date data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        return struct.pack(
            self._STRUCT_FMT, 0, self.year, self.month, self.day
        )


# ----------------------------------------------------------------------


@register_payload(Code._0006)
class SystemChangeCounterPayload(PayloadBase):
    """Master payload dispatcher for system change counter (Opcode 0006).

    Dispatches system change counter payloads to 3-byte or 4-byte
    variant sub-dataclasses based on payload length.
    """

    VARIANTS: ClassVar[tuple[type[PayloadBase], ...]] = ()

    change_counter: int | None

    def __new__(
        cls,
        change_counter: int | None = None,
        hdr: int = 0,
        h1: int = 0,
        h2: int = 5,
    ) -> "SystemChangeCounterPayload":
        """Construct SystemChangeCounter payload variant dynamically from arguments."""
        if cls is not SystemChangeCounterPayload:
            return super().__new__(cls)
        return SystemChangeCounter4BPayload(
            h1=h1, h2=h2, change_counter=change_counter
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert system change counter payload to legacy dictionary layout."""
        return {"change_counter": getattr(self, "change_counter", None)}

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> "SystemChangeCounterPayload":
        """Unpack system change counter binary payload, dispatching by length."""
        if len(raw_data) < 3:
            raise ValueError(
                f"Invalid payload length for 0006: {len(raw_data)}"
            )
        if len(raw_data) >= 4:
            return SystemChangeCounter4BPayload.from_bytes(raw_data)
        return SystemChangeCounter3BPayload.from_bytes(raw_data)

    def to_bytes(self) -> bytes:
        """Pack payload base default method.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        :raises NotImplementedError: Master dispatcher must dispatch to
            variant sub-dataclass.
        """
        raise NotImplementedError("Use concrete variant sub-dataclass")


@dataclass(frozen=True, slots=True)
class SystemChangeCounter3BPayload(SystemChangeCounterPayload):
    """3-byte system change counter payload (Opcode 0006).

    3-byte System Change Counter binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Header / Domain              : 00
      +1       H      2B   Change Counter (uint16)      : 00 01 (1 change)
      --------------------------------------------------------------
      Field-spaced hex : 00 0001
      Payload hex      : 000001

    Sample Packet Logs (Opcode 0009):
    # .I --- 23:100224 --:------ 23:100224 0009 003 0100FF  # 2-zone ST9520C
    # .I --- 10:040239 01:223036 --:------ 0009 003 000000
    # .I --- 01:145038 --:------ 01:145038 0009 006 FC01FFF901FF
    # .I --- 01:145038 --:------ 01:145038 0009 003 0700FF
    # .I --- --:------ --:------ 12:227486 0009 003 0000FF
    """

    _STRUCT_FMT: ClassVar[str] = ">BH"

    hdr: int
    change_counter: int | None

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 3-byte system change counter binary payload."""
        if len(raw_data) < 3:
            raise ValueError(
                f"Invalid payload length for SystemChangeCounter3BPayload: {len(raw_data)}"
            )
        hdr, counter_raw = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        counter_val = None if counter_raw == 0xFFFF else counter_raw
        return cls(hdr=hdr, change_counter=counter_val)

    def to_bytes(self) -> bytes:
        """Pack 3-byte system change counter binary payload."""
        counter_val = (
            0xFFFF if self.change_counter is None else self.change_counter
        )
        return struct.pack(self._STRUCT_FMT, self.hdr, counter_val)


@dataclass(frozen=True, slots=True)
class SystemChangeCounter4BPayload(SystemChangeCounterPayload):
    """4-byte system change counter payload (Opcode 0006).

    4-byte System Change Counter binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Header / Index               : 00
      +1       B      1B   Sub-Header / Flag            : 05
      +2       H      2B   Change Counter (uint16)      : 00 01 (1 change)
      --------------------------------------------------------------
      Field-spaced hex : 00 05 0001
      Payload hex      : 00050001
    """

    _STRUCT_FMT: ClassVar[str] = ">BBH"

    h1: int
    h2: int
    change_counter: int | None

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 4-byte system change counter binary payload."""
        if len(raw_data) < 4:
            raise ValueError(
                f"Invalid payload length for SystemChangeCounter4BPayload: {len(raw_data)}"
            )
        h1, h2, counter_raw = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        counter_val = None if counter_raw == 0xFFFF else counter_raw
        return cls(h1=h1, h2=h2, change_counter=counter_val)

    def to_bytes(self) -> bytes:
        """Pack 4-byte system change counter binary payload."""
        counter_val = (
            0xFFFF if self.change_counter is None else self.change_counter
        )
        return struct.pack(self._STRUCT_FMT, self.h1, self.h2, counter_val)


# Update VARIANTS property after variants are defined
SystemChangeCounterPayload.VARIANTS = (
    SystemChangeCounter3BPayload,
    SystemChangeCounter4BPayload,
)


# ----------------------------------------------------------------------


@register_payload(Code._000E)
@dataclass(frozen=True, slots=True)
class OemCodePayload(PayloadBase):
    """OEM code payload (Opcode 000E).

    2-byte OEM Code binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       2s     2B   OEM Code Payload (bytes)     : 00 01
      --------------------------------------------------------------
      Field-spaced hex : 00 01
      Payload hex      : 0001

    :param payload_hex: Raw payload hex string representation.
    :type payload_hex: str
    """

    _STRUCT_FMT: ClassVar[str] = ">2s"

    payload_hex: str

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack OEM code binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked OemCodePayload instance.
        :rtype: Self
        """
        return cls(payload_hex=raw_data.hex().upper())

    def to_bytes(self) -> bytes:
        """Pack OEM code data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        return bytes.fromhex(self.payload_hex)

    def to_dict(self, msg: Any = None) -> dict[str, Any]:
        """Convert OEM code payload to legacy dictionary format.

        :param msg: Optional message context object.
        :type msg: Any
        :returns: Decoded OEM code dictionary.
        :rtype: dict[str, Any]
        """
        return {"payload": self.payload_hex}


@dataclass(frozen=True, slots=True)
class SystemRolePayload(PayloadBase):
    """System role assignment payload.

    2-byte System Role binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Domain Index (uint8)         : 00
      +1       B      1B   Role Code (uint8)            : 01
      --------------------------------------------------------------
      Field-spaced hex : 00 01
      Payload hex      : 0001

    :param domain_index: System domain index byte.
    :type domain_index: int
    :param role_code: Device role classification code byte.
    :type role_code: int
    """

    _STRUCT_FMT: ClassVar[str] = ">BB"

    domain_index: int
    role_code: int

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack system role binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked SystemRolePayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 2 bytes.
        """
        if len(raw_data) < 2:
            raise ValueError(
                f"Invalid payload length for 000E: {len(raw_data)}"
            )
        dom, role = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(domain_index=dom, role_code=role)

    def to_bytes(self) -> bytes:
        """Pack system role data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        return struct.pack(self._STRUCT_FMT, self.domain_index, self.role_code)


# ----------------------------------------------------------------------


@register_payload(Code._0016)
@dataclass(frozen=True, slots=True)
class SystemFlagPayload(PayloadBase):
    """System status flags payload (Opcode 0016).

    2-byte System Flag binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Domain Index (uint8)         : 00
      +1       B      1B   Flag Value (uint8)           : 01
      --------------------------------------------------------------
      Field-spaced hex : 00 01
      Payload hex      : 0001

    :param domain_index: System domain index byte.
    :type domain_index: int
    :param flag_value: System flag byte value.
    :type flag_value: int

    Sample Packet Logs & Protocol Notes:
    # Sent by THM when in signal strength test mode (0505, except 1st packet):
    # sent by a THM when is signal strength test mode (0505, except 1st packet)
    # .I --- 32:155617 32:155617 --:------ 0016 002 0000
    # .I --- 34:021943 34:021943 --:------ 0016 002 0000
    # 12:40:02.098 061  W --- 12:010740 --:------ 12:010740 0001 005 0000000501
    # 12:40:08.099 058  W --- 12:010740 --:------ 12:010740 0001 005 0000000501
    # 13:48:38.518 080  W --- 12:010740 --:------ 12:010740 0001 005 0000000501
    # 13:48:45.518 074  W --- 12:010740 --:------ 12:010740 0001 005 0000000505
    # 13:48:50.518 077  W --- 12:010740 --:------ 12:010740 0001 005 0000000505
    # 15:12:47.769 053  W --- 01:145038 --:------ 01:145038 0001 005 FC00000505
    # 15:12:47.869 053 RQ --- 01:145038 13:237335 --:------ 0016 002 00FF
    # 15:12:47.880 053 RP --- 13:237335 01:145038 --:------ 0016 002 0017
    # 12:30:18.083 047  W --- 01:145038 --:------ 01:145038 0001 005 0800000505
    # 12:30:23.084 049  W --- 01:145038 --:------ 01:145038 0001 005 0800000505
    # 15:03:33.187 054  W --- 01:145038 --:------ 01:145038 0001 005 FC00000505
    # 15:03:38.188 063  W --- 01:145038 --:------ 01:145038 0001 005 FC00000505
    # 15:03:43.188 064  W --- 01:145038 --:------ 01:145038 0001 005 FC00000505
    # 15:13:19.757 053  W --- 01:145038 --:------ 01:145038 0001 005 FF00000505
    # 15:13:24.758 054  W --- 01:145038 --:------ 01:145038 0001 005 FF00000505
    # 15:13:29.758 068  W --- 01:145038 --:------ 01:145038 0001 005 FF00000505
    # 15:13:34.759 063  W --- 01:145038 --:------ 01:145038 0001 005 FF00000505
    # 16:49:46.125 057  W --- 04:166090 --:------ 01:032820 0001 005 0100000505
    """

    _STRUCT_FMT: ClassVar[str] = ">BB"

    domain_index: int
    flag_value: int

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack system flag binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked SystemFlagPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 2 bytes.
        """
        if len(raw_data) < 2:
            raise ValueError(
                f"Invalid payload length for 0016: {len(raw_data)}"
            )
        dom, flg = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(domain_index=dom, flag_value=flg)

    def to_bytes(self) -> bytes:
        """Pack system flag data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        return struct.pack(
            self._STRUCT_FMT, self.domain_index, self.flag_value
        )


# ----------------------------------------------------------------------


# ----------------------------------------------------------------------


@register_payload(Code._0100)
class SystemLanguagePayload(PayloadBase):
    """Master payload dispatcher for system language (Opcode 0100).

    Dispatches system language configuration payloads to 2-byte or
    3-byte variant sub-dataclasses based on payload length.
    """

    VARIANTS: ClassVar[tuple[type[PayloadBase], ...]] = ()

    language: str | None = None

    @classmethod
    def from_bytes(
        cls, raw_data: bytes
    ) -> "SystemLanguage2BPayload | SystemLanguage3BPayload":
        """Unpack system language binary payload by length."""
        if len(raw_data) >= 3:
            return SystemLanguage3BPayload.from_bytes(raw_data)
        return SystemLanguage2BPayload.from_bytes(raw_data)

    def to_bytes(self) -> bytes:
        """Pack payload base default method.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        :raises NotImplementedError: Master dispatcher must dispatch to
            variant sub-dataclass.
        """
        raise NotImplementedError("Use concrete variant sub-dataclass")


@dataclass(frozen=True, slots=True)
class SystemLanguage2BPayload(SystemLanguagePayload):
    """System language 2-byte configuration layout (Opcode 0100).

    2-byte Language binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Header / Domain              : 00
      +1       B      1B   Language Code (0=EN, 1=NL)   : 00
      --------------------------------------------------------------
      Field-spaced hex : 00 00
      Payload hex      : 0000

    :param language: Hex string representation of the language code.
    :type language: str | None
    """

    _STRUCT_FMT: ClassVar[str] = ">BB"

    language: str | None = None

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 2-byte system language binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked SystemLanguage2BPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 2 bytes.
        """
        if len(raw_data) < 2:
            raise ValueError(
                f"Invalid payload length for SystemLanguage2BPayload: {len(raw_data)}"
            )
        _hdr, l_code = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(language=f"{l_code:02x}")

    def to_bytes(self) -> bytes:
        """Pack 2-byte system language into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        if not self.language:
            return b"\x00\x00"
        try:
            l_code = int(self.language, 16)
        except ValueError:
            l_code = 0
        return struct.pack(self._STRUCT_FMT, 0x00, l_code)

    def to_dict(self) -> dict[str, Any]:
        """Convert system language payload to legacy dictionary layout."""
        return {"language": self.language}


@dataclass(frozen=True, slots=True)
class SystemLanguage3BPayload(SystemLanguagePayload):
    """System language 3-byte string configuration layout (Opcode 0100).

    3-byte Language binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Header / Domain              : 00
      +1       s      2B   Language string (latin-1)    : 'EN' (45 4E)
      --------------------------------------------------------------
      Field-spaced hex : 00 45 4E
      Payload hex      : 00454E

    :param language: Decoded latin-1 language string.
    :type language: str | None
    """

    _STRUCT_FMT: ClassVar[str] = ">B2s"

    language: str | None = None

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 3-byte system language string binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked SystemLanguage3BPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 3 bytes.
        """
        if len(raw_data) < 3:
            raise ValueError(
                f"Invalid payload length for SystemLanguage3BPayload: {len(raw_data)}"
            )
        lang = raw_data[1:3].decode("latin-1", errors="ignore")
        return cls(language=lang)

    def to_bytes(self) -> bytes:
        """Pack 3-byte system language string into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        if not self.language:
            return b"\x00\x00\x00"
        return b"\x00" + self.language.encode("latin-1")[:2]

    def to_dict(self) -> dict[str, Any]:
        """Convert system language payload to legacy dictionary layout."""
        return {"language": self.language}


# Update VARIANTS property after variants are defined
SystemLanguagePayload.VARIANTS = (
    SystemLanguage2BPayload,
    SystemLanguage3BPayload,
)


# ----------------------------------------------------------------------


@register_payload(Code._01D0)
@dataclass(frozen=True, slots=True)
class SystemParameterPayload(PayloadBase):
    """System configuration parameter payload (Opcode 01D0).

    2-byte System Parameter binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Parameter Index (uint8)      : 00
      +1       B      1B   Parameter Value (uint8)      : 01
      --------------------------------------------------------------
      Field-spaced hex : 00 01
      Payload hex      : 0001

    Protocol Notes:
      # unknown_01d0, from a HR91 (when its buttons are pushed)
      # .W --- 04:000722 01:158182 --:------ 01D0 002 0003  # TRV in zone 00
      # .I --- 01:158182 04:000722 --:------ 01D0 002 0003

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
        """Unpack system parameter binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked SystemParameterPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 2 bytes.
        """
        if len(raw_data) < 2:
            raise ValueError(
                f"Invalid payload length for 01D0: {len(raw_data)}"
            )
        index, value = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(parameter_index=index, parameter_value=value)

    def to_bytes(self) -> bytes:
        """Pack system parameter data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        return struct.pack(
            self._STRUCT_FMT, self.parameter_index, self.parameter_value
        )


# ----------------------------------------------------------------------


@register_payload(Code._01E9)
@dataclass(frozen=True, slots=True)
class SystemFaultPayload(PayloadBase):
    """System fault alert payload (Opcode 01E9).

    2-byte System Fault binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Fault Code (uint8)           : 00
      +1       B      1B   Fault Severity / Flag        : 01
      --------------------------------------------------------------
      Field-spaced hex : 00 01
      Payload hex      : 0001

    Protocol Notes:
      # unknown_01e9, from a HR91 (when its buttons are pushed)
      # .W --- 04:000722 01:158182 --:------ 01E9 002 0003  # TRV in zone 00
      # .I --- 01:158182 04:000722 --:------ 01E9 002 0000

    :param fault_code: System fault code byte.
    :type fault_code: int
    :param flag_value: Fault flag byte.
    :type flag_value: int
    """

    _STRUCT_FMT: ClassVar[str] = ">BB"

    fault_code: int
    flag_value: int

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack system fault binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked SystemFaultPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 2 bytes.
        """
        if len(raw_data) < 2:
            raise ValueError(
                f"Invalid payload length for 01E9: {len(raw_data)}"
            )
        code, flg = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(fault_code=code, flag_value=flg)

    def to_bytes(self) -> bytes:
        """Pack system fault data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        return struct.pack(self._STRUCT_FMT, self.fault_code, self.flag_value)


# ----------------------------------------------------------------------


@register_payload(Code._0418)
@dataclass(frozen=True, slots=True)
class SystemFaultLogPayload(PayloadBase):
    """System fault log entry payload (Opcode 0418).

    Variable-length System Fault Log binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Log Index (uint8)            : 00
      +1       xB     vB   Fault Log Data Bytes         : 00 01 00 00
      --------------------------------------------------------------
      Field-spaced hex : 00 00010000
      Payload hex      : 0000010000

    :param log_index: Fault log index byte.
    :type log_index: int
    :param log_data: Fault log raw byte sequence.
    :type log_data: bytes
    """

    _STRUCT_FMT_HEADER: ClassVar[str] = ">B"

    log_index: int
    log_data: bytes

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack system fault log binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked SystemFaultLogPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data is empty.
        """
        if not raw_data:
            return cls(log_index=0, log_data=b"")
        return cls(log_index=raw_data[0], log_data=raw_data[1:])

    def to_bytes(self) -> bytes:
        """Pack system fault log data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        return bytes([self.log_index]) + self.log_data

    def to_dict(self, msg: Any = None) -> dict[str, Any]:
        """Convert fault log payload to legacy dictionary layout.

        :param msg: Optional message context object.
        :type msg: Any
        :returns: Decoded fault log dictionary.
        :rtype: dict[str, Any]
        """
        raw_hex = (bytes([self.log_index]) + self.log_data).hex().upper()
        log_index_str = (
            raw_hex[4:6] if len(raw_hex) >= 6 else f"{self.log_index:02X}"
        )

        verb = getattr(msg, "verb", Verb.I_) if msg is not None else Verb.I_
        verb_str = str(getattr(verb, "value", verb))
        if verb == Verb.RQ or verb_str == RQ:
            return {SZ_LOG_INDEX: log_index_str}

        if len(raw_hex) < 44 or hex_to_dts(raw_hex[18:30]) is None:
            return {SZ_LOG_ENTRY: None}

        try:
            log_entry_dict = parse_fault_log_entry(raw_hex)
            if (
                not isinstance(log_entry_dict, dict)
                or SZ_TIMESTAMP not in log_entry_dict
            ):
                return {SZ_LOG_ENTRY: None}

            keys_to_include = (SZ_TIMESTAMP, SZ_FAULT_STATE, SZ_FAULT_TYPE)
            entry = [
                v for k, v in log_entry_dict.items() if k in keys_to_include
            ]

            dev_class = log_entry_dict.get(SZ_DEVICE_CLASS)
            domain_index = log_entry_dict.get(
                SZ_DOMAIN_INDEX, log_entry_dict.get("domain_index")
            )
            dev_id = log_entry_dict.get(SZ_DEVICE_ID)

            if dev_class != FaultDeviceClass.ACTUATOR:
                entry.append(dev_class)
            elif domain_index == FC:
                entry.append(DEV_ROLE_MAP[DevRole.APP])
            elif domain_index == FA:
                entry.append(DEV_ROLE_MAP[DevRole.HTG])
            elif domain_index == F9:
                entry.append(DEV_ROLE_MAP[DevRole.HT1])
            else:
                entry.append(FaultDeviceClass.ACTUATOR)

            if dev_class != FaultDeviceClass.CONTROLLER:
                entry.append(domain_index)

            if dev_id not in ("00:000000", "00:000001", "00:000002"):
                entry.append(dev_id)

            entry.extend((raw_hex[6:8], raw_hex[14:18], raw_hex[30:38]))

            return {
                SZ_LOG_INDEX: log_index_str,
                SZ_LOG_ENTRY: tuple([str(r) for r in entry]),
            }
        except Exception:
            return {SZ_LOG_INDEX: log_index_str, SZ_LOG_ENTRY: None}


# ----------------------------------------------------------------------


@register_payload(Code._042F)
@dataclass(frozen=True, slots=True)
class SystemLogIndexPayload(PayloadBase):
    """System log index payload (Opcode 042F).

    2-byte Log Index binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Log Domain Index (uint8)     : 00
      +1       B      1B   Log Pointer (uint8)          : 01
      --------------------------------------------------------------
      Field-spaced hex : 00 01
      Payload hex      : 0001

    Protocol Notes:
      # .I --- 32:168090 --:------ 32:168090 042F 009 000000100F00105050
      # RP --- 10:048122 18:006402 --:------ 042F 009 000200001400163010
      # Non-evohome (VMI) are len==9.

    :param domain_index: Log domain index byte.
    :type domain_index: int
    :param log_pointer: Current log entry pointer.
    :type log_pointer: int
    :param raw_bytes: Optional raw extra payload bytes.
    :type raw_bytes: bytes | None
    """

    _STRUCT_FMT: ClassVar[str] = ">BB"

    domain_index: int
    log_pointer: int
    raw_bytes: bytes | None = None

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack system log index binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked SystemLogIndexPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 2 bytes.
        """
        if len(raw_data) < 2:
            raise ValueError(
                f"Invalid payload length for 042F: {len(raw_data)}"
            )
        dom, ptr = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(domain_index=dom, log_pointer=ptr, raw_bytes=raw_data)

    def to_bytes(self) -> bytes:
        """Pack system log index data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        if self.raw_bytes is not None:
            return self.raw_bytes
        return struct.pack(
            self._STRUCT_FMT, self.domain_index, self.log_pointer
        )

    def to_dict(self, msg: Any = None) -> dict[str, Any]:
        """Convert system log index payload to legacy dictionary format.

        :param msg: Optional message context object.
        :type msg: Any
        :returns: Decoded log index dictionary.
        :rtype: dict[str, Any]
        """
        if self.raw_bytes is not None and len(self.raw_bytes) == 9:
            raw_hex = self.raw_bytes.hex().upper()
            return {
                "counter_1": f"0x{raw_hex[2:6]}",
                "counter_3": f"0x{raw_hex[6:10]}",
                "counter_5": f"0x{raw_hex[10:14]}",
                "unknown_7": f"0x{raw_hex[14:18]}",
            }
        return {
            SZ_DOMAIN_INDEX: self.domain_index,
            "log_pointer": self.log_pointer,
        }


# ----------------------------------------------------------------------


@register_payload(Code._0B04)
@dataclass(frozen=True, slots=True)
class SystemStatusPayload(PayloadBase):
    """System operational status payload (Opcode 0B04).

    2-byte System Status binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   System State Code (uint8)    : 00
      +1       B      1B   System Flags (uint8)         : 00
      --------------------------------------------------------------
      Field-spaced hex : 00 00
      Payload hex      : 0000

    Protocol Notes:
      # .I --- --:------ --:------ 12:207082 0B04 002 00C8
      # RP --- 10:048122 18:006402 --:------ 0B04 002 0000
      # TODO: unknown_0b04, from THM (only when its a CTL?)

    :param state_code: System operational state code.
    :type state_code: int
    :param flags: System status flags.
    :type flags: int
    """

    _STRUCT_FMT: ClassVar[str] = ">BB"

    state_code: int
    flags: int

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack system status binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked SystemStatusPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 2 bytes.
        """
        if len(raw_data) < 2:
            raise ValueError(
                f"Invalid payload length for 0B04: {len(raw_data)}"
            )
        code, flg = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(state_code=code, flags=flg)

    def to_bytes(self) -> bytes:
        """Pack system status data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        return struct.pack(self._STRUCT_FMT, self.state_code, self.flags)


# ----------------------------------------------------------------------


@register_payload(Code._1060)
@dataclass(frozen=True, slots=True)
class DeviceBatteryPayload(PayloadBase):
    """Device battery status payload (Opcode 1060).

    3-byte Battery Status binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Header / Zone Index          : 00
      +1       B      1B   Battery Level (uint8)        : 64 (50.0%)
      +2       B      1B   Flags (0=Low, 1=OK)          : 01 (OK)
      --------------------------------------------------------------
      Field-spaced hex : 00 64 01
      Payload hex      : 006401

    :param header: Header or zone index byte.
    :type header: int
    :param battery_level: Battery level float (0.0 to 1.0) or None if absent/sentinel.
    :type battery_level: float | None
    :param battery_low: True if battery is low, False if OK.
    :type battery_low: bool
    """

    _STRUCT_FMT: ClassVar[str] = ">BBB"

    header: int
    battery_level: float | None
    battery_low: bool

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 3-byte battery status binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked DeviceBatteryPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 3 bytes.
        """
        if len(raw_data) < 3:
            raise ValueError(
                f"Invalid payload length for 1060: {len(raw_data)}"
            )
        hdr = raw_data[0]
        raw_level = raw_data[1]
        raw_flags = raw_data[2]
        level = None if raw_level in (0x00, 0xFF) else raw_level / 200.0
        low = raw_flags == 0x00
        return cls(header=hdr, battery_level=level, battery_low=low)

    def to_bytes(self) -> bytes:
        """Pack battery status into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        raw_level = (
            0xFF
            if self.battery_level is None
            else int(round(self.battery_level * 200.0))
        )
        raw_flags = 0x00 if self.battery_low else 0x01
        return bytes([self.header, raw_level, raw_flags])

    def to_dict(self, msg: Any = None) -> dict[str, Any]:
        """Convert 1060 payload to legacy dictionary format.

        :param msg: Optional message context object.
        :type msg: Any
        :returns: Decoded battery dictionary.
        :rtype: dict[str, Any]
        """
        result: dict[str, Any] = {
            "battery_low": self.battery_low,
            "battery_level": self.battery_level,
        }
        if self.header != 0:
            result[SZ_ZONE_INDEX] = f"{self.header:02X}"
        return result


# ----------------------------------------------------------------------


@register_payload(Code._10E0)
@register_payload(Code._10E1)
@dataclass(frozen=True, slots=True)
class SystemDeviceInfoPayload(PayloadBase):
    """System device information & firmware payload (Opcode 10E0, 10E1).

    Variable-length System Device Info binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Info Type Code (uint8)       : 00
      +1       xB     vB   Hardware/Firmware Info Bytes : 01 02 03
      --------------------------------------------------------------
      Field-spaced hex : 00 010203
      Payload hex      : 00010203

    Protocol Notes:
      # Some HVAC devices will RP|10E0|00.
      # Payload length is variable (typically >= 19 bytes).
      # Trailing 0x00 padding bytes are stripped during string decoding.

    :param info_type: Device info type byte.
    :type info_type: int
    :param info_bytes: Firmware and hardware information raw bytes.
    :type info_bytes: bytes
    """

    _STRUCT_FMT_HEADER: ClassVar[str] = ">B"

    info_type: int
    info_bytes: bytes

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack system device info binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked SystemDeviceInfoPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data is empty.
        """
        if not raw_data:
            raise ValueError("Payload data cannot be empty")
        (i_type,) = struct.unpack_from(cls._STRUCT_FMT_HEADER, raw_data, 0)
        return cls(info_type=i_type, info_bytes=raw_data[1:])

    def to_bytes(self) -> bytes:
        """Pack system device info data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        return (
            struct.pack(self._STRUCT_FMT_HEADER, self.info_type)
            + self.info_bytes
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert device info payload to legacy dictionary layout.

        :returns: Decoded device info dictionary.
        :rtype: dict[str, Any]
        """
        raw_payload = bytes([self.info_type]) + self.info_bytes
        hex_str = raw_payload.hex().upper()
        if hex_str == "00":
            return {}
        hex_str = re.sub("(00)*$", "", hex_str)
        if len(hex_str) < 36:
            return {
                "info_type": self.info_type,
                "info_bytes": self.info_bytes.hex(),
            }
        desc_hex, _, _ = hex_str[36:].partition("00")
        if len(desc_hex) % 2 != 0:
            desc_hex += "0"
        try:
            desc = bytearray.fromhex(desc_hex).decode()
        except Exception:
            desc = ""
        return {
            "oem_code": hex_str[14:16],
            "manufacturer_sub_id": hex_str[6:8],
            "product_id": hex_str[8:10],
            "date_1": hex_to_date(hex_str[28:36]) or "0000-00-00",
            "date_2": hex_to_date(hex_str[20:28]) or "0000-00-00",
            "description": desc,
        }


# ----------------------------------------------------------------------


@register_payload(Code._1290)
@dataclass(frozen=True, slots=True)
class SystemOutdoorTempPayload(PayloadBase):
    """System outdoor temperature sensor payload (Opcode 1290).

    2-byte Outdoor Temp binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       h      2B   Outdoor Temperature (int16*100): 05 DC (15.00°C)
      --------------------------------------------------------------
      Field-spaced hex : 05DC
      Payload hex      : 05DC

    :param outdoor_temp: Outdoor temperature in °C, or None if invalid.
    :type outdoor_temp: float | None
    """

    _STRUCT_FMT: ClassVar[str] = ">h"

    outdoor_temp: float | None

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack outdoor temperature binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked SystemOutdoorTempPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 2 bytes.
        """
        if len(raw_data) < 2:
            raise ValueError(
                f"Invalid payload length for 1290: {len(raw_data)}"
            )
        temp_bytes = raw_data[1:3] if len(raw_data) >= 3 else raw_data[0:2]
        if temp_bytes in (b"\x7f\xff", b"\x31\xff", b"\x7f\x7f"):
            return cls(outdoor_temp=None)
        (temp_raw,) = struct.unpack_from(cls._STRUCT_FMT, temp_bytes, 0)
        return cls(outdoor_temp=temp_raw / 100.0)

    def to_bytes(self) -> bytes:
        """Pack outdoor temperature data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        temp_raw = (
            0x7FFF
            if self.outdoor_temp is None
            else int(round(self.outdoor_temp * 100.0))
        )
        return struct.pack(self._STRUCT_FMT, temp_raw)

    def to_dict(self) -> dict[str, Any]:
        """Convert outdoor temperature payload to legacy dictionary layout.

        :returns: Decoded outdoor temperature dictionary.
        :rtype: dict[str, Any]
        """
        return {"outdoor_temp": self.outdoor_temp}


# ----------------------------------------------------------------------


# ----------------------------------------------------------------------


@register_payload(Code._1F09)
class SystemSyncHeartbeatPayload(PayloadBase):
    """Master payload dispatcher for sync heartbeat (Opcode 1F09).

    Protocol Notes:
      # system_sync - FF (I), 00 (RP), F8 (W, after 1FC9).
      # FF is evohome, DB is Hometronics.
    """

    VARIANTS: ClassVar[tuple[type[PayloadBase], ...]] = ()

    sync_sequence: int
    remaining_seconds: float | None = None

    @classmethod
    def from_bytes(
        cls, raw_data: bytes
    ) -> "SystemSyncHeartbeat1BPayload | SystemSyncHeartbeat3BPayload":
        """Unpack system sync heartbeat binary payload by length."""
        if len(raw_data) >= 3:
            return SystemSyncHeartbeat3BPayload.from_bytes(raw_data)
        return SystemSyncHeartbeat1BPayload.from_bytes(raw_data)

    def to_bytes(self) -> bytes:
        """Pack payload base default method.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        :raises NotImplementedError: Master dispatcher must dispatch to
            variant sub-dataclass.
        """
        raise NotImplementedError("Use concrete variant sub-dataclass")


@dataclass(frozen=True, slots=True)
class SystemSyncHeartbeat1BPayload(SystemSyncHeartbeatPayload):
    """System synchronization heartbeat 1-byte layout (Opcode 1F09).

    1-byte System Sync Heartbeat binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Sync Sequence Flag (uint8)   : 00
      --------------------------------------------------------------
      Field-spaced hex : 00
      Payload hex      : 00

    :param sync_sequence: Synchronization sequence flag.
    :type sync_sequence: int
    :param remaining_seconds: Remaining seconds (None for 1B payload).
    :type remaining_seconds: float | None
    """

    _STRUCT_FMT: ClassVar[str] = ">B"

    sync_sequence: int
    remaining_seconds: float | None = None

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 1-byte system sync heartbeat binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked SystemSyncHeartbeat1BPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data is empty.
        """
        if not raw_data:
            raise ValueError("Payload data cannot be empty")
        (sequence,) = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(sync_sequence=sequence)

    def to_bytes(self) -> bytes:
        """Pack 1-byte system sync heartbeat into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        return struct.pack(self._STRUCT_FMT, self.sync_sequence)

    def to_dict(self) -> dict[str, Any]:
        """Convert system sync heartbeat payload to legacy dictionary layout."""
        return {"sync_sequence": self.sync_sequence}


@dataclass(frozen=True, slots=True)
class SystemSyncHeartbeat3BPayload(SystemSyncHeartbeatPayload):
    """System synchronization heartbeat 3-byte layout (Opcode 1F09).

    3-byte System Sync Heartbeat binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Sync Sequence Flag (uint8)   : 00
      +1       H      2B   Remaining Seconds (uint16*10): 05 14 (130.0s)
      --------------------------------------------------------------
      Field-spaced hex : 00 0514
      Payload hex      : 000514

    :param sync_sequence: Synchronization sequence flag.
    :type sync_sequence: int
    :param remaining_seconds: Remaining synchronization seconds.
    :type remaining_seconds: float | None
    """

    _STRUCT_FMT: ClassVar[str] = ">BH"

    sync_sequence: int
    remaining_seconds: float | None = None

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 3-byte system sync heartbeat binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked SystemSyncHeartbeat3BPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 3 bytes.
        """
        if len(raw_data) < 3:
            raise ValueError(
                f"Invalid payload length for SystemSyncHeartbeat3BPayload: {len(raw_data)}"
            )
        sequence, secs_raw = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(sync_sequence=sequence, remaining_seconds=secs_raw / 10.0)

    def to_bytes(self) -> bytes:
        """Pack 3-byte system sync heartbeat into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        secs_raw = (
            0
            if self.remaining_seconds is None
            else int(round(self.remaining_seconds * 10.0))
        )
        return struct.pack(self._STRUCT_FMT, self.sync_sequence, secs_raw)

    def to_dict(self) -> dict[str, Any]:
        """Convert system sync heartbeat payload to legacy dictionary layout."""
        return {"remaining_seconds": self.remaining_seconds}


# Update VARIANTS property after variants are defined
SystemSyncHeartbeatPayload.VARIANTS = (
    SystemSyncHeartbeat1BPayload,
    SystemSyncHeartbeat3BPayload,
)


# ----------------------------------------------------------------------


@register_payload(Code._2E04)
@register_payload(Code._2E10)
class SystemConfigPayload(PayloadBase):
    """Master payload dispatcher for Opcode 2E04 and 2E10.

    Protocol Notes:
      # Hometronics lifestyle ID: presence_detect, HVAC sensor, or Timed boost for Vasco D60.
    """

    VARIANTS: ClassVar[tuple[type[PayloadBase], ...]] = ()

    config_index: int
    config_value: int
    raw_extra: bytes | None = None

    @classmethod
    def from_bytes(
        cls, raw_data: bytes
    ) -> "SystemConfig2BPayload | SystemConfigVarPayload":
        """Unpack system config binary payload by length."""
        if len(raw_data) > 2:
            return SystemConfigVarPayload.from_bytes(raw_data)
        return SystemConfig2BPayload.from_bytes(raw_data)

    def to_bytes(self) -> bytes:
        """Pack payload base default method.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        :raises NotImplementedError: Master dispatcher must dispatch to
            variant sub-dataclass.
        """
        raise NotImplementedError("Use concrete variant sub-dataclass")


@dataclass(frozen=True, slots=True)
class SystemConfig2BPayload(SystemConfigPayload):
    """System configuration parameter 2-byte payload (Opcode 2E04, 2E10).

    2-byte System Config binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Config Index (uint8)         : 00
      +1       B      1B   Config Value (uint8)         : 00
      --------------------------------------------------------------
      Field-spaced hex : 00 00
      Payload hex      : 0000

    :param config_index: Configuration index byte.
    :type config_index: int
    :param config_value: Configuration value byte.
    :type config_value: int
    :param raw_extra: Trailing bytes (None for 2B payload).
    :type raw_extra: bytes | None
    """

    _STRUCT_FMT: ClassVar[str] = ">BB"

    config_index: int
    config_value: int
    raw_extra: bytes | None = None

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 2-byte system config binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked SystemConfig2BPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 2 bytes.
        """
        if len(raw_data) < 2:
            raise ValueError(
                f"Invalid payload length for SystemConfig2BPayload: {len(raw_data)}"
            )
        c_index, c_val = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(config_index=c_index, config_value=c_val)

    def to_bytes(self) -> bytes:
        """Pack 2-byte system config into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        return struct.pack(
            self._STRUCT_FMT, self.config_index, self.config_value
        )

    def to_dict(self, msg: Any = None) -> dict[str, Any]:
        """Convert system config payload to legacy dictionary layout."""
        if msg and hasattr(msg, "src"):
            dev_type = (
                getattr(msg.src, "type", None)
                or getattr(msg.src, "id", "")[:2]
            )
            if dev_type in ("21", "37", "32"):
                return {"presence_detected": bool(self.config_value != 0)}

        mode_code = f"{self.config_index:02X}"
        if mode_code in SYS_MODE_MAP:
            result: dict[str, Any] = {SZ_SYSTEM_MODE: SYS_MODE_MAP[mode_code]}
            if mode_code not in (
                SYS_MODE_MAP.AUTO,
                SYS_MODE_MAP.HEAT_OFF,
                SYS_MODE_MAP.AUTO_WITH_RESET,
            ):
                result[SZ_UNTIL] = None
            return result

        return {
            "config_index": self.config_index,
            "config_val": self.config_value,
        }


@dataclass(frozen=True, slots=True)
class SystemConfigVarPayload(SystemConfigPayload):
    """System configuration parameter variable-length payload (Opcode 2E04, 2E10).

    Variable-length System Config binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Config Index (uint8)         : 01
      +1       B      1B   Config Value (uint8)         : 00
      +2       6s     6B   Trailing config bytes        : 00 00 00 00 00 00
      --------------------------------------------------------------
      Field-spaced hex : 01 00 000000000000
      Payload hex      : 0100000000000000

    :param config_index: Configuration index byte.
    :type config_index: int
    :param config_value: Configuration value byte.
    :type config_value: int
    :param raw_extra: Trailing configuration bytes.
    :type raw_extra: bytes | None
    """

    _STRUCT_FMT: ClassVar[str] = ">BB"

    config_index: int
    config_value: int
    raw_extra: bytes | None = None

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack variable-length system config binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked SystemConfigVarPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 3 bytes.
        """
        if len(raw_data) < 2:
            raise ValueError(
                f"Invalid payload length for SystemConfigVarPayload: {len(raw_data)}"
            )
        c_index, c_val = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(
            config_index=c_index, config_value=c_val, raw_extra=raw_data[2:]
        )

    def to_bytes(self) -> bytes:
        """Pack variable-length system config into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        return struct.pack(
            self._STRUCT_FMT, self.config_index, self.config_value
        ) + (self.raw_extra or b"")

    def to_dict(self, msg: Any = None) -> dict[str, Any]:
        """Convert system config payload to legacy dictionary layout."""
        if msg and hasattr(msg, "src"):
            dev_type = (
                getattr(msg.src, "type", None)
                or getattr(msg.src, "id", "")[:2]
            )
            if dev_type in ("21", "37", "32"):
                return {"presence_detected": bool(self.config_value != 0)}

        mode_code = f"{self.config_index:02X}"
        if mode_code in SYS_MODE_MAP:
            result: dict[str, Any] = {SZ_SYSTEM_MODE: SYS_MODE_MAP[mode_code]}
            if mode_code not in (
                SYS_MODE_MAP.AUTO,
                SYS_MODE_MAP.HEAT_OFF,
                SYS_MODE_MAP.AUTO_WITH_RESET,
            ):
                full_bytes = self.to_bytes()
                until_dtm = None
                if len(full_bytes) >= 8 and full_bytes[7] != 0:
                    until_dtm = hex_to_dtm(full_bytes[1:7].hex().upper())
                result[SZ_UNTIL] = until_dtm
            return result

        return {
            "config_index": self.config_index,
            "config_val": self.config_value,
        }


# Update VARIANTS property after variants are defined
SystemConfigPayload.VARIANTS = (
    SystemConfig2BPayload,
    SystemConfigVarPayload,
)


# ----------------------------------------------------------------------


@register_payload(Code._313F)
class SystemDateTimePayload(PayloadBase):
    """Master payload dispatcher for system date and time (Opcode 313F).

    Protocol Notes:
      # datetime (time report)
      # .W --- 30:253184 34:010943 --:------ 313F 009 006000070E0507E500
    """

    VARIANTS: ClassVar[tuple[type[PayloadBase], ...]] = ()

    domain_index: int
    datetime_str: str | None = None
    is_daylight_saving: bool | None = None

    @classmethod
    def from_bytes(
        cls, raw_data: bytes
    ) -> "SystemDateTime2BPayload | SystemDateTime9BPayload":
        """Unpack system date & time payload, dispatching by length."""
        if len(raw_data) >= 9:
            return SystemDateTime9BPayload.from_bytes(raw_data)
        return SystemDateTime2BPayload.from_bytes(raw_data)

    def to_bytes(self) -> bytes:
        """Pack payload base default method.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        :raises NotImplementedError: Master dispatcher must dispatch to
            variant sub-dataclass.
        """
        raise NotImplementedError("Use concrete variant sub-dataclass")


@dataclass(frozen=True, slots=True)
class SystemDateTime2BPayload(SystemDateTimePayload):
    """2-byte system date and time header payload (Opcode 313F).

    2-byte System Date & Time Header binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Domain / Header Index        : 00
      +1       B      1B   Sub-index / Flag             : 00
      --------------------------------------------------------------
      Field-spaced hex : 00 00
      Payload hex      : 0000

    :param domain_index: Domain/header index byte.
    :type domain_index: int
    """

    _STRUCT_FMT: ClassVar[str] = ">BB"

    domain_index: int

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 2-byte system date & time header binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked SystemDateTime2BPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 2 bytes.
        """
        if len(raw_data) < 2:
            raise ValueError(
                f"Invalid payload length for SystemDateTime2BPayload: {len(raw_data)}"
            )
        dom, _flag = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(domain_index=dom)

    def to_bytes(self) -> bytes:
        """Pack 2-byte system date & time header binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        return struct.pack(self._STRUCT_FMT, self.domain_index, 0x00)

    def to_dict(self) -> dict[str, Any]:
        """Convert date & time payload to legacy dictionary layout.

        :returns: Decoded system date & time dictionary.
        :rtype: dict[str, Any]
        """
        return {SZ_IS_DAYLIGHT_SAVING: None}


@dataclass(frozen=True, slots=True)
class SystemDateTime9BPayload(SystemDateTimePayload):
    """9-byte system date and time payload (Opcode 313F).

    9-byte System Date & Time binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Domain / Header Index        : 00
      +1       B      1B   Sub-index / Flags            : F0
      +2       B      1B   Seconds | DST Flag (0x80)    : BB (59s + DST)
      +3       B      1B   Minutes (0-59)               : 00 (0m)
      +4       B      1B   Hours (0-23)                 : 04 (4h)
      +5       B      1B   Day (1-31)                   : 0C (12)
      +6       B      1B   Month (1-12)                 : 05 (May)
      +7       H      2B   Year (uint16)                : 07 EA (2026)
      --------------------------------------------------------------
      Field-spaced hex : 00 F0 BB 00 04 0C 05 07EA
      Payload hex      : 00F0BB00040C0507EA

    :param domain_index: Domain/header index byte.
    :type domain_index: int
    :param datetime_str: Formatted ISO datetime string (e.g.,
        '2026-05-12T04:00:59').
    :type datetime_str: str | None
    :param is_daylight_saving: Daylight Saving Time active flag.
    :type is_daylight_saving: bool | None
    """

    _STRUCT_FMT: ClassVar[str] = ">BBBBBBBH"

    domain_index: int
    datetime_str: str | None = None
    is_daylight_saving: bool | None = None

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack 9-byte system date & time binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked SystemDateTime9BPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 9 bytes.
        """
        if len(raw_data) < 9:
            raise ValueError(
                f"Invalid payload length for SystemDateTime9BPayload: {len(raw_data)}"
            )
        dom = raw_data[0]
        hex_str = raw_data[2:9].hex().upper()
        dt_str = hex_to_dtm(hex_str)
        dst_flag = True if bool(raw_data[2] & 0x80) else None
        return cls(
            domain_index=dom,
            datetime_str=dt_str,
            is_daylight_saving=dst_flag,
        )

    def to_bytes(self) -> bytes:
        """Pack 9-byte system date & time data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        if self.datetime_str is not None:
            dt_hex = hex_from_dtm(
                self.datetime_str,
                is_daylight_saving=self.is_daylight_saving or False,
                incl_seconds=True,
            )
            return bytes([self.domain_index, 0xF0]) + bytes.fromhex(dt_hex)
        return struct.pack(
            self._STRUCT_FMT, self.domain_index, 0xF0, 0, 0, 0, 0, 0, 0, 0
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert system date & time payload to legacy dictionary layout.

        :returns: Decoded system date & time dictionary.
        :rtype: dict[str, Any]
        """
        result: dict[str, Any] = {}
        if self.datetime_str is not None:
            result["datetime"] = self.datetime_str
        result[SZ_IS_DAYLIGHT_SAVING] = self.is_daylight_saving
        return result


# Update VARIANTS property after variants are defined
SystemDateTimePayload.VARIANTS = (
    SystemDateTime2BPayload,
    SystemDateTime9BPayload,
)


@dataclass(frozen=True, slots=True)
class SystemActuatorPayload(PayloadBase):
    """System actuator control payload.

    2-byte System Actuator binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Actuator Domain Byte (uint8) : 00
      +1       B      1B   Actuator Output Value        : 64 (50%)
      --------------------------------------------------------------
      Field-spaced hex : 00 64
      Payload hex      : 0064

    :param domain: Actuator domain byte.
    :type domain: int
    :param actuator_value: Actuator output value byte.
    :type actuator_value: int
    """

    _STRUCT_FMT: ClassVar[str] = ">BB"

    domain: int
    actuator_value: int

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack system actuator binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked SystemActuatorPayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 2 bytes.
        """
        if len(raw_data) < 2:
            raise ValueError(f"Invalid payload length: {len(raw_data)}")
        dom, val_raw = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(domain=dom, actuator_value=val_raw)

    def to_bytes(self) -> bytes:
        """Pack system actuator data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        return struct.pack(self._STRUCT_FMT, self.domain, self.actuator_value)


# ----------------------------------------------------------------------


@register_payload(Code._3222)
@dataclass(frozen=True, slots=True)
class SystemOpenThermBridgePayload(PayloadBase):
    """System OpenTherm bridge status payload (Opcode 3222).

    2-byte System OpenTherm Bridge binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Bridge Mode (uint8)          : 00
      +1       B      1B   Bridge Status Flags (uint8)  : 00
      --------------------------------------------------------------
      Field-spaced hex : 00 00
      Payload hex      : 0000

    :param raw_bytes: Raw binary payload byte sequence.
    :type raw_bytes: bytes
    """

    _STRUCT_FMT: ClassVar[str] = ">BB"

    raw_bytes: bytes

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack system OpenTherm bridge binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked SystemOpenThermBridgePayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 3 bytes.
        """
        if len(raw_data) < 2:
            raise ValueError(
                f"Invalid payload length for 3222: {len(raw_data)}"
            )
        return cls(raw_bytes=raw_data)

    def to_bytes(self) -> bytes:
        """Pack system OpenTherm bridge data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        return self.raw_bytes

    def to_dict(self, msg: Any = None) -> dict[str, Any]:
        """Convert 3222 payload to legacy dictionary format.

        :param msg: Optional message context object.
        :type msg: Any
        :returns: Decoded 3222 dictionary.
        :rtype: dict[str, Any]
        """
        b = self.raw_bytes
        if len(b) >= 4:
            off_hex = f"0x{b[1]:02X}"
            len_hex = f"0x{b[2]:02X}"
            data_hex = b[3:].hex().upper()
            return {"offset": off_hex, "length": len_hex, "_data": data_hex}
        if len(b) == 3:
            return {"_value": f"0x{b[1]:02X}"}
        return {"raw": b.hex()}


# ----------------------------------------------------------------------


@register_payload(Code._PUZZ)
@dataclass(frozen=True, slots=True)
class PuzzlePayload(PayloadBase):
    """Special puzzle / diagnostic payload (Opcode 7FFF).

    Variable-length Puzzle Diagnostic binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       2B     2B   Message Type Classification  : 00 01
      +2       xB     vB   Diagnostic Payload Bytes     : 00 FF
      --------------------------------------------------------------
      Field-spaced hex : 0001 00FF
      Payload hex      : 000100FF

    :param msg_type: Message type classification byte sequence.
    :type msg_type: bytes
    :param payload_data: Raw diagnostic payload byte sequence.
    :type payload_data: bytes
    """

    _STRUCT_FMT_HEADER: ClassVar[str] = ">2s"

    msg_type: bytes
    payload_data: bytes

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack puzzle diagnostic binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked PuzzlePayload instance.
        :rtype: Self
        :raises ValueError: If raw_data length is less than 2 bytes.
        """
        if len(raw_data) < 2:
            raise ValueError(
                f"Invalid payload length for 7FFF: {len(raw_data)}"
            )
        (m_type,) = struct.unpack_from(cls._STRUCT_FMT_HEADER, raw_data, 0)
        return cls(msg_type=m_type, payload_data=raw_data[2:])

    def to_bytes(self) -> bytes:
        """Pack puzzle diagnostic data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        return (
            struct.pack(self._STRUCT_FMT_HEADER, self.msg_type)
            + self.payload_data
        )

    def to_dict(self, msg: Any = None) -> dict[str, Any]:
        """Convert puzzle diagnostic payload to legacy dictionary format.

        :param msg: Optional message context object.
        :type msg: Any
        :returns: Decoded puzzle diagnostic dictionary with hex-encoded
            byte fields (JSON-serialisable).
        :rtype: dict[str, Any]
        """
        return {
            "msg_type": self.msg_type.hex().upper(),
            "payload_data": self.payload_data.hex().upper(),
        }


# ----------------------------------------------------------------------


@register_payload(Code._0009)
@dataclass(frozen=True, slots=True)
class RelayFailsafePayload(PayloadBase):
    """Relay failsafe mode payload (Opcode 0009).

    2-byte Relay Failsafe binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       B      1B   Domain / Zone Index (uint8)  : 00
      +1       B      1B   Failsafe Enabled (0=No, 1=Yes): 01
      --------------------------------------------------------------
      Field-spaced hex : 00 01
      Payload hex      : 0001

    :param domain_or_zone_index: Domain or zone index byte.
    :type domain_or_zone_index: int
    :param failsafe_enabled: Failsafe status flag boolean.
    :type failsafe_enabled: bool
    """

    _STRUCT_FMT: ClassVar[str] = ">BB"

    domain_or_zone_index: int
    failsafe_enabled: bool
    _unknown_0: str | None = None

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self | list[Self]:
        """Unpack relay failsafe binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked RelayFailsafePayload instance or list of instances.
        :rtype: Self | list[Self]
        :raises ValueError: If raw_data length is less than 2 bytes.
        """
        if len(raw_data) >= 6 and len(raw_data) % 3 == 0:
            return [
                cls._from_3b(raw_data[i : i + 3])
                for i in range(0, len(raw_data), 3)
            ]
        if len(raw_data) < 2:
            raise ValueError(
                f"Invalid payload length for 0009: {len(raw_data)}"
            )
        if len(raw_data) >= 3:
            return cls._from_3b(raw_data[:3])
        index, flag = struct.unpack_from(cls._STRUCT_FMT, raw_data, 0)
        return cls(domain_or_zone_index=index, failsafe_enabled=bool(flag))

    @classmethod
    def _from_3b(cls, raw_data: bytes) -> Self:
        index = raw_data[0]
        flag = raw_data[1]
        u0 = f"{raw_data[2]:02X}"
        return cls(
            domain_or_zone_index=index,
            failsafe_enabled=bool(flag),
            _unknown_0=u0,
        )

    def to_bytes(self) -> bytes:
        """Pack relay failsafe data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        if self._unknown_0 is not None:
            return bytes(
                [
                    self.domain_or_zone_index,
                    int(self.failsafe_enabled),
                    int(self._unknown_0, 16),
                ]
            )
        return struct.pack(
            self._STRUCT_FMT,
            self.domain_or_zone_index,
            int(self.failsafe_enabled),
        )

    def to_dict(self, msg: Any = None) -> dict[str, Any]:
        """Convert relay failsafe payload to legacy dictionary format.

        :param msg: Optional message context object.
        :type msg: Any
        :returns: Decoded relay failsafe dictionary.
        :rtype: dict[str, Any]
        """
        index_str = f"{self.domain_or_zone_index:02X}"
        result: dict[str, Any] = {
            SZ_DOMAIN_INDEX: index_str,
            "failsafe_enabled": self.failsafe_enabled,
        }
        if self._unknown_0 is not None:
            result["unknown_0"] = self._unknown_0
        return result


# ----------------------------------------------------------------------


@register_payload(Code._0204)
@dataclass(frozen=True, slots=True)
class SystemFrame0204Payload(PayloadBase):
    """System frame payload (Opcode 0204).

    Variable-length System Frame binary layout:
      Offset  Format  Len  Description                    Sample Hex
      --------------------------------------------------------------
      +0       xB     vB   Raw Payload Byte Sequence    : 00 01 02 03
      --------------------------------------------------------------
      Field-spaced hex : 00010203
      Payload hex      : 00010203

    :param raw_payload_bytes: Raw payload byte string.
    :type raw_payload_bytes: bytes
    """

    _STRUCT_FMT: ClassVar[str] = ">s"

    raw_payload_bytes: bytes

    @classmethod
    def from_bytes(cls, raw_data: bytes) -> Self:
        """Unpack system frame 0204 binary payload.

        :param raw_data: Raw binary byte string.
        :type raw_data: bytes
        :returns: Unpacked SystemFrame0204Payload instance.
        :rtype: Self
        """
        return cls(raw_payload_bytes=raw_data)

    def to_bytes(self) -> bytes:
        """Pack system frame 0204 data into binary payload.

        :returns: Packed binary payload bytes.
        :rtype: bytes
        """
        return self.raw_payload_bytes
