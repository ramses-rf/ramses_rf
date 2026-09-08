#!/usr/bin/env python3
"""RAMSES RF - Topology and Entity Relationships.

This module manages the graph relationships (Parent/Child) between RAMSES
entities, such as the association between a Zone and its Actuators.

# ARCHITECTURE NOTE: Topology & Heuristic Eavesdropping
#
# Determining bindings to a controller:
#  - Config: As per any explicitly loaded schema.
#  - Discovery: If in 000C packet, or packet *to* device where src is a controller.
#  - Eavesdrop: If packet *from* device where dst is a controller.
#
# Determining location in a schema (domain/DHW/zone):
#  - Config: As per any explicitly loaded schema.
#  - Discovery: If in 000C packet - (Note: unable to do this for 10: & 00: TRVs).
#  - Discovery: From packet fingerprint, excl. payloads (only for 10:).
#  - Eavesdrop: From packet fingerprint, incl. payloads.
#
# NOTE: L7 Messages are routed only to physical Devices. Routing to virtual entities
# (i.e., Systems, Zones, Circuits) is handled internally by those Devices (e.g., UFC to UfhCircuit).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from ramses_tx.const import F9, FA, FC, FF

from . import exceptions as exc
from .const import SZ_ACTUATORS, SZ_SENSOR
from .enums import DevType
from .interfaces import ControllerInterface, ParentInterface
from .schemas import SZ_CIRCUITS


@runtime_checkable
class _HasTcs(Protocol):
    tcs: ParentInterface | None


@runtime_checkable
class _HasZones(Protocol):
    _max_zones: int

    def get_dhw_zone(self) -> ParentInterface: ...

    def get_htg_zone(self, zone_index: str) -> ParentInterface: ...


if TYPE_CHECKING:
    from ramses_tx.typing import DeviceIdT

    from .systems.tcs import SystemBase


_LOGGER = logging.getLogger(__name__)
_TRACE = logging.getLogger("ramses_rf.legacy_trace")


class Child:
    """A Device can be the Child of a Parent (System, Zone, or UFH Controller).

    A Child maintains a reference to its Parent and relies on the central
    Event Bus for topological updates.
    """

    def __init__(
        self,
        *args: Any,
        parent: ParentInterface | None = None,
        is_sensor: bool | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the Child relationship manager.

        :param parent: The parent entity, if known.
        :type parent: ParentInterface | None
        :param is_sensor: Whether this entity is a sensor for the parent.
        :type is_sensor: bool | None
        """
        super().__init__(*args, **kwargs)

        self._parent: ParentInterface | None = parent
        self._is_sensor = is_sensor
        self._child_id: str | None = None
        self.ctl: ControllerInterface | None = None
        self.tcs: SystemBase | None = None

    def _get_parent(
        self,
        parent: ParentInterface | None,
        *,
        child_id: str | None = None,
        is_sensor: bool | None = None,
    ) -> tuple[ParentInterface, str | None]:
        """Validate and retrieve the target parent for this device.

        :param parent: The proposed parent.
        :type parent: ParentInterface | None
        :param child_id: The specific sub-index (e.g. F9, FA).
        :type child_id: str | None
        :param is_sensor: Whether the child is a sensor.
        :type is_sensor: bool | None
        :returns: A tuple of the validated parent and child_id.
        :rtype: tuple[ParentInterface, str | None]
        :raises SchemaInconsistentError: If validation rules are violated.
        """
        if parent is None:
            raise exc.SchemaInconsistentError(f"{self}: parent cannot be None")

        parent_class = parent.__class__.__name__
        self_class = self.__class__.__name__

        if self_class == "UfhController":
            child_id = FF

        if parent_class == "Controller":
            if isinstance(parent, _HasTcs) and parent.tcs is not None:
                parent = parent.tcs
                parent_class = parent.__class__.__name__
                _TRACE.info(
                    "SUB-CONTROLLER: %s shifted parent to %s",
                    self,
                    parent_class,
                )
            else:
                raise exc.SchemaInconsistentError(
                    f"{self}: controller parent tcs cannot be None"
                )

        if parent_class in ("Evohome", "System") and child_id:
            if child_id in (F9, FA):
                if isinstance(parent, _HasZones):
                    parent = parent.get_dhw_zone()
                    parent_class = parent.__class__.__name__
                    _TRACE.info(
                        "DHW SHIFT: %s shifted parent to %s",
                        self,
                        parent_class,
                    )
            elif (
                isinstance(parent, _HasZones)
                and int(child_id, 16) < parent._max_zones
            ):
                parent = parent.get_htg_zone(child_id)
                parent_class = parent.__class__.__name__
                _TRACE.info(
                    "ZONE SHIFT: %s shifted parent to %s", self, parent_class
                )

        elif (
            parent_class
            in (
                "Zone",
                "DhwZone",
                "EleZone",
                "MixZone",
                "RadZone",
                "UfhZone",
                "ValZone",
            )
            and not child_id
        ):
            child_id = child_id or getattr(parent, "index", None)

        if self._parent and self._parent != parent:
            prev_parent_class = self._parent.__class__.__name__
            if prev_parent_class in (
                "System",
                "Evohome",
            ) and parent_class not in (
                "System",
                "Evohome",
            ):
                _TRACE.info(
                    "PARENT PROMOTION: %s promoted parent from %s to %s",
                    self,
                    prev_parent_class,
                    parent_class,
                )
                dev_id = getattr(self, "id", None)
                if (
                    hasattr(self._parent, "actuators")
                    and self in self._parent.actuators
                ):
                    self._parent.actuators.remove(self)
                if (
                    hasattr(self._parent, "actuator_by_id")
                    and dev_id is not None
                    and dev_id in self._parent.actuator_by_id
                ):
                    del self._parent.actuator_by_id[dev_id]
            else:
                err_msg = (
                    f"{self} can't change parent "
                    f"({self._parent}_{self._child_id} to {parent}_{child_id})"
                )
                _TRACE.error("PARENT CHANGE EXCEPTION: %s", err_msg)
                raise exc.SystemSchemaInconsistent(err_msg)

        PARENT_RULES: dict[str, dict[str, tuple[str, ...]]] = {
            "DhwZone": {
                SZ_ACTUATORS: ("BdrSwitch",),
                SZ_SENSOR: ("DhwSensor",),
            },
            "System": {
                SZ_ACTUATORS: ("BdrSwitch", "OtbGateway", "UfhController"),
                SZ_SENSOR: ("OutSensor",),
            },
            "Evohome": {
                SZ_ACTUATORS: ("BdrSwitch", "OtbGateway", "UfhController"),
                SZ_SENSOR: ("OutSensor",),
            },
            "UfhController": {SZ_ACTUATORS: ("UfhCircuit",), SZ_SENSOR: ()},
            "Zone": {
                SZ_ACTUATORS: ("BdrSwitch", "TrvActuator", "UfhCircuit"),
                SZ_SENSOR: ("Controller", "Thermostat", "TrvActuator"),
            },
            "EleZone": {
                SZ_ACTUATORS: ("BdrSwitch", "TrvActuator", "UfhCircuit"),
                SZ_SENSOR: ("Controller", "Thermostat", "TrvActuator"),
            },
            "MixZone": {
                SZ_ACTUATORS: ("BdrSwitch", "TrvActuator", "UfhCircuit"),
                SZ_SENSOR: ("Controller", "Thermostat", "TrvActuator"),
            },
            "RadZone": {
                SZ_ACTUATORS: ("BdrSwitch", "TrvActuator", "UfhCircuit"),
                SZ_SENSOR: ("Controller", "Thermostat", "TrvActuator"),
            },
            "UfhZone": {
                SZ_ACTUATORS: ("BdrSwitch", "TrvActuator", "UfhCircuit"),
                SZ_SENSOR: ("Controller", "Thermostat", "TrvActuator"),
            },
            "ValZone": {
                SZ_ACTUATORS: ("BdrSwitch", "TrvActuator", "UfhCircuit"),
                SZ_SENSOR: ("Controller", "Thermostat", "TrvActuator"),
            },
        }

        rules = PARENT_RULES.get(parent_class)
        if not rules:
            _TRACE.error(
                "PARENT RULES EXCEPTION: %s is not a valid parent.", parent
            )
            raise exc.SchemaInconsistentError(
                f"for Parent {parent}: not a valid parent"
            )

        if is_sensor and self_class not in rules[SZ_SENSOR]:
            _TRACE.error(
                "RULES EXCEPTION: Sensor %s must be %s for parent %s",
                self,
                rules[SZ_SENSOR],
                parent,
            )
            raise exc.SchemaInconsistentError(
                f"for Parent {parent}: Sensor {self} must be {rules[SZ_SENSOR]}"
            )
        if not is_sensor and self_class not in rules[SZ_ACTUATORS]:
            _TRACE.error(
                "RULES EXCEPTION: Actuator %s must be %s for parent %s",
                self,
                rules[SZ_ACTUATORS],
                parent,
            )
            raise exc.SchemaInconsistentError(
                f"for Parent {parent}: Actuator {self} must be {rules[SZ_ACTUATORS]}"
            )

        return parent, child_id

    def _apply_topology_link(
        self,
        parent: ParentInterface | None,
        *,
        child_id: str | None = None,
        is_sensor: bool | None = None,
    ) -> ParentInterface:
        """Establish a topological link to a parent entity.

        This is a protected method that MUST only be called by the
        DeviceRegistry when processing a validated TopologyChangedEvent.

        :param parent: The parent to link to.
        :type parent: ParentInterface | None
        :param child_id: The specific sub-index.
        :type child_id: str | None
        :param is_sensor: Whether this child is a sensor.
        :type is_sensor: bool | None
        :returns: The validated parent entity.
        :rtype: ParentInterface
        :raises SystemSchemaInconsistent: If a controller conflict occurs.
        """
        try:
            parent, child_id = self._get_parent(
                parent, child_id=child_id, is_sensor=is_sensor
            )
            controller_candidate = (
                parent
                if getattr(parent, "_SLUG", None) == DevType.UFC
                or parent.__class__.__name__ == "UfhController"
                else getattr(parent, "ctl", None)
            )
            controller: ControllerInterface | None = (
                controller_candidate
                if isinstance(controller_candidate, ControllerInterface)
                else None
            )

            if self.ctl and self.ctl is not controller:
                raise exc.SystemSchemaInconsistent(
                    f"{self} can't change controller: {self.ctl} to {controller}"
                )

            parent._add_child(self, child_id=child_id, is_sensor=is_sensor)

        except (
            exc.SchemaInconsistentError,
            exc.SystemSchemaInconsistent,
        ) as err:
            _TRACE.error(
                "LINK EXCEPTION: Failed applying link for %s: %s", self, err
            )
            raise

        self._child_id = child_id
        self._parent = parent

        self.ctl = controller
        self.tcs = getattr(controller, "tcs", None)

        return parent


