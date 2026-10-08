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
from ramses_rf.address import HGI_DEV_ADDR, NON_DEV_ADDR, Address
from ramses_rf.commands.core import Command as Intent_
from ramses_rf.const import (
    F9,
    FA,
    FC,
    SZ_ACTIVE,
    SZ_ACTUATOR_COUNTDOWN,
    SZ_ACTUATOR_ENABLED,
    SZ_AIR_PRESSURE_FAULT,
    SZ_AIR_QUALITY,
    SZ_AIR_QUALITY_BASIS,
    SZ_BASE,
    SZ_BATTERY_LEVEL,
    SZ_BATTERY_LOW,
    SZ_BOILER_EXHAUST,
    SZ_BOILER_OUTPUT,
    SZ_BOILER_RETURN,
    SZ_BOILER_SETPOINT,
    SZ_BOOST_TIMER_MINS,
    SZ_BURNER_FAILED_STARTS,
    SZ_BURNER_HOURS,
    SZ_BURNER_STARTS,
    SZ_BYPASS_MODE,
    SZ_BYPASS_POSITION,
    SZ_BYPASS_STATE,
    SZ_CH_ACTIVE,
    SZ_CH_ENABLED,
    SZ_CH_MAX_SETPOINT,
    SZ_CH_PUMP_HOURS,
    SZ_CH_PUMP_STARTS,
    SZ_CH_SETPOINT,
    SZ_CH_WATER_PRESSURE,
    SZ_CIRCUIT_INDEX,
    SZ_CIRCUIT_MODE,
    SZ_CIRCUIT_MODES,
    SZ_CIRCUIT_TO_ZONE_MAP,
    SZ_CIRCUITS,
    SZ_CO2_LEVEL,
    SZ_CO2_LEVEL_FAULT,
    SZ_COOLING_ACTIVE,
    SZ_COOLING_DEMAND,
    SZ_COOLING_DEMANDS,
    SZ_COOLING_ENABLED,
    SZ_COOLING_MODE,
    SZ_COUNTERS,
    SZ_CYCLE_COUNTDOWN,
    SZ_CYCLE_RATE,
    SZ_DATETIME,
    SZ_DEWPOINT_TEMP,
    SZ_DHW,
    SZ_DHW_ACTIVE,
    SZ_DHW_BLOCKING,
    SZ_DHW_BURNER_HOURS,
    SZ_DHW_BURNER_STARTS,
    SZ_DHW_ENABLED,
    SZ_DHW_FLOW_RATE,
    SZ_DHW_INDEX,
    SZ_DHW_PUMP_HOURS,
    SZ_DHW_PUMP_STARTS,
    SZ_DHW_SETPOINT,
    SZ_DIAGNOSTIC_CODE,
    SZ_DIFFERENTIAL,
    SZ_DOMAIN_ID_WIRE,
    SZ_DOMAIN_INDEX,
    SZ_DOMAIN_OR_ZONE_INDEX,
    SZ_EXHAUST_FAN_SPEED,
    SZ_EXHAUST_FLOW,
    SZ_EXHAUST_TEMP,
    SZ_FAILSAFE_ENABLED,
    SZ_FAN_INFO,
    SZ_FAN_MODE,
    SZ_FAN_RATE,
    SZ_FAULT_PRESENT,
    SZ_FILTER_REMAINING_DAYS,
    SZ_FILTER_REMAINING_PERCENT,
    SZ_FLAGS,
    SZ_FLAME_ACTIVE,
    SZ_FLAME_SIGNAL_LOW,
    SZ_GAS_FLAME_FAULT,
    SZ_HEAT_DEMAND,
    SZ_HEAT_DEMANDS,
    SZ_INDOOR_HUMIDITY,
    SZ_INDOOR_TEMP,
    SZ_LANGUAGE,
    SZ_LAST_UPDATED,
    SZ_LOCAL_OVERRIDE,
    SZ_LOCKOUT_RESET,
    SZ_LOW_WATER_PRESSURE,
    SZ_MAX_REL_MODULATION,
    SZ_MAX_TEMP,
    SZ_MESSAGE_ID,
    SZ_MIN_TEMP,
    SZ_MINUTES,
    SZ_MODE,
    SZ_MODULATION_LEVEL,
    SZ_MULTIROOM_MODE,
    SZ_NAME,
    SZ_OEM_CODE,
    SZ_OEM_FAULT_CODE,
    SZ_OPENWINDOW_FUNCTION,
    SZ_OTC_ACTIVE,
    SZ_OUTDOOR_HUMIDITY,
    SZ_OUTDOOR_TEMP,
    SZ_OUTSIDE,
    SZ_OVERRUN,
    SZ_POST_HEAT,
    SZ_PRE_HEAT,
    SZ_PRESENCE_DETECTED,
    SZ_PRESSURE,
    SZ_PUMP_RELAY_STATE,
    SZ_REL_MODULATION_LEVEL,
    SZ_RELAY_DEMAND,
    SZ_RELAY_DEMAND_FA,
    SZ_RELAY_DEMAND_FC,
    SZ_RELAY_FAILSAFE,
    SZ_REMAINING_DAYS,
    SZ_REMAINING_MINS,
    SZ_REMAINING_PERCENT,
    SZ_REQ_REASON,
    SZ_REQ_SPEED,
    SZ_REQUEST_FAN_SPEED,
    SZ_REQUEST_REASON,
    SZ_REQUEST_SPEED,
    SZ_SERVICE_REQUEST,
    SZ_SETPOINT,
    SZ_SETPOINT_BOUNDS,
    SZ_SETPOINTS,
    SZ_SPEED_CAPABILITIES,
    SZ_STATE,
    SZ_SUMMER_MODE,
    SZ_SUPPLY_FAN_SPEED,
    SZ_SUPPLY_FLOW,
    SZ_SUPPLY_TEMP,
    SZ_SYSTEM_MODE,
    SZ_TEMP_HIGH,
    SZ_TEMP_LOW,
    SZ_TEMPERATURE,
    SZ_TEMPERATURES,
    SZ_TIMESTAMP,
    SZ_UFH_INDEX,
    SZ_UNKNOWN,
    SZ_UNTIL,
    SZ_VALUE,
    SZ_WATER_OVER_TEMPERATURE,
    SZ_WINDOW_OPEN,
    SZ_ZONE_INDEX,
    DevType,
)
from ramses_rf.devices.dev_base import DeviceBase
from ramses_rf.enums import Action, PumpRelayState
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
    UfhCircuitState,
    UfhState,
    ZoneState,
)
from ramses_rf.protocol.opentherm import (
    SZ_VALUE_HB,
    SZ_VALUE_LB,
    OtDataId,
)
from ramses_rf.state_projector import (
    _get_dhw_zone_from_msg,
    _route_2411_to_fan,
)
from ramses_tx import Priority
from ramses_tx.const import I_, RQ, Code

