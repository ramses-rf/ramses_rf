"""RAMSES RF - System-wide command intent to L3 payload translation."""

from __future__ import annotations

from typing import cast

from ramses_rf.commands.builders.helpers import resolve_addrs
from ramses_rf.commands.core import Command
from ramses_tx.address import ALL_DEV_ADDR, dev_id_to_hex_id
from ramses_tx.command.base import _check_idx
from ramses_tx.const import (
    DEFAULT_NUM_REPEATS,
    DEV_TYPE_MAP,
    FF,
    I_,
    RP,
    RQ,
    SYS_MODE_MAP,
    W_,
    Code,
    Priority,
)
from ramses_tx.dtos import CommandDTO
from ramses_tx.helpers import (
    hex_from_bool,
    hex_from_dtm,
    hex_from_percent,
    hex_from_str,
    hex_from_temp,
    timestamp,
)


def build_put_weather_temp(intent: Command) -> CommandDTO:
    """Translate a PUT_WEATHER_TEMP intent into a CommandDTO."""
    temperature = intent.get("temperature")

    if intent.src.id[:2] != DEV_TYPE_MAP.OUT:
        raise ValueError(
            f"Faked device {intent.src.id} has an unsupported device type: "
            f"device_id should be like {DEV_TYPE_MAP.OUT}:xxxxxx"
        )

    addr1, addr2, addr3 = resolve_addrs(intent.src, intent.src)
    payload = f"00{hex_from_temp(temperature)}01"

    return CommandDTO(
        verb=I_,
        addr1=addr1,
        addr2=addr2,
        addr3=addr3,
        code=Code._0002,
        payload=payload,
        priority=Priority.DEFAULT,
        num_repeats=DEFAULT_NUM_REPEATS,
    )


def build_get_relay_demand(intent: Command) -> CommandDTO:
    """Translate a GET_RELAY_DEMAND intent into a CommandDTO."""
    zone_idx = intent.get("zone_idx")
    payload = "00" if zone_idx is None else _check_idx(zone_idx)

    addr1, addr2, addr3 = resolve_addrs(intent.src, intent.dst)

    return CommandDTO(
        verb=RQ,
        addr1=addr1,
        addr2=addr2,
        addr3=addr3,
        code=Code._0008,
        payload=payload,
        priority=Priority.DEFAULT,
        num_repeats=DEFAULT_NUM_REPEATS,
    )


def build_get_system_language(intent: Command) -> CommandDTO:
    """Translate a GET_SYSTEM_LANGUAGE intent into a CommandDTO."""
    addr1, addr2, addr3 = resolve_addrs(intent.src, intent.dst)

    return CommandDTO(
        verb=RQ,
        addr1=addr1,
        addr2=addr2,
        addr3=addr3,
        code=Code._0100,
        payload="00",
        priority=Priority.DEFAULT,
        num_repeats=DEFAULT_NUM_REPEATS,
    )


def build_get_mix_valve_params(intent: Command) -> CommandDTO:
    """Translate a GET_MIX_VALVE_PARAMS intent into a CommandDTO."""
    zone_idx = intent.get("zone_idx")
    zon_idx = _check_idx(zone_idx)
    addr1, addr2, addr3 = resolve_addrs(intent.src, intent.dst)

    return CommandDTO(
        verb=RQ,
        addr1=addr1,
        addr2=addr2,
        addr3=addr3,
        code=Code._1030,
        payload=zon_idx,
        priority=Priority.DEFAULT,
        num_repeats=DEFAULT_NUM_REPEATS,
    )


