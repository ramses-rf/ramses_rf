from dataclasses import replace
from datetime import datetime as dt

import pytest

import ramses_rf.payloads.dhw
import ramses_rf.payloads.heating
import ramses_rf.payloads.hvac
import ramses_rf.payloads.system
import ramses_tx.const as tx_const
from ramses_rf.const import (
    SZ_DIAGNOSTIC_CODE,
    SZ_FAN_MODE,
    SZ_FLAGS,
    SZ_FRAME_CODE,
    SZ_HEAT_DEMAND,
    SZ_STATUS_CODE,
    SZ_TEMPERATURE,
    SZ_TICKER,
    SZ_VALUE,
    Code,
    Verb,
)
from ramses_rf.parsers.decoder import decode_packet
from ramses_rf.payloads.adapters import payload_to_dict
from ramses_rf.payloads.dhw import (
    DhwConfigPayload,
    DhwParams3BPayload,
    DhwParamsPayload,
    DhwStatePayload,
    DhwTempPayload,
)
from ramses_rf.payloads.heating import (
    ActuatorStatePayload,
    BindingPayload,
    FlowTempPayload,
    HeatDemandPayload,
    OutdoorTempPayload,
    RelayDemandPayload,
    ScheduleFragmentPayload,
    ScheduleSwitchpointPayload,
    SetPointInfoPayload,
    SystemSyncPayload,
    TemperaturePayload,
    ZoneConfigPayload,
    ZoneModePayload,
    ZoneNamePayload,
    ZoneSetpointPayload,
)
from ramses_rf.payloads.hvac import (
    Co2Payload,
    Co22BPayload,
    Co23BPayload,
    FanModePayload,
    HvacAirQualityPayload,
    HvacBypassStatePayload,
    HvacFanParamPayload,
    HvacFaultStatusPayload,
    HvacFilterChangePayload,
    HvacTimeOffsetPayload,
    HvacVentilationStatusPayload,
    RelativeHumidityPayload,
    WindowStatePayload,
)
from ramses_rf.payloads.opentherm import (
    OpenThermBridgeStatusPayload,
    OpenThermDiagnosticsPayload,
    OpenThermFaultFlags2BPayload,
    OpenThermFaultFlags3BPayload,
    OpenThermFaultFlagsPayload,
    OpenThermFrameExPayload,
    OpenThermMsgPayload,
    OpenThermParamsPayload,
    OpenThermSetpointPayload,
    OpenThermStatusPayload,
    ReturnTempPayload,
)
from ramses_rf.payloads.registry import PAYLOAD_REGISTRY
from ramses_rf.payloads.system import (
    SystemClockPayload,
    SystemConfigPayload,
    SystemDatePayload,
    SystemDateTimePayload,
    SystemFaultLogPayload,
)
from ramses_tx.dtos import PacketDTO


def test_heat_demand_payload_3150_parity() -> None:
    # Arrange
    raw_hex = "C8"
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = HeatDemandPayload.from_bytes(raw_bytes)
    assert isinstance(payload, HeatDemandPayload)
    reencoded = payload.to_bytes().hex().upper()
    as_dict = payload_to_dict(payload)

    # Assert
    assert payload.demand_percent == 200
    assert reencoded == raw_hex
    assert as_dict == {
        "domain_or_zone_index": None,
        "demand_percent": 200,
        "raw_extra": None,
    }


def test_heat_demand_payload_3150_2byte_parity() -> None:
    # Arrange
    raw_hex = "01CA"
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = HeatDemandPayload.from_bytes(raw_bytes)
    assert isinstance(payload, HeatDemandPayload)
    reencoded = payload.to_bytes().hex().upper()
    as_dict = payload_to_dict(payload)

    # Assert
    assert payload.domain_or_zone_index == 1
    assert payload.demand_percent == 202
    assert payload.raw_extra is None
    assert reencoded == raw_hex
    assert as_dict == {
        "domain_or_zone_index": 1,
        "demand_percent": 202,
        "raw_extra": None,
    }


def test_heat_demand_payload_3150_multibyte_parity() -> None:
    # Arrange
    raw_hex = "01CA0011"
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = HeatDemandPayload.from_bytes(raw_bytes)
    assert isinstance(payload, list)
    reencoded = b"".join(x.to_bytes() for x in payload).hex().upper()
    as_dict = [payload_to_dict(x) for x in payload]

    # Assert
    assert payload[0].domain_or_zone_index == 1
    assert payload[0].demand_percent == 202
    assert reencoded == raw_hex
    assert as_dict == [
        {"domain_or_zone_index": 1, "demand_percent": 202, "raw_extra": None},
        {"domain_or_zone_index": 0, "demand_percent": 17, "raw_extra": None},
    ]


def test_temperature_payload_30c9_simple_parity() -> None:
    # Arrange
    raw_hex = "07D0"
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = TemperaturePayload.from_bytes(raw_bytes)
    assert isinstance(payload, TemperaturePayload)
    reencoded = payload.to_bytes().hex().upper()
    as_dict = payload_to_dict(payload)

    # Assert
    assert payload.zone_index is None
    assert payload.temperature == 20.0
    assert reencoded == raw_hex
    assert as_dict == {"zone_index": None, "temperature": 20.0}


def test_temperature_payload_30c9_zone_parity() -> None:
    # Arrange
    raw_hex = "0107D0"
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = TemperaturePayload.from_bytes(raw_bytes)
    assert isinstance(payload, TemperaturePayload)
    reencoded = payload.to_bytes().hex().upper()
    as_dict = payload_to_dict(payload)

    # Assert
    assert payload.zone_index == 1
    assert payload.temperature == 20.0
    assert reencoded == raw_hex
    assert as_dict == {"zone_index": 1, "temperature": 20.0}


def test_schedule_switchpoint_payload_0404_parity() -> None:
    # Arrange
    raw_hex = "00000000010000000100000068010000D0070000"
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = ScheduleSwitchpointPayload.from_bytes(raw_bytes)
    assert isinstance(payload, ScheduleSwitchpointPayload)
    reencoded = payload.to_bytes().hex().upper()
    as_dict = payload_to_dict(payload)

    # Assert
    assert payload.zone_index == 1
    assert payload.day_of_week == 1
    assert payload.time_of_day_mins == 360
    assert payload.setpoint_value == 2000
    assert reencoded == raw_hex
    assert as_dict == {
        "zone_index": 1,
        "day_of_week": 1,
        "time_of_day_mins": 360,
        "setpoint_value": 2000,
    }


def test_dhw_params_payload_10a0_parity() -> None:
    # Arrange
    raw_hex = "000ED8"
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = DhwParamsPayload.from_bytes(raw_bytes)
    assert isinstance(payload, DhwParams3BPayload)
    reencoded = payload.to_bytes().hex().upper()
    as_dict = payload_to_dict(payload)

    # Assert
    assert payload.dhw_index == 0
    assert payload.setpoint == 38.0

    assert reencoded == raw_hex
    assert as_dict == {
        "dhw_index": 0,
        "setpoint": 38.0,
        "overrun": None,
        "differential": None,
    }


def test_system_sync_payload_1030_parity() -> None:
    # Arrange
    raw_hex = "00"
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = SystemSyncPayload.from_bytes(raw_bytes)
    reencoded = payload.to_bytes().hex().upper()
    as_dict = payload_to_dict(payload)

    # Assert
    assert payload.sync_flag == 0
    assert reencoded == raw_hex
    assert as_dict == {
        "sync_flag": 0,
        "max_flow_setpoint": None,
        "min_flow_setpoint": None,
        "valve_run_time": None,
        "pump_run_time": None,
    }

    # Verify programmatic creation on the fly packs parameter bytes dynamically
    on_fly_payload = SystemSyncPayload.create(
        sync_flag=10, max_flow_setpoint=55
    )
    assert on_fly_payload.to_bytes().hex().upper() == "0AC80137"


def test_binding_payload_1fc9_parity() -> None:
    # Arrange
    raw_hex = "000102030405"
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = BindingPayload.from_bytes(raw_bytes)
    reencoded = payload.to_bytes().hex().upper()
    as_dict = payload_to_dict(payload)

    # Assert
    assert payload.binding_type == 0
    assert payload.binding_data == b"\x01\x02\x03\x04\x05"
    assert reencoded == raw_hex
    assert as_dict == {
        "binding_type": 0,
        "binding_data": b"\x01\x02\x03\x04\x05",
    }


def test_zone_config_payload_000a_parity() -> None:
    # Arrange
    raw_hex = "000001F40DB8"
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = ZoneConfigPayload.from_bytes(raw_bytes)
    assert isinstance(payload, ZoneConfigPayload)
    reencoded = payload.to_bytes().hex().upper()
    as_dict = payload_to_dict(payload)

    # Assert
    assert payload.zone_index == 0
    assert payload.zone_flags == 0
    assert payload.min_temp == 5.0
    assert payload.max_temp == 35.12
    assert reencoded == raw_hex
    assert as_dict == {
        "zone_index": 0,
        "zone_flags": 0,
        "min_temp": 5.0,
        "max_temp": 35.12,
    }


