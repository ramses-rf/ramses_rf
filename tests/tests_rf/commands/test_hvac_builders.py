#!/usr/bin/env python3
"""Unittests for ramses_rf HVAC command builders (CQRS Intents)."""

from typing import Any

import pytest

from ramses_rf.address import Address
from ramses_rf.commands.builders import build_dto
from ramses_rf.commands.core import Command as Intent
from ramses_rf.enums import Action


def test_set_fan_mode_orcon_2byte_default() -> None:
    """Default scheme (orcon) produces a 3-byte payload with mode_max=07."""
    expected_payload = "000107"
    expected_code = "22F1"

    intent = Intent(
        src=Address("18:000730"),
        dst=Address("37:111111"),
        action=Action.SET_FAN_MODE,
        data={"fan_mode": "low", "scheme": "orcon"},
    )
    dto = build_dto(intent)

    assert dto.payload == expected_payload
    assert str(dto.code) == expected_code


def test_set_fan_mode_orcon_2byte_explicit() -> None:
    """Explicit orcon scheme with mode_max='' produces legacy 2-byte payload."""
    expected_payload = "0001"

    intent = Intent(
        src=Address("18:000730"),
        dst=Address("37:111111"),
        action=Action.SET_FAN_MODE,
        data={"fan_mode": "low", "scheme": "orcon", "legacy_format": True},
    )
    dto = build_dto(intent)

    assert dto.payload == expected_payload


def test_set_fan_mode_itho_3byte() -> None:
    """Itho scheme produces 3-byte payload with mode_max=04."""
    expected_payload = "000204"

    intent = Intent(
        src=Address("18:000730"),
        dst=Address("37:111111"),
        action=Action.SET_FAN_MODE,
        data={"fan_mode": "low", "scheme": "itho"},
    )
    dto = build_dto(intent)

    assert dto.payload == expected_payload


def test_set_fan_mode_vasco_3byte() -> None:
    """Vasco scheme produces 3-byte payload with mode_max=06."""
    expected_payload = "000406"

    intent = Intent(
        src=Address("18:000730"),
        dst=Address("37:111111"),
        action=Action.SET_FAN_MODE,
        data={"fan_mode": "high", "scheme": "vasco"},
    )
    dto = build_dto(intent)

    assert dto.payload == expected_payload


def test_set_fan_mode_siber_3byte_from_issue() -> None:
    """Siber DF Evo 4 payloads from issue #547 (orcon scheme, mode_max=07)."""
    expected_low = "000107"
    expected_int = "000207"

    intent_low = Intent(
        src=Address("18:000730"),
        dst=Address("37:111111"),
        action=Action.SET_FAN_MODE,
        data={"fan_mode": "low", "scheme": "orcon"},
    )
    dto_low = build_dto(intent_low)

    intent_int = Intent(
        src=Address("18:000730"),
        dst=Address("37:111111"),
        action=Action.SET_FAN_MODE,
        data={"fan_mode": 0x02, "scheme": "orcon"},
    )
    dto_int = build_dto(intent_int)

    assert dto_low.payload == expected_low
    assert dto_int.payload == expected_int


def test_set_fan_mode_nuaire_3byte() -> None:
    """Nuaire scheme produces 3-byte payload with mode_max=0A."""
    expected_payload = "00020A"

    intent = Intent(
        src=Address("18:000730"),
        dst=Address("37:111111"),
        action=Action.SET_FAN_MODE,
        data={"fan_mode": "normal", "scheme": "nuaire"},
    )
    dto = build_dto(intent)

    assert dto.payload == expected_payload


def test_set_fan_mode_int_index() -> None:
    """Integer fan_mode is treated as a hex mode index."""
    expected_payload = "000307"

    intent = Intent(
        src=Address("18:000730"),
        dst=Address("37:111111"),
        action=Action.SET_FAN_MODE,
        data={"fan_mode": 3, "scheme": "orcon"},
    )
    dto = build_dto(intent)

    assert dto.payload == expected_payload


def test_set_fan_mode_none_is_off() -> None:
    """None fan_mode maps to mode 00 (off/away)."""
    expected_payload = "000007"

    intent = Intent(
        src=Address("18:000730"),
        dst=Address("37:111111"),
        action=Action.SET_FAN_MODE,
        data={"fan_mode": None, "scheme": "orcon"},
    )
    dto = build_dto(intent)

    assert dto.payload == expected_payload


