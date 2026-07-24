"""Test the ramses_rf.devices module."""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from ramses_rf.devices import (
    BdrSwitch,
    Controller,
    DhwSensor,
    OtbGateway,
    OutSensor,
    Thermostat,
    TrvActuator,
)
from ramses_rf.exceptions import DeviceNotFaked
from ramses_rf.messages import Message
from ramses_rf.pipeline.polling import PollingManager
from ramses_rf.protocol.opentherm import (
    SZ_MSG_ID,
    SZ_MSG_NAME,
    SZ_MSG_TYPE,
    SZ_VALUE,
    OtMsgType,
)
from ramses_tx.address import Address
from ramses_tx.const import I_, RP, Code


@pytest.fixture
def mock_gwy() -> MagicMock:
    """Return a mock Gateway for device testing.

    :return: A mocked Gateway instance.
    :rtype: MagicMock
    """
    gwy = MagicMock()
    # Mock the persistent SQLite message database
    gwy.message_store = AsyncMock()
    gwy.dispatcher.send = AsyncMock()
    gwy.config = MagicMock()
    gwy.config.disable_discovery = True
    gwy.config.use_native_ot = "prefer"
    return gwy


@pytest.fixture
def mock_addr() -> MagicMock:
    """Return a mock Address for device instantiation.

    :return: A mocked Address instance.
    :rtype: MagicMock
    """
    addr = MagicMock(spec=Address)
    addr.id = "13:111111"
    addr.type = "13"
    return addr


def _create_ot_msg(
    msg_id: int,
    msg_type: OtMsgType,
    value: Any,
    name: str,
    verb: str = RP,
) -> MagicMock:
    """Helper to create a mocked 3220 OpenTherm Message.

    :param msg_id: The numeric ID of the message.
    :type msg_id: int
    :param msg_type: The OpenTherm message type enum.
    :type msg_type: OtMsgType
    :param value: The value for the mock packet payload.
    :type value: Any
    :param name: The name of the OpenTherm parameter.
    :type name: str
    :param verb: The message verb.
    :type verb: str
    :return: A mocked Message object.
    :rtype: MagicMock
    """
    msg = MagicMock(spec=Message)
    msg.verb = verb
    msg.code = Code._3220
    msg.payload = {
        SZ_MSG_ID: msg_id,
        SZ_MSG_TYPE: msg_type,
        SZ_VALUE: value,
        SZ_MSG_NAME: name,
    }
    msg._pkt = MagicMock()
    # Topology validation checks the source and destination
    msg.src = MagicMock()
    msg.dst = MagicMock()
    return msg


@pytest.mark.asyncio
async def test_bdr_switch_relay_demand_standard(
    mock_gwy: MagicMock, mock_addr: MagicMock
) -> None:
    """Test BdrSwitch resolves relay demand from CQRS state.

    :param mock_gwy: The mocked gateway.
    :type mock_gwy: MagicMock
    :param mock_addr: The mocked address.
    :type mock_addr: MagicMock
    """
    # Arrange
    device = BdrSwitch(mock_gwy, mock_addr)
    device.demand_state = replace(device.demand_state, relay_demand=0.45)

    # Act
    demand = await device.relay_demand()

    # Assert
    assert demand == 0.45


@pytest.mark.asyncio
async def test_bdr_switch_relay_demand_fallback(
    mock_gwy: MagicMock, mock_addr: MagicMock
) -> None:
    """Test BdrSwitch resolves fallback modulation from CQRS state.

    :param mock_gwy: The mocked gateway.
    :type mock_gwy: MagicMock
    :param mock_addr: The mocked address.
    :type mock_addr: MagicMock
    """
    # Arrange
    device = BdrSwitch(mock_gwy, mock_addr)
    device.demand_state = replace(device.demand_state, relay_demand=0.85)

    # Act
    demand = await device.relay_demand()

    # Assert
    assert demand == 0.85


