"""RAMSES RF - Heating Sensor Devices."""

from __future__ import annotations

import dataclasses
import logging
from datetime import timedelta as td
from typing import Any, Final

from ramses_rf.const import HEARTBEAT_TIMEOUT_DHW, SZ_TEMPERATURE, DevType
from ramses_rf.enums import Action, ZoneRole
from ramses_rf.messages import Message
from ramses_rf.models import DeviceTraits
from ramses_rf.typing import SchemaT
from ramses_tx import Packet
from ramses_tx.const import FA, RQ, Code
from ramses_tx.typing import PayDictT

from .dev_base import BatteryState, DeviceHeat, Fakeable
from .helpers import send_fake_intent

_LOGGER = logging.getLogger(__name__)


class Weather(DeviceHeat):  # 0002
    """Outdoor weather / temperature sensor base class."""

    async def temperature(self) -> float | None:  # 0002
        """Return the current outdoor temperature in degrees Celsius."""
        return self.temp_state.temperature

    async def set_temperature(self, value: float | None) -> Message | None:
        """Fake the outdoor temperature of the sensor."""
        # Update local state immediately so the temperature is available
        # even if the RF command times out (e.g. faked devices on a simulator)
        self.temp_state = dataclasses.replace(
            self.temp_state, temperature=value
        )
        return await send_fake_intent(
            self, Action.PUT_OUTDOOR_TEMP, {"temperature": value}
        )

    async def status(self) -> SchemaT:
        """Return the current operating status dictionary."""
        base_status = await super().status()
        return {
            **base_status,
            SZ_TEMPERATURE: await self.temperature(),
        }


class DhwTemperature(DeviceHeat):  # 1260
    """Domestic hot water temperature sensor mixin class."""

    async def temperature(self) -> float | None:  # 1260
        """Return the current DHW temperature in degrees Celsius."""
        return self.temp_state.temperature

    async def set_temperature(self, value: float | None) -> Message | None:
        """Fake the DHW temperature of the sensor."""
        # Update local state immediately so the temperature is available
        # even if the RF command times out (e.g. faked devices on a simulator)
        self.temp_state = dataclasses.replace(
            self.temp_state, temperature=value
        )
        return await send_fake_intent(
            self, Action.PUT_DHW_TEMP, {"temperature": value}
        )

    async def status(self) -> SchemaT:
        """Return the current operating status dictionary."""
        base_status = await super().status()
        return {
            **base_status,
            SZ_TEMPERATURE: await self.temperature(),
        }


class Temperature(DeviceHeat):  # 30C9
    """Indoor temperature sensor base class."""

    # .I --- 34:145039 --:------ 34:145039 1FC9 012 00-30C9-8A368F 00-1FC9-8A368F
    # .W --- 01:054173 34:145039 --:------ 1FC9 006 03-2309-04D39D  # real CTL
    # .I --- 34:145039 01:054173 --:------ 1FC9 006 00-30C9-8A368F
    async def temperature(self) -> float | None:  # 30C9
        """Return the current indoor temperature in degrees Celsius."""
        return self.temp_state.temperature

    async def set_temperature(self, value: float | None) -> Message | None:
        """Fake the indoor temperature of the sensor."""
        # Update local state immediately so the temperature is available
        # even if the RF command times out (e.g. faked devices on a simulator)
        self.temp_state = dataclasses.replace(
            self.temp_state, temperature=value
        )
        # Determine the zone_index from the parent zone (if bound) so that
        # UFH zone sensors emit 30C9 with the correct zone_index.  Without
        # this, the fake always sends index 00 and the UFC ignores it.
        # Only UFH zones need the parent's zone index — standard evohome
        # CTL zones (RAD/ELE/MIX/VAL) must send "00" (issue 929).
        # Evohome UFH zones are also CTL-bound: their sensors must use
        # "00" so RF binding selects the zone (ramses-rf/ramses_cc#929).
        zone_index = "00"
        if (
            self._parent is not None
            and hasattr(self._parent, "index")
            and getattr(self._parent, "_SLUG", None) == ZoneRole.UFH
            and getattr(self.ctl, "_SLUG", None) != DevType.CTL
        ):
            zone_index = self._parent.index
        return await send_fake_intent(
            self,
            Action.PUT_SENSOR_TEMP,
            {
                "temperature": value,
                "zone_index": zone_index,
            },
        )

    async def status(self) -> SchemaT:
        """Return the current operating status dictionary."""
        base_status = await super().status()
        return {
            **base_status,
            SZ_TEMPERATURE: await self.temperature(),
        }


class DhwSensor(
    DhwTemperature, BatteryState, Fakeable
):  # DHW (07): 10A0, 1260
    """The DHW class, such as a CS92."""

    DHW_PARAMS: Final = "dhw_params"

    _SLUG: str = DevType.DHW
    _STATE_ATTR = SZ_TEMPERATURE

    # DHW sensors (e.g. CS92) are battery-powered and only transmit on
    # temperature changes, not on a regular heartbeat schedule.  The
    # CTL polls them for 10A0 (DHW params) every 24 hours, so the
    # default 1-hour timeout is far too short — use 24 hours to match
    # the CTL's polling interval (issue 925).
    _HEARTBEAT_TIMEOUT: td = HEARTBEAT_TIMEOUT_DHW

    def __init__(  # type: ignore[explicit-any]  # heterogeneous construction/schema kwargs
        self, *args: Any, traits: DeviceTraits | None = None, **kwargs: Any
    ) -> None:
        """Initialize the DHW sensor device."""
        super().__init__(*args, traits=traits, **kwargs)

        self._child_id = FA  # NOTE: domain_id

    async def initiate_binding_process(
        self,
    ) -> tuple[Message, Message, Message, Packet | None]:
        """Initiate the RF binding handshake for the DHW sensor."""
        return await super()._initiate_binding_process(Code._1260)

    async def dhw_params(self) -> PayDictT._10A0 | None:
        """Return the DHW parameters (10A0) payload or None."""
        return await self.entity_state.get_value(
            Code._10A0, verb=RQ
        ) or await self.entity_state.get_value(Code._10A0)

    async def params(self) -> SchemaT:
        """Return the device parameter dictionary."""
        base_params = await super().params()
        return {
            **base_params,
            self.DHW_PARAMS: await self.dhw_params(),
        }


class OutSensor(Weather, Fakeable):  # OUT: 17
    """The OUT class (external sensor), such as a HB85/HB95."""

    # LUMINOSITY = "luminosity"  # lux
    # WINDSPEED = "windspeed"  # km/h

    _SLUG = DevType.OUT
    _STATE_ATTR = SZ_TEMPERATURE

    def __init__(  # type: ignore[explicit-any]  # heterogeneous construction/schema kwargs
        self, *args: Any, traits: DeviceTraits | None = None, **kwargs: Any
    ) -> None:
        """Initialize the outdoor sensor device."""
        super().__init__(*args, traits=traits, **kwargs)

    # async def initiate_binding_process(self) -> tuple[Packet, Message, Packet, Packet | None]:
    #     return await super()._initiate_binding_process(...)