def test_set_fan_mode_invalid_scheme_raises() -> None:
    """An unknown scheme raises CommandInvalid."""
    intent = Intent(
        src=Address("18:000730"),
        dst=Address("37:111111"),
        action=Action.SET_FAN_MODE,
        data={"fan_mode": "low", "scheme": "bogus"},
    )
    with pytest.raises(ValueError, match="scheme is not valid"):
        build_dto(intent)


def test_set_fan_mode_invalid_mode_raises() -> None:
    """A mode not in the scheme's map raises CommandInvalid."""
    intent = Intent(
        src=Address("18:000730"),
        dst=Address("37:111111"),
        action=Action.SET_FAN_MODE,
        data={"fan_mode": "turbo", "scheme": "itho"},
    )
    with pytest.raises(ValueError, match="fan_mode is not valid"):
        build_dto(intent)


def test_build_put_co2_level() -> None:
    intent = Intent(
        src=Address("32:111111"),
        dst=Address("32:111111"),
        action=Action.PUT_CO2_LEVEL,
        data={"co2_level": 400.0},
    )
    dto = build_dto(intent)
    assert str(dto.verb) == " I"
    assert str(dto.code) == "1298"
    assert dto.payload == "000190"


def test_build_put_indoor_humidity() -> None:
    intent = Intent(
        src=Address("32:111111"),
        dst=Address("32:111111"),
        action=Action.PUT_INDOOR_HUMIDITY,
        data={"indoor_humidity": 0.5},
    )
    dto = build_dto(intent)
    assert str(dto.verb) == " I"
    assert str(dto.code) == "12A0"
    assert dto.payload == "0032"


def test_build_set_bypass_position() -> None:
    intent = Intent(
        src=Address("18:000730"),
        dst=Address("32:111111"),
        action=Action.SET_BYPASS_POSITION,
        data={"bypass_mode": "auto"},
    )
    dto = build_dto(intent)
    assert str(dto.verb) == " W"
    assert str(dto.code) == "22F7"
    assert dto.payload == "00FF"


def test_build_get_fan_param() -> None:
    intent = Intent(
        src=Address("18:000730"),
        dst=Address("32:111111"),
        action=Action.GET_FAN_PARAM,
        data={"param_id": "31"},
    )
    dto = build_dto(intent)
    assert str(dto.verb) == "RQ"
    assert str(dto.code) == "2411"
    assert dto.payload == "000031"


def test_build_set_fan_param() -> None:
    intent = Intent(
        src=Address("18:000730"),
        dst=Address("32:111111"),
        action=Action.SET_FAN_PARAM,
        data={"param_id": "31", "value": 30},
    )
    dto = build_dto(intent)
    assert str(dto.verb) == " W"
    assert str(dto.code) == "2411"
    assert dto.payload == "00003100100000001E0000000000000708000000010001"


def test_build_get_hvac_fan_31da() -> None:
    intent = Intent(
        src=Address("32:111111"),
        dst=Address("32:111111"),
        action=Action.GET_HVAC_FAN_31DA,
        data={
            "hvac_id": "0000",
            "bypass_position": None,
            "air_quality": None,
            "co2_level": None,
            "indoor_humidity": None,
            "outdoor_humidity": None,
            "exhaust_temp": None,
            "supply_temp": None,
            "indoor_temp": None,
            "outdoor_temp": None,
            "speed_capabilities": [],
            "fan_info": None,
            "_unknown_fan_info_flags": [],
            "exhaust_fan_speed": None,
            "supply_fan_speed": None,
            "remaining_mins": None,
            "post_heat": None,
            "pre_heat": None,
            "supply_flow": None,
            "exhaust_flow": None,
            "air_quality_basis": "00",
        },
    )
    dto = build_dto(intent)
    assert str(dto.verb) == " I"
    assert str(dto.code) == "31DA"
    assert dto.payload == "0000EF007FFFEFEF7FFF7FFF7FFF7FFF0000EFEFFFFF7FFFEFEF7FFF7FFF"


