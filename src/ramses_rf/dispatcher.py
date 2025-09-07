#!/usr/bin/env python3
"""RAMSES RF - Decode/process a message (payload into JSON)."""

# TODO:
# - fix dispatching - what devices (some are Addr) are sent packets, esp. 1FC9s

from __future__ import annotations

import contextlib
import logging
from datetime import timedelta as td
from typing import TYPE_CHECKING, Final

from ramses_tx import ALL_DEV_ADDR, CODES_BY_DEV_SLUG, Message
from ramses_tx.ramses import (
    CODES_OF_HEAT_DOMAIN,
    CODES_OF_HEAT_DOMAIN_ONLY,
    CODES_OF_HVAC_DOMAIN_ONLY,
)

from . import exceptions as exc
from .const import (
    DEV_TYPE_MAP,
    DONT_CREATE_ENTITIES,
    DONT_UPDATE_ENTITIES,
    SZ_DEVICES,
    SZ_OFFER,
    SZ_PHASE,
    DevType,
)
from .device import Device, Fakeable

from .const import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    I_,
    RP,
    RQ,
    W_,
    Code,
)

if TYPE_CHECKING:
    from . import Gateway

#
# NOTE: All debug flags should be False for deployment to end-users
_DBG_FORCE_LOG_MESSAGES: Final[bool] = False  # useful for dev/test
_DBG_INCREASE_LOG_LEVELS: Final[bool] = (
    False  # set True for developer-friendly log spam
)

_LOGGER = logging.getLogger(__name__)


__all__ = ["detect_array_fragment", "process_msg"]


MSG_FORMAT_18 = "|| {:18s} | {:18s} | {:2s} | {:16s} | {:^4s} || {}"

_TD_SECONDS_003 = td(seconds=3)


def _create_devices_from_addrs(gwy: Gateway, this: Message) -> None:
    """Discover and create any new devices using the packet addresses (not payload)."""

    # FIXME: changing Address to Devices is messy: ? Protocol for same method signatures
    # prefer Devices but can continue with Addresses if required...
    this.src = gwy.device_by_id.get(this.src.id, this.src)  # type: ignore[assignment]
    this.dst = gwy.device_by_id.get(this.dst.id, this.dst)  # type: ignore[assignment]

    # Devices need to know their controller, ?and their location ('parent' domain)
    # NB: only addrs processed here, packet metadata is processed elsewhere

    # Determinging bindings to a controller:
    #  - configury; As per any schema                                                   # codespell:ignore configury
    #  - discovery: If in 000C pkt, or pkt *to* device where src is a controller
    #  - eavesdrop: If pkt *from* device where dst is a controller

    # Determinging location in a schema (domain/DHW/zone):
    #  - configury; As per any schema                                                   # codespell:ignore configury
    #  - discovery: If in 000C pkt - unable for 10: & 00: (TRVs)
    #  - discovery: from packet fingerprint, excl. payloads (only for 10:)
    #  - eavesdrop: from packet fingerprint, incl. payloads

    if not isinstance(this.src, Device):  # type: ignore[unreachable]
        # may: LookupError, but don't suppress
        this.src = gwy.get_device(this.src.id)  # type: ignore[assignment]
        if this.dst.id == this.src.id:
            this.dst = this.src
            return

    if not gwy.config.enable_eavesdrop:
        return

    if not isinstance(this.dst, Device) and this.src != gwy.hgi:  # type: ignore[unreachable]
        with contextlib.suppress(LookupError):
            this.dst = gwy.get_device(this.dst.id)  # type: ignore[assignment]


