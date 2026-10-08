#!/usr/bin/env python3
"""RAMSES RF - CQRS State Projector for mapping telemetry to read-models."""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import uuid
from collections.abc import Callable
from datetime import datetime as dt
from typing import TYPE_CHECKING, Any, Final, cast

from ramses_tx.const import RQ, Code

from . import exceptions as exc, quirks
from .const import (
    SZ_ACTIVE,
    SZ_AIR_QUALITY,
    SZ_AIR_QUALITY_BASIS,
    SZ_BYPASS_MODE,
    SZ_BYPASS_POSITION,
    SZ_BYPASS_STATE,
    SZ_CO2_LEVEL,
    SZ_CO2_LEVEL_FAULT,
    SZ_COOLING_DEMAND,
    SZ_COOLING_MODE,
    SZ_DATETIME,
    SZ_DIFFERENTIAL,
    SZ_DOMAIN_INDEX,
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
    SZ_LOCAL_OVERRIDE,
    SZ_MAX_TEMP,
    SZ_MIN_TEMP,
    SZ_MINUTES,
    SZ_MODE,
    SZ_MULTIROOM_MODE,
    SZ_NAME,
    SZ_OPENWINDOW_FUNCTION,
    SZ_OUTDOOR_HUMIDITY,
    SZ_OUTDOOR_TEMP,
    SZ_OVERRUN,
    SZ_POST_HEAT,
    SZ_PRE_HEAT,
    SZ_PRESENCE_DETECTED,
    SZ_RELAY_DEMAND,
    SZ_REMAINING_DAYS,
    SZ_REMAINING_MINS,
    SZ_REMAINING_PERCENT,
    SZ_REQUEST_REASON,
    SZ_REQUEST_SPEED,
    SZ_SETPOINT,
    SZ_SPEED_CAPABILITIES,
    SZ_SUPPLY_FAN_SPEED,
    SZ_SUPPLY_FLOW,
    SZ_SUPPLY_TEMP,
    SZ_SYSTEM_MODE,
    SZ_TEMPERATURE,
    SZ_UFH_INDEX,
    SZ_UNTIL,
    SZ_ZONE_INDEX,
    DevType,
)
from .devices.dev_base import DeviceBase
from .devices.hvac_ventilators import HvacVentilator
from .messages import Message
from .models import StateUpdatedEvent, SystemState
from .systems.faultlog import FaultLogEntry
from .systems.zones import DhwZone
from .topology_builder import update_topology_schema_state

if TYPE_CHECKING:
    from .gateway import Gateway

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "StateProjector",
    "StateProjectorRegistry",
    "process_state_updates",
]


class StateProjectorRegistry:
    """Registry mapping Code opcode enums to specific state updater functions."""

    def __init__(self) -> None:
        """Initialize the state projector registry."""
        self._handlers: dict[
            Code, list[Callable[[Any, dict[str, Any], Message], None]]
        ] = {}

    def register(
        self,
        code: Code,
        handler: Callable[[Any, dict[str, Any], Message], None],
    ) -> None:
        """Register an updater handler for a specific opcode.

        :param code: The packet opcode to handle.
        :type code: Code
        :param handler: The updater function to invoke.
        :type handler: Callable[[Any, dict[str, Any], Message], None]
        """
        self._handlers.setdefault(code, []).append(handler)

    def get_handlers(
        self, code: Code
    ) -> list[Callable[[Any, dict[str, Any], Message], None]]:
        """Retrieve registered handlers for an opcode.

        :param code: The packet opcode to look up.
        :type code: Code
        :return: List of updater callables for the opcode.
        :rtype: list[Callable[[Any, dict[str, Any], Message], None]]
        """
        return self._handlers.get(code, [])


