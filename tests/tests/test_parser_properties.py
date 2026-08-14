#!/usr/bin/env python3
"""Property-based tests for RAMSES RF payload parsers."""

from datetime import datetime as dt
from typing import cast

import pytest
from hypothesis import assume, given, settings, strategies as st

from ramses_rf.messages import Message
from ramses_rf.parsers.heating import (
    parser_2d49,
    parser_3ef0,
    parser_30c9,
    parser_3150,
)
from ramses_tx import Packet
from ramses_tx.exceptions import PacketPayloadInvalid


# Feature: ramses-rf-hcc100-cooling, Property 1: 2D49 Valid Payload Parsing
# Validates: Requirements 1.1, 1.2, 1.3, 1.4
@given(
    zone_idx=st.integers(min_value=0x00, max_value=0xFF),
    demand=st.sampled_from((0x00, 0xC8)),
    padding=st.integers(min_value=0x00, max_value=0xFF),
)
@settings(max_examples=200)
def test_2d49_valid_payload_parsing(
    zone_idx: int, demand: int, padding: int
) -> None:
    """Valid 2D49 payloads retain their zone index and decode cooling demand."""

    payload = f"{zone_idx:02X}{demand:02X}{padding:02X}"

    result = parser_2d49(payload, cast(Message, None))

    assert set(result) == {"zone_idx", "cooling_demand"}
    assert result["zone_idx"] == f"{zone_idx:02X}"
    assert len(result["zone_idx"]) == 2
    assert isinstance(result["cooling_demand"], bool)
    assert result["cooling_demand"] is (demand == 0xC8)


# Feature: ramses-rf-hcc100-cooling, Property 2: 2D49 Invalid Length Rejection
# Validates: Requirements 1.5
@given(
    encoded_payload=st.one_of(
        st.binary(min_size=0, max_size=2),
        st.binary(min_size=4, max_size=20),
    )
)
@settings(max_examples=200)
def test_2d49_invalid_length_rejection(encoded_payload: bytes) -> None:
    """2D49 payloads other than three bytes are rejected without a result."""

    payload = encoded_payload.hex().upper()

    with pytest.raises(PacketPayloadInvalid, match="Invalid 2D49 payload length"):
        parser_2d49(payload, cast(Message, None))


# Feature: ramses-rf-hcc100-cooling, Property 4: UFC 3EF0 Pump Relay Bit-Mapping
# Validates: Requirements 4.1, 4.2, 4.3, 4.4, 4.5, 4.6
@given(relay_byte=st.integers(min_value=0x00, max_value=0xFF))
@settings(max_examples=200)
def test_3ef0_ufc_pump_relay_bit_mapping(relay_byte: int) -> None:
    """Nine-byte UFC 3EF0 packets map every relay-byte bit pattern correctly."""

    payload = f"000000{relay_byte:02X}0000000000"
    msg = Message._from_pkt(
        Packet(
            dt.now(),
            f"...  I --- 02:000001 --:------ 02:000001 3EF0 009 {payload}",
        )
    )

    result = parser_3ef0(payload, msg)

    expected_state = (
        "cooling"
        if relay_byte & 0x10
        else "heating"
        if relay_byte & 0x02
        else "off"
    )
    include_raw = bool(relay_byte & ~0x12) or (relay_byte & 0x12) == 0x12

    assert result["pump_relay_state"] == expected_state
    if include_raw:
        assert result["relay_byte_raw"] == relay_byte
    else:
        assert "relay_byte_raw" not in result


