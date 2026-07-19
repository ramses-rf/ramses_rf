#!/usr/bin/env python3
"""RAMSES RF - The modular Command package."""

from __future__ import annotations

from ..const import I_, RP, RQ, W_, Code
from .base import CommandBase
from .system import SystemMixins


class Command(SystemMixins, CommandBase):
    """The Command class (packets to be transmitted).

    They have QoS and/or callbacks (but no RSSI).
    """


# A convenience dict
CODE_API_MAP = {
    f"{RP}|{Code._3EF1}": Command.put_actuator_cycle,
    f"{I_}|{Code._3EF0}": Command.put_actuator_state,
    f"{I_}|{Code._1FC9}": Command.put_bind,
    f"{W_}|{Code._1FC9}": Command.put_bind,
    f"{RQ}|{Code._1030}": Command.get_mix_valve_params,
    f"{W_}|{Code._1030}": Command.set_mix_valve_params,
    f"{RQ}|{Code._3220}": Command.get_opentherm_data,
    f"{I_}|{Code._1290}": Command.put_outdoor_temp,
    f"{I_}|{Code._2E10}": Command.put_presence_detected,
    f"{RQ}|{Code._0008}": Command.get_relay_demand,
    f"{RQ}|{Code._0404}": Command.get_schedule_fragment,
    f"{W_}|{Code._0404}": Command.set_schedule_fragment,
    f"{RQ}|{Code._0006}": Command.get_schedule_version,
    f"{I_}|{Code._30C9}": Command.put_sensor_temp,
    f"{RQ}|{Code._0100}": Command.get_system_language,
    f"{RQ}|{Code._0418}": Command.get_system_log_entry,
    f"{RQ}|{Code._2E04}": Command.get_system_mode,
    f"{W_}|{Code._2E04}": Command.set_system_mode,
    f"{RQ}|{Code._313F}": Command.get_system_time,
    f"{W_}|{Code._313F}": Command.set_system_time,
    f"{RQ}|{Code._1100}": Command.get_tpi_params,
    f"{W_}|{Code._1100}": Command.set_tpi_params,
    f"{I_}|{Code._0002}": Command.put_weather_temp,
}