class StateProjector:
    """CQRS State Projector for translating raw payloads into read-models."""

    def __init__(self, gateway: Gateway) -> None:
        """Initialize the state projector with a gateway instance.

        :param gateway: The gateway handling device entities.
        :type gateway: Gateway
        """
        self._gateway = gateway
        self._registry = StateProjectorRegistry()
        self._setup_registry()

    def _setup_registry(self) -> None:
        """Populate opcode handlers into the registry."""
        self._registry.register(Code._0100, _update_system_state)
        self._registry.register(Code._2E04, _update_system_state)
        self._registry.register(Code._313F, _update_system_state)
        self._registry.register(Code._2D49, _update_system_state)

        self._registry.register(Code._10A0, _update_dhw_state)
        self._registry.register(Code._1F41, _update_dhw_state)

        self._registry.register(Code._0006, _update_schedule_state)
        self._registry.register(Code._0404, _update_schedule_state)

        self._registry.register(Code._0418, _update_faultlog_state)

    async def process_msg(self, msg: Message) -> None:
        """Project a decoded message into read-models.

        :param msg: The message containing payload telemetry.
        :type msg: Message
        """
        await process_state_updates(self._gateway, msg)


_DHW_OPCODES: Final[frozenset[Code | str]] = frozenset(
    {Code._1260, Code._10A0, Code._1F41}
)


def _get_dhw_zone_from_msg(msg: Message, source_device: Any) -> DhwZone | None:
    """Resolve the DhwZone that should ingest a DHW opcode (1260/10A0/1F41).

    These payloads carry no ``zone_index``/``domain_id``, so standard target
    resolution in ``_resolve_logical_targets`` misses the DhwZone.

    ``1260`` is sent by the DhwSensor (or relayed by the Controller as an
    RP); ``10A0``/``1F41`` are sent by the Controller.  The
    appliance_control (OTB) also emits ``10A0``/``1260`` with different
    semantics (CH setpoint / null temp) and is excluded to avoid
    clobbering the DHW read-models.

    See: https://github.com/ramses-rf/ramses_cc/issues/843

    :param msg: The inbound message.
    :type msg: Message
    :param source_device: The source device (DhwSensor or Controller).
    :type source_device: Any
    :return: The DhwZone to route to, or ``None`` if the message is not
        a DHW opcode or the source is not a DHW sender.
    :rtype: DhwZone | None
    """
    if msg.code not in _DHW_OPCODES:
        return None

    src_type = getattr(source_device, "type", None) or (
        msg.src.id[:2] if hasattr(msg.src, "id") and msg.src.id else None
    )
    src_slug = str(getattr(source_device, "_SLUG", ""))
    if msg.code == Code._1260:
        is_dhw_src = src_type in ("07", "01") or src_slug in ("DHW", "CTL")
    else:  # 10A0 / 1F41 are owned by the Controller
        is_dhw_src = src_type == "01" or "CTL" in src_slug or "PRG" in src_slug

    if not is_dhw_src:
        return None

    tcs = getattr(source_device, "tcs", None) or getattr(
        source_device, "_tcs", None
    )
    if tcs is None and hasattr(source_device, "dhw"):
        tcs = source_device

    if tcs is None:
        return None

    return cast(DhwZone | None, getattr(tcs, "dhw", None))


