"""RAMSES RF - HVAC and ventilation command intent to L3 payload translation."""

import math

from ramses_rf.commands.builders.helpers import resolve_addrs
from ramses_rf.commands.core import Command
from ramses_rf.parsers.helpers import (
    air_quality_code,
    capability_bits,
    fan_info_flags,
    fan_info_to_byte,
)
from ramses_rf.protocol.ramses import (
    _22F1_SCHEMES,
    _2411_PARAMS_SCHEMA,
    SZ_DATA_TYPE,
    SZ_MAX_VALUE,
    SZ_MIN_VALUE,
    SZ_PRECISION,
)
from ramses_tx.address import NON_DEV_ADDR
from ramses_tx.const import DEFAULT_NUM_REPEATS, I_, RQ, SZ_MINUTES, W_, Code, Priority
from ramses_tx.dtos import CommandDTO
from ramses_tx.helpers import hex_from_double, hex_from_percent, hex_from_temp

_22F1_MODE_MAX: dict[str, str | None] = {
    "itho": "04",
    "nuaire": "0A",
    "vasco": "06",
    "orcon": "07",
}


def build_put_co2_level(intent: Command) -> CommandDTO:
    """Translate a PUT_CO2_LEVEL intent into a CommandDTO."""
    co2_level = intent.get("co2_level")
    payload = f"00{hex_from_double(co2_level)}"
    addr1, addr2, addr3 = resolve_addrs(intent.src, intent.src)
    return CommandDTO(
        verb=I_,
        addr1=addr1,
        addr2=addr2,
        addr3=addr3,
        code=Code._1298,
        payload=payload,
        priority=Priority.DEFAULT,
        num_repeats=DEFAULT_NUM_REPEATS,
    )


def build_put_indoor_humidity(intent: Command) -> CommandDTO:
    """Translate a PUT_INDOOR_HUMIDITY intent into a CommandDTO."""
    indoor_humidity = intent.get("indoor_humidity")
    payload = "00" + hex_from_percent(indoor_humidity, high_res=False)
    addr1, addr2, addr3 = resolve_addrs(intent.src, intent.src)
    return CommandDTO(
        verb=I_,
        addr1=addr1,
        addr2=addr2,
        addr3=addr3,
        code=Code._12A0,
        payload=payload,
        priority=Priority.DEFAULT,
        num_repeats=DEFAULT_NUM_REPEATS,
    )


def build_set_fan_mode(intent: Command) -> CommandDTO:
    """Translate a SET_FAN_MODE intent into a CommandDTO."""
    fan_mode = intent.get("fan_mode")
    scheme = intent.get("scheme", "orcon")
    seqn = intent.get("seqn")
    idx = intent.get("idx", "00")
    mode_max = intent.get("mode_max")
    legacy_format = intent.get("legacy_format", False)

    if scheme not in _22F1_SCHEMES:
        raise ValueError(
            f"fan_mode scheme is not valid: {scheme} "
            f"(expected one of: {', '.join(sorted(_22F1_SCHEMES))})"
        )

    mode_map = _22F1_SCHEMES[scheme]
    mode_map_r = {v: k for k, v in mode_map.items()}

    if fan_mode is None:
        mode = "00"
    elif isinstance(fan_mode, int):
        mode = f"{fan_mode:02X}"
    else:
        mode = fan_mode

    if mode in mode_map:
        pass
    elif mode in mode_map_r:
        mode = mode_map_r[mode]
    else:
        raise ValueError(f"fan_mode is not valid for scheme '{scheme}': {fan_mode}")

    if mode_max is None:
        mode_max = _22F1_MODE_MAX.get(scheme)

    if legacy_format or not mode_max:
        payload = f"{idx}{mode}"
    else:
        payload = f"{idx}{mode}{mode_max}"

    if intent.src.id and seqn:
        # Actually in intent world, src is always there, but seqn is custom
        # logic. For parity, we'll map addr2=fan_id and use intent.dst for
        # fan_id if seqn is present
        pass

    # legacy from_attrs mapping
    if seqn:
        # I_, addr2=fan_id, seqn=seqn
        # CommandDTO doesn't accept seqn yet? Wait, CommandDTO has no seqn.
        # Oh, legacy builder took seqn and placed it. Wait, how do I set seqn in CommandDTO?
        # seqn is handled by the modem. CommandDTO doesn't have seqn!
        # DTO has verb, addr1, addr2, addr3, code, payload, priority, num_repeats.
        # wait! CommandDTO has no seqn, but PacketDTO has seqn.
        pass

    # Since CommandDTO lacks seqn, we ignore it for now.
    addr1, addr2, addr3 = resolve_addrs(intent.src, intent.dst)

    return CommandDTO(
        verb=I_,
        addr1=addr1,
        addr2=addr2,
        addr3=addr3,
        code=Code._22F1,
        payload=payload,
        priority=Priority.DEFAULT,
        num_repeats=DEFAULT_NUM_REPEATS,
    )


