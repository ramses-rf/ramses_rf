#!/usr/bin/env python3
"""RAMSES RF - Message processing pipeline validation stages."""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Final

from ramses_tx.address import HGI_DEV_ADDR

from . import exceptions as exc
from .const import (
    DEV_TYPE_MAP,
    DONT_CREATE_ENTITIES,
    DONT_UPDATE_ENTITIES,
    I_,
    RP,
    RQ,
    W_,
    Code,
    DevType,
)
from .eavesdropper import EavesdropEngine
from .messages import Message
from .protocol.ramses import (
    CODES_BY_DEV_SLUG,
    CODES_OF_HEAT_DOMAIN,
    CODES_OF_HEAT_DOMAIN_ONLY,
    CODES_OF_HVAC_DOMAIN_ONLY,
)

if TYPE_CHECKING:
    from .gateway import Gateway

_DBG_INCREASE_LOG_LEVELS: Final[bool] = False

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "instantiate_devices",
    "validate_addresses",
    "validate_slugs",
]


def validate_addresses(gateway: Gateway, msg: Message) -> bool:
    """Validate the packet's address set for basic structural rules.

    This is Stage 1 of the processing pipeline. It evaluates the raw addressing
    metadata. If the addresses violate domain-specific rules, an exception is
    raised and caught by the pipeline executor.

    :param gateway: The gateway handling the message.
    :type gateway: Gateway
    :param msg: The message containing source/destination addresses.
    :type msg: Message
    :raises exc.PacketAddrSetInvalid: If the address pair is invalid.
    :return: True if the pipeline should proceed, False if processing
             is configured to halt before entity creation.
    :rtype: bool
    """
    # TODO: needs work: doesn't take into account device's (non-HVAC) class
    if (
        msg.src.id != msg.dst.id
        and msg.src.type == msg.dst.type
        and msg.src.type
        in DEV_TYPE_MAP.HEAT_DEVICES  # could still be HVAC domain
    ):
        # .I --- 18:013393 18:000730 --:------ 0001 005 00FFFF0200     # invalid
        # .I --- 01:078710 --:------ 01:144246 1F09 003 FF04B5         # invalid
        # .I --- 29:151550 29:237552 --:------ 22F3 007 00023C03040000 # valid? HVAC

        # 🚨 CQRS Bypass: Permit UFCs (02:) to communicate directly (e.g. Autotemp)
        if msg.src.type == "02":
            pass
        elif msg.code in CODES_OF_HEAT_DOMAIN_ONLY:
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

    # TODO: any use in creating a device only if the payload is valid?
    return gateway.config.reduce_processing < DONT_CREATE_ENTITIES


def instantiate_devices(gateway: Gateway, msg: Message) -> bool:
    """Ensure the source and destination devices exist in the registry.

    This is Stage 2 of the processing pipeline. It attempts to discover or
    map the addresses to actual Device objects. If a required device cannot be
    found, it logs a warning and halts the pipeline.

    :param gateway: The gateway containing the device registry.
    :type gateway: Gateway
    :param msg: The message to inject discovered devices into.
    :type msg: Message
    :return: True if devices were mapped/created successfully, False otherwise.
    :rtype: bool
    """
    try:
        # FIXME: changing Address to Devices is messy: ? Protocol for same
        # method signatures. prefer Devices but can continue with Addresses...
        src_dev = gateway.device_registry.device_by_id.get(msg.src.id)

        # Devices need to know their controller, ?and their location ('parent' domain)
        # NB: only addrs processed here, packet metadata is processed elsewhere

        # Determining bindings to a controller:
        #  - configury; As per any schema      # codespell:ignore configury
        #  - discovery: If in 000C packet, or packet *to* device where src is a controller
        #  - eavesdrop: If packet *from* device where dst is a controller

        # Determining location in a schema (domain/DHW/zone):
        #  - configury; As per any schema      # codespell:ignore configury
        #  - discovery: If in 000C packet - unable for 10: & 00: (TRVs)
        #  - discovery: from packet fingerprint, excl. payloads (only for 10:)
        #  - eavesdrop: from packet fingerprint, incl. payloads

        hgi_id = gateway.hgi.id if gateway.hgi else None

        if src_dev is None:
            # Foreign HGIs (18: devices that are not the active gateway and
            # not the generic HGI_DEV_ADDR 18:000730) communicate with our
            # controller — the controller's RPs are addressed to them, and
            # they send RQs to the controller.  The active gateway eavesdrops
            # on both directions (issue 822).
            #
            # The protocol-level filter (_is_wanted_addrs in ramses_tx) lets
            # foreign HGIs through, but when enforce_known_list is True the
            # device-registry filter (check_filter_lists in dev_filter.py)
            # rejects them because they are not in the known_list.  This
            # get_device call is NOT suppressed (the src device is needed for
            # payload routing), so a DeviceNotFoundError here drops the entire
            # packet and adds the foreign HGI to the _unwanted list — causing
            # repeating FILTER EXCEPTION warnings on every subsequent packet
            # from the foreign HGI (issue 822, comment 5017168119).
            #
            # Skip device creation for foreign HGI sources only when
            # enforce_known_list is active (the filter would reject them).
            # When enforce_known_list is False, the foreign HGI is created
            # normally (as an HgiGateway) — this preserves existing behaviour
            # for systems that don't enforce the known_list.
            if (
                gateway.config.engine.enforce_known_list
                and getattr(msg.src, "type", None) in ("18", DevType.HGI)
                and msg.src.id != HGI_DEV_ADDR.id
                and msg.src.id != hgi_id
                and msg.src.id not in gateway.config.known_list
            ):
                # Foreign HGI as source — skip device creation, continue
                # processing (the dst device will be created below)
                pass
            else:
                # may: DeviceNotFoundError, but don't suppress
                src_dev = gateway.device_registry.get_device(msg.src.id)
                if (
                    getattr(msg.src, "type", None) in ("01", DevType.CTL)
                    and hasattr(src_dev, "tcs")
                    and src_dev.tcs is None
                    and hasattr(src_dev, "_make_tcs_controller")
                ):
                    src_dev._make_tcs_controller(msg=msg)
        if getattr(gateway.config, "enable_eavesdrop", False):
            engine = getattr(gateway, "_eavesdrop_engine", None)
            if engine is None:
                engine = EavesdropEngine(gateway)
                with contextlib.suppress(AttributeError):
                    gateway._eavesdrop_engine = engine
            engine.eavesdrop_referenced_devices(msg)

        if msg.dst.id == msg.src.id:
            return True

    except exc.DeviceNotFoundError as err:
        (_LOGGER.error if _DBG_INCREASE_LOG_LEVELS else _LOGGER.warning)(
            "%s < %s(%s)", msg, err.__class__.__name__, err
        )
        return False

    return True


