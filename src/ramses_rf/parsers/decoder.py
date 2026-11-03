"""RAMSES RF - DTO-based Payload Decoder module.

This module provides the entry point for decoding L2 PacketDTOs into
L7 semantic dictionaries, strictly separating domain logic from transport.
"""

import logging
import struct
from abc import ABC, abstractmethod
from typing import Any

from ramses_rf.const import (
    SZ_DHW_INDEX,
    SZ_DOMAIN_INDEX,
    SZ_HVAC_ID,
    SZ_OTHER_INDEX,
    SZ_UFH_INDEX,
    SZ_ZONE_INDEX,
)
from ramses_rf.payloads import get_payload_class
from ramses_rf.payloads.base import PayloadBase
from ramses_rf.protocol.ramses import (
    CODE_INDEX_ARE_COMPLEX,
    CODE_INDEX_ARE_NONE,
    CODE_INDEX_ARE_SIMPLE,
    CODES_ONLY_FROM_CTL,
    CODES_WITH_ARRAYS,
    RQ_INDEX_COMPLEX,
    RQ_NO_PAYLOAD,
)
from ramses_rf.typing import PayloadT
from ramses_tx import exceptions as exc
from ramses_tx.const import I_, RQ, Code
from ramses_tx.dtos import PacketDTO

from .registry import get_parser

# Constants
_UNKNOWN_PACKET_HELP_MSG: str = (
    "Support the development of ramses_rf by reporting this packet"
)

# Variables
_LOGGER = logging.getLogger(__name__)


def _get_code(code_string: str) -> Code | None:
    """Safely convert a string to a Code enum, returning None if invalid.

    :param code_string: The raw string representation of the packet code.
    :return: A matching Code enum object or None if invalid.
    """
    try:
        return Code(code_string)
    except ValueError:
        return None


class _LegacyAddress:
    """Adapter class to mimic ramses_tx.Address for pre-0.60 parsers."""

    def __init__(self, address_string: str) -> None:
        """Initialize the pre-0.60 address with id and type.

        :param address_string: The raw address string instance.
        """
        self.id = address_string
        self.type = (
            address_string.split(":")[0] if ":" in address_string else ""
        )

    def __eq__(self, other: object) -> bool:
        """Evaluate equality based on address ID.

        :param other: The other object to compare against.
        :return: True if addresses match, otherwise NotImplemented.
        """
        if hasattr(other, "id"):
            return bool(self.id == other.id)
        return NotImplemented


