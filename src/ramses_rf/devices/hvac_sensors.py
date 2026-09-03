"""RAMSES RF - HVAC Sensor Devices."""

from __future__ import annotations

from datetime import timedelta as td
from typing import Any

from ramses_rf import exceptions as exc
from ramses_rf.address import Address
from ramses_rf.commands.core import Command as Intent
from ramses_rf.const import (
    HEARTBEAT_TIMEOUT_SENSOR,
    SZ_CO2_LEVEL,
    SZ_INDOOR_HUMIDITY,
    SZ_PRESENCE_DETECTED,
    SZ_TEMPERATURE,
    DevType,
)
from ramses_rf.enums import Action
from ramses_rf.messages import Message
from ramses_rf.models import DeviceTraits, HvacState
from ramses_rf.schemas import SZ_BOUND_TO
from ramses_rf.strategies import VentilationControlStrategy, best_hvac_strategy
from ramses_tx import Packet, Priority
from ramses_tx.const import Code
from ramses_tx.typing import DeviceIdT

from .dev_base import BatteryState, DeviceHvac, Fakeable


async def _send_hvac_sensor_intent(
    device: HvacSensorBase, action: Action, data: dict[str, Any]
) -> Message | None:
    """Fake the sensor reading by sending an intent."""
    if not device.is_faked:
        raise exc.DeviceNotFaked(f"{device}: Faking is not enabled")

    intent = Intent(
        src=Address(device.id),
        dst=Address(device.id),
        action=action,
        data=data,
    )
    return await device._gateway.dispatcher.send(
        intent, priority=Priority.HIGH
    )


class HvacSensorBase(DeviceHvac):
    """Base class for HVAC sensor devices.

    This class serves as a base for all sensor devices in the HVAC domain.
    It provides common functionality for sensor data collection and processing.
    """

    def __init__(
        self, *args: Any, traits: DeviceTraits | None = None, **kwargs: Any
    ) -> None:
        """Initialize the HvacSensorBase class.

        :param args: Positional arguments passed to the parent class
        :param traits: Strictly typed traits object for device creation
        :param kwargs: Keyword arguments passed to the parent class
        """
        super().__init__(*args, traits=traits, **kwargs)
        if not hasattr(self, "hvac_state"):
            self.hvac_state = HvacState()

    @property
    def heartbeat_timeout(self) -> td:
        """Return the timeout before the device is considered unavailable.

        :return: The timeout duration.
        :rtype: td
        """
        return HEARTBEAT_TIMEOUT_SENSOR


class CarbonDioxide(HvacSensorBase):  # 1298
    """The CO2 sensor (cardinal code is 1298)."""

    async def co2_level(self) -> int | None:
        """Get the CO2 level in ppm.

        :return: The CO2 level in parts per million (ppm), or None if not available
        :rtype: int | None
        """
        return self.hvac_state.co2_level

    async def set_co2_level(self, value: int | None) -> Message | None:
        """Set a fake CO2 level for the sensor.

        :param value: The CO2 level in ppm to set, or None to clear the fake value
        :type value: int | None
        :raises TypeError: If the sensor is not in faked mode
        :return: The sent message, or None if no response was returned
        :rtype: Message | None
        """
        return await _send_hvac_sensor_intent(
            self, Action.PUT_CO2_LEVEL, {"co2_level": value}
        )

    async def status(self) -> dict[str, Any]:
        """Return the status of the CO2 sensor.

        :return: A dictionary containing the sensor's status including CO2 level
        :rtype: dict[str, Any]
        """
        base_status = await super().status()
        return {
            **base_status,
            SZ_CO2_LEVEL: await self.co2_level(),
        }


class IndoorHumidity(HvacSensorBase):  # 12A0
    """The relative humidity sensor (12A0)."""

    async def indoor_humidity(self) -> float | None:
        """Get the indoor relative humidity.

        :return: The indoor relative humidity as a percentage (0-100), or None if not available
        :rtype: float | None
        """
        return self.hvac_state.indoor_humidity

    async def set_indoor_humidity(self, value: float | None) -> Message | None:
        """Set a fake indoor humidity value for the sensor.

        :param value: The humidity percentage to set (0-100), or None to clear the fake value
        :type value: float | None
        :raises TypeError: If the sensor is not in faked mode
        :return: The sent message, or None if no response was returned
        :rtype: Message | None
        """
        return await _send_hvac_sensor_intent(
            self, Action.PUT_INDOOR_HUMIDITY, {"indoor_humidity": value}
        )

    async def status(self) -> dict[str, Any]:
        """Return the status of the indoor humidity sensor.

        :return: A dictionary containing the sensor's status including humidity level
        :rtype: dict[str, Any]
        """
        base_status = await super().status()
        return {
            **base_status,
            SZ_INDOOR_HUMIDITY: await self.indoor_humidity(),
        }