@pytest.mark.parametrize(
    ("data", "expected_payload"),
    [
        ({"fan_mode": None}, "000007"),
        ({"fan_mode": 0}, "000007"),
        ({"fan_mode": 1}, "000107"),
        ({"fan_mode": 2}, "000207"),
        ({"fan_mode": 3}, "000307"),
        ({"fan_mode": 4}, "000407"),
        ({"fan_mode": 5}, "000507"),
        ({"fan_mode": 6}, "000607"),
        ({"fan_mode": 7}, "000707"),
        ({"fan_mode": "00"}, "000007"),
        ({"fan_mode": "01"}, "000107"),
        ({"fan_mode": "02"}, "000207"),
        ({"fan_mode": "03"}, "000307"),
        ({"fan_mode": "04"}, "000407"),
        ({"fan_mode": "05"}, "000507"),
        ({"fan_mode": "06"}, "000607"),
        ({"fan_mode": "07"}, "000707"),
        ({"fan_mode": "away"}, "000007"),
        ({"fan_mode": "low"}, "000107"),
        ({"fan_mode": "medium"}, "000207"),
        ({"fan_mode": "high"}, "000307"),
        ({"fan_mode": "auto"}, "000407"),
        ({"fan_mode": "auto_alt"}, "000507"),
        ({"fan_mode": "boost"}, "000607"),
        ({"fan_mode": "off"}, "000707"),
    ],
)
def test_build_set_fan_mode_exhaustive(
    data: dict[str, Any], expected_payload: str
) -> None:
    intent = Intent(
        src=Address("18:000730"),
        dst=Address("37:111111"),
        action=Action.SET_FAN_MODE,
        data=data,
    )
    dto = build_dto(intent)
    assert str(dto.verb) == " I"  # wait! I frame? My previous tests used I... wait!
    # Wait, 22F1 from the old test was: f"000  I --- {REM} {HRU} {NUL} 22F1 003 000007"
    # Wait, the code in build_set_fan_mode defaults to I_.
    assert str(dto.code) == "22F1"
    assert dto.payload == expected_payload


@pytest.mark.parametrize(
    ("data", "expected_payload"),
    [
        ({"bypass_position": None}, "00FF"),
        ({"bypass_position": 0.0}, "0000"),
        ({"bypass_position": 1.0}, "00C8"),
        ({"bypass_mode": "auto"}, "00FF"),
        ({"bypass_mode": "off"}, "0000"),
        ({"bypass_mode": "on"}, "00C8"),
    ],
)
def test_build_set_bypass_position_exhaustive(
    data: dict[str, Any], expected_payload: str
) -> None:
    intent = Intent(
        src=Address("18:000730"),
        dst=Address("37:111111"),
        action=Action.SET_BYPASS_POSITION,
        data=data,
    )
    dto = build_dto(intent)
    assert str(dto.verb) == " W"
    assert str(dto.code) == "22F7"
    assert dto.payload == expected_payload


@pytest.mark.parametrize(
    ("data", "expected_payload"),
    [
        ({"indoor_humidity": None}, "00EF"),
        ({"indoor_humidity": 0.55}, "0037"),
    ],
)
def test_build_put_indoor_humidity_exhaustive(
    data: dict[str, Any], expected_payload: str
) -> None:
    intent = Intent(
        src=Address("32:111111"),
        dst=Address("32:111111"),
        action=Action.PUT_INDOOR_HUMIDITY,
        data=data,
    )
    dto = build_dto(intent)
    assert str(dto.verb) == " I"
    assert str(dto.code) == "12A0"
    assert dto.payload == expected_payload


@pytest.mark.parametrize(
    ("data", "expected_payload"),
    [
        ({"co2_level": 802}, "000322"),
    ],
)
def test_build_put_co2_level_exhaustive(
    data: dict[str, Any], expected_payload: str
) -> None:
    intent = Intent(
        src=Address("32:111111"),
        dst=Address("32:111111"),
        action=Action.PUT_CO2_LEVEL,
        data=data,
    )
    dto = build_dto(intent)
    assert str(dto.verb) == " I"
    assert str(dto.code) == "1298"
    assert dto.payload == expected_payload


