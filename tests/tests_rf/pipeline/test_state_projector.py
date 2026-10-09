"""Pure unit tests for the StateProjector worker in the pipeline.

This suite tests the StateProjector in absolute isolation from the Gateway
or live file streaming, verifying the hexagonal data conversion logic
using pure mock entities.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime as dt, timedelta as td
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from ramses_rf.const import (
    SZ_ACTUATOR_COUNTDOWN,
    SZ_ACTUATOR_ENABLED,
    SZ_CH_ACTIVE,
    SZ_CH_ENABLED,
    SZ_CH_SETPOINT,
    SZ_COOLING_DEMAND,
    SZ_CYCLE_COUNTDOWN,
    SZ_DHW_ACTIVE,
    SZ_DIAGNOSTIC_CODE,
    SZ_DOMAIN_INDEX,
    SZ_FAN_MODE,
    SZ_FLAGS,
    SZ_FLAME_ACTIVE,
    SZ_LOCAL_OVERRIDE,
    SZ_MAX_REL_MODULATION,
    SZ_MAX_TEMP,
    SZ_MESSAGE_ID,
    SZ_MIN_TEMP,
    SZ_MODULATION_LEVEL,
    SZ_MULTIROOM_MODE,
    SZ_OPENWINDOW_FUNCTION,
    SZ_REL_MODULATION_LEVEL,
    SZ_RELAY_DEMAND,
    SZ_REMAINING_DAYS,
    SZ_REMAINING_PERCENT,
    SZ_SETPOINT,
    SZ_TICKER,
    SZ_VALUE,
    SZ_ZONE_INDEX,
    Code,
    DevType,
    Verb,
)
from ramses_rf.devices.hvac_remotes import HvacRemote
from ramses_rf.devices.hvac_ventilators import HvacVentilator
from ramses_rf.gateway import Gateway
from ramses_rf.messages import Message
from ramses_rf.models import (
    ActuatorState,
    DhwState,
    HvacState,
    OpenThermState,
    StateUpdatedEvent,
    SystemState,
    TemperatureState,
    ZoneState,
)
from ramses_rf.payloads.hvac import HvacBypassStatePayload
from ramses_rf.pipeline.ingestion import StateProjector
from ramses_rf.protocol.opentherm import (
    SZ_VALUE_HB,
    SZ_VALUE_LB,
    OtDataId,
)
from ramses_rf.state_projector import (
    _update_hvac_state,
    process_state_updates,
)
from ramses_rf.strategies.orcon import OrconStrategy
from ramses_tx import Address


class MockAddr:
    """Mock hardware address descriptor."""

    def __init__(self, device_id: str) -> None:
        """Initialize the mock address container.

        :param device_id: The target hardware ID string.
        :type device_id: str
        """
        self.id: str = device_id


class MockMessage:
    """Mock message container simulating a fully parsed L7 telemetry packet."""

    def __init__(
        self,
        code: Code,
        verb: str,
        payload: dict[str, Any],
        src_id: str,
        dst_id: str = "--:------",
        dtm: dt | None = None,
    ) -> None:
        """Initialize the mock message wrapper envelope.

        :param code: The packet command code signature.
        :type code: Code
        :param verb: The transmission verb signature.
        :type verb: str
        :param payload: The raw decoded dictionary.
        :type payload: dict[str, Any]
        :param src_id: The source hardware ID string.
        :type src_id: str
        :param dst_id: The destination hardware ID string.
        :type dst_id: str
        :param dtm: The timestamp of the message.
        :type dtm: dt | None
        """
        self.code: Code = code
        self.verb: str = verb
        self.payload: dict[str, Any] | None = payload
        self.src: MockAddr = MockAddr(src_id)
        self.dst: MockAddr = MockAddr(dst_id)
        self.dtm: dt = dtm or dt.now()
        self.correlation_id: uuid.UUID = uuid.uuid4()
        self.message_id: uuid.UUID = uuid.uuid4()


class FakeDevice:
    """A minimal fake device twin to act as an outbound target port."""

    def __init__(self) -> None:
        """Initialize the fake device with base state models."""
        self.id: str = "10:064873"
        self._SLUG: str = DevType.OTB
        self.opentherm_state: OpenThermState = OpenThermState()
        self.act_state: ActuatorState = ActuatorState()
        self.hvac_state: HvacState = HvacState()
        self.demand_state: Any = None  # set by tests that need it
        self.tcs: Any | None = None
        self.events: list[StateUpdatedEvent] = []
        self._supports_10d0: bool = False

    def apply_state_update(self, event: StateUpdatedEvent) -> None:
        """Accept an immutable state event and apply it to the read-model.

        :param event: The state update event container.
        :type event: StateUpdatedEvent
        :return: None
        :rtype: None
        """
        self.events.append(event)
        if isinstance(event.state, OpenThermState):
            self.opentherm_state = event.state
        elif isinstance(event.state, ActuatorState):
            self.act_state = event.state
        elif isinstance(event.state, HvacState):
            self.hvac_state = event.state


class FakeNonOtbDevice:
    """A minimal fake device twin for non-OTB actuators."""

    def __init__(self, slug: str) -> None:
        """Initialize the fake non-OTB device.

        :param slug: The device type slug.
        :type slug: str
        """
        self.id: str = "13:064873"
        self._SLUG: str = slug
        self.events: list[StateUpdatedEvent] = []

    def apply_state_update(self, event: StateUpdatedEvent) -> None:
        """Accept an immutable state event.

        :param event: The state update event container.
        :type event: StateUpdatedEvent
        """
        self.events.append(event)


class FakeZone:
    """A minimal fake zone entity to act as a state target."""

    def __init__(self, zone_id: str, setpoint: float = 5.0) -> None:
        """Initialize the fake zone with default state models.

        :param zone_id: The unique zone ID string.
        :type zone_id: str
        :param setpoint: Initial setpoint in degrees Celsius.
        :type setpoint: float
        """
        self.id: str = zone_id
        self.temp_state: TemperatureState = TemperatureState(setpoint=setpoint)
        self.zone_state: ZoneState = ZoneState(setpoint=setpoint)
        self.events: list[StateUpdatedEvent] = []

    def apply_state_update(self, event: StateUpdatedEvent) -> None:
        """Accept an immutable state update event for the zone.

        :param event: The state update event container.
        :type event: StateUpdatedEvent
        """
        self.events.append(event)
        if isinstance(event.state, TemperatureState):
            self.temp_state = event.state
        elif isinstance(event.state, ZoneState):
            self.zone_state = event.state


class FakeDhw:
    """A minimal fake DHW entity to act as a state target."""

    def __init__(self, dhw_id: str, setpoint: float = 50.0) -> None:
        """Initialize the fake DHW entity with default state models.

        :param dhw_id: The unique DHW ID string.
        :type dhw_id: str
        :param setpoint: Initial DHW setpoint in degrees Celsius.
        :type setpoint: float
        """
        self.id: str = dhw_id
        self.temp_state: TemperatureState = TemperatureState()
        self.dhw_state: DhwState = DhwState(setpoint=setpoint)
        self.events: list[StateUpdatedEvent] = []

    def apply_state_update(self, event: StateUpdatedEvent) -> None:
        """Accept an immutable state update event for DHW.

        :param event: The state update event container.
        :type event: StateUpdatedEvent
        """
        self.events.append(event)
        if isinstance(event.state, TemperatureState):
            self.temp_state = event.state
        elif isinstance(event.state, DhwState):
            self.dhw_state = event.state


class FakeRegistry:
    """A minimal fake registry port to handle logical target lookups."""

    def __init__(self, device: FakeDevice) -> None:
        """Initialize the registry with a tracking device map.

        :param device: The fake device to store.
        :type device: FakeDevice
        """
        self.device_by_id: dict[str, FakeDevice] = {device.id: device}
        self.systems: list[Any] = []


class FakeGatewayAdapter:
    """A mock gateway boundary adapter wrapping our device registry port."""

    def __init__(self, registry: FakeRegistry) -> None:
        """Initialize the gateway adapter port wrapper.

        :param registry: The fake registry instance to wrap.
        :type registry: FakeRegistry
        """
        self.device_registry: FakeRegistry = registry


def test_worker_opentherm_modulation_parsing() -> None:
    """Verify that the worker translates modulation messages into OpenThermStates."""
    # Arrange
    device = FakeDevice()
    registry = FakeRegistry(device)
    gwy_adapter = FakeGatewayAdapter(registry)
    queue: asyncio.Queue[Message] = asyncio.Queue()

    worker = StateProjector(gwy_adapter, queue)

    mock_msg = MockMessage(
        code=Code._3220,
        verb=Verb.RP,
        payload={
            SZ_MESSAGE_ID: int(OtDataId.REL_MODULATION_LEVEL),
            SZ_VALUE: 0.425,
        },
        src_id=device.id,
    )

    # Act
    worker._update_opentherm_state(device, mock_msg.payload, mock_msg)

    # Assert
    assert device.opentherm_state.rel_modulation_level == 0.425
    assert len(device.events) == 1
    assert device.events[0].entity_id == device.id


def test_worker_opentherm_status_flag_parsing() -> None:
    """Verify that the worker translates status arrays into flag booleans."""
    # Arrange
    device = FakeDevice()
    registry = FakeRegistry(device)
    gwy_adapter = FakeGatewayAdapter(registry)
    queue: asyncio.Queue[Message] = asyncio.Queue()

    worker = StateProjector(gwy_adapter, queue)

    status_array = [0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 1, 0]
    mock_msg = MockMessage(
        code=Code._3220,
        verb=Verb.RP,
        payload={SZ_MESSAGE_ID: int(OtDataId.STATUS), SZ_VALUE: status_array},
        src_id=device.id,
    )

    # Act
    worker._update_opentherm_state(device, mock_msg.payload, mock_msg)

    # Assert
    assert device.opentherm_state.flags.ch_active is True
    assert device.opentherm_state.flags.flame_active is True
    assert device.opentherm_state.flags.ch_enabled is False


def test_worker_opentherm_fault_flag_parsing() -> None:
    """Verify msg_id 0x05 populates fault flags and the OEM fault code."""
    # Arrange
    device = FakeDevice()
    registry = FakeRegistry(device)
    gwy_adapter = FakeGatewayAdapter(registry)
    queue: asyncio.Queue[Message] = asyncio.Queue()

    worker = StateProjector(gwy_adapter, queue)

    mock_msg = MockMessage(
        code=Code._3220,
        verb=Verb.RP,
        payload={
            SZ_MESSAGE_ID: int(OtDataId.OEM_FAULTS),
            SZ_VALUE_HB: [1, 0, 1, 0, 0, 1, 0, 0],
            SZ_VALUE_LB: 42,
        },
        src_id=device.id,
    )

    # Act
    worker._update_opentherm_state(device, mock_msg.payload, mock_msg)

    # Assert
    faults = device.opentherm_state.faults
    assert faults.service_request is True
    assert faults.lockout_reset is False
    assert faults.low_water_pressure is True
    assert faults.gas_flame_fault is False
    assert faults.air_pressure_fault is False
    assert faults.water_over_temperature is True
    assert device.opentherm_state.oem_fault_code == 42


def test_worker_opentherm_exhaust_temp_parsing() -> None:
    """Verify msg_id 0x21 populates the boiler exhaust temperature."""
    # Arrange
    device = FakeDevice()
    registry = FakeRegistry(device)
    gwy_adapter = FakeGatewayAdapter(registry)
    queue: asyncio.Queue[Message] = asyncio.Queue()

    worker = StateProjector(gwy_adapter, queue)

    mock_msg = MockMessage(
        code=Code._3220,
        verb=Verb.RP,
        payload={
            SZ_MESSAGE_ID: int(OtDataId.BOILER_EXHAUST_TEMP),
            SZ_VALUE: 58,
        },
        src_id=device.id,
    )

    # Act
    worker._update_opentherm_state(device, mock_msg.payload, mock_msg)

    # Assert
    assert device.opentherm_state.temperatures.boiler_exhaust == 58


def test_worker_hvac_state_parsing() -> None:
    """Verify that the worker extracts multi-property HVAC data into HvacState."""
    # Arrange
    device = FakeDevice()
    device.id = "32:123456"
    device._SLUG = DevType.HVC

    registry = FakeRegistry(device)
    gwy_adapter = FakeGatewayAdapter(registry)
    queue: asyncio.Queue[Message] = asyncio.Queue()

    worker = StateProjector(gwy_adapter, queue)

    hvac_payload = {
        "co2_level": 850,
        "indoor_humidity": 45.2,
        "fan_mode": "auto",
        "presence_detected": True,
        SZ_REMAINING_DAYS: 120,
    }

    mock_msg = MockMessage(
        code=Code._31D9,
        verb=Verb.I_,
        payload=hvac_payload,
        src_id=device.id,
    )

    # Act
    worker._update_hvac_state(device, mock_msg.payload, mock_msg)

    # Assert
    assert device.hvac_state.co2_level == 850
    assert device.hvac_state.indoor_humidity == 45.2
    assert device.hvac_state.fan_mode == "auto"
    assert device.hvac_state.presence_detected is True
    assert device.hvac_state.filter_remaining_days == 120

    assert len(device.events) == 1
    assert isinstance(device.events[0].state, HvacState)


def test_worker_hvac_state_parses_10d0_support() -> None:
    """Verify that the worker sets HVAC 10D0 flag on device."""
    # Arrange
    device = FakeDevice()
    device.id = "32:123456"
    device._SLUG = DevType.FAN

    registry = FakeRegistry(device)
    gwy_adapter = FakeGatewayAdapter(registry)
    queue: asyncio.Queue[Message] = asyncio.Queue()

    worker = StateProjector(gwy_adapter, queue)

    hvac_payload = {SZ_REMAINING_DAYS: 120, SZ_REMAINING_PERCENT: None}

    mock_msg = MockMessage(
        code=Code._10D0,
        verb=Verb.RP,
        payload=hvac_payload,
        src_id=device.id,
    )

    # Act
    worker._update_hvac_state(device, mock_msg.payload, mock_msg)

    # Assert
    assert device.hvac_state.filter_remaining_days == 120

    assert device._supports_10d0 is True


def test_worker_opentherm_modulation_keys_parity() -> None:
    # Arrange
    device = FakeDevice()
    registry = FakeRegistry(device)
    gwy_adapter = FakeGatewayAdapter(registry)
    queue: asyncio.Queue[Message] = asyncio.Queue()
    worker = StateProjector(gwy_adapter, queue)

    msg_mod_level = MockMessage(
        code=Code._3EF0,
        verb=Verb.I_,
        payload={SZ_MODULATION_LEVEL: 0.65},
        src_id=device.id,
    )
    msg_rel_mod_level = MockMessage(
        code=Code._3EF0,
        verb=Verb.I_,
        payload={SZ_REL_MODULATION_LEVEL: 0.85},
        src_id=device.id,
    )

    # Act
    worker._update_opentherm_state(
        device, msg_mod_level.payload, msg_mod_level
    )

    # Assert
    assert device.opentherm_state.rel_modulation_level == 0.65

    # Act
    worker._update_opentherm_state(
        device, msg_rel_mod_level.payload, msg_rel_mod_level
    )

    # Assert
    assert device.opentherm_state.rel_modulation_level == 0.85


def test_state_projector_routes_controller_22d9_to_appliance_control() -> None:
    # Arrange
    ctl_dev = FakeDevice()
    ctl_dev.id = "01:054173"
    ctl_dev._SLUG = DevType.CTL

    otb_dev = FakeDevice()
    otb_dev.id = "10:048122"
    otb_dev._SLUG = DevType.OTB

    class FakeTcs:
        id = "01:054173"
        appliance_control = otb_dev
        zone_by_index: dict[str, Any] = {}

    mock_tcs = FakeTcs()
    registry = FakeRegistry(ctl_dev)
    registry.device_by_id[otb_dev.id] = otb_dev
    registry.systems = [mock_tcs]

    gwy_adapter = FakeGatewayAdapter(registry)
    queue: asyncio.Queue[Message] = asyncio.Queue()
    worker = StateProjector(gwy_adapter, queue)

    # Act: Controller broadcasts 22D9 (Boiler Setpoint 48.5C) to --:------
    broadcast_msg = MockMessage(
        code=Code._22D9,
        verb=Verb.I_,
        payload={SZ_SETPOINT: 48.5},
        src_id="01:054173",
        dst_id="--:------",
    )
    worker.process_message_state(broadcast_msg)

    # Assert
    assert otb_dev.opentherm_state.temperatures.boiler_setpoint == 48.5


def test_state_projector_routes_controller_3ef0_to_appliance_control() -> None:
    # Arrange
    ctl_dev = FakeDevice()
    ctl_dev.id = "01:054173"
    ctl_dev._SLUG = DevType.CTL

    otb_dev = FakeDevice()
    otb_dev.id = "10:048122"
    otb_dev._SLUG = DevType.OTB

    class FakeTcs:
        id = "01:054173"
        appliance_control = otb_dev
        zone_by_index: dict[str, Any] = {}

    mock_tcs = FakeTcs()
    registry = FakeRegistry(ctl_dev)
    registry.device_by_id[otb_dev.id] = otb_dev
    registry.systems = [mock_tcs]

    gwy_adapter = FakeGatewayAdapter(registry)
    queue: asyncio.Queue[Message] = asyncio.Queue()
    worker = StateProjector(gwy_adapter, queue)

    # Act: Controller broadcasts 3EF0 (Modulation 55% + CH active) to --:------
    broadcast_msg = MockMessage(
        code=Code._3EF0,
        verb=Verb.I_,
        payload={
            SZ_MODULATION_LEVEL: 0.55,
            "ch_active": True,
            "ch_enabled": True,
        },
        src_id="01:054173",
        dst_id="--:------",
    )
    worker.process_message_state(broadcast_msg)

    # Assert
    assert otb_dev.opentherm_state.rel_modulation_level == 0.55
    assert otb_dev.opentherm_state.flags.ch_active is True
    assert otb_dev.opentherm_state.flags.ch_enabled is True
    assert otb_dev.act_state.modulation_level == 0.55


def test_state_projector_routes_opentherm_3220_data_id_responses() -> None:
    # Arrange
    device = FakeDevice()
    registry = FakeRegistry(device)
    gwy_adapter = FakeGatewayAdapter(registry)
    queue: asyncio.Queue[Message] = asyncio.Queue()
    worker = StateProjector(gwy_adapter, queue)

    # Act: Ingest Boiler Output Temp (0x19), Return Temp (0x1C), DHW Setpoint (0x38), CH Max Setpoint (0x39)
    msg_flow_temp = MockMessage(
        code=Code._3220,
        verb=Verb.RP,
        payload={
            SZ_MESSAGE_ID: int(OtDataId.BOILER_OUTPUT_TEMP),
            SZ_VALUE: 58.5,
        },
        src_id=device.id,
    )
    msg_return_temp = MockMessage(
        code=Code._3220,
        verb=Verb.RP,
        payload={
            SZ_MESSAGE_ID: int(OtDataId.BOILER_RETURN_TEMP),
            SZ_VALUE: 41.2,
        },
        src_id=device.id,
    )
    msg_dhw_setpoint = MockMessage(
        code=Code._3220,
        verb=Verb.RP,
        payload={
            SZ_MESSAGE_ID: int(OtDataId.DHW_SETPOINT),
            SZ_VALUE: 52.0,
        },
        src_id=device.id,
    )
    msg_ch_max_setpoint = MockMessage(
        code=Code._3220,
        verb=Verb.RP,
        payload={
            SZ_MESSAGE_ID: int(OtDataId.CH_MAX_SETPOINT),
            SZ_VALUE: 75.0,
        },
        src_id=device.id,
    )

    worker.process_message_state(msg_flow_temp)
    worker.process_message_state(msg_return_temp)
    worker.process_message_state(msg_dhw_setpoint)
    worker.process_message_state(msg_ch_max_setpoint)

    # Assert
    assert device.opentherm_state.temperatures.boiler_output == 58.5
    assert device.opentherm_state.temperatures.boiler_return == 41.2
    assert device.opentherm_state.temperatures.dhw_setpoint == 52.0
    assert device.opentherm_state.temperatures.ch_max_setpoint == 75.0


def test_state_projector_routes_controller_2d49_to_tcs() -> None:
    # Arrange
    ctl_dev = FakeDevice()
    ctl_dev.id = "01:054173"
    ctl_dev._SLUG = DevType.CTL

    class FakeTcs:
        id = "01:054173"
        zone_by_index: dict[str, Any] = {}
        system_state: SystemState = SystemState()

    mock_tcs = FakeTcs()
    ctl_dev.tcs = mock_tcs
    registry = FakeRegistry(ctl_dev)
    registry.systems = [mock_tcs]

    gwy_adapter = FakeGatewayAdapter(registry)
    queue: asyncio.Queue[Message] = asyncio.Queue()
    worker = StateProjector(gwy_adapter, queue)

    # Act: Controller broadcasts 2D49 (Cooling Active)
    broadcast_msg = MockMessage(
        code=Code._2D49,
        verb=Verb.I_,
        payload={SZ_ZONE_INDEX: "00", SZ_COOLING_DEMAND: True},
        src_id="01:054173",
        dst_id="--:------",
    )
    worker.process_message_state(broadcast_msg)

    # Assert
    assert mock_tcs.system_state.cooling_mode is True


def test_state_projector_routes_ufc_2d49_to_tcs() -> None:
    # Arrange
    ufc_dev = FakeDevice()
    ufc_dev.id = "02:123456"
    ufc_dev._SLUG = DevType.UFC

    class FakeTcs:
        id = "01:054173"
        zone_by_index: dict[str, Any] = {}
        system_state: SystemState = SystemState()

    mock_tcs = FakeTcs()
    ufc_dev.tcs = mock_tcs
    registry = FakeRegistry(ufc_dev)
    registry.systems = [mock_tcs]

    gwy_adapter = FakeGatewayAdapter(registry)
    queue: asyncio.Queue[Message] = asyncio.Queue()
    worker = StateProjector(gwy_adapter, queue)

    # Act: UFC broadcasts 2D49 (Cooling Active)
    broadcast_msg = MockMessage(
        code=Code._2D49,
        verb=Verb.I_,
        payload={SZ_ZONE_INDEX: "01", SZ_COOLING_DEMAND: True},
        src_id="02:123456",
        dst_id="--:------",
    )
    worker.process_message_state(broadcast_msg)

    # Assert
    assert mock_tcs.system_state.cooling_mode is True


def test_state_projector_routes_2d49_inactive_cooling_to_tcs() -> None:
    # Arrange
    ctl_dev = FakeDevice()
    ctl_dev.id = "01:054173"
    ctl_dev._SLUG = DevType.CTL

    class FakeTcs:
        id = "01:054173"
        zone_by_index: dict[str, Any] = {}
        system_state: SystemState = SystemState()

    mock_tcs = FakeTcs()
    ctl_dev.tcs = mock_tcs
    registry = FakeRegistry(ctl_dev)
    registry.systems = [mock_tcs]

    gwy_adapter = FakeGatewayAdapter(registry)
    queue: asyncio.Queue[Message] = asyncio.Queue()
    worker = StateProjector(gwy_adapter, queue)

    # Act: Controller broadcasts 2D49 (Cooling Inactive)
    broadcast_msg = MockMessage(
        code=Code._2D49,
        verb=Verb.I_,
        payload={SZ_ZONE_INDEX: "00", SZ_COOLING_DEMAND: False},
        src_id="01:054173",
        dst_id="--:------",
    )
    worker.process_message_state(broadcast_msg)

    # Assert
    assert mock_tcs.system_state.cooling_mode is False


def test_22d9_boiler_setpoint_does_not_mutate_zone_00_setpoint() -> None:
    # Arrange
    ctl_dev = FakeDevice()
    ctl_dev.id = "01:216136"
    ctl_dev._SLUG = DevType.CTL

    otb_dev = FakeDevice()
    otb_dev.id = "10:064873"
    otb_dev._SLUG = DevType.OTB

    zone_00 = FakeZone("01:216136_00", setpoint=5.0)
    zone_01 = FakeZone("01:216136_01", setpoint=20.0)
    dhw = FakeDhw("01:216136_HW", setpoint=50.0)

    class FakeTcs:
        def __init__(self) -> None:
            self.id = "01:216136"
            self.appliance_control = otb_dev
            self.dhw = dhw
            self.zone_by_index = {"00": zone_00, "01": zone_01}

    mock_tcs = FakeTcs()
    registry = FakeRegistry(ctl_dev)
    registry.device_by_id[otb_dev.id] = otb_dev
    registry.systems = [mock_tcs]

    gwy_adapter = FakeGatewayAdapter(registry)
    queue: asyncio.Queue[Message] = asyncio.Queue()
    worker = StateProjector(gwy_adapter, queue)

    # Act 1: Controller broadcasts 22D9 (Boiler Setpoint 10.0C, idle)
    msg_22d9_idle = MockMessage(
        code=Code._22D9,
        verb=Verb.I_,
        payload={SZ_DOMAIN_INDEX: "00", SZ_SETPOINT: 10.0},
        src_id="01:216136",
        dst_id="--:------",
    )
    worker.process_message_state(msg_22d9_idle)

    # Assert 1: OTB receives boiler setpoint; Zone 00 remains 5.0C
    assert otb_dev.opentherm_state.temperatures.boiler_setpoint == 10.0
    assert zone_00.temp_state.setpoint == 5.0
    assert zone_01.temp_state.setpoint == 20.0
    assert dhw.dhw_state.setpoint == 50.0

    # Act 2: Controller broadcasts 22D9 (Boiler Setpoint 60.0C, burn)
    msg_22d9_burn = MockMessage(
        code=Code._22D9,
        verb=Verb.I_,
        payload={SZ_DOMAIN_INDEX: "00", SZ_SETPOINT: 60.0},
        src_id="01:216136",
        dst_id="--:------",
    )
    worker.process_message_state(msg_22d9_burn)

    # Assert 2: OTB receives boiler setpoint 60.0C; Zone 00 remains 5.0C
    assert otb_dev.opentherm_state.temperatures.boiler_setpoint == 60.0
    assert zone_00.temp_state.setpoint == 5.0
    assert zone_01.temp_state.setpoint == 20.0


def test_2309_and_2349_correctly_update_zones_and_do_not_mutate_appliance_control() -> (
    None
):
    # Arrange
    ctl_dev = FakeDevice()
    ctl_dev.id = "01:216136"
    ctl_dev._SLUG = DevType.CTL

    otb_dev = FakeDevice()
    otb_dev.id = "10:064873"
    otb_dev._SLUG = DevType.OTB

    zone_00 = FakeZone("01:216136_00", setpoint=5.0)
    zone_01 = FakeZone("01:216136_01", setpoint=20.0)

    class FakeTcs:
        id = "01:216136"
        appliance_control = otb_dev
        zone_by_index = {"00": zone_00, "01": zone_01}

    mock_tcs = FakeTcs()
    registry = FakeRegistry(ctl_dev)
    registry.device_by_id[otb_dev.id] = otb_dev
    registry.systems = [mock_tcs]

    gwy_adapter = FakeGatewayAdapter(registry)
    queue: asyncio.Queue[Message] = asyncio.Queue()
    worker = StateProjector(gwy_adapter, queue)

    # Initial boiler setpoint
    msg_22d9 = MockMessage(
        code=Code._22D9,
        verb=Verb.I_,
        payload={SZ_DOMAIN_INDEX: "00", SZ_SETPOINT: 60.0},
        src_id="01:216136",
        dst_id="--:------",
    )
    worker.process_message_state(msg_22d9)
    assert otb_dev.opentherm_state.temperatures.boiler_setpoint == 60.0

    # Act 1: Ingest 2309 for Zone 00 (target 18.0C)
    msg_2309_z00 = MockMessage(
        code=Code._2309,
        verb=Verb.I_,
        payload={SZ_ZONE_INDEX: "00", SZ_SETPOINT: 18.0},
        src_id="01:216136",
        dst_id="--:------",
    )
    worker.process_message_state(msg_2309_z00)

    # Assert 1: Zone 00 updates to 18.0C, OTB boiler setpoint unchanged
    assert zone_00.temp_state.setpoint == 18.0
    assert otb_dev.opentherm_state.temperatures.boiler_setpoint == 60.0

    # Act 2: Ingest 2349 for Zone 01 (target 21.5C)
    msg_2349_z01 = MockMessage(
        code=Code._2349,
        verb=Verb.I_,
        payload={SZ_ZONE_INDEX: "01", SZ_SETPOINT: 21.5},
        src_id="01:216136",
        dst_id="--:------",
    )
    worker.process_message_state(msg_2349_z01)

    # Assert 2: Zone 01 updates to 21.5C, Zone 00 remains 18.0C
    assert zone_01.temp_state.setpoint == 21.5
    assert zone_00.temp_state.setpoint == 18.0
    assert otb_dev.opentherm_state.temperatures.boiler_setpoint == 60.0


def test_issue_989_interleaved_packet_sequence_no_zone_00_oscillation() -> (
    None
):
    # Arrange: System with Zone 00 ("Garden room") and OTB appliance control
    ctl_dev = FakeDevice()
    ctl_dev.id = "01:216136"
    ctl_dev._SLUG = DevType.CTL

    otb_dev = FakeDevice()
    otb_dev.id = "10:064873"
    otb_dev._SLUG = DevType.OTB

    zone_00 = FakeZone("01:216136_00", setpoint=5.0)

    class FakeTcs:
        id = "01:216136"
        appliance_control = otb_dev
        zone_by_index = {"00": zone_00}

    mock_tcs = FakeTcs()
    registry = FakeRegistry(ctl_dev)
    registry.device_by_id[otb_dev.id] = otb_dev
    registry.systems = [mock_tcs]

    gwy_adapter = FakeGatewayAdapter(registry)
    queue: asyncio.Queue[Message] = asyncio.Queue()
    worker = StateProjector(gwy_adapter, queue)

    # Act: Replay exact interleaved sequence from issue #989
    interleaved_msgs = [
        # 1. 22D9 Boiler setpoint 10.0C (idle)
        MockMessage(
            code=Code._22D9,
            verb=Verb.I_,
            payload={SZ_DOMAIN_INDEX: "00", SZ_SETPOINT: 10.0},
            src_id="01:216136",
            dst_id="--:------",
        ),
        # 2. 2309 Zone 00 setpoint sync 5.0C
        MockMessage(
            code=Code._2309,
            verb=Verb.I_,
            payload={SZ_ZONE_INDEX: "00", SZ_SETPOINT: 5.0},
            src_id="01:216136",
            dst_id="--:------",
        ),
        # 3. 22D9 Boiler setpoint 60.0C (firing)
        MockMessage(
            code=Code._22D9,
            verb=Verb.I_,
            payload={SZ_DOMAIN_INDEX: "00", SZ_SETPOINT: 60.0},
            src_id="01:216136",
            dst_id="--:------",
        ),
        # 4. 22D9 Boiler setpoint 10.0C (idle)
        MockMessage(
            code=Code._22D9,
            verb=Verb.I_,
            payload={SZ_DOMAIN_INDEX: "00", SZ_SETPOINT: 10.0},
            src_id="01:216136",
            dst_id="--:------",
        ),
    ]

    for msg in interleaved_msgs:
        worker.process_message_state(msg)
        # Assert: Throughout the entire sequence, Zone 00 setpoint NEVER oscillates
        assert zone_00.temp_state.setpoint == 5.0


def test_000A_zone_config_updates_zone_state() -> None:
    """000A (zone config) populates ZoneState.min_temp/max_temp (issue 1102).

    The CTL broadcasts 000A with zone config arrays.  Without a zone_index
    in to_dict() and a zone state handler, ZoneState.min_temp/max_temp were
    permanently None and ramses_cc fell back to hardcoded 5/35°C.
    """
    ctl_dev = FakeDevice()
    ctl_dev.id = "01:216136"
    ctl_dev._SLUG = DevType.CTL

    zone_03 = FakeZone("01:216136_03")
    mock_tcs = type(
        "FakeTcs",
        (),
        {
            "id": "01:216136",
            "appliance_control": None,
            "zone_by_index": {"03": zone_03},
        },
    )()
    registry = FakeRegistry(ctl_dev)
    registry.systems = [mock_tcs]
    gwy_adapter = FakeGatewayAdapter(registry)
    queue: asyncio.Queue[Message] = asyncio.Queue()
    worker = StateProjector(gwy_adapter, queue)

    msg = MockMessage(
        code=Code._000A,
        verb=Verb.I_,
        payload={
            SZ_ZONE_INDEX: "03",
            SZ_MIN_TEMP: 5.0,
            SZ_MAX_TEMP: 35.0,
            SZ_LOCAL_OVERRIDE: True,
            SZ_OPENWINDOW_FUNCTION: True,
            SZ_MULTIROOM_MODE: False,
        },
        src_id="01:216136",
        dst_id="--:------",
    )
    worker.process_message_state(msg)

    assert zone_03.zone_state.min_temp == 5.0
    assert zone_03.zone_state.max_temp == 35.0
    assert zone_03.zone_state.local_override is True
    assert zone_03.zone_state.openwindow_function is True
    assert zone_03.zone_state.multiroom_mode is False


def test_000A_to_dict_includes_zone_index() -> None:
    """ZoneConfigPayload.to_dict() must include zone_index for routing (issue 1102)."""
    from ramses_rf.payloads.heating import ZoneConfigPayload

    p = ZoneConfigPayload(
        zone_index=0x0B, zone_flags=0x00, min_temp=5.0, max_temp=35.0
    )
    d = p.to_dict()
    assert SZ_ZONE_INDEX in d, "zone_index must be in to_dict for routing"
    assert d[SZ_ZONE_INDEX] == "0B"
    assert d[SZ_MIN_TEMP] == 5.0
    assert d[SZ_MAX_TEMP] == 35.0


def test_0008_relay_demand_populates_tcs_dict() -> None:
    """0008 relay demand populates TCS._relay_demands per domain (issue 1102 / ramses_cc#1026).

    The legacy _handle_msg stored 0008 messages keyed by domain in
    _relay_demands.  After the CQRS migration, the dict was never
    populated, so tcs.relay_demands always returned None.
    """
    from ramses_rf.models import DemandState

    ctl_dev = FakeDevice()
    ctl_dev.id = "01:216136"
    ctl_dev._SLUG = DevType.CTL
    ctl_dev.demand_state = DemandState()

    class FakeTcsWithDicts:
        id = "01:216136"
        appliance_control = None
        zone_by_index: dict[str, Any] = {}
        _heat_demands: dict[str, Any] = {}
        _relay_demands: dict[str, Any] = {}
        _tpi_params: dict[str, Any] = {}

    mock_tcs = FakeTcsWithDicts()
    ctl_dev.tcs = mock_tcs
    registry = FakeRegistry(ctl_dev)
    registry.systems = [mock_tcs]
    gwy_adapter = FakeGatewayAdapter(registry)
    queue: asyncio.Queue[Message] = asyncio.Queue()
    worker = StateProjector(gwy_adapter, queue)

    msg = MockMessage(
        code=Code._0008,
        verb=Verb.I_,
        payload={SZ_DOMAIN_INDEX: "FC", SZ_RELAY_DEMAND: 50.0},
        src_id="01:216136",
        dst_id="--:------",
    )
    worker.process_message_state(msg)

    assert "FC" in mock_tcs._relay_demands, (
        "TCS._relay_demands should be populated with FC domain"
    )


def test_1100_tpi_params_populates_tcs_dict() -> None:
    """1100 TPI params populate TCS._tpi_params dict (issue 1102 / ramses_cc#1026).

    tcs.tpi_params was using the deprecated entity_state.get_value(Code._1100)
    which is never hydrated.  Now it reads from _tpi_params populated by
    the CQRS handler.
    """
    from ramses_rf.models import DemandState

    ctl_dev = FakeDevice()
    ctl_dev.id = "01:216136"
    ctl_dev._SLUG = DevType.CTL
    ctl_dev.demand_state = DemandState()

    class FakeTcsWithTpi:
        id = "01:216136"
        appliance_control = None
        zone_by_index: dict[str, Any] = {}
        _heat_demands: dict[str, Any] = {}
        _relay_demands: dict[str, Any] = {}
        _tpi_params: dict[str, Any] = {}

    mock_tcs = FakeTcsWithTpi()
    ctl_dev.tcs = mock_tcs
    registry = FakeRegistry(ctl_dev)
    registry.systems = [mock_tcs]
    gwy_adapter = FakeGatewayAdapter(registry)
    queue: asyncio.Queue[Message] = asyncio.Queue()
    worker = StateProjector(gwy_adapter, queue)

    msg = MockMessage(
        code=Code._1100,
        verb=Verb.I_,
        payload={
            SZ_DOMAIN_INDEX: "FC",
            "cycle_rate": 6,
            "min_on_time": 1.0,
            "min_off_time": 1.0,
            "proportional_band_width": None,
        },
        src_id="01:216136",
        dst_id="--:------",
    )
    worker.process_message_state(msg)

    assert "FC" in mock_tcs._tpi_params, (
        "TCS._tpi_params should be populated with FC domain"
    )
    assert mock_tcs._tpi_params["FC"]["cycle_rate"] == 6


def test_3ef0_actuator_state_pure_projection() -> None:
    # Arrange
    device = FakeDevice()
    device.id = "13:123456"
    device._SLUG = DevType.BDR
    registry = FakeRegistry(device)
    gwy_adapter = FakeGatewayAdapter(registry)
    queue: asyncio.Queue[Message] = asyncio.Queue()
    worker = StateProjector(gwy_adapter, queue)

    msg = MockMessage(
        code=Code._3EF0,
        verb=Verb.I_,
        payload={
            SZ_MODULATION_LEVEL: 0.75,
            SZ_ACTUATOR_ENABLED: True,
            SZ_CH_ACTIVE: True,
            SZ_DHW_ACTIVE: False,
            SZ_FLAME_ACTIVE: True,
            SZ_ACTUATOR_COUNTDOWN: 120,
            SZ_CYCLE_COUNTDOWN: 600,
            # OpenTherm fields that should NOT be set on ActuatorState
            SZ_CH_SETPOINT: 55.0,
            SZ_MAX_REL_MODULATION: 1.0,
            SZ_CH_ENABLED: True,
        },
        src_id="13:123456",
        dst_id="--:------",
    )

    # Act
    worker.process_message_state(msg)

    # Assert
    assert device.act_state.modulation_level == 0.75
    assert device.act_state.actuator_enabled is True
    assert device.act_state.ch_active is True
    assert device.act_state.dhw_active is False
    assert device.act_state.flame_active is True
    assert device.act_state.actuator_countdown == 120
    assert device.act_state.cycle_countdown == 600
    assert not hasattr(device.act_state, "ch_setpoint")
    assert not hasattr(device.act_state, "ch_enabled")
    assert not hasattr(device.act_state, "cool_active")
    assert not hasattr(device.act_state, "flame_on")


def test_3ef0_opentherm_state_projection() -> None:
    # Arrange
    device = FakeDevice()
    device.id = "10:064873"
    device._SLUG = DevType.OTB
    registry = FakeRegistry(device)
    gwy_adapter = FakeGatewayAdapter(registry)
    queue: asyncio.Queue[Message] = asyncio.Queue()
    worker = StateProjector(gwy_adapter, queue)

    msg = MockMessage(
        code=Code._3EF0,
        verb=Verb.I_,
        payload={
            SZ_MODULATION_LEVEL: 0.6,
            SZ_CH_ACTIVE: True,
            SZ_CH_ENABLED: True,
            SZ_DHW_ACTIVE: False,
            SZ_FLAME_ACTIVE: True,
            SZ_CH_SETPOINT: 60.0,
            SZ_MAX_REL_MODULATION: 0.8,
        },
        src_id="10:064873",
        dst_id="--:------",
    )

    # Act
    worker.process_message_state(msg)

    # Assert
    assert device.opentherm_state.rel_modulation_level == 0.6
    assert device.opentherm_state.max_rel_modulation == 0.8
    assert device.opentherm_state.temperatures.ch_setpoint == 60.0
    assert device.opentherm_state.flags.ch_active is True
    assert device.opentherm_state.flags.ch_enabled is True
    assert device.opentherm_state.flags.dhw_active is False
    assert device.opentherm_state.flags.flame_active is True


def test_opentherm_modulation_bounds_validation() -> None:
    """Verify that valid 3220 modulation values (0.0 <= val <= 1.0) are projected."""
    # Arrange
    device = FakeDevice()
    device._SLUG = DevType.OTB
    registry = FakeRegistry(device)
    gwy_adapter = FakeGatewayAdapter(registry)
    queue: asyncio.Queue[Message] = asyncio.Queue()
    worker = StateProjector(gwy_adapter, queue)

    msg_rel = MockMessage(
        code=Code._3220,
        verb=Verb.RP,
        payload={
            SZ_MESSAGE_ID: int(OtDataId.REL_MODULATION_LEVEL),
            SZ_VALUE: 0.75,
        },
        src_id=device.id,
    )
    msg_max = MockMessage(
        code=Code._3220,
        verb=Verb.RP,
        payload={
            SZ_MESSAGE_ID: int(OtDataId._0E),
            SZ_VALUE: 1.0,
        },
        src_id=device.id,
    )

    # Act
    worker._update_opentherm_state(device, msg_rel.payload, msg_rel)
    worker._update_opentherm_state(device, msg_max.payload, msg_max)

    # Assert
    assert device.opentherm_state.rel_modulation_level == 0.75
    assert device.opentherm_state.max_rel_modulation == 1.0


def test_opentherm_modulation_out_of_bounds_rejected() -> None:
    """Verify that out-of-bounds 3220 modulation values are rejected without mutating state."""
    # Arrange
    device = FakeDevice()
    device._SLUG = DevType.OTB
    registry = FakeRegistry(device)
    gwy_adapter = FakeGatewayAdapter(registry)
    queue: asyncio.Queue[Message] = asyncio.Queue()
    worker = StateProjector(gwy_adapter, queue)

    # Set clean baseline
    device.opentherm_state = OpenThermState(rel_modulation_level=0.5)

    for bad_value in (-0.01, 1.01, 42.5, 100.0):
        mock_msg = MockMessage(
            code=Code._3220,
            verb=Verb.RP,
            payload={
                SZ_MESSAGE_ID: int(OtDataId.REL_MODULATION_LEVEL),
                SZ_VALUE: bad_value,
            },
            src_id=device.id,
        )

        # Act
        worker._update_opentherm_state(device, mock_msg.payload, mock_msg)

        # Assert
        assert device.opentherm_state.rel_modulation_level == 0.5


def test_opentherm_modulation_invalid_types_rejected() -> None:
    """Verify that non-numeric or boolean types for modulation are rejected."""
    # Arrange
    device = FakeDevice()
    device._SLUG = DevType.OTB
    registry = FakeRegistry(device)
    gwy_adapter = FakeGatewayAdapter(registry)
    queue: asyncio.Queue[Message] = asyncio.Queue()
    worker = StateProjector(gwy_adapter, queue)

    device.opentherm_state = OpenThermState(rel_modulation_level=0.5)

    bad_values: list[Any] = [None, True, False, "0.5", [], {}]
    for bad_value in bad_values:
        mock_msg = MockMessage(
            code=Code._3220,
            verb=Verb.RP,
            payload={
                SZ_MESSAGE_ID: int(OtDataId.REL_MODULATION_LEVEL),
                SZ_VALUE: bad_value,
            },
            src_id=device.id,
        )

        # Act
        worker._update_opentherm_state(device, mock_msg.payload, mock_msg)

        # Assert
        assert device.opentherm_state.rel_modulation_level == 0.5


def test_controller_3ef0_precedence_over_invalid_3220() -> None:
    """Verify that controller 3EF0 modulation is retained when invalid 3220 arrives."""
    # Arrange
    device = FakeDevice()
    device.id = "10:064873"
    device._SLUG = DevType.OTB
    registry = FakeRegistry(device)
    gwy_adapter = FakeGatewayAdapter(registry)
    queue: asyncio.Queue[Message] = asyncio.Queue()
    worker = StateProjector(gwy_adapter, queue)

    msg_3ef0 = MockMessage(
        code=Code._3EF0,
        verb=Verb.I_,
        payload={SZ_MODULATION_LEVEL: 0.65},
        src_id="10:064873",
    )
    msg_invalid_3220 = MockMessage(
        code=Code._3220,
        verb=Verb.RP,
        payload={
            SZ_MESSAGE_ID: int(OtDataId.REL_MODULATION_LEVEL),
            SZ_VALUE: 42.5,
        },
        src_id="10:064873",
    )
    msg_valid_3220 = MockMessage(
        code=Code._3220,
        verb=Verb.RP,
        payload={
            SZ_MESSAGE_ID: int(OtDataId.REL_MODULATION_LEVEL),
            SZ_VALUE: 0.40,
        },
        src_id="10:064873",
    )

    # Act & Assert 1: Controller broadcast establishes 0.65
    worker.process_message_state(msg_3ef0)
    assert device.opentherm_state.rel_modulation_level == 0.65

    # Act & Assert 2: Invalid 3220 frame is rejected, retaining 0.65
    worker.process_message_state(msg_invalid_3220)
    assert device.opentherm_state.rel_modulation_level == 0.65

    # Act & Assert 3: Valid 3220 frame updates to 0.40
    worker.process_message_state(msg_valid_3220)
    assert device.opentherm_state.rel_modulation_level == 0.40


def test_opentherm_1fd0_diagnostics_projection() -> None:
    """Verify that Opcode 1FD0 diagnostic code updates oem_code and last_updated."""
    # Arrange
    device = FakeDevice()
    device._SLUG = DevType.OTB
    registry = FakeRegistry(device)
    gwy_adapter = FakeGatewayAdapter(registry)
    queue: asyncio.Queue[Message] = asyncio.Queue()
    worker = StateProjector(gwy_adapter, queue)

    msg = MockMessage(
        code=Code._1FD0,
        verb=Verb.I_,
        payload={SZ_DIAGNOSTIC_CODE: 115, SZ_FLAGS: 0},
        src_id=device.id,
    )

    # Act
    worker._update_opentherm_state(device, msg.payload, msg)

    # Assert
    assert device.opentherm_state.oem_code == 115
    assert len(device.events) == 1
    assert device.events[0].entity_id == device.id


def test_opentherm_1fd4_ticker_projection() -> None:
    """Verify that Opcode 1FD4 slave ticker refreshes OpenThermState.last_updated."""
    # Arrange
    device = FakeDevice()
    device._SLUG = DevType.OTB
    registry = FakeRegistry(device)
    gwy_adapter = FakeGatewayAdapter(registry)
    queue: asyncio.Queue[Message] = asyncio.Queue()
    worker = StateProjector(gwy_adapter, queue)

    msg = MockMessage(
        code=Code._1FD4,
        verb=Verb.I_,
        payload={SZ_TICKER: 23294},
        src_id=device.id,
    )

    # Act
    worker._update_opentherm_state(device, msg.payload, msg)

    # Assert
    assert len(device.events) == 1
    assert device.events[0].entity_id == device.id


def test_opentherm_ingestion_ignores_non_otb_devices() -> None:
    """Verify that non-OTB devices (BDR, TRV, UFC, FAN) are untouched by OpenTherm ingestion."""
    # Arrange
    for slug in (DevType.BDR, DevType.TRV, DevType.UFC, DevType.FAN):
        device = FakeNonOtbDevice(slug)
        registry = FakeRegistry(device)
        gwy_adapter = FakeGatewayAdapter(registry)
        queue: asyncio.Queue[Message] = asyncio.Queue()
        worker = StateProjector(gwy_adapter, queue)

        msg_3220 = MockMessage(
            code=Code._3220,
            verb=Verb.RP,
            payload={
                SZ_MESSAGE_ID: int(OtDataId.REL_MODULATION_LEVEL),
                SZ_VALUE: 0.5,
            },
            src_id=device.id,
        )
        msg_1fd0 = MockMessage(
            code=Code._1FD0,
            verb=Verb.I_,
            payload={SZ_DIAGNOSTIC_CODE: 115, SZ_FLAGS: 0},
            src_id=device.id,
        )

        # Act
        worker._update_opentherm_state(device, msg_3220.payload, msg_3220)
        worker._update_opentherm_state(device, msg_1fd0.payload, msg_1fd0)

        # Assert
        assert not hasattr(device, "opentherm_state")
        assert len(device.events) == 0


def test_controller_3ef1_broadcast_ingestion() -> None:
    """Verify that controller 3EF1 broadcasts update modulation, setpoints, and flags."""
    # Arrange
    device = FakeDevice()
    device.id = "10:064873"
    device._SLUG = DevType.OTB
    registry = FakeRegistry(device)
    gwy_adapter = FakeGatewayAdapter(registry)
    queue: asyncio.Queue[Message] = asyncio.Queue()
    worker = StateProjector(gwy_adapter, queue)

    msg = MockMessage(
        code=Code._3EF1,
        verb=Verb.I_,
        payload={
            SZ_MODULATION_LEVEL: 0.45,
            SZ_CH_ACTIVE: True,
            SZ_CH_ENABLED: True,
            SZ_DHW_ACTIVE: True,
            SZ_FLAME_ACTIVE: True,
            SZ_CH_SETPOINT: 55.0,
            SZ_MAX_REL_MODULATION: 0.9,
        },
        src_id="10:064873",
        dst_id="--:------",
    )

    # Act
    worker.process_message_state(msg)

    # Assert
    assert device.opentherm_state.rel_modulation_level == 0.45
    assert device.opentherm_state.max_rel_modulation == 0.9
    assert device.opentherm_state.temperatures.ch_setpoint == 55.0
    assert device.opentherm_state.flags.ch_active is True
    assert device.opentherm_state.flags.ch_enabled is True
    assert device.opentherm_state.flags.dhw_active is True
    assert device.opentherm_state.flags.flame_active is True


def test_opentherm_counters_projection() -> None:
    """Verify that Opcode 3220 counter messages update OpenThermState.counters."""
    # Arrange
    device = FakeDevice()
    device._SLUG = DevType.OTB
    registry = FakeRegistry(device)
    gwy_adapter = FakeGatewayAdapter(registry)
    queue: asyncio.Queue[Message] = asyncio.Queue()
    worker = StateProjector(gwy_adapter, queue)

    msg_burner_starts = MockMessage(
        code=Code._3220,
        verb=Verb.RP,
        payload={
            SZ_MESSAGE_ID: int(OtDataId.BURNER_STARTS),
            SZ_VALUE: 1250,
        },
        src_id=device.id,
    )
    msg_pump_hours = MockMessage(
        code=Code._3220,
        verb=Verb.RP,
        payload={
            SZ_MESSAGE_ID: int(OtDataId.CH_PUMP_HOURS),
            SZ_VALUE: 340,
        },
        src_id=device.id,
    )

    # Act
    worker._update_opentherm_state(
        device, msg_burner_starts.payload, msg_burner_starts
    )
    worker._update_opentherm_state(
        device, msg_pump_hours.payload, msg_pump_hours
    )

    # Assert
    assert device.opentherm_state.counters.burner_starts == 1250
    assert device.opentherm_state.counters.ch_pump_hours == 340


class _LivenessDevice:
    """A minimal device exposing the liveness attributes under test."""

    def __init__(self, device_id: str, missed_polls: int = 0) -> None:
        """Initialize the device with liveness tracking attributes.

        :param device_id: The hardware ID string.
        :type device_id: str
        :param missed_polls: Initial consecutive missed poll count.
        :type missed_polls: int
        """
        self.id: str = device_id
        self._SLUG: str = DevType.FAN
        self._last_msg_dtm: dt | None = None
        self._last_msg: MockMessage | None = None
        self._missed_polls: int = missed_polls


@pytest.mark.asyncio
async def test_process_state_updates_src_only_liveness() -> None:
    """Only the source device proves liveness; the destination does not."""
    # Arrange — a FAN with pending missed polls
    fan_dev = _LivenessDevice("32:153289", missed_polls=3)
    registry = FakeRegistry(fan_dev)
    gwy_adapter = FakeGatewayAdapter(registry)

    # Act — a message merely addressed to the FAN (e.g. a poll echo)
    echo_msg = MockMessage(
        code=Code._10E0,
        verb=Verb.RQ,
        payload={},
        src_id="18:130236",
        dst_id="32:153289",
    )
    echo_msg.payload = None  # early return; liveness block already ran
    await process_state_updates(gwy_adapter, echo_msg)

    # Assert — destination involvement does not prove the FAN is alive
    assert fan_dev._last_msg_dtm is None
    assert fan_dev._last_msg is None
    assert fan_dev._missed_polls == 3

    # Act — a message transmitted by the FAN itself
    reply_msg = MockMessage(
        code=Code._10E0,
        verb=Verb.RP,
        payload={},
        src_id="32:153289",
        dst_id="18:130236",
    )
    reply_msg.payload = None
    await process_state_updates(gwy_adapter, reply_msg)

    # Assert — source involvement proves liveness and resets the count
    assert fan_dev._last_msg_dtm == reply_msg.dtm
    # cast to object: the earlier `is None` assert narrows the member to
    # None, and mypy marks any `is`/`==` with reply_msg as unreachable
    assert cast(object, fan_dev._last_msg) is reply_msg
    assert fan_dev._missed_polls == 0


@pytest.mark.asyncio
async def test_process_state_updates_liveness_is_monotonic() -> None:
    """A stale packet completing out of order must not regress liveness.

    Cached-packet replay at gateway restore feeds packets through
    fire-and-forget handler tasks whose completion order does not match
    packet order; without the monotonic guard the oldest packet's stamp
    wins, leaving ``_last_msg_dtm`` stale and flapping the device to
    unavailable until the next live broadcast (ramses_cc issue 1278).
    """
    # Arrange — a FAN that already holds a fresh stamp
    fresh_dtm = dt.now()
    stale_dtm = fresh_dtm - td(hours=3)
    fan_dev = _LivenessDevice("32:153289", missed_polls=3)
    fan_dev._last_msg_dtm = fresh_dtm
    gwy_adapter = FakeGatewayAdapter(FakeRegistry(fan_dev))

    # Act — an older packet completes after the fresh one
    stale_msg = MockMessage(
        code=Code._10E0,
        verb=Verb.RP,
        payload={},
        src_id="32:153289",
        dtm=stale_dtm,
    )
    stale_msg.payload = None
    await process_state_updates(gwy_adapter, stale_msg)

    # Assert — the liveness triple is not rolled back
    assert fan_dev._last_msg_dtm == fresh_dtm
    assert fan_dev._last_msg is None
    assert fan_dev._missed_polls == 3

    # Act — a fresher packet still updates normally
    newer_msg = MockMessage(
        code=Code._10E0,
        verb=Verb.RP,
        payload={},
        src_id="32:153289",
        dtm=fresh_dtm + td(minutes=1),
    )
    newer_msg.payload = None
    await process_state_updates(gwy_adapter, newer_msg)

    assert fan_dev._last_msg_dtm == newer_msg.dtm
    assert cast(object, fan_dev._last_msg) is newer_msg
    assert fan_dev._missed_polls == 0


@pytest.mark.asyncio
async def test_process_state_updates_prestamped_dtm_still_stamps() -> None:
    """An equal stored stamp must not block the rest of the triple.

    ``_mark_src_device_alive()`` in the dispatcher stamps
    ``_last_msg_dtm`` before validation, so when a live packet reaches
    ``process_state_updates`` the stored stamp already equals
    ``msg.dtm``; a strictly-newer gate then leaves ``_last_msg`` unset
    and ``_missed_polls`` counting up forever (ramses_rf issue 1262).
    """
    # Arrange — the dispatcher's early liveness stamp has already run
    fan_dev = _LivenessDevice("32:153289", missed_polls=3)
    live_msg = MockMessage(
        code=Code._10E0,
        verb=Verb.RP,
        payload={},
        src_id="32:153289",
        dst_id="18:130236",
    )
    live_msg.payload = None
    fan_dev._last_msg_dtm = live_msg.dtm
    gwy_adapter = FakeGatewayAdapter(FakeRegistry(fan_dev))

    # Act — the same live packet completes the state-update path
    await process_state_updates(gwy_adapter, live_msg)

    # Assert — the remainder of the liveness triple still applies
    assert fan_dev._last_msg_dtm == live_msg.dtm
    assert cast(object, fan_dev._last_msg) is live_msg
    assert fan_dev._missed_polls == 0


# --- 22F1 scheme-aware mode remap + last_fan_mode_dtm (issue 1216) ---


def _hvac_gateway() -> MagicMock:
    """Return a minimal mocked Gateway for real HVAC device twins.

    :return: A MagicMock specced to Gateway.
    :rtype: MagicMock
    """
    gwy = MagicMock(spec=Gateway)
    gwy.tzinfo = None
    return gwy


def update_hvac_state(
    target: Any, payload: dict[str, Any], msg: Message
) -> None:
    """Feed a payload through both live hvac-state ingestion paths.

    Production runs the state_projector module function (via
    ``process_state_updates``) and then the ingestion StateProjector
    method, so the fix must hold under both.

    :param target: The device twin to update.
    :type target: Any
    :param payload: The decoded payload dict.
    :type payload: dict[str, Any]
    :param msg: The message envelope.
    :type msg: Message
    :rtype: None
    """
    _update_hvac_state(target, payload, msg)
    worker = StateProjector(
        FakeGatewayAdapter(FakeRegistry(FakeDevice())), asyncio.Queue()
    )
    worker._update_hvac_state(target, payload, msg)


def _22f1_payload(mode_index: str, mode_max: str = "04") -> dict[str, Any]:
    """Return a 22F1 payload as the mode_max heuristic parser emits it.

    :param mode_index: The transmitted mode index hex byte.
    :type mode_index: str
    :param mode_max: The transmitted mode_max byte ("04" => itho names).
    :type mode_max: str
    :return: A payload dict shaped like HvacFanModePayload.to_dict().
    :rtype: dict[str, Any]
    """
    heuristic_names = {
        ("01", "04"): "auto",
        ("02", "04"): "low",
        ("03", "07"): "high",
    }
    return {
        SZ_FAN_MODE: heuristic_names.get((mode_index, mode_max), mode_index),
        "_mode_index": mode_index,
        "_mode_max": mode_max,
        "_scheme": "itho" if mode_max == "04" else "orcon",
    }


def _22f1_msg(payload: dict[str, Any], src_id: str) -> MockMessage:
    """Return a 22F1 command message sourced by a remote.

    :param payload: The decoded payload dict.
    :type payload: dict[str, Any]
    :param src_id: The remote's device ID.
    :type src_id: str
    :return: A MockMessage envelope.
    :rtype: MockMessage
    """
    return MockMessage(
        code=Code._22F1,
        verb=Verb.I_,
        payload=payload,
        src_id=src_id,
        dst_id="32:153289",
    )


def test_22f1_fan_target_uses_configured_scheme() -> None:
    """A configured FAN scheme overrides the mode_max name heuristic."""
    fan = HvacVentilator(_hvac_gateway(), Address("32:153289"))
    fan.set_strategy(OrconStrategy())

    for mode_index, expected in (("01", "low"), ("02", "medium")):
        msg = _22f1_msg(_22f1_payload(mode_index), "29:176861")
        update_hvac_state(fan, dict(msg.payload), msg)
        assert fan.hvac_state.fan_mode == expected


def test_22f1_remote_target_uses_bound_fan_scheme() -> None:
    """A remote's own state uses the bound FAN's scheme vocabulary."""
    fan = HvacVentilator(_hvac_gateway(), Address("32:153289"))
    fan.set_strategy(OrconStrategy())
    rem = HvacRemote(_hvac_gateway(), Address("29:176861"))
    rem._parent_fan = fan

    msg = _22f1_msg(_22f1_payload("01"), rem.id)
    update_hvac_state(rem, dict(msg.payload), msg)
    assert rem.hvac_state.fan_mode == "low"

    msg = _22f1_msg(_22f1_payload("02"), rem.id)
    update_hvac_state(rem, dict(msg.payload), msg)
    assert rem.hvac_state.fan_mode == "medium"


def test_22f1_remote_without_scheme_keeps_heuristic() -> None:
    """Without a configured scheme the mode_max heuristic is unchanged."""
    rem = HvacRemote(_hvac_gateway(), Address("29:176861"))

    msg = _22f1_msg(_22f1_payload("01"), rem.id)
    update_hvac_state(rem, dict(msg.payload), msg)
    assert rem.hvac_state.fan_mode == "auto"  # mode_max=04 => itho map

    fan = HvacVentilator(_hvac_gateway(), Address("32:153289"))
    msg = _22f1_msg(_22f1_payload("01"), "29:176861")
    update_hvac_state(fan, dict(msg.payload), msg)
    assert fan.hvac_state.fan_mode == "auto"


def test_22f1_mode_max_07_still_decodes_orcon() -> None:
    """mode_max=07 already maps to orcon names; scheme remap is a no-op."""
    fan = HvacVentilator(_hvac_gateway(), Address("32:153289"))
    fan.set_strategy(OrconStrategy())

    msg = _22f1_msg(_22f1_payload("03", mode_max="07"), "37:168270")
    update_hvac_state(fan, dict(msg.payload), msg)
    assert fan.hvac_state.fan_mode == "high"


def test_22f1_unknown_index_keeps_heuristic_name() -> None:
    """An index absent from the scheme map keeps the parsed name."""
    fan = HvacVentilator(_hvac_gateway(), Address("32:153289"))
    fan.set_strategy(OrconStrategy())

    payload = _22f1_payload("09")
    payload[SZ_FAN_MODE] = "09"  # heuristic falls back to raw hex
    msg = _22f1_msg(payload, "29:176861")
    update_hvac_state(fan, dict(msg.payload), msg)
    # "09" is not in the orcon map; the raw-hex fan_mode is then dropped
    # by the non-semantic hex filter, leaving the previous value (None)
    assert fan.hvac_state.fan_mode is None


def test_last_fan_mode_dtm_stamped_on_fan_mode_update() -> None:
    """_last_fan_mode_dtm tracks only messages that write fan_mode."""
    rem = HvacRemote(_hvac_gateway(), Address("29:176861"))
    assert rem.last_fan_mode_dtm is None

    msg = _22f1_msg(_22f1_payload("01"), rem.id)
    update_hvac_state(rem, dict(msg.payload), msg)
    assert rem.last_fan_mode_dtm == msg.dtm

    # a non-fan_mode HVAC payload must not move the stamp
    msg2 = MockMessage(
        code=Code._22F3,
        verb=Verb.I_,
        payload={"remaining_mins": 15},
        src_id=rem.id,
    )
    update_hvac_state(rem, dict(msg2.payload), msg2)
    assert rem.last_fan_mode_dtm == msg.dtm


# --- 31D9 Orcon 3-byte mode decode (ramses-rf/ramses_cc#1231, MVS-15) ---


def _31d9_msg(speed_byte: int, src_id: str) -> MockMessage:
    """Return a 3-byte 31D9 broadcast decoded by the real parser.

    :param speed_byte: The third payload byte (mode index on Orcon).
    :type speed_byte: int
    :param src_id: The FAN's device ID.
    :type src_id: str
    :return: A MockMessage envelope carrying the parsed payload.
    :rtype: MockMessage
    """
    msg = MockMessage(
        code=Code._31D9,
        verb=Verb.I_,
        payload={},
        src_id=src_id,
        dst_id=src_id,
    )
    msg.payload = HvacBypassStatePayload.from_bytes(
        bytes((0x00, 0x00, speed_byte))
    ).to_dict(msg)
    return msg


@pytest.mark.parametrize(
    ("speed_byte", "expected"),
    [
        (0x00, "away"),
        (0x01, "low"),
        (0x02, "medium"),
        (0x03, "high"),  # also what a timed boost reports
        (0x04, "auto"),
    ],
)
def test_31d9_orcon_3byte_modes(speed_byte: int, expected: str) -> None:
    """A configured Orcon FAN decodes 3-byte 31D9 via its mode map."""
    fan = HvacVentilator(_hvac_gateway(), Address("32:231021"))
    fan.set_strategy(OrconStrategy())

    msg = _31d9_msg(speed_byte, fan.id)
    update_hvac_state(fan, dict(msg.payload), msg)

    assert fan.hvac_state.fan_mode == expected
    assert fan.hvac_state.exhaust_fan_speed is None


def test_31d9_3byte_without_scheme_keeps_generic_decode() -> None:
    """Unconfigured FANs keep the Vasco-style 31D9 decode."""
    fan = HvacVentilator(_hvac_gateway(), Address("32:231021"))

    msg = _31d9_msg(0x01, fan.id)
    update_hvac_state(fan, dict(msg.payload), msg)

    assert fan.hvac_state.fan_mode == "1 (trickle)"
    assert fan.hvac_state.exhaust_fan_speed == 0.005
