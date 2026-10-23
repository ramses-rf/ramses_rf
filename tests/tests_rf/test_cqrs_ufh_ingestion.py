"""RAMSES RF - Tests for UFH CQRS Ingestion and State Models."""

from __future__ import annotations

from datetime import UTC, datetime as dt
from typing import cast
from unittest.mock import MagicMock

from ramses_rf.const import (
    FA,
    FC,
    SZ_COOLING_DEMAND,
    SZ_DOMAIN_ID_WIRE,
    SZ_DOMAIN_INDEX,
    SZ_DOMAIN_OR_ZONE_INDEX,
    SZ_FLAGS,
    SZ_HEAT_DEMAND,
    SZ_MODE,
    SZ_PUMP_RELAY_STATE,
    SZ_RELAY_DEMAND,
    SZ_REMAINING_DAYS,
    SZ_REMAINING_PERCENT,
    SZ_SETPOINT,
    SZ_SETPOINT_BOUNDS,
    SZ_STATE,
    SZ_TEMP_HIGH,
    SZ_TEMP_LOW,
    SZ_UFH_INDEX,
    SZ_ZONE_INDEX,
)
from ramses_rf.devices.hvac_ventilators import HvacVentilator
from ramses_rf.enums import DevType, PumpRelayState
from ramses_rf.models import (
    HvacState,
    StateUpdatedEvent,
    SystemState,
    UfhCircuitState,
    UfhState,
)
from ramses_rf.pipeline.ingestion import StateProjector
from ramses_rf.state_projector import (
    _update_hvac_state as _sp_update_hvac_state,
    _update_system_state as _sp_update_system_state,
)
from ramses_tx.address import NON_DEV_ADDR, Address
from ramses_tx.const import Code, Verb
from ramses_tx.typing import DeviceIdT


class MockUfcTarget:
    """Mock target representing an Underfloor Heating Controller (02:)."""

    _SLUG = DevType.UFC
    id = "02:000921"

    def __init__(self) -> None:
        self.ufh_state: UfhState = UfhState()
        self.received_events: list[StateUpdatedEvent] = []

    def apply_state_update(self, event: StateUpdatedEvent) -> None:
        if isinstance(event.state, UfhState):
            self.ufh_state = event.state
            self.received_events.append(event)


def _make_msg(code: Code, src: str = "02:000921") -> MagicMock:
    msg = MagicMock()
    msg.code = code
    msg.verb = Verb.I_
    msg.src = Address(src)
    msg.dst = Address(NON_DEV_ADDR.id)
    msg.dtm = None
    msg.timestamp = None
    return msg


def test_ufh_circuit_state_initialization() -> None:
    # Arrange & Act
    circuit = UfhCircuitState(
        ufh_index="00",
        zone_index="01",
        heat_demand=0.75,
        cooling_demand=0.0,
        circuit_mode="heating",
        setpoint=21.0,
        min_temp=10.0,
        max_temp=28.0,
        flags=1,
    )

    # Assert
    assert circuit.ufh_index == "00"
    assert circuit.circuit_index == "00"
    assert circuit.zone_index == "01"
    assert circuit.heat_demand == 0.75
    assert circuit.cooling_demand == 0.0
    assert circuit.circuit_mode == "heating"
    assert circuit.setpoint == 21.0
    assert circuit.min_temp == 10.0
    assert circuit.max_temp == 28.0
    assert circuit.flags == 1


def test_update_ufh_state_heat_demand_single_and_multi_circuit() -> None:
    # Arrange
    projector = StateProjector(MagicMock(), MagicMock())
    target = MockUfcTarget()
    msg = _make_msg(Code._3150)

    # Act - Circuit 00
    payload_00 = {SZ_UFH_INDEX: "00", SZ_HEAT_DEMAND: 0.87}
    projector._update_ufh_state(target, payload_00, msg)

    # Act - Circuit 01
    payload_01 = {SZ_UFH_INDEX: "01", SZ_HEAT_DEMAND: 0.58}
    projector._update_ufh_state(target, payload_01, msg)

    # Assert
    assert target.ufh_state.heat_demands["00"] == 0.87
    assert target.ufh_state.heat_demands["01"] == 0.58
    assert "00" in target.ufh_state.circuits
    assert "01" in target.ufh_state.circuits
    assert target.ufh_state.circuits["00"].heat_demand == 0.87
    assert target.ufh_state.circuits["01"].heat_demand == 0.58
    assert len(target.received_events) == 2