class _LegacyMessage:
    """Anti-corruption adapter mimicking the pre-0.60 ramses_tx.Message interface."""

    def __init__(self, dto: PacketDTO) -> None:
        """Initialize the pre-0.60 message using data strictly from the DTO.

        :param dto: The data transfer object representation of the packet.
        """
        self.verb = dto.verb  # Do not strip: Preserves padding for I_ (" I")
        self.seqn = dto.seq
        try:
            self.len = int(dto.length)
        except ValueError:
            self.len = 0

        self.code = dto.code
        self.code_enum = _get_code(self.code)
        self.payload = dto.raw_payload
        self._len = int(len(self.payload) / 2)

        # Instance interning cache to support Python 'is'/'is not' identity checks
        self._addr_cache: dict[str, _LegacyAddress] = {}

        raw_addrs = [dto.addr1, dto.addr2, dto.addr3]
        valid_addrs = [a for a in raw_addrs if a and a != "--:------"]

        src_id = valid_addrs[0] if valid_addrs else "--:------"
        dst_id = valid_addrs[1] if len(valid_addrs) > 1 else "--:------"

        if src_id == dst_id:
            dst_id = src_id

        self.src = self._get_addr(src_id)
        self.dst = self._get_addr(dst_id)

        self._addrs = [
            self._get_addr(dto.addr1),
            self._get_addr(dto.addr2),
            self._get_addr(dto.addr3),
        ]

        self.dtm = dto.timestamp
        _tz = getattr(self.dtm, "tzinfo", None)
        if _tz is not None and hasattr(self.dtm, "replace"):
            self.dtm = self.dtm.replace(tzinfo=None)

        self._has_ctl_: bool | None = None
        self._index_: bool | str | None = None
        self._ctx_: bool | str | None = None

        self._has_array = self._calculate_has_array()

    def _get_addr(self, address_string: str) -> _LegacyAddress:
        """Retrieve or create an interned pre-0.60 address instance.

        :param address_string: The address identity string.
        :return: A cached or newly instantiated _LegacyAddress object.
        """
        if address_string not in self._addr_cache:
            self._addr_cache[address_string] = _LegacyAddress(address_string)
        return self._addr_cache[address_string]

    @property
    def _has_payload(self) -> bool:
        """Return False if there is no payload, matching pre-0.60 message.py exactly.

        :return: True if a parsable payload is expected, otherwise False.
        """
        if self._len == 1:
            return False
        if self.verb.strip() == RQ:
            if self.code_enum in RQ_NO_PAYLOAD:
                return False
            if self._len == 2 and self.code != Code._0016:
                return False
        return True

    def _calculate_has_array(self) -> bool:
        """Determine if the payload represents an array.

        :return: True if payload structure models an array configuration.
        """
        if self.code == Code._1FC9:
            return self.verb.strip() != RQ

        if self.verb.strip() != I_ or self.code_enum not in CODES_WITH_ARRAYS:
            return False

        element_len = CODES_WITH_ARRAYS[self.code_enum][0]
        assert isinstance(element_len, int)

        if self._len != element_len:
            a, b = divmod(self._len, element_len)
            return bool(a > 0 and b == 0)

        return bool(
            self.code in (Code._22C9, Code._3150)
            and self.src.type == "02"
            and self.src.id == self.dst.id
            and self.payload[:1] != "F"
        )

    @property
    def _has_ctl(self) -> bool:
        """Return True if the packet is to/from a controller.

        :return: Boolean evaluation denoting controller interaction flags.
        """
        if self._has_ctl_ is not None:
            return self._has_ctl_

        if {self.src.type, self.dst.type} & {"01", "02", "23"}:
            self._has_ctl_ = True
        elif self.dst.id == self.src.id:
            self._has_ctl_ = any(
                (
                    self.code == Code._3B00 and self.payload[:2] == "FC",
                    self.code_enum
                    in tuple(CODES_ONLY_FROM_CTL) + (Code._31D9, Code._31DA),
                )
            )
        elif self.dst.id == "--:------":
            self._has_ctl_ = self.src.type != "10"  # OTB (OpenTherm Bridge)
        elif self.dst.type in ("04", "22"):
            self._has_ctl_ = True
        else:
            self._has_ctl_ = False

        return self._has_ctl_

    @property
    def _index(self) -> bool | str:
        """Return the payload's index, if any.

        :return: String value of index parameter or boolean flag state.
        """
        if self._index_ is not None:
            return self._index_

        result = self._packet_index()
        self._index_ = result if result is not None else False
        return self._index_

    def _packet_index(self) -> bool | str | None:
        """Extract the exact index leveraging protocol.ramses definitions.

        :return: String or boolean representation if index parsing passes constraints.
        :raises exc.PacketPayloadInvalid: If structural pre-0.60 checks fail validations.
        """
        if self.code == Code._0005:
            return self._has_array

        if self.code == Code._0009 and self.src.type == "10":
            return False

        if self.code == Code._000C:
            if self.payload[2:4] == "000F":
                return "FC"
            if self.payload[0:4] == "010E":
                return "F9"
            if self.payload[2:4] in ("000D", "000E"):
                return "FA"
            return self.payload[:2]

        if self.code == Code._0404:
            return "HW" if self.payload[2:4] == "23" else self.payload[:2]

        if self.code == Code._0418:
            return self.payload[4:6]

        if self.code == Code._1100:
            return self.payload[:2] if self.payload[:1] == "F" else False

        if self.code == Code._3220:
            return self.payload[4:6]

        if self.code == Code._0001 and self.payload[:2] != "00":
            return self.payload[:2]

        if self.code_enum in CODE_INDEX_ARE_COMPLEX:
            pass

        if self.code_enum in CODE_INDEX_ARE_NONE:
            if self.payload[:2] != "00":
                raise exc.PacketPayloadInvalid(
                    f"Packet index is {self.payload[:2]}, but expecting no index (00) (0xAA)"
                )
            return False

        if self._has_array:
            return True

        if self.payload[:2] in ("F8", "F9", "FA", "FC"):
            return self.payload[:2]

        if self._has_ctl:
            return self.payload[:2]

        if self.code in (Code._31D9, Code._31DA):
            return self.payload[:2]

        # CODE_INDEX_ARE_SIMPLE codes have schemas that explicitly permit a
        # non-zero index (e.g. 30C9 from a 03:/04:/12: sensor uses its zone_index).
        # Accept the packet without raising — the index simply won't be injected
        # into the result dict by _build_index_dict (which has its own _has_ctl
        # gate).  This must be checked *before* the 0xAB guard below, which
        # would otherwise reject valid non-controller packets with a non-zero
        # index (see issue ramses_cc#929 — faking Zone THM broken).
        if self.code_enum in CODE_INDEX_ARE_SIMPLE:
            return None

        # Explicit pre-0.60 guard (0xAB block) - non-controllers cannot send non-zero indices here
        if self.payload[:2] != "00":
            raise exc.PacketPayloadInvalid(
                f"Packet index is {self.payload[:2]}, but expecting no index (00) (0xAB)"
            )

        return None

    @property
    def _ctx(self) -> bool | str:
        """Return the payload's full context, if any.

        :return: Context state metadata mapping.
        """
        if self._ctx_ is not None:
            return self._ctx_

        if self.code in (Code._0005, Code._000C):
            self._ctx_ = self.payload[:4]
        elif self.code == Code._0404:
            index_str = (
                str(self._index) if isinstance(self._index, str) else ""
            )
            self._ctx_ = index_str + self.payload[10:12]
        else:
            self._ctx_ = self._index
        return self._ctx_