def test_fan_mode_payload_22f1_parity() -> None:
    # Arrange
    raw_hex = "000204"
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = FanModePayload.from_bytes(raw_bytes)
    reencoded = payload.to_bytes().hex().upper()
    as_dict = payload_to_dict(payload)

    # Assert
    assert payload.header == 0
    assert payload.mode_index == 2
    assert payload.mode_max == 4
    assert reencoded == raw_hex
    assert as_dict == {"header": 0, "mode_index": 2, "mode_max": 4}


def test_hvac_fan_param_payload_2411_parity() -> None:
    # Arrange
    raw_hex = "00000A0010000000050000000000000064000000010001"
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = HvacFanParamPayload.from_bytes(raw_bytes)
    reencoded = payload.to_bytes().hex().upper()
    as_dict = payload_to_dict(payload)

    # Assert
    assert payload.parameter_id == 10
    assert payload.data_type == 16
    assert payload.value_scaled == 5
    assert payload.min_value_scaled == 0
    assert payload.max_value_scaled == 100
    assert payload.precision_scaled == 1
    assert payload.trailer_bytes == b"\x00\x01"
    assert reencoded == raw_hex
    assert as_dict == {
        "parameter_id": 10,
        "data_type": 16,
        "value_scaled": 5,
        "min_value_scaled": 0,
        "max_value_scaled": 100,
        "precision_scaled": 1,
        "trailer_bytes": b"\x00\x01",
    }


def test_hvac_fan_param_temperature_scaling_2411() -> None:
    # Arrange (Param 75 Comfort Temperature, 20.0 °C, 0-30 °C, prec 0.01)
    raw_hex = "0000750092000007D00000000000000753000000010001"
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = HvacFanParamPayload.from_bytes(raw_bytes)
    reencoded = payload.to_bytes().hex().upper()
    to_dict_res = payload.to_dict()

    # Assert
    assert payload.parameter_id == 0x75
    assert payload.data_type == 0x92
    assert payload.value_scaled == 20.0
    assert payload.min_value_scaled == 0.0
    assert payload.max_value_scaled == 18.75  # 0x0753 / 100 = 18.75
    assert payload.precision_scaled == 0.01
    assert reencoded == raw_hex
    assert to_dict_res["parameter"] == "75"
    assert to_dict_res["value"] == 20.0
    assert to_dict_res["min_value"] == 0.0
    assert to_dict_res["precision"] == 0.01


def test_hvac_fan_param_percentage_scaling_2411() -> None:
    # Arrange (Param 41 Medium Fan Rate, 50% = 0.5, 0-100%, prec 0.005)
    # raw value 100 (0x64) -> 0.5, max value 200 (0xC8) -> 1.0, prec 1 -> 0.005
    raw_hex = "000041000F0000006400000000000000C8000000010001"
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = HvacFanParamPayload.from_bytes(raw_bytes)
    reencoded = payload.to_bytes().hex().upper()
    to_dict_res = payload.to_dict()

    # Assert
    assert payload.parameter_id == 0x41
    assert payload.data_type == 0x0F
    assert payload.value_scaled == 0.5
    assert payload.min_value_scaled == 0.0
    assert payload.max_value_scaled == 1.0
    assert payload.precision_scaled == 0.005
    assert reencoded == raw_hex
    assert to_dict_res["parameter"] == "41"
    assert to_dict_res["value"] == 0.5
    assert to_dict_res["min_value"] == 0.0
    assert to_dict_res["max_value"] == 1.0
    assert to_dict_res["precision"] == 0.005


def test_hvac_fan_param_centile_scaling_2411() -> None:
    # Arrange (Param 52 Sensor Sensitivity %, 2.0%, 0-25.0%, prec 0.1)
    # raw value 20 (0x14) -> 2.0, max value 250 (0xFA) -> 25.0, prec 1 -> 0.1
    raw_hex = "00005200010000001400000000000000FA000000010001"
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = HvacFanParamPayload.from_bytes(raw_bytes)
    reencoded = payload.to_bytes().hex().upper()
    to_dict_res = payload.to_dict()

    # Assert
    assert payload.parameter_id == 0x52
    assert payload.data_type == 0x01
    assert payload.value_scaled == 2.0
    assert payload.min_value_scaled == 0.0
    assert payload.max_value_scaled == 25.0
    assert payload.precision_scaled == 0.1
    assert reencoded == raw_hex
    assert to_dict_res["parameter"] == "52"
    assert to_dict_res["value"] == 2.0
    assert to_dict_res["min_value"] == 0.0
    assert to_dict_res["max_value"] == 25.0
    assert to_dict_res["precision"] == 0.1


def test_co2_payload_1298_parity() -> None:
    # Arrange
    raw_hex = "02D0"
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = Co2Payload.from_bytes(raw_bytes)
    reencoded = payload.to_bytes().hex().upper()
    as_dict = payload_to_dict(payload)

    # Assert
    assert payload.co2_level == 720
    assert payload.co2_level_fault is None
    assert reencoded == raw_hex
    assert as_dict == {"co2_level": 720, "co2_level_fault": None}
    # to_dict() omits the fault key when there is no fault
    assert payload.to_dict() == {"co2_level": 720}


def test_co2_payload_1298_fault_out_of_range_low() -> None:
    # Arrange — 0x8400 is a sensor fault (out_of_range_low), not 33792 ppm.
    # See issue ramses-rf/ramses_rf#1105 (Tweakers Orcon CO2 spike report).
    raw_hex = "8400"
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = Co2Payload.from_bytes(raw_bytes)
    as_dict = payload_to_dict(payload)

    # Assert
    assert payload.co2_level is None
    assert payload.co2_level_fault == "out_of_range_low"
    assert as_dict == {
        "co2_level": None,
        "co2_level_fault": "out_of_range_low",
    }
    assert payload.to_dict() == {
        "co2_level": None,
        "co2_level_fault": "out_of_range_low",
    }


def test_co2_payload_1298_fault_3byte() -> None:
    # Arrange — 3-byte variant with domain_index + fault high byte.
    raw_hex = "008300"
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = Co2Payload.from_bytes(raw_bytes)

    # Assert
    assert isinstance(payload, Co23BPayload)
    assert payload.domain_index == 0
    assert payload.co2_level is None
    assert payload.co2_level_fault == "out_of_range_high"


@pytest.mark.parametrize(
    "raw_hex,fault_name",
    [
        ("8000", "short_circuit"),
        ("8100", "open_circuit"),
        ("8200", "unavailable"),
        ("8300", "out_of_range_high"),
        ("8400", "out_of_range_low"),
        ("8500", "unreliable"),
    ],
)
def test_co2_payload_1298_fault_map(raw_hex: str, fault_name: str) -> None:
    # Arrange
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = Co22BPayload.from_bytes(raw_bytes)

    # Assert
    assert payload.co2_level is None
    assert payload.co2_level_fault == fault_name


def test_co2_payload_1298_sentinel_no_fault() -> None:
    # Arrange — 0x7FFF sentinel means "no reading", not a fault.
    raw_bytes = (0x7FFF).to_bytes(2, "big")

    # Act
    payload = Co22BPayload.from_bytes(raw_bytes)

    # Assert
    assert payload.co2_level is None
    assert payload.co2_level_fault is None


def test_relative_humidity_payload_12a0_parity() -> None:
    # Arrange
    raw_hex = "64"
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = RelativeHumidityPayload.from_bytes(raw_bytes)
    assert isinstance(payload, RelativeHumidityPayload)
    reencoded = payload.to_bytes().hex().upper()
    as_dict = payload_to_dict(payload)

    # Assert
    assert payload.humidity_percent == 50.0
    assert reencoded == raw_hex
    assert as_dict == {"humidity_percent": 50.0}


def test_opentherm_msg_payload_3220_parity() -> None:
    # Arrange
    raw_hex = "0010001900"
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = OpenThermMsgPayload.from_bytes(raw_bytes)
    reencoded = payload.hex()
    as_dict = payload_to_dict(payload)

    # Assert
    assert payload.opentherm_index == 0
    assert payload.msg_id == 0
    assert payload.msg_type == 1
    assert payload.raw_value == b"\x19\x00"
    assert reencoded == raw_hex
    assert as_dict == {
        "opentherm_index": 0,
        "msg_id": 0,
        "msg_type": 1,
        "raw_value": b"\x19\x00",
    }


def test_dhw_temp_payload_1260_parity() -> None:
    # Arrange
    raw_hex = "000837"
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = DhwTempPayload.from_bytes(raw_bytes)
    reencoded = payload.to_bytes().hex().upper()
    as_dict = payload_to_dict(payload)

    # Assert
    assert payload.dhw_index == 0
    assert payload.temperature == 21.03
    assert reencoded == raw_hex
    assert as_dict == {"dhw_index": 0, "temperature": 21.03}
    assert payload.to_dict() == {"temperature": 21.03}


