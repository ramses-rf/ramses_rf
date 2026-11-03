#!/usr/bin/env python3
"""RAMSES RF - a RAMSES-II protocol decoder & analyser."""

from __future__ import annotations

from datetime import timedelta as td
from enum import IntEnum
from typing import Final

from ramses_tx.const import (
    DEFAULT_MAX_ZONES as DEFAULT_MAX_ZONES,
    DEV_MODE as DEV_MODE,
    DEVICE_ID_REGEX as DEVICE_ID_REGEX,
    DOMAIN_TYPE_MAP as DOMAIN_TYPE_MAP,
    F9 as F9,
    FA as FA,
    FAULT_DEVICE_CLASS as FAULT_DEVICE_CLASS,
    FAULT_STATE as FAULT_STATE,
    FAULT_TYPE as FAULT_TYPE,
    FC as FC,
    FF as FF,
    I_ as I_,
    LOOKUP_PUZZ as LOOKUP_PUZZ,
    RP as RP,
    RQ as RQ,
    W_ as W_,
    AttrDict as AttrDict,
    Code as Code,
    FaultDeviceClass as FaultDeviceClass,
    FaultState as FaultState,
    FaultType as FaultType,
    IndexT as IndexT,
    SystemType as SystemType,
    Verb as Verb,
    __dev_mode__ as __dev_mode__,
    attr_dict_factory as attr_dict_factory,
)

from .enums import DevRole as DevRole, DevType as DevType, ZoneRole as ZoneRole

