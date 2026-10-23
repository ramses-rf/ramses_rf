"""RAMSES RF - Heating Controller Devices."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Final, cast

from ramses_rf.const import (
    SZ_CIRCUITS,
    SZ_HEAT_DEMAND,
    SZ_RELAY_DEMAND,
    SZ_ZONE_INDEX,
    DevType,
)
from ramses_rf.entity import Entity
from ramses_rf.enums import PumpRelayState, ThermalMode
from ramses_rf.models import (
    DeviceTraits,
    UfhCircuitDemandDTO,
    UfhCircuitDTO,
    UfhCircuitState,
    UfhState,
)
from ramses_rf.topology import Child, Parent
from ramses_rf.typing import SchemaT
from ramses_tx.const import FA
from ramses_tx.typing import DeviceIdT, DevIndexT

from .dev_base import DeviceHeat

if TYPE_CHECKING:
    from ramses_rf.systems import Evohome, Zone

    from ..messages import Message


_LOGGER = logging.getLogger(__name__)


class Controller(DeviceHeat):  # CTL (01):
    """The Controller base class."""

    HEAT_DEMAND: Final = SZ_HEAT_DEMAND

    _SLUG = DevType.CTL
    _STATE_ATTR = HEAT_DEMAND

    def __init__(  # type: ignore[explicit-any]  # heterogeneous construction/schema kwargs
        self, *args: Any, traits: DeviceTraits | None = None, **kwargs: Any
    ) -> None:
        """Initialise the Controller device.

        :param args: Positional arguments passed to parent class.
        :type args: Any
        :param traits: Optional device traits metadata.
        :type traits: DeviceTraits | None
        :param kwargs: Keyword arguments passed to parent class.
        :type kwargs: Any
        """
        super().__init__(*args, traits=traits, **kwargs)

        self.tcs: Evohome | None = None  # TODO: = self?
        self._make_tcs_controller(
            **kwargs
        )  # NOTE: must create_from_schema first

    def _make_tcs_controller(  # type: ignore[explicit-any]  # heterogeneous construction/schema kwargs
        self, *, msg: Message | None = None, **_schema: Any
    ) -> None:  # CH/DHW
        """Attach a TCS (create it if required) after passing it any msg.

        Schema kwargs are accepted for backwards compatibility but ignored:
        schema hydration is performed by load_tcs() calling
        tcs._update_schema() directly — no caller passes schema here.
        """
        super()._make_tcs_controller(msg=None, **_schema)

        # Deferred import to prevent circular dependency at module load time
        # DO NOT MOVE to module level.
        from ramses_rf.systems import Evohome, system_factory

        if not self.tcs:
            tcs = system_factory(self, msg=msg)
            if isinstance(tcs, Evohome):
                self.tcs = tcs

        assert self.tcs is not None


class Programmer(Controller):  # PRG (23):
    """The Controller base class."""

    _SLUG = DevType.PRG


class RfgGateway(DeviceHeat):  # RFG (30:)
    """The RFG100 base class."""

    _SLUG = DevType.RFG

    _STATE_ATTR = None


class UfhController(Parent["UfhCircuit"], DeviceHeat):  # UFC (02):
    """The UFC class, the HCE80 that controls the UFH zones."""

    HEAT_DEMAND: Final = SZ_HEAT_DEMAND

    _SLUG = DevType.UFC
    _STATE_ATTR = HEAT_DEMAND

    _child_id = FA
    _iz_controller = True

    childs: list[UfhCircuit]
    ufh_state: UfhState | None = None

    # 12:27:24.398 067  I --- 02:000921 --:------ 01:191718 3150 002 0360
    # 12:27:24.546 068  I --- 02:000921 --:------ 01:191718 3150 002 065A
    # 12:27:24.693 067  I --- 02:000921 --:------ 01:191718 3150 002 045C
    # 12:27:24.824 059  I --- 01:191718 --:------ 01:191718 3150 002 FC5C
    # 12:27:24.857 067  I --- 02:000921 --:------ 02:000921 3150 006 0060-015A-025C

    def __init__(  # type: ignore[explicit-any]  # heterogeneous construction/schema kwargs
        self, *args: Any, traits: DeviceTraits | None = None, **kwargs: Any
    ) -> None:
        """Initialise the UfhController device.

        :param args: Positional arguments passed to parent class.
        :type args: Any
        :param traits: Optional device traits metadata.
        :type traits: DeviceTraits | None
        :param kwargs: Keyword arguments passed to parent class.
        :type kwargs: Any
        """
        super().__init__(*args, traits=traits, **kwargs)
        self._init_ufh_state()

    def _init_ufh_state(self) -> None:
        """Initialize UFH-specific instance attributes (idempotent)."""
        self.__dict__.setdefault(
            "circuit_by_id", {f"{i:02X}": {} for i in range(8)}
        )

    def get_circuit(  # type: ignore[explicit-any]  # heterogeneous construction/schema kwargs
        self, ufh_index: str, *, msg: Message | None = None, **schema: Any
    ) -> UfhCircuit:
        """Return a UFH circuit, create it if required.

        First, use the schema to create/update it, then pass it any msg
        to handle.

        Circuits are uniquely identified by a UFH controller ID|cct_index
        pair. If a circuit is created, attach it to this UFC.

        :param ufh_index: Two-character hexadecimal circuit index string.
        :type ufh_index: str
        :param msg: Optional packet message triggering creation.
        :type msg: Message | None
        :param schema: Circuit schema parameters.
        :type schema: Any
        :returns: The created or retrieved UfhCircuit instance.
        :rtype: UfhCircuit
        """
        schema = {}  # shrink(SCH_CCT(schema))

        child = self.child_by_id.get(ufh_index)
        cct = child if isinstance(child, UfhCircuit) else None
        if not cct:
            cct = UfhCircuit(self, ufh_index)
            self.child_by_id[ufh_index] = cct
            self.childs.append(cct)

        elif schema:
            cct._update_schema(**schema)

        return cct

    @property
    def circuits(self) -> list[UfhCircuit]:
        """Return the list of child circuits for this UFH controller.

        :returns: List of child UfhCircuit entities.
        :rtype: list[UfhCircuit]
        """
        return list(self.childs)

    async def heat_demand(
        self,
    ) -> float | None:  # 3150|FC (there is also 3150|FA)
        """Return the overall heating demand percentage (0.0 to 1.0).

        :returns: Heating demand as a fraction (0.0 to 1.0) or None.
        :rtype: float | None
        """
        state = getattr(self, "demand_state", None)
        return state.heat_demand if state else None

    async def heat_demand_fc(
        self,
    ) -> float | None:  # 3150|FC
        """Return the primary domain (FC) heating demand percentage.

        Alias for :meth:`heat_demand`.

        :returns: Primary domain heat demand as a fraction or None.
        :rtype: float | None
        """
        return await self.heat_demand()

    async def heat_demand_fa(
        self,
    ) -> float | None:  # 3150|FA
        """Return the secondary domain (FA) heating demand percentage.

        :returns: Secondary domain heat demand as a fraction or None.
        :rtype: float | None
        """
        state = getattr(self, "ufh_state", None)
        return state.relay_demand_fa if state else None

    async def thermal_demands(self) -> list[UfhCircuitDemandDTO] | None:
        """Return the UFH circuit thermal demands as CQRS DTOs.

        :returns: List of circuit demand DTOs or None.
        :rtype: list[UfhCircuitDemandDTO] | None
        """
        state = getattr(self, "ufh_state", None)
        if state and state.heat_demands:
            return [
                UfhCircuitDemandDTO(ufh_index=str(k), thermal_demand=v)
                for k, v in state.heat_demands.items()
            ]
        return None

    async def cooling_demands(self) -> list[UfhCircuitDemandDTO] | None:
        """Return the UFH circuit cooling demands as CQRS DTOs.

        :returns: List of circuit demand DTOs in COOL mode, or None.
        :rtype: list[UfhCircuitDemandDTO] | None
        """
        state = getattr(self, "ufh_state", None)
        if state and state.cooling_demands:
            return [
                UfhCircuitDemandDTO(
                    ufh_index=str(k),
                    thermal_demand=v,
                    mode=ThermalMode.COOL,
                )
                for k, v in state.cooling_demands.items()
            ]
        return None

    async def circuit_modes(
        self,
    ) -> dict[str, ThermalMode | str | None] | None:
        """Return the operating modes keyed by circuit index.

        :returns: Mapping from circuit index to mode, or None.
        :rtype: dict[str, ThermalMode | str | None] | None
        """
        state = getattr(self, "ufh_state", None)
        return state.circuit_modes if state else None

    async def relay_demand(self) -> float | None:  # 0008|FC
        """Return the primary relay demand percentage (0.0 to 1.0).

        :returns: Primary relay demand fraction or None.
        :rtype: float | None
        """
        state = getattr(self, "demand_state", None)
        return state.relay_demand if state else None

    async def relay_demand_fa(self) -> float | None:  # 0008|FA
        """Return the secondary relay demand percentage (0.0 to 1.0).

        :returns: Secondary FA relay demand fraction or None.
        :rtype: float | None
        """
        state = getattr(self, "ufh_state", None)
        return state.relay_demand_fa if state else None

    async def pump_relay_state(self) -> PumpRelayState | None:  # 3EF0
        """Return the UFC pump heating/cooling relay state.

        Falls back to evaluating the secondary relay demand (`relay_demand_fa`)
        if no explicit 3EF0 packet has been received from the HCE80.

        :returns: 'cooling', 'heating', 'off', or None.
        :rtype: PumpRelayState | None
        """
        state = getattr(self, "ufh_state", None)
        if state and getattr(state, "pump_relay_state", None) is not None:
            pump_state = state.pump_relay_state
            if isinstance(pump_state, PumpRelayState):
                return pump_state

        relay_demand_fa_value = await self.relay_demand_fa()
        if relay_demand_fa_value is not None:
            if relay_demand_fa_value > 0.0:
                return PumpRelayState.HEATING
            return PumpRelayState.OFF

        return None

    async def setpoints(self) -> SchemaT | None:  # 22C9|ufh_index array
        """Return the UFH setpoints dictionary.

        # TODO: Refactor for #714 (CQRS API Boundaries).
        # This is a legacy shim to maintain backward compatibility with ramses_cc.

        :returns: Setpoints dictionary mapped by circuit index, or None.
        :rtype: SchemaT | None
        """
        state = getattr(self, "ufh_state", None)
        if state is None:
            return None

        result = state.setpoints
        return result if isinstance(result, dict) else None

    async def schema(self) -> SchemaT:
        """Return the device circuit configuration schema.

        :returns: Schema dictionary containing circuits mapping.
        :rtype: SchemaT
        """
        base_schema = await super().schema()
        circuits_schema: dict[str, dict[str, str | None]] = {}
        fallback_circuits = getattr(self, "circuit_by_id", {})
        if self.circuits:
            for circuit in self.circuits:
                circuit_entry: dict[str, str | None] = {}
                fallback_entry = (
                    fallback_circuits.get(circuit.ufh_index, {})
                    if isinstance(fallback_circuits, dict)
                    else {}
                )
                fallback_zone_index = (
                    fallback_entry.get(SZ_ZONE_INDEX)
                    if isinstance(fallback_entry, dict)
                    else None
                )
                zone_index = circuit.zone_index or fallback_zone_index
                if zone_index is not None:
                    circuit_entry[SZ_ZONE_INDEX] = str(zone_index)
                elif (
                    isinstance(fallback_entry, dict)
                    and SZ_ZONE_INDEX in fallback_entry
                ):
                    circuit_entry[SZ_ZONE_INDEX] = fallback_entry[
                        SZ_ZONE_INDEX
                    ]
                circuits_schema[circuit.ufh_index] = circuit_entry
        elif isinstance(fallback_circuits, dict):
            for ufh_index, raw_entry in fallback_circuits.items():
                if isinstance(raw_entry, dict):
                    fallback_zone_index = raw_entry.get(SZ_ZONE_INDEX)
                    entry: dict[str, str | None] = {}
                    if fallback_zone_index is not None:
                        entry[SZ_ZONE_INDEX] = str(fallback_zone_index)
                    elif SZ_ZONE_INDEX in raw_entry:
                        entry[SZ_ZONE_INDEX] = raw_entry[SZ_ZONE_INDEX]
                    circuits_schema[str(ufh_index)] = entry

        return {
            **base_schema,
            SZ_CIRCUITS: circuits_schema,
        }

    async def params(self) -> SchemaT:
        """Return the device setpoints and parameter values.

        :returns: Parameter dictionary containing circuit setpoints.
        :rtype: SchemaT
        """
        base_params = await super().params()
        return {
            **base_params,
            SZ_CIRCUITS: await self.setpoints(),
        }

    async def status(self) -> SchemaT:
        """Return the current operating status dictionary.

        :returns: Status dictionary with heat demand and relay demands.
        :rtype: SchemaT
        """
        base_status = await super().status()
        return {
            **base_status,
            SZ_HEAT_DEMAND: await self.heat_demand(),
            SZ_RELAY_DEMAND: await self.relay_demand(),
            f"{SZ_RELAY_DEMAND}_fa": await self.relay_demand_fa(),
        }


class UfhCircuit(Child, Entity):
    """The UFH circuit class (UFC:circuit is much like CTL/TCS:zone).

    NOTE: for circuits, there's a difference between:
     - `self.ctl`: the UFH controller, and
     - `self.tcs.ctl`: the Evohome controller
    """

    _SLUG: str = "CCT"
    _STATE_ATTR: str | None = None

    def __init__(self, ufc: UfhController, ufh_index: str) -> None:
        """Initialise the UFH circuit entity.

        :param ufc: Parent UFH controller device.
        :type ufc: UfhController
        :param ufh_index: 2-character hexadecimal circuit index string.
        :type ufh_index: str
        """
        super().__init__(ufc._gateway)

        # Context required by child entities (parent device ID and
        # circuit index)
        self._z_id = ufc.id
        self._z_index = DevIndexT(ufh_index)

        self.id: DeviceIdT = cast(DeviceIdT, f"{ufc.id}_{ufh_index}")

        self.ufc: UfhController = ufc
        self._child_id = ufh_index

        # self.ufc is the 02: HCE80 controller; self._ctl is the optional
        # bound 01: Evohome controller
        self._ctl: Controller | None = None
        self._zone: Zone | None = None

    @property
    def _state(self) -> UfhCircuitState | None:
        """Return the CQRS state slice for this circuit, if available."""
        ufh_state = getattr(self.ufc, "ufh_state", None)
        if isinstance(ufh_state, UfhState):
            circuit_state = ufh_state.circuits.get(str(self._child_id))
            if isinstance(circuit_state, UfhCircuitState):
                return circuit_state
        return None

    def set_zone(self, zone: Zone | None) -> None:
        """Set or update the associated heating zone for this circuit.

        :param zone: Associated heating zone entity or None.
        :type zone: Zone | None
        """
        self._zone = zone

    def _update_schema(  # type: ignore[explicit-any]  # heterogeneous construction/schema kwargs
        self, *, zone: Zone | None = None, **kwargs: Any
    ) -> None:
        """Update circuit schema parameters.

        :param zone: Associated zone entity if known.
        :type zone: Zone | None
        :param kwargs: Additional schema keyword arguments.
        :type kwargs: Any
        """
        if zone is not None:
            self.set_zone(zone)
        if kwargs:
            _LOGGER.debug(
                "%s: Unhandled schema update attributes: %s",
                self,
                list(kwargs),
            )

    @property
    def zone(self) -> Zone | None:
        """Return the associated heating zone entity or None.

        :returns: Associated heating zone entity or None.
        :rtype: Zone | None
        """
        return self._zone

    @property
    def ufh_index(self) -> str:
        """Return the UFH circuit index string.

        :returns: 2-character hexadecimal circuit index string.
        :rtype: str
        """
        return str(self._child_id)

    @property
    def circuit_index(self) -> str:
        """Return the UFH circuit index string (alias for ufh_index).

        :returns: 2-character hexadecimal circuit index string.
        :rtype: str
        """
        return self.ufh_index

    @property
    def zone_index(self) -> str | None:
        """Return the associated zone index string or None.

        :returns: Associated heating zone index, or None.
        :rtype: str | None
        """
        if self._zone:
            return str(self._zone._child_id)
        state = self._state
        return state.zone_index if state else None

    async def name(self) -> str | None:
        """Return the friendly name of the UFH circuit.

        If bound to a zone, formats as '{zone_name} Circuit {ufh_index}'.
        Otherwise, returns None.

        :returns: The circuit friendly name or None.
        :rtype: str | None
        """
        if self._zone:
            zone_name = await self._zone.name()
            if zone_name:
                return f"{zone_name} Circuit {self.ufh_index}"
        return None

    @property
    def heat_demand(self) -> float | None:
        """Return the heating demand percentage (0.0 to 1.0) or None.

        :returns: Heating demand as fraction, or None.
        :rtype: float | None
        """
        state = self._state
        return state.heat_demand if state else None

    @property
    def cooling_demand(self) -> float | None:
        """Return the cooling demand percentage (0.0 to 1.0) or None.

        :returns: Cooling demand as fraction, or None.
        :rtype: float | None
        """
        state = self._state
        return state.cooling_demand if state else None

    @property
    def circuit_mode(self) -> ThermalMode | str | None:
        """Return the circuit thermal operating mode or None.

        :returns: Operating mode or None.
        :rtype: ThermalMode | str | None
        """
        state = self._state
        return state.circuit_mode if state else None

    @property
    def setpoint(self) -> float | None:
        """Return the target setpoint temperature in °C or None.

        :returns: Target setpoint temperature or None.
        :rtype: float | None
        """
        state = self._state
        return state.setpoint if state else None

    @property
    def min_temp(self) -> float | None:
        """Return the minimum allowable setpoint temperature or None.

        :returns: Minimum temperature or None.
        :rtype: float | None
        """
        state = self._state
        return state.min_temp if state else None

    @property
    def max_temp(self) -> float | None:
        """Return the maximum allowable setpoint temperature or None.

        :returns: Maximum temperature or None.
        :rtype: float | None
        """
        state = self._state
        return state.max_temp if state else None

    @property
    def flags(self) -> int | None:
        """Return the raw mode/configuration flags integer or None.

        :returns: Flags integer or None.
        :rtype: int | None
        """
        state = self._state
        return state.flags if state else None

    def to_dto(self) -> UfhCircuitDTO:
        """Return an immutable CQRS DTO snapshot of this circuit entity.

        :returns: DTO snapshot representing this circuit.
        :rtype: UfhCircuitDTO
        """
        return UfhCircuitDTO(
            ufh_index=self.ufh_index,
            zone_index=self.zone_index,
            heat_demand=self.heat_demand,
            cooling_demand=self.cooling_demand,
            circuit_mode=self.circuit_mode,
            setpoint=self.setpoint,
            min_temp=self.min_temp,
            max_temp=self.max_temp,
            flags=self.flags,
        )
