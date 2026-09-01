#!/usr/bin/env python3
"""RAMSES RF - a RAMSES-II protocol decoder & analyser.

Base for all devices.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, datetime as dt, timedelta as td
from typing import TYPE_CHECKING, Any, Self

from ramses_rf.address import Address
from ramses_rf.binding_fsm import BindingManager
from ramses_rf.const import (
    DEV_TYPE_MAP,
    GATEWAY_MESSAGE_TIMEOUT,
    HEARTBEAT_TIMEOUT_DEFAULT,
    SZ_BATTERY_LEVEL,
    SZ_BATTERY_LOW,
    SZ_BATTERY_STATE,
    SZ_OEM_CODE,
    DevType,
)
from ramses_rf.entity import Entity, class_by_attr
from ramses_rf.exceptions import DeviceNotFaked, SchemaInconsistentError
from ramses_rf.models import (
    ActuatorState,
    DemandState,
    PowerState,
    TemperatureState,
)
from ramses_rf.models.state_signal import CommunicationQuality, compute_quality
from ramses_rf.schemas import (
    SZ_ALIAS,
    SZ_CLASS,
    SZ_FAKED,
    SZ_IS_BATTERY,
    SZ_POLLING_INTERVAL,
)
from ramses_rf.strategies import HvacStrategy, best_hvac_strategy
from ramses_rf.topology import Child
from ramses_tx import Packet
from ramses_tx.const import Code

from ..messages import Message
from ..protocol.ramses import CODES_BY_DEV_SLUG

if TYPE_CHECKING:
    from ramses_rf import Gateway
    from ramses_rf.devices.hvac_ventilators import HvacVentilator
    from ramses_rf.models import DeviceTraits
    from ramses_rf.systems import Zone
    from ramses_rf.typing import PollingIntervalsT
    from ramses_tx import CommandDTO
    from ramses_tx.const import IndexT
    from ramses_tx.typing import DeviceIdT


BIND_WAITING_TIMEOUT = 300  # how long to wait, listening for an offer
BIND_REQUEST_TIMEOUT = (
    5  # how long to wait for an accept after sending an offer
)
BIND_CONFIRM_TIMEOUT = (
    5  # how long to wait for a confirm after sending an accept
)


_LOGGER = logging.getLogger(__name__)


class DeviceBase(Entity):
    """The Device base class - can also be used for unknown device types."""

    _SLUG: str = DevType.DEV
    _STATE_ATTR: str | None = None

    _binding_manager: BindingManager | None = None

    def __init__(
        self,
        gateway: Gateway,
        device_address: Address,
        *,
        traits: DeviceTraits | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialise the device base class.

        :param gateway: The gateway instance managing this device.
        :type gateway: Gateway
        :param device_address: The physical address of the device.
        :type device_address: Address
        :param traits: Optional traits to apply during initialisation.
        :type traits: DeviceTraits | None
        :param kwargs: Additional arguments for the underlying entity.
        :type kwargs: Any
        """
        super().__init__(gateway, **kwargs)

        # FIXME: gwy.message_store entities must know their parent device ID
        # and their own index
        self._z_id = device_address.id  # the responsible device is itself
        self._z_index = None  # depends upon its location in the schema

        self.id: DeviceIdT = device_address.id

        self.addr = device_address
        self.type = (
            device_address.type
        )  # DEX  # TODO: remove this attr? use SLUG?

        self._scheme: str | None = traits.scheme if traits else None
        self._strategy: HvacStrategy | None = None
        self._polling_interval: PollingIntervalsT | None = (
            traits.polling_interval if traits else None
        )
        self._is_battery: bool | None = traits.is_battery if traits else None
        self._last_msg_dtm: dt | None = None

        self.power_state = PowerState()

    def __str__(self) -> str:
        """Return a string representation of the device."""
        if self._STATE_ATTR and hasattr(self, self._STATE_ATTR):
            state = getattr(self, self._STATE_ATTR)
            if not callable(state) and state is not None:
                return f"{self.id} ({self._SLUG}): {state}"
        return f"{self.id} ({self._SLUG})"

    def __lt__(self, other: object) -> bool:
        """Return True if this device's ID is less than the other's."""
        if not hasattr(other, "id"):
            return NotImplemented
        return bool(self.id < other.id)

    @property
    def heartbeat_timeout(self) -> td:
        """Return the timeout after which the device is considered unavailable.

        :return: The timeout duration before going unavailable.
        :rtype: td
        """
        return HEARTBEAT_TIMEOUT_DEFAULT

    @property
    def is_available(self) -> bool:
        """Return True if the device is available based on its heartbeat.

        :return: Availability status based on the latest message
            timestamp.
        :rtype: bool
        """
        if self._last_msg_dtm is None:
            return True  # Assume available until we receive baseline telemetry

        if self._last_msg_dtm.tzinfo is not None:
            now = dt.now(UTC).astimezone(self._last_msg_dtm.tzinfo)
        else:
            now = dt.now()

        return bool((now - self._last_msg_dtm) <= self.heartbeat_timeout)

    @property
    def communication_quality(self) -> CommunicationQuality | None:
        """Return the device's communication quality snapshot, or None.

        Computes ``best_rssi`` across all HGI RSSI trackers and
        staleness from the device's last message timestamp.  Returns
        ``None`` if the gateway has no RSSI tracker (e.g. during
        tests without a real gateway).

        When the transport is a ``PooledTransport``, the per-child
        RSSI trackers are included so that the **best** RSSI across
        all connected HGIs is used (issue 1119 — multi-HGI pool).

        :return: Communication quality snapshot, or ``None``.
        :rtype: CommunicationQuality | None
        """
        gwy = getattr(self, "_gateway", None)
        if gwy is None:
            return None
        tracker = getattr(gwy, "_rssi_tracker", None)
        if tracker is None:
            return None
        trackers = [tracker]
        # If the transport is a PooledTransport, include per-child
        # RSSI trackers so we use the best RSSI across all HGIs.
        transport = getattr(gwy, "_transport", None)
        if transport is not None:
            pool_trackers = transport.get_extra_info("pool_rssi_trackers")
            if pool_trackers:
                trackers.extend(pool_trackers)
        return compute_quality(str(self.id), trackers)

    def set_strategy(self, strategy: HvacStrategy) -> None:
        """Set the HVAC strategy for this device.

        :param strategy: Vendor-specific HVAC strategy.
        :type strategy: HvacStrategy
        :rtype: None
        """
        self._strategy = strategy

    def _get_strategy(self) -> HvacStrategy:
        """Return the explicit or scheme-selected HVAC strategy."""
        return self._strategy or best_hvac_strategy(self.id, self._scheme)

    def _get_configured_strategy(self) -> HvacStrategy | None:
        """Return a strategy only when explicitly configured or selected."""
        if self._strategy:
            return self._strategy
        if self._scheme:
            return best_hvac_strategy(self.id, self._scheme)
        return None

    def _update_traits(self, traits: DeviceTraits) -> None:
        """Update a device with new schema attributes.

        :param traits: The traits to apply (e.g., alias, class, faked)
        :raises DeviceNotFaked: If the device is not fakeable but
            'faked' is set.
        :rtype: None
        """
        if traits.faked:  # class & alias are done elsewhere
            if not isinstance(self, Fakeable):
                raise DeviceNotFaked(
                    f"Device is not fakeable: {self} (traits={traits})"
                )
            self._make_fake()

        self._scheme = traits.scheme
        self._polling_interval = traits.polling_interval
        self._is_battery = traits.is_battery

    @classmethod
    def create_from_schema(
        cls,
        gateway: Gateway,
        device_address: Address,
        *,
        traits: DeviceTraits | None = None,
    ) -> Self:
        """Create a device (for a GWY) and set its schema attrs (aka traits).

        All devices have traits, but also controllers (CTL, UFC) have a
        system schema.

        The appropriate Device class should have been determined by a
        factory. Schema attrs include: class (SLUG), alias, and faked.

        :param gateway: The gateway to attach the device to.
        :type gateway: Gateway
        :param device_address: The physical address of the device.
        :type device_address: Address
        :param traits: The traits to apply to the newly created device.
        :type traits: DeviceTraits | None
        :return: The fully initialised device instance.
        :rtype: DeviceBase
        """
        device = cls(gateway, device_address, traits=traits)
        if traits:
            device._update_traits(traits)
        return device

    async def has_battery(self) -> None | bool:  # 1060
        """Return True if the device is battery powered.

        Excludes battery-backup devices.

        :return: True if the device has a battery, False otherwise.
        :rtype: None | bool
        """
        return isinstance(self, BatteryState) or (
            self.power_state.battery_low is not None
            or self.power_state.battery_level is not None
        )

    @property
    def polling_interval(self) -> PollingIntervalsT | None:
        """Return the polling interval dictionary for this device.

        :return: A dictionary mapping command code to interval in seconds, or None.
        :rtype: PollingIntervalsT | None
        """
        return self._polling_interval

    @property
    def effective_polling_interval(self) -> PollingIntervalsT | None:
        """Return the active effective polling schedule for this device.

        :returns: A dictionary mapping active command codes to interval seconds.
        :rtype: PollingIntervalsT | None
        """
        if getattr(self._gateway, "polling_manager", None):
            mgr = self._gateway.polling_manager
            return mgr.resolve_schedule_for_device(self)
        return self._polling_interval

    def set_polling_interval(self, interval: int | None) -> None:
        """Set or update the overall polling interval override for this device.

        :param interval: The polling interval in seconds, or None to reset to default.
        :type interval: int | None
        :raises ValueError: If interval is negative or if setting a battery device below 300s.
        """
        if interval is not None and interval < 0:
            raise ValueError(
                f"Polling interval cannot be negative: {interval}"
            )
        if self.is_battery and interval is not None and 0 < interval < 300:
            raise ValueError(
                f"Battery-powered device {self.id} polling interval cannot be set below 300s (got {interval}s)"
            )

        if interval is None:
            self._polling_interval = None
        else:
            eff_schedule = self.effective_polling_interval or {}
            codes = list(eff_schedule.keys()) or [Code._10E0]
            self._polling_interval = {code: interval for code in codes}

        if getattr(self._gateway, "polling_manager", None):
            self._gateway.polling_manager.update_device_tasks(self)

    def set_command_polling_interval(
        self, code: str, interval: int | None
    ) -> None:
        """Set or update the polling interval for a specific packet code.

        :param code: The hex code string for the packet type.
        :type code: str
        :param interval: The polling interval in seconds (None or 0 to reset).
        :type interval: int | None
        :raises ValueError: If interval is negative or if setting a battery device below 300s.
        """
        if interval is not None and interval < 0:
            raise ValueError(
                f"Polling interval cannot be negative: {interval}"
            )
        if self.is_battery and interval is not None and 0 < interval < 300:
            raise ValueError(
                f"Battery-powered device {self.id} polling interval cannot be set below 300s (got {interval}s)"
            )

        if self._polling_interval is None:
            self._polling_interval = {}

        if interval is None:
            self._polling_interval.pop(code, None)
        else:
            self._polling_interval[code] = interval

        if getattr(self._gateway, "polling_manager", None):
            self._gateway.polling_manager.update_device_tasks(self)

    @property
    def is_battery(self) -> bool | None:
        """Return True if the device is explicitly configured as battery-powered.

        :return: True if marked as battery-powered in traits, False if mains-powered, or None.
        :rtype: bool | None
        """
        if self._is_battery is not None:
            return self._is_battery
        if isinstance(self, BatteryState):
            return True
        return None

    @property
    def is_faked(self) -> bool:
        """Return True if the device is faked.

        :return: True if the device is actively faked.
        :rtype: bool
        """
        return bool(self._binding_manager)  # isinstance(self, Fakeable) and...

    @property
    def _is_binding(self) -> bool:
        """Return True if the (faked) device is actively binding."""
        return bool(
            self._binding_manager and self._binding_manager.is_binding is True
        )

    async def _is_present(self) -> bool:
        """Exclude ghost devices from corrupt packet addresses.

        Inspects the active message log flat cache to verify if any
        unexpired packets have been received from this device.
        """
        msgs = await self.entity_state.get_message_log_flat()
        return any(
            m.src == self
            for m in msgs.values()
            if not getattr(m, "_expired", False)
        )  # TODO: needs addressing

    async def schema(self) -> dict[str, Any]:
        """Return the fixed attributes of the device.

        :return: A dictionary containing the device schema.
        :rtype: dict[str, Any]
        """
        return {}  # SZ_CLASS: DEV_TYPE_MAP[self._SLUG]}

    async def params(self) -> dict[str, Any]:
        """Return the configurable attributes of the device.

        :return: A dictionary containing device parameters.
        :rtype: dict[str, Any]
        """
        return {}

    async def status(self) -> dict[str, Any]:
        """Return the state attributes of the device.

        :return: A dictionary of status properties.
        :rtype: dict[str, Any]
        """
        return {}

    async def traits(self) -> dict[str, Any]:
        """Get the traits of the device.

        :return: A dictionary detailing the device's traits.
        :rtype: dict[str, Any]
        """
        result = await self.entity_state.traits()

        known_dev = self._gateway.config.known_list.get(self.id)

        result.update(
            {
                SZ_CLASS: DEV_TYPE_MAP[self._SLUG],
                SZ_ALIAS: known_dev.get(SZ_ALIAS) if known_dev else None,
                SZ_FAKED: self.is_faked,
                SZ_POLLING_INTERVAL: self.polling_interval,
                SZ_IS_BATTERY: self.is_battery,
            }
        )

        result["_bind"] = await self.entity_state.get_value(Code._1FC9)
        return result