def validate_slugs(gateway: Gateway, msg: Message) -> bool:
    """Validate the device classes against the transmitted code/verb.

    This is Stage 3 of the processing pipeline. It verifies whether the
    source is permitted to Tx this payload, and if the destination is
    permitted to Rx it, based on protocol schemas.

    :param gateway: The gateway handling the message.
    :type gateway: Gateway
    :param msg: The message containing the verb and code to validate.
    :type msg: Message
    :raises exc.PacketInvalid: If either slug cannot process the verb/code.
    :return: True if slugs are valid, False if processing limits dictate halting.
    :rtype: bool
    """
    # 1. Check Source Slug
    src_dev = gateway.device_registry.device_by_id.get(msg.src.id)
    slug = getattr(src_dev, "_SLUG", None)

    if slug not in (None, DevType.HGI, DevType.DEV, DevType.HEA, DevType.HVC):
        # TODO: use DEV_TYPE_MAP.PROMOTABLE_SLUGS
        if slug not in CODES_BY_DEV_SLUG:
            raise exc.PacketInvalid(
                f"{msg!r} < Unknown src slug ({slug}), is it HVAC?"
            )

        if msg.code not in CODES_BY_DEV_SLUG[slug]:
            raise exc.PacketInvalid(
                f"{msg!r} < Unexpected code for src ({slug}) to Tx"
            )

        if msg.verb not in CODES_BY_DEV_SLUG[slug][msg.code]:
            raise exc.PacketInvalid(
                f"{msg!r} < Unexpected verb/code for src ({slug}) to Tx"
            )

    # 2. Check Destination Slug
    if (
        slug != DevType.HGI  # avoid: msg.src.id != gwy.hgi.id
        and msg.verb != I_
        and msg.dst.id != msg.src.id
    ):
        # HGI80 can do what it likes
        # receiving an I_ isn't currently in the schema & so can't yet be tested
        dst_dev = gateway.device_registry.device_by_id.get(msg.dst.id)
        dst_slug = getattr(dst_dev, "_SLUG", None)

        if dst_slug not in (
            None,
            DevType.HGI,
            DevType.DEV,
            DevType.HEA,
            DevType.HVC,
        ):
            if dst_slug not in CODES_BY_DEV_SLUG:
                raise exc.PacketInvalid(
                    f"{msg!r} < Unknown dst slug ({dst_slug}), is it HVAC?"
                )

            # Match exception-to-the-rule opcode patterns using structural pattern matching
            match (dst_slug, msg.verb, msg.code):
                case (DevType.CTL, verb, Code._3EF1) if verb == RQ:
                    pass
                case (_, verb, Code._0001) if verb == W_:
                    pass
                case (DevType.BDR, verb, Code._3EF0) if verb == RQ:
                    pass
                case _:
                    if msg.code not in CODES_BY_DEV_SLUG[dst_slug]:
                        raise exc.PacketInvalid(
                            f"{msg!r} < Unexpected code for dst ({dst_slug}) to Rx"
                        )

                    expected_verb = {RQ: RP, RP: RQ, W_: I_}[msg.verb]
                    if (
                        expected_verb
                        not in CODES_BY_DEV_SLUG[dst_slug][msg.code]
                    ):
                        raise exc.PacketInvalid(
                            f"{msg!r} < Unexpected verb/code for dst ({dst_slug}) to Rx"
                        )

    return gateway.config.reduce_processing < DONT_UPDATE_ENTITIES
