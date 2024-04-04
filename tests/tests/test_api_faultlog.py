#!/usr/bin/env python3
#
"""RAMSES RF - Test the Faultlog functions."""

import random
from datetime import datetime as dt

from ramses_rf import Address, Command, Message, Packet
from ramses_rf.system.faultlog import FaultLog, FaultLogEntry
from ramses_tx.address import HGI_DEVICE_ID
from ramses_tx.const import SZ_LOG_ENTRY, FaultDeviceClass, FaultState, FaultType
from ramses_tx.schemas import DeviceIdT
from ramses_tx.typed_dicts import LogIdxT
from tests.helpers import TEST_DIR

from ramses_tx.const import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    I_,
    RP,
    RQ,
    W_,
    Code,
)

WORK_DIR = f"{TEST_DIR}/parsers"


# ### TEST DATA #######################################################################

CTL_ID = Address("01:145038").id
HGI_ID = HGI_DEVICE_ID


def _fault_log_entry(*args, timestamp: str | None = None, **kwargs) -> FaultLogEntry:
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
TEST_FAULTS: dict[LogIdxT:FaultLogEntry] = {}  # type: ignore[assignment

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


class Controller:
    def __init__(self, ctl_id: DeviceIdT) -> None:
        self.id = ctl_id
        self._gwy = None


def _proc_log_line(pkt_line):
    try:
        pkt = Packet.from_file(pkt_line[:26], pkt_line[27:])
    except ValueError:
        return

    msg = Message(pkt)
    if msg.code != Code._0418 or not msg.payload.get(SZ_LOG_ENTRY):
        return

    entry = FaultLogEntry.from_msg(msg)
    assert entry


def _proc_null_fault_entry(fault_log: FaultLog, _log_idx: LogIdxT = "00") -> Command:
    """Return a 0418 packet with no entry."""
    cmd = Command.from_attrs(
        I_, CTL_ID, Code._0418, f"0000{_log_idx}B0000000000000000000007FFFFF7000000000"
    )
    fault_log._handle_msg(Message(Packet._from_cmd(cmd)))


def _proc_test_fault_entry(
    fault_log: FaultLog, text_idx: LogIdxT, _log_idx: LogIdxT = "00"
):
    entry: FaultLogEntry = TEST_FAULTS[text_idx]

    cmd = Command._put_system_log_entry(
        CTL_ID,
        entry.fault_state,
        entry.fault_type,
        entry.device_class,
        device_id=entry.device_id,
        domain_idx=entry.domain_idx,
        _log_idx=_log_idx,
        timestamp=entry.timestamp,
    )
    fault_log._handle_msg(Message(Packet._from_cmd(cmd)))


# ### TESTS ###########################################################################


def test_faultlog_entries():
    """Test instantiation of faultlog entries."""

    with open(f"{WORK_DIR}/code_0418.log") as f:
        while line := (f.readline()):
            _proc_log_line(line)


def test_faultlog_instantiation_0():
    """Log entries arrive in order of timestamp (i.e. as they'd occur)."""

    fault_log = FaultLog(Controller(CTL_ID))

    # log entries arrive in order of timestamp (i.e. as they'd occur)
    for i in reversed(range(len(TEST_FAULTS))):
        _proc_test_fault_entry(fault_log, f"{i:02}")  # _log_idx="00")

    # assert sorted(fault_log._log.keys(), reverse=True) == list(EXPECTED_MAP.values())
    assert fault_log._map == EXPECTED_MAP


def test_faultlog_instantiation_1():
    """Log entries arrive in order of log_idx (e.g. enumerating the log via RQs)."""

    fault_log = FaultLog(Controller(CTL_ID))

    # log entries arrive in order of log_idx (e.g. enumerating the log via RQs)
    for i in reversed(range(len(TEST_FAULTS))):
        _proc_test_fault_entry(fault_log, f"{i:02}", _log_idx=f"{i:02}")

    assert sorted(fault_log._log.keys(), reverse=True) == list(EXPECTED_MAP.values())
    assert fault_log._map == EXPECTED_MAP


def test_faultlog_instantiation_2():
    """Log entries arrive in random order albeit with their correct log_idx."""

    fault_log = FaultLog(Controller(CTL_ID))

    # log entries arrive in random order albeit with their correct log_idx
    numbers = list(range(len(TEST_FAULTS)))
    random.shuffle(numbers)

    for i in numbers:
        _proc_test_fault_entry(fault_log, f"{i:02}", _log_idx=f"{i:02}")

    assert sorted(fault_log._log.keys(), reverse=True) == list(EXPECTED_MAP.values())
    assert fault_log._map == EXPECTED_MAP


def test_faultlog_instantiation_3():
    """Log entries arrive in an order set to confuse."""

    fault_log = FaultLog(Controller(CTL_ID))

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
    _proc_test_fault_entry(fault_log, "04", _log_idx="03")  # _log_idx was 01, above

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


def test_faultlog_instantiation_4():
    """Log entries arrive in an order set to confuse."""

    fault_log = FaultLog(Controller(CTL_ID))

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

    _proc_test_fault_entry(fault_log, "04", _log_idx="04")  # _log_idx was 01, above

    assert fault_log._map == {
        1: "21-12-23T00:58:01",
        2: "21-12-23T00:57:02",
        3: "21-12-23T00:56:03",
        4: "21-12-23T00:55:04",
        5: "21-12-23T00:54:05",
    }