@pytest.mark.parametrize(
    ("data", "expected_payload"),
    [
        (
            {
                "hvac_id": "00",
                "bypass_position": 0.000,
                "indoor_humidity": 0.52,
                "outdoor_humidity": 0.51,
                "exhaust_temp": 22.0,
                "supply_temp": 22.0,
                "indoor_temp": 21.86,
                "outdoor_temp": 21.78,
                "speed_capabilities": ["off", "low_med_high", "timer", "boost", "auto"],
                "fan_info": "away",
                "_unknown_fan_info_flags": [0, 0, 0],
                "exhaust_fan_speed": 0.1,
                "supply_fan_speed": 0.1,
                "remaining_mins": 0,
                "supply_flow": 15.25,
                "exhaust_flow": 15.55,
            },
            "00EF007FFF343308980898088A0882F800001514140000EFEF05F50613",
        ),
        (
            {
                "hvac_id": "00",
                "co2_level": 1202,
                "air_quality": 1.0,
                "air_quality_basis": "rel_humidity",
                "speed_capabilities": [
                    "off",
                    "low_med_high",
                    "timer",
                    "boost",
                    "auto",
                    "auto_night",
                ],
                "fan_info": "speed 3, high",
                "_unknown_fan_info_flags": [1, 0, 0],
                "exhaust_fan_speed": 0.155,
                "supply_fan_speed": 0.0,
                "remaining_mins": 0,
            },
            "00C84004B2EFEF7FFF7FFF7FFF7FFFF808EF831F000000EFEF7FFF7FFF",
        ),
        (
            {
                "hvac_id": "00",
                "speed_capabilities": [
                    "off",
                    "low_med_high",
                    "timer",
                    "boost",
                    "auto",
                    "auto_night",
                ],
                "fan_info": "speed 3, high",
                "_unknown_fan_info_flags": [1, 0, 0],
                "indoor_humidity": 0.48,
                "exhaust_fan_speed": 0.995,
                "supply_fan_speed": 0.0,
                "remaining_mins": 0,
            },
            "00EF007FFF30EF7FFF7FFF7FFF7FFFF808EF83C7000000EFEF7FFF7FFF",
        ),
        (
            {
                "hvac_id": "00",
                "speed_capabilities": ["off", "low_med_high", "timer", "boost"],
                "fan_info": "speed 1, low",
                "_unknown_fan_info_flags": [0, 0, 0],
                "exhaust_fan_speed": 0.49,
                "supply_fan_speed": 0.0,
                "remaining_mins": 0,
            },
            "00EF007FFFEFEF7FFF7FFF7FFF7FFFF000EF0162000000EFEF7FFF7FFF",
        ),
        (
            {
                "hvac_id": "21",
                "speed_capabilities": ["post_heater"],
                "fan_info": "auto",
                "_unknown_fan_info_flags": [0, 0, 0],
                "co2_level": 513,
                "indoor_humidity": 0.54,
                "remaining_mins": 0,
                "post_heat": 0.0,
            },
            "21EF00020136EF7FFF7FFF7FFF7FFF0002EF18FFFF000000EF7FFF7FFF",
        ),
        (
            {
                "hvac_id": "00",
                "indoor_humidity": 0.44,
                "speed_capabilities": ["off", "low_med_high", "timer", "boost", "auto"],
                "fan_info": "speed 1, low",
                "_unknown_fan_info_flags": [0, 0, 0],
                "exhaust_fan_speed": 0.2,
                "supply_fan_speed": 0.0,
                "remaining_mins": 0,
                "_extra": "00",
            },
            "00EF007FFF2CEF7FFF7FFF7FFF7FFFF800EF0128000000EFEF7FFF7FFF00",
        ),
        (
            {
                "hvac_id": "21",
                "exhaust_fan_speed": 0.26,
                "supply_fan_speed": 0.32,
                "indoor_humidity": 0.65,
                "exhaust_temp": 20.54,
                "supply_temp": 20.08,
                "indoor_temp": 23.76,
                "outdoor_temp": 18.47,
                "speed_capabilities": [
                    "off",
                    "low_med_high",
                    "timer",
                    "boost",
                    "post_heater",
                ],
                "bypass_position": 0.85,
                "fan_info": "speed 2, medium",
                "_unknown_fan_info_flags": [0, 0, 0],
                "remaining_mins": 0,
                "post_heat": 0.0,
                "pre_heat": 0.46,
                "_extra": "00",
            },
            "21EF007FFF41EF080607D709480737F002AA0234400000005C7FFF7FFF00",
        ),
    ],
)
def test_build_get_hvac_fan_31da_exhaustive(
    data: dict[str, Any], expected_payload: str
) -> None:
    # Need to add missing fields as None so that get_hvac_fan_31da doesn't complain about missing keys,
    # but build_dto for 31DA actually handles None for missing data fields!
    intent = Intent(
        src=Address("32:111111"),
        dst=Address("32:111111"),
        action=Action.GET_HVAC_FAN_31DA,
        data=data,
    )
    dto = build_dto(intent)
    assert str(dto.verb) == " I"
    assert str(dto.code) == "31DA"
    assert dto.payload == expected_payload