def test_update_ufh_state_relay_demands_fa_fc() -> None:
    # Arrange
    projector = StateProjector(MagicMock(), MagicMock())
    target = MockUfcTarget()

    # Act - 0008 FA
    msg_0008 = _make_msg(Code._0008)
    payload_fa = {SZ_DOMAIN_INDEX: FA, SZ_RELAY_DEMAND: 0.50}
    projector._update_ufh_state(target, payload_fa, msg_0008)

    # Act - 0008 FC
    payload_fc = {SZ_DOMAIN_INDEX: FC, SZ_RELAY_DEMAND: 1.00}
    projector._update_ufh_state(target, payload_fc, msg_0008)

    # Assert
    assert target.ufh_state.relay_demand_fa == 0.50
    assert target.ufh_state.relay_demand_fc == 1.00


def test_update_ufh_state_cooling_demand_2d49() -> None:
    # Arrange
    projector = StateProjector(MagicMock(), MagicMock())
    target = MockUfcTarget()
    msg = _make_msg(Code._2D49)

    # Act - Circuit 02 cooling demand
    payload = {SZ_UFH_INDEX: "02", SZ_COOLING_DEMAND: 0.70}
    projector._update_ufh_state(target, payload, msg)

    # Assert
    assert target.ufh_state.cooling_demands["02"] == 0.70
    assert target.ufh_state.circuits["02"].cooling_demand == 0.70


def test_update_ufh_state_cooling_demand_boolean_state() -> None:
    # Arrange
    projector = StateProjector(MagicMock(), MagicMock())
    target = MockUfcTarget()
    msg = _make_msg(Code._2D49)

    # Act - Circuit 00 with bool state True (HCC100 active cooling)
    payload = {SZ_DOMAIN_OR_ZONE_INDEX: 0, SZ_STATE: True}
    projector._update_ufh_state(target, payload, msg)

    # Assert
    assert target.ufh_state.cooling_demands["00"] == 1.0
    assert target.ufh_state.circuits["00"].cooling_demand == 1.0


def test_update_ufh_state_circuit_mode_2249() -> None:
    # Arrange
    projector = StateProjector(MagicMock(), MagicMock())
    target = MockUfcTarget()
    msg = _make_msg(Code._2249)

    # Act
    payload = {SZ_UFH_INDEX: "01", SZ_MODE: "cooling"}
    projector._update_ufh_state(target, payload, msg)

    # Assert
    assert target.ufh_state.circuit_modes["01"] == "cooling"
    assert target.ufh_state.circuits["01"].circuit_mode == "cooling"


def test_update_ufh_state_circuit_binding_000c() -> None:
    # Arrange
    projector = StateProjector(MagicMock(), MagicMock())
    target = MockUfcTarget()
    msg = _make_msg(Code._000C)

    # Act - Circuit 00 bound to Zone 03
    payload_bound = {SZ_UFH_INDEX: "00", SZ_ZONE_INDEX: "03"}
    projector._update_ufh_state(target, payload_bound, msg)

    # Act - Circuit 04 unbound
    payload_unbound = {SZ_UFH_INDEX: "04", SZ_ZONE_INDEX: None}
    projector._update_ufh_state(target, payload_unbound, msg)

    # Assert
    assert target.ufh_state.circuit_to_zone_map["00"] == "03"
    assert target.ufh_state.circuit_to_zone_map["04"] is None
    assert target.ufh_state.circuits["00"].zone_index == "03"
    assert target.ufh_state.circuits["04"].zone_index is None