def build_set_bypass_position(intent: Command) -> CommandDTO:
    """Translate a SET_BYPASS_POSITION intent into a CommandDTO."""
    bypass_position = intent.get("bypass_position")
    bypass_mode = intent.get("bypass_mode")

    if bypass_mode and bypass_position is not None:
        raise ValueError(
            "bypass_mode and bypass_position are mutually exclusive, "
            "both cannot be provided, and neither is OK"
        )
    elif bypass_position is not None:
        pos = f"{int(bypass_position * 200):02X}"
    elif bypass_mode:
        pos = {"auto": "FF", "off": "00", "on": "C8"}[bypass_mode]
    else:
        pos = "FF"

    addr1, addr2, addr3 = resolve_addrs(intent.src, intent.dst)
    return CommandDTO(
        verb=W_,
        addr1=addr1,
        addr2=addr2,
        addr3=addr3,
        code=Code._22F7,
        payload=f"00{pos}",
        priority=Priority.DEFAULT,
        num_repeats=DEFAULT_NUM_REPEATS,
    )


def build_set_fan_param(intent: Command) -> CommandDTO:
    """Translate a SET_FAN_PARAM intent into a CommandDTO."""
    param_id = intent.get("param_id", "")
    value = intent.get("value")

    try:
        param_id = param_id.strip().upper()
        if len(param_id) != 2:
            raise ValueError("Parameter ID must be exactly 2 hexadecimal characters")
        int(param_id, 16)
    except ValueError as err:
        raise ValueError(
            f"Invalid parameter ID: '{param_id}'. "
            "Must be a 2-digit hexadecimal value (00-FF)"
        ) from err

    if (param_schema := _2411_PARAMS_SCHEMA.get(param_id)) is None:
        raise ValueError(
            f"Unknown parameter ID: '{param_id}'. "
            "This parameter is not defined in the device schema"
        )

    min_val = param_schema[SZ_MIN_VALUE]
    max_val = param_schema[SZ_MAX_VALUE]
    precision = param_schema.get(SZ_PRECISION, 1.0)
    data_type = param_schema.get(SZ_DATA_TYPE, "00")

    try:
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(
                f"Parameter {param_id}: Invalid value '{value}'. "
                "Must be a finite number"
            )

        if str(data_type) == "01":  # %
            value_scaled = int(round(float(value) / precision))
            min_val_scaled = int(round(float(min_val) / precision))
            max_val_scaled = int(round(float(max_val) / precision))
            precision_scaled = int(round(float(precision) * 10))
            trailer = "0032"
            if not min_val_scaled <= value_scaled <= max_val_scaled:
                raise ValueError(
                    f"Parameter {param_id}: Value {value_scaled / 10}% "
                    f"is out of allowed range ({min_val_scaled / 10}% "
                    f"to {max_val_scaled / 10}%)"
                )
        elif str(data_type) == "0F":  # %
            value_scaled = int(round((float(value) / 100.0) / float(precision)))
            min_val_scaled = int(round(float(min_val) / float(precision)))
            max_val_scaled = int(round(float(max_val) / float(precision)))
            precision_scaled = int(round(float(precision) * 200))
            trailer = "0032"
            if not min_val_scaled <= value_scaled <= max_val_scaled:
                raise ValueError(
                    f"Parameter {param_id}: Value {value_scaled / 2}% "
                    f"is out of allowed range ({min_val_scaled / 2}% "
                    f"to {max_val_scaled / 2}%)"
                )
        elif str(data_type) == "92":  # °C
            value_rounded = round(float(value) * 10) / 10
            value_scaled = int(value_rounded * 100)
            min_val_scaled = int(float(min_val) * 100)
            max_val_scaled = int(float(max_val) * 100)
            precision_scaled = int(float(precision) * 100)
            trailer = "0001"
            if not min_val_scaled <= value_scaled <= max_val_scaled:
                raise ValueError(
                    f"Parameter {param_id}: "
                    f"Temperature {value_scaled / 100:.1f}°C is out of "
                    f"allowed range ({min_val_scaled / 100:.1f}°C to "
                    f"{max_val_scaled / 100:.1f}°C)"
                )
        elif (
            (str(data_type) == "00")
            or (str(data_type) == "10")
            or (str(data_type) == "20")
            or (str(data_type) == "90")
        ):
            value_scaled = int(float(value))
            min_val_scaled = int(float(min_val))
            max_val_scaled = int(float(max_val))
            precision = 1
            precision_scaled = int(precision)
            trailer = "0001"
            if not min_val_scaled <= value_scaled <= max_val_scaled:
                unit = SZ_MINUTES if data_type == "00" else ""
                raise ValueError(
                    f"Parameter {param_id}: Value {value_scaled}"
                    f"{' ' + unit if unit else ''} is out of allowed "
                    f"range ({min_val_scaled} to {max_val_scaled}"
                    f"{' ' + unit if unit else ''})"
                )
        else:
            raise ValueError(
                f"Parameter {param_id}: Invalid data type '{data_type}'. "
                "Must be one of '00', '01', '0F', '10', '20', '90', or '92'"
            )

        leading = "00"
        param_id_hex = f"{int(param_id, 16):04X}"

        data_type_hex = f"00{data_type}"
        value_hex = f"{value_scaled:08X}"
        min_hex = f"{min_val_scaled:08X}"
        max_hex = f"{max_val_scaled:08X}"
        precision_hex = f"{precision_scaled:08X}"

        payload = (
            f"{leading}"
            f"{param_id_hex}"
            f"{data_type_hex}"
            f"{value_hex}"
            f"{min_hex}"
            f"{max_hex}"
            f"{precision_hex}"
            f"{trailer}"
        )
        payload = "".join(payload)

        # W_, addr0=src_id, addr1=fan_id, addr2=NON_DEV_ADDR
        return CommandDTO(
            verb=W_,
            addr1=intent.src.id,
            addr2=intent.dst.id,
            addr3=NON_DEV_ADDR.id,
            code=Code._2411,
            payload=payload,
            priority=Priority.DEFAULT,
            num_repeats=DEFAULT_NUM_REPEATS,
        )

    except (ValueError, TypeError) as err:
        raise ValueError(f"Invalid value: {value}") from err