SZ_ACTIVE: Final = "active"
SZ_ACTUATOR: Final = "actuator"
SZ_ACTUATOR_ENABLED: Final = "actuator_enabled"
SZ_ACTUATORS: Final = "actuators"
SZ_ACTUATOR_SYNC: Final = "actuator_sync"
# used by 1060
SZ_BATTERY_LEVEL: Final = "battery_level"
SZ_BATTERY_LOW: Final = "battery_low"
SZ_BATTERY_STATE: Final = "battery_state"
SZ_BASE: Final = "base"
SZ_BDR_STATE: Final = "bdr_state"
SZ_BINDINGS: Final = "bindings"
SZ_BOILER_EXHAUST: Final = "boiler_exhaust"
SZ_BOILER_OUTPUT: Final = "boiler_output"
SZ_BOILER_RETURN: Final = "boiler_return"
SZ_BOOST_TIMER_MINS: Final = "boost_timer_mins"
SZ_BYPASS_POSITION: Final = "bypass_position"
SZ_CH_ACTIVE: Final = "ch_active"
SZ_CH_ENABLED: Final = "ch_enabled"
SZ_CHILD_ID: Final = "child_id"
SZ_CIRCUIT_MODE: Final = "circuit_mode"
SZ_CIRCUIT_MODES: Final = "circuit_modes"
SZ_CIRCUIT_TO_ZONE_MAP: Final = "circuit_to_zone_map"
SZ_CIRCUITS: Final = "circuits"
SZ_CONFIG: Final = "config"
SZ_COOL_MODE: Final = "cool_mode"
SZ_COOLING_ACTIVE: Final = "cooling_active"
SZ_COOLING_DEMAND: Final = "cooling_demand"
SZ_COOLING_DEMANDS: Final = "cooling_demands"
SZ_COOLING_ENABLED: Final = "cooling_enabled"
SZ_COOLING_MODE: Final = "cooling_mode"
SZ_COUNTERS: Final = "counters"
SZ_CYCLE_RATE: Final = "cycle_rate"
SZ_DATETIME: Final = "datetime"
SZ_DHW: Final = "dhw"
SZ_DHW_ACTIVE: Final = "dhw_active"
SZ_DHW_BLOCKING: Final = "dhw_blocking"
SZ_DHW_ENABLED: Final = "dhw_enabled"
SZ_FAULT_PRESENT: Final = "fault_present"
SZ_FLAGS: Final = "flags"
SZ_FLAME_ACTIVE: Final = "flame_active"
SZ_OTC_ACTIVE: Final = "otc_active"
SZ_DEMAND: Final = "demand"
SZ_DEVICE_ID: Final = "device_id"
SZ_DEVICE_ROLE: Final = "device_role"
SZ_DEVICES: Final = "devices"
SZ_DHW_INDEX: Final = "dhw_index"
SZ_DHW_IDX: Final = SZ_DHW_INDEX
SZ_DIAGNOSTIC_CODE: Final = "diagnostic_code"
HW: Final = "HW"
DOMAIN_ID_HW: Final = HW
SZ_DIFFERENTIAL: Final = "differential"
SZ_DOMAIN_INDEX: Final = "domain_index"
SZ_DOMAIN_ID: Final = SZ_DOMAIN_INDEX
SZ_DOMAIN_IDX: Final = SZ_DOMAIN_INDEX
SZ_DOMAIN_ID_WIRE: Final = "domain_id"
SZ_DOMAIN_OR_ZONE_INDEX: Final = "domain_or_zone_index"
SZ_DURATION: Final = "duration"
SZ_FAILSAFE_ENABLED: Final = "failsafe_enabled"
SZ_FILTER_REMAINING_DAYS: Final = "filter_remaining_days"
SZ_FRAME_CODE: Final = "frame_code"
SZ_HEAT_DEMAND: Final = "heat_demand"
SZ_HEAT_DEMANDS: Final = "heat_demands"
SZ_HEAT_DEMAND_FAULT: Final = "heat_demand_fault"
SZ_HEAT_MODE: Final = "heat_mode"
SZ_HEAT_SETPOINT: Final = "heat_setpoint"
SZ_HVAC_ID: Final = "hvac_id"
SZ_IS_DAYLIGHT_SAVING: Final = "is_daylight_saving"
SZ_IS_DST: Final = SZ_IS_DAYLIGHT_SAVING
SZ_JIM_STATE: Final = "jim_state"
SZ_LANGUAGE: Final = "language"
SZ_LAST_UPDATED: Final = "last_updated"
SZ_LOCAL_OVERRIDE: Final = "local_override"
SZ_MAX_FLOW_SETPOINT: Final = "max_flow_setpoint"
SZ_MAX_TEMP: Final = "max_temp"
SZ_MESSAGE_ID: Final = "msg_id"
SZ_MSG_ID: Final = SZ_MESSAGE_ID
SZ_MIN_FLOW_SETPOINT: Final = "min_flow_setpoint"
SZ_MIN_OFF_TIME: Final = "min_off_time"
SZ_MIN_ON_TIME: Final = "min_on_time"
SZ_MIN_TEMP: Final = "min_temp"
SZ_MODE: Final = "mode"
SZ_MODULATION_LEVEL: Final = "modulation_level"
SZ_MULTIROOM_MODE: Final = "multiroom_mode"
SZ_NAME: Final = "name"
SZ_OEM_CODE: Final = "oem_code"
SZ_OPENWINDOW_FUNCTION: Final = "openwindow_function"
SZ_OPENTHERM_PARAMS: Final = "opentherm_params"
SZ_OPENTHERM_SCHEMA: Final = "opentherm_schema"
SZ_OPENTHERM_STATE: Final = "opentherm_state"
SZ_OTHER_INDEX: Final = "other_index"
SZ_OTHER_IDX: Final = SZ_OTHER_INDEX
SZ_OUTSIDE: Final = "outside"
SZ_OVERRUN: Final = "overrun"
SZ_PARAMETER_ID: Final = "parameter_id"
SZ_PARAM_ID: Final = SZ_PARAMETER_ID
SZ_PARAMETER_INDEX: Final = "parameter_index"
SZ_PARAM_IDX: Final = SZ_PARAMETER_INDEX
SZ_PARAMETER_VALUE: Final = "parameter_value"
SZ_PARAM_VAL: Final = SZ_PARAMETER_VALUE
SZ_PAYLOAD: Final = "payload"
SZ_PRESSURE: Final = "pressure"
SZ_PROPORTIONAL_BAND_WIDTH: Final = "proportional_band_width"
SZ_PUMP_RUN_TIME: Final = "pump_run_time"
SZ_PUMP_RELAY_STATE: Final = "pump_relay_state"
SZ_RAMSES_II_PARAMS: Final = "ramses_ii_params"
SZ_RAMSES_II_SCHEMA: Final = "ramses_ii_schema"
SZ_RAW_VALUE: Final = "raw_value"
SZ_RELAY_DEMAND: Final = "relay_demand"
SZ_RELAY_DEMAND_FA: Final = "relay_demand_fa"
SZ_RELAY_DEMAND_FC: Final = "relay_demand_fc"
SZ_RELAY_FAILSAFE: Final = "relay_failsafe"
SZ_REQUEST_FAN_SPEED: Final = "request_fan_speed"
SZ_SENSOR: Final = "sensor"
SZ_SETPOINT: Final = "setpoint"
SZ_SETPOINTS: Final = "setpoints"
SZ_SETPOINT_BOUNDS: Final = "setpoint_bounds"
SZ_SETPOINT_INDEX: Final = "setpoint_index"
SZ_SETPOINT_IDX: Final = SZ_SETPOINT_INDEX
SZ_STATE: Final = "state"
SZ_STATUS_CODE: Final = "status_code"
SZ_SYSTEM_MODE: Final = "system_mode"
SZ_SYNC_FLAG: Final = "sync_flag"
SZ_TEMPERATURE: Final = "temperature"
SZ_TEMPERATURES: Final = "temperatures"
SZ_TEMP_HIGH: Final = "temp_high"
SZ_TEMP_LOW: Final = "temp_low"
SZ_THERMAL_DEMAND: Final = "thermal_demand"
SZ_THERMAL_DEMANDS: Final = "thermal_demands"
SZ_TICKER: Final = "ticker"
SZ_UFH_INDEX: Final = "ufh_index"
SZ_UFH_IDX: Final = SZ_UFH_INDEX
SZ_CIRCUIT_INDEX: Final = SZ_UFH_INDEX
SZ_UNKNOWN: Final = "unknown"
SZ_UNTIL: Final = "until"
SZ_VALVE_RUN_TIME: Final = "valve_run_time"
SZ_VALUE: Final = "value"
SZ_WINDOW_OPEN: Final = "window_open"
SZ_ZONE_CLASS: Final = "zone_class"
SZ_ZONE_INDEX: Final = "zone_index"
SZ_ZONE_IDX: Final = SZ_ZONE_INDEX
SZ_ZONE_MASK: Final = "zone_mask"
SZ_ZONE_TYPE: Final = "zone_type"
SZ_ZONES: Final = "zones"

