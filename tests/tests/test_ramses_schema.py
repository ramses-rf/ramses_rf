#!/usr/bin/env python3
"""RAMSES RF - Test the RAMSES II schema."""

import logging
import re
from datetime import datetime as dt

import pytest
from hypothesis import given, settings, strategies as st

from ramses_rf import RQ
from ramses_rf.devices import HEAT_DEV_CLASS_BY_SLUG, HVAC_DEV_CLASS_BY_SLUG
from ramses_rf.parsers.decoder import DtoPayloadDecoderPipeline
from ramses_rf.protocol.ramses import (
    _DEV_KLASSES_HEAT,
    _DEV_KLASSES_HVAC,
    _HVAC_VC_PAIR_BY_CLASS,
    CODE_IDX_ARE_COMPLEX,
    CODE_IDX_ARE_NONE,
    CODE_IDX_ARE_SIMPLE,
    CODES_SCHEMA,
    CODES_WITH_ARRAYS,
    HVAC_KLASS_BY_VC_PAIR,
    RQ_NO_PAYLOAD,
)
from ramses_tx import exceptions as exc
from ramses_tx.const import Code, DevType
from ramses_tx.dtos import PacketDTO


def test_code_counts() -> None:
    """All known command codes should be in the schema & vice versa."""

    # assert len(Code) == len(CODES_SCHEMA)
    assert not [c for c in CODES_SCHEMA if c not in Code]
    assert not [c for c in Code if c not in CODES_SCHEMA]


def test_verb_code_pairs() -> None:
    """Verb/code pairs are used to detect HVAC device classes: they should be unique."""

    assert len(HVAC_KLASS_BY_VC_PAIR) == (
        sum(len(v) for v in _HVAC_VC_PAIR_BY_CLASS.values())
    ), "Coding error: There is a duplicate verb/code pair"


def test_device_heat_slugs() -> None:
    """Every Heat device slug should have an entry in it domain's _DEV_KLASSES_*."""

    assert not [s for s in _DEV_KLASSES_HEAT if s not in HEAT_DEV_CLASS_BY_SLUG]
    assert not [
        s
        for s in HEAT_DEV_CLASS_BY_SLUG
        if s not in _DEV_KLASSES_HEAT and s != DevType.HEA
    ]


def test_device_hvac_slugs() -> None:
    """Every HVAC device slug should have an entry in it domain's _DEV_KLASSES_*."""

    assert not [s for s in _DEV_KLASSES_HVAC if s not in HVAC_DEV_CLASS_BY_SLUG]
    assert not [
        s
        for s in HVAC_DEV_CLASS_BY_SLUG
        if s not in _DEV_KLASSES_HVAC and s != DevType.HVC
    ]


def assert_codes_idx_mutex(mutex_list: set[Code], other_list: set[Code]) -> None:
    """Assert the two lists are mutually exclusive."""

    codes = sorted(c for c in mutex_list if c in other_list)
    assert not codes


def test_codes_idx_mutex() -> None:
    """Every code should be in one of the three CODE_IDX_* constants."""

    codes_idx_all = CODE_IDX_ARE_COMPLEX | CODE_IDX_ARE_NONE | CODE_IDX_ARE_SIMPLE
    assert not [c for c in CODES_SCHEMA if c not in codes_idx_all]


def test_codes_idx_complex_mutex() -> None:
    """The three CODE_IDX_* constants should be mutually exclusive."""

    assert_codes_idx_mutex(
        CODE_IDX_ARE_COMPLEX, CODE_IDX_ARE_NONE | CODE_IDX_ARE_SIMPLE
    )


def test_codes_idx_none_mutex() -> None:
    """The three CODE_IDX_* constants should be mutually exclusive."""

    assert_codes_idx_mutex(
        CODE_IDX_ARE_NONE, CODE_IDX_ARE_SIMPLE | CODE_IDX_ARE_COMPLEX
    )


def test_codes_idx_simple_mutex() -> None:
    """The three CODE_IDX_* constants should be mutually exclusive."""

    assert_codes_idx_mutex(
        CODE_IDX_ARE_SIMPLE, CODE_IDX_ARE_NONE | CODE_IDX_ARE_COMPLEX
    )


def test_codes_mutex() -> None:
    # RQ_IDX_ONLY is a list, convert to set for the helper
    assert_codes_idx_mutex(set(RQ_IDX_ONLY), CODE_IDX_ARE_NONE)


# Cast .get() result to str() to ensure it is indexable/sliceable
RQ_IDX_NONE = [k for k, v in CODES_SCHEMA.items() if str(v.get(RQ, ""))[:3] == "^00"]