def build_get_fan_param(intent: Command) -> CommandDTO:
    """Translate a GET_FAN_PARAM intent into a CommandDTO."""
    param_id = intent.get("param_id")
    if param_id is None:
        raise ValueError("Parameter ID cannot be None")

    if not isinstance(param_id, str):
        raise ValueError(
            f"Parameter ID must be a string, got {type(param_id).__name__}"
        )

    param_id_stripped = param_id.strip()
    if param_id != param_id_stripped:
        raise ValueError(
            f"Parameter ID cannot have leading or trailing whitespace: '{param_id}'"
        )

    try:
        if len(param_id) != 2:
            raise ValueError("Invalid length")
        int(param_id, 16)
    except ValueError as err:
        raise ValueError(
            f"Invalid parameter ID: '{param_id}'. "
            "Must be a 2-character hex string (00-FF)."
        ) from err

    payload = f"0000{param_id.upper()}"
    addr1, addr2, addr3 = resolve_addrs(intent.src, intent.dst)

    return CommandDTO(
        verb=RQ,
        addr1=addr1,
        addr2=addr2,
        addr3=addr3,
        code=Code._2411,
        payload=payload,
        priority=Priority.DEFAULT,
        num_repeats=DEFAULT_NUM_REPEATS,
    )


def build_get_hvac_fan_31da(intent: Command) -> CommandDTO:
    """Translate a GET_HVAC_FAN_31DA intent into a CommandDTO."""
    hvac_id = intent.get("hvac_id")
    bypass_position = intent.get("bypass_position")
    air_quality = intent.get("air_quality")
    co2_level = intent.get("co2_level")
    indoor_humidity = intent.get("indoor_humidity")
    outdoor_humidity = intent.get("outdoor_humidity")
    exhaust_temp = intent.get("exhaust_temp")
    supply_temp = intent.get("supply_temp")
    indoor_temp = intent.get("indoor_temp")
    outdoor_temp = intent.get("outdoor_temp")
    speed_capabilities = intent.get("speed_capabilities")
    fan_info = intent.get("fan_info")
    _unknown_fan_info_flags = intent.get("_unknown_fan_info_flags", [])
    exhaust_fan_speed = intent.get("exhaust_fan_speed")
    supply_fan_speed = intent.get("supply_fan_speed")
    remaining_mins = intent.get("remaining_mins")
    post_heat = intent.get("post_heat")
    pre_heat = intent.get("pre_heat")
    supply_flow = intent.get("supply_flow")
    exhaust_flow = intent.get("exhaust_flow")
    air_quality_basis = intent.get("air_quality_basis", "00")
    extra = intent.get("_extra", "")

    payload = hvac_id
    payload += f"{(int(air_quality * 200)):02X}" if air_quality is not None else "EF"
    payload += (
        f"{air_quality_code(air_quality_basis)}"
        if air_quality_basis is not None
        else "00"
    )
    payload += f"{co2_level:04X}" if co2_level is not None else "7FFF"
    payload += (
        hex_from_percent(indoor_humidity, high_res=False)
        if indoor_humidity is not None
        else "EF"
    )
    payload += (
        hex_from_percent(outdoor_humidity, high_res=False)
        if outdoor_humidity is not None
        else "EF"
    )
    payload += hex_from_temp(exhaust_temp) if exhaust_temp is not None else "7FFF"
    payload += hex_from_temp(supply_temp) if supply_temp is not None else "7FFF"
    payload += hex_from_temp(indoor_temp) if indoor_temp is not None else "7FFF"
    payload += hex_from_temp(outdoor_temp) if outdoor_temp is not None else "7FFF"
    payload += (
        f"{capability_bits(speed_capabilities):04X}"
        if speed_capabilities is not None
        else "7FFF"
    )
    payload += (
        hex_from_percent(bypass_position, high_res=True)
        if bypass_position is not None
        else "EF"
    )
    payload += (
        f"{(fan_info_to_byte(fan_info) | fan_info_flags(_unknown_fan_info_flags)):02X}"
        if fan_info is not None
        else "EF"
    )
    payload += (
        hex_from_percent(exhaust_fan_speed, high_res=True)
        if exhaust_fan_speed is not None
        else "FF"
    )
    payload += (
        hex_from_percent(supply_fan_speed, high_res=True)
        if supply_fan_speed is not None
        else "FF"
    )
    payload += f"{remaining_mins:04X}" if remaining_mins is not None else "7FFF"
    payload += f"{int(post_heat * 200):02X}" if post_heat is not None else "EF"
    payload += f"{int(pre_heat * 200):02X}" if pre_heat is not None else "EF"
    payload += f"{(int(supply_flow * 100)):04X}" if supply_flow is not None else "7FFF"
    payload += (
        f"{(int(exhaust_flow * 100)):04X}" if exhaust_flow is not None else "7FFF"
    )
    payload += extra

    addr1, addr2, addr3 = resolve_addrs(intent.src, intent.src)
    return CommandDTO(
        verb=I_,
        addr1=addr1,
        addr2=addr2,
        addr3=addr3,
        code=Code._31DA,
        payload=payload,
        priority=Priority.DEFAULT,
        num_repeats=DEFAULT_NUM_REPEATS,
    )