def _build_index_dict(msg: _LegacyMessage) -> dict[str, str]:
    """Build the dictionary for index merging, matching message.py logic exactly.

    :param msg: The _LegacyMessage instance being processed.
    :return: Generated payload structural dictionary mappings.
    """
    if not isinstance(msg._index, str):
        return {}

    if msg.code_enum in CODE_INDEX_ARE_COMPLEX:
        return {}

    if msg.code in (Code._31D9, Code._31DA):
        return {SZ_HVAC_ID: str(msg._index)}

    if msg.code == Code._3220:
        return {}

    # Pre-0.60 (AA) logic: Filters strictly for allowed indexed device types
    if not {msg.src.type, msg.dst.type} & {
        "01",
        "02",
        "03",
        "04",
        "12",
        "18",
        "23",
    }:
        return {}

    # Pre-0.60 (AB) logic: Additional constraint if sent to itself
    if msg.src.type == msg.dst.type and msg.src.type not in (
        "01",
        "02",
        "03",
        "18",
        "23",
    ):
        return {}

    # Pre-0.60 (BC) logic: Bypasses strict internal constraints if it isn't a recognized controller
    if msg.src.type == msg.dst.type and not msg._has_ctl:
        return {}

    if msg.code in (Code._000A, Code._2309) and msg.src.type == "02":
        return {SZ_UFH_INDEX: str(msg._index)}

    index_val = str(msg._index)
    index_name = (
        SZ_DOMAIN_INDEX if index_val.startswith("F") else SZ_ZONE_INDEX
    )

    index_names: dict[Code | str, str] = {
        Code._0002: SZ_OTHER_INDEX,
        Code._10A0: SZ_DHW_INDEX,
        Code._1260: SZ_DHW_INDEX,
        Code._1F41: SZ_DHW_INDEX,
        Code._22C9: SZ_UFH_INDEX,
        Code._22D9: SZ_DOMAIN_INDEX,
        Code._2389: SZ_OTHER_INDEX,
        Code._2D49: SZ_OTHER_INDEX,
    }

    index_name = index_names.get(msg.code, index_name)
    return {index_name: index_val}