def build_set_mix_valve_params(intent: Command) -> CommandDTO:
    """Translate a SET_MIX_VALVE_PARAMS intent into a CommandDTO."""
    zone_idx = intent.get("zone_idx")
    max_flow_setpoint = intent.get("max_flow_setpoint", 55)
    min_flow_setpoint = intent.get("min_flow_setpoint", 15)
    valve_run_time = intent.get("valve_run_time", 150)
    pump_run_time = intent.get("pump_run_time", 15)
    boolean_cc = intent.get("boolean_cc", 1)

    zon_idx = _check_idx(zone_idx)

    if not (0 <= max_flow_setpoint <= 99):
        raise ValueError(f"Out of range, max_flow_setpoint: {max_flow_setpoint}")
    if not (0 <= min_flow_setpoint <= 50):
        raise ValueError(f"Out of range, min_flow_setpoint: {min_flow_setpoint}")
    if not (0 <= valve_run_time <= 240):
        raise ValueError(f"Out of range, valve_run_time: {valve_run_time}")
    if not (0 <= pump_run_time <= 99):
        raise ValueError(f"Out of range, pump_run_time: {pump_run_time}")

    payload = "".join(
        (
            zon_idx,
            f"C801{max_flow_setpoint:02X}",
            f"C901{min_flow_setpoint:02X}",
            f"CA01{valve_run_time:02X}",
            f"CB01{pump_run_time:02X}",
            f"CC01{boolean_cc:02X}",
        )
    )

    addr1, addr2, addr3 = resolve_addrs(intent.src, intent.dst)

    return CommandDTO(
        verb=W_,
        addr1=addr1,
        addr2=addr2,
        addr3=addr3,
        code=Code._1030,
        payload=payload,
        priority=Priority.DEFAULT,
        num_repeats=DEFAULT_NUM_REPEATS,
    )


def build_get_tpi_params(intent: Command) -> CommandDTO:
    """Translate a GET_TPI_PARAMS intent into a CommandDTO."""
    domain_id = intent.get("domain_id")
    if domain_id is None:
        from ramses_tx.const import FC

        domain_id = "00" if intent.dst.id[:2] == DEV_TYPE_MAP.BDR else FC

    addr1, addr2, addr3 = resolve_addrs(intent.src, intent.dst)
    return CommandDTO(
        verb=RQ,
        addr1=addr1,
        addr2=addr2,
        addr3=addr3,
        code=Code._1100,
        payload=_check_idx(domain_id),
        priority=Priority.DEFAULT,
        num_repeats=DEFAULT_NUM_REPEATS,
    )


def build_set_tpi_params(intent: Command) -> CommandDTO:
    """Translate a SET_TPI_PARAMS intent into a CommandDTO."""
    domain_id = intent.get("domain_id", "00")
    if domain_id is None:
        domain_id = "00"

    cycle_rate = intent.get("cycle_rate", 3)
    min_on_time = intent.get("min_on_time", 5)
    min_off_time = intent.get("min_off_time", 5)
    proportional_band_width = intent.get("proportional_band_width")

    payload = "".join(
        (
            _check_idx(domain_id),
            f"{cycle_rate * 4:02X}",
            f"{int(min_on_time * 4):02X}",
            f"{int(min_off_time * 4):02X}00",
            f"{hex_from_temp(proportional_band_width)}01",
        )
    )

    addr1, addr2, addr3 = resolve_addrs(intent.src, intent.dst)
    return CommandDTO(
        verb=W_,
        addr1=addr1,
        addr2=addr2,
        addr3=addr3,
        code=Code._1100,
        payload=payload,
        priority=Priority.DEFAULT,
        num_repeats=DEFAULT_NUM_REPEATS,
    )


def build_put_bind(intent: Command) -> CommandDTO:
    """Translate a PUT_BIND intent into a CommandDTO."""
    verb = intent.get("verb")
    codes = intent.get("codes")
    oem_code = intent.get("oem_code")
    idx = intent.get("idx")

    if not codes:
        kodes = []
    elif len(list(codes)[0]) == len(Code._1FC9):
        kodes = list(codes)
    elif len(list(codes)[0]) == len(Code._1FC9[0]):
        kodes = [cast(Code, codes)]
    else:
        raise ValueError(f"Invalid codes for a bind command: {codes}")

    if verb == I_ and intent.dst.id in (None, intent.src.id, ALL_DEV_ADDR.id):
        # put_bind_offer
        kodes = [c for c in kodes if c not in (Code._1FC9, Code._10E0)]
        if not kodes:
            raise ValueError(f"Invalid codes for a bind offer: {codes}")
        hex_id = dev_id_to_hex_id(intent.src.id)
        payload = "".join(f"00{c}{hex_id}" for c in kodes)
        if oem_code:
            payload += f"{oem_code}{Code._10E0}{hex_id}"
        payload += f"00{Code._1FC9}{hex_id}"

        dst = intent.dst if intent.dst.id != ALL_DEV_ADDR.id else intent.src
        addr1, addr2, addr3 = resolve_addrs(intent.src, dst)
        return CommandDTO(
            verb=I_,
            addr1=addr1,
            addr2=addr2,
            addr3=addr3,
            code=Code._1FC9,
            payload=payload,
            priority=Priority.DEFAULT,
            num_repeats=DEFAULT_NUM_REPEATS,
        )

    elif verb == W_ and intent.dst.id not in (None, intent.src.id):
        # put_bind_accept
        if not kodes:
            raise ValueError(f"Invalid codes for a bind accept: {codes}")
        hex_id = dev_id_to_hex_id(intent.src.id)
        payload = "".join(f"{idx or '00'}{c}{hex_id}" for c in kodes)
        addr1, addr2, addr3 = resolve_addrs(intent.src, intent.dst)
        return CommandDTO(
            verb=W_,
            addr1=addr1,
            addr2=addr2,
            addr3=addr3,
            code=Code._1FC9,
            payload=payload,
            priority=Priority.DEFAULT,
            num_repeats=DEFAULT_NUM_REPEATS,
        )

    elif verb == I_:
        # put_bind_confirm
        if not kodes:
            payload = idx or "00"
        else:
            hex_id = dev_id_to_hex_id(intent.src.id)
            payload = f"{idx or '00'}{kodes[0]}{hex_id}"
        addr1, addr2, addr3 = resolve_addrs(intent.src, intent.dst)
        return CommandDTO(
            verb=I_,
            addr1=addr1,
            addr2=addr2,
            addr3=addr3,
            code=Code._1FC9,
            payload=payload,
            priority=Priority.DEFAULT,
            num_repeats=DEFAULT_NUM_REPEATS,
        )

    raise ValueError(f"Invalid verb|dst_id for a bind command: {verb}|{intent.dst.id}")