def _check_msg_addrs(msg: Message) -> None:  # TODO
    """Validate the packet's address set.

    Raise InvalidAddrSetError if the meta data is invalid, otherwise simply return.
    """

    # TODO: needs work: doesn't take into account device's (non-HVAC) class

    if (
        msg.src.id != msg.dst.id
        and msg.src.type == msg.dst.type
        and msg.src.type in DEV_TYPE_MAP.HEAT_DEVICES  # could still be HVAC domain
    ):
        # .I --- 18:013393 18:000730 --:------ 0001 005 00FFFF0200     # invalid
        # .I --- 01:078710 --:------ 01:144246 1F09 003 FF04B5         # invalid
        # .I --- 29:151550 29:237552 --:------ 22F3 007 00023C03040000 # valid? HVAC
        if msg.code in CODES_OF_HEAT_DOMAIN_ONLY:
            raise exc.PacketAddrSetInvalid(
                f"Invalid addr pair: {msg.src!r}/{msg.dst!r}"
            )
        elif msg.code in CODES_OF_HEAT_DOMAIN:
            _LOGGER.warning(
                f"{msg!r} < Invalid addr pair: {msg.src!r}/{msg.dst!r}, is it HVAC?"
            )
        elif msg.code not in CODES_OF_HVAC_DOMAIN_ONLY:
            _LOGGER.info(
                f"{msg!r} < Invalid addr pair: {msg.src!r}/{msg.dst!r}, is it HVAC?"
            )


def _check_src_slug(msg: Message, *, slug: str | None = None) -> None:
    """Validate the packet's source device class against its verb/code pair."""

    if slug is None:  # slug = best_dev_role(msg.src, msg=msg)._SLUG
        slug = getattr(msg.src, "_SLUG", None)
    if slug in (None, DevType.HGI, DevType.DEV, DevType.HEA, DevType.HVC):
        return  # TODO: use DEV_TYPE_MAP.PROMOTABLE_SLUGS

    if slug not in CODES_BY_DEV_SLUG:
        raise exc.PacketInvalid(f"{msg!r} < Unknown src slug ({slug}), is it HVAC?")

    #

    if msg.code not in CODES_BY_DEV_SLUG[slug]:
        raise exc.PacketInvalid(f"{msg!r} < Unexpected code for src ({slug}) to Tx")

    #
    #

    if msg.verb not in CODES_BY_DEV_SLUG[slug][msg.code]:
        raise exc.PacketInvalid(
            f"{msg!r} < Unexpected verb/code for src ({slug}) to Tx"
        )


def _check_dst_slug(msg: Message, *, slug: str | None = None) -> None:
    """Validate the packet's destination device class against its verb/code pair."""

    if slug is None:
        slug = getattr(msg.dst, "_SLUG", None)
    if slug in (None, DevType.HGI, DevType.DEV, DevType.HEA, DevType.HVC):
        return  # TODO: use DEV_TYPE_MAP.PROMOTABLE_SLUGS

    if slug not in CODES_BY_DEV_SLUG:
        raise exc.PacketInvalid(f"{msg!r} < Unknown dst slug ({slug}), is it HVAC?")

    if f"{slug}/{msg.verb}/{msg.code}" in (f"CTL/{RQ}/{Code._3EF1}",):
        return  # HACK: an exception-to-the-rule that need sorting

    if msg.code not in CODES_BY_DEV_SLUG[slug]:
        raise exc.PacketInvalid(f"{msg!r} < Unexpected code for dst ({slug}) to Rx")

    if f"{msg.verb}/{msg.code}" in (f"{W_}/{Code._0001}",):
        return  # HACK: an exception-to-the-rule that need sorting
    if f"{slug}/{msg.verb}/{msg.code}" in (f"{DevType.BDR}/{RQ}/{Code._3EF0}",):
        return  # HACK: an exception-to-the-rule that need sorting

    if {RQ: RP, RP: RQ, W_: I_}[msg.verb] not in CODES_BY_DEV_SLUG[slug][msg.code]:
        raise exc.PacketInvalid(
            f"{msg!r} < Unexpected verb/code for dst ({slug}) to Rx"
        )


