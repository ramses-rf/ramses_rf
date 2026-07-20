from datetime import datetime as dt
from typing import Any
from unittest.mock import patch

import pytest

from ramses_rf.address import Address
from ramses_rf.commands.builders import build_dto
from ramses_rf.commands.core import Command as Intent
from ramses_rf.enums import Action
from ramses_tx.const import ZON_MODE_MAP, FaultDeviceClass, FaultState, FaultType
from ramses_tx.packet import Packet


def test_build_get_schedule_version(snapshot: Any) -> None:
    intent = Intent(
        src=Address("18:000730"),
        dst=Address("01:111111"),
        action=Action.GET_SCHEDULE_VERSION,
        data={},
    )
    dto = build_dto(intent)
    assert str(Packet._from_cmd(dto)._frame) == snapshot


def test_build_get_dhw_params(snapshot: Any) -> None:
    intent = Intent(
        src=Address("18:000730"),
        dst=Address("01:111111"),
        action=Action.GET_DHW_PARAMS,
        data={"dhw_idx": 0},
    )
    dto = build_dto(intent)
    assert str(Packet._from_cmd(dto)._frame) == snapshot


@pytest.mark.parametrize(
    ("setpoint", "overrun", "differential"),
    [
        (55.0, 8, 2.0),
        (30.0, 0, 1.0),
        (85.0, 10, 10.0),
        (50.0, 5, 1.0),
    ],
)
def test_build_set_dhw_params_parity(
    setpoint: float, overrun: int, differential: float, snapshot: Any
) -> None:
    intent = Intent(
        src=Address("18:000730"),
        dst=Address("01:111111"),
        action=Action.SET_DHW_PARAMS,
        data={
            "dhw_idx": 0,
            "setpoint": setpoint,
            "overrun": overrun,
            "differential": differential,
        },
    )
    dto = build_dto(intent)
    assert str(Packet._from_cmd(dto)._frame) == snapshot


def test_build_get_dhw_temp(snapshot: Any) -> None:
    intent = Intent(
        src=Address("18:000730"),
        dst=Address("01:111111"),
        action=Action.GET_DHW_TEMP,
        data={"dhw_idx": 0},
    )
    dto = build_dto(intent)
    assert str(Packet._from_cmd(dto)._frame) == snapshot


def test_build_put_dhw_temp(snapshot: Any) -> None:
    intent = Intent(
        src=Address("07:111111"),
        dst=Address("07:111111"),
        action=Action.PUT_DHW_TEMP,
        data={"dhw_idx": 0, "temperature": 50.5},
    )
    dto = build_dto(intent)
    assert str(Packet._from_cmd(dto)._frame) == snapshot


def test_build_get_dhw_mode(snapshot: Any) -> None:
    intent = Intent(
        src=Address("18:000730"),
        dst=Address("01:111111"),
        action=Action.GET_DHW_MODE,
        data={"dhw_idx": 0},
    )
    dto = build_dto(intent)
    assert str(Packet._from_cmd(dto)._frame) == snapshot


@pytest.mark.parametrize(
    ("mode", "active", "until", "duration"),
    [
        (0, True, None, None),
        (1, False, None, None),
        (2, True, None, None),
        (4, True, dt(2026, 7, 18, 12, 0), None),
        ("follow_schedule", None, None, None),
        ("advanced_override", True, None, None),
        ("permanent_override", False, None, None),
        ("temporary_override", True, dt(2026, 7, 18, 12, 0), None),
        (ZON_MODE_MAP.FOLLOW, None, None, None),
        (ZON_MODE_MAP.ADVANCED, False, None, None),
        (ZON_MODE_MAP.PERMANENT, True, None, None),
        (ZON_MODE_MAP.TEMPORARY, False, dt(2026, 7, 18, 12, 0), None),
    ],
)
def test_build_set_dhw_mode_parity(
    mode: Any, active: Any, until: Any, duration: Any, snapshot: Any
) -> None:
    intent = Intent(
        src=Address("18:000730"),
        dst=Address("01:111111"),
        action=Action.SET_DHW_MODE,
        data={
            "dhw_idx": 0,
            "mode": mode,
            "active": active,
            "until": until,
            "duration": duration,
        },
    )
    dto = build_dto(intent)
    assert str(Packet._from_cmd(dto)._frame) == snapshot


def test_build_get_schedule_fragment(snapshot: Any) -> None:
    intent = Intent(
        src=Address("18:000730"),
        dst=Address("01:111111"),
        action=Action.GET_SCHEDULE_FRAGMENT,
        data={"zone_idx": 0, "frag_number": 1, "total_frags": 0},
    )
    dto = build_dto(intent)
    assert str(Packet._from_cmd(dto)._frame) == snapshot


