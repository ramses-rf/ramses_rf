"""RAMSES RF - Zone command intent to L3 payload translation."""

from ramses_rf.commands.builders.helpers import resolve_addrs
from ramses_rf.commands.core import Command
from ramses_tx.command.base import _check_idx, _normalise_mode, _normalise_until
from ramses_tx.const import DEFAULT_NUM_REPEATS, RQ, W_, Code, Priority
from ramses_tx.dtos import CommandDTO
from ramses_tx.helpers import hex_from_dtm, hex_from_temp


def build_set_temperature(intent: Command) -> CommandDTO:
    """Translate an Action.SET_ZONE_SETPOINT intent into a CommandDTO.

    :param intent: The intent containing 'zone_idx' (int|str) and 'setpoint'
        (float).
    :return: A CommandDTO representing the intent.
    """
    zone_idx = intent.get("zone_idx")
    setpoint = intent.get("setpoint")

    if zone_idx is None or setpoint is None:
        raise ValueError("Missing 'zone_idx' or 'setpoint' in intent data")

    payload = f"{_check_idx(zone_idx)}{hex_from_temp(setpoint)}"
    addr1, addr2, addr3 = resolve_addrs(intent.src, intent.dst)

    return CommandDTO(
        verb=W_,
        addr1=addr1,
        addr2=addr2,
        addr3=addr3,
        code=Code._2309,
        payload=payload,
        priority=Priority.DEFAULT,
        num_repeats=DEFAULT_NUM_REPEATS,
    )


def build_set_mode(intent: Command) -> CommandDTO:
    """Translate an Action.SET_ZONE_MODE intent into a CommandDTO.

    :param intent: The intent containing 'zone_idx' (int|str), 'mode'
        (int|str|None), 'setpoint' (float|None), 'until' (str|datetime|None),
        and 'duration' (int|None).
    :return: A CommandDTO representing the intent.
    """
    zone_idx = intent.get("zone_idx")
    if zone_idx is None:
        raise ValueError("Missing 'zone_idx' in intent data")

    mode = intent.get("mode")
    setpoint = intent.get("setpoint")
    until = intent.get("until")
    duration = intent.get("duration")

    mode = _normalise_mode(mode, setpoint, until, duration)

    if setpoint is not None and not isinstance(setpoint, (float, int)):
        raise ValueError(f"Invalid args: setpoint={setpoint}, but must be a float")

    until, duration = _normalise_until(mode, setpoint, until, duration)

    payload = "".join(
        (
            _check_idx(zone_idx),
            hex_from_temp(setpoint),
            mode,
            "FFFFFF" if duration is None else f"{duration:06X}",
            "" if until is None else hex_from_dtm(until),
        )
    )
    addr1, addr2, addr3 = resolve_addrs(intent.src, intent.dst)

    return CommandDTO(
        verb=W_,
        addr1=addr1,
        addr2=addr2,
        addr3=addr3,
        code=Code._2349,
        payload=payload,
        priority=Priority.DEFAULT,
        num_repeats=DEFAULT_NUM_REPEATS,
    )


def build_set_name(intent: Command) -> CommandDTO:
    """Translate an Action.SET_ZONE_NAME intent into a CommandDTO.

    :param intent: The intent containing 'zone_idx' (int|str) and 'name' (str).
    :return: A CommandDTO representing the intent.
    """
    zone_idx = intent.get("zone_idx")
    name = intent.get("name")

    if zone_idx is None or name is None:
        raise ValueError("Missing 'zone_idx' or 'name' in intent data")

    from ramses_tx.helpers import hex_from_str

    payload = f"{_check_idx(zone_idx)}00{hex_from_str(name)[:40]:0<40}"
    addr1, addr2, addr3 = resolve_addrs(intent.src, intent.dst)

    return CommandDTO(
        verb=W_,
        addr1=addr1,
        addr2=addr2,
        addr3=addr3,
        code=Code._0004,
        payload=payload,
        priority=Priority.DEFAULT,
        num_repeats=DEFAULT_NUM_REPEATS,
    )


