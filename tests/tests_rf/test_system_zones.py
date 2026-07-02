#!/usr/bin/env python3
"""Test the System zones logic, providing maximum test coverage for zones.py."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ramses_rf.const import I_, ZON_ROLE_MAP, Code
from ramses_rf.devices import BdrSwitch, DhwSensor
from ramses_rf.exceptions import (
    SchemaInconsistentError,
    SystemInconsistent,
    SystemSchemaInconsistent,
)
from ramses_rf.messages import Message
from ramses_rf.systems.zones import (
    DhwZone,
    EleZone,
    UfhZone,
    Zone,
    ZoneBase,
    ZoneSchedule,
    _transform,
    zone_factory,
)
from ramses_tx.exceptions import ProtocolTimeoutError
from ramses_tx.packet import Packet


@pytest.fixture
def mock_gwy() -> MagicMock:
    """Provide a mocked Gateway instance."""
    gwy = MagicMock()
    gwy.config.enable_eavesdrop = False
    gwy.device_registry.get_device.return_value = MagicMock()
    gwy.async_send_cmd = AsyncMock(return_value="mocked_packet")
    gwy.message_store = None
    return gwy


@pytest.fixture
def mock_tcs(mock_gwy: MagicMock) -> MagicMock:
    """Provide a mocked TCS (Evohome) instance."""
    tcs = MagicMock()
    tcs.id = "01:123456"
    tcs._gwy = mock_gwy
    tcs.ctl = MagicMock()
    tcs.ctl.id = "01:123456"
    tcs.ctl.addr = MagicMock()
    tcs.dhw = None
    tcs.zone_by_idx = {}
    tcs._max_zones = 12
    return tcs


def create_mock_msg(code: str, payload: Any, src: Any) -> MagicMock:
    """Create a simulated Message object for handle_msg testing."""
    msg = MagicMock(spec=Message)
    msg.code = code
    msg.verb = I_
    msg.src = src
    msg.dst = MagicMock()
    msg.payload = payload

    # Internal context attributes needed for caching / state tracking
    msg._pkt = MagicMock()
    msg._pkt._ctx = f"mock_ctx_{code}"

    return msg


def test_transform_function() -> None:
    """Test the valve position to demand percentage transformation."""
    assert _transform(0.15) == 0.0  # 15% <= 30% -> 0
    assert _transform(0.30) == 0.0  # 30% <= 30% -> 0
    # 50% -> (50-30)*30/(70-30) + 0 + 0.5 = 15.5 -> floor is 15 -> 0.15
    assert _transform(0.50) == 0.15
    # 80% -> (80-70)*70/(100-70) + 30 + 0.5 = 53.83 -> floor is 53 -> 0.53
    assert _transform(0.80) == 0.53


@pytest.mark.asyncio
async def test_zone_base(mock_tcs: MagicMock) -> None:
    """Test the ZoneBase initialization and base methods."""
    zon = ZoneBase(mock_tcs, "00")
    assert zon.idx == "00"
    assert zon.id == "01:123456_00"
    assert repr(zon) == "01:123456_00 (None)"

    zon2 = ZoneBase(mock_tcs, "01")
    assert zon < zon2
    # Check that comparison with non-ZoneBase appropriately returns NotImplemented
    assert zon.__lt__("string_fallback") is NotImplemented

    assert await zon.schema() == {}
    assert await zon.params() == {}
    assert await zon.status() == {}


@pytest.mark.asyncio
async def test_zone_schedule(mock_tcs: MagicMock) -> None:
    """Test schedule retrieval and mutations."""
    zon = ZoneSchedule(mock_tcs, "02")
    zon._schedule = MagicMock()
    zon._schedule.version = 42
    zon._schedule.get_schedule = AsyncMock()
    zon._schedule.set_schedule = AsyncMock()
    zon._schedule.schedule = []

    zon.entity_state = MagicMock()
    zon.entity_state._msg_value = AsyncMock(return_value={})

    await zon.get_schedule(force_io=True)
    zon._schedule.get_schedule.assert_called_once_with(force_io=True)

    await zon.set_schedule({"new": "schedule"})
    zon._schedule.set_schedule.assert_called_once_with({"new": "schedule"})

    assert zon.schedule == []
    assert await zon.schedule_version() == 42

    status = await zon.status()
    assert status["schedule_version"] == 42


@pytest.mark.asyncio
async def test_dhw_zone_initialization(mock_tcs: MagicMock) -> None:
    """Test the DhwZone initialization constraints."""
    dhw = DhwZone(mock_tcs, "HW")
    assert dhw.idx == "HW"

    mock_tcs.dhw = dhw
    with pytest.raises(SchemaInconsistentError):
        DhwZone(mock_tcs, "HW")

    mock_tcs.dhw = None
    with pytest.raises(SchemaInconsistentError):
        DhwZone(mock_tcs, "01")


def test_dhw_zone_schema_updates(mock_tcs: MagicMock) -> None:
    """Test schema injection into DHW."""
    dhw = DhwZone(mock_tcs, "HW")

    # Needs to match specific instances to bypass asserts in _update_schema
    mock_sensor = MagicMock(spec=DhwSensor)
    mock_sensor.id = "07:123456"
    mock_valve = MagicMock(spec=BdrSwitch)
    mock_valve.id = "13:123456"

    mock_tcs._gwy.device_registry.get_device.side_effect = [
        mock_sensor,
        mock_valve,
        mock_valve,
    ]

    dhw._update_schema(
        sensor="07:123456", hotwater_valve="13:123456", heating_valve="13:654321"
    )
    assert dhw.sensor is not None
    assert dhw.sensor.id == "07:123456"
    assert dhw.hotwater_valve is not None
    assert dhw.hotwater_valve.id == "13:123456"


@pytest.mark.asyncio
async def test_dhw_commands(mock_tcs: MagicMock) -> None:
    """Test command generation wrappers for DHW."""
    dhw = DhwZone(mock_tcs, "HW")

    await dhw.set_setpoint(55.0)
    mock_tcs._gwy.async_send_cmd.assert_called()

    await dhw.set_boost_mode()
    assert mock_tcs._gwy.async_send_cmd.call_count == 2

    await dhw.reset_mode()
    assert mock_tcs._gwy.async_send_cmd.call_count == 3

    await dhw.reset_config()
    assert mock_tcs._gwy.async_send_cmd.call_count == 4


@pytest.mark.asyncio
async def test_zone_initialization(mock_tcs: MagicMock) -> None:
    """Test standard Zone initialisation and validation rules."""
    zon = Zone(mock_tcs, "00")
    assert zon.idx == "00"

    mock_tcs.zone_by_idx = {"00": zon}
    with pytest.raises(SchemaInconsistentError):
        Zone(mock_tcs, "00")

    mock_tcs.zone_by_idx = {}
    with pytest.raises(SchemaInconsistentError):
        Zone(mock_tcs, "0C")  # 12 is max (0C is 12 -> raises Error)


def test_zone_schema_promotion(mock_tcs: MagicMock) -> None:
    """Test dynamic class promotion through schema definitions."""
    zon = Zone(mock_tcs, "01")
    zon._setup_discovery_cmds = MagicMock()  # type: ignore[method-assign]

    zon._update_schema(**{"class": "underfloor_heating"})
    assert isinstance(zon, UfhZone)

    with pytest.raises(SystemSchemaInconsistent):
        zon._update_schema(**{"class": "radiator_valve"})


def test_zone_handle_msg_discovery(mock_tcs: MagicMock) -> None:
    """Test zone promotion correctly fires on discovery packet."""
    zon = Zone(mock_tcs, "01")
    zon._setup_discovery_cmds = MagicMock()  # type: ignore[method-assign]

    # Extract a valid heat zone type dynamically to guarantee passing the conditional
    heat_zone_type = list(ZON_ROLE_MAP.HEAT_ZONES)[0]

    msg = create_mock_msg(Code._000C, {}, mock_tcs.ctl)
    msg.payload = {
        "zone_idx": "01",
        "zone_type": heat_zone_type,
        "devices": ["04:123456"],
    }

    zon._handle_msg(msg)

    # Regardless of the map implementation, it should successfully promote out of base Zone
    assert zon.__class__ is not Zone


def test_zone_handle_msg_eavesdrop(mock_tcs: MagicMock) -> None:
    """Test fallback zone promotion from eavesdropping normal packets."""
    mock_tcs._gwy.config.enable_eavesdrop = True
    zon = Zone(mock_tcs, "02")
    zon._setup_discovery_cmds = MagicMock()  # type: ignore[method-assign]

    msg = create_mock_msg(Code._0008, [{"zone_idx": "02"}], mock_tcs.ctl)
    zon._handle_msg(msg)
    assert isinstance(zon, EleZone)


@pytest.mark.asyncio
async def test_ele_zone_inconsistent_messages(mock_tcs: MagicMock) -> None:
    """Test that Electric zones explicitly deny invalid heat demand msgs."""
    zon = EleZone(mock_tcs, "01")

    msg_3150 = create_mock_msg(Code._3150, [{"zone_idx": "01"}], mock_tcs.ctl)
    with pytest.raises(SystemInconsistent):
        zon._handle_msg(msg_3150)

    msg_3ef0 = create_mock_msg(Code._3EF0, [{"zone_idx": "01"}], mock_tcs.ctl)
    with pytest.raises(SystemInconsistent):
        zon._handle_msg(msg_3ef0)


async def test_zone_handle_msg_name(mock_tcs: MagicMock) -> None:
    """Test zone handle 004 msg."""
    zon = Zone(mock_tcs, "00")

    msg = create_mock_msg(Code._0004, {"name": "my_name"}, mock_tcs.ctl)
    zon._handle_msg(msg)
    assert await zon.name() == "my_name"


@pytest.mark.asyncio
async def test_zone_commands(mock_tcs: MagicMock) -> None:
    """Test command generation overrides for general Zones."""
    zon = Zone(mock_tcs, "01")

    await zon.set_setpoint(21.0)
    mock_tcs._gwy.async_send_cmd.assert_called_once()

    await zon.set_setpoint(None)  # Invokes reset_mode under the hood
    assert mock_tcs._gwy.async_send_cmd.call_count == 2

    await zon.set_config(min_temp=10.0, max_temp=30.0)
    assert mock_tcs._gwy.async_send_cmd.call_count == 3

    await zon.set_name("Living Room")
    assert mock_tcs._gwy.async_send_cmd.call_count == 4


def test_zone_factory_routing(mock_tcs: MagicMock) -> None:
    """Test the factory constructs the correct initial base class."""
    dhw = zone_factory(mock_tcs, "HW")
    assert isinstance(dhw, DhwZone)

    zon = zone_factory(mock_tcs, "03")
    assert isinstance(zon, Zone)


@pytest.mark.asyncio
async def test_zone_get_temp_handles_protocol_timeout(
    mock_tcs: MagicMock,
) -> None:
    """Verify _get_temp gracefully handles ProtocolTimeoutError."""
    # Arrange: Create a standard Zone
    zon = Zone(mock_tcs, "01")

    # Mock async_send_cmd to raise ProtocolTimeoutError
    async def mock_send_cmd(*args: Any, **kwargs: Any) -> Packet:
        raise ProtocolTimeoutError("Mocked 20-second FSM timeout")

    mock_tcs._gwy.async_send_cmd = AsyncMock(side_effect=mock_send_cmd)

    # Act & Assert: Call _get_temp, it should catch the timeout
    # and return None without crashing the task runner.
    result = await zon._get_temp()

    # Verify it handled the exception and returned None
    assert result is None
