#!/usr/bin/env python3
"""RAMSES RF - a RAMSES-II protocol decoder & analyser."""

from __future__ import annotations

import re
from enum import EnumCheck, IntEnum, StrEnum, verify
from types import SimpleNamespace
from typing import Any, Final, Literal, NoReturn

__dev_mode__ = False  # NOTE: this is const.py
DEV_MODE = __dev_mode__

# used by protocol QoS FSM (echo tout is different? for MQTT)...
DEFAULT_DISABLE_QOS: Final[bool | None] = None
DEFAULT_WAIT_FOR_REPLY: Final[bool | None] = None

DEFAULT_ECHO_TIMEOUT: Final[float] = 0.50  # waiting for echo pkt after cmd sent
DEFAULT_RPLY_TIMEOUT: Final[float] = 0.50  # waiting for reply pkt after echo pkt rcvd
DEFAULT_BUFFER_SIZE: Final[int] = 32

DEFAULT_SEND_TIMEOUT: Final[float] = 20.0  # total waiting for successful send: FIXME
MAX_SEND_TIMEOUT: Final[float] = 20.0  # for a command to be sent, incl. queuing time

MAX_RETRY_LIMIT: Final[int] = 3  # for a command to be re-sent (not incl. 1st send)

MIN_INTER_WRITE_GAP: Final[float] = 0.05  # seconds
DEFAULT_GAP_DURATION: Final[float] = MIN_INTER_WRITE_GAP
DEFAULT_MAX_RETRIES: Final[int] = 3
DEFAULT_NUM_REPEATS: Final[int] = 0

SZ_QOS: Final = "qos"

SZ_CALLBACK: Final = "callback"
SZ_GAP_DURATION: Final = "gap_duration"
SZ_MAX_RETRIES: Final = "max_retries"
SZ_NUM_REPEATS: Final = "num_repeats"
SZ_PRIORITY: Final = "priority"
SZ_TIMEOUT: Final = "timeout"


# used by transport...
SZ_ACTIVE_HGI: Final = "active_gwy"
SZ_SIGNATURE: Final = "signature"
SZ_IS_EVOFW3: Final = "is_evofw3"

# default values for transmit rate governers...
DUTY_CYCLE_DURATION = 60  #      time window (seconds) where rate limiting occurs
MAX_DUTY_CYCLE_RATE = 0.01  #    % bandwidth used per cycle
MAX_TRANSMIT_RATE_TOKENS = 80  # transmits per cycle


# used by schedule.py...
SZ_FRAGMENT: Final = "fragment"
SZ_FRAG_NUMBER: Final = "frag_number"
SZ_FRAG_LENGTH: Final = "frag_length"
SZ_TOTAL_FRAGS: Final = "total_frags"

SZ_SCHEDULE: Final = "schedule"
SZ_CHANGE_COUNTER: Final = "change_counter"

SZ_SENSOR_FAULT: Final = "sensor_fault"


# used by 31DA
SZ_AIR_QUALITY: Final = "air_quality"
SZ_AIR_QUALITY_BASIS: Final = "air_quality_basis"
SZ_BOOST_TIMER: Final = "boost_timer"
SZ_BYPASS_MODE: Final = "bypass_mode"
SZ_BYPASS_POSITION: Final = "bypass_position"
SZ_BYPASS_STATE: Final = "bypass_state"
SZ_CO2_LEVEL: Final = "co2_level"
SZ_DEWPOINT_TEMP: Final = "dewpoint_temp"
SZ_EXHAUST_FAN_SPEED: Final = "exhaust_fan_speed"
SZ_EXHAUST_FLOW: Final = "exhaust_flow"
SZ_EXHAUST_TEMP: Final = "exhaust_temp"
SZ_FAN_INFO: Final = "fan_info"
SZ_FAN_MODE: Final = "fan_mode"
SZ_FAN_RATE: Final = "fan_rate"
SZ_FILTER_REMAINING: Final = "filter_remaining"
SZ_INDOOR_HUMIDITY: Final = "indoor_humidity"
SZ_INDOOR_TEMP: Final = "indoor_temp"
SZ_OUTDOOR_HUMIDITY: Final = "outdoor_humidity"
SZ_OUTDOOR_TEMP: Final = "outdoor_temp"
SZ_POST_HEAT: Final = "post_heat"
SZ_PRE_HEAT: Final = "pre_heat"
SZ_REL_HUMIDITY: Final = "rel_humidity"
SZ_REMAINING_DAYS: Final = "days_remaining"
SZ_REMAINING_MINS: Final = "remaining_mins"
SZ_REMAINING_PERCENT: Final = "percent_remaining"
SZ_REQ_REASON: Final = "req_reason"
SZ_REQ_SPEED: Final = "req_speed"
SZ_SUPPLY_FAN_SPEED: Final = "supply_fan_speed"
SZ_SUPPLY_FLOW: Final = "supply_flow"
SZ_SUPPLY_TEMP: Final = "supply_temp"
SZ_SPEED_CAPABILITIES: Final = "speed_capabilities"