def _resolve_logical_targets(
    gateway: Gateway, msg: Message, p: dict[str, Any]
) -> list[Any]:
    """Resolve software twin entities targeted by a payload.

    :param gateway: Gateway instance with device registry.
    :type gateway: Gateway
    :param msg: L7 Message envelope.
    :type msg: Message
    :param p: Parsed payload dictionary.
    :type p: dict[str, Any]
    :return: List of target entity instances.
    :rtype: list[Any]
    """
    targets: list[Any] = []
    registry = getattr(gateway, "device_registry", None)
    src_dev = registry.device_by_id.get(msg.src.id) if registry else None
    dst_dev = (
        registry.device_by_id.get(msg.dst.id)
        if registry and hasattr(msg.dst, "id")
        else None
    )

    tcs = getattr(src_dev, "tcs", None) if src_dev else None
    tcs = tcs or getattr(gateway, "tcs", None)
    if tcs is None and registry:
        for candidate_dev in registry.device_by_id.values():
            if str(candidate_dev.id).startswith("01:"):
                tcs = getattr(candidate_dev, "tcs", None) or candidate_dev
                break

    # 1. Fault logs strictly target the TCS (if it exists) or the source device
    if msg.code == Code._0418:
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
    # Route packets to the destination device's cache.
    # HVAC packets (e.g. 22F1 fan_mode from REM->FAN) target the destination
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

    # 4. Virtual twins (Zones) get updates if explicitly addressed by index,
    # but only when the Controller is the source or the destination: a
    # self-addressed broadcast (e.g. a DTS92 announcing its own setpoint,
    # `I --- 22:xxx --:------ 22:xxx`) carries a device-local idx that must
    # not be read as a controller zone index.  See:
    # https://github.com/ramses-rf/ramses_cc/issues/1208
    # Non-controller 30C9 (sensor broadcasts) is handled in step 8.
    if SZ_ZONE_INDEX in p and tcs:
        tcs_id = getattr(tcs, "id", "")
        is_ctl_idx = src_type in (
            "01",
            "02",
            DevType.CTL,
            DevType.UFC,
        ) or tcs_id in (
            getattr(msg.src, "id", ""),
            getattr(msg.dst, "id", ""),
        )
        if is_ctl_idx:
            if zone := tcs.zone_by_index.get(p[SZ_ZONE_INDEX]):
                if zone not in targets:
                    targets.append(zone)

    # 5. Domain twins (TCS, DHW) get updates.
    domain_index = p.get(SZ_DOMAIN_INDEX) or p.get("domain_id")
    if domain_index and tcs:
        if domain_index == "FC" and tcs not in targets:
            targets.append(tcs)
        elif (
            domain_index in ("FA", "F9")
            and getattr(tcs, "dhw", None) is not None
        ):
            if tcs.dhw not in targets:
                targets.append(tcs.dhw)

    # 6. System-level opcodes (2E04/0100/313F/2D49) target the TCS directly.
    #    These packets have no domain_id/zone_index, so steps 4/5 miss them.
    if (
        msg.code in (Code._2E04, Code._0100, Code._313F, Code._2D49)
        and tcs
        and tcs not in targets
    ):
        targets.append(tcs)

    # 7. DHW opcodes (1260/10A0/1F41) carry no domain_id/zone_index, so steps
    #    4/5 miss the DhwZone.  Route them via the shared helper.
    #    See: https://github.com/ramses-rf/ramses_cc/issues/843
    dhw = _get_dhw_zone_from_msg(msg, src_dev)
    if dhw is not None and dhw not in targets:
        targets.append(dhw)

    # 8. Sensor-sourced 30C9 has no zone_index (the sensor is not a controller,
    #    so _build_index_dict injects no zone_index), so step 4 misses the parent
    #    zone. Route 30C9 from a sensor to its parent zone so the zone's
    #    current_temperature is hydrated even when the controller doesn't
    #    broadcast 30C9 for that zone.
    #    Restricted to designated sensor or sole actuator (issue #976).
    #    See: https://github.com/ramses-rf/ramses_cc/issues/927
    if msg.code == Code._30C9 and src_dev and "temperature" in p:
        parent = getattr(src_dev, "_parent", None)
        if (
            parent is not None
            and hasattr(parent, "temp_state")
            and hasattr(parent, "zone_state")
            and parent not in targets
        ):
            parent_sensor = getattr(parent, "sensor", None)
            parent_actuators = getattr(parent, "actuators", [])
            if src_dev is parent_sensor or (
                parent_sensor is None
                and len(parent_actuators) == 1
                and src_dev in parent_actuators
            ):
                targets.append(parent)

    return targets


