"""Tests for Underfloor Heating (UFH) Topology Graph and Circuit-to-Zone Models."""

from __future__ import annotations

from typing import cast
from unittest.mock import MagicMock

import pytest

from ramses_rf.const import (
    SZ_DEVICES,
    SZ_UFH_INDEX,
    SZ_ZONE_INDEX,
    SZ_ZONE_TYPE,
    ZON_ROLE_MAP,
)
from ramses_rf.devices import Controller
from ramses_rf.devices.dev_registry import DeviceRegistry
from ramses_rf.devices.heat_controllers import UfhController
from ramses_rf.enums import ThermalMode, TopologyAction
from ramses_rf.messages.core import Message
from ramses_rf.models import (
    TopologyChangedEvent,
    UfhCircuitDTO,
    UfhCircuitState,
    UfhState,
)
from ramses_rf.pipeline.topology_handlers.ufh import UfhTopologyHandler
from ramses_rf.systems.tcs import Evohome
from ramses_rf.systems.zones import UfhZone, Zone
from ramses_rf.topology_builder import update_topology_schema_state
from ramses_tx.address import Address
from ramses_tx.const import Code, Verb
from ramses_tx.typing import DeviceIdT


def _make_mock_system(
    ctl_id: str = "01:145038", ufc_id: str = "02:000921"
) -> tuple[Evohome, UfhController]:
    """Helper to assemble a connected Evohome TCS and UfhController hierarchy."""
    gwy = MagicMock()
    msg_store = MagicMock()
    gwy.msg_transport = None
    gwy._msg_db = None
    gwy._transport = None
    gwy.message_store = msg_store
    gwy.config.known_list = {}
    gwy.config.block_list = {}
    gwy.config.max_zones = 16
    gwy.device_registry.system_by_id = {}

    ctl = Controller(gwy, Address(ctl_id))
    tcs = Evohome(ctl)
    ctl.tcs = tcs
    tcs._max_zones = 16
    tcs.zone_by_index = {}
    tcs.child_by_id = {}
    tcs.childs = []

    ufc = UfhController(gwy, Address(ufc_id))
    ufc.tcs = tcs
    tcs.childs.append(ufc)
    tcs.child_by_id[ufc.id] = ufc

    return tcs, ufc


def test_zone_circuits_empty_default_on_base_zone() -> None:
    # Arrange
    tcs, _ = _make_mock_system()
    base_zone = Zone(tcs, "00")

    # Act
    circuits = base_zone.circuits
    entities = base_zone.circuit_entities

    # Assert
    assert isinstance(circuits, list)
    assert len(circuits) == 0
    assert isinstance(entities, list)
    assert len(entities) == 0


def test_ufh_zone_multi_circuit_single_zone_binding() -> None:
    # Arrange
    tcs, ufc = _make_mock_system("01:145038", "02:044435")
    ufh_zone = UfhZone(tcs, "00")
    tcs.zone_by_index["00"] = ufh_zone

    circuit_00 = ufc.get_circuit("00")
    circuit_01 = ufc.get_circuit("01")
    circuit_02 = ufc.get_circuit("02")

    circuit_00.set_zone(ufh_zone)
    circuit_01.set_zone(ufh_zone)
    circuit_02.set_zone(ufh_zone)

    # Act
    circuit_entities = ufh_zone.circuit_entities
    circuits_dto = ufh_zone.circuits

    # Assert
    assert len(circuit_entities) == 3
    assert circuit_entities == [circuit_00, circuit_01, circuit_02]

    assert len(circuits_dto) == 3
    assert all(isinstance(dto, UfhCircuitDTO) for dto in circuits_dto)
    assert [dto.ufh_index for dto in circuits_dto] == ["00", "01", "02"]
    assert all(dto.zone_index == "00" for dto in circuits_dto)