class PresenceDetect(HvacSensorBase):  # 2E10
    """The presence sensor (2E10/31E0)."""

    # .I --- 37:154011 --:------ 37:154011 1FC9 030 00-31E0-96599B 00-1298-96599B 00-2E10-96599B 01-10E0-96599B 00-1FC9-96599B    # CO2, index|10E0 == 01
    # .W --- 28:126620 37:154011 --:------ 1FC9 012 00-31D9-49EE9C 00-31DA-49EE9C                                                 # FAN, BRDG-02A55
    # .I --- 37:154011 28:126620 --:------ 1FC9 001 00                                                                            # CO2, incl. integrated control, PIR

    async def presence_detected(self) -> bool | None:
        """Get the presence detection status.

        :return: True if presence is detected, False if not, None if status is unknown
        :rtype: bool | None
        """
        return self.hvac_state.presence_detected

    async def set_presence_detected(
        self, value: bool | None
    ) -> Message | None:
        """Set a fake presence detection state for the sensor.

        :param value: The presence state to set (True/False), or None to clear the fake value
        :type value: bool | None
        :raises TypeError: If the sensor is not in faked mode
        :return: The sent message, or None if no response was returned
        :rtype: Message | None
        """
        if not self.is_faked:
            raise exc.DeviceNotFaked(f"{self}: Faking is not enabled")

        intent = Intent(
            src=Address(self.id),
            dst=Address(self.id),
            action=Action.PUT_PRESENCE_DETECTED,
            data={"presence_detected": value},
        )
        return await self._gateway.dispatcher.send(
            intent, priority=Priority.HIGH
        )

    async def status(self) -> dict[str, Any]:
        """Return the status of the presence sensor.

        :return: A dictionary containing the sensor's status including presence detection state
        :rtype: dict[str, Any]
        """
        base_status = await super().status()
        return {
            **base_status,
            SZ_PRESENCE_DETECTED: await self.presence_detected(),
        }


class HvacHumiditySensor(
    BatteryState, IndoorHumidity, Fakeable
):  # HUM: I/12A0
    """The class for a humidity sensor.

    The cardinal code is 12A0.
    """

    _SLUG: str = DevType.HUM

    async def temperature(self) -> float | None:
        """Return the current temperature in Celsius.

        :return: The temperature in degrees Celsius, or None if not available
        :rtype: float | None
        """
        return self.hvac_state.temperature

    async def dewpoint_temp(self) -> float | None:
        """Return the dewpoint temperature in Celsius.

        :return: The dewpoint temperature in degrees Celsius, or None if not available
        :rtype: float | None
        """
        return self.hvac_state.dewpoint_temp

    async def status(self) -> dict[str, Any]:
        """Return the status of the humidity sensor.

        :return: A dictionary containing the sensor's status including temperature and humidity
        :rtype: dict[str, Any]
        """
        base_status = await super().status()
        return {
            **base_status,
            SZ_TEMPERATURE: await self.temperature(),
            "dewpoint_temp": await self.dewpoint_temp(),
        }


class HvacCarbonDioxideSensor(CarbonDioxide, Fakeable):  # CO2: I/1298
    """The class for a CO2 sensor.

    The cardinal code is 1298.
    """

    _SLUG: str = DevType.CO2

    # .I --- 29:181813 63:262142 --:------ 1FC9 030 00-31E0-76C635 01-31E0-76C635 00-1298-76C635 67-10E0-76C635 00-1FC9-76C635
    # .W --- 32:155617 29:181813 --:------ 1FC9 012 00-31D9-825FE1 00-31DA-825FE1  # The HRU
    # .I --- 29:181813 32:155617 --:------ 1FC9 001 00

    async def _resolve_fan_strategy(
        self, fan_id: DeviceIdT
    ) -> VentilationControlStrategy:
        """Resolve capability from the destination fan and its model."""
        fan = self._gateway.device_registry.get_device(fan_id)
        explicit_strategy: object = fan._strategy
        if isinstance(explicit_strategy, VentilationControlStrategy):
            return explicit_strategy

        info = await fan.entity_state.get_value(Code._10E0)
        model = info.get("description") if isinstance(info, dict) else None
        strategy = best_hvac_strategy(fan.id, fan._scheme, model=model)
        if not isinstance(strategy, VentilationControlStrategy):
            raise ValueError(f"{fan}: ventilation demand is not supported")
        return strategy

    def _bound_fan_id(self) -> DeviceIdT | None:
        """Return the configured bound fan ID, if present."""
        traits = self._gateway.config.known_list.get(self.id, {})
        bound = traits.get(SZ_BOUND_TO)
        device_ids = bound if isinstance(bound, list) else [bound]
        return next(
            (
                DeviceIdT(fan_id)
                for fan_id in device_ids
                if isinstance(fan_id, str) and fan_id.startswith("32:")
            ),
            None,
        )

    async def initiate_binding_process(
        self,
    ) -> tuple[Message, Message, Message, Packet | None]:
        """Initiate the binding process for the CO2 sensor.

        :return: The packets/messages generated during the binding process.
        :rtype: tuple[Message, Message, Message, Packet | None]
        :raises exc.BindingError: If binding fails
        """
        configured_strategy: object = self._get_configured_strategy()
        strategy = (
            configured_strategy
            if isinstance(configured_strategy, VentilationControlStrategy)
            else None
        )
        if not strategy and (fan_id := self._bound_fan_id()):
            strategy = await self._resolve_fan_strategy(fan_id)
        offer_codes = (
            strategy.co2_binding_codes()
            if strategy
            else (Code._31E0, Code._1298, Code._2E10)
        )
        return await super()._initiate_binding_process(offer_codes)

    async def set_ventilation_demand(
        self, fan_id: DeviceIdT, value: float
    ) -> Message | None:
        """Send a transient ventilation demand to a bound fan.

        :param fan_id: The device ID of the destination fan.
        :type fan_id: DeviceIdT
        :param value: The demand as a value from 0.0 to 1.0.
        :type value: float
        :returns: The resulting message, if one was generated.
        :rtype: Message | None
        :raises exc.DeviceNotFaked: If faking is not enabled.
        """
        if not self.is_faked:
            raise exc.DeviceNotFaked(f"{self}: Faking is not enabled")

        strategy = await self._resolve_fan_strategy(fan_id)
        intent = Intent(
            src=Address(self.id),
            dst=Address(fan_id),
            action=Action.PUT_VENTILATION_DEMAND,
            data={
                "ventilation_demand": value,
                "strategy": strategy,
            },
        )
        return await self._gateway.dispatcher.send(
            intent, priority=Priority.HIGH
        )