# used in 0418 only?
SZ_CONFIG_INDEX: Final = "config_index"
SZ_CONFIG_IDX: Final = SZ_CONFIG_INDEX
SZ_CONFIG_VALUE: Final = "config_value"
SZ_CONFIG_VAL: Final = SZ_CONFIG_VALUE
SZ_DEVICE_CLASS: Final = "device_class"
SZ_FAULT_STATE: Final = "fault_state"
SZ_FAULT_TYPE: Final = "fault_type"
SZ_LOG_ENTRY: Final = "log_entry"
SZ_LOG_INDEX: Final = "log_index"
SZ_LOG_IDX: Final = SZ_LOG_INDEX
SZ_TIMESTAMP: Final = "timestamp"

# used in 1FC9
SZ_OFFER: Final = "offer"
SZ_ACCEPT: Final = "accept"
SZ_CONFIRM: Final = "confirm"
SZ_PHASE: Final = "phase"

# used by schedule.py...
SZ_FRAGMENT: Final = "fragment"
SZ_FRAGMENT_NUMBER: Final = "fragment_number"
SZ_FRAG_NUMBER: Final = SZ_FRAGMENT_NUMBER
SZ_FRAGMENT_LENGTH: Final = "fragment_length"
SZ_FRAG_LENGTH: Final = SZ_FRAGMENT_LENGTH
SZ_TOTAL_FRAGMENTS: Final = "total_fragments"
SZ_TOTAL_FRAGS: Final = SZ_TOTAL_FRAGMENTS

