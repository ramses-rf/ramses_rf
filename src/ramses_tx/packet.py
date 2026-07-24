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
from .address import ALL_DEV_ADDR, NON_DEV_ADDR, Address, pkt_addrs
from .const import I_, RAW_LINE_REGEX, RP, W_, Code, VerbT
from .dtos import CommandDTO, PacketDTO
from .logger import getLogger
from .typing import HeaderT, PayloadT

_LOGGER = logging.getLogger(__name__)
PKT_LOGGER = getLogger(f"{__name__}_log", pkt_log=True)


class Packet:
    """Stateful L3 transport envelope wrapping an immutable PacketDTO.

    Traps and logs invalid packets, parses raw ASCII line structures, and
    provides positional address access and DTO conversion.
    """

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
    _idx_: str | bool | None
    _repr: str | None
    _lifespan: bool | td

    def __init__(
        self,
        dto_or_dtm: PacketDTO | dt,
        raw_line: str = "",
        /,
        *,
        comment: str = "",
        err_msg: str = "",
        raw_frame: bytes | str = b"",
    ) -> None:
        """Create a packet from a PacketDTO or timestamp + raw_line string.

        :param dto_or_dtm: Pre-parsed PacketDTO or received timestamp
        :type dto_or_dtm: PacketDTO | dt
        :param raw_line: Unparsed raw ASCII line string if dto_or_dtm is dt
        :type raw_line: str
        :param comment: Optional comment extracted from log line
        :type comment: str
        :param err_msg: Optional error message from parser
        :type err_msg: str
        :param raw_frame: Raw physical bytes sequence from hardware interface
        :type raw_frame: bytes | str
        :returns: None
        :rtype: None
        :raises PacketInvalid: If raw_line content is malformed
        """
        if isinstance(dto_or_dtm, dt):
            constructed = self.from_raw_line(
                dto_or_dtm,
                raw_line,
                comment=comment,
                err_msg=err_msg,
                raw_frame=raw_frame,
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
            self._idx_ = None
            self._repr = None
            self._lifespan = False
            return

        self._dto = dto_or_dtm
        self.comment = comment
        self.error_text = err_msg
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
            ) = pkt_addrs(f"{self._dto.addr1} {self._dto.addr2} {self._dto.addr3}")
            self._addrs = (self.addr1, self.addr2, self.addr3)
        except exc.PacketInvalid as err:
            raise exc.PacketInvalid("Bad frame: Invalid address set") from err

        self._ctx_ = None
        self._hdr_ = None
        self._idx_ = None
        self._repr = None
        self._lifespan = False

        self._validate(strict_checking=False)

    @classmethod
    def from_raw_line(
        cls,
        dtm: dt | str,
        raw_line: str,
        *,
        comment: str = "",
        err_msg: str = "",
        raw_frame: bytes | str = b"",
    ) -> Packet:
        """Canonical factory for ingesting unparsed raw ASCII line sequences.

        :param dtm: Timestamp object or ISO format timestamp string
        :type dtm: dt | str
        :param raw_line: Unparsed raw ASCII line from wire/log file
        :type raw_line: str
        :param comment: Optional comment string
        :type comment: str
        :param err_msg: Optional error text string
        :type err_msg: str
        :param raw_frame: Raw physical bytes sequence from hardware interface
        :type raw_frame: bytes | str
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
            and (line[:3].isdigit() or line[:3] in ("...", "---", "///"))
        ):
            rssi = line[:3]
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
        seqn = fields[1]
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

        seq_str = seqn if seqn != "---" else ""
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
            payload=payload,
        )

        pkt = cls.__new__(cls)
        pkt._dto = dto
        pkt.comment = comment or extracted_comment
        pkt.error_text = err_msg or extracted_err
        pkt.raw_line = raw_line
        if isinstance(raw_frame, str):
            pkt.raw_frame = raw_frame.encode("ascii", errors="replace")
        elif raw_frame:
            pkt.raw_frame = raw_frame
        else:
            pkt.raw_frame = raw_line.encode("ascii", errors="replace")

        pkt._raw_line = raw_line_body

        try:
            (
                pkt._src,
                pkt._dst,
                pkt.addr1,
                pkt.addr2,
                pkt.addr3,
            ) = pkt_addrs(f"{dto.addr1} {dto.addr2} {dto.addr3}")
            pkt._addrs = (pkt.addr1, pkt.addr2, pkt.addr3)
        except exc.PacketInvalid as err:
            raise exc.PacketInvalid("Bad frame: Invalid address set") from err

        pkt._ctx_ = None
        pkt._hdr_ = None
        pkt._idx_ = None
        pkt._repr = None
        pkt._lifespan = False

        pkt._validate(strict_checking=False)
        return pkt

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

            if not strict_checking:
                if len(self.__dict__) > 0:
                    PKT_LOGGER.info("", extra=self.__dict__)
                return

            if self.addr1 == NON_DEV_ADDR:
                assert self.verb == I_, "wrong verb or dst addr should be present"
            elif self.addr3 == NON_DEV_ADDR:
                assert self.verb == I_ or self.src is not self.dst, (
                    "wrong verb or dst addr should not be src"
                )
            elif self.addr1 == self.addr3:
                assert self.verb == I_, "wrong verb or dst addr should not be src"
            else:
                assert self.verb in (I_, W_), "wrong verb or dst addr should be src"

            if len(self.__dict__) > 0:
                PKT_LOGGER.info("", extra=self.__dict__)

        except AssertionError as err:
            raise exc.PacketInvalid(f"Bad frame: Invalid address set: {err}") from err
        except exc.PacketInvalid as err:
            if getattr(self, "_frame", "") or self.error_text:
                PKT_LOGGER.warning("%s", err, extra=self.__dict__)
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
                    self.payload,
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
        other_line = getattr(other, "_frame", None) or getattr(other, "_raw_line", None)
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
    def verb(self) -> VerbT:
        """Return the action verb enum/string.

        :returns: Action verb instance
        :rtype: VerbT
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
    def payload(self) -> PayloadT:
        """Return the raw payload string.

        :returns: PayloadT hex string
        :rtype: PayloadT
        """
        return PayloadT(self._dto.payload)

    @payload.setter
    def payload(self, value: PayloadT | str) -> None:
        """Set the payload string (updates internal PacketDTO).

        :param value: New payload hex string
        :type value: PayloadT | str
        """
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
            payload=str(value),
        )
        self._raw_line = None

    @property
    def _len(self) -> int:
        """Return the payload byte count.

        :returns: Integer byte count
        :rtype: int
        """
        return int(len(self._dto.payload) / 2)

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
            f"{self._dto.length} {self._dto.payload}"
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
        """Return the payload's context (e.g. zone_idx or domain_id).

        :returns: Context index string or False if unavailable
        :rtype: str | bool
        """
        if self._ctx_ is not None:
            return self._ctx_

        if self.code in (Code._0005, Code._000C):
            self._ctx_ = self.payload[:4]
        elif self.code == Code._0404:
            self._ctx_ = (
                (self.payload[:2] + self.payload[10:12])
                if len(self.payload) >= 12
                else self.payload[:2]
            )
        elif self.code in (Code._0418, Code._3220):
            self._ctx_ = self.payload[4:6] if len(self.payload) >= 6 else False
        elif len(self.payload) >= 2 and self.payload[:2] != "00":
            self._ctx_ = self.payload[:2]
        else:
            self._ctx_ = False

        return self._ctx_

    @property
    def _idx(self) -> str | bool:
        """Return the payload's index, if any.

        :returns: Index string or False
        :rtype: str | bool
        """
        if self._idx_ is not None:
            return self._idx_

        res = self._ctx
        self._idx_ = res if isinstance(res, str) else False
        return self._idx_

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

        res = pkt_header(self)
        self._hdr_ = res if res is not None else HeaderT(f"{self.code}|{self.verb}")
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
        fragment, _, err_msg = fragment.partition("*")
        pkt_str, _, _ = fragment.partition("<")  # discard any parser hints

        parts = tuple(map(str.strip, (pkt_str, err_msg, comment)))
        return parts[0], parts[1], parts[2]

    @classmethod
    def _from_cmd(cls, cmd: CommandDTO, dtm: dt | None = None) -> Packet:
        """Create a Packet from a CommandDTO.

        :param cmd: Command DTO object
        :type cmd: CommandDTO
        :param dtm: Optional timestamp for packet creation
        :type dtm: dt | None
        :returns: Constructed Packet instance
        :rtype: Packet
        """
        if dtm is None:
            dtm = dt.now()
        raw_line = (
            f"{cmd.verb.strip():>2} --- {cmd.addr1} {cmd.addr2} {cmd.addr3} "
            f"{cmd.code} {int(len(cmd.payload) / 2):03d} {cmd.payload}"
        )
        return cls.from_raw_line(dtm, f"... {raw_line}")

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
                payload=self._dto.payload,
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

        res: dict[str, Any] = {
            "dtm": dtm_str,
            "rssi": rssi,
            "verb": dto.verb,
            "seq": dto.seq,
            "addr1": dto.addr1,
            "addr2": dto.addr2,
            "addr3": dto.addr3,
            "code": dto.code,
            "length": dto.length,
            "payload": dto.payload,
            "frame": self._frame,
        }

        if parsed_payload is not None:
            res["parsed_payload"] = parsed_payload

        return res

    def to_json(self) -> bytes:
        """Serialize packet dataclass directly to JSON byte stream via orjson.

        :returns: UTF-8 encoded JSON byte stream
        :rtype: bytes
        """
        return orjson.dumps(self.to_dto())

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
            rssi = f"{int(rssi_val):03d}" if rssi_val is not None else "..."
        except (ValueError, TypeError):
            rssi = "..."

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
        if "timestamp" in raw and "payload" in raw:
            dto = PacketDTO(
                timestamp=dt.fromisoformat(raw["timestamp"]),
                rssi=raw.get("rssi", ""),
                verb=raw.get("verb", ""),
                seq=raw.get("seq", ""),
                addr1=raw.get("addr1", ""),
                addr2=raw.get("addr2", ""),
                addr3=raw.get("addr3", ""),
                code=raw.get("code", ""),
                length=raw.get("length", ""),
                payload=raw.get("payload", ""),
            )
            return cls(dto)

        dtm = raw.get("dtm", "")
        return cls.from_dict(dtm, raw)

    @classmethod
    def from_file(cls, dtm: str, raw_line: str) -> Packet:
        """Create a packet from a log file line (delegates to from_raw_line).

        :param dtm: ISO timestamp string
        :type dtm: str
        :param raw_line: Log line string
        :type raw_line: str
        :returns: Instantiated Packet object
        :rtype: Packet
        """
        return cls.from_raw_line(dtm, raw_line)

    @classmethod
    def from_port(cls, dtm: dt, raw_line: str, raw_frame: bytes | str = b"") -> Packet:
        """Create a packet from a hardware port ingestion line (delegates to from_raw_line).

        :param dtm: Packet arrival timestamp
        :type dtm: dt
        :param raw_line: Parsed text frame string
        :type raw_line: str
        :param raw_frame: Original raw bytes or string from modem
        :type raw_frame: bytes | str
        :returns: Instantiated Packet object
        :rtype: Packet
        """
        return cls.from_raw_line(dtm, raw_line, raw_frame=raw_frame)


def pkt_header(pkt: Packet, /) -> HeaderT | None:
    """Return the QoS header fingerprint of a packet.

    :param pkt: Packet instance to evaluate
    :type pkt: Packet
    :returns: Header fingerprint string or None
    :rtype: HeaderT | None
    """
    if pkt.code == Code._1FC9:
        device_id = ALL_DEV_ADDR.id if pkt.src == pkt.dst else pkt.dst.id
        return HeaderT("|".join((pkt.code, pkt.verb, device_id)))

    if pkt.verb in (I_, RP) or pkt.src == pkt.dst:
        header = "|".join((pkt.code, pkt.verb, pkt.src.id))
    else:
        header = "|".join((pkt.code, pkt.verb, pkt.dst.id))

    try:
        return HeaderT(f"{header}|{pkt._ctx}" if isinstance(pkt._ctx, str) else header)
    except AssertionError:
        return HeaderT(header)