def test_dhw_config_payload_12f0_parity() -> None:
    # Arrange
    raw_hex = "001388"
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = DhwConfigPayload.from_bytes(raw_bytes)
    reencoded = payload.to_bytes().hex().upper()
    as_dict = payload_to_dict(payload)

    # Assert
    assert payload.dhw_index == 0
    assert payload.setpoint_temp == 50.0
    assert reencoded == raw_hex
    assert as_dict == {"dhw_index": 0, "setpoint_temp": 50.0}


def test_dhw_state_payload_1f41_parity() -> None:
    # Arrange
    raw_hex = Code._0001
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = DhwStatePayload.from_bytes(raw_bytes)
    reencoded = payload.to_bytes().hex().upper()
    as_dict = payload_to_dict(payload)

    # Assert
    assert payload.dhw_index == 0
    assert payload.active_flag == 1
    assert reencoded == raw_hex
    assert as_dict == {
        "dhw_index": 0,
        "active_flag": 1,
        "mode_value": None,
    }

    # Verify programmatic creation with mode_val does not drop mode_val
    on_fly_payload = DhwStatePayload(dhw_index=0, active_flag=1, mode_value=2)
    assert on_fly_payload.to_bytes().hex().upper() == "000102"

    # Verify field update on unpacked payload reflects new data dynamically
    from dataclasses import replace

    updated_payload = replace(payload, active_flag=0)
    assert updated_payload.to_bytes().hex().upper() == "0000"


def test_zone_setpoint_payload_0004_parity() -> None:
    # Arrange
    raw_hex = "0107D0"
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = ZoneSetpointPayload.from_bytes(raw_bytes)
    assert not isinstance(payload, list)
    reencoded = payload.to_bytes().hex().upper()
    as_dict = payload_to_dict(payload)

    # Assert
    assert isinstance(payload, ZoneSetpointPayload)
    assert payload.zone_index == 1
    assert payload.setpoint_temp == 20.0

    assert reencoded == raw_hex
    assert as_dict == {"zone_index": 1, "setpoint_temp": 20.0}


def test_outdoor_temp_payload_12c0_parity() -> None:
    # Arrange
    raw_hex = "05DC"
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = OutdoorTempPayload.from_bytes(raw_bytes)
    reencoded = payload.to_bytes().hex().upper()
    as_dict = payload_to_dict(payload)

    # Assert
    assert payload.temperature == 15.0
    assert reencoded == raw_hex
    assert as_dict == {"temperature": 15.0}


def test_setpoint_info_payload_2309_parity() -> None:
    # Arrange
    raw_hex = "000834"
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = SetPointInfoPayload.from_bytes(raw_bytes)
    assert isinstance(payload, SetPointInfoPayload)
    reencoded = payload.to_bytes().hex().upper()
    as_dict = payload_to_dict(payload)

    # Assert
    assert payload.zone_index == 0
    assert payload.setpoint_temp == 21.0
    assert reencoded == raw_hex
    assert as_dict == {"zone_index": 0, "setpoint_temp": 21.0}


def test_flow_temp_payload_3200_parity() -> None:
    # Arrange
    raw_hex = "00131A"
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = FlowTempPayload.from_bytes(raw_bytes)
    reencoded = payload.to_bytes().hex().upper()
    as_dict = payload_to_dict(payload)

    # Assert
    assert payload.domain_index == 0
    assert payload.temperature == 48.90
    assert reencoded == raw_hex
    assert as_dict == {"domain_index": 0, "temperature": 48.90}
    assert payload.to_dict() == {"temperature": 48.90}


def test_hvac_ventilation_status_payload_22e0_parity() -> None:
    # Arrange
    raw_hex = Code._0100
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = HvacVentilationStatusPayload.from_bytes(raw_bytes)
    reencoded = payload.to_bytes().hex().upper()
    as_dict = payload_to_dict(payload)

    # Assert
    assert payload.flow_mode == 1
    assert payload.status_flags == 0
    assert reencoded == raw_hex
    assert as_dict == {"flow_mode": 1, "status_flags": 0}


def test_hvac_bypass_state_payload_31d9_parity() -> None:
    # Arrange
    raw_hex = "6400"
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = HvacBypassStatePayload.from_bytes(raw_bytes)
    reencoded = payload.to_bytes().hex().upper()
    as_dict = payload_to_dict(payload)

    # Assert
    assert payload.bypass_position == 100
    assert payload.mode_flags == 0
    assert reencoded == raw_hex
    assert as_dict == {"bypass_position": 100, "mode_flags": 0}


@pytest.mark.parametrize(
    ("mode_index", "fan_mode"),
    (
        (0, "away"),
        (1, "low"),
        (2, "medium"),
        (3, "high"),
        (4, "auto"),
        (5, "auto_alt"),
        (6, "boost"),
        (7, "off"),
        (8, "08"),
    ),
)
def test_hvac_bypass_state_payload_31d9_orcon_modes(
    mode_index: int, fan_mode: str
) -> None:
    """Decode 4-byte Orcon 31D9 mode feedback without conflating away/off."""
    payload = HvacBypassStatePayload.from_bytes(
        bytes((0x00, 0x20, mode_index, 0x00))
    )

    assert payload.to_dict()[SZ_FAN_MODE] == fan_mode


def test_hvac_air_quality_payload_3110_parity() -> None:
    # Arrange
    raw_hex = "00C8"
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = HvacAirQualityPayload.from_bytes(raw_bytes)
    reencoded = payload.to_bytes().hex().upper()
    as_dict = payload_to_dict(payload)

    # Assert
    assert payload.air_quality_aqi == 200
    assert reencoded == raw_hex
    assert as_dict == {"air_quality_aqi": 200}


def test_hvac_fault_status_payload_4e01_parity() -> None:
    # Arrange
    raw_hex = "0000"
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = HvacFaultStatusPayload.from_bytes(raw_bytes)
    reencoded = payload.to_bytes().hex().upper()
    as_dict = payload_to_dict(payload)

    # Assert
    assert payload.fault_code == 0
    assert payload.flags == 0
    assert reencoded == raw_hex
    assert as_dict == {"fault_code": 0, "flags": 0}


def test_system_clock_payload_0001_parity() -> None:
    # Arrange
    raw_hex = "000C1E0001"
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = SystemClockPayload.from_bytes(raw_bytes)
    reencoded = payload.to_bytes().hex().upper()
    as_dict = payload_to_dict(payload)

    # Assert
    assert payload.hour == 12
    assert payload.minute == 30
    assert payload.second == 0
    assert payload.day_of_week == 1
    assert reencoded == raw_hex
    assert as_dict == {
        "hour": 12,
        "minute": 30,
        "second": 0,
        "day_of_week": 1,
    }


def test_system_date_payload_0002_parity() -> None:
    # Arrange
    raw_hex = "001A0807"
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = SystemDatePayload.from_bytes(raw_bytes)
    reencoded = payload.to_bytes().hex().upper()
    as_dict = payload_to_dict(payload)

    # Assert
    assert payload.year == 26
    assert payload.month == 8
    assert payload.day == 7
    assert reencoded == raw_hex
    assert as_dict == {"year": 26, "month": 8, "day": 7}


def test_opentherm_status_payload_0150_parity() -> None:
    # Arrange
    raw_hex = Code._0100
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = OpenThermStatusPayload.from_bytes(raw_bytes)
    reencoded = payload.to_bytes().hex().upper()
    as_dict = payload_to_dict(payload)

    # Assert
    assert payload.master_status == 1
    assert payload.slave_status == 0
    assert reencoded == raw_hex
    assert as_dict == {"master_status": 1, "slave_status": 0}


def test_opentherm_setpoint_payload_1098_parity() -> None:
    # Arrange
    raw_hex = "1388"
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = OpenThermSetpointPayload.from_bytes(raw_bytes)
    reencoded = payload.to_bytes().hex().upper()
    as_dict = payload_to_dict(payload)

    # Assert
    assert payload.setpoint_temp == 50.0
    assert reencoded == raw_hex
    assert as_dict == {"setpoint_temp": 50.0}


def test_pipeline_shadow_parity_execution() -> None:
    # Arrange
    dto = PacketDTO(
        timestamp=dt.now(),
        rssi="-70",
        verb=Verb.I_,
        seq="001",
        addr1="04:123456",
        addr2="--:------",
        addr3="--:------",
        code=Code._3150,
        length="002",
        payload="00C8",
    )

    # Act
    result = decode_packet(dto)

    # Assert
    assert isinstance(result, dict)
    assert result.get("seqx_num") == "001"


def test_relay_demand_payload_0008_parity() -> None:
    # Arrange
    raw_hex = "0064"
    raw_bytes = bytes.fromhex(raw_hex)
    from ramses_rf.payloads.heating import RelayDemandPayload

    # Act
    payload = RelayDemandPayload.from_bytes(raw_bytes)
    reencoded = payload.to_bytes().hex().upper()
    as_dict = payload_to_dict(payload)

    # Assert
    assert payload.domain_or_zone_index == 0
    assert payload.demand_percent == 0.5
    assert reencoded == raw_hex
    assert as_dict == {
        "domain_or_zone_index": 0,
        "demand_percent": 0.5,
        "raw_extra": None,
    }


