"""RAMSES RF - Cross-domain enumerations for the L7 event pipeline."""

from enum import EnumCheck, StrEnum, verify


# slugs for device/zone entity klasses, used by 0005/000C
@verify(EnumCheck.UNIQUE)
class DevRole(StrEnum):
    """Slugs for device/zone entity classes, used by commands 0005/000C."""

    ACT = "ACT"  # Generic heating zone actuator group
    SEN = "SEN"  # Generic heating zone sensor group
    ELE = "ELE"  # BDRs (no heat demand)
    MIX = "MIX"  # HM8s
    RAD = "RAD"  # TRVs
    UFH = "UFH"  # UFC (circuits)
    VAL = "VAL"  # BDRs
    DHW = "DHW"  # DHW sensor (a zone, but not a heating zone)
    HTG = "HTG"  # BDR (DHW relay, HTG relay)
    HT1 = "HT1"  # BDR (HTG relay)
    OUT = "OUT"  # OUT (external weather sensor)
    RFG = "RFG"  # RFG
    APP = "APP"  # BDR/OTB (appliance relay)


# slugs for device entity types, used in device_ids
@verify(EnumCheck.UNIQUE)
class DevType(StrEnum):
    """Slugs for device entity types, used in device_ids."""

    DEV = "DEV"  # xx: Promotable device
    HEA = "HEA"  # xx: Promotable Heat device, aka CH/DHW device
    HVC = "HVC"  # xx: Promotable HVAC device
    THM = "THM"  # xx: Generic thermostat

    BDR = "BDR"  # 13: Electrical relay
    CTL = "CTL"  # 01: Controller (zoned)
    DHW = "DHW"  # 07: DHW sensor
    DTS = "DTS"  # 12: Thermostat, DTS92(E)
    DT2 = "DT2"  # 22: Thermostat, DTS92(E)
    HCW = "HCW"  # 03: Thermostat - don't use STA
    HGI = "HGI"  # 18: Gateway interface (RF to USB), HGI80
    OTB = "OTB"  # 10: OpenTherm bridge
    OUT = "OUT"  # 17: External weather sensor
    PRG = "PRG"  # 23: Programmer
    RFG = "RFG"  # 30: RF gateway (RF to ethernet), RFG100
    RND = "RND"  # 34: Thermostat, TR87RF
    TRV = "TRV"  # 04: Thermostatic radiator valve
    TR0 = "TR0"  # 00: Thermostatic radiator valve
    UFC = "UFC"  # 02: UFH controller

    JIM = "JIM"  # 08: Jasper Interface Module (EIM?)
    JST = "JST"  # 31: Jasper Stat

    RFS = "RFS"  # ??: HVAC spIDer gateway
    FAN = "FAN"  # ??: HVAC fan
    CO2 = "CO2"  # ??: HVAC CO2 sensor
    HUM = "HUM"  # ??: HVAC humidity sensor
    PIR = "PIR"  # ??: HVAC pesence sensor
    REM = "REM"  # ??: HVAC switch
    SW2 = "SW2"  # ??: HVAC switch, Orcon variant
    DIS = "DIS"  # ??: HVAC switch with display


# slugs for zone entity klasses, used by 0005/000C
@verify(EnumCheck.UNIQUE)
class ZoneRole(StrEnum):
    """Slugs for zone entity classes, used by commands 0005/000C."""

    ACT = "ACT"  # Generic heating zone actuator group
    SEN = "SEN"  # Generic heating zone sensor group
    ELE = "ELE"  # heating zone with BDRs (no heat demand)
    MIX = "MIX"  # heating zone with HM8s
    RAD = "RAD"  # heating zone with TRVs
    UFH = "UFH"  # heating zone with UFC circuits
    VAL = "VAL"  # heating zone with BDRs
    DHW = "DHW"  # DHW zone with BDRs


class Topic(StrEnum):
    """Event Bus routing discriminators."""

    RAW_EVENT = "raw_event"
    STATE_UPDATE = "state_update"
    TOPOLOGY_DISCOVERY = "topology_discovery"


