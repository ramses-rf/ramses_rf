#!/usr/bin/env python3
"""RAMSES RF - Decode/process a message (payload into JSON)."""

from __future__ import annotations

import logging
import re
from datetime import datetime as dt, timedelta as td
from functools import lru_cache
from typing import TYPE_CHECKING

from . import exceptions as exc
from .address import Address
from .command import Command
from .const import DEV_TYPE_MAP, SZ_DHW_IDX, SZ_DOMAIN_ID, SZ_UFH_IDX, SZ_ZONE_IDX
from .packet import Packet
from .parsers import parse_payload
from .ramses import CODE_IDX_ARE_COMPLEX, CODES_SCHEMA, RQ_IDX_COMPLEX

# TODO:
# long-format msg.__str__ - alias columns don't line up


from .const import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    I_,
    RP,
    RQ,
    W_,
    Code,
)

if TYPE_CHECKING:
    from ramses_rf import Gateway

    from .const import IndexT, VerbT  # noqa: F401, pylint: disable=unused-import


__all__ = ["Message"]


CODE_NAMES = {k: v["name"] for k, v in CODES_SCHEMA.items()}

MSG_FORMAT_10 = "|| {:10s} | {:10s} | {:2s} | {:16s} | {:^4s} || {}"

_TD_SECS_003 = td(seconds=3)


_LOGGER = logging.getLogger(__name__)