SZ_PRESENCE_DETECTED: Final = "presence_detected"


# used by OTB
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
SZ_OUTSIDE_TEMP: Final = "outside_temp"
SZ_REL_MODULATION_LEVEL: Final = "rel_modulation_level"

SZ_CH_ACTIVE: Final = "ch_active"
SZ_CH_ENABLED: Final = "ch_enabled"
SZ_COOLING_ACTIVE: Final = "cooling_active"
SZ_COOLING_ENABLED: Final = "cooling_enabled"
SZ_DHW_ACTIVE: Final = "dhw_active"
SZ_DHW_BLOCKING: Final = "dhw_blocking"
SZ_DHW_ENABLED: Final = "dhw_enabled"
SZ_FAULT_PRESENT: Final = "fault_present"
SZ_FLAME_ACTIVE: Final = "flame_active"
SZ_SUMMER_MODE: Final = "summer_mode"
SZ_OTC_ACTIVE: Final = "otc_active"


@verify(EnumCheck.UNIQUE)
class Priority(IntEnum):
    LOWEST = 4
    LOW = 2
    DEFAULT = 0
    HIGH = -2
    HIGHEST = -4


def slug(string: str) -> str:
    """Convert a string to snake_case."""
    return re.sub(r"[\W_]+", "_", string.lower())


# TODO: FIXME: This is a mess - needs converting to StrEnum
class AttrDict(dict):  # type: ignore[type-arg]
    _SZ_AKA_SLUG: Final = "_root_slug"
    _SZ_DEFAULT: Final = "_default"
    _SZ_SLUGS: Final = "SLUGS"

    @classmethod
    def __readonly(cls, *args: Any, **kwargs: Any) -> NoReturn:
        raise TypeError(f"'{cls.__class__.__name__}' object is read only")

    __delitem__ = __readonly
    __setitem__ = __readonly
    clear = __readonly
    pop = __readonly
    popitem = __readonly
    setdefault = __readonly
    update = __readonly

    del __readonly

    def __init__(self, main_table: dict[str, dict], attr_table: dict[str, Any]) -> None:  # type: ignore[type-arg]
        self._main_table = main_table
        self._attr_table = attr_table
        self._attr_table[self._SZ_SLUGS] = tuple(sorted(main_table.keys()))

        self._slug_lookup: dict = {  # type: ignore[type-arg]
            None: slug  # noqa: B035
            for slug, table in main_table.items()
            for k in table.values()
            if isinstance(k, str) and table.get(self._SZ_DEFAULT)
        }  # i.e. {None: 'HEA'}
        self._slug_lookup.update(
            {
                k: table.get(self._SZ_AKA_SLUG, slug)
                for slug, table in main_table.items()
                for k in table
                if isinstance(k, str) and len(k) == 2
            }  # e.g. {'00': 'TRV', '01': 'CTL', '04': 'TRV', ...}
        )
        self._slug_lookup.update(
            {
                k: slug
                for slug, table in main_table.items()
                for k in table.values()
                if isinstance(k, str) and table.get(self._SZ_AKA_SLUG) is None
            }  # e.g. {'heat_device':'HEA', 'dhw_sensor':'DHW', ...}
        )

        self._forward = {
            k: v
            for table in main_table.values()
            for k, v in table.items()
            if isinstance(k, str) and k[:1] != "_"
        }  # e.g. {'00': 'radiator_valve', '01': 'controller', ...}
        self._reverse = {
            v: k
            for table in main_table.values()
            for k, v in table.items()
            if isinstance(k, str) and k[:1] != "_" and self._SZ_AKA_SLUG not in table
        }  # e.g. {'radiator_valve': '00', 'controller': '01', ...}
        self._forward = dict(sorted(self._forward.items(), key=lambda item: item[0]))

        super().__init__(self._forward)

    def __getitem__(self, key: str) -> Any:
        if key in self._main_table:  # map[ZON_ROLE.DHW] -> "dhw_sensor"
            return list(self._main_table[key].values())[0]
        # if key in self._forward:  # map["0D"] -> "dhw_sensor"
        #     return self._forward.__getitem__(key)
        if key in self._reverse:  # map["dhw_sensor"] -> "0D"
            return self._reverse.__getitem__(key)
        return super().__getitem__(key)

    def __getattr__(self, name: str) -> Any:
        if name in self._main_table:  # map.DHW -> "0D" (using slug)
            if (result := list(self._main_table[name].keys())[0]) is not None:
                return result
        elif name in self._attr_table:  # bespoke attrs
            return self._attr_table[name]
        elif len(name) and name[1:] in self._forward:  # map._0D -> "dhw_sensor"
            return self._forward[name[1:]]
        elif name.isupper() and name.lower() in self._reverse:  # map.DHW_SENSOR -> "0D"
            return self[name.lower()]
        return self.__getattribute__(name)

    def _hex(self, key: str) -> str:
        """Return the key/ID (2-byte hex string) of the two-way dict (e.g. '04')."""
        if key in self._main_table:
            return list(self._main_table[key].keys())[0]  # type: ignore[no-any-return]
        if key in self._reverse:
            return self._reverse[key]
        raise KeyError(key)

    def _str(self, key: str) -> str:
        """Return the value (string) of the two-way dict (e.g. 'radiator_valve')."""
        if key in self._main_table:
            return list(self._main_table[key].values())[0]  # type: ignore[no-any-return]
        if key in self:
            return self[key]  # type: ignore[no-any-return]
        raise KeyError(key)

    # def values(self):
    #     return {k: k for k in super().values()}.values()

    def slug(self, key: str) -> str:
        """WIP: Return master slug for a hex key/ID (e.g. 00 -> 'TRV', not 'TR0')."""
        slug_ = self._slug_lookup[key]
        # if slug_ in self._attr_table["_TRANSFORMS"]:
        #     return self._attr_table["_TRANSFORMS"][slug_]
        return slug_  # type: ignore[no-any-return]

    def slugs(self) -> tuple[str]:
        """Return the slugs from the main table."""
        return self._attr_table[self._SZ_SLUGS]  # type: ignore[no-any-return]