def parse_unknown_payload(raw_payload: str, msg: _LegacyMessage) -> PayloadT:
    """Apply a generic parser for unrecognized packet codes.

    :param raw_payload: The raw hex string of the packet payload.
    :type raw_payload: str
    :param msg: The pre-0.60 message abstraction layer object.
    :type msg: _LegacyMessage
    :returns: Standard structural error dictionaries or generic payloads.
    :rtype: PayloadT
    """
    if msg.len == 2 and raw_payload[:2] == "00":
        return {
            "_payload": raw_payload,
            "_value": {"00": False, "C8": True}.get(
                raw_payload[2:], int(raw_payload[2:] or "0", 16)
            ),
        }

    if msg.len == 3 and raw_payload[:2] == "00":
        # HACK: Using ramses_tx helper locally, replace with explicit logic if desired.
        from ramses_tx.helpers import hex_to_temp

        return {
            "_payload": raw_payload,
            "_value": hex_to_temp(raw_payload[2:]),
        }

    return {
        "_payload": raw_payload,
        "_unknown_code": msg.code,
        "_parse_error": "No parser available for this packet type",
    }


def parse_heartbeat_payload(raw_payload: str, msg: _LegacyMessage) -> PayloadT:
    """Parse a 1-byte heartbeat packet (payload '00').

    :param raw_payload: The raw packet payload value string.
    :type raw_payload: str
    :param msg: The pre-0.60 structural envelope message object.
    :type msg: _LegacyMessage
    :returns: Decoded validation flags.
    :rtype: PayloadT
    """
    return {"heartbeat": True}


class PayloadDecoder(ABC):
    """Abstract base class for the payload decoder chain."""

    def __init__(self) -> None:
        """Initialize the base decoder state."""
        self._next_decoder: PayloadDecoder | None = None

    def set_next(self, decoder: "PayloadDecoder") -> "PayloadDecoder":
        """Set the next decoder in the chain.

        :param decoder: The next step implementation payload decoder instance.
        :type decoder: PayloadDecoder
        :returns: Fluid returned instance object.
        :rtype: PayloadDecoder
        """
        self._next_decoder = decoder
        return decoder

    @abstractmethod
    def decode(
        self,
        dto: PacketDTO,
        raw_payload: str,
        payload_len: int,
        msg: _LegacyMessage,
    ) -> PayloadT | list[PayloadT] | None:
        """Decode the payload.

        :param dto: The packet raw serialization structure object.
        :type dto: PacketDTO
        :param raw_payload: Raw configuration hex payload representation.
        :type raw_payload: str
        :param payload_len: Clean length tracking metric.
        :type payload_len: int
        :param msg: Internal compatibility envelope schema instance.
        :type msg: _LegacyMessage
        :returns: Extracted parser structural outputs.
        :rtype: PayloadT | list[PayloadT] | None
        """
        if self._next_decoder:
            return self._next_decoder.decode(
                dto, raw_payload, payload_len, msg
            )
        return {}


class HeartbeatDecoder(PayloadDecoder):
    """Decoder that intercepts 1-byte '00' heartbeats."""

    def decode(
        self,
        dto: PacketDTO,
        raw_payload: str,
        payload_len: int,
        msg: _LegacyMessage,
    ) -> PayloadT | list[PayloadT] | None:
        """Evaluate and intercept simple heartbeat packages.

        :param dto: The packet raw serialization structure object.
        :type dto: PacketDTO
        :param raw_payload: Raw configuration hex payload representation.
        :type raw_payload: str
        :param payload_len: Clean length tracking metric.
        :type payload_len: int
        :param msg: Internal compatibility envelope schema instance.
        :type msg: _LegacyMessage
        :returns: Extracted parser structural outputs.
        :rtype: PayloadT | list[PayloadT] | None
        """
        code = _get_code(dto.code)
        if not msg._has_payload and (
            dto.verb.strip() == RQ and code not in RQ_INDEX_COMPLEX
        ):
            return None

        if payload_len == 1 and raw_payload == "00" and dto.code != Code._1FC9:
            try:
                parser = get_parser(dto.code) or parse_unknown_payload
                result = parser(raw_payload, msg)
                if result == {}:
                    return None
            except (
                exc.ParserBaseError,
                IndexError,
                KeyError,
                TypeError,
                ValueError,
            ) as err:
                _LOGGER.debug(
                    "Parser for code %s failed, falling back to heartbeat: %s",
                    dto.code,
                    err,
                )
            return parse_heartbeat_payload(raw_payload, msg)

        if self._next_decoder:
            return self._next_decoder.decode(
                dto, raw_payload, payload_len, msg
            )
        return {}