def _update_system_state(target: Any, p: dict[str, Any], msg: Message) -> None:
    """Translate system configuration opcodes into SystemState.

    Handles 2E04 (system_mode), 0100 (language), 313F (datetime), and
    2D49 (cooling_mode).

    :param target: Target entity (TCS/Evohome) to update.
    :type target: Any
    :param p: Parsed message payload dictionary.
    :type p: dict[str, Any]
    :param msg: Immutable Message envelope.
    :type msg: Message
    """
    system_state = getattr(target, "system_state", None)
    if system_state is None or not dataclasses.is_dataclass(system_state):
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
    elif msg.code == Code._2D49:
        if SZ_COOLING_DEMAND in p:
            updates[SZ_COOLING_MODE] = p[SZ_COOLING_DEMAND]
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
    porting the logic into the CQRS state projector.
    See issues ramses-rf/ramses_rf#649 and ramses-rf/ramses_rf#547.

    :param target: Target entity to update.
    :type target: Any
    :param p: Parsed payload dictionary.
    :type p: dict[str, Any]
    :param msg: Message envelope.
    :type msg: Message
    """
    if getattr(target, "_SLUG", "") in (
        "CTL",
        "BDR",
        "TRV",
        "OTB",
        "UFC",
        "DHW",
    ):
        return

    hvac_state = getattr(target, "hvac_state", None)
    if hvac_state is None or not dataclasses.is_dataclass(hvac_state):
        return

    strategy = (
        target.get_configured_strategy()
        if isinstance(target, DeviceBase)
        else None
    )
    p = quirks.apply_hvac_quirks(
        p, target.hvac_state, msg.code, strategy=strategy
    )

    # The parser names 22F1 modes via a mode_max heuristic, which
    # mis-decodes real Orcon remotes (e.g. VMN-15LF01 sends
    # mode_max=04 -> "itho" names, so index 01 "low" displays as
    # "auto").  A remote has no scheme of its own: its commands use the
    # bound FAN's vocabulary, so fall back to the parent fan's
    # configured scheme.  See ramses-rf/ramses_cc issue 1216.
    if (
        msg.code == Code._22F1
        and SZ_FAN_MODE in p
        and p.get("_mode_index") is not None
    ):
        mode_strategy = strategy
        if mode_strategy is None and isinstance(target, DeviceBase):
            parent_fan = getattr(target, "parent_fan", None)
            if isinstance(parent_fan, DeviceBase):
                mode_strategy = parent_fan._get_configured_strategy()
        if mode_strategy is not None:
            mapped = mode_strategy.fan_modes.get(str(p["_mode_index"]))
            if mapped is not None:
                p[SZ_FAN_MODE] = mapped

    fields = [
        SZ_CO2_LEVEL,
        SZ_CO2_LEVEL_FAULT,
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

    _NULL_HUMIDITY_FIELDS = frozenset(
        {SZ_INDOOR_HUMIDITY, SZ_OUTDOOR_HUMIDITY}
    )

    updates: dict[str, Any] = {}
    for field_name in fields:
        if field_name not in p:
            continue
        field_val = p[field_name]
        # Filter out null-marker values that 31DA/31D9 snapshots emit for
        # sensors the device does not have.  Without this, every polling cycle
        # (~10 min) overwrites good telemetry from 22F1/12A0/22F7 with null
        # markers, causing sensors to bounce to None/FF/0.  See issue #742.
        if field_val is None:
            continue
        # None = "not implemented" (e.g. EF in bypass_position)
        # Raw hex (e.g. "FF", "04") = non-semantic fan_mode from 31D9
        # long-payload devices; the quirk normalises these to None, but
        # filter here as belt-and-suspenders.  See ramses_cc issue 723.
        if (
            field_name == SZ_FAN_MODE
            and isinstance(field_val, str)
            and len(field_val) == 2
        ):
            try:
                int(field_val, 16)
                continue
            except ValueError:
                pass
        # 0.0 for humidity = "no sensor" (00 parses as 0%, physically impossible)
        if field_name in _NULL_HUMIDITY_FIELDS and field_val == 0:
            continue
        updates[field_name] = field_val

    # Flag filter_remaining support on device
    if (
        msg.code == Code._10D0
        and not target._supports_10d0
        and (
            p[SZ_REMAINING_DAYS] is not None
            or p[SZ_REMAINING_PERCENT] is not None
        )
    ):
        target._supports_10d0 = True
        _LOGGER.debug(
            "Device %s confirmed 10D0 Filter Change support", target.id
        )

    # Handle non-standard names passed by the semantic parsers
    if SZ_REMAINING_DAYS in p and p[SZ_REMAINING_DAYS] is not None:
        updates["filter_remaining_days"] = p[SZ_REMAINING_DAYS]
    if SZ_REMAINING_PERCENT in p and p[SZ_REMAINING_PERCENT] is not None:
        updates["filter_remaining_percent"] = p[SZ_REMAINING_PERCENT]
    if (
        SZ_MINUTES in p
        and msg.code == Code._22F3
        and p[SZ_MINUTES] is not None
    ):
        updates["boost_timer_mins"] = p[SZ_MINUTES]
    req_speed = p.get(SZ_REQUEST_SPEED, p.get("req_speed"))
    if req_speed is not None:
        updates["request_fan_speed"] = req_speed
    req_reason = p.get(SZ_REQUEST_REASON, p.get("req_reason"))
    if req_reason is not None:
        updates["request_reason"] = req_reason

    if not updates:
        return

    if SZ_FAN_MODE in updates and hasattr(target, "_last_fan_mode_dtm"):
        target._last_fan_mode_dtm = msg.dtm

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

    Hydrates the DhwZone's ``dhw_state`` read-model (setpoint/overrun/
    differential from 10A0, mode/active/until from 1F41) in addition
    to ``temp_state``.

    :param target: Target entity to update.
    :type target: Any
    :param p: Parsed payload dictionary.
    :type p: dict[str, Any]
    :param msg: Message envelope.
    :type msg: Message
    """
    if not hasattr(target, "dhw_state"):
        return
    dhw_state = getattr(target, "dhw_state", None)
    if dhw_state is None or not dataclasses.is_dataclass(dhw_state):
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


