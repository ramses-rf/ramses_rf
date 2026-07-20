#!/usr/bin/env python3
"""RAMSES RF - Test the Faultlog functions."""

import random
from datetime import datetime as dt
from typing import Any

from ramses_rf import Address, Command, Message, Packet
from ramses_rf.systems.faultlog import FaultLog, FaultLogEntry
from ramses_tx.address import HGI_DEVICE_ID, NON_DEV_ADDR
from ramses_tx.command_legacy_shim import LegacyCommandShim
from ramses_tx.const import SZ_LOG_ENTRY, FaultDeviceClass, FaultState, FaultType
from ramses_tx.dtos import CommandDTO

from ramses_tx.const import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    I_,
    RP,
    RQ,
    W_,
    Code,
)
from ramses_tx.typing import DeviceIdT, LogIdxT

from .helpers import TEST_DIR


def _put_system_log_entry(
    ctl_id: DeviceIdT | str,
    fault_state: str,
    fault_type: str,
    device_class: str,
    device_id: DeviceIdT | str | None = None,
    domain_idx: int | str = "00",
    _log_idx: int | str | None = None,
    timestamp: dt | str | None = None,
    **kwargs: Any,
) -> Command:
    from ramses_rf.commands.builders import build_dto
    from ramses_rf.commands.core import Command as Intent
    from ramses_rf.enums import Action
    from ramses_tx.command_legacy_shim import LegacyCommandShim

    return LegacyCommandShim.from_dto(
        build_dto(
            Intent(
                src=Address(HGI_DEVICE_ID),
                dst=Address(ctl_id),
                action=Action.PUT_FAULTLOG_ENTRY,
                data={
                    "fault_state": fault_state,
                    "fault_type": fault_type,
                    "device_class": device_class,
                    "device_id": device_id,
                    "domain_idx": domain_idx,
                    "log_idx": _log_idx,
                    "timestamp": timestamp,
                },
            )
        )
    )


WORK_DIR = f"{TEST_DIR}/parsers"


# ### TEST DATA #######################################################################

CTL_ID = Address("01:145038").id
HGI_ID = HGI_DEVICE_ID


def _fault_log_entry(
    *args: Any, timestamp: str | None = None, **kwargs: Any
) -> FaultLogEntry:
    if timestamp is None:
        timestamp = dt.strftime(dt.now(), "%y-%m-%dT%H:%M:%S")

    return FaultLogEntry(
        timestamp=timestamp,
        fault_state=args[0],
        fault_type=args[1],
        domain_idx=kwargs.get("domain_idx", "00"),
        device_class=args[2],
        device_id=kwargs.get("device_id"),
    )


# Keys corresponding to order in the faultlog
TEST_FAULTS: dict[LogIdxT, FaultLogEntry] = {}

TEST_FAULTS["00"] = _fault_log_entry(
    FaultState.RESTORE,
    FaultType.BATTERY_ERROR,
    FaultDeviceClass.CONTROLLER,
    timestamp="21-12-23T00:59:00",
)
TEST_FAULTS["01"] = _fault_log_entry(
    FaultState.FAULT,
    FaultType.BATTERY_ERROR,
    FaultDeviceClass.CONTROLLER,
    timestamp="21-12-23T00:58:01",
)
TEST_FAULTS["02"] = _fault_log_entry(
    FaultState.RESTORE,
    FaultType.BATTERY_LOW,
    FaultDeviceClass.ACTUATOR,
    timestamp="21-12-23T00:57:02",
    device_id="04:111111",
    domain_idx="03",
)
TEST_FAULTS["03"] = _fault_log_entry(
    FaultState.FAULT,
    FaultType.BATTERY_LOW,
    FaultDeviceClass.ACTUATOR,
    timestamp="21-12-23T00:56:03",
    device_id="04:111111",
    domain_idx="03",
)
TEST_FAULTS["04"] = _fault_log_entry(
    FaultState.RESTORE,
    FaultType.COMMS_FAULT,
    FaultDeviceClass.SETPOINT,
    timestamp="21-12-23T00:55:04",
    device_id="03:123456",
    domain_idx="06",
)
TEST_FAULTS["05"] = _fault_log_entry(
    FaultState.FAULT,
    FaultType.COMMS_FAULT,
    FaultDeviceClass.SETPOINT,
    timestamp="21-12-23T00:54:05",
    device_id="03:123456",
    domain_idx="06",
)