def test_relay_failsafe_payload_0009_parity() -> None:
    # Arrange
    raw_hex = Code._0001
    raw_bytes = bytes.fromhex(raw_hex)
    from ramses_rf.payloads.system import RelayFailsafePayload

    # Act
    payload = RelayFailsafePayload.from_bytes(raw_bytes)
    assert isinstance(payload, RelayFailsafePayload)
    reencoded = payload.to_bytes().hex().upper()
    as_dict = payload_to_dict(payload)

    # Assert
    assert payload.domain_or_zone_index == 0
    assert payload.failsafe_enabled is True
    assert reencoded == raw_hex
    assert as_dict == {"domain_or_zone_index": 0, "failsafe_enabled": True}


def test_window_state_payload_12b0_parity() -> None:
    # Arrange
    raw_hex = "000100"
    raw_bytes = bytes.fromhex(raw_hex)
    from ramses_rf.payloads.hvac import WindowStatePayload

    # Act
    payload = WindowStatePayload.from_bytes(raw_bytes)
    reencoded = payload.to_bytes().hex().upper()
    as_dict = payload_to_dict(payload)

    # Assert
    assert payload.zone_index == 0
    assert payload.window_open is True
    assert reencoded == raw_hex
    assert as_dict == {"zone_index": 0, "window_open": True}


def test_return_temp_payload_3210_parity() -> None:
    # Arrange
    raw_hex = "001388"
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = ReturnTempPayload.from_bytes(raw_bytes)
    reencoded = payload.to_bytes().hex().upper()

    # Assert
    assert payload.return_temp == 50.0
    assert reencoded == raw_hex
    assert payload_to_dict(payload) == {"return_temp": 50.0}
    assert payload.to_dict() == {SZ_TEMPERATURE: 50.0}


def test_relay_demand_payload_0008_jasper_13byte_parity() -> None:
    # Arrange
    raw_hex = "00640102030405060708090A0B"
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = RelayDemandPayload.from_bytes(raw_bytes)
    reencoded = payload.to_bytes().hex().upper()
    as_dict = payload_to_dict(payload)

    # Assert
    assert payload.domain_or_zone_index == 0
    assert payload.demand_percent == 0.5
    assert payload.raw_extra == bytes.fromhex("0102030405060708090A0B")
    assert reencoded == raw_hex
    assert as_dict["raw_extra"] == bytes.fromhex("0102030405060708090A0B")


def test_system_sync_payload_1030_mixvalve_parity() -> None:
    # Arrange
    raw_hex = "0AC80137C9010FCA0196CB0100"
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = SystemSyncPayload.from_bytes(raw_bytes)
    reencoded = payload.to_bytes().hex().upper()
    as_dict = payload_to_dict(payload)

    # Assert
    assert payload.sync_flag == 10
    assert payload.max_flow_setpoint == 55
    assert payload.min_flow_setpoint == 15
    assert payload.valve_run_time == 150
    assert payload.pump_run_time == 0
    assert reencoded == raw_hex
    assert as_dict["max_flow_setpoint"] == 55
    assert as_dict["min_flow_setpoint"] == 15
    assert as_dict["valve_run_time"] == 150
    assert as_dict["pump_run_time"] == 0


def test_spider_thermostat_payload_01ff_na_sentinel_parity() -> None:
    # Arrange
    raw_hex = "00807F8046"
    raw_bytes = bytes.fromhex(raw_hex)
    from ramses_rf.payloads.hvac import SpiderThermostatPayload

    # Act
    payload = SpiderThermostatPayload.from_bytes(raw_bytes)
    reencoded = payload.to_bytes().hex().upper()
    as_dict = payload_to_dict(payload)

    # Assert
    assert payload.temp is None
    assert payload.setpoint_min is None
    assert payload.setpoint_max == 35.0
    assert reencoded == "00807F7F46"
    assert as_dict == {
        "temp": None,
        "setpoint_min": None,
        "setpoint_max": 35.0,
    }


def test_system_fault_log_payload_0418_parity() -> None:
    # Arrange
    raw_hex = "0000010000"
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = SystemFaultLogPayload.from_bytes(raw_bytes)
    reencoded = payload.to_bytes().hex().upper()
    as_dict = payload_to_dict(payload)

    # Assert
    assert payload.log_index == 0
    assert payload.log_data == bytes.fromhex("00010000")
    assert reencoded == raw_hex
    assert as_dict == {"log_index": 0, "log_data": b"\x00\x01\x00\x00"}


def test_system_config_payload_2e04_parity() -> None:
    # Arrange
    raw_hex = "0000"
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = SystemConfigPayload.from_bytes(raw_bytes)
    reencoded = payload.to_bytes().hex().upper()
    as_dict = payload_to_dict(payload)

    # Assert
    assert payload.config_index == 0
    assert payload.config_value == 0
    assert reencoded == raw_hex
    assert as_dict == {"config_index": 0, "config_value": 0, "raw_extra": None}


def test_zone_config_payload_000a_array_parity() -> None:
    # Arrange
    raw_hex = "081001F409C4091001F409C40A1001F409C40B1001F409C4"
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payloads = ZoneConfigPayload.from_bytes(raw_bytes)
    assert isinstance(payloads, list)
    reencoded = b"".join(p.to_bytes() for p in payloads).hex().upper()
    as_dicts = [payload_to_dict(p) for p in payloads]

    # Assert
    assert len(payloads) == 4
    assert reencoded == raw_hex
    assert as_dicts[0] == {
        "zone_index": 8,
        "zone_flags": 16,
        "min_temp": 5.0,
        "max_temp": 25.0,
    }


def test_hvac_fan_param_payload_2411_22byte_parity() -> None:
    # Arrange
    raw_hex = "00000100000000003200000000000000FF0000000120"
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = HvacFanParamPayload.from_bytes(raw_bytes)
    as_dict = payload_to_dict(payload)

    # Assert
    assert payload.parameter_id == 1
    assert payload.value_scaled == 50
    assert as_dict["parameter_id"] == 1
    assert as_dict["value_scaled"] == 50


def test_schedule_fragment_payload_0404_parity() -> None:
    # Arrange
    raw_hex = (
        "0120000829010368816DCCC91183301005D1D93428200E1C7D720C04402C0442640E8200"
        "0C851701ADD3AFAED1131151"
    )
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = ScheduleSwitchpointPayload.from_bytes(raw_bytes)
    assert isinstance(payload, ScheduleFragmentPayload)
    reencoded = payload.to_bytes().hex().upper()
    as_dict = payload_to_dict(payload)

    # Assert
    assert payload.zone_index == 1
    assert payload.frag_number == 1
    assert payload.total_frags == 3
    assert reencoded == raw_hex
    assert as_dict == {
        "zone_index": 1,
        "frag_number": 1,
        "total_frags": 3,
        "fragment_bytes": bytes.fromhex(
            "68816DCCC91183301005D1D93428200E1C7D720C04402C0442640E82000C851701ADD3AFAED1131151"
        ),
    }


def test_hvac_filter_change_payload_10d0_reset_parity() -> None:
    # Arrange
    raw_hex = "00FF"
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = HvacFilterChangePayload.from_bytes(raw_bytes)
    reencoded = payload.to_bytes().hex().upper()
    as_dict = payload_to_dict(payload)

    # Assert
    assert payload.reset_counter is True
    assert reencoded == raw_hex
    assert as_dict == {
        "remaining_days": None,
        "days_lifetime": None,
        "remaining_percent": None,
        "reset_counter": True,
    }


def test_zone_mode_payload_2349_parity() -> None:
    """Verify ZoneModePayload packs, unpacks, and serializes Opcode 2349 binary data."""
    # Arrange
    raw_hex = "00083400FFFFFF"
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = ZoneModePayload.from_bytes(raw_bytes)
    reencoded = payload.to_bytes().hex().upper()
    as_dict = payload_to_dict(payload)

    # Assert
    assert payload.zone_index == 0
    assert payload.setpoint_temp == 21.0
    assert payload.mode_code == 0
    assert payload.duration_minutes is None
    assert reencoded == raw_hex
    assert as_dict == {
        "zone_index": 0,
        "setpoint_temp": 21.0,
        "mode_code": 0,
        "duration_minutes": None,
        "until_dtm": None,
    }


def test_zone_name_payload_parity() -> None:
    """Verify ZoneNamePayload packs, unpacks, and serializes Opcode 0004 name binary data."""
    # Arrange
    raw_hex = "00004C6F756E67650000000000000000000000000000"
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = ZoneNamePayload.from_bytes(raw_bytes)
    reencoded = payload.to_bytes().hex().upper()
    as_dict = payload_to_dict(payload)

    # Assert
    assert payload.zone_index == 0
    assert payload.name == "Lounge"
    assert reencoded == raw_hex
    assert as_dict == {
        "zone_index": 0,
        "name": "Lounge",
    }