def _update_temperature_state(
    target: Any, p: dict[str, Any], msg: Message
) -> None:
    """Translate temperature data into a frozen StateUpdatedEvent.

    :param target: Target entity to update.
    :type target: Any
    :param p: Parsed payload dictionary.
    :type p: dict[str, Any]
    :param msg: Message envelope.
    :type msg: Message
    """
    temp_state = getattr(target, "temp_state", None)
    if temp_state is None or not dataclasses.is_dataclass(temp_state):
        return

    updates: dict[str, Any] = {}

    if SZ_TEMPERATURE in p:
        target_id = getattr(target, "id", str(target))
        src_id = getattr(msg.src, "id", str(msg.src))

        # Legacy Parity: Physical sensors only track their own local sensor readings.
        # We must ignore Zone temperature syncs sent TO them by the Controller.
        if (
            getattr(target, "_SLUG", "") in ("TRV", "THM")
            and src_id != target_id
        ):
            pass
        else:
            updates[SZ_TEMPERATURE] = p[SZ_TEMPERATURE]

    if "setpoint" in p:
        # Prevent boiler setpoint opcodes (e.g. 22D9) from mutating zone setpoints
        if msg.code != Code._22D9 or not hasattr(target, "zone_state"):
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


def _update_zone_state(target: Any, p: dict[str, Any], msg: Message) -> None:
    """Translate zone configuration opcodes into ZoneState.

    Handles:
    - 0004 (zone_name): updates zone_state.name
    - 000A (zone_config): updates min_temp, max_temp, local_override,
      openwindow_function, multiroom_mode (issue 1102)
    - 2349 (zone_mode): updates mode, setpoint, until
    - 2309 (setpoint): updates setpoint

    :param target: Target zone entity to update.
    :type target: Any
    :param p: Parsed payload dictionary.
    :type p: dict[str, Any]
    :param msg: Message envelope.
    :type msg: Message
    """
    zone_state = getattr(target, "zone_state", None)
    if zone_state is None or not dataclasses.is_dataclass(zone_state):
        return

    updates: dict[str, Any] = {}

    if msg.code == Code._0004:
        if SZ_NAME in p:
            updates[SZ_NAME] = str(p[SZ_NAME])

    elif msg.code == Code._000A:
        if SZ_MIN_TEMP in p:
            updates[SZ_MIN_TEMP] = p[SZ_MIN_TEMP]
        if SZ_MAX_TEMP in p:
            updates[SZ_MAX_TEMP] = p[SZ_MAX_TEMP]
        if SZ_LOCAL_OVERRIDE in p:
            updates[SZ_LOCAL_OVERRIDE] = p[SZ_LOCAL_OVERRIDE]
        if SZ_OPENWINDOW_FUNCTION in p:
            updates[SZ_OPENWINDOW_FUNCTION] = p[SZ_OPENWINDOW_FUNCTION]
        if SZ_MULTIROOM_MODE in p:
            updates[SZ_MULTIROOM_MODE] = p[SZ_MULTIROOM_MODE]

    elif msg.code == Code._2349:
        if SZ_MODE in p:
            updates[SZ_MODE] = p[SZ_MODE]
        if SZ_SETPOINT in p:
            updates[SZ_SETPOINT] = p[SZ_SETPOINT]
        if SZ_UNTIL in p:
            updates[SZ_UNTIL] = p[SZ_UNTIL]

    elif msg.code == Code._2309:
        if SZ_SETPOINT in p:
            updates[SZ_SETPOINT] = p[SZ_SETPOINT]

    else:
        return

    if not updates:
        return

    new_state = dataclasses.replace(target.zone_state, **updates)
    event = StateUpdatedEvent(
        entity_id=getattr(target, "id", "unknown"),
        state=new_state,
        correlation_id=getattr(msg, "correlation_id", uuid.uuid4()),
        causation_id=getattr(msg, "message_id", uuid.uuid4()),
    )
    target.apply_state_update(event)


