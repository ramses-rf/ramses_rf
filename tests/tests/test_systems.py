#!/usr/bin/env python3
"""RAMSES RF - Test the payload parsers and corresponding output.

Includes gwy dicts (schema, traits, params, status).
"""

from pathlib import Path, PurePath

import pytest

from ramses_rf import Gateway
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

        if not (pkt_line := pkt_line.strip()):
            return

        # Skip lines that are too short to contain a valid packet (timestamp + space + frame)
        if len(pkt_line) < 27:
            return

        pkt = Packet.from_file(pkt_line[:26], pkt_line[27:])
        _ = Message(pkt)

        # assert msg.payload == eval(pkt_eval)  # TODO: not yet robust enough

    with open(f"{dir_name}/packet.log") as f:
        while line := f.readline():
            proc_log_line(line)


# Run Gateway tests with both legacy dicts and SQLite msg_db
# TODO(eb): remove legacy tests Q3 2026, as in tests/tests/test_api_schedule.py


async def test_schemax_with_log_file(dir_name: Path) -> None:
    """Compare the schema built from a log file with the expected results."""

    expected: dict = load_expected_results(dir_name) or {}
    gwy: Gateway = await load_test_gwy(
        dir_name, **expected["schema"], known_list=expected["known_list"]
    )

    schema, packets = await gwy.get_state()

    # assert shrink(gwy.schema) == shrink(expected["schema"])
    assert_expected(schema, expected["schema"])

    await gwy.stop()


async def test_schemax_with_log_file_sql(dir_name: Path) -> None:
    """Compare the schema built from a log file with the expected results, using SQLite msg_db."""

    expected: dict = load_expected_results(dir_name) or {}
    gwy: Gateway = await load_test_gwy(
        dir_name,
        **expected["schema"],
        known_list=expected["known_list"],
        _sqlite_index=True,
    )
    schema, packets = await gwy.get_state()

    # assert shrink(gwy.schema) == shrink(expected["schema"])
    assert_expected(schema, expected["schema"])

    await gwy.stop()


async def test_status_from_log_file(dir_name: Path) -> None:
    """Compare the system built from a log file with the expected results."""

    expected: dict = load_expected_results(dir_name) or {}
    gwy: Gateway = await load_test_gwy(
        dir_name, **expected["schema"], known_list=expected["known_list"]
    )

    # assert shrink(gwy.schema) == shrink(expected["schema"])
    if expected.get("status"):
        assert_expected(
            gwy.system_by_id["01:145038"].status,  # type: ignore[index]
            expected["status"],
        )

    await gwy.stop()


async def test_params_from_log_file(dir_name: Path) -> None:
    """Compare the system built from a log file with the expected results."""

    expected: dict = load_expected_results(dir_name) or {}
    gwy: Gateway = await load_test_gwy(
        dir_name, **expected["schema"], known_list=expected["known_list"]
    )

    # assert shrink(gwy.schema) == shrink(expected["schema"])
    if expected.get("params"):
        assert_expected(
            gwy.system_by_id["01:145038"].params,  # type: ignore[index]
            expected["params"],
        )

    await gwy.stop()


# async def test_restor1_from_log_file(dir_name: Path) -> None:
# """Compare the system built from a get_state log file with the expected results."""

# expected: dict = load_expected_results(dir_name) or {}
# gwy: Gateway = Gateway(None, input_file="")  # empty file, TODO skip reader

# # schema, packets = gwy.get_state(include_expired=True)
# await gwy._restore_cached_packets(packets)

# assert_expected_set(gwy, expected)

# await gwy.stop()


async def test_restore_from_log_file(dir_name: Path) -> None:
    """Compare the system built from a get_state log file with the expected results."""

    expected: dict = load_expected_results(dir_name) or {}
    gwy: Gateway = await load_test_gwy(dir_name)

    schema, packets = await gwy.get_state(include_expired=True)
    assert_expected(schema, expected["schema"])

    # packets = shuffle_dict(packets)
    await gwy._restore_cached_packets(packets)
    assert_expected(gwy.schema, expected["schema"])

    # for dev in gwy.devices:
    #     if hasattr(dev, "_msgs"):
    #         assert not [
    #             m for m in dev._msgs.values() if m.code == "3220"
    #         ], f"Assert 1: {dev} qry != _msgs_"
    #         # assert not dev._msgs.pop("3220", None), f"Assert 2: {dev} qry != _msgs_"
    #         # assert not getattr(
    #         #     dev, "qry", None
    #         # ), f"Assert 2: {dev} qry != _msgs_"

    await gwy.stop()


# @pytest.mark.skip(reason="Not strictly consistent")
async def test_restore_from_log_file_sql(dir_name: Path) -> None:
    """Compare the system built from a get_state log file with the expected results, using SQLite msg_db."""

    expected: dict = load_expected_results(dir_name) or {}
    gwy: Gateway = await load_test_gwy(dir_name, _sqlite_index=True)

    schema, packets = await gwy.get_state(include_expired=True)
    assert_expected(schema, expected["schema"])

    # packets = shuffle_dict(packets)
    await gwy._restore_cached_packets(packets)
    assert_expected(gwy.schema, expected["schema"])

    # for dev in gwy.devices:
    #     if hasattr(dev, "_msgs"):
    #         assert not [
    #             m for m in dev._msgs.values() if m.code == "3220"
    #         ], f"Assert 1: {dev} qry != _msgs_"
    #         # assert not dev._msgs.pop("3220", None), f"Assert 2: {dev} qry != _msgs_"
    #         # assert not getattr(
    #         #     dev, "qry", None
    #         # ), f"Assert 2: {dev} qry != _msgs_"

    await gwy.stop()


async def test_shuffle_from_log_file(dir_name: Path) -> None:
    """Compare the system built from a shuffled log file with the expected results."""

    expected: dict = load_expected_results(dir_name) or {}
    gwy: Gateway = await load_test_gwy(dir_name)

    schema, packets = await gwy.get_state(include_expired=True)
    packets = shuffle_dict(packets)
    await gwy._restore_cached_packets(packets)

    assert_expected_set(gwy, expected)
    # sert shrink(gwy.schema) == shrink(schema)

    packets = shuffle_dict(packets)
    await gwy._restore_cached_packets(packets)

    assert_expected_set(gwy, expected)
    # sert shrink(gwy.schema) == shrink(schema)

    await gwy.stop()


async def test_shuffle_from_log_file_sql(dir_name: Path) -> None:
    """Compare the system built from a shuffled log file with the expected results, using SQLite msg_db."""

    expected: dict = load_expected_results(dir_name) or {}
    gwy: Gateway = await load_test_gwy(dir_name, _sqlite_index=True)

    schema, packets = await gwy.get_state(include_expired=True)
    packets = shuffle_dict(packets)
    await gwy._restore_cached_packets(packets)

    assert_expected_set(gwy, expected)
    # sert shrink(gwy.schema) == shrink(schema)

    packets = shuffle_dict(packets)
    await gwy._restore_cached_packets(packets)

    assert_expected_set(gwy, expected)
    # sert shrink(gwy.schema) == shrink(schema)

    await gwy.stop()