# --- Translation Maps (Static Constant Blocks) ---

RAMSES_HEATING_MAP: Final[dict[Code, tuple[str, str, str]]] = {
    Code._3200: (SZ_TEMPERATURE, SZ_TEMPERATURES, SZ_BOILER_OUTPUT),
    Code._3210: (SZ_TEMPERATURE, SZ_TEMPERATURES, SZ_BOILER_RETURN),
    Code._22D9: (SZ_SETPOINT, SZ_TEMPERATURES, SZ_BOILER_SETPOINT),
    Code._1081: (SZ_SETPOINT, SZ_TEMPERATURES, SZ_CH_MAX_SETPOINT),
    Code._1300: (SZ_PRESSURE, SZ_BASE, SZ_CH_WATER_PRESSURE),
    Code._12F0: (SZ_DHW_FLOW_RATE, SZ_BASE, SZ_DHW_FLOW_RATE),
    Code._10A0: (SZ_SETPOINT, SZ_TEMPERATURES, SZ_DHW_SETPOINT),
    Code._1260: (SZ_TEMPERATURE, SZ_TEMPERATURES, SZ_DHW),
    Code._1290: (SZ_TEMPERATURE, SZ_TEMPERATURES, SZ_OUTSIDE),
}

OPENTHERM_FIELD_MAP: Final[dict[OtDataId, tuple[str, str]]] = {
    OtDataId.BOILER_OUTPUT_TEMP: (SZ_TEMPERATURES, SZ_BOILER_OUTPUT),
    OtDataId.BOILER_RETURN_TEMP: (SZ_TEMPERATURES, SZ_BOILER_RETURN),
    OtDataId.BOILER_EXHAUST_TEMP: (SZ_TEMPERATURES, SZ_BOILER_EXHAUST),
    OtDataId.CONTROL_SETPOINT: (SZ_TEMPERATURES, SZ_BOILER_SETPOINT),
    OtDataId.CH_MAX_SETPOINT: (SZ_TEMPERATURES, SZ_CH_MAX_SETPOINT),
    OtDataId.CH_WATER_PRESSURE: (SZ_BASE, SZ_CH_WATER_PRESSURE),
    OtDataId.DHW_FLOW_RATE: (SZ_BASE, SZ_DHW_FLOW_RATE),
    OtDataId.DHW_SETPOINT: (SZ_TEMPERATURES, SZ_DHW_SETPOINT),
    OtDataId.DHW_TEMP: (SZ_TEMPERATURES, SZ_DHW),
    OtDataId.OEM_CODE: (SZ_BASE, SZ_OEM_CODE),
    OtDataId.OUTSIDE_TEMP: (SZ_TEMPERATURES, SZ_OUTSIDE),
    OtDataId.REL_MODULATION_LEVEL: (SZ_BASE, SZ_REL_MODULATION_LEVEL),
    OtDataId._0E: (SZ_BASE, SZ_MAX_REL_MODULATION),
    OtDataId.BURNER_HOURS: (SZ_COUNTERS, SZ_BURNER_HOURS),
    OtDataId.BURNER_STARTS: (SZ_COUNTERS, SZ_BURNER_STARTS),
    OtDataId.BURNER_FAILED_STARTS: (SZ_COUNTERS, SZ_BURNER_FAILED_STARTS),
    OtDataId.CH_PUMP_HOURS: (SZ_COUNTERS, SZ_CH_PUMP_HOURS),
    OtDataId.CH_PUMP_STARTS: (SZ_COUNTERS, SZ_CH_PUMP_STARTS),
    OtDataId.DHW_BURNER_HOURS: (SZ_COUNTERS, SZ_DHW_BURNER_HOURS),
    OtDataId.DHW_BURNER_STARTS: (SZ_COUNTERS, SZ_DHW_BURNER_STARTS),
    OtDataId.DHW_PUMP_HOURS: (SZ_COUNTERS, SZ_DHW_PUMP_HOURS),
    OtDataId.DHW_PUMP_STARTS: (SZ_COUNTERS, SZ_DHW_PUMP_STARTS),
    OtDataId.FLAME_LOW_SIGNALS: (SZ_COUNTERS, SZ_FLAME_SIGNAL_LOW),
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
        :param ssot_queue: Single Source of Truth Queue.
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
        # uses to create the ~15 parameter number entities.  This was moved
        # from HvacVentilator._handle_msg (removed in Phase 2.95) into the
        # StateProjector.  See ramses_cc issue 851.
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
            if msg.dst.id != NON_DEV_ADDR.id and msg.dst.id != msg.src.id:
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
            zone_val = payload.get(SZ_ZONE_INDEX, payload.get(SZ_ZONE_INDEX))
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
                                getattr(parent, "id", SZ_UNKNOWN),
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
                payload.get(SZ_DOMAIN_ID_WIRE, payload.get(SZ_DOMAIN_INDEX)),
            )
            if (
                domain_val is not None
                and src_dev
                and msg.src.id in system_by_id
            ):
                tcs = system_by_id[msg.src.id]
                if domain_val == FC and tcs is not None:
                    try:
                        self._update_demand_state(tcs, payload, msg)
                    except Exception as err:
                        _LOGGER.error(
                            "CQRS extraction failed for TCS %s: %s",
                            tcs.id,
                            err,
                        )
                elif (
                    domain_val in (FA, F9)
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
                            getattr(appliance_control, "id", SZ_UNKNOWN),
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
                try:
                    intent_3ef1 = Intent_(
                        src=HGI_DEV_ADDR,
                        dst=Address(msg.src.id),
                        action=Action.GET_RELAY_DEMAND,
                        data={SZ_DOMAIN_INDEX: "00"},
                    )
                    self._gateway.dispatcher.send_background(
                        intent_3ef1, priority=Priority.LOW
                    )
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
                    intent_1260 = Intent_(
                        src=HGI_DEV_ADDR,
                        dst=Address(src_dev.ctl.id),
                        action=Action.GET_DHW_TEMP,
                        data={SZ_DHW_INDEX: 0},
                    )
                    self._gateway.dispatcher.send_background(intent_1260)
                except Exception as err:
                    _LOGGER.error(
                        "Failed to trigger CQRS 1260 reactor for %s: %s",
                        src_dev.ctl.id,
                        err,
                    )

    def _update_opentherm_state(
        self, target: Any, payload: dict[str, Any], msg: Message
    ) -> None:
        """Translate OpenTherm frames or parallel opcodes into OpenThermState."""
        current_state = getattr(target, "opentherm_state", None)
        if current_state is None:
            if getattr(target, "_SLUG", "") == DevType.OTB:
                current_state = OpenThermState()
            else:
                return

        base_updates: dict[str, Any] = {}
        flag_updates: dict[str, Any] = {}
        fault_updates: dict[str, Any] = {}
        temperature_updates: dict[str, Any] = {}
        counter_updates: dict[str, Any] = {}

        match msg.code:
            case Code._3220:
                raw_id = payload.get(SZ_MESSAGE_ID)
                value = payload.get(SZ_VALUE)

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
                    flag_updates.update(
                        {
                            SZ_CH_ENABLED: bool(value[0]),
                            SZ_DHW_ENABLED: bool(value[1]),
                            SZ_COOLING_ENABLED: bool(value[2]),
                            SZ_OTC_ACTIVE: bool(value[3]),
                            SZ_SUMMER_MODE: bool(value[5]),
                            SZ_DHW_BLOCKING: bool(value[6]),
                            SZ_FAULT_PRESENT: bool(value[8]),
                            SZ_CH_ACTIVE: bool(value[9]),
                            SZ_DHW_ACTIVE: bool(value[10]),
                            SZ_FLAME_ACTIVE: bool(value[11]),
                            SZ_COOLING_ACTIVE: bool(value[12]),
                        }
                    )
                elif msg_id == OtDataId.OEM_FAULTS:
                    # HB: application-specific fault flags (bits 0-5,
                    # LSB-first); LB: OEM fault code (no universal "none"
                    # sentinel — 0x00 on ATAG, 0xFF on Vaillant/VR33).
                    value_hb = payload.get(SZ_VALUE_HB)
                    if (
                        isinstance(value_hb, (list, tuple))
                        and len(value_hb) >= 6
                    ):
                        fault_updates.update(
                            {
                                SZ_SERVICE_REQUEST: bool(value_hb[0]),
                                SZ_LOCKOUT_RESET: bool(value_hb[1]),
                                SZ_LOW_WATER_PRESSURE: bool(value_hb[2]),
                                SZ_GAS_FLAME_FAULT: bool(value_hb[3]),
                                SZ_AIR_PRESSURE_FAULT: bool(value_hb[4]),
                                SZ_WATER_OVER_TEMPERATURE: bool(value_hb[5]),
                            }
                        )
                    value_lb = payload.get(SZ_VALUE_LB)
                    if isinstance(value_lb, int) and not isinstance(
                        value_lb, bool
                    ):
                        base_updates[SZ_OEM_FAULT_CODE] = value_lb

                elif value is not None and msg_id in OPENTHERM_FIELD_MAP:
                    category, field_key = OPENTHERM_FIELD_MAP[msg_id]
                    match category:
                        case "base":
                            if field_key in (
                                SZ_REL_MODULATION_LEVEL,
                                SZ_MAX_REL_MODULATION,
                            ):
                                if (
                                    isinstance(value, (int, float))
                                    and not isinstance(value, bool)
                                    and 0.0 <= value <= 1.0
                                ):
                                    base_updates[field_key] = value
                            else:
                                base_updates[field_key] = value
                        case "temperatures":
                            temperature_updates[field_key] = value
                        case "counters":
                            counter_updates[field_key] = value
                        case "flags":
                            flag_updates[field_key] = value

            case Code._1FD0:
                diagnostic_code = payload.get(
                    SZ_DIAGNOSTIC_CODE, payload.get(SZ_OEM_CODE)
                )
                if isinstance(diagnostic_code, int):
                    base_updates[SZ_OEM_CODE] = diagnostic_code

            case Code._1FD4:
                dtm = getattr(msg, "dtm", getattr(msg, SZ_TIMESTAMP, None))
                if dtm:
                    base_updates[SZ_LAST_UPDATED] = dtm

            case Code._3EF0 | Code._3EF1:
                mod_val = payload.get(
                    SZ_MODULATION_LEVEL, payload.get(SZ_REL_MODULATION_LEVEL)
                )
                if mod_val is not None:
                    base_updates[SZ_REL_MODULATION_LEVEL] = mod_val
                if SZ_MAX_REL_MODULATION in payload:
                    base_updates[SZ_MAX_REL_MODULATION] = payload[
                        SZ_MAX_REL_MODULATION
                    ]
                if SZ_CH_SETPOINT in payload:
                    temperature_updates[SZ_CH_SETPOINT] = payload[
                        SZ_CH_SETPOINT
                    ]
                for key in (
                    SZ_CH_ACTIVE,
                    SZ_CH_ENABLED,
                    SZ_DHW_ACTIVE,
                    SZ_FLAME_ACTIVE,
                ):
                    if key in payload:
                        flag_updates[key] = payload[key]

            case code if code in RAMSES_HEATING_MAP:
                payload_key, category, state_field = RAMSES_HEATING_MAP[code]
                if payload_key in payload:
                    if category == SZ_BASE:
                        base_updates[state_field] = payload[payload_key]
                    elif category == SZ_TEMPERATURES:
                        temperature_updates[state_field] = payload[payload_key]

        if not any(
            (
                base_updates,
                flag_updates,
                fault_updates,
                temperature_updates,
                counter_updates,
            )
        ):
            return

        dtm = getattr(msg, "dtm", getattr(msg, SZ_TIMESTAMP, None))
        if dtm:
            base_updates[SZ_LAST_UPDATED] = dtm

        new_flags = current_state.flags
        if flag_updates:
            new_flags = dataclasses.replace(new_flags, **flag_updates)

        new_faults = current_state.faults
        if fault_updates:
            new_faults = dataclasses.replace(new_faults, **fault_updates)

        new_temps = current_state.temperatures
        if temperature_updates:
            new_temps = dataclasses.replace(new_temps, **temperature_updates)

        new_counters = current_state.counters
        if counter_updates:
            new_counters = dataclasses.replace(new_counters, **counter_updates)

        new_state = dataclasses.replace(
            current_state,
            flags=new_flags,
            faults=new_faults,
            temperatures=new_temps,
            counters=new_counters,
            **base_updates,
        )
        target.opentherm_state = new_state

        event = StateUpdatedEvent(
            entity_id=getattr(target, "id", SZ_UNKNOWN),
            state=new_state,
            correlation_id=getattr(msg, "correlation_id", uuid.uuid4()),
            causation_id=getattr(msg, "message_id", uuid.uuid4()),
        )
        if hasattr(target, "apply_state_update"):
            target.apply_state_update(event)

    def _update_hvac_state(
        self, target: Any, payload: dict[str, Any], msg: Message
    ) -> None:
        """Translate complex multi-opcode ventilation payloads into HvacState.

        Applies hardware-specific stateful FSM rules (via the Quirks
        middleware) prior to hydration.
        """
        if getattr(target, "_SLUG", "") in (
            DevType.CTL,
            DevType.BDR,
            DevType.TRV,
            DevType.OTB,
            DevType.UFC,
            DevType.DHW,
        ):
            return

        current_state = getattr(target, "hvac_state", None) or HvacState()
        strategy = (
            target.get_configured_strategy()
            if isinstance(target, DeviceBase)
            else None
        )
        payload = quirks.apply_hvac_quirks(
            payload, current_state, msg.code, strategy=strategy
        )

        # The parser names 22F1 modes via a mode_max heuristic, which
        # mis-decodes real Orcon remotes (e.g. VMN-15LF01 sends
        # mode_max=04 -> "itho" names, so index 01 "low" displays as
        # "auto").  A remote has no scheme of its own: its commands use
        # the bound FAN's vocabulary, so fall back to the parent fan's
        # configured scheme.  See ramses-rf/ramses_cc issue 1216.
        if (
            msg.code == Code._22F1
            and SZ_FAN_MODE in payload
            and payload.get("_mode_index") is not None
        ):
            mode_strategy = strategy
            if mode_strategy is None and isinstance(target, DeviceBase):
                parent_fan = getattr(target, "parent_fan", None)
                if isinstance(parent_fan, DeviceBase):
                    mode_strategy = parent_fan._get_configured_strategy()
            if mode_strategy is not None:
                mapped = mode_strategy.fan_modes.get(
                    str(payload["_mode_index"])
                )
                if mapped is not None:
                    payload[SZ_FAN_MODE] = mapped

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
            if field_name not in payload:
                continue
            field_val = payload[field_name]
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

        # Flag filter_remaining support on device
        if (
            msg.code == Code._10D0
            and not target._supports_10d0
            and (
                payload[SZ_REMAINING_DAYS] is not None
                or payload[SZ_REMAINING_PERCENT] is not None
            )
        ):
            target._supports_10d0 = True
            _LOGGER.debug(
                "Device %s confirmed 10D0 Filter Change support", target.id
            )

        # Handle non-standard names passed by the semantic parsers
        if SZ_REMAINING_DAYS in payload:
            updates[SZ_FILTER_REMAINING_DAYS] = payload[SZ_REMAINING_DAYS]
        if SZ_REMAINING_PERCENT in payload:
            updates[SZ_FILTER_REMAINING_PERCENT] = payload[
                SZ_REMAINING_PERCENT
            ]
        if SZ_MINUTES in payload and msg.code == Code._22F3:
            updates[SZ_BOOST_TIMER_MINS] = payload[SZ_MINUTES]
        req_speed = payload.get(SZ_REQUEST_SPEED, payload.get(SZ_REQ_SPEED))
        if req_speed is not None:
            updates[SZ_REQUEST_FAN_SPEED] = req_speed
        req_reason = payload.get(SZ_REQUEST_REASON, payload.get(SZ_REQ_REASON))
        if req_reason is not None:
            updates[SZ_REQUEST_REASON] = req_reason

        if not updates:
            return

        if SZ_FAN_MODE in updates and hasattr(target, "_last_fan_mode_dtm"):
            target._last_fan_mode_dtm = getattr(
                msg, "dtm", getattr(msg, SZ_TIMESTAMP, None)
            )

        dtm = getattr(msg, "dtm", getattr(msg, SZ_TIMESTAMP, None))
        if dtm:
            updates[SZ_LAST_UPDATED] = dtm

        new_state = dataclasses.replace(current_state, **updates)
        target.hvac_state = new_state

        event = StateUpdatedEvent(
            entity_id=getattr(target, "id", SZ_UNKNOWN),
            state=new_state,
            correlation_id=getattr(msg, "correlation_id", uuid.uuid4()),
            causation_id=getattr(msg, "message_id", uuid.uuid4()),
        )
        if hasattr(target, "apply_state_update"):
            target.apply_state_update(event)

    def _update_power_state(
        self, target: Any, payload: dict[str, Any], msg: Message
    ) -> None:
        """Translate battery opcodes into PowerState."""
        updates: dict[str, Any] = {}
        if msg.code == Code._1060:
            if SZ_BATTERY_LOW in payload:
                updates[SZ_BATTERY_LOW] = payload[SZ_BATTERY_LOW]
            if SZ_BATTERY_LEVEL in payload:
                updates[SZ_BATTERY_LEVEL] = payload[SZ_BATTERY_LEVEL]

        if not updates:
            return

        dtm = getattr(msg, "dtm", getattr(msg, SZ_TIMESTAMP, None))
        if dtm:
            updates[SZ_LAST_UPDATED] = dtm

        current_state = getattr(target, "power_state", None) or PowerState()
        new_state = dataclasses.replace(current_state, **updates)
        target.power_state = new_state

        event = StateUpdatedEvent(
            entity_id=getattr(target, "id", SZ_UNKNOWN),
            state=new_state,
            correlation_id=getattr(msg, "correlation_id", uuid.uuid4()),
            causation_id=getattr(msg, "message_id", uuid.uuid4()),
        )
        if hasattr(target, "apply_state_update"):
            target.apply_state_update(event)

    def _update_dhw_state(
        self, target: Any, payload: dict[str, Any], msg: Message
    ) -> None:
        """Translate DHW opcodes into DhwState."""
        if msg.code not in (Code._10A0, Code._1260, Code._1F41):
            return

        updates: dict[str, Any] = {}
        if msg.code == Code._10A0:
            if SZ_SETPOINT in payload:
                updates[SZ_SETPOINT] = payload[SZ_SETPOINT]
            if SZ_OVERRUN in payload:
                updates[SZ_OVERRUN] = payload[SZ_OVERRUN]
            if SZ_DIFFERENTIAL in payload:
                updates[SZ_DIFFERENTIAL] = payload[SZ_DIFFERENTIAL]
        elif msg.code == Code._1260:
            if SZ_TEMPERATURE in payload:
                updates[SZ_TEMPERATURE] = payload[SZ_TEMPERATURE]
        elif msg.code == Code._1F41:
            if SZ_MODE in payload:
                updates[SZ_MODE] = payload[SZ_MODE]
            if SZ_ACTIVE in payload:
                updates[SZ_ACTIVE] = payload[SZ_ACTIVE]
            if SZ_UNTIL in payload:
                updates[SZ_UNTIL] = payload[SZ_UNTIL]

        if not updates:
            return

        dtm = getattr(msg, "dtm", getattr(msg, SZ_TIMESTAMP, None))
        if dtm:
            updates[SZ_LAST_UPDATED] = dtm

        current_state = getattr(target, "dhw_state", None) or DhwState()
        new_state = dataclasses.replace(current_state, **updates)
        target.dhw_state = new_state

        event = StateUpdatedEvent(
            entity_id=getattr(target, "id", SZ_UNKNOWN),
            state=new_state,
            correlation_id=getattr(msg, "correlation_id", uuid.uuid4()),
            causation_id=getattr(msg, "message_id", uuid.uuid4()),
        )
        if hasattr(target, "apply_state_update"):
            target.apply_state_update(event)

    def _update_system_state(
        self, target: Any, payload: dict[str, Any], msg: Message
    ) -> None:
        """Translate system configuration opcodes into SystemState."""
        if msg.code not in (Code._0100, Code._2E04, Code._313F, Code._2D49):
            return

        updates: dict[str, Any] = {}
        if msg.code == Code._0100:
            if SZ_LANGUAGE in payload:
                updates[SZ_LANGUAGE] = payload[SZ_LANGUAGE]
        elif msg.code == Code._2E04:
            if SZ_SYSTEM_MODE in payload:
                updates[SZ_SYSTEM_MODE] = payload[SZ_SYSTEM_MODE]
            if SZ_UNTIL in payload:
                updates[SZ_UNTIL] = payload[SZ_UNTIL]
        elif msg.code == Code._313F:
            if SZ_DATETIME in payload:
                updates[SZ_DATETIME] = payload[SZ_DATETIME]
        elif msg.code == Code._2D49:
            if SZ_COOLING_DEMAND in payload:
                updates[SZ_COOLING_MODE] = payload[SZ_COOLING_DEMAND]

        if not updates:
            return

        dtm = getattr(msg, "dtm", getattr(msg, SZ_TIMESTAMP, None))
        if dtm:
            updates[SZ_LAST_UPDATED] = dtm

        current_state = getattr(target, "system_state", None) or SystemState()
        new_state = dataclasses.replace(current_state, **updates)
        target.system_state = new_state

        event = StateUpdatedEvent(
            entity_id=getattr(target, "id", SZ_UNKNOWN),
            state=new_state,
            correlation_id=getattr(msg, "correlation_id", uuid.uuid4()),
            causation_id=getattr(msg, "message_id", uuid.uuid4()),
        )
        if hasattr(target, "apply_state_update"):
            target.apply_state_update(event)

    def _update_temperature_state(
        self, target: Any, payload: dict[str, Any], msg: Message
    ) -> None:
        """Translate temperature/TRV opcodes into TrvState & TemperatureState."""
        dtm = getattr(msg, "dtm", getattr(msg, SZ_TIMESTAMP, None))

        if msg.code == Code._12B0 and SZ_WINDOW_OPEN in payload:
            current_trv = getattr(target, "trv_state", None) or TrvState()
            trv_updates = {SZ_WINDOW_OPEN: payload[SZ_WINDOW_OPEN]}
            if dtm:
                trv_updates[SZ_LAST_UPDATED] = dtm

            new_trv = dataclasses.replace(current_trv, **trv_updates)
            target.trv_state = new_trv

            event = StateUpdatedEvent(
                entity_id=getattr(target, "id", SZ_UNKNOWN),
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
            if SZ_TEMPERATURE in payload:
                # Legacy Parity: Physical sensors only track their own local sensor readings.
                # We must ignore Zone temperature syncs sent TO them by the Controller.
                # Keep same as src/ramses_rf/dispatcher.py#_update_temperature_state
                target_id = getattr(target, "id", str(target))
                src_id = msg.src.id

                if (
                    getattr(target, "_SLUG", "") in (DevType.TRV, DevType.THM)
                    and src_id != target_id
                ):
                    pass
                else:
                    updates[SZ_TEMPERATURE] = payload[SZ_TEMPERATURE]

            if SZ_SETPOINT in payload:
                updates[SZ_SETPOINT] = payload[SZ_SETPOINT]

            if updates:
                if dtm:
                    updates[SZ_LAST_UPDATED] = dtm

                current_temp = (
                    getattr(target, "temp_state", None) or TemperatureState()
                )
                new_temp = dataclasses.replace(current_temp, **updates)
                target.temp_state = new_temp

                event = StateUpdatedEvent(
                    entity_id=getattr(target, "id", SZ_UNKNOWN),
                    state=new_temp,
                    correlation_id=getattr(
                        msg, "correlation_id", uuid.uuid4()
                    ),
                    causation_id=getattr(msg, "message_id", uuid.uuid4()),
                )
                if hasattr(target, "apply_state_update"):
                    target.apply_state_update(event)

    def _update_demand_state(
        self, target: Any, payload: dict[str, Any], msg: Message
    ) -> None:
        """Translate demand opcodes into DemandState."""
        if msg.code not in (Code._3150, Code._0008, Code._0009, Code._1100):
            return

        updates: dict[str, Any] = {}
        slug = getattr(target, "_SLUG", "")

        if msg.code == Code._3150 and SZ_HEAT_DEMAND in payload:
            # Prevent flattened array payloads (e.g., UFH circuit demands)
            # from overwriting the controller's aggregate FC heat demand.
            if slug in (DevType.CTL, DevType.UFC):
                if (
                    payload.get(SZ_DOMAIN_INDEX)
                    or payload.get(SZ_DOMAIN_ID_WIRE)
                    or payload.get(SZ_DOMAIN_INDEX)
                ) == FC:
                    updates[SZ_HEAT_DEMAND] = payload[SZ_HEAT_DEMAND]
            elif SZ_UFH_INDEX not in payload:
                updates[SZ_HEAT_DEMAND] = payload[SZ_HEAT_DEMAND]

        elif msg.code == Code._0008 and SZ_RELAY_DEMAND in payload:
            # Prevent FA (UFH) relay demands from overwriting FC relay demand
            if (
                slug == DevType.UFC
                and (
                    payload.get(SZ_DOMAIN_INDEX)
                    or payload.get(SZ_DOMAIN_ID_WIRE)
                    or payload.get(SZ_DOMAIN_INDEX)
                )
                != FC
            ):
                pass
            else:
                updates[SZ_RELAY_DEMAND] = payload[SZ_RELAY_DEMAND]

        elif msg.code == Code._0009 and SZ_FAILSAFE_ENABLED in payload:
            updates[SZ_RELAY_FAILSAFE] = payload[SZ_FAILSAFE_ENABLED]

        # 1100 (TPI params) — populate the TCS's _tpi_params dict (issue 1102).
        # TPI params don't fit into DemandState; they're stored per-domain on
        # the TCS and read by tcs.tpi_params.  Handled before the `if not
        # updates` guard because 1100 has no demand fields.
        if msg.code == Code._1100 and SZ_CYCLE_RATE in payload:
            tcs_for_tpi = getattr(target, "tcs", None) or (
                target if slug in (DevType.CTL, DevType.BDR) else None
            )
            if tcs_for_tpi is not None:
                tpi_dict = getattr(tcs_for_tpi, "_tpi_params", None)
                if tpi_dict is not None:
                    domain = (
                        payload.get(SZ_DOMAIN_INDEX)
                        or payload.get(SZ_DOMAIN_ID_WIRE)
                        or FC
                    )
                    tpi_dict[domain] = payload

        if not updates:
            return

        dtm = getattr(msg, "dtm", getattr(msg, SZ_TIMESTAMP, None))
        if dtm:
            updates[SZ_LAST_UPDATED] = dtm

        current_state = getattr(target, "demand_state", None) or DemandState()
        new_state = dataclasses.replace(current_state, **updates)
        target.demand_state = new_state

        event = StateUpdatedEvent(
            entity_id=getattr(target, "id", SZ_UNKNOWN),
            state=new_state,
            correlation_id=getattr(msg, "correlation_id", uuid.uuid4()),
            causation_id=getattr(msg, "message_id", uuid.uuid4()),
        )
        if hasattr(target, "apply_state_update"):
            target.apply_state_update(event)

        # Populate the TCS's per-domain demand dicts (issue 1102 / ramses_cc#1026).
        tcs = getattr(target, "tcs", None) or (
            target if slug in (DevType.CTL, DevType.UFC) else None
        )
        if tcs is not None:
            domain = payload.get(SZ_DOMAIN_INDEX) or payload.get(
                SZ_DOMAIN_ID_WIRE
            )
            if domain and SZ_RELAY_DEMAND in payload:
                relay_dict = getattr(tcs, "_relay_demands", None)
                if relay_dict is not None:
                    relay_dict[domain] = msg
            if (
                domain
                and SZ_HEAT_DEMAND in payload
                and slug in (DevType.CTL, DevType.UFC)
            ):
                heat_dict = getattr(tcs, "_heat_demands", None)
                if heat_dict is not None:
                    heat_dict[domain] = msg

    def _update_ufh_state(
        self, target: Any, payload: dict[str, Any], msg: Message
    ) -> None:
        """Translate UFH circuit arrays, demands, and telemetry into UfhState.

        :param target: Target entity or device (e.g. UfhController).
        :type target: Any
        :param payload: Decoded payload dictionary.
        :type payload: dict[str, Any]
        :param msg: Associated protocol message.
        :type msg: Message
        """
        if getattr(target, "_SLUG", "") != DevType.UFC:
            return

        current_state: UfhState = (
            getattr(target, "ufh_state", None) or UfhState()
        )
        updates: dict[str, Any] = {}
        new_circuits = dict(current_state.circuits)
        dtm = getattr(msg, "dtm", getattr(msg, SZ_TIMESTAMP, None))

        def _get_circuit(circuit_index: str) -> UfhCircuitState:
            return new_circuits.get(circuit_index) or UfhCircuitState(
                ufh_index=circuit_index
            )

        def _get_circuit_index(data: dict[str, Any]) -> str | None:
            for key in (
                SZ_UFH_INDEX,
                SZ_CIRCUIT_INDEX,
                SZ_ZONE_INDEX,
                SZ_DOMAIN_OR_ZONE_INDEX,
            ):
                if (raw_index := data.get(key)) is not None:
                    return (
                        f"{int(raw_index):02X}"
                        if isinstance(raw_index, int)
                        else str(raw_index)
                    )
            return None

        match msg.code:
            case Code._3150:
                domain = (
                    payload.get(SZ_DOMAIN_INDEX)
                    or payload.get(SZ_DOMAIN_ID_WIRE)
                    or payload.get(SZ_DOMAIN_INDEX)
                )
                if domain == FA and SZ_HEAT_DEMAND in payload:
                    updates[SZ_RELAY_DEMAND_FA] = payload[SZ_HEAT_DEMAND]
                elif domain == FC and SZ_HEAT_DEMAND in payload:
                    updates[SZ_RELAY_DEMAND_FC] = payload[SZ_HEAT_DEMAND]

                circuit_index = _get_circuit_index(payload)
                if circuit_index is not None and SZ_HEAT_DEMAND in payload:
                    heat_value = (
                        float(payload[SZ_HEAT_DEMAND])
                        if payload[SZ_HEAT_DEMAND] is not None
                        else None
                    )
                    new_demands = dict(current_state.heat_demands)
                    new_demands[circuit_index] = heat_value
                    updates[SZ_HEAT_DEMANDS] = new_demands

                    circuit = _get_circuit(circuit_index)
                    circuit_kwargs: dict[str, Any] = {
                        SZ_HEAT_DEMAND: heat_value
                    }
                    if dtm:
                        circuit_kwargs[SZ_LAST_UPDATED] = dtm
                    new_circuits[circuit_index] = dataclasses.replace(
                        circuit, **circuit_kwargs
                    )
                    updates[SZ_CIRCUITS] = new_circuits

            case Code._2D49:
                circuit_index = _get_circuit_index(payload)
                cooling_value = payload.get(
                    SZ_COOLING_DEMAND,
                    payload.get(SZ_STATE, payload.get(SZ_COOLING_ACTIVE)),
                )
                if circuit_index is not None and cooling_value is not None:
                    value_float = (
                        float(cooling_value)
                        if isinstance(cooling_value, (int, float))
                        else (1.0 if cooling_value else 0.0)
                    )
                    new_cool = dict(current_state.cooling_demands)
                    new_cool[circuit_index] = value_float
                    updates[SZ_COOLING_DEMANDS] = new_cool

                    circuit = _get_circuit(circuit_index)
                    circuit_kwargs = {SZ_COOLING_DEMAND: value_float}
                    if dtm:
                        circuit_kwargs[SZ_LAST_UPDATED] = dtm
                    new_circuits[circuit_index] = dataclasses.replace(
                        circuit, **circuit_kwargs
                    )
                    updates[SZ_CIRCUITS] = new_circuits

            case Code._2249:
                circuit_index = _get_circuit_index(payload)
                mode_value = payload.get(SZ_MODE, payload.get(SZ_MODE))
                if circuit_index is not None and mode_value is not None:
                    mode_string = str(mode_value)
                    new_modes = dict(current_state.circuit_modes)
                    new_modes[circuit_index] = mode_string
                    updates[SZ_CIRCUIT_MODES] = new_modes

                    circuit = _get_circuit(circuit_index)
                    circuit_kwargs = {SZ_CIRCUIT_MODE: mode_string}
                    if dtm:
                        circuit_kwargs[SZ_LAST_UPDATED] = dtm
                    new_circuits[circuit_index] = dataclasses.replace(
                        circuit, **circuit_kwargs
                    )
                    updates[SZ_CIRCUITS] = new_circuits

            case Code._0008:
                domain = (
                    payload.get(SZ_DOMAIN_INDEX)
                    or payload.get(SZ_DOMAIN_ID_WIRE)
                    or payload.get(SZ_DOMAIN_INDEX)
                )
                if domain == FA and SZ_RELAY_DEMAND in payload:
                    updates[SZ_RELAY_DEMAND_FA] = payload[SZ_RELAY_DEMAND]
                elif domain == FC and SZ_RELAY_DEMAND in payload:
                    updates[SZ_RELAY_DEMAND_FC] = payload[SZ_RELAY_DEMAND]

            case Code._000C:
                circuit_index = _get_circuit_index(payload)
                zone_index = payload.get(SZ_ZONE_INDEX) or payload.get(
                    SZ_ZONE_INDEX
                )
                if circuit_index is not None:
                    zone_string = (
                        str(zone_index) if zone_index is not None else None
                    )
                    new_map = dict(current_state.circuit_to_zone_map)
                    new_map[circuit_index] = zone_string
                    updates[SZ_CIRCUIT_TO_ZONE_MAP] = new_map

                    circuit = _get_circuit(circuit_index)
                    circuit_kwargs = {SZ_ZONE_INDEX: zone_string}
                    if dtm:
                        circuit_kwargs[SZ_LAST_UPDATED] = dtm
                    new_circuits[circuit_index] = dataclasses.replace(
                        circuit, **circuit_kwargs
                    )
                    updates[SZ_CIRCUITS] = new_circuits

            case Code._22C9:
                circuit_index = _get_circuit_index(payload)
                if circuit_index is not None:
                    new_setpoints = dict(current_state.setpoints)
                    setpoint_data = dict(new_setpoints.get(circuit_index, {}))

                    bounds = payload.get(SZ_SETPOINT_BOUNDS)
                    min_temp = payload.get(SZ_MIN_TEMP)
                    max_temp = payload.get(SZ_MAX_TEMP)
                    if isinstance(bounds, tuple) and len(bounds) == 2:
                        setpoint_data[SZ_TEMP_LOW] = bounds[0]
                        setpoint_data[SZ_TEMP_HIGH] = bounds[1]
                        min_temp = min_temp or bounds[0]
                        max_temp = max_temp or bounds[1]

                    flags = payload.get(SZ_FLAGS) or payload.get(SZ_MODE)
                    if flags is not None and isinstance(flags, int):
                        setpoint_data[SZ_FLAGS] = flags

                    new_setpoints[circuit_index] = setpoint_data
                    updates[SZ_SETPOINTS] = new_setpoints

                    circuit = _get_circuit(circuit_index)
                    circuit_kwargs = {}
                    if dtm:
                        circuit_kwargs[SZ_LAST_UPDATED] = dtm
                    if min_temp is not None:
                        circuit_kwargs[SZ_MIN_TEMP] = min_temp
                    if max_temp is not None:
                        circuit_kwargs[SZ_MAX_TEMP] = max_temp
                    if flags is not None and isinstance(flags, int):
                        circuit_kwargs[SZ_FLAGS] = flags

                    new_circuits[circuit_index] = dataclasses.replace(
                        circuit, **circuit_kwargs
                    )
                    updates[SZ_CIRCUITS] = new_circuits

            case Code._2309:
                circuit_index = _get_circuit_index(payload)
                setpoint = payload.get(SZ_SETPOINT)
                if circuit_index is not None and setpoint is not None:
                    circuit = _get_circuit(circuit_index)
                    circuit_kwargs = {SZ_SETPOINT: setpoint}
                    if dtm:
                        circuit_kwargs[SZ_LAST_UPDATED] = dtm
                    new_circuits[circuit_index] = dataclasses.replace(
                        circuit, **circuit_kwargs
                    )
                    updates[SZ_CIRCUITS] = new_circuits

            case Code._3EF0 | Code._3EF1:
                pump_state = payload.get(
                    SZ_PUMP_RELAY_STATE, payload.get(SZ_PUMP_RELAY_STATE)
                )
                if pump_state is not None:
                    if isinstance(pump_state, PumpRelayState):
                        updates[SZ_PUMP_RELAY_STATE] = pump_state
                    elif isinstance(pump_state, str):
                        try:
                            updates[SZ_PUMP_RELAY_STATE] = PumpRelayState(
                                pump_state
                            )
                        except ValueError:
                            _LOGGER.warning(
                                "Unknown pump relay state: %s", pump_state
                            )

            case _:
                return

        if not updates:
            return

        if dtm:
            updates[SZ_LAST_UPDATED] = dtm

        new_state = dataclasses.replace(current_state, **updates)
        target.ufh_state = new_state

        event = StateUpdatedEvent(
            entity_id=getattr(target, "id", SZ_UNKNOWN),
            state=new_state,
            correlation_id=getattr(msg, "correlation_id", uuid.uuid4()),
            causation_id=getattr(msg, "message_id", uuid.uuid4()),
        )
        if hasattr(target, "apply_state_update"):
            target.apply_state_update(event)

    def _update_actuator_state(
        self, target: Any, payload: dict[str, Any], msg: Message
    ) -> None:
        """Translate actuator state opcodes into ActuatorState."""
        if msg.code not in (Code._3EF0, Code._3EF1):
            return

        updates: dict[str, Any] = {}
        if SZ_MODULATION_LEVEL in payload:
            # NOTE: semantic parser custom keys
            updates[SZ_MODULATION_LEVEL] = payload[SZ_MODULATION_LEVEL]
        elif SZ_REL_MODULATION_LEVEL in payload:
            updates[SZ_MODULATION_LEVEL] = payload[SZ_REL_MODULATION_LEVEL]

        if SZ_ACTUATOR_ENABLED in payload:
            updates[SZ_ACTUATOR_ENABLED] = payload[SZ_ACTUATOR_ENABLED]
        if SZ_CH_ACTIVE in payload:
            updates[SZ_CH_ACTIVE] = payload[SZ_CH_ACTIVE]
        if SZ_DHW_ACTIVE in payload:
            updates[SZ_DHW_ACTIVE] = payload[SZ_DHW_ACTIVE]
        if SZ_FLAME_ACTIVE in payload:
            updates[SZ_FLAME_ACTIVE] = payload[SZ_FLAME_ACTIVE]

        if SZ_ACTUATOR_COUNTDOWN in payload:
            updates[SZ_ACTUATOR_COUNTDOWN] = payload[SZ_ACTUATOR_COUNTDOWN]
        if SZ_CYCLE_COUNTDOWN in payload:
            updates[SZ_CYCLE_COUNTDOWN] = payload[SZ_CYCLE_COUNTDOWN]

        if not updates:
            return

        dtm = getattr(msg, "dtm", getattr(msg, SZ_TIMESTAMP, None))
        if dtm:
            updates[SZ_LAST_UPDATED] = dtm

        current_state = getattr(target, "act_state", None) or ActuatorState()
        new_state = dataclasses.replace(current_state, **updates)
        target.act_state = new_state

        event = StateUpdatedEvent(
            entity_id=getattr(target, "id", SZ_UNKNOWN),
            state=new_state,
            correlation_id=getattr(msg, "correlation_id", uuid.uuid4()),
            causation_id=getattr(msg, "message_id", uuid.uuid4()),
        )
        if hasattr(target, "apply_state_update"):
            target.apply_state_update(event)

    def _update_zone_state(
        self, target: Any, payload: dict[str, Any], msg: Message
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
            if SZ_NAME in payload:
                updates[SZ_NAME] = str(payload[SZ_NAME])

        elif msg.code == Code._000A:
            if SZ_MIN_TEMP in payload:
                updates[SZ_MIN_TEMP] = payload[SZ_MIN_TEMP]
            if SZ_MAX_TEMP in payload:
                updates[SZ_MAX_TEMP] = payload[SZ_MAX_TEMP]
            if SZ_LOCAL_OVERRIDE in payload:
                updates[SZ_LOCAL_OVERRIDE] = payload[SZ_LOCAL_OVERRIDE]
            if SZ_OPENWINDOW_FUNCTION in payload:
                updates[SZ_OPENWINDOW_FUNCTION] = payload[
                    SZ_OPENWINDOW_FUNCTION
                ]
            if SZ_MULTIROOM_MODE in payload:
                updates[SZ_MULTIROOM_MODE] = payload[SZ_MULTIROOM_MODE]

        elif msg.code == Code._2349:
            if SZ_MODE in payload:
                updates[SZ_MODE] = payload[SZ_MODE]
            if SZ_SETPOINT in payload:
                updates[SZ_SETPOINT] = payload[SZ_SETPOINT]
            if SZ_UNTIL in payload:
                updates[SZ_UNTIL] = payload[SZ_UNTIL]

        elif msg.code == Code._2309:
            if SZ_SETPOINT in payload:
                updates[SZ_SETPOINT] = payload[SZ_SETPOINT]

        else:
            return

        if not updates:
            return

        dtm = getattr(msg, "dtm", getattr(msg, SZ_TIMESTAMP, None))
        if dtm:
            updates[SZ_LAST_UPDATED] = dtm

        current_state = getattr(target, "zone_state", None) or ZoneState()
        new_state = dataclasses.replace(current_state, **updates)

        event = StateUpdatedEvent(
            entity_id=getattr(target, "id", SZ_UNKNOWN),
            state=new_state,
            correlation_id=getattr(msg, "correlation_id", uuid.uuid4()),
            causation_id=getattr(msg, "message_id", uuid.uuid4()),
        )
        if hasattr(target, "apply_state_update"):
            target.apply_state_update(event)
        else:
            target.zone_state = new_state  # noqa: B010
