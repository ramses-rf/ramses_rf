#!/usr/bin/env python3
"""RAMSES RF - Test the payload parsers."""

import logging
from datetime import datetime as dt
from pathlib import Path, PurePath
from typing import cast

import pytest

from ramses_rf.messages import Message
from ramses_rf.parsers.heating import (
    parser_2d49,
    parser_30c9,
    parser_3150,
    parser_3ef0,
)
from ramses_tx.const import Code
from ramses_tx.exceptions import PacketInvalid
from ramses_tx.packet import Packet

from .helpers import TEST_DIR

WORK_DIR = f"{TEST_DIR}/parsers"

HAS_ARRAY = "has_array"
HAS_IDX = "has_idx"
HAS_PAYLOAD = "has_payload"
IS_FRAGMENT = "is_fragment"
META_KEYS = (HAS_ARRAY, HAS_IDX, HAS_PAYLOAD, IS_FRAGMENT)


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "f_name" not in metafunc.fixturenames:
        return

    def id_fnc(param: Path) -> str:
        return PurePath(param).name

    metafunc.parametrize("f_name", sorted(Path(WORK_DIR).glob("*.log")), ids=id_fnc)


def _proc_log_line(log_line: str) -> None:
    pkt_line, pkt_eval, *_ = list(
        map(str.strip, log_line.split("#", maxsplit=1) + [""])
    )

    if not pkt_line:
        return

    pkt = Packet.from_file(pkt_line[:26], pkt_line[27:])

    try:
        msg = Message(pkt.to_dto())
    except PacketInvalid:
        # If the log line didn't expect a valid payload (wip logs), ignore it
        if not pkt_eval:
            return

        # The new L7 strict decoding raises PacketInvalid instead of returning
        # a payload dictionary with a "_parse_error" key.
        if "_parse_error" in pkt_eval:
            return

        raise

    # assert bool(msg._is_fragment) == pkt._is_fragment
    # assert bool(msg._idx): dict == pkt._idx: Optional[bool | str]
    # not useful

    if not pkt_eval:
        return
    try:
        pkt_dict = eval(pkt_eval)
    except SyntaxError:
        if "{" in pkt_eval:  # if so, there is an issue with the log line
            raise  # that should be addressed
        return

    if isinstance(pkt_dict, list) or not any(k for k in pkt_dict if k in META_KEYS):
        payload = msg.payload

        keys_to_strip = (
            "zone_idx",
            "domain_id",
            "dhw_idx",
            "hvac_id",
            "ufh_idx",
            "other_idx",
        )

        # Safely align the payload for comparison against legacy logs
        if isinstance(payload, dict) and isinstance(pkt_dict, dict):
            payload = dict(payload)
            for key in keys_to_strip:
                if key in payload and key not in pkt_dict:
                    del payload[key]

        # Apply the same stripping logic if the payload is an array of dicts
        elif isinstance(payload, list) and isinstance(pkt_dict, list):
            payload = list(payload)
            for i, item in enumerate(payload):
                if (
                    isinstance(item, dict)
                    and i < len(pkt_dict)
                    and isinstance(pkt_dict[i], dict)
                ):
                    item = dict(item)
                    for key in keys_to_strip:
                        if key in item and key not in pkt_dict[i]:
                            del item[key]
                    payload[i] = item

        # NOTE: For compatibility with legacy test logs where 1-byte "00"
        # was `{}`.
        if pkt_dict == {} and payload == {"heartbeat": True}:
            return

        assert payload == pkt_dict, pkt_line
        return

    assert HAS_ARRAY not in pkt_dict or pkt._has_array == pkt_dict[HAS_ARRAY]
    assert HAS_IDX not in pkt_dict or pkt._idx == pkt_dict[HAS_IDX]
    assert HAS_PAYLOAD not in pkt_dict or pkt._has_payload == pkt_dict[HAS_PAYLOAD]
    assert IS_FRAGMENT not in pkt_dict or pkt._is_fragment == pkt_dict[IS_FRAGMENT]