def build_get_system_mode(intent: Command) -> CommandDTO:
    """Translate a GET_SYSTEM_MODE intent into a CommandDTO."""
    addr1, addr2, addr3 = resolve_addrs(intent.src, intent.dst)
    return CommandDTO(
        verb=RQ,
        addr1=addr1,
        addr2=addr2,
        addr3=addr3,
        code=Code._2E04,
        payload=FF,
        priority=Priority.DEFAULT,
        num_repeats=DEFAULT_NUM_REPEATS,
    )


def build_set_system_mode(intent: Command) -> CommandDTO:
    """Translate a SET_SYSTEM_MODE intent into a CommandDTO."""
    system_mode = intent.get("system_mode", SYS_MODE_MAP.AUTO)
    until = intent.get("until")

    if system_mode is None:
        system_mode = SYS_MODE_MAP.AUTO
    if isinstance(system_mode, int):
        system_mode = f"{system_mode:02X}"
    if system_mode not in SYS_MODE_MAP:
        system_mode = SYS_MODE_MAP._hex(system_mode)

    if until is not None and system_mode in (
        SYS_MODE_MAP.AUTO,
        SYS_MODE_MAP.AUTO_WITH_RESET,
        SYS_MODE_MAP.HEAT_OFF,
    ):
        raise ValueError(
            f"Invalid args: For system_mode={SYS_MODE_MAP[system_mode]}, until must be None"
        )

    payload = "".join(
        (
            system_mode,
            hex_from_dtm(until),
            "00" if until is None else "01",
        )
    )

    addr1, addr2, addr3 = resolve_addrs(intent.src, intent.dst)
    return CommandDTO(
        verb=W_,
        addr1=addr1,
        addr2=addr2,
        addr3=addr3,
        code=Code._2E04,
        payload=payload,
        priority=Priority.DEFAULT,
        num_repeats=DEFAULT_NUM_REPEATS,
    )


def build_put_presence_detected(intent: Command) -> CommandDTO:
    """Translate a PUT_PRESENCE_DETECTED intent into a CommandDTO."""
    presence_detected = intent.get("presence_detected")
    payload = f"00{hex_from_bool(presence_detected)}"
    addr1, addr2, addr3 = resolve_addrs(intent.src, intent.src)
    return CommandDTO(
        verb=I_,
        addr1=addr1,
        addr2=addr2,
        addr3=addr3,
        code=Code._2E10,
        payload=payload,
        priority=Priority.DEFAULT,
        num_repeats=DEFAULT_NUM_REPEATS,
    )


def build_get_system_time(intent: Command) -> CommandDTO:
    """Translate a GET_SYSTEM_TIME intent into a CommandDTO."""
    addr1, addr2, addr3 = resolve_addrs(intent.src, intent.dst)
    return CommandDTO(
        verb=RQ,
        addr1=addr1,
        addr2=addr2,
        addr3=addr3,
        code=Code._313F,
        payload="00",
        priority=Priority.DEFAULT,
        num_repeats=DEFAULT_NUM_REPEATS,
    )


