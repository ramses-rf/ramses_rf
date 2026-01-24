#!/usr/bin/env python3
"""RAMSES RF - Test the payload parsers and corresponding output.

Includes gwy dicts (schema, traits, params, status).
"""

import asyncio
import contextlib
from datetime import datetime as dt
from pathlib import Path, PurePath

import pytest

from ramses_rf import Gateway
from ramses_tx import exceptions as exc
from ramses_tx.message import Message
from ramses_tx.packet import Packet

from .helpers import (
    TEST_DIR,
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

        if not (pkt_line := pkt_line.strip()):
            return

        dtm = dt.now()
        # Parse timestamp if present (e.g. "2022-01-01T... ... RP --- ...")
        if " ... " in pkt_line:
            dtm_str, pkt_line = pkt_line.split(" ... ", maxsplit=1)
            with contextlib.suppress(ValueError):
                dtm = dt.fromisoformat(dtm_str)

        try:
            pkt = Packet(dtm, f"... {pkt_line}")
        except (ValueError, exc.PacketInvalid):
            return

        msg = Message(pkt)

        try:
            expected = eval(pkt_eval)
        except SyntaxError:
            return

        assert msg.payload == expected

    with open(f"{dir_name}/packet.log") as f:
        while line := f.readline():
            proc_log_line(line)


async def test_restore_from_log_file(dir_name: Path) -> None:
    """Compare the system built from a log file with the expected results."""

    expected: dict = load_expected_results(dir_name) or {}
    gwy: Gateway = await load_test_gwy(dir_name)

    # assert_expected(gwy, expected)
    assert_expected_set(gwy, expected)

    await gwy.stop()


async def test_restore_from_log_file_sql(dir_name: Path) -> None:
    """Compare the system built from a log file with the expected results, using SQLite msg_db."""

    expected: dict = load_expected_results(dir_name) or {}
    gwy: Gateway = await load_test_gwy(dir_name, _sqlite_index=True)

    # assert_expected(gwy, expected)
    assert_expected_set(gwy, expected)

    await gwy.stop()


async def test_shuffle_from_log_file(dir_name: Path) -> None:
    """Compare the system built from a shuffled log file with the expected results."""

    expected: dict = load_expected_results(dir_name) or {}
    gwy: Gateway = await load_test_gwy(dir_name)

    schema, packets = await gwy.get_state(include_expired=True)
    packets = shuffle_dict(packets)
    await gwy._restore_cached_packets(packets)
    if gwy.msg_db:
        gwy.msg_db.flush()
    await asyncio.sleep(0.05)  # Let loop process updates

    assert_expected_set(gwy, expected)

    await gwy.stop()


async def test_shuffle_from_log_file_sql(dir_name: Path) -> None:
    """Compare the system built from a shuffled log file with the expected results, using SQLite msg_db."""

    expected: dict = load_expected_results(dir_name) or {}
    gwy: Gateway = await load_test_gwy(dir_name, _sqlite_index=True)

    schema, packets = await gwy.get_state(include_expired=True)
    packets = shuffle_dict(packets)
    await gwy._restore_cached_packets(packets)
    if gwy.msg_db:
        gwy.msg_db.flush()
    await asyncio.sleep(0.05)  # Let loop process updates

    assert_expected_set(gwy, expected)

    await gwy.stop()


async def test_fuzz_from_log_file(dir_name: Path) -> None:
    """Compare the system built from a fuzzed log file with the expected results."""

    expected: dict = load_expected_results(dir_name) or {}
    gwy: Gateway = await load_test_gwy(dir_name)

    # for dev in gwy.devices:
    #     if dev._msgs:
    #         assert dev._msgs == gwy.msg_db.get(
    #             src=dev.id, dtms=list(dev._msgs.keys())
    #         ), f"Assert 1: {dev} qry != _msgs_"

    schema, packets = await gwy.get_state(include_expired=True)

    # This loop is non-deterministic, but should be stable (fails rarely)
    # The logic is that the system state should be consistent regardless of the order
    # of the packets (within reason)
    for _ in range(3):
        packets = shuffle_dict(packets)
        await gwy._restore_cached_packets(packets)
        if gwy.msg_db:
            gwy.msg_db.flush()
        await asyncio.sleep(0.05)  # Let loop process updates

        assert_expected_set(gwy, expected)

    # for dev in gwy.devices:
    #     if dev._msgs:
    #         assert dev._msgs == gwy.msg_db.get(
    #             src=dev.id, dtms=list(dev._msgs.keys())
    #         ), f"Assert 2: {dev} qry != _msgs_"

    await gwy.stop()


async def test_fuzz_from_log_file_sql(dir_name: Path) -> None:
    """Compare the system built from a fuzzed log file with the expected results, using SQLite msg_db."""

    expected: dict = load_expected_results(dir_name) or {}
    gwy: Gateway = await load_test_gwy(dir_name, _sqlite_index=True)

    # for dev in gwy.devices:
    #     if dev._msgs:
    #         assert dev._msgs == gwy.msg_db.get(
    #             src=dev.id, dtms=list(dev._msgs.keys())
    #         ), f"Assert 1: {dev} qry != _msgs_"

    schema, packets = await gwy.get_state(include_expired=True)

    # This loop is non-deterministic, but should be stable (fails rarely)
    # The logic is that the system state should be consistent regardless of the order
    # of the packets (within reason)
    for _ in range(3):
        packets = shuffle_dict(packets)
        await gwy._restore_cached_packets(packets)
        if gwy.msg_db:
            gwy.msg_db.flush()
        await asyncio.sleep(0.05)  # Let loop process updates

        assert_expected_set(gwy, expected)

    # for dev in gwy.devices:
    #     if dev._msgs:
    #         assert dev._msgs == gwy.msg_db.get(
    #             src=dev.id, dtms=list(dev._msgs.keys())
    #         ), f"Assert 2: {dev} qry != _msgs_"

    await gwy.stop()