SZ_SCHEDULE: Final = "schedule"
SZ_CHANGE_COUNTER: Final = "change_counter"
SZ_DAY_OF_WEEK: Final = "day_of_week"
SZ_SWITCHPOINTS: Final = "switchpoints"
SZ_TIME_OF_DAY: Final = "time_of_day"
SZ_ENABLED: Final = "enabled"

SZ_SENSOR_FAULT: Final = "sensor_fault"


# used by 31DA (HVAC)
SZ_AIR_QUALITY: Final = "air_quality"
SZ_AIR_QUALITY_BASIS: Final = "air_quality_basis"
SZ_BOOST_TIMER: Final = "boost_timer"
SZ_BYPASS_MODE: Final = "bypass_mode"
SZ_BYPASS_STATE: Final = "bypass_state"
SZ_CO2_LEVEL: Final = "co2_level"
SZ_CO2_LEVEL_FAULT: Final = "co2_level_fault"
SZ_DEWPOINT_TEMP: Final = "dewpoint_temp"
SZ_EXHAUST_FAN_SPEED: Final = "exhaust_fan_speed"
SZ_EXHAUST_FLOW: Final = "exhaust_flow"
SZ_EXHAUST_TEMP: Final = "exhaust_temp"
SZ_FAN_INFO: Final = "fan_info"
SZ_FAN_MODE: Final = "fan_mode"
SZ_FAN_RATE: Final = "fan_rate"
SZ_FILTER_DIRTY: Final = "filter_dirty"
SZ_FROST_CYCLE: Final = "frost_cycle"
SZ_HAS_FAULT: Final = "has_fault"
SZ_FILTER_REMAINING: Final = "filter_remaining"
SZ_FILTER_REMAINING_PERCENT: Final = "filter_remaining_percent"
SZ_INDOOR_HUMIDITY: Final = "indoor_humidity"
SZ_INDOOR_TEMP: Final = "indoor_temp"
SZ_MINUTES: Final = "minutes"
SZ_OUTDOOR_HUMIDITY: Final = "outdoor_humidity"
SZ_OUTDOOR_TEMP: Final = "outdoor_temp"
SZ_POST_HEAT: Final = "post_heat"
SZ_PRE_HEAT: Final = "pre_heat"
SZ_PRESENCE_DETECTED: Final = "presence_detected"
SZ_RELATIVE_HUMIDITY: Final = "relative_humidity"
SZ_REL_HUMIDITY: Final = SZ_RELATIVE_HUMIDITY
SZ_REMAINING_DAYS: Final = "days_remaining"
SZ_REMAINING_MINS: Final = "remaining_mins"
SZ_REMAINING_PERCENT: Final = "percent_remaining"
SZ_REQUEST_REASON: Final = "request_reason"
SZ_REQ_REASON: Final = "req_reason"
SZ_REQUEST_SPEED: Final = "request_speed"
SZ_REQ_SPEED: Final = "req_speed"
SZ_SUPPLY_FAN_SPEED: Final = "supply_fan_speed"
SZ_SUPPLY_FLOW: Final = "supply_flow"
SZ_SUPPLY_TEMP: Final = "supply_temp"
SZ_SPEED_CAPABILITIES: Final = "speed_capabilities"


# used by OTB (OpenTherm Bridge)
SZ_BURNER_HOURS: Final = "burner_hours"
SZ_BURNER_STARTS: Final = "burner_starts"
SZ_BURNER_FAILED_STARTS: Final = "burner_failed_starts"
SZ_CH_PUMP_HOURS: Final = "ch_pump_hours"
SZ_CH_PUMP_STARTS: Final = "ch_pump_starts"
SZ_DHW_BURNER_HOURS: Final = "dhw_burner_hours"
SZ_DHW_BURNER_STARTS: Final = "dhw_burner_starts"
SZ_DHW_PUMP_HOURS: Final = "dhw_pump_hours"
SZ_DHW_PUMP_STARTS: Final = "dhw_pump_starts"
SZ_FLAME_SIGNAL_LOW: Final = "flame_signal_low"