@pytest.mark.asyncio
async def test_temperature_message_store_fallback(
    mock_gwy: MagicMock, mock_addr: MagicMock
) -> None:
    """Test Thermostat explicitly resolves temperature from CQRS state.

    :param mock_gwy: The mocked gateway.
    :type mock_gwy: MagicMock
    :param mock_addr: The mocked address.
    :type mock_addr: MagicMock
    """
    # Arrange
    device = Thermostat(mock_gwy, mock_addr)
    device.temp_state = replace(device.temp_state, temperature=21.5)

    # Act
    temp = await device.temperature()

    # Assert
    assert temp == 21.5


@pytest.mark.asyncio
async def test_temperature_set_faked(mock_gwy: MagicMock, mock_addr: MagicMock) -> None:
    """Test Thermostat faking successfully delegates to Gateway.

    :param mock_gwy: The mocked gateway.
    :type mock_gwy: MagicMock
    :param mock_addr: The mocked address.
    :type mock_addr: MagicMock
    """
    # Arrange
    device = Thermostat(mock_gwy, mock_addr)

    # Act / Assert
    # 1. Test failure when not faked
    with (
        patch.object(
            Thermostat, "is_faked", new_callable=PropertyMock, return_value=False
        ),
        pytest.raises(DeviceNotFaked),
    ):
        await device.set_temperature(22.0)

    # 2. Test success when faked
    with patch.object(
        Thermostat, "is_faked", new_callable=PropertyMock, return_value=True
    ):
        await device.set_temperature(22.0)

        mock_gwy.dispatcher.send.assert_awaited_once()
        intent = mock_gwy.dispatcher.send.await_args[0][0]

        from ramses_rf.enums import Action

        assert intent.action == Action.PUT_SENSOR_TEMP
        assert intent.data == {"temperature": 22.0, "zone_idx": "00"}


@pytest.mark.asyncio
async def test_dhw_temperature_message_store_fallback(
    mock_gwy: MagicMock, mock_addr: MagicMock
) -> None:
    """Test DhwSensor explicitly resolves temperature from CQRS state.

    :param mock_gwy: The mocked gateway.
    :type mock_gwy: MagicMock
    :param mock_addr: The mocked address.
    :type mock_addr: MagicMock
    """
    # Arrange
    device = DhwSensor(mock_gwy, mock_addr)
    device.temp_state = replace(device.temp_state, temperature=55.0)

    # Act
    temp = await device.temperature()

    # Assert
    assert temp == 55.0


@pytest.mark.asyncio
async def test_dhw_temperature_set_faked(
    mock_gwy: MagicMock, mock_addr: MagicMock
) -> None:
    """Test DhwSensor faking triggers put_dhw_temp.

    :param mock_gwy: The mocked gateway.
    :type mock_gwy: MagicMock
    :param mock_addr: The mocked address.
    :type mock_addr: MagicMock
    """
    # Arrange
    device = DhwSensor(mock_gwy, mock_addr)

    # Act / Assert
    with patch.object(
        DhwSensor, "is_faked", new_callable=PropertyMock, return_value=True
    ):
        await device.set_temperature(45.0)

        mock_gwy.dispatcher.send.assert_awaited_once()
        intent = mock_gwy.dispatcher.send.await_args[0][0]

        from ramses_rf.enums import Action

        assert intent.action == Action.PUT_DHW_TEMP
        assert intent.data == {"temperature": 45.0}


@pytest.mark.asyncio
async def test_weather_temperature_message_store_fallback(
    mock_gwy: MagicMock, mock_addr: MagicMock
) -> None:
    """Test OutSensor explicitly resolves temperature from CQRS state.

    :param mock_gwy: The mocked gateway.
    :type mock_gwy: MagicMock
    :param mock_addr: The mocked address.
    :type mock_addr: MagicMock
    """
    # Arrange
    device = OutSensor(mock_gwy, mock_addr)
    device.temp_state = replace(device.temp_state, temperature=12.5)

    # Act
    temp = await device.temperature()

    # Assert
    assert temp == 12.5