def attr_dict_factory(
    main_table: dict[str, dict],  # type: ignore[type-arg]
    attr_table: dict | None = None,  # type: ignore[type-arg]
) -> AttrDict:  # is: SlottedAttrDict
    if attr_table is None:
        attr_table = {}

    class SlottedAttrDict(AttrDict):
        pass  # TODO: low priority
        # __slots__ = (
        #     list(main_table.keys())
        #     + [
        #         f"_{k}"
        #         for t in main_table.values()
        #         for k in t.keys()
        #         if isinstance(k, str) and len(k) == 2
        #     ]
        #     + [v for t in main_table.values() for v in t.values()]
        #         + list(attr_table.keys())
        #         + [AttrDict._SZ_AKA_SLUG, AttrDict._SZ_SLUGS]
        # )

    return SlottedAttrDict(main_table, attr_table=attr_table)


# slugs for device/zone entity klasses, used by 0005/000C
@verify(EnumCheck.UNIQUE)
class DevRole(StrEnum):
    #
    # Generic device/zone classes
    ACT = "ACT"  # Generic heating zone actuator group
    SEN = "SEN"  # Generic heating zone sensor group
    #
    # Standard device/zone classes
    ELE = "ELE"  # BDRs (no heat demand)
    MIX = "MIX"  # HM8s
    RAD = "RAD"  # TRVs
    UFH = "UFH"  # UFC (circuits)
    VAL = "VAL"  # BDRs
    #
    # DHW device/zone classes
    DHW = "DHW"  # DHW sensor (a zone, but not a heating zone)
    HTG = "HTG"  # BDR (DHW relay, HTG relay)
    HT1 = "HT1"  # BDR (HTG relay)
    #
    # Other device/zone classes
    OUT = "OUT"  # OUT (external weather sensor)
    RFG = "RFG"  # RFG
    APP = "APP"  # BDR/OTB (appliance relay)


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
        DevRole.ELE: {"11": "ele_actuator"},  # ELE(VAL) - no RP from older evos
    },  # 03, 05, 06, 07: & >11 - no response from an 01:
    {
        "HEAT_DEVICES": ("00", "04", "08", "09", "0A", "0B", "11"),
        "DHW_DEVICES": ("0D", "0E"),
        "SENSORS": ("04", "0C", "0D"),
    },
)