def test_build_set_schedule_fragment(snapshot: Any) -> None:
    intent = Intent(
        src=Address("18:000730"),
        dst=Address("01:111111"),
        action=Action.SET_SCHEDULE_FRAGMENT,
        data={"zone_idx": 0, "frag_num": 1, "frag_cnt": 3, "fragment": "0011223344"},
    )
    dto = build_dto(intent)
    assert str(Packet._from_cmd(dto)._frame) == snapshot


def test_build_get_faultlog_entry(snapshot: Any) -> None:
    intent = Intent(
        src=Address("18:000730"),
        dst=Address("01:111111"),
        action=Action.GET_FAULTLOG_ENTRY,
        data={"log_idx": 5},
    )
    dto = build_dto(intent)
    assert str(Packet._from_cmd(dto)._frame) == snapshot


def test_build_get_opentherm_data(snapshot: Any) -> None:
    intent = Intent(
        src=Address("18:000730"),
        dst=Address("10:111111"),
        action=Action.GET_OPENTHERM_DATA,
        data={"msg_id": 14},
    )
    dto = build_dto(intent)
    assert str(Packet._from_cmd(dto)._frame) == snapshot


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


def test_build_put_co2_level(snapshot: Any) -> None:
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


def test_build_put_indoor_humidity(snapshot: Any) -> None:
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


def test_build_set_bypass_position(snapshot: Any) -> None:
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


def test_build_get_fan_param(snapshot: Any) -> None:
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


def test_build_set_fan_param(snapshot: Any) -> None:
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


def test_build_get_hvac_fan_31da(snapshot: Any) -> None:
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
    data: dict[str, Any], expected_payload: str, snapshot: Any
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
    data: dict[str, Any], expected_payload: str, snapshot: Any
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
    data: dict[str, Any], expected_payload: str, snapshot: Any
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
    data: dict[str, Any], expected_payload: str, snapshot: Any
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
    data: dict[str, Any], expected_payload: str, snapshot: Any
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


@pytest.mark.parametrize(
    "setpoint",
    [5.0, 10.0, 15.5, 21.0, 21.5, 30.0, 35.0],
)
def test_build_set_zone_setpoint_parity(setpoint: float, snapshot: Any) -> None:

    intent = Intent(
        src=Address("18:000730"),
        dst=Address("01:111111"),
        action=Action.SET_TEMPERATURE,
        data={"zone_idx": 0, "setpoint": setpoint},
    )

    dto = build_dto(intent)

    assert str(Packet._from_cmd(dto)._frame) == snapshot


@pytest.mark.parametrize(
    "name",
    [
        "Living Room",
        "A",
        "",
        "Extremely Long Zone Name That Exceeds The Limit Of Characters",
        "12345678901234567890",
    ],
)
def test_build_set_zone_name_parity(name: str, snapshot: Any) -> None:

    intent = Intent(
        src=Address("18:000730"),
        dst=Address("01:111111"),
        action=Action.SET_ZONE_NAME,
        data={"zone_idx": 1, "name": name},
    )

    dto = build_dto(intent)

    assert str(Packet._from_cmd(dto)._frame) == snapshot


@pytest.mark.parametrize("local_override", [True, False])
@pytest.mark.parametrize("openwindow_function", [True, False])
@pytest.mark.parametrize("multiroom_mode", [True, False])
@pytest.mark.parametrize(
    ("min_temp", "max_temp"),
    [(5.0, 35.0), (10.0, 25.0), (21.0, 21.0)],
)
def test_build_set_zone_config_parity(
    local_override: bool,
    openwindow_function: bool,
    multiroom_mode: bool,
    min_temp: float,
    max_temp: float,
    snapshot: Any,
) -> None:

    intent = Intent(
        src=Address("18:000730"),
        dst=Address("01:111111"),
        action=Action.SET_ZONE_CONFIG,
        data={
            "zone_idx": 2,
            "min_temp": min_temp,
            "max_temp": max_temp,
            "local_override": local_override,
            "openwindow_function": openwindow_function,
            "multiroom_mode": multiroom_mode,
        },
    )

    dto = build_dto(intent)

    assert str(Packet._from_cmd(dto)._frame) == snapshot