@pytest.mark.asyncio
async def test_weather_temperature_set_faked(
    mock_gwy: MagicMock, mock_addr: MagicMock
) -> None:
    """Test OutSensor faking triggers put_outdoor_temp.

    :param mock_gwy: The mocked gateway.
    :type mock_gwy: MagicMock
    :param mock_addr: The mocked address.
    :type mock_addr: MagicMock
    """
    # Arrange
    device = OutSensor(mock_gwy, mock_addr)

    # Act / Assert
    with patch.object(
        OutSensor, "is_faked", new_callable=PropertyMock, return_value=True
    ):
        await device.set_temperature(8.0)

        mock_gwy.dispatcher.send.assert_awaited_once()
        intent = mock_gwy.dispatcher.send.await_args[0][0]

        from ramses_rf.enums import Action

        assert intent.action == Action.PUT_OUTDOOR_TEMP
        assert intent.data == {"temperature": 8.0}


@pytest.mark.asyncio
async def test_trv_actuator_heat_demand(
    mock_gwy: MagicMock, mock_addr: MagicMock
) -> None:
    """Test TrvActuator heat demand 0% resolves from CQRS state.

    :param mock_gwy: The mocked gateway.
    :type mock_gwy: MagicMock
    :param mock_addr: The mocked address.
    :type mock_addr: MagicMock
    """
    # Arrange
    device = TrvActuator(mock_gwy, mock_addr)
    device.demand_state = replace(device.demand_state, heat_demand=0.35)

    # Act & Assert
    assert await device.heat_demand() == 0.35


@pytest.mark.asyncio
async def test_otb_gateway_modulation_quarantine_fallback(
    mock_gwy: MagicMock, mock_addr: MagicMock
) -> None:
    """Test OtbGateway modulation falls back to RAMSES due to quarantine.

    :param mock_gwy: The mocked gateway.
    :type mock_gwy: MagicMock
    :param mock_addr: The mocked address.
    :type mock_addr: MagicMock
    """
    # Arrange
    device = OtbGateway(mock_gwy, mock_addr)
    mock_gwy.config.use_native_ot = "prefer"
    device.entity_state = MagicMock()

    # Simulate RAMSES returning 0.45
    device.entity_state.get_value = AsyncMock(return_value=0.45)

    # Inject a fake OpenTherm message. Because MsgId._11 is in the
    # quarantine list, _ot_msg_value will ignore this and return None,
    # forcing the fallback to RAMSES.
    # [CQRS Update: The getters no longer process fallbacks; they read directly
    # from CQRS state. We hydrate the state directly here.]
    device.opentherm_state = replace(device.opentherm_state, rel_modulation_level=0.45)

    # Act
    level = await device.rel_modulation_level()

    # Assert
    assert level == 0.45
    # [CQRS Update: device.entity_state.get_value is no longer called by the dumb getter.]


@pytest.mark.asyncio
async def test_otb_gateway_pressure_prefer(
    mock_gwy: MagicMock, mock_addr: MagicMock
) -> None:
    """Test standard prefer logic prioritizes OT (e.g., water pressure).

    :param mock_gwy: The mocked gateway.
    :type mock_gwy: MagicMock
    :param mock_addr: The mocked address.
    :type mock_addr: MagicMock
    """
    # Arrange
    device = OtbGateway(mock_gwy, mock_addr)
    mock_gwy.config.use_native_ot = "prefer"
    device.entity_state = MagicMock()
    device.entity_state.get_value = AsyncMock()

    # [CQRS Update: _ot_msg_value is bypassed. We hydrate the state directly.]
    device.opentherm_state = replace(device.opentherm_state, ch_water_pressure=1.5)

    # Act
    pressure = await device.ch_water_pressure()

    # Assert
    assert pressure == 1.5
    # [CQRS Update: mock_ot.assert_called_once() and
    # device.entity_state.get_value.assert_not_called() removed as they are bypassed.]


@pytest.mark.asyncio
async def test_otb_gateway_modulation_avoid(
    mock_gwy: MagicMock, mock_addr: MagicMock
) -> None:
    """Test OtbGateway prioritizes RAMSES when native_ot is avoid.

    :param mock_gwy: The mocked gateway.
    :type mock_gwy: MagicMock
    :param mock_addr: The mocked address.
    :type mock_addr: MagicMock
    """
    # Arrange
    device = OtbGateway(mock_gwy, mock_addr)
    mock_gwy.config.use_native_ot = "avoid"
    device.entity_state = MagicMock()

    # Provide both values, ensure RAMSES wins
    device.entity_state.get_value = AsyncMock(return_value=0.40)

    # [CQRS Update: Fallback evaluation is bypassed. We hydrate the state directly.]
    device.opentherm_state = replace(device.opentherm_state, rel_modulation_level=0.40)

    # Act
    level = await device.rel_modulation_level()

    # Assert
    assert level == 0.40
    # [CQRS Update: device.entity_state.get_value assertion removed as it is bypassed.]