def process_msg(gwy: Gateway, msg: Message) -> None:
    """Decoding the packet payload and route it appropriately."""

    # All methods require msg with a valid payload, except _create_devices_from_addrs(),
    # which requires a valid payload only for 000C.

    def logger_xxxx(msg: Message) -> None:
        if _DBG_FORCE_LOG_MESSAGES:
            _LOGGER.warning(msg)
        elif msg.src != gwy.hgi or (msg.code != Code._PUZZ and msg.verb != RQ):
            _LOGGER.info(msg)
        elif msg.src != gwy.hgi or msg.verb != RQ:
            _LOGGER.info(msg)
        elif _LOGGER.getEffectiveLevel() == logging.DEBUG:
            _LOGGER.info(msg)

    try:  # validate / dispatch the packet
        _check_msg_addrs(msg)  # ?InvalidAddrSetError  TODO: ?useful at all

        # TODO: any use in creating a device only if the payload is valid?
        if gwy.config.reduce_processing >= DONT_CREATE_ENTITIES:
            logger_xxxx(msg)  # return ensures try's else: clause won't be invoked
            return

        try:
            _create_devices_from_addrs(gwy, msg)
        except LookupError as err:
            (_LOGGER.error if _DBG_INCREASE_LOG_LEVELS else _LOGGER.warning)(
                "%s < %s(%s)", msg._pkt, err.__class__.__name__, err
            )
            return

        _check_src_slug(msg)  # ? raise exc.PacketInvalid
        if (
            msg.src._SLUG != DevType.HGI  # avoid: msg.src.id != gwy.hgi.id
            and msg.verb != I_
            and msg.dst != msg.src
        ):
            # HGI80 can do what it likes
            # receiving an I isn't currently in the schema & so can't yet be tested
            _check_dst_slug(msg)  # ? raise exc.PacketInvalid

        if gwy.config.reduce_processing >= DONT_UPDATE_ENTITIES:
            logger_xxxx(msg)  # return ensures try's else: clause won't be invoked
            return

        # NOTE: here, msgs are routed only to devices: routing to other entities (i.e.
        # systems, zones, circuits) is done by those devices (e.g. UFC to UfhCircuit)

        if isinstance(msg.src, Device):  # type: ignore[unreachable]
            gwy._loop.call_soon(msg.src._handle_msg, msg)  # type: ignore[unreachable]

        # TODO: only be for fully-faked (not Fakable) dst (it picks up via RF if not)

        if msg.code == Code._1FC9 and msg.payload[SZ_PHASE] == SZ_OFFER:
            devices = [d for d in gwy.devices if d != msg.src and d._is_binding]

        elif msg.dst == ALL_DEV_ADDR:  # some offers use dst=63:, so after 1FC9 offer
            devices = [d for d in gwy.devices if d != msg.src and d.is_faked]

        elif msg.dst is not msg.src and isinstance(msg.dst, Fakeable):  # type: ignore[unreachable]
            # to eavesdrop pkts from other devices, but relevant to this device
            # dont: msg.dst._handle_msg(msg)
            devices = [msg.dst]  # type: ignore[unreachable]

        # TODO: this may not be required...
        elif hasattr(msg.src, SZ_DEVICES):  # FIXME: use isinstance()
            # elif isinstance(msg.src, Controller):
            # .I --- 22:060293 --:------ 22:060293 0008 002 000C
            # .I --- 01:054173 --:------ 01:054173 0008 002 03AA
            # needed for (e.g.) faked relays: each device decides if the pkt is useful
            devices = msg.src.devices

        else:
            devices = []

        for d in devices:  # FIXME: some may be Addresses?
            gwy._loop.call_soon(d._handle_msg, msg)

    except (AssertionError, exc.RamsesException, NotImplementedError) as err:
        (_LOGGER.error if _DBG_INCREASE_LOG_LEVELS else _LOGGER.warning)(
            "%s < %s(%s)", msg._pkt, err.__class__.__name__, err
        )

    except (AttributeError, LookupError, TypeError, ValueError) as err:
        _LOGGER.exception("%s < %s(%s)", msg._pkt, err.__class__.__name__, err)

    else:
        logger_xxxx(msg)
        if gwy._zzz:
            gwy._zzz.add(msg)


# TODO: this needs cleaning up (e.g. handle intervening packet)
def detect_array_fragment(this: Message, prev: Message) -> bool:  # _PayloadT
    """Return a merged array if this pkt is the latter half of an array."""
    # This will work, even if the 2nd pkt._is_array == False as 1st == True
    # .I --- 01:158182 --:------ 01:158182 000A 048 001201F409C4011101F409C40...
    # .I --- 01:158182 --:------ 01:158182 000A 006 081001F409C4

    return bool(
        prev._has_array
        and this.code in (Code._000A, Code._22C9)  # TODO: not a complete list
        and this.code == prev.code
        and this.verb == prev.verb == I_
        and this.src == prev.src
        and this.dtm < prev.dtm + _TD_SECONDS_003
    )