def _update_demand_state(target: Any, p: dict[str, Any], msg: Message) -> None:
    """Translate demand data into a frozen StateUpdatedEvent.

    :param target: Target entity to update.
    :type target: Any
    :param p: Parsed payload dictionary.
    :type p: dict[str, Any]
    :param msg: Message envelope.
    :type msg: Message
    """
    demand_state = getattr(target, "demand_state", None)
    if demand_state is None or not dataclasses.is_dataclass(demand_state):
        return

    slug = getattr(target, "_SLUG", "")
    updates: dict[str, Any] = {}

    if SZ_HEAT_DEMAND in p:
        if slug in ("CTL", "UFC"):
            if (p.get(SZ_DOMAIN_INDEX) or p.get("domain_id")) == "FC":
                updates[SZ_HEAT_DEMAND] = p[SZ_HEAT_DEMAND]
        elif SZ_UFH_INDEX not in p:
            updates[SZ_HEAT_DEMAND] = p[SZ_HEAT_DEMAND]

    if SZ_RELAY_DEMAND in p:
        if (
            slug == "UFC"
            and (p.get(SZ_DOMAIN_INDEX) or p.get("domain_id")) != "FC"
        ):
            pass
        else:
            updates[SZ_RELAY_DEMAND] = p[SZ_RELAY_DEMAND]
            updates["relay_active"] = float(p[SZ_RELAY_DEMAND]) > 0.0

    if getattr(msg, "code", None) == Code._0009 and "failsafe_enabled" in p:
        updates["relay_failsafe"] = p["failsafe_enabled"]

    # 1100 (TPI params) — populate the TCS's _tpi_params dict (issue 1102).
    # TPI params don't fit into DemandState; they're stored per-domain on the
    # TCS and read by tcs.tpi_params.
    if msg.code == Code._1100 and "cycle_rate" in p:
        tcs_for_tpi = getattr(target, "tcs", None) or (
            target if slug in ("CTL", "BDR") else None
        )
        if tcs_for_tpi is not None:
            tpi_dict = getattr(tcs_for_tpi, "_tpi_params", None)
            if tpi_dict is not None:
                domain = p.get(SZ_DOMAIN_INDEX) or p.get("domain_id") or "FC"
                tpi_dict[domain] = p

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

    # Populate the TCS's per-domain demand dicts (issue 1102 / ramses_cc#1026).
    # The legacy _handle_msg stored 0008/3150 messages keyed by domain/zone
    # index in _relay_demands/_heat_demands.  The CQRS DemandState only tracks
    # a single heat_demand/relay_demand, so the per-domain dicts were never
    # populated after the legacy handler was removed.
    tcs = getattr(target, "tcs", None) or (
        target if slug in ("CTL", "UFC") else None
    )
    if tcs is not None:
        domain = p.get(SZ_DOMAIN_INDEX) or p.get("domain_id")
        if domain and SZ_RELAY_DEMAND in p:
            relay_dict = getattr(tcs, "_relay_demands", None)
            if relay_dict is not None:
                relay_dict[domain] = msg
        if domain and SZ_HEAT_DEMAND in p and slug in ("CTL", "UFC"):
            heat_dict = getattr(tcs, "_heat_demands", None)
            if heat_dict is not None:
                heat_dict[domain] = msg