def test_update_ufh_state_setpoints_and_bounds_22c9() -> None:
    # Arrange
    projector = StateProjector(MagicMock(), MagicMock())
    target = MockUfcTarget()
    msg = _make_msg(Code._22C9)

    # Act
    payload = {
        SZ_UFH_INDEX: "00",
        SZ_SETPOINT_BOUNDS: (12.0, 26.0),
        SZ_FLAGS: 1,
    }
    projector._update_ufh_state(target, payload, msg)

    # Assert
    assert target.ufh_state.setpoints["00"][SZ_TEMP_LOW] == 12.0
    assert target.ufh_state.setpoints["00"][SZ_TEMP_HIGH] == 26.0
    assert target.ufh_state.setpoints["00"][SZ_FLAGS] == 1
    assert target.ufh_state.circuits["00"].min_temp == 12.0
    assert target.ufh_state.circuits["00"].max_temp == 26.0
    assert target.ufh_state.circuits["00"].flags == 1


def test_update_ufh_state_setpoint_2309() -> None:
    # Arrange
    projector = StateProjector(MagicMock(), MagicMock())
    target = MockUfcTarget()
    msg = _make_msg(Code._2309)

    # Act
    payload = {
        SZ_UFH_INDEX: "00",
        SZ_SETPOINT: 20.5,
    }
    projector._update_ufh_state(target, payload, msg)

    # Assert
    assert target.ufh_state.circuits["00"].setpoint == 20.5


def test_update_ufh_state_pump_relay_3ef0() -> None:
    # Arrange
    projector = StateProjector(MagicMock(), MagicMock())
    target = MockUfcTarget()
    msg = _make_msg(Code._3EF0)

    # Act
    payload = {SZ_PUMP_RELAY_STATE: PumpRelayState.HEATING}
    projector._update_ufh_state(target, payload, msg)

    # Assert
    assert target.ufh_state.pump_relay_state == PumpRelayState.HEATING


def test_update_ufh_state_non_ufc_target_ignored() -> None:
    # Arrange
    class NonUfcTarget:
        _SLUG = DevType.TRV
        id = "04:123456"
        events: list[StateUpdatedEvent] = []

        def apply_state_update(self, event: StateUpdatedEvent) -> None:
            self.events.append(event)

    projector = StateProjector(MagicMock(), MagicMock())
    target = NonUfcTarget()
    msg = _make_msg(Code._3150)
    payload = {SZ_UFH_INDEX: "00", SZ_HEAT_DEMAND: 0.50}

    # Act
    projector._update_ufh_state(target, payload, msg)

    # Assert
    assert not hasattr(target, "ufh_state")
    assert len(target.events) == 0


def test_update_ufh_state_3150_domain_relay_demands() -> None:
    # Arrange
    projector = StateProjector(MagicMock(), MagicMock())
    target = MockUfcTarget()
    msg = _make_msg(Code._3150)

    # Act - 3150 FA and FC
    payload_fa = {SZ_DOMAIN_ID_WIRE: FA, SZ_HEAT_DEMAND: 0.65}
    projector._update_ufh_state(target, payload_fa, msg)
    payload_fc = {SZ_DOMAIN_ID_WIRE: FC, SZ_HEAT_DEMAND: 0.85}
    projector._update_ufh_state(target, payload_fc, msg)

    # Assert
    assert target.ufh_state.relay_demand_fa == 0.65
    assert target.ufh_state.relay_demand_fc == 0.85