class Action(StrEnum):
    """Standardized intents for outbound commands."""

    GET_ZONE_NAME = "get_zone_name"
    SET_ZONE_NAME = "set_zone_name"
    GET_ZONE_CONFIG = "get_zone_config"
    SET_ZONE_CONFIG = "set_zone_config"
    GET_WINDOW_STATE = "get_window_state"
    GET_ZONE_WINDOW_STATE = "get_zone_window_state"
    GET_SETPOINT = "get_setpoint"
    GET_ZONE_SETPOINT = "get_zone_setpoint"
    SET_SETPOINT = "set_setpoint"
    GET_MODE = "get_mode"
    SET_MODE = "set_mode"
    GET_ZONE_TEMP = "get_zone_temp"
    SET_TEMPERATURE = "set_temperature"

    GET_DHW_PARAMS = "get_dhw_params"
    SET_DHW_PARAMS = "set_dhw_params"
    GET_DHW_TEMP = "get_dhw_temp"
    PUT_DHW_TEMP = "put_dhw_temp"
    GET_DHW_MODE = "get_dhw_mode"
    SET_DHW_MODE = "set_dhw_mode"

    PUT_CO2_LEVEL = "put_co2_level"
    PUT_VENTILATION_DEMAND = "put_ventilation_demand"
    PUT_INDOOR_HUMIDITY = "put_indoor_humidity"
    PUT_OUTDOOR_TEMP = "put_outdoor_temp"
    PUT_SENSOR_TEMP = "put_sensor_temp"
    SET_FAN_MODE = "set_fan_mode"
    SET_BYPASS_POSITION = "set_bypass_position"
    SET_FAN_PARAM = "set_fan_param"
    GET_FAN_PARAM = "get_fan_param"
    GET_HVAC_FAN_31DA = "get_hvac_fan_31da"
    SET_PROGRAM_ENABLED = "set_program_enabled"

    GET_SCHEDULE_VERSION = "get_schedule_version"
    GET_SCHEDULE_FRAGMENT = "get_schedule_fragment"
    SET_SCHEDULE_FRAGMENT = "set_schedule_fragment"

    GET_FAULTLOG_ENTRY = "get_faultlog_entry"
    PUT_FAULTLOG_ENTRY = "put_faultlog_entry"
    CLEAR_FAULTLOG = "clear_faultlog"

    GET_OPENTHERM_DATA = "get_opentherm_data"
    GET_OPENTHERM_CONFIG = "get_opentherm_config"
    SET_OPENTHERM_CONFIG = "set_opentherm_config"

    PUT_WEATHER_TEMP = "put_weather_temp"
    GET_RELAY_DEMAND = "get_relay_demand"
    GET_SYSTEM_LANGUAGE = "get_system_language"
    GET_MIX_VALVE_PARAMS = "get_mix_valve_params"
    SET_MIX_VALVE_PARAMS = "set_mix_valve_params"
    GET_TPI_PARAMS = "get_tpi_params"
    SET_TPI_PARAMS = "set_tpi_params"
    PUT_BIND = "put_bind"
    GET_SYSTEM_MODE = "get_system_mode"
    SET_SYSTEM_MODE = "set_system_mode"
    PUT_PRESENCE_DETECTED = "put_presence_detected"
    GET_SYSTEM_TIME = "get_system_time"
    SET_SYSTEM_TIME = "set_system_time"
    PUT_ACTUATOR_STATE = "put_actuator_state"
    PUT_ACTUATOR_CYCLE = "put_actuator_cycle"
    SEND_PUZZLE = "send_puzzle"


class TopologyAction(StrEnum):
    """Structural graph mutation actions."""

    UPDATE_DEVICE_CLASS = "update_device_class"
    UPDATE_TRAITS = "update_traits"
    BIND_DEVICE = "bind_device"
    CREATE_CONTROLLER = "create_controller"
    CREATE_CIRCUIT = "create_circuit"


@verify(EnumCheck.UNIQUE)
class ThermalMode(StrEnum):
    """Thermal operating mode for climate systems (driven by 2D49)."""

    HEAT = "heat"
    COOL = "cool"
    OFF = "off"


@verify(EnumCheck.UNIQUE)
class PumpRelayState(StrEnum):
    """Actuator pump relay state for underfloor heating/cooling systems (Opcode 3EF0)."""

    HEATING = "heating"
    COOLING = "cooling"
    OFF = "off"
