"""Focused HCC100 cooling packet tests."""

from datetime import datetime as dt

import pytest
from hypothesis import given, settings, strategies as st

from ramses_rf.parsers.decoder import decode_packet
from ramses_rf.payloads.heating import ActuatorStatePayload, HeatDemandPayload, TemperaturePayload
from ramses_rf.payloads.hvac import CoolingStatePayload
from ramses_rf.protocol.ramses import CODES_WITH_ARRAYS
from ramses_tx.const import Code
from ramses_tx.dtos import PacketDTO


def _decode(code: str, payload: str, source_type: str = "02") -> dict[str, object]:
    dto = PacketDTO(
        timestamp=dt.now(), rssi="-64", verb=" I", seq="---",
        addr1=f"{source_type}:000001", addr2="--:------",
        addr3=f"{source_type}:000001", code=code,
        length=f"{len(payload) // 2:03d}", payload=payload,
    )
    result = decode_packet(dto)
    assert isinstance(result, dict)
    return result


@pytest.mark.parametrize(
    ("payload", "expected"),
    (("1EC800", True), ("200000", False), ("23C800", True), ("880000", False), ("FDC800", True)),
)
def test_2d49_hcc100_cooling_demand(payload: str, expected: bool) -> None:
    result = _decode("2D49", payload, "01")
    assert result["zone_idx"] == payload[:2]
    assert result["cooling_demand"] is expected


def test_2d49_unknown_demand_is_conservative(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING", logger="ramses_rf.payloads.hvac"):
        assert _decode("2D49", "1E6400", "01")["cooling_demand"] is False
    assert "Unknown 2D49 cooling demand byte: 64" in caplog.text


# Feature: ramses-rf-hcc100-cooling, Property 1: 2D49 Valid Payload Parsing
# Validates: Requirements 1.1, 1.2, 1.3, 1.4
@given(zone=st.integers(0, 255), demand=st.sampled_from((0, 0xC8)), reserved=st.integers(0, 255))
@settings(max_examples=200)
def test_2d49_valid_payload_parsing(zone: int, demand: int, reserved: int) -> None:
    payload = CoolingStatePayload.from_bytes(bytes((zone, demand, reserved)))
    assert payload.to_dict() == {"zone_idx": f"{zone:02X}", "cooling_demand": demand == 0xC8}


# Feature: ramses-rf-hcc100-cooling, Property 2: 2D49 Invalid Length Rejection
# Validates: Requirements 1.5
@given(raw=st.one_of(st.binary(max_size=2), st.binary(min_size=4, max_size=20)))
@settings(max_examples=200)
def test_2d49_invalid_length_rejection(raw: bytes) -> None:
    with pytest.raises(ValueError, match="Invalid payload length for 2D49"):
        CoolingStatePayload.from_bytes(raw)


# Feature: ramses-rf-hcc100-cooling, Property 3: Full Zone Index Range
# Validates: Requirements 2.1, 2.4, 3.1, 3.3
@given(index=st.integers(0, 255))
@settings(max_examples=200)
def test_full_byte_zone_indexes_and_ufc_array_metadata(index: int) -> None:
    assert "02" in CODES_WITH_ARRAYS[Code._30C9][1]
    assert "02" in CODES_WITH_ARRAYS[Code._3150][1]
    temperature = TemperaturePayload.from_bytes(bytes((index, 0x08, 0x34)))
    demand = HeatDemandPayload.from_bytes(bytes((index, 0xC8)))
    assert temperature.to_dict()["zone_idx"] == f"{index:02X}"
    assert demand.domain_or_zone_idx == index
    assert HeatDemandPayload.from_bytes(b"\xFC\xC8").to_dict()["domain_id"] == "FC"


@pytest.mark.parametrize(
    ("relay_byte", "state", "raw"),
    ((0x10, "cooling", None), (0x02, "heating", None), (0x00, "off", None), (0x12, "cooling", 0x12), (0x15, "cooling", 0x15)),
)
def test_3ef0_ufc_relay_examples(relay_byte: int, state: str, raw: int | None) -> None:
    result = _decode("3EF0", f"000000{relay_byte:02X}0000000000")
    assert result["pump_relay_state"] == state
    assert result.get("relay_byte_raw") == raw


# Feature: ramses-rf-hcc100-cooling, Property 4: UFC 3EF0 Pump Relay Bit-Mapping
# Validates: Requirements 4.1, 4.2, 4.3, 4.4, 4.5, 4.6
@given(relay_byte=st.integers(0, 255))
@settings(max_examples=200)
def test_3ef0_ufc_pump_relay_bit_mapping(relay_byte: int) -> None:
    result = _decode("3EF0", f"000000{relay_byte:02X}0000000000")
    assert result["pump_relay_state"] == ("cooling" if relay_byte & 0x10 else "heating" if relay_byte & 0x02 else "off")
    assert ("relay_byte_raw" in result) is bool(relay_byte & ~0x12 or relay_byte & 0x12 == 0x12)


# Feature: ramses-rf-hcc100-cooling, Property 5: Non-UFC 3EF0 Routing Guard
# Validates: Requirements 4.7, 5.3
@given(payload_length=st.sampled_from((3, 6, 9)), source_type=st.sampled_from(("01", "02", "13")))
@settings(max_examples=200)
def test_3ef0_non_ufc_routing_guard(payload_length: int, source_type: str) -> None:
    if payload_length == 9 and source_type == "02":
        return
    payload = (bytes((0, 100, 0, 0, 0, 0, 0, 20, 200)) if payload_length == 9 else bytes((0, 100, 0, 0, 0, 0)) if payload_length == 6 else bytes((0, 100, 0))).hex()
    result = _decode("3EF0", payload.upper(), source_type)
    assert "pump_relay_state" not in result and "relay_byte_raw" not in result


# Feature: ramses-rf-hcc100-cooling, Property 6: Mixed Zone Index Array Parsing
# Validates: Requirements 5.4
@given(standard=st.lists(st.integers(0, 15), min_size=1, max_size=4), extended=st.lists(st.integers(16, 239), min_size=1, max_size=4))
@settings(max_examples=200)
def test_mixed_zone_index_array_parsing(standard: list[int], extended: list[int]) -> None:
    indexes = [*standard, *extended]
    temperatures = TemperaturePayload.from_bytes(b"".join(bytes((index, 0x08, 0x34)) for index in indexes))
    demands = HeatDemandPayload.from_bytes(b"".join(bytes((index, 0xC8)) for index in indexes))
    assert [item.to_dict()["zone_idx"] for item in temperatures] == [f"{index:02X}" for index in indexes]
    assert [item.to_dict()["zone_idx"] for item in demands] == [f"{index:02X}" for index in indexes]


def test_3ef0_standard_payload_is_unchanged() -> None:
    payload = ActuatorStatePayload.from_bytes(bytes.fromhex("0064FF1000FF0114C8"))
    assert payload.to_dict() == {"modulation_level": 0.5, "ch_active": False, "dhw_active": False, "flame_on": False, "cool_active": True, "ch_enabled": True, "ch_setpoint": 20, "max_rel_modulation": 1.0}
