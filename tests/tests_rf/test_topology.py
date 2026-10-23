# --- START OF FILE test_topology_isolated.py ---

"""Isolated test to prove ramses_rf Phase 2.95 topology regressions.

This test completely bypasses Home Assistant and ramses_cc to evaluate
the raw output of the new TopologyBuilder pipeline,
using the exact fixtures from the failing CI environment.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, cast

import pytest

import ramses_rf.exceptions as exc
from ramses_rf.gateway import Gateway, GatewayConfig
from ramses_rf.interfaces import ControllerInterface, ParentInterface
from ramses_rf.topology import Child, Parent
from ramses_tx import DeviceIdT
from ramses_tx.config import EngineConfig


async def async_flush_queues(gwy: Gateway) -> None:
    """Deterministically drain specific backend CQRS queues.

    Hardcoded references are used to avoid introspection side-effects.
    """
    queues: list[asyncio.Queue[Any]] = []

    if hasattr(gwy, "msg_queue") and isinstance(gwy.msg_queue, asyncio.Queue):
        queues.append(gwy.msg_queue)

    engine = getattr(gwy, "_engine", None)
    if engine and hasattr(engine, "_msg_queue"):
        if isinstance(engine._msg_queue, asyncio.Queue):
            queues.append(engine._msg_queue)

    dispatcher = getattr(gwy, "dispatcher", None) or getattr(
        gwy, "central_dispatcher", None
    )
    if dispatcher:
        for q_name in (
            "_in_queue",
            "ssot_queue",
            "discovery_queue",
            "binding_queue",
            "faked_queue",
        ):
            if hasattr(dispatcher, q_name):
                q = getattr(dispatcher, q_name)
                if isinstance(q, asyncio.Queue):
                    queues.append(q)

    for q in queues:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(q.join(), timeout=5.0)

    for _ in range(50):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_ramses_rf_isolated_topology() -> None:
    """Test ramses_rf parsing an input log and building a graph."""

    # Provide the path to the packets_rcvd.log file you uploaded.
    # Adjust this path if running from a different directory.
    INPUT_FILE = "/home/phil/software/ramses_cc/tests/tests_new/fixtures/default/packets_rcvd.log"

    # 1. Translate configuration.yaml into native ramses_rf config
    known_list = {
        "01:145038": {"class": "CTL"},
        "03:123456": {"class": "THM", "faked": True},
        "10:123456": {"class": "OTB"},
        "18:006402": {"class": "HGI"},
        "13:120241": {"class": "BDR"},
        "13:120242": {"class": "BDR"},
        "07:046947": {"class": "DHW"},
        "34:092243": {"class": "THM"},
        "04:056053": {"class": "TRV"},
        "22:140285": {"class": "THM"},
        "04:189082": {"class": "TRV"},
        "13:081775": {"class": "BDR"},
        "13:202850": {"class": "BDR"},
        "32:097710": {"class": "CO2"},
        "32:139773": {"class": "HUM"},
    }

    # Translate the schema definitions
    schema = {
        "main_tcs": "01:145038",
        "01:145038": {
            "system": {"appliance_control": "10:123456"},
            "zones": {"00": {"sensor": "01:145038"}},
        },
    }

    engine_config = EngineConfig(
        disable_qos=True,
        input_file=INPUT_FILE,
        enforce_known_list=True,  # Crucial setting from config
        disable_sending=True,
    )

    gwy_config = GatewayConfig(
        disable_discovery=True,
        engine=engine_config,
        known_list=known_list,
        schema=schema,
    )

    # 2. Arrange: Instantiate raw Gateway
    gwy = Gateway(port_name=None, config=gwy_config)

    # 3. Act: Start gateway, process log, and flush queues
    await gwy.start()
    await async_flush_queues(gwy)
    await gwy.stop()

    # 4. Assert: Prove what ramses_rf actually built
    devices = {d.id: d for d in gwy.device_registry.devices}
    systems = gwy.device_registry.systems

    print("\n--- RAMSES_RF ISOLATED DIAGNOSTICS ---")
    print(f"Total Devices Found: {len(devices)}")
    print(f"Total Systems Found: {len(systems)}")
    print(f"Device IDs: {list(devices.keys())}")

    # Assert the Gateway / HGI exists
    assert "18:006402" in devices, (
        "CRITICAL: The Gateway (HGI) was not registered! "
        "Check device_registry.py instantiation."
    )

    # Assert the Controller exists (It is in the log AND the known_list)
    assert "01:145038" in devices, (
        "CRITICAL: The Controller was not registered! "
        "Check TopologyBuilder eavesdrop rules."
    )

    # Assert the faked Thermostat exists (It is NOT in the log!)
    # This will likely fail if Phase 2.95 requires packets to build devices.
    assert "03:123456" in devices, (
        "CRITICAL: The Faked Thermostat was not registered! "
        "The new architecture is ignoring configuration schemas."
    )

    # Assert the Evohome system was instantiated
    assert len(systems) > 0, (
        "CRITICAL: TopologyBuilder failed to create a System!"
    )


# --- Unit Tests for Topology Parent Promotion & Re-binding ---


class System(Parent["BdrSwitch"]):
    def __init__(self, sys_id: str) -> None:
        super().__init__(child_id="00")
        self.id = sys_id
        self.actuators: list[BdrSwitch] = []
        self.actuator_by_id: dict[DeviceIdT, BdrSwitch] = {}


class Zone(Parent["BdrSwitch"]):
    def __init__(self, index: str) -> None:
        super().__init__(child_id=index)
        self.index = index
        self.id = index
        self.ctl: MockController | None = None
        self.actuators: list[BdrSwitch] = []
        self.actuator_by_id: dict[DeviceIdT, BdrSwitch] = {}


class BdrSwitch(Child):
    def __init__(self, dev_id: str) -> None:
        super().__init__()
        self.id = dev_id


def test_child_set_parent_initial_assignment() -> None:
    # Arrange
    child = BdrSwitch("13:123456")
    parent_system = System("01:145038")

    # Act
    child._apply_topology_link(parent_system, child_id="FC")

    # Assert
    assert child._parent is parent_system
    assert child._child_id == "FC"


def test_child_set_parent_promotion_from_system_to_zone() -> None:
    # Arrange
    child = BdrSwitch("13:123456")
    parent_system = System("01:145038")
    parent_zone = Zone("00")

    # Act 1: Initial System parent binding
    child_dev_id = cast(DeviceIdT, child.id)
    child._apply_topology_link(parent_system, child_id="FC")

    # Act 2: Promote parent to Zone
    child._apply_topology_link(parent_zone, child_id="00")

    # Assert
    assert child._parent is parent_zone
    assert child not in parent_system.actuators
    assert child_dev_id not in parent_system.actuator_by_id


def test_child_set_parent_rejects_invalid_cross_zone_change() -> None:
    # Arrange
    child = BdrSwitch("13:123456")
    zone_00 = Zone("00")
    zone_01 = Zone("01")

    # Act
    child._apply_topology_link(zone_00, child_id="00")

    # Assert
    with pytest.raises(exc.SystemSchemaInconsistent) as exc_info:
        child._apply_topology_link(zone_01, child_id="01")

    assert "can't change parent" in str(exc_info.value)


def test_child_set_parent_idempotent() -> None:
    # Arrange
    child = BdrSwitch("13:123456")
    zone_00 = Zone("00")

    # Act
    child._apply_topology_link(zone_00, child_id="00")
    child._apply_topology_link(zone_00, child_id="00")

    # Assert
    assert child._parent is zone_00


def test_generic_parent_add_and_detach_child() -> None:
    # Arrange
    child = BdrSwitch("13:123456")
    parent = Zone("00")

    # Act 1: Add child actuator
    parent._add_child(child, child_id="00")

    # Assert 1
    assert child in parent.childs
    assert parent.child_by_id["13:123456"] is child
    assert child in parent.actuators
    assert parent.actuator_by_id[cast(DeviceIdT, "13:123456")] is child

    # Act 2: Detach child
    parent._detach_child(child)

    # Assert 2
    assert child not in parent.childs
    assert "13:123456" not in parent.child_by_id
    assert child not in parent.actuators
    assert cast(DeviceIdT, "13:123456") not in parent.actuator_by_id
    assert child._parent is None
    assert child._child_id is None


def test_child_rejects_none_parent() -> None:
    # Arrange
    child = BdrSwitch("13:123456")

    # Act & Assert
    with pytest.raises(exc.SchemaInconsistentError) as exc_info:
        child._get_parent(None)

    assert "parent cannot be None" in str(exc_info.value)


def test_parent_zone_index_getter_setter() -> None:
    # Arrange
    parent = Zone("01")

    # Act & Assert
    assert parent.zone_index == "01"
    parent.zone_index = "02"
    assert parent.zone_index == "02"


class MockController(Child):
    """Mock controller for protocol testing."""

    def __init__(self, dev_id: str) -> None:
        super().__init__()
        self.id = cast(DeviceIdT, dev_id)
        self._SLUG = "01"
        self.tcs = None

    async def traits(self) -> dict[str, Any]:
        return {}


class EvohomeSystem(Parent[BdrSwitch]):
    """Mock system parent exposing DHW and heating valve slots."""

    def __init__(self, sys_id: str, ctl: MockController | None = None) -> None:
        super().__init__(child_id="00")
        self.id = sys_id
        self.ctl = ctl


class HeatingZone(Parent[BdrSwitch]):
    """Mock zone parent exposing sensor and actuator slots."""

    def __init__(self, index: str, ctl: MockController | None = None) -> None:
        super().__init__(child_id=index)
        self.index = index
        self.id = index
        self.actuators: list[BdrSwitch] = []
        self.actuator_by_id: dict[DeviceIdT, BdrSwitch] = {}
        self.ctl = ctl

    @property
    def sensor(self) -> BdrSwitch | None:
        return self._sensor


class MockUfhController(Parent[BdrSwitch]):
    """Mock UFH controller exposing circuits."""

    _SLUG = "02"

    def __init__(self, dev_id: str) -> None:
        super().__init__(child_id="FA")
        self.id = cast(DeviceIdT, dev_id)
        self.tcs: Any | None = None
        self.circuits: dict[str, BdrSwitch] = {}
        self.circuit_by_id: dict[str, BdrSwitch] = {}

    async def traits(self) -> dict[str, Any]:
        return {}


def test_runtime_checkable_protocols() -> None:
    # Arrange
    parent_system = EvohomeSystem("01:145038")
    mock_ctl = MockController("01:145038")
    ufc = MockUfhController("02:000921")

    # Assert
    assert isinstance(parent_system, ParentInterface)
    assert isinstance(mock_ctl, ControllerInterface)
    assert isinstance(ufc, ParentInterface)
    assert isinstance(ufc, ControllerInterface)


def test_parent_dhw_sensor_slot_and_conflict() -> None:
    # Arrange
    sys = EvohomeSystem("01:145038")
    sensor1 = BdrSwitch("07:111111")
    sensor2 = BdrSwitch("07:222222")

    # Act 1: Add first DHW sensor
    sys._add_child(sensor1, child_id="FA", is_sensor=True)

    # Assert 1
    assert sys._dhw_sensor is sensor1

    # Act 2 & Assert 2: Conflict on re-adding different DHW sensor
    with pytest.raises(exc.SystemSchemaInconsistent) as exc_info:
        sys._add_child(sensor2, child_id="FA", is_sensor=True)

    assert "changed dhw_sensor" in str(exc_info.value)


def test_parent_dhw_sensor_detach() -> None:
    # Arrange
    sys = EvohomeSystem("01:145038")
    sensor = BdrSwitch("07:111111")
    sys._add_child(sensor, child_id="FA", is_sensor=True)

    # Act
    sys._detach_child(sensor)

    # Assert
    assert sys._dhw_sensor is None
    assert sensor not in sys.childs


def test_parent_htg_valve_slot_and_conflict() -> None:
    # Arrange
    sys = EvohomeSystem("01:145038")
    valve1 = BdrSwitch("13:111111")
    valve2 = BdrSwitch("13:222222")

    # Act 1: Add heating valve
    sys._add_child(valve1, child_id="F9")

    # Assert 1
    assert sys._htg_valve is valve1

    # Act 2 & Assert 2: Conflict on re-adding different heating valve
    with pytest.raises(exc.SystemSchemaInconsistent) as exc_info:
        sys._add_child(valve2, child_id="F9")

    assert "changed htg_valve" in str(exc_info.value)


def test_parent_htg_valve_detach() -> None:
    # Arrange
    sys = EvohomeSystem("01:145038")
    valve = BdrSwitch("13:111111")
    sys._add_child(valve, child_id="F9")

    # Act
    sys._detach_child(valve)

    # Assert
    assert sys._htg_valve is None
    assert valve not in sys.childs


def test_parent_dhw_valve_slot_and_conflict() -> None:
    # Arrange
    sys = EvohomeSystem("01:145038")
    valve1 = BdrSwitch("13:111111")
    valve2 = BdrSwitch("13:222222")

    # Act 1: Add DHW valve
    sys._add_child(valve1, child_id="FA")

    # Assert 1
    assert sys._dhw_valve is valve1

    # Act 2 & Assert 2: Conflict on re-adding different DHW valve
    with pytest.raises(exc.SystemSchemaInconsistent) as exc_info:
        sys._add_child(valve2, child_id="FA")

    assert "changed dhw_valve" in str(exc_info.value)


def test_parent_dhw_valve_detach() -> None:
    # Arrange
    sys = EvohomeSystem("01:145038")
    valve = BdrSwitch("13:111111")
    sys._add_child(valve, child_id="FA")

    # Act
    sys._detach_child(valve)

    # Assert
    assert sys._dhw_valve is None
    assert valve not in sys.childs


def test_parent_app_cntrl_slot_and_conflict() -> None:
    # Arrange
    sys = EvohomeSystem("01:145038")
    relay1 = BdrSwitch("10:111111")
    relay2 = BdrSwitch("10:222222")

    # Act 1: Add appliance control relay
    sys._add_child(relay1, child_id="FC")

    # Assert 1
    assert sys._app_cntrl is relay1

    # Act 2 & Assert 2: Conflict on re-adding different appliance control relay
    with pytest.raises(exc.SystemSchemaInconsistent) as exc_info:
        sys._add_child(relay2, child_id="FC")

    assert "changed app_cntrl" in str(exc_info.value)


def test_parent_app_cntrl_detach() -> None:
    # Arrange
    sys = EvohomeSystem("01:145038")
    relay = BdrSwitch("10:111111")
    sys._add_child(relay, child_id="FC")

    # Act
    sys._detach_child(relay)

    # Assert
    assert sys._app_cntrl is None
    assert relay not in sys.childs


def test_parent_zone_sensor_slot_and_conflict() -> None:
    # Arrange
    zone = HeatingZone("00")
    sensor1 = BdrSwitch("34:111111")
    sensor2 = BdrSwitch("34:222222")

    # Act 1: Add zone sensor
    zone._add_child(sensor1, is_sensor=True)

    # Assert 1
    assert zone._sensor is sensor1

    # Act 2 & Assert 2: Conflict on re-adding different sensor
    with pytest.raises(exc.SystemSchemaInconsistent) as exc_info:
        zone._add_child(sensor2, is_sensor=True)

    assert "changed zone sensor" in str(exc_info.value)


def test_parent_zone_sensor_detach() -> None:
    # Arrange
    zone = HeatingZone("00")
    sensor = BdrSwitch("34:111111")
    zone._add_child(sensor, is_sensor=True)

    # Act
    zone._detach_child(sensor)

    # Assert
    assert zone._sensor is None
    assert sensor not in zone.childs


def test_parent_circuit_slot_and_detach() -> None:
    # Arrange
    ufc = MockUfhController("02:000921")
    circuit = BdrSwitch("13:123456")

    # Act 1: Add circuit
    ufc._add_child(circuit, child_id="00")

    # Assert 1
    assert "13:123456" in ufc.circuit_by_id
    assert ufc.circuit_by_id["13:123456"] is circuit

    # Act 2: Detach circuit
    ufc._detach_child(circuit)

    # Assert 2
    assert "13:123456" not in ufc.circuit_by_id
    assert circuit not in ufc.childs


def test_child_controller_conflict_rejection() -> None:
    # Arrange
    ctl1 = MockController("01:111111")
    ctl2 = MockController("01:222222")
    zone = Zone("00")
    zone.ctl = ctl2
    child = BdrSwitch("13:123456")
    child.ctl = ctl1

    # Act & Assert: Attempting to link to parent with different controller raises error
    with pytest.raises(exc.SystemSchemaInconsistent) as exc_info:
        child._apply_topology_link(zone, child_id="00")

    assert "can't change controller" in str(exc_info.value)


def test_parent_invalid_combination_raises() -> None:
    # Arrange
    sys = EvohomeSystem("01:145038")
    child = BdrSwitch("13:123456")

    # Act & Assert: is_sensor=True on parent without sensor slot
    with pytest.raises(exc.SchemaInconsistentError) as exc_info:
        sys._add_child(child, is_sensor=True)

    assert "not a valid combination" in str(exc_info.value)
