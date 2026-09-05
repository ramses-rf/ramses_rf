#!/usr/bin/env python3
"""RAMSES RF - a RAMSES-II protocol decoder & analyser.

Decode and process ingested transport packets.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import datetime as dt, timedelta as td
from typing import Any

import orjson

from . import exceptions as exc
from .address import ALL_DEV_ADDR, NON_DEV_ADDR, Address, packet_addrs
from .const import I_, RAW_LINE_REGEX, RP, W_, Code, Verb
from .dtos import CommandDTO, PacketDTO
from .logger import getLogger
from .typing import HeaderT, PayloadT

_LOGGER = logging.getLogger(__name__)
PKT_LOGGER = getLogger(f"{__name__}_log", packet_log=True)

# evofw3/ramses_esp output unsigned positive (10–138, negated CC1101
# signed byte).  Values > 138 are signed-as-unsigned from custom
# firmware (e.g. 202 = -54 dBm).  See ramses-rf/ramses_cc#1046.
_RSSI_UNSIGNED_MAX = 138


def _normalise_rssi(raw: str) -> str:
    """Normalise an RSSI string to signed dBm.

    Accepts three input formats and returns signed dBm:

    - ``074`` (unsigned positive, evofw3/ramses_esp/HGI80) → ``-74``
    - ``202`` (signed-as-unsigned, custom firmware) → ``-54``
    - ``-54`` (already signed dBm) → ``-54``

    Sentinel values (``...``, ``---``, ``///``) are returned as-is.

    :param raw: 3-character RSSI string from the raw packet line.
    :returns: Normalised RSSI string in signed dBm.
    """
    if not raw or raw in ("...", "---", "///"):
        return raw
    with contextlib.suppress(ValueError):
        val = int(raw)
        if val == 0:
            return "..."
        if val > _RSSI_UNSIGNED_MAX:
            # Signed-as-unsigned (custom firmware): 202 → -54
            return str(val - 256)
        if val >= 0:
            # Unsigned positive (evofw3/ramses_esp/HGI80): 074 → -74
            return str(-val)
        # Already signed dBm
        return raw
    return raw


class Packet:
    """Stateful L3 transport envelope wrapping an immutable PacketDTO.

    Traps and logs invalid packets, parses raw ASCII line structures, and
    provides positional address access and DTO conversion.
    """

    # Limit instance memory overhead and accelerate attribute access for
    # high-volume objects
    __slots__ = (
        "_dto",
        "_src",
        "_dst",
        "addr1",
        "addr2",
        "addr3",
        "_addrs",
        "comment",
        "error_text",
        "raw_line",
        "raw_frame",
        "_raw_line",
        "_ctx_",
        "_hdr_",
        "_index_",
        "_repr",
        "_lifespan",
        "_is_echo",
        "_is_tx",
        "_ingress_hgi_id",
    )

    _dto: PacketDTO
    _src: Address
    _dst: Address
    addr1: Address
    addr2: Address
    addr3: Address
    _addrs: tuple[Address, Address, Address]

    comment: str
    error_text: str
    raw_line: str
    raw_frame: bytes
    _raw_line: str | None

    _ctx_: str | bool | None
    _hdr_: HeaderT | None
    _index_: str | bool | None
    _repr: str | None
    _lifespan: bool | td
    _is_echo: bool
    _is_tx: bool
    _ingress_hgi_id: str | None

    def __init__(
        self,
        dto_or_dtm: PacketDTO | dt,
        raw_line: str = "",
        /,
        *,
        comment: str = "",
        error_message: str = "",
        raw_frame: bytes | str = b"",
        is_echo: bool = False,
        is_tx: bool = False,
    ) -> None:
        """Create a packet from a PacketDTO or timestamp + raw_line string.

        :param dto_or_dtm: Pre-parsed PacketDTO or received timestamp
        :type dto_or_dtm: PacketDTO | dt
        :param raw_line: Unparsed raw ASCII line string if dto_or_dtm is dt
        :type raw_line: str
        :param comment: Optional comment extracted from log line
        :type comment: str
        :param error_message: Optional error message from parser
        :type error_message: str
        :param raw_frame: Raw physical bytes sequence from hardware interface
        :type raw_frame: bytes | str
        :param is_echo: True if packet is a serial hardware echo
        :type is_echo: bool
        :param is_tx: True if packet is an outbound transmission
        :type is_tx: bool
        :returns: None
        :rtype: None
        :raises PacketInvalid: If raw_line content is malformed
        """
        if isinstance(dto_or_dtm, dt):
            constructed = self.from_raw_line(
                dto_or_dtm,
                raw_line,
                comment=comment,
                error_message=error_message,
                raw_frame=raw_frame,
                is_echo=is_echo,
                is_tx=is_tx,
            )
            self._dto = constructed._dto
            self._src = constructed._src
            self._dst = constructed._dst
            self.addr1 = constructed.addr1
            self.addr2 = constructed.addr2
            self.addr3 = constructed.addr3
            self._addrs = constructed._addrs
            self.comment = constructed.comment
            self.error_text = constructed.error_text
            self.raw_line = constructed.raw_line
            self.raw_frame = constructed.raw_frame
            self._raw_line = getattr(constructed, "_raw_line", None)
            self._ctx_ = None
            self._hdr_ = None
            self._index_ = None
            self._repr = None
            self._lifespan = False
            self._ingress_hgi_id = None
            return

        self._dto = dto_or_dtm
        self.comment = comment
        self.error_text = error_message
        self.raw_line = raw_line
        if isinstance(raw_frame, str):
            self.raw_frame = raw_frame.encode("ascii", errors="replace")
        elif raw_frame:
            self.raw_frame = raw_frame
        else:
            self.raw_frame = raw_line.encode("ascii", errors="replace")

        self._raw_line = (
            f"{self._dto.verb} {self.seqn} {self._dto.addr1} "
            f"{self._dto.addr2} {self._dto.addr3} {self._dto.code} "
            f"{self._dto.length} {self._dto.payload}"
        )

        try:
            (
                self._src,
                self._dst,
                self.addr1,
                self.addr2,
                self.addr3,
            ) = packet_addrs(
                f"{self._dto.addr1} {self._dto.addr2} {self._dto.addr3}"
            )
            self._addrs = (self.addr1, self.addr2, self.addr3)
        except exc.PacketInvalid as err:
            raise exc.PacketInvalid("Bad frame: Invalid address set") from err

        self._ctx_ = None
        self._hdr_ = None
        self._index_ = None
        self._repr = None
        self._lifespan = False
        self._ingress_hgi_id = None

        self._validate(strict_checking=False)

    @classmethod
    def from_raw_line(
        cls,
        dtm: dt | str,
        raw_line: str,
        *,
        comment: str = "",
        error_message: str = "",
        raw_frame: bytes | str = b"",
        is_echo: bool = False,
        is_tx: bool = False,
    ) -> Packet:
        """Canonical factory for ingesting unparsed raw ASCII line sequences.

        :param dtm: Timestamp object or ISO format timestamp string
        :type dtm: dt | str
        :param raw_line: Unparsed raw ASCII line from wire/log file
        :type raw_line: str
        :param comment: Optional comment string
        :type comment: str
        :param error_message: Optional error text string
        :type error_message: str
        :param raw_frame: Raw physical bytes sequence from hardware interface
        :type raw_frame: bytes | str
        :param is_echo: True if packet is a serial hardware echo
        :type is_echo: bool
        :param is_tx: True if packet is an outbound transmission
        :type is_tx: bool
        :returns: Instantiated Packet object
        :rtype: Packet
        :raises ValueError: If raw_line string is empty
        :raises PacketInvalid: If raw_line layout or payload is invalid
        """
        parsed_dtm = dt.fromisoformat(dtm) if isinstance(dtm, str) else dtm
        line_body, extracted_err, extracted_comment = cls._partition(raw_line)
        if not line_body:
            if comment or extracted_comment:
                raise exc.PacketInvalid("Null packet")
            raise ValueError(f"null frame: >>>{line_body}<<<")

        line = line_body.strip()
        if (
            len(line) >= 4
            and line[3] == " "
            and (
                line[:3].isdigit()
                or line[:3] in ("...", "---", "///")
                or (line[0] == "-" and line[1:3].isdigit())
            )
        ):
            rssi = _normalise_rssi(line[:3])
            raw_line_body = line[4:]
        else:
            rssi = "..."
            raw_line_body = line

        if not RAW_LINE_REGEX.match(raw_line_body):
            raise exc.PacketInvalid(
                f"Bad frame: Invalid structure: >>>{raw_line_body}<<<"
            )

        fields = raw_line_body.lstrip().split(" ")
        if len(fields) < 8:
            raise exc.PacketInvalid(
                f"Bad frame: Insufficient fields: >>>{raw_line_body}<<<"
            )

        verb = raw_line_body[:2]
        sequence_number = fields[1]
        addr1 = fields[2]
        addr2 = fields[3]
        addr3 = fields[4]
        code = fields[5]
        len_ = fields[6]
        payload = fields[7]

        if len(payload) != int(len_) * 2:
            raise exc.PacketInvalid(
                f"Bad frame: Invalid payload: len({payload}) is not int('{len_}' * 2))"
            )

        seq_str = sequence_number if sequence_number != "---" else ""
        rssi_str = rssi if rssi not in ("...", "---") else ""

        dto = PacketDTO(
            timestamp=parsed_dtm,
            rssi=rssi_str,
            verb=verb,
            seq=seq_str,
            addr1=addr1,
            addr2=addr2,
            addr3=addr3,
            code=code,
            length=len_,
            raw_payload=payload,
            is_tx=is_tx,
        )

        packet = cls.__new__(cls)
        packet._dto = dto
        packet._is_echo = is_echo
        packet._is_tx = is_tx
        packet.comment = comment or extracted_comment
        packet.error_text = error_message or extracted_err
        packet.raw_line = raw_line
        if isinstance(raw_frame, str):
            packet.raw_frame = raw_frame.encode("ascii", errors="replace")
        elif raw_frame:
            packet.raw_frame = raw_frame
        else:
            packet.raw_frame = raw_line.encode("ascii", errors="replace")

        packet._raw_line = raw_line_body

        try:
            (
                packet._src,
                packet._dst,
                packet.addr1,
                packet.addr2,
                packet.addr3,
            ) = packet_addrs(f"{dto.addr1} {dto.addr2} {dto.addr3}")
            packet._addrs = (packet.addr1, packet.addr2, packet.addr3)
        except exc.PacketInvalid as err:
            raise exc.PacketInvalid("Bad frame: Invalid address set") from err

        packet._ctx_ = None
        packet._hdr_ = None
        packet._index_ = None
        packet._repr = None
        packet._lifespan = False
        packet._ingress_hgi_id = None

        packet._validate(strict_checking=False)
        return packet

    @property
    def _packet_extra(self) -> dict[str, Any]:
        """Return extra dictionary attributes for PKT_LOGGER logging."""
        return {
            "_frame": getattr(self, "_frame", ""),
            "_rssi": getattr(self, "rssi", "..."),
            "error_text": getattr(self, "error_text", ""),
            "comment": getattr(self, "comment", ""),
            "dtm": getattr(self, "dtm", None),
        }

    def _validate(self, *, strict_checking: bool = False) -> None:
        """Validate the packet and emit packet log entries.

        :param strict_checking: Enforce strict address validity checks
        :type strict_checking: bool
        :returns: None
        :rtype: None
        :raises PacketInvalid: If packet or address configuration is invalid
        """
        try:
            if self.error_text:
                raise exc.PacketInvalid(self.error_text)

            if getattr(self, "_is_echo", False) is True:
                return

            if not strict_checking:
                if getattr(self, "_frame", "") or self.error_text:
                    PKT_LOGGER.info("", extra=self._packet_extra)
                return

            if self.addr1 == NON_DEV_ADDR:
                assert self.verb == I_, (
                    "wrong verb or dst addr should be present"
                )
            elif self.addr3 == NON_DEV_ADDR:
                assert self.verb == I_ or self.src is not self.dst, (
                    "wrong verb or dst addr should not be src"
                )
            elif self.addr1 == self.addr3:
                assert self.verb == I_, (
                    "wrong verb or dst addr should not be src"
                )
            else:
                assert self.verb in (I_, W_), (
                    "wrong verb or dst addr should be src"
                )

            if getattr(self, "_frame", "") or self.error_text:
                PKT_LOGGER.info("", extra=self._packet_extra)

        except AssertionError as err:
            raise exc.PacketInvalid(
                f"Bad frame: Invalid address set: {err}"
            ) from err
        except exc.PacketInvalid as err:
            if getattr(self, "_frame", "") or self.error_text:
                PKT_LOGGER.warning("%s", err, extra=self._packet_extra)
            raise err

    def __repr__(self) -> str:
        """Return an unambiguous string representation of this object.

        :returns: ISO timestamp and formatted raw line representation
        :rtype: str
        """
        if self._repr is None:
            dtm_str = (
                self.dtm.isoformat(timespec="microseconds")
                if hasattr(self, "_dto") and self._dto and self._dto.timestamp
                else dt.min.isoformat(timespec="microseconds")
            )
            try:
                hdr = f" # {self._hdr}{f' ({self._ctx})' if self._ctx else ''}"
            except (exc.PacketInvalid, NotImplementedError):
                hdr = ""
            line_str = " ".join(
                (
                    self.verb,
                    self.seqn,
                    *(repr(a) for a in self._addrs),
                    self.code,
                    self.len_,
                    self.raw_payload,
                )
            )
            self._repr = f"{dtm_str} ... {line_str}{hdr}"
        return self._repr

    def __str__(self) -> str:
        """Return a brief readable string representation of this object.

        :returns: Brief raw_line representation string
        :rtype: str
        """
        return self._frame

    def __eq__(self, other: object) -> bool:
        """Evaluate equality against another Packet or PacketDTO.

        :param other: The target object to compare
        :type other: object
        :returns: True if raw_lines match, otherwise NotImplemented/False
        :rtype: bool
        """
        if not hasattr(other, "_frame") and not hasattr(other, "_raw_line"):
            return NotImplemented
        other_line = getattr(other, "_frame", None) or getattr(
            other, "_raw_line", None
        )
        return self._frame == other_line

    @property
    def dtm(self) -> dt:
        """Return the datetime when the packet was received.

        :returns: Received timestamp
        :rtype: dt
        """
        return self._dto.timestamp

    @property
    def rssi(self) -> str:
        """Return the received signal strength indicator (RSSI).

        :returns: 3-character RSSI string
        :rtype: str
        """
        return self._dto.rssi or "..."

    @property
    def ingress_hgi_id(self) -> str | None:
        """Return the HGI that heard this frame, if known.

        Set by :class:`PooledTransport` when forwarding packets from
        child transports.  ``None`` for packets that did not arrive
        through a pool (single-HGI, log replay, etc.).

        :returns: The ingress HGI device ID, or ``None``.
        """
        return self._ingress_hgi_id

    @property
    def verb(self) -> Verb:
        """Return the action verb enum/string.

        :returns: Action verb instance
        :rtype: Verb
        """
        return self._dto.verb  # type: ignore[return-value]

    @property
    def seqn(self) -> str:
        """Return the sequence number string.

        :returns: Sequence number
        :rtype: str
        """
        return self._dto.seq or "---"

    @property
    def code(self) -> Code:
        """Return the packet code string/enum.

        :returns: Packet code
        :rtype: Code
        """
        return self._dto.code  # type: ignore[return-value]

    @property
    def len_(self) -> str:
        """Return the payload length string.

        :returns: Payload length
        :rtype: str
        """
        return self._dto.length

    @property
    def raw_payload(self) -> str:
        """Return the raw hex payload string.

        :returns: Raw ASCII hex payload string
        :rtype: str
        """
        return self._dto.raw_payload

    @property
    def payload(self) -> Any:
        """Return the typed payload dataclass object or raw hex string.

        TODO: Temporary fallback for PR 10 transition. In PR 12 of #1001,
        this will exclusively return PayloadBase instances without raw
        string fallbacks.

        :returns: Strongly-typed payload object or raw hex string fallback
        :rtype: Any
        """
        if self._dto.payload is not None:
            return self._dto.payload
        return PayloadT(self._dto.raw_payload)

    @payload.setter
    def payload(self, value: Any) -> None:
        """Set the payload object or hex string (updates internal PacketDTO).

        TODO: Temporary fallback for PR 10 transition. In PR 12 of #1001,
        this setter will accept only PayloadBase dataclass instances.

        :param value: New payload object or hex string
        :type value: Any
        """
        if isinstance(value, str):
            self._dto = PacketDTO(
                timestamp=self._dto.timestamp,
                rssi=self._dto.rssi,
                verb=self._dto.verb,
                seq=self._dto.seq,
                addr1=self._dto.addr1,
                addr2=self._dto.addr2,
                addr3=self._dto.addr3,
                code=self._dto.code,
                length=self._dto.length,
                raw_payload=str(value),
                payload=self._dto.payload,
            )
        else:
            self._dto = PacketDTO(
                timestamp=self._dto.timestamp,
                rssi=self._dto.rssi,
                verb=self._dto.verb,
                seq=self._dto.seq,
                addr1=self._dto.addr1,
                addr2=self._dto.addr2,
                addr3=self._dto.addr3,
                code=self._dto.code,
                length=self._dto.length,
                raw_payload=self._dto.raw_payload,
                payload=value,
            )
        self._raw_line = None

    @property
    def _len(self) -> int:
        """Return the payload byte count.

        :returns: Integer byte count
        :rtype: int
        """
        return int(len(self._dto.raw_payload) / 2)

    @property
    def _frame(self) -> str:
        """Return the formatted raw frame body string.

        :returns: Formatted raw ASCII frame body string
        :rtype: str
        """
        if self._raw_line is not None:
            return self._raw_line
        return (
            f"{self._dto.verb} {self.seqn} {self._dto.addr1} "
            f"{self._dto.addr2} {self._dto.addr3} {self._dto.code} "
            f"{self._dto.length} {self._dto.raw_payload}"
        )

    @_frame.setter
    def _frame(self, value: str) -> None:
        """Set the formatted raw frame body string.

        :param value: Raw ASCII frame body string
        :type value: str
        """
        self._raw_line = value

    @property
    def src(self) -> Address:
        """Return the logical source address (addr1).

        :returns: Source Address instance
        :rtype: Address
        """
        return self._src

    @property
    def dst(self) -> Address:
        """Return the logical destination address (addr2).

        :returns: Destination Address instance
        :rtype: Address
        """
        return self._dst

    @property
    def _ctx(self) -> str | bool:
        """Return the payload's context (e.g. zone_index or domain_id).

        :returns: Context index string or False if unavailable
        :rtype: str | bool
        """
        if self._ctx_ is not None:
            return self._ctx_

        if self.code in (Code._0005, Code._000C):
            self._ctx_ = self.raw_payload[:4]
        elif self.code == Code._0404:
            self._ctx_ = (
                (self.raw_payload[:2] + self.raw_payload[10:12])
                if len(self.raw_payload) >= 12
                else self.raw_payload[:2]
            )
        elif self.code in (Code._0418, Code._3220):
            self._ctx_ = (
                self.raw_payload[4:6] if len(self.raw_payload) >= 6 else False
            )
        elif len(self.raw_payload) >= 2 and self.raw_payload[:2] != "00":
            self._ctx_ = self.raw_payload[:2]
        else:
            self._ctx_ = False

        return self._ctx_

    @property
    def _index(self) -> str | bool:
        """Return the payload's index, if any.

        :returns: Index string or False
        :rtype: str | bool
        """
        if self._index_ is not None:
            return self._index_

        result = self._ctx
        self._index_ = result if isinstance(result, str) else False
        return self._index_

    @property
    def _has_payload(self) -> bool:
        """Return True if packet contains payload data beyond 1-byte header.

        :returns: True if payload is not 1-byte fallback
        :rtype: bool
        """
        return self._len > 1

    @property
    def _hdr(self) -> HeaderT:
        """Return the QoS header fingerprint of this packet.

        :returns: Formatted HeaderT instance
        :rtype: HeaderT
        """
        if self._hdr_ is not None:
            return self._hdr_

        result = packet_header(self)
        self._hdr_ = (
            result
            if result is not None
            else HeaderT(f"{self.code}|{self.verb}")
        )
        return self._hdr_

    @staticmethod
    def _partition(raw_line: str) -> tuple[str, str, str]:
        """Partition a raw packet line into line body, error text, and comment.

        :param raw_line: Raw log or port line string
        :type raw_line: str
        :returns: Tuple of (line_string, error_text, comment)
        :rtype: tuple[str, str, str]
        """
        fragment, _, comment = raw_line.partition("#")
        fragment, _, error_message = fragment.partition("*")
        packet_str, _, _ = fragment.partition("<")  # discard any parser hints

        parts = tuple(map(str.strip, (packet_str, error_message, comment)))
        return parts[0], parts[1], parts[2]

    @classmethod
    def _from_cmd(cls, command: CommandDTO, dtm: dt | None = None) -> Packet:
        """Create a Packet from a CommandDTO.

        :param command: Command DTO object
        :type command: CommandDTO
        :param dtm: Optional timestamp for packet creation
        :type dtm: dt | None
        :returns: Constructed Packet instance
        :rtype: Packet
        """
        if dtm is None:
            dtm = dt.now()
        raw_line = (
            f"{command.verb.strip():>2} --- {command.addr1} {command.addr2} {command.addr3} "
            f"{command.code} {int(len(command.payload) / 2):03d} {command.payload}"
        )
        # is_echo=True suppresses PKT_LOGGER output in _validate — this
        # Packet is an internal helper for computing tx_header, not an
        # actual packet reception, so it must not appear in the packet log.
        # Without this, every access to CommandDTO.tx_header (which happens
        # per-incoming-packet in WantEcho.packet_rcvd) logs a spurious
        # "... RQ" entry (issue 1041).
        return cls.from_raw_line(dtm, f"... {raw_line}", is_echo=True)

    def to_dto(self) -> PacketDTO:
        """Return the internal immutable PacketDTO object in O(1) time.

        :returns: Immutable PacketDTO representation
        :rtype: PacketDTO
        """
        ts = self._dto.timestamp
        if ts.tzinfo is None:
            ts = ts.astimezone()

        if self._dto.timestamp != ts:
            return PacketDTO(
                timestamp=ts,
                rssi=self._dto.rssi,
                verb=self._dto.verb,
                seq=self._dto.seq,
                addr1=self._dto.addr1,
                addr2=self._dto.addr2,
                addr3=self._dto.addr3,
                code=self._dto.code,
                length=self._dto.length,
                raw_payload=self._dto.raw_payload,
                payload=self._dto.payload,
                is_tx=self._dto.is_tx,
            )
        return self._dto

    def to_dict(
        self, parsed_payload: dict[str, Any] | list[Any] | None = None
    ) -> dict[str, Any]:
        """Serialize packet state for JSON storage and warm-restart persistence.

        :param parsed_payload: Optional parsed domain payload data
        :type parsed_payload: dict[str, Any] | list[Any] | None
        :returns: Serialized state dictionary
        :rtype: dict[str, Any]
        """
        dto = self._dto
        ts = dto.timestamp
        dtm_str = (ts.astimezone() if ts.tzinfo is None else ts).isoformat(
            timespec="microseconds"
        )

        rssi_val = dto.rssi
        if not rssi_val or rssi_val in ("...", "---"):
            rssi: int | None = None
        else:
            with contextlib.suppress(ValueError):
                rssi = int(rssi_val)

        result: dict[str, Any] = {
            "dtm": dtm_str,
            "rssi": rssi,
            "verb": dto.verb,
            "seq": dto.seq,
            "addr1": dto.addr1,
            "addr2": dto.addr2,
            "addr3": dto.addr3,
            "code": dto.code,
            "length": dto.length,
            "payload": dto.raw_payload,
            "frame": self._frame,
        }

        if parsed_payload is not None:
            result["parsed_payload"] = parsed_payload

        return result

    def to_json(self) -> bytes:
        """Serialize packet dataclass directly to JSON byte stream via orjson.

        :returns: UTF-8 encoded JSON byte stream
        :rtype: bytes
        """
        return orjson.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, dtm: str, state: dict[str, Any] | str) -> Packet:
        """Deserialize stored state dictionary or log line during warm restart.

        :param dtm: ISO timestamp string
        :type dtm: str
        :param state: Dictionary of state parameters or line string
        :type state: dict[str, Any] | str
        :returns: Instantiated Packet object
        :rtype: Packet
        """
        if isinstance(state, str):
            return cls.from_raw_line(dtm, state)

        rssi_val = state.get("rssi")
        try:
            rssi_int = int(rssi_val) if rssi_val is not None else None
        except (ValueError, TypeError):
            rssi_int = None

        if rssi_int is None or rssi_int == 0:
            rssi = "..."
        elif rssi_int > _RSSI_UNSIGNED_MAX:
            # Signed-as-unsigned (custom firmware): 202 → -54
            rssi = str(rssi_int - 256)
        elif rssi_int > 0:
            # Unsigned positive (evofw3/ramses_esp/HGI80): 74 → -74
            rssi = str(-rssi_int)
        else:
            # Already signed dBm (negative)
            rssi = str(rssi_int)

        frame_body = state.get("frame") or state.get("raw_packet", "")
        raw_line = f"{rssi[:3].ljust(3)} {frame_body}"
        return cls.from_raw_line(dtm, raw_line)

    @classmethod
    def from_json(cls, json_data: bytes | str) -> Packet:
        """Deserialize packet directly from JSON byte stream via orjson.

        :param json_data: JSON byte stream or string
        :type json_data: bytes | str
        :returns: Instantiated Packet object
        :rtype: Packet
        """
        raw: dict[str, Any] = orjson.loads(json_data)
        if "timestamp" in raw and ("raw_payload" in raw or "payload" in raw):
            raw_payload_val = raw.get("raw_payload")
            if not isinstance(raw_payload_val, str) or not raw_payload_val:
                raw_payload_val = (
                    raw["payload"]
                    if isinstance(raw.get("payload"), str)
                    else ""
                )
            dto = PacketDTO(
                timestamp=dt.fromisoformat(raw["timestamp"]),
                rssi=str(raw.get("rssi", ""))
                if raw.get("rssi") is not None
                else "",
                verb=raw.get("verb", ""),
                seq=raw.get("seq", ""),
                addr1=raw.get("addr1", ""),
                addr2=raw.get("addr2", ""),
                addr3=raw.get("addr3", ""),
                code=raw.get("code", ""),
                length=raw.get("length", ""),
                raw_payload=raw_payload_val,
            )
            return cls(dto)

        dtm = raw.get("dtm", "")
        return cls.from_dict(dtm, raw)

    @classmethod
    def from_file(
        cls,
        dtm: str,
        raw_line: str,
        *,
        is_echo: bool = False,
        is_tx: bool = False,
    ) -> Packet:
        """Create a packet from a log file line (delegates to from_raw_line).

        :param dtm: ISO timestamp string
        :type dtm: str
        :param raw_line: Log line string
        :type raw_line: str
        :param is_echo: True if packet is a serial hardware echo
        :type is_echo: bool
        :param is_tx: True if packet is an outbound transmission
        :type is_tx: bool
        :returns: Instantiated Packet object
        :rtype: Packet
        """
        return cls.from_raw_line(dtm, raw_line, is_echo=is_echo, is_tx=is_tx)

    @classmethod
    def from_port(
        cls,
        dtm: dt,
        raw_line: str,
        raw_frame: bytes | str = b"",
        *,
        is_echo: bool = False,
        is_tx: bool = False,
    ) -> Packet:
        """Create a packet from a hardware port ingestion line (delegates to from_raw_line).

        :param dtm: Packet arrival timestamp
        :type dtm: dt
        :param raw_line: Parsed text frame string
        :type raw_line: str
        :param raw_frame: Original raw bytes or string from modem
        :type raw_frame: bytes | str
        :param is_echo: True if packet is a serial hardware echo
        :type is_echo: bool
        :param is_tx: True if packet is an outbound transmission
        :type is_tx: bool
        :returns: Instantiated Packet object
        :rtype: Packet
        """
        return cls.from_raw_line(
            dtm, raw_line, raw_frame=raw_frame, is_echo=is_echo, is_tx=is_tx
        )


def packet_header(packet: Packet, /) -> HeaderT | None:
    """Return the QoS header fingerprint of a packet.

    :param packet: Packet instance to evaluate
    :type packet: Packet
    :returns: Header fingerprint string or None
    :rtype: HeaderT | None
    """
    if packet.code == Code._1FC9:
        device_id = (
            ALL_DEV_ADDR.id if packet.src == packet.dst else packet.dst.id
        )
        return HeaderT("|".join((packet.code, packet.verb, device_id)))

    if packet.verb in (I_, RP) or packet.src == packet.dst:
        header = "|".join((packet.code, packet.verb, packet.src.id))
    else:
        header = "|".join((packet.code, packet.verb, packet.dst.id))

    try:
        return HeaderT(
            f"{header}|{packet._ctx}"
            if isinstance(packet._ctx, str)
            else header
        )
    except AssertionError:
        return HeaderT(header)