def test_ufh_zone_multi_zone_distribution() -> None:
    # Arrange: Single UFC (02:044446) with circuits assigned to different zones
    tcs, ufc = _make_mock_system("01:145038", "02:044446")

    zone_01 = UfhZone(tcs, "01")
    zone_04 = UfhZone(tcs, "04")
    zone_05 = UfhZone(tcs, "05")
    zone_06 = UfhZone(tcs, "06")

    tcs.zone_by_index["01"] = zone_01
    tcs.zone_by_index["04"] = zone_04
    tcs.zone_by_index["05"] = zone_05
    tcs.zone_by_index["06"] = zone_06

    circuit_00 = ufc.get_circuit("00")
    circuit_01 = ufc.get_circuit("01")
    circuit_02 = ufc.get_circuit("02")
    circuit_03 = ufc.get_circuit("03")
    circuit_04 = ufc.get_circuit("04")

    circuit_00.set_zone(zone_04)
    circuit_01.set_zone(zone_05)
    circuit_02.set_zone(zone_01)
    circuit_03.set_zone(zone_01)
    circuit_04.set_zone(zone_06)

    # Act & Assert - Zone 01 (should have circuits 02 and 03)
    assert [dto.ufh_index for dto in zone_01.circuits] == ["02", "03"]
    assert [c.ufh_index for c in zone_01.circuit_entities] == ["02", "03"]

    # Act & Assert - Zone 04 (should have circuit 00)
    assert [dto.ufh_index for dto in zone_04.circuits] == ["00"]

    # Act & Assert - Zone 05 (should have circuit 01)
    assert [dto.ufh_index for dto in zone_05.circuits] == ["01"]

    # Act & Assert - Zone 06 (should have circuit 04)
    assert [dto.ufh_index for dto in zone_06.circuits] == ["04"]


def test_ufh_zone_bidirectional_linkage() -> None:
    # Arrange
    tcs, ufc = _make_mock_system("01:145038", "02:000921")
    zone = UfhZone(tcs, "03")
    tcs.zone_by_index["03"] = zone

    circuit = ufc.get_circuit("01")

    # Act
    circuit.set_zone(zone)

    # Assert
    assert circuit.zone_index == "03"
    assert circuit.zone is zone
    assert len(zone.circuits) == 1
    assert zone.circuits[0].ufh_index == "01"
    assert zone.circuits[0].zone_index == "03"


def test_ufh_zone_dynamic_telemetry_reflection() -> None:
    # Arrange
    tcs, ufc = _make_mock_system("01:145038", "02:000921")
    zone = UfhZone(tcs, "02")
    tcs.zone_by_index["02"] = zone

    circuit = ufc.get_circuit("00")
    circuit.set_zone(zone)

    # Act - Update UFC CQRS state
    ufc.ufh_state = UfhState(
        circuits={
            "00": UfhCircuitState(
                ufh_index="00",
                zone_index="02",
                heat_demand=0.75,
                cooling_demand=0.10,
                circuit_mode=ThermalMode.HEAT,
                setpoint=21.5,
            )
        }
    )

    # Assert
    assert len(zone.circuits) == 1
    dto = zone.circuits[0]
    assert dto.ufh_index == "00"
    assert dto.zone_index == "02"
    assert dto.heat_demand == 0.75
    assert dto.cooling_demand == 0.10
    assert dto.circuit_mode == ThermalMode.HEAT
    assert dto.setpoint == 21.5


def test_ufh_zone_unassigned_circuits_filtered() -> None:
    # Arrange
    tcs, ufc = _make_mock_system("01:145038", "02:000921")
    zone = UfhZone(tcs, "01")
    tcs.zone_by_index["01"] = zone

    circuit_00 = ufc.get_circuit("00")  # Unassigned
    circuit_01 = ufc.get_circuit("01")  # Assigned to Zone 01
    circuit_01.set_zone(zone)

    # Act & Assert
    assert len(zone.circuits) == 1
    assert zone.circuits[0].ufh_index == "01"
    assert circuit_00.zone_index is None
    assert circuit_00 not in zone.circuit_entities