def test_hvac_time_offset_payload_313e_parity() -> None:

    # Arrange
    raw_hex = "000000003C00003C800000"
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = HvacTimeOffsetPayload.from_bytes(raw_bytes)
    reencoded = payload.to_bytes().hex().upper()
    as_dict = payload_to_dict(payload)

    # Assert
    assert payload.offset_mins == 60
    assert payload.offset_secs == 0
    assert reencoded == raw_hex
    assert as_dict == {
        "offset_mins": 60,
        "offset_secs": 0,
    }
    assert payload.to_dict() == {
        "value_02": "0000003C",
        "value_10": "00",
        "value_12": "003C800000",
    }


def test_actuator_state_payload_3ef0_3byte_parity() -> None:
    # Arrange
    raw_hex = "0064FF"
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = ActuatorStatePayload.from_bytes(raw_bytes)
    reencoded = payload.to_bytes().hex().upper()
    legacy_dict = payload.to_dict()

    # Assert
    assert payload.domain_id == 0
    assert payload.modulation_level == 0.5
    assert payload.flags_2 == 255
    assert payload.flags_3 is None
    assert reencoded == raw_hex
    assert legacy_dict == {"modulation_level": 0.5}


def test_actuator_state_payload_3ef0_4byte_parity() -> None:
    # Arrange
    raw_hex = "0064FF10"
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = ActuatorStatePayload.from_bytes(raw_bytes)
    reencoded = payload.to_bytes().hex().upper()
    legacy_dict = payload.to_dict()

    # Assert
    assert payload.domain_id == 0
    assert payload.modulation_level == 0.5
    assert payload.flags_2 == 255
    assert payload.flags_3 == 0x10
    assert payload.flags_6 is None
    assert reencoded == raw_hex
    assert legacy_dict == {
        "modulation_level": 0.5,
        "ch_active": False,
        "dhw_active": False,
        "flame_active": False,
        "cooling_active": True,
    }


def test_actuator_state_payload_3ef0_9byte_parity() -> None:
    # Arrange
    raw_hex = "0064FF1000FF0114C8"
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = ActuatorStatePayload.from_bytes(raw_bytes)
    reencoded = payload.to_bytes().hex().upper()
    legacy_dict = payload.to_dict()

    # Assert
    assert payload.domain_id == 0
    assert payload.modulation_level == 0.5
    assert payload.flags_2 == 255
    assert payload.flags_3 == 0x10
    assert payload.unknown_4 == 0
    assert payload.unknown_5 == 255
    assert payload.flags_6 == 1
    assert payload.ch_setpoint == 20
    assert payload.max_rel_modulation == 1.0
    assert reencoded == raw_hex
    assert legacy_dict == {
        "modulation_level": 0.5,
        "ch_active": False,
        "dhw_active": False,
        "flame_active": False,
        "cooling_active": True,
        "ch_enabled": True,
        "ch_setpoint": 20,
        "max_rel_modulation": 1.0,
    }


def test_complete_payload_registry_coverage() -> None:

    # Arrange & Act
    known_codes = [
        getattr(tx_const.Code, a)
        for a in dir(tx_const.Code)
        if a.startswith("_") and len(a) == 5
    ]

    aliases = [Code._2E10]
    all_expected = known_codes + aliases

    # Assert
    for code in all_expected:
        assert code in PAYLOAD_REGISTRY, (
            f"Opcode {code} missing from PAYLOAD_REGISTRY"
        )
    assert len(PAYLOAD_REGISTRY._registry) == 108


def test_pipeline_3150_non_array_preserves_index() -> None:
    # Arrange
    dto = PacketDTO(
        timestamp=dt.now(),
        rssi="-70",
        verb=Verb.I_,
        seq="001",
        addr1="04:123456",
        addr2="--:------",
        addr3="01:555555",
        code=Code._3150,
        length="002",
        payload="00C8",
    )

    # Act
    result = decode_packet(dto)

    # Assert
    assert isinstance(result, dict)
    assert result.get("heat_demand") == 1.0
    assert result.get("zone_index") == "00"


def test_pipeline_3150_ufc_emits_canonical_ufh_index() -> None:
    # Arrange
    dto = PacketDTO(
        timestamp=dt.now(),
        rssi="-70",
        verb=Verb.I_,
        seq="001",
        addr1="02:000921",
        addr2="--:------",
        addr3="02:000921",
        code=Code._3150,
        length="002",
        payload="0360",
    )

    # Act
    result = decode_packet(dto)

    # Assert
    assert isinstance(result, dict)
    assert result.get("ufh_index") == "03"
    assert "ufx_index" not in result
    assert result.get("heat_demand") == pytest.approx(0.48, abs=0.01)


def test_opentherm_msg_payload_replace_recalculates_parity() -> None:
    # Arrange
    raw_hex = "00C01307C0"
    raw_bytes = bytes.fromhex(raw_hex)
    payload = OpenThermMsgPayload.from_bytes(raw_bytes)

    # Act
    modified_payload = replace(payload, msg_id=0x19)
    reencoded = modified_payload.to_bytes().hex().upper()
    modified_dict = payload_to_dict(modified_payload)

    # Assert
    assert modified_payload.msg_id == 0x19
    assert reencoded != raw_hex
    assert reencoded == "00C01907C0"
    assert modified_dict.get("msg_id") == 25


def test_system_datetime_payload_replace_recalculates_bytes() -> None:
    # Arrange
    raw_hex = "00F036020A020507E6"
    raw_bytes = bytes.fromhex(raw_hex)
    payload = SystemDateTimePayload.from_bytes(raw_bytes)

    # Act
    modified_payload = replace(payload, datetime_str="2023-06-15T12:00:00")
    reencoded = modified_payload.to_bytes().hex().upper()
    modified_dict = payload_to_dict(modified_payload)

    # Assert
    assert modified_payload.datetime_str == "2023-06-15T12:00:00"
    assert reencoded != raw_hex
    assert modified_dict.get("datetime_str") == "2023-06-15T12:00:00"


def test_hvac_payload_roundtrip_codecs_parity() -> None:
    # Arrange & Act 1: OutdoorHumidityPayload (1280)
    raw_1280 = bytes.fromhex("0064")
    p_1280 = ramses_rf.payloads.hvac.OutdoorHumidityPayload.from_bytes(
        raw_1280
    )
    assert p_1280.humidity_percent == 50.0
    assert p_1280.to_bytes() == raw_1280

    # Arrange & Act 2: AirQualityBasisPayload (12C8)
    raw_12c8 = bytes.fromhex("6400")
    p_12c8 = ramses_rf.payloads.hvac.AirQualityBasisPayload.from_bytes(
        raw_12c8
    )
    assert p_12c8.air_quality_percent == 0.5
    assert p_12c8.to_bytes() == raw_12c8

    # Arrange & Act 3: HvacProgrammeEnabledPayload (22B0)
    raw_22b0 = bytes.fromhex(Code._0005)
    p_22b0 = ramses_rf.payloads.hvac.HvacProgrammeEnabledPayload.from_bytes(
        raw_22b0
    )
    assert p_22b0.enabled is True
    assert p_22b0.to_bytes() == raw_22b0

    # Arrange & Act 4: HvacFanModePayload (22F1)
    raw_22f1 = bytes.fromhex("000204")
    p_22f1 = ramses_rf.payloads.hvac.HvacFanModePayload.from_bytes(raw_22f1)
    assert p_22f1.mode_index == 2
    assert p_22f1.to_bytes() == raw_22f1

    # Arrange & Act 5: HvacFlowRatePayload (22F2)
    raw_22f2 = bytes.fromhex("000064")
    p_22f2 = ramses_rf.payloads.hvac.HvacFlowRatePayload.from_bytes(raw_22f2)
    assert p_22f2.measures == ((0, 1.0),)
    assert p_22f2.to_bytes() == raw_22f2


