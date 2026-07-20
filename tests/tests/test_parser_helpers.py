#!/usr/bin/env python3
"""RAMSES RF - Test the various helper APIs."""

# TODO: add test for ramses_tx.frame.pkt_header()

from datetime import datetime as dt

from ramses_rf.messages import Message
from ramses_rf.systems.zones import _transform
from ramses_tx.exceptions import CommandInvalid, PacketInvalid
from ramses_tx.helpers import (
    hex_from_bool,
    hex_from_double,
    hex_from_dtm,
    hex_from_dts,
    hex_from_flag8,
    hex_from_percent,
    # hex_from_str,
    hex_from_temp,
    hex_to_bool,
    # hex_to_date,
    hex_to_double,
    hex_to_dtm,
    hex_to_dts,
    hex_to_flag8,
    hex_to_percent,
    # hex_to_str,
    hex_to_temp,
)
from ramses_tx.packet import Packet

from .helpers import TEST_DIR

WORK_DIR = f"{TEST_DIR}/parser_helpers"


# --- 22F1 parser scheme detection tests (issue #87) ---


def _make_22f1_msg(pkt_str: str) -> Message:
    """Build a Message from a raw packet string for parser testing."""
    pkt = Packet.from_file(pkt_str[:26], pkt_str[27:])
    return Message(pkt.to_dto())


def test_22f1_itho_directed_mode_max_04() -> None:
    """Directed 22F1 with mode_max=04 must be itho, not orcon (issue #87).

    Previously, only broadcast (NON_DEV_ADDR) packets with mode_max=04 were
    identified as itho.  Directed packets from Itho remotes (e.g. CVE-S CO2
    with AUTO RFT-N) were misidentified as orcon, causing:
      02 → "medium" (orcon) instead of "low"    (itho)
      04 → "auto"   (orcon) instead of "high"   (itho)
      03 → "high"   (orcon) instead of "medium" (itho)
    """
    # Low:    37:198669 → 37:019136, 22F1 003 000204
    msg = _make_22f1_msg(
        "2023-11-12T19:48:24.587738 044  I --- 37:198669 37:019136 --:------ 22F1 003 000204"
    )
    result = msg.payload
    assert result["_scheme"] == "itho"
    assert result["fan_mode"] == "low"
    assert result["_mode_idx"] == "02"
    assert result["_mode_max"] == "04"

    # High:   37:198669 → 37:019136, 22F1 003 000404
    msg = _make_22f1_msg(
        "2023-11-12T19:48:28.266746 045  I --- 37:198669 37:019136 --:------ 22F1 003 000404"
    )
    result = msg.payload
    assert result["_scheme"] == "itho"
    assert result["fan_mode"] == "high"

    # Medium: 37:198669 → 37:019136, 22F1 003 000304
    msg = _make_22f1_msg(
        "2023-11-12T19:48:31.786152 043  I --- 37:198669 37:019136 --:------ 22F1 003 000304"
    )
    result = msg.payload
    assert result["_scheme"] == "itho"
    assert result["fan_mode"] == "medium"


def test_22f1_itho_broadcast_still_itho() -> None:
    """Broadcast 22F1 with mode_max=04 must still be itho (no regression)."""
    msg = _make_22f1_msg(
        "2021-02-12T17:27:06.905245 075  I 015 --:------ --:------ 39:159057 22F1 003 000204"
    )
    result = msg.payload
    assert result["_scheme"] == "itho"
    assert result["fan_mode"] == "low"


def test_22f1_orcon_directed_mode_max_07_still_orcon() -> None:
    """Directed 22F1 with mode_max=07 must still be orcon (no regression)."""
    msg = _make_22f1_msg(
        "2022-06-22T21:36:43.354510 ...  I --- 37:155617 32:155617 --:------ 22F1 003 000207"
    )
    result = msg.payload
    assert result["_scheme"] == "orcon"
    assert result["fan_mode"] == "medium"


def test_22f1_vasco_directed_mode_max_06_still_vasco() -> None:
    """Directed 22F1 with mode_max=06 must still be vasco (no regression)."""
    msg = _make_22f1_msg(
        "2024-10-10T22:26:28.420378 041  I --- 29:091138 32:022222 --:------ 22F1 003 000206"
    )
    result = msg.payload
    assert result["_scheme"] == "vasco"
    assert result["fan_mode"] == "low"


# --- 2411 parser data_type tests (issue #740) ---


def test_2411_data_type_13_ventura_filter_time() -> None:
    """2411 param 31 with data_type 13 (ClimaRad Ventura) must parse without warning.

    The Ventura sends data_type 13 instead of 10 for the filter time parameter.
    See issue #740.
    """
    msg = _make_22f1_msg(
        "2026-06-26T22:08:35.885000 040 RP --- 32:022222 29:091138 --:------ 2411 022 "
        "0000313E130000006400000000000075300000000164"
    )
    result = msg.payload
    assert result["parameter"] == "31"
    assert result["description"] == "Time to change filter (days)"
    assert result["value"] == 100  # 0x00000064 = 100
    assert result["min_value"] == 0
    assert result["max_value"] == 30000  # 0x00007530
    assert result["precision"] == 1
    assert "_unknown_data_type" not in result