class MessageBase:
    """The Message class; will trap/log invalid msgs."""

    def __init__(self, pkt: Packet) -> None:
        """Create a message from a valid packet.

        Will raise InvalidPacketError if it is invalid.
        """

        self._pkt = pkt

        self.src: Address = pkt.src
        self.dst: Address = pkt.dst
        self._addrs: tuple[Address, Address, Address] = pkt._addrs

        self.dtm: dt = pkt.dtm

        self.verb: VerbT = pkt.verb
        self.seqn: str = pkt.seqn
        self.code: Code = pkt.code
        self.len: int = pkt._len

        self._payload = self._validate(self._pkt.payload)  # ? raise InvalidPacketError

        self._str: str = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        """Return an unambiguous string representation of this object."""
        return str(self._pkt)  # repr or str?

    def __str__(self) -> str:
        """Return a brief readable string representation of this object."""

        def ctx(pkt: Packet) -> str:
            ctx = {True: "[..]", False: "", None: "??"}.get(pkt._ctx, pkt._ctx)  # type: ignore[arg-type]
            if not ctx and pkt.payload[:2] not in ("00", "FF"):
                return f"({pkt.payload[:2]})"
            return ctx

        if self._str is not None:
            return self._str

        if self.src.id == self._addrs[0].id:  # type: ignore[unreachable]
            name_0 = self._name(self.src)
            name_1 = "" if self.dst is self.src else self._name(self.dst)
        else:
            name_0 = ""
            name_1 = self._name(self.src)

        code_name = CODE_NAMES.get(self.code, f"unknown_{self.code}")
        self._str = MSG_FORMAT_10.format(
            name_0, name_1, self.verb, code_name, ctx(self._pkt), self.payload
        )
        return self._str

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Message):
            return NotImplemented
        return (self.src, self.dst, self.verb, self.code, self._pkt.payload) == (
            other.src,
            other.dst,
            other.verb,
            other.code,
            other._pkt.payload,
        )

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Message):
            return NotImplemented
        return self.dtm < other.dtm

    def _name(self, addr: Address) -> str:
        """Return a friendly name for an Address, or a Device."""
        return f" {addr.id}"  # can't do 'CTL:123456' instead of ' 01:123456'

    @property
    def payload(self):  # type: ignore[no-untyped-def]  # FIXME -> dict | list:
        """Return the payload."""
        return self._payload

    @property
    def _has_payload(self) -> bool:
        """Return False if there is no payload (may falsely Return True).

        The message (i.e. the raw payload) may still have an idx.
        """

        return self._pkt._has_payload

    @property
    def _has_array(self) -> bool:
        """Return True if the message's raw payload is an array."""

        return bool(self._pkt._has_array)

    @property
    def _idx(self) -> dict[str, str]:
        """Return the domain_id/zone_idx/other_idx of a message payload, if any.

        Used to identify the zone/domain that a message applies to. Returns an empty
        dict if there is none such, or None if undetermined.
        """

        # .I --- 01:145038 --:------ 01:145038 3B00 002 FCC8

        IDX_NAMES = {
            Code._0002: "other_idx",  # non-evohome: hometronics
            Code._10A0: SZ_DHW_IDX,  # can be 2 DHW zones per system, albeit unusual
            Code._1260: SZ_DHW_IDX,  # can be 2 DHW zones per system, albeit unusual
            Code._1F41: SZ_DHW_IDX,  # can be 2 DHW zones per system, albeit unusual
            Code._22C9: SZ_UFH_IDX,  # UFH circuit
            Code._2389: "other_idx",  # anachronistic
            Code._2D49: "other_idx",  # non-evohome: hometronics
            Code._31D9: "hvac_id",
            Code._31DA: "hvac_id",
            Code._3220: "msg_id",
        }  # ALSO: SZ_DOMAIN_ID, SZ_ZONE_IDX

        if self.code in (Code._31D9, Code._31DA):  # shouldn't be needed?
            assert isinstance(self._pkt._idx, str)  # mypy hint
            return {"hvac_id": self._pkt._idx}

        if self._pkt._idx in (True, False) or self.code in CODE_IDX_ARE_COMPLEX:
            return {}  # above was: CODE_IDX_COMPLEX + (Code._3150):

        if self.code in (Code._3220,):  # FIXME: should be _SIMPLE
            return {}

        # .I 068 03:201498 --:------ 03:201498 30C9 003 0106D6 # rare

        # .I --- 00:034798 --:------ 12:126457 2309 003 0201F4
        if not {self.src.type, self.dst.type} & {
            DEV_TYPE_MAP.CTL,
            DEV_TYPE_MAP.UFC,
            DEV_TYPE_MAP.HCW,  # ?remove (see above, rare)
            DEV_TYPE_MAP.DTS,
            DEV_TYPE_MAP.HGI,
            DEV_TYPE_MAP.DT2,
            DEV_TYPE_MAP.PRG,
        }:  # FIXME: DEX should be deprecated to use device type rather than class
            assert self._pkt._idx == "00", "What!! (AA)"
            return {}

        # .I 035 --:------ --:------ 12:126457 30C9 003 017FFF
        if self.src.type == self.dst.type and self.src.type not in (
            DEV_TYPE_MAP.CTL,
            DEV_TYPE_MAP.UFC,
            DEV_TYPE_MAP.HCW,  # ?remove (see above, rare)
            DEV_TYPE_MAP.HGI,
            DEV_TYPE_MAP.PRG,
        ):  # DEX
            assert self._pkt._idx == "00", "What!! (AB)"
            return {}

        # .I --- 04:029362 --:------ 12:126457 3150 002 0162
        # if not getattr(self.src, "_is_controller", True) and not getattr(
        #     self.dst, "_is_controller", True
        # ):
        #     assert self._pkt._idx == "00", "What!! (BA)"
        #     return {}

        # .I --- 04:029362 --:------ 12:126457 3150 002 0162
        # if not (
        #     getattr(self.src, "_is_controller", True)
        #     or getattr(self.dst, "_is_controller", True)
        # ):
        #     assert self._pkt._idx == "00", "What!! (BB)"
        #     return {}

        if self.src.type == self.dst.type and not getattr(
            self.src, "_is_controller", True
        ):  # DEX
            assert self._pkt._idx == "00", "What!! (BC)"
            return {}

        # TODO: also 000C (but is a complex idx)
        # TODO: also 3150 (when not domain, and will be array if so)
        if self.code in (Code._000A, Code._2309) and self.src.type == DEV_TYPE_MAP.UFC:
            assert isinstance(self._pkt._idx, str)  # mypy hint
            return {IDX_NAMES[Code._22C9]: self._pkt._idx}

        assert isinstance(self._pkt._idx, str)  # mypy check
        idx_name = SZ_DOMAIN_ID if self._pkt._idx[:1] == "F" else SZ_ZONE_IDX
        index_name = IDX_NAMES.get(self.code, idx_name)

        return {index_name: self._pkt._idx}

    # TODO: needs work...
    def _validate(self, raw_payload: str) -> dict | list[dict]:  # type: ignore[type-arg]
        """Validate the message, and parse the payload if so.

        Raise an exception (InvalidPacketError) if it is not valid.
        """

        try:  # parse the payload
            # TODO: only accept invalid packets to/from HGI when flag raised
            _check_msg_payload(self, self._pkt.payload)  # ? InvalidPayloadError

            if not self._has_payload and (
                self.verb == RQ and self.code not in RQ_IDX_COMPLEX
            ):
                # _LOGGER.error("%s", msg)
                return {}

            result = parse_payload(self)

            if isinstance(result, list):
                return result
            if isinstance(result, dict):
                return {**self._idx, **result}

            raise TypeError(f"Invalid payload type: {type(result)}")

        except exc.PacketInvalid as err:
            _LOGGER.warning("%s < %s", self._pkt, err)
            raise err

        except AssertionError as err:
            # beware: HGI80 can send 'odd' but parseable packets +/- get invalid reply
            _LOGGER.exception("%s < %s", self._pkt, f"{err.__class__.__name__}({err})")
            raise exc.PacketInvalid("Bad packet") from err

        except (AttributeError, LookupError, TypeError, ValueError) as err:  # TODO: dev
            _LOGGER.exception(
                "%s < Coding error: %s", self._pkt, f"{err.__class__.__name__}({err})"
            )
            raise exc.PacketInvalid from err

        except NotImplementedError as err:  # parser_unknown (unknown packet code)
            _LOGGER.warning("%s < Unknown packet code (cannot parse)", self._pkt)
            raise exc.PacketInvalid from err