def test_system_payload_roundtrip_codecs_parity() -> None:
    # Arrange & Act 1: OemCodePayload (000E)
    raw_000e = bytes.fromhex(Code._0001)
    p_000e = ramses_rf.payloads.system.OemCodePayload.from_bytes(raw_000e)
    assert p_000e.payload_hex == Code._0001
    assert p_000e.to_bytes() == raw_000e

    # Arrange & Act 2: DeviceBatteryPayload (1060)
    raw_1060 = bytes.fromhex("00C801")
    p_1060 = ramses_rf.payloads.system.DeviceBatteryPayload.from_bytes(
        raw_1060
    )
    assert p_1060.battery_level == 1.0
    assert p_1060.battery_low is False
    assert p_1060.to_bytes() == raw_1060

    # Arrange & Act 3: SystemSyncHeartbeat1BPayload / 3BPayload (1F09)
    raw_1f09_1b = bytes.fromhex("00")
    p_1f09_1b = (
        ramses_rf.payloads.system.SystemSyncHeartbeatPayload.from_bytes(
            raw_1f09_1b
        )
    )
    assert isinstance(
        p_1f09_1b, ramses_rf.payloads.system.SystemSyncHeartbeat1BPayload
    )
    assert p_1f09_1b.sync_sequence == 0
    assert p_1f09_1b.to_bytes() == raw_1f09_1b

    raw_1f09_3b = bytes.fromhex("000514")
    p_1f09_3b = (
        ramses_rf.payloads.system.SystemSyncHeartbeatPayload.from_bytes(
            raw_1f09_3b
        )
    )
    assert isinstance(
        p_1f09_3b, ramses_rf.payloads.system.SystemSyncHeartbeat3BPayload
    )
    assert p_1f09_3b.remaining_seconds == 130.0
    assert p_1f09_3b.to_bytes() == raw_1f09_3b

    # Arrange & Act 4: SystemFrame0204Payload (0204)
    raw_0204 = bytes.fromhex("00010203")
    p_0204 = ramses_rf.payloads.system.SystemFrame0204Payload.from_bytes(
        raw_0204
    )
    assert p_0204.raw_payload_bytes == raw_0204
    assert p_0204.to_bytes() == raw_0204


def test_payload_invalid_byte_length_exception_handling() -> None:
    # Assert ValueError raised on truncated bytes for multi-byte payloads
    with pytest.raises(ValueError, match="Invalid payload length"):
        ramses_rf.payloads.dhw.DhwConfigPayload.from_bytes(b"\x00")

    with pytest.raises(ValueError, match="Invalid payload length"):
        ramses_rf.payloads.heating.Temperature3BPayload.from_bytes(b"\x00\x01")

    with pytest.raises(ValueError, match="Invalid payload length"):
        ramses_rf.payloads.hvac.OutdoorHumidityPayload.from_bytes(b"\x00")

    with pytest.raises(ValueError, match="Invalid payload length"):
        ramses_rf.payloads.system.DeviceBatteryPayload.from_bytes(b"\x00")


def test_polymorphic_dispatchers_parity_comprehensive() -> None:
    """Verify polymorphic dispatcher decoding, serialization, and dictionary parity across all variants."""
    # 1. ZoneName (0004): 22B and Short 3B
    p_0004_22b = ZoneNamePayload.from_bytes(
        bytes.fromhex("00004C6976696E6720526F6F6D000000000000000000")
    )
    assert isinstance(
        p_0004_22b, ramses_rf.payloads.heating.ZoneName22BPayload
    )
    assert p_0004_22b.name == "Living Room"
    assert p_0004_22b.zone_index == 0
    assert payload_to_dict(p_0004_22b) == {
        "zone_index": 0,
        "name": "Living Room",
    }

    p_0004_3b = ZoneNamePayload.from_bytes(bytes.fromhex("0007D0"))
    assert isinstance(
        p_0004_3b, ramses_rf.payloads.heating.ZoneNameShort3BPayload
    )
    assert p_0004_3b.zone_index == 0
    assert p_0004_3b.setpoint_temp == 20.0
    assert payload_to_dict(p_0004_3b) == {
        "zone_index": 0,
        "setpoint_temp": 20.0,
    }

    # 2. RelayDemand (0008): 2B and Jasper 13B
    p_0008_2b = RelayDemandPayload.from_bytes(bytes.fromhex("00C8"))
    assert isinstance(
        p_0008_2b, ramses_rf.payloads.heating.RelayDemand2BPayload
    )
    assert p_0008_2b.demand_percent == 1.0
    assert payload_to_dict(p_0008_2b) == {
        "domain_or_zone_index": 0,
        "demand_percent": 1.0,
        "raw_extra": None,
    }

    p_0008_13b = RelayDemandPayload.from_bytes(
        bytes.fromhex("00000000000000000000000000")
    )
    assert isinstance(
        p_0008_13b, ramses_rf.payloads.heating.RelayDemand2BPayload
    )
    assert p_0008_13b.to_bytes().hex().upper() == "00000000000000000000000000"

    # 3. SystemLanguage (0100): 2B and 3B
    p_0100_2b = ramses_rf.payloads.system.SystemLanguagePayload.from_bytes(
        bytes.fromhex("0000")
    )
    assert isinstance(
        p_0100_2b, ramses_rf.payloads.system.SystemLanguage2BPayload
    )
    assert p_0100_2b.language == "00"
    assert payload_to_dict(p_0100_2b) == {"language": "00"}

    p_0100_3b = ramses_rf.payloads.system.SystemLanguagePayload.from_bytes(
        bytes.fromhex("00454E")
    )
    assert isinstance(
        p_0100_3b, ramses_rf.payloads.system.SystemLanguage3BPayload
    )
    assert p_0100_3b.language == "EN"
    assert payload_to_dict(p_0100_3b) == {"language": "EN"}

    # 4. TpiParams (1100): 4B and 8B
    p_1100_4b = ramses_rf.payloads.heating.TpiParamsPayload.from_bytes(
        bytes.fromhex("FC180404")
    )
    assert isinstance(p_1100_4b, ramses_rf.payloads.heating.TpiParams4BPayload)
    assert p_1100_4b.cycle_rate == 6
    assert p_1100_4b.min_on_time == 1.0
    assert p_1100_4b.min_off_time == 1.0

    p_1100_8b = ramses_rf.payloads.heating.TpiParamsPayload.from_bytes(
        bytes.fromhex("FC180404007FFF00")
    )
    assert isinstance(p_1100_8b, ramses_rf.payloads.heating.TpiParams8BPayload)
    assert p_1100_8b.cycle_rate == 6
    assert p_1100_8b.proportional_band_width is None

    # 5. RelativeHumidity (12A0): 1B, 2B, and 6B
    p_12a0_1b = RelativeHumidityPayload.from_bytes(bytes.fromhex("64"))
    assert isinstance(
        p_12a0_1b, ramses_rf.payloads.hvac.RelativeHumidity1BPayload
    )
    assert p_12a0_1b.humidity_percent == 50.0
    assert payload_to_dict(p_12a0_1b) == {"humidity_percent": 50.0}

    p_12a0_2b = RelativeHumidityPayload.from_bytes(bytes.fromhex("0064"))
    assert isinstance(
        p_12a0_2b, ramses_rf.payloads.hvac.RelativeHumidity2BPayload
    )
    assert p_12a0_2b.humidity_percent == 1.0
    assert payload_to_dict(p_12a0_2b) == {"humidity_percent": 1.0}

    p_12a0_6b = RelativeHumidityPayload.from_bytes(
        bytes.fromhex("006407D00834")
    )
    assert isinstance(
        p_12a0_6b, ramses_rf.payloads.hvac.RelativeHumidity6BPayload
    )
    assert p_12a0_6b.humidity_percent == 1.0
    assert p_12a0_6b.temperature == 20.0
    assert p_12a0_6b.dewpoint_temp == 21.0
    assert payload_to_dict(p_12a0_6b) == {"humidity_percent": 1.0}

    # 6. HvacVentilationStatus (22E0, 22E5, 22E9): 2B and 4B
    p_22e0_2b = HvacVentilationStatusPayload.from_bytes(
        bytes.fromhex(Code._0100)
    )
    assert isinstance(
        p_22e0_2b, ramses_rf.payloads.hvac.HvacVentilationStatus2BPayload
    )
    assert p_22e0_2b.flow_mode == 1
    assert p_22e0_2b.status_flags == 0
    assert payload_to_dict(p_22e0_2b) == {"flow_mode": 1, "status_flags": 0}

    p_22e0_4b = HvacVentilationStatusPayload.from_bytes(
        bytes.fromhex("0034A01E")
    )
    assert isinstance(
        p_22e0_4b, ramses_rf.payloads.hvac.HvacVentilationStatus4BPayload
    )
    assert p_22e0_4b.flow_mode == 0
    assert p_22e0_4b.status_flags == 0x34
    assert payload_to_dict(p_22e0_4b)["flow_mode"] == 0

    # 7. ZoneSetpoint (2309): 3B
    p_2309 = ZoneSetpointPayload.from_bytes(bytes.fromhex("0007D0"))
    assert isinstance(p_2309, ramses_rf.payloads.heating.ZoneSetpoint3BPayload)
    assert p_2309.zone_index == 0
    assert p_2309.setpoint_temp == 20.0
    assert payload_to_dict(p_2309) == {"zone_index": 0, "setpoint_temp": 20.0}

    # 8. ZoneMode (2349): 4B, 7B, and 13B
    p_2349_4b = ZoneModePayload.from_bytes(bytes.fromhex("0007D000"))
    assert isinstance(p_2349_4b, ramses_rf.payloads.heating.ZoneMode4BPayload)
    assert p_2349_4b.zone_index == 0
    assert p_2349_4b.setpoint_temp == 20.0
    assert p_2349_4b.mode_code == 0
    assert payload_to_dict(p_2349_4b) == {
        "zone_index": 0,
        "setpoint_temp": 20.0,
        "mode_code": 0,
        "duration_minutes": None,
        "until_dtm": None,
    }

    p_2349_7b = ZoneModePayload.from_bytes(bytes.fromhex("0007D00200003C"))
    assert isinstance(p_2349_7b, ramses_rf.payloads.heating.ZoneMode7BPayload)
    assert p_2349_7b.duration_minutes == 60
    assert payload_to_dict(p_2349_7b).get("duration_minutes") == 60

    p_2349_13b = ZoneModePayload.from_bytes(
        bytes.fromhex("0007D001FFFFFF00110E0507E5")
    )
    assert isinstance(
        p_2349_13b, ramses_rf.payloads.heating.ZoneMode13BPayload
    )
    assert p_2349_13b.zone_index == 0
    assert p_2349_13b.setpoint_temp == 20.0
    assert p_2349_13b.until_dtm == "2021-05-14T17:00:00"

    # 9. SystemDateTime (313F): 2B and 9B
    p_313f_2b = SystemDateTimePayload.from_bytes(bytes.fromhex("0000"))
    assert isinstance(
        p_313f_2b, ramses_rf.payloads.system.SystemDateTime2BPayload
    )
    assert p_313f_2b.domain_index == 0
    assert payload_to_dict(p_313f_2b) == {"domain_index": 0}

    p_313f_9b = SystemDateTimePayload.from_bytes(
        bytes.fromhex("00F0BB00040C0507EA")
    )
    assert isinstance(
        p_313f_9b, ramses_rf.payloads.system.SystemDateTime9BPayload
    )
    assert p_313f_9b.datetime_str == "2026-05-12T04:00:59"
    assert p_313f_9b.is_daylight_saving is True

    # 10. HeatDemand (3150): 1B and 2B
    p_3150_1b = HeatDemandPayload.from_bytes(bytes.fromhex("C8"))
    assert isinstance(
        p_3150_1b, ramses_rf.payloads.heating.HeatDemand1BPayload
    )
    assert p_3150_1b.demand_percent == 200

    p_3150_2b = HeatDemandPayload.from_bytes(bytes.fromhex("01CA"))
    assert isinstance(
        p_3150_2b, ramses_rf.payloads.heating.HeatDemand2BPayload
    )
    assert p_3150_2b.domain_or_zone_index == 1
    assert p_3150_2b.demand_percent == 202