def build_set_config(intent: Command) -> CommandDTO:
    """Translate an Action.SET_ZONE_CONFIG intent into a CommandDTO.

    :param intent: The intent containing 'zone_idx' (int|str), 'min_temp' (float),
        'max_temp' (float), 'local_override' (bool), 'openwindow_function' (bool),
        and 'multiroom_mode' (bool).
    :return: A CommandDTO representing the intent.
    """
    zone_idx = intent.get("zone_idx")
    min_temp = intent.get("min_temp", 5.0)
    max_temp = intent.get("max_temp", 35.0)
    local_override = intent.get("local_override", False)
    openwindow_function = intent.get("openwindow_function", False)
    multiroom_mode = intent.get("multiroom_mode", False)

    if zone_idx is None:
        raise ValueError("Missing 'zone_idx' in intent data")

    if not (5 <= min_temp <= 21):
        raise ValueError(f"Out of range, min_temp: {min_temp}")
    if not (21 <= max_temp <= 35):
        raise ValueError(f"Out of range, max_temp: {max_temp}")
    if not isinstance(local_override, bool):
        raise ValueError(f"Invalid arg, local_override: {local_override}")
    if not isinstance(openwindow_function, bool):
        raise ValueError(f"Invalid arg, openwindow_function: {openwindow_function}")
    if not isinstance(multiroom_mode, bool):
        raise ValueError(f"Invalid arg, multiroom_mode: {multiroom_mode}")

    bitmap = 0 if local_override else 1
    bitmap |= 0 if openwindow_function else 2
    bitmap |= 0 if multiroom_mode else 16

    payload = "".join(
        (
            _check_idx(zone_idx),
            f"{bitmap:02X}",
            hex_from_temp(min_temp),
            hex_from_temp(max_temp),
        )
    )
    addr1, addr2, addr3 = resolve_addrs(intent.src, intent.dst)

    return CommandDTO(
        verb=W_,
        addr1=addr1,
        addr2=addr2,
        addr3=addr3,
        code=Code._000A,
        payload=payload,
        priority=Priority.DEFAULT,
        num_repeats=DEFAULT_NUM_REPEATS,
    )


def build_get_name(intent: Command) -> CommandDTO:
    zone_idx = intent.get("zone_idx")
    if zone_idx is None:
        raise ValueError("Missing 'zone_idx' in intent data")

    payload = f"{_check_idx(zone_idx)}00"
    addr1, addr2, addr3 = resolve_addrs(intent.src, intent.dst)

    return CommandDTO(
        verb=RQ,
        addr1=addr1,
        addr2=addr2,
        addr3=addr3,
        code=Code._0004,
        payload=payload,
        priority=Priority.DEFAULT,
        num_repeats=DEFAULT_NUM_REPEATS,
    )


def build_get_config(intent: Command) -> CommandDTO:
    zone_idx = intent.get("zone_idx")
    if zone_idx is None:
        raise ValueError("Missing 'zone_idx' in intent data")

    payload = f"{_check_idx(zone_idx)}"
    addr1, addr2, addr3 = resolve_addrs(intent.src, intent.dst)

    return CommandDTO(
        verb=RQ,
        addr1=addr1,
        addr2=addr2,
        addr3=addr3,
        code=Code._000A,
        payload=payload,
        priority=Priority.DEFAULT,
        num_repeats=DEFAULT_NUM_REPEATS,
    )


def build_get_window_state(intent: Command) -> CommandDTO:
    zone_idx = intent.get("zone_idx")
    if zone_idx is None:
        raise ValueError("Missing 'zone_idx' in intent data")

    payload = f"{_check_idx(zone_idx)}"
    addr1, addr2, addr3 = resolve_addrs(intent.src, intent.dst)

    return CommandDTO(
        verb=RQ,
        addr1=addr1,
        addr2=addr2,
        addr3=addr3,
        code=Code._12B0,
        payload=payload,
        priority=Priority.DEFAULT,
        num_repeats=DEFAULT_NUM_REPEATS,
    )


def build_get_setpoint(intent: Command) -> CommandDTO:
    zone_idx = intent.get("zone_idx")
    if zone_idx is None:
        raise ValueError("Missing 'zone_idx' in intent data")

    payload = f"{_check_idx(zone_idx)}"
    addr1, addr2, addr3 = resolve_addrs(intent.src, intent.dst)

    return CommandDTO(
        verb=RQ,
        addr1=addr1,
        addr2=addr2,
        addr3=addr3,
        code=Code._2309,
        payload=payload,
        priority=Priority.DEFAULT,
        num_repeats=DEFAULT_NUM_REPEATS,
    )


def build_get_mode(intent: Command) -> CommandDTO:
    zone_idx = intent.get("zone_idx")
    if zone_idx is None:
        raise ValueError("Missing 'zone_idx' in intent data")

    payload = f"{_check_idx(zone_idx)}"
    addr1, addr2, addr3 = resolve_addrs(intent.src, intent.dst)

    return CommandDTO(
        verb=RQ,
        addr1=addr1,
        addr2=addr2,
        addr3=addr3,
        code=Code._2349,
        payload=payload,
        priority=Priority.DEFAULT,
        num_repeats=DEFAULT_NUM_REPEATS,
    )


def build_get_temp(intent: Command) -> CommandDTO:
    zone_idx = intent.get("zone_idx")
    if zone_idx is None:
        raise ValueError("Missing 'zone_idx' in intent data")

    payload = f"{_check_idx(zone_idx)}"
    addr1, addr2, addr3 = resolve_addrs(intent.src, intent.dst)

    return CommandDTO(
        verb=RQ,
        addr1=addr1,
        addr2=addr2,
        addr3=addr3,
        code=Code._30C9,
        payload=payload,
        priority=Priority.DEFAULT,
        num_repeats=DEFAULT_NUM_REPEATS,
    )
