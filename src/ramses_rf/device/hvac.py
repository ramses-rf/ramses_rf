#!/usr/bin/env python3
"""RAMSES RF - devices from the HVAC domain."""

from __future__ import annotations

import asyncio
import time
import logging

from typing import Any, TypeVar

from ramses_rf import exceptions as exc
from ramses_rf.const import (
    FAN_MODE,
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
    SZ_INDOOR_HUMIDITY,
    SZ_INDOOR_TEMP,
    SZ_OUTDOOR_HUMIDITY,
    SZ_OUTDOOR_TEMP,
    SZ_POST_HEAT,
    SZ_PRE_HEAT,
    SZ_PRESENCE_DETECTED,
    SZ_REMAINING_DAYS,
    SZ_REMAINING_MINS,
    SZ_REMAINING_PERCENT,
    SZ_REQ_REASON,
    SZ_REQ_SPEED,
    SZ_SPEED_CAPABILITIES,
    SZ_SUPPLY_FAN_SPEED,
    SZ_SUPPLY_FLOW,
    SZ_SUPPLY_TEMP,
    SZ_TEMPERATURE,
    DevType,
)
from ramses_rf.entity_base import class_by_attr
from ramses_rf.helpers import shrink
from ramses_rf.schemas import SCH_VCS, SZ_REMOTES, SZ_SENSORS
from ramses_tx import Address, Command, Message, Packet, Priority
from ramses_tx.ramses import (
    CODES_OF_HVAC_DOMAIN_ONLY,
    HVAC_KLASS_BY_VC_PAIR,
    _2411_PARAMS_SCHEMA,
)

from .base import BatteryState, DeviceHvac, Fakeable

from ramses_rf.const import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    I_,
    RP,
    RQ,
    W_,
    Code,
)

# TODO: Switch this module to utilise the (run-time) decorator design pattern...
# - https://refactoring.guru/design-patterns/decorator/python/example
# - will probably need setattr()?
# BaseComponents: FAN (HRU, PIV, EXT), SENsor (CO2, HUM, TEMp), SWItch (RF gateway?)
# - a device could be a combination of above (e.g. Spider Gateway)
# Track binding for SWI (HA service call) & SEN (HA trigger) to FAN/other

# Challenges:
# - may need two-tier system (HVAC -> FAN|SEN|SWI -> command class)
# - thus, Composite design pattern may be more appropriate


_LOGGER = logging.getLogger(__name__)


_HvacRemoteBaseT = TypeVar("_HvacRemoteBaseT", bound="HvacRemoteBase")
_HvacSensorBaseT = TypeVar("_HvacSensorBaseT", bound="HvacSensorBase")


class HvacRemoteBase(DeviceHvac):
    pass


class HvacSensorBase(DeviceHvac):
    pass


class CarbonDioxide(HvacSensorBase):  # 1298
    """The CO2 sensor (cardinal code is 1298)."""

    @property
    def co2_level(self) -> int | None:  # 1298
        return self._msg_value(Code._1298, key=SZ_CO2_LEVEL)

    @co2_level.setter
    def co2_level(self, value: int | None) -> None:
        """Fake the CO2 level of the sensor."""

        if not self.is_faked:
            raise exc.DeviceNotFaked(f"{self}: Faking is not enabled")

        cmd = Command.put_co2_level(self.id, value)
        self._gwy.send_cmd(cmd, num_repeats=2, priority=Priority.HIGH)

    @property
    def status(self) -> dict[str, Any]:
        return {
            **super().status,
            SZ_CO2_LEVEL: self.co2_level,
        }


class IndoorHumidity(HvacSensorBase):  # 12A0
    """The relative humidity sensor (12A0)."""

    @property
    def indoor_humidity(self) -> float | None:  # 12A0
        return self._msg_value(Code._12A0, key=SZ_INDOOR_HUMIDITY)

    @indoor_humidity.setter
    def indoor_humidity(self, value: float | None) -> None:
        """Fake the indoor humidity of the sensor."""

        if not self.is_faked:
            raise exc.DeviceNotFaked(f"{self}: Faking is not enabled")

        cmd = Command.put_indoor_humidity(self.id, value)
        self._gwy.send_cmd(cmd, num_repeats=2, priority=Priority.HIGH)

    @property
    def status(self) -> dict[str, Any]:
        return {
            **super().status,
            SZ_INDOOR_HUMIDITY: self.indoor_humidity,
        }


