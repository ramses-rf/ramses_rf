"""RAMSES RF - Heating Actuator Devices."""

from __future__ import annotations

from datetime import timedelta as td
from typing import Any, Final

from ramses_rf.const import (
    DOMAIN_TYPE_MAP,
    HEARTBEAT_TIMEOUT_BDR,
    SZ_BDR_STATE,
    SZ_HEAT_DEMAND,
    SZ_JIM_STATE,
    SZ_RELAY_DEMAND,
    DevType,
)
from ramses_rf.models import (
    ActuatorCycleDTO,
    BdrStateDTO,
    DeviceTraits,
    JimStateDTO,
)
from ramses_rf.typing import SchemaT
from ramses_tx import Priority
from ramses_tx.const import SZ_PRIORITY, Code
from ramses_tx.typing import PayDictT

from .dev_base import DeviceHeat

QOS_LOW = {SZ_PRIORITY: Priority.LOW}  # FIXME:  deprecate QoS in kwargs


class Actuator(DeviceHeat):  # 3EF0, 3EF1 (for 10:/13:)
    """Heating actuator base class (e.g. 10: / 13: devices)."""

    # .I --- 13:109598 --:------ 13:109598 3EF0 003 00C8FF                # event-driven, 00/C8
    # RP --- 13:109598 18:002563 --:------ 0008 002 00C8                  # 00/C8, as above
    # RP --- 13:109598 18:002563 --:------ 3EF1 007 0000BF-00BFC8FF       # 00/C8, as above

    # RP --- 10:048122 18:140805 --:------ 3EF1 007 007FFF-003C2A10       # 10:s only RP, always 7FFF
    # RP --- 13:109598 18:199952 --:------ 3EF1 007 0001B8-01B800FF       # 13:s only RP

    # RP --- 10:047707 18:199952 --:------ 3EF0 009 001110-0A00FF-033100  # 10:s only RP
    # RP --- 10:138926 34:010253 --:------ 3EF0 006 002E11-0000FF         # 10:s only RP
    # .I --- 13:209679 --:------ 13:209679 3EF0 003 00C8FF                # 13:s only  I

    ACTUATOR_CYCLE: Final = "actuator_cycle"
    ACTUATOR_ENABLED: Final = "actuator_enabled"  # boolean
    ACTUATOR_STATE: Final = "actuator_state"
    MODULATION_LEVEL: Final = "modulation_level"  # percentage (0.0-1.0)

    async def actuator_cycle(self) -> ActuatorCycleDTO | None:  # 3EF1
        """Return the actuator cycle state as a CQRS DTO.

        :returns: ActuatorCycleDTO or None.
        :rtype: ActuatorCycleDTO | None
        """
        state = getattr(self, "act_state", None)
        if not state:
            return None

        return ActuatorCycleDTO(
            actuator_countdown=state.actuator_countdown,
            cycle_countdown=state.cycle_countdown,
            actuator_enabled=state.actuator_enabled,
            modulation_level=state.modulation_level,
        )

    async def bdr_state(self) -> BdrStateDTO | None:  # 3EF0
        """Return the BDR actuator modulation state as a CQRS DTO.

        :returns: BdrStateDTO or None.
        :rtype: BdrStateDTO | None
        """
        state = getattr(self, "act_state", None)
        if not state:
            return None

        return BdrStateDTO(
            modulation_level=state.modulation_level,
            actuator_enabled=state.actuator_enabled,
            last_updated=state.last_updated,
        )

    async def actuator_state(self) -> BdrStateDTO | None:  # 3EF0
        """Return the actuator modulation state as a CQRS DTO.

        :returns: BdrStateDTO or None.
        :rtype: BdrStateDTO | None
        """
        return await self.bdr_state()

    async def status(self) -> SchemaT:
        """Return the current operating status dictionary."""
        base_status = await super().status()
        return {
            **base_status,
            self.ACTUATOR_CYCLE: await self.actuator_cycle(),
            self.ACTUATOR_STATE: await self.actuator_state(),
        }