# Feature: ramses-rf-hcc100-cooling, Property 5: Non-UFC 3EF0 Routing Guard
# Validates: Requirements 4.7, 5.3
@given(
    payload_length=st.sampled_from((3, 6, 9)),
    source_type=st.sampled_from(("01", "02", "13")),
    modulation=st.sampled_from((0x00, 0xC8)),
    flags_2=st.sampled_from((0x00, 0x10, 0x11)),
    setpoint=st.integers(min_value=10, max_value=90),
    max_modulation=st.sampled_from((0x00, 0x64)),
)
@settings(max_examples=200)
def test_3ef0_non_ufc_routing_guard(
    payload_length: int,
    source_type: str,
    modulation: int,
    flags_2: int,
    setpoint: int,
    max_modulation: int,
) -> None:
    """Non-UFC packets and short UFC packets retain standard 3EF0 parsing."""

    assume(payload_length != 9 or source_type != "02")

    payload = (
        f"00{modulation:02X}FF"
        if payload_length == 3
        else f"00{modulation:02X}{flags_2:02X}0000FF"
        if payload_length == 6
        else f"00{modulation:02X}{flags_2:02X}0000FF02{setpoint:02X}{max_modulation:02X}"
    )
    msg = Message._from_pkt(
        Packet(
            dt.now(),
            f"...  I --- {source_type}:000001 --:------ {source_type}:000001 "
            f"3EF0 {payload_length:03d} {payload}",
        )
    )

    result = parser_3ef0(payload, msg)

    assert {"modulation_level", "_flags_2"} <= result.keys()
    assert "pump_relay_state" not in result
    assert "relay_byte_raw" not in result
    if payload_length >= 6:
        assert {"_flags_3", "ch_active", "dhw_active", "cool_active", "flame_on"} <= result.keys()
    if payload_length == 9:
        assert {"_flags_6", "ch_enabled", "ch_setpoint", "max_rel_modulation"} <= result.keys()


# Feature: ramses-rf-hcc100-cooling, Property 6: Mixed Zone Index Array Parsing
# Validates: Requirements 5.4
@given(
    standard_elements=st.lists(
        st.tuples(
            st.integers(min_value=0x00, max_value=0x0F),
            st.integers(min_value=-2000, max_value=5000),
            st.integers(min_value=0x00, max_value=0xC8),
        ),
        min_size=1,
        max_size=4,
    ),
    extended_elements=st.lists(
        st.tuples(
            st.integers(min_value=0x10, max_value=0xEF),
            st.integers(min_value=-2000, max_value=5000),
            st.integers(min_value=0x00, max_value=0xC8),
        ),
        min_size=1,
        max_size=4,
    ),
)
@settings(max_examples=200)
def test_mixed_zone_index_array_parsing(
    standard_elements: list[tuple[int, int, int]],
    extended_elements: list[tuple[int, int, int]],
) -> None:
    """Mixed standard and extended zone indices retain their paired values.

    ``F0``–``FF`` are intentionally excluded because established 3150 protocol
    semantics use that range for aggregate domain-demand sentinels, not zone indices.
    """

    elements = [*standard_elements, *extended_elements]
    temperature_payload = "".join(
        f"{zone_idx:02X}{temperature & 0xFFFF:04X}"
        for zone_idx, temperature, _ in elements
    )
    demand_payload = "".join(
        f"{zone_idx:02X}{thermal_demand:02X}"
        for zone_idx, _, thermal_demand in elements
    )

    temperature_msg = Message._from_pkt(
        Packet(
            dt.now(),
            "...  I --- 01:000001 --:------ 01:000001 "
            f"30C9 {len(temperature_payload) // 2:03d} {temperature_payload}",
        )
    )
    demand_msg = Message._from_pkt(
        Packet(
            dt.now(),
            "...  I --- 01:000001 --:------ 01:000001 "
            f"3150 {len(demand_payload) // 2:03d} {demand_payload}",
        )
    )

    temperatures = parser_30c9(temperature_payload, temperature_msg)
    demands = parser_3150(demand_payload, demand_msg)

    assert isinstance(temperatures, list)
    assert isinstance(demands, list)
    assert len(temperatures) == len(demands) == len(elements)
    for parsed, (zone_idx, temperature, _) in zip(temperatures, elements, strict=True):
        assert parsed == {
            "zone_idx": f"{zone_idx:02X}",
            "temperature": temperature / 100,
        }
    for parsed, (zone_idx, _, thermal_demand) in zip(demands, elements, strict=True):
        assert parsed == {
            "zone_idx": f"{zone_idx:02X}",
            "zone_demand": thermal_demand / 200,
        }
