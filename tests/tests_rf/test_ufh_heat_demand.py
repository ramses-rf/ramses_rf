"""Tests for Underfloor Heating (UFH) demand routing, schema preservation, and pump states."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from ramses_rf.config import SZ_CLASS
from ramses_rf.const import (
    SZ_CIRCUITS,
    SZ_DOMAIN_INDEX,
    SZ_HEAT_DEMAND,
    SZ_UFH_INDEX,
    SZ_ZONE_INDEX,
    SZ_ZONES,
)
from ramses_rf.devices import Controller
from ramses_rf.devices.heat_controllers import UfhController
from ramses_rf.enums import PumpRelayState
from ramses_rf.models import UfhState
from ramses_rf.payloads.heating import HeatDemand2BPayload
from ramses_rf.pipeline.ingestion import StateProjector
from ramses_rf.schemas import (
    SZ_UFH_SYSTEM,
    load_tcs,
)
from ramses_rf.state_projector import (
    _resolve_logical_targets,
    _update_demand_state,
)
from ramses_rf.systems.tcs import Evohome
from ramses_rf.systems.zones import UfhZone, Zone
from ramses_tx.address import Address
from ramses_tx.const import FC, Code, Verb
from ramses_tx.typing import DeviceIdT


def _make_mock_ufh_system(
    ctl_id: str = "01:195932", ufc_id: str = "02:007533"
) -> tuple[MagicMock, Evohome, UfhController]:
    """Assemble a mock Gateway, Evohome TCS, and UfhController hierarchy.

    :param ctl_id: Device ID of the Evohome controller.
    :type ctl_id: str
    :param ufc_id: Device ID of the UFH controller.
    :type ufc_id: str
    :returns: Tuple of (mock gateway, Evohome TCS, UfhController).
    :rtype: tuple[MagicMock, Evohome, UfhController]
    """
    gateway = MagicMock()
    gateway.config.known_list = {}
    gateway.config.block_list = {}
    gateway.config.max_zones = 16
    gateway.device_registry.system_by_id = {}
    gateway.device_registry.device_by_id = {}
    gateway.device_registry._cqrs_actuators = {}
    gateway.engine = gateway._engine
    gateway.engine.enforce_known_list = False
    gateway.engine.include_list = []
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
    gateway.tcs = tcs
    gateway.device_registry.system_by_id[controller.id] = tcs
    gateway.device_registry.device_by_id[controller.id] = controller

    ufc = UfhController(gateway, Address(ufc_id))
    ufc.tcs = tcs
    tcs.childs.append(ufc)
    tcs.child_by_id[ufc.id] = ufc
    gateway.device_registry.device_by_id[ufc.id] = ufc

    gateway.device_registry.devices = [controller, ufc]
    gateway.device_registry.get_device = lambda device_id, **kwargs: (
        ufc if str(device_id) == ufc_id else controller
    )

    return gateway, tcs, ufc


def _make_msg(
    code: Code,
    src: str = "02:007533",
    dst: str = "01:195932",
    verb: Verb = Verb.I_,
) -> MagicMock:
    """Create a mock packet message.

    :param code: Protocol command opcode.
    :type code: Code
    :param src: Source address string.
    :type src: str
    :param dst: Destination address string.
    :type dst: str
    :param verb: Message verb.
    :type verb: Verb
    :returns: Mock Message object.
    :rtype: MagicMock
    """
    msg = MagicMock()
    msg.code = code
    msg.verb = verb
    msg.src = Address(src)
    msg.dst = Address(dst)
    msg._has_array = False
    msg.dtm = None
    msg.timestamp = None
    return msg


# ======================================================================
# Bug 1 & Safeguard Tests: Schema Preservation (circuit.set_zone & circuit_by_id)
# ======================================================================


@pytest.mark.asyncio
async def test_ufh_controller_schema_preserves_zone_index() -> None:
    """Verify UfhController.schema() serializes zone_index from bound zones and fallback."""
    gateway, tcs, ufc = _make_mock_ufh_system()

    zone_00 = UfhZone(tcs, "00")
    tcs.zone_by_index["00"] = zone_00
    circuit_00 = ufc.get_circuit("00")
    circuit_00.set_zone(zone_00)

    # Circuit 01 is not yet bound to an entity, but has fallback mapping in circuit_by_id
    ufc.get_circuit("01")
    circuit_by_id_map: dict[str, Any] = ufc.circuit_by_id
    circuit_by_id_map["01"] = {SZ_ZONE_INDEX: "01"}

    # Act
    schema = await ufc.schema()

    # Assert
    assert SZ_CIRCUITS in schema
    circuits_schema = schema[SZ_CIRCUITS]
    assert circuits_schema["00"] == {SZ_ZONE_INDEX: "00"}
    assert circuits_schema["01"] == {SZ_ZONE_INDEX: "01"}


def test_load_tcs_hydrates_legacy_zone_idx() -> None:
    """Verify _load_ufh_system() accepts legacy 'zone_idx' key in user schema."""
    gateway, tcs, ufc = _make_mock_ufh_system("01:195932", "02:007533")
    zone_00 = UfhZone(tcs, "00")
    zone_01 = UfhZone(tcs, "01")
    tcs.zone_by_index["00"] = zone_00
    tcs.zone_by_index["01"] = zone_01

    schema = {
        SZ_UFH_SYSTEM: {
            "02:007533": {
                SZ_CIRCUITS: {
                    "00": {"zone_idx": "00"},
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
    result_tcs = load_tcs(gateway, cast(DeviceIdT, "01:195932"), schema)

    # Assert
    assert result_tcs is tcs
    circuit_00 = ufc.get_circuit("00")
    circuit_01 = ufc.get_circuit("01")

    assert circuit_00.zone is zone_00
    assert circuit_00.zone_index == "00"
    assert circuit_01.zone is zone_01
    assert circuit_01.zone_index == "01"
    circuit_by_id_map: dict[str, Any] = ufc.circuit_by_id
    assert circuit_by_id_map["00"] == {SZ_ZONE_INDEX: "00"}
    assert circuit_by_id_map["01"] == {SZ_ZONE_INDEX: "01"}


# ======================================================================
# Bug 1 Tests: HeatDemand2BPayload & 3150 Routing to Zone & Controller
# ======================================================================


def test_heat_demand_2b_payload_decodes_zone_index_when_sent_to_controller() -> (
    None
):
    """Verify HeatDemand2BPayload.to_dict() emits zone_index when dst is a CTL."""
    msg = _make_msg(Code._3150, src="02:007533", dst="01:195932")

    # Act: 00 60 (zone index 0x00, demand 0x60 = 96 / 200 = 0.48)
    payload_00 = HeatDemand2BPayload.from_bytes(bytes.fromhex("0060"))
    dict_00 = payload_00.to_dict(msg)

    # Assert: Must emit SZ_ZONE_INDEX, NOT SZ_UFH_INDEX
    assert SZ_ZONE_INDEX in dict_00
    assert dict_00[SZ_ZONE_INDEX] == "00"
    assert SZ_UFH_INDEX not in dict_00
    assert dict_00[SZ_HEAT_DEMAND] == 0.48

    # Act: 0B 60 (zone index 0x0B = Zone 11)
    payload_0b = HeatDemand2BPayload.from_bytes(bytes.fromhex("0B60"))
    dict_0b = payload_0b.to_dict(msg)

    # Assert
    assert dict_0b[SZ_ZONE_INDEX] == "0B"
    assert SZ_UFH_INDEX not in dict_0b
    assert dict_0b[SZ_HEAT_DEMAND] == 0.48


def test_heat_demand_2b_payload_decodes_ufh_index_when_sent_to_self() -> None:
    """Verify HeatDemand2BPayload.to_dict() emits ufh_index when dst is self (UFC)."""
    msg = _make_msg(Code._3150, src="02:007533", dst="02:007533")

    # Act: 01 5C (circuit 0x01, demand 0x5C = 92 / 200 = 0.46)
    payload_circuit = HeatDemand2BPayload.from_bytes(bytes.fromhex("015C"))
    dict_circuit = payload_circuit.to_dict(msg)

    # Assert: Must emit SZ_UFH_INDEX
    assert SZ_UFH_INDEX in dict_circuit
    assert dict_circuit[SZ_UFH_INDEX] == "01"
    assert SZ_ZONE_INDEX not in dict_circuit
    assert dict_circuit[SZ_HEAT_DEMAND] == 0.46


def test_heat_demand_2b_payload_decodes_domain_fc() -> None:
    """Verify HeatDemand2BPayload.to_dict() emits FC domain index for aggregate demand."""
    msg = _make_msg(Code._3150, src="02:007533", dst="02:007533")

    # Act: FC 5C (domain 0xFC)
    payload_fc = HeatDemand2BPayload.from_bytes(bytes.fromhex("FC5C"))
    dict_fc = payload_fc.to_dict(msg)

    # Assert
    assert SZ_DOMAIN_INDEX in dict_fc
    assert dict_fc[SZ_DOMAIN_INDEX] == FC
    assert SZ_ZONE_INDEX not in dict_fc
    assert SZ_UFH_INDEX not in dict_fc
    assert dict_fc[SZ_HEAT_DEMAND] == 0.46


@pytest.mark.asyncio
async def test_state_projector_routes_ufc_to_ctl_3150_to_zone() -> None:
    """Verify _resolve_logical_targets and _update_demand_state update Zone.heat_demand."""
    gateway, tcs, ufc = _make_mock_ufh_system("01:195932", "02:007533")
    zone_00 = UfhZone(tcs, "00")
    zone_01 = UfhZone(tcs, "01")
    zone_0b = UfhZone(tcs, "0B")
    tcs.zone_by_index["00"] = zone_00
    tcs.zone_by_index["01"] = zone_01
    tcs.zone_by_index["0B"] = zone_0b

    msg_00 = _make_msg(Code._3150, src="02:007533", dst="01:195932")
    payload_00 = HeatDemand2BPayload.from_bytes(bytes.fromhex("0060")).to_dict(
        msg_00
    )

    # Act 1: Resolve targets and apply demand update for Zone 00
    targets_00 = _resolve_logical_targets(gateway, msg_00, payload_00)
    assert zone_00 in targets_00

    _update_demand_state(zone_00, payload_00, msg_00)
    demand_00 = await zone_00.heat_demand()
    assert demand_00 == 0.48

    # Act 2: Resolve targets and apply demand update for Zone 0B
    msg_0b = _make_msg(Code._3150, src="02:007533", dst="01:195932")
    payload_0b = HeatDemand2BPayload.from_bytes(bytes.fromhex("0B60")).to_dict(
        msg_0b
    )

    targets_0b = _resolve_logical_targets(gateway, msg_0b, payload_0b)
    assert zone_0b in targets_0b

    _update_demand_state(zone_0b, payload_0b, msg_0b)
    demand_0b = await zone_0b.heat_demand()
    assert demand_0b == 0.48


@pytest.mark.asyncio
async def test_pipeline_ingestion_updates_zone_demand() -> None:
    """Verify pipeline StateProjector.process_message_state() routes 3150 to Zone demand_state."""
    gateway, tcs, ufc = _make_mock_ufh_system("01:195932", "02:007533")
    gateway.device_registry.systems = [tcs]
    zone_01 = UfhZone(tcs, "01")
    tcs.zone_by_index["01"] = zone_01

    projector = StateProjector(gateway, MagicMock())
    msg = _make_msg(Code._3150, src="02:007533", dst="01:195932")
    msg.payload = {SZ_ZONE_INDEX: "01", SZ_HEAT_DEMAND: 0.48}

    # Act
    projector.process_message_state(msg)

    # Assert
    demand_01 = await zone_01.heat_demand()
    assert demand_01 == 0.48


@pytest.mark.asyncio
async def test_ufh_state_does_not_create_phantom_circuit_from_zone_3150() -> (
    None
):
    """Verify 3150 for Zone 0B does not create phantom circuit 0B in ufc.

    When 02: UFC sends 3150 to 01: CTL for a zone, _update_ufh_state()
    must resolve the demand to bound circuits instead of blindly treating
    the zone index as a circuit index.
    """
    # Arrange
    gateway, tcs, ufc = _make_mock_ufh_system("01:195932", "02:007533")
    gateway.device_registry.systems = [tcs]
    zone_0b = UfhZone(tcs, "0B")
    tcs.zone_by_index["0B"] = zone_0b

    # Case 1: Unbound zone 0B - no circuits bound to Zone 0B
    ufc.get_circuit("00")
    ufc.ufh_state = UfhState()

    projector = StateProjector(gateway, MagicMock())
    msg_0b = _make_msg(Code._3150, src="02:007533", dst="01:195932")
    msg_0b.payload = {SZ_ZONE_INDEX: "0B", SZ_HEAT_DEMAND: 0.48}

    # Act: Process message for unbound zone 0B
    projector.process_message_state(msg_0b)

    # Assert: Zone receives heat demand, but UFC creates NO phantom circuit "0B"
    assert await zone_0b.heat_demand() == 0.48
    assert "0B" not in ufc.ufh_state.circuits
    assert "0B" not in ufc.ufh_state.heat_demands
    assert "00" not in ufc.ufh_state.heat_demands

    # Case 2: Circuit 02 is bound to Zone 0B
    circuit_02 = ufc.get_circuit("02")
    circuit_02.set_zone(zone_0b)

    # Act: Process message when circuit 02 is bound to Zone 0B
    projector.process_message_state(msg_0b)

    # Assert: Bound circuit 02 gets the demand, and NO circuit 0B is created
    assert ufc.ufh_state.heat_demands.get("02") == 0.48
    assert ufc.ufh_state.circuits["02"].heat_demand == 0.48
    assert "0B" not in ufc.ufh_state.circuits
    assert "0B" not in ufc.ufh_state.heat_demands


# ======================================================================
# Bug 2 Tests: UfhController.pump_relay_state Fallback
# ======================================================================


@pytest.mark.asyncio
async def test_ufh_controller_pump_relay_state_explicit_and_fallback() -> None:
    """Verify pump_relay_state() prefers explicit 3EF0 and falls back to relay_demand_fa."""
    _, _, ufc = _make_mock_ufh_system()

    # Case 1: Explicit 3EF0 pump_relay_state takes priority
    ufc.ufh_state = UfhState(
        pump_relay_state=PumpRelayState.COOLING,
        relay_demand_fa=0.8,
    )
    assert await ufc.pump_relay_state() == PumpRelayState.COOLING

    # Case 2: No explicit 3EF0, relay_demand_fa > 0.0 -> HEATING
    ufc.ufh_state = UfhState(
        pump_relay_state=None,
        relay_demand_fa=0.45,
    )
    assert await ufc.pump_relay_state() == PumpRelayState.HEATING

    # Case 3: No explicit 3EF0, relay_demand_fa == 0.0 -> OFF
    ufc.ufh_state = UfhState(
        pump_relay_state=None,
        relay_demand_fa=0.0,
    )
    assert await ufc.pump_relay_state() == PumpRelayState.OFF

    # Case 4: No explicit 3EF0, relay_demand_fa is None -> None
    ufc.ufh_state = UfhState(
        pump_relay_state=None,
        relay_demand_fa=None,
    )
    assert await ufc.pump_relay_state() is None


# ======================================================================
# Bug 5 Tests: UfhCircuit.name() Friendly Name
# ======================================================================


@pytest.mark.asyncio
async def test_ufh_circuit_friendly_name() -> None:
    """Verify UfhCircuit.name() formats friendly name from bound zone."""
    _, tcs, ufc = _make_mock_ufh_system()
    circuit = ufc.get_circuit("00")

    # Case 1: Unbound circuit -> None
    assert await circuit.name() is None

    # Case 2: Bound to zone with name -> "{zone_name} Circuit {ufh_index}"
    mock_zone = MagicMock(spec=Zone)
    mock_zone._child_id = "00"
    mock_zone.name = AsyncMock(return_value="Kitchen & Util")
    circuit.set_zone(mock_zone)

    assert await circuit.name() == "Kitchen & Util Circuit 00"

    # Case 3: Bound to zone without name -> None
    mock_zone.name = AsyncMock(return_value=None)
    assert await circuit.name() is None