def test_dataclass_buffer_underrun_guards() -> None:
    """Verify that concrete and polymorphic payload classes guard against truncated bytes."""
    # Arrange & Act & Assert
    with pytest.raises(ValueError):
        ZoneSetpointPayload.from_bytes(b"\x00\x01")  # Expected 3 bytes

    with pytest.raises(ValueError):
        DhwParamsPayload.from_bytes(b"\x00")  # Expected at least 3 bytes

    with pytest.raises(ValueError):
        ZoneModePayload.from_bytes(b"\x00\x01")  # Expected at least 4 bytes

    with pytest.raises(ValueError):
        SystemDateTimePayload.from_bytes(b"\x00")  # Expected at least 2 bytes

    with pytest.raises(ValueError):
        HeatDemandPayload.from_bytes(b"")  # Expected at least 1 byte


# ---------------------------------------------------------------------------
# create() factory method tests (PR4: polymorphic __new__ → create())
# ---------------------------------------------------------------------------


def test_heat_demand_payload_create_1b() -> None:
    """HeatDemandPayload.create() with no zone_index → 1B variant."""
    payload = HeatDemandPayload.create(demand_percent=200)
    assert isinstance(payload, ramses_rf.payloads.heating.HeatDemand1BPayload)
    assert payload.demand_percent == 200
    assert payload.to_bytes().hex().upper() == "C8"


def test_heat_demand_payload_create_2b() -> None:
    """HeatDemandPayload.create() with zone_index → 2B variant."""
    payload = HeatDemandPayload.create(
        domain_or_zone_index=1, demand_percent=202
    )
    assert isinstance(payload, ramses_rf.payloads.heating.HeatDemand2BPayload)
    assert payload.domain_or_zone_index == 1
    assert payload.demand_percent == 202
    assert payload.to_bytes().hex().upper() == "01CA"


def test_temperature_payload_create_2b() -> None:
    """TemperaturePayload.create() with no zone_index → 2B variant."""
    payload = TemperaturePayload.create(temperature=21.5)
    assert isinstance(payload, ramses_rf.payloads.heating.Temperature2BPayload)
    assert payload.temperature == 21.5
    assert payload.to_bytes().hex().upper() == "0866"


def test_temperature_payload_create_3b() -> None:
    """TemperaturePayload.create() with zone_index → 3B variant."""
    payload = TemperaturePayload.create(zone_index=3, temperature=21.5)
    assert isinstance(payload, ramses_rf.payloads.heating.Temperature3BPayload)
    assert payload.zone_index == 3
    assert payload.temperature == 21.5
    assert payload.to_bytes().hex().upper() == "030866"


def test_zone_name_payload_create_22b() -> None:
    """ZoneNamePayload.create() with name → 22B variant."""
    payload = ZoneNamePayload.create(zone_index=3, name="Living Room")
    assert isinstance(payload, ramses_rf.payloads.heating.ZoneName22BPayload)
    assert payload.zone_index == 3
    assert payload.name == "Living Room"
    raw = payload.to_bytes()
    assert len(raw) == 22


def test_zone_name_payload_create_short_3b() -> None:
    """ZoneNamePayload.create() with setpoint_temp → 3B short variant."""
    payload = ZoneNamePayload.create(zone_index=3, setpoint_temp=21.0)
    assert isinstance(
        payload, ramses_rf.payloads.heating.ZoneNameShort3BPayload
    )
    assert payload.zone_index == 3
    assert payload.setpoint_temp == 21.0


def test_zone_setpoint_payload_create() -> None:
    """ZoneSetpointPayload.create() → 3B variant."""
    payload = ZoneSetpointPayload.create(zone_index=3, setpoint_temp=21.0)
    assert isinstance(
        payload, ramses_rf.payloads.heating.ZoneSetpoint3BPayload
    )
    assert payload.zone_index == 3
    assert payload.setpoint_temp == 21.0
    assert payload.to_bytes().hex().upper() == "030834"


def test_system_zones_payload_create() -> None:
    """SystemZonesPayload.create() → 4B variant."""
    from ramses_rf.payloads.heating import SystemZonesPayload

    payload = SystemZonesPayload.create(
        zone_type=0x0A, zone_mask=0x0001, zone_class_id=0x0A
    )
    assert isinstance(payload, ramses_rf.payloads.heating.SystemZones4BPayload)
    assert payload.zone_type == 0x0A
    assert payload.zone_mask == 0x0001
    assert payload.to_bytes().hex().upper() == "000A0100"


def test_relay_demand_payload_create() -> None:
    """RelayDemandPayload.create() → 2B variant."""
    payload = RelayDemandPayload.create(
        domain_or_zone_index=0xFC, demand_percent=1.0
    )
    assert isinstance(payload, ramses_rf.payloads.heating.RelayDemand2BPayload)
    assert payload.domain_or_zone_index == 0xFC
    assert payload.demand_percent == 1.0
    assert payload.to_bytes().hex().upper() == "FCC8"


def test_zone_devices_payload_create_5b() -> None:
    """ZoneDevicesPayload.create() without sub_index → 5B variant."""
    from ramses_rf.payloads.heating import ZoneDevicesPayload

    payload = ZoneDevicesPayload.create(
        zone_index_raw=0, device_role_id=0, device_id_raw=0x123456
    )
    assert isinstance(payload, ramses_rf.payloads.heating.ZoneDevices5BPayload)
    assert payload.zone_index_raw == 0
    assert payload.device_id_raw == 0x123456


def test_zone_devices_payload_create_6b() -> None:
    """ZoneDevicesPayload.create() with sub_index → 6B variant."""
    from ramses_rf.payloads.heating import ZoneDevicesPayload

    payload = ZoneDevicesPayload.create(
        zone_index_raw=0, device_role_id=0, sub_index=1, device_id_raw=0x123456
    )
    assert isinstance(payload, ramses_rf.payloads.heating.ZoneDevices6BPayload)
    assert payload.sub_index == 1


def test_zone_mode_payload_create_7b() -> None:
    """ZoneModePayload.create() without until_dtm → 7B variant."""
    payload = ZoneModePayload.create(
        zone_index=3, setpoint_temp=21.0, mode_code=4, duration_minutes=60
    )
    assert isinstance(payload, ramses_rf.payloads.heating.ZoneMode7BPayload)
    assert payload.zone_index == 3
    assert payload.setpoint_temp == 21.0
    assert payload.mode_code == 4


def test_zone_mode_payload_create_13b() -> None:
    """ZoneModePayload.create() with until_dtm → 13B variant."""
    payload = ZoneModePayload.create(
        zone_index=3,
        setpoint_temp=21.0,
        mode_code=4,
        until_dtm="2026-01-15-12-00",
    )
    assert isinstance(payload, ramses_rf.payloads.heating.ZoneMode13BPayload)
    assert payload.zone_index == 3