def test_ufh_topology_handler_000c_from_ufc_emits_create_circuit() -> None:
    # Arrange
    emitted_events: list[TopologyChangedEvent] = []
    handler = UfhTopologyHandler(emit_event_cb=emitted_events.append)

    mock_msg = MagicMock(spec=Message)
    mock_msg.header = MagicMock(code=Code._000C, verb=Verb.RP)
    mock_msg.src = Address("02:044435")
    mock_msg.dst = Address("18:200214")
    mock_msg.addr3 = Address("--:------")
    mock_msg.data = {
        SZ_ZONE_TYPE: ZON_ROLE_MAP.UFH,
        SZ_UFH_INDEX: "00",
        SZ_ZONE_INDEX: "08",
        SZ_DEVICES: ["01:073976"],
    }
    mock_msg.payload = [
        {
            SZ_ZONE_TYPE: ZON_ROLE_MAP.UFH,
            SZ_UFH_INDEX: "00",
            SZ_ZONE_INDEX: "08",
            SZ_DEVICES: ["01:073976"],
        }
    ]

    # Act
    handler.consume(mock_msg)

    # Assert
    circuit_events = [
        e for e in emitted_events if e.action == TopologyAction.CREATE_CIRCUIT
    ]
    assert len(circuit_events) == 1
    event = circuit_events[0]
    assert event.device_id == "02:044435"
    assert event.metadata[SZ_UFH_INDEX] == "00"
    assert event.metadata[SZ_ZONE_INDEX] == "08"


def test_device_registry_handle_create_circuit_wires_zone() -> None:
    # Arrange
    gwy = MagicMock()
    gwy.config.known_list = {}
    gwy.config.block_list = {}
    filter_mock = MagicMock()
    filter_mock.is_allowed.return_value = True

    registry = DeviceRegistry(
        device_filter=filter_mock,
        config=gwy.config,
        device_factory_cb=MagicMock(),
    )
    tcs, ufc = _make_mock_system("01:145038", "02:044435")
    registry.device_by_id[cast(DeviceIdT, "01:145038")] = tcs.ctl
    registry.device_by_id[cast(DeviceIdT, "02:044435")] = ufc

    zone = UfhZone(tcs, "05")
    tcs.zone_by_index["05"] = zone

    event = TopologyChangedEvent(
        action=TopologyAction.CREATE_CIRCUIT,
        device_id=cast(DeviceIdT, "02:044435"),
        metadata={
            SZ_UFH_INDEX: "01",
            SZ_ZONE_INDEX: "05",
            "tcs_id": "01:145038",
        },
        causation="Rule_UFH_000C_Circuit",
    )

    # Act
    registry._handle_create_circuit(event)

    # Assert
    circuit = ufc.get_circuit("01")
    assert circuit.zone is zone
    assert circuit.zone_index == "05"
    assert "01:145038_05" in registry._cqrs_actuators
    assert circuit.id in registry._cqrs_actuators["01:145038_05"]
    assert len(zone.circuits) == 1
    assert zone.circuits[0].ufh_index == "01"


@pytest.mark.asyncio
async def test_topology_builder_build_zone_wires_circuits() -> None:
    # Arrange
    gwy = MagicMock()
    gwy.config.known_list = {}
    gwy.config.block_list = {}
    gwy.config.enable_eavesdrop = False

    tcs, ufc = _make_mock_system("01:145038", "02:044435")
    zone = UfhZone(tcs, "02")
    tcs.zone_by_index["02"] = zone
    gwy.tcs = tcs
    gwy.device_registry.device_by_id = {
        cast(DeviceIdT, "01:145038"): tcs.ctl,
        cast(DeviceIdT, "02:044435"): ufc,
    }

    mock_msg = MagicMock(spec=Message)
    mock_msg.code = Code._000C
    mock_msg.src = Address("02:044435")
    mock_msg.dst = Address("18:200214")

    payload = {
        SZ_ZONE_INDEX: "02",
        SZ_ZONE_TYPE: "09",
        SZ_UFH_INDEX: "03",
        SZ_DEVICES: ["01:145038"],
    }

    # Act
    await update_topology_schema_state(gwy, payload, mock_msg)

    # Assert
    circuit = ufc.get_circuit("03")
    assert circuit.zone is zone
    assert circuit.zone_index == "02"
    assert len(zone.circuits) == 1
    assert zone.circuits[0].ufh_index == "03"