# slugs for device entity types, used in device_ids
@verify(EnumCheck.UNIQUE)
class DevType(StrEnum):
    #
    # Promotable/Generic devices
    DEV = "DEV"  # xx: Promotable device
    HEA = "HEA"  # xx: Promotable Heat device, aka CH/DHW device
    HVC = "HVC"  # xx: Promotable HVAC device
    THM = "THM"  # xx: Generic thermostat
    #
    # Heat (CH/DHW) devices
    BDR = "BDR"  # 13: Electrical relay
    CTL = "CTL"  # 01: Controller (zoned)
    DHW = "DHW"  # 07: DHW sensor
    DTS = "DTS"  # 12: Thermostat, DTS92(E)
    DT2 = "DT2"  # 22: Thermostat, DTS92(E)
    HCW = "HCW"  # 03: Thermostat - don't use STA
    HGI = "HGI"  # 18: Gateway interface (RF to USB), HGI80
    # 8 = "HM8"  # xx: HM80 mixer valve (Rx-only, does not Tx)
    OTB = "OTB"  # 10: OpenTherm bridge
    OUT = "OUT"  # 17: External weather sensor
    PRG = "PRG"  # 23: Programmer
    RFG = "RFG"  # 30: RF gateway (RF to ethernet), RFG100
    RND = "RND"  # 34: Thermostat, TR87RF
    TRV = "TRV"  # 04: Thermostatic radiator valve
    TR0 = "TR0"  # 00: Thermostatic radiator valve
    UFC = "UFC"  # 02: UFH controller
    #
    # Honeywell Jasper, other Heat devices
    JIM = "JIM"  # 08: Jasper Interface Module (EIM?)
    JST = "JST"  # 31: Jasper Stat
    #
    # HVAC devices, these are more like classes (i.e. no reliable device type)
    RFS = "RFS"  # ??: HVAC spIDer gateway
    FAN = "FAN"  # ??: HVAC fan, 31D[9A]: 20|29|30|37 (some, e.g. 29: only 31D9)
    CO2 = "CO2"  # ??: HVAC CO2 sensor
    HUM = "HUM"  # ??: HVAC humidity sensor, 1260: 32
    PIR = "PIR"  # ??: HVAC pesence sensor, 2E10
    REM = "REM"  # ??: HVAC switch, 22F[13]: 02|06|20|32|39|42|49|59 (no 20: are both)
    SW2 = "SW2"  # ??: HVAC switch, Orcon variant
    DIS = "DIS"  # ??: HVAC switch with display


DEV_TYPE_MAP = attr_dict_factory(
    {
        # Generic devices (would be promoted)
        DevType.DEV: {None: "generic_device"},  # , AttrDict._SZ_DEFAULT: True},
        DevType.HEA: {None: "heat_device"},
        DevType.HVC: {None: "hvac_device"},
        # HGI80
        DevType.HGI: {"18": "gateway_interface"},  # HGI80
        # Heat (CH/DHW) devices
        DevType.TR0: {"00": "radiator_valve", AttrDict._SZ_AKA_SLUG: DevType.TRV},
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
        DevType.DT2: {"22": "digital_thermostat", AttrDict._SZ_AKA_SLUG: DevType.DTS},
        DevType.PRG: {"23": "programmer"},
        DevType.RFG: {"30": "rf_gateway"},  # RFG100
        DevType.RND: {"34": "round_thermostat"},
        # Other (jasper) devices
        DevType.JIM: {"08": "jasper_interface"},
        DevType.JST: {"31": "jasper_thermostat"},
        # Ventilation devices
        DevType.CO2: {None: "co2_sensor"},
        DevType.DIS: {None: "switch_display"},
        DevType.FAN: {None: "ventilator"},  # Both Fans and HRUs
        DevType.HUM: {None: "rh_sensor"},
        DevType.PIR: {None: "presence_sensor"},
        DevType.RFS: {None: "hvac_gateway"},  # Spider
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
        ),  # CH/DHW devices instead of HVAC/other
        "HEAT_ZONE_SENSORS": ("00", "01", "03", "04", "12", "22", "34"),
        "HEAT_ZONE_ACTUATORS": ("00", "02", "04", "13"),
        "THM_DEVICES": ("03", "12", "22", "34"),
        "TRV_DEVICES": ("00", "04"),
        "CONTROLLERS": ("01", "12", "22", "23", "34"),  # potentially controllers
        "PROMOTABLE_SLUGS": (DevType.DEV, DevType.HEA, DevType.HVC),
        "HVAC_SLUGS": {
            DevType.CO2: "co2_sensor",
            DevType.FAN: "ventilator",  # Both Fans and HRUs
            DevType.HUM: "rh_sensor",
            DevType.RFS: "hvac_gateway",  # Spider
            DevType.REM: "switch",
        },
    },
)


# slugs for zone entity klasses, used by 0005/000C
class ZoneRole(StrEnum):
    #
    # Generic device/zone classes
    ACT = "ACT"  # Generic heating zone actuator group
    SEN = "SEN"  # Generic heating zone sensor group
    #
    # Standard device/zone classes
    ELE = "ELE"  # heating zone with BDRs (no heat demand)
    MIX = "MIX"  # heating zone with HM8s
    RAD = "RAD"  # heating zone with TRVs
    UFH = "UFH"  # heating zone with UFC circuits
    VAL = "VAL"  # zheating one with BDRs
    # Standard device/zone classes *not a heating zone)
    DHW = "DHW"  # DHW zone with BDRs