class Message(MessageBase):
    """Extend the Message class, so is useful to a stateful Gateway.

    Adds _expired attr to the Message class.
    """

    CANT_EXPIRE = -1  # sentinel value for fraction_expired

    HAS_EXPIRED = 2.0  # fraction_expired >= HAS_EXPIRED
    # .HAS_DIED = 1.0  # fraction_expired >= 1.0 (is expected lifespan)
    IS_EXPIRING = 0.8  # fraction_expired >= 0.8 (and < HAS_EXPIRED)

    _gwy: Gateway
    _fraction_expired: float | None = None

    @classmethod
    def _from_cmd(cls, cmd: Command, dtm: dt | None = None) -> Message:
        """Create a Message from a Command."""
        return cls(Packet._from_cmd(cmd, dtm=dtm))

    @classmethod
    def _from_pkt(cls, pkt: Packet) -> Message:
        """Create a Message from a Packet."""
        return cls(pkt)

    @property
    def _expired(self) -> bool:
        """Return True if the message is dated (or False otherwise)."""
        # fraction_expired = (dt_now - self.dtm - _TD_SECONDS_003) / self._pkt._lifespan
        # TODO: keep none >7d, even 10E0, etc.

        def fraction_expired(lifespan: td) -> float:
            """Return the packet's age as fraction of its 'normal' life span."""
            return (self._gwy._dt_now() - self.dtm - _TD_SECS_003) / lifespan

        # 1. Look for easy win...
        if self._fraction_expired is not None:
            if self._fraction_expired == self.CANT_EXPIRE:
                return False
            if self._fraction_expired >= self.HAS_EXPIRED:
                return True

        # 2. Need to update the fraction_expired...
        if self.code == Code._1F09 and self.verb != RQ:  # sync_cycle is a special case
            # RQs won't have remaining_seconds, RP/Ws have only partial cycle times
            self._fraction_expired = fraction_expired(
                td(seconds=self.payload["remaining_seconds"]),
            )

        elif self._pkt._lifespan is False:  # Can't expire
            self._fraction_expired = self.CANT_EXPIRE

        elif self._pkt._lifespan is True:  # Can't expire
            raise NotImplementedError

        else:
            self._fraction_expired = fraction_expired(self._pkt._lifespan)

        return self._fraction_expired >= self.HAS_EXPIRED


@lru_cache(maxsize=256)
def re_compile_re_match(regex: str, string: str) -> bool:  # Optional[Match[Any]]
    # TODO: confirm this does speed things up
    # Python has its own caching of re.compile, _MAXCACHE = 512
    # https://github.com/python/cpython/blob/3.10/Lib/re.py
    return re.compile(regex).match(string)  # type: ignore[return-value]


def _check_msg_payload(msg: MessageBase, payload: str) -> None:
    """Validate the packet's payload against its verb/code pair.

    Raise an InvalidPayloadError if the payload is seen as invalid. Such payloads may
    actually be valid, in which case the rules (likely the regex) will need updating.
    """

    _ = repr(msg._pkt)  # HACK: ? raise InvalidPayloadError

    if msg.code not in CODES_SCHEMA:
        raise exc.PacketInvalid(f"Unknown code: {msg.code}")

    try:
        regex = CODES_SCHEMA[msg.code][msg.verb]
    except KeyError:
        raise exc.PacketInvalid(
            f"Unknown verb/code pair: {msg.verb}/{msg.code}"
        ) from None

    if not re_compile_re_match(regex, payload):
        raise exc.PacketPayloadInvalid(f"Payload doesn't match '{regex}': {payload}")