def test_2411_data_type_11_ventura_param_da() -> None:
    """2411 param DA with data_type 11 (Orcon/ClimaRad Ventura) must parse without warning.

    The Orcon and ClimaRad Ventura send data_type 11 for param DA.
    This is a 4-byte counter variant (same shape as data_type 10 and 13).
    """
    msg = _make_22f1_msg(
        "2026-07-15T16:14:33.612000 040 RP --- 32:153289 37:168270 --:------ 2411 023 "
        "0000DAC111000003E8000000000000138800000001CB52"
    )
    result = msg.payload
    assert result["parameter"] == "DA"
    assert result["description"] == "Unknown (ClimaRad Ventura)"
    assert result["value"] == 1000  # 0x000003E8 = 1000
    assert result["min_value"] == 0
    assert result["max_value"] == 5000  # 0x00001388
    assert result["precision"] == 1
    assert "_unknown_data_type" not in result


def test_2411_unknown_param_ids_ventura() -> None:
    """2411 params 07, 4C, 88, DA (ClimaRad Ventura) must parse without warning.

    These params are now in _2411_PARAMS_SCHEMA with placeholder descriptions.
    The parser should use the data_type from the packet to decode the value.
    See issue 740 (reopened).
    """
    # param 07, data_type 00 (2-byte counter)
    msg = _make_22f1_msg(
        "2026-07-11T17:17:52.000000 040 RP --- 32:022222 29:091138 --:------ 2411 022 "
        "00000700000000010000000000000001000000018A00"
    )
    result = msg.payload
    assert result["parameter"] == "07"
    assert result["description"] == "Base ventilation enable"
    assert "_unknown_data_type" not in result

    # param 4C, data_type 10 (4-byte counter)
    msg = _make_22f1_msg(
        "2026-07-11T17:17:52.000000 040 RP --- 32:022222 29:091138 --:------ 2411 022 "
        "00004C10000000640000000000007530000000016400"
    )
    result = msg.payload
    assert result["parameter"] == "4C"
    assert result["description"] == "Unknown (ClimaRad Ventura)"
    assert "_unknown_data_type" not in result

    # param 88, data_type 10 (4-byte counter)
    msg = _make_22f1_msg(
        "2026-07-11T17:17:52.000000 040 RP --- 32:022222 29:091138 --:------ 2411 023 "
        "000088100000026400000001900000076C000000018A33"
    )
    result = msg.payload
    assert result["parameter"] == "88"
    assert result["description"] == "Timer configuration (ClimaRad Ventura)"
    assert "_unknown_data_type" not in result

    # param DA, data_type 10 (4-byte counter)
    msg = _make_22f1_msg(
        "2026-07-11T17:17:52.000000 040 RP --- 32:022222 29:091138 --:------ 2411 023 "
        "0000DA1000000000000000000000000001000000018A00"
    )
    result = msg.payload
    assert result["parameter"] == "DA"
    assert result["description"] == "Unknown (ClimaRad Ventura)"
    assert "_unknown_data_type" not in result


def test_2411_data_type_80_minibox_param_07() -> None:
    """2411 param 07 with data_type 80 (ClimaRad MiniBox) must parse without warning.

    The MiniBox FAN does not support this parameter and signals this by
    returning sentinel values (0x000000FF for value, 0xFFFFFFFF for min).
    data_type 80 is a 4-byte boolean: 0=False, 1=True, anything else=None.
    See issue 830.
    """
    msg = _make_22f1_msg(
        "2026-07-16T07:51:23.626000 078 RP --- 29:099029 29:123150 --:------ 2411 022 "
        "0000071F80000000FFFFFFFFFF000000010000000120"
    )
    result = msg.payload
    assert result["parameter"] == "07"
    assert result["description"] == "Base ventilation enable"
    assert result["value"] is None  # 0x000000FF = not available
    assert result["min_value"] is None  # 0xFFFFFFFF = not available
    assert result["max_value"] == 1
    assert result["precision"] == 1
    assert "_unknown_data_type" not in result


def test_2411_data_type_80_bool_values() -> None:
    """2411 data_type 80 must decode 0=False, 1=True for supported devices."""
    # value = 0x00000000 (False)
    msg = _make_22f1_msg(
        "2026-07-16T07:51:23.626000 078 RP --- 29:099029 29:123150 --:------ 2411 022 "
        "0000071F8000000000FFFFFFFF000000010000000120"
    )
    result = msg.payload
    assert result["value"] == 0
    assert "_unknown_data_type" not in result

    # value = 0x00000001 (True)
    msg = _make_22f1_msg(
        "2026-07-16T07:51:23.626000 078 RP --- 29:099029 29:123150 --:------ 2411 022 "
        "0000071F8000000001FFFFFFFF000000010000000120"
    )
    result = msg.payload
    assert result["value"] == 1
    assert "_unknown_data_type" not in result