def _proc_log_line_pair_4e15(log_line: str, prev_msg: Message | None) -> Message | None:
    pkt_line, *_ = list(map(str.strip, log_line.split("#", maxsplit=1) + [""]))

    if not pkt_line:
        return None

    pkt = Packet.from_file(pkt_line[:26], pkt_line[27:])

    try:
        this_msg = Message(pkt.to_dto())
    except PacketInvalid:
        return None

    if not prev_msg or prev_msg.code != Code._4E15:
        return this_msg

    if this_msg.code != Code._3EF0:
        return None

    assert prev_msg.payload["is_cooling"] == this_msg.payload["cool_active"]
    assert prev_msg.payload["is_heating"] == this_msg.payload["ch_active"]
    assert prev_msg.payload["is_dhw_ing"] == this_msg.payload["dhw_active"]

    return this_msg


def test_parsers_from_log_files(f_name: Path) -> None:
    with open(f_name) as f:
        while line := (f.readline()):
            _proc_log_line(line)


@pytest.mark.parametrize(
    ("payload", "zone_idx", "cooling_demand"),
    (
        ("1EC800", "1E", True),
        ("200000", "20", False),
        ("23C800", "23", True),
        ("880000", "88", False),
        ("FDC800", "FD", True),
    ),
)
def test_parser_2d49_hcc100_cooling_demand(
    payload: str, zone_idx: str, cooling_demand: bool
) -> None:
    """Parse active/inactive cooling demand for representative HCC100 zones."""
    result = parser_2d49(payload, cast(Message, None))

    assert result == {
        "zone_idx": zone_idx,
        "cooling_demand": cooling_demand,
    }


