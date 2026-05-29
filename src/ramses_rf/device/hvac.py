#!/usr/bin/env python3
"""RAMSES RF - devices from the HVAC domain."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import timedelta as td
from typing import TYPE_CHECKING, Any, TypeVar

from ramses_rf import exceptions as exc
from ramses_rf.address import Address
from ramses_rf.const import (
    HEARTBEAT_TIMEOUT_FILTER,
    HEARTBEAT_TIMEOUT_REMOTE,
    HEARTBEAT_TIMEOUT_SENSOR,
    SZ_AIR_QUALITY,
    SZ_AIR_QUALITY_BASIS,
    SZ_BOOST_TIMER,
    SZ_BYPASS_MODE,
    SZ_BYPASS_POSITION,
    SZ_BYPASS_STATE,
    SZ_CO2_LEVEL,
    SZ_EXHAUST_FAN_SPEED,
    SZ_EXHAUST_FLOW,
    SZ_EXHAUST_TEMP,
    SZ_FAN_INFO,
    SZ_FAN_MODE,
    SZ_FAN_RATE,
    SZ_FILTER_DIRTY,
    SZ_FROST_CYCLE,
    SZ_HAS_FAULT,
    SZ_INDOOR_HUMIDITY,
    SZ_INDOOR_TEMP,
    SZ_OUTDOOR_HUMIDITY,
    SZ_OUTDOOR_TEMP,
    SZ_POST_HEAT,
    SZ_PRE_HEAT,
    SZ_PRESENCE_DETECTED,
    SZ_REMAINING_MINS,
    SZ_REQ_REASON,
    SZ_REQ_SPEED,
    SZ_SPEED_CAPABILITIES,
    SZ_SUPPLY_FAN_SPEED,
    SZ_SUPPLY_FLOW,
    SZ_SUPPLY_TEMP,
    SZ_TEMPERATURE,
    DevType,
)
from ramses_rf.entity import class_by_attr
from ramses_rf.helpers import schedule_task
from ramses_rf.models import HvacState
from ramses_tx import Command, Packet, Priority
from ramses_tx.typing import PayloadT

from ..messages import Message
from ..protocol.ramses import CODES_OF_HVAC_DOMAIN_ONLY, HVAC_KLASS_BY_VC_PAIR
from .base import BatteryState, DeviceHvac, Fakeable

from ramses_rf.const import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    I_,
    RP,
    RQ,
    W_,
    Code,
)

if TYPE_CHECKING:
    from ramses_rf.models import DeviceTraits

# TODO: Switch this module to utilise the (run-time) decorator design pattern...
# - https://refactoring.guru/design-patterns/decorator/python/example
# - will probably need setattr()?
# BaseComponents: FAN (HRU, PIV, EXT), SENsor (CO2, HUM, TEMp), SWItch (RFS gateway?)
# - a device could be a combination of above (e.g. Spider Gateway)
# Track binding for SWI (HA service call) & SEN (HA trigger) to FAN/other

# Challenges:
# - may need two-tier system (HVAC -> FAN|SEN|SWI -> command class)
# - thus, Composite design pattern may be more appropriate


_LOGGER = logging.getLogger(__name__)


_HvacRemoteBaseT = TypeVar("_HvacRemoteBaseT", bound="HvacRemoteBase")
_HvacSensorBaseT = TypeVar("_HvacSensorBaseT", bound="HvacSensorBase")


class HvacRemoteBase(DeviceHvac):
    """Base class for HVAC remote control devices.

    This class serves as a base for all remote control devices in the HVAC domain.
    It provides common functionality and interfaces for remote control operations.
    """

    def __init__(
        self, *args: Any, traits: DeviceTraits | None = None, **kwargs: Any
    ) -> None:
        """Initialize the HvacRemoteBase class.

        :param args: Positional arguments passed to the parent class
        :param traits: Strictly typed traits object for device creation
        :param kwargs: Keyword arguments passed to the parent class
        """
        super().__init__(*args, traits=traits, **kwargs)
        if not hasattr(self, "hvac_state"):
            self.hvac_state = HvacState()

    def _post_class_promote(self) -> None:
        """Initialize state when promoted from a generic HVAC device."""
        if not hasattr(self, "hvac_state"):
            self.hvac_state = HvacState()

    @property
    def heartbeat_timeout(self) -> td:
        """Return the timeout before the device is considered unavailable.

        :return: The timeout duration.
        :rtype: td
        """
        return HEARTBEAT_TIMEOUT_REMOTE


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

    def _post_class_promote(self) -> None:
        """Initialize state when promoted from a generic HVAC device."""
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

    async def set_co2_level(self, value: int | None) -> Packet:
        """Set a fake CO2 level for the sensor.

        :param value: The CO2 level in ppm to set, or None to clear the fake value
        :type value: int | None
        :raises TypeError: If the sensor is not in faked mode
        :return: The sent packet
        :rtype: Packet
        """

        if not self.is_faked:
            raise exc.DeviceNotFaked(f"{self}: Faking is not enabled")

        cmd = Command.put_co2_level(self.id, value)
        return await self._gwy.async_send_cmd(
            cmd, num_repeats=2, priority=Priority.HIGH
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

    async def set_indoor_humidity(self, value: float | None) -> Packet:
        """Set a fake indoor humidity value for the sensor.

        :param value: The humidity percentage to set (0-100), or None to clear the fake value
        :type value: float | None
        :raises TypeError: If the sensor is not in faked mode
        :return: The sent packet
        :rtype: Packet
        """

        if not self.is_faked:
            raise exc.DeviceNotFaked(f"{self}: Faking is not enabled")

        cmd = Command.put_indoor_humidity(self.id, value)
        return await self._gwy.async_send_cmd(
            cmd, num_repeats=2, priority=Priority.HIGH
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

    # .I --- 37:154011 --:------ 37:154011 1FC9 030 00-31E0-96599B 00-1298-96599B 00-2E10-96599B 01-10E0-96599B 00-1FC9-96599B    # CO2, idx|10E0 == 01
    # .W --- 28:126620 37:154011 --:------ 1FC9 012 00-31D9-49EE9C 00-31DA-49EE9C                                                 # FAN, BRDG-02A55
    # .I --- 37:154011 28:126620 --:------ 1FC9 001 00                                                                            # CO2, incl. integrated control, PIR

    async def presence_detected(self) -> bool | None:
        """Get the presence detection status.

        :return: True if presence is detected, False if not, None if status is unknown
        :rtype: bool | None
        """
        return self.hvac_state.presence_detected

    async def set_presence_detected(self, value: bool | None) -> Packet:
        """Set a fake presence detection state for the sensor.

        :param value: The presence state to set (True/False), or None to clear the fake value
        :type value: bool | None
        :raises TypeError: If the sensor is not in faked mode
        :return: The sent packet
        :rtype: Packet
        """

        if not self.is_faked:
            raise exc.DeviceNotFaked(f"{self}: Faking is not enabled")

        cmd = Command.put_presence_detected(self.id, value)
        return await self._gwy.async_send_cmd(
            cmd, num_repeats=2, priority=Priority.HIGH
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


class FilterChange(DeviceHvac):  # FAN: 10D0
    """The filter state sensor (10D0)."""

    def __init__(
        self, *args: Any, traits: DeviceTraits | None = None, **kwargs: Any
    ) -> None:
        """Initialize the FilterChange class and start dayly polling.

        :param args: Positional arguments passed to the parent class
        :param traits: Strictly typed traits object for device creation
        :param kwargs: Keyword arguments passed to the parent class
        """
        super().__init__(*args, traits=traits, **kwargs)
        if not hasattr(self, "hvac_state"):
            self.hvac_state = HvacState()

        self._poller: asyncio.Task[None] | None = None
        self._rq_cmd: Command = Command.from_attrs(
            RQ, self.id, Code._10D0, PayloadT("00")
        )

        if self._gwy.config.disable_discovery:  # discovery will poll for filter
            try:
                asyncio.get_running_loop().call_soon(self.start_poller)
            except RuntimeError:
                # Fallback if instantiated outside of a running event loop context
                _LOGGER.debug(
                    "No running event loop; filter_change poller not started."
                )

    def _post_class_promote(self) -> None:
        """Initialize state when promoted from a generic HVAC device."""
        if not hasattr(self, "hvac_state"):
            self.hvac_state = HvacState()

    def _setup_discovery_cmds(self) -> None:
        """Set up the discovery commands for the filter change sensor."""
        super()._setup_discovery_cmds()

        self.discovery.add_cmd(
            self._rq_cmd,
            60 * 60 * 24,
            delay=30,
        )

    def start_poller(self) -> None:
        """
        Start polling the filter_remaining state of a fan.
        Messages are cleaned up every 12h, the 10D0 message must be RQd
        """
        if not self._poller:
            task = asyncio.create_task(
                self._gwy.async_send_cmd(
                    self._rq_cmd, num_repeats=2, priority=Priority.HIGH
                )
            )
            self._poller = schedule_task(task, 60 * 60 * 12, delay=120)
            self._poller.set_name(f"{self.id}_10d0_poller")
            self._gwy.add_task(self._poller)

    async def stop_poller(self) -> None:
        """Stop the discovery poller (only if it is running)."""
        if not self._poller or self._poller.done():
            return

        self._poller.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._poller

    async def filter_remaining(self) -> int | None:
        """Return the remaining days until filter change is needed.

        :return: Number of days remaining until filter change, or None if not available
        :rtype: int | None
        """
        return self.hvac_state.filter_remaining_days

    async def filter_remaining_percent(self) -> float | None:
        """Return the remaining filter life as a percentage.

        :return: Percentage of filter life remaining (0-100), or None if not available
        :rtype: float | None
        """
        return self.hvac_state.filter_remaining_percent

    @property
    def heartbeat_timeout(self) -> td:
        """Return the timeout before the device is considered unavailable.

        :return: The timeout duration.
        :rtype: td
        """
        return HEARTBEAT_TIMEOUT_FILTER


class RfsGateway(DeviceHvac):  # RFS: (spIDer gateway)
    """The spIDer gateway base class."""

    _SLUG: str = DevType.RFS

    def __init__(
        self, *args: Any, traits: DeviceTraits | None = None, **kwargs: Any
    ) -> None:
        """Initialize the RFS gateway.

        :param args: Positional arguments passed to the parent class
        :param traits: Strictly typed traits object for device creation
        :param kwargs: Keyword arguments passed to the parent class
        """
        super().__init__(*args, traits=traits, **kwargs)
        if not hasattr(self, "hvac_state"):
            self.hvac_state = HvacState()

        self.ctl = None
        self._child_id = "hv"  # NOTE: domain_id
        self.tcs = None

    def _post_class_promote(self) -> None:
        """Initialize state when promoted from a generic HVAC device."""
        if not hasattr(self, "hvac_state"):
            self.hvac_state = HvacState()


class HvacHumiditySensor(BatteryState, IndoorHumidity, Fakeable):  # HUM: I/12A0
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

    async def initiate_binding_process(self) -> Packet:
        """Initiate the binding process for the CO2 sensor.

        :return: The packet sent to initiate binding
        :rtype: Packet
        :raises exc.BindingError: If binding fails
        """
        return await super()._initiate_binding_process(
            (Code._31E0, Code._1298, Code._2E10)
        )


class HvacRemote(BatteryState, Fakeable, HvacRemoteBase):  # REM: I/22F[138]
    """The REM (remote/switch) class, such as a 4-way switch.

    The cardinal codes are 22F1, 22F3 (also 22F8?).
    """

    _SLUG: str = DevType.REM

    async def initiate_binding_process(self) -> Packet:
        # .I --- 37:155617 --:------ 37:155617 1FC9 024 00-22F1-965FE1 00-22F3-965FE1 67-10E09-65FE1 00-1FC9-965FE1
        # .W --- 32:155617 37:155617 --:------ 1FC9 012 00-31D9-825FE1 00-31DA-825FE1
        # .I --- 37:155617 32:155617 --:------ 1FC9 001 00

        return await super()._initiate_binding_process(
            Code._22F1 if self._scheme == "nuaire" else (Code._22F1, Code._22F3)
        )

    async def fan_rate(self) -> str | None:
        """Get the current fan rate setting.

        :return: The fan rate as a string, or None if not available
        :rtype: str | None
        :note: This is a work in progress - rate can be either int or str
        """
        return self.hvac_state.fan_rate

    async def set_fan_rate(self, value: int) -> Packet:
        """Set a fake fan rate for the remote control.

        :param value: The fan rate to set (can be int or str, but not None)
        :type value: int
        :raises TypeError: If the remote is not in faked mode
        :return: The sent packet
        :rtype: Packet
        :note: This is a work in progress
        """

        if not self.is_faked:  # NOTE: some remotes are stateless (i.e. except seqn)
            raise exc.DeviceNotFaked(f"{self}: Faking is not enabled")

        # TODO: num_repeats=2, or wait_for_reply=True ?

        # NOTE: this is not completely understood (i.e. diffs between vendor schemes)
        cmd = Command.set_fan_mode(self.id, int(4 * value), src_id=self.id)
        return await self._gwy.async_send_cmd(
            cmd, num_repeats=2, priority=Priority.HIGH
        )

    async def fan_mode(self) -> str | None:
        """Return the current fan mode.

        :return: The fan mode as a string, or None if not available
        :rtype: str | None
        """
        return self.hvac_state.fan_mode

    async def boost_timer(self) -> int | None:
        """Return the remaining boost timer in minutes.

        :return: The remaining boost time in minutes, or None if boost is not active
        :rtype: int | None
        """
        return self.hvac_state.boost_timer_mins

    async def status(self) -> dict[str, Any]:
        base_status = await super().status()
        return {
            **base_status,
            SZ_FAN_MODE: await self.fan_mode(),
            SZ_BOOST_TIMER: await self.boost_timer(),
        }


class HvacDisplayRemote(HvacRemote):  # DIS
    """The DIS (display switch)."""

    _SLUG: str = DevType.DIS

    # async def initiate_binding_process(self) -> Packet:
    #     return await super()._initiate_binding_process(
    #         (Code._31E0, Code._1298, Code._2E10)
    #     )


class HvacVentilator(FilterChange):  # FAN: RP/31DA, I/31D[9A], 2411
    """The FAN (ventilation) class.

    The cardinal codes are 31D9, 31DA.  Signature is RP/31DA.

    Also handles 2411 parameter messages for configuration.
    Since 2411 is not supported by all vendors, discovery is used to determine if it is supported.
    Since more than 1 different parameters can be sent on 2411 messages,
    we process these in the dedicated _handle_2411_message method.
    """

    # Itho Daalderop (NL)
    # Heatrae Sadia (UK)
    # Nuaire (UK), e.g. DRI-ECO-PIV
    # Orcon/Ventiline
    # ClimaRad (NL)
    # Vasco (B)

    _SLUG: str = DevType.FAN

    # FAN-specific instance attributes (initialized in _init_fan_state)
    _supports_2411: bool
    _params_2411: dict[str, float]
    _initialized_callback: Callable[[], None] | None
    _param_update_callback: Callable[[str, Any], None] | None
    _hgi: Any | None
    _bound_devices: dict[str, str]

    def __init__(
        self, *args: Any, traits: DeviceTraits | None = None, **kwargs: Any
    ) -> None:
        """Initialize the HvacVentilator.

        :param args: Positional arguments passed to the parent class
        :param traits: Strictly typed traits object for device creation
        :param kwargs: Keyword arguments passed to the parent class
        """
        super().__init__(*args, traits=traits, **kwargs)
        self._init_fan_state()

    def _init_fan_state(self) -> None:
        """Initialize FAN-specific instance attributes (idempotent)."""
        self.__dict__.setdefault("_supports_2411", False)
        self.__dict__.setdefault("_params_2411", {})
        self.__dict__.setdefault("_initialized_callback", None)
        self.__dict__.setdefault("_param_update_callback", None)
        self.__dict__.setdefault("_hgi", None)
        self.__dict__.setdefault("_bound_devices", {})
        if not hasattr(self, "hvac_state"):
            self.hvac_state = HvacState()

    def _post_class_promote(self) -> None:
        """Initialize FAN state when promoted from a generic HVAC device."""
        super()._post_class_promote()
        self._init_fan_state()

    def set_initialized_callback(self, callback: Callable[[], None] | None) -> None:
        """Set a callback to be executed when the next message (any) is received.

        The callback will be used exactly once to indicate that the device is fully functional.
        In ramses_cc, 2411 entities are created - on the fly - only for devices that support them.

        :param callback: A callable that takes no arguments and returns None.
                         If None, any existing callback will be cleared.
        :type callback: Callable[[], None] | None
        :raises ValueError: If the callback is not callable and not None
        """
        if callback is not None and not callable(callback):
            raise ValueError("Callback must be callable or None")

        self._initialized_callback = callback
        if callback is not None:
            _LOGGER.debug("Initialization callback set for %s", self.id)

    def _handle_initialized_callback(self) -> None:
        """Handle the initialization callback.

        This method is called when the device has been fully initialized and
        is ready to process commands. It triggers any registered initialization
        callbacks and performs necessary setup for 2411 parameter support.
        """
        if self._initialized_callback is not None and self.supports_2411:
            _LOGGER.debug("2411-Device initialized: %s", self.id)
            if callable(self._initialized_callback):
                try:
                    self._initialized_callback()
                except Exception as ex:
                    _LOGGER.warning("Error in initialized_callback: %s", ex)
                finally:
                    # Clear the callback so it's only called once
                    self._initialized_callback = None

    def set_param_update_callback(
        self, callback: Callable[[str, Any], None] | None
    ) -> None:
        """Set a callback to be called when 2411 parameters are updated.

        This method registers a callback function that will be invoked whenever
        a 2411 parameter is updated. The callback receives the parameter ID and
        its new value as arguments.

        Since 2411 parameters are configuration entities, we are not polling for them
        and we update them immediately after receiving a 2411 message. We don't wait for them,
        we only process when we see a 2411 response for our device. The request may have come
        from another REM or DIS, but we will update to that as well.

        :param callback: A callable that will be invoked with (param_id, value) when a
                         2411 parameter is updated, or None to clear the current callback
        :type callback: Callable[[str, Any], None] | None
        """
        self._param_update_callback = callback

    def _handle_param_update(self, param_id: str, value: Any) -> None:
        """Handle a parameter update and notify listeners.

        This method processes parameter updates and notifies any registered
        callbacks of the change. It ensures thread safety and handles any
        exceptions that may occur during callback execution.

        :param param_id: The ID of the parameter that was updated
        :type param_id: str
        :param value: The new value of the parameter
        :type value: float
        """
        if callable(self._param_update_callback):
            try:
                self._param_update_callback(param_id, value)
            except Exception as ex:
                _LOGGER.warning("Error in param_update_callback: %s", ex)

    @property
    def supports_2411(self) -> bool:
        """Return whether this device supports 2411 parameters.

        :return: True if the device supports 2411 parameters, False otherwise
        :rtype: bool
        """
        return self._supports_2411

    @property
    def hgi(self) -> Any | None:
        """Return the HGI (Home Gateway Interface) device if available.

        The HGI device provides additional functionality for certain operations.

        :return: The HGI device instance, or None if not available
        :rtype: float | None
        """
        if self._hgi is None and self._gwy and hasattr(self._gwy, "hgi"):
            self._hgi = self._gwy.hgi
        return self._hgi

    def get_2411_param(self, param_id: str) -> float | None:
        """Get a 2411 parameter value.

        :param param_id: The parameter ID to retrieve.
        :type param_id: str
        :return: The parameter value if found, None otherwise
        :rtype: float | None
        """
        return self._params_2411.get(param_id)

    def set_2411_param(self, param_id: str, value: float) -> bool:
        """Set a 2411 parameter value.

        :param param_id: The parameter ID to retrieve.
        :type param_id: str
        :param value: The parameter value to set.
        :type value: float
        :return: True if the parameter was set, False otherwise
        :rtype: bool
        """
        if not self._supports_2411:
            _LOGGER.warning("Device %s doesn't support 2411 parameters", self.id)
            return False

        self._params_2411[param_id] = value
        return True

    def get_fan_param(self, param_id: str) -> float | None:
        """Retrieve a fan parameter value from the device's message store.

        This wrapper method gets a specific parameter value for a FAN device stored in
        _params_2411 dict. It first makes sure we use the proper param_id format

        :param param_id: The parameter ID to retrieve.
        :type param_id: str
        :return: The parameter value if found, None otherwise
        :rtype: float | None
        """
        # Ensure param_id is uppercase and strip leading zeros for consistency
        param_id = (
            str(param_id).upper().lstrip("0") or "0"
        )  # Handle case where param_id is "0"

        param_value = self.get_2411_param(param_id)
        if param_value is not None:
            return param_value
        else:
            _LOGGER.debug("Parameter %s not found for %s", param_id, self.id)
            return None

    def _handle_2411_message(self, msg: Message) -> None:
        """Handle incoming 2411 parameter messages.

        This method processes 2411 parameter update messages, updates the device's
        message store, and triggers any registered parameter update callbacks.
        It handles parameter value normalization and validation.

        :param msg: The incoming 2411 message
        :type msg: Message to process
        """
        if not hasattr(msg, "payload") or not isinstance(msg.payload, dict):
            _LOGGER.debug("Invalid 2411 message format: %s", msg)
            return

        param_id = msg.payload.get("parameter")
        param_value = msg.payload.get("value")

        if not param_id or param_value is None:
            _LOGGER.debug("Missing parameter ID or value in 2411 message: %s", msg)
            return

        # Normalize param_id: uppercase and strip leading zeros for consistency with get_fan_param
        param_id = str(param_id).upper().lstrip("0") or "0"

        # Mark that we support 2411 parameters
        if not self._supports_2411:
            self._supports_2411 = True
            _LOGGER.debug("Device %s supports 2411 parameters", self.id)

        # Normalize the value if needed
        if param_id == "75" and isinstance(param_value, (int, float)):
            param_value = round(float(param_value), 1)
        elif param_id in ("52", "95"):  # Percentage parameters
            param_value = round(float(param_value), 3)  # Keep precision for percentages

        # Store in params
        old_value = self.get_2411_param(param_id)
        self.set_2411_param(param_id, param_value)

        # Log the update
        _LOGGER.debug(
            "Updated 2411 parameter %s: %s (was: %s) for %s",
            param_id,
            param_value,
            old_value,
            self.id,
        )

        # call the 2411 parameter update callback
        self._handle_param_update(param_id, param_value)

    def _handle_msg(self, msg: Message) -> None:
        """Handle a message from this device.

        This method processes incoming messages for the device, with special
        handling for 2411 parameter messages. It updates the device state and
        triggers any necessary callbacks.

        After handling the messages, it calls the initialized callback - if set - to notify that
        the device was fully initialized.

        :param msg: The incoming message to process
        :type msg: Message
        """
        super()._handle_msg(msg)

        # Handle 2411 parameter messages
        if msg.code == Code._2411:
            _LOGGER.debug(
                "Received 2411 message from %s: verb=%s, payload=%s, src=%s, dst=%s",
                self.id,
                msg.verb,
                msg.payload,
                msg.src,
                msg.dst,
            )
            self._handle_2411_message(msg)

        self._handle_initialized_callback()

    def _setup_discovery_cmds(self) -> None:
        """Set up discovery commands for the RFS gateway.

        This method initializes the discovery commands needed to identify and
        communicate with the RFS gateway device.
        """
        super()._setup_discovery_cmds()

        # RP --- 32:155617 18:005904 --:------ 22F1 003 000207
        self.discovery.add_cmd(
            Command.from_attrs(RQ, self.id, Code._22F1, PayloadT("00")),
            60 * 60 * 24,
            delay=15,
        )  # to learn scheme: orcon/itho/other (04/07/0?)

        # Add a single discovery command for all parameters (3F likely to be supported if any)
        # The handler will process the response and update the appropriate parameter and
        # also set the supports_2411 flag
        _LOGGER.debug("Adding single discovery command for all 2411 parameters")
        self.discovery.add_cmd(
            Command.from_attrs(RQ, self.id, Code._2411, PayloadT("00003F")),
            interval=60 * 60 * 24,  # Check daily
            delay=40,  # Initial delay before first discovery
        )

        # Standard discovery commands for other codes
        for code in (
            Code._2210,  # Air quality
            Code._22E0,  # Bypass position
            Code._22E5,  # Remaining minutes
            Code._22E9,  # Speed cap
            Code._22F2,  # Post heat
            Code._22F4,  # Pre heat
            Code._22F8,  # Air quality base
        ):
            self.discovery.add_cmd(
                Command.from_attrs(RQ, self.id, code, PayloadT("00")), 60 * 30, delay=15
            )

        for code in (Code._313E, Code._3222):
            self.discovery.add_cmd(
                Command.from_attrs(RQ, self.id, code, PayloadT("00")), 60 * 30, delay=30
            )

    def add_bound_device(self, device_id: str, device_type: str) -> None:
        """Add a bound device to this FAN.

        This method registers a REM or DIS device as bound to this FAN device.
        Bound devices are required for certain operations like setting parameters.

        A bound device is needed to be able to send 2411 parameter Set messages,
        or the device will not accept and respond to them.
        In HomeAssistant, ramses_cc, you can set a bound device in the device configuration.

        System schema and known devices example:

        .. code-block::

            "32:153289":
              bound: "37:168270"
              class: FAN
            "37:168270":
              class: REM
              faked: true

        :param device_id: The unique identifier of the device to bind
        :type device_id: str
        :param device_type: The type of device (must be 'REM' or 'DIS')
        :type device_type: str
        :raises ValueError: If the device type is not 'REM' or 'DIS'
        """
        if device_type not in (DevType.REM, DevType.DIS):
            _LOGGER.warning(
                "Cannot bind device %s of type %s to FAN %s: must be REM or DIS",
                device_id,
                device_type,
                self.id,
            )
            return

        self._bound_devices[device_id] = device_type
        _LOGGER.info("Bound %s device %s to FAN %s", device_type, device_id, self.id)

    def remove_bound_device(self, device_id: str) -> None:
        """Remove a bound device from this FAN.

        This method unregisters a previously bound device from this FAN.

        :param device_id: The unique identifier of the device to unbind
        :type device_id: str
        """
        if device_id in self._bound_devices:
            device_type = self._bound_devices.pop(device_id)
            _LOGGER.info(
                "Removed bound %s device %s from FAN %s",
                device_type,
                device_id,
                self.id,
            )

    def get_bound_rem(self) -> str | None:
        """Get the first bound REM/DIS device ID for this FAN.

        This method retrieves the device ID of the first bound REM or DIS device.
        Bound devices are required for certain operations like setting parameters.

        :return: The device ID of the first bound REM or DIS device, or None if none found
        :rtype: str | None
        """
        if not self._bound_devices:
            _LOGGER.debug("No bound devices found for FAN %s", self.id)
            return None

        # Find first REM or DIS device
        for device_id, device_type in self._bound_devices.items():
            if device_type in (DevType.REM, DevType.DIS):
                _LOGGER.debug(
                    "Found bound %s device %s for FAN %s",
                    device_type,
                    device_id,
                    self.id,
                )
                return device_id

        _LOGGER.debug("No bound REM or DIS devices found for FAN %s", self.id)
        return None

    async def set_fan_mode(self, fan_mode: str | int) -> Packet:
        """Set the operating mode/speed of the ventilator.

        :param fan_mode: The desired fan mode (e.g., 'low', 'medium', 'high', 'boost').
        :return: The sent packet.
        :raises CommandInvalid: If unable to determine a valid source ID.
        """
        # 22F1 commands to a FAN typically must originate from a bound Remote (REM)
        # We attempt to impersonate the first bound REM. If none exists, fallback to HGI.
        src_id = self.get_bound_rem()

        if not src_id:
            if self.hgi:
                src_id = self.hgi.id
            else:
                raise exc.CommandInvalid(
                    f"{self}: Cannot set fan mode without a bound REM or HGI"
                )

        _LOGGER.debug(
            "Sending set_fan_mode '%s' to %s via src %s", fan_mode, self.id, src_id
        )

        cmd = Command.set_fan_mode(self.id, fan_mode, src_id=src_id)
        return await self._gwy.async_send_cmd(
            cmd, num_repeats=2, priority=Priority.HIGH
        )

    async def air_quality(self) -> float | None:
        """Return the current air quality measurement.

        :return: The air quality measurement as a float, or None if not available
        :rtype: float | None
        """
        return self.hvac_state.air_quality

    async def air_quality_base(self) -> float | None:
        """Return the base air quality measurement.

        This represents the baseline or raw air quality measurement before any
        processing or normalization.

        :return: The base air quality measurement, or None if not available
        :rtype: float | None
        """
        return self.hvac_state.air_quality_basis

    async def bypass_mode(self) -> str | None:
        """
        :return: bypass mode as on|off|auto
        """
        return self.hvac_state.bypass_mode

    async def bypass_position(self) -> float | str | None:
        """
        Position info is found in 22F7 and in 31DA. The most recent packet is returned.
        :return: bypass position as percentage: 0.0 (closed) or 1.0 (open), on error: "x_faulted"
        """
        return self.hvac_state.bypass_position

    async def bypass_state(self) -> str | None:
        """
        Orcon, others?
        :return: bypass position as on/off
        """
        return self.hvac_state.bypass_state

    async def co2_level(self) -> int | None:
        """Return the CO2 level in parts per million (ppm).

        :return: The CO2 level in ppm, or None if not available
        :rtype: int | None
        """
        return self.hvac_state.co2_level

    async def exhaust_fan_speed(self) -> float | None:
        """
        Some fans (Vasco, Itho) use Code._31D9 for speed + mode,
        Orcon sends SZ_EXHAUST_FAN_SPEED in 31DA. See parser for details.
        :return: speed as percentage
        """
        return self.hvac_state.exhaust_fan_speed

    async def exhaust_flow(self) -> float | None:
        """Return the current exhaust air flow rate.

        :return: The exhaust air flow rate in m³/h, or None if not available
        :rtype: float | None
        """
        return self.hvac_state.exhaust_flow

    async def exhaust_temp(self) -> float | None:
        """Return the current exhaust air temperature.

        :return: The exhaust air temperature in degrees Celsius, or None if not available
        :rtype: float | None
        """
        return self.hvac_state.exhaust_temp

    async def fan_rate(self) -> str | None:
        """
        Lookup fan mode description from _22F4  message payload, e.g. "low", "medium", "boost".
        For manufacturers Orcon, Vasco, ClimaRad.

        :return: int or str describing rate of fan
        """
        return self.hvac_state.fan_rate

    async def fan_mode(self) -> str | None:
        """
        Lookup fan mode description from _22F4  message payload, e.g. "auto", "manual", "off".
        For manufacturers Orcon, Vasco, ClimaRad.

        :return: a string describing mode
        """
        return self.hvac_state.fan_mode

    async def fan_info(self) -> str | None:
        """
        Extract fan info description from MessageStore _31D9 or _31DA payload,
        e.g. "speed 2, medium".
        By its name, the result is picked up by a sensor in HA Climate UI.
        Some manufacturers (Orcon, Vasco) include the fan mode (auto, manual), others don't (Itho).

        :return: string describing fan mode, speed
        """
        return self.hvac_state.fan_info

    async def indoor_humidity(self) -> float | None:
        """
        Extract indoor_humidity from MessageStore _12A0 or _31DA payload
        Just a demo for SQLite query helper at the moment.

        :return: float RH value from 0.0 to 1.0 = 100%
        """
        return self.hvac_state.indoor_humidity

    async def indoor_temp(self) -> float | None:
        """Return the current indoor temperature.

        :return: The indoor temperature in degrees Celsius, or None if not available
        :rtype: float | None
        """
        return self.hvac_state.indoor_temp

    async def outdoor_humidity(self) -> float | None:
        """Return the outdoor relative humidity.

        Handles special case for Ventura devices that send humidity data in 12A0 messages.

        :return: The outdoor relative humidity as a percentage (0-100), or None if not available
        :rtype: float | None
        """
        return self.hvac_state.outdoor_humidity

    async def outdoor_temp(self) -> float | None:
        """Return the outdoor temperature in Celsius.

        :return: The outdoor temperature in degrees Celsius, or None if not available
        :rtype: float | None
        """
        return self.hvac_state.outdoor_temp

    async def post_heat(self) -> int | None:
        """Return the post-heat status.

        :return: The post-heat status as an integer, or None if not available
        :rtype: int | None
        """
        return self.hvac_state.post_heat

    async def pre_heat(self) -> int | None:
        """Return the pre-heat status.

        :return: The pre-heat status as an integer, or None if not available
        :rtype: int | None
        """
        return self.hvac_state.pre_heat

    async def remaining_mins(self) -> int | None:
        """Return the remaining minutes for the current operation.

        :return: The remaining minutes as an integer, or None if not available
        :rtype: int | None
        """
        return self.hvac_state.remaining_mins

    async def request_fan_speed(self) -> float | None:
        """Return the requested fan speed.

        :return: The requested fan speed as a percentage, or None if not available
        :rtype: float | None
        """
        return self.hvac_state.request_fan_speed

    async def request_src(self) -> str | None:
        """
        Orcon, others?
        :return: source sensor of auto speed request: IDL, CO2 or HUM
        """
        return self.hvac_state.request_reason

    async def speed_cap(self) -> int | None:
        """Return the speed capabilities of the fan.

        :return: The speed capabilities as an integer, or None if not available
        :rtype: int | None
        """
        return self.hvac_state.speed_capabilities

    async def supply_fan_speed(self) -> float | None:
        """Return the supply fan speed.

        :return: The supply fan speed as a percentage, or None if not available
        :rtype: float | None
        """
        return self.hvac_state.supply_fan_speed

    async def supply_flow(self) -> float | None:
        """Return the supply air flow rate.

        :return: The supply air flow rate in m³/h, or None if not available
        :rtype: float | None
        """
        return self.hvac_state.supply_flow

    async def supply_temp(self) -> float | None:
        """Return the supply air temperature.

        Handles special case for Ventura devices that send temperature data in 12A0 messages.

        :return: The supply air temperature in Celsius, or None if not available
        :rtype: float | None
        """
        return self.hvac_state.supply_temp

    async def status(self) -> dict[str, Any]:
        """Return the status of the ventilation device."""
        base_status = await super().status()
        cqrs_status = {
            SZ_AIR_QUALITY: await self.air_quality(),
            SZ_AIR_QUALITY_BASIS: await self.air_quality_base(),
            SZ_BYPASS_MODE: await self.bypass_mode(),
            SZ_BYPASS_POSITION: await self.bypass_position(),
            SZ_BYPASS_STATE: await self.bypass_state(),
            SZ_CO2_LEVEL: await self.co2_level(),
            SZ_EXHAUST_FAN_SPEED: await self.exhaust_fan_speed(),
            SZ_EXHAUST_FLOW: await self.exhaust_flow(),
            SZ_EXHAUST_TEMP: await self.exhaust_temp(),
            SZ_FAN_INFO: await self.fan_info(),
            SZ_FAN_MODE: await self.fan_mode(),
            SZ_FAN_RATE: await self.fan_rate(),
            SZ_FILTER_DIRTY: await self.filter_dirty(),
            SZ_FROST_CYCLE: await self.frost_cycle(),
            SZ_HAS_FAULT: await self.has_fault(),
            SZ_INDOOR_HUMIDITY: await self.indoor_humidity(),
            SZ_INDOOR_TEMP: await self.indoor_temp(),
            SZ_OUTDOOR_HUMIDITY: await self.outdoor_humidity(),
            SZ_OUTDOOR_TEMP: await self.outdoor_temp(),
            SZ_POST_HEAT: await self.post_heat(),
            SZ_PRE_HEAT: await self.pre_heat(),
            SZ_REMAINING_MINS: await self.remaining_mins(),
            SZ_REQ_REASON: await self.request_src(),
            SZ_REQ_SPEED: await self.request_fan_speed(),
            SZ_SPEED_CAPABILITIES: await self.speed_cap(),
            SZ_SUPPLY_FAN_SPEED: await self.supply_fan_speed(),
            SZ_SUPPLY_FLOW: await self.supply_flow(),
            SZ_SUPPLY_TEMP: await self.supply_temp(),
            SZ_TEMPERATURE: await self.temperature(),
        }

        # Emulate the legacy behaviour by only exposing populated payload keys
        return {
            **base_status,
            **{k: v for k, v in cqrs_status.items() if v is not None},
        }

    async def temperature(self) -> float | None:  # Celsius
        """Return the current temperature in Celsius.

        Handles special cases.

        :return: The temperature in degrees Celsius, or None if not available
        :rtype: float | None
        """
        return self.hvac_state.temperature

    async def filter_dirty(self) -> bool | None:
        """Return the dirty filter diagnostic flag.

        :return: True if the filter is dirty, False if clean, or None if unavailable.
        :rtype: bool | None
        """
        return self.hvac_state.filter_dirty

    async def frost_cycle(self) -> bool | None:
        """Return the frost cycle diagnostic flag.

        :return: True if the frost cycle is active, False otherwise, or None if unavailable.
        :rtype: bool | None
        """
        return self.hvac_state.frost_cycle

    async def has_fault(self) -> bool | None:
        """Return the hardware fault diagnostic flag.

        :return: True if a fault is active, False otherwise, or None if unavailable.
        :rtype: bool | None
        """
        return self.hvac_state.has_fault


# class HvacFanHru(HvacVentilator):
#     """A Heat recovery unit (aka: HRU, WTW)."""
#     _SLUG: str = DEV_TYPE.HRU
# class HvacFanCve(HvacVentilator):
#     """An extraction unit (aka: CVE, CVD)."""
#     _SLUG: str = DEV_TYPE.CVE
# class HvacFanPiv(HvacVentilator):
#     """A positive input ventilation unit (aka: PIV)."""
#     _SLUG: str = DEV_TYPE.PIV


# e.g. {"HUM": HvacHumiditySensor}
HVAC_CLASS_BY_SLUG: dict[str, type[DeviceHvac]] = class_by_attr(__name__, "_SLUG")


def class_dev_hvac(
    dev_addr: Address, *, msg: Message | None = None, eavesdrop: bool = False
) -> type[DeviceHvac]:
    """Return a device class, but only if the device must be from the HVAC group.

    May return a base class, `DeviceHvac`, which will need promotion.
    """

    if not eavesdrop:
        raise exc.DeviceNotRecognised(
            f"No HVAC class for: {dev_addr} (no eavesdropping)"
        )

    if msg is None:
        raise exc.DeviceNotRecognised(f"No HVAC class for: {dev_addr} (no msg)")

    if klass := HVAC_KLASS_BY_VC_PAIR.get((msg.verb, msg.code)):
        return HVAC_CLASS_BY_SLUG[klass]

    if msg.code in CODES_OF_HVAC_DOMAIN_ONLY:
        return DeviceHvac

    raise exc.DeviceNotRecognised(
        f"No HVAC class for: {dev_addr} (insufficient meta-data)"
    )


_REMOTES = {
    "21800000": {
        "name": "Orcon 15RF",
        "mode": "1,2,3,T,Auto,Away",
    },
    "21800060": {
        "name": "Orcon 15RF Display",
        "mode": "1,2,3,T,Auto,Away",
    },
    "xxx": {
        "name": "Orcon CO2 Control",
        "mode": "1T,2T,3T,Auto,Away",
    },
    "03-00062": {
        "name": "RFT-SPIDER",
        "mode": "1,2,3,T,A",
    },
    "04-00045": {"name": "RFT-CO2"},  # mains-powered
    "04-00046": {"name": "RFT-RV"},
    "545-7550": {
        "name": "RFT-PIR",
    },
    "536-0124": {  # idx="00"
        "name": "RFT",
        "mode": "1,2,3,T",
        "CVE": False,  # not clear
        "HRV": True,
    },
    "536-0146": {  # idx="??"
        "name": "RFT-DF",
        "mode": "",
        "CVE": True,
        "HRV": False,
    },
    "536-0150": {  # idx = "63"
        "name": "RFT-AUTO",
        "mode": "1,Auto,3,T",
        "CVE": True,
        "HRV": True,
    },
}


# see: https://github.com/arjenhiemstra/ithowifi/blob/master/software/NRG_itho_wifi/src/IthoPacket.h

"""
Itho Remote (model) enums.