def _convert_to_dict(  # type: ignore[explicit-any]  # duck-typed payload object
    payload_obj: Any, msg: _LegacyMessage
) -> Any:
    """Invoke payload.to_dict safely passing msg context if supported."""
    to_dict_fn = getattr(payload_obj, "to_dict", None)
    if not callable(to_dict_fn):
        return None
    try:
        return to_dict_fn(msg=msg)
    except TypeError:
        return to_dict_fn()


def _inject_header_metadata(
    res_dict: PayloadT, msg: _LegacyMessage, dto: PacketDTO
) -> PayloadT:
    """Inject packet header index metadata and sequence number into dictionary."""
    merged = _build_index_dict(msg)
    merged.update(res_dict)
    if dto.seq and dto.seq.isnumeric():
        merged["seqx_num"] = dto.seq
    return merged


class DataclassPayloadDecoder(PayloadDecoder):
    """Active decoder using registered PayloadBase dataclass parsers."""

    def _aggregate_zone_devices(
        self,
        payload_list: list[PayloadBase],
        msg: _LegacyMessage,
        dto: PacketDTO,
    ) -> PayloadT | None:
        """Combine multi-chunk 000C device binding items into a single dictionary."""
        dev_ids: list[str] = []
        base_dict: PayloadT | None = None
        for item in payload_list:
            if isinstance(d := _convert_to_dict(item, msg), dict):
                if base_dict is None:
                    base_dict = dict(d)
                dev_ids.extend(d.get("devices", []))
        if base_dict is not None:
            base_dict["devices"] = dev_ids
            return _inject_header_metadata(base_dict, msg, dto)
        return None

    def decode(
        self,
        dto: PacketDTO,
        raw_payload: str,
        payload_len: int,
        msg: _LegacyMessage,
    ) -> PayloadT | list[PayloadT] | None:
        """Decode raw payload bytes into typed dataclass instances.

        :param dto: The packet raw serialization structure object.
        :type dto: PacketDTO
        :param raw_payload: Raw configuration hex payload representation.
        :type raw_payload: str
        :param payload_len: Clean length tracking metric.
        :type payload_len: int
        :param msg: Internal compatibility envelope schema instance.
        :type msg: _LegacyMessage
        :returns: Extracted parser structural outputs.
        :rtype: PayloadT | list[PayloadT] | None
        """
        payload_cls = get_payload_class(dto.code)

        if payload_cls is not None and raw_payload:
            try:
                raw_bytes = bytes.fromhex(raw_payload)
                decoded_payload = payload_cls.from_bytes(raw_bytes)

                if isinstance(decoded_payload, list):
                    if dto.code == Code._000C:
                        return self._aggregate_zone_devices(
                            decoded_payload, msg, dto
                        )

                    converted_payloads: list[PayloadT] = []
                    for item in decoded_payload:
                        if isinstance(
                            converted_dict := _convert_to_dict(item, msg), dict
                        ):
                            converted_payloads.append(
                                _inject_header_metadata(
                                    converted_dict, msg, dto
                                )
                            )
                        elif converted_dict is not None:
                            converted_payloads.append(converted_dict)
                        else:
                            if self._next_decoder is not None:
                                return self._next_decoder.decode(
                                    dto, raw_payload, payload_len, msg
                                )
                            return None
                    return converted_payloads

                if isinstance(
                    converted_dict := _convert_to_dict(decoded_payload, msg),
                    dict,
                ):
                    return _inject_header_metadata(converted_dict, msg, dto)
                elif isinstance(converted_dict, list) and converted_dict:
                    return converted_dict

            except (ValueError, struct.error, TypeError, KeyError) as err:
                if not msg._has_payload:
                    return {}
                _LOGGER.warning(
                    "Dataclass decoding failed for opcode %s: %s < %s",
                    dto.code,
                    err,
                    _UNKNOWN_PACKET_HELP_MSG,
                )
                raise exc.PacketPayloadInvalid(
                    f"Dataclass decoding failed for opcode {dto.code}: {err}"
                ) from err

        if self._next_decoder is not None:
            return self._next_decoder.decode(
                dto, raw_payload, payload_len, msg
            )
        return None