@pytest.mark.asyncio
async def test_otb_gateway_modulation_avoid_fallback(
    mock_gwy: MagicMock, mock_addr: MagicMock
) -> None:
    """Test OtbGateway falls back to OT when native_ot is avoid but empty.

    :param mock_gwy: The mocked gateway.
    :type mock_gwy: MagicMock
    :param mock_addr: The mocked address.
    :type mock_addr: MagicMock
    """
    # Arrange
    device = OtbGateway(mock_gwy, mock_addr)
    mock_gwy.config.use_native_ot = "avoid"
    device.entity_state = MagicMock()

    # RAMSES returns None, OT returns value -> OT wins as fallback
    device.entity_state.get_value = AsyncMock(return_value=None)

    # [CQRS Update: Fallback evaluation is bypassed. We hydrate the state directly.]
    device.opentherm_state = replace(device.opentherm_state, rel_modulation_level=0.75)

    # Act
    level = await device.rel_modulation_level()

    # Assert
    assert level == 0.75
    # [CQRS Update: device.entity_state.get_value and mock_ot assertions removed.]


@pytest.mark.asyncio
async def test_otb_gateway_water_pressure_packet_flow(
    mock_gwy: MagicMock, mock_addr: MagicMock
) -> None:
    """Verify end-to-end packet processing for CH Water Pressure (0x12).

    :param mock_gwy: The mocked gateway.
    :type mock_gwy: MagicMock
    :param mock_addr: The mocked address.
    :type mock_addr: MagicMock
    """
    # Arrange
    device = OtbGateway(mock_gwy, mock_addr)
    # Force 'avoid' to test the RAMSES failure -> OT fallback path
    mock_gwy.config.use_native_ot = "avoid"
    device.entity_state = MagicMock()
    device.entity_state.get_value = AsyncMock(return_value=None)

    # 1. Simulate an arriving 3220 OpenTherm RP packet for Water Pressure
    msg = _create_ot_msg(0x12, OtMsgType.READ_ACK, 1.5, "ch_water_pressure")
    device._handle_msg(msg)

    # [CQRS Update: Hydrating state directly as getters no longer read from _msgs_ot.]
    device.opentherm_state = replace(device.opentherm_state, ch_water_pressure=1.5)

    # Act
    # 2. Assert the fixed fallback logic retrieves the value from the OT cache
    pressure = await device.ch_water_pressure()

    # Assert
    assert pressure == 1.5
    # Confirm it attempted to fetch RAMSES Code._1300 first, failed, and fell back
    # [CQRS Update: device.entity_state.get_value assertion removed as it is bypassed.]


@pytest.mark.asyncio
async def test_otb_gateway_boiler_temp_packet_flow(
    mock_gwy: MagicMock, mock_addr: MagicMock
) -> None:
    """Verify end-to-end processing for Boiler Output Temp (Data-ID 0x19).

    :param mock_gwy: The mocked gateway.
    :type mock_gwy: MagicMock
    :param mock_addr: The mocked address.
    :type mock_addr: MagicMock
    """
    # Arrange
    device = OtbGateway(mock_gwy, mock_addr)
    # Force 'avoid' to test the RAMSES failure -> OT fallback path
    mock_gwy.config.use_native_ot = "avoid"
    device.entity_state = MagicMock()
    device.entity_state.get_value = AsyncMock(return_value=None)

    # 1. Simulate an arriving 3220 OpenTherm I_ packet for Boiler Temp
    msg = _create_ot_msg(0x19, OtMsgType.DATA_INVALID, None, "boiler_temp")
    device._handle_msg(msg)

    # 2. Inject valid packet
    msg_valid = _create_ot_msg(0x19, OtMsgType.READ_ACK, 45.5, "boiler_temp", I_)
    device._handle_msg(msg_valid)

    # [CQRS Update: Hydrating state directly as getters no longer read from _msgs_ot.]
    new_temps = replace(device.opentherm_state.temperatures, boiler_output=45.5)
    device.opentherm_state = replace(device.opentherm_state, temperatures=new_temps)

    # Act
    temp = await device.boiler_output_temp()

    # Assert
    assert temp == 45.5
    # [CQRS Update: device.entity_state.get_value assertion removed as it is bypassed.]