def build_set_system_time(intent: Command) -> CommandDTO:
    """Translate a SET_SYSTEM_TIME intent into a CommandDTO."""
    datetime = intent.get("datetime")
    is_dst = intent.get("is_dst", False)

    dt_str = hex_from_dtm(datetime, is_dst=is_dst, incl_seconds=True)
    payload = f"0060{dt_str}"
    addr1, addr2, addr3 = resolve_addrs(intent.src, intent.dst)
    return CommandDTO(
        verb=W_,
        addr1=addr1,
        addr2=addr2,
        addr3=addr3,
        code=Code._313F,
        payload=payload,
        priority=Priority.DEFAULT,
        num_repeats=DEFAULT_NUM_REPEATS,
    )


def build_put_actuator_state(intent: Command) -> CommandDTO:
    """Translate a PUT_ACTUATOR_STATE intent into a CommandDTO."""
    modulation_level = intent.get("modulation_level")

    if intent.src.id[:2] != DEV_TYPE_MAP.BDR:
        raise ValueError(
            f"Faked device {intent.src.id} has an unsupported device type: "
            f"device_id should be like {DEV_TYPE_MAP.BDR}:xxxxxx"
        )

    payload = (
        "007FFF"
        if modulation_level is None
        else f"00{int(modulation_level * 200):02X}FF"
    )
    addr1, addr2, addr3 = resolve_addrs(intent.src, intent.src)
    return CommandDTO(
        verb=I_,
        addr1=addr1,
        addr2=addr2,
        addr3=addr3,
        code=Code._3EF0,
        payload=payload,
        priority=Priority.DEFAULT,
        num_repeats=DEFAULT_NUM_REPEATS,
    )


def build_put_actuator_cycle(intent: Command) -> CommandDTO:
    """Translate a PUT_ACTUATOR_CYCLE intent into a CommandDTO."""
    modulation_level = intent.get("modulation_level")
    actuator_countdown = intent.get("actuator_countdown")
    cycle_countdown = intent.get("cycle_countdown")

    if intent.src.id[:2] != DEV_TYPE_MAP.BDR:
        raise ValueError(
            f"Faked device {intent.src.id} has an unsupported device type: "
            f"device_id should be like {DEV_TYPE_MAP.BDR}:xxxxxx"
        )

    payload = "00"
    payload += f"{cycle_countdown:04X}" if cycle_countdown is not None else "7FFF"
    payload += f"{actuator_countdown:04X}"
    payload += hex_from_percent(modulation_level)
    payload += "FF"
    addr1, addr2, addr3 = resolve_addrs(intent.src, intent.dst)
    return CommandDTO(
        verb=RP,
        addr1=addr1,
        addr2=addr2,
        addr3=addr3,
        code=Code._3EF1,
        payload=payload,
        priority=Priority.DEFAULT,
        num_repeats=DEFAULT_NUM_REPEATS,
    )


def build_send_puzzle(intent: Command) -> CommandDTO:
    """Translate a SEND_PUZZLE intent into a CommandDTO."""
    from ramses_tx.const import LOOKUP_PUZZ
    from ramses_tx.version import VERSION

    msg_type = intent.get("msg_type")
    message = intent.get("message", "")

    if msg_type is None:
        msg_type = "12" if message else "10"

    if msg_type not in LOOKUP_PUZZ:
        raise ValueError(f"Invalid/deprecated Puzzle type: {msg_type}")

    payload = f"00{msg_type}"

    if int(msg_type, 16) >= int("20", 16):
        payload += f"{int(timestamp() * 1e7):012X}"
    elif msg_type != "13":
        payload += f"{int(timestamp() * 1000):012X}"

    if msg_type == "10":
        payload += hex_from_str(f"v{VERSION}")
    elif msg_type == "11":
        payload += hex_from_str(message[:4] + message[5:7] + message[8:])
    else:
        payload += hex_from_str(message)

    addr1, addr2, addr3 = resolve_addrs(intent.src, ALL_DEV_ADDR)
    return CommandDTO(
        verb=I_,
        addr1=addr1,
        addr2=addr2,
        addr3=addr3,
        code=Code._PUZZ,
        payload=payload[:48],
        priority=Priority.DEFAULT,
        num_repeats=DEFAULT_NUM_REPEATS,
    )
