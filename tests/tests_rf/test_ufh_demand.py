"""Tests for Underfloor Heating (UFH) demand aggregation and schema hydration."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from ramses_rf.const import (
    SZ_CIRCUITS,
    SZ_ZONE_INDEX,
    SZ_ZONES,
)
from ramses_rf.devices import Controller
from ramses_rf.devices.heat_controllers import UfhController
from ramses_rf.enums import ThermalMode
from ramses_rf.models import (
    DemandState,
    ThermalDemandDTO,
    UfhCircuitState,
    UfhState,
)
from ramses_rf.schemas import SZ_CLASS, SZ_UFH_SYSTEM, load_tcs
from ramses_rf.systems.tcs import Evohome
from ramses_rf.systems.zones import UfhZone
from ramses_rf.topology import Parent
from ramses_tx.address import Address
from ramses_tx.const import FF
from ramses_tx.typing import DeviceIdT


def _make_mock_ufh_system(
    ctl_id: str = "01:145038", ufc_id: str = "02:000921"
) -> tuple[MagicMock, Evohome, UfhController]:
    """Assemble a mock Gateway, Evohome TCS, and UfhController hierarchy."""
    gateway = MagicMock()
    gateway.config.known_list = {}
    gateway.config.block_list = {}
    gateway.config.max_zones = 16
    gateway.device_registry.system_by_id = {}
    gateway.device_registry._cqrs_actuators = {}
    gateway._engine._enforce_known_list = False
    gateway._engine._exclude = []
    gateway._engine._include = []

    controller = Controller(gateway, Address(ctl_id))
    tcs = Evohome(controller)
    controller.tcs = tcs
    tcs._max_zones = 16
    tcs.zone_by_index = {}
    tcs.child_by_id = {}
    tcs.childs = []
    gateway.device_registry.system_by_id[controller.id] = tcs

    ufc = UfhController(gateway, Address(ufc_id))
    ufc.tcs = tcs
    tcs.childs.append(ufc)
    tcs.child_by_id[ufc.id] = ufc

    gateway.device_registry.devices = [controller, ufc]
    gateway.device_registry.get_device = lambda device_id, **kwargs: (
        ufc if str(device_id) == ufc_id else controller
    )

    return gateway, tcs, ufc


@pytest.mark.asyncio
async def test_ufh_zone_heat_demand_empty_circuits_fallback() -> None:
    # Arrange
    _, tcs, _ = _make_mock_ufh_system()
    zone = UfhZone(tcs, "00")
    tcs.zone_by_index["00"] = zone

    # Act: No circuits and no demand state
    demand_none = await zone.heat_demand()

    # Assert
    assert demand_none is None

    # Act: Fallback to demand_state when present
    zone.demand_state = DemandState(heat_demand=0.45)
    demand_fallback = await zone.heat_demand()

    # Assert
    assert demand_fallback == 0.45


@pytest.mark.asyncio
async def test_ufh_zone_heat_demand_zero_not_none() -> None:
    # Arrange
    _, tcs, ufc = _make_mock_ufh_system()
    zone = UfhZone(tcs, "00")
    tcs.zone_by_index["00"] = zone

    circuit = ufc.get_circuit("00")
    circuit.set_zone(zone)

    ufc.ufh_state = UfhState(
        circuits={
            "00": UfhCircuitState(
                ufh_index="00",
                zone_index="00",
                heat_demand=0.0,
            )
        }
    )

    # Act
    demand = await zone.heat_demand()

    # Assert
    assert demand is not None
    assert demand == 0.0


@pytest.mark.asyncio
async def test_ufh_zone_heat_demand_max_aggregation() -> None:
    # Arrange: Multi-circuit zone bound to circuits 00, 01, and 02
    _, tcs, ufc = _make_mock_ufh_system()
    zone = UfhZone(tcs, "00")
    tcs.zone_by_index["00"] = zone

    circuit_00 = ufc.get_circuit("00")
    circuit_01 = ufc.get_circuit("01")
    circuit_02 = ufc.get_circuit("02")

    circuit_00.set_zone(zone)
    circuit_01.set_zone(zone)
    circuit_02.set_zone(zone)

    ufc.ufh_state = UfhState(
        circuits={
            "00": UfhCircuitState(
                ufh_index="00",
                zone_index="00",
                heat_demand=0.15,
            ),
            "01": UfhCircuitState(
                ufh_index="01",
                zone_index="00",
                heat_demand=0.60,
            ),
            "02": UfhCircuitState(
                ufh_index="02",
                zone_index="00",
                heat_demand=0.40,
            ),
        }
    )

    # Act
    demand = await zone.heat_demand()

    # Assert: Must return max() of all bound circuits
    assert demand == 0.60


@pytest.mark.asyncio
async def test_ufh_zone_heat_demand_partial_none_handling() -> None:
    # Arrange: One circuit has telemetry, another is None
    _, tcs, ufc = _make_mock_ufh_system()
    zone = UfhZone(tcs, "01")
    tcs.zone_by_index["01"] = zone

    circuit_00 = ufc.get_circuit("00")
    circuit_01 = ufc.get_circuit("01")

    circuit_00.set_zone(zone)
    circuit_01.set_zone(zone)

    ufc.ufh_state = UfhState(
        circuits={
            "00": UfhCircuitState(
                ufh_index="00",
                zone_index="01",
                heat_demand=None,
            ),
            "01": UfhCircuitState(
                ufh_index="01",
                zone_index="01",
                heat_demand=0.35,
            ),
        }
    )

    # Act
    demand = await zone.heat_demand()

    # Assert
    assert demand == 0.35


@pytest.mark.asyncio
async def test_ufh_zone_thermal_demand_dto() -> None:
    # Arrange
    _, tcs, ufc = _make_mock_ufh_system()
    zone = UfhZone(tcs, "02")
    tcs.zone_by_index["02"] = zone

    circuit = ufc.get_circuit("00")
    circuit.set_zone(zone)

    ufc.ufh_state = UfhState(
        circuits={
            "00": UfhCircuitState(
                ufh_index="00",
                zone_index="02",
                heat_demand=0.55,
            )
        }
    )

    # Act
    dto = await zone.thermal_demand()

    # Assert
    assert dto is not None
    assert isinstance(dto, ThermalDemandDTO)
    assert dto.thermal_demand == 0.55
    assert dto.mode == ThermalMode.HEAT
    assert dto.ufh_index == "02"


def test_load_tcs_hydrates_ufh_circuits() -> None:
    # Arrange
    gateway, tcs, ufc = _make_mock_ufh_system("01:145038", "02:000921")
    zone_00 = UfhZone(tcs, "00")
    zone_01 = UfhZone(tcs, "01")
    tcs.zone_by_index["00"] = zone_00
    tcs.zone_by_index["01"] = zone_01

    schema = {
        SZ_UFH_SYSTEM: {
            "02:000921": {
                SZ_CIRCUITS: {
                    "00": {SZ_ZONE_INDEX: "00"},
                    "01": {SZ_ZONE_INDEX: "01"},
                }
            }
        },
        SZ_ZONES: {
            "00": {SZ_CLASS: "underfloor_heating"},
            "01": {SZ_CLASS: "underfloor_heating"},
        },
    }

    # Act
    result_tcs = load_tcs(gateway, DeviceIdT("01:145038"), schema)

    # Assert
    assert result_tcs is tcs
    assert len(ufc.circuits) == 2

    circuit_00 = ufc.get_circuit("00")
    circuit_01 = ufc.get_circuit("01")

    assert circuit_00.zone is zone_00
    assert circuit_01.zone is zone_01

    cqrs_actuators = gateway.device_registry._cqrs_actuators
    assert "01:145038_00" in cqrs_actuators
    assert circuit_00.id in cqrs_actuators["01:145038_00"]
    assert "01:145038_01" in cqrs_actuators
    assert circuit_01.id in cqrs_actuators["01:145038_01"]


def test_parent_add_child_deduplication() -> None:
    # Arrange
    parent: Parent = Parent()
    child = MagicMock()

    # Act: Add the same child multiple times
    parent._add_child(child, child_id=FF)
    parent._add_child(child, child_id=FF)
    parent._add_child(child, child_id=FF)

    # Assert
    assert len(parent.childs) == 1
    assert parent.childs == [child]