def _update_faultlog_state(
    target: Any, p: dict[str, Any], msg: Message
) -> None:
    """Translate 0418 fault log data into a frozen StateUpdatedEvent.

    This handles the immutable tuple appending tracking required by the
    CQRS FaultLogState read-model container.

    :param target: Target entity to update.
    :type target: Any
    :param p: Parsed payload dictionary.
    :type p: dict[str, Any]
    :param msg: Message envelope.
    :type msg: Message
    """
    if msg.code != Code._0418 or getattr(target, "state", None) is None:
        return
    if type(target.state).__name__ != "FaultLogState":
        return

    # Guard: Ensure the entry index exists in the parsed payload
    if "log_index" not in p:
        return

    try:
        entry = FaultLogEntry.from_msg(msg)
        current_entries = getattr(target.state, "entries", ())
        # Append to the immutable tuple, safely removing stale matching timestamps
        filtered = [
            e for e in current_entries if e.timestamp != entry.timestamp
        ]
        new_entries = tuple(filtered) + (entry,)

        new_state = dataclasses.replace(target.state, entries=new_entries)

        event = StateUpdatedEvent(
            entity_id=getattr(target, "id", "unknown"),
            state=new_state,
            correlation_id=getattr(msg, "correlation_id", uuid.uuid4()),
            causation_id=getattr(msg, "message_id", uuid.uuid4()),
        )
        target.apply_state_update(event)
    except (AttributeError, KeyError, TypeError, ValueError) as err:
        _LOGGER.warning(
            "Failed to process fault log entry from msg %s: %s", msg, err
        )


