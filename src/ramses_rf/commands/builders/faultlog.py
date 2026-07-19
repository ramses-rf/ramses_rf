"""RAMSES RF - Faultlog command intent to L3 payload translation."""

from ramses_rf.commands.builders.helpers import resolve_addrs
from ramses_rf.commands.core import Command
from ramses_tx.const import DEFAULT_NUM_REPEATS, RQ, Code, Priority
from ramses_tx.dtos import CommandDTO


def build_get_faultlog_entry(intent: Command) -> CommandDTO:
    """Translate a GET_FAULTLOG_ENTRY intent into a CommandDTO.

    :param intent: The GET_FAULTLOG_ENTRY intent. It is expected to
        contain the `log_idx` key (int | str) in its data dictionary.
    :return: A populated CommandDTO.
    """
    log_idx = intent.get("log_idx")

    if log_idx is None:
        raise ValueError("Missing 'log_idx' in intent data")

    log_idx_int = log_idx if isinstance(log_idx, int) else int(log_idx, 16)
    payload = f"{log_idx_int:06X}"
    addr1, addr2, addr3 = resolve_addrs(intent.src, intent.dst)

    return CommandDTO(
        verb=RQ,
        addr1=addr1,
        addr2=addr2,
        addr3=addr3,
        code=Code._0418,
        payload=payload,
        priority=Priority.DEFAULT,
        num_repeats=DEFAULT_NUM_REPEATS,
    )


def build_put_faultlog_entry(intent: Command) -> CommandDTO:
    """Translate a PUT_FAULTLOG_ENTRY intent into a CommandDTO."""
    import enum
    from datetime import datetime as dt

    from ramses_tx.address import dev_id_to_hex_id
    from ramses_tx.const import FAULT_DEVICE_CLASS, FAULT_STATE, FAULT_TYPE, I_
    from ramses_tx.helpers import hex_from_dts

    fault_state = intent.get("fault_state")
    fault_type = intent.get("fault_type")
    device_class = intent.get("device_class")
    device_id = intent.get("device_id")
    domain_idx = intent.get("domain_idx", "00")
    log_idx = intent.get("log_idx", 0)
    timestamp = intent.get("timestamp")

    if isinstance(device_class, enum.Enum):
        device_class = {v: k for k, v in FAULT_DEVICE_CLASS.items()}[device_class]
    if device_class not in FAULT_DEVICE_CLASS:
        raise ValueError(f"Invalid device_class: {device_class}")

    if isinstance(fault_state, enum.Enum):
        fault_state = {v: k for k, v in FAULT_STATE.items()}[fault_state]
    if fault_state not in FAULT_STATE:
        raise ValueError(f"Invalid fault_state: {fault_state}")

    if isinstance(fault_type, enum.Enum):
        fault_type = {v: k for k, v in FAULT_TYPE.items()}[fault_type]
    if fault_type not in FAULT_TYPE:
        raise ValueError(f"Invalid fault_type: {fault_type}")

    if not isinstance(domain_idx, str) or len(domain_idx) != 2:
        raise ValueError(f"Invalid domain_idx: {domain_idx}")

    log_idx_str = f"{log_idx:02X}" if not isinstance(log_idx, str) else log_idx
    if not (0 <= int(log_idx_str, 16) <= 0x3F):
        raise ValueError(f"Invalid log_idx: {log_idx_str}")

    if timestamp is None:
        timestamp = dt.now()
    ts = hex_from_dts(timestamp)

    dev_id = dev_id_to_hex_id(device_id) if device_id else "000000"

    payload = "".join(
        (
            "00",
            fault_state,
            log_idx_str,
            "B0",
            fault_type,
            domain_idx,
            device_class,
            "0000",
            ts,
            "FFFF7000",
            dev_id,
        )
    )

    addr1, addr2, addr3 = resolve_addrs(intent.src, intent.dst)

    return CommandDTO(
        verb=I_,
        addr1=addr1,
        addr2=addr2,
        addr3=addr3,
        code=Code._0418,
        payload=payload,
        priority=Priority.DEFAULT,
        num_repeats=DEFAULT_NUM_REPEATS,
    )