def test_parser_2d49_unknown_demand_warns_and_defaults_inactive(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unknown 2D49 demand bytes are warned about and safely treated as inactive."""
    with caplog.at_level(logging.WARNING, logger="ramses_rf.parsers.heating"):
        result = parser_2d49("1E6400", cast(Message, None))

    assert result == {"zone_idx": "1E", "cooling_demand": False}
    assert "Unknown 2D49 cooling demand byte: 64 (payload=1E6400)" in caplog.text


@pytest.mark.parametrize(
    ("relay_byte", "expected"),
    (
        ("10", {"pump_relay_state": "cooling"}),
        ("02", {"pump_relay_state": "heating"}),
        ("00", {"pump_relay_state": "off"}),
        (
            "12",
            {"pump_relay_state": "cooling", "relay_byte_raw": 0x12},
        ),
        (
            "15",
            {"pump_relay_state": "cooling", "relay_byte_raw": 0x15},
        ),
    ),
)
def test_parser_3ef0_ufc_pump_relay_examples(
    relay_byte: str, expected: dict[str, str | int]
) -> None:
    """Nine-byte UFC 3EF0 packets decode pump relay flags from byte three."""
    payload = f"000000{relay_byte}0000000000"
    msg = Message._from_pkt(
        Packet(
            dt.now(),
            f"...  I --- 02:000001 --:------ 02:000001 3EF0 009 {payload}",
        )
    )

    assert parser_3ef0(payload, msg) == expected


@pytest.mark.parametrize(
    ("payload", "expected"),
    (
        (
            "0000FF",
            {"modulation_level": 0.0, "_flags_2": "FF"},
        ),
        (
            "00C8100000FF",
            {
                "modulation_level": 1.0,
                "_flags_2": "10",
                "_flags_3": [0, 0, 0, 0, 0, 0, 0, 0],
                "ch_active": False,
                "dhw_active": False,
                "cool_active": False,
                "flame_on": False,
                "_unknown_4": "00",
                "_unknown_5": "FF",
            },
        ),
    ),
)
def test_parser_3ef0_non_ufc_standard_routing_examples(
    payload: str, expected: dict[str, object]
) -> None:
    """Three- and six-byte non-UFC packets retain standard 3EF0 parsing."""
    msg = Message._from_pkt(
        Packet(
            dt.now(),
            f"...  I --- 13:000001 --:------ 13:000001 3EF0 {len(payload) // 2:03d} {payload}",
        )
    )

    result = parser_3ef0(payload, msg)

    assert result == expected
    assert "pump_relay_state" not in result
    assert "relay_byte_raw" not in result


def test_standard_honeywell_30c9_array_regression() -> None:
    """Standard Honeywell 30C9 arrays retain their established decoded values."""
    payload = "00086001078102073503070F04070E0507B206073F0707A3"
    msg = Message._from_pkt(
        Packet(
            dt.now(),
            f"...  I --- 01:050858 --:------ 01:050858 30C9 024 {payload}",
        )
    )

    result = parser_30c9(payload, msg)

    assert result == [
        {"zone_idx": "00", "temperature": 21.44},
        {"zone_idx": "01", "temperature": 19.21},
        {"zone_idx": "02", "temperature": 18.45},
        {"zone_idx": "03", "temperature": 18.07},
        {"zone_idx": "04", "temperature": 18.06},
        {"zone_idx": "05", "temperature": 19.7},
        {"zone_idx": "06", "temperature": 18.55},
        {"zone_idx": "07", "temperature": 19.55},
    ]
    assert all(
        set(item) == {"zone_idx", "temperature"}
        and isinstance(item["zone_idx"], str)
        and isinstance(item["temperature"], float)
        for item in result
    )


def test_standard_honeywell_3150_array_regression() -> None:
    """Standard Honeywell 3150 arrays retain their established decoded values."""
    payload = "000001AE02000300040A"
    msg = Message._from_pkt(
        Packet(
            dt.now(),
            f"...  I --- 02:044446 --:------ 02:044446 3150 010 {payload}",
        )
    )

    result = parser_3150(payload, msg)

    assert result == [
        {"ufx_idx": "00", "zone_demand": 0.0},
        {"ufx_idx": "01", "zone_demand": 0.87},
        {"ufx_idx": "02", "zone_demand": 0.0},
        {"ufx_idx": "03", "zone_demand": 0.0},
        {"ufx_idx": "04", "zone_demand": 0.05},
    ]
    assert all(
        set(item) == {"ufx_idx", "zone_demand"}
        and isinstance(item["ufx_idx"], str)
        and isinstance(item["zone_demand"], float)
        for item in result
    )


def test_previously_valid_packet_still_decodes_without_exception() -> None:
    """A captured Honeywell 2309 packet remains valid through message decoding."""
    msg = Message._from_pkt(
        Packet(
            dt.now(),
            "...  I --- 04:189076 --:------ 01:145038 2309 003 0205DC",
        )
    )

    assert msg.payload == {"zone_idx": "02", "setpoint": 15.0}


def _test_parser_31da(f_name: Path) -> None:
    # assert _31DA_FAN_INFO[int(payload[36:38], 16) & 0x1F] in (
    #     speed_capabilities(payload[30:34])["speed_capabilities"]
    # ) or (
    #     int(payload[36:38], 16) & 0x1F in (1, 2, 3) and int(
    #         payload[30:34], 16
    #     ) & 2**14
    # ) or (
    #     int(payload[36:38], 16) & 0x1F in (11, 12, 13) and int(
    #         payload[30:34], 16
    #     ) & 2**14 and int(payload[30:34], 16) & 2**13
    # ) or (
    #     int(payload[36:38], 16) & 0x1F in (0x00, 0x18, 0x15)
    # ), {
    #     _31DA_FAN_INFO[
    #         int(payload[36:38], 16) & 0x1F
    #     ]: speed_capabilities(payload[30:34])
    # }

    # assert payload[36:38] not in ("0B", "0C", "0D") or payload[
    #     42:46
    # ] == "0000", (
    #     payload[36:38], payload[42:46]
    # )

    pass


def _test_parser_pairs_31d9_31da(f_name: Path) -> None:
    pass


def _test_parser_pairs_4e15_3ef0(f_name: Path) -> None:
    if "4e15" in str(f_name):
        with open(f_name) as f:
            msg = None
            while this_line := (f.readline()):
                msg = _proc_log_line_pair_4e15(this_line, msg)

    # elif "01ff" in str(f_name):
    #     with open(f_name) as f:
    #         msg = None
    #         while this_line := (f.readline()):
    #             msg = _proc_log_line_pair_01ff(this_line, msg)