class HeatDemand(DeviceHeat):  # 3150
    """Mixin for devices providing a heat demand percentage."""

    HEAT_DEMAND: Final = SZ_HEAT_DEMAND  # percentage valve open (0.0-1.0)

    async def heat_demand(self) -> float | None:  # 3150
        """Return the current heat demand percentage (0.0 to 1.0)."""
        return self.demand_state.heat_demand

    async def status(self) -> SchemaT:
        """Return the current operating status dictionary."""
        base_status = await super().status()
        return {
            **base_status,
            self.HEAT_DEMAND: await self.heat_demand(),
        }


class RelayDemand(DeviceHeat):  # 0008
    """Mixin for devices tracking relay demand percentage."""

    # .I --- 01:054173 --:------ 01:054173 1FC9 018 03-0008-04D39D FC-3B00-04D39D 03-1FC9-04D39D
    # .W --- 13:123456 01:054173 --:------ 1FC9 006 00-3EF0-35E240
    # .I --- 01:054173 13:123456 --:------ 1FC9 006 00-FFFF-04D39D

    # Some either 00/C8, others 00-C8
    # .I --- 01:145038 --:------ 01:145038 0008 002 0314  # ZON valve zone (ELE too?)
    # .I --- 01:145038 --:------ 01:145038 0008 002 F914  # HTG valve
    # .I --- 01:054173 --:------ 01:054173 0008 002 FA00  # DHW valve
    # .I --- 01:145038 --:------ 01:145038 0008 002 FC14  # appliance_relay

    # RP --- 13:109598 18:199952 --:------ 0008 002 0000
    # RP --- 13:109598 18:199952 --:------ 0008 002 00C8

    RELAY_DEMAND: Final = SZ_RELAY_DEMAND  # percentage (0.0-1.0)

    async def relay_demand(self) -> float | None:  # 0008
        """Return the current relay demand percentage (0.0 to 1.0)."""
        return self.demand_state.relay_demand

    async def status(self) -> SchemaT:
        """Return the current operating status dictionary."""
        base_status = await super().status()
        return {
            **base_status,
            self.RELAY_DEMAND: await self.relay_demand(),
        }


class BdrSwitch(Actuator, RelayDemand):  # BDR (13):
    """The BDR class, such as a BDR91.

    BDR91s can be used in six distinct modes, including:

    - x2 boiler controller (FC/TPI): either traditional, or newer heat pump-aware
    - x1 electric heat zones (0x/ELE)
    - x1 zone valve zones (0x/VAL)
    - x2 DHW thingys (F9/DHW, FA/DHW)
    """

    ACTIVE: Final = "active"
    BDR_STATE: Final = SZ_BDR_STATE
    TPI_PARAMS: Final = "tpi_params"

    _SLUG = DevType.BDR
    _STATE_ATTR = "active"

    # BDR91s are mains-powered relays that only transmit 3EF0 on state
    # changes (on/off).  When in a steady state they can go many hours
    # without sending any RF traffic, so the default 1-hour timeout is
    # far too short — use 24 hours (issue 1010).
    _HEARTBEAT_TIMEOUT: td = HEARTBEAT_TIMEOUT_BDR

    def __init__(  # type: ignore[explicit-any]  # heterogeneous construction/schema kwargs
        self, *args: Any, traits: DeviceTraits | None = None, **kwargs: Any
    ) -> None:
        """Initialize the BDR switch device."""
        super().__init__(*args, traits=traits, **kwargs)

    async def active(self) -> bool | None:  # 3EF0, 3EF1
        """Return the actuator's current state."""
        state = getattr(self, "act_state", None)
        if state and state.modulation_level is not None:
            return bool(state.modulation_level)
        return None

    async def relay_demand(self) -> float | None:
        """Return the relay demand of the BDR91."""
        if (demand := await super().relay_demand()) is not None:
            return demand
        state = getattr(self, "act_state", None)
        return state.modulation_level if state else None

    async def role(self) -> str | None:
        """Return the role of the BDR91A (there are six possibilities)."""
        # TODO: use self._parent?
        child_id = getattr(self, "_child_id", None)
        if child_id in DOMAIN_TYPE_MAP:
            return DOMAIN_TYPE_MAP[child_id]
        elif self._parent and type(self._parent).__name__ == "Zone":
            # TODO: remove need for string comparison hack
            return getattr(self._parent, "heating_type", None)

        # if Code._3B00 in _msgs and _msgs[Code._3B00].verb == I_:
        #     self._is_tpi = True
        # if Code._1FC9 in _msgs and _msgs[Code._1FC9].verb == RP:
        #     if Code._3B00 in _msgs[Code._1FC9].raw_payload:
        #         self._is_tpi = True

        return None

    async def tpi_params(self) -> PayDictT._1100 | None:
        """Return the TPI configuration parameters (1100) or None."""
        return await self.entity_state.get_value(Code._1100)

    async def schema(self) -> SchemaT:
        """Return the device configuration schema."""
        base_schema = await super().schema()
        return {
            **base_schema,
            "role": await self.role(),
        }

    async def params(self) -> SchemaT:
        """Return the device parameter dictionary."""
        base_params = await super().params()
        return {
            **base_params,
            self.TPI_PARAMS: await self.tpi_params(),
        }

    async def status(self) -> SchemaT:
        """Return the current operating status dictionary."""
        base_status = await super().status()
        return {
            **base_status,
            self.ACTIVE: await self.active(),
        }