def _route_2411_to_fan(gateway: Gateway, msg: Message) -> None:
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
    the per-payload loop in ``process_state_updates``.

    :param gwy: Gateway handling device entities.
    :type gwy: Gateway
    :param msg: Message envelope.
    :type msg: Message
    """
    if getattr(msg, "verb", "") == RQ:
        return

    registry = getattr(gateway, "device_registry", None)
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

    for candidate_dev in candidates:
        if not isinstance(candidate_dev, HvacVentilator):
            continue
        try:
            candidate_dev._handle_2411_message(msg)
            candidate_dev._handle_initialized_callback()
        except (
            exc.RamsesException,
            AttributeError,
            TypeError,
            ValueError,
        ) as err:
            _LOGGER.error(
                "Failed to route 2411 message to ventilator %s: %s",
                candidate_dev.id,
                err,
            )


def _update_schedule_state(
    target: Any, p: dict[str, Any], msg: Message
) -> None:
    """Route 0006 version and 0404 fragment packets to Schedule read-models.

    :param target: Target entity.
    :type target: Any
    :param p: Parsed payload dictionary.
    :type p: dict[str, Any]
    :param msg: Message envelope.
    :type msg: Message
    """
    if msg.code not in (Code._0006, Code._0404):
        return

    sched = getattr(target, "schedule", None)
    if sched is not None and hasattr(sched, "process_schedule_msg"):
        sched.process_schedule_msg(msg)


def _is_newer_dtm(new_dtm: dt, old_dtm: dt | None) -> bool:
    """Return True if a message is fresher than a stored timestamp.

    Liveness stamps must be monotonic: message handlers run as
    fire-and-forget tasks whose completion order does not match packet
    order, so a late or replayed packet must never roll
    ``_last_msg_dtm`` backwards and flap the device to unavailable
    (ramses_cc issue 1278).  ``timestamp()`` treats naive datetimes as
    local time, matching ``DeviceBase.is_available`` semantics.

    :param new_dtm: The incoming message timestamp.
    :type new_dtm: dt
    :param old_dtm: The currently stored timestamp (may be ``None``).
    :type old_dtm: dt | None
    :return: True if the incoming stamp should replace the stored one.
    :rtype: bool
    """
    if old_dtm is None:
        return True
    try:
        return new_dtm.timestamp() > old_dtm.timestamp()
    except TypeError:
        # Non-comparable values (mocks in tests) fail open to preserve the
        # previous unconditional stamping behaviour.
        return True


async def process_state_updates(gateway: Gateway, msg: Message) -> None:
    """Ingest message payloads into entity state read-models.

    Acts as a Strangler Fig, intercepting decoded payloads and mapping
    them directly into the new `StateUpdatedEvent` structures.

    :param gateway: Gateway handling device registry and state.
    :type gateway: Gateway
    :param msg: Message envelope containing payload.
    :type msg: Message
    """
    # Notify candidate devices of _last_msg_dtm and all binding devices of
    # rcvd_msg.  Only the source device is updated: a message merely
    # addressed to a device does not prove that device is reachable (our
    # own poll echoes would otherwise mask a silent device forever).
    if registry := getattr(gateway, "device_registry", None):
        for device in list(registry.device_by_id.values()):
            src_id = getattr(msg.src, "id", None)
            if device.id == src_id:
                # The whole liveness triple updates atomically and only
                # for a fresher message: an out-of-order cached/late
                # packet must not roll these back (ramses_cc issue 1278).
                # NB: _mark_src_device_alive() already stamped
                # _last_msg_dtm with this msg's dtm upstream, so the
                # stored stamp may EQUAL msg.dtm — allow equality or
                # _last_msg and _missed_polls are never updated for
                # live packets (ramses_rf issue 1262).
                stored_dtm = getattr(device, "_last_msg_dtm", None)
                if _is_newer_dtm(msg.dtm, stored_dtm) or (
                    stored_dtm is not None and stored_dtm == msg.dtm
                ):
                    if hasattr(device, "_last_msg_dtm"):
                        device._last_msg_dtm = msg.dtm
                    if hasattr(device, "_last_msg"):
                        device._last_msg = msg
                    if hasattr(device, "_missed_polls"):
                        device._missed_polls = 0
            if device.id in (
                src_id,
                getattr(msg.dst, "id", None),
            ):
                # Fire the initialized callback on the first message from/to
                # a FAN device.  Phase 2.95 removed the _handle_msg override
                # that used to do this; without it, ramses_cc never sends the
                # initial 2411 RQs and all parameter entities stay
                # unavailable.  See ramses_cc issue 851.
                if isinstance(device, HvacVentilator):
                    device._handle_initialized_callback()
            if (bm := getattr(device, "_binding_manager", None)) and getattr(
                bm, "is_binding", False
            ):
                bm.rcvd_msg(msg)

    # Record RSSI for the source device in the HGI's tracker
    # (issue 1047: transport-layer RSSI tracking per HGI).
    if tracker := getattr(gateway, "_rssi_tracker", None):
        src_id = getattr(msg.src, "id", None)
        if src_id and msg.rssi and msg.rssi not in ("...", "---", "///"):
            tracker.record(src_id, msg.rssi, msg.dtm)

    if not isinstance(msg.payload, (dict, list)):
        return

    # 2411 parameter messages are handled by the FAN aggregate root directly
    # (they set _supports_2411 and store the parameter value).  This runs
    # before the per-payload loop because _handle_2411_message reads
    # msg.payload as a whole.  See ramses_cc issue 851.
    if msg.code == Code._2411:
        _route_2411_to_fan(gateway, msg)

    raw_payloads = (
        msg.payload if isinstance(msg.payload, list) else [msg.payload]
    )
    payloads = [
        p.to_dict() if hasattr(p, "to_dict") else p for p in raw_payloads
    ]
    with contextlib.suppress(
        exc.DeviceNotFoundError, exc.SchemaInconsistentError
    ):
        for payload in payloads:
            if isinstance(payload, dict):
                await update_topology_schema_state(gateway, payload, msg)

    # Legacy Parity: Request packets (RQ) do not contain state update telemetry.
    if getattr(msg, "verb", "") == RQ:
        return

    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        targets = _resolve_logical_targets(gateway, msg, payload)
        for target in targets:
            _update_system_state(target, payload, msg)
            _update_hvac_state(target, payload, msg)
            _update_dhw_state(target, payload, msg)
            _update_zone_state(target, payload, msg)
            _update_temperature_state(target, payload, msg)
            _update_demand_state(target, payload, msg)
            _update_faultlog_state(target, payload, msg)
            _update_schedule_state(target, payload, msg)
