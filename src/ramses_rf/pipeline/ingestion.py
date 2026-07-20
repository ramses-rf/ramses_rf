"""RAMSES RF - Asynchronous CQRS State Ingestion Engine.

Consumes messages from the central dispatcher queues and translates
decoded telemetry payloads into frozen, observable StateUpdatedEvents.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import uuid
from typing import Any, Final

from ramses_rf import quirks
from ramses_rf.address import HGI_DEV_ADDR, Address
from ramses_rf.commands.builders import build_dto
from ramses_rf.commands.core import Command as Intent_
from ramses_rf.const import (
    SZ_ACTIVE,
    SZ_ACTUATOR_COUNTDOWN,
    SZ_ACTUATOR_ENABLED,
    SZ_AIR_QUALITY,
    SZ_AIR_QUALITY_BASIS,
    SZ_BATTERY_LEVEL,
    SZ_BATTERY_LOW,
    SZ_BYPASS_MODE,
    SZ_BYPASS_POSITION,
    SZ_BYPASS_STATE,
    SZ_CH_ACTIVE,
    SZ_CH_ENABLED,
    SZ_CH_SETPOINT,
    SZ_CO2_LEVEL,
    SZ_COOL_ACTIVE,
    SZ_CYCLE_COUNTDOWN,
    SZ_DATETIME,
    SZ_DEWPOINT_TEMP,
    SZ_DHW_ACTIVE,
    SZ_DHW_FLOW_RATE,
    SZ_DIFFERENTIAL,
    SZ_DOMAIN_ID,
    SZ_EXHAUST_FAN_SPEED,
    SZ_EXHAUST_FLOW,
    SZ_EXHAUST_TEMP,
    SZ_FAN_INFO,
    SZ_FAN_MODE,
    SZ_FAN_RATE,
    SZ_FLAME_ON,
    SZ_HEAT_DEMAND,
    SZ_INDOOR_HUMIDITY,
    SZ_INDOOR_TEMP,
    SZ_LANGUAGE,
    SZ_MAX_REL_MODULATION,
    SZ_MINUTES,
    SZ_MODE,
    SZ_MODULATION_LEVEL,
    SZ_NAME,
    SZ_OUTDOOR_HUMIDITY,
    SZ_OUTDOOR_TEMP,
    SZ_OVERRUN,
    SZ_POST_HEAT,
    SZ_PRE_HEAT,
    SZ_PRESENCE_DETECTED,
    SZ_PRESSURE,
    SZ_REL_MODULATION_LEVEL,
    SZ_RELAY_DEMAND,
    SZ_RELAY_FAILSAFE,
    SZ_REMAINING_DAYS,
    SZ_REMAINING_MINS,
    SZ_REMAINING_PERCENT,
    SZ_REQ_REASON,
    SZ_REQ_SPEED,
    SZ_SETPOINT,
    SZ_SETPOINT_BOUNDS,
    SZ_SPEED_CAPABILITIES,
    SZ_SUPPLY_FAN_SPEED,
    SZ_SUPPLY_FLOW,
    SZ_SUPPLY_TEMP,
    SZ_SYSTEM_MODE,
    SZ_TEMPERATURE,
    SZ_UFH_IDX,
    SZ_UNTIL,
    SZ_WINDOW_OPEN,
    SZ_ZONE_IDX,
    Code,
    DevType,
)
from ramses_rf.enums import Action
from ramses_rf.messages import Message
from ramses_rf.models import (
    ActuatorState,
    DemandState,
    DhwState,
    HvacState,
    OpenThermState,
    PowerState,
    StateUpdatedEvent,
    SystemState,
    TemperatureState,
    TrvState,
    UfhState,
    ZoneState,
)
from ramses_rf.protocol.opentherm import OtDataId

# --- Translation Maps (Static Constant Blocks) ---

RAMSES_HEATING_MAP: Final[dict[Code, tuple[str, str, str]]] = {
    Code._3200: (SZ_TEMPERATURE, "temperatures", "boiler_output"),
    Code._3210: (SZ_TEMPERATURE, "temperatures", "boiler_return"),
    Code._22D9: (SZ_SETPOINT, "temperatures", "boiler_setpoint"),
    Code._1081: (SZ_SETPOINT, "temperatures", "ch_max_setpoint"),
    Code._1300: (SZ_PRESSURE, "base", "ch_water_pressure"),
    Code._12F0: (SZ_DHW_FLOW_RATE, "base", "dhw_flow_rate"),
    Code._10A0: (SZ_SETPOINT, "temperatures", "dhw_setpoint"),
    Code._1260: (SZ_TEMPERATURE, "temperatures", "dhw"),
    Code._1290: (SZ_TEMPERATURE, "temperatures", "outside"),
}

OPENTHERM_FIELD_MAP: Final[dict[OtDataId, tuple[str, str]]] = {
    OtDataId.BOILER_OUTPUT_TEMP: ("temperatures", "boiler_output"),
    OtDataId.BOILER_RETURN_TEMP: ("temperatures", "boiler_return"),
    OtDataId.CONTROL_SETPOINT: ("temperatures", "boiler_setpoint"),
    OtDataId.CH_MAX_SETPOINT: ("temperatures", "ch_max_setpoint"),
    OtDataId.CH_WATER_PRESSURE: ("base", "ch_water_pressure"),
    OtDataId.DHW_FLOW_RATE: ("base", "dhw_flow_rate"),
    OtDataId.DHW_SETPOINT: ("temperatures", "dhw_setpoint"),
    OtDataId.DHW_TEMP: ("temperatures", "dhw"),
    OtDataId.OEM_CODE: ("base", "oem_code"),
    OtDataId.OUTSIDE_TEMP: ("temperatures", "outside"),
    OtDataId.REL_MODULATION_LEVEL: ("base", "rel_modulation_level"),
    OtDataId._0E: ("base", "max_rel_modulation"),
    OtDataId.BURNER_HOURS: ("counters", "burner_hours"),
    OtDataId.BURNER_STARTS: ("counters", "burner_starts"),
    OtDataId.BURNER_FAILED_STARTS: ("counters", "burner_failed_starts"),
    OtDataId.CH_PUMP_HOURS: ("counters", "ch_pump_hours"),
    OtDataId.CH_PUMP_STARTS: ("counters", "ch_pump_starts"),
    OtDataId.DHW_BURNER_HOURS: ("counters", "dhw_burner_hours"),
    OtDataId.DHW_BURNER_STARTS: ("counters", "dhw_burner_starts"),
    OtDataId.DHW_PUMP_HOURS: ("counters", "dhw_pump_hours"),
    OtDataId.DHW_PUMP_STARTS: ("counters", "dhw_pump_starts"),
    OtDataId.FLAME_LOW_SIGNALS: ("counters", "flame_signal_low"),
}

_LOGGER: Final[logging.Logger] = logging.getLogger(__name__)


class StateProjector:
    """Projector task that transforms incoming telemetry into immutable
    states.
    """

    def __init__(self, gwy: Any, ssot_queue: asyncio.Queue[Message]) -> None:
        """Initialize the state projector background worker.

        :param gwy: The active Gateway facade instance.
        :type gwy: Any
        :param ssot_queue: Single Source of Truth Queue from
            CentralDispatcher.
        :type ssot_queue: asyncio.Queue[Message]
        """
        self._gwy = gwy
        self._queue = ssot_queue
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the background consumer projector loop.

        :return: None
        :rtype: None
        """
        if self._task is None:
            self._task = asyncio.create_task(self._worker_loop())

    async def stop(self) -> None:
        """Stop the background consumer projector loop cleanly.

        :return: None
        :rtype: None
        """
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _worker_loop(self) -> None:
        """Continuously pop messages from the queue for state processing.

        :return: None
        :rtype: None
        """
        while True:
            msg = await self._queue.get()
            try:
                self.process_message_state(msg)
            except Exception as err:
                _LOGGER.error("Failed to ingest state payload: %s", err)
            finally:
                self._queue.task_done()

    def _route_2411_to_fan(self, msg: Message) -> None:
        """Route a 2411 parameter message to its FAN aggregate root.

        Mirrors ``dispatcher._route_2411_to_fan`` for the StateProjector
        ingestion path.  Phase 2.95 removed the
        ``HvacVentilator._handle_msg`` override that previously invoked
        ``_handle_2411_message`` (sets ``_supports_2411`` and stores the
        parameter) and ``_handle_initialized_callback`` (fires the
        ramses_cc entity-creation callback).  Without this routing, FAN
        devices never advertise 2411 support, so ramses_cc never creates
        the ~15 parameter ``number`` entities.  See ramses_cc issue 851.
        """
        registry = getattr(self._gwy, "device_registry", None)
        if registry is None:
            return

        candidates: list[Any] = []
        src_dev = registry.device_by_id.get(msg.src.id)
        dst_dev = registry.device_by_id.get(msg.dst.id)
        if src_dev is not None:
            candidates.append(src_dev)
        if dst_dev is not None and dst_dev is not src_dev:
            candidates.append(dst_dev)

        for dev in candidates:
            if getattr(dev, "_SLUG", "") != DevType.FAN:
                continue
            handler = getattr(dev, "_handle_2411_message", None)
            init_cb = getattr(dev, "_handle_initialized_callback", None)
            if not callable(handler) or not callable(init_cb):
                continue
            with contextlib.suppress(AttributeError, TypeError, ValueError):
                handler(msg)
                init_cb()

    def process_message_state(self, msg: Message) -> None:
        """Route valid inbound message envelopes to their respective
        engines.

        :param msg: The message envelope containing raw telemetry.
        :type msg: Message
        :return: None
        :rtype: None
        """
        if getattr(msg, "verb", "") == "RQ" or not isinstance(
            msg.payload, (dict, list)
        ):
            return

        # 2411 parameter messages are owned by the FAN aggregate root: they
        # set _supports_2411 and fire the initialized callback that ramses_cc
        # uses to create the ~15 parameter number entities.  Phase 2.95 moved
        # this out of HvacVentilator._handle_msg; it must be routed here for
        # the StateProjector path to keep parity with the dispatcher path.
        # See ramses_cc issue 851.
        if msg.code == Code._2411:
            self._route_2411_to_fan(msg)

        payloads = msg.payload if isinstance(msg.payload, list) else [msg.payload]

        # Unfold dict-of-dicts arrays (e.g. {'00': {'temp_low': 10}})
        unfolded_payloads: list[dict[str, Any]] = []
        for p in payloads:
            if not isinstance(p, dict):
                continue

            if (
                SZ_UFH_IDX not in p
                and SZ_ZONE_IDX not in p
                and "ufx_idx" not in p
                and SZ_DOMAIN_ID not in p
                and all(isinstance(v, dict) for v in p.values())
            ):
                for k, v in p.items():
                    if isinstance(v, dict):
                        # Inject the outer index key so it isn't lost during unfold
                        v_copy = dict(v)
                        v_copy[SZ_UFH_IDX] = k
                        unfolded_payloads.append(v_copy)
            else:
                unfolded_payloads.append(p)

        registry = getattr(self._gwy, "device_registry", None)
        if not registry:
            return

        systems = getattr(registry, "systems", [])
        system_by_id = {s.id: s for s in systems}

        for p in unfolded_payloads:
            # Hexagonal Boundary Enforcement: Route telemetry to Source
            src_dev = registry.device_by_id.get(msg.src.id)
            if src_dev:
                try:
                    self._update_opentherm_state(src_dev, p, msg)
                    self._update_hvac_state(src_dev, p, msg)
                    self._update_power_state(src_dev, p, msg)
                    self._update_dhw_state(src_dev, p, msg)
                    self._update_system_state(src_dev, p, msg)
                    self._update_temperature_state(src_dev, p, msg)
                    self._update_demand_state(src_dev, p, msg)
                    self._update_ufh_state(src_dev, p, msg)
                    self._update_actuator_state(src_dev, p, msg)
                except Exception as err:
                    _LOGGER.error(
                        "CQRS extraction failed for src %s: %s",
                        src_dev.id,
                        err,
                    )

            # Route to Destination Device (Aggregation)
            if msg.dst.id != "--:------" and msg.dst.id != msg.src.id:
                dst_dev = registry.device_by_id.get(msg.dst.id)
                if dst_dev:
                    try:
                        self._update_opentherm_state(dst_dev, p, msg)
                        self._update_hvac_state(dst_dev, p, msg)
                        self._update_power_state(dst_dev, p, msg)
                        self._update_dhw_state(dst_dev, p, msg)
                        self._update_system_state(dst_dev, p, msg)
                        self._update_temperature_state(dst_dev, p, msg)
                        self._update_demand_state(dst_dev, p, msg)
                        self._update_ufh_state(dst_dev, p, msg)
                        self._update_actuator_state(dst_dev, p, msg)
                    except Exception as err:
                        _LOGGER.error(
                            "CQRS extraction failed for dst %s: %s",
                            dst_dev.id,
                            err,
                        )

            # Route CQRS state to Systems (TCS) and Zones
            if SZ_ZONE_IDX in p and msg.src.id in system_by_id:
                tcs = system_by_id[msg.src.id]
                zone = tcs.zone_by_idx.get(str(p[SZ_ZONE_IDX]))
                if zone:
                    try:
                        self._update_zone_state(zone, p, msg)
                    except Exception as err:
                        _LOGGER.error(
                            "CQRS extraction failed for zone %s: %s",
                            zone.id,
                            err,
                        )

            # Route domain-id opcodes (0008/0009/3150) to the DhwZone (F9/FA)
            # or TCS (FC).  The ingestion path above only routes to src_dev
            # and dst_dev, but the DhwZone/TCS are virtual twins that are
            # neither src nor dst.  Without this, demand_state on the DhwZone
            # is never hydrated (relay_demand, relay_failsafe, heat_demand).
            # See: https://github.com/ramses-rf/ramses_cc/issues/843
            if SZ_DOMAIN_ID in p and src_dev and msg.src.id in system_by_id:
                tcs = system_by_id[msg.src.id]
                domain_id = p[SZ_DOMAIN_ID]
                if domain_id == "FC" and tcs is not None:
                    try:
                        self._update_demand_state(tcs, p, msg)
                    except Exception as err:
                        _LOGGER.error(
                            "CQRS extraction failed for TCS %s: %s",
                            tcs.id,
                            err,
                        )
                elif (
                    domain_id in ("FA", "F9") and getattr(tcs, "dhw", None) is not None
                ):
                    try:
                        self._update_demand_state(tcs.dhw, p, msg)
                    except Exception as err:
                        _LOGGER.error(
                            "CQRS extraction failed for DHW %s: %s",
                            tcs.dhw.id,
                            err,
                        )

            # Route DHW opcodes (1260/10A0/1F41) to the DhwZone.
            # These payloads carry no zone_idx/domain_id, so the block above
            # misses the DhwZone.  1260 is sent by the DhwSensor (or relayed
            # by the Controller as an RP); 10A0/1F41 are sent by the
            # Controller.  All of these expose a ``tcs`` attribute whose
            # ``dhw`` is the DhwZone.  The appliance_control (OTB) also emits
            # 10A0/1260 with different semantics (CH setpoint / null temp), so
            # it must be excluded to avoid clobbering the DHW read-models.
            # Without this, the CQRS read-models (temp_state/dhw_state) on the
            # DhwZone are never hydrated and the ramses_cc water_heater entity
            # shows no current/target temperature.
            # See: https://github.com/ramses-rf/ramses_cc/issues/843
            if msg.code in (Code._1260, Code._10A0, Code._1F41) and src_dev:
                src_slug = getattr(src_dev, "_SLUG", "")
                if msg.code == Code._1260:
                    is_dhw_src = src_slug in ("DHW", "CTL")
                else:  # 10A0 / 1F41 are owned by the Controller
                    is_dhw_src = src_slug == "CTL"
                if is_dhw_src:
                    tcs = getattr(src_dev, "tcs", None)
                    dhw = getattr(tcs, "dhw", None) if tcs is not None else None
                    if dhw is not None:
                        try:
                            self._update_dhw_state(dhw, p, msg)
                            self._update_temperature_state(dhw, p, msg)
                        except Exception as err:
                            _LOGGER.error(
                                "CQRS extraction failed for DHW %s: %s",
                                dhw.id,
                                err,
                            )

        # --- CQRS Reactor Hooks ---
        # Automate the legacy Actuator discovery query (3EF1) in response to 3EF0 (I)
        if msg.code == Code._3EF0 and getattr(msg, "verb", "") == " I":
            src_dev = registry.device_by_id.get(msg.src.id)
            if src_dev and not getattr(src_dev, "is_faked", False):
                from ramses_tx import Command, Priority
                from ramses_tx.const import RQ
                from ramses_tx.typing import PayloadT

                try:
                    cmd = Command.from_attrs(RQ, msg.src.id, Code._3EF1, PayloadT("00"))
                    self._gwy.send_cmd(cmd, priority=Priority.LOW)
                except Exception as err:
                    _LOGGER.error(
                        "Failed to trigger CQRS 3EF1 reactor for %s: %s",
                        msg.src.id,
                        err,
                    )

        # Automate the legacy DHW Sensor discovery query (1260) to keep CTL in sync
        if msg.code == Code._1260:
            src_dev = registry.device_by_id.get(msg.src.id)
            if src_dev and getattr(src_dev, "ctl", None):
                try:
                    dto = build_dto(
                        Intent_(
                            src=HGI_DEV_ADDR,
                            dst=Address(src_dev.ctl.id),
                            action=Action.GET_DHW_TEMP,
                            data={"dhw_idx": 0},
                        )
                    )
                    self._gwy.send_cmd(dto)
                except Exception as err:
                    _LOGGER.error(
                        "Failed to trigger CQRS 1260 reactor for %s: %s",
                        src_dev.ctl.id,
                        err,
                    )

    def _update_opentherm_state(
        self, target: Any, p: dict[str, Any], msg: Message
    ) -> None:
        """Translate OpenTherm frames or parallel opcodes into
        OpenThermState.
        """
        current_state = getattr(target, "opentherm_state", None)
        if current_state is None:
            if getattr(target, "_SLUG", "") == "OTB":
                current_state = OpenThermState()
            else:
                return

        upd_base: dict[str, Any] = {}
        upd_flag: dict[str, Any] = {}
        upd_temp: dict[str, Any] = {}
        upd_count: dict[str, Any] = {}

        if msg.code == Code._3220:
            raw_id = p.get("msg_id")
            val = p.get("value")

            if raw_id is None:
                return

            try:
                msg_id = OtDataId(raw_id)
            except ValueError:
                return

            if (
                msg_id == OtDataId.STATUS
                and isinstance(val, (list, tuple))
                and len(val) >= 13
            ):
                upd_flag.update(
                    {
                        "ch_enabled": bool(val[0]),
                        "dhw_enabled": bool(val[1]),
                        "cooling_enabled": bool(val[2]),
                        "otc_active": bool(val[3]),
                        "summer_mode": bool(val[5]),
                        "dhw_blocking": bool(val[6]),
                        "fault_present": bool(val[8]),
                        "ch_active": bool(val[9]),
                        "dhw_active": bool(val[10]),
                        "flame_active": bool(val[11]),
                        "cooling_active": bool(val[12]),
                    }
                )
            elif val is not None and msg_id in OPENTHERM_FIELD_MAP:
                category, field_key = OPENTHERM_FIELD_MAP[msg_id]
                if category == "base":
                    upd_base[field_key] = val
                elif category == "temperatures":
                    upd_temp[field_key] = val
                elif category == "counters":
                    upd_count[field_key] = val
                elif category == "flags":
                    upd_flag[field_key] = val
        else:
            if msg.code in RAMSES_HEATING_MAP:
                data = RAMSES_HEATING_MAP[msg.code]
                payload_key, category, state_field = data
                if payload_key in p:
                    if category == "base":
                        upd_base[state_field] = p[payload_key]
                    elif category == "temperatures":
                        upd_temp[state_field] = p[payload_key]
            elif msg.code in (Code._3EF0, Code._3EF1):
                if SZ_REL_MODULATION_LEVEL in p:
                    upd_base["rel_modulation_level"] = p[SZ_REL_MODULATION_LEVEL]
                if SZ_MAX_REL_MODULATION in p:
                    upd_base["max_rel_modulation"] = p[SZ_MAX_REL_MODULATION]
                if SZ_CH_SETPOINT in p:
                    upd_temp["ch_setpoint"] = p[SZ_CH_SETPOINT]
                if SZ_CH_ACTIVE in p:
                    upd_flag["ch_active"] = p[SZ_CH_ACTIVE]
                if SZ_CH_ENABLED in p:
                    upd_flag["ch_enabled"] = p[SZ_CH_ENABLED]
                if SZ_DHW_ACTIVE in p:
                    upd_flag["dhw_active"] = p[SZ_DHW_ACTIVE]
                if SZ_FLAME_ON in p:
                    # NOTE: semantic parser maps this specifically
                    upd_flag["flame_active"] = p[SZ_FLAME_ON]

        if not any((upd_base, upd_flag, upd_temp, upd_count)):
            return

        dtm = getattr(msg, "dtm", getattr(msg, "timestamp", None))
        if dtm:
            upd_base["last_updated"] = dtm

        new_flags = current_state.flags
        if upd_flag:
            new_flags = dataclasses.replace(new_flags, **upd_flag)

        new_temps = current_state.temperatures
        if upd_temp:
            new_temps = dataclasses.replace(new_temps, **upd_temp)

        new_counters = current_state.counters
        if upd_count:
            new_counters = dataclasses.replace(new_counters, **upd_count)

        new_state = dataclasses.replace(
            current_state,
            flags=new_flags,
            temperatures=new_temps,
            counters=new_counters,
            **upd_base,
        )
        target.opentherm_state = new_state

        event = StateUpdatedEvent(
            entity_id=getattr(target, "id", "unknown"),
            state=new_state,
            correlation_id=getattr(msg, "correlation_id", uuid.uuid4()),
            causation_id=getattr(msg, "message_id", uuid.uuid4()),
        )
        if hasattr(target, "apply_state_update"):
            target.apply_state_update(event)

    def _update_hvac_state(self, target: Any, p: dict[str, Any], msg: Message) -> None:
        """Translate complex multi-opcode ventilation payloads into
        HvacState.

        Applies hardware-specific stateful FSM rules (via the Quirks
        middleware) prior to hydration.
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

        current_state = getattr(target, "hvac_state", None) or HvacState()
        p = quirks.apply_hvac_quirks(p, current_state, msg.code)

        updates: dict[str, Any] = {}

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
            SZ_DEWPOINT_TEMP,
        ]

        # Filter out null-marker values that 31DA/31D9 snapshots emit for
        # sensors the device does not have.  Without this, every polling cycle
        # (~10 min) overwrites good telemetry from 22F1/12A0/22F7 with null
        # markers, causing sensors to bounce to None/FF/0.  See issue #742.
        # This must mirror the filtering in dispatcher._update_hvac_state.
        _NULL_HUMIDITY_FIELDS = frozenset({SZ_INDOOR_HUMIDITY, SZ_OUTDOOR_HUMIDITY})

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
        if SZ_REMAINING_DAYS in p:
            updates["filter_remaining_days"] = p[SZ_REMAINING_DAYS]
        if SZ_REMAINING_PERCENT in p:
            updates["filter_remaining_percent"] = p[SZ_REMAINING_PERCENT]
        if SZ_MINUTES in p and msg.code == Code._22F3:
            updates["boost_timer_mins"] = p[SZ_MINUTES]
        if SZ_REQ_SPEED in p:
            updates["request_fan_speed"] = p[SZ_REQ_SPEED]
        if SZ_REQ_REASON in p:
            updates["request_reason"] = p[SZ_REQ_REASON]

        if not updates:
            return

        dtm = getattr(msg, "dtm", getattr(msg, "timestamp", None))
        if dtm:
            updates["last_updated"] = dtm

        new_state = dataclasses.replace(current_state, **updates)
        target.hvac_state = new_state

        event = StateUpdatedEvent(
            entity_id=getattr(target, "id", "unknown"),
            state=new_state,
            correlation_id=getattr(msg, "correlation_id", uuid.uuid4()),
            causation_id=getattr(msg, "message_id", uuid.uuid4()),
        )
        if hasattr(target, "apply_state_update"):
            target.apply_state_update(event)

    def _update_power_state(self, target: Any, p: dict[str, Any], msg: Message) -> None:
        """Translate battery opcodes into PowerState."""
        updates: dict[str, Any] = {}
        if msg.code == Code._1060:
            if SZ_BATTERY_LOW in p:
                updates[SZ_BATTERY_LOW] = p[SZ_BATTERY_LOW]
            if SZ_BATTERY_LEVEL in p:
                updates[SZ_BATTERY_LEVEL] = p[SZ_BATTERY_LEVEL]

        if not updates:
            return

        dtm = getattr(msg, "dtm", getattr(msg, "timestamp", None))
        if dtm:
            updates["last_updated"] = dtm

        current_state = getattr(target, "power_state", None) or PowerState()
        new_state = dataclasses.replace(current_state, **updates)
        target.power_state = new_state

        event = StateUpdatedEvent(
            entity_id=getattr(target, "id", "unknown"),
            state=new_state,
            correlation_id=getattr(msg, "correlation_id", uuid.uuid4()),
            causation_id=getattr(msg, "message_id", uuid.uuid4()),
        )
        if hasattr(target, "apply_state_update"):
            target.apply_state_update(event)

    def _update_dhw_state(self, target: Any, p: dict[str, Any], msg: Message) -> None:
        """Translate DHW opcodes into DhwState."""
        if msg.code not in (Code._10A0, Code._1260, Code._1F41):
            return

        updates: dict[str, Any] = {}
        if msg.code == Code._10A0:
            if SZ_SETPOINT in p:
                updates[SZ_SETPOINT] = p[SZ_SETPOINT]
            if SZ_OVERRUN in p:
                updates[SZ_OVERRUN] = p[SZ_OVERRUN]
            if SZ_DIFFERENTIAL in p:
                updates[SZ_DIFFERENTIAL] = p[SZ_DIFFERENTIAL]
        elif msg.code == Code._1260:
            if SZ_TEMPERATURE in p:
                updates[SZ_TEMPERATURE] = p[SZ_TEMPERATURE]
        elif msg.code == Code._1F41:
            if SZ_MODE in p:
                updates[SZ_MODE] = p[SZ_MODE]
            if SZ_ACTIVE in p:
                updates[SZ_ACTIVE] = p[SZ_ACTIVE]
            if SZ_UNTIL in p:
                updates[SZ_UNTIL] = p[SZ_UNTIL]

        if not updates:
            return

        dtm = getattr(msg, "dtm", getattr(msg, "timestamp", None))
        if dtm:
            updates["last_updated"] = dtm

        current_state = getattr(target, "dhw_state", None) or DhwState()
        new_state = dataclasses.replace(current_state, **updates)
        target.dhw_state = new_state

        event = StateUpdatedEvent(
            entity_id=getattr(target, "id", "unknown"),
            state=new_state,
            correlation_id=getattr(msg, "correlation_id", uuid.uuid4()),
            causation_id=getattr(msg, "message_id", uuid.uuid4()),
        )
        if hasattr(target, "apply_state_update"):
            target.apply_state_update(event)

    def _update_system_state(
        self, target: Any, p: dict[str, Any], msg: Message
    ) -> None:
        """Translate system configuration opcodes into SystemState."""
        if msg.code not in (Code._0100, Code._2E04, Code._313F):
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

        if not updates:
            return

        dtm = getattr(msg, "dtm", getattr(msg, "timestamp", None))
        if dtm:
            updates["last_updated"] = dtm

        current_state = getattr(target, "system_state", None) or SystemState()
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

    def _update_temperature_state(
        self, target: Any, p: dict[str, Any], msg: Message
    ) -> None:
        """Translate temperature/TRV opcodes into TrvState &
        TemperatureState.
        """
        dtm = getattr(msg, "dtm", getattr(msg, "timestamp", None))

        if msg.code == Code._12B0 and SZ_WINDOW_OPEN in p:
            current_trv = getattr(target, "trv_state", None) or TrvState()
            trv_updates = {"window_open": p[SZ_WINDOW_OPEN]}
            if dtm:
                trv_updates["last_updated"] = dtm

            new_trv = dataclasses.replace(current_trv, **trv_updates)
            target.trv_state = new_trv

            event = StateUpdatedEvent(
                entity_id=getattr(target, "id", "unknown"),
                state=new_trv,
                correlation_id=getattr(msg, "correlation_id", uuid.uuid4()),
                causation_id=getattr(msg, "message_id", uuid.uuid4()),
            )
            if hasattr(target, "apply_state_update"):
                target.apply_state_update(event)

        if msg.code in (Code._30C9, Code._1260, Code._0002, Code._12C0):
            updates: dict[str, Any] = {}
            if SZ_TEMPERATURE in p:
                # Legacy Parity: Physical sensors only track their own local sensor readings.
                # We must ignore Zone temperature syncs sent TO them by the Controller.
                # Keep same as src/ramses_rf/dispatcher.py#_update_temperature_state
                target_id = getattr(target, "id", str(target))
                src_id = getattr(msg.src, "id", str(msg.src))

                if (
                    getattr(target, "_SLUG", "") in ("TRV", "THM")
                    and src_id != target_id
                ):
                    pass
                else:
                    updates[SZ_TEMPERATURE] = p[SZ_TEMPERATURE]

            if SZ_SETPOINT in p:
                updates[SZ_SETPOINT] = p[SZ_SETPOINT]

            if updates:
                if dtm:
                    updates["last_updated"] = dtm

                current_temp = getattr(target, "temp_state", None) or TemperatureState()
                new_temp = dataclasses.replace(current_temp, **updates)
                target.temp_state = new_temp

                event = StateUpdatedEvent(
                    entity_id=getattr(target, "id", "unknown"),
                    state=new_temp,
                    correlation_id=getattr(msg, "correlation_id", uuid.uuid4()),
                    causation_id=getattr(msg, "message_id", uuid.uuid4()),
                )
                if hasattr(target, "apply_state_update"):
                    target.apply_state_update(event)

    def _update_demand_state(
        self, target: Any, p: dict[str, Any], msg: Message
    ) -> None:
        """Translate demand opcodes into DemandState."""
        if msg.code not in (Code._3150, Code._0008, Code._0009):
            return

        updates: dict[str, Any] = {}
        slug = getattr(target, "_SLUG", "")

        if msg.code == Code._3150 and SZ_HEAT_DEMAND in p:
            # Prevent flattened array payloads (e.g., UFH circuit demands)
            # from overwriting the controller's aggregate FC heat demand.
            if slug in ("CTL", "UFC"):
                if p.get(SZ_DOMAIN_ID) == "FC":
                    updates[SZ_HEAT_DEMAND] = p[SZ_HEAT_DEMAND]
            elif SZ_UFH_IDX not in p and "ufx_idx" not in p:
                updates[SZ_HEAT_DEMAND] = p[SZ_HEAT_DEMAND]

        elif msg.code == Code._0008 and SZ_RELAY_DEMAND in p:
            # Prevent FA (UFH) relay demands from overwriting FC relay demand
            if slug == "UFC" and p.get(SZ_DOMAIN_ID) != "FC":
                pass
            else:
                updates[SZ_RELAY_DEMAND] = p[SZ_RELAY_DEMAND]

        elif msg.code == Code._0009 and "failsafe_enabled" in p:
            updates[SZ_RELAY_FAILSAFE] = p["failsafe_enabled"]

        if not updates:
            return

        dtm = getattr(msg, "dtm", getattr(msg, "timestamp", None))
        if dtm:
            updates["last_updated"] = dtm

        current_state = getattr(target, "demand_state", None) or DemandState()
        new_state = dataclasses.replace(current_state, **updates)
        target.demand_state = new_state

        event = StateUpdatedEvent(
            entity_id=getattr(target, "id", "unknown"),
            state=new_state,
            correlation_id=getattr(msg, "correlation_id", uuid.uuid4()),
            causation_id=getattr(msg, "message_id", uuid.uuid4()),
        )
        if hasattr(target, "apply_state_update"):
            target.apply_state_update(event)

    def _update_ufh_state(self, target: Any, p: dict[str, Any], msg: Message) -> None:
        """Translate UFH circuit arrays and bounds into UfhState."""
        if msg.code not in (Code._3150, Code._0008, Code._22C9):
            return

        if getattr(target, "_SLUG", "") != "UFC":
            return

        current_state = getattr(target, "ufh_state", None) or UfhState()
        updates: dict[str, Any] = {}

        # Safely extract index matching legacy typo "ufx_idx"
        idx = p.get("ufx_idx") or p.get(SZ_UFH_IDX) or p.get(SZ_ZONE_IDX)

        if msg.code == Code._3150 and idx is not None and SZ_HEAT_DEMAND in p:
            new_demands = dict(current_state.heat_demands)
            new_demands[str(idx)] = p[SZ_HEAT_DEMAND]
            updates["heat_demands"] = new_demands
        elif (
            msg.code == Code._0008
            and p.get(SZ_DOMAIN_ID) == "FA"
            and SZ_RELAY_DEMAND in p
        ):
            updates["relay_demand_fa"] = p[SZ_RELAY_DEMAND]
        elif msg.code == Code._22C9 and idx is not None:
            new_sp = dict(current_state.setpoints)
            sp_data = dict(new_sp.get(str(idx), {}))

            # Legacy parsers return an empty dict if no bounds exist.
            # Only populate the bounds if they are explicitly present.
            bounds = p.get(SZ_SETPOINT_BOUNDS)
            if isinstance(bounds, tuple) and len(bounds) == 2:
                sp_data["temp_low"] = bounds[0]
                sp_data["temp_high"] = bounds[1]

            new_sp[str(idx)] = sp_data
            updates["setpoints"] = new_sp

        if not updates:
            return

        dtm = getattr(msg, "dtm", getattr(msg, "timestamp", None))
        if dtm:
            updates["last_updated"] = dtm

        new_state = dataclasses.replace(current_state, **updates)
        target.ufh_state = new_state

        event = StateUpdatedEvent(
            entity_id=getattr(target, "id", "unknown"),
            state=new_state,
            correlation_id=getattr(msg, "correlation_id", uuid.uuid4()),
            causation_id=getattr(msg, "message_id", uuid.uuid4()),
        )
        if hasattr(target, "apply_state_update"):
            target.apply_state_update(event)

    def _update_actuator_state(
        self, target: Any, p: dict[str, Any], msg: Message
    ) -> None:
        """Translate actuator state opcodes into ActuatorState."""
        if msg.code not in (Code._3EF0, Code._3EF1):
            return

        updates: dict[str, Any] = {}
        if SZ_MODULATION_LEVEL in p:
            # NOTE: semantic parser custom keys
            updates[SZ_MODULATION_LEVEL] = p[SZ_MODULATION_LEVEL]
        elif SZ_REL_MODULATION_LEVEL in p:
            updates[SZ_MODULATION_LEVEL] = p[SZ_REL_MODULATION_LEVEL]

        if SZ_ACTUATOR_ENABLED in p:
            updates[SZ_ACTUATOR_ENABLED] = p[SZ_ACTUATOR_ENABLED]
        if SZ_CH_ACTIVE in p:
            updates[SZ_CH_ACTIVE] = p[SZ_CH_ACTIVE]
        if SZ_CH_ENABLED in p:
            updates[SZ_CH_ENABLED] = p[SZ_CH_ENABLED]
        if SZ_DHW_ACTIVE in p:
            updates[SZ_DHW_ACTIVE] = p[SZ_DHW_ACTIVE]
        if SZ_FLAME_ON in p:
            # NOTE: semantic parser maps this specifically
            updates["flame_active"] = p[SZ_FLAME_ON]
            updates[SZ_FLAME_ON] = p[SZ_FLAME_ON]

        # Legacy diagnostic payloads restored for backwards compatibility
        if SZ_CH_SETPOINT in p:
            updates[SZ_CH_SETPOINT] = p[SZ_CH_SETPOINT]
        if SZ_MAX_REL_MODULATION in p:
            updates[SZ_MAX_REL_MODULATION] = p[SZ_MAX_REL_MODULATION]
        if SZ_COOL_ACTIVE in p:
            updates[SZ_COOL_ACTIVE] = p[SZ_COOL_ACTIVE]
        if SZ_ACTUATOR_COUNTDOWN in p:
            updates[SZ_ACTUATOR_COUNTDOWN] = p[SZ_ACTUATOR_COUNTDOWN]
        if SZ_CYCLE_COUNTDOWN in p:
            updates[SZ_CYCLE_COUNTDOWN] = p[SZ_CYCLE_COUNTDOWN]

        if not updates:
            return

        dtm = getattr(msg, "dtm", getattr(msg, "timestamp", None))
        if dtm:
            updates["last_updated"] = dtm

        current_state = getattr(target, "act_state", None) or ActuatorState()
        new_state = dataclasses.replace(current_state, **updates)
        target.act_state = new_state

        event = StateUpdatedEvent(
            entity_id=getattr(target, "id", "unknown"),
            state=new_state,
            correlation_id=getattr(msg, "correlation_id", uuid.uuid4()),
            causation_id=getattr(msg, "message_id", uuid.uuid4()),
        )
        if hasattr(target, "apply_state_update"):
            target.apply_state_update(event)

    def _update_zone_state(self, target: Any, p: dict[str, Any], msg: Message) -> None:
        """Translate zone configuration opcodes into ZoneState."""
        if msg.code != Code._0004:
            return

        updates: dict[str, Any] = {}
        if SZ_NAME in p:
            updates[SZ_NAME] = str(p[SZ_NAME])

        if not updates:
            return

        dtm = getattr(msg, "dtm", getattr(msg, "timestamp", None))
        if dtm:
            updates["last_updated"] = dtm

        current_state = getattr(target, "zone_state", None) or ZoneState()
        new_state = dataclasses.replace(current_state, **updates)
        target.zone_state = new_state

        event = StateUpdatedEvent(
            entity_id=getattr(target, "id", "unknown"),
            state=new_state,
            correlation_id=getattr(msg, "correlation_id", uuid.uuid4()),
            causation_id=getattr(msg, "message_id", uuid.uuid4()),
        )
        if hasattr(target, "apply_state_update"):
            target.apply_state_update(event)