class PresenceDetect(HvacSensorBase):  # 2E10
    """The presence sensor (2E10/31E0)."""

    # .I --- 37:154011 --:------ 37:154011 1FC9 030 00-31E0-96599B 00-1298-96599B 00-2E10-96599B 01-10E0-96599B 00-1FC9-96599B    # CO2, idx|10E0 == 01
    # .W --- 28:126620 37:154011 --:------ 1FC9 012 00-31D9-49EE9C 00-31DA-49EE9C                                                 # FAN, BRDG-02A55
    # .I --- 37:154011 28:126620 --:------ 1FC9 001 00                                                                            # CO2, incl. integrated control, PIR

    @property
    def presence_detected(self) -> bool | None:
        return self._msg_value(Code._2E10, key=SZ_PRESENCE_DETECTED)

    @presence_detected.setter
    def presence_detected(self, value: bool | None) -> None:
        """Fake the presence state of the sensor."""

        if not self.is_faked:
            raise exc.DeviceNotFaked(f"{self}: Faking is not enabled")

        cmd = Command.put_presence_detected(self.id, value)
        self._gwy.send_cmd(cmd, num_repeats=2, priority=Priority.HIGH)

    @property
    def status(self) -> dict[str, Any]:
        return {
            **super().status,
            SZ_PRESENCE_DETECTED: self.presence_detected,
        }


class FilterChange(DeviceHvac):  # FAN: 10D0
    """The filter state sensor (10D0)."""

    def _setup_discovery_cmds(self) -> None:
        super()._setup_discovery_cmds()

        self._add_discovery_cmd(
            Command.from_attrs(RQ, self.id, Code._10D0, "00"), 60 * 60 * 24, delay=30
        )

    @property
    def filter_remaining(self) -> int | None:
        _val = self._msg_value(Code._10D0, key=SZ_REMAINING_DAYS)
        assert isinstance(_val, (int | type(None)))
        return _val

    @property
    def filter_remaining_percent(self) -> float | None:
        _val = self._msg_value(Code._10D0, key=SZ_REMAINING_PERCENT)
        assert isinstance(_val, (float | type(None)))
        return _val


class RfsGateway(DeviceHvac):  # RFS: (spIDer gateway)
    """The spIDer gateway base class."""

    _SLUG: str = DevType.RFS

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

        self.ctl = None
        self._child_id = "hv"  # NOTE: domain_id
        self.tcs = None