SZ_BOILER_EXHAUST_TEMP: Final = "boiler_exhaust_temp"
SZ_BOILER_OUTPUT_TEMP: Final = "boiler_output_temp"
SZ_BOILER_RETURN_TEMP: Final = "boiler_return_temp"
SZ_BOILER_SETPOINT: Final = "boiler_setpoint"
SZ_CH_MAX_SETPOINT: Final = "ch_max_setpoint"
SZ_CH_SETPOINT: Final = "ch_setpoint"
SZ_CH_WATER_PRESSURE: Final = "ch_water_pressure"
SZ_DHW_FLOW_RATE: Final = "dhw_flow_rate"
SZ_DHW_SETPOINT: Final = "dhw_setpoint"
SZ_DHW_TEMP: Final = "dhw_temp"
SZ_MAX_REL_MODULATION: Final = "max_rel_modulation"
# SZ_OEM_CODE:Final[str] = "oem_code"
SZ_OEM_FAULT_CODE: Final = "oem_fault_code"
SZ_OUTSIDE_TEMP: Final = "outside_temp"
SZ_REL_MODULATION_LEVEL: Final = "rel_modulation_level"
SZ_SUMMER_MODE: Final = "summer_mode"

# OpenTherm application-specific fault flags (msg_id 0x05)
SZ_SERVICE_REQUEST: Final = "service_request"
SZ_LOCKOUT_RESET: Final = "lockout_reset"
SZ_LOW_WATER_PRESSURE: Final = "low_water_pressure"
SZ_GAS_FLAME_FAULT: Final = "gas_flame_fault"
SZ_AIR_PRESSURE_FAULT: Final = "air_pressure_fault"
SZ_WATER_OVER_TEMPERATURE: Final = "water_over_temperature"


# used by heat actuators / cycling
SZ_ACTUATOR_COUNTDOWN: Final = "actuator_countdown"
SZ_CYCLE_COUNTDOWN: Final = "cycle_countdown"


class Discover(IntEnum):
    """Flags for the discovery process."""

    NOTHING = 0
    SCHEMA = 1
    PARAMS = 2
    STATUS = 4
    FAULTS = 8
    SCHEDS = 16
    TRAITS = 32
    DEFAULT = 1 + 2 + 4


DONT_CREATE_MESSAGES: Final[int] = 3
DONT_CREATE_ENTITIES: Final[int] = 2
DONT_UPDATE_ENTITIES: Final[int] = 1

SZ_DISABLE_POLLING: Final = "disable_polling"
SZ_DISABLE_DISCOVERY: Final = "disable_discovery"
SZ_HEARTBEAT_TIMEOUT: Final = "heartbeat_timeout"
SZ_IS_BATTERY: Final = "is_battery"
SZ_POLLING_INTERVAL: Final = "polling_interval"

SCHED_REFRESH_INTERVAL: Final[int] = 3  # minutes

# 0004 (zone_name) and 1060 (device_battery) are intentionally excluded:
# they are low-volume (sent every ~6h and ~1/day respectively) and carry
# semi-static state.  Classifying them as high-volume caused cached packets
# older than 1h to be skipped during _restore_cached_packets, so zone names
# were lost on restart (issue 822) and battery states went Unknown after
# restart (issue 840 — battery devices send 1060 only 1/day, so the cached
# 1060 is almost always >1h old and was silently dropped on restore).
# See:
#   - https://github.com/ramses-rf/ramses_cc/issues/822  (0004 zone_name)
#   - https://github.com/ramses-rf/ramses_cc/issues/840  (1060 battery)
HIGH_VOLUME_STATUS_CODES: Final = (
    Code._2309,
    Code._2349,
    Code._30C9,
)

