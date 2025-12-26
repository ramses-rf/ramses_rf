#!/usr/bin/env python3

# TODO:
# - sort out gwy.config...
# - sort out reduced processing


"""RAMSES RF -the gateway (i.e. HGI80 / evofw3, not RFG100)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from ramses_tx import (
    Address,
    Command,
    Engine,
    Message,
    Packet,
    Priority,
    extract_known_hgi_id,
    is_valid_dev_id,
    protocol_factory,
    set_pkt_logging_config,
    transport_factory,
)
from ramses_tx.const import (
    DEFAULT_GAP_DURATION,
    DEFAULT_MAX_RETRIES,
    DEFAULT_NUM_REPEATS,
    DEFAULT_SEND_TIMEOUT,
    DEFAULT_WAIT_FOR_REPLY,
    SZ_ACTIVE_HGI,
)
from ramses_tx.schemas import (
    SCH_ENGINE_CONFIG,
    SZ_BLOCK_LIST,
    SZ_ENFORCE_KNOWN_LIST,
    SZ_KNOWN_LIST,
    PktLogConfigT,
    PortConfigT,
)
from ramses_tx.transport import SZ_READER_TASK

from .const import DONT_CREATE_MESSAGES, SZ_DEVICES
from .database import MessageIndex
from .device import DeviceHeat, DeviceHvac, Fakeable, HgiGateway, device_factory
from .dispatcher import detect_array_fragment, process_msg
from .schemas import (
    SCH_GATEWAY_CONFIG,
    SCH_GLOBAL_SCHEMAS,
    SCH_TRAITS,
    SZ_ALIAS,
    SZ_CLASS,
    SZ_CONFIG,
    SZ_DISABLE_DISCOVERY,
    SZ_ENABLE_EAVESDROP,
    SZ_FAKED,
    SZ_MAIN_TCS,
    SZ_ORPHANS,
    load_schema,
)
from .system import Evohome

from .const import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    I_,
    RP,
    RQ,
    W_,
    Code,
)

if TYPE_CHECKING:
    from ramses_tx import DeviceIdT, DeviceListT, RamsesTransportT

    from .device import Device
    from .entity_base import Parent

_LOGGER = logging.getLogger(__name__)


class Gateway(Engine):
    """The gateway class.

    This class serves as the primary interface for the RAMSES RF network. It manages
    the serial connection (via ``Engine``), device discovery, schema maintenance,
    and message dispatching.
    """

    def __init__(
        self,
        port_name: str | None,
        input_file: str | None = None,
        port_config: PortConfigT | None = None,
        packet_log: PktLogConfigT | None = None,
        block_list: DeviceListT | None = None,
        known_list: DeviceListT | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
        transport_constructor: Callable[..., Awaitable[RamsesTransportT]] | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the Gateway instance.

        :param port_name: The serial port name (e.g., '/dev/ttyUSB0') or None if using a file.
        :type port_name: str | None
        :param input_file: Path to a packet log file for playback, defaults to None.
        :type input_file: str | None, optional
        :param port_config: Configuration dictionary for the serial port, defaults to None.
        :type port_config: PortConfigT | None, optional
        :param packet_log: Configuration for packet logging, defaults to None.
        :type packet_log: PktLogConfigT | None, optional
        :param block_list: A list of device IDs to block/ignore, defaults to None.
        :type block_list: DeviceListT | None, optional
        :param known_list: A list of known device IDs and their traits, defaults to None.
        :type known_list: DeviceListT | None, optional
        :param loop: The asyncio event loop to use, defaults to None.
        :type loop: asyncio.AbstractEventLoop | None, optional
        :param transport_constructor: A factory for creating the transport layer, defaults to None.
        :type transport_constructor: Callable[..., Awaitable[RamsesTransportT]] | None, optional
        :param kwargs: Additional configuration parameters passed to the engine and schema.
        :type kwargs: Any
        """
        if kwargs.pop("debug_mode", None):
            _LOGGER.setLevel(logging.DEBUG)

        kwargs = {k: v for k, v in kwargs.items() if k[:1] != "_"}  # anachronism
        config: dict[str, Any] = kwargs.pop(SZ_CONFIG, {})

        super().__init__(
            port_name,
            input_file=input_file,
            port_config=port_config,
            packet_log=packet_log,
            block_list=block_list,
            known_list=known_list,
            loop=loop,
            transport_constructor=transport_constructor,
            **SCH_ENGINE_CONFIG(config),
        )

        if self._disable_sending:
            config[SZ_DISABLE_DISCOVERY] = True
        if config.get(SZ_ENABLE_EAVESDROP):
            _LOGGER.warning(
                f"{SZ_ENABLE_EAVESDROP}=True: this is strongly discouraged"
                " for routine use (there be dragons here)"
            )

        self.config = SimpleNamespace(**SCH_GATEWAY_CONFIG(config))
        self._schema: dict[str, Any] = SCH_GLOBAL_SCHEMAS(kwargs)

        self._tcs: Evohome | None = None

        self.devices: list[Device] = []
        self.device_by_id: dict[DeviceIdT, Device] = {}

        self.msg_db: MessageIndex | None = None

    def __repr__(self) -> str:
        """Return a string representation of the Gateway.

        :returns: A string describing the gateway's input source (port or file).
        :rtype: str
        """
        if not self.ser_name:
            return f"Gateway(input_file={self._input_file})"
        return f"Gateway(port_name={self.ser_name}, port_config={self._port_config})"

    @property
    def hgi(self) -> HgiGateway | None:
        """Return the active HGI80-compatible gateway device, if known.

        :returns: The gateway device instance or None if the transport is not set up
                  or the HGI ID is not found.
        :rtype: HgiGateway | None
        """
        if not self._transport:
            return None
        if device_id := self._transport.get_extra_info(SZ_ACTIVE_HGI):
            return self.device_by_id.get(device_id)  # type: ignore[return-value]
        return None

    async def start(
        self,
        /,
        *,
        start_discovery: bool = True,
        cached_packets: dict[str, str] | None = None,
    ) -> None:
        """Start the Gateway and Initiate discovery as required.

        This method initializes packet logging, the SQLite index, loads the schema,
        and optionally restores state from cached packets before starting the transport.

        :param start_discovery: Whether to initiate the discovery process after start, defaults to True.
        :type start_discovery: bool, optional
        :param cached_packets: A dictionary of packet strings used to restore state, defaults to None.
        :type cached_packets: dict[str, str] | None, optional
        :returns: None
        :rtype: None
        """

        def initiate_discovery(dev_list: list[Device], sys_list: list[Evohome]) -> None:
            _LOGGER.debug("ENGINE: Initiating/enabling discovery...")

            # [d._start_discovery_poller() for d in devs]
            for device in dev_list:
                device._start_discovery_poller()

            for system in sys_list:
                system._start_discovery_poller()
                for zone in system.zones:
                    zone._start_discovery_poller()
                if system.dhw:
                    system.dhw._start_discovery_poller()

        await set_pkt_logging_config(  # type: ignore[arg-type]
            cc_console=self.config.reduce_processing >= DONT_CREATE_MESSAGES,
            **self._packet_log,
        )

        # initialize SQLite index, set in _tx/Engine
        if self._sqlite_index:  # TODO(eb): default to ON in Q4 2025
            _LOGGER.info("Ramses RF starts SQLite MessageIndex")
            self.create_sqlite_message_index()  # if activated in ramses_cc > Engine

        # temporarily turn on discovery, remember original state
        self.config.disable_discovery, disable_discovery = (
            True,
            self.config.disable_discovery,
        )

        load_schema(self, known_list=self._include, **self._schema)  # create faked too

        await super().start()  # TODO: do this *after* restore cache
        if cached_packets:
            await self._restore_cached_packets(cached_packets)

        # reset discovery to original state
        self.config.disable_discovery = disable_discovery

        if (
            not self._disable_sending
            and not self.config.disable_discovery
            and start_discovery
        ):
            initiate_discovery(self.devices, self.systems)

    def create_sqlite_message_index(self) -> None:
        """Initialize the SQLite MessageIndex.

        :returns: None
        :rtype: None
        """
        self.msg_db = MessageIndex()  # start the index

    async def stop(self) -> None:
        """Stop the Gateway and tidy up.

        Stops the message database and the underlying engine/transport.

        :returns: None
        :rtype: None
        """

        if self.msg_db:
            self.msg_db.stop()
        await super().stop()

    def _pause(self, *args: Any) -> None:
        """Pause the (unpaused) gateway (disables sending/discovery).

        There is the option to save other objects, as `args`.

        :param args: Additional objects/state to save during the pause.
        :type args: Any
        :returns: None
        :rtype: None
        :raises RuntimeError: If the engine fails to pause.
        """
        _LOGGER.debug("Gateway: Pausing engine...")

        self.config.disable_discovery, disc_flag = True, self.config.disable_discovery

        try:
            super()._pause(disc_flag, *args)
        except RuntimeError:
            self.config.disable_discovery = disc_flag
            raise

    def _resume(self) -> tuple[Any]:
        """Resume the (paused) gateway (enables sending/discovery, if applicable).

        Will restore other objects, as `args`.

        :returns: A tuple of arguments saved during the pause.
        :rtype: tuple[Any]
        """
        args: tuple[Any]

        _LOGGER.debug("Gateway: Resuming engine...")

        self.config.disable_discovery, *args = super()._resume()  # type: ignore[assignment]

        return args

    def get_state(
        self, include_expired: bool = False
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Return the current schema & state (may include expired packets).

        :param include_expired: If True, include expired packets in the state, defaults to False.
        :type include_expired: bool, optional
        :returns: A tuple containing the schema dictionary and the packet log dictionary.
        :rtype: tuple[dict[str, Any], dict[str, str]]
        """

        self._pause()

        def wanted_msg(msg: Message, include_expired: bool = False) -> bool:
            if msg.code == Code._313F:
                return msg.verb in (I_, RP)  # usu. expired, useful 4 back-back restarts
            if msg._expired and not include_expired:
                return False
            if msg.code == Code._0404:
                return msg.verb in (I_, W_) and msg._pkt._len > 7
            if msg.verb in (W_, RQ):
                return False
            # if msg.code == Code._1FC9 and msg.verb != RP:
            #     return True
            return include_expired or not msg._expired

        if self.msg_db:
            pkts = {
                f"{repr(msg._pkt)[:26]}": f"{repr(msg._pkt)[27:]}"
                for msg in self.msg_db.all(include_expired=True)
                if wanted_msg(msg, include_expired=include_expired)
            }
        else:  # deprecated, to be removed in Q1 2026
            msgs = [m for device in self.devices for m in device._msg_list]
            # add systems._msgs and zones._msgs
            for system in self.systems:
                msgs.extend(list(system._msgs.values()))
                msgs.extend([m for z in system.zones for m in z._msgs.values()])
                # msgs.extend([m for z in system.dhw for m in z._msgs.values()])  # TODO: DHW
                # Related to/Fixes ramses_cc Issue 249 non-existing via-device _HW ?

            pkts = {  # BUG: assumes pkts have unique dtms: may be untrue for contrived logs
                f"{repr(msg._pkt)[:26]}": f"{repr(msg._pkt)[27:]}"
                for msg in msgs
                if wanted_msg(msg, include_expired=include_expired)
            }
            # _LOGGER.warning("Missing MessageIndex")

        self._resume()

        return self.schema, dict(sorted(pkts.items()))

    async def _restore_cached_packets(
        self, packets: dict[str, str], _clear_state: bool = False
    ) -> None:
        """Restore cached packets (may include expired packets).

        This process uses a temporary transport to replay the packet history
        into the message handler.

        :param packets: A dictionary of packet strings.
        :type packets: dict[str, str]
        :param _clear_state: If True, reset internal state before restoration (for testing), defaults to False.
        :type _clear_state: bool, optional
        :returns: None
        :rtype: None
        """

        def clear_state() -> None:
            _LOGGER.info("GATEWAY: Clearing existing schema/state...")

            # self._schema = {}

            self._tcs = None
            self.devices = []
            self.device_by_id = {}

            self._prev_msg = None
            self._this_msg = None

        tmp_transport: RamsesTransportT  # mypy hint

        _LOGGER.debug("GATEWAY: Restoring a cached packet log...")
        self._pause()

        if _clear_state:  # only intended for test suite use
            clear_state()

        # We do not always enforce the known_list whilst restoring a cache because
        # if it does not contain a correctly configured HGI, a 'working' address is
        # used (which could be different to the address in the cache) & wanted packets
        # can be dropped unnecessarily.

        enforce_include_list = bool(
            self._enforce_known_list
            and extract_known_hgi_id(
                self._include, disable_warnings=True, strict_checking=True
            )
        )

        # The actual HGI address will be discovered when the actual transport was/is
        # started up (usually before now)

        tmp_protocol = protocol_factory(
            self._msg_handler,
            disable_sending=True,
            enforce_include_list=enforce_include_list,
            exclude_list=self._exclude,
            include_list=self._include,
        )

        tmp_transport = await transport_factory(
            tmp_protocol,
            packet_dict=packets,
        )

        await tmp_transport.get_extra_info(SZ_READER_TASK)

        _LOGGER.debug("GATEWAY: Restored, resuming")
        self._resume()

    def _add_device(self, dev: Device) -> None:  # TODO: also: _add_system()
        """Add a device to the gateway (called by devices during instantiation).

        :param dev: The device instance to add.
        :type dev: Device
        :returns: None
        :rtype: None
        :raises LookupError: If the device already exists in the gateway.
        """

        if dev.id in self.device_by_id:
            raise LookupError(f"Device already exists: {dev.id}")

        self.devices.append(dev)
        self.device_by_id[dev.id] = dev

    def get_device(
        self,
        device_id: DeviceIdT,
        *,
        msg: Message | None = None,
        parent: Parent | None = None,
        child_id: str | None = None,
        is_sensor: bool | None = None,
    ) -> Device:  # TODO: **schema/traits) -> Device:  # may: LookupError
        """Return a device, creating it if it does not already exist.

        This method uses provided traits to create or update a device and optionally
        passes a message for it to handle. All devices have traits, but only
        controllers (CTL, UFC) have a schema.

        :param device_id: The unique identifier for the device (e.g., '01:123456').
        :type device_id: DeviceIdT
        :param msg: An optional initial message for the device to process, defaults to None.
        :type msg: Message | None, optional
        :param parent: The parent entity of this device, if any, defaults to None.
        :type parent: Parent | None, optional
        :param child_id: The specific ID of the child component if applicable, defaults to None.
        :type child_id: str | None, optional
        :param is_sensor: Indicates if this device should be treated as a sensor, defaults to None.
        :type is_sensor: bool | None, optional
        :returns: The existing or newly created device instance.
        :rtype: Device
        :raises LookupError: If the device ID is blocked or not in the allowed known_list.
        """

        def check_filter_lists(dev_id: DeviceIdT) -> None:  # may: LookupError
            """Raise a LookupError if a device_id is filtered out by a list."""

            if dev_id in self._unwanted:  # TODO: shouldn't invalidate a msg
                raise LookupError(f"Can't create {dev_id}: it is unwanted or invalid")

            if self._enforce_known_list and (
                dev_id not in self._include and dev_id != getattr(self.hgi, "id", None)
            ):
                self._unwanted.append(dev_id)
                raise LookupError(
                    f"Can't create {dev_id}: it is not an allowed device_id"
                    f" (if required, add it to the {SZ_KNOWN_LIST})"
                )

            if dev_id in self._exclude:
                self._unwanted.append(dev_id)
                raise LookupError(
                    f"Can't create {dev_id}: it is a blocked device_id"
                    f" (if required, remove it from the {SZ_BLOCK_LIST})"
                )

        try:
            check_filter_lists(device_id)
        except LookupError:
            # have to allow for GWY not being in known_list...
            if device_id != self._protocol.hgi_id:
                raise  # TODO: make parochial

        dev = self.device_by_id.get(device_id)

        if not dev:
            # voluptuous bug workaround: https://github.com/alecthomas/voluptuous/pull/524
            _traits: dict[str, Any] = self._include.get(device_id, {})  # type: ignore[assignment]
            _traits.pop("commands", None)

            traits: dict[str, Any] = SCH_TRAITS(self._include.get(device_id, {}))

            dev = device_factory(self, Address(device_id), msg=msg, **_traits)

            if traits.get(SZ_FAKED):
                if isinstance(dev, Fakeable):
                    dev._make_fake()
                else:
                    _LOGGER.warning(f"The device is not fakeable: {dev}")

        # TODO: the exact order of the following may need refining...
        # TODO: some will be done by devices themselves?

        # if schema:  # Step 2: Only controllers have a schema...
        #     dev._update_schema(**schema)  # TODO: schema/traits

        if parent or child_id:
            dev.set_parent(parent, child_id=child_id, is_sensor=is_sensor)

        # if msg:
        #     dev._handle_msg(msg)

        return dev

    def fake_device(
        self,
        device_id: DeviceIdT,
        create_device: bool = False,
    ) -> Device | Fakeable:
        """Create a faked device.

        Converts an existing device to a fake device, or creates a new fake device
        if it satisfies strict criteria (valid ID, presence in known_list).

        :param device_id: The ID of the device to fake.
        :type device_id: DeviceIdT
        :param create_device: If True, allow creation of a new device if it doesn't exist, defaults to False.
        :type create_device: bool, optional
        :returns: The faked device instance.
        :rtype: Device | Fakeable
        :raises TypeError: If the device ID is invalid or the device is not fakeable.
        :raises LookupError: If the device does not exist and create_device is False,
                             or if create_device is True but the ID is not in known_list.
        """

        if not is_valid_dev_id(device_id):
            raise TypeError(f"The device id is not valid: {device_id}")

        if not create_device and device_id not in self.device_by_id:
            raise LookupError(f"The device id does not exist: {device_id}")
        elif create_device and device_id not in self.known_list:
            raise LookupError(f"The device id is not in the known_list: {device_id}")

        if (dev := self.get_device(device_id)) and isinstance(dev, Fakeable):
            dev._make_fake()
            return dev

        raise TypeError(f"The device is not fakeable: {device_id}")

    @property
    def tcs(self) -> Evohome | None:
        """Return the primary Temperature Control System (TCS), if any.

        :returns: The primary Evohome system or None.
        :rtype: Evohome | None
        """

        if self._tcs is None and self.systems:
            self._tcs = self.systems[0]
        return self._tcs

    @property
    def known_list(self) -> DeviceListT:
        """Return the working known_list (a superset of the provided known_list).

        Unlike orphans, which are always instantiated when a schema is loaded, these
        devices may/may not exist. However, if they are ever instantiated, they should
        be given these traits.

        :returns: A dictionary where keys are device IDs and values are their traits.
        :rtype: DeviceListT
        """

        result = self._include  # could be devices here, not (yet) in gwy.devices
        result.update(
            {
                d.id: {k: d.traits[k] for k in (SZ_CLASS, SZ_ALIAS, SZ_FAKED)}  # type: ignore[misc]
                for d in self.devices
                if not self._enforce_known_list or d.id in self._include
            }
        )
        return result

    @property
    def system_by_id(self) -> dict[DeviceIdT, Evohome]:
        """Return a mapping of device IDs to their associated Evohome systems.

        :returns: A dictionary mapping DeviceId to Evohome instances.
        :rtype: dict[DeviceIdT, Evohome]
        """
        return {
            d.id: d.tcs
            for d in self.devices
            if hasattr(d, "tcs") and getattr(d.tcs, "id", None) == d.id
        }  # why something so simple look so messy

    @property
    def systems(self) -> list[Evohome]:
        """Return a list of all identified Evohome systems.

        :returns: A list of Evohome system instances.
        :rtype: list[Evohome]
        """
        return list(self.system_by_id.values())

    @property
    def _config(self) -> dict[str, Any]:
        """Return the working configuration.

        Includes:
         - config
         - schema (everything else)
         - known_list
         - block_list

        :returns: A dictionary representing the current internal configuration state.
        :rtype: dict[str, Any]
        """

        return {
            "_gateway_id": self.hgi.id if self.hgi else None,
            SZ_MAIN_TCS: self.tcs.id if self.tcs else None,
            SZ_CONFIG: {SZ_ENFORCE_KNOWN_LIST: self._enforce_known_list},
            SZ_KNOWN_LIST: self.known_list,
            SZ_BLOCK_LIST: [{k: v} for k, v in self._exclude.items()],
            "_unwanted": sorted(self._unwanted),
        }

    @property
    def schema(self) -> dict[str, Any]:
        """Return the global schema.

        This 'active' schema may exclude non-present devices from the configured schema
        that was loaded during initialisation.

        Orphans are devices that 'exist' but don't yet have a place in the schema
        hierarchy (if ever): therefore, they are instantiated when the schema is loaded,
        just like the other devices in the schema.

        :returns: A dictionary representing the entire system schema structure.
        :rtype: dict[str, Any]
        """

        schema: dict[str, Any] = {SZ_MAIN_TCS: self.tcs.ctl.id if self.tcs else None}

        for tcs in self.systems:
            schema[tcs.ctl.id] = tcs.schema

        dev_list: list[DeviceIdT] = sorted(
            [
                d.id
                for d in self.devices
                if not getattr(d, "tcs", None)
                and isinstance(d, DeviceHeat)
                and d._is_present
            ]
        )
        schema[f"{SZ_ORPHANS}_heat"] = dev_list

        dev_list = sorted(
            [d.id for d in self.devices if isinstance(d, DeviceHvac) and d._is_present]
        )
        schema[f"{SZ_ORPHANS}_hvac"] = dev_list

        return schema

    @property
    def params(self) -> dict[str, Any]:
        """Return the parameters for all devices.

        :returns: A dictionary containing parameters for every device in the gateway.
        :rtype: dict[str, Any]
        """
        return {SZ_DEVICES: {d.id: d.params for d in sorted(self.devices)}}

    @property
    def status(self) -> dict[str, Any]:
        """Return the status for all devices and the transport rate.

        :returns: A dictionary containing device statuses and transmission rate.
        :rtype: dict[str, Any]
        """
        tx_rate = self._transport.get_extra_info("tx_rate") if self._transport else None
        return {
            SZ_DEVICES: {d.id: d.status for d in sorted(self.devices)},
            "_tx_rate": tx_rate,
        }

    def _msg_handler(self, msg: Message) -> None:
        """A callback to handle messages from the protocol stack.

        Handles message reassembly (fragmentation) and dispatches the message for processing.

        :param msg: The incoming message to handle.
        :type msg: Message
        :returns: None
        :rtype: None
        """

        super()._msg_handler(msg)

        # TODO: ideally remove this feature...
        assert self._this_msg  # mypy check

        if self._prev_msg and detect_array_fragment(self._this_msg, self._prev_msg):
            msg._pkt._force_has_array()  # may be an array of length 1
            msg._payload = self._prev_msg.payload + (
                msg.payload if isinstance(msg.payload, list) else [msg.payload]
            )

        process_msg(self, msg)

    def send_cmd(
        self,
        cmd: Command,
        /,
        *,
        gap_duration: float = DEFAULT_GAP_DURATION,
        num_repeats: int = DEFAULT_NUM_REPEATS,
        priority: Priority = Priority.DEFAULT,
        timeout: float = DEFAULT_SEND_TIMEOUT,
        wait_for_reply: bool | None = DEFAULT_WAIT_FOR_REPLY,
    ) -> asyncio.Task[Packet]:
        """Wrapper to schedule an async_send_cmd() and return the Task.

        :param cmd: The command object to send.
        :type cmd: Command
        :param gap_duration: The gap between repeats (in seconds), defaults to DEFAULT_GAP_DURATION.
        :type gap_duration: float, optional
        :param num_repeats: Number of times to repeat the command (0 = once, 1 = twice, etc.), defaults to DEFAULT_NUM_REPEATS.
        :type num_repeats: int, optional
        :param priority: The priority of the command, defaults to Priority.DEFAULT.
        :type priority: Priority, optional
        :param timeout: Time to wait for a send to complete, defaults to DEFAULT_SEND_TIMEOUT.
        :type timeout: float, optional
        :param wait_for_reply: Whether to wait for a reply packet, defaults to DEFAULT_WAIT_FOR_REPLY.
        :type wait_for_reply: bool | None, optional
        :returns: The asyncio Task wrapping the send operation.
        :rtype: asyncio.Task[Packet]
        """

        coro = self.async_send_cmd(
            cmd,
            gap_duration=gap_duration,
            num_repeats=num_repeats,
            priority=priority,
            timeout=timeout,
            wait_for_reply=wait_for_reply,
        )

        task = self._loop.create_task(coro)
        self.add_task(task)
        return task

    async def async_send_cmd(
        self,
        cmd: Command,
        /,
        *,
        gap_duration: float = DEFAULT_GAP_DURATION,
        num_repeats: int = DEFAULT_NUM_REPEATS,
        priority: Priority = Priority.DEFAULT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        timeout: float = DEFAULT_SEND_TIMEOUT,
        wait_for_reply: bool | None = DEFAULT_WAIT_FOR_REPLY,
    ) -> Packet:
        """Send a Command and return the corresponding (echo or reply) Packet.

        If wait_for_reply is True (*and* the Command has a rx_header), return the
        reply Packet. Otherwise, simply return the echo Packet.

        :param cmd: The command object to send.
        :type cmd: Command
        :param gap_duration: The gap between repeats (in seconds), defaults to DEFAULT_GAP_DURATION.
        :type gap_duration: float, optional
        :param num_repeats: Number of times to repeat the command, defaults to DEFAULT_NUM_REPEATS.
        :type num_repeats: int, optional
        :param priority: The priority of the command, defaults to Priority.DEFAULT.
        :type priority: Priority, optional
        :param max_retries: Maximum number of retries if sending fails, defaults to DEFAULT_MAX_RETRIES.
        :type max_retries: int, optional
        :param timeout: Time to wait for the command to send, defaults to DEFAULT_SEND_TIMEOUT.
        :type timeout: float, optional
        :param wait_for_reply: Whether to wait for a reply packet, defaults to DEFAULT_WAIT_FOR_REPLY.
        :type wait_for_reply: bool | None, optional
        :returns: The echo packet or reply packet depending on wait_for_reply.
        :rtype: Packet
        :raises ProtocolSendFailed: If the command was sent but no reply/echo was received.
        :raises ProtocolError: If the system failed to attempt the transmission.
        """

        return await super().async_send_cmd(
            cmd,
            gap_duration=gap_duration,
            num_repeats=num_repeats,
            priority=priority,
            max_retries=max_retries,
            timeout=timeout,
            wait_for_reply=wait_for_reply,
        )  # may: raise ProtocolError/ProtocolSendFailed