def test_update_ufh_state_integer_circuit_indices() -> None:
    # Arrange
    projector = StateProjector(MagicMock(), MagicMock())
    target = MockUfcTarget()
    msg = _make_msg(Code._3150)

    # Act - Integer 0 and 7
    payload_0 = {SZ_UFH_INDEX: 0, SZ_HEAT_DEMAND: 0.40}
    projector._update_ufh_state(target, payload_0, msg)
    payload_7 = {SZ_UFH_INDEX: 7, SZ_HEAT_DEMAND: 0.90}
    projector._update_ufh_state(target, payload_7, msg)

    # Assert
    assert target.ufh_state.heat_demands["00"] == 0.40
    assert target.ufh_state.heat_demands["07"] == 0.90
    assert target.ufh_state.circuits["00"].heat_demand == 0.40
    assert target.ufh_state.circuits["07"].heat_demand == 0.90


def test_update_ufh_state_cooling_demand_false_state() -> None:
    # Arrange
    projector = StateProjector(MagicMock(), MagicMock())
    target = MockUfcTarget()
    msg = _make_msg(Code._2D49)

    # Act
    payload = {SZ_DOMAIN_OR_ZONE_INDEX: 1, SZ_STATE: False}
    projector._update_ufh_state(target, payload, msg)

    # Assert
    assert target.ufh_state.cooling_demands["01"] == 0.0
    assert target.ufh_state.circuits["01"].cooling_demand == 0.0


def test_update_ufh_state_pump_relay_string_and_invalid_3ef1() -> None:
    # Arrange
    projector = StateProjector(MagicMock(), MagicMock())
    target = MockUfcTarget()

    # Act - Valid string via 3EF1
    msg_3ef1 = _make_msg(Code._3EF1)
    payload_valid = {SZ_PUMP_RELAY_STATE: "cooling"}
    projector._update_ufh_state(target, payload_valid, msg_3ef1)

    # Assert
    assert target.ufh_state.pump_relay_state == PumpRelayState.COOLING

    # Act - Invalid string via 3EF0 (logs warning and does not crash)
    msg_3ef0 = _make_msg(Code._3EF0)
    payload_invalid = {SZ_PUMP_RELAY_STATE: "non_existent_mode"}
    projector._update_ufh_state(target, payload_invalid, msg_3ef0)

    # Assert - pump_relay_state remains unchanged
    assert target.ufh_state.pump_relay_state == PumpRelayState.COOLING


def test_update_ufh_state_unhandled_opcode_and_empty_payload() -> None:
    # Arrange
    projector = StateProjector(MagicMock(), MagicMock())
    target = MockUfcTarget()

    # Act - Unhandled opcode
    msg_unhandled = _make_msg(Code._1F09)
    projector._update_ufh_state(target, {}, msg_unhandled)

    # Act - Handled opcode but empty payload
    msg_empty = _make_msg(Code._3150)
    projector._update_ufh_state(target, {}, msg_empty)

    # Assert
    assert len(target.received_events) == 0


def test_update_ufh_state_timestamp_propagation() -> None:
    # Arrange
    projector = StateProjector(MagicMock(), MagicMock())
    target = MockUfcTarget()
    test_dtm = dt(2026, 8, 29, 12, 0, 0, tzinfo=UTC)
    msg = _make_msg(Code._3150)
    msg.dtm = test_dtm

    # Act
    payload = {SZ_UFH_INDEX: "02", SZ_HEAT_DEMAND: 0.60}
    projector._update_ufh_state(target, payload, msg)

    # Assert
    assert target.ufh_state.last_updated == test_dtm
    assert target.ufh_state.circuits["02"].last_updated == test_dtm
    assert target.received_events[0].state.last_updated == test_dtm


class MockTcsTarget:
    """Mock target representing an evohome controller (TCS, 01:)."""

    _SLUG = DevType.CTL
    id = "01:150000"

    def __init__(self) -> None:
        self.system_state: SystemState = SystemState()
        self.received_events: list[StateUpdatedEvent] = []

    def apply_state_update(self, event: StateUpdatedEvent) -> None:
        if isinstance(event.state, SystemState):
            self.system_state = event.state
            self.received_events.append(event)


