#!/usr/bin/env python3
"""RAMSES RF - Decode/process a message (payload into JSON)."""

# TODO:
# - fix dispatching - what devices (some are Addr) are sent packets, esp. 1FC9s

from __future__ import annotations

import contextlib
import dataclasses
import logging
import uuid
from datetime import timedelta as td
from typing import TYPE_CHECKING, Any, Final

from ramses_tx import ALL_DEV_ADDR
from ramses_tx.address import HGI_DEV_ADDR

from . import exceptions as exc
from .const import (
    DEV_TYPE_MAP,
    DONT_CREATE_ENTITIES,
    DONT_UPDATE_ENTITIES,
    I_,
    RP,
    RQ,
    SZ_ACTIVE,
    SZ_AIR_QUALITY,
    SZ_AIR_QUALITY_BASIS,
    SZ_BYPASS_MODE,
    SZ_BYPASS_POSITION,
    SZ_BYPASS_STATE,
    SZ_CO2_LEVEL,
    SZ_DATETIME,
    SZ_DEVICES,
    SZ_DIFFERENTIAL,
    SZ_EXHAUST_FAN_SPEED,
    SZ_EXHAUST_FLOW,
    SZ_EXHAUST_TEMP,
    SZ_FAN_INFO,
    SZ_FAN_MODE,
    SZ_FAN_RATE,
    SZ_FILTER_DIRTY,
    SZ_FROST_CYCLE,
    SZ_HAS_FAULT,
    SZ_HEAT_DEMAND,
    SZ_INDOOR_HUMIDITY,
    SZ_INDOOR_TEMP,
    SZ_LANGUAGE,
    SZ_MINUTES,
    SZ_MODE,
    SZ_OFFER,
    SZ_OUTDOOR_HUMIDITY,
    SZ_OUTDOOR_TEMP,
    SZ_OVERRUN,
    SZ_PHASE,
    SZ_POST_HEAT,
    SZ_PRE_HEAT,
    SZ_PRESENCE_DETECTED,
    SZ_RELAY_DEMAND,
    SZ_REMAINING_DAYS,
    SZ_REMAINING_MINS,
    SZ_REMAINING_PERCENT,
    SZ_REQ_REASON,
    SZ_REQ_SPEED,
    SZ_SETPOINT,
    SZ_SPEED_CAPABILITIES,
    SZ_SUPPLY_FAN_SPEED,
    SZ_SUPPLY_FLOW,
    SZ_SUPPLY_TEMP,
    SZ_SYSTEM_MODE,
    SZ_TEMPERATURE,
    SZ_UNTIL,
    W_,
    Code,
    DevType,
)
from .devices.hvac_ventilators import HvacVentilator
from .messages import Message
from .models import StateUpdatedEvent, SystemState
from .protocol.ramses import (
    CODES_BY_DEV_SLUG,
    CODES_OF_HEAT_DOMAIN,
    CODES_OF_HEAT_DOMAIN_ONLY,
    CODES_OF_HVAC_DOMAIN_ONLY,
)
from .systems.zones import DhwZone

if TYPE_CHECKING:
    from .gateway import Gateway

#
# NOTE: All debug flags should be False for deployment to end-users
_DBG_FORCE_LOG_MESSAGES: Final[bool] = False  # useful for dev/test
_DBG_INCREASE_LOG_LEVELS: Final[bool] = (
    False  # set True for developer-friendly log spam
)

_LOGGER = logging.getLogger(__name__)


__all__ = [
    "detect_array_fragment",
    "instantiate_devices",
    "process_msg",
    "route_payload",
    "validate_addresses",
    "validate_slugs",
]


MSG_FORMAT_18 = "|| {:18s} | {:18s} | {:2s} | {:16s} | {:^4s} || {}"

_TD_SECONDS_003 = td(seconds=3)


def _log_message(gwy: Gateway, msg: Message) -> None:
    """Log msg according to src, code, log.debug setting.

    :param gwy: The gateway handling the message.
    :type gwy: Gateway
    :param msg: the Message being processed.
    :type msg: Message
    """
    if _DBG_FORCE_LOG_MESSAGES:
        _LOGGER.warning(msg)
    elif msg.src != gwy.hgi or (msg.code != Code._PUZZ and msg.verb != RQ):
        _LOGGER.info(msg)
    elif msg.src != gwy.hgi or msg.verb != RQ:
        _LOGGER.info(msg)
    elif _LOGGER.getEffectiveLevel() == logging.DEBUG:
        _LOGGER.info(msg)