ZON_ROLE_MAP = attr_dict_factory(
    {
        ZoneRole.ACT: {"00": "heating_zone"},  # any actuator
        ZoneRole.SEN: {"04": "heating_zone"},  # any sensor
        ZoneRole.RAD: {"08": "radiator_valve"},  # TRVs
        ZoneRole.UFH: {"09": "underfloor_heating"},  # UFCs
        ZoneRole.VAL: {"0A": "zone_valve"},  # BDRs
        ZoneRole.MIX: {"0B": "mixing_valve"},  # HM8s
        ZoneRole.DHW: {"0D": "stored_hotwater"},  # DHWs
        # N_CLASS.HTG: {"0E": "stored_hotwater", AttrDict._SZ_AKA_SLUG: ZON_ROLE.DHW},
        ZoneRole.ELE: {"11": "electric_heat"},  # BDRs
    },
    {
        "HEAT_ZONES": ("08", "09", "0A", "0B", "11"),
    },
)

# Zone modes
ZON_MODE_MAP = attr_dict_factory(
    {
        "FOLLOW": {"00": "follow_schedule"},
        "ADVANCED": {"01": "advanced_override"},  # . until the next scheduled setpoint
        "PERMANENT": {"02": "permanent_override"},  # indefinitely, until auto_reset
        "COUNTDOWN": {"03": "countdown_override"},  # for x mins (duration, max 1,215?)
        "TEMPORARY": {"04": "temporary_override"},  # until a given date/time (until)
    }
)

# System modes
SYS_MODE_MAP = attr_dict_factory(
    {
        "au_00": {"00": "auto"},  # .          indef (only)
        "ho_01": {"01": "heat_off"},  # .      indef (only)
        "eb_02": {"02": "eco_boost"},  # .     indef/<=24h: is either Eco, *or* Boost
        "aw_03": {"03": "away"},  # .          indef/<=99d (0d = end of today, 00:00)
        "do_04": {"04": "day_off"},  # .       indef/<=99d: rounded down to 00:00 by CTL
        "de_05": {"05": "day_off_eco"},  # .   indef/<=99d: set to Eco when DayOff ends
        "ar_06": {"06": "auto_with_reset"},  # indef (only)
        "cu_07": {"07": "custom"},  # .        indef/<=99d
    }
)


SZ_ACTIVE: Final = "active"
SZ_ACTUATOR: Final = "actuator"
SZ_ACTUATORS: Final = "actuators"
SZ_BINDINGS: Final = "bindings"
SZ_CONFIG: Final = "config"
SZ_DATETIME: Final = "datetime"
SZ_DEMAND: Final = "demand"
SZ_DEVICE_ID: Final = "device_id"
SZ_DEVICE_ROLE: Final = "device_role"
SZ_DEVICES: Final = "devices"
SZ_DHW_IDX: Final = "dhw_idx"
SZ_DOMAIN_ID: Final = "domain_id"
SZ_DURATION: Final = "duration"
SZ_HEAT_DEMAND: Final = "heat_demand"
SZ_IS_DST: Final = "is_dst"
SZ_LANGUAGE: Final = "language"
SZ_LOCAL_OVERRIDE: Final = "local_override"
SZ_MAX_TEMP: Final = "max_temp"
SZ_MIN_TEMP: Final = "min_temp"
SZ_MIX_CONFIG: Final = "mix_config"
SZ_MODE: Final = "mode"
SZ_MULTIROOM_MODE: Final = "multiroom_mode"
SZ_NAME: Final = "name"
SZ_OEM_CODE: Final = "oem_code"
SZ_OPENWINDOW_FUNCTION: Final = "openwindow_function"
SZ_PAYLOAD: Final = "payload"
SZ_PERCENTAGE: Final = "percentage"
SZ_PRESSURE: Final = "pressure"
SZ_RELAY_DEMAND: Final = "relay_demand"
SZ_RELAY_FAILSAFE: Final = "relay_failsafe"
SZ_SENSOR: Final = "sensor"
SZ_SETPOINT: Final = "setpoint"
SZ_SETPOINT_BOUNDS: Final = "setpoint_bounds"
SZ_SLUG: Final = "_SLUG"
SZ_SYSTEM_MODE: Final = "system_mode"
SZ_TEMPERATURE: Final = "temperature"
SZ_UFH_IDX: Final = "ufh_idx"
SZ_UNKNOWN: Final = "unknown"
SZ_UNTIL: Final = "until"
SZ_VALUE: Final = "value"
SZ_WINDOW_OPEN: Final = "window_open"
SZ_ZONE_CLASS: Final = "zone_class"
SZ_ZONE_IDX: Final = "zone_idx"
SZ_ZONE_MASK: Final = "zone_mask"
SZ_ZONE_TYPE: Final = "zone_type"
SZ_ZONES: Final = "zones"