def test_update_system_state_w_echo_not_projected() -> None:
    # A W 2E04 is our own command echo: it carries the *requested* mode,
    # not the controller's confirmed state.  If nothing changes (e.g.
    # auto_with_reset while already in auto) no I 2E04 follows and the
    # echoed mode would stick as phantom state (ramses_cc issue 1323).
    # Arrange
    projector = StateProjector(MagicMock(), MagicMock())
    target = MockTcsTarget()

    msg = _make_msg(Code._2E04, src="18:001234")
    msg.verb = Verb.W_
    msg.dst = Address("01:150000")
    payload = {"system_mode": "auto_with_reset", "until": None}

    # Act
    projector._update_system_state(target, payload, msg)

    # Assert
    assert target.system_state.system_mode is None
    assert len(target.received_events) == 0


def test_update_system_state_i_broadcast_projected() -> None:
    # An I 2E04 is the controller's confirmed mode and must be projected.
    # Arrange
    projector = StateProjector(MagicMock(), MagicMock())
    target = MockTcsTarget()

    msg = _make_msg(Code._2E04, src="01:150000")
    msg.verb = Verb.I_
    payload = {"system_mode": "away", "until": None}

    # Act
    projector._update_system_state(target, payload, msg)

    # Assert
    assert target.system_state.system_mode == "away"
    assert len(target.received_events) == 1


def test_legacy_update_system_state_w_echo_not_projected() -> None:
    # Same guard for the registry-based projector path.
    # Arrange
    target = MockTcsTarget()

    msg = _make_msg(Code._2E04, src="18:001234")
    msg.verb = Verb.W_
    msg.dst = Address("01:150000")
    payload = {"system_mode": "auto_with_reset", "until": None}

    # Act
    _sp_update_system_state(target, payload, msg)

    # Assert
    assert target.system_state.system_mode is None
    assert len(target.received_events) == 0


class MockRemTarget:
    """Mock target representing a remote (REM) receiving a 10D0 RP.

    Has ``hvac_state`` (remotes get one) but no ``_supports_10d0`` —
    that flag only exists on HvacVentilator (ramses_cc issue 1330).
    """

    _SLUG = DevType.REM
    id = "29:176861"

    def __init__(self) -> None:
        self.hvac_state: HvacState = HvacState()
        self.received_events: list[StateUpdatedEvent] = []

    def apply_state_update(self, event: StateUpdatedEvent) -> None:
        if isinstance(event.state, HvacState):
            self.hvac_state = event.state
            self.received_events.append(event)


def test_update_hvac_state_10d0_rp_to_non_fan_no_crash() -> None:
    # A 10D0 RP is addressed to the *requester* — a REM/HGI, which lacks
    # _supports_10d0; the support-flag block must be skipped (not crash).
    # Arrange
    projector = StateProjector(MagicMock(), MagicMock())
    target = MockRemTarget()
    msg = _make_msg(Code._10D0)
    msg.verb = Verb.RP
    payload = {SZ_REMAINING_DAYS: 30, SZ_REMAINING_PERCENT: None}

    # Act (must not raise AttributeError)
    projector._update_hvac_state(target, payload, msg)
    _sp_update_hvac_state(target, payload, msg)

    # Assert: no phantom flag on a non-fan device
    assert not hasattr(target, "_supports_10d0")


def test_update_hvac_state_10d0_rp_to_fan_sets_flag() -> None:
    # A 10D0 RP to a real fan still flags _supports_10d0.
    # Arrange
    projector = StateProjector(MagicMock(), MagicMock())
    target = HvacVentilator.__new__(HvacVentilator)
    target.hvac_state = HvacState()
    target._supports_10d0 = False
    target._strategy = None
    target._scheme = None
    target.id = cast(DeviceIdT, "32:153289")
    msg = _make_msg(Code._10D0)
    msg.verb = Verb.RP
    payload = {SZ_REMAINING_DAYS: 30, SZ_REMAINING_PERCENT: None}

    # Act
    projector._update_hvac_state(target, payload, msg)
    _sp_update_hvac_state(target, payload, msg)

    # Assert
    assert target._supports_10d0 is True
