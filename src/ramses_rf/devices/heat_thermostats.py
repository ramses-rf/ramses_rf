"""RAMSES RF - Heating Thermostat & Setpoint Devices."""

from __future__ import annotations

from datetime import timedelta as td
from typing import TYPE_CHECKING, Any, Final

from ramses_rf.const import (
    HEARTBEAT_TIMEOUT_TRV,
    SZ_HEAT_DEMAND,
    SZ_SETPOINT,
    SZ_WINDOW_OPEN,
    DevType,
)
from ramses_rf.models import DeviceTraits, TrvState
from ramses_rf.typing import SchemaT
from ramses_tx import Packet
from ramses_tx.const import Code

from .dev_base import BatteryState, Fakeable
from .heat_actuators import HeatDemand
from .heat_sensors import Temperature

if TYPE_CHECKING:
    from ..messages import Message


class Setpoint(Temperature):  # 2309
    """Mixin class for devices providing a temperature setpoint."""

    SETPOINT: Final = SZ_SETPOINT  # degrees Celsius

    async def setpoint(self) -> float | None:  # 2309
        """Return the target setpoint temperature in degrees Celsius."""
        return self.temp_state.setpoint

    async def status(self) -> SchemaT:
        """Return the current operating status dictionary."""
        base_status = await super().status()
        return {
            **base_status,
            self.SETPOINT: await self.setpoint(),
        }


class Thermostat(BatteryState, Setpoint, Fakeable):  # THM (..):
    """The THM/STA class, such as a TR87RF."""

    _SLUG = DevType.THM
    _STATE_ATTR = "temperature"

    def __init__(  # type: ignore[explicit-any]  # heterogeneous construction/schema kwargs
        self, *args: Any, traits: DeviceTraits | None = None, **kwargs: Any
    ) -> None:
        """Initialize the thermostat device."""
        super().__init__(*args, traits=traits, **kwargs)

    async def initiate_binding_process(
        self,
    ) -> tuple[Message, Message, Message, Packet | None]:
        """Initiate the RF binding handshake for the thermostat."""
        return await super()._initiate_binding_process(
            (Code._2309, Code._30C9, Code._0008)
        )


class TrvActuator(BatteryState, HeatDemand, Setpoint):  # TRV (04):
    """The TRV class, such as a HR92."""

    WINDOW_OPEN: Final = SZ_WINDOW_OPEN

    _SLUG = DevType.TRV
    _STATE_ATTR = SZ_HEAT_DEMAND
    _HEARTBEAT_TIMEOUT: td = HEARTBEAT_TIMEOUT_TRV

    def __init__(  # type: ignore[explicit-any]  # heterogeneous construction/schema kwargs
        self, *args: Any, traits: DeviceTraits | None = None, **kwargs: Any
    ) -> None:
        """Initialize the TRV actuator device."""
        super().__init__(*args, traits=traits, **kwargs)
        self.trv_state = TrvState()

    async def heat_demand(self) -> float | None:  # 3150
        """Return current heat demand percentage (0.0 to 1.0)."""
        if (heat_demand := self.demand_state.heat_demand) is None:
            if await self.setpoint() is False:
                return (
                    0  # instead of None (no 3150s sent when setpoint is False)
                )
        return heat_demand

    async def window_open(self) -> bool | None:  # 12B0
        """Return whether open-window detection is currently active."""
        return self.trv_state.window_open

    async def status(self) -> SchemaT:
        """Return the current operating status dictionary."""
        base_status = await super().status()
        return {
            **base_status,
            self.WINDOW_OPEN: await self.window_open(),
        }