EXPECTED_MAP = {
    0: "21-12-23T00:59:00",
    1: "21-12-23T00:58:01",
    2: "21-12-23T00:57:02",
    3: "21-12-23T00:56:03",
    4: "21-12-23T00:55:04",
    5: "21-12-23T00:54:05",
}


# ### FIXTURES, ETC ###################################################################


class EvohomeStub:
    def __init__(self, ctl_id: DeviceIdT) -> None:
        self.id = ctl_id
        self._gwy = None


def _proc_log_line(log_line: str) -> None:
    try:
        pkt = Packet.from_file(log_line[:26], log_line[27:])
    except ValueError:
        return

    # Utilize the correct shim to cast directly to PacketDTO
    msg = Message._from_pkt(pkt)
    if msg.code != Code._0418 or not msg.payload.get(SZ_LOG_ENTRY):
        return

    entry = FaultLogEntry.from_msg(msg)
    assert entry


def _proc_null_fault_entry(fault_log: FaultLog, _log_idx: LogIdxT = "00") -> None:
    """Return a 0418 packet with no entry."""
    cmd = LegacyCommandShim.from_dto(
        CommandDTO(
            verb=I_,
            addr1=HGI_DEVICE_ID,
            addr2=NON_DEV_ADDR.id,
            addr3=CTL_ID,
            code=Code._0418,
            payload=f"0000{_log_idx}B0000000000000000000007FFFFF7000000000",
        )
    )
    fault_log.handle_msg(Message._from_cmd(cmd))


def _proc_test_fault_entry(
    fault_log: FaultLog, text_idx: LogIdxT, _log_idx: LogIdxT = "00"
) -> None:
    entry: FaultLogEntry = TEST_FAULTS[text_idx]

    cmd = _put_system_log_entry(
        CTL_ID,
        entry.fault_state,
        entry.fault_type,
        entry.device_class,
        device_id=entry.device_id,
        domain_idx=entry.domain_idx,
        _log_idx=_log_idx,
        timestamp=entry.timestamp,
    )
    fault_log.handle_msg(Message._from_cmd(cmd))


# ### TESTS ###########################################################################


def test_faultlog_entries() -> None:
    """Test instantiation of faultlog entries."""

    with open(f"{WORK_DIR}/code_0418.log") as f:
        while line := (f.readline()):
            _proc_log_line(line)


def test_faultlog_instantiation_0() -> None:
    """Log entries arrive in order of timestamp (i.e. as they'd occur)."""

    fault_log = FaultLog(EvohomeStub(CTL_ID))  # type: ignore[type-var]

    # log entries arrive in order of timestamp (i.e. as they'd occur)
    for i in reversed(range(len(TEST_FAULTS))):
        _proc_test_fault_entry(fault_log, f"{i:02}")  # _log_idx="00")

    # assert sorted(fault_log._log.keys(), reverse=True) == list(EXPECTED_MAP.values())
    assert fault_log._map == EXPECTED_MAP


def test_faultlog_instantiation_1() -> None:
    """Log entries arrive in order of log_idx (e.g. enumerating the log via RQs)."""

    fault_log = FaultLog(EvohomeStub(CTL_ID))  # type: ignore[type-var]

    # log entries arrive in order of log_idx (e.g. enumerating the log via RQs)
    for i in reversed(range(len(TEST_FAULTS))):
        _proc_test_fault_entry(fault_log, f"{i:02}", _log_idx=f"{i:02}")

    assert sorted(fault_log._log.keys(), reverse=True) == list(EXPECTED_MAP.values())
    assert fault_log._map == EXPECTED_MAP


def test_faultlog_instantiation_2() -> None:
    """Log entries arrive in random order albeit with their correct log_idx."""

    fault_log = FaultLog(EvohomeStub(CTL_ID))  # type: ignore[type-var]

    # log entries arrive in random order albeit with their correct log_idx
    numbers = list(range(len(TEST_FAULTS)))
    random.shuffle(numbers)

    for i in numbers:
        _proc_test_fault_entry(fault_log, f"{i:02}", _log_idx=f"{i:02}")

    assert sorted(fault_log._log.keys(), reverse=True) == list(EXPECTED_MAP.values())
    assert fault_log._map == EXPECTED_MAP