# used in 0418 only?
SZ_DEVICE_CLASS: Final = "device_class"
# _DEVICE_ID: Final = "device_id"
SZ_DOMAIN_IDX: Final = "domain_idx"
SZ_FAULT_STATE: Final = "fault_state"
SZ_FAULT_TYPE: Final = "fault_type"
SZ_LOG_ENTRY: Final = "log_entry"
SZ_LOG_IDX: Final = "log_idx"
SZ_TIMESTAMP: Final = "timestamp"

# used in 1FC9
SZ_OFFER: Final = "offer"
SZ_ACCEPT: Final = "accept"
SZ_CONFIRM: Final = "confirm"
SZ_PHASE: Final = "phase"


DEFAULT_MAX_ZONES: Final = 16 if DEV_MODE else 12
# Evohome: 12 (0-11), older/initial version was 8
# Hometronics: 16 (0-15), or more?
# Sundial RF2: 2 (0-1), usually only one, but ST9520C can do two zones


DEVICE_ID_REGEX = SimpleNamespace(
    ANY=re.compile(r"^[0-9]{2}:[0-9]{6}$"),
    BDR=re.compile(r"^13:[0-9]{6}$"),
    CTL=re.compile(r"^(01|23):[0-9]{6}$"),
    DHW=re.compile(r"^07:[0-9]{6}$"),
    HGI=re.compile(r"^18:[0-9]{6}$"),
    APP=re.compile(r"^(10|13):[0-9]{6}$"),
    UFC=re.compile(r"^02:[0-9]{6}$"),
    SEN=re.compile(r"^(01|03|04|12|22|34):[0-9]{6}$"),
)

# Domains
F6: Final = "F6"
F7: Final = "F7"
F8: Final = "F8"
F9: Final = "F9"
FA: Final = "FA"
FB: Final = "FB"
FC: Final = "FC"
FD: Final = "FD"
FE: Final = "FE"
FF: Final = "FF"

DOMAIN_TYPE_MAP: dict[str, str] = {
    F6: "cooling_valve",  # cooling
    F7: "domain_f7",
    F8: "domain_f8",
    F9: DEV_ROLE_MAP[DevRole.HT1],  # Heating Valve
    FA: DEV_ROLE_MAP[DevRole.HTG],  # HW Valve (or UFH loop if src.type == UFC?)
    FB: "domain_fb",  # also: cooling valve?
    FC: DEV_ROLE_MAP[DevRole.APP],  # appliance_control
    FD: "domain_fd",  # seen with hometronics
    # "FE": ???
    # FF: "system",  # TODO: remove this, is not a domain
}  # "21": "Ventilation", "88": ???
DOMAIN_TYPE_LOOKUP = {v: k for k, v in DOMAIN_TYPE_MAP.items() if k != FF}

DHW_STATE_MAP: dict[str, str] = {"00": "off", "01": "on"}
DHW_STATE_LOOKUP = {v: k for k, v in DHW_STATE_MAP.items()}

DTM_LONG_REGEX = re.compile(
    r"\d{4}-[01]\d-[0-3]\d(T| )[0-2]\d:[0-5]\d:[0-5]\d\.\d{6} ?"
)  # 2020-11-30T13:15:00.123456
DTM_TIME_REGEX = re.compile(r"[0-2]\d:[0-5]\d:[0-5]\d\.\d{3} ?")  # 13:15:00.123

# Used by packet structure validators
r = r"(-{3}|\d{3}|\.{3})"  # RSSI, '...' was used by an older version of evofw3
v = r"( I|RP|RQ| W)"  # verb
d = r"(-{2}:-{6}|\d{2}:\d{6})"  # device ID
c = r"[0-9A-F]{4}"  # code
l = r"\d{3}"  # length # noqa: E741
p = r"([0-9A-F]{2}){1,48}"  # payload

# DEVICE_ID_REGEX = re.compile(f"^{d}$")
COMMAND_REGEX = re.compile(f"^{v} {r} {d} {d} {d} {c} {l} {p}$")
MESSAGE_REGEX = re.compile(f"^{r} {v} {r} {d} {d} {d} {c} {l} {p}$")


# Used by 0418/system_fault parser
class FaultDeviceClass(StrEnum):
    CONTROLLER = "controller"
    SENSOR = "sensor"
    SETPOINT = "setpoint"
    ACTUATOR = "actuator"  # if domain is FC, then "boiler_relay"
    DHW_ACTUATOR = "dhw_sensor"
    RF_GATEWAY = "rf_gateway"
    BOILER_RELAY = "boiler_relay"
    UNKNOWN = "unknown"