class BatteryState(DeviceBase):  # 1060
    """The base state class for battery-powered devices.

    battery_low: boolean
    battery_level: float percentage (0.0-1.0)
    battery_state: dict containing is_low, level
    """

    async def battery_low(self) -> None | bool:  # 1060
        """Return the current low battery warning state.

        :return: True if the battery is low, otherwise False.
        :rtype: None | bool
        """
        if self.is_faked:
            return False
        return self.power_state.battery_low

    async def battery_state(self) -> dict[str, Any] | None:  # 1060
        """Return a mapping of the current battery state.

        :return: A dictionary containing battery low and level metrics.
        :rtype: dict[str, Any] | None
        """
        if self.is_faked:
            return None
        if self.power_state.battery_level is None:
            return None
        return {
            SZ_BATTERY_LOW: self.power_state.battery_low,
            SZ_BATTERY_LEVEL: self.power_state.battery_level,
        }

    async def status(self) -> dict[str, Any]:
        """Return the state attributes of the device including battery.

        :return: A dictionary of status properties.
        :rtype: dict[str, Any]
        """
        base_status = await super().status()
        if (bat_state := await self.battery_state()) is not None:
            return {
                **base_status,
                SZ_BATTERY_STATE: bat_state,
            }
        return base_status


class DeviceInfo(DeviceBase):  # 10E0
    """The base state class for device information (10E0) payloads."""

    async def device_info(self) -> dict[str, Any] | None:  # 10E0
        """Return the device specification and manufacturing data.

        :return: A dictionary of device information.
        :rtype: dict[str, Any] | None
        """
        result = await self.entity_state.get_value(Code._10E0)
        return result if isinstance(result, dict) else None

    async def traits(self) -> dict[str, Any]:
        """Return the traits of the device.

        :return: A dictionary detailing the device's traits.
        :rtype: dict[str, Any]
        """
        result = await super().traits()
        msgs = await self.entity_state.get_message_log_flat()

        if Code._10E0 in msgs or Code._10E0 in CODES_BY_DEV_SLUG.get(
            self._SLUG, []
        ):
            result.update({"_info": await self.device_info()})

        return result