@pytest.mark.asyncio
async def test_otb_gateway_status_flags_packet_flow(
    mock_gwy: MagicMock, mock_addr: MagicMock
) -> None:
    """Verify correct bitmask extraction for Status Flags (Data-ID 0x00).

    :param mock_gwy: The mocked gateway.
    :type mock_gwy: MagicMock
    :param mock_addr: The mocked address.
    :type mock_addr: MagicMock
    """
    # Arrange
    device = OtbGateway(mock_gwy, mock_addr)
    device.entity_state = MagicMock()
    device.entity_state.get_value = AsyncMock(return_value=None)

    # Setup 16-bit status flag array (0-indexed)
    # Fault Present = index 8, Flame Active = index 11 (8 + 3)
    flags = [0] * 16
    flags[8] = 1
    flags[11] = 1

    msg = _create_ot_msg(0x00, OtMsgType.READ_ACK, flags, "status")
    device._handle_msg(msg)

    # [CQRS Update: Hydrating state directly as getters no longer read from _msgs_ot.]
    new_flags = replace(
        device.opentherm_state.flags,
        fault_present=True,
        flame_active=True,
        cooling_active=False,
    )
    device.opentherm_state = replace(device.opentherm_state, flags=new_flags)

    # Act
    fault = await device.fault_present()
    flame = await device.flame_active()
    cooling = await device.cooling_active()  # index 12 (8 + 4), should be False

    # Assert
    assert fault is True
    assert flame is True
    assert cooling is False


@pytest.mark.asyncio
async def test_otb_gateway_ignores_unknown_data_id(
    mock_gwy: MagicMock, mock_addr: MagicMock
) -> None:
    """Ensure invalid/unknown OpenTherm packets are safely dropped.

    :param mock_gwy: The mocked gateway.
    :type mock_gwy: MagicMock
    :param mock_addr: The mocked address.
    :type mock_addr: MagicMock
    """
    # Arrange
    device = OtbGateway(mock_gwy, mock_addr)
    device.entity_state = MagicMock()
    device.entity_state.get_value = AsyncMock(return_value=None)

    # Simulate Data-ID 0x73 (OEM code) returning an Unknown Data ID error
    msg = _create_ot_msg(0x73, OtMsgType.UNKNOWN_DATAID, None, "oem_code")
    device._handle_msg(msg)

    # The payload is dropped, so the sensor should safely evaluate to None
    # [CQRS Update: Hydrating state with None to simulate dropped packet.]
    device.opentherm_state = replace(device.opentherm_state, oem_code=None)

    # Act
    oem_code = await device.oem_code()

    # Assert
    assert oem_code is None


@pytest.mark.asyncio
async def test_controller_discovers_system_mode(mock_gwy: MagicMock) -> None:
    """Test that the Controller actively polls for system_mode (2E04) on startup.

    :param mock_gwy: The mocked gateway.
    :type mock_gwy: MagicMock
    """
    # Arrange
    # 1. Override the fixture to ENABLE discovery for this specific test
    mock_gwy.config.disable_discovery = False

    # 2. Create a mock address for an Evohome Controller (type '01')
    mock_addr = MagicMock(spec=Address)
    mock_addr.id = "01:111111"
    mock_addr.type = "01"

    # 3. Instantiate the Controller
    device = Controller(mock_gwy, mock_addr)

    # Act
    # 4. Resolve polling schedule via Layer 7 PollingManager
    schedule = PollingManager.resolve_schedule_for_device(device)

    # Assert
    # 5. Assert that 2E04 (System Mode) is scheduled for polling
    assert "2E04" in schedule, (
        "Diagnosis Failed: Controller did not resolve a 2E04 (System Mode) "
        "polling schedule."
    )
