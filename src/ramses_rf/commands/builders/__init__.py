"""RAMSES RF - Intent-to-DTO Translation Builders.

This package houses the pure L7-to-L3 payload translation logic.
"""

from collections.abc import Callable

from ramses_rf.commands.core import Command
from ramses_rf.enums import Action
from ramses_tx.dtos import CommandDTO

from . import dhw, faultlog, heat, hvac, opentherm, schedules, system, zones

# Maps an Action intent to the appropriate payload constructor.
BUILDERS: dict[Action, Callable[[Command], CommandDTO]] = {
    # DHW Commands
    Action.GET_DHW_PARAMS: dhw.build_get_dhw_params,
    Action.SET_DHW_PARAMS: dhw.build_set_dhw_params,
    Action.GET_DHW_TEMP: dhw.build_get_dhw_temp,
    Action.PUT_DHW_TEMP: dhw.build_put_dhw_temp,
    Action.GET_DHW_MODE: dhw.build_get_dhw_mode,
    Action.SET_DHW_MODE: dhw.build_set_dhw_mode,
    # HVAC Commands
    Action.PUT_CO2_LEVEL: hvac.build_put_co2_level,
    Action.PUT_INDOOR_HUMIDITY: hvac.build_put_indoor_humidity,
    Action.SET_FAN_MODE: hvac.build_set_fan_mode,
    Action.SET_BYPASS_POSITION: hvac.build_set_bypass_position,
    Action.SET_FAN_PARAM: hvac.build_set_fan_param,
    Action.GET_FAN_PARAM: hvac.build_get_fan_param,
    Action.GET_HVAC_FAN_31DA: hvac.build_get_hvac_fan_31da,
    Action.SET_PROGRAM_ENABLED: hvac.build_set_program_enabled,
    # Heat Commands
    Action.PUT_OUTDOOR_TEMP: heat.build_put_outdoor_temp,
    Action.PUT_DHW_TEMP: heat.build_put_dhw_temp,
    Action.PUT_SENSOR_TEMP: heat.build_put_sensor_temp,
    # Zone Commands
    Action.SET_TEMPERATURE: zones.build_set_temperature,
    # Schedule Commands
    Action.GET_SCHEDULE_VERSION: schedules.build_get_schedule_version,
    Action.GET_SCHEDULE_FRAGMENT: schedules.build_get_schedule_fragment,
    Action.SET_SCHEDULE_FRAGMENT: schedules.build_set_schedule_fragment,
    # FaultLog Commands
    Action.GET_FAULTLOG_ENTRY: faultlog.build_get_faultlog_entry,
    # OpenTherm Commands
    Action.GET_OPENTHERM_DATA: opentherm.build_get_opentherm_data,
    Action.SET_MODE: zones.build_set_mode,
    Action.SET_ZONE_NAME: zones.build_set_name,
    Action.SET_ZONE_CONFIG: zones.build_set_config,
    Action.GET_ZONE_NAME: zones.build_get_name,
    Action.GET_ZONE_CONFIG: zones.build_get_config,
    Action.GET_ZONE_WINDOW_STATE: zones.build_get_window_state,
    Action.GET_ZONE_SETPOINT: zones.build_get_setpoint,
    Action.GET_MODE: zones.build_get_mode,
    Action.GET_ZONE_TEMP: zones.build_get_temp,
    # Faultlog Commands
    Action.GET_FAULTLOG_ENTRY: faultlog.build_get_faultlog_entry,
    Action.PUT_FAULTLOG_ENTRY: faultlog.build_put_faultlog_entry,
    # System Commands
    Action.PUT_WEATHER_TEMP: system.build_put_weather_temp,
    Action.GET_RELAY_DEMAND: system.build_get_relay_demand,
    Action.GET_SYSTEM_LANGUAGE: system.build_get_system_language,
    Action.GET_MIX_VALVE_PARAMS: system.build_get_mix_valve_params,
    Action.SET_MIX_VALVE_PARAMS: system.build_set_mix_valve_params,
    Action.GET_TPI_PARAMS: system.build_get_tpi_params,
    Action.SET_TPI_PARAMS: system.build_set_tpi_params,
    Action.PUT_BIND: system.build_put_bind,
    Action.GET_SYSTEM_MODE: system.build_get_system_mode,
    Action.SET_SYSTEM_MODE: system.build_set_system_mode,
    Action.PUT_PRESENCE_DETECTED: system.build_put_presence_detected,
    Action.GET_SYSTEM_TIME: system.build_get_system_time,
    Action.SET_SYSTEM_TIME: system.build_set_system_time,
    Action.PUT_ACTUATOR_STATE: system.build_put_actuator_state,
    Action.PUT_ACTUATOR_CYCLE: system.build_put_actuator_cycle,
    Action.SEND_PUZZLE: system.build_send_puzzle,
}


def build_dto(intent: Command) -> CommandDTO:
    """Translate an L7 Command intent into a strict L3 CommandDTO.

    :param intent: The high-level domain intent.
    :return: The low-level modem instruction.
    :raises NotImplementedError: If no builder is mapped for the action.
    """
    builder = BUILDERS.get(intent.action)
    if not builder:
        raise NotImplementedError(
            f"No translation builder registered for intent action: {intent.action}"
        )
    return builder(intent)