@pytest.mark.parametrize(
    ("mode", "setpoint", "until", "duration"),
    [
        (0, 15.0, None, None),
        (1, 21.0, None, None),
        (2, 22.0, None, None),
        (4, 10.0, None, None),
        ("follow_schedule", None, None, None),
        ("advanced_override", 20.0, None, None),
        ("permanent_override", 21.0, None, None),
        ("temporary_override", 22.0, dt(2026, 7, 18, 12, 0), None),
        (ZON_MODE_MAP.FOLLOW, None, None, None),
        (ZON_MODE_MAP.ADVANCED, 20.0, None, None),
        (ZON_MODE_MAP.PERMANENT, 21.0, None, None),
        (ZON_MODE_MAP.TEMPORARY, 22.0, dt(2026, 7, 18, 12, 0), None),
    ],
)
def test_build_set_zone_mode_parity(
    mode: Any, setpoint: Any, until: Any, duration: Any, snapshot: Any
) -> None:

    intent = Intent(
        src=Address("18:000730"),
        dst=Address("01:111111"),
        action=Action.SET_MODE,
        data={
            "zone_idx": 0,
            "mode": mode,
            "setpoint": setpoint,
            "until": until,
            "duration": duration,
        },
    )

    dto = build_dto(intent)

    assert str(Packet._from_cmd(dto)._frame) == snapshot


def test_build_put_weather_temp(snapshot: Any) -> None:
    intent = Intent(
        src=Address("17:000730"),
        dst=Address("17:000730"),
        action=Action.PUT_WEATHER_TEMP,
        data={"temperature": 12.5},
    )
    dto = build_dto(intent)
    assert str(Packet._from_cmd(dto)._frame) == snapshot


def test_build_get_relay_demand(snapshot: Any) -> None:
    intent = Intent(
        src=Address("18:000730"),
        dst=Address("13:111111"),
        action=Action.GET_RELAY_DEMAND,
        data={"zone_idx": 0},
    )
    dto = build_dto(intent)
    assert str(Packet._from_cmd(dto)._frame) == snapshot


def test_build_get_system_language(snapshot: Any) -> None:
    intent = Intent(
        src=Address("18:000730"),
        dst=Address("01:111111"),
        action=Action.GET_SYSTEM_LANGUAGE,
        data={},
    )
    dto = build_dto(intent)
    assert str(Packet._from_cmd(dto)._frame) == snapshot


def test_build_get_mix_valve_params(snapshot: Any) -> None:
    intent = Intent(
        src=Address("18:000730"),
        dst=Address("01:111111"),
        action=Action.GET_MIX_VALVE_PARAMS,
        data={"zone_idx": 0},
    )
    dto = build_dto(intent)
    assert str(Packet._from_cmd(dto)._frame) == snapshot


def test_build_set_mix_valve_params(snapshot: Any) -> None:
    intent = Intent(
        src=Address("18:000730"),
        dst=Address("01:111111"),
        action=Action.SET_MIX_VALVE_PARAMS,
        data={
            "zone_idx": 0,
            "max_flow_setpoint": 55,
            "min_flow_setpoint": 15,
            "valve_run_time": 150,
            "pump_run_time": 15,
        },
    )
    dto = build_dto(intent)
    assert str(Packet._from_cmd(dto)._frame) == snapshot


def test_build_get_tpi_params(snapshot: Any) -> None:
    intent = Intent(
        src=Address("18:000730"),
        dst=Address("01:111111"),
        action=Action.GET_TPI_PARAMS,
        data={"domain_id": "00"},
    )
    dto = build_dto(intent)
    assert str(Packet._from_cmd(dto)._frame) == snapshot


def test_build_set_tpi_params(snapshot: Any) -> None:
    intent = Intent(
        src=Address("18:000730"),
        dst=Address("01:111111"),
        action=Action.SET_TPI_PARAMS,
        data={
            "domain_id": "00",
            "cycle_rate": 3,
            "min_on_time": 5,
            "min_off_time": 5,
            "proportional_band_width": 1.5,
        },
    )
    dto = build_dto(intent)
    assert str(Packet._from_cmd(dto)._frame) == snapshot


def test_build_put_bind_offer(snapshot: Any) -> None:
    intent = Intent(
        src=Address("13:111111"),
        dst=Address("63:262143"),
        action=Action.PUT_BIND,
        data={"verb": " I", "codes": ["3EF0"], "oem_code": "01"},
    )
    dto = build_dto(intent)
    assert str(Packet._from_cmd(dto)._frame) == snapshot


def test_build_put_bind_accept(snapshot: Any) -> None:
    intent = Intent(
        src=Address("01:111111"),
        dst=Address("13:111111"),
        action=Action.PUT_BIND,
        data={"verb": " W", "codes": ["3EF0"]},
    )
    dto = build_dto(intent)
    assert str(Packet._from_cmd(dto)._frame) == snapshot


