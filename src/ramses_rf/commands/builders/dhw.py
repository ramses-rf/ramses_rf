"""RAMSES RF - DHW command intent to L3 payload translation."""

from ramses_rf.commands.builders.helpers import (
    _check_idx,
    _normalise_mode,
    _normalise_until,
    resolve_addrs,
)
from ramses_rf.commands.core import Command
from ramses_tx.const import (
    DEFAULT_NUM_REPEATS,
    I_,
    RQ,
    SZ_DHW_IDX,
    W_,
    ZON_MODE_MAP,
    Code,
    Priority,
)
from ramses_tx.dtos import CommandDTO
from ramses_tx.helpers import hex_from_dtm, hex_from_temp


def build_get_dhw_params(intent: Command) -> CommandDTO:
    """Translate a GET_DHW_PARAMS intent into a CommandDTO."""
    dhw_idx = _check_idx(intent.get(SZ_DHW_IDX, 0))
    addr1, addr2, addr3 = resolve_addrs(intent.src, intent.dst)
    return CommandDTO(
        verb=RQ,
        addr1=addr1,
        addr2=addr2,
        addr3=addr3,
        code=Code._10A0,
        payload=dhw_idx,
        priority=Priority.DEFAULT,
        num_repeats=DEFAULT_NUM_REPEATS,
    )


def build_set_dhw_params(intent: Command) -> CommandDTO:
    """Translate a SET_DHW_PARAMS intent into a CommandDTO."""
    dhw_idx = _check_idx(intent.get(SZ_DHW_IDX, 0))
    setpoint = intent.get("setpoint", 50.0)
    overrun = intent.get("overrun", 5)
    differential = intent.get("differential", 1.0)

    if not (30.0 <= setpoint <= 85.0):
        raise ValueError(f"Out of range, setpoint: {setpoint}")
    if not (0 <= overrun <= 10):
        raise ValueError(f"Out of range, overrun: {overrun}")
    if not (1 <= differential <= 10):
        raise ValueError(f"Out of range, differential: {differential}")

    addr1, addr2, addr3 = resolve_addrs(intent.src, intent.dst)
    payload = (
        f"{dhw_idx}{hex_from_temp(setpoint)}{overrun:02X}{hex_from_temp(differential)}"
    )
    return CommandDTO(
        verb=W_,
        addr1=addr1,
        addr2=addr2,
        addr3=addr3,
        code=Code._10A0,
        payload=payload,
        priority=Priority.DEFAULT,
        num_repeats=DEFAULT_NUM_REPEATS,
    )


def build_get_dhw_temp(intent: Command) -> CommandDTO:
    """Translate a GET_DHW_TEMP intent into a CommandDTO."""
    dhw_idx = _check_idx(intent.get(SZ_DHW_IDX, 0))
    addr1, addr2, addr3 = resolve_addrs(intent.src, intent.dst)
    return CommandDTO(
        verb=RQ,
        addr1=addr1,
        addr2=addr2,
        addr3=addr3,
        code=Code._1260,
        payload=dhw_idx,
        priority=Priority.DEFAULT,
        num_repeats=DEFAULT_NUM_REPEATS,
    )


def build_put_dhw_temp(intent: Command) -> CommandDTO:
    """Translate a PUT_DHW_TEMP intent into a CommandDTO."""
    from ramses_tx.const import DEV_TYPE_MAP

    dhw_idx = _check_idx(intent.get(SZ_DHW_IDX, 0))
    temperature = intent.get("temperature")

    if intent.src.id[:2] != DEV_TYPE_MAP.DHW:
        raise ValueError(
            f"Faked device {intent.src.id} has an unsupported device type: "
            f"device_id should be like {DEV_TYPE_MAP.DHW}:xxxxxx"
        )

    # I_ requires addr0=src, addr2=dst (which are the same for put_dhw_temp)
    addr1, addr2, addr3 = resolve_addrs(intent.src, intent.src)
    payload = f"{dhw_idx}{hex_from_temp(temperature)}"
    return CommandDTO(
        verb=I_,
        addr1=addr1,
        addr2=addr2,
        addr3=addr3,
        code=Code._1260,
        payload=payload,
        priority=Priority.DEFAULT,
        num_repeats=DEFAULT_NUM_REPEATS,
    )


def build_get_dhw_mode(intent: Command) -> CommandDTO:
    """Translate a GET_DHW_MODE intent into a CommandDTO."""
    dhw_idx = _check_idx(intent.get(SZ_DHW_IDX, 0))
    addr1, addr2, addr3 = resolve_addrs(intent.src, intent.dst)
    return CommandDTO(
        verb=RQ,
        addr1=addr1,
        addr2=addr2,
        addr3=addr3,
        code=Code._1F41,
        payload=dhw_idx,
        priority=Priority.DEFAULT,
        num_repeats=DEFAULT_NUM_REPEATS,
    )


def build_set_dhw_mode(intent: Command) -> CommandDTO:
    """Translate a SET_DHW_MODE intent into a CommandDTO."""
    dhw_idx = _check_idx(intent.get(SZ_DHW_IDX, 0))
    mode = intent.get("mode")
    active = intent.get("active")
    until = intent.get("until")
    duration = intent.get("duration")

    mode = _normalise_mode(mode, active, until, duration)

    if mode == ZON_MODE_MAP.FOLLOW:
        active = None
    if active is not None and not isinstance(active, (bool, int)):
        raise ValueError(f"Invalid args: active={active}, but must be a bool")

    until, duration = _normalise_until(mode, active, until, duration)

    payload = "".join(
        (
            dhw_idx,
            "FF" if active is None else "01" if bool(active) else "00",
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
        code=Code._1F41,
        payload=payload,
        priority=Priority.DEFAULT,
        num_repeats=DEFAULT_NUM_REPEATS,
    )