class JimDevice(Actuator):  # BDR (08):
    """JIM actuator device class (08:)."""

    _SLUG: str = DevType.JIM
    _STATE_ATTR: str | None = None

    JIM_STATE: Final = SZ_JIM_STATE

    # JIM actuators are relay-type devices that, like BDR91s, only
    # transmit on state changes and can go many hours without RF
    # traffic (issue 1010).
    _HEARTBEAT_TIMEOUT: td = HEARTBEAT_TIMEOUT_BDR

    def __init__(  # type: ignore[explicit-any]  # heterogeneous construction/schema kwargs
        self, *args: Any, traits: DeviceTraits | None = None, **kwargs: Any
    ) -> None:
        """Initialize the JIM device."""
        super().__init__(*args, traits=traits, **kwargs)

    async def jim_state(self) -> JimStateDTO | None:  # 3EF0
        """Return the JIM appliance interface state as a CQRS DTO.

        :returns: JimStateDTO or None.
        :rtype: JimStateDTO | None
        """
        state = getattr(self, "act_state", None)
        if not state:
            return None

        return JimStateDTO(
            modulation_level=state.modulation_level,
            actuator_enabled=state.actuator_enabled,
            ch_active=state.ch_active,
            dhw_active=state.dhw_active,
            flame_active=state.flame_active,
            cooling_active=getattr(state, "cooling_active", None),
            last_updated=state.last_updated,
        )

    async def status(self) -> SchemaT:
        """Return the current operating status dictionary."""
        base_status = await super().status()
        return {
            **base_status,
            self.ACTUATOR_STATE: await self.jim_state(),
        }


class JstDevice(RelayDemand):  # BDR (31):
    """JST relay device class (31:)."""

    _SLUG: str = DevType.JST
    _STATE_ATTR: str | None = None

    # JST relays are relay-type devices that, like BDR91s, only
    # transmit on state changes and can go many hours without RF
    # traffic (issue 1010).
    _HEARTBEAT_TIMEOUT: td = HEARTBEAT_TIMEOUT_BDR

    def __init__(  # type: ignore[explicit-any]  # heterogeneous construction/schema kwargs
        self, *args: Any, traits: DeviceTraits | None = None, **kwargs: Any
    ) -> None:
        """Initialize the JST device."""
        super().__init__(*args, traits=traits, **kwargs)