def test_build_put_bind_confirm(snapshot: Any) -> None:
    intent = Intent(
        src=Address("13:111111"),
        dst=Address("01:111111"),
        action=Action.PUT_BIND,
        data={"verb": " I", "codes": ["3EF0"]},
    )
    dto = build_dto(intent)
    assert str(Packet._from_cmd(dto)._frame) == snapshot


def test_build_get_system_mode(snapshot: Any) -> None:
    intent = Intent(
        src=Address("18:000730"),
        dst=Address("01:111111"),
        action=Action.GET_SYSTEM_MODE,
        data={},
    )
    dto = build_dto(intent)
    assert str(Packet._from_cmd(dto)._frame) == snapshot


def test_build_set_system_mode(snapshot: Any) -> None:
    intent = Intent(
        src=Address("18:000730"),
        dst=Address("01:111111"),
        action=Action.SET_SYSTEM_MODE,
        data={"system_mode": 1},
    )
    dto = build_dto(intent)
    assert str(Packet._from_cmd(dto)._frame) == snapshot


def test_build_put_presence_detected(snapshot: Any) -> None:
    intent = Intent(
        src=Address("18:000730"),
        dst=Address("18:000730"),
        action=Action.PUT_PRESENCE_DETECTED,
        data={"presence_detected": True},
    )
    dto = build_dto(intent)
    assert str(Packet._from_cmd(dto)._frame) == snapshot


def test_build_get_system_time(snapshot: Any) -> None:
    intent = Intent(
        src=Address("18:000730"),
        dst=Address("01:111111"),
        action=Action.GET_SYSTEM_TIME,
        data={},
    )
    dto = build_dto(intent)
    assert str(Packet._from_cmd(dto)._frame) == snapshot


def test_build_set_system_time(snapshot: Any) -> None:
    intent = Intent(
        src=Address("18:000730"),
        dst=Address("01:111111"),
        action=Action.SET_SYSTEM_TIME,
        data={"datetime": dt(2026, 7, 18, 12, 0)},
    )
    dto = build_dto(intent)
    assert str(Packet._from_cmd(dto)._frame) == snapshot


def test_build_put_actuator_state(snapshot: Any) -> None:
    intent = Intent(
        src=Address("13:111111"),
        dst=Address("13:111111"),
        action=Action.PUT_ACTUATOR_STATE,
        data={"modulation_level": 0.5},
    )
    dto = build_dto(intent)
    assert str(Packet._from_cmd(dto)._frame) == snapshot


def test_build_put_actuator_cycle(snapshot: Any) -> None:
    intent = Intent(
        src=Address("13:111111"),
        dst=Address("01:111111"),
        action=Action.PUT_ACTUATOR_CYCLE,
        data={
            "modulation_level": 0.5,
            "actuator_countdown": 200,
            "cycle_countdown": 100,
        },
    )
    dto = build_dto(intent)
    assert str(Packet._from_cmd(dto)._frame) == snapshot


@patch("ramses_tx.version.VERSION", "0.0.0")
@patch("ramses_rf.commands.builders.system.timestamp", return_value=1700000000.0)
def test_build_send_puzzle(mock_timestamp: Any, snapshot: Any) -> None:
    intent = Intent(
        src=Address("18:000730"),
        dst=Address("63:262143"),
        action=Action.SEND_PUZZLE,
        data={"msg_type": "10"},
    )
    dto = build_dto(intent)
    assert str(Packet._from_cmd(dto)._frame) == snapshot


def test_build_put_faultlog_entry(snapshot: Any) -> None:

    intent = Intent(
        src=Address("01:111111"),
        dst=Address("18:000730"),
        action=Action.PUT_FAULTLOG_ENTRY,
        data={
            "fault_state": FaultState.FAULT,
            "fault_type": FaultType.COMMS_FAULT,
            "device_class": FaultDeviceClass.CONTROLLER,
            "device_id": "01:111111",
            "log_idx": 1,
            "timestamp": dt(2026, 7, 18, 12, 0),
        },
    )
    dto = build_dto(intent)
    assert str(Packet._from_cmd(dto)._frame) == snapshot


def test_build_put_sensor_temp(snapshot: Any) -> None:
    intent = Intent(
        src=Address("04:111111"),
        dst=Address("04:111111"),
        action=Action.PUT_SENSOR_TEMP,
        data={"temperature": 21.5},
    )
    dto = build_dto(intent)
    assert str(Packet._from_cmd(dto)._frame) == snapshot


def test_build_put_outdoor_temp(snapshot: Any) -> None:
    intent = Intent(
        src=Address("17:111111"),
        dst=Address("17:111111"),
        action=Action.PUT_OUTDOOR_TEMP,
        data={"temperature": 15.0},
    )
    dto = build_dto(intent)
    assert str(Packet._from_cmd(dto)._frame) == snapshot