FAULT_DEVICE_CLASS: Final[dict[str, FaultDeviceClass]] = {
    "00": FaultDeviceClass.CONTROLLER,
    "01": FaultDeviceClass.SENSOR,
    "02": FaultDeviceClass.SETPOINT,
    "04": FaultDeviceClass.ACTUATOR,  # if domain is FC, then BOILER_RELAY
    "05": FaultDeviceClass.DHW_ACTUATOR,
    "06": FaultDeviceClass.RF_GATEWAY,
}


class FaultState(StrEnum):
    FAULT = "fault"
    RESTORE = "restore"
    UNKNOWN_C0 = "unknown_c0"
    UNKNOWN = "unknown"


FAULT_STATE: Final[dict[str, FaultState]] = {  # a bitmap?
    "00": FaultState.FAULT,
    "40": FaultState.RESTORE,
    "C0": FaultState.UNKNOWN_C0,  # C0s do not appear in the evohome UI
}


class FaultType(StrEnum):
    SYSTEM_FAULT = "system_fault"
    MAINS_LOW = "mains_low"
    BATTERY_LOW = "battery_low"
    BATTERY_ERROR = "battery_error"  # actually: 'evotouch_battery_error'
    COMMS_FAULT = "comms_fault"
    SENSOR_FAULT = "sensor_fault"  # seen with zone sensor
    SENSOR_ERROR = "sensor_error"
    BAD_VALUE = "bad_value"
    UNKNOWN = "unknown"


FAULT_TYPE: Final[dict[str, FaultType]] = {
    "01": FaultType.SYSTEM_FAULT,
    "03": FaultType.MAINS_LOW,
    "04": FaultType.BATTERY_LOW,
    "05": FaultType.BATTERY_ERROR,  # actually: 'evotouch_battery_error'
    "06": FaultType.COMMS_FAULT,
    "07": FaultType.SENSOR_FAULT,  # seen with zone sensor
    "0A": FaultType.SENSOR_ERROR,
}


class SystemType(StrEnum):
    CHRONOTHERM = "chronotherm"
    EVOHOME = "evohome"
    HOMETRONICS = "hometronics"
    PROGRAMMER = "programmer"
    SUNDIAL = "sundial"
    GENERIC = "generic"


# used by 22Fx parser, and FanSwitch devices
# SZ_BOOST_TIMER:Final = "boost_timer"  # minutes, e.g. 10, 20, 30 minutes
HEATER_MODE: Final = "heater_mode"  # e.g. auto, off
FAN_MODE: Final = "fan_mode"  # e.g. low. high   # .     deprecated, use SZ_FAN_MODE, to be removed in Q1 2026
FAN_RATE: Final = "fan_rate"  # percentage, 0.0 - 1.0  # deprecated, use SZ_FAN_MODE, to be removed in Q1 2026


# RP --- 01:054173 18:006402 --:------ 0005 004 00100000  # before adding RFG100
# .I --- 01:054173 --:------ 01:054173 1FC9 012 0010E004D39D001FC904D39D
# .W --- 30:248208 01:054173 --:------ 1FC9 012 0010E07BC9900012907BC990
# .I --- 01:054173 30:248208 --:------ 1FC9 006 00FFFF04D39D

# RP --- 01:054173 18:006402 --:------ 0005 004 00100100  # after adding RFG100
# RP --- 01:054173 18:006402 --:------ 000C 006 0010007BC990  # 30:082155
# RP --- 01:054173 18:006402 --:------ 0005 004 00100100  # before deleting RFG from CTL
# .I --- 01:054173 --:------ 01:054173 0005 004 00100000  # when the RFG was deleted
# RP --- 01:054173 18:006402 --:------ 0005 004 00100000  # after deleting the RFG

# RP|zone_devices | 000E0... || {'domain_id': 'FA', 'device_role': 'dhw_valve', 'devices': ['13:081807']}  # noqa: E501
# RP|zone_devices | 010E0... || {'domain_id': 'FA', 'device_role': 'htg_valve', 'devices': ['13:106039']}  # noqa: E501

# Example of:
#  - Sundial RF2 Pack 3: 23:(ST9420C), 07:(CS92), and 22:(DTS92(E))

# HCW80 has option of being wired (normally wireless)
# ST9420C has battery back-up (as does evohome)


# Below, verbs & codes - can use Verb/Code/Index for mypy type checking
@verify(EnumCheck.UNIQUE)
class VerbT(StrEnum):
    I_ = " I"
    RQ = "RQ"
    RP = "RP"
    W_ = " W"


I_: Final = VerbT.I_
RQ: Final = VerbT.RQ
RP: Final = VerbT.RP
W_: Final = VerbT.W_