class Parent[ChildT: Child]:
    """A Parent can be a System (TCS), a heating Zone, or a UFH Controller.

    A Parent maintains a registry of Child entities and validates the
    relationships based on domain-specific rules.
    """

    actuator_by_id: dict[DeviceIdT, ChildT]
    actuators: list[ChildT]
    circuit_by_id: dict[str, ChildT]

    _app_cntrl: ChildT | None = None
    _dhw_sensor: ChildT | None = None
    _dhw_valve: ChildT | None = None
    _htg_valve: ChildT | None = None
    _sensor: ChildT | None = None

    def __init__(
        self, *args: Any, child_id: str | None = None, **kwargs: Any
    ) -> None:
        """Initialize the Parent relationship manager.

        :param child_id: The domain or zone index for this parent.
        :type child_id: str | None
        """
        super().__init__(*args, **kwargs)

        self._child_id: str | None = child_id
        self.child_by_id: dict[str, ChildT] = {}
        self.childs: list[ChildT] = []

    @property
    def zone_index(self) -> str | None:
        """Return the domain or zone index.

        :returns: The index string.
        :rtype: str | None
        """
        return self._child_id

    @zone_index.setter
    def zone_index(self, value: str | None) -> None:
        """Set the domain or zone index after validation.

        :param value: The new index.
        :type value: str | None
        """
        self._child_id = value

    def _add_child(
        self,
        child: ChildT,
        *,
        child_id: str | None = None,
        is_sensor: bool | None = None,
    ) -> None:
        """Add a child device to this Parent, validating the association.

        :param child: The child entity to add.
        :type child: ChildT
        :param child_id: The specific sub-index (e.g. F9, FA), optional.
        :type child_id: str | None
        :param is_sensor: Whether the child acts as a sensor, optional.
        :type is_sensor: bool | None
        :raises SystemSchemaInconsistent: If the child contradicts existing schema.
        :raises SchemaInconsistentError: If the combination is invalid.
        """
        child_id_val = getattr(child, "id", None)

        try:
            if is_sensor and child_id == FA:
                if (
                    self._dhw_sensor
                    and getattr(self._dhw_sensor, "id", None) != child_id_val
                ):
                    raise exc.SystemSchemaInconsistent(
                        f"{self} changed dhw_sensor (from {self._dhw_sensor} to {child})"
                    )
                self._dhw_sensor = child

            elif is_sensor and hasattr(self, SZ_SENSOR):
                existing_sensor = getattr(self, SZ_SENSOR, None)
                if (
                    existing_sensor
                    and getattr(existing_sensor, "id", None) != child_id_val
                    and getattr(existing_sensor, "type", None)
                    not in ("01", DevType.CTL)
                ):
                    raise exc.SystemSchemaInconsistent(
                        f"{self} changed zone sensor (from {existing_sensor} to {child})"
                    )
                self._sensor = child

            elif is_sensor:
                raise exc.SchemaInconsistentError(
                    f"not a valid combination for {self}: {child}|{child_id}|{is_sensor}"
                )

            elif hasattr(self, "circuit_by_id"):
                circuit_key = str(child_id_val or child)
                if circuit_key not in self.circuit_by_id:
                    self.circuit_by_id[circuit_key] = child

            elif hasattr(self, SZ_ACTUATORS):
                if (
                    child not in self.actuators
                    and child_id_val not in self.actuator_by_id
                ):
                    self.actuators.append(child)
                    if child_id_val is not None:
                        self.actuator_by_id[child_id_val] = child

            elif child_id == F9:
                if (
                    self._htg_valve
                    and getattr(self._htg_valve, "id", None) != child_id_val
                ):
                    raise exc.SystemSchemaInconsistent(
                        f"{self} changed htg_valve (from {self._htg_valve} to {child})"
                    )
                self._htg_valve = child

            elif child_id == FA:
                if (
                    self._dhw_valve
                    and getattr(self._dhw_valve, "id", None) != child_id_val
                ):
                    raise exc.SystemSchemaInconsistent(
                        f"{self} changed dhw_valve (from {self._dhw_valve} to {child})"
                    )
                self._dhw_valve = child

            elif child_id == FC:
                if self._app_cntrl and self._app_cntrl is not child:
                    raise exc.SystemSchemaInconsistent(
                        f"{self} changed app_cntrl (from {self._app_cntrl} to {child})"
                    )
                self._app_cntrl = child

            elif child_id == FF:
                pass

            else:
                raise exc.SchemaInconsistentError(
                    f"not a valid combination for {self}: {child}|{child_id}|{is_sensor}"
                )

        except (
            exc.SchemaInconsistentError,
            exc.SystemSchemaInconsistent,
        ) as err:
            _TRACE.error(
                "ADD_CHILD EXCEPTION: Validating %s to parent %s failed: %s",
                child,
                self,
                err,
            )
            raise

        if child not in self.childs:
            self.childs.append(child)
        self.child_by_id[str(child_id_val or child)] = child

    def _detach_child(self, child: ChildT) -> None:
        """Detach a child device from this Parent, maintaining referential integrity.

        This is the inverse of :meth:`_add_child`. It clears the slot the
        child occupied (sensor, valve, actuator, circuit, or appliance
        control), removes the child from the ``childs`` list and
        ``child_by_id`` registry, and clears the child's back-references.

        :param child: The child entity to detach.
        :type child: ChildT
        """
        # Clear the slot based on identity (not child_id, which may have
        # been mutated since the child was added)
        child_id_: DeviceIdT | None = getattr(child, "id", None)

        if getattr(self, "_dhw_sensor", None) is child:
            self._dhw_sensor = None
        elif getattr(self, "_dhw_valve", None) is child:
            self._dhw_valve = None
        elif getattr(self, "_htg_valve", None) is child:
            self._htg_valve = None
        elif getattr(self, "_app_cntrl", None) is child:
            self._app_cntrl = None
        elif (
            hasattr(self, SZ_SENSOR)
            and getattr(self, "_sensor", None) is child
        ):
            self._sensor = None
        elif hasattr(self, SZ_ACTUATORS) and child in getattr(
            self, "actuators", []
        ):
            self.actuators.remove(child)
            if child_id_ is not None:
                self.actuator_by_id.pop(child_id_, None)
        elif (
            child_id_ is not None
            and hasattr(self, SZ_CIRCUITS)
            and child_id_ in getattr(self, "circuit_by_id", {})
        ):
            self.circuit_by_id.pop(child_id_, None)

        # Remove from child registries
        if child in self.childs:
            self.childs.remove(child)
        if child_id_ is not None:
            self.child_by_id.pop(str(child_id_), None)

        # Clear the child's back-references
        child._parent = None
        child._child_id = None