class HvacHumiditySensor(BatteryState, IndoorHumidity, Fakeable):  # HUM: I/12A0
    """The class for a humidity sensor.

    The cardinal code is 12A0.
    """

    _SLUG: str = DevType.HUM

    @property
    def temperature(self) -> float | None:  # Celsius
        return self._msg_value(Code._12A0, key=SZ_TEMPERATURE)

    @property
    def dewpoint_temp(self) -> float | None:  # Celsius
        return self._msg_value(Code._12A0, key="dewpoint_temp")

    @property
    def status(self) -> dict[str, Any]:
        return {
            **super().status,
            SZ_TEMPERATURE: self.temperature,
            "dewpoint_temp": self.dewpoint_temp,
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

    @property
    def fan_rate(self) -> str | None:  # 22F1
        # NOTE: WIP: rate can be int or str
        return self._msg_value(Code._22F1, key="rate")

    @fan_rate.setter
    def fan_rate(self, value: int) -> None:  # NOTE: value can be int or str, not None
        """Fake a fan rate from a remote (to a FAN, is a WIP)."""

        if not self.is_faked:  # NOTE: some remotes are stateless (i.e. except seqn)
            raise exc.DeviceNotFaked(f"{self}: Faking is not enabled")

        # TODO: num_repeats=2, or wait_for_reply=True ?

        # NOTE: this is not completely understood (i.e. diffs between vendor schemes)
        cmd = Command.set_fan_mode(self.id, int(4 * value), src_id=self.id)
        self._gwy.send_cmd(cmd, num_repeats=2, priority=Priority.HIGH)

    @property
    def fan_mode(self) -> str | None:
        return self._msg_value(Code._22F1, key=SZ_FAN_MODE)

    @property
    def boost_timer(self) -> int | None:
        return self._msg_value(Code._22F3, key=SZ_BOOST_TIMER)

    @property
    def status(self) -> dict[str, Any]:
        return {
            **super().status,
            SZ_FAN_MODE: self.fan_mode,
            SZ_BOOST_TIMER: self.boost_timer,
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
    Since different parameters for 1 Code, we will process the 2411 messages in the _handle_msg method.
    """

    # Itho Daalderop (NL)
    # Heatrae Sadia (UK)
    # Nuaire (UK), e.g. DRI-ECO-PIV
    # Orcon/Ventiline
    # ClimaRad (NL)
    # Vasco (B)

    _SLUG: str = DevType.FAN

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the HvacVentilator."""
        super().__init__(*args, **kwargs)
        self._found_codes = set()  # Tracks which message codes we've encountered
        self._pending_callbacks = []  # List of (code, callback) tuples
        self._hgi = None  # Will be set when HGI is available
        self._cleanup_tasks = []  # Track any cleanup tasks
        self._bound_devices = {}  # Track bound devices (e.g., REM/DIS)
        self._bound_to = None  # Track which REM/DIS this FAN is bound to

        """
        _2411_params
            A dictionary of 2411 parameters.
            This is only created if the device supports 2411.
            We need a special way to store these parameters since we cannot rely on
            the standard msg_value method where only the last msg is stored.
            Unlike eg the 31DA, the 2411 parameters each have their own RP and I codes.
            I did concider using _pkt._ctx, but it seems work in progress and i don't know
            if it would break anything else in the codebase.
            This _2411_params is straightforward, and only used if there is support anyways.
        found_codes
            Whenever a message is received, the code is added to this set.
            This allows to track support of certain codes (e.g. 2411).

        Callback Mechanism:
            The callback system allows other components to be notified when the device is fully initialized.
            See [callback_mechanism.md](../docs/callback_mechanism.md) for detailed documentation.
        """

    def add_initialization_callback(self, callback, code=None):
        """Add a callback to be called after initialization is complete.

        Args:
            callback: A callable that will be invoked when the device is initialized.
                     If code is provided, the callback will only be called when that
                     specific code is processed.
            code: Optional message code (e.g., Code._2411) that triggers this callback.
                  If None, the callback will be called for any initialization.
        """
        if code is None or code in self._found_codes:
            try:
                callback()
            except Exception as ex:
                _LOGGER.warning("Error in initialization callback: %s", ex)
        else:
            self._pending_callbacks.append((code, callback))

    def _process_pending_callbacks(self, code=None):
        """Process any pending callbacks that match the given code.

        Args:
            code: The message code that was just processed. If None, processes all
                  callbacks regardless of code.

        Callbacks are processed in the order they were added. After a callback is
        successfully processed, its code is marked as initialized, so future callbacks
        for the same code will be executed immediately.
        """
        remaining_callbacks = []
        processed_codes = set()

        for cb_code, callback in self._pending_callbacks:
            if code is not None and cb_code is not None and cb_code != code:
                remaining_callbacks.append((cb_code, callback))
                continue

            try:
                callback()
                # Only mark as processed if no exception was raised
                if cb_code is not None:
                    processed_codes.add(cb_code)
            except Exception as e:
                _LOGGER.warning(
                    f"Error in callback for {self} (code={cb_code}): {e}",
                    exc_info=True,
                )
                remaining_callbacks.append((cb_code, callback))

        # Update found codes with all processed codes
        self._found_codes.update(processed_codes)

        # If we processed a specific code, ensure it's marked as found
        if code is not None and code not in self._found_codes:
            self._found_codes.add(code)

        self._pending_callbacks = remaining_callbacks

    @property
    def supports_2411(self) -> bool:
        """Return True if this device supports 2411 parameters."""
        return Code._2411 in self._found_codes

    @property
    def param_value(self, param_id: str) -> Any:
        """Get a parameter value, compatible with _msg_value pattern."""
        if hasattr(self, "_2411_params") and param_id in self._2411_params:
            return self._2411_params[param_id]
        return None

    @property
    def hgi(self):
        """Return the HGI device if available."""
        if self._hgi is None and self._gwy and hasattr(self._gwy, "hgi"):
            self._hgi = self._gwy.hgi
        return self._hgi

    def _handle_2411_message(self, msg: Message) -> None:
        # Initialize the parameters dictionary if it doesn't exist
        if not hasattr(self, "_2411_params"):
            self._2411_params = {}

        # Mark as supporting 2411 if we get a valid response (RP) or info (I)
        if msg.verb in (RP, I_):
            _LOGGER.debug("Processing 2411 message with verb %s", msg.verb)
            if not self.supports_2411:
                self._found_codes.add(Code._2411)
                _LOGGER.info(
                    "Device %s supports 2411 parameters (detected via %s message)",
                    self.id,
                    msg.verb,
                )
            # Process parameter value if payload is valid
            if (
                isinstance(msg.payload, dict)
                and "parameter" in msg.payload
                and "value" in msg.payload
            ):
                param_id = msg.payload["parameter"].upper()
                value = msg.payload["value"]

                _LOGGER.debug(
                    "Processing 2411 parameter: device=%s, param_id=%s, value=%s",
                    self.id,
                    param_id,
                    value,
                )

                # Store the parameter value in the standard location used by _msg_value
                old_value = (
                    self._2411_params.get(param_id)
                    if hasattr(self, "_2411_params")
                    else None
                )
                self._2411_params[param_id] = value

                _LOGGER.debug(
                    "Stored parameter %s value: %s (total params: %d)",
                    param_id,
                    value,
                    len(self._2411_params),
                )

                # Also store in the _params_2411 dictionary for backward compatibility
                if not hasattr(self, "_params_2411"):
                    self._params_2411 = {}
                self._params_2411[param_id] = value

    def _handle_msg(self, msg: Message) -> None:
        """Handle a message from this device.

        Args:
            msg: The incoming message to process

        This method processes the message and triggers any callbacks registered
        for the message's code. It also handles special processing for 2411 messages.
        """
        super()._handle_msg(msg)

        # Special handling for 2411 parameter messages
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

        # Process callbacks for this specific code
        if hasattr(msg, "code") and msg.code is not None:
            self._process_pending_callbacks(code=msg.code)

        # # After first message, mark as initialized and process callbacks
        # if not self._initialized:
        #     self._initialized = True
        #     self._process_pending_callbacks()

    def _setup_discovery_cmds(self) -> None:
        super()._setup_discovery_cmds()

        # RP --- 32:155617 18:005904 --:------ 22F1 003 000207
        self._add_discovery_cmd(
            Command.from_attrs(RQ, self.id, Code._22F1, "00"), 60 * 60 * 24, delay=15
        )  # to learn scheme: orcon/itho/other (04/07/0?)

        # Add a single discovery command for all parameters (3F likely to be supported if any)
        # The handler will process the response and update the appropriate parameter and
        # also set the supports_2411 flag
        _LOGGER.debug("Adding single discovery command for all 2411 parameters")
        self._add_discovery_cmd(
            Command.from_attrs(RQ, self.id, Code._2411, "00003F"),
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
            self._add_discovery_cmd(
                Command.from_attrs(RQ, self.id, code, "00"), 60 * 30, delay=15
            )

        for code in (Code._313E, Code._3222):
            self._add_discovery_cmd(
                Command.from_attrs(RQ, self.id, code, "00"), 60 * 30, delay=30
            )

    def add_bound_device(self, device_id: str, device_type: str) -> None:
        """Add a bound device to this FAN.

        Args:
            device_id: The ID of the device to bind
            device_type: The type of device (e.g., 'REM', 'DIS')
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

        Args:
            device_id: The ID of the device to unbind
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

        Returns:
            The device ID of the first bound REM/DIS device, or None if none found
        """
        if not self._bound_devices:
            _LOGGER.debug("No bound devices found for FAN %s", self.id)
            return None

        # Return the first bound device ID
        device_id = next(iter(self._bound_devices))
        _LOGGER.debug("Using bound device %s for FAN %s", device_id, self.id)
        return device_id

    def get_fan_param(self, param_id: str) -> Any:
        """Get a fan parameter value by ID.

        Args:
            param_id: The parameter ID to get (e.g. '31' or '3E')
                    Case-insensitive, will be converted to uppercase for lookup

        Returns:
            The parameter value if found, None otherwise
        """
        if not self.supports_2411:
            _LOGGER.debug(
                "Cannot get parameter %s from %s: 2411 not supported", param_id, self.id
            )
            return None

        # Ensure param_id is uppercase for consistent lookup
        param_id = param_id.upper()

        # Check if we have the parameter in our cache
        if hasattr(self, "_2411_params") and param_id in self._2411_params:
            value = self._2411_params[param_id]
            _LOGGER.debug(
                "Found cached parameter %s for %s: %s", param_id, self.id, value
            )
            return value

        # Parameter not in cache, request it
        _LOGGER.debug("Requesting parameter %s from %s", param_id, self.id)
        self._send_cmd(Command.from_attrs(RQ, self.id, Code._2411, f"0000{param_id}"))
        _LOGGER.debug("Sent parameter request for %s to %s", param_id, self.id)

        return None

    # @property
    # def fan_params(self) -> dict[str, Any]:
    #     """Return a dictionary of all fan parameters."""
    #     from ramses_tx.ramses import _2411_PARAMS_SCHEMA

    #     params = {}
    #     for param_id in _2411_PARAMS_SCHEMA:
    #         # Ensure param_id is uppercase hex for consistency
    #         normalized_id = param_id.upper()
    #         # Use the normalized parameter ID as the key and fetch its value
    #         params[normalized_id] = self.get_fan_param(normalized_id)

    #     return params

    async def set_fan_param(
        self, param_id: str, value: Any, max_retries: int = 2, timeout: float = 5.0
    ) -> bool:
        """Set a fan parameter value with request/response tracking.

        Args:
            param_id: The parameter ID to set (e.g., '31' or '3E')
            value: The value to set (will be converted to int)
            max_retries: Maximum number of retry attempts (unused, kept for backward compatibility)
            timeout: Timeout in seconds for each retry (unused, kept for backward compatibility)

        Returns:
            bool: True if the parameter was set successfully, False otherwise

        Note:
            This method sends the command directly to the FAN device using the bound REM/DIS device
            as the source. The FAN must be bound to a REM or DIS device for this to work.
        """
        if not self.supports_2411:
            _LOGGER.debug(
                "Cannot set parameter %s on %s: 2411 parameters not supported",
                param_id,
                self.id,
            )
            return False

        # Ensure param_id is uppercase for consistency
        param_id = param_id.upper()

        # Get the bound REM/DIS device ID
        src_id = self.get_bound_rem()
        if not src_id:
            _LOGGER.error(
                "Cannot set parameter %s on %s: No bound REM/DIS device found. "
                "The FAN must be bound to a REM or DIS device to set parameters. "
                "See https://github.com/zxdavb/ramses_cc/wiki/5.-Faking-&-Impersonation for more information.",
                param_id,
                self.id,
            )
            return False

        _LOGGER.debug(
            "Setting parameter %s to %s on %s using bound device %s",
            param_id,
            value,
            self.id,
            src_id,
        )

        try:
            # Convert value to int if it's numeric
            try:
                value_int = int(value)
            except (TypeError, ValueError) as err:
                _LOGGER.error(
                    "Invalid value '%s' for parameter %s on %s: %s",
                    value,
                    param_id,
                    self.id,
                    err,
                )
                return False

            # Create and send the command
            cmd = Command.set_fan_param(self.id, param_id, value_int, src_id=src_id)
            self._send_cmd(cmd)
            _LOGGER.debug("Sent parameter set command: %s", cmd)
            return True

        except Exception as err:
            _LOGGER.error(
                "Failed to set parameter %s to %s on %s: %s",
                param_id,
                value,
                self.id,
                err,
                exc_info=_LOGGER.isEnabledFor(logging.DEBUG),
            )
            return False

    @property
    def air_quality(self) -> float | None:
        return self._msg_value(Code._31DA, key=SZ_AIR_QUALITY)

    @property
    def air_quality_base(self) -> float | None:
        return self._msg_value(Code._31DA, key=SZ_AIR_QUALITY_BASIS)

    @property
    def bypass_mode(self) -> str | None:
        """
        :return: bypass mode as on|off|auto
        """
        return self._msg_value(Code._22F7, key=SZ_BYPASS_MODE)

    @property
    def bypass_position(self) -> float | str | None:
        """
        Position info is found in 22F7 and in 31DA. The most recent packet is returned.
        :return: bypass position as percentage: 0.0 (closed) or 1.0 (open), on error: "x_faulted"
        """
        # if both packets exist and both have the key, returns the most recent
        return self._msg_value((Code._22F7, Code._31DA), key=SZ_BYPASS_POSITION)

    @property
    def bypass_state(self) -> str | None:
        """
        Orcon, others?
        :return: bypass position as on/off
        """
        return self._msg_value(Code._22F7, key=SZ_BYPASS_STATE)

    @property
    def co2_level(self) -> int | None:
        return self._msg_value(Code._31DA, key=SZ_CO2_LEVEL)

    @property
    def exhaust_fan_speed(
        self,
    ) -> float | None:
        """
        Some fans (Vasco, Itho) use Code._31D9 for speed + mode,
        Orcon sends SZ_EXHAUST_FAN_SPEED in 31DA. See parser for details.
        :return: speed as percentage
        """
        speed: float = -1
        for code in [c for c in (Code._31D9, Code._31DA) if c in self._msgs]:
            if v := self._msgs[code].payload.get(SZ_EXHAUST_FAN_SPEED):
                # if both packets exist and both have the key, use the highest value
                if v is not None:
                    speed = max(v, speed)
        if speed >= 0:
            return speed
        return None

    @property
    def exhaust_flow(self) -> float | None:
        return self._msg_value(Code._31DA, key=SZ_EXHAUST_FLOW)

    @property
    def exhaust_temp(self) -> float | None:
        return self._msg_value(Code._31DA, key=SZ_EXHAUST_TEMP)

    @property
    def fan_rate(self) -> str | None:
        """
        Lookup fan mode description from _22F4  message payload, e.g. "low", "medium", "boost".
        For manufacturers Orcon, Vasco, ClimaRad.

        :return: int or str describing rate of fan
        """
        return self._msg_value(Code._22F4, key=SZ_FAN_RATE)

    @property
    def fan_mode(self) -> str | None:
        """
        Lookup fan mode description from _22F4  message payload, e.g. "auto", "manual", "off".
        For manufacturers Orcon, Vasco, ClimaRad.

        :return: a string describing mode
        """
        return self._msg_value(Code._22F4, key=SZ_FAN_MODE)

    @property
    def fan_info(self) -> str | None:
        """
        Extract fan info description from _31D9 or _31DA message payload, e.g. "speed 2, medium".
        By its name, the result is automatically displayed in HA Climate UI.
        Some manufacturers (Orcon, Vasco) include the fan mode (auto, manual), others don't (Itho).

        :return: a string describing mode, speed
        """
        if Code._31D9 in self._msgs:
            # Itho, Vasco D60 and ClimaRad (MiniBox fan) send mode/speed in _31D9
            v: str
            for k, v in self._msgs[Code._31D9].payload.items():
                if k == SZ_FAN_MODE and len(v) > 2:  # prevent non-lookups to pass
                    return v
            # continue to 31DA
        return str(self._msg_value(Code._31DA, key=SZ_FAN_INFO))  # Itho lookup

    @property
    def indoor_humidity(self) -> float | None:
        """
        Extract humidity value from _12A0 or _31DA JSON message payload

        :return: percentage <= 1.0
        """
        if Code._12A0 in self._msgs and isinstance(
            self._msgs[Code._12A0].payload, list
        ):  # FAN Ventura sends RH/temps as a list; element [0] contains indoor_hum
            if v := self._msgs[Code._12A0].payload[0].get(SZ_INDOOR_HUMIDITY):
                assert isinstance(v, (float | type(None)))
                return v
        return self._msg_value((Code._12A0, Code._31DA), key=SZ_INDOOR_HUMIDITY)

    @property
    def indoor_temp(self) -> float | None:
        return self._msg_value(Code._31DA, key=SZ_INDOOR_TEMP)

    @property
    def outdoor_humidity(self) -> float | None:
        if Code._12A0 in self._msgs and isinstance(
            self._msgs[Code._12A0].payload, list
        ):  # FAN Ventura sends RH/temps as a list; element [1] contains outdoor_hum
            if v := self._msgs[Code._12A0].payload[1].get(SZ_OUTDOOR_HUMIDITY):
                assert isinstance(v, (float | type(None)))
                return v
        return self._msg_value(Code._31DA, key=SZ_OUTDOOR_HUMIDITY)

    @property
    def outdoor_temp(self) -> float | None:
        return self._msg_value(Code._31DA, key=SZ_OUTDOOR_TEMP)

    @property
    def post_heat(self) -> int | None:
        return self._msg_value(Code._31DA, key=SZ_POST_HEAT)

    @property
    def pre_heat(self) -> int | None:
        return self._msg_value(Code._31DA, key=SZ_PRE_HEAT)

    @property
    def remaining_mins(self) -> int | None:
        return self._msg_value(Code._31DA, key=SZ_REMAINING_MINS)

    @property
    def request_fan_speed(self) -> float | None:
        return self._msg_value(Code._2210, key=SZ_REQ_SPEED)

    @property
    def request_src(self) -> str | None:
        """
        Orcon, others?
        :return: source sensor of auto speed request: IDL, CO2 or HUM
        """
        return self._msg_value(Code._2210, key=SZ_REQ_REASON)

    @property
    def speed_cap(self) -> int | None:
        return self._msg_value(Code._31DA, key=SZ_SPEED_CAPABILITIES)

    @property
    def supply_fan_speed(self) -> float | None:
        return self._msg_value(Code._31DA, key=SZ_SUPPLY_FAN_SPEED)

    @property
    def supply_flow(self) -> float | None:
        return self._msg_value(Code._31DA, key=SZ_SUPPLY_FLOW)

    @property
    def supply_temp(self) -> float | None:
        if Code._12A0 in self._msgs and isinstance(
            self._msgs[Code._12A0].payload, list
        ):  # FAN Ventura sends RH/temps as a list;
            # pass element [0] in place of supply_temp, which is always None in VenturaV1x 31DA
            if v := self._msgs[Code._12A0].payload[1].get(SZ_TEMPERATURE):
                assert isinstance(v, (float | type(None)))
                return v
        return self._msg_value(Code._31DA, key=SZ_SUPPLY_TEMP)

    @property
    def status(self) -> dict[str, Any]:
        return {
            **super().status,
            SZ_EXHAUST_FAN_SPEED: self.exhaust_fan_speed,
            **{
                k: v
                for code in [c for c in (Code._31D9, Code._31DA) if c in self._msgs]
                for k, v in self._msgs[code].payload.items()
                if k != SZ_EXHAUST_FAN_SPEED
            },
        }

    @property
    def temperature(self) -> float | None:  # Celsius
        if Code._12A0 in self._msgs and isinstance(
            self._msgs[Code._12A0].payload, list
        ):  # FAN Ventura sends RH/temps as a list; use element [1]
            if v := self._msgs[Code._12A0].payload[0].get(SZ_TEMPERATURE):
                assert isinstance(v, (float | type(None)))
                return v
        # ClimaRad minibox FAN sends (indoor) temp in 12A0
        return self._msg_value(Code._12A0, key=SZ_TEMPERATURE)


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

    May return a base clase, DeviceHvac, which will need promotion.
    """

    if not eavesdrop:
        raise TypeError(f"No HVAC class for: {dev_addr} (no eavesdropping)")

    if msg is None:
        raise TypeError(f"No HVAC class for: {dev_addr} (no msg)")

    if klass := HVAC_KLASS_BY_VC_PAIR.get((msg.verb, msg.code)):
        return HVAC_CLASS_BY_SLUG[klass]

    if msg.code in CODES_OF_HVAC_DOMAIN_ONLY:
        return DeviceHvac

    raise TypeError(f"No HVAC class for: {dev_addr} (insufficient meta-data)")


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
# CVE/HRU remote (536-0124) [RFT W: 3 modes, timer]
    "away":       (Code._22F1, 00, 01|04"),  # how to invoke?
    "low":        (Code._22F1, 00, 02|04"),  # aka eco
    "medium":     (Code._22F1, 00, 03|04"),  # aka auto (with sensors) - is that only for 63?
    "high":       (Code._22F1, 00, 04|04"),  # aka full

    "timer_1":    (Code._22F3, 00, 00|0A"),  # 10 minutes full speed
    "timer_2":    (Code._22F3, 00, 00|14"),  # 20 minutes full speed
    "timer_3":    (Code._22F3, 00, 00|1E"),  # 30 minutes full speed

# RFT-AUTO (536-0150) [RFT CAR: 2 modes, auto, timer]: idx = 63, essentially same as above, but also...
    "auto_night": (Code._22F8, 63, 02|03"),  # additional - press auto x2

# RFT-RV (04-00046), RFT-CO2 (04-00045) - sensors with control
    "medium":     (Code._22F1, 00, 03|07"), 1=away, 2=low?
    "auto":       (Code._22F1, 00, 05|07"), 4=high
    "auto_night": (Code._22F1, 00, 0B|0B"),

    "timer_1":    (Code._22F3, 00, 00|0A, 00|00, 0000"),  # 10 minutes
    "timer_2":    (Code._22F3, 00, 00|14, 00|00, 0000"),  # 20 minutes
    "timer_3":    (Code._22F3, 00, 00|1E, 00|00, 0000"),  # 30 minutes

# RFT-PIR (545-7550) - presence sensor

# RFT_DF: DemandFlow remote (536-0146)
    "timer_1":    (Code._22F3, 00, 42|03, 03|03"),  # 0b01-000-010 = 3 hrs, back to last mode
    "timer_2":    (Code._22F3, 00, 42|06, 03|03"),  # 0b01-000-010 = 6 hrs, back to last mode
    "timer_3":    (Code._22F3, 00, 42|09, 03|03"),  # 0b01-000-010 = 9 hrs, back to last mode
    "cook_30":    (Code._22F3, 00, 02|1E, 02|03"),  # 30 mins (press 1x)
    "cook_60":    (Code._22F3, 00, 02|3C, 02|03"),  # 60 mins (press 2x)

    "low":        (Code._22F8, 00, 01|02"),  # ?eco     co2 <= 1200 ppm?
    "high":       (Code._22F8, 00, 02|02"),  # ?comfort co2 <= 1000 ppm?

# Join commands:
    "CVERFT":     (Code._1FC9,  00, Code._22F1, 0x000000,                        01, Code._10E0, 0x000000"),  # CVE/HRU remote    (536-0124)
    "AUTORFT":    (Code._1FC9,  63, Code._22F8, 0x000000,                        01, Code._10E0, 0x000000"),  # AUTO RFT          (536-0150)
    "DF":         (Code._1FC9,  00, Code._22F8, 0x000000,                        00, Code._10E0, 0x000000"),  # DemandFlow remote (536-0146)
    "RV":         (Code._1FC9,  00, Code._12A0, 0x000000,                        01, Code._10E0, 0x000000,  00, Code._31E0, 0x000000,  00, Code._1FC9, 0x000000"),  # RFT-RV   (04-00046)
    "CO2":        (Code._1FC9,  00, Code._1298, 0x000000,  00, Code._2E10, 0x000000,  01, Code._10E0, 0x000000,  00, Code._31E0, 0x000000,  00, Code._1FC9, 0x000000"),  # RFT-CO2  (04-00045)

# Leave commands:
    "Others":      (Code._1FC9, 00, Code._1FC9, 0x000000"),  # standard leave command
    "AUTORFT":     (Code._1FC9, 63, Code._1FC9, 0x000000"),  # leave command of AUTO RFT (536-0150)

    # RQ 0x00
    # I_ 0x01
    # W_ 0x02
    # RP 0x03

"""
