#!/usr/bin/env python3

# TODO:
# - sort out reduced processing

"""RAMSES RF -the gateway (i.e. HGI80 / evofw3, not RFG100)."""

from __future__ import annotations

import asyncio
import logging
import threading
import warnings
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime as dt, timedelta as td
from logging.handlers import QueueListener
from typing import TYPE_CHECKING, Any, Literal, cast

from ramses_tx import (
    Command,
    Engine,
    Packet,
    extract_known_hgi_id,
    protocol_factory,
    set_pkt_logging_config,
)
from ramses_tx.config import EngineConfig
from ramses_tx.const import (
    DEFAULT_GAP_DURATION,
    DEFAULT_MAX_RETRIES,
    DEFAULT_NUM_REPEATS,
    DEFAULT_SEND_TIMEOUT,
    DEFAULT_WAIT_FOR_REPLY,
    SZ_ACTIVE_HGI,
    Priority,
)
from ramses_tx.dtos import PacketDTO
from ramses_tx.exceptions import ProtocolSendFailed
from ramses_tx.logger import flush_packet_log
from ramses_tx.ramses import CODES_SCHEMA
from ramses_tx.schemas import SZ_BLOCK_LIST, SZ_ENFORCE_KNOWN_LIST, SZ_KNOWN_LIST
from ramses_tx.typing import PayloadT

from . import message as rf_msg
from .application_message import ApplicationMessage
from .const import DONT_CREATE_MESSAGES, HIGH_VOLUME_STATUS_CODES

from .const import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    I_,
    RP,
    RQ,
    W_,
    Code,
    VerbT,
)
from .device import HgiGateway
from .device_filter import DeviceFilter
from .device_registry import DeviceRegistry
from .dispatcher import detect_array_fragment, process_msg
from .interfaces import (
    DeviceFilterInterface,
    DeviceRegistryInterface,
    GatewayInterface,
    MessageStoreInterface,
)
from .message import Message
from .message_store import MessageStore
from .schemas import (
    SCH_GLOBAL_SCHEMAS,
    SZ_CONFIG,
    SZ_ENABLE_EAVESDROP,
    SZ_MAIN_TCS,
    SZ_ORPHANS,
    load_schema,
)
from .system import Evohome
from .typing import DeviceIdT, DeviceListT

if TYPE_CHECKING:
    from ramses_tx import RamsesTransportT

    from .device import Device

_LOGGER = logging.getLogger(__name__)


@dataclass
class GatewayConfig:
    """Configuration parameters for the Ramses Gateway.

    :param disable_discovery: Disable device discovery, defaults to
        False.
    :type disable_discovery: bool
    :param enable_eavesdrop: Enable eavesdropping mode, defaults to
        False.
    :type enable_eavesdrop: bool
    :param reduce_processing: Level of reduced processing, defaults to
        0.
    :type reduce_processing: int
    :param max_zones: Maximum number of zones allowed, defaults to 12.
    :type max_zones: int
    :param use_aliases: Mapping of aliases for device IDs.
    :type use_aliases: dict[str, str]
    :param enforce_strict_handling: Enforce strict handling of packets.
    :type enforce_strict_handling: bool
    :param use_native_ot: Preference for using native OpenTherm.
    :type use_native_ot: Literal["always", "prefer", "avoid",
        "never"] | None
    :param schema: Dictionary representing the schema.
    :type schema: dict[str, Any]
    :param debug_mode: If True, set the logger to debug mode.
    :type debug_mode: bool
    :param gateway_timeout: Custom timeout threshold in minutes.
    :type gateway_timeout: int | None
    :param database_path: Target disk path for the SQLite DB.
    :type database_path: str | None
    :param engine: Typed configuration object for the Transport layer.
    :type engine: EngineConfig
    """

    disable_discovery: bool = False
    enable_eavesdrop: bool = False
    reduce_processing: int = 0
    max_zones: int = 12
    use_aliases: dict[str, str] = field(default_factory=dict)
    enforce_strict_handling: bool = False
    use_native_ot: Literal["always", "prefer", "avoid", "never"] | None = None

    schema: dict[str, Any] = field(default_factory=dict)
    debug_mode: bool = False
    gateway_timeout: int | None = None
    database_path: str | None = "ramses.db"

    # Transport layer configuration encapsulated perfectly
    engine: EngineConfig = field(default_factory=EngineConfig)