class Fakeable(DeviceBase):
    """Base class for faked or impersonated devices.

    There are two types of Faking: impersonation (of real devices) and
    full-faking.

    Impersonation of physical devices simply means sending packets on
    their behalf. This is straight-forward for sensors and remotes
    (they do not usually receive packets).

    Faked (virtual) devices must have any packet addressed to them sent
    to their handle_msg() method by the dispatcher. Impersonated
    devices will simply pick up such packets via RF.
    """

    def __init__(
        self,
        gateway: Gateway,
        *args: Any,
        traits: DeviceTraits | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialise a device capable of being faked or impersonated.

        :param gateway: The gateway managing the faked device.
        :type gateway: Gateway
        :param args: Positional arguments for the base device.
        :type args: Any
        :param traits: Optional traits establishing faking context.
        :type traits: DeviceTraits | None
        :param kwargs: Keyword arguments for the underlying entity.
        :type kwargs: Any
        """
        super().__init__(gateway, *args, traits=traits, **kwargs)

        self._binding_manager: BindingManager | None = None

        if self.id in gateway.config.known_list and gateway.config.known_list[
            self.id
        ].get(SZ_FAKED):
            self._make_fake()

        if traits and traits.faked:
            self._make_fake()

    def _make_fake(self) -> None:
        """Enable faking mechanisms for this device."""
        if self._binding_manager:
            return

        self._binding_manager = BindingManager(self, self._gateway.dispatcher)
        if self.id not in self._gateway.config.known_list:
            self._gateway.config.known_list[self.id] = {}
        self._gateway.config.known_list[self.id][SZ_FAKED] = (
            True  # TODO: remove this
        )
        _LOGGER.info("Faking now enabled for: %s", self)

    async def _wait_for_binding_request(
        self,
        accept_codes: Iterable[Code],
        /,
        *,
        zone_index: IndexT = "00",
        require_ratify: bool = False,
    ) -> tuple[Message, Message, Message, Message | None]:
        """Listen for a binding and return the Offer packets.

        :param accept_codes: The codes allowed for this binding.
        :type accept_codes: Iterable[Code]
        :param zone_index: The index to bind to, defaults to "00".
        :type zone_index: IndexT
        :param require_ratify: Whether ratification is required.
        :type require_ratify: bool
        :return: A tuple of the four binding transaction packets.
        :rtype: tuple[Message, Message, Message, Message | None]
        """
        if not self._binding_manager:
            raise DeviceNotFaked(f"Device is not fakeable: {self}")

        return await self._binding_manager.wait_for_binding_request(
            accept_codes, zone_index=zone_index, require_ratify=require_ratify
        )

    async def wait_for_binding_request(
        self,
        accept_codes: Iterable[Code],
        /,
        *,
        zone_index: IndexT = "00",
        require_ratify: bool = False,
    ) -> tuple[Message, Message, Message, Message | None]:
        """Listen for a binding and return the Offer packets.

        :param accept_codes: The codes allowed for this binding.
        :type accept_codes: Iterable[Code]
        :param zone_index: The index to bind to, defaults to "00".
        :type zone_index: IndexT
        :param require_ratify: Whether ratification is required.
        :type require_ratify: bool
        :return: A tuple of the four binding transaction packets.
        :rtype: tuple[Message, Message, Message, Message | None]
        :raises NotImplementedError: Subclasses must implement this.
        """
        raise NotImplementedError

    async def _initiate_binding_process(
        self,
        offer_codes: Code | Iterable[Code],
        /,
        *,
        confirm_code: Code | None = None,
        ratify_command: CommandDTO | None = None,
    ) -> tuple[Message, Message, Message, Packet | None]:
        """Start a binding and return the Accept, or raise an exception.

        :param offer_codes: Codes to offer during the binding process.
        :type offer_codes: Code | Iterable[Code]
        :param confirm_code: The code required to confirm the bind.
        :type confirm_code: Code | None
        :param ratify_command: An optional ratification command to send.
        :type ratify_command: CommandDTO | None
        :return: A tuple of the binding transaction packets.
        :rtype: tuple[Message, Message, Message, Packet | None]
        :raises DeviceNotFaked: If faking is not enabled.
        """
        # confirm_code can be FFFF.

        if not self._binding_manager:
            raise DeviceNotFaked(f"Device is not fakeable: {self}")

        if isinstance(offer_codes, str):
            codes: tuple[Code, ...] = (offer_codes,)
        else:
            codes = tuple(offer_codes)

        return await self._binding_manager.initiate_binding_process(
            codes, confirm_code=confirm_code, ratify_command=ratify_command
        )

    async def initiate_binding_process(
        self,
    ) -> tuple[Message, Message, Message, Packet | None]:
        """Start a binding and return the Accept, or raise an exception.

        :return: A tuple of the binding transaction packets.
        :rtype: tuple[Message, Message, Message, Packet | None]
        :raises NotImplementedError: Subclasses must implement this.
        """
        raise NotImplementedError

    async def _wait_for_binding_accept(
        self, offer: Message, /, *, zone_index: IndexT = "00"
    ) -> tuple[Packet, Message, Packet, Packet | None]:
        """Listen for a binding accept packet.

        :param offer: The binding offer message.
        :type offer: Message
        :param zone_index: The index to bind to, defaults to "00".
        :type zone_index: IndexT
        :return: A tuple of the binding transaction packets.
        :rtype: tuple[Packet, Message, Packet, Packet | None]
        :raises NotImplementedError: Subclasses must implement this.
        """
        raise NotImplementedError

    async def oem_code(self) -> str | None:
        """Return the device Original Equipment Manufacturer code.

        Returns the 2-character ASCII OEM string for this device, if one
        is present in traits or message state.

        :return: The Original Equipment Manufacturer code string.
        :rtype: str | None
        """
        traits = await self.traits()
        if not traits.get(SZ_OEM_CODE):
            result = await self.entity_state.get_value(
                Code._10E0, key=SZ_OEM_CODE
            )
            return str(result) if result is not None else None
        oem = traits.get(SZ_OEM_CODE)
        return str(oem) if oem is not None else None


class Device(Child, DeviceBase):
    """The base class for all devices."""

    def __init__(
        self,
        gateway: Gateway,
        device_address: Address,
        *,
        traits: DeviceTraits | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialise a standard child device within the topology.

        :param gateway: The gateway managing this device.
        :type gateway: Gateway
        :param device_address: The physical address of the device.
        :type device_address: Address
        :param traits: Optional traits outlining class and aliases.
        :type traits: DeviceTraits | None
        :param kwargs: Additional arguments for the base initialiser.
        :type kwargs: Any
        """
        _LOGGER.debug(
            "Creating a Device: %s (%s)", device_address.id, self.__class__
        )
        super().__init__(gateway, device_address, traits=traits, **kwargs)

        gateway.device_registry._add_device(self)


class HgiGateway(Device):  # HGI (18:)
    """The HGI80 base class."""

    _SLUG: str = DevType.HGI

    def __init__(
        self, *args: Any, traits: DeviceTraits | None = None, **kwargs: Any
    ) -> None:
        """Initialise the hardware gateway interface device.

        :param args: Positional arguments for the base initialiser.
        :type args: Any
        :param traits: Optional traits dictating configuration.
        :type traits: DeviceTraits | None
        :param kwargs: Keyword arguments for the base initialiser.
        :type kwargs: Any
        """
        super().__init__(*args, traits=traits, **kwargs)

        self._child_id = "gw"  # TODO

    @property
    def message_timeout(self) -> td:
        """Return the dynamic timeout threshold for the gateway.

        :return: The configured or default message timeout limit.
        :rtype: td
        """
        # Safely extract the custom timeout from the GatewayConfig
        custom_timeout = getattr(self._gateway.config, "gateway_timeout", None)

        if custom_timeout is not None:
            return td(minutes=int(custom_timeout))

        return GATEWAY_MESSAGE_TIMEOUT

    async def is_active(self) -> bool:
        """Return True if the gateway has received messages recently.

        :return: The active operational status of the gateway interface.
        :rtype: bool
        """
        # Ensure that this message is safely extracted
        protocol = getattr(self._gateway._engine, "_protocol", None)
        last_msg = getattr(protocol, "_this_msg", None)

        if not last_msg or not hasattr(last_msg, "timestamp"):
            return False

        dtm: dt = last_msg.timestamp
        now = (
            dt.now(UTC).astimezone(dtm.tzinfo)
            if dtm.tzinfo is not None
            else dt.now()
        )

        # Compare against our new dynamic property
        return bool((now - dtm) < self.message_timeout)


class DeviceHeat(Device):  # Heat domain: Honeywell CH/DHW or compatible
    """Base class for Honeywell CH/DHW-compatible heating devices.

    Includes UFH and heatpumps (which can also cool).
    """

    _SLUG: str = DevType.HEA  # shouldn't be any of these instantiated

    def __init__(
        self,
        gateway: Gateway,
        device_address: Address,
        *,
        traits: DeviceTraits | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialise a device within the heating domain.

        :param gateway: The gateway managing this heating device.
        :type gateway: Gateway
        :param device_address: The physical address of the device.
        :type device_address: Address
        :param traits: Optional traits detailing structural schemas.
        :type traits: DeviceTraits | None
        :param kwargs: Additional arguments for the base initialiser.
        :type kwargs: Any
        """
        super().__init__(gateway, device_address, traits=traits, **kwargs)

        self._child_id = None  # domain_id, or zone_index

        self._iz_controller: None | bool | Message = None

        self.temp_state = TemperatureState()
        self.demand_state = DemandState()
        self.act_state: ActuatorState | None = None

    def _make_tcs_controller(
        self, *, msg: Message | None = None, **schema: Any
    ) -> None:  # CH/DHW
        """Attach a TCS (create/update as required) after passing it any msg."""
        if (
            self.type not in DEV_TYPE_MAP.CONTROLLERS
        ):  # potentially can be controllers
            raise SchemaInconsistentError(
                f"Invalid device type to be a controller: {self}"
            )

        self._iz_controller = self._iz_controller or msg or True

    @property
    def _is_controller(self) -> None | bool:
        """Return True if the device is designated as a controller."""
        if self._iz_controller is not None:
            return bool(self._iz_controller)  # True, False, or msg

        if self.ctl is not None:  # TODO: messy
            return self.ctl is self

        return False

    @property
    def zone(self) -> Zone | None:
        """Return the device's parent zone, if known.

        :return: The parent zone instance, or None if unassigned.
        :rtype: Zone | None
        """
        # Deferred import to prevent circular dependency at module load time
        # DO NOT MOVE to module level.
        from ramses_rf.systems.zones import ZoneBase

        return self._parent if isinstance(self._parent, ZoneBase) else None  # type: ignore[return-value]


class DeviceHvac(Device):  # HVAC domain: ventilation, PIV, MV/HR
    """The Device base class for the HVAC domain (ventilation, PIV, MV/HR)."""

    _SLUG: str = (
        DevType.HVC
    )  # these may be instantiated, and promoted later on

    def __init__(
        self,
        gateway: Gateway,
        device_address: Address,
        *,
        traits: DeviceTraits | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialise a device within the HVAC ventilation domain.

        :param gateway: The gateway managing this HVAC device.
        :type gateway: Gateway
        :param device_address: The physical address of the device.
        :type device_address: Address
        :param traits: Optional traits detailing structural schemas.
        :type traits: DeviceTraits | None
        :param kwargs: Additional arguments for the base initialiser.
        :type kwargs: Any
        """
        super().__init__(gateway, device_address, traits=traits, **kwargs)

        self._child_id = "hv"  # TODO: domain_id/deprecate
        # 6d: bidirectional parent link — set by HvacVentilator._update_schema()
        # when this device is added to a FAN's remotes[]/sensors[] list.
        # Used by ramses_cc's via_device logic to group REM/CO2 under their
        # FAN in the HA device registry.
        self._parent_fan: HvacVentilator | None = None


# e.g. {"HGI": HgiGateway}
BASE_CLASS_BY_SLUG: dict[str, type[Device]] = class_by_attr(__name__, "_SLUG")
