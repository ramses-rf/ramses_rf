"""RAMSES RF - Heating Sensor Devices."""

from __future__ import annotations

import dataclasses
import logging
from typing import TYPE_CHECKING, Any, Final, cast

from ramses_rf.const import FA, SZ_TEMPERATURE, Code, DevType
from ramses_rf.enums import Action
from ramses_rf.models import DeviceTraits
from ramses_tx import Packet
from ramses_tx.typing import PayDictT

from .dev_base import BatteryState, DeviceHeat, Fakeable
from .helpers import send_fake_intent

_LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..messages import Message


class Weather(DeviceHeat):  # 0002
    TEMPERATURE: Final = SZ_TEMPERATURE  # TODO: deprecate

    async def temperature(self) -> float | None:  # 0002
        return self.temp_state.temperature

    async def set_temperature(self, value: float | None) -> Packet | None:
        """Fake the outdoor temperature of the sensor."""
        # Update local state immediately so the temperature is available
        # even if the RF command times out (e.g. faked devices on a simulator)
        self.temp_state = dataclasses.replace(self.temp_state, temperature=value)
        return await send_fake_intent(
            self, Action.PUT_OUTDOOR_TEMP, {"temperature": value}
        )

    async def status(self) -> dict[str, Any]:
        base_status = await super().status()
        return {
            **base_status,
            self.TEMPERATURE: await self.temperature(),
        }


class DhwTemperature(DeviceHeat):  # 1260
    TEMPERATURE: Final = SZ_TEMPERATURE  # TODO: deprecate

    async def temperature(self) -> float | None:  # 1260
        return self.temp_state.temperature

    async def set_temperature(self, value: float | None) -> Packet | None:
        """Fake the DHW temperature of the sensor."""
        # Update local state immediately so the temperature is available
        # even if the RF command times out (e.g. faked devices on a simulator)
        self.temp_state = dataclasses.replace(self.temp_state, temperature=value)
        return await send_fake_intent(self, Action.PUT_DHW_TEMP, {"temperature": value})

    async def status(self) -> dict[str, Any]:
        base_status = await super().status()
        return {
            **base_status,
            self.TEMPERATURE: await self.temperature(),
        }


class Temperature(DeviceHeat):  # 30C9
    # .I --- 34:145039 --:------ 34:145039 1FC9 012 00-30C9-8A368F 00-1FC9-8A368F
    # .W --- 01:054173 34:145039 --:------ 1FC9 006 03-2309-04D39D  # real CTL
    # .I --- 34:145039 01:054173 --:------ 1FC9 006 00-30C9-8A368F
    async def temperature(self) -> float | None:  # 30C9
        return self.temp_state.temperature

    async def set_temperature(self, value: float | None) -> Packet | None:
        """Fake the indoor temperature of the sensor."""
        # Update local state immediately so the temperature is available
        # even if the RF command times out (e.g. faked devices on a simulator)
        self.temp_state = dataclasses.replace(self.temp_state, temperature=value)
        return await send_fake_intent(
            self, Action.PUT_SENSOR_TEMP, {"temperature": value}
        )

    async def status(self) -> dict[str, Any]:
        base_status = await super().status()
        return {
            **base_status,
            SZ_TEMPERATURE: await self.temperature(),
        }


class DhwSensor(DhwTemperature, BatteryState, Fakeable):  # DHW (07): 10A0, 1260
    """The DHW class, such as a CS92."""

    DHW_PARAMS: Final = "dhw_params"

    _SLUG: str = DevType.DHW
    _STATE_ATTR = DhwTemperature.TEMPERATURE

    def __init__(
        self, *args: Any, traits: DeviceTraits | None = None, **kwargs: Any
    ) -> None:
        super().__init__(*args, traits=traits, **kwargs)

        self._child_id = FA  # NOTE: domain_id

    async def initiate_binding_process(
        self,
    ) -> tuple[Packet, Message, Packet, Packet | None]:
        return await super()._initiate_binding_process(Code._1260)

    async def dhw_params(self) -> PayDictT._10A0 | None:
        return cast(
            PayDictT._10A0 | None, await self.entity_state.get_value(Code._10A0)
        )

    async def params(self) -> dict[str, Any]:
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

    def __init__(
        self, *args: Any, traits: DeviceTraits | None = None, **kwargs: Any
    ) -> None:
        super().__init__(*args, traits=traits, **kwargs)

    # async def initiate_binding_process(self) -> tuple[Packet, Message, Packet, Packet | None]:
    #     return await super()._initiate_binding_process(...)