def validate_addresses(gwy: Gateway, msg: Message) -> bool:
    """Validate the packet's address set for basic structural rules.

    This is Stage 1 of the processing pipeline. It evaluates the raw addressing
    metadata. If the addresses violate domain-specific rules, an exception is
    raised and caught by the pipeline executor.

    :param gwy: The gateway handling the message.
    :type gwy: Gateway
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
        and msg.src.type in DEV_TYPE_MAP.HEAT_DEVICES  # could still be HVAC domain
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
    return gwy.config.reduce_processing < DONT_CREATE_ENTITIES


def instantiate_devices(gwy: Gateway, msg: Message) -> bool:
    """Ensure the source and destination devices exist in the registry.

    This is Stage 2 of the processing pipeline. It attempts to discover or
    map the addresses to actual Device objects. If a required device cannot be
    found, it logs a warning and halts the pipeline.

    :param gwy: The gateway containing the device registry.
    :type gwy: Gateway
    :param msg: The message to inject discovered devices into.
    :type msg: Message
    :return: True if devices were mapped/created successfully, False otherwise.
    :rtype: bool
    """
    try:
        # FIXME: changing Address to Devices is messy: ? Protocol for same
        # method signatures. prefer Devices but can continue with Addresses...
        src_dev = gwy.device_registry.device_by_id.get(msg.src.id)
        dst_dev = gwy.device_registry.device_by_id.get(msg.dst.id)

        # Devices need to know their controller, ?and their location ('parent' domain)
        # NB: only addrs processed here, packet metadata is processed elsewhere

        # Determining bindings to a controller:
        #  - configury; As per any schema      # codespell:ignore configury
        #  - discovery: If in 000C pkt, or pkt *to* device where src is a controller
        #  - eavesdrop: If pkt *from* device where dst is a controller

        # Determining location in a schema (domain/DHW/zone):
        #  - configury; As per any schema      # codespell:ignore configury
        #  - discovery: If in 000C pkt - unable for 10: & 00: (TRVs)
        #  - discovery: from packet fingerprint, excl. payloads (only for 10:)
        #  - eavesdrop: from packet fingerprint, incl. payloads

        hgi_id = gwy.hgi.id if gwy.hgi else None

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
                gwy.config.engine.enforce_known_list
                and msg.src.id[:2] == "18"
                and msg.src.id != HGI_DEV_ADDR.id
                and msg.src.id != hgi_id
            ):
                # Foreign HGI as source — skip device creation, continue
                # processing (the dst device will be created below)
                pass
            else:
                # may: DeviceNotFoundError, but don't suppress
                src_dev = gwy.device_registry.get_device(msg.src.id)
                if msg.dst.id == msg.src.id:
                    return True

        if not gwy.config.enable_eavesdrop:
            return True

        if dst_dev is None and msg.src.id != hgi_id:
            with contextlib.suppress(exc.DeviceNotFoundError):
                gwy.device_registry.get_device(msg.dst.id)

        # Eavesdrop: Instantiate implicitly referenced devices (e.g., parent
        # controller in addr2)
        if (addrs := getattr(msg._pkt, "_addrs", None)) is not None:
            for addr in addrs:
                if addr.id not in (msg.src.id, getattr(msg.dst, "id", None)):
                    with contextlib.suppress(exc.DeviceNotFoundError):
                        gwy.device_registry.get_device(addr.id)

    except exc.DeviceNotFoundError as err:
        (_LOGGER.error if _DBG_INCREASE_LOG_LEVELS else _LOGGER.warning)(
            "%s < %s(%s)", msg._pkt, err.__class__.__name__, err
        )
        return False

    return True


def validate_slugs(gwy: Gateway, msg: Message) -> bool:
    """Validate the device classes against the transmitted code/verb.

    This is Stage 3 of the processing pipeline. It verifies whether the
    source is permitted to Tx this payload, and if the destination is
    permitted to Rx it, based on protocol schemas.

    :param gwy: The gateway handling the message.
    :type gwy: Gateway
    :param msg: The message containing the verb and code to validate.
    :type msg: Message
    :raises exc.PacketInvalid: If either slug cannot process the verb/code.
    :return: True if slugs are valid, False if processing limits dictate halting.
    :rtype: bool
    """
    # 1. Check Source Slug
    src_dev = gwy.device_registry.device_by_id.get(msg.src.id)
    slug = getattr(src_dev, "_SLUG", None)

    if slug not in (None, DevType.HGI, DevType.DEV, DevType.HEA, DevType.HVC):
        # TODO: use DEV_TYPE_MAP.PROMOTABLE_SLUGS
        if slug not in CODES_BY_DEV_SLUG:
            raise exc.PacketInvalid(f"{msg!r} < Unknown src slug ({slug}), is it HVAC?")

        if msg.code not in CODES_BY_DEV_SLUG[slug]:
            raise exc.PacketInvalid(f"{msg!r} < Unexpected code for src ({slug}) to Tx")

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
        dst_dev = gwy.device_registry.device_by_id.get(msg.dst.id)
        dst_slug = getattr(dst_dev, "_SLUG", None)

        if dst_slug not in (None, DevType.HGI, DevType.DEV, DevType.HEA, DevType.HVC):
            if dst_slug not in CODES_BY_DEV_SLUG:
                raise exc.PacketInvalid(
                    f"{msg!r} < Unknown dst slug ({dst_slug}), is it HVAC?"
                )

            if f"{dst_slug}/{msg.verb}/{msg.code}" not in (f"CTL/{RQ}/{Code._3EF1}",):
                # HACK: an exception-to-the-rule that need sorting
                if msg.code not in CODES_BY_DEV_SLUG[dst_slug]:
                    raise exc.PacketInvalid(
                        f"{msg!r} < Unexpected code for dst ({dst_slug}) to Rx"
                    )

                if f"{msg.verb}/{msg.code}" not in (f"{W_}/{Code._0001}",):
                    # HACK: an exception-to-the-rule that need sorting
                    if f"{dst_slug}/{msg.verb}/{msg.code}" not in (
                        f"{DevType.BDR}/{RQ}/{Code._3EF0}",
                    ):
                        # HACK: an exception-to-the-rule that need sorting
                        if {RQ: RP, RP: RQ, W_: I_}[msg.verb] not in CODES_BY_DEV_SLUG[
                            dst_slug
                        ][msg.code]:
                            raise exc.PacketInvalid(
                                f"{msg!r} < Unexpected verb/code for dst "
                                f"({dst_slug}) to Rx"
                            )

    return gwy.config.reduce_processing < DONT_UPDATE_ENTITIES


# DHW opcodes that carry no zone_idx/domain_id and need special routing.
_DHW_OPCODES: Final[frozenset[Code]] = frozenset({Code._1260, Code._10A0, Code._1F41})


def _get_dhw_zone_from_msg(msg: Message, src_dev: Any) -> DhwZone | None:
    """Resolve the DhwZone that should ingest a DHW opcode (1260/10A0/1F41).

    These payloads carry no ``zone_idx``/``domain_id``, so the standard
    routing in ``_resolve_logical_targets`` (and the equivalent block in
    ``StateProjector.process_message_state``) misses the DhwZone.

    ``1260`` is sent by the DhwSensor (or relayed by the Controller as an
    RP); ``10A0``/``1F41`` are sent by the Controller.  The
    appliance_control (OTB) also emits ``10A0``/``1260`` with different
    semantics (CH setpoint / null temp) and is excluded to avoid
    clobbering the DHW read-models.

    See: https://github.com/ramses-rf/ramses_cc/issues/843

    :param msg: The inbound message.
    :type msg: Message
    :param src_dev: The source device (DhwSensor or Controller).
    :return: The DhwZone to route to, or ``None`` if the message is not
        a DHW opcode or the source is not a DHW sender.
    :rtype: DhwZone | None
    """
    if msg.code not in _DHW_OPCODES or src_dev is None:
        return None

    src_slug = getattr(src_dev, "_SLUG", "")
    if msg.code == Code._1260:
        is_dhw_src = src_slug in ("DHW", "CTL")
    else:  # 10A0 / 1F41 are owned by the Controller
        is_dhw_src = src_slug == "CTL"

    if not is_dhw_src:
        return None

    tcs = getattr(src_dev, "tcs", None)
    if tcs is None:
        return None

    return getattr(tcs, "dhw", None)


def _resolve_logical_targets(
    gwy: Gateway, msg: Message, p: dict[str, Any]
) -> list[Any]:
    """Resolve all logical software twins that should ingest this payload."""
    targets = []
    src_dev = gwy.device_registry.device_by_id.get(msg.src.id)
    dst_dev = gwy.device_registry.device_by_id.get(msg.dst.id)
    tcs = getattr(src_dev, "tcs", None) if src_dev else None

    # 1. Fault logs strictly target the TCS (if it exists) or the source device
    if msg.code == "0418":
        if tcs:
            targets.append(getattr(tcs, "faultlog", src_dev))
        elif src_dev:
            targets.append(src_dev)
        return targets

    # 2. Hardware twin (Sender) always gets the update UNLESS it's a Controller/UFC
    # actively broadcasting an array of children's states (e.g., a 30C9 sync).
    src_type = getattr(src_dev, "type", None)
    has_arr = getattr(msg, "_has_array", False)
    if src_type not in ("01", "02") or not has_arr:
        if src_dev:
            targets.append(src_dev)

    # 3. Hardware twin (Destination) gets the update.
    # Legacy routes packets to the destination device's cache. To maintain
    # strict parity, we mirror this.
    # HVAC packets (e.g. 22F1 fan_mode from REM→FAN) target the destination
    # device's hvac_state directly, so we also accept devices that have
    # hvac_state even if they lack apply_state_update.
    if msg.dst.id != msg.src.id and getattr(msg.dst, "id", "") != "63:262142":
        if (
            dst_dev
            and (
                getattr(dst_dev, "apply_state_update", None) is not None
                or getattr(dst_dev, "hvac_state", None) is not None
            )
            and dst_dev not in targets
        ):
            targets.append(dst_dev)

    # 4. Virtual twins (Zones) get updates if explicitly addressed by idx.
    if "zone_idx" in p and tcs:
        if zone := tcs.zone_by_idx.get(p["zone_idx"]):
            if zone not in targets:
                targets.append(zone)

    # 5. Domain twins (TCS, DHW) get updates.
    if "domain_id" in p and tcs:
        domain_id = p["domain_id"]
        if domain_id == "FC" and tcs not in targets:
            targets.append(tcs)
        elif domain_id in ("FA", "F9") and getattr(tcs, "dhw", None) is not None:
            if tcs.dhw not in targets:
                targets.append(tcs.dhw)

    # 6. System-level opcodes (2E04/0100/313F) target the TCS directly.
    #    These packets have no domain_id/zone_idx, so steps 4/5 miss them.
    if msg.code in (Code._2E04, Code._0100, Code._313F) and tcs and tcs not in targets:
        targets.append(tcs)

    # 7. DHW opcodes (1260/10A0/1F41) carry no domain_id/zone_idx, so steps
    #    4/5 miss the DhwZone.  Route them via the shared helper.
    #    See: https://github.com/ramses-rf/ramses_cc/issues/843
    dhw = _get_dhw_zone_from_msg(msg, src_dev)
    if dhw is not None and dhw not in targets:
        targets.append(dhw)

    return targets


def _update_temperature_state(target: Any, p: dict[str, Any], msg: Message) -> None:
    """Translate temperature data into a frozen StateUpdatedEvent."""
    temp_state = getattr(target, "temp_state", None)
    if temp_state is None:
        return

    updates: dict[str, Any] = {}

    if SZ_TEMPERATURE in p:
        # Legacy Parity: Physical sensors only track their own local sensor readings.
        # We must ignore Zone temperature syncs sent TO them by the Controller.
        # Keep same as src/ramses_rf/pipeline/ingestion.py#_update_temperature_state
        target_id = getattr(target, "id", str(target))
        src_id = getattr(msg.src, "id", str(msg.src))

        if getattr(target, "_SLUG", "") in ("TRV", "THM") and src_id != target_id:
            pass
        else:
            updates[SZ_TEMPERATURE] = p[SZ_TEMPERATURE]

    if "setpoint" in p:
        updates[SZ_SETPOINT] = p[SZ_SETPOINT]

    if not updates:
        return

    new_state = dataclasses.replace(target.temp_state, **updates)
    event = StateUpdatedEvent(
        entity_id=getattr(target, "id", "unknown"),
        state=new_state,
        correlation_id=getattr(msg, "correlation_id", uuid.uuid4()),
        causation_id=getattr(msg, "message_id", uuid.uuid4()),
    )
    target.apply_state_update(event)


def _update_demand_state(target: Any, p: dict[str, Any], msg: Message) -> None:
    """Translate demand data into a frozen StateUpdatedEvent."""
    demand_state = getattr(target, "demand_state", None)
    if demand_state is None:
        return

    updates: dict[str, Any] = {}
    if SZ_HEAT_DEMAND in p:
        updates[SZ_HEAT_DEMAND] = p[SZ_HEAT_DEMAND]
    if SZ_RELAY_DEMAND in p:
        updates[SZ_RELAY_DEMAND] = p[SZ_RELAY_DEMAND]
        updates["relay_active"] = float(p[SZ_RELAY_DEMAND]) > 0.0
    if msg.code == Code._0009 and "failsafe_enabled" in p:
        updates["relay_failsafe"] = p["failsafe_enabled"]

    if not updates:
        return

    new_state = dataclasses.replace(target.demand_state, **updates)
    event = StateUpdatedEvent(
        entity_id=getattr(target, "id", "unknown"),
        state=new_state,
        correlation_id=getattr(msg, "correlation_id", uuid.uuid4()),
        causation_id=getattr(msg, "message_id", uuid.uuid4()),
    )
    target.apply_state_update(event)


def _update_faultlog_state(target: Any, p: dict[str, Any], msg: Message) -> None:
    """Translate 0418 fault log data into a frozen StateUpdatedEvent.

    This handles the immutable tuple appending tracking required by the
    CQRS FaultLogState read-model container.

    :param target: The target entity software twin to update.
    :type target: Any
    :param p: The parsed message payload dictionary.
    :type p: dict[str, Any]
    :param msg: The immutable Message L7 envelope.
    :type msg: Message
    :return: None
    :rtype: None
    """
    if msg.code != "0418" or getattr(target, "state", None) is None:
        return
    if type(target.state).__name__ != "FaultLogState":
        return

    # Guard: Ensure the entry index exists in the parsed payload
    if "log_idx" not in p:
        return

    from ramses_rf.systems.faultlog import FaultLogEntry

    with contextlib.suppress(Exception):
        entry = FaultLogEntry.from_msg(msg)

        # Append to the immutable tuple, safely removing stale matching timestamps
        current_entries = getattr(target.state, "entries", ())
        filtered = [e for e in current_entries if e.timestamp != entry.timestamp]
        new_entries = tuple(filtered) + (entry,)

        latest = getattr(target.state, "latest_fault", None)
        if getattr(entry.fault_state, "value", str(entry.fault_state)) == "fault":
            latest = entry

        new_state = dataclasses.replace(
            target.state, entries=new_entries, latest_fault=latest
        )

        event = StateUpdatedEvent(
            entity_id=getattr(target, "id", "unknown"),
            state=new_state,
            correlation_id=getattr(msg, "correlation_id", uuid.uuid4()),
            causation_id=getattr(msg, "message_id", uuid.uuid4()),
        )
        target.apply_state_update(event)


def _update_system_state(target: Any, p: dict[str, Any], msg: Message) -> None:
    """Translate system configuration opcodes into SystemState.

    Handles 2E04 (system_mode), 0100 (language), and 313F (datetime).

    :param target: The target entity (TCS/Evohome) to update.
    :param p: The parsed message payload dictionary.
    :param msg: The immutable Message L7 envelope.
    """
    system_state = getattr(target, "system_state", None)
    if system_state is None:
        return

    updates: dict[str, Any] = {}
    if msg.code == Code._0100:
        if SZ_LANGUAGE in p:
            updates[SZ_LANGUAGE] = p[SZ_LANGUAGE]
    elif msg.code == Code._2E04:
        if SZ_SYSTEM_MODE in p:
            updates[SZ_SYSTEM_MODE] = p[SZ_SYSTEM_MODE]
        if SZ_UNTIL in p:
            updates[SZ_UNTIL] = p[SZ_UNTIL]
    elif msg.code == Code._313F:
        if SZ_DATETIME in p:
            updates[SZ_DATETIME] = p[SZ_DATETIME]
    else:
        return

    if not updates:
        return

    dtm = getattr(msg, "dtm", getattr(msg, "timestamp", None))
    if dtm:
        updates["last_updated"] = dtm

    current_state = target.system_state or SystemState()
    new_state = dataclasses.replace(current_state, **updates)
    target.system_state = new_state

    event = StateUpdatedEvent(
        entity_id=getattr(target, "id", "unknown"),
        state=new_state,
        correlation_id=getattr(msg, "correlation_id", uuid.uuid4()),
        causation_id=getattr(msg, "message_id", uuid.uuid4()),
    )
    if hasattr(target, "apply_state_update"):
        target.apply_state_update(event)


def _update_hvac_state(target: Any, p: dict[str, Any], msg: Message) -> None:
    """Translate HVAC ventilation payloads into a frozen HvacState.

    Handles 31D9/31DA/22F1/22F3/10D0/12A0/1298 and related opcodes,
    porting the logic from ``pipeline/ingestion.py`` into the dispatcher's
    CQRS ingestion engine.  See issue #649 / #547.
    """
    if getattr(target, "_SLUG", "") in ("CTL", "BDR", "TRV", "OTB", "UFC", "DHW"):
        return

    hvac_state = getattr(target, "hvac_state", None)
    if hvac_state is None:
        return

    from ramses_rf import quirks

    p = quirks.apply_hvac_quirks(p, target.hvac_state, msg.code)

    fields = [
        SZ_CO2_LEVEL,
        SZ_AIR_QUALITY,
        SZ_AIR_QUALITY_BASIS,
        SZ_BYPASS_MODE,
        SZ_BYPASS_POSITION,
        SZ_BYPASS_STATE,
        SZ_EXHAUST_FAN_SPEED,
        SZ_EXHAUST_FLOW,
        SZ_EXHAUST_TEMP,
        SZ_FAN_RATE,
        SZ_FAN_MODE,
        SZ_FAN_INFO,
        SZ_INDOOR_HUMIDITY,
        SZ_INDOOR_TEMP,
        SZ_OUTDOOR_HUMIDITY,
        SZ_OUTDOOR_TEMP,
        SZ_POST_HEAT,
        SZ_PRE_HEAT,
        SZ_PRESENCE_DETECTED,
        SZ_REMAINING_MINS,
        SZ_SPEED_CAPABILITIES,
        SZ_SUPPLY_FAN_SPEED,
        SZ_SUPPLY_FLOW,
        SZ_SUPPLY_TEMP,
        SZ_TEMPERATURE,
        SZ_FILTER_DIRTY,
        SZ_FROST_CYCLE,
        SZ_HAS_FAULT,
        "dewpoint_temp",
    ]

    # Filter out null-marker values that 31DA/31D9 snapshots emit for
    # sensors the device does not have.  Without this, every polling cycle
    # (~10 min) overwrites good telemetry from 22F1/12A0/22F7 with null
    # markers, causing sensors to bounce to None/FF/0.  See issue #742.
    _NULL_HUMIDITY_FIELDS = frozenset({SZ_INDOOR_HUMIDITY, SZ_OUTDOOR_HUMIDITY})

    updates: dict[str, Any] = {}
    for f in fields:
        if f not in p:
            continue
        val = p[f]
        # None = "not implemented" (e.g. EF in bypass_position)
        if val is None:
            continue
        # Raw hex (e.g. "FF", "04") = non-semantic fan_mode from 31D9
        # long-payload devices; the quirk normalises these to None, but
        # filter here as belt-and-suspenders.  See ramses_cc issue 723.
        if f == SZ_FAN_MODE and isinstance(val, str) and len(val) == 2:
            try:
                int(val, 16)
                continue
            except ValueError:
                pass
        # 0.0 for humidity = "no sensor" (00 parses as 0%, physically impossible)
        if f in _NULL_HUMIDITY_FIELDS and val == 0:
            continue
        updates[f] = val

    # Handle non-standard names passed by the semantic parsers
    if SZ_REMAINING_DAYS in p and p[SZ_REMAINING_DAYS] is not None:
        updates["filter_remaining_days"] = p[SZ_REMAINING_DAYS]
    if SZ_REMAINING_PERCENT in p and p[SZ_REMAINING_PERCENT] is not None:
        updates["filter_remaining_percent"] = p[SZ_REMAINING_PERCENT]
    if SZ_MINUTES in p and msg.code == Code._22F3 and p[SZ_MINUTES] is not None:
        updates["boost_timer_mins"] = p[SZ_MINUTES]
    if SZ_REQ_SPEED in p and p[SZ_REQ_SPEED] is not None:
        updates["request_fan_speed"] = p[SZ_REQ_SPEED]
    if SZ_REQ_REASON in p and p[SZ_REQ_REASON] is not None:
        updates["request_reason"] = p[SZ_REQ_REASON]

    if not updates:
        return

    new_state = dataclasses.replace(target.hvac_state, **updates)
    target.hvac_state = new_state

    event = StateUpdatedEvent(
        entity_id=getattr(target, "id", "unknown"),
        state=new_state,
        correlation_id=getattr(msg, "correlation_id", uuid.uuid4()),
        causation_id=getattr(msg, "message_id", uuid.uuid4()),
    )
    if hasattr(target, "apply_state_update"):
        target.apply_state_update(event)


def _update_dhw_state(target: Any, p: dict[str, Any], msg: Message) -> None:
    """Translate DHW opcodes (10A0/1260/1F41) into the frozen DhwState.

    Mirrors ``pipeline.ingestion.StateProjector._update_dhw_state`` so that
    the legacy dispatcher hydrates the DhwZone's ``dhw_state`` read-model
    (setpoint/overrun/differential from 10A0, mode/active/until from 1F41)
    in addition to ``temp_state``.
    """
    if not isinstance(target, DhwZone):
        return

    updates: dict[str, Any] = {}
    if msg.code == Code._10A0:
        if SZ_SETPOINT in p:
            updates[SZ_SETPOINT] = p[SZ_SETPOINT]
        if SZ_OVERRUN in p:
            updates[SZ_OVERRUN] = p[SZ_OVERRUN]
        if SZ_DIFFERENTIAL in p:
            updates[SZ_DIFFERENTIAL] = p[SZ_DIFFERENTIAL]
    elif msg.code == Code._1F41:
        if SZ_MODE in p:
            updates[SZ_MODE] = p[SZ_MODE]
        if SZ_ACTIVE in p:
            updates[SZ_ACTIVE] = p[SZ_ACTIVE]
        if SZ_UNTIL in p:
            updates[SZ_UNTIL] = p[SZ_UNTIL]

    if not updates:
        return

    new_state = dataclasses.replace(target.dhw_state, **updates)
    target.dhw_state = new_state

    event = StateUpdatedEvent(
        entity_id=target.id,
        state=new_state,
        correlation_id=getattr(msg, "correlation_id", uuid.uuid4()),
        causation_id=getattr(msg, "message_id", uuid.uuid4()),
    )
    target.apply_state_update(event)


def _route_2411_to_fan(gwy: Gateway, msg: Message) -> None:
    """Route a 2411 parameter message to its HvacVentilator aggregate root.

    Phase 2.95 removed the ``HvacVentilator._handle_msg`` override that
    previously invoked ``_handle_2411_message`` (which sets
    ``_supports_2411`` and stores the parameter) and
    ``_handle_initialized_callback`` (which fires the ramses_cc entity
    creation callback).  Without this routing, FAN devices never advertise
    2411 support, so ramses_cc never creates the ~15 parameter ``number``
    entities (comfort temperature, etc.) — see ramses_cc issue 851.

    This re-wires the 2411 handling into the CQRS ingestion pipeline (where
    issue 639 wants domain logic to live) instead of restoring the leaky
    ``_handle_msg`` override.  ``_handle_2411_message`` reads
    ``msg.payload`` directly, so it is invoked once per FAN target, outside
    the per-payload loop in ``_cqrs_ingestion_engine``.
    """
    if getattr(msg, "verb", "") == "RQ":
        return

    registry = getattr(gwy, "device_registry", None)
    if registry is None:
        return

    candidates: list[Any] = []
    if msg.src is not None:
        src_dev = registry.device_by_id.get(msg.src.id)
        if src_dev is not None:
            candidates.append(src_dev)
    if msg.dst is not None:
        dst_dev = registry.device_by_id.get(msg.dst.id)
        if dst_dev is not None and dst_dev not in candidates:
            candidates.append(dst_dev)

    for dev in candidates:
        if not isinstance(dev, HvacVentilator):
            continue
        try:
            dev._handle_2411_message(msg)
            dev._handle_initialized_callback()
        except Exception as err:
            _LOGGER.error(
                "Failed to route 2411 message to ventilator %s: %s",
                dev.id,
                err,
            )


def _cqrs_ingestion_engine(gwy: Gateway, msg: Message) -> None:
    """Parallel ingestion engine to populate immutable CQRS read-models.

    This acts as a Strangler Fig, intercepting decoded payloads and mapping
    them directly into the new `StateUpdatedEvent` structures.
    """
    # Legacy Parity: Request packets do not contain authoritative telemetry.
    if getattr(msg, "verb", "") == "RQ":
        return

    if not isinstance(msg.payload, (dict, list)):
        return

    # 2411 parameter messages are handled by the FAN aggregate root directly
    # (they set _supports_2411 and fire the initialized callback).  This runs
    # before the per-payload loop because _handle_2411_message reads
    # msg.payload as a whole.  See ramses_cc issue 851.
    if msg.code == Code._2411:
        _route_2411_to_fan(gwy, msg)

    payloads = msg.payload if isinstance(msg.payload, list) else [msg.payload]

    for p in payloads:
        if not isinstance(p, dict):
            continue

        targets = _resolve_logical_targets(gwy, msg, p)
        for target in targets:
            with contextlib.suppress(AttributeError, TypeError, ValueError):
                _update_system_state(target, p, msg)
                _update_hvac_state(target, p, msg)
                _update_dhw_state(target, p, msg)
                _update_temperature_state(target, p, msg)
                _update_demand_state(target, p, msg)
                _update_faultlog_state(target, p, msg)


def route_payload(gwy: Gateway, msg: Message) -> None:
    """Determine target entities and deliver the payload to them.

    This is the final stage (Stage 4) of the pipeline. It routes messages to
    the source device (for internal state updates) and constructs a list of
    destination devices based on binding offers, eavesdropping rules, and
    faked device states.

    :param gwy: The gateway handling the message routing.
    :type gwy: Gateway
    :param msg: The fully validated message to be dispatched.
    :type msg: Message
    """
    # NOTE: here, msgs are routed only to devices: routing to other entities (i.e.
    # systems, zones, circuits) is done by those devices (e.g. UFC to UfhCircuit)

    src_dev = gwy.device_registry.device_by_id.get(msg.src.id)
    if src_dev is not None:
        gwy._engine._loop.call_soon(src_dev._handle_msg, msg)

    devices: list[Any] = []

    if (
        msg.code == Code._1FC9
        and isinstance(msg.payload, dict)  # 1. Ensure it's a dict (not bytes)
        and msg.payload.get(SZ_PHASE) == SZ_OFFER  # 2. Safely check for key
    ):
        devices = [
            d
            for d in gwy.device_registry.devices
            if d.id != msg.src.id and d._is_binding
        ]

    elif msg.dst.id == ALL_DEV_ADDR.id:  # some offers use dst=63:, so after 1FC9
        devices = [
            d for d in gwy.device_registry.devices if d.id != msg.src.id and d.is_faked
        ]

    else:
        dst_dev = gwy.device_registry.device_by_id.get(msg.dst.id)
        if msg.dst.id != msg.src.id and dst_dev is not None:
            devices.append(dst_dev)

        src_dev_devices = getattr(src_dev, SZ_DEVICES, None) if src_dev else None
        if src_dev_devices:
            for d in src_dev_devices:
                if d.id != msg.src.id and d not in devices:
                    devices.append(d)

    # Add Eavesdropping Correlation Routing
    if (
        gwy.config.enable_eavesdrop
        and (addrs := getattr(msg._pkt, "_addrs", None)) is not None
    ):
        for addr in addrs:
            if addr.id != msg.src.id and addr.id != getattr(msg.dst, "id", None):
                if dev := gwy.device_registry.device_by_id.get(addr.id):
                    if dev not in devices:
                        devices.append(dev)

    for d in devices:
        if d.id != msg.src.id:
            gwy._engine._loop.call_soon(d._handle_msg, msg)


async def process_msg(gwy: Gateway, msg: Message) -> None:
    """Decode the packet payload and route it through the message pipeline.

    This executor acts as a Chain of Responsibility, routing the message
    through sequential, mathematically isolated validation and dispatch stages.

    :param gwy: The gateway instance handling the routing.
    :type gwy: Gateway
    :param msg: The processed message to route.
    :type msg: Message
    """
    # All methods require msg with a valid payload, except instantiate_devices(),
    # which requires a valid payload only for 000C.
    try:
        if not validate_addresses(gwy, msg):
            _log_message(gwy, msg)
            return

        if not instantiate_devices(gwy, msg):
            return

        if not validate_slugs(gwy, msg):
            _log_message(gwy, msg)
            return

        _cqrs_ingestion_engine(gwy, msg)

        route_payload(gwy, msg)

    except (AssertionError, exc.RamsesException, NotImplementedError) as err:
        (_LOGGER.error if _DBG_INCREASE_LOG_LEVELS else _LOGGER.warning)(
            "%s < %s(%s)", msg._pkt, err.__class__.__name__, err
        )

    except (AttributeError, LookupError, TypeError, ValueError) as err:
        if getattr(gwy.config, "enforce_strict_handling", False):
            raise
        _LOGGER.warning(
            "%s < %s(%s)", msg._pkt, err.__class__.__name__, err, exc_info=True
        )

    else:
        _log_message(gwy, msg)
        if gwy.message_store:
            gwy.message_store.add(msg)
            # why add it? enable for evohome


# TODO: this needs cleaning up (e.g. handle intervening packet)
def detect_array_fragment(this: Message, prev: Message) -> bool:  # _PayloadT
    """Return True if this pkt is the latter half of an array.

    :param this: The current message being evaluated.
    :type this: Message
    :param prev: The previously received message.
    :type prev: Message
    :return: True if the packet is part of a merged array, False otherwise.
    :rtype: bool
    """
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