class LegacyParserDecoder(PayloadDecoder):
    """Decoder routing payload to the appropriate 4-digit code parser."""

    def decode(
        self,
        dto: PacketDTO,
        raw_payload: str,
        payload_len: int,
        msg: _LegacyMessage,
    ) -> PayloadT | list[PayloadT] | None:
        """Route payload to the appropriate 4-digit code parser.

        :param dto: The packet raw serialization structure object.
        :type dto: PacketDTO
        :param raw_payload: Raw configuration hex payload representation.
        :type raw_payload: str
        :param payload_len: Clean length tracking metric.
        :type payload_len: int
        :param msg: Internal compatibility envelope schema instance.
        :type msg: _LegacyMessage
        :returns: Extracted parser structural outputs.
        :rtype: PayloadT | list[PayloadT] | None
        """
        if not raw_payload:
            return {}

        try:
            parser = get_parser(dto.code) or parse_unknown_payload
            result = parser(raw_payload, msg)

            if isinstance(result, dict) and dto.seq and dto.seq.isnumeric():
                result["seqx_num"] = dto.seq

            if isinstance(result, list):
                return result
            if isinstance(result, dict):
                return result
            return {}
        except (AssertionError, ValueError) as err:
            err_result = {
                "_payload": raw_payload,
                "_parse_error": f"{err.__class__.__name__}: {err}",
                "_unknown_code": dto.code,
            }

            if dto.seq and dto.seq.isnumeric():
                err_result["seqx_num"] = dto.seq
            return err_result


class PayloadDecoderPipeline:
    """The Chain of Responsibility pipeline for decoding DTO payloads."""

    def __init__(self) -> None:
        """Initialize pipeline linking specific system validation decoders."""
        self.head = HeartbeatDecoder()
        self.head.set_next(DataclassPayloadDecoder()).set_next(
            LegacyParserDecoder()
        )

    def decode(self, dto: PacketDTO) -> PayloadT | list[PayloadT] | None:
        """Route tracking models cleanly through downstream chain decoders.

        :param dto: The network transfer packet state container model.
        :type dto: PacketDTO
        :returns: Fully structured mapping outputs or null evaluations.
        :rtype: PayloadT | list[PayloadT] | None
        """
        raw_payload: str = dto.raw_payload
        try:
            payload_len: int = int(dto.length)
        except ValueError:
            payload_len = 0

        msg = _LegacyMessage(dto)

        # 1. Parsing Phase (Catches and suppresses exceptions for null payloads)
        try:
            result = self.head.decode(dto, raw_payload, payload_len, msg)
        except exc.PacketPayloadInvalid as err:
            if not msg._has_payload:
                result = {}
            else:
                raise err
        except exc.PacketInvalid as err:
            raise err
        except AssertionError as err:
            raise exc.PacketInvalid(f"Bad packet: {err}") from err
        except (AttributeError, LookupError, TypeError, ValueError) as err:
            raise exc.PacketInvalid(f"Coding error: {err}") from err
        except NotImplementedError as err:
            raise exc.PacketInvalid("Unknown packet code") from err

        # 2. Evaluation Phase
        if result is None:
            return {}
        if isinstance(result, list):
            return result

        # 3. Index Injection Phase (Errors raised here will bypass the null-payload swallow)
        try:
            index_dict = _build_index_dict(msg)
            return {**index_dict, **result}
        except exc.PacketInvalid as err:
            raise err


def decode_packet(dto: PacketDTO) -> PayloadT | list[PayloadT]:
    """Entry point for the new DTO-based payload decoder.

    :param dto: The network protocol target transfer object packet frame.
    :type dto: PacketDTO
    :returns: Processed semantic runtime dictionary details.
    :rtype: PayloadT | list[PayloadT]
    """
    pipeline = PayloadDecoderPipeline()
    result = pipeline.decode(dto)

    if result is None:
        return {}

    return result