def test_helper_demand_transform() -> None:
    assert [x[1] for x in TRANSFORMS] == [_transform(x[0]) for x in TRANSFORMS]


def test_helper_field_parsers() -> None:
    for val in ("FF", "00", "C8"):
        assert val == hex_from_bool(hex_to_bool(val))

    for val in ("7FFF", "0000", "0001", "0010", "0100", "1000"):
        assert val == hex_from_double(hex_to_double(val))
        assert val == hex_from_double(hex_to_double(val, factor=100), factor=100)

    for val in (
        "FF" * 6,
        "FF" * 7,
        "00141B0A07E3",
        "00110E0507E5",
        "0400041C0A07E3",
    ):
        assert val == hex_from_dtm(hex_to_dtm(val), incl_seconds=(len(val) == 14))

    for val in ("00000000007F",):
        assert val == hex_from_dts(hex_to_dts(val))

    for val in ("00", "01", "08", "10", "E0", "CC", "FF"):
        assert val == hex_from_flag8(hex_to_flag8(val))
        assert val == hex_from_flag8(hex_to_flag8(val, lsb=True), lsb=True)

    for val in ("7FFF", "7EFF", "0000", "0010", "0200", "D000"):
        assert val == hex_from_temp(hex_to_temp(val))

    for tmp in (None, False, -127.99, -100, -22.5, -1.53, 0, 1.53, 22.5, 100, 127.98):
        assert tmp == hex_to_temp(hex_from_temp(tmp))

    for cent in (None, 0, 0.05, 0.1, 0.5, 0.95, 1.0):
        assert cent == hex_to_percent(hex_from_percent(cent))


def _test_pkt_dev_class() -> None:
    """Check that the device class is correctly inferred from the packet.

    Some packets (not all) can be used to determine the domain (Heat vs HVAC) and
    class (e.g. BDR, CTL, FAN, etc)of the source device.
    """

    def proc_log_line(log_line: str) -> None:
        if "#" not in log_line:
            return
        pkt_line, pkt_eval = log_line.split("#", maxsplit=1)

        if not pkt_line[27:].strip():
            return

        pkt = Packet.from_file(pkt_line[:26], pkt_line[27:])

        assert pkt.src.type == eval(pkt_eval)  # TODO: finish this test

    with open(f"{WORK_DIR}/pkt_dev_class.log") as f:
        while line := (f.readline()):
            if line.strip():
                proc_log_line(line)


def test_pkt_addr_sets() -> None:
    """Check that the address set is correctly inferred from the packet."""

    def proc_log_line(log_line: str) -> None:
        if "#" not in log_line:
            return
        pkt_line, pkt_eval = log_line.split("#", maxsplit=1)

        if not pkt_line[27:].strip():
            return
        expected = eval(pkt_eval)

        try:
            pkt = Packet.from_port(dt.now(), f"... {pkt_line[31:].rstrip()}")
            pkt._validate(strict_checking=True)
        except (CommandInvalid, PacketInvalid) as err:
            assert err.__class__.__name__ in ("CommandInvalid", "PacketInvalid")
            assert err.message and err.message.startswith(expected.message)
            return

        res = {"src": pkt.src.id, "dst": pkt.dst.id, "set": [a.id for a in pkt._addrs]}
        assert res == expected

    with open(f"{WORK_DIR}/pkt_addr_set.log") as f:
        while line := (f.readline()):
            if (line := line.strip()) and line[:1] != "#":
                proc_log_line(line)


TRANSFORMS = [
    (0.000, 0),
    (0.220, 0),
    (0.230, 0),
    (0.260, 0),
    # (0.295, 0),  # needs confirming
    (0.300, 0),
    # (0.305, 0),  # needs confirming
    (0.310, 0.01),
    (0.340, 0.03),
    (0.350, 0.04),
    (0.370, 0.05),
    (0.380, 0.06),
    (0.390, 0.07),
    (0.400, 0.08),
    (0.410, 0.08),
    (0.420, 0.09),
    (0.430, 0.10),
    (0.450, 0.11),
    (0.470, 0.13),
    (0.480, 0.14),
    (0.530, 0.17),
    (0.540, 0.18),
    (0.550, 0.19),
    (0.560, 0.20),
    (0.575, 0.21),
    (0.610, 0.23),
    (0.620, 0.24),
    (0.650, 0.26),
    (0.660, 0.27),
    (0.680, 0.29),
    (0.690, 0.29),
    # (0.695, 0.30),  # needs confirming
    (0.700, 0.30),
    # (0.705, 0.31),  # needs confirming
    (0.720, 0.35),
    (0.740, 0.39),
    (0.760, 0.44),
    (0.770, 0.46),
    (0.790, 0.51),
    (0.800, 0.53),
    (0.820, 0.58),
    (0.830, 0.60),
    (0.840, 0.63),
    (0.930, 0.84),
    (0.950, 0.88),
    # (0.995, 0.99),  # needs confirming
    (1.000, 1.0),
]