RQ_IDX_ONLY = [
    k
    for k, v in CODES_SCHEMA.items()
    if k not in RQ_NO_PAYLOAD and (v.get(RQ) in (r"^0[0-9A-F]00$", r"^0[0-9A-F](00)?$"))
]

RQ_IDX_UNKNOWN = [
    k
    for k, v in CODES_SCHEMA.items()
    if k not in RQ_NO_PAYLOAD + RQ_IDX_ONLY and RQ in v
]


# Feature: ramses-rf-vaillant-cooling, Property 3: 30C9/3150 Regex Full Zone Index Range
# Validates: Requirements 2.1, 2.4, 3.1, 3.3
@given(
    temperature_elements=st.lists(
        st.tuples(
            st.integers(min_value=0x00, max_value=0xFF),
            st.integers(min_value=0x00, max_value=0xFF),
            st.integers(min_value=0x00, max_value=0xFF),
        ),
        min_size=1,
        max_size=8,
    ),
    demand_elements=st.lists(
        st.tuples(
            st.integers(min_value=0x00, max_value=0xFF),
            st.integers(min_value=0x00, max_value=0xFF),
        ),
        min_size=1,
        max_size=8,
    ),
)
@settings(max_examples=200)
def test_30c9_3150_regex_full_zone_index_range(
    temperature_elements: list[tuple[int, int, int]],
    demand_elements: list[tuple[int, int]],
) -> None:
    """Both array schemas accept one to eight elements indexed from 00 to FF."""

    temperature_payload = "".join(
        f"{zone_idx:02X}{high_byte:02X}{low_byte:02X}"
        for zone_idx, high_byte, low_byte in temperature_elements
    )
    demand_payload = "".join(
        f"{zone_idx:02X}{demand:02X}" for zone_idx, demand in demand_elements
    )

    assert re.fullmatch(str(CODES_SCHEMA[Code._30C9][" I"]), temperature_payload)
    assert re.fullmatch(str(CODES_SCHEMA[Code._3150][" I"]), demand_payload)


# Validates: Requirements 2.2, 3.2
def test_vaillant_extended_zone_index_examples() -> None:
    """The observed Vaillant zone indices match their respective array schemas."""

    temperature_schema = str(CODES_SCHEMA[Code._30C9][" I"])
    demand_schema = str(CODES_SCHEMA[Code._3150][" I"])

    for zone_idx in ("E1", "E2", "E3", "E4", "E5", "E6"):
        assert re.fullmatch(temperature_schema, f"{zone_idx}1234")

    for zone_idx in ("21", "22", "23"):
        assert re.fullmatch(demand_schema, f"{zone_idx}C8")


# Validates: Requirements 2.4, 3.3
def test_standard_zone_array_examples_remain_valid() -> None:
    """Standard single- and multi-element arrays continue to match exactly."""

    assert re.fullmatch(str(CODES_SCHEMA[Code._30C9][" I"]), "000834")
    assert re.fullmatch(str(CODES_SCHEMA[Code._30C9][" I"]), "0008340F1234")
    assert re.fullmatch(str(CODES_SCHEMA[Code._3150][" I"]), "00C8")
    assert re.fullmatch(str(CODES_SCHEMA[Code._3150][" I"]), "00C80F64")


# Validates: Requirements 2.3, 3.4
def test_ufc_is_registered_as_an_array_source() -> None:
    """UFC devices can dispatch both Vaillant zone packet formats as arrays."""

    assert "02" in CODES_WITH_ARRAYS[Code._30C9][1]
    assert "02" in CODES_WITH_ARRAYS[Code._3150][1]


# Validates: Requirements 2.5, 5.1, 5.2
def test_invalid_30c9_payload_is_rejected_after_ufc_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A malformed UFC temperature packet keeps the established warning/rejection path."""

    payload = "E1ABCDEF"
    dto = PacketDTO(
        timestamp=dt.now(),
        rssi="-64",
        verb=" I",
        seq="---",
        addr1="02:123456",
        addr2="--:------",
        addr3="02:123456",
        code="30C9",
        length="004",
        payload=payload,
    )

    with caplog.at_level(logging.WARNING, logger="ramses_rf.parsers.decoder"):
        with pytest.raises(exc.PacketPayloadInvalid, match="Payload doesn't match"):
            DtoPayloadDecoderPipeline().decode(dto)

    assert f"RAMSES 30C9  I from 02:123456 to 02:123456: payload={payload}" in caplog.text