# Status codes for Worcester Bosch boilers - OT|OEM diagnostic code
WB_STATUS_CODES: Final[dict[str, str]] = {
    "200": "CH system is being heated.",
    "201": "DHW system is being heated.",
    "202": (
        "Anti rapid cycle mode. The boiler has commenced anti-cycle period for CH."
    ),
    "203": "System standby mode.",
    "204": "System waiting, appliance waiting for heating system to cool.",
    "208": "Appliance in service Test mode (Min/Max)",
    "265": (
        "EMS controller has forced stand-by-mode due to low heating "
        "load (power required is less than the minimum output)"
    ),
    "268": (
        "Component test mode (is running the manual component test as "
        "activated in the menus)."
    ),
    "270": "Power up mode (appliance is powering up).",
    "283": "Burner starting. The fan and the pump are being controlled.",
    "284": (
        "Gas valve(s) opened, flame must be detected within safety "
        "time. The gas valve is being controlled."
    ),
    "305": (
        "Anti fast cycle mode (DHW keep warm function). Diverter valve "
        "is held in DHW position for a period of time after DHW demand."
    ),
    "357": (
        "Appliance in air purge mode. Primary heat exchanger air "
        "venting program active - approximately 100 seconds."
    ),
    "358": (
        "Three way valve kick. If the 3-way valve hasn't moved in "
        "within 48 hours, the valve will operate once to prevent "
        "seizure"
    ),
}

# Device Availability Timeouts
HEARTBEAT_TIMEOUT_DEFAULT = td(hours=1)
HEARTBEAT_TIMEOUT_BDR = td(
    hours=24
)  # BDR91: mains relay, only transmits 3EF0 on state change
HEARTBEAT_TIMEOUT_DHW = td(
    hours=24
)  # CS92A: battery DHW sensor, polled every 24h by CTL
HEARTBEAT_TIMEOUT_FILTER = td(hours=24)
HEARTBEAT_TIMEOUT_FAN = td(
    minutes=15
)  # HVAC FANs broadcast 31D9/31DA every ~90s; ~10 missed cycles
HEARTBEAT_TIMEOUT_OTB = td(hours=24)
HEARTBEAT_TIMEOUT_TRV = td(hours=12)
HEARTBEAT_TIMEOUT_REMOTE = td(hours=24)
HEARTBEAT_TIMEOUT_SENSOR = td(hours=12)
GATEWAY_MESSAGE_TIMEOUT: td = td(minutes=10)


DEV_ROLE_MAP = attr_dict_factory(
    {
        DevRole.ACT: {"00": "zone_actuator"},
        DevRole.SEN: {"04": "zone_sensor"},
        DevRole.RAD: {"08": "rad_actuator"},
        DevRole.UFH: {"09": "ufh_actuator"},
        DevRole.VAL: {"0A": "val_actuator"},
        DevRole.MIX: {"0B": "mix_actuator"},
        DevRole.OUT: {"0C": "out_sensor"},
        DevRole.DHW: {"0D": "dhw_sensor"},
        DevRole.HTG: {"0E": "hotwater_valve"},  # payload[:4] == 000E
        DevRole.HT1: {None: "heating_valve"},  # payload[:4] == 010E
        DevRole.APP: {"0F": "appliance_control"},  # the heat/cool source
        DevRole.RFG: {"10": "remote_gateway"},
        DevRole.ELE: {
            "11": "ele_actuator"
        },  # ELE(VAL) - no RP from older evos
    },
    {
        "HEAT_DEVICES": ("00", "04", "08", "09", "0A", "0B", "11"),
        "DHW_DEVICES": ("0D", "0E"),
        "SENSORS": ("04", "0C", "0D"),
    },
)


