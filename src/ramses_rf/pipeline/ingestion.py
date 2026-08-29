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
    SZ_CO2_LEVEL_FAULT,
    SZ_COOL_ACTIVE,
    SZ_COOLING_DEMAND,
    SZ_COOLING_MODE,
    SZ_CYCLE_COUNTDOWN,
    SZ_DATETIME,
    SZ_DEWPOINT_TEMP,
    SZ_DHW_ACTIVE,
    SZ_DHW_FLOW_RATE,
    SZ_DHW_INDEX,
    SZ_DIFFERENTIAL,
    SZ_DOMAIN_INDEX,
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
    SZ_LOCAL_OVERRIDE,
    SZ_MAX_REL_MODULATION,
    SZ_MAX_TEMP,
    SZ_MIN_TEMP,
    SZ_MINUTES,
    SZ_MODE,
    SZ_MODULATION_LEVEL,
    SZ_MULTIROOM_MODE,
    SZ_NAME,
    SZ_OPENWINDOW_FUNCTION,
    SZ_OUTDOOR_HUMIDITY,
    SZ_OUTDOOR_TEMP,
    SZ_OVERRUN,
    SZ_POST_HEAT,
    SZ_PRE_HEAT,
    SZ_PRESENCE_DETECTED,
    SZ_PRESSURE,
    SZ_PUMP_RELAY_STATE,
    SZ_REL_MODULATION_LEVEL,
    SZ_RELAY_DEMAND,
    SZ_RELAY_FAILSAFE,
    SZ_REMAINING_DAYS,
    SZ_REMAINING_MINS,
    SZ_REMAINING_PERCENT,
    SZ_REQUEST_REASON,
    SZ_REQUEST_SPEED,
    SZ_SETPOINT,
    SZ_SETPOINT_BOUNDS,
    SZ_SPEED_CAPABILITIES,
    SZ_SUPPLY_FAN_SPEED,
    SZ_SUPPLY_FLOW,
    SZ_SUPPLY_TEMP,
    SZ_SYSTEM_MODE,
    SZ_TEMPERATURE,
    SZ_UFH_INDEX,
    SZ_UNTIL,
    SZ_WINDOW_OPEN,
    SZ_ZONE_INDEX,
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
from ramses_rf.state_projector import (
    _get_dhw_zone_from_msg,
    _route_2411_to_fan,
)
from ramses_tx.const import I_, RQ, Code

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
    """Projector task that transforms incoming telemetry into immutable states."""

    def __init__(
        self, gateway: Any, ssot_queue: asyncio.Queue[Message]
    ) -> None:
        """Initialize the state projector background worker.

        :param gateway: The active Gateway facade instance.
        :type gateway: Any
        :param ssot_queue: Single Source of Truth Queue from
            CentralDispatcher.
        :type ssot_queue: asyncio.Queue[Message]
        """
        self._gateway = gateway
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

        Delegates to the shared helper in ``dispatcher.py``.
        """
        _route_2411_to_fan(self._gateway, msg)

    def process_message_state(self, msg: Message) -> None:
        """Route valid inbound message envelopes to their respective engines.

        :param msg: The message envelope containing raw telemetry.
        :type msg: Message
        :return: None
        :rtype: None
        """
        if msg.verb == RQ or not isinstance(msg.payload, (dict, list)):
            return

        # 2411 parameter messages are owned by the FAN aggregate root: they
        # set _supports_2411 and fire the initialized callback that ramses_cc
        # uses to create the ~15 parameter number entities.  Phase 2.95 moved
        # this out of HvacVentilator._handle_msg; it must be routed here for
        # the StateProjector path to keep parity with the dispatcher path.
        # See ramses_cc issue 851.
        if msg.code == Code._2411:
            _route_2411_to_fan(self._gateway, msg)

        # Normalise payload to a list of dicts.  Array payloads (e.g. 000A,
        # 22C9) arrive as list[dict] after to_dict() conversion on the
        # base.py:Message.payload property; single payloads arrive as dict.
        # The legacy dict-of-dicts unfolding (for {'00': {...}, '01': {...}}
        # structures) has been removed — no PayloadBase.to_dict() method
        # produces nested dicts after the Phase 6 dataclass migration
        # (issue 1039).  All array payloads now arrive as list[dict] with
        # the index key (zone_index, ufh_index, etc.) embedded in each
        # element by the dataclass's to_dict().
        payloads: list[dict[str, Any]] = []
        raw_payloads = (
            msg.payload if isinstance(msg.payload, list) else [msg.payload]
        )
        for payload in raw_payloads:
            if isinstance(payload, dict):
                payloads.append(payload)

        registry = getattr(self._gateway, "device_registry", None)
        if not registry:
            return

        systems = getattr(registry, "systems", [])
        system_by_id = {s.id: s for s in systems}

        for payload in payloads:
            # Hexagonal Boundary Enforcement: Route telemetry to Source
            src_dev = registry.device_by_id.get(msg.src.id)
            if src_dev:
                try:
                    self._update_opentherm_state(src_dev, payload, msg)
                    self._update_hvac_state(src_dev, payload, msg)
                    self._update_power_state(src_dev, payload, msg)
                    self._update_dhw_state(src_dev, payload, msg)
                    self._update_system_state(src_dev, payload, msg)
                    self._update_temperature_state(src_dev, payload, msg)
                    self._update_demand_state(src_dev, payload, msg)
                    self._update_ufh_state(src_dev, payload, msg)
                    self._update_actuator_state(src_dev, payload, msg)
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
                        self._update_opentherm_state(dst_dev, payload, msg)
                        self._update_hvac_state(dst_dev, payload, msg)
                        self._update_power_state(dst_dev, payload, msg)
                        self._update_dhw_state(dst_dev, payload, msg)
                        self._update_system_state(dst_dev, payload, msg)
                        self._update_temperature_state(dst_dev, payload, msg)
                        self._update_demand_state(dst_dev, payload, msg)
                        self._update_ufh_state(dst_dev, payload, msg)
                        self._update_actuator_state(dst_dev, payload, msg)
                    except Exception as err:
                        _LOGGER.error(
                            "CQRS extraction failed for dst %s: %s",
                            dst_dev.id,
                            err,
                        )

            # Route CQRS state to Systems (TCS) and Zones
            zone_val = payload.get(SZ_ZONE_INDEX, payload.get("zone_index"))
            if zone_val is not None and msg.src.id in system_by_id:
                tcs = system_by_id[msg.src.id]
                zone = tcs.zone_by_index.get(str(zone_val))
                if zone:
                    try:
                        self._update_zone_state(zone, payload, msg)
                        # 2309/2349 also carry a setpoint that the Zone's
                        # `setpoint` property reads from temp_state.  Without
                        # this, the zone climate entity's target_temperature
                        # stays None (issue 843).
                        # 30C9 carries the zone temperature that the Zone's
                        # `temperature` property reads from temp_state.
                        # Without this, the zone climate entity's
                        # current_temperature stays None (issue 927).
                        if msg.code in (Code._2309, Code._2349, Code._30C9):
                            self._update_temperature_state(zone, payload, msg)
                    except Exception as err:
                        _LOGGER.error(
                            "CQRS extraction failed for zone %s: %s",
                            zone.id,
                            err,
                        )

            # Route 30C9 from a sensor to its parent zone.  Sensor-sourced
            # 30C9 packets have no zone_index in the decoded payload (the
            # sensor is not a controller, so _build_index_dict injects no
            # zone_index), so the zone routing path above is not reached.
            # Without this, the zone's current_temperature stays None when
            # only the sensor broadcasts 30C9 (issue 927).
            # Restricted to designated sensor or sole actuator (issue 976).
            if (
                msg.code == Code._30C9
                and src_dev
                and SZ_TEMPERATURE in payload
                and getattr(src_dev, "_parent", None) is not None
            ):
                parent = src_dev._parent
                if hasattr(parent, "temp_state") and hasattr(
                    parent, "zone_state"
                ):
                    parent_sensor = getattr(parent, "sensor", None)
                    parent_actuators = getattr(parent, "actuators", [])
                    if src_dev is parent_sensor or (
                        parent_sensor is None
                        and len(parent_actuators) == 1
                        and src_dev in parent_actuators
                    ):
                        try:
                            self._update_temperature_state(
                                parent, payload, msg
                            )
                        except Exception as err:
                            _LOGGER.error(
                                "CQRS extraction failed for parent zone %s: %s",
                                getattr(parent, "id", "unknown"),
                                err,
                            )

            # Route domain-id opcodes (0008/0009/3150) to the DhwZone (F9/FA)
            # or TCS (FC).  The ingestion path above only routes to src_dev
            # and dst_dev, but the DhwZone/TCS are virtual twins that are
            # neither src nor dst.  Without this, demand_state on the DhwZone
            # is never hydrated (relay_demand, relay_failsafe, heat_demand).
            # See: https://github.com/ramses-rf/ramses_cc/issues/843
            domain_val = payload.get(
                SZ_DOMAIN_INDEX,
                payload.get("domain_id", payload.get("domain_index")),
            )
            if (
                domain_val is not None
                and src_dev
                and msg.src.id in system_by_id
            ):
                tcs = system_by_id[msg.src.id]
                if domain_val == "FC" and tcs is not None:
                    try:
                        self._update_demand_state(tcs, payload, msg)
                    except Exception as err:
                        _LOGGER.error(
                            "CQRS extraction failed for TCS %s: %s",
                            tcs.id,
                            err,
                        )
                elif (
                    domain_val in ("FA", "F9")
                    and getattr(tcs, "dhw", None) is not None
                ):
                    try:
                        self._update_demand_state(tcs.dhw, payload, msg)
                    except Exception as err:
                        _LOGGER.error(
                            "CQRS extraction failed for DHW %s: %s",
                            tcs.dhw.id,
                            err,
                        )

            # Route DHW opcodes (1260/10A0/1F41) to the DhwZone.
            # These payloads carry no zone_index/domain_id, so the block above
            # misses the DhwZone.  The shared helper in dispatcher.py
            # encapsulates the routing logic (src_slug check, OTB exclusion).
            # See: https://github.com/ramses-rf/ramses_cc/issues/843
            dhw = _get_dhw_zone_from_msg(msg, src_dev)
            if dhw is not None:
                try:
                    self._update_dhw_state(dhw, payload, msg)
                    self._update_temperature_state(dhw, payload, msg)
                except Exception as err:
                    _LOGGER.error(
                        "CQRS extraction failed for DHW %s: %s",
                        dhw.id,
                        err,
                    )

            # Route 22D9 and 3EF0 broadcasts from controller to
            # appliance_control.  Controller broadcasts to --:------ are
            # routed to src_dev (01:), which drops them because _SLUG !=
            # "OTB".  When the TCS has an appliance_control (e.g.
            # OtbGateway), route 22D9 (setpoint) and 3EF0 (modulation)
            # to it.  See: https://github.com/ramses-rf/ramses_cc/issues/975
            if (
                msg.code in (Code._22D9, Code._3EF0)
                and msg.src.id in system_by_id
            ):
                tcs = system_by_id[msg.src.id]
                appliance_control = getattr(tcs, "appliance_control", None)
                if appliance_control is not None:
                    try:
                        self._update_opentherm_state(
                            appliance_control, payload, msg
                        )
                        self._update_actuator_state(
                            appliance_control, payload, msg
                        )
                    except Exception as err:
                        _LOGGER.error(
                            "CQRS extraction failed for appliance_control %s: %s",
                            getattr(appliance_control, "id", "unknown"),
                            err,
                        )

            # Route system-level opcodes (0100/2E04/313F/2D49) to the TCS.
            # The ingestion loop routes to src_dev (Controller/UFC device),
            # but the TCS is the system read-model entity whose system_state
            # / thermal_mode is queried by downstream integrations.
            # See: https://github.com/ramses-rf/ramses_cc/issues/965
            if msg.code in (
                Code._0100,
                Code._2E04,
                Code._313F,
                Code._2D49,
            ):
                tcs_target = system_by_id.get(msg.src.id) or getattr(
                    src_dev, "tcs", None
                )
                if tcs_target is None and len(systems) == 1:
                    tcs_target = systems[0]
                if tcs_target is not None:
                    try:
                        self._update_system_state(tcs_target, payload, msg)
                    except Exception as err:
                        _LOGGER.error(
                            "CQRS extraction failed for TCS %s: %s",
                            tcs_target.id,
                            err,
                        )

        # --- CQRS Reactor Hooks ---
        # Automate the legacy Actuator discovery query (3EF1) in response to 3EF0 (I)
        if msg.code == Code._3EF0 and msg.verb == I_:
            src_dev = registry.device_by_id.get(msg.src.id)
            if src_dev and not getattr(src_dev, "is_faked", False):
                from ramses_rf.devices.helpers import build_rq_cmd
                from ramses_tx import Priority

                try:
                    command = build_rq_cmd(msg.src.id, Code._3EF1, "00")
                    self._gateway.send_cmd(command, priority=Priority.LOW)
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
                            data={SZ_DHW_INDEX: 0},
                        )
                    )
                    self._gateway.send_cmd(dto)
                except Exception as err:
                    _LOGGER.error(
                        "Failed to trigger CQRS 1260 reactor for %s: %s",
                        src_dev.ctl.id,
                        err,
                    )

    def _update_opentherm_state(
        self, target: Any, p: dict[str, Any], msg: Message
    ) -> None:
        """Translate OpenTherm frames or parallel opcodes into OpenThermState."""
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
            value = p.get("value")

            if raw_id is None:
                return

            try:
                msg_id = OtDataId(raw_id)
            except ValueError:
                return

            if (
                msg_id == OtDataId.STATUS
                and isinstance(value, (list, tuple))
                and len(value) >= 13
            ):
                upd_flag.update(
                    {
                        "ch_enabled": bool(value[0]),
                        "dhw_enabled": bool(value[1]),
                        "cooling_enabled": bool(value[2]),
                        "otc_active": bool(value[3]),
                        "summer_mode": bool(value[5]),
                        "dhw_blocking": bool(value[6]),
                        "fault_present": bool(value[8]),
                        "ch_active": bool(value[9]),
                        "dhw_active": bool(value[10]),
                        "flame_active": bool(value[11]),
                        "cooling_active": bool(value[12]),
                    }
                )
            elif value is not None and msg_id in OPENTHERM_FIELD_MAP:
                category, field_key = OPENTHERM_FIELD_MAP[msg_id]
                if category == "base":
                    upd_base[field_key] = value
                elif category == "temperatures":
                    upd_temp[field_key] = value
                elif category == "counters":
                    upd_count[field_key] = value
                elif category == "flags":
                    upd_flag[field_key] = value
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
                if SZ_MODULATION_LEVEL in p:
                    upd_base["rel_modulation_level"] = p[SZ_MODULATION_LEVEL]
                elif SZ_REL_MODULATION_LEVEL in p:
                    upd_base["rel_modulation_level"] = p[
                        SZ_REL_MODULATION_LEVEL
                    ]
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

    def _update_hvac_state(
        self, target: Any, p: dict[str, Any], msg: Message
    ) -> None:
        """Translate complex multi-opcode ventilation payloads into HvacState.

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
            SZ_DEWPOINT_TEMP,
        ]

        # Filter out null-marker values that 31DA/31D9 snapshots emit for
        # sensors the device does not have.  Without this, every polling cycle
        # (~10 min) overwrites good telemetry from 22F1/12A0/22F7 with null
        # markers, causing sensors to bounce to None/FF/0.  See issue #742.
        # This must mirror the filtering in dispatcher._update_hvac_state.
        _NULL_HUMIDITY_FIELDS = frozenset(
            {SZ_INDOOR_HUMIDITY, SZ_OUTDOOR_HUMIDITY}
        )

        for field_name in fields:
            if field_name not in p:
                continue
            field_val = p[field_name]
            # None = "not implemented" (e.g. EF in bypass_position)
            if field_val is None:
                continue
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

        # Handle non-standard names passed by the semantic parsers
        if SZ_REMAINING_DAYS in p:
            updates["filter_remaining_days"] = p[SZ_REMAINING_DAYS]
        if SZ_REMAINING_PERCENT in p:
            updates["filter_remaining_percent"] = p[SZ_REMAINING_PERCENT]
        if SZ_MINUTES in p and msg.code == Code._22F3:
            updates["boost_timer_mins"] = p[SZ_MINUTES]
        req_speed = p.get(SZ_REQUEST_SPEED, p.get("req_speed"))
        if req_speed is not None:
            updates["request_fan_speed"] = req_speed
        req_reason = p.get(SZ_REQUEST_REASON, p.get("req_reason"))
        if req_reason is not None:
            updates["request_reason"] = req_reason

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

    def _update_power_state(
        self, target: Any, p: dict[str, Any], msg: Message
    ) -> None:
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

    def _update_dhw_state(
        self, target: Any, p: dict[str, Any], msg: Message
    ) -> None:
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
        if msg.code not in (Code._0100, Code._2E04, Code._313F, Code._2D49):
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
        """Translate temperature/TRV opcodes into TrvState & TemperatureState."""
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

        if msg.code in (
            Code._30C9,
            Code._1260,
            Code._0002,
            Code._12C0,
            Code._2309,
            Code._2349,
        ):
            updates: dict[str, Any] = {}
            if SZ_TEMPERATURE in p:
                # Legacy Parity: Physical sensors only track their own local sensor readings.
                # We must ignore Zone temperature syncs sent TO them by the Controller.
                # Keep same as src/ramses_rf/dispatcher.py#_update_temperature_state
                target_id = getattr(target, "id", str(target))
                src_id = msg.src.id

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

                current_temp = (
                    getattr(target, "temp_state", None) or TemperatureState()
                )
                new_temp = dataclasses.replace(current_temp, **updates)
                target.temp_state = new_temp

                event = StateUpdatedEvent(
                    entity_id=getattr(target, "id", "unknown"),
                    state=new_temp,
                    correlation_id=getattr(
                        msg, "correlation_id", uuid.uuid4()
                    ),
                    causation_id=getattr(msg, "message_id", uuid.uuid4()),
                )
                if hasattr(target, "apply_state_update"):
                    target.apply_state_update(event)

    def _update_demand_state(
        self, target: Any, p: dict[str, Any], msg: Message
    ) -> None:
        """Translate demand opcodes into DemandState."""
        if msg.code not in (Code._3150, Code._0008, Code._0009, Code._1100):
            return

        updates: dict[str, Any] = {}
        slug = getattr(target, "_SLUG", "")

        if msg.code == Code._3150 and SZ_HEAT_DEMAND in p:
            # Prevent flattened array payloads (e.g., UFH circuit demands)
            # from overwriting the controller's aggregate FC heat demand.
            if slug in ("CTL", "UFC"):
                if (
                    p.get(SZ_DOMAIN_INDEX)
                    or p.get("domain_id")
                    or p.get("domain_index")
                ) == "FC":
                    updates[SZ_HEAT_DEMAND] = p[SZ_HEAT_DEMAND]
            elif (
                SZ_UFH_INDEX not in p
                and "ufh_index" not in p
                and "ufx_index" not in p
            ):
                updates[SZ_HEAT_DEMAND] = p[SZ_HEAT_DEMAND]

        elif msg.code == Code._0008 and SZ_RELAY_DEMAND in p:
            # Prevent FA (UFH) relay demands from overwriting FC relay demand
            if (
                slug == "UFC"
                and (
                    p.get(SZ_DOMAIN_INDEX)
                    or p.get("domain_id")
                    or p.get("domain_index")
                )
                != "FC"
            ):
                pass
            else:
                updates[SZ_RELAY_DEMAND] = p[SZ_RELAY_DEMAND]

        elif msg.code == Code._0009 and "failsafe_enabled" in p:
            updates[SZ_RELAY_FAILSAFE] = p["failsafe_enabled"]

        # 1100 (TPI params) — populate the TCS's _tpi_params dict (issue 1102).
        # TPI params don't fit into DemandState; they're stored per-domain on
        # the TCS and read by tcs.tpi_params.  Handled before the `if not
        # updates` guard because 1100 has no demand fields.
        if msg.code == Code._1100 and "cycle_rate" in p:
            tcs_for_tpi = getattr(target, "tcs", None) or (
                target if slug in ("CTL", "BDR") else None
            )
            if tcs_for_tpi is not None:
                tpi_dict = getattr(tcs_for_tpi, "_tpi_params", None)
                if tpi_dict is not None:
                    domain = (
                        p.get(SZ_DOMAIN_INDEX) or p.get("domain_id") or "FC"
                    )
                    tpi_dict[domain] = p

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

        # Populate the TCS's per-domain demand dicts (issue 1102 / ramses_cc#1026).
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

    def _update_ufh_state(
        self, target: Any, p: dict[str, Any], msg: Message
    ) -> None:
        """Translate UFH circuit arrays and bounds into UfhState."""
        if msg.code not in (Code._3150, Code._0008, Code._22C9):
            return

        if getattr(target, "_SLUG", "") != "UFC":
            return

        current_state = getattr(target, "ufh_state", None) or UfhState()
        updates: dict[str, Any] = {}

        # Safely extract index matching legacy typo "ufx_index"
        ufh_index = (
            p.get("ufx_index")
            or p.get(SZ_UFH_INDEX)
            or p.get("ufh_index")
            or p.get(SZ_ZONE_INDEX)
            or p.get("zone_index")
        )

        if (
            msg.code == Code._3150
            and ufh_index is not None
            and SZ_HEAT_DEMAND in p
        ):
            new_demands = dict(current_state.heat_demands)
            new_demands[str(ufh_index)] = p[SZ_HEAT_DEMAND]
            updates["heat_demands"] = new_demands

        elif (
            msg.code == Code._0008
            and (
                p.get(SZ_DOMAIN_INDEX)
                or p.get("domain_id")
                or p.get("domain_index")
            )
            == "FA"
            and SZ_RELAY_DEMAND in p
        ):
            updates["relay_demand_fa"] = p[SZ_RELAY_DEMAND]
        elif msg.code == Code._22C9 and ufh_index is not None:
            new_sp = dict(current_state.setpoints)
            sp_data = dict(new_sp.get(str(ufh_index), {}))

            # Legacy parsers return an empty dict if no bounds exist.
            # Only populate the bounds if they are explicitly present.
            bounds = p.get(SZ_SETPOINT_BOUNDS)
            if isinstance(bounds, tuple) and len(bounds) == 2:
                sp_data["temp_low"] = bounds[0]
                sp_data["temp_high"] = bounds[1]

            new_sp[str(ufh_index)] = sp_data
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
        if SZ_PUMP_RELAY_STATE in p:
            updates[SZ_PUMP_RELAY_STATE] = p[SZ_PUMP_RELAY_STATE]

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

    def _update_zone_state(
        self, target: Any, p: dict[str, Any], msg: Message
    ) -> None:
        """Translate zone configuration opcodes into ZoneState.

        Handles:
        - 0004 (zone_name): updates zone_state.name
        - 000A (zone_config): updates min_temp, max_temp, local_override,
          openwindow_function, multiroom_mode (issue 1102)
        - 2349 (zone_mode): updates zone_state.mode, setpoint, until
        - 2309 (setpoint): updates zone_state.setpoint
        """
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

        dtm = getattr(msg, "dtm", getattr(msg, "timestamp", None))
        if dtm:
            updates["last_updated"] = dtm

        current_state = getattr(target, "zone_state", None) or ZoneState()
        new_state = dataclasses.replace(current_state, **updates)

        event = StateUpdatedEvent(
            entity_id=getattr(target, "id", "unknown"),
            state=new_state,
            correlation_id=getattr(msg, "correlation_id", uuid.uuid4()),
            causation_id=getattr(msg, "message_id", uuid.uuid4()),
        )
        if hasattr(target, "apply_state_update"):
            target.apply_state_update(event)
        else:
            target.zone_state = new_state  # noqa: B010
