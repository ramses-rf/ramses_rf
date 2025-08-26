#!/usr/bin/env python3
"""RAMSES RF - Test the payload parsers and corresponding output.

Includes gwy dicts (schema, traits, params, status).
"""

from pathlib import Path, PurePath

import pytest

from ramses_rf import Gateway
from ramses_rf.helpers import shrink
from ramses_tx.message import Message
from ramses_tx.packet import Packet

from .helpers import (
    TEST_DIR,
    assert_expected,
    assert_expected_set,
    load_expected_results,
    load_test_gwy,
    shuffle_dict,
)

WORK_DIR = f"{TEST_DIR}/systems"


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    def id_fnc(param: Path) -> str:
        return PurePath(param).name

    folders = [f for f in Path(WORK_DIR).iterdir() if f.is_dir() and f.name[:1] != "_"]
    metafunc.parametrize("dir_name", folders, ids=id_fnc)


def test_payload_from_log_file(dir_name: Path) -> None:
    """Assert that each message payload is as expected (different to other tests)."""
    # RP --- 02:044328 18:200214 --:------ 2309 003 0007D0       # {'ufh_idx': '00', 'setpoint': 20.0}

    def proc_log_line(log_line: str) -> None:
        if "#" not in log_line:
            return
        pkt_line, pkt_eval = log_line.split("#", maxsplit=1)

        if not pkt_line[27:].strip():
            return

        pkt = Packet.from_file(pkt_line[:26], pkt_line[27:])
        msg = Message(pkt)

        assert msg.payload == eval(pkt_eval)

    with open(f"{dir_name}/packet.log") as f:
        while line := (f.readline()):
            if line.strip():
                proc_log_line(line)


async def test_schemax_with_log_file(dir_name: Path) -> None:
    """Compare the schema built from a log file with the expected results."""

    expected: dict = load_expected_results(dir_name) or {}
    gwy: Gateway = await load_test_gwy(
        dir_name, **expected["schema"], known_list=expected["known_list"]
    )

    schema, packets = gwy.get_state()

    # sert_expected_set(gwy, expected)
    assert_expected(shrink(schema), shrink(expected["schema"]))

    await gwy.stop()


async def test_systemx_from_log_file(dir_name: Path) -> None:
    """Compare the system built from a log file with the expected results."""

    expected: dict = load_expected_results(dir_name) or {}
    gwy: Gateway = await load_test_gwy(dir_name)

    assert_expected_set(gwy, expected)
    # sert shrink(gwy.schema) == shrink(schema)

    for dev in gwy.devices:
        _ = dev.schema
        _ = dev.traits
        _ = dev.params
        _ = dev.status

    for tcs in gwy.systems:
        _ = tcs.schema
        _ = tcs.traits
        _ = tcs.params
        _ = tcs.status

    await gwy.stop()


# async def test_restor1_from_log_file(dir_name: Path) -> None:
# """Compare the system built from a get_state log file with the expected results."""

# expected: dict = load_expected_results(dir_name) or {}
# gwy: Gateway = Gateway(None, input_file=io.StringIO())  # empty file

# # schema, packets = gwy.get_state(include_expired=True)
# await gwy._restore_cached_packets(packets)

# assert_expected_set(gwy, expected)
# # sert shrink(gwy.schema) == shrink(schema)

# await gwy.stop()


async def test_restore_from_log_file(dir_name: Path) -> None:
    """Compare the system built from a get_state log file with the expected results."""

    expected: dict = load_expected_results(dir_name) or {}
    gwy: Gateway = await load_test_gwy(dir_name)

    schema, packets = gwy.get_state(include_expired=True)
    await gwy._restore_cached_packets(packets)

    assert_expected_set(gwy, expected)
    # sert shrink(gwy.schema) == shrink(schema)

    await gwy.stop()

    for dev in gwy.devices:  # TODO: ZZZ project should pass this test
        if dev._gwy.msg_db:
            assert sorted(dev._msgs) == sorted(dev._msgs_), dev
            assert sorted(dev._msgz) == sorted(dev._msgz_), dev


async def test_shuffle_from_log_file(dir_name: Path) -> None:
    """Compare the system built from a shuffled log file with the expected results."""

    expected: dict = load_expected_results(dir_name) or {}
    gwy: Gateway = await load_test_gwy(dir_name)

    schema, packets = gwy.get_state(include_expired=True)
    packets = shuffle_dict(packets)
    await gwy._restore_cached_packets(packets)

    assert_expected_set(gwy, expected)
    # sert shrink(gwy.schema) == shrink(schema)

    packets = shuffle_dict(packets)
    await gwy._restore_cached_packets(packets)

    assert_expected_set(gwy, expected)
    # sert shrink(gwy.schema) == shrink(schema)

    await gwy.stop()
