#!/usr/bin/env python3
"""RAMSES RF - Decode/process a message (payload into JSON/DTO)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime as dt
from typing import TYPE_CHECKING, Any, TypeVar, cast

from ramses_rf.address import Address, id_to_address
from ramses_rf.const import (
    SZ_DHW_INDEX,
    SZ_DOMAIN_INDEX,
    SZ_HVAC_ID,
    SZ_MSG_ID,
    SZ_OTHER_INDEX,
    SZ_UFH_INDEX,
    SZ_ZONE_INDEX,
)
from ramses_rf.payloads.base import PayloadBase
from ramses_tx import CommandDTO, PacketDTO
from ramses_tx.models import DeviceId, RawPacket, TransportMessage
from ramses_tx.typing import DeviceIdT

from .. import exceptions as exc
from ..const import DEV_TYPE_MAP
from ..parsers.decoder import decode_packet
from ..protocol.ramses import CODE_INDEX_ARE_COMPLEX
from ..routing import RoutingContext, StateHeader, extract_context_value

from ..const import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    I_,
    RP,
    RQ,
    W_,
    Code,
    Verb,
)

if TYPE_CHECKING:
    # pylint: disable=unused-import
    from ..const import IndexT  # noqa: F401
    from ..gateway import Gateway


__all__ = ["Message"]


MSG_FORMAT_10: str = "|| {:10s} | {:10s} | {:2s} | {:16s} | {:^4s} || {}"


_LOGGER = logging.getLogger(__name__)


# Transition alias for typing until full payload migration is complete
type PayloadT = Any  # type: ignore[explicit-any]  # legacy payload union


# TypeVar bound to Message to allow strict inheritance typing
_MessageT = TypeVar("_MessageT", bound="Message")


class Message:
    """The Message class; will trap/log invalid msgs."""

    # Domain Bridges (Injected by Gateway)
    _IS_CONTROLLER_CB: Callable[[str], bool] | None = None
    _GET_CODE_NAME_CB: Callable[[Code | str], str] | None = None
    _GET_MSG_INDEX_CB: Callable[[Message], dict[str, str]] | None = None

    _gateway: Gateway | None = None

    def __init__(self, dto: PacketDTO) -> None:
        """Create a message from a valid packet.

        :param dto: The packet data transfer object to process.
        :type dto: PacketDTO
        :raises PacketInvalid: If the packet payload cannot be parsed.
        """
        self._dto: PacketDTO = dto

        self.dtm: dt = dto.timestamp
        self.rssi: str = dto.rssi

        # Cleanly cast properties
        self.verb: Verb = Verb(dto.verb)
        self.seqn: str = dto.seq

        self.code: Code
        try:
            self.code = Code(dto.code)
        except ValueError:
            self.code = cast(Code, dto.code)

        try:
            self.len: int = int(dto.length)
        except ValueError:
            self.len = 0

        # Safely resolve addresses via L2 Positional MACs
        addr1 = dto.addr1 if dto.addr1 else "--:------"
        addr2 = dto.addr2 if dto.addr2 else "--:------"
        addr3 = dto.addr3 if dto.addr3 else "--:------"

        self._addrs: tuple[Address, Address, Address] = (
            id_to_address(cast(DeviceIdT, addr1)),
            id_to_address(cast(DeviceIdT, addr2)),
            id_to_address(cast(DeviceIdT, addr3)),
        )

        valid = [a for a in self._addrs if a.id != "--:------"]
        self.src: Address = (
            valid[0] if valid else id_to_address(cast(DeviceIdT, "--:------"))
        )
        self.dst: Address = valid[1] if len(valid) > 1 else self.src

        # Initialize attributes before parsing to prevent AttributeError
        # if an exception is raised and __repr__ is called.
        self._str: str | None = None
        self._payload: PayloadT = {}

        self._has_array_: bool = False
        context_value = extract_context_value(
            dto.payload, raw_payload=dto.raw_payload, code=self.code
        )
        self._index_value: str | bool = (
            context_value if context_value is not None else False
        )

        self._payload = self._validate(dto.raw_payload)

    @property
    def context(self) -> RoutingContext:
        """Calculate the context natively.

        :return: The context value.
        :rtype: RoutingContext
        """
        context_value = extract_context_value(
            self._dto.payload,
            raw_payload=self._dto.raw_payload,
            code=self.code,
        )
        return RoutingContext(
            context_value
            if context_value is not None
            else (
                self._index_value if self._index_value is not False else None
            )
        )

    @property
    def state_header(self) -> StateHeader:
        """Calculate the state routing header natively.

        :return: The state header instance.
        :rtype: StateHeader
        """
        return StateHeader.create(
            code=self.code,
            verb=self.verb,
            source_id=self.src.id,
            context_value=self.context.value,
        )

    def _format_frame(self, sequence_number: str | None = None) -> str:
        """Format the message into a standard ASCII RAMSES RF packet frame.

        :param sequence_number: Optional sequence number string. Defaults to "---".
        :type sequence_number: str | None
        :returns: The formatted ASCII frame string.
        :rtype: str
        """
        seq_str = sequence_number if sequence_number else "---"
        dto = self._dto
        return f"{dto.verb} {seq_str} {dto.addr1} {dto.addr2} {dto.addr3} {dto.code} {dto.length} {dto.raw_payload}"

    @property
    def raw_payload(self) -> str:
        """Return the raw ASCII hex payload string.

        :returns: The raw ASCII hex payload string.
        :rtype: str
        """
        return self._dto.raw_payload

    @property
    def raw_frame(self) -> str:
        """Return the raw packet frame string.

        :returns: The raw ASCII frame.
        :rtype: str
        """
        return self._format_frame("---")

    @property
    def raw_frame_snapshot(self) -> str:
        """Return the raw frame string formatted for snapshot serialization.

        :returns: The frame string with sequence number included if present.
        :rtype: str
        """
        return self._format_frame(self.seqn)

    @classmethod
    def _from_packet(  # type: ignore[explicit-any]  # legacy Packet | Message
        cls: type[_MessageT], packet: Any
    ) -> _MessageT:
        """Create a Message (or subclass) from a legacy Packet.

        :param packet: The legacy packet object.
        :type packet: Any
        :return: The generated message.
        :rtype: Message
        """
        if isinstance(packet, cls):
            return packet
        msg = getattr(packet, "_msg", None)
        if isinstance(msg, cls):
            return msg
        return cls(packet.to_dto())

    @classmethod
    def _from_cmd(
        cls: type[_MessageT], command: CommandDTO, dtm: dt | None = None
    ) -> _MessageT:
        """Create a Message (or subclass) from a Command.

        :param command: The command.
        :type command: CommandDTO
        :param dtm: Datetime overrides.
        :type dtm: dt | None
        :return: The generated message.
        :rtype: Message
        """
        # Temporary shim bridging backwards logic during Phase 2
        from ramses_tx.packet import Packet

        packet = Packet._from_cmd(command, dtm=dtm)
        return cls(packet.to_dto())

    def __str__(self) -> str:
        """Return a human-readable string representation of this object.

        :return: A human-readable string representation of this object.
        :rtype: str
        """

        def format_context(dto: PacketDTO) -> str:
            """Extract the context string from the packet safely."""
            context_val: str = ""
            if self._index_value is True:
                context_val = "[..]"
            elif self._index_value is False:
                context_val = ""
            else:
                context_val = str(self._index_value)

            if (
                not context_val
                and isinstance(dto.raw_payload, str)
                and dto.raw_payload[:2] not in ("00", "FF")
            ):
                return f"({dto.raw_payload[:2]})"
            return context_val

        if self._str is not None:
            return self._str

        if self.src.id == self._addrs[0].id:
            name_0 = self._name(self.src)
            # use 'is', issue_cc 318
            name_1 = "" if self.dst is self.src else self._name(self.dst)
        else:
            name_0 = ""
            name_1 = self._name(self.src)

        if Message._GET_CODE_NAME_CB is not None:
            code_name = Message._GET_CODE_NAME_CB(self.code)
        else:
            code_name = f"unknown_{self.code}"

        self._str = MSG_FORMAT_10.format(
            name_0,
            name_1,
            self.verb,
            code_name,
            format_context(self._dto),
            self.raw_payload,
        )
        return self._str

    def __repr__(self) -> str:
        """Return an unambiguous string representation of this object.

        :return: An unambiguous string representation of this object.
        :rtype: str
        """
        raw_payload = self._dto.raw_payload
        addr1 = self._addrs[0].id
        addr2 = self._addrs[1].id
        addr3 = self._addrs[2].id
        sequence_number = self.seqn if self.seqn else "---"
        return (
            f"{self.verb} {sequence_number} {addr1} {addr2} {addr3} "
            f"{self.code} {self.len:03d} {raw_payload}"
        )

    def __eq__(self, other: object) -> bool:
        """Check equality against another Message."""
        if not isinstance(other, Message):
            return NotImplemented
        return (
            self.src,
            self.dst,
            self.verb,
            self.code,
            self._dto.raw_payload,
        ) == (
            other.src,
            other.dst,
            other.verb,
            other.code,
            other._dto.raw_payload,
        )

    def __lt__(self, other: object) -> bool:
        """Compare timestamps for ordering."""
        if not isinstance(other, Message):
            return NotImplemented
        return self.dtm < other.dtm

    def _name(self, address: Address) -> str:
        """Return a friendly name for an Address, or a Device.

        :param address: The address to identify.
        :type address: Address
        :return: A friendly name for an Address, or a Device.
        :rtype: str
        """
        # can't do 'CTL:123456' instead of ' 01:123456'
        return f" {address.id}"

    @property
    def payload(self) -> PayloadT:
        """Return the parsed payload, preferably as legacy dictionary or list.

        :return: The payload.
        :rtype: PayloadT
        """
        if not self._has_payload:
            return {}
        if isinstance(self._payload, PayloadBase):
            try:
                return self._payload.to_dict(msg=self)
            except TypeError:
                return self._payload.to_dict()
        if isinstance(self._payload, list):
            result = []
            for item in self._payload:
                if isinstance(item, PayloadBase):
                    try:
                        result.append(item.to_dict(msg=self))
                    except TypeError:
                        result.append(item.to_dict())
                else:
                    result.append(item)
            return result
        return self._payload

    @property
    def _has_payload(self) -> bool:
        """Return False if there is no payload (may falsely return True).

        The message (i.e. the raw payload) may still have an index.

        :return: False if there is no payload (may falsely return True).
        :rtype: bool
        """
        v_str = str(self.verb.value).split(".")[-1].strip()
        if v_str not in (RQ, f"{RQ}_") and self.code in (
            Code._1FC9,
            Code._1F09,
        ):
            return True
        if self.len == 1:
            return False
        if str(self.verb).strip() == RQ:
            if self.len == 2 and self.code != Code._0016:
                return False
        return True

    @property
    def _has_array(self) -> bool:
        """Return True if the message's raw payload is an array.

        :return: True if the message's raw payload is an array.
        :rtype: bool
        """
        return self._has_array_

    def _force_has_array(self) -> None:
        """Force the payload to be interpreted as an array fragment."""
        self._has_array_ = True

    @property
    def _index(self) -> dict[str, str]:
        """Get the domain_id/zone_index/other_index of a message payload.

        Used to identify the zone/domain that a message applies to.

        :return: an empty dict if there is none such, or None if
            undetermined.
        :rtype: dict[str, str]
        """
        if Message._GET_MSG_INDEX_CB is not None:
            return Message._GET_MSG_INDEX_CB(self)

        INDEX_NAMES = {
            Code._0002: SZ_OTHER_INDEX,
            Code._10A0: SZ_DHW_INDEX,
            Code._1260: SZ_DHW_INDEX,
            Code._1F41: SZ_DHW_INDEX,
            Code._22C9: SZ_UFH_INDEX,
            Code._22D9: SZ_DOMAIN_INDEX,
            Code._2389: SZ_OTHER_INDEX,
            Code._2D49: SZ_OTHER_INDEX,
            Code._31D9: SZ_HVAC_ID,
            Code._31DA: SZ_HVAC_ID,
            Code._3220: SZ_MSG_ID,
        }  # ALSO: SZ_DOMAIN_INDEX, SZ_ZONE_INDEX

        if self.code in (Code._31D9, Code._31DA):
            assert isinstance(self._index_value, str)  # mypy hint
            return {SZ_HVAC_ID: self._index_value}

        if (
            self._index_value in (True, False)
            or self.code in CODE_INDEX_ARE_COMPLEX
        ):
            return {}

        if self.code in (Code._3220,):  # FIXME: should be _SIMPLE
            return {}

        if not {self.src.type, self.dst.type} & {
            DEV_TYPE_MAP.CTL,
            DEV_TYPE_MAP.UFC,
            DEV_TYPE_MAP.HCW,
            DEV_TYPE_MAP.DTS,
            DEV_TYPE_MAP.HGI,
            DEV_TYPE_MAP.DT2,
            DEV_TYPE_MAP.PRG,
            # FIXME: DEX should be deprecated to use device type rather than class
        }:
            assert self._index_value == "00", "What!! (AA)"
            return {}

        if self.src.type == self.dst.type and self.src.type not in (
            DEV_TYPE_MAP.CTL,
            DEV_TYPE_MAP.UFC,
            DEV_TYPE_MAP.HCW,
            DEV_TYPE_MAP.HGI,
            DEV_TYPE_MAP.PRG,
        ):
            assert self._index_value == "00", "What!! (AB)"
            return {}

        # BRIDGED LOGIC:
        is_controller = True
        if Message._IS_CONTROLLER_CB is not None:
            # Use the injected domain logic from ramses_rf
            is_controller = Message._IS_CONTROLLER_CB(self.src.id)
        else:
            # Fallback for legacy tests until they are updated
            is_controller = getattr(self.src, "_is_controller", True)

        if (
            self.src.type == self.dst.type
            and not is_controller
            and self.src.type != DEV_TYPE_MAP.UFC
        ):
            assert self._index_value == "00", "What!! (BC)"
            return {}

        if self.code in (Code._000A, Code._2309) and (
            self.src.type == DEV_TYPE_MAP.UFC
        ):
            assert isinstance(self._index_value, str)  # mypy hint
            return {INDEX_NAMES[Code._22C9]: self._index_value}

        assert isinstance(self._index_value, str)  # mypy hint
        default_index_name = (
            SZ_DOMAIN_INDEX if self._index_value[:1] == "F" else SZ_ZONE_INDEX
        )
        index_name = INDEX_NAMES.get(self.code, default_index_name)

        return {index_name: self._index_value}

    @property
    def dto(self) -> TransportMessage:
        """Generate a TransportMessage DTO from this legacy Message.

        This acts as a safe, passive bridge to validate the new Data
        Transfer Objects against the legacy snapshot tests before fully
        migrating the transport layer.
        """
        raw_hex_payload = self._dto.raw_payload
        payload_length = self.len

        addr1_str = self._addrs[0].id
        addr2_str = self._addrs[1].id
        addr3_str = self._addrs[2].id

        code_str = self._dto.code
        try:
            code_int = int(code_str, 16)
        except ValueError:
            code_int = 0

        raw_packet = RawPacket(
            raw_packet=repr(self),
            rssi=str(self.rssi),
            verb=self.verb,
            seq=str(self.seqn),
            device_id_1=addr1_str,
            device_id_2=addr2_str,
            device_id_3=addr3_str,
            code=code_str,
            payload_len=f"{payload_length:03d}",
            payload=raw_hex_payload,
        )

        return TransportMessage(
            dtm=self.dtm,
            source_packets=(raw_packet,),
            rssi=int(self.rssi) if str(self.rssi).lstrip("-").isdigit() else 0,
            verb=self.verb,
            device_id_1=DeviceId.from_string(addr1_str),
            device_id_2=DeviceId.from_string(addr2_str),
            device_id_3=DeviceId.from_string(addr3_str),
            code=code_int,
            payload_len=int(payload_length),
            raw_payload=raw_hex_payload,
        )

    def _validate(self, raw_payload: str) -> PayloadT:
        """Validate a message packet payload, and parse it if valid.

        :param raw_payload: The raw payload string.
        :type raw_payload: str
        :return: A dict containing key: value pairs, or a list/DTO
            created from the payload.
        :rtype: PayloadT
        :raises PacketInvalid: If it is not valid or parsable.
        """
        # TODO: only accept invalid packets to/from HGI when flag raised
        try:
            try:
                # Semantic parsing is explicitly mapped to DTO processing
                result = decode_packet(self._dto)
            except exc.PacketPayloadInvalid as err:
                if not self._has_payload:
                    return {}  # Heartbeat fallback for null payloads
                raise err

            if isinstance(result, list):
                self._has_array_ = True
                return result

            # The DTO pipeline natively handles index extraction.
            # Return the strongly-typed PayloadBase DTO object
            return result

        except exc.PacketInvalid as err:
            _LOGGER.warning("%s < %s", repr(self), err)
            raise err

        except AssertionError as err:
            _LOGGER.exception(
                "%s < %s",
                repr(self),
                f"{err.__class__.__name__}({err})",
            )
            raise exc.PacketInvalid("Bad packet") from err

    @property
    def addr3(self) -> Address:
        """Return the third address field (the logical destination or owner).

        :return: The third address object.
        :rtype: Address
        """
        return self._addrs[2]