class Gateway(GatewayInterface):
    """The gateway class.

    This class serves as the primary interface for the RAMSES RF network.
    It manages the serial connection (via ``Engine``), device discovery,
    schema maintenance, and message dispatching.
    """

    def __init__(
        self,
        port_name: str | None = None,
        *,
        config: GatewayConfig | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
        transport_constructor: Callable[..., Awaitable[RamsesTransportT]] | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the Gateway instance.

        :param port_name: The serial port name (e.g., '/dev/ttyUSB0')
            or None if using a file.
        :type port_name: str | None
        :param config: The typed configuration parameters.
        :type config: GatewayConfig | None, optional
        :param loop: The asyncio event loop to use.
        :type loop: asyncio.AbstractEventLoop | None, optional
        :param transport_constructor: A factory for creating transport.
        :type transport_constructor: Callable | None, optional
        :param kwargs: Catch-all for legacy keyword arguments.
        :type kwargs: Any
        """
        self._gwy_config = config or GatewayConfig()

        if port_name is not None:
            self._gwy_config.engine.port_name = port_name

        if kwargs:
            keys = list(kwargs.keys())
            _LOGGER.warning(
                "Gateway received legacy kwargs: %s. Please migrate "
                "ramses_cc to use the GatewayConfig object.",
                keys,
            )
            warnings.warn(
                f"Initializing Gateway with **kwargs {keys} is deprecated "
                "and will be removed in a future release. Please use "
                "GatewayConfig.",
                DeprecationWarning,
                stacklevel=2,
            )

            def _apply_kwargs(cfg_dict: dict[str, Any]) -> None:
                """Recursively unpack nested dictionaries to apply configs."""
                for key, value in cfg_dict.items():
                    if hasattr(self._gwy_config.engine, key):
                        setattr(self._gwy_config.engine, key, value)
                    elif hasattr(self._gwy_config, key):
                        setattr(self._gwy_config, key, value)
                    elif isinstance(value, dict):
                        _apply_kwargs(value)
                    else:
                        _LOGGER.error(
                            "Gateway received unsupported kwarg: %s. "
                            "This argument is ignored.",
                            key,
                        )

            _apply_kwargs(kwargs)

        if self._gwy_config.debug_mode:
            _LOGGER.setLevel(logging.DEBUG)

        self._engine = Engine(
            self._gwy_config.engine,
            loop=loop,
            transport_constructor=transport_constructor,
        )

        # Force the engine's protocol to use Gateway's message handler
        self._engine._set_msg_handler(self._msg_handler)

        if self._engine._disable_sending:
            self._gwy_config.disable_discovery = True

        if self._gwy_config.enable_eavesdrop:
            _LOGGER.warning(
                f"{SZ_ENABLE_EAVESDROP}=True: this is strongly discouraged "
                "for routine use (there be dragons here)"
            )

        self._schema: dict[str, Any] = SCH_GLOBAL_SCHEMAS(self._gwy_config.schema or {})

        self._tcs: Evohome | None = None

        self._device_registry: DeviceRegistryInterface = DeviceRegistry(self)

        self._device_filter: DeviceFilterInterface = DeviceFilter(
            include=cast(DeviceListT, self._engine._include),
            exclude=cast(DeviceListT, self._engine._exclude),
            unwanted=self._engine._unwanted,
            enforce_known_list=self._engine._enforce_known_list,
            hgi_id_provider=lambda: getattr(self.hgi, "id", None),
        )

        self._message_store: MessageStoreInterface | None = None
        self._pkt_log_listener: QueueListener | None = None

        self._prev_msg: ApplicationMessage | None = None
        self._this_msg: ApplicationMessage | None = None
        self._history_lock = threading.Lock()

        # 1. Controller Knowledge Bridge
        def is_controller(device_id: str) -> bool:
            """Provide ramses_tx with domain knowledge safely.

            :param device_id: The string device ID (e.g., '01:145038').
            :type device_id: str
            :returns: True if the device is a controller or unknown,
                False otherwise.
            :rtype: bool
            """
            # HACK: Preserve legacy bug-compatibility for snapshot tests.
            # ramses_tx previously assumed UFCs (02:) were controllers
            # because its Address object lacked domain knowledge and
            # defaulted to True.
            if device_id.startswith("02:"):
                return True

            dev = self._device_registry.device_by_id.get(cast(DeviceIdT, device_id))

            if dev:
                return getattr(dev, "_is_controller", True)
            return True

        rf_msg._IS_CONTROLLER_CB = is_controller

    def __repr__(self) -> str:
        """Return a string representation of the Gateway.

        :returns: A string describing the gateway's input source
            (port or file).
        :rtype: str
        """
        if not self._engine.ser_name:
            return f"Gateway(input_file={self._engine._input_file})"
        return (
            f"Gateway(port_name={self._engine.ser_name}, "
            f"port_config={self._engine._port_config})"
        )

    @property
    def device_registry(self) -> DeviceRegistryInterface:
        """Return the Device Registry service.

        :returns: The instantiated DeviceRegistryInterface.
        :rtype: DeviceRegistryInterface
        """
        return self._device_registry

    @property
    def config(self) -> GatewayConfig:
        """Return the gateway configuration.

        :returns: The configuration object for this gateway.
        :rtype: GatewayConfig
        """
        return self._gwy_config

    @property
    def message_store(self) -> MessageStoreInterface | None:
        """Return the message database if configured.
        ...
        """
        return self._message_store

    @message_store.setter
    def message_store(self, value: MessageStoreInterface | None) -> None:
        """Set the message database.
        ...
        """
        self._message_store = value

    @property
    def hgi(self) -> HgiGateway | None:
        """Return the active HGI80-compatible gateway device, if known.

        :returns: The active HGI gateway device if found, else None.
        :rtype: HgiGateway | None
        """
        if not self._engine._transport:
            return None
        if device_id := self._engine._transport.get_extra_info(SZ_ACTIVE_HGI):
            return self.device_registry.device_by_id.get(device_id)
        return None

    def update_message_history(self, msg: ApplicationMessage) -> None:
        """Update the message history in a thread-safe manner."""
        with self._history_lock:
            self._prev_msg = self._this_msg
            self._this_msg = msg

    def clear_message_history(self) -> None:
        """Clear the message history in a thread-safe manner."""
        with self._history_lock:
            self._prev_msg = None
            self._this_msg = None

    async def start(
        self,
        /,
        *,
        start_discovery: bool = True,
        cached_packets: dict[str, dict[str, Any] | str] | None = None,
    ) -> None:
        """Start the Gateway and Initiate discovery as required.

        This method initializes packet logging, the SQLite index, loads
        the schema, and optionally restores state from cached packets
        before starting the transport.

        :param start_discovery: Whether to initiate the discovery
            process after start, defaults to True.
        :type start_discovery: bool, optional
        :param cached_packets: A dictionary of packet strings used to
            restore state, defaults to None.
        :type cached_packets: dict[str, dict[str, Any] | str] | None,
            optional
        :returns: None
        :rtype: None
        """

        def initiate_discovery(dev_list: list[Device], sys_list: list[Evohome]) -> None:
            """Initiate polling discovery on devices and systems.

            :param dev_list: List of devices to discover.
            :type dev_list: list[Device]
            :param sys_list: List of systems to discover.
            :type sys_list: list[Evohome]
            :returns: None
            :rtype: None
            """
            _LOGGER.debug("Engine: Initiating/enabling discovery...")

            # Routing to components
            for device in dev_list:
                device.discovery.start_poller()

            for system in sys_list:
                system.discovery.start_poller()
                for zone in system.zones:
                    zone.discovery.start_poller()
                if system.dhw:
                    system.dhw.discovery.start_poller()

        _, self._pkt_log_listener = await set_pkt_logging_config(
            cc_console=(self.config.reduce_processing >= DONT_CREATE_MESSAGES),
            **self._engine._packet_log,
        )  # type: ignore[arg-type]

        if self._pkt_log_listener:
            self._pkt_log_listener.start()

            pkt_log_config = cast("dict[str, Any]", self._engine._packet_log)
            if flush_interval := pkt_log_config.get("flush_interval", 0):

                async def _periodic_flush() -> None:
                    """Periodically flush the packet log."""
                    try:
                        while True:
                            await asyncio.sleep(flush_interval)
                            await self._engine._loop.run_in_executor(
                                None, flush_packet_log, self._pkt_log_listener
                            )
                    except asyncio.CancelledError:
                        pass

                self.add_task(self._engine._loop.create_task(_periodic_flush()))

        _LOGGER.info("Ramses RF starts central MessageStore")
        self.create_sqlite_message_index()

        # temporarily turn on discovery, remember original state
        self.config.disable_discovery, disable_discovery = (
            True,
            self.config.disable_discovery,
        )

        load_schema(
            self, known_list=self._engine._include, **self._schema
        )  # create faked too

        # Restored cache *before* starting the engine to ensure historic
        # state is reconstructed correctly before live RF packets arrive.
        if cached_packets:
            await self._restore_cached_packets(cached_packets)

        await self._engine.start()

        # reset discovery to original state
        self.config.disable_discovery = disable_discovery

        if (
            not self._engine._disable_sending
            and not self.config.disable_discovery
            and start_discovery
        ):
            initiate_discovery(
                self.device_registry.devices, self.device_registry.systems
            )

    def create_sqlite_message_index(self) -> None:
        """Initialize the SQLite MessageStore.

        :returns: None
        :rtype: None
        """
        self._message_store = MessageStore(
            disk_path=self.config.database_path
        )  # start the index

    async def stop(self) -> None:
        """Stop the Gateway and tidy up.

        Stops the message database and the underlying engine/transport.

        :returns: None
        :rtype: None
        """
        # Disable discovery to prevent the domain layer spawning new tasks
        self.config.disable_discovery = True

        # Stop the Engine first to ensure no tasks/callbacks try to write
        # to the DB while we are closing it.
        await self._engine.stop()

        if self._pkt_log_listener:

            def _stop_listener(listener: QueueListener) -> None:
                """Stop the listener and close its handlers synchronously."""
                listener.stop()
                # Close handlers to ensure files are flushed/closed
                for handler in listener.handlers:
                    handler.close()

            await self._engine._loop.run_in_executor(
                None, _stop_listener, self._pkt_log_listener
            )
            self._pkt_log_listener = None

        if self._message_store:
            self._message_store.stop()

    async def _pause(self, *args: Any) -> None:
        """Pause the (unpaused) gateway (disables sending/discovery).

        There is the option to save other objects, as `args`.

        :param args: Additional objects/state to save during the pause.
        :type args: Any
        :returns: None
        :rtype: None
        :raises RuntimeError: If the engine fails to pause.
        """
        _LOGGER.debug("Gateway: Pausing engine...")

        self.config.disable_discovery, disc_flag = (
            True,
            self.config.disable_discovery,
        )

        try:
            await self._engine._pause(disc_flag, *args)
        except RuntimeError:
            self.config.disable_discovery = disc_flag
            raise

    async def _resume(self) -> tuple[Any, ...]:
        """Resume the (paused) gateway (enables sending/discovery, if
        applicable).

        Will restore other objects, as `args`.

        :returns: A tuple of arguments saved during the pause.
        :rtype: tuple[Any, ...]
        """
        args: tuple[Any, ...]

        _LOGGER.debug("Gateway: Resuming engine...")

        self.config.disable_discovery, *args = (
            await self._engine._resume()  # type: ignore[assignment]
        )

        return tuple(args)

    async def get_state(
        self, include_expired: bool = False
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return the current schema & state (may include expired packets).

        :param include_expired: If True, include expired packets.
        :type include_expired: bool, optional
        :returns: A tuple containing the schema dict and packet log dict.
        :rtype: tuple[dict[str, Any], dict[str, Any]]
        """
        await self._pause()

        def wanted_msg(msg: Message, include_expired: bool = False) -> bool:
            """Determine if a message is wanted for state reconstruction.

            :param msg: The message to evaluate.
            :type msg: Message
            :param include_expired: Whether to include expired messages,
                defaults to False.
            :type include_expired: bool, optional
            :returns: True if the message should be kept, otherwise False.
            :rtype: bool
            """
            if msg.code == Code._313F:
                # usu. expired, useful 4 back-back restarts
                return msg.verb in (I_, RP)
            if getattr(msg, "_expired", False) and not include_expired:
                return False
            if msg.code == Code._0404:
                return msg.verb in (I_, W_) and msg.len > 7
            if msg.verb in (W_, RQ):
                return False
            # if msg.code == Code._1FC9 and msg.verb != RP:
            #     return True
            return include_expired or not getattr(msg, "_expired", False)

        pkts: dict[str, Any] = {}
        if self.message_store:
            all_msgs = await self.message_store.all(include_expired=True)
            for i, msg in enumerate(all_msgs):
                if wanted_msg(msg, include_expired=include_expired):
                    dtm_str = msg.dtm.isoformat(timespec="microseconds")
                    pkts[dtm_str] = msg.dto.source_packets[0].__dict__
                if i > 0 and i % 100 == 0:
                    await asyncio.sleep(0)

        await self._resume()

        return await self.schema(), dict(sorted(pkts.items()))

    async def _restore_cached_packets(
        self, packets: dict[str, dict[str, Any] | str], _clear_state: bool = False
    ) -> None:
        """Restore cached packets (may include expired packets).

        This process uses a temporary transport to replay the packet
        history into the message handler. Converts DTOs back to strings
        to satisfy legacy requirements in `transport_factory`.

        :param packets: A dictionary of packet strings or DTO dicts.
        :type packets: dict[str, dict[str, Any] | str]
        :param _clear_state: If True, reset internal state before
            restoration (for testing), defaults to False.
        :type _clear_state: bool, optional
        :returns: None
        :rtype: None
        """

        def clear_state() -> None:
            """Clear existing internal schema and state records.

            :returns: None
            :rtype: None
            """
            _LOGGER.info("Gateway: Clearing existing schema/state...")

            self._tcs = None
            self.device_registry.devices.clear()
            self.device_registry.device_by_id.clear()
            self.clear_message_history()

        _LOGGER.debug("Gateway: Restoring a cached packet log...")
        await self._pause()

        if _clear_state:  # only intended for test suite use
            clear_state()

        # We do not always enforce the known_list whilst restoring a cache
        # because if it does not contain a correctly configured HGI, a
        # 'working' address is used (which could be different to the
        # address in the cache) & wanted packets can be dropped
        # unnecessarily.

        enforce_include_list = bool(
            self._engine._enforce_known_list
            and extract_known_hgi_id(
                self._engine._include,
                disable_warnings=True,
                strict_checking=True,
            )
        )

        tmp_protocol = protocol_factory(
            self._msg_handler,
            disable_sending=True,
            enforce_include_list=enforce_include_list,
            exclude_list=self._engine._exclude,
            include_list=self._engine._include,
        )

        # The actual HGI address will be discovered when the actual
        # transport was/is started up (usually before now)

        cutoff_dtm = dt.now() - td(hours=1)

        for i, (dtm, state) in enumerate(packets.items()):
            if i > 0 and i % 100 == 0:
                await asyncio.sleep(0)

            try:
                clean_dtm = dtm.replace("Z", "+00:00")
                pkt_dtm = dt.fromisoformat(clean_dtm)
                is_old = pkt_dtm < cutoff_dtm
            except ValueError:
                is_old = False

            if is_old:
                is_match = False
                if isinstance(state, dict) and "code" in state:
                    is_match = state["code"] in HIGH_VOLUME_STATUS_CODES
                else:
                    frame_str = (
                        state.get("frame", "")
                        if isinstance(state, dict)
                        else str(state)
                    )
                    is_match = any(
                        f" {c} " in frame_str for c in HIGH_VOLUME_STATUS_CODES
                    )

                if is_match:
                    continue

            try:
                pkt = Packet.from_dict(dtm, state)
                tmp_protocol.pkt_received(pkt)
            except Exception as err:
                _LOGGER.debug("Gateway: Failed to restore packet %s: %s", dtm, err)

        _LOGGER.debug("Gateway: Restored, resuming")
        await self._resume()

    @property
    def tcs(self) -> Evohome | None:
        """Return the primary Temperature Control System (TCS), if any.

        :returns: The primary Evohome system or None.
        :rtype: Evohome | None
        """

        if self._tcs is None and self.device_registry.systems:
            self._tcs = self.device_registry.systems[0]
        return self._tcs

    async def _config(self) -> dict[str, Any]:
        """Return the working configuration.

        :returns: A dictionary containing the current configuration state.
        :rtype: dict[str, Any]
        """
        return {
            "_gateway_id": self.hgi.id if self.hgi else None,
            SZ_MAIN_TCS: self.tcs.id if self.tcs else None,
            SZ_CONFIG: {SZ_ENFORCE_KNOWN_LIST: self.config.engine.enforce_known_list},
            SZ_KNOWN_LIST: await self.device_registry.known_list(),
            SZ_BLOCK_LIST: [
                {k: v} for k, v in (self.config.engine.block_list or {}).items()
            ],
            "_unwanted": sorted(self._engine._unwanted),
        }

    async def schema(self) -> dict[str, Any]:
        """Return the global schema.

        :returns: A dictionary representing the global system schema.
        :rtype: dict[str, Any]
        """

        schema: dict[str, Any] = {SZ_MAIN_TCS: self.tcs.ctl.id if self.tcs else None}

        for tcs in self.device_registry.systems:
            schema[tcs.ctl.id] = await tcs.schema()

        schema[f"{SZ_ORPHANS}_heat"] = await self.device_registry.get_heat_orphans()
        schema[f"{SZ_ORPHANS}_hvac"] = await self.device_registry.get_hvac_orphans()

        return schema

    async def params(self) -> dict[str, Any]:
        """Return the parameters for all devices.

        :returns: A dictionary containing parameters for all devices.
        :rtype: dict[str, Any]
        """
        return await self.device_registry.params()

    async def status(self) -> dict[str, Any]:
        """Return the status for all devices and the transport rate.

        :returns: A dictionary containing device statuses and the
            transport transmission rate.
        :rtype: dict[str, Any]
        """
        status_dict = await self.device_registry.status()
        tx_rate = (
            self._engine._transport.get_extra_info("tx_rate")
            if self._engine._transport
            else None
        )
        status_dict["_tx_rate"] = tx_rate
        return status_dict

    async def _msg_handler(self, dto: PacketDTO) -> None:
        """A callback to handle messages from the protocol stack.

        :param dto: The data transfer object to be handled.
        :type dto: PacketDTO
        :returns: None
        :rtype: None
        """
        # 1. Promote the raw DTO to an ApplicationMessage subclass
        app_msg = ApplicationMessage.from_dto(dto)

        # 2. Attach the gateway/engine context (so ._expired works correctly)
        app_msg.set_gateway(self._engine)

        # 3. Restore the critical ramses_rf linkage for dynamic resolution
        app_msg.bind_context(self)  # noqa: B010

        # 4. Store it safely in the engine state
        self.update_message_history(app_msg)

        # TODO: ideally remove this feature...
        assert self._this_msg  # mypy check

        if self._prev_msg and detect_array_fragment(
            self._this_msg,
            self._prev_msg,
        ):
            app_msg._force_has_array()
            app_msg._payload = self._prev_msg.payload + (
                app_msg.payload
                if isinstance(app_msg.payload, list)
                else [app_msg.payload]
            )

        # Ensure the downstream application gets the extended subclass
        await process_msg(self, app_msg)

    def add_msg_handler(
        self,
        msg_handler: Callable[[PacketDTO], Awaitable[None]],
        /,
        *,
        msg_filter: Callable[[PacketDTO], bool] | None = None,
    ) -> Callable[[], None]:
        """Add a Message handler to the underlying Protocol.

        :param msg_handler: The message handler callback.
        :type msg_handler: Callable[[Message], Awaitable[None]]
        :param msg_filter: An optional filter to only handle specific
            messages.
        :type msg_filter: Callable[[Message], bool] | None, optional
        :returns: A callable to remove the handler.
        :rtype: Callable[[], None]
        """
        return self._engine.add_msg_handler(msg_handler, msg_filter=msg_filter)

    def add_task(self, task: asyncio.Task[Any]) -> None:
        """Add a task to the engine's task list.

        :param task: The asyncio Task to track.
        :type task: asyncio.Task[Any]
        :returns: None
        :rtype: None
        """
        self._engine.add_task(task)

    @staticmethod
    def create_cmd(
        verb: VerbT,
        device_id: DeviceIdT,
        code: Code,
        payload: PayloadT,
        **kwargs: Any,
    ) -> Command:
        """Make a command addressed to device_id.

        :param verb: The command verb (e.g. RQ, W).
        :type verb: str
        :param device_id: The target device identifier.
        :type device_id: str
        :param code: The code representing the command.
        :type code: Code | str
        :param payload: The payload of the command.
        :type payload: str
        :param kwargs: Additional arguments for the command generation.
        :type kwargs: Any
        :returns: The created Command instance.
        :rtype: Command
        """
        return Engine.create_cmd(
            verb,
            device_id,
            code,
            payload,
            **kwargs,
        )

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
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> asyncio.Task[Packet]:
        """Wrapper to schedule an async_send_cmd() and return the Task.

        Commands are queued and sent FIFO, except higher-priority
        Commands are always sent first.

        :param cmd: The command object to send.
        :type cmd: Command
        :param gap_duration: The gap between repeats (in seconds),
            defaults to DEFAULT_GAP_DURATION.
        :type gap_duration: float, optional
        :param num_repeats: Number of times to repeat the command
            (0 = once, 1 = twice, etc.), defaults to
            DEFAULT_NUM_REPEATS.
        :type num_repeats: int, optional
        :param priority: The priority of the command, defaults to
            Priority.DEFAULT.
        :type priority: Priority, optional
        :param timeout: Time to wait for a send to complete, defaults
            to DEFAULT_SEND_TIMEOUT.
        :type timeout: float, optional
        :param wait_for_reply: Whether to wait for a reply packet,
            defaults to DEFAULT_WAIT_FOR_REPLY.
        :type wait_for_reply: bool | None, optional
        :param max_retries: Maximum number of retries if sending fails,
            defaults to DEFAULT_MAX_RETRIES.
        :type max_retries: int, optional
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
            max_retries=max_retries,
        )

        task = self._engine._loop.create_task(coro)

        def _clear_exc(fut: asyncio.Task[Any]) -> None:
            """Silently clear unhandled exceptions on background tasks."""
            if not fut.cancelled() and fut.exception():
                _LOGGER.debug("Background task failed: %s", fut.exception())

        task.add_done_callback(_clear_exc)
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
        timeout: float = DEFAULT_SEND_TIMEOUT,
        wait_for_reply: bool | None = DEFAULT_WAIT_FOR_REPLY,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> Packet:
        """Send a Command and return the corresponding (echo or reply)
        Packet.

        If wait_for_reply is True (*and* the Command has a rx_header),
        return the reply Packet. Otherwise, simply return the echo
        Packet.

        :param cmd: The command object to send.
        :type cmd: Command
        :param gap_duration: The gap between repeats (in seconds),
            defaults to DEFAULT_GAP_DURATION.
        :type gap_duration: float, optional
        :param num_repeats: Number of times to repeat the command,
            defaults to DEFAULT_NUM_REPEATS.
        :type num_repeats: int, optional
        :param priority: The priority of the command, defaults to
            Priority.DEFAULT.
        :type priority: Priority, optional
        :param max_retries: Maximum number of retries if sending fails,
            defaults to DEFAULT_MAX_RETRIES.
        :type max_retries: int, optional
        :param timeout: Time to wait for the command to send, defaults to
            DEFAULT_SEND_TIMEOUT.
        :type timeout: float, optional
        :param wait_for_reply: Whether to wait for a reply packet,
            defaults to DEFAULT_WAIT_FOR_REPLY.
        :type wait_for_reply: bool | None, optional
        :returns: The echo packet or reply packet depending on
            wait_for_reply.
        :rtype: Packet
        :raises ProtocolSendFailed: If the command was sent but no
            reply/echo was received.
        :raises ProtocolError: If the system failed to attempt the
            transmission.
        """
        try:
            return await self._engine.async_send_cmd(
                cmd,
                gap_duration=gap_duration,
                num_repeats=num_repeats,
                priority=priority,
                max_retries=max_retries,
                timeout=timeout,
                wait_for_reply=wait_for_reply,
            )
        except ProtocolSendFailed as err:
            if (
                self.config.disable_discovery
                or self._engine._disable_sending
                or "Inactive" in str(err)
            ):
                raise asyncio.CancelledError(
                    f"Gateway shutting down, suppressed teardown leak: {err}"
                ) from err
            raise


# Schema & Routing Bridges
def get_code_name(code: str) -> str:
    """Provide ramses_tx with human-readable code names for logging."""
    if code in CODES_SCHEMA:
        return str(CODES_SCHEMA[Code(code)].get("name", f"unknown_{code}"))
    return f"unknown_{code}"


rf_msg._GET_CODE_NAME_CB = get_code_name


def get_msg_idx(msg: Any) -> dict[str, str]:
    """Provide ramses_tx with the semantic zone_idx/domain_id routing."""
    rf_msg._GET_MSG_IDX_CB = None
    idx_result = msg._idx
    rf_msg._GET_MSG_IDX_CB = get_msg_idx
    return cast("dict[str, str]", idx_result)


rf_msg._GET_MSG_IDX_CB = get_msg_idx