DEV_TYPE_MAP = attr_dict_factory(
    {
        # Generic devices (would be promoted)
        DevType.DEV: {None: "generic_device"},
        DevType.HEA: {None: "heat_device"},
        DevType.HVC: {None: "hvac_device"},
        # HGI80
        DevType.HGI: {"18": "gateway_interface"},
        # Heat (CH/DHW) devices
        DevType.TR0: {
            "00": "radiator_valve",
            AttrDict._SZ_AKA_SLUG: DevType.TRV,
        },
        DevType.CTL: {"01": "controller"},
        DevType.UFC: {"02": "ufh_controller"},
        DevType.HCW: {"03": "analog_thermostat"},
        DevType.THM: {None: "thermostat"},
        DevType.TRV: {"04": "radiator_valve"},
        DevType.DHW: {"07": "dhw_sensor"},
        DevType.OTB: {"10": "opentherm_bridge"},
        DevType.DTS: {"12": "digital_thermostat"},
        DevType.BDR: {"13": "electrical_relay"},
        DevType.OUT: {"17": "outdoor_sensor"},
        DevType.DT2: {
            "22": "digital_thermostat",
            AttrDict._SZ_AKA_SLUG: DevType.DTS,
        },
        DevType.PRG: {"23": "programmer"},
        DevType.RFG: {"30": "rf_gateway"},
        DevType.RND: {"34": "round_thermostat"},
        # Other (jasper) devices
        DevType.JIM: {"08": "jasper_interface"},
        DevType.JST: {"31": "jasper_thermostat"},
        # Ventilation devices
        DevType.CO2: {None: "co2_sensor"},
        DevType.DIS: {None: "switch_display"},
        DevType.FAN: {None: "ventilator"},
        DevType.HUM: {None: "rh_sensor"},
        DevType.PIR: {None: "presence_sensor"},
        DevType.RFS: {None: "hvac_gateway"},
        DevType.REM: {None: "switch"},
        DevType.SW2: {None: "switch_variant"},
    },
    {
        "HEAT_DEVICES": (
            "00",
            "01",
            "02",
            "03",
            "04",
            "07",
            "10",
            "12",
            "13",
            "17",
            "22",
            "30",
            "34",
        ),
        "HEAT_ZONE_SENSORS": ("00", "01", "03", "04", "12", "22", "34"),
        "HEAT_ZONE_ACTUATORS": ("00", "02", "04", "13"),
        "THM_DEVICES": ("03", "12", "21", "22", "34"),
        "TRV_DEVICES": ("00", "04"),
        "CONTROLLERS": (
            "01",
            "02",
            "12",
            "22",
            "23",
            "30",
            "34",
        ),
        "PROMOTABLE_SLUGS": (DevType.DEV, DevType.HEA, DevType.HVC),
        "HVAC_SLUGS": {
            DevType.CO2: "co2_sensor",
            DevType.FAN: "ventilator",
            DevType.HUM: "rh_sensor",
            DevType.RFS: "hvac_gateway",
            DevType.REM: "switch",
            DevType.DIS: "switch_display",
            DevType.SW2: "switch_variant",
            DevType.PIR: "presence_sensor",
        },
    },
)


ZON_ROLE_MAP = attr_dict_factory(
    {
        ZoneRole.ACT: {"00": "heating_zone"},
        ZoneRole.SEN: {"04": "heating_zone"},
        ZoneRole.RAD: {"08": "radiator_valve"},
        ZoneRole.UFH: {"09": "underfloor_heating"},
        ZoneRole.VAL: {"0A": "zone_valve"},
        ZoneRole.MIX: {"0B": "mixing_valve"},
        ZoneRole.DHW: {"0D": "stored_hotwater"},
        ZoneRole.ELE: {"11": "electric_heat"},
    },
    {
        "HEAT_ZONES": ("08", "09", "0A", "0B", "11"),
    },
)


ZON_MODE_MAP = attr_dict_factory(
    {
        "FOLLOW": {"00": "follow_schedule"},
        "ADVANCED": {"01": "advanced_override"},
        "PERMANENT": {"02": "permanent_override"},
        "COUNTDOWN": {"03": "countdown_override"},
        "TEMPORARY": {"04": "temporary_override"},
    }
)


SYS_MODE_MAP = attr_dict_factory(
    {
        "au_00": {"00": "auto"},
        "ho_01": {"01": "heat_off"},
        "eb_02": {"02": "eco_boost"},
        "aw_03": {"03": "away"},
        "do_04": {"04": "day_off"},
        "de_05": {"05": "day_off_eco"},
        "ar_06": {"06": "auto_with_reset"},
        "cu_07": {"07": "custom"},
    }
)