def test_faultlog_instantiation_3() -> None:
    """Log entries arrive in an order set to confuse."""

    fault_log = FaultLog(EvohomeStub(CTL_ID))  # type: ignore[type-var]

    # a log with two entries arrives in order
    _proc_test_fault_entry(fault_log, "05")
    _proc_test_fault_entry(fault_log, "04")

    assert fault_log._map == {
        0: "21-12-23T00:55:04",
        1: "21-12-23T00:54:05",
    }

    # the two entries arrives out of order
    _proc_test_fault_entry(fault_log, "05", _log_idx="01")
    _proc_test_fault_entry(fault_log, "04", _log_idx="00")

    assert fault_log._map == {
        0: "21-12-23T00:55:04",
        1: "21-12-23T00:54:05",
    }

    # the log is cleared
    _proc_null_fault_entry(fault_log)

    assert fault_log._map == {}

    # a log with three entries is enumerated, kinda
    _proc_test_fault_entry(fault_log, "03", _log_idx="00")
    # roc_fault_entry(fault_log, "04", _log_idx="01")  # went missing
    _proc_test_fault_entry(fault_log, "05", _log_idx="02")

    assert fault_log._map == {
        0: "21-12-23T00:56:03",
        2: "21-12-23T00:54:05",
    }

    # the missing entry arrives, only after a new entry
    _proc_test_fault_entry(fault_log, "02")  # pushes others down
    _proc_test_fault_entry(fault_log, "01")  # pushes others down
    # _log_idx was 01, above
    _proc_test_fault_entry(fault_log, "04", _log_idx="03")

    assert fault_log._map == {
        0: "21-12-23T00:58:01",
        1: "21-12-23T00:57:02",
        2: "21-12-23T00:56:03",
        3: "21-12-23T00:55:04",
        4: "21-12-23T00:54:05",
    }

    # a new entry
    _proc_test_fault_entry(fault_log, "00")  # pushes others down

    assert fault_log._map == {
        0: "21-12-23T00:59:00",
        1: "21-12-23T00:58:01",
        2: "21-12-23T00:57:02",
        3: "21-12-23T00:56:03",
        4: "21-12-23T00:55:04",
        5: "21-12-23T00:54:05",
    }


def test_faultlog_instantiation_4() -> None:
    """Log entries arrive in an order set to confuse."""

    fault_log = FaultLog(EvohomeStub(CTL_ID))  # type: ignore[type-var]

    # a log with three entries is enumerated, kinda
    _proc_test_fault_entry(fault_log, "03", _log_idx="00")
    # roc_fault_entry(fault_log, "04", _log_idx="01")  # went missing
    _proc_test_fault_entry(fault_log, "05", _log_idx="02")

    assert fault_log._map == {
        0: "21-12-23T00:56:03",
        2: "21-12-23T00:54:05",
    }

    _proc_test_fault_entry(fault_log, "02", _log_idx="02")  # pushes others down

    assert fault_log._map == {
        2: "21-12-23T00:57:02",
        3: "21-12-23T00:56:03",
        5: "21-12-23T00:54:05",
    }

    _proc_test_fault_entry(fault_log, "01")

    assert fault_log._map == {
        0: "21-12-23T00:58:01",
        2: "21-12-23T00:57:02",
        3: "21-12-23T00:56:03",
        5: "21-12-23T00:54:05",
    }

    _proc_test_fault_entry(fault_log, "01", _log_idx="01")

    assert fault_log._map == {
        1: "21-12-23T00:58:01",
        2: "21-12-23T00:57:02",
        3: "21-12-23T00:56:03",
        5: "21-12-23T00:54:05",
    }

    # _log_idx was 01, above
    _proc_test_fault_entry(fault_log, "04", _log_idx="04")

    assert fault_log._map == {
        1: "21-12-23T00:58:01",
        2: "21-12-23T00:57:02",
        3: "21-12-23T00:56:03",
        4: "21-12-23T00:55:04",
        5: "21-12-23T00:54:05",
    }