@verify(EnumCheck.UNIQUE)
class MsgId(StrEnum):
    _00 = "00"
    _03 = "03"
    _06 = "06"
    _01 = "01"
    _05 = "05"
    _0E = "0E"
    _0F = "0F"
    _11 = "11"
    _12 = "12"
    _13 = "13"
    _19 = "19"
    _1A = "1A"
    _1B = "1B"
    _1C = "1C"
    _30 = "30"
    _31 = "31"
    _38 = "38"
    _39 = "39"
    _71 = "71"  # unclear if is supported bt OTB
    _72 = "72"  # unclear if is supported bt OTB
    _73 = "73"
    _74 = "74"  # unclear if is supported bt OTB
    _75 = "75"  # unclear if is supported bt OTB
    _76 = "76"  # unclear if is supported bt OTB
    _77 = "77"  # unclear if is supported bt OTB
    _78 = "78"  # unclear if is supported bt OTB
    _79 = "79"  # unclear if is supported bt OTB
    _7A = "7A"  # unclear if is supported bt OTB
    _7B = "7B"  # unclear if is supported bt OTB
    _7F = "7F"


# StrEnum is intended include all known codes, see: test suite, code schema in ramses.py
@verify(EnumCheck.UNIQUE)
class Code(StrEnum):
    _0001 = "0001"
    _0002 = "0002"
    _0004 = "0004"
    _0005 = "0005"
    _0006 = "0006"
    _0008 = "0008"
    _0009 = "0009"
    _000A = "000A"
    _000C = "000C"
    _000E = "000E"
    _0016 = "0016"
    _0100 = "0100"
    _0150 = "0150"
    _01D0 = "01D0"
    _01E9 = "01E9"
    _01FF = "01FF"
    _0404 = "0404"
    _0418 = "0418"
    _042F = "042F"
    _0B04 = "0B04"
    _1030 = "1030"
    _1060 = "1060"
    _1081 = "1081"
    _1090 = "1090"
    _1098 = "1098"
    _10A0 = "10A0"
    _10B0 = "10B0"
    _10D0 = "10D0"
    _10E0 = "10E0"
    _10E1 = "10E1"
    _10E2 = "10E2"
    _1100 = "1100"
    _11F0 = "11F0"
    _1260 = "1260"
    _1280 = "1280"
    _1290 = "1290"
    _1298 = "1298"
    _12A0 = "12A0"
    _12B0 = "12B0"
    _12C0 = "12C0"
    _12C8 = "12C8"
    _12F0 = "12F0"
    _1300 = "1300"
    _1470 = "1470"
    _1F09 = "1F09"
    _1F41 = "1F41"
    _1F70 = "1F70"
    _1FC9 = "1FC9"
    _1FCA = "1FCA"
    _1FD0 = "1FD0"
    _1FD4 = "1FD4"
    _2210 = "2210"
    _2249 = "2249"
    _22C9 = "22C9"
    _22D0 = "22D0"
    _22D9 = "22D9"
    _22E0 = "22E0"
    _22E5 = "22E5"
    _22E9 = "22E9"
    _22F1 = "22F1"
    _22F2 = "22F2"
    _22F3 = "22F3"
    _22F4 = "22F4"
    _22F7 = "22F7"
    _22F8 = "22F8"
    _22B0 = "22B0"
    _2309 = "2309"
    _2349 = "2349"
    _2389 = "2389"
    _2400 = "2400"
    _2401 = "2401"
    _2410 = "2410"
    _2411 = "2411"
    _2420 = "2420"
    _2D49 = "2D49"
    _2E04 = "2E04"
    _2E10 = "2E10"
    _30C9 = "30C9"
    _3110 = "3110"
    _3120 = "3120"
    _313E = "313E"
    _313F = "313F"
    _3150 = "3150"
    _31D9 = "31D9"
    _31DA = "31DA"
    _31E0 = "31E0"
    _3200 = "3200"
    _3210 = "3210"
    _3220 = "3220"
    _3221 = "3221"
    _3222 = "3222"
    _3223 = "3223"
    _3B00 = "3B00"
    _3EF0 = "3EF0"
    _3EF1 = "3EF1"
    _4401 = "4401"
    _4E01 = "4E01"
    _4E02 = "4E02"
    _4E04 = "4E04"
    _4E0D = "4E0D"
    _4E15 = "4E15"
    _4E16 = "4E16"
    _PUZZ = "7FFF"  # for internal use: not to be a RAMSES II code


# fmt: off
IndexT = Literal[
    "00", "01", "02", "03", "04", "05", "06", "07", "08", "09", "0A", "0B", "0C", "0D", "0E", "0F",
    "21",  # used by Nuaire
    "F0", "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "FA", "FB", "FC", "FD", "FE", "FF"
]
# fmt: on