def test_actuator_cycle_payload_create_6b() -> None:
    """ActuatorCyclePayload.create() without domain_index → 6B variant."""
    from ramses_rf.payloads.heating import ActuatorCyclePayload

    payload = ActuatorCyclePayload.create(
        cycle_countdown_sec=0,
        actuator_countdown_sec=0,
        modulation_level=0.5,
    )
    assert isinstance(
        payload, ramses_rf.payloads.heating.ActuatorCycle6BPayload
    )
    assert payload.modulation_level == 0.5


def test_actuator_cycle_payload_create_7b() -> None:
    """ActuatorCyclePayload.create() with domain_index → 7B variant."""
    from ramses_rf.payloads.heating import ActuatorCyclePayload

    payload = ActuatorCyclePayload.create(
        cycle_countdown_sec=0,
        actuator_countdown_sec=0,
        modulation_level=0.5,
        domain_index=1,
    )
    assert isinstance(
        payload, ramses_rf.payloads.heating.ActuatorCycle7BPayload
    )
    assert payload.domain_index == 1


def test_co2_payload_create_2b() -> None:
    """Co2Payload.create() without domain_index → 2B variant."""
    payload = Co2Payload.create(co2_level=450)
    assert isinstance(payload, Co22BPayload)
    assert payload.co2_level == 450


def test_co2_payload_create_3b() -> None:
    """Co2Payload.create() with domain_index → 3B variant."""
    payload = Co2Payload.create(co2_level=450, domain_index=0)
    assert isinstance(payload, Co23BPayload)
    assert payload.domain_index == 0
    assert payload.co2_level == 450


def test_window_state_payload_create() -> None:
    """WindowStatePayload.create() → 3B variant."""
    payload = WindowStatePayload.create(zone_index=3, window_open=False)
    assert isinstance(payload, ramses_rf.payloads.hvac.WindowState3BPayload)
    assert payload.zone_index == 3
    assert payload.window_open is False


def test_hvac_ventilation_demand_payload_create() -> None:
    """HvacVentilationDemandPayload.create() → 4B variant."""
    from ramses_rf.payloads.hvac import HvacVentilationDemandPayload

    payload = HvacVentilationDemandPayload.create(flags=0, demand_percent=0.5)
    assert isinstance(
        payload, ramses_rf.payloads.hvac.HvacVentilationDemand4BPayload
    )
    assert payload.demand_percent == 0.5


def test_opentherm_msg_payload_create_4b() -> None:
    """OpenThermMsgPayload.create() without opentherm_index → 4B variant."""
    payload = OpenThermMsgPayload.create(
        msg_id=0x18, msg_type=0, raw_value=b"\x00\x00"
    )
    assert isinstance(
        payload, ramses_rf.payloads.opentherm.OpenThermMsg4BPayload
    )
    assert payload.msg_id == 0x18


def test_opentherm_msg_payload_create_5b() -> None:
    """OpenThermMsgPayload.create() with opentherm_index → 5B variant."""
    payload = OpenThermMsgPayload.create(
        msg_id=0x18, msg_type=0, raw_value=b"\x00\x00", opentherm_index=1
    )
    assert isinstance(
        payload, ramses_rf.payloads.opentherm.OpenThermMsg5BPayload
    )
    assert payload.opentherm_index == 1


def test_opentherm_fault_flags_payload_create_2b() -> None:
    """OpenThermFaultFlagsPayload.create() without hdr → 2B variant."""
    payload = OpenThermFaultFlagsPayload.create(fault_code=0, flags=0)
    assert isinstance(payload, OpenThermFaultFlags2BPayload)


def test_opentherm_fault_flags_payload_create_3b() -> None:
    """OpenThermFaultFlagsPayload.create() with hdr → 3B variant."""
    payload = OpenThermFaultFlagsPayload.create(fault_code=0, flags=0, hdr=1)
    assert isinstance(payload, OpenThermFaultFlags3BPayload)
    assert payload.hdr == 1


def test_dhw_params_payload_create() -> None:
    """DhwParamsPayload.create() → 6B variant (with overrun + differential)."""
    payload = DhwParamsPayload.create(
        setpoint=55.0, overrun=8, differential=2.0
    )
    assert isinstance(payload, ramses_rf.payloads.dhw.DhwParams6BPayload)
    assert payload.setpoint == 55.0


def test_opentherm_diagnostics_payload_1fd0_parity() -> None:
    # Arrange
    raw_hex = "7300"
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = OpenThermDiagnosticsPayload.from_bytes(raw_bytes)
    reencoded = payload.to_bytes().hex().upper()

    # Assert
    assert payload.diagnostic_code == 115
    assert payload.flags == 0
    assert reencoded == raw_hex
    assert payload_to_dict(payload) == {"diagnostic_code": 115, "flags": 0}
    assert payload.to_dict() == {SZ_DIAGNOSTIC_CODE: 115, SZ_FLAGS: 0}


def test_opentherm_diagnostics_payload_1fd0_length_guard() -> None:
    # Arrange
    raw_bytes = b"\x73"

    # Act & Assert
    with pytest.raises(ValueError, match="Invalid payload length"):
        OpenThermDiagnosticsPayload.from_bytes(raw_bytes)


def test_opentherm_fault_flags_payload_1fd4_2b_parity() -> None:
    # Arrange
    raw_hex = "5AFE"
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = OpenThermFaultFlagsPayload.from_bytes(raw_bytes)
    reencoded = payload.to_bytes().hex().upper()

    # Assert
    assert isinstance(payload, OpenThermFaultFlags2BPayload)
    assert payload.fault_code == 90
    assert payload.flags == 254
    assert reencoded == raw_hex
    assert payload_to_dict(payload) == {"fault_code": 90, "flags": 254}
    assert payload.to_dict() == {SZ_TICKER: (90 << 8) | 254}


def test_opentherm_fault_flags_payload_1fd4_3b_parity() -> None:
    # Arrange
    raw_hex = "005AFE"
    raw_bytes = bytes.fromhex(raw_hex)

    # Act
    payload = OpenThermFaultFlagsPayload.from_bytes(raw_bytes)
    reencoded = payload.to_bytes().hex().upper()

    # Assert
    assert isinstance(payload, OpenThermFaultFlags3BPayload)
    assert payload.hdr == 0
    assert payload.fault_code == 90
    assert payload.flags == 254
    assert reencoded == raw_hex
    assert payload_to_dict(payload) == {
        "hdr": 0,
        "fault_code": 90,
        "flags": 254,
    }
    assert payload.to_dict() == {SZ_TICKER: (90 << 8) | 254}


def test_opentherm_fault_flags_payload_1fd4_length_guard() -> None:
    # Arrange & Act & Assert
    with pytest.raises(ValueError, match="Invalid payload length"):
        OpenThermFaultFlagsPayload.from_bytes(b"\x5a")
    with pytest.raises(ValueError, match="Invalid payload length"):
        OpenThermFaultFlags2BPayload.from_bytes(b"\x5a")
    with pytest.raises(ValueError, match="Invalid payload length"):
        OpenThermFaultFlags3BPayload.from_bytes(b"\x00\x5a")


def test_opentherm_frame_ex_payload_3221_parity() -> None:
    # Arrange & Act & Assert
    p_zero = OpenThermFrameExPayload.from_bytes(bytes.fromhex("0000"))
    assert payload_to_dict(p_zero) == {"frame_code": 0, "flags": 0}
    assert p_zero.to_dict() == {SZ_VALUE: 0}

    p_flags = OpenThermFrameExPayload.from_bytes(bytes.fromhex("010F"))
    assert payload_to_dict(p_flags) == {"frame_code": 1, "flags": 15}
    assert p_flags.to_dict() == {SZ_FRAME_CODE: 1, SZ_FLAGS: 15}


def test_opentherm_bridge_status_payload_3223_parity() -> None:
    # Arrange & Act & Assert
    p_zero = OpenThermBridgeStatusPayload.from_bytes(bytes.fromhex("0000"))
    assert payload_to_dict(p_zero) == {"status_code": 0, "flags": 0}
    assert p_zero.to_dict() == {SZ_VALUE: 0}

    p_status = OpenThermBridgeStatusPayload.from_bytes(bytes.fromhex("0201"))
    assert payload_to_dict(p_status) == {"status_code": 2, "flags": 1}
    assert p_status.to_dict() == {SZ_STATUS_CODE: 2, SZ_FLAGS: 1}


def test_opentherm_params_payload_2401_parity() -> None:
    # Arrange & Act & Assert
    payload = OpenThermParamsPayload.from_bytes(bytes.fromhex("00CA"))
    assert payload_to_dict(payload) == {
        "parameter_index": 0,
        "parameter_value": 202,
    }
    assert payload.to_dict() == {SZ_HEAT_DEMAND: 1.0}