CVE/HRU remote (536-0124) RFT W: 3 modes, timer
-------------------------------------------------

.. table:: 536-0124
   :widths: auto

   ===========  =========================  ================================================
    "away":     (Code._22F1, 00, 01|04"),  how to invoke?
    "low":      (Code._22F1, 00, 02|04"),  aka eco
    "medium":   (Code._22F1, 00, 03|04"),  aka auto (with sensors) - is that only for 63?
    "high":     (Code._22F1, 00, 04|04"),  aka full

    "timer_1":  (Code._22F3, 00, 00|0A"),  10 minutes full speed
    "timer_2":  (Code._22F3, 00, 00|14"),  20 minutes full speed
    "timer_3":  (Code._22F3, 00, 00|1E"),  30 minutes full speed
   ===========  =========================  ================================================

RFT-AUTO (536-0150) RFT CAR: 2 modes, auto, timer: idx = 63, essentially same as above, but also...
-----------------------------------------------------------------------------------------------------

.. table:: 536-0150
   :widths: auto

   =============  =========================  ================================================
   "auto_night":  (Code._22F8, 63, 02|03"),  additional - press auto x2
   =============  =========================  ================================================

RFT-RV (04-00046), RFT-CO2 (04-00045) - sensors with control
------------------------------------------------------------

.. table:: 04-00046
   :widths: auto

   ==============  ========================================   =============
    "medium":      (Code._22F1, 00, 03|07"),                  1=away, 2=low?
    "auto":        (Code._22F1, 00, 05|07"),                  4=high
    "auto_night":  (Code._22F1, 00, 0B|0B"),

    "timer_1":     (Code._22F3, 00, 00|0A, 00|00, 0000"),     10 minutes
    "timer_2":     (Code._22F3, 00, 00|14, 00|00, 0000"),     20 minutes
    "timer_3":     (Code._22F3, 00, 00|1E, 00|00, 0000"),     30 minutes
   ==============  ========================================   =============

RFT-PIR (545-7550) - presence sensor
------------------------------------

RFT_DF: DemandFlow remote (536-0146)
------------------------------------

.. table:: 536-0146
   :widths: auto

   ===========  ================================  =========================================
    "timer_1":  (Code._22F3, 00, 42|03, 03|03"),  0b01-000-010 = 3 hrs, back to last mode
    "timer_2":  (Code._22F3, 00, 42|06, 03|03"),  0b01-000-010 = 6 hrs, back to last mode
    "timer_3":  (Code._22F3, 00, 42|09, 03|03"),  0b01-000-010 = 9 hrs, back to last mode
    "cook_30":  (Code._22F3, 00, 02|1E, 02|03"),  30 mins (press 1x)
    "cook_60":  (Code._22F3, 00, 02|3C, 02|03"),  60 mins (press 2x)

    "low":      (Code._22F8, 00, 01|02"),         ?eco     co2 <= 1200 ppm?
    "high":     (Code._22F8, 00, 02|02"),         ?comfort co2 <= 1000 ppm?
   ===========  ================================  =========================================


Join commands:
--------------

.. table:: join per accessory type
   :widths: auto

   ==========  =================  =====================  =========================  ==========================  =========================  ==========================  =================  ==========
   type        set 1              set 2                  set 3                      set 4                       set 5                      set 6                       description        art #
   ==========  =================  =====================  =========================  ==========================  =========================  ==========================  =================  ==========
   "CVERFT":   (Code._1FC9,  00,  Code._22F1, 0x000000,                             01, Code._10E0, 0x000000")                                                         CVE/HRU remote     (536-0124)
   "AUTORFT":  (Code._1FC9,  63,  Code._22F8, 0x000000,                             01, Code._10E0, 0x000000")                                                         AUTO RFT           (536-0150)
   "DF":       (Code._1FC9,  00,  Code._22F8, 0x000000,                             00, Code._10E0, 0x000000")                                                         DemandFlow remote  (536-0146)
   "RV":       (Code._1FC9,  00,  Code._12A0, 0x000000,                             01, Code._10E0, 0x000000,   00, Code._31E0, 0x000000,  00, Code._1FC9, 0x000000")  RFT-RV             (04-00046)
   "CO2":      (Code._1FC9,  00,  Code._1298, 0x000000,  00, Code._2E10, 0x000000,  01, Code._10E0, 0x000000,   00, Code._31E0, 0x000000,  00, Code._1FC9, 0x000000")  RFT-CO2            (04-00045)
   ==========  =================  =====================  =========================  ==========================  =========================  ==========================  =================  ==========

Leave commands:
---------------

.. table:: leave per accessory type
   :widths: auto

   ==========  =================  ======================  =========================  ==========
   type        set 1              set 2                   description                art #
   ==========  =================  ======================  =========================  ==========
   "Others":   (Code._1FC9, 00,   Code._1FC9, 0x000000")  standard leave command
   "AUTORFT":  (Code._1FC9, 63,   Code._1FC9, 0x000000")  leave command of AUTO RFT  (536-0150)
   ==========  =================  ======================  =========================  ==========

.. table:: verbs
   :widths: 2, 4

   ======  ========
   verb    byte
   ======  ========
   ``RQ``  ``0x00``
   ``I_``  ``0x01``
   ``W_``  ``0x02``
   ``RP``  ``0x03``
   ======  ========

"""
